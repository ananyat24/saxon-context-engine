# app/retrieval/mcp_query_helper.py's _pick_string_param: pure function,
# no MCP connection involved. query_mcp_tool itself (the actual MCP
# handshake) is exercised indirectly through
# test_fabric_iq_ontology_retriever.py/test_work_iq_retriever.py, which
# monkeypatch it rather than faking a real MCP server.
from app.retrieval.mcp_query_helper import _pick_string_param


def test_prefers_a_required_string_property():
    schema = {
        "properties": {"limit": {"type": "integer"}, "query": {"type": "string"}},
        "required": ["query"],
    }
    assert _pick_string_param(schema) == "query"


def test_falls_back_to_any_string_property_when_none_required():
    schema = {"properties": {"limit": {"type": "integer"}, "question": {"type": "string"}}}
    assert _pick_string_param(schema) == "question"


def test_returns_none_when_no_string_property_exists():
    schema = {"properties": {"limit": {"type": "integer"}}, "required": ["limit"]}
    assert _pick_string_param(schema) is None


def test_returns_none_for_an_empty_schema():
    assert _pick_string_param({}) is None


def test_required_non_string_property_is_skipped_in_favor_of_an_optional_string_one():
    schema = {
        "properties": {"count": {"type": "integer"}, "text": {"type": "string"}},
        "required": ["count"],
    }
    assert _pick_string_param(schema) == "text"
