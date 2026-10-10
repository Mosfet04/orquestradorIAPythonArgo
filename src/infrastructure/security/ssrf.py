"""Guarda SSRF de destino: metadata de nuvem e link-local nunca são alvo (F2-02; base do F4-01).

Sem framework: recebe host (já extraído da URL) e um resolver injetável. ``check_host`` resolve
o nome UMA vez e recusa se qualquer endereço for bloqueado; nome que não resolve também é
recusado (falha fechada). IP literal é reconhecido inclusive nas formas legadas que
``socket.inet_aton``/``getaddrinfo`` aceitam e o ``ipaddress`` não (``2852039166``,
``0xa9.0xfe.0xa9.0xfe``, ``169.254.43518``, octal), e IPv4 embutido em IPv6 (mapeado,
compatível, NAT64 ``64:ff9b::/96`` e ``64:ff9b:1::/48`` com sub-prefixo /96, 6to4) vale pelo IPv4.

Resíduo (fica para o F4-01): o SDK resolve o nome de novo ao conectar, então DNS rebinding
(primeira resposta legítima, segunda para metadata) não é coberto por esta checagem; fechar
exige conectar no IP verificado (transporte próprio), o que esta guarda não faz.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Iterable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Iterable[str]]

_BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),  # link-local IPv4 (metadata AWS/GCP/Azure)
    ipaddress.ip_network("0.0.0.0/8"),  # "esta rede": 0.0.0.0 chega no próprio host
    ipaddress.ip_network("100.100.100.200/32"),  # metadata Alibaba Cloud
    ipaddress.ip_network("192.0.0.192/32"),  # metadata Oracle Cloud
    ipaddress.ip_network("168.63.129.16/32"),  # WireServer da Azure (plataforma, acessível de VMs)
    ipaddress.ip_network("fe80::/10"),  # link-local IPv6
    ipaddress.ip_network("fd00:ec2::254/128"),  # metadata AWS em IPv6
    ipaddress.ip_network("::/128"),  # não especificado
)
# NAT64 com o IPv4 nos últimos 32 bits: prefixo bem conhecido e o de uso local (RFC 8215) com
# sub-prefixo /96, a forma comum de implantação.
_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
# Só dígitos, hexa, 'x' e pontos: o inet_aton do glibc aceita lixo depois de espaço ("1.2.3.4 x").
_LEGACY_IPV4 = re.compile(r"[0-9a-fx.]+")


class BlockedDestinationError(ValueError):
    """Destino recusado pela guarda SSRF. A mensagem não cita o host."""


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    packed_tail = ipaddress.IPv4Address(address.packed[-4:])
    if any(address in network for network in _NAT64):
        return packed_tail
    if address in _IPV4_COMPATIBLE and int(address) > 1:  # ::a.b.c.d (fora :: e ::1)
        return packed_tail
    return None


def is_blocked_address(address: IPAddress) -> bool:
    """``True`` para metadata/link-local, inclusive IPv4 bloqueado embutido em IPv6."""
    if isinstance(address, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(address)
        if embedded is not None and is_blocked_address(embedded):
            return True
    return any(address in network for network in _BLOCKED_NETWORKS if address.version == network.version)


def _strip(host: str) -> str:
    candidate = host.strip().lower()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    return candidate


def _canonical_ip(candidate: str) -> str | None:
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def normalize_host(host: str) -> str:
    """Forma única para comparar hosts (allowlist, hosts do provider): minúsculo, sem colchetes,
    IP na forma canônica (``[FD00:0::7]`` -> ``fd00::7``) e nome sem o ponto final. Não valida.

    ``10.0.0.5.`` fica como está: é um nome DNS (``inet_aton``/``AI_NUMERICHOST`` recusam), não o
    IP, e não pode casar com ``10.0.0.5`` nem contar como loopback literal.
    """
    candidate = _strip(host)
    canonical = _canonical_ip(candidate)
    if canonical is not None:
        return canonical
    name = candidate.removesuffix(".")
    return candidate if _canonical_ip(name) is not None else name


def literal_address(host: str) -> IPAddress | None:
    """IP de ``host`` se for literal (inclusive formas legadas de IPv4); senão ``None``."""
    candidate = _strip(host).split("%", 1)[0]  # escopo IPv6 (fe80::1%eth0)
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    if not candidate or not _LEGACY_IPV4.fullmatch(candidate):
        return None
    try:
        return ipaddress.IPv4Address(socket.inet_aton(candidate))
    except OSError:
        return None


def is_loopback_host(host: str) -> bool:
    """``localhost``, ``127.x.y.z`` ou ``::1`` literais (colchetes opcionais).

    IPv4 mapeado (``::ffff:127.0.0.1``) não conta: o ``is_loopback`` dele é ``False`` no
    3.11/3.12 e ``True`` a partir do 3.13; negar é o lado seguro e não depende da versão.
    Forma legada (``2130706433``) também não: aqui só vale o literal canônico.
    """
    candidate = _strip(host)
    if candidate == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    return address.is_loopback


def system_resolver(host: str) -> list[str]:
    """Endereços de ``host`` pelo resolver do sistema (bloqueante: chame fora do event loop)."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]


def check_host(host: str, *, resolver: Resolver) -> None:
    """Recusa (``BlockedDestinationError``) host de metadata/link-local, literal ou resolvido.

    Literal não consulta DNS. Nome é resolvido uma vez; qualquer endereço bloqueado, nenhum
    endereço ou erro de resolução recusam.
    """
    literal = literal_address(host)
    if literal is not None:
        if is_blocked_address(literal):
            raise BlockedDestinationError("destino de metadata/link-local recusado")
        return
    try:
        resolved = list(resolver(host))
    except (OSError, ValueError):  # ValueError: UnicodeError do idna (rótulo > 63, vazio...)
        resolved = []
    addresses = [literal_address(item) for item in resolved]
    if not addresses or any(address is None for address in addresses):
        raise BlockedDestinationError("host do destino não resolve para um endereço válido")
    if any(address is not None and is_blocked_address(address) for address in addresses):
        raise BlockedDestinationError("host do destino resolve para metadata/link-local; recusado")
