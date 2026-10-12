"""QA do F2-09: caracterização da ordem e do texto das recusas de ``base_url``.

O refactor separou ``_base_url_text`` de ``_check_base_url``. Os testes existentes provam que cada
URL hostil é recusada (``match="base_url"``), mas não qual regra vale quando a URL viola mais de
uma: o texto da recusa é o contrato observável (vai ao log do isolamento do documento), então
a ordem das checagens fica fixada aqui.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.domain.entities.model_config import MAX_BASE_URL_LENGTH, ModelConfig

NOT_TEXT = "base_url deve ser texto"
TOO_LONG = f"base_url maior que {MAX_BASE_URL_LENGTH} caracteres"
CONTROL = "base_url não pode ter espaço, '\\' nem caractere de controle"
SHAPE = "base_url deve ser http(s)://host[:porta][/caminho]"
CREDENTIALS = "base_url não pode ter credenciais; use api_key_ref"
QUERY = "base_url não pode ter query nem fragmento"
ASCII_HOST = "base_url: host só em ASCII, sem '%' (IDN em punycode)"
PORT = "base_url com porta vazia ou 0"

LONG = "http://h/" + "a" * MAX_BASE_URL_LENGTH


def _refusal(url: Any) -> str:
    with pytest.raises(ValueError) as exc_info:
        ModelConfig(provider="p", model_id="m", base_url=url)
    return str(exc_info.value)


@pytest.mark.parametrize(
    ("url", "message"),
    [
        # uma regra por vez
        (123, NOT_TEXT),
        (b"http://h", NOT_TEXT),
        (["http://h"], NOT_TEXT),
        (LONG, TOO_LONG),
        ("http://h/a b", CONTROL),
        ("http://h\\x", CONTROL),
        ("http://h\x00", CONTROL),
        ("ftp://h", SHAPE),
        ("http://", SHAPE),
        ("http://[::1", SHAPE),
        ("http://u:p@h", CREDENTIALS),
        ("http://h?x=1", QUERY),
        ("http://h/#f", QUERY),
        ("http://bücher.example/", ASCII_HOST),
        ("http://%68ost", ASCII_HOST),
        ("http://h:0", PORT),
        ("http://h:", PORT),
        # mais de uma violação: vale a primeira da ordem texto -> tamanho -> controle -> forma
        # -> credencial -> query/fragmento -> ASCII/% -> porta
        (LONG + " x", TOO_LONG),
        ("ftp://h x", CONTROL),
        ("ftp://u@h?x", SHAPE),
        ("http://u@h?x#y", CREDENTIALS),
        ("http://u@bücher", CREDENTIALS),
        ("http://bücher?x", QUERY),
        ("http://h:0?x", QUERY),
        ("http://bücher:0", ASCII_HOST),
        ("http://%68:0", ASCII_HOST),
    ],
    ids=repr,
)
def test_recusa_de_base_url_tem_o_texto_da_primeira_regra_violada(url: Any, message: str) -> None:
    assert _refusal(url) == message


def test_url_no_limite_de_tamanho_passa_pelo_texto_e_segue_para_as_demais_regras() -> None:
    at_limit = "http://h/" + "a" * (MAX_BASE_URL_LENGTH - len("http://h/"))
    assert len(at_limit) == MAX_BASE_URL_LENGTH

    assert ModelConfig(provider="p", model_id="m", base_url=at_limit).base_url == at_limit
    assert _refusal(at_limit.replace("http", "ftp", 1)) == SHAPE
    assert _refusal(at_limit + "?") == TOO_LONG


def test_base_url_none_continua_sendo_ausente_e_nao_passa_pela_checagem() -> None:
    assert ModelConfig(provider="p", model_id="m", base_url=None).base_url is None
