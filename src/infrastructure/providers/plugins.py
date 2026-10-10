"""Plugins de provider por entry point (F2-03), no mesmo ``register`` dos built-ins.

Um plugin é uma distribuição instalada que declara, no grupo ``orquestrador.providers``, um
entry point que aponta para uma ``ProviderSpec`` (ex.: ``acme = acme_provider:SPEC``). Só
carrega o que está em ``PLUGIN_ALLOWLIST`` (``distribuição:nome``, distribuição normalizada pela
PEP 503), e a conferência usa só os metadados (``Name`` do ``METADATA`` e nome do entry point)
antes de qualquer ``ep.load()``: plugin fora da lista nunca é importado. Entry point sem
distribuição identificável (``METADATA`` ilegível ou sem ``Name``) é recusado. Entrada da lista
sem plugin instalado, ou casada por mais de uma distribuição (mesmo ``Name`` em diretórios
``*.dist-info`` de nomes diferentes; com o mesmo nome de diretório o stdlib entrega só o primeiro
no ``sys.path``), recusa o startup antes de importar qualquer plugin.

``DYNAMIC_PROVIDER_SPECS`` (``módulo:atributo``, para desenvolver um plugin sem empacotá-lo)
só vale com ``ALLOW_DYNAMIC_IMPORT=true`` no modo dev local; definida fora disso, recusa o
startup sem importar nada.

O registry é o mesmo dos built-ins, sem caminho privilegiado: o plugin passa pelas mesmas
guardas de destino e de chave do F2-02. Id ou alias repetido (sem caixa), entre plugins ou
com um built-in, recusa o startup. Toda recusa é ``PluginLoadError``, cuja mensagem cita
distribuição/nome do entry point (metadados do pacote instalado) ou a posição da entrada na
env, nunca o valor de uma env.

Síncrono e com I/O (metadados em disco, import): no caminho async, chame via
``asyncio.to_thread``.
"""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Sequence
from importlib.metadata import EntryPoint

from src.domain.ports import ILogger
from src.infrastructure.providers.registry import ProviderRegistry, ProviderSpec, normalize_provider_name

ENTRY_POINT_GROUP = "orquestrador.providers"
_UNIQUE_HINT = "id e aliases de provider são únicos, sem diferenciar caixa"
_BUILT_IN = "built-in"


class PluginLoadError(RuntimeError):
    """Plugin de provider recusado no startup; a mensagem nunca traz valor de env."""


def normalize_distribution_name(name: str) -> str:
    """Nome de distribuição pela PEP 503: ``-``, ``_`` e ``.`` em sequência viram ``-``, sem caixa."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_provider_plugins(
    registry: ProviderRegistry,
    *,
    allowlist: Sequence[str],
    logger: ILogger,
    dynamic_specs: Sequence[str] = (),
    dynamic_import_allowed: bool = False,
) -> None:
    """Registra em ``registry`` os plugins de ``allowlist`` e os ``dynamic_specs``.

    ``allowlist``: ``distribuição:nome`` já validados e normalizados (``AppConfig``).
    ``dynamic_import_allowed``: ``ALLOW_DYNAMIC_IMPORT=true`` **e** modo dev local, decidido
    pelo composition root. Qualquer recusa levanta ``PluginLoadError``.
    """
    if dynamic_specs and not dynamic_import_allowed:
        raise PluginLoadError(
            "DYNAMIC_PROVIDER_SPECS definida, mas import dinâmico só vale com ALLOW_DYNAMIC_IMPORT=true "
            "e no modo dev local (sem API_KEY_RUN/API_KEY_ADMIN, APP_HOST em loopback, ENVIRONMENT "
            "development ou test); em outros ambientes, instale o plugin e use PLUGIN_ALLOWLIST"
        )
    owners: dict[str, str] = {}
    for origin, entry_point, distribution, version in _allowed_entry_points(allowlist, logger):
        spec = _load(entry_point, origin)
        _register(registry, spec, origin, owners)
        logger.info(
            "Plugin de provider carregado",
            distribution=distribution,
            version=version,
            entry_point=entry_point.name,
            provider_id=spec.id,
        )
    for position, target in enumerate(dynamic_specs, start=1):
        origin = f"DYNAMIC_PROVIDER_SPECS (entrada {position})"
        spec = _load(EntryPoint(name=f"dinamico-{position}", value=target, group=ENTRY_POINT_GROUP), origin)
        _register(registry, spec, origin, owners)
        logger.warning(
            "Provider carregado por import dinâmico (DYNAMIC_PROVIDER_SPECS, só no modo dev local)",
            entry=position,
            target=target,  # validado no AppConfig: só identificadores Python, ``.`` e ``:``
            provider_id=spec.id,
        )


def _allowed_entry_points(
    allowlist: Sequence[str], logger: ILogger
) -> list[tuple[str, EntryPoint, str, str | None]]:
    """``(origem, entry point, distribuição, versão)`` permitidos, sem importar nada.

    Toda entrada da allowlist precisa casar com um entry point instalado; senão, erro antes
    do primeiro ``ep.load()``.
    """
    selected: list[tuple[str, EntryPoint, str, str | None]] = []
    found: list[str] = []
    for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        identity = _distribution(entry_point)
        if identity is None:
            logger.warning(
                "Plugin de provider recusado: entry point sem distribuição identificável",
                entry_point=entry_point.name,
            )
            continue
        distribution, version = identity
        key = f"{normalize_distribution_name(distribution)}:{entry_point.name}"
        if key not in allowlist:
            logger.warning(
                "Plugin de provider ignorado: fora da PLUGIN_ALLOWLIST",
                distribution=distribution,
                entry_point=entry_point.name,
            )
            continue
        found.append(key)
        selected.append((f"plugin '{key}'", entry_point, distribution, version))
    # O stdlib deduplica distribuições pelo nome do diretório ``*.dist-info``, não pelo ``Name``:
    # duas cópias com o mesmo ``Name`` em diretórios diferentes chegam aqui as duas.
    ambiguous = [str(position) for position, entry in enumerate(allowlist, start=1) if found.count(entry) > 1]
    if ambiguous:
        raise PluginLoadError(
            f"PLUGIN_ALLOWLIST: entrada {', '.join(ambiguous)} casa com mais de uma distribuição instalada "
            "(mesmo Name nos metadados, em diretórios *.dist-info diferentes); remova a cópia que não "
            "deveria estar instalada"
        )
    missing = [str(position) for position, entry in enumerate(allowlist, start=1) if entry not in found]
    if missing:
        raise PluginLoadError(
            f"PLUGIN_ALLOWLIST: entrada {', '.join(missing)} (contando da esquerda as não vazias) sem "
            f"plugin instalado no grupo {ENTRY_POINT_GROUP} com nome nos metadados da distribuição; "
            "instale a distribuição ou remova a entrada"
        )
    return selected


def _distribution(entry_point: EntryPoint) -> tuple[str, str | None] | None:
    """``(Name, Version)`` do ``METADATA`` da distribuição do entry point; ``None`` sem ``Name``.

    O nome do diretório ``*.dist-info`` não conta: sem ``Name`` nos metadados, falha fechada.
    """
    dist = entry_point.dist
    if dist is None:
        return None
    try:
        metadata = dist.metadata
    except (UnicodeDecodeError, OSError):
        # ``PathDistribution.read_text`` lê em UTF-8 e só suprime arquivo ausente/sem permissão
        # (importlib/metadata/__init__.py:811-819): bytes inválidos ou erro de E/S chegam aqui.
        return None
    # ``in`` antes de ``[]``: sem a chave, o ``[]`` do 3.12 devolve ``None`` com DeprecationWarning.
    name = metadata["Name"] if "Name" in metadata else None
    if not isinstance(name, str) or not name.strip():
        return None
    version = metadata["Version"] if "Version" in metadata else None
    return name.strip(), version if isinstance(version, str) else None


def _load(entry_point: EntryPoint, origin: str) -> ProviderSpec:
    """Importa o alvo do entry point (só depois da allowlist) e exige uma ``ProviderSpec``."""
    try:
        loaded = entry_point.load()
    except Exception as exc:  # qualquer falha do código do plugin recusa o startup
        raise PluginLoadError(f"{origin}: falha ao carregar ({type(exc).__name__})") from exc
    if not isinstance(loaded, ProviderSpec):
        raise PluginLoadError(
            f"{origin}: o entry point deve apontar para uma ProviderSpec (recebido: {type(loaded).__name__})"
        )
    # A dataclass não valida tipos: ``id=None`` quebraria no register e ``aliases="ab"`` viraria "a" e "b".
    valid_id = isinstance(loaded.id, str) and bool(loaded.id.strip())
    valid_aliases = isinstance(loaded.aliases, tuple) and all(isinstance(alias, str) for alias in loaded.aliases)
    if not (valid_id and valid_aliases):
        raise PluginLoadError(f"{origin}: ProviderSpec inválida (id/aliases)")
    return loaded


def _register(registry: ProviderRegistry, spec: ProviderSpec, origin: str, owners: dict[str, str]) -> None:
    """``registry.register`` com mensagem que diz com quem a spec conflita.

    Conflito com nome já registrado é conferido aqui para citar o dono (built-in ou plugin); a
    repetição dentro da própria spec fica com a regra (e a mensagem) do ``register``.
    """
    names = list(dict.fromkeys(normalize_provider_name(name) for name in (spec.id, *spec.aliases)))
    taken = [name for name in names if name in registry]
    if taken:
        # Nome que nenhum plugin deste loader registrou já estava lá antes: é de um built-in.
        owner = ", ".join(f"{name}: {owners.get(name, _BUILT_IN)}" for name in taken)
        raise PluginLoadError(
            f"{origin}: provider '{spec.id}' com id ou alias já registrado ({owner}); {_UNIQUE_HINT}"
        )
    try:
        registry.register(spec)
    except ValueError as exc:
        raise PluginLoadError(f"{origin}: {exc}; {_UNIQUE_HINT}") from exc
    for name in names:
        owners[name] = origin
