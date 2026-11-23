"""`.gitignore` protege arquivos `.env*` locais e mantém versionados só os modelos (F3-01).

Usa ``git check-ignore`` (a semântica real do git, inclusive a negação ``!``); não toca em arquivos
``.env`` reais: os caminhos consultados nem precisam existir.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("git") is None, reason="git indisponível"),
    pytest.mark.skipif(not (ROOT / ".git").exists(), reason="checkout sem .git (ex.: git archive)"),
]


def _ignored(path: str) -> bool:
    result = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "-q", "--no-index", path],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    assert result.returncode in (0, 1), "git check-ignore falhou (fora de um repositório?)"
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [".env", ".env.local", ".env.production", ".env.staging", ".env.qualquer-outro", "ci.env"],
)
def test_arquivos_env_locais_sao_ignorados(path: str) -> None:
    assert _ignored(path), f"{path} deveria estar no .gitignore"


@pytest.mark.parametrize("path", [".env.example", ".env.development"])
def test_modelos_de_env_versionados_nao_sao_ignorados(path: str) -> None:
    assert not _ignored(path), f"{path} é versionado e não pode ser ignorado"
