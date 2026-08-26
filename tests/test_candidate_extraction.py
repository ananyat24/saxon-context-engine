# Tests _extract_candidate_entities (the proper-noun regex used for named-
# entity resolution -- see graph_repository.py's module comment). No
# database needed, pure regex.
from app.graph.graph_repository import _extract_candidate_entities


def test_extracts_a_name_joined_by_ampersand():
    # Found via a real demo query that silently failed to reconcile: without
    # this, "Fenwick & Cole Legal" only ever extracted as "Cole Legal" --
    # a truncated fragment that CONTAINS-matches (and is deliberately never
    # reconciled across connectors) instead of exactly matching the real,
    # full entity name.
    assert _extract_candidate_entities("What is going on with Fenwick & Cole Legal?") == ["Fenwick & Cole Legal"]


def test_extracts_a_name_joined_by_and():
    assert _extract_candidate_entities("Tell me about Fenwick and Cole Legal") == ["Fenwick and Cole Legal"]


def test_extracts_a_name_joined_by_of():
    assert _extract_candidate_entities("How is Bank of America doing?") == ["Bank of America"]


def test_two_plain_two_word_names_still_both_extract():
    result = _extract_candidate_entities("How is Riverton Robotics connected to Blue Harbor Logistics?")
    assert set(result) == {"Riverton Robotics", "Blue Harbor Logistics"}


def test_does_not_match_a_single_capitalized_word():
    assert _extract_candidate_entities("What do we know about Contoso?") == []


def test_does_not_extend_past_a_lowercase_word_that_is_not_a_connector():
    # "was" isn't and/of/&, so the phrase should stop there, not swallow
    # the rest of the sentence.
    result = _extract_candidate_entities("Fenwick Legal was marked at risk recently")
    assert result == ["Fenwick Legal"]
