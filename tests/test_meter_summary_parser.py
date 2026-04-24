"""Unit tests for ``meter_summary_parser`` — pure HTML/dict → dataclass.

The parser is split in two:

* :func:`parse_meter_summary_from_dict` — fed by a bookmarklet that
  pre-extracts the four fields client-side.
* :func:`parse_meter_summary_from_html` — fed by a bookmarklet that
  posts the raw consumption-page HTML and lets the integration do
  the scraping. This is the default path.

Loaded by file path so we don't go through
``custom_components.canal_isabel_ii.__init__`` (which pulls in
HomeAssistant).
"""

from __future__ import annotations

import importlib.util
import os as _os
import sys as _sys
import types as _types
from datetime import datetime
from pathlib import Path

import pytest

# ---------------------------------------------------------------------
# Module loader — same standalone-loader trick as
# ``test_continuation_stats.py``.
# ---------------------------------------------------------------------


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

    models = _load("models")
    parser = _load("meter_summary_parser")
    return models, parser


_models, _parser = _load_modules()
MeterSummary = _models.MeterSummary
parse_meter_summary_from_dict = _parser.parse_meter_summary_from_dict
parse_meter_summary_from_html = _parser.parse_meter_summary_from_html
parse_meter_value_to_liters = _parser.parse_meter_value_to_liters
parse_dmy_hm = _parser.parse_dmy_hm


# ``conftest.py`` declares an autouse fixture that pulls in
# ``enable_custom_integrations`` from
# ``pytest-homeassistant-custom-component`` — which we don't need (and
# don't have installed) for these pure-parser tests. Override at
# module scope with a no-op so collection succeeds without the HA
# test rig.
@pytest.fixture
def enable_custom_integrations():
    yield


# =====================================================================
# parse_meter_value_to_liters — Spanish-locale m³ string → litres
# =====================================================================


class TestParseMeterValueToLiters:
    def test_no_thousands_separator(self):
        assert parse_meter_value_to_liters("56,735m³") == 56735.0

    def test_with_thousands_separator(self):
        # "1.234,567 m³" — Spanish locale: . is thousands, , is decimal.
        assert parse_meter_value_to_liters("1.234,567 m³") == 1234567.0

    def test_with_unit_variation_m3_no_superscript(self):
        assert parse_meter_value_to_liters("56,735 m3") == 56735.0

    def test_returns_none_for_empty(self):
        assert parse_meter_value_to_liters("") is None
        assert parse_meter_value_to_liters("   ") is None

    def test_returns_none_for_no_match(self):
        assert parse_meter_value_to_liters("(no reading)") is None

    def test_returns_none_for_unparseable_number(self):
        assert parse_meter_value_to_liters("abc,defm³") is None


# =====================================================================
# parse_dmy_hm — "22/04/2026 03:00" → datetime
# =====================================================================


class TestParseDmyHm:
    def test_happy_path(self):
        assert parse_dmy_hm("22/04/2026 03:00") == datetime(2026, 4, 22, 3, 0)

    def test_returns_none_for_empty(self):
        assert parse_dmy_hm("") is None

    def test_returns_none_for_wrong_separator(self):
        assert parse_dmy_hm("22-04-2026 03:00") is None

    def test_returns_none_for_invalid_date(self):
        # 31 February doesn't exist
        assert parse_dmy_hm("31/02/2026 03:00") is None


# =====================================================================
# parse_meter_summary_from_dict — bookmarklet's pre-parsed object
# =====================================================================


class TestParseMeterSummaryFromDict:
    def test_full_payload(self):
        raw = {
            "reading_liters": 56735.0,
            "reading_at": "2026-04-22T03:00:00",
            "meter": "Y20HK123456",
            "address": "C/ Ejemplo 1",
            "raw_reading": "56,735 m³",
        }
        s = parse_meter_summary_from_dict(raw)
        assert isinstance(s, MeterSummary)
        assert s.reading_liters == 56735.0
        assert s.reading_at == datetime(2026, 4, 22, 3, 0)
        assert s.meter == "Y20HK123456"
        assert s.address == "C/ Ejemplo 1"
        assert s.raw_reading == "56,735 m³"

    def test_missing_reading_at(self):
        s = parse_meter_summary_from_dict({"reading_liters": 100.0, "meter": "M", "address": "A"})
        assert s is not None
        assert s.reading_at is None
        assert s.reading_liters == 100.0

    def test_malformed_reading_at_drops_only_timestamp(self):
        # Losing the absolute reading is worse than losing the precise
        # timestamp — preserve the value, drop the date.
        s = parse_meter_summary_from_dict(
            {"reading_liters": 100.0, "reading_at": "not-an-iso", "meter": "M"}
        )
        assert s is not None
        assert s.reading_at is None
        assert s.reading_liters == 100.0

    def test_string_reading_liters_is_coerced(self):
        s = parse_meter_summary_from_dict({"reading_liters": "56735.0"})
        assert s is not None
        assert s.reading_liters == 56735.0

    def test_returns_none_for_non_dict(self):
        assert parse_meter_summary_from_dict(None) is None
        assert parse_meter_summary_from_dict("oops") is None
        assert parse_meter_summary_from_dict([1, 2, 3]) is None
        assert parse_meter_summary_from_dict(42) is None

    def test_returns_none_when_reading_liters_missing(self):
        assert parse_meter_summary_from_dict({"meter": "M"}) is None

    def test_returns_none_when_reading_liters_unparseable(self):
        assert parse_meter_summary_from_dict({"reading_liters": "not a number"}) is None

    def test_string_fields_default_to_empty(self):
        s = parse_meter_summary_from_dict({"reading_liters": 1.0})
        assert s is not None
        assert s.meter == ""
        assert s.address == ""
        assert s.raw_reading == ""


# =====================================================================
# parse_meter_summary_from_html — full consumption-page scrape
# =====================================================================


_PAGE_HTML = """
<html>
<body>
<ul>
  <li>
    <h5 class="titulo">Dirección suministro</h5>
    <h5 class="dato dato1">C/ Ejemplo 1, Madrid</h5>
  </li>
  <li>
    <h5 class="titulo">Contador</h5>
    <h5 class="dato dato1">Y20HK123456</h5>
  </li>
  <li>
    <h5 class="titulo">Última lectura</h5>
    <h5 class="dato minusculas">56,735m³</h5>
  </li>
  <li>
    <h5 class="titulo">Fecha y hora lectura</h5>
    <h5 class="dato dato1">22/04/2026 03:00</h5>
  </li>
</ul>
</body>
</html>
"""


class TestParseMeterSummaryFromHtml:
    def test_happy_path(self):
        s = parse_meter_summary_from_html(_PAGE_HTML)
        assert s is not None
        assert s.reading_liters == 56735.0
        assert s.reading_at == datetime(2026, 4, 22, 3, 0)
        assert s.meter == "Y20HK123456"
        assert s.address == "C/ Ejemplo 1, Madrid"
        assert s.raw_reading == "56,735m³"

    def test_returns_none_for_unrelated_html(self):
        assert parse_meter_summary_from_html("<html><body>Login required</body></html>") is None

    def test_returns_none_for_empty(self):
        assert parse_meter_summary_from_html("") is None

    def test_returns_none_when_reading_card_missing(self):
        # All three other cards present, but no "Última lectura" — we
        # require the absolute reading or there's no summary worth
        # constructing.
        broken = """
        <li><h5 class="titulo">Dirección</h5><h5 class="dato dato1">A</h5></li>
        <li><h5 class="titulo">Contador</h5><h5 class="dato dato1">M</h5></li>
        <li><h5 class="titulo">Fecha y hora lectura</h5><h5 class="dato dato1">22/04/2026 03:00</h5></li>
        """
        assert parse_meter_summary_from_html(broken) is None

    def test_handles_extra_classes(self):
        # Real portal renders ``class="dato minusculas"`` for some
        # cards; the regex must match ``dato`` even with siblings.
        html = """
        <li>
          <h5 class="titulo grande">Última lectura</h5>
          <h5 class="dato minusculas large-screen">12,500 m³</h5>
        </li>
        """
        s = parse_meter_summary_from_html(html)
        assert s is not None
        assert s.reading_liters == 12500.0

    def test_handles_inner_spans(self):
        # The portal sometimes wraps the value in a <span>; the tag
        # stripper must collapse to plain text without losing the value.
        html = """
        <li>
          <h5 class="titulo">Última lectura</h5>
          <h5 class="dato dato1"><span>56,735</span> m³</h5>
        </li>
        """
        s = parse_meter_summary_from_html(html)
        assert s is not None
        assert s.reading_liters == 56735.0

    def test_accepts_accented_lookup(self):
        # Some pages render "última" without the accent — the parser
        # must accept both ``última`` and ``ultima``. Note: "100,5 m³"
        # is Spanish locale (comma decimal) = 100.5 m³ = 100500 L.
        html = """
        <li>
          <h5 class="titulo">Ultima lectura</h5>
          <h5 class="dato">100,5 m³</h5>
        </li>
        """
        s = parse_meter_summary_from_html(html)
        assert s is not None
        assert s.reading_liters == 100500.0
