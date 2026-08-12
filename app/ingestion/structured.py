# Graphiti's extraction is built for natural-language text, not structured rows.
# This turns a structured record (e.g. a CRM export row) into readable prose so
# the LLM can pull entities and facts out of it -- see scripts/test_graph.py for
# an example of what that text looks like when hand-written for a CRM/ERP scenario.
#
# Why prose rather than the raw "col: value, col: value" dump this used to emit:
# the extraction step is an LLM reading English. "Customer ALFKI is Alfreds
# Futterkiste, contact Maria Anders" gives it far more to work with than
# "CustomerID: ALFKI, CompanyName: Alfreds Futterkiste, ContactName: Maria Anders",
# where it has to infer that CompanyName names the same thing CustomerID keys.
import re
from typing import Any


def humanize_column(name: str) -> str:
    """Turn a database-style column name into readable words:
    "ShipPostalCode" -> "ship postal code", "order_date" -> "order date"."""
    name = name.replace("_", " ")
    # Split camelCase/PascalCase boundaries, and acronym-to-word boundaries
    # ("OrderID" -> "Order ID", "ShipVia" -> "Ship Via").
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return name.strip().lower()


class StructuredIngestor:
    """Converts structured payload dictionaries into natural-language episode text."""

    def format_as_text(
        self,
        record: dict[str, Any],
        record_type: str,
        subject: str | None = None,
        subject_id: str | None = None,
    ) -> str:
        """Render one record as a sentence.

        `subject` is the record's human-readable name, when it has one. Passing
        it makes the name the thing being described and carries the id as a
        parenthetical, so extraction treats them as one entity rather than two.
        Without it, the record's first field leads instead.
        """
        items = list(record.items())

        if subject:
            head = f"{record_type} {subject}"
            if subject_id:
                head += f" (id {subject_id})"
            descriptors = [f"{humanize_column(k)} {v}" for k, v in items]
        elif items:
            _, id_value = items[0]
            head = f"{record_type} {id_value}"
            descriptors = [f"{humanize_column(k)} {v}" for k, v in items[1:]]
        else:
            return f"{record_type} record with no fields."

        if not descriptors:
            return head + "."
        return head + " has " + ", ".join(descriptors) + "."
