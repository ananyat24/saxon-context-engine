# Ingests the sample datasets in data/samples/ into the graph.
#
#   python scripts/ingest_samples.py northwind --limit 20
#   python scripts/ingest_samples.py legal --limit 2
#   python scripts/ingest_samples.py northwind --dry-run
#
# Each record costs several LLM calls (extraction, embedding, deduplication),
# and Gemini's free tier is rate-limited per minute, so:
#   - --limit caps how many records are sent in one run (default 20)
#   - --delay spaces records out (default 15s, tune to your quota)
#   - already-ingested records are skipped via data/processed/ingest_log.json,
#     and failures aren't marked, so a re-run retries exactly what failed
#   - --dry-run prints the generated text without calling the LLM at all, which
#     is the cheap way to check the text reads well before spending quota
import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from app.graph.graphiti_adapter import build_graphiti
from app.graph.spend_limiter import SpendLimitExceeded
from app.ingestion.file_source import FileSourceSpec, SourceRecord, read_csv_records, read_text_records
from app.ingestion.ingest_log import IngestLog
from app.ingestion.pipeline import IngestionPipeline
from app.ontology.bootstrap import build_scoped_registry
from app.ontology.graphiti_types import build_graphiti_schema

logging.basicConfig(level=logging.INFO, format="%(message)s")
# These libraries log every HTTP call at INFO, which drowns out this script's
# own progress output.
for noisy in ("neo4j", "graphiti_core", "httpx", "google_genai.models"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

SAMPLES = Path("data/samples")

# Which ontology domain pack(s) each dataset is extracted against. Scoping this
# per dataset keeps the extraction prompt focused: a Northwind ingest has no use
# for the legal or pharma vocabularies, and every unused type is prompt tokens
# spent plus one more option for the LLM to weigh.
DATASET_DOMAINS = {
    "northwind": ["sales", "supply_chain"],
    "contoso": ["sales", "supply_chain"],
    "manufacturing": ["manufacturing"],
    "legal": ["legal"],
}

# Ingestion order matters: customers/employees/products first so the entities
# they describe already exist in the graph by the time orders reference them.
# Graphiti resolves an entity mentioned in a later episode against ones it
# already knows, so seeding the "nouns" before the "transactions" gives it the
# best chance of linking an order to the right customer rather than inventing
# a new node for the same company.
NORTHWIND_SPECS = [
    FileSourceSpec("customers.csv", "Customer", "CustomerID", name_column="CompanyName"),
    FileSourceSpec("products.csv", "Product", "ProductID", name_column="ProductName"),
    FileSourceSpec("suppliers.csv", "Supplier", "SupplierID", name_column="CompanyName",
                   skip_columns={"HomePage"}),
    FileSourceSpec("shippers.csv", "Shipper", "ShipperID", name_column="CompanyName"),
    # Employees have no single name column (first/last are separate), so these
    # fall back to leading with the id.
    FileSourceSpec("employees.csv", "Employee", "EmployeeID",
                   skip_columns={"Notes", "PhotoPath", "TitleOfCourtesy", "Extension"}),
    FileSourceSpec("orders.csv", "Order", "OrderID", date_column="OrderDate",
                   skip_columns={"ShipAddress", "ShipPostalCode", "RequiredDate"}),
]

# How many orders to sample for a small Northwind ingest. Same relational
# problem as Contoso: orders.csv only carries CustomerID/EmployeeID/ShipVia,
# not product info (that's in order_details.csv, which nothing here ingests),
# so a small sample is built by sampling orders first and pulling only the
# customers/employees/shippers those specific orders reference. Products and
# suppliers aren't part of this sample since orders.csv doesn't link to them.
NORTHWIND_ORDERS_SAMPLE = 5


def _collect_northwind_sample() -> list[SourceRecord]:
    import csv as _csv

    base = SAMPLES / "northwind"
    with (base / "orders.csv").open("r", encoding="utf-8-sig", newline="") as f:
        order_rows = list(_csv.DictReader(f))[:NORTHWIND_ORDERS_SAMPLE]

    customer_ids = {row["CustomerID"] for row in order_rows}
    employee_ids = {row["EmployeeID"] for row in order_rows}
    shipper_ids = {row["ShipVia"] for row in order_rows}
    order_ids = {row["OrderID"] for row in order_rows}

    customers_spec, _products, _suppliers, shippers_spec, employees_spec, orders_spec = NORTHWIND_SPECS

    records: list[SourceRecord] = []
    records.extend(read_csv_records(
        base / customers_spec.filename, customers_spec,
        row_filter=lambda row, a=customer_ids: row.get("CustomerID") in a,
    ))
    records.extend(read_csv_records(
        base / employees_spec.filename, employees_spec,
        row_filter=lambda row, a=employee_ids: row.get("EmployeeID") in a,
    ))
    records.extend(read_csv_records(
        base / shippers_spec.filename, shippers_spec,
        row_filter=lambda row, a=shipper_ids: row.get("ShipperID") in a,
    ))
    records.extend(read_csv_records(
        base / orders_spec.filename, orders_spec,
        row_filter=lambda row, a=order_ids: row.get("OrderID") in a,
    ))
    return records

MANUFACTURING_SPECS = [
    FileSourceSpec("ai4i2020_sample.csv", "Machine reading", "UDI",
                   skip_columns={"RNF"}),
]

# Same ordering rationale as Northwind above: customers/products/stores are
# the "nouns" a sale refers to, so they need to exist first.
CONTOSO_SPECS = [
    # StartDT/EndDT/GeoAreaKey are warehouse plumbing (slowly-changing-dimension
    # bookkeeping), not facts about the person -- skipped so extraction spends
    # its attention (and tokens) on the columns that actually describe them.
    FileSourceSpec("customer.csv", "Customer", "CustomerKey",
                   skip_columns={"Latitude", "Longitude", "MiddleInitial", "StartDT", "EndDT", "GeoAreaKey"}),
    FileSourceSpec("product.csv", "Product", "ProductKey", name_column="ProductName"),
    FileSourceSpec("store.csv", "Store", "StoreKey", name_column="Description"),
    FileSourceSpec("sales.csv", "Sale", "OrderKey", date_column="OrderDate"),
]

# How many sales rows to sample for a small Contoso ingest. Unlike Northwind's
# collect() (which hands over every row and lets --limit truncate the front of
# the list), Contoso's tables are relational: truncating a flat concatenation
# would ingest a pile of customer profiles with no sales pointing at them,
# since customer.csv sorts first. Sampling sales first and then pulling only
# the customers/products/stores those specific sales reference keeps a small
# ingest genuinely connected instead of just small.
CONTOSO_SALES_SAMPLE = 6


def _collect_contoso() -> list[SourceRecord]:
    import csv as _csv

    base = SAMPLES / "contoso_dw"
    with (base / "sales.csv").open("r", encoding="utf-8-sig", newline="") as f:
        sales_rows = list(_csv.DictReader(f))[:CONTOSO_SALES_SAMPLE]

    referenced = {
        "CustomerKey": {row["CustomerKey"] for row in sales_rows},
        "ProductKey": {row["ProductKey"] for row in sales_rows},
        "StoreKey": {row["StoreKey"] for row in sales_rows},
    }

    records: list[SourceRecord] = []
    for spec, key_column in zip(CONTOSO_SPECS[:3], ["CustomerKey", "ProductKey", "StoreKey"]):
        path = base / spec.filename
        allowed = referenced[key_column]
        records.extend(read_csv_records(path, spec, row_filter=lambda row, k=key_column, a=allowed: row.get(k) in a))

    # Sales themselves aren't filtered further -- these specific rows are the
    # sample the customers/products/stores above were chosen to match.
    sales_spec = CONTOSO_SPECS[3]
    sampled_order_keys = {row["OrderKey"] for row in sales_rows}
    records.extend(
        read_csv_records(
            base / sales_spec.filename, sales_spec,
            row_filter=lambda row, a=sampled_order_keys: row.get("OrderKey") in a,
        )
    )
    return records


def collect(dataset: str) -> list[SourceRecord]:
    if dataset == "northwind":
        return _collect_northwind_sample()

    if dataset == "contoso":
        return _collect_contoso()

    if dataset == "manufacturing":
        records = []
        for spec in MANUFACTURING_SPECS:
            path = SAMPLES / "manufacturing_ai4i2020" / spec.filename
            if path.exists():
                records.extend(read_csv_records(path, spec))
        return records

    if dataset == "legal":
        return list(read_text_records(SAMPLES / "legal_cuad" / "contracts", "CUAD contract"))

    raise ValueError(f"Unknown dataset: {dataset}")


async def run(args: argparse.Namespace) -> None:
    records = collect(args.dataset)
    log = IngestLog()

    pending = [r for r in records if not log.already_ingested(args.group_id, r.name)]
    skipped = len(records) - len(pending)
    batch = pending[: args.limit]

    logger.info(
        f"{len(records)} record(s) found, {skipped} already ingested, "
        f"{len(pending)} pending, ingesting {len(batch)} this run."
    )

    if args.dry_run:
        for r in batch:
            logger.info(f"\n--- {r.name} ({r.source_description}) ---")
            body = r.body if len(r.body) <= 400 else r.body[:400] + f"... [{len(r.body)} chars total]"
            logger.info(body)
        logger.info(f"\nDry run: nothing sent to the LLM, nothing written to the graph.")
        return

    if not batch:
        logger.info("Nothing to do.")
        return

    # Constrain extraction to the ontology, so the LLM picks from types the
    # ontology defines instead of inventing its own.
    scoped = build_scoped_registry(DATASET_DOMAINS[args.dataset])
    entity_types, edge_types, edge_type_map = build_graphiti_schema(scoped)
    logger.info(
        f"Ontology scope: core + {', '.join(DATASET_DOMAINS[args.dataset])} "
        f"({len(entity_types)} entity types, {len(edge_types)} relationship types)"
    )

    graphiti = build_graphiti()
    pipeline = IngestionPipeline(
        graphiti,
        entity_types=entity_types,
        edge_types=edge_types,
        edge_type_map=edge_type_map,
    )
    ingested = 0
    failures: list[tuple[str, str]] = []
    consecutive_rate_limits = 0
    try:
        for i, record in enumerate(batch, 1):
            logger.info(f"[{i}/{len(batch)}] {record.name}")
            try:
                await pipeline.ingest_episode(
                    name=record.name,
                    body=record.body,
                    source_description=record.source_description,
                    group_id=args.group_id,
                    reference_time=record.reference_time,
                )
            except SpendLimitExceeded as e:
                # Unlike a rate limit, this isn't transient -- retrying later
                # won't clear it, so stop the whole run rather than burning
                # through the rest of the batch re-raising on every record.
                logger.error(f"  {e}")
                failures.append((record.name, str(e)))
                break
            except Exception as e:
                # One bad record (or a rate-limit hit) shouldn't discard the
                # progress already made -- the log is saved in `finally` below,
                # and only successful records are marked, so a re-run retries
                # exactly what failed rather than starting over.
                logger.error(f"  failed: {e}")
                failures.append((record.name, str(e)))

                if "rate limit" in str(e).lower():
                    consecutive_rate_limits += 1
                    # One record is several LLM calls in a tight burst (extraction
                    # plus multiple embedding calls), not one -- that burst alone
                    # can exceed a per-minute cap regardless of the delay between
                    # records. Retrying immediately just piles onto an already-
                    # exhausted window and never lets it clear: a real run of this
                    # script did exactly that, going 2 successes -> continuous
                    # failures with a flat 15s delay. Back off hard instead, and
                    # give up after a few in a row rather than burning through the
                    # rest of the batch on a limit that isn't clearing.
                    if consecutive_rate_limits >= 3:
                        logger.error(
                            f"\n{consecutive_rate_limits} rate limit errors in a row -- "
                            f"stopping early rather than continuing to fail. Wait a few "
                            f"minutes and re-run; it will resume from here."
                        )
                        break
                    backoff = args.delay * 4
                    logger.warning(f"  rate limited, backing off {backoff:.0f}s before continuing")
                    time.sleep(backoff)
                    continue
                else:
                    consecutive_rate_limits = 0

                if i < len(batch):
                    time.sleep(args.delay)
                continue

            consecutive_rate_limits = 0
            log.mark(args.group_id, record.name)
            ingested += 1
            if i < len(batch):
                time.sleep(args.delay)
    finally:
        log.save()
        await graphiti.close()

    logger.info(f"\nIngested {ingested} of {len(batch)} record(s) into group '{args.group_id}'.")
    logger.info(f"Total ingested for this group: {log.count(args.group_id)}")

    if failures:
        logger.warning(f"\n{len(failures)} record(s) failed and were NOT marked ingested:")
        for name, err in failures[:5]:
            logger.warning(f"  {name}: {err}")
        if any("rate limit" in err.lower() for _, err in failures):
            # Each record is several LLM calls (extraction, embedding, dedup),
            # not one, so the free tier's per-minute cap is easy to trip.
            logger.warning(
                f"\nRate limit hit. Re-run the same command to retry just these "
                f"records, ideally with a longer --delay (currently {args.delay}s)."
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", choices=["northwind", "contoso", "manufacturing", "legal"])
    parser.add_argument("--group-id", default="samples", help="Tenant/data-isolation bucket (default: samples)")
    parser.add_argument("--limit", type=int, default=20, help="Max records to ingest this run (default: 20)")
    # 15s because one record is several LLM calls, not one -- 4s between
    # records tripped Gemini's free-tier limit on 6 of 10 records in testing.
    parser.add_argument("--delay", type=float, default=15.0, help="Seconds between records (default: 15)")
    parser.add_argument("--dry-run", action="store_true", help="Print generated text without calling the LLM")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
