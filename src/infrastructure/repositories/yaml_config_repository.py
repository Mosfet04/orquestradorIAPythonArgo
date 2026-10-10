"""Config de agentes, teams e tools num arquivo YAML (``CONFIG_STORE=yaml`` + ``CONFIG_YAML_PATH``).

Implementa as mesmas portas dos repositórios Mongo (``IAgentConfigRepository``,
``ITeamConfigRepository``, ``IToolRepository``) com o mesmo mapper e a mesma validação
(``config_documents``). Formato: um mapeamento com as seções ``agents``, ``teams`` e ``tools``
(todas opcionais), cada uma uma lista de documentos no formato das coleções do Mongo.

Semântica igual à do Mongo (suíte de ``tests/contract``): a ordem do arquivo faz o papel da ordem
por ``_id``; só ``active: true`` entra nas listagens; documento inválido isola só ele (log com id,
``position`` = posição na seção a partir de 1, e tipo do erro); id repetido: o primeiro.

Leitura: ``yaml`` com o loader seguro (``CSafeLoader`` quando o PyYAML tem a libyaml; sem tags de
objeto Python), fora do event loop. O arquivo é relido só quando muda (``st_ino``, ``st_size`` e
``st_mtime_ns`` de ``os.stat``, que segue symlink: a troca de um ConfigMap invalida); consultas
concorrentes esperam um parse só. Sem watcher nem hot-reload: a config é consultada no startup e no
``POST /admin/refresh-cache``. Recusados, com ``YamlConfigError`` (mensagem nossa, só linha/coluna,
nunca trecho do arquivo): YAML inválido, âncora/alias (não suportados: também fecham a expansão
exponencial), chave repetida num mapeamento, aninhamento acima de ``MAX_DEPTH`` (o composer da libyaml
é recursivo em C: aninhamento muito fundo derruba o processo) e topo/seções fora do formato.
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

import yaml

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import Tool
from src.domain.ports import ILogger
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.repositories.config_documents import (
    INVALID_DOCUMENT_ERRORS,
    Document,
    first_tool_per_id,
    is_active,
    log_invalid_document,
    map_agent_document,
    map_team_document,
    map_tool_document,
    text_id,
    warn_ignored_camel_case,
)

SECTIONS = ("agents", "teams", "tools")

# Por seção: mensagem do log de documento inválido e o campo do id (os mesmos do Mongo).
INVALID_MESSAGES = {
    "agents": ("Documento de agente inválido ignorado", "agent_id"),
    "teams": ("Documento de team inválido ignorado", "team_id"),
    "tools": ("Documento de tool inválido ignorado", "tool_id"),
}

# Config real aninha poucos níveis (agents -> item -> rag_config -> model_params).
MAX_DEPTH = 64

# Loader seguro: o da libyaml (C) quando disponível.
SAFE_LOADER: Any = yaml.CSafeLoader if yaml.__with_libyaml__ else yaml.SafeLoader

_FileKey = tuple[int, int, int]
Sections = dict[str, list[tuple[int, Document]]]
# Item de seção que não é mapeamento: (seção, posição, item).
_InvalidItems = list[tuple[str, int, object]]

T = TypeVar("T")


class YamlConfigError(ValueError):
    """Arquivo de ``CONFIG_YAML_PATH`` ilegível ou fora do formato. A mensagem nunca traz conteúdo do arquivo."""


def _at(mark: object) -> str:
    line, column = getattr(mark, "line", None), getattr(mark, "column", None)
    return f" (linha {line + 1}, coluna {column + 1})" if isinstance(line, int) and isinstance(column, int) else ""


def _check_events(text: str) -> None:
    """Primeira passada (eventos do parser, sem recursão): recusa alias e aninhamento acima de ``MAX_DEPTH``."""
    depth = 0
    for event in yaml.parse(text, Loader=SAFE_LOADER):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise YamlConfigError(
                f"CONFIG_YAML_PATH: âncoras e aliases (& e *) não são suportados{_at(event.start_mark)}"
            )
        if isinstance(event, yaml.CollectionStartEvent):
            depth += 1
            if depth > MAX_DEPTH:
                raise YamlConfigError(
                    f"CONFIG_YAML_PATH: aninhamento acima de {MAX_DEPTH} níveis{_at(event.start_mark)}"
                )
        elif isinstance(event, yaml.CollectionEndEvent):
            depth -= 1


def _check_duplicate_keys(root: yaml.Node) -> None:
    """Segunda passada (grafo do ``compose``, iterativa): chave escalar repetida num mapeamento é erro
    (o PyYAML ficaria com a última em silêncio, ex.: duas seções ``agents``)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, yaml.MappingNode):
            seen: set[tuple[str, str]] = set()
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode):
                    # Limitação: compara tag e texto da chave, não o valor (``1`` e ``0x1`` passam como distintas).
                    identity = (key.tag, key.value)
                    if identity in seen:
                        raise YamlConfigError(f"CONFIG_YAML_PATH: chave repetida num mapeamento{_at(key.start_mark)}")
                    seen.add(identity)
                stack.extend((key, value))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)


def _parse(text: str) -> object:
    """Texto -> dados, com o loader seguro e as duas conferências acima."""
    _check_events(text)
    loader = SAFE_LOADER(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _check_duplicate_keys(node)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def _sections(raw: object) -> dict[str, list[object]]:
    if raw is None:  # arquivo vazio: nenhuma config, como coleções vazias
        raw = {}
    if not isinstance(raw, Mapping):
        raise YamlConfigError("CONFIG_YAML_PATH: o topo do arquivo deve ser um mapeamento com agents, teams e tools")
    if any(key not in SECTIONS for key in raw):
        raise YamlConfigError("CONFIG_YAML_PATH: chave de topo desconhecida; use só agents, teams e tools")
    sections: dict[str, list[object]] = {}
    for name in SECTIONS:
        items = raw.get(name)
        if items is None:
            items = []
        if not isinstance(items, list):
            raise YamlConfigError(f"CONFIG_YAML_PATH: a seção {name} deve ser uma lista de documentos")
        sections[name] = items
    return sections


class YamlConfigFile:
    """O arquivo de config YAML do operador, relido só quando muda.

    Os documentos devolvidos são compartilhados entre as consultas: quem os mapeia copia antes
    (``copy.deepcopy`` do documento casado), para a entidade nunca apontar para o cache. Item da seção
    que não é mapeamento (ex.: ``- texto``) é documento inválido: logado uma vez por leitura do arquivo.
    """

    def __init__(self, path: str, *, logger: ILogger) -> None:
        self._path = path
        self._logger = logger
        self._lock = asyncio.Lock()
        self._cached: tuple[_FileKey, Sections] | None = None

    @property
    def path(self) -> str:
        return self._path

    async def section(self, name: str) -> list[tuple[int, Document]]:
        """Documentos (mapeamentos) da seção na ordem do arquivo, com a posição (1 = primeiro item)."""
        async with self._lock:
            key = await asyncio.to_thread(self._stat)
            if self._cached is None or self._cached[0] != key:
                loaded_key, documents, invalid = await asyncio.to_thread(self._load)
                self._cached = (loaded_key, documents)
                self._report_invalid_items(invalid)
            return list(self._cached[1][name])

    def _stat(self) -> _FileKey:
        try:
            info = os.stat(self._path)
        except OSError as exc:
            raise YamlConfigError(f"CONFIG_YAML_PATH: não foi possível ler o arquivo ({type(exc).__name__})") from None
        return info.st_ino, info.st_size, info.st_mtime_ns

    def _load(self) -> tuple[_FileKey, Sections, _InvalidItems]:
        """Lê (a chave do cache vem do arquivo efetivamente aberto) e confere a estrutura."""
        try:
            with open(self._path, encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                text = stream.read()
        except OSError as exc:
            raise YamlConfigError(f"CONFIG_YAML_PATH: não foi possível ler o arquivo ({type(exc).__name__})") from None
        except UnicodeDecodeError:
            raise YamlConfigError("CONFIG_YAML_PATH: o arquivo não é UTF-8 válido") from None
        try:
            raw = _parse(text)
        except yaml.YAMLError as exc:
            # Sem str(exc): o erro do PyYAML cita o trecho do arquivo (prompt, header...).
            raise YamlConfigError(f"CONFIG_YAML_PATH: YAML inválido{_at(getattr(exc, 'problem_mark', None))}") from None
        except (ValueError, RecursionError) as exc:
            if isinstance(exc, YamlConfigError):
                raise
            # Ex.: data impossível sem aspas (2024-13-45); o texto do erro pode citar o valor.
            raise YamlConfigError(f"CONFIG_YAML_PATH: valor inválido no YAML ({type(exc).__name__})") from None
        sections = _sections(raw)
        invalid: _InvalidItems = [
            (name, position, item)
            for name in SECTIONS
            for position, item in enumerate(sections[name], start=1)
            if not isinstance(item, Mapping)
        ]
        documents: Sections = {
            name: [(position, item) for position, item in enumerate(items, start=1) if isinstance(item, Mapping)]
            for name, items in sections.items()
        }
        return (info.st_ino, info.st_size, info.st_mtime_ns), documents, invalid

    def _report_invalid_items(self, invalid: _InvalidItems) -> None:
        for name, position, item in invalid:
            message, id_field = INVALID_MESSAGES[name]
            log_invalid_document(self._logger, message, id_field, item, TypeError(), {"position": position})


class _YamlRepository:
    """Leitura de uma seção com isolamento de documento inválido (igual ao Mongo, F1-10)."""

    _section: str
    # Aviso de apiKeyRef/baseUrl/modelParams ignoradas: só agentes e teams têm modelo (como no Mongo).
    _warns_camel_case = True

    def __init__(self, source: YamlConfigFile, *, logger: ILogger) -> None:
        self._source = source
        self._logger = logger
        self._invalid_message, self._id_field = INVALID_MESSAGES[self._section]

    async def _active(
        self, mapper: Callable[[Document], T], match: Callable[[Document], bool] | None = None
    ) -> list[T]:
        """Documentos ativos (e que casam com ``match``) na ordem do arquivo; inválido isola só ele."""
        result: list[T] = []
        for position, doc in await self._source.section(self._section):
            if not is_active(doc) or (match is not None and not match(doc)):
                continue
            location = {"position": position}
            if self._warns_camel_case:
                warn_ignored_camel_case(self._logger, self._id_field, doc, location)
            try:
                result.append(mapper(copy.deepcopy(doc)))
            except INVALID_DOCUMENT_ERRORS as exc:
                log_invalid_document(self._logger, self._invalid_message, self._id_field, doc, exc, location)
        return result

    async def _first(self, item_id: str) -> tuple[int, Document] | None:
        """O primeiro documento com o id, ativo ou não (como ``find_one`` ordenado por ``_id``), já copiado."""
        for position, doc in await self._source.section(self._section):
            if text_id(doc) == item_id:
                return position, copy.deepcopy(doc)
        return None


class YamlAgentConfigRepository(_YamlRepository, IAgentConfigRepository):
    """Seção ``agents`` do arquivo de config."""

    _section = "agents"

    async def get_active_agents(self) -> list[AgentConfig]:
        return await self._active(lambda doc: map_agent_document(doc, self._logger))

    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        found = await self._first(agent_id)
        if found is None:
            raise ValueError(f"Agente {agent_id} não encontrado")
        position, doc = found
        warn_ignored_camel_case(self._logger, self._id_field, doc, {"position": position})
        return map_agent_document(doc, self._logger)


class YamlTeamConfigRepository(_YamlRepository, ITeamConfigRepository):
    """Seção ``teams`` do arquivo de config."""

    _section = "teams"

    async def get_active_teams(self) -> list[TeamConfig]:
        return await self._active(lambda doc: map_team_document(doc, self._logger))

    async def get_team_by_id(self, team_id: str) -> TeamConfig | None:
        found = await self._first(team_id)
        if found is None:
            return None
        position, doc = found
        warn_ignored_camel_case(self._logger, self._id_field, doc, {"position": position})
        return map_team_document(doc, self._logger)


class YamlToolRepository(_YamlRepository, IToolRepository):
    """Seção ``tools`` do arquivo de config; uma tool por id (a primeira), como no Mongo."""

    _section = "tools"
    _warns_camel_case = False

    async def get_tools_by_ids(self, tool_ids: list[str]) -> list[Tool]:
        if not tool_ids:
            return []
        wanted = set(tool_ids)
        tools = await self._active(map_tool_document, lambda doc: text_id(doc) in wanted)
        return first_tool_per_id(tools, self._logger)

    async def get_tool_by_id(self, tool_id: str) -> Tool:
        found = await self._first(tool_id)
        if found is None:
            raise ValueError(f"Tool {tool_id} não encontrada")
        return map_tool_document(found[1])

    async def get_all_active_tools(self) -> list[Tool]:
        return first_tool_per_id(await self._active(map_tool_document), self._logger)
