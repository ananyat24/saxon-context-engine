from typing import List


class ContextComposer:
    """Composes retrieved context fragments into a formatted LLM prompt block."""

    def compose(self, fact_strings: List[str]) -> str:
        return "\n".join([f"- {fact}" for fact in fact_strings])
