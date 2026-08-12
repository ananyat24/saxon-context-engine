# Tracks which records have already been ingested, so re-running an ingestion
# doesn't reprocess everything. This matters more than it might look: every
# episode costs an LLM call, and Gemini's free tier is rate-limited, so
# re-ingesting a whole directory to pick up a handful of new rows wastes both
# quota and wall-clock time.
#
# Deliberately a plain JSON file rather than a table in Neo4j: it's ingestion
# bookkeeping, not graph content, and keeping it out of the graph means clearing
# it (to force a re-ingest) doesn't touch any actual data.
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("data/processed/ingest_log.json")


class IngestLog:
    """Remembers the episode names already ingested, per group_id."""

    def __init__(self, path: Path = DEFAULT_LOG_PATH):
        self.path = path
        self._seen: dict[str, set[str]] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._seen = {group: set(names) for group, names in raw.items()}

    def already_ingested(self, group_id: str, name: str) -> bool:
        return name in self._seen.get(group_id, set())

    def mark(self, group_id: str, name: str) -> None:
        self._seen.setdefault(group_id, set()).add(name)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {group: sorted(names) for group, names in self._seen.items()}
        self.path.write_text(json.dumps(serializable, indent=2) + "\n", encoding="utf-8")

    def count(self, group_id: str) -> int:
        return len(self._seen.get(group_id, set()))
