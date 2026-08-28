# In-memory, merged view of the ontology: the result of loading core.yaml plus every
# domain pack (and, eventually, a customer-specific extension file) into one combined
# set of entity/relationship definitions. This is what the rest of the app queries
# when it needs to know "what entity types exist?" -- see app/ontology/bootstrap.py
# for how a populated registry is built at startup.
from copy import deepcopy
from typing import Any

from .validator import OntologyValidationError, OntologyValidator


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
                existing = self._ontology[section].get(key)
                if isinstance(existing, dict) and isinstance(value, dict):
                    # Both this layer and an earlier layer define the same key (e.g.
                    # two domain packs both touch "Organization") -- merge their
                    # properties instead of one silently replacing the other.
                    existing.update(deepcopy(value))
                elif existing is not None and existing != value:
                    # A scalar section (aliases is the only one today -- entities/
                    # relationships/event_types/fact_types entries are always dicts,
                    # handled by the merge branch above) where two layers define the
                    # same key with genuinely different values: e.g. one domain pack's
                    # alias "po" -> "Order" and another's "po" -> "PurchaseOrder".
                    # This used to silently let whichever pack loaded last win, which
                    # meant a real ambiguity (which type does "po" actually mean?)
                    # never surfaced anywhere -- fail loudly at registration time
                    # instead, the same way a malformed ontology file already does
                    # (see OntologyValidator), rather than resolving it implicitly by
                    # load order.
                    raise OntologyValidationError(
                        f"Ontology conflict in section '{section}': '{key}' is "
                        f"{existing!r} in an earlier-registered ontology file and "
                        f"{value!r} in this one. Rename one of them -- domain packs "
                        f"are additive (see ontology/README.md) and must not "
                        f"silently redefine the same key with a different value."
                    )
                else:
                    # Either genuinely new, or the same value redefined identically
                    # (harmless -- two packs agreeing on an alias isn't a conflict).
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
