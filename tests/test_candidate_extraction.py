# Tests _extract_candidate_entities (the proper-noun regex used for named-
# entity resolution: see entity_resolution.py's module docstring). No
# database needed, pure regex.
from app.graph.entity_resolution import (
    _extract_candidate_entities,
    _extract_lowercase_word_candidates,
    _normalize_entity_name,
)


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


# --- _extract_lowercase_word_candidates: the lenient, single-word
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
    # Below the 3-char floor: these show up in almost every sentence
    # ("is", "at", "up") and would just be noise as query candidates.
    result = _extract_lowercase_word_candidates("is he at risk up here")
    assert "is" not in result
    assert "up" not in result


def test_dedupes_and_is_case_insensitive():
    result = _extract_lowercase_word_candidates("Diego said diego was fine")
    assert result.count("diego") == 1


# --- Real bug found by testing against real data: a sentence-initial
# auxiliary verb ("Has", "Should", ...), capitalized only because it starts
# the sentence, glued onto an immediately adjacent real proper noun and got
# extracted as part of the same candidate ("Has Ferrotek's"), which then
# failed to resolve and, because it's still treated as a proper-noun
# candidate, hard-short-circuited the whole query into a false "no entity
# matching that name was found", even though "Ferrotek" two words later
# resolves fine entirely on its own.


def test_strips_a_leading_auxiliary_verb_glued_onto_a_real_proper_noun():
    result = _extract_candidate_entities("Has Ferrotek Components resolved the issue?")
    assert result == ["Ferrotek Components"]
    assert "Has Ferrotek Components" not in result


def test_strips_a_leading_modal_verb_too():
    result = _extract_candidate_entities("Should Vantus Robotics be notified?")
    assert result == ["Vantus Robotics"]


def test_a_genuine_single_capitalized_word_after_stripping_still_extracts():
    # "Has Ferrotek's" is two capitalized tokens (the regex requires at
    # least two to match at all): stripping "Has" leaves one real word,
    # which must still come back as a candidate, not be discarded for
    # being "too short" now.
    result = _extract_candidate_entities("Has Ferrotek's certification been restored?")
    assert result == ["Ferrotek's"]


def test_does_not_strip_a_real_word_that_only_coincidentally_matches_a_stopword_elsewhere():
    # Sanity check the stripping is leading-only: a stopword-like word
    # elsewhere in an otherwise-real multi-word candidate must survive.
    result = _extract_candidate_entities("What about Bank of America Corp?")
    assert result == ["Bank of America Corp"]


# --- _normalize_entity_name's possessive handling: the other half of the
# same real bug: even once a candidate like "Ferrotek's" extracts cleanly on
# its own, matching still has to recognize it as referring to "Ferrotek
# Components".


def test_normalize_strips_a_trailing_possessive():
    assert _normalize_entity_name("Ferrotek's") == "ferrotek"


def test_normalize_strips_a_curly_quote_possessive():
    assert _normalize_entity_name("Ferrotek’s") == "ferrotek"


def test_normalize_strips_a_bare_trailing_apostrophe():
    # Plural possessive, e.g. "the suppliers' defect rate".
    assert _normalize_entity_name("Suppliers'") == "suppliers"


def test_normalize_does_not_touch_a_genuine_mid_name_apostrophe():
    # A real name-internal apostrophe (a person's name) must survive --
    # this isn't "strip every apostrophe", only a trailing possessive one.
    assert _normalize_entity_name("O'Brien") == "o'brien"
