"""QA F2-04: o núcleo (domain/application/presentation) não conhece o framework de agentes.

Os imports estáticos (diretos e indiretos) são do ``lint-imports`` (contratos ``camadas``,
``nucleo-sem-frameworks`` e ``apresentacao-sem-runtime``). Aqui: o ``sys.modules`` de um
interpretador limpo depois de importar cada pacote inteiro (pega também import dinâmico na carga
do módulo, que o ``lint-imports`` não vê) e a superfície da porta (``AgentRuntime`` abstrata;
``IAgentBuilder``/``AgentInstance`` e os módulos antigos saíram).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from src.domain import ports
from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import AgentHandle, AgentRuntime, TeamHandle

ROOT = Path(__file__).resolve().parents[2]
CORE_PACKAGES = ("src.domain", "src.application", "src.presentation")

_IMPORT_ALL = """
import importlib, pkgutil, sys
sys.path.insert(0, {root!r})
package = importlib.import_module({package!r})
for info in pkgutil.walk_packages(package.__path__, {package!r} + "."):
    importlib.import_module(info.name)
leaked = sorted(name for name in sys.modules if name == "agno" or name.startswith("agno."))
print("LEAKED:" + ",".join(leaked))
"""


@pytest.mark.parametrize("package", CORE_PACKAGES)
def test_importar_o_pacote_inteiro_num_interpretador_limpo_nao_carrega_o_agno(package: str) -> None:
    result = subprocess.run(  # noqa: S603 - interpretador do próprio venv, argumentos fixos
        [sys.executable, "-I", "-c", _IMPORT_ALL.format(package=package, root=str(ROOT))],
        cwd=ROOT,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "LEAKED:"


def test_agent_runtime_e_abstrato_e_exige_os_dois_metodos() -> None:
    with pytest.raises(TypeError):
        AgentRuntime()  # type: ignore[abstract]

    class OnlyAgents(AgentRuntime):
        async def build_agent(self, config: AgentConfig) -> AgentHandle:
            raise NotImplementedError

    with pytest.raises(TypeError):
        OnlyAgents()  # type: ignore[abstract]


def test_handles_expoem_so_o_id() -> None:
    assert {n for n in vars(AgentHandle) if not n.startswith("_")} == {"id"}
    assert {n for n in vars(TeamHandle) if not n.startswith("_")} == {"id"}


def test_porta_exporta_o_runtime_e_nao_exporta_mais_o_builder_antigo() -> None:
    assert {"AgentRuntime", "AgentHandle", "TeamHandle"} <= set(ports.__all__)
    assert not {"IAgentBuilder", "AgentInstance"} & set(ports.__all__)
    assert not hasattr(ports, "IAgentBuilder") and not hasattr(ports, "AgentInstance")


@pytest.mark.parametrize(
    "module",
    [
        "src.application.services.agent_factory_service",
        "src.application.services.team_factory_service",
        # Sem o pacote "src.infrastructure.tools": um diretório só com __pycache__/ (sobra de
        # checkout de commit antigo) vira namespace package (PEP 420) e o import passaria.
        "src.infrastructure.tools.hierarchical_search_tool",
        "src.domain.ports.agent_builder_port",
    ],
)
def test_modulos_antigos_deixaram_de_existir(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_modulos_novos_existem_e_o_runtime_e_o_unico_que_o_pacote_exporta() -> None:
    package = importlib.import_module("src.infrastructure.runtime.agno")

    assert package.__all__ == ["AgnoRuntime"]
    for name in ("agent_factory_service", "team_factory_service", "hierarchical_search_tool", "user_id_guardrail"):
        importlib.import_module(f"src.infrastructure.runtime.agno.{name}")
