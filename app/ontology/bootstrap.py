from pathlib import Path

from app.ontology.loader import OntologyLoader
from app.ontology.registry import OntologyRegistry

DEFAULT_ONTOLOGY_ROOT = Path("ontology")


def build_default_registry(ontology_root: str | Path = DEFAULT_ONTOLOGY_ROOT) -> OntologyRegistry:
    """Build a registry populated with Enterprise Core + all domain packs."""
    root = Path(ontology_root)
    reg = OntologyRegistry()

    reg.register(OntologyLoader.load(root / "core.yaml"))
    for domain_file in sorted((root / "domains").glob("*.yaml")):
        reg.register(OntologyLoader.load(domain_file))

    return reg


registry = build_default_registry()
