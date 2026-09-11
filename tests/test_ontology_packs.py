# No database needed: list_ontology_packs() only reads ontology/*.yaml off
# disk through OntologyLoader, same as app/ontology/bootstrap.py does at
# import time. See app/api/ontology_packs.py's module docstring for why
# this exists as a separate route from GET /entities.
from app.api.ontology_packs import list_ontology_packs


def test_lists_core_and_every_domain_file_on_disk():
    from pathlib import Path

    result = list_ontology_packs()
    domain_files = sorted(p.stem for p in Path("ontology/domains").glob("*.yaml"))
    assert [d["id"].removeprefix("saxon-") for d in result["domains"]] == domain_files
    assert result["core"]["name"]


def test_a_populated_pack_is_not_marked_empty():
    result = list_ontology_packs()
    supply_chain = next(d for d in result["domains"] if d["id"] == "saxon-supply_chain")
    assert supply_chain["is_empty"] is False
    assert "Order" in supply_chain["entity_types"] or len(supply_chain["entity_types"]) > 0


def test_a_stub_pack_with_no_entities_or_relationships_is_marked_empty():
    result = list_ontology_packs()
    finance = next(d for d in result["domains"] if d["id"] == "saxon-finance")
    assert finance["is_empty"] is True
    assert finance["entity_types"] == []
    assert finance["relationship_types"] == []
