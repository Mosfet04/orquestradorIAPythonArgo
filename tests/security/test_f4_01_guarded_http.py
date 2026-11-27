"""F4-01: transporte HTTP guardado. A guarda SSRF roda na conexão e o TCP abre no IP conferido.

Fecha os resíduos do F2-02/FA-06: DNS rebinding (o nome era resolvido de novo pelo cliente ao conectar),
redirect seguido e proxy do ambiente (que esconderia o destino da guarda). Sem rede: o backend de rede do
httpcore é o falso de ``tests/fakes/network_backend.py`` e o DNS é injetado.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.infrastructure.security.guarded_http import (
    GuardedAsyncTransport,
    GuardedHttp,
    GuardedSyncTransport,
)
from src.infrastructure.security.ssrf import BlockedDestinationError, DestinationRule
from tests.fakes.dns import no_dns
from tests.fakes.network_backend import RecordingAsyncBackend, RecordingSyncBackend, http_response


class _Dns:
    """Resolver falso que responde, a cada chamada, a próxima resposta da fila (a última se repete)."""

    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _client(rule: DestinationRule, dns, backend: RecordingAsyncBackend) -> httpx.AsyncClient:
    transport = GuardedAsyncTransport(rule, resolver=dns, network_backend=backend)
    return httpx.AsyncClient(transport=transport, follow_redirects=False)


async def test_conecta_no_ip_conferido_e_mantem_host_e_sni_do_nome():
    backend = RecordingAsyncBackend()
    async with _client(DestinationRule(), _Dns(["93.184.216.34"]), backend) as client:
        response = await client.get("https://api.example.com/v1/x")
    assert response.status_code == 200
    assert backend.connects == [("93.184.216.34", 443)]
    assert backend.server_hostnames == ["api.example.com"]  # certificado conferido pelo nome
    assert b"Host: api.example.com\r\n" in backend.sent()


async def test_dns_rebinding_a_segunda_resolucao_para_metadata_e_recusada_sem_conectar():
    dns = _Dns(["93.184.216.34"], ["169.254.169.254"])
    backend = RecordingAsyncBackend()
    async with _client(DestinationRule(), dns, backend) as client:
        assert (await client.get("http://api.example.com/a")).status_code == 200
        with pytest.raises(BlockedDestinationError):
            await client.get("http://api.example.com/b")
    assert dns.calls == ["api.example.com", "api.example.com"]  # re-checa a cada conexão nova
    assert backend.connects == [("93.184.216.34", 80)]


@pytest.mark.parametrize("answer", [["10.0.0.5"], ["127.0.0.1"], ["::ffff:10.1.2.3"], ["fe80::1"]])
async def test_nome_publico_que_resolve_para_rede_interna_e_recusado(answer: list[str]):
    backend = RecordingAsyncBackend()
    async with _client(DestinationRule(), _Dns(answer), backend) as client:
        with pytest.raises(BlockedDestinationError):
            await client.get("http://api.example.com/")
    assert backend.connects == []


async def test_ip_literal_em_forma_legada_conecta_na_forma_canonica_sem_dns():
    backend = RecordingAsyncBackend()
    rule = DestinationRule(trusted_hosts=frozenset({"2130706433"}))
    async with _client(rule, no_dns, backend) as client:
        await client.get("http://2130706433:8080/")
    assert backend.connects == [("127.0.0.1", 8080)]


async def test_metadata_literal_recusado_mesmo_confiavel():
    backend = RecordingAsyncBackend()
    rule = DestinationRule(trusted_hosts=frozenset({"169.254.169.254"}))
    async with _client(rule, no_dns, backend) as client:
        with pytest.raises(BlockedDestinationError):
            await client.get("http://169.254.169.254/latest/meta-data/")
    assert backend.connects == []


async def test_redirect_nao_e_seguido_pelo_cliente_guardado():
    backend = RecordingAsyncBackend(http_response(302, b"", {"Location": "http://169.254.169.254/"}))
    http = GuardedHttp(DestinationRule(public_only=False), resolver=_Dns(["93.184.216.34"]), async_backend=backend)
    try:
        response = await http.async_client().get("http://gw.example.com/")
    finally:
        await http.aclose()
    assert response.status_code == 302
    assert backend.connects == [("93.184.216.34", 80)]


async def test_tenta_o_proximo_endereco_conferido_se_o_connect_falha():
    backend = RecordingAsyncBackend(refuse=("2001:db8::1",))
    rule = DestinationRule(public_only=False)
    async with _client(rule, _Dns(["2001:db8::1", "10.0.0.7"]), backend) as client:
        assert (await client.get("http://gw.corp/")).status_code == 200
    assert backend.connects == [("2001:db8::1", 80), ("10.0.0.7", 80)]


async def test_dns_sem_resposta_no_prazo_vira_connect_timeout_sem_conectar():
    import threading

    release = threading.Event()

    def slow(host: str) -> list[str]:
        release.wait(5)
        return ["93.184.216.34"]

    backend = RecordingAsyncBackend()
    transport = GuardedAsyncTransport(DestinationRule(), resolver=slow, dns_timeout=0.05, network_backend=backend)
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ConnectTimeout):
                await client.get("http://lento.example.com/")
    finally:
        release.set()
    assert backend.connects == []


async def test_proxy_do_ambiente_nao_e_usado(monkeypatch: pytest.MonkeyPatch):
    """Com proxy, o TCP iria ao proxy e o destino real escaparia da guarda."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.9.9.9:3128")
    monkeypatch.setenv("ALL_PROXY", "http://10.9.9.9:3128")
    backend = RecordingAsyncBackend()
    http = GuardedHttp(DestinationRule(), resolver=_Dns(["93.184.216.34"]), async_backend=backend)
    try:
        await http.async_client().get("http://api.example.com/")
    finally:
        await http.aclose()
    assert backend.connects == [("93.184.216.34", 80)]


def test_transporte_sincrono_conecta_no_ip_conferido_e_recusa_rebinding():
    dns = _Dns(["93.184.216.34"], ["10.0.0.5"])
    backend = RecordingSyncBackend()
    transport = GuardedSyncTransport(DestinationRule(), resolver=dns, network_backend=backend)
    with httpx.Client(transport=transport, follow_redirects=False) as client:
        assert client.get("https://api.example.com/").status_code == 200
        with pytest.raises(BlockedDestinationError):
            client.get("https://api.example.com/")
    assert backend.connects == [("93.184.216.34", 443)]
    assert backend.server_hostnames == ["api.example.com"]


async def test_um_transporte_atende_cliente_sincrono_e_assincrono():
    """SDKs como o ``ollama`` usam os mesmos kwargs no ``httpx.Client`` e no ``httpx.AsyncClient``."""
    sync_backend, async_backend = RecordingSyncBackend(), RecordingAsyncBackend()
    http = GuardedHttp(
        DestinationRule(public_only=False),
        resolver=_Dns(["10.0.0.9"]),
        async_backend=async_backend,
        sync_backend=sync_backend,
    )
    transport = http.transport()
    try:
        with httpx.Client(transport=transport) as sync_client:
            assert sync_client.get("http://gw.corp/").status_code == 200  # noqa: ASYNC212 - de propósito
        async with httpx.AsyncClient(transport=transport) as async_client:
            assert (await async_client.get("http://gw.corp/")).status_code == 200
        assert http.sync_client().get("http://gw.corp/").status_code == 200
    finally:
        await http.aclose()
    assert sync_backend.connects == [("10.0.0.9", 80), ("10.0.0.9", 80)]
    assert async_backend.connects == [("10.0.0.9", 80)]


async def test_clientes_do_guarded_http_sao_compartilhados_e_fechados_no_aclose():
    http = GuardedHttp(DestinationRule(), resolver=no_dns)
    first, again = http.async_client(), http.async_client()
    sync_first = http.sync_client()
    assert first is again
    assert sync_first is http.sync_client()
    assert first.follow_redirects is False and sync_first.follow_redirects is False
    await http.aclose()
    assert first.is_closed and sync_first.is_closed
    await http.aclose()  # idempotente


_SET_COOKIE = http_response(headers={"Set-Cookie": "session=user-A-secret; Path=/"})


async def test_clientes_compartilhados_dos_modelos_nao_guardam_cookie():
    """Os clientes servem a todos os modelos/agentes/usuários: cookie guardado vazaria a sessão entre eles."""
    sync_backend = RecordingSyncBackend(_SET_COOKIE)
    async_backend = RecordingAsyncBackend(_SET_COOKIE)
    http = GuardedHttp(
        DestinationRule(),
        resolver=_Dns(["93.184.216.34"]),
        async_backend=async_backend,
        sync_backend=sync_backend,
    )
    try:
        for _ in range(2):
            assert (await http.async_client().get("https://api.example.com/")).status_code == 200
            assert http.sync_client().get("https://api.example.com/").status_code == 200
        assert len(http.async_client().cookies) == 0 and len(http.sync_client().cookies) == 0
    finally:
        await http.aclose()
    assert b"session=" not in async_backend.sent()
    assert b"session=" not in sync_backend.sent()


async def test_dns_preso_nao_ocupa_o_executor_padrao_do_app():
    """O thread do ``getaddrinfo`` não é cancelável: no executor padrão, prenderia o ``asyncio.to_thread`` do app."""
    import concurrent.futures
    import threading

    release = threading.Event()

    def stuck(host: str) -> list[str]:
        release.wait(5)
        return ["93.184.216.34"]

    asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
    transport = GuardedAsyncTransport(DestinationRule(), resolver=stuck, dns_timeout=0.05)
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            for _ in range(3):
                with pytest.raises(httpx.ConnectTimeout):
                    await client.get("http://lento.example.com/")
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "livre"), 1.0) == "livre"
    finally:
        release.set()
