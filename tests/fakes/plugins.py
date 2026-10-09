"""Distribuições falsas com entry points de provider (F2-03), sem ``pip install``.

``FakeSite`` grava num diretório temporário o que o pip deixaria em ``site-packages``: o
``<dist>-<versão>.dist-info`` (``METADATA`` + ``entry_points.txt``) e os módulos ao lado. A
fixture ``plugin_site`` põe o diretório no começo do ``sys.path``, então o
``importlib.metadata`` real descobre os entry points; no teardown, tira do ``sys.modules`` os
módulos gravados e limpa os caches de import.

Prova de "nunca importado": ``side_effect_module`` grava um marcador ao ser importado.
"""

from __future__ import annotations

import importlib
import sys
import textwrap
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# Literal de propósito: é o nome público que autores de plugin declaram no pyproject.toml.
ENTRY_POINT_GROUP = "orquestrador.providers"

SPEC_TEMPLATE = """
from src.infrastructure.providers import ClassSpec, ProviderSpec

SPEC = ProviderSpec(
    id={provider_id!r},
    sdk_package="acme-sdk",
    aliases={aliases!r},
    chat=ClassSpec(
        class_path="tests.fakes.providers.RecordingChatModel",
        params={{"temperature": "temperature"}},
        base_url_kwarg="base_url",
    ),
    embedder=ClassSpec(
        class_path="tests.fakes.providers.RecordingEmbedder",
        base_url_kwarg="base_url",
    ),
    default_hosts=frozenset({{"api.acme.example"}}),
    api_key_env="ACME_API_KEY",
)
"""


def spec_module(provider_id: str, *aliases: str) -> str:
    """Código de um módulo de plugin que expõe ``SPEC`` (classes de ``tests.fakes.providers``)."""
    return SPEC_TEMPLATE.format(provider_id=provider_id, aliases=tuple(aliases))


@dataclass
class FakeSite:
    """Diretório no ``sys.path`` onde "instalar" distribuições falsas."""

    root: Path
    modules: list[str] = field(default_factory=list)

    def marker(self, module: str) -> Path:
        """Arquivo que ``side_effect_module(module)`` grava quando é importado."""
        return self.root / f"{module}.importado"

    def side_effect_module(self, module: str, provider_id: str = "acme") -> str:
        """Módulo de plugin que, ao ser importado, grava ``marker(module)`` e expõe ``SPEC``."""
        return (
            f"import pathlib\npathlib.Path({str(self.marker(module))!r}).write_text('importado')\n"
            + spec_module(provider_id)
        )

    def install(
        self,
        dist: str,
        entry_points: Mapping[str, str],
        modules: Mapping[str, str] | None = None,
        *,
        version: str = "1.0.0",
        name_in_metadata: bool = True,
        group: str = ENTRY_POINT_GROUP,
        folder: str | None = None,
    ) -> None:
        """Grava ``<dist>-<versão>.dist-info`` (ou ``folder``) e os módulos (nome -> código) ao lado."""
        info = self.root / (folder or f"{dist.replace('-', '_')}-{version}.dist-info")
        info.mkdir()
        metadata = ["Metadata-Version: 2.1"]
        if name_in_metadata:
            metadata.append(f"Name: {dist}")
        metadata.append(f"Version: {version}")
        (info / "METADATA").write_text("\n".join(metadata) + "\n", encoding="utf-8")
        lines = [f"[{group}]", *(f"{name} = {value}" for name, value in entry_points.items())]
        (info / "entry_points.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        for module, source in (modules or {}).items():
            (self.root / f"{module}.py").write_text(textwrap.dedent(source), encoding="utf-8")
            self.modules.append(module)
        importlib.invalidate_caches()

    def imported(self, module: str) -> bool:
        """O módulo rodou (marcador gravado) ou está em ``sys.modules``."""
        return self.marker(module).exists() or module in sys.modules


@pytest.fixture
def plugin_site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSite]:
    root = tmp_path / "site-packages"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    site = FakeSite(root)
    yield site
    for module in site.modules:
        sys.modules.pop(module, None)
    importlib.invalidate_caches()
