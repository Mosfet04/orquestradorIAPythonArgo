"""FA-06: escrita de tool pela API: destino da ``route`` (SSRF) e headers por referência.

Caso de uso real (``build_config_admin``, mappers do store, repositórios em memória) com a guarda de destino
real sobre um resolver falso. Valores de header são de teste.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.application.use_cases.config_admin_use_case import ConfigAdmin, InvalidConfig
from src.infrastructure.dependency_injection import build_config_admin
from tests.fakes.admin_config_app import fake_audit_log, fake_runtime_swap, route_guard, tool, tool_body
from tests.fakes.dns import no_dns
from tests.fakes.logger import RecordingLogger
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)

TRUSTED_ROUTE = "https://api.interna.example/{cep}"
SECRET = "valor-de-teste-do-segredo"  # noqa: S105 - marcador de vazamento, não é segredo


def _admin(tools: InMemoryToolRepository, *, guard=None, logger: RecordingLogger | None = None) -> ConfigAdmin:
    agents, teams = InMemoryAgentConfigRepository(), InMemoryTeamConfigRepository()
    return build_config_admin(
        agents,
        teams,
        tools,
        logger or RecordingLogger(),
        runtime=fake_runtime_swap(agents, teams),
        audit=fake_audit_log(),
        route_guard=guard or route_guard("api.interna.example"),
    )


async def _refused_field(admin: ConfigAdmin, body: dict[str, object]) -> str | None:
    with pytest.raises(InvalidConfig) as caught:
        await admin.tools.create(body, actor="admin")
    return caught.value.field


# ── route ──────────────────────────────────────────────────────────


async def test_route_para_metadata_e_recusada_com_o_campo_e_nada_e_gravado():
    tools = InMemoryToolRepository()
    logger = RecordingLogger()
    admin = _admin(tools, logger=logger)

    field = await _refused_field(admin, tool_body(route="http://169.254.169.254/latest/meta-data/{cep}"))

    assert field == "route"
    assert await tools.list_all() == []
    assert "169.254" not in repr(logger.records)


async def test_route_com_nome_que_resolve_para_ip_privado_e_recusada():
    tools = InMemoryToolRepository()
    admin = _admin(tools, guard=route_guard(resolver=lambda host: ["10.0.0.7"]))

    assert await _refused_field(admin, tool_body(route="https://interno.example.invalid/{cep}")) == "route"
    assert await tools.list_all() == []


async def test_route_com_credencial_e_recusada_e_nao_chega_ao_historico():
    """Resíduo do FA-05: segredo colado na URL ficaria no documento e no diário."""
    tools = InMemoryToolRepository()
    admin = _admin(tools)

    body = tool_body(route=f"https://usuario:{SECRET}@api.example.invalid/{{cep}}")
    assert await _refused_field(admin, body) == "route"
    assert await admin.tools.history("cep") == []


async def test_alteracao_tambem_passa_pela_guarda():
    tools = InMemoryToolRepository([tool("cep")])
    admin = _admin(tools)
    shown = dict((await admin.tools.get("cep")).entity)

    with pytest.raises(InvalidConfig) as caught:
        await admin.tools.replace("cep", {**shown, "route": "http://10.1.2.3/{cep}"}, 0, actor="admin")

    assert caught.value.field == "route"
    assert [i.entity.route for i in await tools.list_all()] == [tool("cep").route]


# ── headers ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["Authorization", "authorization", "Proxy-Authorization", "Cookie", "X-Api-Key", "X-Auth-Token", "api_key"]
)
async def test_header_sensivel_em_texto_novo_e_recusado(name: str):
    tools = InMemoryToolRepository()
    admin = _admin(tools)

    assert await _refused_field(admin, tool_body(route=TRUSTED_ROUTE, headers={name: SECRET})) == "headers"
    assert await tools.list_all() == []


async def test_header_por_referencia_e_gravado_e_mostrado_como_referencia():
    """A referência não é segredo (como o ``api_key_ref``): o GET e o histórico a mostram; texto sai ``***``."""
    tools = InMemoryToolRepository()
    admin = _admin(tools)
    headers = {"Authorization": "env:CEP_API_KEY", "X-Token-Arquivo": "file:/run/secrets/cep", "Accept": "text/plain"}

    written = await admin.tools.create(tool_body(route=TRUSTED_ROUTE, headers=headers), actor="admin")

    expected = {"Authorization": "env:CEP_API_KEY", "X-Token-Arquivo": "file:/run/secrets/cep", "Accept": "***"}
    assert written.item.entity["headers"] == expected
    assert (await admin.tools.get("cep")).entity["headers"] == expected
    [entry] = await admin.tools.history("cep")
    assert entry.document is not None and entry.document["headers"] == expected
    assert [i.entity.headers for i in await tools.list_all()] == [headers]


async def test_header_por_referencia_para_host_fora_da_allowlist_e_recusado():
    """Sem isso, quem tem a chave admin mandaria qualquer ``*_API_KEY`` do processo para um host seu."""
    tools = InMemoryToolRepository()
    admin = _admin(tools)
    body = tool_body(route="https://publico.example.invalid/{cep}", headers={"X-Extra": "env:OPENAI_API_KEY"})

    assert await _refused_field(admin, body) == "headers"
    assert await tools.list_all() == []


@pytest.mark.parametrize(
    "value", ["env:cep_api_key", "env:MONGO_CONNECTION_STRING", "env:", "file:relativo/cep", "file:", "env:A_API_KEY\n"]
)
async def test_referencia_fora_do_formato_e_recusada(value: str):
    admin = _admin(InMemoryToolRepository())

    assert await _refused_field(admin, tool_body(route=TRUSTED_ROUTE, headers={"Authorization": value})) == "headers"


@pytest.mark.parametrize("name", ["X Api", "X-Api\n", "X-Api\r\nHost: evil", "", "Host:", "Ção", "X-\x00"])
async def test_nome_de_header_que_nao_e_token_http_e_recusado_sem_resolver_dns(name: str):
    admin = _admin(InMemoryToolRepository(), guard=route_guard(resolver=no_dns))

    body = tool_body(route="https://api.example.invalid/{cep}", headers={name: "x"})
    assert await _refused_field(admin, body) == "headers"


@pytest.mark.parametrize("value", ["a\r\nX-Injetado: 1", "a\nb", "a\x00b", "a\x7fb"])
async def test_valor_de_header_com_controle_e_recusado(value: str):
    admin = _admin(InMemoryToolRepository())

    body = tool_body(route=TRUSTED_ROUTE, headers={"X-Extra": value})
    assert await _refused_field(admin, body) == "headers"


async def test_mascara_mantem_a_referencia_gravada_na_mesma_origem():
    tools = InMemoryToolRepository()
    admin = _admin(tools)
    await admin.tools.create(
        tool_body(route=TRUSTED_ROUTE, headers={"Authorization": "env:CEP_API_KEY", "Accept": "text/plain"}),
        actor="admin",
    )
    shown = dict((await admin.tools.get("cep")).entity)

    await admin.tools.replace("cep", {**shown, "description": "nova"}, 1, actor="admin")

    assert [i.entity.headers for i in await tools.list_all()] == [
        {"Authorization": "env:CEP_API_KEY", "Accept": "text/plain"}
    ]


async def test_mascara_nao_mantem_referencia_para_host_que_nao_e_do_operador():
    """Legado ou allowlist encolhida: a referência gravada não segue, pela máscara, para host público."""
    headers = {"Authorization": "env:CEP_API_KEY"}
    stored = replace(tool("cep", headers=headers), route="https://publico.example.invalid/{cep}")
    tools = InMemoryToolRepository([stored])
    admin = _admin(tools, guard=route_guard())
    shown = dict((await admin.tools.get("cep")).entity)

    with pytest.raises(InvalidConfig) as caught:
        await admin.tools.replace("cep", {**shown, "headers": {"Authorization": "***"}}, 0, actor="admin")

    assert caught.value.field == "headers"
    assert [(i.entity.route, i.entity.headers) for i in await tools.list_all()] == [(stored.route, headers)]


async def test_header_sensivel_legado_em_texto_segue_mantido_pela_mascara():
    """Compatibilidade: o texto gravado antes do FA-06 continua lido e o ciclo GET -> PUT o mantém."""
    legacy = tool("cep", headers={"Authorization": SECRET})
    tools = InMemoryToolRepository([legacy])
    admin = _admin(tools)
    shown = dict((await admin.tools.get("cep")).entity)

    assert shown["headers"] == {"Authorization": "***"}
    await admin.tools.replace("cep", {**shown, "description": "nova"}, 0, actor="admin")

    assert [i.entity.headers for i in await tools.list_all()] == [{"Authorization": SECRET}]
