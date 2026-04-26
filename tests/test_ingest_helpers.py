"""Pure-function coverage for ``ingest.py``.

The ingest endpoint is mostly HA-bound (aiohttp ``web.Request``,
``HomeAssistantView``, ``hass.config_entries``…), but a few helpers
are testable in isolation:

* ``_extract_bearer(request)`` — header parsing. Get this wrong and
  every POST returns 401.
* ``_json(request, status, body)`` / ``_error(request, status, code,
  detail)`` — response shape. Front-end (the bookmarklet) parses
  these by literal key names, so a typo changes user-visible
  behaviour.

These tests fake ``aiohttp.web.Request`` with the smallest possible
``MagicMock`` (a ``.headers`` attribute is enough for ``_extract_bearer``;
the response helpers only use the status/body), so we don't need a
real aiohttp app context.

A regression here is invisible until a user complains about 401s or
about the bookmarklet showing "OK" when the server returned an error
(or vice versa) — and at that point the bookmarklet UI has already
been triggered hundreds of times against the wrong shape. Cheap to
guard now; expensive to debug later.
"""

from __future__ import annotations

import importlib.util
import json
import os as _os
import sys as _sys
import types as _types
from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------
# Stub HA + aiohttp before loading ingest.py — same pattern as the
# store tests. We need:
#   - homeassistant.components.http.HomeAssistantView (just a class)
#   - homeassistant.core.HomeAssistant (just a marker type)
#   - aiohttp.web (Request, Response, json_response)
#
# We only exercise the pure helpers, so the stubs can be dumb.
# ---------------------------------------------------------------------


class _StubResponse:
    """Tiny stand-in for ``aiohttp.web.Response``.

    Stores the constructor args so the test can assert on them directly
    without needing a running aiohttp app to inspect a real Response.
    """

    def __init__(self, *, body=None, text=None, status=200, content_type=None, **kwargs):
        self.body = body
        self.text = text
        self.status = status
        self.content_type = content_type
        self.kwargs = kwargs


def _stub_json_response(body, status=200, **kwargs):
    """Stand-in for ``aiohttp.web.json_response`` — serialise body
    deterministically and stash on a ``_StubResponse`` for the test
    to inspect.
    """
    return _StubResponse(text=json.dumps(body, sort_keys=True), status=status, **kwargs)


def _install_stubs() -> None:
    if "aiohttp" not in _sys.modules:
        aiohttp = _types.ModuleType("aiohttp")
        web = _types.ModuleType("aiohttp.web")
        web.Request = MagicMock  # only used as a type hint
        web.Response = _StubResponse
        web.json_response = _stub_json_response
        aiohttp.web = web
        _sys.modules["aiohttp"] = aiohttp
        _sys.modules["aiohttp.web"] = web

    if "homeassistant" not in _sys.modules:
        _sys.modules["homeassistant"] = _types.ModuleType("homeassistant")
    if "homeassistant.core" not in _sys.modules:
        core = _types.ModuleType("homeassistant.core")
        core.HomeAssistant = MagicMock
        _sys.modules["homeassistant.core"] = core
    if "homeassistant.components" not in _sys.modules:
        _sys.modules["homeassistant.components"] = _types.ModuleType("homeassistant.components")
    if "homeassistant.components.http" not in _sys.modules:
        http = _types.ModuleType("homeassistant.components.http")

        class _HomeAssistantView:
            url = ""
            name = ""
            requires_auth = False
            cors_allowed = False

        http.HomeAssistantView = _HomeAssistantView
        _sys.modules["homeassistant.components.http"] = http


def _load_ingest_module():
    repo = Path(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    src_dir = repo / "custom_components" / "canal_isabel_ii"

    pkg_name = "_canal_isabel_ii_ingest_test"
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

    # Ingest depends on csv_parser, meter_summary_parser, models, const.
    # All of those have no HA imports of their own, so loading them via
    # the same fake package keeps everything consistent.
    _load("const")
    _load("models")
    _load("csv_parser")
    _load("meter_summary_parser")
    return _load("ingest")


_install_stubs()
_ingest = _load_ingest_module()
_extract_bearer = _ingest._extract_bearer
_json = _ingest._json
_error = _ingest._error


def _req(headers: dict[str, str] | None = None):
    """Build a fake ``web.Request`` with just enough surface for
    the helpers we test. ``MagicMock`` would over-approximate (every
    attribute would silently exist) — we want to keep the surface
    narrow so a future helper that touches more of the request must
    explicitly extend the fake.
    """
    r = MagicMock()
    r.headers = headers or {}
    return r


# ---------------------------------------------------------------------
# _extract_bearer
# ---------------------------------------------------------------------


def test_extract_bearer_returns_token_when_present():
    """Standard ``Authorization: Bearer <tok>`` header is parsed."""
    assert _extract_bearer(_req({"Authorization": "Bearer abc123"})) == "abc123"


def test_extract_bearer_strips_surrounding_whitespace():
    """Trailing/leading whitespace inside the token slot is stripped.

    Real-world cause: HA's notification-published bookmarklet snippet
    ends with a newline that some browsers preserve in the header.
    """
    assert _extract_bearer(_req({"Authorization": "Bearer   abc123  "})) == "abc123"


def test_extract_bearer_case_insensitive_scheme():
    """The Bearer scheme name is matched case-insensitively (RFC 6750)."""
    assert _extract_bearer(_req({"Authorization": "bearer abc"})) == "abc"
    assert _extract_bearer(_req({"Authorization": "BEARER abc"})) == "abc"
    assert _extract_bearer(_req({"Authorization": "BeArEr abc"})) == "abc"


def test_extract_bearer_rejects_other_schemes():
    """Basic / Token / Digest / no-scheme → empty string (caller will
    treat it as "no token provided" and return 401).
    """
    assert _extract_bearer(_req({"Authorization": "Basic dXNlcjpwd2Q="})) == ""
    assert _extract_bearer(_req({"Authorization": "Token abc"})) == ""
    assert _extract_bearer(_req({"Authorization": "abc"})) == ""


def test_extract_bearer_returns_empty_when_header_missing():
    """No Authorization header at all → empty string, not a crash."""
    assert _extract_bearer(_req({})) == ""


def test_extract_bearer_returns_empty_when_header_is_just_scheme():
    """``Authorization: Bearer`` with no token slot → empty string.

    The 7-char slice for "bearer " on an exact match leaves ``""`` —
    which is exactly what we want the caller's `if not provided`
    branch to catch as a 401.
    """
    assert _extract_bearer(_req({"Authorization": "Bearer "})) == ""
    assert _extract_bearer(_req({"Authorization": "Bearer    "})) == ""


# ---------------------------------------------------------------------
# _json
# ---------------------------------------------------------------------


def test_json_response_serialises_body_with_status():
    """Body and status round-trip through to the response shape."""
    resp = _json(_req(), 200, {"ok": True, "imported": 42})
    assert resp.status == 200
    assert json.loads(resp.text) == {"ok": True, "imported": 42}


def test_json_response_default_status_through_callers():
    """The helper itself takes status as positional — verify a 4xx
    propagates without the helper coercing it back to 200."""
    resp = _json(_req(), 418, {"ok": False, "code": "teapot"})
    assert resp.status == 418
    assert json.loads(resp.text)["code"] == "teapot"


# ---------------------------------------------------------------------
# _error
# ---------------------------------------------------------------------


def test_error_response_uses_canonical_shape():
    """``_error`` must always produce ``{ok: false, code, detail}``.

    The bookmarklet UI looks up these exact keys; a typo here breaks
    every error-path message in the user's browser.
    """
    resp = _error(_req(), 400, "missing_csv", "Field 'csv' is required.")
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body == {
        "ok": False,
        "code": "missing_csv",
        "detail": "Field 'csv' is required.",
    }


def test_error_response_status_round_trips():
    """4xx, 5xx, 4xx-with-emoji-detail — none of it should mangle status."""
    for status in (400, 401, 404, 409, 413, 500):
        resp = _error(_req(), status, "x", "y")
        assert resp.status == status, f"status {status} got mangled"
