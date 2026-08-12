from fastapi import APIRouter
from app.ontology.bootstrap import registry

router = APIRouter()


@router.get("")
def list_entity_types():
    return {
        "entity_types": registry.entity_types(),
        "relationship_types": registry.relationship_types(),
    }
