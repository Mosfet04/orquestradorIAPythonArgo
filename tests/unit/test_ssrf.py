"""Guarda SSRF de destino (F2-02; reutilizável pelo F4-01): metadata/link-local sempre recusados.

Sem DNS real: o resolver é injetado (``check_host(host, resolver=...)``).
"""

from __future__ import annotations

import ipaddress

import pytest

from src.infrastructure.security.ssrf import (
    BlockedDestinationError,
    check_host,
    is_blocked_address,
    is_loopback_host,
    literal_address,
)


def _resolver(*addresses: str):
    calls: list[str] = []

    def resolve(host: str) -> list[str]:
        calls.append(host)
        return list(addresses)

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _no_dns(host: str) -> list[str]:
    raise AssertionError(f"literal não deveria resolver DNS: {host}")


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # AWS/GCP/Azure
        "169.254.0.1",  # link-local IPv4
        "100.100.100.200",  # Alibaba
        "192.0.0.192",  # Oracle
        "0.0.0.0",  # noqa: S104 - é o destino que a guarda recusa
        "0.1.2.3",
        "fe80::1",  # link-local IPv6
        "fd00:ec2::254",  # AWS IPv6
        "::",
        "::ffff:169.254.169.254",  # IPv4 mapeado
        "::169.254.169.254",  # IPv4 compatível
        "64:ff9b::a9fe:a9fe",  # NAT64
        "2002:a9fe:a9fe::1",  # 6to4
    ],
)
def test_enderecos_de_metadata_e_link_local_sao_bloqueados(address: str):
    assert is_blocked_address(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize(
    "address", ["10.0.0.5", "192.168.1.10", "127.0.0.1", "::1", "8.8.8.8", "2606:4700::1111", "100.100.100.201"]
)
def test_enderecos_comuns_nao_sao_bloqueados(address: str):
    assert is_blocked_address(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "2852039166",
        "0xa9.0xfe.0xa9.0xfe",
        "169.254.43518",
        "0251.0376.0251.0376",
        "0xA9FEA9FE",
        "[::ffff:a9fe:a9fe]",
    ],
)
def test_formas_legadas_de_ip_sao_reconhecidas_como_literal(host: str):
    address = literal_address(host)
    assert address is not None and is_blocked_address(address)
    with pytest.raises(BlockedDestinationError):
        check_host(host, resolver=_no_dns)


@pytest.mark.parametrize("host", ["api.openai.com", "1.2.3.4 x", "deadbeef", "08.1.1.1", ""])
def test_hostname_nao_vira_literal(host: str):
    assert literal_address(host) is None


def test_literal_permitido_nao_consulta_dns():
    check_host("10.0.0.5", resolver=_no_dns)


def test_host_que_resolve_para_metadata_e_recusado():
    resolver = _resolver("10.0.0.1", "169.254.169.254")

    with pytest.raises(BlockedDestinationError, match="metadata"):
        check_host("gw.example.invalid", resolver=resolver)
    assert resolver.calls == ["gw.example.invalid"]  # resolve uma vez só


def test_host_que_resolve_para_link_local_ipv6_com_escopo_e_recusado():
    with pytest.raises(BlockedDestinationError):
        check_host("gw.example.invalid", resolver=_resolver("fe80::1%eth0"))


def test_host_que_resolve_para_enderecos_comuns_passa():
    check_host("gw.example.invalid", resolver=_resolver("10.0.0.1", "2606:4700::1111"))


@pytest.mark.parametrize("resolver", [_resolver(), _resolver("nao-e-ip")])
def test_host_sem_endereco_valido_falha_fechado(resolver):
    with pytest.raises(BlockedDestinationError, match="não resolve"):
        check_host("gw.example.invalid", resolver=resolver)


def test_erro_de_dns_falha_fechado_sem_encadear():
    def broken(host: str) -> list[str]:
        raise OSError("Name or service not known")

    with pytest.raises(BlockedDestinationError, match="não resolve") as caught:
        check_host("gw.example.invalid", resolver=broken)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "127.0.0.1", "127.9.9.9", "::1", "[::1]", " localhost "])
def test_loopback_literal(host: str):
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host", ["::ffff:127.0.0.1", "localhost.example.invalid", "10.0.0.1", "0.0.0.0", "2130706433"]  # noqa: S104
)
def test_nao_loopback(host: str):
    """IPv4 mapeado não conta (como no F1-04); forma legada também não (falha fechado)."""
    assert is_loopback_host(host) is False


def test_system_resolver_devolve_os_enderecos_do_getaddrinfo(monkeypatch: pytest.MonkeyPatch):
    """Sem DNS real: o ``getaddrinfo`` é trocado e o resolver só extrai o endereço de cada entrada."""
    from src.infrastructure.security import ssrf

    def fake_getaddrinfo(host: str, port: object, **kwargs: object) -> list[tuple[object, ...]]:
        assert host == "gw.example.invalid"
        return [(2, 1, 6, "", ("10.0.0.1", 0)), (10, 1, 6, "", ("fe80::1%eth0", 0, 0, 2))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)

    assert ssrf.system_resolver("gw.example.invalid") == ["10.0.0.1", "fe80::1%eth0"]
    with pytest.raises(BlockedDestinationError):
        check_host("gw.example.invalid", resolver=ssrf.system_resolver)


def test_nat64_de_uso_local_com_metadata_embutido_e_bloqueado():
    """RFC 8215: ``64:ff9b:1::/48`` (com sub-prefixo /96, o IPv4 nos últimos 32 bits)."""
    assert is_blocked_address(ipaddress.ip_address("64:ff9b:1::a9fe:a9fe")) is True
    assert is_blocked_address(ipaddress.ip_address("64:ff9b:1::a00:1")) is False  # 10.0.0.1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" GW.Example.COM. ", "gw.example.com"),
        ("[FD00:0::7]", "fd00::7"),
        ("fd00:0::7", "fd00::7"),
        ("10.0.0.5", "10.0.0.5"),
        ("localhost", "localhost"),
    ],
)
def test_normalize_host_e_a_forma_unica_de_comparacao(raw: str, expected: str):
    from src.infrastructure.security.ssrf import normalize_host

    assert normalize_host(raw) == expected


def test_wireserver_da_azure_e_bloqueado():
    """168.63.129.16: endpoint de plataforma da Azure acessível de qualquer VM (achado do QA)."""
    assert is_blocked_address(ipaddress.ip_address("168.63.129.16")) is True
    assert is_blocked_address(ipaddress.ip_address("::ffff:168.63.129.16")) is True
    assert is_blocked_address(ipaddress.ip_address("168.63.129.17")) is False


@pytest.mark.parametrize("host", ["10.0.0.5.", "127.0.0.1."])
def test_ip_com_ponto_final_e_nome_dns_e_nao_vira_o_ip(host: str):
    """N5: ``10.0.0.5.`` é nome (``AI_NUMERICHOST`` recusa); não pode casar com o IP nem ser loopback."""
    from src.infrastructure.security.ssrf import normalize_host

    assert normalize_host(host) == host
    assert is_loopback_host(host) is False
