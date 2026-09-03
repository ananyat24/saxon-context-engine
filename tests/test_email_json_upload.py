# EmailConnector's new upload-folder path (see app/ingestion/email_source.py)
#: reads a JSON array export ({from, to, subject, date, body} objects, the
# shape a Gmail/Outlook data export produces) the same way DatabaseConnector
# reads uploaded CSVs, falling back to the bundled mock inbox when nothing's
# been uploaded. No real network/Neo4j needed.
import asyncio
import json

from app.ingestion.email_source import EmailConnector


def test_falls_back_to_mock_inbox_with_no_connector_id():
    connector = EmailConnector()
    records = asyncio.run(connector.fetch())
    assert len(records) > 0
    assert connector.source_description() == "Demo inbox (mock email data)"


def test_falls_back_to_mock_inbox_when_upload_folder_is_empty(monkeypatch, tmp_path):
    import app.ingestion.email_source as email_source

    monkeypatch.setattr(email_source, "connector_upload_dir", lambda cid: tmp_path / cid)
    connector = EmailConnector("some-connector-id")
    records = asyncio.run(connector.fetch())
    assert len(records) > 0  # the bundled mock data, not an empty result


def test_reads_an_uploaded_json_export(monkeypatch, tmp_path):
    import app.ingestion.email_source as email_source

    monkeypatch.setattr(email_source, "connector_upload_dir", lambda cid: tmp_path / cid)
    upload_dir = tmp_path / "connector-1"
    upload_dir.mkdir()
    (upload_dir / "gmail_messages.json").write_text(
        json.dumps(
            [
                {
                    "from": "Owen Whitfield <owen@example.com>",
                    "to": "Marcus Voss <marcus@example.com>",
                    "subject": "Expedited qualification request",
                    "date": "Thu, 20 Aug 2026 14:12:00 -0700",
                    "body": "Following up on our call about the CX-17 Power Relay.",
                }
            ]
        ),
        encoding="utf-8",
    )

    connector = EmailConnector("connector-1", source_label="Gmail")
    records = asyncio.run(connector.fetch())

    assert len(records) == 1
    assert "CX-17 Power Relay" in records[0].body
    assert "Owen Whitfield" in records[0].body
    assert records[0].source_description.startswith("Gmail (")
    assert connector.source_description() == "Gmail (uploaded export)"


def test_skips_messages_with_no_body_and_malformed_files_without_failing(monkeypatch, tmp_path):
    import app.ingestion.email_source as email_source

    monkeypatch.setattr(email_source, "connector_upload_dir", lambda cid: tmp_path / cid)
    upload_dir = tmp_path / "connector-2"
    upload_dir.mkdir()
    (upload_dir / "batch1.json").write_text(
        json.dumps(
            [
                {"from": "a@example.com", "subject": "Empty body", "body": "   "},
                {"from": "b@example.com", "subject": "Real message", "body": "Real content here."},
            ]
        ),
        encoding="utf-8",
    )
    (upload_dir / "not_json_at_all.json").write_text("{not valid json", encoding="utf-8")
    (upload_dir / "not_a_list.json").write_text(json.dumps({"oops": "this is an object, not an array"}))

    connector = EmailConnector("connector-2")
    records = asyncio.run(connector.fetch())

    assert len(records) == 1
    assert "Real content here." in records[0].body


def test_content_hash_matches_shared_hash_records_helper():
    from app.ingestion.connector_base import hash_records
    from app.ingestion.file_source import SourceRecord

    records = [SourceRecord(name="a", body="x", source_description="d")]
    connector = EmailConnector()
    assert connector.content_hash(records) == hash_records(records)
