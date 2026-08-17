"""Shared test helpers.

Lives at `tests/` so both `tests/unit` and `tests/integration` see it; neither
directory is a package, so a fixture is the import path that works from both.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

# Attributes whose value is a URL the browser will fetch. `href` covers
# stylesheets, favicons and anchors; `data` is <object>; `poster` is <video>;
# `ping`, `action`, `formaction`, `cite`, `longdesc`, `background` and
# `manifest` are the rest of the fetching surface HTML still has.
_URL_ATTRS = frozenset(
    {
        "src",
        "href",
        "srcset",
        "imagesrcset",
        "data",
        "poster",
        "action",
        "formaction",
        "background",
        "cite",
        "longdesc",
        "ping",
        "manifest",
        "xlink:href",
    }
)

# CSS constructs that reach off-document, or exist only to. `url(...)` is
# checked by its argument rather than banned outright, because `url(data:...)`
# is genuinely self-contained; the other three are banned as written.
_CSS_BANNED = (
    ("@import", "@import pulls in another stylesheet"),
    ("@font-face", "@font-face is how a web font gets in"),
    ("image-set(", "image-set() names alternate image sources"),
)
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)


def _is_off_document(url: str) -> bool:
    """True when *url* would make the browser fetch something else.

    Only two things are self-contained: a fragment into this same document,
    and a `data:` URI, which carries its own bytes. Everything else --
    absolute (`https://x`), protocol-relative (`//x`), root-relative (`/x`)
    and plain relative (`style.css`) alike -- is a second file this document
    does not contain.
    """
    stripped = url.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return not stripped.lower().startswith("data:")


def _srcset_urls(value: str) -> list[str]:
    """The URLs in a srcset, dropping each candidate's descriptor.

    Not `value.split(",")`: a `data:` URI contains a comma of its own
    (`data:image/gif;base64,R0lGOD`), and splitting on commas tore one in half
    and reported the tail as an off-document reference. That is a false
    positive on the one URL form that IS self-contained, and it made a
    mutation test pass for the wrong reason. Parsed the way the HTML spec
    does instead: a candidate's URL is the leading run of non-whitespace, an
    optional descriptor follows, and the comma separates candidates.
    """
    urls: list[str] = []
    index, end = 0, len(value)
    while index < end:
        while index < end and (value[index].isspace() or value[index] == ","):
            index += 1
        start = index
        while index < end and not value[index].isspace():
            index += 1
        if index > start:
            # A trailing comma is the separator, not part of the URL: a
            # candidate written without a descriptor ends `x.png,`.
            urls.append(value[start:index].rstrip(","))
        while index < end and value[index] != ",":
            index += 1
    return urls


class _SelfContainmentAuditor(HTMLParser):
    """Collects every off-document reference in a whole document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.offences: list[str] = []
        self._in_style = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "style":
            self._in_style = True
        for name, value in attrs:
            if value is None:
                continue
            lowered = name.lower()
            if lowered == "style":
                self._audit_css(value, f"<{tag} style=...>")
            elif lowered in _URL_ATTRS:
                urls = (
                    _srcset_urls(value)
                    if lowered in ("srcset", "imagesrcset")
                    else [value]
                )
                for url in urls:
                    if _is_off_document(url):
                        self.offences.append(f"<{tag} {lowered}={url!r}>")
        # <meta http-equiv="refresh" content="0; url=...">
        attr_map = {name.lower(): value for name, value in attrs}
        if tag == "meta" and (attr_map.get("http-equiv") or "").lower() == "refresh":
            content = attr_map.get("content") or ""
            match = re.search(r"url\s*=\s*(\S+)", content, re.IGNORECASE)
            if match and _is_off_document(match.group(1)):
                self.offences.append(f"<meta refresh {match.group(1)!r}>")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self._audit_css(data, "<style>")

    def _audit_css(self, css: str, where: str) -> None:
        lowered = css.lower()
        for needle, why in _CSS_BANNED:
            if needle in lowered:
                self.offences.append(f"{where}: {needle} -- {why}")
        for _, url in _CSS_URL.findall(css):
            if _is_off_document(url):
                self.offences.append(f"{where}: url({url!r})")


def _offences(html: str) -> list[str]:
    """Every off-document reference in *html*. Empty means self-contained."""
    auditor = _SelfContainmentAuditor()
    auditor.feed(html)
    auditor.close()
    return auditor.offences


@pytest.fixture
def self_containment_offences():
    """The raw auditor, for tests that assert a document DOES offend."""
    return _offences


@pytest.fixture
def assert_self_contained():
    """Assert a rendered report fetches nothing but itself.

    Replaces `assert "https://" not in html.split("</style>")[0]`, which
    inspected the HEAD ONLY and matched one literal scheme. Swapping
    deliberately-broken templates past that assertion: a remote <img>, a
    remote <iframe>, an @font-face web font, a protocol-relative
    `url(//cdn...)`, an `@import url("//cdn...")`, a remote srcset and a
    `<script defer src="https://...">` all PASSED. Seven of eight regressions
    it exists to prevent went undetected; only a remote favicon <link>, which
    happens to sit in the head, failed. `test_report.py` pins all eight
    against this auditor so the replacement cannot quietly go vacuous too.
    """

    def _assert(html: str) -> None:
        offences = _offences(html)
        assert offences == [], (
            "the report must fetch nothing but itself; these references leave "
            "the document:\n  " + "\n  ".join(offences)
        )

    return _assert


# Docker helpers, so the integration suite sees the same fixtures the unit suite
# does. One definition, in `tests/_docker.py` -- see its docstring for why a
# conftest could not be that definition.
from _docker import (  # noqa: E402
    build_is_expected as build_is_expected,
)
from _docker import (  # noqa: E402
    docker_expected as docker_expected,
)
from _docker import (  # noqa: E402
    docker_works as docker_works,
)
from _docker import (  # noqa: E402
    needs_docker as needs_docker,
)
from _docker import (  # noqa: E402
    sandbox_image as sandbox_image,
)
