import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from src.infrastructure.logging.config import LOGGING_CONFIG, setup_logging

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def restore_logging():
    """setup_logging() usa dictConfig (estado global): devolve root e loggers nomeados como estavam."""
    names = [None, *LOGGING_CONFIG["loggers"]]
    saved = {}
    for name in names:
        lg = logging.getLogger(name)
        saved[name] = (list(lg.handlers), lg.level, lg.propagate, lg.disabled)
    console_level = LOGGING_CONFIG["handlers"]["console"]["level"]
    yield
    for name, (handlers, level, propagate, disabled) in saved.items():
        lg = logging.getLogger(name)
        for handler in lg.handlers:
            if handler not in handlers:
                handler.close()
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate
        lg.disabled = disabled
    LOGGING_CONFIG["handlers"]["console"]["level"] = console_level


@pytest.mark.usefixtures("restore_logging")
def test_setup_logging_creates_handlers_and_logs_dir(tmp_path, monkeypatch):
    # Forçar diretório de trabalho temporário para não poluir workspace
    monkeypatch.chdir(tmp_path)

    # Executa setup
    setup_logging()

    # Deve existir dir logs e logger funcional
    assert os.path.isdir("logs")
    logger = logging.getLogger('orquestrador_ia')
    assert isinstance(logger, logging.Logger)
    # Pelo menos um handler deve estar presente
    assert logger.handlers


def test_importar_o_modulo_nao_configura_logging_nem_cria_diretorio(tmp_path):
    """Import sem efeito colateral: quem configura é o ponto de entrada, chamando setup_logging()."""
    # Processo novo: neste processo o módulo já foi importado por outros testes.
    script = textwrap.dedent(
        """
        import logging, pathlib, shutil
        # O pacote pai (structlog) tem efeitos próprios, fora do escopo: isola só o import de config.
        import src.infrastructure.logging
        shutil.rmtree("logs", ignore_errors=True)
        root_antes = list(logging.getLogger().handlers)
        import src.infrastructure.logging.config  # noqa: F401
        assert logging.getLogger("orquestrador_ia").handlers == [], "logger da app configurado no import"
        assert logging.getLogger().handlers == root_antes, "root logger alterado no import"
        assert not pathlib.Path("logs").exists(), "diretório logs/ criado no import"
        """
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    result = subprocess.run(  # noqa: S603 - interpretador do próprio teste, script fixo
        [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
