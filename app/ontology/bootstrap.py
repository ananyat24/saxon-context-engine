# Builds the single OntologyRegistry the rest of the app uses. Anything that needs
# to know "what entity/relationship types exist" (e.g. the /entities API endpoint,
# or future ingestion/validation code) should import `registry` from this module
# rather than constructing its own OntologyRegistry.
from pathlib import Path

from app.ontology.loader import OntologyLoader
from app.ontology.registry import OntologyRegistry

DEFAULT_ONTOLOGY_ROOT = Path("ontology")


def build_default_registry(ontology_root: str | Path = DEFAULT_ONTOLOGY_ROOT) -> OntologyRegistry:
    """Load ontology/core.yaml plus every *.yaml file under ontology/domains/,
    and merge them into one registry. Domain files are loaded in alphabetical
    order (via sorted()) so the merge result is deterministic across runs."""
    root = Path(ontology_root)
    reg = OntologyRegistry()

    reg.register(OntologyLoader.load(root / "core.yaml"))
    for domain_file in sorted((root / "domains").glob("*.yaml")):
        reg.register(OntologyLoader.load(domain_file))

    return reg


# Built once, when this module is first imported, and then reused everywhere --
# re-parsing and re-merging the YAML files on every request would be wasted work
# since the ontology doesn't change while the app is running. If you edit the YAML
# files, restart the app (or call build_default_registry() again) to pick up changes.
registry = build_default_registry()
