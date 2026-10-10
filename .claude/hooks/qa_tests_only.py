#!/usr/bin/env python3
"""Hook PreToolUse do qa-engineer: só permite Edit/Write em tests/ e docs/qa/.

Recebe o JSON do Claude Code no stdin. Sai com código 2 (bloqueia a chamada)
quando o alvo está fora das pastas permitidas ou fora do projeto.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ALLOWED_PREFIXES = (("tests",), ("docs", "qa"))


def decide(project_root: str, file_path: str) -> tuple[int, str]:
    """Retorna (código de saída, motivo) para o caminho pedido."""
    root = Path(project_root).resolve()
    target = Path(file_path)
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        rel = target.relative_to(root)
    except ValueError:
        return 2, f"{file_path} está fora do projeto"
    for prefix in ALLOWED_PREFIXES:
        if rel.parts[: len(prefix)] == prefix:
            return 0, "ok"
    return 2, f"QA só pode escrever em tests/ ou docs/qa/ (pedido: {rel})"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print("Hook do QA: entrada inválida", file=sys.stderr)
        return 2
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return 0
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
    code, reason = decide(root, file_path)
    if code:
        print(reason, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
