from pathlib import Path
from typing import Any
import yaml


class OntologyLoader:
    """Loads core and optional domain ontology YAML files."""

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Ontology file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if not isinstance(data, dict):
            raise ValueError(f"Ontology root must be a mapping: {path}")

        return data

    @classmethod
    def load_many(cls, paths: list[str | Path]) -> list[dict[str, Any]]:
        return [cls.load(p) for p in paths]
