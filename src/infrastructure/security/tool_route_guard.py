r"""Destino da ``route`` de tool HTTP gravada pela API admin (FA-06; antecipa parte do F4-01).

Vale só na escrita: documento legado segue lido e chamado como está. A ``route`` tem de ser
``http(s)://host[:porta]/...`` com host fixo (placeholder ``{x}`` só no caminho e na query: senão o
modelo escolheria o host), sem credencial (lugar de segredo é o header por referência), sem espaço,
``\`` nem caractere de controle, host em ASCII sem ``%``. Destino: host de ``TOOL_ROUTE_ALLOWLIST``
(pode ser privado; nunca metadata/link-local) ou loopback literal no modo dev local; fora disso, só
endereço público (``check_host(..., public_only=True)``), literal ou resolvido.

O DNS roda fora do event loop (``asyncio.to_thread``) e só para nome; IP literal não resolve. Tem prazo
(``DNS_TIMEOUT_SECONDS``): a guarda roda sob a trava comum de escrita/reload/sync e sem resposta recusa.
Resíduos (F4-01): DNS rebinding (o cliente resolve de novo ao conectar) e a re-checagem na chamada.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit

from src.domain.entities.validation import InvalidField
from src.infrastructure.security.ssrf import (
    BlockedDestinationError,
    Resolver,
    check_host,
    is_loopback_host,
    literal_address,
    normalize_host,
    system_resolver,
)

_SCHEMES = ("http", "https")
DNS_TIMEOUT_SECONDS = 5.0
"""Prazo da resolução do host na escrita; o ``getaddrinfo`` do sistema pode levar ~30 s até desistir."""


@dataclass(frozen=True)
class ToolRouteGuard:
    """Recusa (``InvalidField("route")``, sem o valor) destino proibido; devolve se o host é do operador.

    ``allowlist``: ``TOOL_ROUTE_ALLOWLIST`` já normalizada (``normalize_host``). ``allow_loopback``: só no
    modo dev local do F1-04. ``resolver``: injetável (testes nunca resolvem host real). ``dns_timeout``:
    prazo da resolução em segundos; estourado, recusa como erro de DNS. "Do operador" =
    host da allowlist ou loopback no modo dev: o único destino que pode receber header por referência.
    """

    allowlist: frozenset[str] = frozenset()
    allow_loopback: bool = False
    resolver: Resolver = system_resolver
    dns_timeout: float = DNS_TIMEOUT_SECONDS

    async def __call__(self, route: str) -> bool:
        """``True`` se o host é do operador, ``False`` se é público; ``InvalidField`` se recusado."""
        host = _host(route)
        trusted = normalize_host(host) in self.allowlist
        if is_loopback_host(host) and not trusted:
            if self.allow_loopback:
                return True
            raise InvalidField("route", "route em loopback só no modo dev local ou na TOOL_ROUTE_ALLOWLIST")
        try:
            if literal_address(host) is not None:
                check_host(host, resolver=self.resolver, public_only=not trusted)
            else:
                await asyncio.wait_for(
                    asyncio.to_thread(check_host, host, resolver=self.resolver, public_only=not trusted),
                    timeout=self.dns_timeout,
                )
        except BlockedDestinationError as exc:
            raise InvalidField("route", f"route: {exc}") from None  # texto nosso, sem o host
        except TimeoutError:
            # O thread do getaddrinfo não é cancelável: termina sozinho, já sem segurar a trava.
            raise InvalidField("route", "route: host sem resposta de DNS no prazo") from None
        return trusted


def _host(route: object) -> str:
    """Host da ``route`` conferida na forma; ``InvalidField("route")`` sem eco do valor."""
    if not isinstance(route, str):
        raise InvalidField("route", "route deve ser texto")
    if any(ch.isspace() or not ch.isprintable() for ch in route) or "\\" in route:
        raise InvalidField("route", "route não pode ter espaço, '\\' nem caractere de controle")
    parts = _split(route)
    if parts is None or parts.scheme not in _SCHEMES or not parts.hostname:
        raise InvalidField("route", "route deve ser http(s)://host[:porta]/caminho")
    _check_netloc(parts)
    return parts.hostname


def _check_netloc(parts: SplitResult) -> None:
    """Recusa credencial, placeholder, não ASCII, ``%`` e porta vazia/0 no ``host[:porta]``."""
    if "@" in parts.netloc:
        raise InvalidField("route", "route não pode ter credenciais; use header por referência")
    if "{" in parts.netloc or "}" in parts.netloc:
        raise InvalidField("route", "route: placeholder só no caminho ou na query, nunca no host ou na porta")
    if not parts.netloc.isascii() or "%" in parts.netloc:
        raise InvalidField("route", "route: host só em ASCII, sem '%' (IDN em punycode)")
    if parts.port == 0 or parts.netloc.endswith(":"):
        raise InvalidField("route", "route com porta vazia ou 0")


def _split(route: str) -> SplitResult | None:
    """``urlsplit`` com a porta validada; ``None`` se não parseia (o erro do stdlib cita a URL)."""
    try:
        parts = urlsplit(route)
        _ = parts.port
    except ValueError:
        return None
    return parts
