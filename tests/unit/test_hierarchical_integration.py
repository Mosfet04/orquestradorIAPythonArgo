"""Testes de integração e retrocompatibilidade para busca hierárquica."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.application.services.agent_factory_service import AgentFactoryService


class TestAgentFactoryRetrocompat:
    """Garante que agentes existentes sem search_strategy continuam funcionando."""

    def setup_method(self):
        self.mock_logger = MagicMock()
        self.mock_model_factory = MagicMock()
        self.mock_embedder_factory = MagicMock()
        self.mock_tool_factory = AsyncMock()
        self.mock_tool_repo = AsyncMock()
        self.mock_tool_repo.get_tools_by_ids = AsyncMock(return_value=[])

    def _make_config(
        self, search_strategy: SearchStrategy = SearchStrategy.SEMANTIC
    ) -> AgentConfig:
        return AgentConfig(
            id="agent-1",
            nome="Test Agent",
            factory_ia_model="ollama",
            model="llama3.2:latest",
            descricao="Agent de teste",
            prompt="Você é um assistente.",
            rag_config=RagConfig(
                active=True,
                doc_name="basic-prog.txt",
                model="nomic-embed-text:latest",
                factory_ia_model="ollama",
                search_strategy=search_strategy,
            ),
        )

    def test_default_rag_config_uses_semantic(self):
        """RagConfig sem search_strategy explícito deve ser SEMANTIC."""
        config = RagConfig(active=True)
        assert config.search_strategy == SearchStrategy.SEMANTIC

    def test_agent_config_with_semantic_rag(self):
        config = self._make_config(SearchStrategy.SEMANTIC)
        assert config.rag_config.search_strategy == SearchStrategy.SEMANTIC

    def test_agent_config_with_hierarchical_rag(self):
        config = self._make_config(SearchStrategy.HIERARCHICAL)
        assert config.rag_config.search_strategy == SearchStrategy.HIERARCHICAL


class TestMongoAgentConfigRepositoryRetrocompat:
    """Testa deserialização com e sem search_strategy."""

    def test_map_without_search_strategy(self):
        """Documentos MongoDB antigos sem search_strategy devem funcionar."""
        from src.infrastructure.repositories.mongo_agent_config_repository import (
            MongoAgentConfigRepository,
        )

        data = {
            "id": "agent-1",
            "nome": "Agent",
            "model": "llama3.2:latest",
            "factory_ia_model": "ollama",
            "descricao": "Desc",
            "prompt": "Prompt",
            "active": True,
            "rag_config": {
                "active": True,
                "doc_name": "doc.txt",
                "model": "nomic-embed-text:latest",
                "factory_ia_model": "ollama",
                # sem search_strategy
            },
        }

        entity = MongoAgentConfigRepository._map_to_entity(data)

        assert entity.rag_config is not None
        assert entity.rag_config.search_strategy == SearchStrategy.SEMANTIC
        assert entity.rag_config.active is True

    def test_map_with_search_strategy_hierarchical(self):
        from src.infrastructure.repositories.mongo_agent_config_repository import (
            MongoAgentConfigRepository,
        )

        data = {
            "id": "agent-2",
            "nome": "Agent 2",
            "model": "llama3.2:latest",
            "factory_ia_model": "ollama",
            "descricao": "Desc",
            "prompt": "Prompt",
            "active": True,
            "rag_config": {
                "active": True,
                "doc_name": "doc.txt",
                "model": "nomic-embed-text:latest",
                "factory_ia_model": "ollama",
                "search_strategy": "hierarchical",
            },
        }

        entity = MongoAgentConfigRepository._map_to_entity(data)

        assert entity.rag_config.search_strategy == SearchStrategy.HIERARCHICAL

    def test_map_without_rag_config(self):
        from src.infrastructure.repositories.mongo_agent_config_repository import (
            MongoAgentConfigRepository,
        )

        data = {
            "id": "agent-3",
            "nome": "Agent 3",
            "model": "llama3.2:latest",
            "factory_ia_model": "ollama",
            "descricao": "Desc",
            "prompt": "Prompt",
            "active": True,
        }

        entity = MongoAgentConfigRepository._map_to_entity(data)
        assert entity.rag_config is None


class TestAgentFactoryServiceStrategies:
    """Testa criação de agente com diferentes estratégias."""

    def setup_method(self):
        self.mock_logger = MagicMock()
        self.mock_model_factory = MagicMock()
        self.mock_embedder_factory = MagicMock()
        self.mock_tool_factory = AsyncMock()
        self.mock_tool_repo = AsyncMock()
        self.mock_tool_repo.get_tools_by_ids = AsyncMock(return_value=[])
        self.mock_indexing_service = AsyncMock()
        self.mock_search_factory = MagicMock()

    def _make_service(self) -> AgentFactoryService:
        return AgentFactoryService(
            db_url="mongodb://localhost:27017",
            db_name="testdb",
            logger=self.mock_logger,
            model_factory=self.mock_model_factory,
            embedder_factory=self.mock_embedder_factory,
            tool_factory=self.mock_tool_factory,
            tool_repository=self.mock_tool_repo,
            indexing_service=self.mock_indexing_service,
            search_factory=self.mock_search_factory,
        )

    def test_build_knowledge_returns_none_for_hierarchical(self):
        service = self._make_service()
        config = AgentConfig(
            id="a1",
            nome="Agent",
            factory_ia_model="ollama",
            model="llama3.2:latest",
            descricao="Desc",
            prompt="Prompt",
            rag_config=RagConfig(
                active=True,
                doc_name="doc.txt",
                search_strategy=SearchStrategy.HIERARCHICAL,
            ),
        )
        result = service._build_knowledge(config)
        assert result is None

    def test_build_knowledge_returns_none_for_inactive_rag(self):
        service = self._make_service()
        config = AgentConfig(
            id="a2",
            nome="Agent",
            factory_ia_model="ollama",
            model="llama3.2:latest",
            descricao="Desc",
            prompt="Prompt",
            rag_config=RagConfig(active=False),
        )
        result = service._build_knowledge(config)
        assert result is None

    def test_build_knowledge_returns_none_for_no_rag(self):
        service = self._make_service()
        config = AgentConfig(
            id="a3",
            nome="Agent",
            factory_ia_model="ollama",
            model="llama3.2:latest",
            descricao="Desc",
            prompt="Prompt",
        )
        result = service._build_knowledge(config)
        assert result is None

    def test_service_accepts_optional_new_deps(self):
        """AgentFactoryService funciona sem indexing/search (retrocompat)."""
        service = AgentFactoryService(
            db_url="mongodb://localhost:27017",
            db_name="testdb",
            logger=self.mock_logger,
            model_factory=self.mock_model_factory,
            embedder_factory=self.mock_embedder_factory,
            tool_factory=self.mock_tool_factory,
            tool_repository=self.mock_tool_repo,
            # sem indexing_service e search_factory
        )
        assert service._indexing_service is None
        assert service._search_factory is None
