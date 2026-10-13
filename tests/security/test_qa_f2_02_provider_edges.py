"""QA F2-02: bordas de segurança do destino de ``base_url`` e da ``MODEL_BASE_URL_ALLOWLIST``.

- startup recusa allowlist hostil com erro claro, antes do bind;
- modo dev local: só o literal canônico de loopback vale (formas legadas/mapeadas não);
- achados de QA (corrigidos na rodada 3 do F2-02): N5 (ponto final em IP), BUG-F2-02-QA-1 e
  BUG-F2-02-QA-2.
"""

from __future__ import annotations

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.security.ssrf import BlockedDestinationError, check_host, system_resolver
from src.infrastructure.web.app_factory import AppFactory

CRED = "S3nh4-QA-F202-NAO-PODE-VAZAR"


def _public_dns(host: str) -> list[str]:
    return ["10.20.30.40"]


def _registry(*, allowlist: tuple[str, ...] = (), dev: bool = False) -> ProviderRegistry:
    policy = DestinationPolicy(allowlist=allowlist, allow_loopback=dev, resolver=_public_dns)
    return ProviderRegistry(BUILTIN_PROVIDERS, policy=policy)


# ── allowlist hostil: o startup falha, com erro claro ───────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["*", "*.example.com", "https://gw.example.com", "gw.example.com:443", "gw.example.com/", "a b", "gw.example.com;x",
     "gw.example.com,*", "ok.example.com,,*", "gw.example.com\\x", "gw.exa%6dple.com", "xn--ä.example", "0.0.0.0/0",
     "10.0.0.0/8", "[::1]:80", "a" * 64 + ".example.com", ".".join(["a"] * 130)],
)
def test_app_factory_recusa_allowlist_hostil_no_create_app_antes_do_bind(
    monkeypatch: pytest.MonkeyPatch, value: str
):
    monkeypatch.setenv("MODEL_BASE_URL_ALLOWLIST", value)

    with pytest.raises(ValueError, match="MODEL_BASE_URL_ALLOWLIST inválida"):
        AppFactory().create_app()


def test_allowlist_com_metadata_sobe_mas_o_destino_continua_recusado(monkeypatch: pytest.MonkeyPatch):
    """Operador listar o IP de metadata não o libera: a guarda vale mesmo na allowlist."""
    monkeypatch.setenv("MODEL_BASE_URL_ALLOWLIST", "169.254.169.254, fe80::1")
    allowlist = AppConfig.load().model_base_url_allowlist
    registry = _registry(allowlist=allowlist)

    for url in ("http://169.254.169.254/v1", "http://[fe80::1]/v1"):
        with pytest.raises(InvalidModelConfigError, match="metadata"):
            registry.create_model(ModelConfig("openai_compatible", "m", base_url=url))


# ── modo dev local: só o literal canônico de loopback ───────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1:8000/v1",
        "http://2130706433:8000/v1",
        "http://0x7f.0.0.1:8000/v1",
        "http://0177.0.0.1:8000/v1",
        "http://[::ffff:127.0.0.1]:8000/v1",
        "http://[::ffff:7f00:1]:8000/v1",
        "http://localhost.evil.invalid:8000/v1",
        "http://127.0.0.1.evil.invalid:8000/v1",
        "http://0.0.0.0:8000/v1",
        "http://[::]:8000/v1",
        "http://10.0.0.5:8000/v1",
    ],
)
def test_modo_dev_local_nao_aceita_formas_nao_canonicas_de_loopback(url: str):
    registry = _registry(dev=True)

    with pytest.raises(InvalidModelConfigError):
        registry.create_model(ModelConfig("openai_compatible", "m", base_url=url))


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/v1", "http://127.1.2.3:8000/v1", "http://[::1]:8000/v1"])
def test_modo_dev_local_aceita_o_literal_canonico_e_o_producao_nao(url: str):
    ok = _registry(dev=True).create_model(ModelConfig("openai_compatible", "m", base_url=url))

    assert ok.base_url == url  # type: ignore[attr-defined]
    with pytest.raises(InvalidModelConfigError):
        _registry(dev=False).create_model(ModelConfig("openai_compatible", "m", base_url=url))


# ── N5 (conhecido) e variante no modo dev ───────────────────────────────────────────────────────


def test_n5_nome_com_ponto_final_nao_casa_com_ip_da_allowlist():
    registry = _registry(allowlist=("10.0.0.5",))

    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST"):
        registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://10.0.0.5./v1"))


def test_n5_nome_com_ponto_final_nao_conta_como_loopback_literal_no_dev():
    registry = _registry(dev=True)

    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST"):
        registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://127.0.0.1./v1"))


# ── achados do QA ───────────────────────────────────────────────────────────────────────────────


def test_bug_f2_02_qa_1_erro_da_allowlist_nao_ecoa_credencial_da_entrada(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_BASE_URL_ALLOWLIST", f"https://svc:{CRED}@gw.example.com")

    with pytest.raises(ValueError, match="MODEL_BASE_URL_ALLOWLIST") as caught:
        AppConfig.load()

    assert CRED not in str(caught.value)


def test_bug_f2_02_qa_2_check_host_falha_fechado_com_erro_do_resolver_que_nao_e_oserror():
    with pytest.raises(BlockedDestinationError):
        check_host("a" * 64 + ".example.com", resolver=system_resolver)
