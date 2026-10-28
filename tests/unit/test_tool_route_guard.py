"""FA-06: guarda de destino da ``route`` de tool na escrita (esquema, IP privado/loopback/metadata, allowlist).

Sem DNS real: o resolver é injetado. A guarda devolve se o host é do operador (``TOOL_ROUTE_ALLOWLIST`` ou
loopback no modo dev local), o único destino que recebe header por referência.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from src.domain.entities.validation import InvalidField
from src.infrastructure.security.tool_route_guard import ToolRouteGuard
from tests.fakes.dns import PUBLIC_IP, no_dns


def _resolver(*addresses: str):
    calls: list[tuple[str, int]] = []

    def resolve(host: str) -> list[str]:
        calls.append((host, threading.get_ident()))
        return list(addresses)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


async def _refused(guard: ToolRouteGuard, route: str) -> InvalidField:
    with pytest.raises(InvalidField) as caught:
        await guard(route)
    assert caught.value.field == "route"
    return caught.value


@pytest.mark.parametrize(
    "route",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://2852039166/",  # 169.254.169.254 em decimal
        "http://0xa9.0xfe.0xa9.0xfe/",
        "http://[::ffff:169.254.169.254]/",
        "http://[fd00:ec2::254]/",
        "http://10.0.0.5/x",
        "http://192.168.1.10:8080/x",
        "http://172.16.0.1/x",
        "http://100.64.0.1/x",  # CGNAT
        "http://[fc00::1]/x",
        "http://[64:ff9b::a00:5]/x",  # NAT64 de 10.0.0.5
        "http://127.0.0.1:8000/x",
        "http://localhost/x",
        "http://[::1]/x",
        "http://0.0.0.0/x",
        "http://[fec0::1]/x",  # site-local
        "http://224.0.0.1/x",  # multicast
        "http://[ff05::2]/x",
    ],
)
async def test_ip_literal_nao_publico_e_recusado_sem_consultar_dns(route: str):
    await _refused(ToolRouteGuard(resolver=no_dns), route)


async def test_metadata_e_recusada_mesmo_na_allowlist_e_no_modo_dev():
    guard = ToolRouteGuard(allowlist=frozenset({"169.254.169.254"}), allow_loopback=True, resolver=no_dns)
    await _refused(guard, "http://169.254.169.254/latest/meta-data/")


@pytest.mark.parametrize("answer", [["10.0.0.7"], [PUBLIC_IP, "192.168.0.3"], ["169.254.169.254"], ["127.0.0.1"], []])
async def test_nome_que_resolve_para_ip_nao_publico_ou_para_nada_e_recusado(answer: list[str]):
    await _refused(ToolRouteGuard(resolver=_resolver(*answer)), "https://interno.example.invalid/x")


async def test_resolver_que_falha_recusa():
    def failing(host: str) -> list[str]:
        raise OSError("sem DNS")

    await _refused(ToolRouteGuard(resolver=failing), "https://api.example.invalid/x")


async def test_nome_publico_passa_resolvido_fora_do_event_loop_e_nao_e_do_operador():
    resolver = _resolver(PUBLIC_IP, "2606:4700::1111")
    trusted = await ToolRouteGuard(resolver=resolver)("https://api.example.invalid/v1/{cep}?formato=json")
    assert trusted is False
    [(host, thread)] = resolver.calls
    assert host == "api.example.invalid"
    assert thread != threading.get_ident()  # getaddrinfo bloqueia: não roda no thread do loop


async def test_ip_publico_literal_passa_sem_dns():
    assert await ToolRouteGuard(resolver=no_dns)(f"http://{PUBLIC_IP}:8080/x") is False


async def test_host_da_allowlist_aceita_ip_privado_e_e_do_operador():
    guard = ToolRouteGuard(allowlist=frozenset({"api.interna.example", "10.0.0.5"}), resolver=_resolver("10.1.2.3"))
    assert await guard("http://API.Interna.Example./pedidos") is True
    assert await guard("http://10.0.0.5:9000/x") is True


async def test_host_da_allowlist_que_resolve_para_metadata_e_recusado():
    guard = ToolRouteGuard(allowlist=frozenset({"api.interna.example"}), resolver=_resolver("169.254.169.254"))
    await _refused(guard, "http://api.interna.example/x")


@pytest.mark.parametrize("route", ["http://127.0.0.1:8000/x", "http://localhost/x", "http://[::1]:9/x"])
async def test_loopback_literal_so_no_modo_dev_local(route: str):
    assert await ToolRouteGuard(allow_loopback=True, resolver=no_dns)(route) is True
    await _refused(ToolRouteGuard(allow_loopback=False, resolver=no_dns), route)


@pytest.mark.parametrize(
    "route",
    [
        "ftp://api.example.invalid/x",
        "file:///etc/passwd",
        "gopher://api.example.invalid/x",
        "api.example.invalid/x",
        "https:///x",
        "https://usuario:senha@api.example.invalid/x",
        "https://token@api.example.invalid/x",
        "https://{host}/x",
        "https://api.{dominio}.example/x",
        "https://api.example.invalid:{porta}/x",
        "https://api.example.invalid /x",
        "https://api.example.invalid/x\n",
        "https://api.example.invalid\\@evil.example/x",
        "https://api.example.invalid:0/x",
        "https://api.example.invalid:/x",
        "https://api.example.invalid:99999/x",
        "https://ção.example/x",
        "https://api%2eexample.invalid/x",
    ],
)
async def test_forma_da_route_recusada_antes_do_dns(route: str):
    error = await _refused(ToolRouteGuard(allowlist=frozenset({"api.example.invalid"}), resolver=no_dns), route)
    assert "senha" not in str(error) and "token" not in str(error)


async def test_route_que_nao_e_texto_e_recusada():
    with pytest.raises(InvalidField) as caught:
        await ToolRouteGuard(resolver=no_dns)(123)  # type: ignore[arg-type]
    assert caught.value.field == "route"


async def test_resolucao_lenta_nao_trava_o_loop():
    release = threading.Event()

    def slow(host: str) -> list[str]:
        release.wait(5)
        return [PUBLIC_IP]

    check = asyncio.create_task(ToolRouteGuard(resolver=slow)("https://api.example.invalid/x"))
    await asyncio.sleep(0.01)
    assert not check.done()  # o loop seguiu rodando enquanto o DNS esperava
    release.set()
    assert await check is False


async def test_resolucao_sem_resposta_no_prazo_e_recusada():
    """A guarda roda sob a trava de escrita/reload/sync: um ``getaddrinfo`` lento não pode segurá-la."""
    release = threading.Event()

    def stuck(host: str) -> list[str]:
        release.wait(5)
        return [PUBLIC_IP]

    try:
        error = await _refused(ToolRouteGuard(resolver=stuck, dns_timeout=0.01), "https://api.example.invalid/x")
    finally:
        release.set()
    assert "api.example.invalid" not in str(error)


async def test_loopback_listado_na_allowlist_passa_fora_do_modo_dev():
    """Sidecar no mesmo pod: decisão explícita do operador."""
    guard = ToolRouteGuard(allowlist=frozenset({"127.0.0.1"}), resolver=no_dns)
    assert await guard("http://127.0.0.1:15000/x") is True
