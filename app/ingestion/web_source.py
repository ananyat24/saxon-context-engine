# Fetches a single web page and turns it into a SourceRecord the same shape
# app/ingestion/file_source.py produces for a CSV row or .txt file -- so a web
# page flows through the exact same IngestionPipeline.ingest_episode() call as
# every other source, with no special-casing needed downstream. This is the
# first real "connector" (a live external source, not a local sample file) --
# see app/graph/connectors.py for the connector record this backs.
#
# Deliberately dependency-free (stdlib html.parser, not BeautifulSoup/lxml):
# MVP scope for one connector type doesn't justify a new dependency. Reach for
# a real HTML library if/when a connector needs more than "strip tags, keep
# the visible text."
import hashlib
import re
from html.parser import HTMLParser

import httpx

from app.ingestion.file_source import SourceRecord

# Fetching an arbitrary external URL on a user's behalf needs a hard timeout
# and a size cap -- otherwise one slow or huge page ties up the request
# indefinitely. 10s and 2MB are generous for a normal web page/article.
_FETCH_TIMEOUT_SECONDS = 10.0
_MAX_CONTENT_BYTES = 2 * 1024 * 1024
# Keeps a single sync's LLM cost bounded regardless of how long the page is --
# extraction cost scales with input tokens, and nothing here should let one
# oversized page turn into a surprise bill.
_MAX_TEXT_CHARS = 20_000

# Elements whose contents are never real page text -- markup/behavior/style,
# not something a person reading the page would see.
_SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        # Collapses the many small whitespace-only text nodes HTML produces
        # between tags into normal paragraph-ish spacing, rather than one
        # word per line.
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._chunks))


class WebFetchError(Exception):
    """A URL couldn't be fetched, or didn't look like real page content."""


async def fetch_web_record(url: str) -> SourceRecord:
    """Fetches `url`, strips it down to plain text, and returns it as a
    SourceRecord ready for IngestionPipeline.ingest_episode() -- the same
    shape read_csv_records()/read_text_records() produce for their sources.

    Raises WebFetchError (never a raw httpx exception) on anything that
    should stop a sync and surface a clear message: unreachable host, non-2xx
    status, a non-text response (e.g. a PDF or image), too large, or nothing
    extractable once the tags are stripped.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=_FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as e:
        raise WebFetchError(f"Could not fetch '{url}': {e}") from e

    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        raise WebFetchError(f"'{url}' returned {content_type or 'an unknown content type'}, not a web page.")
    if len(resp.content) > _MAX_CONTENT_BYTES:
        raise WebFetchError(f"'{url}' is larger than the {_MAX_CONTENT_BYTES // (1024 * 1024)}MB fetch limit.")

    parser = _TextExtractor()
    parser.feed(resp.text)
    text = parser.text().strip()
    if not text:
        raise WebFetchError(f"'{url}' had no extractable text content.")
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n\n[truncated -- page content exceeded the ingest size cap]"

    return SourceRecord(
        name=f"web-{hashlib.sha1(url.encode()).hexdigest()[:12]}",
        body=text,
        source_description=f"Web page ({url})",
    )


def content_hash(record: SourceRecord) -> str:
    """A cheap fingerprint of a fetched page's text, used to skip re-ingesting
    (and re-paying for extraction on) a sync that found no real change since
    last time -- see app/graph/connectors.py."""
    return hashlib.sha256(record.body.encode("utf-8")).hexdigest()
