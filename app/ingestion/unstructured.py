# Placeholder for cleanup/preprocessing of raw text sources (documents, transcripts,
# emails) before they're handed to IngestionPipeline. Currently just trims
# whitespace; this is where you'd add things like stripping email signatures/quoted
# reply chains, de-duplicating boilerplate, or splitting an overly long document
# into smaller episodes, once a real source of unstructured text is wired up.
class UnstructuredIngestor:
    """Processes raw text snippets, documents, and transcripts."""

    def clean_text(self, text: str) -> str:
        return text.strip()
