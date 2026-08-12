from typing import Any, Dict


class EntityResolver:
    """Handles entity disambiguation and deduplication across disparate source systems."""

    def resolve_alias(self, entity_name: str) -> str:
        return entity_name.strip()
