# Tests _extract_candidate_entities (the proper-noun regex used for named-
# entity resolution -- see entity_resolution.py's module docstring). No
# database needed, pure regex.
from app.graph.entity_resolution import _extract_candidate_entities, _extract_lowercase_word_candidates


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


# --- _extract_lowercase_word_candidates -- the lenient, single-word
# fallback for a casually-typed, uncapitalized name (e.g. "what do we know
# about diego" instead of "...Diego Alvarez?"), which _extract_candidate_entities
# above never matches at all (it requires capitalized, multi-word phrases).


def test_extracts_a_bare_lowercase_name_from_a_normal_sentence():
    result = _extract_lowercase_word_candidates("what do we know about diego")
    assert "diego" in result


def test_excludes_ordinary_filler_words():
    result = _extract_lowercase_word_candidates("what do we know about diego")
    for filler in ("what", "do", "we", "know", "about"):
        assert filler not in result


def test_excludes_short_words():
    # Below the 3-char floor -- these show up in almost every sentence
    # ("is", "at", "up") and would just be noise as query candidates.
    result = _extract_lowercase_word_candidates("is he at risk up here")
    assert "is" not in result
    assert "up" not in result


def test_dedupes_and_is_case_insensitive():
    result = _extract_lowercase_word_candidates("Diego said diego was fine")
    assert result.count("diego") == 1
