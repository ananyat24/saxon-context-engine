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
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector
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


class WebFetchError(ConnectorFetchError):
    """A URL couldn't be fetched, or didn't look like real page content."""


# A tenant supplies this URL and the server fetches it on the tenant's
# behalf -- without a check like this, a connector is a ready-made SSRF
# primitive: "add a web connector pointed at http://169.254.169.254/..." (the
# Azure/AWS/GCP instance-metadata address) or http://localhost:<internal-port>
# would make this server, sitting inside the deployment's own network, fetch
# and then hand back whatever internal service lives there. Only "the request
# would leave the deployment's network to a normal public address" is allowed.
_MAX_REDIRECTS = 5


def _assert_public_host(hostname: str) -> None:
    """Resolves `hostname` and rejects it if any resolved address is
    loopback/private/link-local/reserved/multicast -- covers "localhost",
    bare IPs, and hostnames that merely resolve to an internal address (DNS
    rebinding via a domain the attacker controls)."""
    try:
        addrs = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise WebFetchError(f"Could not resolve host '{hostname}': {e}") from e
    for family, _, _, _, sockaddr in addrs:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise WebFetchError(f"'{hostname}' resolves to a non-public address and can't be fetched.")


def _assert_fetchable(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise WebFetchError(f"'{url}' must be an http(s) URL.")
    if not parsed.hostname:
        raise WebFetchError(f"'{url}' has no host to fetch.")
    _assert_public_host(parsed.hostname)


async def fetch_web_record(url: str) -> SourceRecord:
    """Fetches `url`, strips it down to plain text, and returns it as a
    SourceRecord ready for IngestionPipeline.ingest_episode() -- the same
    shape read_csv_records()/read_text_records() produce for their sources.

    Raises WebFetchError (never a raw httpx exception) on anything that
    should stop a sync and surface a clear message: unreachable host, non-2xx
    status, a non-text response (e.g. a PDF or image), too large, or nothing
    extractable once the tags are stripped. Also rejects the URL up front,
    and every redirect hop, if it points at a non-public address -- see
    _assert_fetchable().
    """
    try:
        # Redirects are followed manually (not follow_redirects=True) so each
        # hop's target gets the same public-address check as the original
        # URL -- otherwise a public URL that 302s to an internal address
        # would sail straight through the check above.
        async with httpx.AsyncClient(follow_redirects=False, timeout=_FETCH_TIMEOUT_SECONDS) as client:
            current_url = url
            for _ in range(_MAX_REDIRECTS + 1):
                _assert_fetchable(current_url)
                resp = await client.get(current_url)
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        break
                    current_url = str(httpx.URL(current_url).join(location))
                    continue
                resp.raise_for_status()
                break
            else:
                raise WebFetchError(f"'{url}' redirected too many times.")
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


class WebConnector(SourceConnector):
    """The SourceConnector implementation for a single web page -- thin
    wrapper around fetch_web_record()/content_hash() above, so both the
    plain functions (used directly by earlier code/tests) and the generic
    connector interface (used by app/api/connectors.py's dispatch table)
    stay available without duplicating logic."""

    def __init__(self, url: str):
        self.url = url

    async def fetch(self) -> list[SourceRecord]:
        return [await fetch_web_record(self.url)]

    def content_hash(self, records: list[SourceRecord]) -> str:
        return content_hash(records[0]) if records else ""

    def source_description(self) -> str:
        return f"Web page ({self.url})"
