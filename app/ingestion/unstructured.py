class UnstructuredIngestor:
    """Processes raw text snippets, documents, and transcripts."""

    def clean_text(self, text: str) -> str:
        return text.strip()
