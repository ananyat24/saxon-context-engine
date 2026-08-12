# Placeholder for entity resolution: recognizing that "Contoso", "Contoso Ltd.",
# and "contoso" from three different source systems all refer to the same
# Organization entity, so they get merged into one node instead of three duplicates.
# Graphiti does some of this automatically during extraction; this hook is for
# resolution rules specific to this project's ontology/customer data (e.g. a lookup
# table of known aliases) that would run before or after Graphiti's own matching.
class EntityResolver:
    """Handles entity disambiguation and deduplication across disparate source systems."""

    def resolve_alias(self, entity_name: str) -> str:
        return entity_name.strip()
