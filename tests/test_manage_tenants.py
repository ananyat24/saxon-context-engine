# Pure file-I/O tests for scripts/manage_tenants.py -- no Neo4j needed, this
# script only ever reads/writes config/tenants.json. Each test points
# CONFIG_PATH at a throwaway file so nothing here ever touches the real one.
import argparse
import importlib
import json
from pathlib import Path

import pytest

manage_tenants = importlib.import_module("scripts.manage_tenants")


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "tenants.json"
    monkeypatch.setattr(manage_tenants, "CONFIG_PATH", path)
    return path


def _write(config_path, tenants):
    config_path.write_text(json.dumps(tenants), encoding="utf-8")


def test_rotate_replaces_the_key_but_keeps_the_rest_of_the_config(config_path):
    _write(config_path, {
        "old-key-123": {
            "tenant_id": "acme_demo",
            "gemini_api_key": "some-gemini-key",
            "knowledge_bases": [{"id": "northwind", "label": "Northwind"}],
        }
    })
    manage_tenants.cmd_rotate(argparse.Namespace(tenant_id="acme_demo", api_key="new-key-456"))

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "old-key-123" not in saved
    assert saved["new-key-456"]["tenant_id"] == "acme_demo"
    assert saved["new-key-456"]["gemini_api_key"] == "some-gemini-key"
    assert saved["new-key-456"]["knowledge_bases"] == [{"id": "northwind", "label": "Northwind"}]


def test_rotate_generates_a_random_key_when_none_given(config_path):
    _write(config_path, {"old-key": {"tenant_id": "acme_demo", "gemini_api_key": "g", "knowledge_bases": []}})
    manage_tenants.cmd_rotate(argparse.Namespace(tenant_id="acme_demo", api_key=None))

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert "old-key" not in saved
    new_keys = list(saved.keys())
    assert len(new_keys) == 1
    assert new_keys[0] != "old-key"
    assert len(new_keys[0]) > 20  # secrets.token_urlsafe(32) is well over 20 chars


def test_rotate_unknown_tenant_exits_with_error(config_path, capsys):
    _write(config_path, {})
    with pytest.raises(SystemExit):
        manage_tenants.cmd_rotate(argparse.Namespace(tenant_id="does_not_exist", api_key="whatever"))
    assert "No tenant found" in capsys.readouterr().err


def test_rotate_to_the_same_key_is_rejected(config_path, capsys):
    _write(config_path, {"same-key": {"tenant_id": "acme_demo", "gemini_api_key": "g", "knowledge_bases": []}})
    with pytest.raises(SystemExit):
        manage_tenants.cmd_rotate(argparse.Namespace(tenant_id="acme_demo", api_key="same-key"))
    assert "identical" in capsys.readouterr().err
