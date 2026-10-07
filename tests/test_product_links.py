"""Linking a shortlist option to the merchant's own product page.

The link is built from the merchant's strings -- its id and its SKUs -- and points
at a page the merchant writes, which in this demo includes one written to
manipulate whoever reads it. So the tests are mostly about what cannot come back
out of that: a SKU cannot escape its path segment, a misconfigured merchant URL
cannot become a `javascript:` href, and the page is opened in its own tab with
noopener rather than inlined anywhere near the gateway's UI.

The other half is duller and is the one that would actually break: both shortlist
paths rebuild an Option to attach `why`, and a field missing from that rebuild is
a field silently dropped on the way to the screen.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from mandate import shopping
from mandate.shopping import Option, page_url

PORTAL = Path(__file__).resolve().parents[1] / "src/mandate/gateway/static/portal.html"

CATALOG = [
    {"sku": "SKU-A", "name": "Navy dress", "description": "midi", "unit_price": "89.00"},
    {"sku": "SKU-B", "name": "Black shift", "description": "knee", "unit_price": "72.00"},
]


class FakeBackend:
    model = "fake-model"
    provider = "fake"

    def __init__(self, text: str):
        self.text = text

    async def complete(self, *, system, turns, tools):
        return type("C", (), {"text": self.text, "calls": [], "usage": None})()


# -- the url itself ---------------------------------------------------------


def test_it_points_at_the_merchants_own_page():
    assert page_url("http://localhost:8001", "m_thread", "SKU-DRESS-NAVY-S") == (
        "http://localhost:8001/merchants/m_thread/products/SKU-DRESS-NAVY-S/page"
    )


def test_a_trailing_slash_does_not_double_up():
    assert page_url("https://shop.test/", "m", "S") == (
        "https://shop.test/merchants/m/products/S/page"
    )


def test_a_base_path_is_kept():
    assert page_url("https://example.test/shop/", "m", "S") == (
        "https://example.test/shop/merchants/m/products/S/page"
    )


@pytest.mark.parametrize(
    "sku",
    [
        "SKU/../../admin",
        'SKU" onmouseover="alert(1)',
        "SKU with spaces",
        "SKU?x=1&y=2",
        "SKU#fragment",
    ],
)
def test_a_hostile_sku_cannot_leave_its_path_segment(sku):
    """SKUs are the merchant's strings. A slash in one must not become a path."""
    url = page_url("https://shop.test", "m_thread", sku)
    tail = url.removeprefix("https://shop.test/merchants/m_thread/products/")
    segment = tail.removesuffix("/page")
    assert "/" not in segment
    assert '"' not in segment
    assert "?" not in segment and "#" not in segment and " " not in segment
    assert url.endswith("/page")


@pytest.mark.parametrize(
    "base",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "",
        "   ",
        "not-a-url",
        "https://",
    ],
)
def test_anything_a_browser_should_not_follow_produces_no_link(base):
    """No link at all beats a link that does something else."""
    assert page_url(base, "m", "S") == ""


# -- it survives both shortlist paths --------------------------------------


def test_every_option_field_is_carried_through_the_rebuild():
    """Both paths rebuild an Option from _as_dict_full to attach `why`."""
    fields = {f.name for f in dataclasses.fields(Option)}
    carried = set(shopping._as_dict_full(
        Option("s", "n", "d", None, "m", "M", "w", "https://x.test/p")
    ))
    assert fields - carried == set(), f"dropped on the way to the page: {fields - carried}"


@pytest.mark.asyncio
async def test_the_model_path_keeps_the_link(monkeypatch):
    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    options, how = await shopping.propose(
        "a dress",
        merchant_url="https://shop.test",
        merchant_id="m_thread",
        backend=FakeBackend('{"options": [{"sku": "SKU-A", "why": "fits"}]}'),
    )
    assert options[0].page_url == (
        "https://shop.test/merchants/m_thread/products/SKU-A/page"
    )
    assert "chosen by" in how


@pytest.mark.asyncio
async def test_the_keyword_fallback_keeps_the_link_too(monkeypatch):
    """The path a judge with no API key sees. It had no link the first time."""
    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    options, _ = await shopping.propose(
        "a dress", merchant_url="https://shop.test", merchant_id="m_thread", backend=None
    )
    assert options
    assert all(o.page_url.endswith("/page") for o in options)


@pytest.mark.asyncio
async def test_the_link_reaches_the_json_the_page_reads(monkeypatch):
    async def fake_catalog(url, mid):
        return CATALOG

    monkeypatch.setattr(shopping, "catalog", fake_catalog)
    options, _ = await shopping.propose(
        "a dress", merchant_url="https://shop.test", merchant_id="m_thread", backend=None
    )
    assert options[0].as_json()["page_url"].startswith("https://shop.test/merchants/")


# -- how the page opens it -------------------------------------------------


def _anchor() -> str:
    """The product link as the page builds it, from the <a to its closing >."""
    page = PORTAL.read_text(encoding="utf-8")
    start = page.index("`<a href=")
    return page[start : page.index(">", page.index("rel=", start))]


def test_the_portal_opens_it_in_its_own_tab_with_noopener():
    """The page on the other end is a third party's, and one of them in this demo
    is actively hostile. It gets no handle on the window that opened it."""
    anchor = _anchor()
    assert 'target="_blank"' in anchor
    assert 'rel="noopener noreferrer"' in anchor


def test_the_portal_escapes_the_href():
    assert "esc(o.page_url)" in _anchor(), "the href has to go through esc()"


def test_no_link_means_plain_text_not_a_link_to_nowhere():
    page = PORTAL.read_text(encoding="utf-8")
    ternary = page[page.index("${o.page_url") : page.index("</div>", page.index("${o.page_url"))]
    assert ": esc(o.name)}" in ternary, (
        "the falsy branch of the ternary must render the name as plain text"
    )
