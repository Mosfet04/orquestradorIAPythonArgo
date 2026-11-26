"""F4-02 (BUG-F4-02-QA-1): a exceção do sanitizador para hash de schema é estreita.

Só passa inteiro o valor de um campo ``*_hash`` que é exatamente SHA-256 em hex minúsculo (64 caracteres, o
formato de ``schema_hash`` e de ``mcp_tools``). Segredo com outra forma, ou hash sob outro nome, segue mascarado.
Valores de teste: sequências sintéticas, não são credenciais.
"""

from __future__ import annotations

from src.infrastructure.logging.structlog_logger import sanitize_log_data

SHA256 = "a" * 24 + "0123456789abcdef" * 2 + "b" * 8
AWS_LIKE = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/+Ab"  # 40 caracteres no alfabeto do padrão aws_secret_key


def test_hash_sha256_em_campo_hash_sai_inteiro():
    result = sanitize_log_data(None, "error", {"expected_hash": SHA256, "observed_hash": SHA256})

    assert result["expected_hash"] == SHA256
    assert result["observed_hash"] == SHA256


def test_segredo_continua_mascarado_mesmo_em_campo_hash_ou_com_formato_de_hash_em_outro_campo():
    result = sanitize_log_data(
        None,
        "error",
        {
            "observed_hash": AWS_LIKE,
            "expected_hash": SHA256.upper(),
            "detail": SHA256,
            "secret_hash": SHA256,
            "observed_hash_extra": f"{SHA256} {AWS_LIKE}",
        },
    )

    assert result["observed_hash"] == "=***AWS_SECRET_KEY_MASKED***"
    assert "AWS_SECRET_KEY_MASKED" in result["expected_hash"]
    assert "AWS_SECRET_KEY_MASKED" in result["detail"]
    assert result["secret_hash"].startswith("***MASKED***")
    assert AWS_LIKE not in result["observed_hash_extra"]
