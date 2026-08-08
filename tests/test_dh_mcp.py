"""The subprocess must never be launched without credentials.

Gate 10a went red on this. `server_env` set `DATAHUB_GMS_URL` only when a caller
passed one explicitly; `gate10_catalog_smoke.py` did not, so the server started
with nothing, hit `MissingConfigError` inside `DataHubClient.from_env()`, and
died before serving. The stdio transport reported the corpse as
`McpError: Connection closed` -- four layers away from the cause, and worded to
look like a protocol problem.

Two lessons, both tested here:

* resolution is explicit and total. There is always a URL, and its provenance
  is ordered: argument, environment, ~/.datahubenv, quickstart default.
* an infrastructure failure must not arrive dressed as a protocol failure. The
  preflight check exists to keep "GMS is down" saying so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polygraph import dh_mcp  # noqa: E402
from polygraph.dh_mcp import DataHubMcpError, resolve_gms, server_env  # noqa: E402

GMS_VARS = ("DATAHUB_GMS_URL", "DATAHUB_GMS_TOKEN")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """No DataHub env vars and no ~/.datahubenv: the state that caused the red."""
    for var in GMS_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_env_always_carries_a_gms_url(clean_env):
    """The regression. No argument, no env, no config file -- still configured."""
    env = server_env(None, None)
    assert env["DATAHUB_GMS_URL"] == dh_mcp.DEFAULT_GMS


def test_argument_wins(clean_env, monkeypatch):
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://from-env:8080")
    assert resolve_gms("http://explicit:8080")[0] == "http://explicit:8080"


def test_environment_beats_the_config_file(clean_env, monkeypatch):
    (clean_env / ".datahubenv").write_text("gms:\n  server: http://from-file:8080\n")
    monkeypatch.setenv("DATAHUB_GMS_URL", "http://from-env:8080")
    assert resolve_gms()[0] == "http://from-env:8080"


def test_config_file_is_read_when_nothing_else_is_set(clean_env):
    (clean_env / ".datahubenv").write_text(
        "gms:\n  server: http://from-file:8080\n  token: abc123\n"
    )
    url, token = resolve_gms()
    assert url == "http://from-file:8080"
    assert token == "abc123"


def test_a_malformed_config_file_degrades_to_the_default(clean_env):
    """A caller that only wanted to read some owners should not crash on a
    config file someone hand-edited."""
    (clean_env / ".datahubenv").write_text("this is not: yaml: at all: ][\n")
    assert resolve_gms()[0] == dh_mcp.DEFAULT_GMS


def test_empty_token_is_not_forwarded(clean_env):
    """The quickstart writes `token: ''`. Forwarding an empty Authorization
    header is worse than sending none."""
    (clean_env / ".datahubenv").write_text("gms:\n  server: http://x:8080\n  token: ''\n")
    assert resolve_gms()[1] is None
    assert "DATAHUB_GMS_TOKEN" not in server_env(None, None)


def test_trailing_slashes_are_stripped(clean_env):
    assert resolve_gms("http://localhost:8080/")[0] == "http://localhost:8080"


def test_preflight_reports_a_down_gms_as_a_down_gms(monkeypatch):
    """Not as a protocol error. This is the whole point of preflighting."""
    import urllib.error

    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(dh_mcp.urllib.request, "urlopen", boom)

    with pytest.raises(DataHubMcpError) as exc:
        dh_mcp.preflight("http://localhost:8080")

    msg = str(exc.value)
    assert "not answering" in msg
    assert "test_connection" in msg, "name why the server cannot start without it"
    assert "Connection closed" not in msg


def test_telemetry_is_disabled_on_the_subprocess(clean_env):
    """Polygraph spawns this server on every call. Phoning home each time is
    repeated latency and a repeated network dependency for a local step."""
    assert server_env(None, None)["DATAHUB_TELEMETRY_ENABLED"] == "false"
