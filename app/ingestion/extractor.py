from typing import Any, Dict, List


class EntityExtractor:
    """Extracts entities and facts guided by active ontology definitions."""

    def extract_candidates(self, text: str) -> List[Dict[str, Any]]:
        # Graphiti performs LLM extraction under the hood; this module allows pre/post extraction rules
        return []
