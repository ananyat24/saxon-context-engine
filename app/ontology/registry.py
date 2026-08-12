from copy import deepcopy
from typing import Any

from .validator import OntologyValidator


class OntologyRegistry:
    """
    Runtime registry that merges Enterprise Core + Domain + Customer extensions.

    Merge order:
      core -> domain -> customer

    Later definitions may enrich earlier definitions. Replacing an existing
    entity/relationship with a conflicting type should be handled through
    governance rather than silently changing application code.
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
        self.validator.validate(ontology)

        if not self._ontology["ontology"]:
            self._ontology["ontology"] = deepcopy(ontology["ontology"])

        for section in (
            "entities",
            "relationships",
            "event_types",
            "fact_types",
            "aliases",
        ):
            incoming = ontology.get(section, {})
            if not isinstance(incoming, dict):
                continue

            for key, value in incoming.items():
                if (
                    key in self._ontology[section]
                    and isinstance(self._ontology[section][key], dict)
                    and isinstance(value, dict)
                ):
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
        return deepcopy(self._ontology)
