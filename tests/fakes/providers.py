"""Classes de provider falsas para specs do ``ProviderRegistry`` (F2-02), sem SDK nem rede.

Uma spec fake aponta para estas classes pelo caminho pontilhado (``tests.fakes.providers.X``),
como uma spec real aponta para a classe do agno: o teste prova que adicionar provider é só
registrar uma spec. Cada instância guarda os kwargs recebidos e se foi criada na thread do
event loop.
"""

from __future__ import annotations

from typing import Any

from tests.fakes.models import running_on_event_loop

CHAT_PATH = "tests.fakes.providers.RecordingChatModel"
EMBEDDER_PATH = "tests.fakes.providers.RecordingEmbedder"
FAILING_PATH = "tests.fakes.providers.FailingChatModel"


class RecordingChatModel:
    """Modelo de chat que só registra os kwargs do construtor."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.id: str = kwargs["id"]
        self.created_on_event_loop = running_on_event_loop()


class RecordingEmbedder:
    """Embedder que registra os kwargs; ``get_embedding`` devolve um vetor fixo."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.id: str = kwargs["id"]

    def get_embedding(self, text: str) -> list[float]:
        return [float(len(text)), 0.0]


class FailingChatModel:
    """Construtor que falha citando a chave recebida, como um SDK descuidado faria."""

    def __init__(self, **kwargs: Any) -> None:
        raise RuntimeError(f"credencial recusada: {kwargs.get('api_key')}")
