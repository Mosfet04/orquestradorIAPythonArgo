"""Stub dos SDKs opcionais que não estão instalados (``anthropic``, ``groq``; ver ``requirements.in``).

A fixture ``optional_sdks_stubbed`` troca o import do SDK ausente por um módulo permissivo só
durante o teste, para provar que o caminho/classe da spec do provider existe no agno instalado
(sem isso, o ``ImportError`` de um caminho errado se confunde com "instale o SDK": B1 do F1-06).
Registrada em ``tests/conftest.py``.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types
from collections.abc import Iterator

import pytest


class _PermissiveModule(types.ModuleType):
    """Módulo que inventa uma classe vazia para qualquer nome importado dele."""

    def __getattr__(self, name: str) -> type:
        if name.startswith("__"):
            raise AttributeError(name)
        stub = type(name, (), {})
        setattr(self, name, stub)
        return stub


class _StubSdkFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Responde ``import <raiz>[.x.y]`` com ``_PermissiveModule`` (pacote, aceita submódulos)."""

    def __init__(self, roots: set[str]) -> None:
        self._roots = roots

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname.split(".")[0] in self._roots:
            return importlib.util.spec_from_loader(fullname, self, is_package=True)
        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        return _PermissiveModule(spec.name)

    def exec_module(self, module: types.ModuleType) -> None:
        return None


def _is_installed(sdk: str) -> bool:
    try:
        return importlib.util.find_spec(sdk) is not None
    except ImportError:
        return False


# Módulos do agno que importam o SDK opcional (direto ou via utilitário).
_AGNO_MODULES_OF_SDK = {
    "anthropic": ("agno.models.anthropic", "agno.utils.models.claude"),
    "groq": ("agno.models.groq",),
}


def _forget(prefixes: list[str]) -> None:
    """Tira do cache (e do pacote pai) o que foi importado em cima do stub."""
    for name in [n for n in sys.modules if any(n == p or n.startswith(p + ".") for p in prefixes)]:
        parent, _, child = name.rpartition(".")
        parent_module = sys.modules.get(parent)
        if parent_module is not None and getattr(parent_module, child, None) is sys.modules[name]:
            delattr(parent_module, child)
        del sys.modules[name]


@pytest.fixture
def optional_sdks_stubbed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub dos SDKs opcionais NÃO instalados; com o SDK instalado, vale o real.

    SDK ausente nunca deixa módulo em cache (import falho não fica em ``sys.modules``),
    então antes do teste não há nada a salvar; depois, tudo que veio do stub é esquecido.
    """
    missing = sorted(sdk for sdk in _AGNO_MODULES_OF_SDK if not _is_installed(sdk))
    prefixes = [*missing, *(m for sdk in missing for m in _AGNO_MODULES_OF_SDK[sdk])]
    _forget(prefixes)
    monkeypatch.setattr(sys, "meta_path", [_StubSdkFinder(set(missing)), *sys.meta_path])
    yield
    _forget(prefixes)
