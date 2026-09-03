# Reads sample/export files off disk and turns them into records ready for
# ingestion. This is the "generic connector" that stands in for a real source
# system (a CRM's API, an ERP export job) while there isn't one yet: drop CSVs
# or .txt files in a directory, point this at it, and every row/document becomes
# an episode for Graphiti to extract facts from.
#
# Two shapes are handled:
#   - CSV  -> one record per row, described by a FileSourceSpec (below)
#   - .txt -> one record per file, ingested whole as unstructured text
import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class SourceRecord:
    """One unit of content to ingest, plus the metadata Graphiti needs."""

    name: str
    body: str
    source_description: str
    reference_time: Optional[datetime] = None


@dataclass
class FileSourceSpec:
    """Describes how to read one CSV file: which column identifies a row, what
    kind of thing each row is, and (optionally) which column holds the date the
    row's content actually happened.

    This exists because a CSV alone doesn't say what its rows mean. Ingesting
    orders.csv well needs to know "each row is an Order, keyed by OrderID, and
    happened on OrderDate": guessing that from column names would be brittle.
    """

    filename: str
    record_type: str
    id_column: str
    # The column holding the record's human-readable name, when it has one.
    # Without this, extraction treats the id and the name as two separate
    # things: ingesting a customer row keyed on CustomerID produced both
    # "ALFKI" and "Alfreds Futterkiste" as distinct entities, linked only by a
    # vague RELATED_TO, when they are one company. Naming the record up front
    # and carrying the id as an attribute keeps it a single entity.
    name_column: Optional[str] = None
    date_column: Optional[str] = None
    date_formats: tuple[str, ...] = ("%m/%d/%Y", "%Y-%m-%d", "%d/%m/%Y")
    # Columns to leave out of the generated text: typically bulk fields that
    # add tokens (and LLM cost) without adding facts worth extracting.
    skip_columns: set[str] = field(default_factory=set)


def parse_date(value: str, formats: tuple[str, ...]) -> Optional[datetime]:
    """Best-effort date parsing across the formats a spec lists. Returns None
    rather than raising, so one malformed date doesn't abort a whole file:
    the record still ingests, just without a reference time."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    logger.warning(f"Could not parse date '{value}'")
    return None


def read_csv_records(
    path: Path, spec: FileSourceSpec, row_filter: Optional[Callable[[dict], bool]] = None
) -> Iterator[SourceRecord]:
    """Yield one SourceRecord per row of a CSV.

    row_filter, if given, skips rows it returns False for, e.g. limiting a
    dimension table to just the rows a small slice of a fact table actually
    references, so a curated sample stays connected instead of pulling in
    unrelated rows that nothing else in the sample points to.
    """
    from app.ingestion.structured import StructuredIngestor

    ingestor = StructuredIngestor()
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row_filter is not None and not row_filter(row):
                continue

            record_id = row.get(spec.id_column, "").strip()
            if not record_id:
                continue

            payload = {k: v for k, v in row.items() if k not in spec.skip_columns and (v or "").strip()}
            reference_time = (
                parse_date(row.get(spec.date_column, ""), spec.date_formats) if spec.date_column else None
            )

            subject = (row.get(spec.name_column) or "").strip() if spec.name_column else ""
            if subject:
                # Lead with the name, and demote the id/name columns out of the
                # descriptor list so they aren't restated as separate facts.
                payload = {
                    k: v for k, v in payload.items() if k not in (spec.id_column, spec.name_column)
                }
                body = ingestor.format_as_text(payload, spec.record_type, subject=subject, subject_id=record_id)
            else:
                body = ingestor.format_as_text(payload, spec.record_type)

            yield SourceRecord(
                name=f"{spec.record_type.lower()}-{record_id}",
                body=body,
                source_description=f"{path.name} ({spec.record_type})",
                reference_time=reference_time,
            )


def read_text_records(directory: Path, source_description: str) -> Iterator[SourceRecord]:
    """Yield one SourceRecord per .txt file in a directory, ingested whole."""
    from app.ingestion.unstructured import UnstructuredIngestor

    ingestor = UnstructuredIngestor()
    for path in sorted(directory.glob("*.txt")):
        text = ingestor.clean_text(path.read_text(encoding="utf-8", errors="replace"))
        if not text:
            continue
        yield SourceRecord(
            name=path.stem,
            body=text,
            source_description=f"{source_description} ({path.name})",
        )
