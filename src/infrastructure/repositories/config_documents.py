"""Documento de configuração -> entidade de domínio, igual para os dois backends (``CONFIG_STORE``).

O Mongo (coleções ``agents_config``, ``teams_config``, ``tools``) e o YAML (``CONFIG_YAML_PATH``,
seções ``agents``, ``teams``, ``tools``) guardam o mesmo formato de documento e passam pelas mesmas
funções: mesmo resultado, mesmas recusas (validação nas entidades). Também ficam aqui os logs comuns
de documento inválido e de chave camelCase ignorada, a regra de uma tool por id e o caminho de volta
da escrita (FA-01): entidade -> documento em snake_case e a ``version`` do documento.

Dois modos de leitura. O padrão é tolerante, para documento já gravado continuar carregando: provider
ausente vira ``ollama``, lista de ids suja perde os itens que não são texto (com log), método HTTP ou
tipo de parâmetro que não é texto vira o default e os demais campos chegam à entidade como vieram. O
``strict=True`` é o da escrita pela API admin (FA-02): o que a leitura coage ou descarta vira
``InvalidField`` com o campo (e as posições, nas listas), sem log e sem o valor. Isso inclui a chave
que o mapper não lê (``promt``, ``apiKeyRef``), recusada com o nome dela (``_known_keys``).
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Iterator, Mapping
from enum import Enum
from itertools import islice
from typing import Any, TypeVar

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import (
    DEFAULT_EMBEDDER_MODEL,
    DEFAULT_EMBEDDER_PROVIDER,
    RagConfig,
    SearchStrategy,
)
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.entities.validation import InvalidField
from src.domain.ports import ILogger

# Documento cru (sem schema): os valores só são validados pelas entidades.
Document = Mapping[str, Any]

# Entidade de config (o tipo de cada um dos três repositórios).
ConfigEntityT = TypeVar("ConfigEntityT", AgentConfig, TeamConfig, Tool)
EnumT = TypeVar("EnumT", bound=Enum)
MappedT = TypeVar("MappedT")

# Erros do mapeamento documento -> entidade (campo ausente, tipo errado, valor fora do enum).
INVALID_DOCUMENT_ERRORS = (ValueError, TypeError, AttributeError, KeyError)

# Grafia camelCase dos campos novos (F2-01): o mapper só lê snake_case; estas são ignoradas na leitura
# (com aviso) e recusadas na escrita estrita, como toda chave que o mapper não lê.
IGNORED_CAMEL_CASE_KEYS = ("apiKeyRef", "baseUrl", "modelParams")

# Chave desconhecida só volta pelo nome (no 422 e no log) se for um identificador curto; senão, o rótulo.
_NAMEABLE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_UNKNOWN_KEY_FIELD = "unknown_key"

# Teto de posições citadas no log de itens descartados de uma lista de ids (a contagem vai inteira).
MAX_LOGGED_ITEM_POSITIONS = 10

# Provider do documento legado sem ``factory_ia_model`` (só na leitura tolerante).
LEGACY_PROVIDER = "ollama"
_OPTIONAL_TEXT = (str, type(None))


def is_active(doc: object) -> bool:
    """Entra nas listagens: ``active`` é o booleano ``True`` (como a consulta ``{"active": True}`` do Mongo)."""
    return isinstance(doc, Mapping) and doc.get("active") is True


def document_version(doc: Document) -> int:
    """``version`` do documento (FA-01); ausente ou nula (legado) é 0.

    Outro valor que não um inteiro >= 0 invalida o documento (``ValueError``), em vez de virar 0: uma
    versão inventada faria toda escrita nele conflitar sem motivo visível. Só a listagem da escrita
    (``list_all``) aplica a regra: o runtime (``get_active_*``, busca por id) segue servindo o
    documento, que some só do admin (com log) até a ``version`` ser corrigida no banco.
    """
    version = doc.get("version")
    if version is None:
        return 0
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("version do documento deve ser inteiro >= 0")
    return version


def entity_document(entity: ConfigEntityT) -> dict[str, Any]:
    """Entidade -> documento em snake_case, o formato que os ``map_*_document`` leem de volta."""
    return {key: _plain(value) for key, value in dataclasses.asdict(entity).items()}


def _plain(value: Any) -> Any:
    """Só dict/list/escalares: enum vira o valor, mapeamento vira dict e tupla vira lista."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def text_id(doc: object) -> str | None:
    """``id`` do documento, se for texto; outro tipo não vai ao log nem casa com busca por id."""
    raw = doc.get("id") if isinstance(doc, Mapping) else None
    return raw if isinstance(raw, str) else None


class _ReadKeys(Mapping[str, Any]):
    """Documento que anota cada chave que o mapper consulta (``get``, ``in`` e ``[]``)."""

    def __init__(self, data: Document) -> None:
        self._data = data
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


def _known_keys(data: Document, strict: bool, build: Callable[[Document], MappedT]) -> MappedT:
    """``build(data)``; no modo estrito, a primeira chave que ``build`` não leu é ``InvalidField``.

    As chaves aceitas são as que o próprio mapper consulta (inclusive as grafias legadas), sem lista
    paralela: a escrita não grava em silêncio um documento sem o campo que veio com outro nome. A chave
    é do cliente: fora de ``_NAMEABLE_KEY`` (quebra de linha, escape, texto enorme), o campo é o rótulo
    ``_UNKNOWN_KEY_FIELD``, para não forjar campo de log nem ecoar o texto.
    """
    if not strict:
        return build(data)
    tracked = _ReadKeys(data)
    built = build(tracked)
    unread = next((key for key in data if key not in tracked.read), None)
    if unread is not None:
        field = unread if _NAMEABLE_KEY.fullmatch(unread) else _UNKNOWN_KEY_FIELD
        raise InvalidField(field, "chave que o documento de config não tem")
    return built


def _of_type(value: Any, field: str, kind: type | tuple[type, ...], strict: bool) -> Any:
    """``value`` como veio; no modo estrito, outro tipo que não ``kind`` é ``InvalidField``."""
    if strict and not isinstance(value, kind):
        raise InvalidField(field, f"{field} com tipo inválido")
    return value


def _enum_member(enum: type[EnumT], raw: object, field: str, strict: bool, fallback: EnumT) -> EnumT:
    """Membro pelo valor ou pelo nome (sem caixa); texto desconhecido é ``InvalidField``.

    Na leitura tolerante, o que não é texto vira ``fallback``; no modo estrito, também é recusado.
    """
    try:
        return enum(raw)
    except ValueError:
        pass
    if isinstance(raw, str) and raw.upper() in enum.__members__:
        return enum[raw.upper()]
    if strict or isinstance(raw, str):
        raise InvalidField(field, f"{field} fora dos valores aceitos")
    return fallback


def _text_ids(
    value: object, field: str, id_field: str, doc: Document, logger: ILogger, *, strict: bool = False
) -> list[str]:
    """Ids de texto de ``tools_ids``/``member_ids``; o resto é descartado com log, sem invalidar o documento.

    Documento que já existia com a lista suja continua carregando, degradado: o que não é lista vira
    lista vazia e o item que não é texto sai (as entidades não conferem o conteúdo da lista: um mapa
    ou número só quebraria, ou casaria errado, na montagem). Um log só por campo por documento (uma
    lista enorme não vira um log por item): id do documento, campo e o tipo recebido ou a contagem
    dos descartados com as primeiras ``MAX_LOGGED_ITEM_POSITIONS`` posições (a partir de 1); nunca o valor.
    No modo estrito, a lista suja é ``InvalidField`` (com as posições recusadas, a partir de 0), sem log.
    """
    if not isinstance(value, list):
        if strict:
            raise InvalidField(field, f"{field} deve ser uma lista de ids")
        logger.error(
            "Lista de ids que não é lista ignorada",
            **{id_field: text_id(doc)},
            field=field,
            value_type=type(value).__name__,
        )
        return []
    dropped = [position for position, item in enumerate(value) if not isinstance(item, str)]
    if dropped and strict:
        raise InvalidField(field, f"{field}: todo id deve ser texto", dropped)
    if dropped:
        logger.error(
            "Itens que não são texto ignorados na lista de ids",
            **{id_field: text_id(doc)},
            field=field,
            dropped_count=len(dropped),
            item_positions=[position + 1 for position in islice(dropped, MAX_LOGGED_ITEM_POSITIONS)],
        )
    return [item for item in value if isinstance(item, str)]


def _headers(value: object) -> dict[str, str] | None:
    """``headers`` da tool: ``None`` ou mapa texto -> texto (vai como header HTTP)."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise InvalidField("headers", "headers da tool deve ser um mapa de texto para texto")
    return dict(value)


def map_rag_document(rag_data: Document, *, strict: bool = False) -> RagConfig:
    """``rag_config`` -> ``RagConfig``.

    Legado (sem ``model_params``/``base_url``/``api_key_ref``): provider/modelo ausentes
    caem nos defaults de sempre. Com algum campo novo, nada de default: endpoint e chave
    novos não podem parar num ollama/nomic implícito, então ``model`` e provider precisam
    estar no documento (senão o ``RagConfig`` recusa).
    """
    model_params = rag_data.get("model_params")
    base_url = rag_data.get("base_url")
    api_key_ref = rag_data.get("api_key_ref")
    legacy = model_params is None and base_url is None and api_key_ref is None
    model = rag_data.get("model", DEFAULT_EMBEDDER_MODEL if legacy else None)
    provider = rag_data.get(
        "factory_ia_model", rag_data.get("factoryIaModel", DEFAULT_EMBEDDER_PROVIDER if legacy else None)
    )
    try:
        search_strategy = SearchStrategy(rag_data.get("search_strategy", "semantic"))
    except ValueError:
        raise InvalidField("search_strategy", "search_strategy fora dos valores aceitos") from None
    return RagConfig(
        active=_of_type(rag_data.get("active", False), "active", bool, strict),
        doc_name=_of_type(rag_data.get("doc_name"), "doc_name", _OPTIONAL_TEXT, strict),
        model=_of_type(model, "model", _OPTIONAL_TEXT, strict),
        factory_ia_model=_of_type(provider, "factory_ia_model", _OPTIONAL_TEXT, strict),
        search_strategy=search_strategy,
        model_params=model_params,
        base_url=base_url,
        api_key_ref=api_key_ref,
    )


def _rag(rag_data: object, strict: bool) -> RagConfig | None:
    """``rag_config`` do agente; a recusa aponta o campo aninhado (``rag_config.<campo>``)."""
    if rag_data is None or (not rag_data and not strict):
        return None
    if not isinstance(rag_data, Mapping):
        raise InvalidField("rag_config", "rag_config deve ser um objeto")
    try:
        return _known_keys(rag_data, strict, lambda doc: map_rag_document(doc, strict=strict))
    except InvalidField as exc:
        raise InvalidField(f"rag_config.{exc.field}", str(exc)) from None


def map_agent_document(data: Document, logger: ILogger, *, strict: bool = False) -> AgentConfig:
    """Documento de agente -> ``AgentConfig`` (``factoryIaModel`` legado aceito).

    ``tools_ids`` sujo não invalida o agente: ele sobe sem as tools descartadas, com log (``_text_ids``).
    """
    return _known_keys(data, strict, lambda doc: _agent(doc, logger, strict))


def _agent(data: Document, logger: ILogger, strict: bool) -> AgentConfig:
    tools_ids = data.get("tools_ids", [])
    return AgentConfig(
        id=data.get("id", ""),
        nome=data.get("nome", ""),
        model=data.get("model", ""),
        factory_ia_model=data.get("factory_ia_model", data.get("factoryIaModel", None if strict else LEGACY_PROVIDER)),
        descricao=data.get("descricao", ""),
        prompt=_of_type(data.get("prompt", ""), "prompt", str, strict),
        active=_of_type(data.get("active", True), "active", bool, strict),
        tools_ids=(
            None if tools_ids is None else _text_ids(tools_ids, "tools_ids", "agent_id", data, logger, strict=strict)
        ),
        rag_config=_rag(data.get("rag_config"), strict),
        user_memory_active=_of_type(data.get("user_memory_active", False), "user_memory_active", bool, strict),
        summary_active=_of_type(data.get("summary_active", False), "summary_active", bool, strict),
        model_params=data.get("model_params"),
        base_url=data.get("base_url"),
        api_key_ref=data.get("api_key_ref"),
        # FA-07 (só snake_case): ausente é o default da entidade; o tipo e a faixa ela valida nos dois modos.
        markdown=data.get("markdown", AgentConfig.markdown),
        add_history_to_context=data.get("add_history_to_context", AgentConfig.add_history_to_context),
        num_history_runs=data.get("num_history_runs", AgentConfig.num_history_runs),
        add_datetime_to_context=data.get("add_datetime_to_context", AgentConfig.add_datetime_to_context),
        reasoning=data.get("reasoning", AgentConfig.reasoning),
    )


def map_team_document(data: Document, logger: ILogger, *, strict: bool = False) -> TeamConfig:
    """Documento de team -> ``TeamConfig``.

    Canônico (README e seed): ``factoryIaModel`` e o resto em snake_case
    (``member_ids``, ``user_memory_active``, ``summary_active``). Ainda lê o legado
    camelCase (``memberIds``, ``userMemoryActive``, ``summaryActive``) e
    ``factory_ia_model``; com as duas grafias no documento, vale a snake_case.
    Opcionais (F2-01, só snake_case): ``model_params``, ``base_url``, ``api_key_ref``; e (FA-07, só
    snake_case) ``markdown`` e ``add_history_to_context``.
    Item de ``member_ids`` que não é texto é descartado com log (``_text_ids``, que cita a chave
    lida); sem nenhum membro de texto, o ``TeamConfig`` recusa o documento, como sempre.
    """
    return _known_keys(data, strict, lambda doc: _team(doc, logger, strict))


def _team(data: Document, logger: ILogger, strict: bool) -> TeamConfig:
    members_key = "member_ids" if "member_ids" in data else "memberIds"
    return TeamConfig(
        id=data.get("id", ""),
        nome=data.get("nome", ""),
        model=data.get("model", ""),
        factory_ia_model=data.get("factory_ia_model", data.get("factoryIaModel", None if strict else LEGACY_PROVIDER)),
        mode=data.get("mode", "route"),
        descricao=data.get("descricao"),
        prompt=_of_type(data.get("prompt"), "prompt", _OPTIONAL_TEXT, strict),
        member_ids=_text_ids(
            data.get("member_ids", data.get("memberIds", [])), members_key, "team_id", data, logger, strict=strict
        ),
        user_memory_active=_of_type(
            data.get("user_memory_active", data.get("userMemoryActive", True)), "user_memory_active", bool, strict
        ),
        summary_active=_of_type(
            data.get("summary_active", data.get("summaryActive", False)), "summary_active", bool, strict
        ),
        active=_of_type(data.get("active", True), "active", bool, strict),
        model_params=data.get("model_params"),
        base_url=data.get("base_url"),
        api_key_ref=data.get("api_key_ref"),
        # FA-07 (só snake_case): ausente é o default da entidade, que valida o tipo nos dois modos.
        markdown=data.get("markdown", TeamConfig.markdown),
        add_history_to_context=data.get("add_history_to_context", TeamConfig.add_history_to_context),
    )


def _tool_parameter(p: Any, strict: bool) -> ToolParameter:
    if strict and not isinstance(p, Mapping):
        raise InvalidField("parameters", "parâmetro da tool deve ser um objeto")
    return _known_keys(p, strict, lambda doc: _parameter(doc, strict))


def _parameter(p: Document, strict: bool) -> ToolParameter:
    return ToolParameter(
        name=_of_type(p.get("name"), "name", str, strict),
        type=_enum_member(ParameterType, p.get("type"), "type", strict, ParameterType.STRING),
        description=_of_type(p.get("description"), "description", str, strict),
        required=_of_type(p.get("required", False), "required", bool, strict),
        default_value=p.get("default_value"),
    )


def _tool_parameters(raw: Any, strict: bool) -> list[ToolParameter]:
    """``parameters`` da tool; a recusa de um parâmetro aponta a posição dele (a partir de 0)."""
    if strict and not isinstance(raw, list):
        raise InvalidField("parameters", "parameters da tool deve ser uma lista")
    parameters: list[ToolParameter] = []
    for position, p in enumerate(raw):
        try:
            parameters.append(_tool_parameter(p, strict))
        except InvalidField as exc:
            raise InvalidField("parameters", str(exc), [position]) from None
    return parameters


def map_tool_document(data: Document, *, strict: bool = False) -> Tool:
    """Documento de tool -> ``Tool`` (tipo de parâmetro e método HTTP pelo valor ou pelo nome)."""
    return _known_keys(data, strict, lambda doc: _tool(doc, strict))


def _tool(data: Document, strict: bool) -> Tool:
    route = data.get("route", "")
    if not isinstance(route, str):
        raise InvalidField("route", "route da tool deve ser texto")
    return Tool(
        id=data.get("id", ""),
        name=_of_type(data.get("name", ""), "name", str, strict),
        description=_of_type(data.get("description", ""), "description", str, strict),
        route=route,
        http_method=_enum_member(HttpMethod, data.get("http_method", "GET"), "http_method", strict, HttpMethod.GET),
        parameters=_tool_parameters(data.get("parameters", []), strict),
        instructions=_of_type(data.get("instructions", ""), "instructions", _OPTIONAL_TEXT, strict),
        headers=_headers(data.get("headers", {})),
        active=_of_type(data.get("active", True), "active", bool, strict),
    )


def log_invalid_document(
    logger: ILogger,
    message: str,
    id_field: str,
    doc: object,
    exc: BaseException,
    location: Mapping[str, object],
) -> None:
    """Log de documento ignorado: id (se for texto), onde ele está no backend e o tipo do erro.

    Nunca o documento nem o texto do erro: config pode carregar segredo ou PII.
    """
    logger.error(message, **{id_field: text_id(doc)}, **location, error_type=type(exc).__name__)


def warn_ignored_camel_case(
    logger: ILogger, id_field: str, doc: object, location: Mapping[str, object]
) -> None:
    """Aviso quando o documento usa ``apiKeyRef``/``baseUrl``/``modelParams`` (raiz ou ``rag_config``).

    As chaves seguem ignoradas (o documento carrega como antes), mas quem gravou achando que
    configurou endpoint/chave precisa saber. Cita só o id e os nomes das chaves, nunca valores.
    """
    if not isinstance(doc, Mapping):
        return
    rag = doc.get("rag_config")
    keys = [key for key in IGNORED_CAMEL_CASE_KEYS if key in doc]
    rag_keys = [key for key in IGNORED_CAMEL_CASE_KEYS if isinstance(rag, Mapping) and key in rag]
    if not keys and not rag_keys:
        return
    logger.warning(
        "Documento com chaves camelCase ignoradas; use model_params, base_url e api_key_ref",
        **{id_field: text_id(doc)},
        **location,
        keys=keys,
        rag_config_keys=rag_keys,
    )


def first_tool_per_id(tools: list[Tool], logger: ILogger) -> list[Tool]:
    """Uma tool por id, a primeira na ordem do backend; a repetida vira log de erro.

    Duas tools com o mesmo id dariam duas funções com o mesmo nome ao modelo. Agentes e teams não
    passam por aqui: o caso de uso já fica com o primeiro e loga o repetido (F1-10).
    """
    unique: dict[str, Tool] = {}
    for tool in tools:
        if tool.id in unique:
            logger.error("Tool com id repetido ignorada", tool_id=tool.id)
            continue
        unique[tool.id] = tool
    return list(unique.values())
