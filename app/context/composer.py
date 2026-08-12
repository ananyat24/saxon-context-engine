# Formats a flat list of fact strings into a bullet-point block, the kind of thing
# you'd paste into an LLM prompt as grounding context. Deliberately simple/text-only
# for now -- more structured composition (grouping by entity, adding citations from
# Evidence records) would build on top of this.
class ContextComposer:
    """Composes retrieved context fragments into a formatted LLM prompt block."""

    def compose(self, fact_strings: list[str]) -> str:
        return "\n".join(f"- {fact}" for fact in fact_strings)
