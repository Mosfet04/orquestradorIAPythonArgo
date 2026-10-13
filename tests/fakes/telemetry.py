"""``TelemetryMetrics`` em memória: guarda o que seria exportado ao OTel."""

from __future__ import annotations

from collections import Counter


class RecordingTelemetryMetrics:
    """Substitui os instrumentos OTel por contadores locais por label.

    Fake estrutural de ``TelemetryMetrics`` (sem herdar): a fachada real é de ``staticmethod``,
    que um fake com estado não sobrescreve com a mesma assinatura. Os métodos daqui têm os
    mesmos parâmetros dos reais (conferido em ``tests/unit/test_fakes.py``); método não
    implementado falha alto em vez de cair no exportador OTel real.

    ``active`` espelha o up-down counter ``agents_active`` (soma dos deltas por agente).
    """

    def __init__(self) -> None:
        self.agent_requests: Counter[str] = Counter()
        self.team_requests: Counter[str] = Counter()
        self.request_statuses: Counter[tuple[str, str]] = Counter()  # (id, status) de agente e team
        self.agent_errors: Counter[str] = Counter()
        self.team_errors: Counter[str] = Counter()
        self.active: Counter[str] = Counter()
        self.durations: list[tuple[str, float]] = []

    def record_agent_request(self, agent_id: str, status: str = "success") -> None:
        self.agent_requests[agent_id] += 1
        self.request_statuses[(agent_id, status)] += 1

    def record_team_request(self, team_id: str, status: str = "success") -> None:
        self.team_requests[team_id] += 1
        self.request_statuses[(team_id, status)] += 1

    def record_agent_error(self, agent_id: str) -> None:
        self.agent_errors[agent_id] += 1

    def record_team_error(self, team_id: str) -> None:
        self.team_errors[team_id] += 1

    def record_agent_active(self, delta: int, agent_id: str = "") -> None:
        self.active[agent_id] += delta

    def record_agent_duration(self, agent_id: str, duration_s: float) -> None:
        self.durations.append((agent_id, duration_s))
