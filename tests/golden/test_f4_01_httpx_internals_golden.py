"""Pino dos internos do httpx/httpcore de que o transporte guardado depende (F4-01).

O ``GuardedAsyncTransport``/``GuardedSyncTransport`` trocam o ``_pool`` do transporte do httpx por um pool
do httpcore com ``network_backend`` guardado. Se uma versão nova mudar isso, este teste quebra antes da
guarda deixar de valer em silêncio (os testes de comportamento estão em
``tests/security/test_f4_01_guarded_http.py``).
"""

from __future__ import annotations

import inspect
import re

import httpcore
import httpx

from src.infrastructure.security.guarded_http import GuardedAsyncTransport, GuardedSyncTransport
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes.dns import no_dns


def test_transportes_do_httpx_so_usam_o_pool_em_pool():
    for method in (
        httpx.AsyncHTTPTransport.handle_async_request,
        httpx.AsyncHTTPTransport.aclose,
        httpx.HTTPTransport.handle_request,
        httpx.HTTPTransport.close,
    ):
        assert "self._pool." in inspect.getsource(method)


def test_transportes_do_httpx_nao_usam_outro_estado_do_init_alem_do_pool():
    """O ``__init__`` do pai não roda nos guardados: qualquer outro ``self.<x>`` lido fora dele quebraria."""
    for cls in (httpx.AsyncHTTPTransport, httpx.HTTPTransport):
        used: set[str] = set()
        for name, method in inspect.getmembers(cls, inspect.isfunction):
            if name != "__init__":
                used |= set(re.findall(r"self\.(\w+)", inspect.getsource(method)))
        assert used == {"_pool"}, (cls.__name__, used)


def test_pool_do_httpcore_aceita_network_backend():
    assert "network_backend" in inspect.signature(httpcore.AsyncConnectionPool).parameters
    assert "network_backend" in inspect.signature(httpcore.ConnectionPool).parameters


def test_conexao_do_httpcore_abre_o_tcp_pelo_backend_com_o_host_da_url():
    for module in (httpcore._async.connection, httpcore._sync.connection):
        source = inspect.getsource(module)
        assert "self._network_backend.connect_tcp(**kwargs)" in source
        assert '"host": self._origin.host.decode("ascii")' in source


def test_transportes_guardados_montam_o_pool_proprio():
    rule = DestinationRule()
    assert isinstance(GuardedAsyncTransport(rule, resolver=no_dns)._pool, httpcore.AsyncConnectionPool)
    assert isinstance(GuardedSyncTransport(rule, resolver=no_dns)._pool, httpcore.ConnectionPool)
