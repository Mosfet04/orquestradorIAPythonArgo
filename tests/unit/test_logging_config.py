import logging
import os
from src.infrastructure.logging.config import setup_logging


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
