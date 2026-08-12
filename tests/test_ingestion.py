from app.ingestion.structured import StructuredIngestor


def test_structured_formatting():
    ingestor = StructuredIngestor()
    res = ingestor.format_as_text({"id": 1, "status": "active"}, "Account")
    assert "Account Record:" in res
