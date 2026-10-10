"""QA F2-03: a documentação do plugin (README pt-br/en e exemplo) bate com o código.

Os trechos de código dos READMEs são executados: se a ``ProviderSpec`` ou o grupo de entry
points mudarem, a documentação quebra o teste antes de enganar quem escreve um plugin.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml
from agno.models.deepseek import DeepSeek

from src.domain.entities.model_config import ModelConfig
from src.infrastructure.providers import ProviderRegistry, ProviderSpec
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.providers.plugins import ENTRY_POINT_GROUP

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "provider_plugin"
READMES = [ROOT / "README.pt-br.md", ROOT / "README.en.md"]
ENVS = ("PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS")


def _blocks(readme: Path, language: str) -> list[str]:
    text = readme.read_text(encoding="utf-8")
    section = text[text.index("### Plugins de provider" if "pt-br" in readme.name else "### Provider plugins") :]
    section = section[: section.index("\n### ", 5)]
    return re.findall(rf"```{language}\n(.*?)```", section, flags=re.DOTALL)


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_toml_do_readme_declara_o_mesmo_entry_point_do_exemplo(readme: Path) -> None:
    [snippet] = _blocks(readme, "toml")
    documented = tomllib.loads(snippet)["project"]
    real = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert documented["name"] == real["name"]
    expected = {ENTRY_POINT_GROUP: {"deepseek": "orquestrador_provider_deepseek:SPEC"}}
    assert documented["entry-points"] == real["entry-points"] == expected


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_codigo_do_readme_e_uma_spec_valida_que_cria_o_modelo_real(
    readme: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    [snippet] = _blocks(readme, "python")
    namespace: dict[str, object] = {}
    exec(compile(snippet, str(readme), "exec"), namespace)  # noqa: S102 - trecho de documentação do repositório
    spec = namespace["SPEC"]
    assert isinstance(spec, ProviderSpec)
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    registry.register(spec)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "chave-de-teste")

    model = registry.create_model(ModelConfig("deepseek", "deepseek-chat", params={"temperature": 0.2}))

    assert isinstance(model, DeepSeek) and model.temperature == 0.2


@pytest.mark.parametrize("readme", READMES, ids=lambda p: p.name)
def test_readme_cita_as_tres_envs_e_o_exemplo_de_allowlist(readme: Path) -> None:
    text = readme.read_text(encoding="utf-8")

    for name in ENVS:
        assert name in text, name
    assert "PLUGIN_ALLOWLIST=orquestrador-provider-deepseek:deepseek" in text
    assert "examples/provider_plugin" in text


def test_env_example_documenta_as_tres_envs_com_defaults_seguros() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()

    assert "PLUGIN_ALLOWLIST=" in lines  # vazio = nenhum plugin
    assert "# ALLOW_DYNAMIC_IMPORT=false" in lines  # desligado e comentado: nunca ativo por cópia do arquivo
    assert "# DYNAMIC_PROVIDER_SPECS=" in lines
    assert not any(line.startswith(("ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS")) for line in lines)


def test_compose_repassa_as_tres_envs_sem_valor_embutido() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services_env = [s.get("environment", {}) for s in compose["services"].values()]

    for name in ENVS:
        assert any(name in env and env[name] is None for env in services_env), name  # repasse puro


def test_exemplo_nao_adiciona_dependencia() -> None:
    project = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["dependencies"] == []
