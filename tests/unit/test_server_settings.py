"""Host/porta do uvicorn vêm do AppConfig (F1-02)."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.web.server_settings import build_uvicorn_settings

ROOT = Path(__file__).resolve().parents[2]
# Valor que o Dockerfile define para APP_HOST (bind em todas as interfaces do container).
ALL_INTERFACES = "0.0.0.0"  # noqa: S104


def _config(**env: str) -> AppConfig:
    with patch.dict(os.environ, env, clear=True):
        return AppConfig.load()


def test_host_e_porta_vem_do_app_config():
    settings = build_uvicorn_settings(_config(APP_HOST=ALL_INTERFACES, APP_PORT="9000"))
    assert settings["host"] == ALL_INTERFACES
    assert settings["port"] == 9000
    assert settings["app"] == "app:app"


def test_default_fora_do_container_e_loopback():
    settings = build_uvicorn_settings(_config())
    assert settings["host"] == "127.0.0.1"
    assert settings["port"] == 7777


def test_reload_desligado():
    """reload não tem efeito com uvicorn.Server.serve(); fica desligado em qualquer ENVIRONMENT."""
    assert build_uvicorn_settings(_config())["reload"] is False


def test_app_py_usa_as_settings_do_config_sem_host_fixo():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_uvicorn_settings" in called
    literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
    assert "127.0.0.1" not in literals and ALL_INTERFACES not in literals
    assert 7777 not in literals


def test_uvloop_importado_com_guarda_e_so_fora_do_windows():
    """uvloop não existe no Windows: import sem guarda derrubava o app.py (F1-03)."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    guarded: list[ast.Import] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            handled = {
                h.type.id for h in node.handlers if isinstance(h.type, ast.Name)
            }
            if "ImportError" in handled or "ModuleNotFoundError" in handled:
                guarded += [n for stmt in node.body for n in ast.walk(stmt) if isinstance(n, ast.Import)]
    all_imports = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Import) and any(a.name == "uvloop" for a in n.names)
    ]
    assert all_imports, "app.py deveria tentar usar uvloop fora do Windows"
    assert all(n in guarded for n in all_imports), "import de uvloop fora de try/except ImportError"
    assert "win32" in (ROOT / "app.py").read_text(encoding="utf-8")
