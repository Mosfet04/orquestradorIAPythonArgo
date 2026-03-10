"""Factory de estratégias de busca RAG."""

from __future__ import annotations

from typing import Any, Optional

from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.knowledge_search_port import IKnowledgeSearchStrategy
from src.domain.ports.logger_port import ILogger
from src.application.services.search_strategies.semantic_search_strategy import (
    SemanticSearchStrategy,
)
from src.application.services.search_strategies.hierarchical_search_strategy import (
    HierarchicalSearchStrategy,
)


class KnowledgeSearchFactory:
    """Cria a estratégia de busca adequada baseado na configuração RAG.

    Implementa o padrão *Factory* para desacoplar a criação da
    estratégia de busca do serviço que a consome.
    """

    def __init__(
        self,
        *,
        tree_repository: IDocumentTreeRepository,
        logger: ILogger,
    ) -> None:
        self._tree_repository = tree_repository
        self._logger = logger

    def create_strategy(
        self,
        rag_config: RagConfig,
        *,
        knowledge: Optional[Any] = None,
        embedder: Optional[Any] = None,
    ) -> IKnowledgeSearchStrategy:
        """Cria a estratégia de busca conforme ``rag_config.search_strategy``.

        Parameters
        ----------
        rag_config:
            Configuração RAG do agente.
        knowledge:
            Instância de ``Knowledge`` do agno (necessária para SEMANTIC).
        embedder:
            Instância do embedder (necessária para HIERARCHICAL).
        """
        strategy = rag_config.search_strategy

        if strategy == SearchStrategy.HIERARCHICAL:
            if embedder is None:
                raise ValueError(
                    "embedder é obrigatório para estratégia HIERARCHICAL"
                )
            if not rag_config.doc_name:
                raise ValueError(
                    "doc_name é obrigatório para estratégia HIERARCHICAL"
                )
            self._logger.info(
                "Criando estratégia HIERARCHICAL",
                doc_name=rag_config.doc_name,
            )
            return HierarchicalSearchStrategy(
                tree_repository=self._tree_repository,
                embedder=embedder,
                doc_name=rag_config.doc_name,
                logger=self._logger,
            )

        # Default: SEMANTIC
        if knowledge is None:
            raise ValueError("knowledge é obrigatório para estratégia SEMANTIC")
        self._logger.info("Criando estratégia SEMANTIC")
        return SemanticSearchStrategy(knowledge=knowledge)
