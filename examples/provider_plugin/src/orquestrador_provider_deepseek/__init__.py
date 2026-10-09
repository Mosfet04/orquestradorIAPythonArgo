"""Plugin de exemplo: provider ``deepseek`` (``agno.models.deepseek.DeepSeek``, SDK ``openai``).

O módulo só expõe a ``ProviderSpec``; quem registra é o orquestrador, no mesmo registry dos
built-ins, e só se ``PLUGIN_ALLOWLIST`` tiver ``orquestrador-provider-deepseek:deepseek``. O
plugin não ganha atalho: ``base_url`` passa pela mesma guarda de destino (aqui só
``https://api.deepseek.com``, ``MODEL_BASE_URL_ALLOWLIST`` ou loopback no modo dev local) e a
chave vem de ``api_key_ref`` ou de ``DEEPSEEK_API_KEY``.

Roda dentro do processo do orquestrador, onde ``src`` é importável. Importe só o que a spec
precisa: a classe do SDK é importada pelo registry quando um agente usa o provider.
"""

from src.infrastructure.providers import ClassSpec, ProviderSpec

SPEC = ProviderSpec(
    id="deepseek",  # valor de factoryIaModel nos documentos (sem caixa); não pode repetir um built-in
    sdk_package="openai",  # citado no erro se o SDK não estiver instalado
    chat=ClassSpec(
        class_path="agno.models.deepseek.DeepSeek",  # conferido no agno 2.5.8
        params={"temperature": "temperature", "top_p": "top_p", "max_tokens": "max_tokens"},  # allowlist
        base_url_kwarg="base_url",
    ),
    default_hosts=frozenset({"api.deepseek.com"}),  # host do provider: base_url aceita só com https
    api_key_env="DEEPSEEK_API_KEY",  # chave do ambiente quando o documento não tem api_key_ref
)
