"""Testes para MongoClientFactory e AsyncMongoRepository."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.infrastructure.repositories.mongo_base import (
    AsyncMongoRepository,
    MongoClientFactory,
)


class TestMongoClientFactory:
    def setup_method(self):
        """Limpa cache de instâncias entre testes."""
        MongoClientFactory._instances.clear()

    @patch("src.infrastructure.repositories.mongo_base.AsyncIOMotorClient")
    def test_get_client_creates_new(self, mock_motor_cls):
        mock_client = MagicMock()
        mock_motor_cls.return_value = mock_client
        client = MongoClientFactory.get_client("mongodb://localhost:27017")
        assert client is mock_client
        mock_motor_cls.assert_called_once()

    @patch("src.infrastructure.repositories.mongo_base.AsyncIOMotorClient")
    def test_get_client_reuses_existing(self, mock_motor_cls):
        mock_client = MagicMock()
        mock_motor_cls.return_value = mock_client
        c1 = MongoClientFactory.get_client("mongodb://localhost:27017")
        c2 = MongoClientFactory.get_client("mongodb://localhost:27017")
        assert c1 is c2
        assert mock_motor_cls.call_count == 1

    @patch("src.infrastructure.repositories.mongo_base.AsyncIOMotorClient")
    def test_get_client_with_tls_for_atlas(self, mock_motor_cls):
        mock_motor_cls.return_value = MagicMock()
        with patch.dict(os.environ, {"USE_TLS": "false", "TLS_ALLOW_INVALID_CERTIFICATES": "false"}, clear=False):
            MongoClientFactory.get_client("mongodb+srv://user:pass@cluster.mongodb.net/db")
        call_kwargs = mock_motor_cls.call_args
        # Deve ter tls=True por causa de mongodb.net na string
        assert call_kwargs[1].get("tls") is True

    @patch("src.infrastructure.repositories.mongo_base.AsyncIOMotorClient")
    def test_get_client_without_tls_for_local(self, mock_motor_cls):
        mock_motor_cls.return_value = MagicMock()
        with patch.dict(os.environ, {"USE_TLS": "false", "TLS_ALLOW_INVALID_CERTIFICATES": "false"}, clear=False):
            MongoClientFactory.get_client("mongodb://localhost:27017")
        call_kwargs = mock_motor_cls.call_args
        assert "tls" not in call_kwargs[1]

    @patch("src.infrastructure.repositories.mongo_base.AsyncIOMotorClient")
    def test_get_client_tls_via_env(self, mock_motor_cls):
        mock_motor_cls.return_value = MagicMock()
        with patch.dict(os.environ, {"USE_TLS": "true", "TLS_ALLOW_INVALID_CERTIFICATES": "true"}, clear=False):
            MongoClientFactory.get_client("mongodb://localhost:27017")
        call_kwargs = mock_motor_cls.call_args
        assert call_kwargs[1].get("tls") is True
        assert call_kwargs[1].get("tlsAllowInvalidCertificates") is True


class TestAsyncMongoRepository:
    @patch("src.infrastructure.repositories.mongo_base.MongoClientFactory.get_client")
    async def test_ping_success(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(return_value={"ok": 1})
        mock_client.__getitem__ = MagicMock(return_value=MagicMock())
        mock_get_client.return_value = mock_client

        repo = AsyncMongoRepository(
            connection_string="mongodb://localhost:27017",
            database_name="testdb",
            collection_name="test_col",
            logger=MagicMock(),
        )
        result = await repo.ping()
        assert result is True

    @patch("src.infrastructure.repositories.mongo_base.MongoClientFactory.get_client")
    async def test_ping_failure(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__getitem__ = MagicMock(return_value=MagicMock())
        mock_get_client.return_value = mock_client

        mock_logger = MagicMock()
        repo = AsyncMongoRepository(
            connection_string="mongodb://localhost:27017",
            database_name="testdb",
            collection_name="test_col",
            logger=mock_logger,
        )
        result = await repo.ping()
        assert result is False
        mock_logger.error.assert_called()
