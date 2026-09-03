from datetime import datetime
from pathlib import Path

from app.ingestion.file_source import FileSourceSpec, parse_date, read_csv_records
from app.ingestion.ingest_log import IngestLog
from app.ingestion.structured import StructuredIngestor, humanize_column


def test_structured_formatting():
    ingestor = StructuredIngestor()
    res = ingestor.format_as_text({"id": 1, "status": "active"}, "Account")
    assert res == "Account 1 has status active."


def test_structured_formatting_single_field():
    ingestor = StructuredIngestor()
    assert ingestor.format_as_text({"id": "X1"}, "Widget") == "Widget X1."


def test_structured_formatting_with_named_subject():
    # Naming the subject keeps the id and the name one entity during
    # extraction rather than two loosely-related ones.
    ingestor = StructuredIngestor()
    res = ingestor.format_as_text(
        {"City": "Berlin"}, "Customer", subject="Alfreds Futterkiste", subject_id="ALFKI"
    )
    assert res == "Customer Alfreds Futterkiste (id ALFKI) has city Berlin."


def test_read_csv_records_uses_name_column_as_subject():
    spec = FileSourceSpec("customers.csv", "Customer", "CustomerID", name_column="CompanyName")
    records = list(read_csv_records(Path("data/samples/northwind/customers.csv"), spec))

    alfki = next(r for r in records if r.name == "customer-ALFKI")
    assert alfki.body.startswith("Customer Alfreds Futterkiste (id ALFKI)")
    # The name shouldn't also be restated as a descriptor.
    assert "company name" not in alfki.body.lower()


def test_humanize_column():
    # Column names get split into words so the extraction LLM reads prose
    # rather than database identifiers.
    assert humanize_column("ShipPostalCode") == "ship postal code"
    assert humanize_column("order_date") == "order date"
    assert humanize_column("CustomerID") == "customer id"


def test_parse_date_handles_multiple_formats():
    formats = ("%m/%d/%Y", "%Y-%m-%d")
    assert parse_date("7/4/1996", formats) == datetime(1996, 7, 4)
    assert parse_date("1996-07-04", formats) == datetime(1996, 7, 4)


def test_parse_date_returns_none_for_unparseable():
    # A bad date shouldn't abort a whole file: the record still ingests
    # without a reference time.
    assert parse_date("not a date", ("%Y-%m-%d",)) is None
    assert parse_date("", ("%Y-%m-%d",)) is None


def test_read_csv_records_from_sample_data():
    spec = FileSourceSpec("customers.csv", "Customer", "CustomerID")
    records = list(read_csv_records(Path("data/samples/northwind/customers.csv"), spec))

    assert len(records) == 91
    alfki = next(r for r in records if r.name == "customer-ALFKI")
    assert "Alfreds Futterkiste" in alfki.body
    assert alfki.source_description == "customers.csv (Customer)"


def test_read_csv_records_parses_reference_time():
    spec = FileSourceSpec("orders.csv", "Order", "OrderID", date_column="OrderDate")
    records = list(read_csv_records(Path("data/samples/northwind/orders.csv"), spec))

    first = records[0]
    assert first.reference_time == datetime(1996, 7, 4)


def test_read_csv_records_honors_skip_columns():
    spec = FileSourceSpec("customers.csv", "Customer", "CustomerID", skip_columns={"Fax", "Phone"})
    records = list(read_csv_records(Path("data/samples/northwind/customers.csv"), spec))

    alfki = next(r for r in records if r.name == "customer-ALFKI")
    assert "fax" not in alfki.body.lower()
    assert "phone" not in alfki.body.lower()


def test_ingest_log_roundtrip(tmp_path):
    log_path = tmp_path / "ingest_log.json"
    log = IngestLog(log_path)

    assert log.already_ingested("tenant-a", "order-1") is False
    log.mark("tenant-a", "order-1")
    log.save()

    # A fresh instance reads back what the previous run recorded: this is what
    # stops a re-run from re-spending LLM calls on records already ingested.
    reloaded = IngestLog(log_path)
    assert reloaded.already_ingested("tenant-a", "order-1") is True
    assert reloaded.already_ingested("tenant-b", "order-1") is False
    assert reloaded.count("tenant-a") == 1
