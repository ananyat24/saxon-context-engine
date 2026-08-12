# Reads ontology definitions from disk. An "ontology" here is just a YAML file that
# lists out the entity types (Person, Organization, ...) and relationship types
# (MANAGES, OWNS, ...) that are allowed in the graph -- see ontology/core.yaml for
# the full definition and ontology/README.md for how the layering (core -> domain ->
# customer) works. This module only handles reading the YAML into a Python dict;
# app/ontology/validator.py checks that the dict is well-formed, and
# app/ontology/registry.py merges multiple ontology files together at runtime.
from pathlib import Path
from typing import Any
import yaml


class OntologyLoader:
    """Reads a single ontology YAML file into a Python dict."""

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Ontology file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            # yaml.safe_load refuses to execute arbitrary Python objects that could be
            # embedded in a YAML file (unlike yaml.load), which matters here because
            # ontology files may eventually come from customer-supplied config.
            # `or {}` covers the case of an empty file, which safe_load returns as None.
            data = yaml.safe_load(f) or {}

        if not isinstance(data, dict):
            raise ValueError(f"Ontology root must be a mapping: {path}")

        return data

    @classmethod
    def load_many(cls, paths: list[str | Path]) -> list[dict[str, Any]]:
        """Convenience helper for loading several ontology files at once, e.g. core + all domain packs."""
        return [cls.load(p) for p in paths]
