"""Administração da config de agents, teams e tools (FA-02): listar, ler, criar, alterar e remover.

A validação é a das entidades: o documento recebido passa pelo mapper do store no modo estrito
(``parse``) e a recusa vira ``InvalidConfig`` só com o campo (``InvalidField``), nunca com a mensagem
nem o valor. Antes, o corpo não pode ter chave de operador (``$``), aninhamento acima de
``MAX_DOCUMENT_DEPTH`` nem valor que o JSON da resposta não serializa (número não finito, surrogate).
Antes de gravar, as referências (``tools_ids`` do agente, ``member_ids`` do team) precisam existir;
antes de remover, ninguém pode usar a entidade. Concorrência otimista (FA-01):
alterar e remover agem no documento do id na versão lida (``If-Match``), e a porta recusa com
``ConfigVersionConflict`` se ele mudou. As escritas dos três editores passam por uma trava comum
(leitura e gravação sem intercalar, num processo) e cada recusa vira um aviso no log, sem id nem valor.
Valores de header de tool saem mascarados; na escrita, o valor mascarado mantém o gravado, desde que
a ``route`` siga na mesma origem.

Troca a quente (FA-04): cada escrita de agente ou team monta todas as entidades ativas com a mudança
(``RuntimeSwap.build``), só então grava e troca as servidas pelo runtime; se a entidade alterada não monta,
``ConfigNotBuildable`` e nada é gravado. Tool é lida do store pela montagem do agente (e nunca o derruba):
grava antes e depois monta e troca. A troca roda dentro da trava de escrita, como o ``reload``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import ClassVar, Generic, Literal, TypeVar
from urllib.parse import urlsplit

from src.application.use_cases.config_change import ConfigChange, ConfigNotBuildable
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import Tool
from src.domain.entities.validation import InvalidField
from src.domain.ports import AgentHandle, AgentRuntime, ILogger, TeamHandle
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository
from src.domain.repositories.versioning import (
    ConfigStoreReadOnly,
    ConfigVersionConflict,
    Versioned,
    VersionedConfigRepository,
)

ConfigKind = Literal["agents", "teams", "tools"]
ConfigDocument = dict[str, object]
"""Documento de config como a API o mostra (o formato que o ``parse`` lê de volta)."""

E = TypeVar("E", AgentConfig, TeamConfig, Tool)
R = TypeVar("R")

MASKED_VALUE = "***"
# Config real tem poucos níveis (tool > parameters > parâmetro > default_value); muito além disso só
# serve para estourar a pilha de quem copia ou serializa o documento.
MAX_DOCUMENT_DEPTH = 32
# Surrogate solitário (``"\ud800"`` no JSON): o Python aceita, mas não vira UTF-8 na resposta nem no store.
_SURROGATE = re.compile("[\ud800-\udfff]")
# Recusas do mapeamento sem campo conhecido (o ``InvalidField`` já diz o campo).
_UNPLACED_DOCUMENT_ERRORS = (ValueError, TypeError, AttributeError, KeyError, RecursionError)


class ConfigNotFound(Exception):
    """Nenhum documento com o id."""

    def __init__(self, entity_id: str) -> None:
        super().__init__("Config não encontrada")
        self.entity_id = entity_id


class ConfigAlreadyExists(Exception):
    """Criação de um id que já tem documento."""

    def __init__(self, entity_id: str) -> None:
        super().__init__("Já existe config com este id")
        self.entity_id = entity_id


class InvalidConfig(Exception):
    """Documento recusado; ``field`` e ``indexes`` dizem onde, quando se sabe. Nunca o valor."""

    def __init__(self, field: str | None = None, indexes: Sequence[int] = ()) -> None:
        super().__init__("Configuração inválida")
        self.field = field
        self.indexes = tuple(indexes)


class ConfigInUse(Exception):
    """Remoção de entidade referenciada; ``used_by`` = tipo -> ids de quem usa."""

    def __init__(self, used_by: Mapping[str, list[str]]) -> None:
        super().__init__("Config em uso")
        self.used_by = dict(used_by)


_WRITE_REFUSALS = (
    InvalidConfig,
    ConfigNotBuildable,
    ConfigNotFound,
    ConfigAlreadyExists,
    ConfigInUse,
    ConfigVersionConflict,
    ConfigStoreReadOnly,
)


@dataclass(frozen=True)
class ConfigWrite:
    """Escrita feita: o documento como a API o mostra e a revisão do runtime que já serve a mudança."""

    item: Versioned[ConfigDocument]
    runtime_revision: int


@dataclass(frozen=True)
class RuntimeBuild:
    """Entidades montadas, prontas para a troca."""

    agents: list[AgentHandle]
    teams: list[TeamHandle]


class RuntimeSwap:
    """Monta todas as entidades ativas (com a mudança da escrita, se houver) e troca as servidas (FA-04).

    A montagem é a dos casos de uso do startup (um por id, falha isolada e logada), fora do caminho de
    request: só quem escreve espera. A troca é da porta (``AgentRuntime.swap``), atômica para quem lê.
    """

    def __init__(
        self, runtime: AgentRuntime, agents: GetActiveAgentsUseCase, teams: GetActiveTeamsUseCase, logger: ILogger
    ) -> None:
        self._runtime = runtime
        self._agents = agents
        self._teams = teams
        self._logger = logger

    async def build(
        self, *, agent: ConfigChange[AgentConfig] | None = None, team: ConfigChange[TeamConfig] | None = None
    ) -> RuntimeBuild:
        """Entidades da config ativa do store com a mudança; ``ConfigNotBuildable`` se a alterada não monta."""
        agents = await self._agents.execute(agent)
        return RuntimeBuild(agents, await self._teams.execute(agents, team))

    def swap(self, build: RuntimeBuild) -> int:
        """Passa a servir ``build``; devolve a revisão nova do runtime."""
        return self._runtime.swap(build.agents, build.teams)

    async def reload(self) -> int:
        """Relê o store, monta e troca; devolve a revisão nova. Falha loga só o tipo e sobe (o runtime segue)."""
        try:
            return self.swap(await self.build())
        except Exception as exc:
            self._logger.error(
                "Falha ao recarregar a config; o runtime anterior segue servindo", error_type=type(exc).__name__
            )
            raise


def _unserializable(value: object) -> bool:
    """Número não finito (``NaN``, ``Infinity``, ``1e999``) ou texto com surrogate solitário.

    O ``json.loads`` aceita os dois, mas a resposta que mostra o documento não os serializa: gravados,
    deixariam a listagem e a leitura do id em 500.
    """
    if isinstance(value, float):
        return not math.isfinite(value)
    return isinstance(value, str) and _SURROGATE.search(value) is not None


def _unsafe_document(body: Mapping[str, object]) -> bool:
    """Operador, aninhamento demais ou chave/valor que não serializa: o corpo não chega ao mapper.

    Chave começando com ``$`` (operador do Mongo), aninhamento acima de ``MAX_DOCUMENT_DEPTH`` ou
    chave ou valor ``_unserializable``. Iterativo: o documento aninhado não estoura a pilha aqui.
    """
    pending: list[tuple[object, int]] = [(body, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, Mapping | list) and depth > MAX_DOCUMENT_DEPTH:
            return True
        if isinstance(item, Mapping):
            if any((isinstance(key, str) and key.startswith("$")) or _unserializable(key) for key in item):
                return True
            pending.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item)
        elif _unserializable(item):
            return True
    return False


def _origin(route: str) -> tuple[str, str | None, int | None] | None:
    """Esquema, host e porta da ``route``; ``None`` se ela não parseia."""
    try:
        parts = urlsplit(route)
        return parts.scheme, parts.hostname, parts.port
    except ValueError:
        return None


def _ids(items: Sequence[Versioned[AgentConfig]] | Sequence[Versioned[Tool]]) -> set[str]:
    return {item.entity.id for item in items}


def _require_known(ids: Sequence[str], known: set[str], field: str) -> None:
    """``InvalidConfig`` com as posições de ``ids`` que não existem (os ids não voltam ao cliente)."""
    missing = [position for position, ref in enumerate(ids) if ref not in known]
    if missing:
        raise InvalidConfig(field, missing)


class ConfigEditor(ABC, Generic[E]):
    """CRUD de um tipo de config; os ganchos ``_checked``/``_users``/``_document`` são do tipo.

    ``write_lock`` é o mesmo nos três editores: a integridade referencial cruza os tipos.
    """

    kind: ClassVar[ConfigKind]

    def __init__(
        self,
        repository: VersionedConfigRepository[E],
        parse: Callable[[Mapping[str, object]], E],
        dump: Callable[[E], ConfigDocument],
        logger: ILogger,
        write_lock: asyncio.Lock,
        runtime: RuntimeSwap,
        *,
        writable: bool,
    ) -> None:
        self._repository: VersionedConfigRepository[E] = repository
        self._parse: Callable[[Mapping[str, object]], E] = parse
        self._dump: Callable[[E], ConfigDocument] = dump
        self._logger = logger
        self._write_lock = write_lock
        self._runtime = runtime
        self._writable = writable

    async def list_all(self) -> list[Versioned[ConfigDocument]]:
        """Todos os documentos (inativos inclusive), na ordem estável do store."""
        return [self._shown(item) for item in await self._repository.list_all()]

    async def get(self, entity_id: str) -> Versioned[ConfigDocument]:
        """O documento do id (o primeiro, com id repetido); ``ConfigNotFound`` se não houver."""
        found = await self._find(entity_id)
        if found is None:
            raise ConfigNotFound(entity_id)
        return self._shown(found)

    async def create(self, body: Mapping[str, object]) -> ConfigWrite:
        """Cria o documento na versão 1; ``ConfigAlreadyExists`` se o id já tiver documento."""
        async with self._writing():
            entity = await self._checked(self._parsed(body), None)
            if await self._find(entity.id) is not None:
                raise ConfigAlreadyExists(entity.id)
            try:
                return await self._saved(entity, 0)
            except ConfigVersionConflict:
                raise ConfigAlreadyExists(entity.id) from None

    async def replace(self, entity_id: str, body: Mapping[str, object], expected_version: int) -> ConfigWrite:
        """Substitui o documento do id; o ``id`` do corpo, se vier, tem de ser o mesmo."""
        async with self._writing():
            current = await self._current(entity_id, expected_version)
            if body.get("id", entity_id) != entity_id:
                raise InvalidConfig("id")
            entity = await self._checked(self._parsed({**body, "id": entity_id}), current.entity)
            return await self._saved(entity, expected_version)

    async def deactivate(self, entity_id: str, expected_version: int) -> ConfigWrite:
        """Remoção lógica (``active: false``); ``ConfigInUse`` se alguém usa a entidade."""
        async with self._writing():
            current = await self._current(entity_id, expected_version)
            await self._ensure_unused(entity_id)
            return await self._saved(dataclasses.replace(current.entity, active=False), expected_version)

    async def remove(self, entity_id: str, expected_version: int) -> int:
        """Remove o documento; ``ConfigInUse`` se alguém usa a entidade. Devolve a revisão do runtime."""
        async with self._writing():
            await self._current(entity_id, expected_version)
            await self._ensure_unused(entity_id)
            _, revision = await self._published(
                entity_id, None, lambda: self._repository.delete(entity_id, expected_version)
            )
            return revision

    @abstractmethod
    async def _checked(self, entity: E, current: E | None) -> E:
        """Confere o que depende de outros documentos (referências, segredos mantidos)."""

    async def _built(self, entity_id: str, entity: E | None) -> RuntimeBuild | None:
        """Entidades montadas com a mudança, antes de gravar; ``None`` = montar só depois de gravar.

        ``entity`` é o documento do id depois da escrita (``None`` se removido).
        """
        return None

    async def _published(self, entity_id: str, entity: E | None, write: Callable[[], Awaitable[R]]) -> tuple[R, int]:
        """Monta (``_built``), grava (``write``) e troca o runtime; devolve o resultado e a revisão nova.

        Montagem recusada (``ConfigNotBuildable``) não grava nada. Falha depois de gravar deixa o runtime
        anterior servindo: loga só o tipo do erro e sobe (o ``POST /admin/config/reload`` recupera).
        """
        build = await self._built(entity_id, entity)
        result = await write()
        try:
            return result, self._runtime.swap(build if build is not None else await self._runtime.build())
        except Exception as exc:
            self._logger.error(
                "Config gravada, mas o runtime não foi trocado; use POST /admin/config/reload",
                kind=self.kind,
                error_type=type(exc).__name__,
            )
            raise

    async def _users(self, entity_id: str) -> dict[str, list[str]]:
        """Tipo -> ids dos documentos que referenciam ``entity_id``."""
        return {}

    def _document(self, entity: E) -> ConfigDocument:
        return self._dump(entity)

    @asynccontextmanager
    async def _writing(self) -> AsyncIterator[None]:
        """Uma escrita por vez (trava comum aos editores) e um aviso por recusa, sem id nem valor.

        Store só leitura recusa antes de ler o corpo ou montar: a montagem tem efeito colateral (o RAG de
        um agente repara o índice e insere na coleção vetorial dele), que a recusa da gravação não desfaz.
        """
        async with self._write_lock:
            try:
                if not self._writable:
                    raise ConfigStoreReadOnly()
                yield
            except _WRITE_REFUSALS as exc:
                field = exc.field if isinstance(exc, InvalidConfig) else None
                self._logger.warning(
                    "Escrita de config recusada", kind=self.kind, refusal=type(exc).__name__, field=field
                )
                raise

    def _parsed(self, body: Mapping[str, object]) -> E:
        """Entidade do corpo; a recusa leva só o campo (a mensagem pode citar o valor recebido)."""
        if _unsafe_document(body):
            raise InvalidConfig()
        try:
            return self._parse(body)
        except InvalidField as exc:
            raise InvalidConfig(exc.field, exc.indexes) from None
        except _UNPLACED_DOCUMENT_ERRORS:
            raise InvalidConfig() from None

    async def _find(self, entity_id: str) -> Versioned[E] | None:
        items = await self._repository.list_all()
        return next((item for item in items if item.entity.id == entity_id), None)

    async def _current(self, entity_id: str, expected_version: int) -> Versioned[E]:
        """O documento do id na versão lida: sem documento do id, 404; só em outra versão, 412.

        Com id repetido, é este documento (e não o primeiro) que valida a escrita e cede o segredo mascarado.
        """
        same_id = [item for item in await self._repository.list_all() if item.entity.id == entity_id]
        if not same_id:
            raise ConfigNotFound(entity_id)
        found = next((item for item in same_id if item.version == expected_version), None)
        if found is None:
            raise ConfigVersionConflict(entity_id, expected_version)
        return found

    async def _ensure_unused(self, entity_id: str) -> None:
        used_by = {kind: ids for kind, ids in (await self._users(entity_id)).items() if ids}
        if used_by:
            raise ConfigInUse(used_by)

    async def _saved(self, entity: E, expected_version: int) -> ConfigWrite:
        version, revision = await self._published(
            entity.id, entity, lambda: self._repository.save(entity, expected_version)
        )
        return ConfigWrite(self._shown(Versioned(entity, version)), revision)

    def _shown(self, item: Versioned[E]) -> Versioned[ConfigDocument]:
        return Versioned(self._document(item.entity), item.version)


class AgentConfigEditor(ConfigEditor[AgentConfig]):
    """Agentes: ``tools_ids`` precisam existir; um agente membro de team não é removido."""

    kind = "agents"

    def __init__(
        self,
        repository: IAgentConfigRepository,
        parse: Callable[[Mapping[str, object]], AgentConfig],
        dump: Callable[[AgentConfig], ConfigDocument],
        logger: ILogger,
        write_lock: asyncio.Lock,
        runtime: RuntimeSwap,
        *,
        writable: bool,
        tools: IToolRepository,
        teams: ITeamConfigRepository,
    ) -> None:
        super().__init__(repository, parse, dump, logger, write_lock, runtime, writable=writable)
        self._tools = tools
        self._teams = teams

    async def _checked(self, entity: AgentConfig, current: AgentConfig | None) -> AgentConfig:
        _require_known(entity.tools_ids or [], _ids(await self._tools.list_all()), "tools_ids")
        return entity

    async def _built(self, entity_id: str, entity: AgentConfig | None) -> RuntimeBuild:
        """Se o agente não monta, nada é gravado."""
        return await self._runtime.build(agent=ConfigChange(entity_id, entity))

    async def _users(self, entity_id: str) -> dict[str, list[str]]:
        teams = await self._teams.list_all()
        return {"teams": list(dict.fromkeys(t.entity.id for t in teams if entity_id in t.entity.member_ids))}


class TeamConfigEditor(ConfigEditor[TeamConfig]):
    """Teams: ``member_ids`` precisam existir."""

    kind = "teams"

    def __init__(
        self,
        repository: ITeamConfigRepository,
        parse: Callable[[Mapping[str, object]], TeamConfig],
        dump: Callable[[TeamConfig], ConfigDocument],
        logger: ILogger,
        write_lock: asyncio.Lock,
        runtime: RuntimeSwap,
        *,
        writable: bool,
        agents: IAgentConfigRepository,
    ) -> None:
        super().__init__(repository, parse, dump, logger, write_lock, runtime, writable=writable)
        self._agents = agents

    async def _checked(self, entity: TeamConfig, current: TeamConfig | None) -> TeamConfig:
        _require_known(entity.member_ids, _ids(await self._agents.list_all()), "member_ids")
        return entity

    async def _built(self, entity_id: str, entity: TeamConfig | None) -> RuntimeBuild:
        """Se o team não monta, nada é gravado."""
        return await self._runtime.build(team=ConfigChange(entity_id, entity))


class ToolConfigEditor(ConfigEditor[Tool]):
    """Tools: headers saem mascarados; uma tool nas ``tools_ids`` de um agente não é removida.

    Monta só depois de gravar (``_built`` padrão): a montagem do agente lê as tools do store, e tool que
    falha não derruba o agente.
    """

    kind = "tools"

    def __init__(
        self,
        repository: IToolRepository,
        parse: Callable[[Mapping[str, object]], Tool],
        dump: Callable[[Tool], ConfigDocument],
        logger: ILogger,
        write_lock: asyncio.Lock,
        runtime: RuntimeSwap,
        *,
        writable: bool,
        agents: IAgentConfigRepository,
    ) -> None:
        super().__init__(repository, parse, dump, logger, write_lock, runtime, writable=writable)
        self._agents = agents

    async def _checked(self, entity: Tool, current: Tool | None) -> Tool:
        """Header com o valor mascarado mantém o valor gravado (o GET devolve a máscara).

        Só na mesma origem (esquema, host e porta) da ``route`` gravada: senão, quem tem a chave admin
        apontaria a tool para um host seu e receberia o segredo sem nunca tê-lo lido.
        """
        headers = entity.headers or {}
        masked = [name for name, value in headers.items() if value == MASKED_VALUE]
        if not masked:
            return entity
        stored = (current.headers if current else None) or {}
        origin = _origin(entity.route)
        same_origin = current is not None and origin is not None and origin == _origin(current.route)
        if not same_origin or any(name not in stored for name in masked):
            raise InvalidConfig("headers")
        kept = {name: stored[name] if value == MASKED_VALUE else value for name, value in headers.items()}
        return dataclasses.replace(entity, headers=kept)

    async def _users(self, entity_id: str) -> dict[str, list[str]]:
        agents = await self._agents.list_all()
        return {"agents": list(dict.fromkeys(a.entity.id for a in agents if entity_id in (a.entity.tools_ids or [])))}

    def _document(self, entity: Tool) -> ConfigDocument:
        document = self._dump(entity)
        if entity.headers:
            document["headers"] = dict.fromkeys(entity.headers, MASKED_VALUE)
        return document


ConfigEditorOfAnyKind = ConfigEditor[AgentConfig] | ConfigEditor[TeamConfig] | ConfigEditor[Tool]


@dataclass(frozen=True)
class ConfigAdmin:
    """Os três editores de config, um por tipo (o que a borda HTTP recebe), com a mesma trava de escrita.

    ``runtime`` e ``write_lock`` são os mesmos dos editores: o ``reload`` espera a vez das escritas.
    """

    agents: AgentConfigEditor
    teams: TeamConfigEditor
    tools: ToolConfigEditor
    runtime: RuntimeSwap
    write_lock: asyncio.Lock

    async def reload(self) -> int:
        """Relê o store, monta e troca o runtime (YAML editado à mão, recuperação manual); devolve a revisão."""
        async with self.write_lock:
            return await self.runtime.reload()

    def editor(self, kind: ConfigKind) -> ConfigEditorOfAnyKind:
        """O editor do tipo."""
        editors: dict[ConfigKind, ConfigEditorOfAnyKind] = {
            "agents": self.agents,
            "teams": self.teams,
            "tools": self.tools,
        }
        return editors[kind]
