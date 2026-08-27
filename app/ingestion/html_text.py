# Shared "strip tags, keep the visible text" helper -- used by every
# connector that can receive HTML content (a fetched web page, an Outlook/
# Gmail message body). Deliberately dependency-free (stdlib html.parser, not
# BeautifulSoup/lxml): MVP scope doesn't justify a new dependency for
# something this small. Reach for a real HTML library if a connector ever
# needs more than this.
import re
from html.parser import HTMLParser

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


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text().strip()
