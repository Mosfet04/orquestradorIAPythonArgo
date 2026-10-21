"""QA do FA-06, segurança: bordas da escrita de tool pela API (SSRF na ``route`` e header por referência).

Borda real (CORS + auth) com repositórios em memória, a guarda real sobre DNS falso e o runtime falso
(``tests/fakes/admin_config_app.py``). Valores são de teste. Cobre o que o dev não cobriu: restore de versão
cuja ``route`` hoje é recusada, recusa sem efeito colateral (nada gravado, nenhuma entrada de auditoria, runtime
sem troca), documento legado lido e removível, header sensível por variação de caixa, placeholder, prazo de DNS
sob a trava de escrita e cancelamento da guarda.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from tests.fakes.admin_config_app import ADMIN, admin_world, if_match, route_guard, tool, tool_body
from tests.fakes.dns import PUBLIC_IP

BASE = "/admin/config/tools"
INVALID_ROUTE = {"detail": "Configuração inválida", "field": "route"}
INVALID_HEADERS = {"detail": "Configuração inválida", "field": "headers"}
TRUSTED = "https://api.interna.example/{cep}"
SECRET = "valor-de-teste-do-segredo"  # noqa: S105 - marcador de vazamento, não é segredo


class _Dns:
    """Resolver falso mutável: ``answers`` troca a resposta de um host entre uma escrita e outra."""

    def __init__(self) -> None:
        self.answers: dict[str, list[str]] = {}

    def __call__(self, host: str) -> list[str]:
        return self.answers.get(host, [PUBLIC_IP])


async def _history_total(world, tool_id: str = "cep") -> int:
    response = await world.client.get(f"{BASE}/{tool_id}/history", headers=ADMIN)
    return int(response.json()["total"])


# ── recusa sem efeito colateral ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body_extra", "field"),
    [
        ({"route": "http://169.254.169.254/{cep}"}, "route"),
        ({"route": "https://usuario:senha@api.interna.example/{cep}"}, "route"),
        ({"route": TRUSTED, "headers": {"Authorization": SECRET}}, "headers"),
        ({"route": "https://publico.example.invalid/{cep}", "headers": {"X-Extra": "env:OPENAI_API_KEY"}}, "headers"),
    ],
)
async def test_put_recusado_nao_grava_nao_audita_e_nao_troca_o_runtime(
    monkeypatch: pytest.MonkeyPatch, body_extra: dict[str, object], field: str
):
    async with admin_world(monkeypatch, tools=[tool("cep")], guard=route_guard("api.interna.example")) as world:
        before = (await world.client.get(f"{BASE}/cep", headers=ADMIN)).json()
        revision, builds = world.runtime.revision, len(world.runtime.agent_builds)
        refused = await world.client.put(f"{BASE}/cep", json=tool_body(**body_extra), headers=if_match(0))
        after = (await world.client.get(f"{BASE}/cep", headers=ADMIN)).json()
        total = await _history_total(world)

    assert (refused.status_code, refused.json()["field"]) == (422, field)
    assert after == before  # mesma versão e mesmo documento
    assert total == 0  # nenhuma entrada de auditoria
    assert (world.runtime.revision, len(world.runtime.agent_builds)) == (revision, builds)
    assert SECRET not in refused.text + repr(world.logger.records)


async def test_post_recusado_nao_deixa_entrada_de_auditoria_nem_troca_o_runtime(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        revision = world.runtime.revision
        refused = await world.client.post(BASE, json=tool_body(route="http://10.0.0.5/{cep}"), headers=ADMIN)
        history = await world.client.get(f"{BASE}/cep/history", headers=ADMIN)

    assert (refused.status_code, refused.json()) == (422, INVALID_ROUTE)
    assert history.json()["total"] == 0
    assert world.runtime.revision == revision
    assert await world.tools.list_all() == []


# ── restore ──────────────────────────────────────────────────────────────────────────────────────────


async def test_restore_de_versao_cuja_route_hoje_e_recusada_responde_422_sem_gravar(monkeypatch: pytest.MonkeyPatch):
    """O DNS do host da versão 1 passou a apontar para IP privado: restaurá-la reabriria o SSRF."""
    dns = _Dns()
    async with admin_world(monkeypatch, guard=route_guard(resolver=dns)) as world:
        created = await world.client.post(
            BASE, json=tool_body(route="https://antiga.example.invalid/{cep}"), headers=ADMIN
        )
        replaced = await world.client.put(
            BASE + "/cep", json=tool_body(route="https://nova.example.invalid/{cep}"), headers=if_match(1)
        )
        dns.answers["antiga.example.invalid"] = ["10.1.2.3"]
        revision, total = world.runtime.revision, await _history_total(world)

        restored = await world.client.post(f"{BASE}/cep/history/1/restore", headers=if_match(2))
        [stored] = await world.tools.list_all()
        total_after = await _history_total(world)

    assert (created.status_code, replaced.status_code) == (201, 200)
    assert (restored.status_code, restored.json()) == (422, INVALID_ROUTE)
    assert stored.entity.route == "https://nova.example.invalid/{cep}"
    assert (stored.version, world.runtime.revision, total_after) == (2, revision, total)


async def _forge_entry(world, version: int, **document: object) -> None:
    """Grava pela porta do diário uma entrada mais nova da ``version`` com o documento alterado (diário legado)."""
    history = await world.audit.history("tools", "cep")
    [original, *_] = [entry for entry in history if entry.version == version]
    assert original.document is not None
    await world.audit.record(replace(original, document={**original.document, **document}))


async def test_restore_de_versao_com_route_com_credencial_gravada_antes_do_fa06_e_recusado(
    monkeypatch: pytest.MonkeyPatch,
):
    """Entrada de auditoria do FA-05 com segredo na URL (resíduo do FA-05): restaurar não a regrava."""
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(BASE, json=tool_body(), headers=ADMIN)
        await _forge_entry(world, 1, route=f"https://usuario:{SECRET}@api.example.invalid/{{cep}}")
        restored = await world.client.post(f"{BASE}/cep/history/1/restore", headers=if_match(1))
        [stored] = await world.tools.list_all()

    assert created.status_code == 201
    assert (restored.status_code, restored.json()) == (422, INVALID_ROUTE)
    assert (stored.version, stored.entity.route) == (1, "https://api.example.invalid/{cep}")
    assert SECRET not in restored.text + repr(world.logger.records)


async def test_restore_de_versao_valida_passa_pela_guarda_e_grava(monkeypatch: pytest.MonkeyPatch):
    """Controle dos testes acima: a mesma pilha restaura quando o destino segue permitido."""
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example")) as world:
        first = tool_body(route=TRUSTED, headers={"X-Extra": "env:CEP_API_KEY"})
        await world.client.post(BASE, json=first, headers=ADMIN)
        await world.client.put(
            BASE + "/cep",
            json=tool_body(route=TRUSTED, description="v2", headers={"X-Extra": "***"}),
            headers=if_match(1),
        )
        restored = await world.client.post(f"{BASE}/cep/history/1/restore", headers=if_match(2))
        [stored] = await world.tools.list_all()

    assert restored.status_code == 200
    assert (stored.version, stored.entity.description) == (3, "Busca CEP")
    assert stored.entity.headers == {"X-Extra": "env:CEP_API_KEY"}


async def test_restore_de_header_por_referencia_para_host_que_deixou_de_ser_do_operador_e_recusado(
    monkeypatch: pytest.MonkeyPatch,
):
    """A allowlist encolheu depois: a referência da versão antiga não vai mais para aquele host."""
    async with admin_world(monkeypatch) as world:  # allowlist vazia
        await world.client.post(BASE, json=tool_body(route="https://publica.example.invalid/{cep}"), headers=ADMIN)
        await _forge_entry(world, 1, headers={"Authorization": "env:CEP_API_KEY"})
        restored = await world.client.post(f"{BASE}/cep/history/1/restore", headers=if_match(1))
        [stored] = await world.tools.list_all()

    assert (restored.status_code, restored.json()) == (422, INVALID_HEADERS)
    assert (stored.version, stored.entity.headers) == (1, {})


# ── documento legado ─────────────────────────────────────────────────────────────────────────────────


async def test_documento_legado_com_route_proibida_segue_lido_e_pode_ser_desativado_e_removido(
    monkeypatch: pytest.MonkeyPatch,
):
    """A guarda vale só na escrita que grava route; desativar e remover o legado não pode ficar preso nela."""
    legacy = replace(tool("antiga"), route="http://169.254.169.254/{cep}")
    gone = replace(tool("some"), route="http://10.0.0.5/{cep}")
    async with admin_world(monkeypatch, tools=[legacy, gone]) as world:
        listed = await world.client.get(BASE, headers=ADMIN)
        deactivated = await world.client.delete(f"{BASE}/antiga", headers=if_match(0))
        removed = await world.client.delete(f"{BASE}/some?hard=true", headers=if_match(0))

    assert listed.status_code == 200
    assert {item["route"] for item in listed.json()["items"]} == {"http://169.254.169.254/{cep}", "http://10.0.0.5/{cep}"}
    assert (deactivated.status_code, removed.status_code) == (200, 204)
    assert [i.entity.active for i in await world.tools.list_all()] == [False]


async def test_put_sobre_legado_com_route_proibida_e_recusado_ate_corrigir_a_route(monkeypatch: pytest.MonkeyPatch):
    legacy = replace(tool("antiga"), route="http://169.254.169.254/{cep}")
    async with admin_world(monkeypatch, tools=[legacy]) as world:
        shown = (await world.client.get(f"{BASE}/antiga", headers=ADMIN)).json()
        same = await world.client.put(f"{BASE}/antiga", json={**shown, "description": "x"}, headers=if_match(0))
        fixed = await world.client.put(
            f"{BASE}/antiga", json={**shown, "route": "https://api.example.invalid/{cep}"}, headers=if_match(0)
        )

    assert (same.status_code, same.json()) == (422, INVALID_ROUTE)
    assert fixed.status_code == 200


async def test_legado_com_texto_que_parece_referencia_invalida_sai_mascarado(monkeypatch: pytest.MonkeyPatch):
    legacy = tool("cep", headers={"X-Extra": "env:minusculo", "Authorization": SECRET})
    async with admin_world(monkeypatch, tools=[legacy]) as world:
        got = await world.client.get(f"{BASE}/cep", headers=ADMIN)

    assert got.json()["headers"] == {"X-Extra": "***", "Authorization": "***"}
    assert SECRET not in got.text


# ── headers ──────────────────────────────────────────────────────────────────────────────────────────


async def test_nome_sensivel_em_outra_caixa_junto_de_referencia_e_recusado(monkeypatch: pytest.MonkeyPatch):
    """Dois nomes que o HTTP trata como o mesmo header: o texto não passa por baixo da referência."""
    headers = {"Authorization": "env:CEP_API_KEY", "authorization": SECRET}
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example")) as world:
        refused = await world.client.post(BASE, json=tool_body(route=TRUSTED, headers=headers), headers=ADMIN)

    assert (refused.status_code, refused.json()) == (422, INVALID_HEADERS)
    assert SECRET not in refused.text and await world.tools.list_all() == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("X-{cep}", "x"),  # placeholder no nome do header
        ("X-Extra", "env:CEP_{cep}_API_KEY"),  # placeholder dentro da referência
    ],
)
async def test_placeholder_no_header_e_recusado(monkeypatch: pytest.MonkeyPatch, name: str, value: str):
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example")) as world:
        refused = await world.client.post(BASE, json=tool_body(route=TRUSTED, headers={name: value}), headers=ADMIN)

    assert (refused.status_code, refused.json()) == (422, INVALID_HEADERS)
    assert await world.tools.list_all() == []


async def test_file_com_chaves_no_caminho_e_aceito_e_gravado_como_esta(monkeypatch: pytest.MonkeyPatch):
    """``{cep}`` num caminho ``file:`` é nome de arquivo legal: aceito e gravado como está."""
    ref = {"X-Extra": "file:/run/secrets/{cep}"}
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example")) as world:
        created = await world.client.post(BASE, json=tool_body(route=TRUSTED, headers=ref), headers=ADMIN)
        [stored] = await world.tools.list_all()

    assert created.status_code == 201
    assert stored.entity.headers == ref


async def test_file_com_byte_nulo_no_caminho_e_recusado(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example")) as world:
        refused = await world.client.post(
            BASE, json=tool_body(route=TRUSTED, headers={"X-Extra": "file:/run/secrets/cep\x00"}), headers=ADMIN
        )

    assert (refused.status_code, refused.json()) == (422, INVALID_HEADERS)
    assert await world.tools.list_all() == []


async def test_header_por_referencia_com_valor_mascarado_em_tool_nova_e_recusado(monkeypatch: pytest.MonkeyPatch):
    """``***`` só mantém o que já está gravado: numa tool nova não há o que manter."""
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example")) as world:
        refused = await world.client.post(
            BASE, json=tool_body(route=TRUSTED, headers={"X-Extra": "***"}), headers=ADMIN
        )

    assert (refused.status_code, refused.json()) == (422, INVALID_HEADERS)


async def test_tool_inativa_tambem_passa_pela_guarda_ao_ser_gravada(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        refused = await world.client.post(
            BASE, json=tool_body(route="http://169.254.169.254/{cep}", active=False), headers=ADMIN
        )

    assert (refused.status_code, refused.json()) == (422, INVALID_ROUTE)
    assert await world.tools.list_all() == []


# ── DNS lento, trava de escrita e cancelamento ───────────────────────────────────────────────────────


async def test_dns_sem_resposta_recusa_e_a_trava_de_escrita_segue_livre(monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()

    def resolver(host: str) -> list[str]:
        if host == "lento.example.invalid":
            release.wait(timeout=10)
        return [PUBLIC_IP]

    guard = replace(route_guard(resolver=resolver), dns_timeout=0.5)
    try:
        async with admin_world(monkeypatch, guard=guard) as world:
            slow = await world.client.post(
                BASE, json=tool_body(route="https://lento.example.invalid/{cep}"), headers=ADMIN
            )
            fast = await asyncio.wait_for(
                world.client.post(BASE, json=tool_body(route="https://rapido.example.invalid/{cep}"), headers=ADMIN),
                timeout=5,
            )
    finally:
        release.set()

    assert (slow.status_code, slow.json()) == (422, INVALID_ROUTE)
    assert fast.status_code == 201


async def test_escritas_concorrentes_com_dns_lento_nao_travam_umas_as_outras_para_sempre(
    monkeypatch: pytest.MonkeyPatch,
):
    release = threading.Event()

    def resolver(host: str) -> list[str]:
        if host.startswith("lento"):
            release.wait(timeout=10)
        return [PUBLIC_IP]

    guard = replace(route_guard(resolver=resolver), dns_timeout=0.5)
    try:
        async with admin_world(monkeypatch, guard=guard) as world:
            hosts = [f"{'lento' if i % 2 else 'rapido'}{i}.example.invalid" for i in range(4)]
            bodies = [tool_body(id=f"t{i}", name=f"t{i}", route=f"https://{h}/{{cep}}") for i, h in enumerate(hosts)]
            responses = await asyncio.wait_for(
                asyncio.gather(*(world.client.post(BASE, json=b, headers=ADMIN) for b in bodies)), timeout=10
            )
    finally:
        release.set()

    assert sorted(r.status_code for r in responses) == [201, 201, 422, 422]
    assert sorted(i.entity.id for i in await world.tools.list_all()) == ["t0", "t2"]


async def test_cancelar_a_escrita_durante_o_dns_libera_a_trava_e_nao_grava(monkeypatch: pytest.MonkeyPatch):
    release = threading.Event()
    started = threading.Event()

    def resolver(host: str) -> list[str]:
        if host == "lento.example.invalid":
            started.set()
            release.wait(timeout=10)
        return [PUBLIC_IP]

    try:
        async with admin_world(monkeypatch, guard=route_guard(resolver=resolver)) as world:
            pending = asyncio.create_task(
                world.client.post(BASE, json=tool_body(route="https://lento.example.invalid/{cep}"), headers=ADMIN)
            )
            assert await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            after = await asyncio.wait_for(
                world.client.post(BASE, json=tool_body(route="https://rapido.example.invalid/{cep}"), headers=ADMIN),
                timeout=5,
            )
            stored = [i.entity.route for i in await world.tools.list_all()]
    finally:
        release.set()

    assert after.status_code == 201
    assert stored == ["https://rapido.example.invalid/{cep}"]


async def test_mascara_de_referencia_nao_acompanha_a_route_para_outro_host_nem_para_outra_porta(
    monkeypatch: pytest.MonkeyPatch,
):
    """O ``***`` mantém a referência só na mesma origem; outro host do operador exige repetir a referência."""
    headers = {"Authorization": "env:CEP_API_KEY"}
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example", "outra.interna.example")) as world:
        await world.client.post(BASE, json=tool_body(route=TRUSTED, headers=headers), headers=ADMIN)
        masked = {"Authorization": "***"}
        other_host = await world.client.put(
            BASE + "/cep",
            json=tool_body(route="https://outra.interna.example/{cep}", headers=masked),
            headers=if_match(1),
        )
        other_port = await world.client.put(
            BASE + "/cep",
            json=tool_body(route="https://api.interna.example:8443/{cep}", headers=masked),
            headers=if_match(1),
        )
        explicit = await world.client.put(
            BASE + "/cep",
            json=tool_body(route="https://outra.interna.example/{cep}", headers=headers),
            headers=if_match(1),
        )

    assert [(r.status_code, r.json().get("field")) for r in (other_host, other_port)] == [(422, "headers")] * 2
    assert explicit.status_code == 200
