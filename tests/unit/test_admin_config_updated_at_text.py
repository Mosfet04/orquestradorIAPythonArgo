"""FA-08: formatação do ``updated_at`` na resposta da API Admin (ISO 8601 UTC em texto, sempre com ``Z``)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from src.presentation.routes.admin_config import updated_at_text


def test_datetime_sem_fuso_e_tratado_como_utc():
    assert updated_at_text(datetime(2026, 11, 26, 14, 3, 0)) == "2026-11-26T14:03:00Z"


def test_datetime_com_fuso_e_convertido_para_utc():
    sao_paulo = timezone(timedelta(hours=-3))

    assert updated_at_text(datetime(2026, 11, 26, 23, 30, 5, tzinfo=sao_paulo)) == "2026-11-27T02:30:05Z"
    assert updated_at_text(datetime(2026, 11, 26, 14, 3, tzinfo=UTC)) == "2026-11-26T14:03:00Z"


def test_fracao_de_segundo_fica_de_fora():
    assert updated_at_text(datetime(2026, 11, 26, 14, 3, 0, 999_999, tzinfo=UTC)) == "2026-11-26T14:03:00Z"


def test_ano_com_menos_de_quatro_digitos_sai_com_zeros():
    assert updated_at_text(datetime(999, 1, 2, 3, 4, 5)) == "0999-01-02T03:04:05Z"


def test_ausente_e_none():
    assert updated_at_text(None) is None


@pytest.mark.parametrize("value", ["2026-11-26T14:03:00Z", 1_794_850_061, 1.5, True, date(2026, 11, 26), {}, []])
def test_valor_que_nao_e_datetime_e_none(value: object):
    assert updated_at_text(value) is None
