"""Configuração neutra de modelo (LLM ou embedder), sem tipo de terceiro (F2-01).

``provider`` e ``model_id`` vêm como estão no documento (o mapper legado traduz
``factory_ia_model``/``model``); a normalização de provider é da fábrica. A chave de API só
entra por referência (``api_key_ref``: ``env:<PROVEDOR>_API_KEY`` ou ``file:/caminho``), nunca o
valor; quem resolve a referência é ``src.infrastructure.config.secrets``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TypeAlias
from urllib.parse import SplitResult, urlsplit

from src.domain.entities.validation import InvalidField, require_text

ParamValue: TypeAlias = str | int | float | bool

ENV_SCHEME = "env"
FILE_SCHEME = "file"
SECRET_REF_FORMATS = (f"{ENV_SCHEME}:<PROVEDOR>_API_KEY", f"{FILE_SCHEME}:<caminho em SECRETS_DIR>")
"""Formatos de ``api_key_ref`` que ``parse_secret_ref`` aceita, para quem monta a config (``/admin/schema``)."""
# Quem grava config não é confiável para ler qualquer variável do processo (API_KEY_ADMIN,
# MONGO_CONNECTION_STRING...): ``env:`` só aponta para chave de provider.
_ENV_VAR_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,119}_API_KEY")
_REF_HINT = (
    "use 'env:<PROVEDOR>_API_KEY' (maiúsculas, ex.: env:OPENAI_API_KEY) ou "
    "'file:/caminho/absoluto' (o valor do segredo nunca vai na configuração)"
)
MAX_BASE_URL_LENGTH = 2048


def parse_secret_ref(ref: object) -> tuple[str, str]:
    """``api_key_ref`` -> ``(esquema, alvo)``. Inválida: ``InvalidField`` sem o valor.

    O valor nunca entra na mensagem: quem erra o formato costuma ter colado o próprio segredo.
    """
    if isinstance(ref, str):
        scheme, sep, target = ref.partition(":")
        if sep and scheme == ENV_SCHEME and _ENV_VAR_NAME.fullmatch(target):
            return scheme, target
        if (
            sep
            and scheme == FILE_SCHEME
            and "\x00" not in target
            and PurePosixPath(target).is_absolute()
        ):
            return scheme, target
    raise InvalidField("api_key_ref", f"api_key_ref inválido: {_REF_HINT}")


class ModelParams(Mapping[str, ParamValue]):
    """Parâmetros escalares do modelo: mapeamento somente leitura e com hash.

    Copiável (``deepcopy``/``pickle``/``dataclasses.asdict``) e na ordem do documento.
    """

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, ParamValue] | None = None) -> None:
        """``None`` = vazio. Chave texto não vazia e valor escalar; senão ``InvalidField`` sem eco."""
        self._items: dict[str, ParamValue] = _validated_params(items)

    def __getitem__(self, key: str) -> ParamValue:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(frozenset(self._items.items()))

    def __repr__(self) -> str:
        return f"ModelParams({self._items!r})"

    def __reduce__(self) -> tuple[type[ModelParams], tuple[dict[str, ParamValue]]]:
        return (ModelParams, (dict(self._items),))


def _validated_params(params: object) -> dict[str, ParamValue]:
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise InvalidField("model_params", "model_params deve ser um objeto (chave -> valor)")
    for key, value in params.items():
        if not isinstance(key, str) or not key:
            raise InvalidField("model_params", "model_params: toda chave deve ser texto não vazio")
        # Sem a chave na mensagem: o documento é de quem grava config, não necessariamente limpo.
        if not isinstance(value, (str, int, float, bool)):
            raise InvalidField("model_params", "model_params: todo valor deve ser texto, número ou booleano")
    return dict(params)


def _split_url(url: str) -> SplitResult | None:
    """``urlsplit`` com a porta validada; ``None`` se não parseia (o erro do stdlib cita a URL)."""
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return None
    return parts


def _base_url_text(url: object) -> str:
    r"""A ``base_url`` como texto curto, sem espaço, ``\`` nem caractere de controle (antes de parsear)."""
    if not isinstance(url, str):
        raise InvalidField("base_url", "base_url deve ser texto")
    if len(url) > MAX_BASE_URL_LENGTH:
        raise InvalidField("base_url", f"base_url maior que {MAX_BASE_URL_LENGTH} caracteres")
    if any(ch.isspace() or not ch.isprintable() for ch in url) or "\\" in url:
        raise InvalidField("base_url", "base_url não pode ter espaço, '\\' nem caractere de controle")
    return url


def _check_base_url(url: object) -> None:
    r"""``http(s)://host[:porta][/caminho]``; sem credencial, query ou fragmento (lugar de segredo).

    Host só ASCII sem ``%`` e sem ``\`` em lugar nenhum: parsers diferentes (stdlib x WHATWG)
    leriam hosts diferentes. Destino (SSRF, allowlist) é política de quem conecta (F2-02/F4-01).
    """
    text = _base_url_text(url)
    parts = _split_url(text)
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise InvalidField("base_url", "base_url deve ser http(s)://host[:porta][/caminho]")
    if "@" in parts.netloc:
        raise InvalidField("base_url", "base_url não pode ter credenciais; use api_key_ref")
    if "?" in text or "#" in text:
        raise InvalidField("base_url", "base_url não pode ter query nem fragmento")
    if not parts.netloc.isascii() or "%" in parts.netloc:
        raise InvalidField("base_url", "base_url: host só em ASCII, sem '%' (IDN em punycode)")
    if parts.port == 0 or parts.netloc.endswith(":"):
        raise InvalidField("base_url", "base_url com porta vazia ou 0")


@dataclass(frozen=True, init=False)
class ModelConfig:
    """Modelo de chat ou embedder: provider, id, parâmetros escalares, endpoint e chave por ref.

    Imutável; igualdade e hash por valor (inclusive ``params``). ``repr`` omite ``params`` e
    ``api_key_ref``. ``params=None`` vale como vazio. A recusa é ``InvalidField`` com o nome do campo
    no documento (``factory_ia_model``, ``model``, ``model_params``, ``base_url``, ``api_key_ref``).
    """

    provider: str
    model_id: str
    params: ModelParams = field(default_factory=ModelParams, repr=False)
    base_url: str | None = None
    api_key_ref: str | None = field(default=None, repr=False)

    def __init__(
        self,
        provider: str,
        model_id: str,
        params: Mapping[str, ParamValue] | None = None,
        base_url: str | None = None,
        api_key_ref: str | None = None,
    ) -> None:
        require_text(provider, "factory_ia_model", "Provider do modelo")
        require_text(model_id, "model", "ID do modelo")
        checked = ModelParams(params)
        if base_url is not None:
            _check_base_url(base_url)
        if api_key_ref is not None:
            parse_secret_ref(api_key_ref)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model_id", model_id)
        object.__setattr__(self, "params", checked)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "api_key_ref", api_key_ref)
