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


def build_scoped_registry(
    domains: list[str], ontology_root: str | Path = DEFAULT_ONTOLOGY_ROOT
) -> OntologyRegistry:
    """Load core.yaml plus only the named domain packs.

    Ingestion passes the resulting schema into the extraction prompt (see
    app/ontology/graphiti_types.py), and every type in it costs prompt tokens
    and gives the LLM one more option to weigh. A manufacturing ingest has no
    use for the legal or pharma vocabularies, so loading all nine domains for
    it makes extraction both more expensive and harder, not more capable.
    """
    root = Path(ontology_root)
    reg = OntologyRegistry()

    reg.register(OntologyLoader.load(root / "core.yaml"))
    for domain in domains:
        reg.register(OntologyLoader.load(root / "domains" / f"{domain}.yaml"))

    return reg


# Built once, when this module is first imported, and then reused everywhere --
# re-parsing and re-merging the YAML files on every request would be wasted work
# since the ontology doesn't change while the app is running. If you edit the YAML
# files, restart the app (or call build_default_registry() again) to pick up changes.
registry = build_default_registry()
