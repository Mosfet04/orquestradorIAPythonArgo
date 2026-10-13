"""Documento de configuração -> entidade de domínio, igual para os dois backends (``CONFIG_STORE``).

O Mongo (coleções ``agents_config``, ``teams_config``, ``tools``) e o YAML (``CONFIG_YAML_PATH``,
seções ``agents``, ``teams``, ``tools``) guardam o mesmo formato de documento e passam pelas mesmas
funções: mesmo resultado, mesmas recusas (validação nas entidades). Também ficam aqui os logs comuns
de documento inválido e de chave camelCase ignorada, e a regra de uma tool por id.
"""

from __future__ import annotations

from collections.abc import Mapping
from itertools import islice
from typing import Any

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import (
    DEFAULT_EMBEDDER_MODEL,
    DEFAULT_EMBEDDER_PROVIDER,
    RagConfig,
    SearchStrategy,
)
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import ILogger

# Documento cru (sem schema): os valores só são validados pelas entidades.
Document = Mapping[str, Any]

# Erros do mapeamento documento -> entidade (campo ausente, tipo errado, valor fora do enum).
INVALID_DOCUMENT_ERRORS = (ValueError, TypeError, AttributeError, KeyError)

# Grafia camelCase dos campos novos (F2-01): o mapper só lê snake_case; estas são ignoradas.
IGNORED_CAMEL_CASE_KEYS = ("apiKeyRef", "baseUrl", "modelParams")

# Teto de posições citadas no log de itens descartados de uma lista de ids (a contagem vai inteira).
MAX_LOGGED_ITEM_POSITIONS = 10


def is_active(doc: object) -> bool:
    """Entra nas listagens: ``active`` é o booleano ``True`` (como a consulta ``{"active": True}`` do Mongo)."""
    return isinstance(doc, Mapping) and doc.get("active") is True


def text_id(doc: object) -> str | None:
    """``id`` do documento, se for texto; outro tipo não vai ao log nem casa com busca por id."""
    raw = doc.get("id") if isinstance(doc, Mapping) else None
    return raw if isinstance(raw, str) else None


def _text_ids(value: object, field: str, id_field: str, doc: Document, logger: ILogger) -> list[str]:
    """Ids de texto de ``tools_ids``/``member_ids``; o resto é descartado com log, sem invalidar o documento.

    Documento que já existia com a lista suja continua carregando, degradado: o que não é lista vira
    lista vazia e o item que não é texto sai (as entidades não conferem o conteúdo da lista: um mapa
    ou número só quebraria, ou casaria errado, na montagem). Um log só por campo por documento (uma
    lista enorme não vira um log por item): id do documento, campo e o tipo recebido ou a contagem
    dos descartados com as primeiras ``MAX_LOGGED_ITEM_POSITIONS`` posições (a partir de 1); nunca o valor.
    """
    if not isinstance(value, list):
        logger.error(
            "Lista de ids que não é lista ignorada",
            **{id_field: text_id(doc)},
            field=field,
            value_type=type(value).__name__,
        )
        return []
    ids = [item for item in value if isinstance(item, str)]
    dropped_count = len(value) - len(ids)
    if dropped_count:
        dropped = (position for position, item in enumerate(value, start=1) if not isinstance(item, str))
        logger.error(
            "Itens que não são texto ignorados na lista de ids",
            **{id_field: text_id(doc)},
            field=field,
            dropped_count=dropped_count,
            item_positions=list(islice(dropped, MAX_LOGGED_ITEM_POSITIONS)),
        )
    return ids


def _headers(value: object) -> dict[str, str] | None:
    """``headers`` da tool: ``None`` ou mapa texto -> texto (vai como header HTTP)."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("headers da tool deve ser um mapa de texto para texto")
    return dict(value)


def map_rag_document(rag_data: Document) -> RagConfig:
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
    return RagConfig(
        active=rag_data.get("active", False),
        doc_name=rag_data.get("doc_name"),
        model=rag_data.get("model", DEFAULT_EMBEDDER_MODEL if legacy else None),
        factory_ia_model=rag_data.get(
            "factory_ia_model",
            rag_data.get("factoryIaModel", DEFAULT_EMBEDDER_PROVIDER if legacy else None),
        ),
        search_strategy=SearchStrategy(rag_data.get("search_strategy", "semantic")),
        model_params=model_params,
        base_url=base_url,
        api_key_ref=api_key_ref,
    )


def map_agent_document(data: Document, logger: ILogger) -> AgentConfig:
    """Documento de agente -> ``AgentConfig`` (``factoryIaModel`` legado aceito).

    ``tools_ids`` sujo não invalida o agente: ele sobe sem as tools descartadas, com log (``_text_ids``).
    """
    rag_data = data.get("rag_config")
    tools_ids = data.get("tools_ids", [])
    return AgentConfig(
        id=data.get("id", ""),
        nome=data.get("nome", ""),
        model=data.get("model", ""),
        factory_ia_model=data.get("factory_ia_model", data.get("factoryIaModel", "ollama")),
        descricao=data.get("descricao", ""),
        prompt=data.get("prompt", ""),
        active=data.get("active", True),
        tools_ids=None if tools_ids is None else _text_ids(tools_ids, "tools_ids", "agent_id", data, logger),
        rag_config=map_rag_document(rag_data) if rag_data else None,
        user_memory_active=data.get("user_memory_active", False),
        summary_active=data.get("summary_active", False),
        model_params=data.get("model_params"),
        base_url=data.get("base_url"),
        api_key_ref=data.get("api_key_ref"),
    )


def map_team_document(data: Document, logger: ILogger) -> TeamConfig:
    """Documento de team -> ``TeamConfig``.

    Canônico (README e seed): ``factoryIaModel`` e o resto em snake_case
    (``member_ids``, ``user_memory_active``, ``summary_active``). Ainda lê o legado
    camelCase (``memberIds``, ``userMemoryActive``, ``summaryActive``) e
    ``factory_ia_model``; com as duas grafias no documento, vale a snake_case.
    Opcionais (F2-01, só snake_case): ``model_params``, ``base_url``, ``api_key_ref``.
    Item de ``member_ids`` que não é texto é descartado com log (``_text_ids``, que cita a chave
    lida); sem nenhum membro de texto, o ``TeamConfig`` recusa o documento, como sempre.
    """
    members_key = "member_ids" if "member_ids" in data else "memberIds"
    return TeamConfig(
        id=data.get("id", ""),
        nome=data.get("nome", ""),
        model=data.get("model", ""),
        factory_ia_model=data.get("factory_ia_model", data.get("factoryIaModel", "ollama")),
        mode=data.get("mode", "route"),
        descricao=data.get("descricao"),
        prompt=data.get("prompt"),
        member_ids=_text_ids(data.get(members_key, []), members_key, "team_id", data, logger),
        user_memory_active=data.get("user_memory_active", data.get("userMemoryActive", True)),
        summary_active=data.get("summary_active", data.get("summaryActive", False)),
        active=data.get("active", True),
        model_params=data.get("model_params"),
        base_url=data.get("base_url"),
        api_key_ref=data.get("api_key_ref"),
    )


def map_tool_document(data: Document) -> Tool:
    """Documento de tool -> ``Tool`` (tipo de parâmetro e método HTTP pelo valor ou pelo nome)."""
    parameters: list[ToolParameter] = []
    for p in data.get("parameters", []):
        raw_type = p.get("type")
        try:
            ptype = ParameterType(raw_type)
        except (ValueError, KeyError):
            ptype = ParameterType[raw_type.upper()] if isinstance(raw_type, str) else ParameterType.STRING
        parameters.append(
            ToolParameter(
                name=p.get("name"),
                type=ptype,
                description=p.get("description"),
                required=p.get("required", False),
                default_value=p.get("default_value"),
            )
        )

    raw_method = data.get("http_method", "GET")
    try:
        method = HttpMethod(raw_method)
    except (ValueError, KeyError):
        method = HttpMethod[raw_method.upper()] if isinstance(raw_method, str) else HttpMethod.GET

    route = data.get("route", "")
    if not isinstance(route, str):
        raise ValueError("route da tool deve ser texto")
    return Tool(
        id=data.get("id", ""),
        name=data.get("name", ""),
        description=data.get("description", ""),
        route=route,
        http_method=method,
        parameters=parameters,
        instructions=data.get("instructions", ""),
        headers=_headers(data.get("headers", {})),
        active=data.get("active", True),
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
