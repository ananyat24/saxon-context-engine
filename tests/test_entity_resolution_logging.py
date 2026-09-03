# Real bugs this session were found by re-deriving "why didn't this
# resolve" from production behavior, because nothing logged the actual
# match/no-match decision at the time it was made. entity_resolution.py's
# whole point is to make that decision inspectable: these tests assert
# the logging actually fires, not just that the module docstring claims it
# does. Pure-logic (a fake execute_cypher), no Neo4j needed.
import asyncio
import logging

from app.graph.entity_resolution import match_entities_by_name, resolve_named_entities


def _fake_execute_cypher(rows_by_query_shape):
    """rows_by_query_shape: a callable (query, params) -> list[dict], so each
    test controls exactly what each of match_entities_by_name's three
    Cypher shapes (exact/contains-for-normalize/final-contains) returns."""
    return rows_by_query_shape


def test_exact_match_logs_which_strategy_matched(caplog):
    def fake(query, params):
        if "toLower(n.name) = toLower($name)" in query:
            return [{"uuid": "u1", "name": "Acme Corp", "summary": "", "group_id": "g1"}]
        return []

    with caplog.at_level(logging.DEBUG, logger="app.graph.entity_resolution"):
        match_entities_by_name(fake, "Acme Corp", ["g1"])

    assert any("exact_name" in r.message for r in caplog.records)


def test_no_match_logs_no_match(caplog):
    def fake(query, params):
        return []

    with caplog.at_level(logging.DEBUG, logger="app.graph.entity_resolution"):
        match_entities_by_name(fake, "Nonexistent Thing", ["g1"])

    assert any("no_match" in r.message for r in caplog.records)


def test_resolve_named_entities_logs_a_summary_line(caplog):
    def fake(query, params):
        if "toLower(n.name) = toLower($name)" in query:
            return [{"uuid": "u1", "name": "Rhodes Furniture", "summary": "", "group_id": "g1"}]
        return []

    with caplog.at_level(logging.DEBUG, logger="app.graph.entity_resolution"):
        asyncio.run(resolve_named_entities(fake, "What's changed about Rhodes Furniture?", ["g1"], None))

    assert any("resolved to" in r.message for r in caplog.records)


def test_unresolved_proper_noun_logs_which_candidate_missed(caplog):
    def fake(query, params):
        return []

    with caplog.at_level(logging.DEBUG, logger="app.graph.entity_resolution"):
        asyncio.run(resolve_named_entities(fake, "What's changed about Ghost Entity?", ["g1"], None))

    assert any("did not resolve" in r.message for r in caplog.records)
