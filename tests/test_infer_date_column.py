# Pure-logic tests for _infer_date_column/_infer_spec (app/ingestion/database_source.py)
# -- no Neo4j, no filesystem. Covers the fix for a real, demonstrated gap:
# an auto-inferred spec (any CSV dropped into a connector's upload folder
# without a hand-picked FileSourceSpec) never tried to detect a date column,
# so every such episode's reference_time defaulted to ingestion time
# regardless of what the CSV's own date fields said -- see CLAUDE.md's v1
# status note on the Solandra day1/day2 transition-tracking gap this
# contributed to.
from app.ingestion.database_source import _infer_date_column, _infer_spec


def test_prefers_a_column_whose_name_literally_contains_date():
    assert _infer_date_column(["OrderID", "CustomerName", "OrderDate", "Status"]) == "OrderDate"


def test_falls_back_to_a_recognized_created_at_style_column():
    assert _infer_date_column(["ID", "Name", "CreatedAt"]) == "CreatedAt"


def test_falls_back_to_a_recognized_updated_on_style_column():
    assert _infer_date_column(["ID", "Name", "UpdatedOn"]) == "UpdatedOn"


def test_does_not_false_positive_on_location_ending_in_on():
    # "Location" ends in "on" the same as "UpdatedOn" does -- a naive
    # suffix-only check would wrongly treat it as a date column.
    assert _infer_date_column(["ID", "Name", "Location"]) is None


def test_returns_none_when_no_column_looks_like_a_date():
    assert _infer_date_column(["ID", "Name", "Status", "Owner"]) is None


def test_date_named_column_takes_priority_over_created_at_style():
    result = _infer_date_column(["ID", "CreatedAt", "ShipDate"])
    assert result == "ShipDate"


def test_infer_spec_wires_the_detected_date_column_through():
    spec = _infer_spec("shipments.csv", ["ShipmentID", "Status", "ShipDate"])
    assert spec.date_column == "ShipDate"


def test_infer_spec_date_column_is_none_when_nothing_matches():
    spec = _infer_spec("notes.csv", ["NoteID", "Text", "Author"])
    assert spec.date_column is None
