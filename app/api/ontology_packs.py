# GET /api/v1/ontology/packs lists the core ontology plus every domain pack
# under ontology/domains/, each with its own entity/relationship type names.
# Purely additive and read-only: it reads the same YAML files
# app/ontology/bootstrap.py merges into the single registry, through the
# same OntologyLoader, but keeps each file's own identity instead of
# flattening them the way GET /entities (app/api/entities.py) does. That
# flattened view is what extraction/retrieval actually runs against and
# stays untouched; this route exists only so a UI can show "here are the
# 9 domain packs" as their own distinct, browsable things -- including the
# ones that are still empty stubs, which is real and worth showing
# honestly rather than hiding.
from pathlib import Path

from fastapi import APIRouter

from app.ontology.bootstrap import DEFAULT_ONTOLOGY_ROOT
from app.ontology.loader import OntologyLoader

router = APIRouter()


def _pack_summary(path: Path) -> dict:
    data = OntologyLoader.load(path)
    meta = data.get("ontology", {})
    entities = data.get("entities") or {}
    relationships = data.get("relationships") or {}
    return {
        "id": meta.get("id", path.stem),
        "name": meta.get("name", path.stem),
        "description": meta.get("description", "").strip(),
        "entity_types": sorted(entities.keys()),
        "relationship_types": sorted(relationships.keys()),
        "is_empty": not entities and not relationships,
    }


@router.get("")
def list_ontology_packs():
    root = Path(DEFAULT_ONTOLOGY_ROOT)
    core = _pack_summary(root / "core.yaml")
    domains = [_pack_summary(p) for p in sorted((root / "domains").glob("*.yaml"))]
    return {"core": core, "domains": domains}
