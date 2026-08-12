# GET /api/v1/entities -- lists every entity and relationship type the currently
# loaded ontology (core + all domain packs) defines. Useful for a client app to
# populate a dropdown, or just to sanity-check what a given ontology config allows.
from fastapi import APIRouter
from app.ontology.bootstrap import registry

router = APIRouter()


@router.get("")
def list_entity_types():
    return {
        "entity_types": registry.entity_types(),
        "relationship_types": registry.relationship_types(),
    }
