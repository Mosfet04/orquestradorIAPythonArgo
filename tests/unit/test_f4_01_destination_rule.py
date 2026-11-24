"""F4-01: ``DestinationRule``, a regra de destino conferida na hora de conectar (tools e modelos).

Sem DNS real: o resolver é injetado. A regra devolve os endereços conferidos, na ordem do resolver:
é neles que a conexão é aberta (contra DNS rebinding).
"""

from __future__ import annotations

import ipaddress

import pytest

from src.infrastructure.security.ssrf import BlockedDestinationError, DestinationRule
from tests.fakes.dns import no_dns


def _answer(*addresses: str):
    def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


def test_nome_publico_devolve_os_enderecos_conferidos_na_ordem():
    rule = DestinationRule()
    got = rule.addresses("api.example.com", _answer("93.184.216.34", "2606:2800:220:1::1"))
    assert got == [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("2606:2800:220:1::1")]


@pytest.mark.parametrize(
    "answer",
    [("10.0.0.5",), ("93.184.216.34", "127.0.0.1"), ("::ffff:10.0.0.5",), ("fd00::1",), ("100.64.0.1",)],
)
def test_publico_exigido_recusa_se_qualquer_endereco_nao_for_publico(answer: tuple[str, ...]):
    with pytest.raises(BlockedDestinationError, match="privado, loopback ou reservado"):
        DestinationRule().addresses("api.example.com", _answer(*answer))


@pytest.mark.parametrize("answer", [("169.254.169.254",), ("::ffff:169.254.169.254",), ("fd00:ec2::254",)])
def test_metadata_recusado_mesmo_no_host_confiavel(answer: tuple[str, ...]):
    rule = DestinationRule(trusted_hosts=frozenset({"interno.corp"}))
    with pytest.raises(BlockedDestinationError, match="metadata/link-local"):
        rule.addresses("interno.corp", _answer(*answer))


def test_host_confiavel_aceita_ip_privado():
    rule = DestinationRule(trusted_hosts=frozenset({"interno.corp"}))
    assert rule.addresses("INTERNO.corp.", _answer("10.0.0.5")) == [ipaddress.ip_address("10.0.0.5")]


@pytest.mark.parametrize(
    ("host", "expected"),
    [("2130706433", "127.0.0.1"), ("0x7f.1", "127.0.0.1"), ("10.0.0.5", "10.0.0.5")],
)
def test_ip_literal_nao_resolve_e_vale_na_forma_canonica(host: str, expected: str):
    rule = DestinationRule(trusted_hosts=frozenset({host}))
    assert rule.addresses(host, no_dns) == [ipaddress.ip_address(expected)]


@pytest.mark.parametrize("host", ["2130706433", "0x7f.1", "127.0.0.1", "[::1]", "::ffff:127.0.0.1"])
def test_loopback_fora_do_modo_dev_e_recusado(host: str):
    with pytest.raises(BlockedDestinationError):
        DestinationRule().addresses(host, no_dns)


def test_loopback_literal_no_modo_dev_e_aceito():
    rule = DestinationRule(allow_loopback=True)
    assert rule.addresses("127.0.0.1", no_dns) == [ipaddress.ip_address("127.0.0.1")]
    assert rule.addresses("localhost", _answer("127.0.0.1", "::1")) == [
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("::1"),
    ]


def test_localhost_no_modo_dev_que_resolve_fora_do_loopback_e_recusado():
    with pytest.raises(BlockedDestinationError):
        DestinationRule(allow_loopback=True).addresses("localhost", _answer("93.184.216.34"))


def test_n10_localhost_com_ponto_final_nao_conta_como_loopback_literal():
    """N10: o host decide como veio; ``localhost.`` é nome DNS, não o loopback literal."""
    with pytest.raises(BlockedDestinationError):
        DestinationRule(allow_loopback=True).addresses("localhost.", _answer("127.0.0.1"))


def test_forma_legada_de_loopback_nao_conta_como_loopback_literal_no_modo_dev():
    with pytest.raises(BlockedDestinationError):
        DestinationRule(allow_loopback=True).addresses("2130706433", no_dns)


def test_sem_publico_exigido_so_recusa_metadata():
    """Modelos (``public_only=False``): o host já foi conferido na montagem; aqui só metadata/link-local."""
    rule = DestinationRule(public_only=False)
    assert rule.addresses("gw.corp", _answer("10.0.0.5")) == [ipaddress.ip_address("10.0.0.5")]
    with pytest.raises(BlockedDestinationError, match="metadata/link-local"):
        rule.addresses("gw.corp", _answer("10.0.0.5", "169.254.169.254"))


@pytest.mark.parametrize("answer", [(), ("nao-e-ip",)])
def test_nome_que_nao_resolve_para_ip_valido_e_recusado(answer: tuple[str, ...]):
    with pytest.raises(BlockedDestinationError, match="não resolve"):
        DestinationRule(public_only=False).addresses("x.example.com", _answer(*answer))


def test_mensagem_nao_cita_o_host():
    with pytest.raises(BlockedDestinationError) as caught:
        DestinationRule().addresses("segredo-interno.example.com", _answer("10.0.0.5"))
    assert "segredo-interno" not in str(caught.value)
