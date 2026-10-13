"""Testes de contrato: a MESMA suíte roda contra toda implementação de uma porta.

Cada suíte recebe, por fixture parametrizada, uma fábrica ``seed -> implementação``.
As implementações em memória de ``tests/fakes`` rodam aqui (elas são a base dos demais
testes, então precisam obedecer ao contrato). Adapter que depende de serviço externo entra
como parâmetro marcado ``live``: as suítes de config e da árvore de documentos têm o backend
``mongo-live`` (Mongo real de ``MONGO_LIVE_URL``, pulado sem ela), que confere a fidelidade
da coleção fake de ``tests/fakes/mongo.py``.

Todo teste deste diretório recebe o marker ``contract`` (ver ``tests/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes.config_stores import (
    DOCUMENT_BACKEND_PARAMS,
    MongoBackend,
    MongoLiveBackend,
    YamlBackend,
    document_backend,
    live_mongo_for,
)


@pytest.fixture(params=DOCUMENT_BACKEND_PARAMS)
def backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> MongoBackend | MongoLiveBackend | YamlBackend:
    """Backend de documentos de config (Mongo sobre a coleção fake, YAML e, ``live``, Mongo real).

    ``test_repository_contracts.py`` define a sua (por entidades, com o backend em memória também).
    """
    return document_backend(request.param, monkeypatch, tmp_path, live_mongo_for(request))
