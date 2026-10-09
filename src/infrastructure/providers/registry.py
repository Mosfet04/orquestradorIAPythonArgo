"""Registry de providers de modelo e embedder (F2-02): uma ``ProviderSpec`` por provider.

Substitui as duas fábricas duplicadas (``ModelFactory``/``EmbedderModelFactory``): adicionar
provider é registrar uma spec (built-ins em ``builtins.py``; plugins por entry point em
``plugins.py``, no mesmo ``register``). Implementa as portas ``IModelFactory`` e ``IEmbedderFactory``.

Ordem do ``build`` (o segredo é a última coisa lida, só quando tudo o mais passou):
provider → destino (``base_url``: host do provider, ``MODEL_BASE_URL_ALLOWLIST`` ou loopback no
modo dev local, e nunca metadata/link-local) → ``model_params`` (allowlist da classe, só
números finitos) → import da classe (SDK ausente diz o pacote) → variáveis do operador →
chave (``api_key_ref`` via ``resolve_secret`` ou ``<PROVEDOR>_API_KEY`` do ambiente) →
construtor. Todo erro sai como ``InvalidModelConfigError`` com texto nosso, sem valor de
segredo nem exceção de SDK encadeada.

A recusa de destino vale com ou sem ``api_key_ref``: os SDKs leem chave do ambiente sozinhos
(``OPENAI_API_KEY``, ``OLLAMA_API_KEY``...) e a conversa também é dado sensível. ``base_url``
do operador (env, como ``OLLAMA_BASE_URL``) é confiável e não passa pela guarda.

Síncrono e com I/O (``file:``, DNS): no caminho async, chame via ``asyncio.to_thread``.
Resíduos para o F4-01: DNS rebinding (o SDK resolve de novo ao conectar) e redirect nos SDKs
em que desligá-lo não é um kwarg simples (openai, google-genai).
"""

from __future__ import annotations

import importlib
import math
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal, cast
from urllib.parse import urlsplit

from src.domain.entities.model_config import ModelConfig, ModelParams, ParamValue
from src.domain.ports.embedder_factory_port import IEmbedderFactory, TextEmbedder
from src.domain.ports.model_factory_port import ChatModel, IModelFactory, InvalidModelConfigError
from src.infrastructure.config.secrets import DEFAULT_SECRETS_DIR, SecretResolutionError, resolve_secret
from src.infrastructure.security.ssrf import (
    BlockedDestinationError,
    Resolver,
    check_host,
    is_blocked_address,
    is_loopback_host,
    literal_address,
    normalize_host,
    system_resolver,
)

Kind = Literal["chat", "embedder"]
_KIND_LABEL: dict[Kind, str] = {"chat": "modelo", "embedder": "embedder"}


@dataclass(frozen=True)
class OperatorEnv:
    """Variável de ambiente do operador repassada a um kwarg (ex.: ``AZURE_ENDPOINT``)."""

    kwarg: str
    env: str
    required: bool = False


@dataclass(frozen=True)
class ClassSpec:
    """Classe (caminho pontilhado) de um tipo de um provider e como montar os kwargs dela.

    ``params``: chave de ``model_params`` -> kwarg; ``"options.top_k"`` vai para o dict
    ``options``. ``base_url_kwarg``: ``None`` = a classe não aceita ``base_url``.
    ``api_key_kwarg``: ``None`` = não recebe chave (``api_key_ref`` é recusada).
    ``untrusted_url_kwargs``: kwargs extras quando a ``base_url`` vem da config (ex.: desligar
    redirect onde é um kwarg simples).
    """

    class_path: str
    params: Mapping[str, str] = field(default_factory=dict)
    base_url_kwarg: str | None = None
    api_key_kwarg: str | None = "api_key"
    operator_env: tuple[OperatorEnv, ...] = ()
    untrusted_url_kwargs: Mapping[str, ParamValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSpec:
    """Provider: id, aliases, pacote do SDK, classes de chat/embedder e destino da chave.

    ``api_key_env``: chave obrigatória do ambiente quando não há ``api_key_ref`` (``None`` =
    o provider funciona sem chave e nenhuma é passada). ``default_hosts``: hosts do próprio
    provider, aceitos em ``base_url`` sem allowlist.
    """

    id: str
    sdk_package: str
    chat: ClassSpec | None = None
    embedder: ClassSpec | None = None
    aliases: tuple[str, ...] = ()
    default_hosts: frozenset[str] = frozenset()
    api_key_env: str | None = None
    requires_base_url: bool = False


@dataclass(frozen=True)
class DestinationPolicy:
    """Onde uma ``base_url`` vinda da config pode apontar.

    ``allowlist``: ``MODEL_BASE_URL_ALLOWLIST`` já normalizada. ``allow_loopback``: só no modo
    dev local do F1-04. ``resolver``: injetável (testes nunca resolvem host real).
    """

    allowlist: tuple[str, ...] = ()
    allow_loopback: bool = False
    resolver: Resolver = system_resolver

    def check(self, base_url: str, default_hosts: frozenset[str]) -> None:
        """Recusa (``BlockedDestinationError``) destino fora do provider/allowlist ou de metadata.

        Host do provider exige ``https`` sempre, mesmo na allowlist (endpoint público nunca
        precisa de http e a chave iria em texto claro); os demais hosts da allowlist e o loopback
        no modo dev aceitam ``http`` (decisão do operador/dev). O DNS
        confere o host como está na URL (o SDK resolve esse nome); a comparação usa a forma
        normalizada.
        """
        parts = urlsplit(base_url)
        raw_host = parts.hostname or ""
        host = normalize_host(raw_host)
        literal = literal_address(host)
        if literal is not None and is_blocked_address(literal):
            raise BlockedDestinationError("destino de metadata/link-local recusado")
        provider_host = host in {normalize_host(item) for item in default_hosts}
        if provider_host and parts.scheme != "https":
            raise BlockedDestinationError("base_url no host do provider exige https")
        allowed = (
            provider_host
            or host in {normalize_host(item) for item in self.allowlist}
            or (self.allow_loopback and is_loopback_host(host))
        )
        if not allowed:
            raise BlockedDestinationError(
                "host da base_url não é do provider nem está em MODEL_BASE_URL_ALLOWLIST"
            )
        check_host(raw_host, resolver=self.resolver)


def normalize_provider_name(name: str) -> str:
    """Forma comparável de id/alias de provider (sem espaços nas pontas, sem caixa)."""
    return name.strip().lower()


def _set(kwargs: dict[str, object], target: str, value: ParamValue) -> None:
    """``"a.b"`` grava em ``kwargs["a"]["b"]``; ``"a"`` grava direto."""
    container, _, leaf = target.rpartition(".")
    if not container:
        kwargs[target] = value
        return
    nested = kwargs.setdefault(container, {})
    cast(dict[str, ParamValue], nested)[leaf] = value


class ProviderRegistry(IModelFactory, IEmbedderFactory):
    """Providers por id/alias (sem caixa); cria modelos e embedders a partir de ``ModelConfig``."""

    def __init__(
        self,
        specs: Iterable[ProviderSpec] = (),
        *,
        secrets_dir: str = DEFAULT_SECRETS_DIR,
        policy: DestinationPolicy | None = None,
        operator_base_urls: Mapping[str, str] | None = None,
    ) -> None:
        """``operator_base_urls``: id do provider -> URL do operador (ex.: ``OLLAMA_BASE_URL``)."""
        self._specs: dict[str, ProviderSpec] = {}
        self._secrets_dir = secrets_dir
        self._policy = policy or DestinationPolicy()
        self._operator_base_urls = {normalize_provider_name(k): v for k, v in (operator_base_urls or {}).items()}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ProviderSpec) -> None:
        """Registra ``spec``; id ou alias já usado (sem caixa) é ``ValueError``."""
        names = [normalize_provider_name(spec.id), *(normalize_provider_name(alias) for alias in spec.aliases)]
        taken = [name for name in names if name in self._specs]
        if taken or len(set(names)) != len(names):
            raise ValueError(f"Provider '{spec.id}': id ou alias duplicado ({', '.join(taken or names)})")
        for name in names:
            self._specs[name] = spec

    def __contains__(self, name: object) -> bool:
        """``name`` (sem caixa) é id ou alias de um provider registrado."""
        return isinstance(name, str) and normalize_provider_name(name) in self._specs

    def supported(self, kind: Kind) -> list[str]:
        """Ids e aliases que criam ``kind``, em ordem alfabética."""
        return sorted(name for name, spec in self._specs.items() if self._class_spec(spec, kind) is not None)

    def create_model(self, config: ModelConfig) -> ChatModel:
        return cast(ChatModel, self._build("chat", config))

    def create_embedder(self, config: ModelConfig) -> TextEmbedder:
        return cast(TextEmbedder, self._build("embedder", config))

    # ── build ───────────────────────────────────────────────────────

    @staticmethod
    def _class_spec(spec: ProviderSpec, kind: Kind) -> ClassSpec | None:
        return spec.chat if kind == "chat" else spec.embedder

    def _build(self, kind: Kind, config: ModelConfig) -> object:
        spec = self._specs.get(normalize_provider_name(config.provider))
        class_spec = self._class_spec(spec, kind) if spec is not None else None
        if spec is None or class_spec is None:
            raise InvalidModelConfigError(
                f"Tipo '{config.provider}' não suportado para {_KIND_LABEL[kind]}. "
                f"Suportados: {', '.join(self.supported(kind))}"
            )
        label = f"{_KIND_LABEL[kind]} do provider '{spec.id}'"
        kwargs: dict[str, object] = {"id": config.model_id}
        self._apply_destination(spec, class_spec, config.base_url, kwargs, label)
        _apply_params(class_spec, config.params, kwargs, label)
        model_class = _import_class(spec, class_spec, label)
        _apply_operator_env(class_spec, kwargs)
        self._apply_api_key(spec, class_spec, config.api_key_ref, kwargs, label)
        return _instantiate(model_class, kwargs, label)

    def _apply_destination(
        self,
        spec: ProviderSpec,
        class_spec: ClassSpec,
        base_url: str | None,
        kwargs: dict[str, object],
        label: str,
    ) -> None:
        if base_url is None:
            operator_url = self._operator_base_urls.get(normalize_provider_name(spec.id))
            if operator_url and class_spec.base_url_kwarg:
                kwargs[class_spec.base_url_kwarg] = operator_url
            elif spec.requires_base_url:
                raise InvalidModelConfigError(f"{label} exige base_url")
            return
        if class_spec.base_url_kwarg is None:
            raise InvalidModelConfigError(f"{label} não aceita base_url")
        reason: str | None = None
        try:
            self._policy.check(base_url, spec.default_hosts)
        except BlockedDestinationError as exc:
            reason = str(exc)  # texto nosso, sem o host
        if reason is not None:
            raise InvalidModelConfigError(f"{label}: {reason}")
        kwargs[class_spec.base_url_kwarg] = base_url
        for target, value in class_spec.untrusted_url_kwargs.items():
            _set(kwargs, target, value)

    def _apply_api_key(
        self,
        spec: ProviderSpec,
        class_spec: ClassSpec,
        api_key_ref: str | None,
        kwargs: dict[str, object],
        label: str,
    ) -> None:
        key_kwarg = class_spec.api_key_kwarg
        if api_key_ref is not None:
            if key_kwarg is None:
                raise InvalidModelConfigError(f"{label} não aceita api_key_ref")
            reason: str | None = None
            try:
                kwargs[key_kwarg] = resolve_secret(api_key_ref, secrets_dir=self._secrets_dir)
            except SecretResolutionError as exc:
                reason = str(exc)  # nome da variável ou caminho, nunca o valor
            if reason is not None:
                raise InvalidModelConfigError(f"{label}: {reason}")
            return
        if key_kwarg is None or spec.api_key_env is None:
            return
        value = os.getenv(spec.api_key_env)
        if not value:
            raise InvalidModelConfigError(f"{spec.api_key_env} não configurado")
        kwargs[key_kwarg] = value


def _apply_params(class_spec: ClassSpec, params: ModelParams, kwargs: dict[str, object], label: str) -> None:
    allowed = class_spec.params
    for key, value in params.items():
        target = allowed.get(key)
        if target is None:
            # Sem a chave recusada: o documento é de quem grava config, não necessariamente limpo.
            raise InvalidModelConfigError(
                f"model_params do {label} com chave não permitida; permitidas: "
                f"{', '.join(sorted(allowed)) or 'nenhuma'}"
            )
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidModelConfigError(f"model_params do {label}: número não finito (NaN/infinito)")
        _set(kwargs, target, value)


def _import_class(spec: ProviderSpec, class_spec: ClassSpec, label: str) -> Callable[..., object]:
    module_path, _, class_name = class_spec.class_path.rpartition(".")
    missing_sdk = False
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        missing_sdk = True  # erro levantado fora do except: a mensagem do import não vai junto
    if missing_sdk:
        raise InvalidModelConfigError(
            f"{label} indisponível: SDK ausente. Instale com: pip install {spec.sdk_package}"
        )
    model_class = getattr(module, class_name, None)
    if not callable(model_class):
        raise InvalidModelConfigError(f"{label}: classe {class_spec.class_path} não encontrada")
    return cast(Callable[..., object], model_class)


def _apply_operator_env(class_spec: ClassSpec, kwargs: dict[str, object]) -> None:
    for item in class_spec.operator_env:
        if item.kwarg in kwargs:
            continue  # base_url da config já preencheu
        value = os.getenv(item.env)
        if value:
            kwargs[item.kwarg] = value
        elif item.required:
            raise InvalidModelConfigError(f"{item.env} não configurado")


def _instantiate(model_class: Callable[..., object], kwargs: dict[str, object], label: str) -> object:
    error_type: str | None = None
    try:
        return model_class(**kwargs)
    except Exception as exc:  # noqa: BLE001 - qualquer falha do SDK vira erro nosso, sem o texto dele
        error_type = type(exc).__name__  # o texto do SDK pode trazer a chave: só o tipo sai daqui
    raise InvalidModelConfigError(f"{label}: falha ao instanciar a classe ({error_type})")
