"""Unit tests for ``csv_parser`` — pure CSV → list[Reading].

The parser is the boundary between the bookmarklet payload and the
rest of the integration. It must:

* Tolerate the two delimiters Canal occasionally swaps between (``,``
  and ``;``).
* Skip rows with bad timestamps / missing values without sinking the
  whole batch.
* Produce naive (no tzinfo) datetimes — the upstream is responsible
  for tz normalisation because it depends on HA's configured zone.

Loaded by file path so we don't drag HomeAssistant into the test
session.
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
import types as _types
from datetime import datetime
from pathlib import Path

import pytest


def _load_modules() -> tuple:
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_isabel_ii_for_test"
    if pkg_name not in _sys.modules:
        pkg = _types.ModuleType(pkg_name)
        pkg.__path__ = [str(src_dir)]
        _sys.modules[pkg_name] = pkg

    def _load(submod: str):
        full = f"{pkg_name}.{submod}"
        if full in _sys.modules:
            return _sys.modules[full]
        spec = importlib.util.spec_from_file_location(full, src_dir / f"{submod}.py")
        assert spec and spec.loader
        m = importlib.util.module_from_spec(spec)
        _sys.modules[full] = m
        spec.loader.exec_module(m)
        return m

    return _load("models"), _load("csv_parser")


_models, _parser = _load_modules()
Reading = _models.Reading
parse_csv = _parser.parse_csv
detect_contracts = _parser.detect_contracts


@pytest.fixture
def enable_custom_integrations():
    yield


# ---------------------------------------------------------------------
# Sample CSVs
# ---------------------------------------------------------------------

CSV_BASIC = (
    "Contrato,Periodo,Contador,Dirección,Frecuencia,Fecha/Hora,Consumo (litros)\n"
    "999000001,Facturado,Y20HK123,C/ Ejemplo,Horaria,22/04/2026 03,12,5\n"
    "999000001,Facturado,Y20HK123,C/ Ejemplo,Horaria,22/04/2026 04,8,2\n"
    "999000001,Facturado,Y20HK123,C/ Ejemplo,Horaria,22/04/2026 05,15,7\n"
)

CSV_SEMICOLON = (
    "Contrato;Periodo;Contador;Dirección;Frecuencia;Fecha/Hora;Consumo (litros)\n"
    "999000001;Facturado;Y20HK123;C/ Ejemplo;Horaria;22/04/2026 03;12,5\n"
    "999000001;Facturado;Y20HK123;C/ Ejemplo;Horaria;22/04/2026 04;8,2\n"
)

# ---------------------------------------------------------------------
# parse_csv
# ---------------------------------------------------------------------


class TestParseCsv:
    def test_basic_three_rows(self):
        # Note: this CSV is malformed because the unquoted "Consumo
        # (litros)" of "12,5" is treated as a comma-separated value
        # split — it becomes liters="12" then a stray "5" column. Real
        # exports from Canal use ; for cells with commas inside, so
        # this case is a fallback. We accept the parse but liters will
        # be 12 not 12.5. See test_real_export_with_quotes for the
        # non-degraded path.
        rows = parse_csv(CSV_BASIC)
        assert len(rows) == 3
        for r in rows:
            assert r.contract == "999000001"
            assert r.timestamp.date() == datetime(2026, 4, 22).date()

    def test_semicolon_delimiter(self):
        rows = parse_csv(CSV_SEMICOLON)
        assert len(rows) == 2
        # Semicolon-separated cells preserve "12,5" as a single
        # token, which is then converted to 12.5.
        assert rows[0].liters == 12.5
        assert rows[1].liters == 8.2

    def test_real_export_with_quoted_decimals(self):
        # Some Canal exports quote cells that contain commas.
        raw = (
            "Contrato,Periodo,Contador,Dirección,Frecuencia,Fecha/Hora,Consumo (litros)\n"
            '999000001,Facturado,Y20HK123,"C/ Ejemplo, 1",Horaria,22/04/2026 03,"12,5"\n'
        )
        rows = parse_csv(raw)
        assert len(rows) == 1
        assert rows[0].address == "C/ Ejemplo, 1"
        assert rows[0].liters == 12.5

    def test_skips_malformed_timestamp(self):
        raw = (
            "Contrato,Periodo,Contador,Dirección,Frecuencia,Fecha/Hora,Consumo (litros)\n"
            "999000001,F,M,A,Horaria,not-a-date,5\n"
            "999000001,F,M,A,Horaria,22/04/2026 03,5\n"
        )
        rows = parse_csv(raw)
        assert len(rows) == 1
        assert rows[0].timestamp == datetime(2026, 4, 22, 3, 0)

    def test_skips_malformed_liters(self):
        raw = (
            "Contrato,Periodo,Contador,Dirección,Frecuencia,Fecha/Hora,Consumo (litros)\n"
            "999000001,F,M,A,Horaria,22/04/2026 03,not-a-number\n"
            "999000001,F,M,A,Horaria,22/04/2026 04,7\n"
        )
        rows = parse_csv(raw)
        assert len(rows) == 1
        assert rows[0].liters == 7.0

    def test_returns_empty_for_empty(self):
        assert parse_csv("") == []
        assert parse_csv("   \n\n  ") == []

    def test_returns_empty_for_header_only(self):
        raw = "Contrato,Periodo,Contador,Dirección,Frecuencia,Fecha/Hora,Consumo (litros)\n"
        assert parse_csv(raw) == []

    def test_naive_datetimes(self):
        # We promise the rest of the integration that timestamps come
        # out tz-naive — sensors handle the conversion to local.
        rows = parse_csv(CSV_SEMICOLON)
        for r in rows:
            assert r.timestamp.tzinfo is None

    def test_multi_contract_in_one_csv(self):
        raw = (
            "Contrato;Periodo;Contador;Dirección;Frecuencia;Fecha/Hora;Consumo (litros)\n"
            "999000001;F;M1;A1;Horaria;22/04/2026 03;5\n"
            "410270591;F;M2;A2;Horaria;22/04/2026 03;7\n"
        )
        rows = parse_csv(raw)
        assert {r.contract for r in rows} == {"999000001", "410270591"}

    def test_metadata_carried_over(self):
        rows = parse_csv(CSV_SEMICOLON)
        assert rows[0].meter == "Y20HK123"
        assert rows[0].address == "C/ Ejemplo"
        assert rows[0].period == "Facturado"
        assert rows[0].frequency == "Horaria"


# ---------------------------------------------------------------------
# detect_contracts
# ---------------------------------------------------------------------


class TestDetectContracts:
    def test_single_contract(self):
        assert detect_contracts(CSV_SEMICOLON) == {"999000001"}

    def test_multiple_contracts(self):
        raw = (
            "Contrato;Periodo;Contador;Dirección;Frecuencia;Fecha/Hora;Consumo (litros)\n"
            "AAA;F;M;A;H;22/04/2026 03;1\n"
            "BBB;F;M;A;H;22/04/2026 03;1\n"
        )
        assert detect_contracts(raw) == {"AAA", "BBB"}

    def test_empty(self):
        assert detect_contracts("") == set()

    def test_skips_empty_contract_column(self):
        raw = (
            "Contrato;Periodo;Contador;Dirección;Frecuencia;Fecha/Hora;Consumo (litros)\n"
            ";F;M;A;H;22/04/2026 03;1\n"
            "AAA;F;M;A;H;22/04/2026 04;1\n"
        )
        assert detect_contracts(raw) == {"AAA"}
