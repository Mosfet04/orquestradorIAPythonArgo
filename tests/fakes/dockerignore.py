"""Emulação do `.dockerignore` (``moby/patternmatcher``) para os testes de higiene de container.

O build real depende do Docker daemon, que a suíte não exige: aqui o arquivo é lido e aplicado como
o Docker faz (``ignorefile.ReadAll`` + ``PatternMatcher.MatchesOrParentMatches``).
"""

from __future__ import annotations

import posixpath
import re

DockerignoreRules = list[tuple[bool, re.Pattern[str]]]


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Converte um padrão do `.dockerignore` em regex como o ``patternmatcher.compile``.

    ``**`` casa qualquer coisa (inclusive ``/``; ``**/`` também casa zero diretórios),
    ``*`` casa tudo menos ``/``, ``?`` casa um caractere que não é ``/``; os
    metacaracteres ``.+()|{}$`` são escapados, o resto passa como regex (ex.: ``[cod]``).
    Ancorado nas duas pontas.
    """
    regex = "^"
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                i += 1
                if i + 1 < len(pattern) and pattern[i + 1] == "/":
                    i += 1
                regex += ".*" if i + 1 >= len(pattern) else "(.*/)?"
            else:
                regex += "[^/]*"
        elif ch == "?":
            regex += "[^/]"
        elif ch in ".+()|{}$":
            regex += "\\" + ch
        elif ch == "\\" and i + 1 < len(pattern):
            i += 1
            regex += re.escape(pattern[i])
        else:
            regex += ch
        i += 1
    return re.compile(regex + "$")


def parse_dockerignore(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    """(é negação, regex), na ordem do arquivo, como o ``ignorefile.ReadAll``.

    Comentário só se ``#`` for o 1º caractere da linha ANTES do trim (``"  #x"`` é padrão).
    """
    rules: list[tuple[bool, re.Pattern[str]]] = []
    for raw in text.splitlines():
        if raw.startswith("#"):
            continue
        line = raw.strip()
        if not line:
            continue
        negate = line.startswith("!")
        pattern = (line[1:] if negate else line).strip()
        pattern = posixpath.normpath(pattern)  # filepath.Clean: "src/" -> "src"
        if len(pattern) > 1 and pattern.startswith("/"):
            pattern = pattern[1:]
        rules.append((negate, _compile_pattern(pattern)))
    return rules


def excluded(path: str, rules: list[tuple[bool, re.Pattern[str]]]) -> bool:
    """``PatternMatcher.MatchesOrParentMatches``: o arquivo ou um diretório pai casa;
    a última regra aplicável vence (negação ``!`` reinclui)."""
    parents = posixpath.dirname(path).split("/") if "/" in path else []
    prefixes = ["/".join(parents[: i + 1]) for i in range(len(parents))]
    matched = False
    for negate, regex in rules:
        if negate != matched:
            continue
        if regex.match(path) or any(regex.match(p) for p in prefixes):
            matched = not negate
    return matched
