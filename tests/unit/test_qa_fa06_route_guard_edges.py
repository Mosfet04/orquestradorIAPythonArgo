"""QA do FA-06: bordas da guarda de destino da ``route`` (modo dev, allowlist, formas de host).

Sem DNS real: o resolver é injetado. Complementa ``test_tool_route_guard.py``.
"""

from __future__ import annotations

import httpx
import pytest

from src.domain.entities.validation import InvalidField
from src.infrastructure.security.ssrf import normalize_host
from src.infrastructure.security.tool_route_guard import ToolRouteGuard
from tests.fakes.dns import PUBLIC_IP, no_dns


async def _refused(guard: ToolRouteGuard, route: str) -> None:
    with pytest.raises(InvalidField) as caught:
        await guard(route)
    assert caught.value.field == "route"


@pytest.mark.parametrize(
    "route",
    [
        "http://0.0.0.0:8000/x",  # escuta em todas as interfaces: não é loopback
        "http://[::]/x",
        "http://0/x",
        "http://2130706433/x",  # 127.0.0.1 em decimal: só o literal canônico vale
        "http://0x7f.1/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://127.0.0.1.nip.io/x",  # nome que resolve para loopback
    ],
)
async def test_modo_dev_local_nao_estende_o_loopback_a_formas_que_nao_sao_o_literal(route: str):
    guard = ToolRouteGuard(allow_loopback=True, resolver=lambda host: ["127.0.0.1"])
    await _refused(guard, route)


@pytest.mark.parametrize("route", ["http://LOCALHOST:8000/x", "http://127.0.0.1/x", "http://127.9.9.9/x", "http://[::1]/x"])
async def test_modo_dev_local_aceita_o_loopback_canonico_sem_dns(route: str):
    assert await ToolRouteGuard(allow_loopback=True, resolver=no_dns)(route) is True


async def test_modo_dev_local_nao_libera_ip_privado_nem_metadata():
    guard = ToolRouteGuard(allow_loopback=True, resolver=no_dns)
    await _refused(guard, "http://10.0.0.5/x")
    await _refused(guard, "http://169.254.169.254/x")


async def test_nome_publico_no_modo_dev_local_segue_sem_ser_do_operador():
    guard = ToolRouteGuard(allow_loopback=True, resolver=lambda host: [PUBLIC_IP])
    assert await guard("https://api.example.invalid/x") is False


@pytest.mark.parametrize("entry", ["169.254.169.254", "[fd00:ec2::254]", "100.100.100.200", "168.63.129.16"])
async def test_metadata_na_allowlist_continua_recusada(entry: str):
    guard = ToolRouteGuard(allowlist=frozenset({normalize_host(entry)}), resolver=no_dns)
    await _refused(guard, f"http://{entry}/latest")


@pytest.mark.parametrize(
    "route",
    [
        "http://API.INTERNA.EXAMPLE/x",
        "http://api.interna.example./x",
        "HTTPS://api.interna.example:8443/x",
        "http://api.interna.example:80/x",
    ],
)
async def test_allowlist_compara_o_host_normalizado_e_ignora_porta(route: str):
    guard = ToolRouteGuard(allowlist=frozenset({"api.interna.example"}), resolver=lambda host: ["10.0.0.9"])
    assert await guard(route) is True


async def test_allowlist_e_exata_nao_vale_para_subdominio_nem_sufixo():
    guard = ToolRouteGuard(allowlist=frozenset({"interna.example"}), resolver=lambda host: ["10.0.0.9"])
    await _refused(guard, "http://api.interna.example/x")
    await _refused(guard, "http://xinterna.example/x")
    await _refused(guard, "http://interna.example.evil.example/x")


async def test_allowlist_com_ipv6_compara_a_forma_canonica():
    guard = ToolRouteGuard(allowlist=frozenset({normalize_host("[FD00:0::7]")}), resolver=no_dns)
    assert await guard("http://[fd00::7]:8080/x") is True
    assert await guard("http://[FD00:0:0:0:0:0:0:7]/x") is True


@pytest.mark.parametrize(
    "route",
    [
        "http://evil.example\t@api.interna.example/x",
        "http://api.interna.example%00.evil.example/x",
        "http://api.interna.example\x00/x",
        "http://[fe80::1%25eth0]/x",
        "http:api.interna.example/x",
        "http://",
        "",
    ],
)
async def test_formas_ambiguas_sao_recusadas_mesmo_com_host_na_allowlist(route: str):
    guard = ToolRouteGuard(allowlist=frozenset({"api.interna.example"}), resolver=lambda host: [PUBLIC_IP])
    await _refused(guard, route)


@pytest.mark.parametrize(
    "route",
    [
        "http://api.interna.example#@evil.example/x",
        "http://api.interna.example?@evil.example/x",
        "http://api.interna.example/x@evil.example",
        "http://api.interna.example/{cep}?u=http://evil.example",
    ],
)
async def test_arroba_fora_do_netloc_nao_muda_o_host_e_o_cliente_http_le_o_mesmo_host(route: str):
    """Diferencial de parser: o host que a guarda aprovou é o que o ``httpx`` vai contatar."""
    guard = ToolRouteGuard(allowlist=frozenset({"api.interna.example"}), resolver=lambda host: ["10.0.0.9"])

    assert await guard(route) is True
    assert httpx.URL(route.replace("{cep}", "1")).host == "api.interna.example"


async def test_host_ipv6_com_forma_de_versao_futura_nunca_vira_host_do_operador():
    """``[v1.fe]`` não é IPv6: o stdlib o entrega como nome; resolvido público, não é do operador nem privado."""
    guard = ToolRouteGuard(allowlist=frozenset({"api.interna.example"}), resolver=lambda host: [PUBLIC_IP])
    assert await guard("http://[v1.fe]/x") is False
    await _refused(ToolRouteGuard(resolver=lambda host: ["10.0.0.1"]), "http://[v1.fe]/x")


async def test_placeholder_no_caminho_e_na_query_e_aceito():
    guard = ToolRouteGuard(resolver=lambda host: [PUBLIC_IP])
    assert await guard("https://api.example.invalid/v1/{cep}/json?chave={chave}") is False


@pytest.mark.parametrize("route", ["https://api.example.invalid?{x}", "https://api.example.invalid/{x}#{y}"])
async def test_placeholder_logo_depois_do_host_e_aceito_pois_o_host_segue_fixo(route: str):
    assert await ToolRouteGuard(resolver=lambda host: [PUBLIC_IP])(route) is False
