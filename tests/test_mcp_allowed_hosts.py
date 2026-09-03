# Tests app/config.py's mcp_allowed_hosts_list(): pure function, no
# database, no network. See app/main.py for why this exists: the MCP SDK's
# DNS-rebinding protection 421s any request whose Host header isn't in this
# list, regardless of API key.
from app.config import settings


def test_default_covers_local_dev_hosts():
    assert settings.mcp_allowed_hosts_list() == ["localhost:8000", "127.0.0.1:8000"]


def test_parses_comma_separated_hosts_and_trims_whitespace(monkeypatch):
    monkeypatch.setattr(settings, "mcp_allowed_hosts", "localhost:8000, example.azurecontainerapps.io ,127.0.0.1:8000")
    assert settings.mcp_allowed_hosts_list() == ["localhost:8000", "example.azurecontainerapps.io", "127.0.0.1:8000"]


def test_empty_string_yields_empty_list(monkeypatch):
    monkeypatch.setattr(settings, "mcp_allowed_hosts", "")
    assert settings.mcp_allowed_hosts_list() == []
