# In-memory, merged view of the ontology: the result of loading core.yaml plus every
# domain pack (and, eventually, a customer-specific extension file) into one combined
# set of entity/relationship definitions. This is what the rest of the app queries
# when it needs to know "what entity types exist?" -- see app/ontology/bootstrap.py
# for how a populated registry is built at startup.
from copy import deepcopy
from typing import Any

from .validator import OntologyValidator


class OntologyRegistry:
    """
    Merges any number of ontology files together, in the order they're registered.

    Intended merge order: core -> domain pack(s) -> customer extension. Each layer
    can add new entity/relationship types or add properties to a type an earlier
    layer already defined; it's not meant to be used to redefine what an earlier
    layer already established (see ontology/README.md, "extension_policy: additive").
    """

    def __init__(self) -> None:
        self._ontology: dict[str, Any] = {
            "ontology": {},
            "entities": {},
            "relationships": {},
            "event_types": {},
            "fact_types": {},
            "aliases": {},
        }
        self.validator = OntologyValidator()

    def register(self, ontology: dict[str, Any]) -> None:
        """Validate one ontology file and fold its contents into the merged registry."""
        self.validator.validate(ontology)

        # Keep the metadata (id/name/version/description) of whichever ontology was
        # registered first -- normally the enterprise core file -- as the registry's
        # "identity". Later files (domain packs) don't overwrite it.
        if not self._ontology["ontology"]:
            self._ontology["ontology"] = deepcopy(ontology["ontology"])

        for section in ("entities", "relationships", "event_types", "fact_types", "aliases"):
            incoming = ontology.get(section, {})
            if not isinstance(incoming, dict):
                continue

            for key, value in incoming.items():
                already_defined = (
                    key in self._ontology[section]
                    and isinstance(self._ontology[section][key], dict)
                    and isinstance(value, dict)
                )
                if already_defined:
                    # Both this layer and an earlier layer define the same key (e.g.
                    # two domain packs both touch "Organization") -- merge their
                    # properties instead of one silently replacing the other.
                    self._ontology[section][key].update(deepcopy(value))
                else:
                    self._ontology[section][key] = deepcopy(value)

    def get_entity_type(self, name: str) -> dict[str, Any] | None:
        return self._ontology["entities"].get(name)

    def get_relationship_type(self, name: str) -> dict[str, Any] | None:
        return self._ontology["relationships"].get(name)

    def entity_types(self) -> list[str]:
        return sorted(self._ontology["entities"].keys())

    def relationship_types(self) -> list[str]:
        return sorted(self._ontology["relationships"].keys())

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy of the full merged ontology, safe for a caller to inspect
        or serialize without risking a mutation to the registry's internal state."""
        return deepcopy(self._ontology)
