# Graphiti already runs LLM-based entity/fact extraction internally when you call
# add_episode() (see app/ingestion/pipeline.py) -- this class is a placeholder hook
# for extraction logic that needs to run on top of that, e.g. applying ontology-
# specific rules before extraction (only look for types this ontology defines) or
# filtering/post-processing candidates Graphiti already produced. It intentionally
# returns nothing yet; there's no extraction logic implemented outside of Graphiti's
# own extraction pipeline at this stage of the project.
from typing import Any


class EntityExtractor:
    """Extracts entities and facts guided by active ontology definitions."""

    def extract_candidates(self, text: str) -> list[dict[str, Any]]:
        return []
