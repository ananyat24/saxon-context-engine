from app.models.entity import Entity
from app.models.relationship import Relationship


def test_entity_creation():
    entity = Entity(
        id="org-001",
        type="Organization",
        name="ABC Corporation"
    )
    assert entity.id == "org-001"
    assert entity.type == "Organization"


def test_relationship_creation():
    relationship = Relationship(
        source_id="org-001",
        relationship_type="PURCHASED",
        target_id="product-001"
    )
    assert relationship.relationship_type == "PURCHASED"
