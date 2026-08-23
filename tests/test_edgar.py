"""EdgarClient (httpx MockTransport, no network) — transport client, and the
EdgarTools ToolProvider reintroduced over it for filing text/citation use.

Audit CA-03 removed the original EdgarTools because it exposed financial
FACTS directly, bypassing market-data-hub; that stands (company_facts is not
on EdgarTools). Filing text is not a fact market-data-hub tracks, and this
provider is opt-in, not part of any bundled finance agent's default tools."""

from __future__ import annotations

import httpx
import pytest

from lazytools.connectors.edgar import EdgarClient, EdgarTools
from lazytools.safety import UrlBlocked

# --- canned SEC payloads ------------------------------------------------ #

COMPANY_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1418121, "ticker": "APLE", "title": "Apple Hospitality REIT, Inc."},
    "2": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    "3": {"cik_str": 999999, "ticker": "ZZZ", "title": "AAPL Tracker Fund"},
}

SUBMISSIONS = {
    "cik": "320193",
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-24-000123", "0000320193-24-000100", "0000320193-23-000106"],
            "form": ["10-K", "8-K", "10-K"],
            "filingDate": ["2024-11-01", "2024-08-02", "2023-11-03"],
            "reportDate": ["2024-09-28", "", "2023-09-30"],
            "items": ["", "2.02,9.01", ""],
            "primaryDocument": ["aapl-20240928.htm", "aapl-8k.htm", "aapl-20230930.htm"],
        }
    },
}

COMPANY_FACTS = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"end": "2024-09-28", "val": 391035000000}]}}}},
}

FILING_HTML = (
    "<html><head><title>10-K</title><style>body{color:red}</style></head>"
    "<body><script>alert('x')</script><h1>Apple Inc.</h1><p>Annual report &amp; results.</p></body></html>"
)


def make_client(**kwargs) -> tuple[EdgarClient, list[str]]:
    """An EdgarClient over a MockTransport; returns (client, requested URLs)."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        path = request.url.path
        if path == "/files/company_tickers.json":
            return httpx.Response(200, json=COMPANY_TICKERS)
        if path == "/submissions/CIK0000320193.json":
            return httpx.Response(200, json=SUBMISSIONS)
        if path == "/api/xbrl/companyfacts/CIK0000320193.json":
            return httpx.Response(200, json=COMPANY_FACTS)
        if path.startswith("/Archives/edgar/data/320193/"):
            return httpx.Response(200, text=FILING_HTML)
        return httpx.Response(404, text="not found")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault("min_request_interval", 0.0)
    return EdgarClient("Test Suite test@example.com", http=http, **kwargs), requested


# --- constructor -------------------------------------------------------- #


def test_user_agent_is_required() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        EdgarClient("")
    with pytest.raises(ValueError, match="fair-access"):
        EdgarClient("   ")


# --- resolve_company ---------------------------------------------------- #


def test_resolve_company_exact_ticker_before_title_substring() -> None:
    client, _ = make_client()
    results = client.resolve_company("aapl")
    # Exact ticker match first, then the title-substring match.
    assert [r["ticker"] for r in results] == ["AAPL", "ZZZ"]
    assert results[0] == {"cik": "0000320193", "ticker": "AAPL", "title": "Apple Inc."}


def test_resolve_company_title_substring_and_limit() -> None:
    client, _ = make_client()
    results = client.resolve_company("apple", limit=1)
    assert len(results) == 1
    assert results[0]["title"] == "Apple Inc."


def test_resolve_company_caches_tickers_file() -> None:
    client, requested = make_client()
    client.resolve_company("msft")
    client.resolve_company("apple")
    assert len([u for u in requested if "company_tickers" in u]) == 1


def test_resolve_company_rejects_empty_query() -> None:
    client, _ = make_client()
    with pytest.raises(ValueError, match="non-empty"):
        client.resolve_company("   ")


# --- list_filings -------------------------------------------------------- #


def test_list_filings_accepts_unpadded_cik() -> None:
    client, requested = make_client()
    filings = client.list_filings("320193")
    assert any("CIK0000320193.json" in url for url in requested)
    assert len(filings) == 3
    # Padded input works identically.
    assert client.list_filings("0000320193")[0] == filings[0]


def test_list_filings_form_filter_and_contract() -> None:
    client, _ = make_client()
    filings = client.list_filings("320193", form="10-K")
    assert [f["form"] for f in filings] == ["10-K", "10-K"]
    assert filings[0] == {
        "accession_no": "0000320193-24-000123",
        "form": "10-K",
        "filed_at": "2024-11-01",
        "report_date": "2024-09-28",
        "items": [],
        "primary_document": "aapl-20240928.htm",
        "url": "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
    }


def test_list_filings_empty_report_date_becomes_none() -> None:
    client, _ = make_client()
    by_form = {f["form"]: f for f in client.list_filings("320193")}
    assert by_form["8-K"]["report_date"] is None


def test_list_filings_parses_an_8ks_item_codes() -> None:
    """Item 2.02 ('Results of Operations and Financial Condition') is how a
    caller tells an earnings 8-K apart from one filed for something else
    entirely (an acquisition, an executive change) -- EDGAR's own submissions
    JSON carries this as a comma-separated string; every other form has none."""
    client, _ = make_client()
    by_form = {f["form"]: f for f in client.list_filings("320193")}
    assert by_form["8-K"]["items"] == ["2.02", "9.01"]
    assert by_form["10-K"]["items"] == []


def test_list_filings_limit() -> None:
    client, _ = make_client()
    assert len(client.list_filings("320193", limit=2)) == 2


def test_list_filings_rejects_bad_cik() -> None:
    client, _ = make_client()
    with pytest.raises(ValueError, match="invalid CIK"):
        client.list_filings("not-a-cik")


# --- get_filing ---------------------------------------------------------- #


def test_get_filing_strips_html_and_flags_untrusted() -> None:
    client, _ = make_client()
    filing = client.get_filing("320193", "0000320193-24-000123")
    assert filing["accession_no"] == "0000320193-24-000123"
    assert filing["form"] == "10-K"  # resolved via submissions
    assert filing["url"].endswith("/000032019324000123/aapl-20240928.htm")
    assert filing["content_is_untrusted"] is True
    # Tags, <script>, <style>, and <title> are gone; entities are decoded.
    assert "<" not in filing["content"]
    assert "alert" not in filing["content"]
    assert "color:red" not in filing["content"]
    assert "Apple Inc." in filing["content"]
    assert "Annual report & results." in filing["content"]


def test_get_filing_accepts_accession_without_dashes() -> None:
    client, _ = make_client()
    filing = client.get_filing("320193", "000032019324000123", primary_document="aapl-20240928.htm")
    assert filing["accession_no"] == "0000320193-24-000123"
    assert filing["form"] is None  # unknown — submissions lookup skipped


def test_get_filing_unknown_accession_raises() -> None:
    client, _ = make_client()
    with pytest.raises(ValueError, match="not found in recent filings"):
        client.get_filing("320193", "0000320193-99-999999")


# --- company_facts -------------------------------------------------------- #


def test_company_facts_returns_raw_json() -> None:
    client, _ = make_client()
    assert client.company_facts("320193") == COMPANY_FACTS


# --- caps, throttle, redirects ------------------------------------------- #


def test_size_cap_enforced() -> None:
    client, _ = make_client(max_response_bytes=64)
    with pytest.raises(RuntimeError, match="max_response_bytes"):
        client.get_filing("320193", "0000320193-24-000123", primary_document="aapl-20240928.htm")


def test_throttle_sleeps_only_inside_the_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make_client(min_request_interval=5.0)
    sleeps: list[float] = []
    monkeypatch.setattr(client, "_sleep", sleeps.append)
    client._throttle()  # first request — nothing to wait for
    assert sleeps == []
    client._throttle()  # immediate second request — must wait out the interval
    assert len(sleeps) == 1
    assert 0.0 < sleeps[0] <= 5.0


def test_throttle_disabled_at_zero_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = make_client(min_request_interval=0.0)
    monkeypatch.setattr(client, "_sleep", lambda _s: pytest.fail("should never sleep"))
    client._throttle()
    client._throttle()


def test_redirect_to_private_ip_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secrets"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = EdgarClient("Test test@example.com", http=http, min_request_interval=0.0)
    with pytest.raises(UrlBlocked):
        client.company_facts("320193")


def test_redirect_off_sec_hosts_is_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = EdgarClient("Test test@example.com", http=http, min_request_interval=0.0)
    with pytest.raises(UrlBlocked, match="allowed host"):
        client.company_facts("320193")


def test_same_host_redirect_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/xbrl/companyfacts/CIK0000320193.json":
            return httpx.Response(301, headers={"location": "/api/xbrl/companyfacts/CIK0000320193v2.json"})
        return httpx.Response(200, json=COMPANY_FACTS)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = EdgarClient("Test test@example.com", http=http, min_request_interval=0.0)
    assert client.company_facts("320193") == COMPANY_FACTS


# --- EdgarTools (LazyBridge ToolProvider) -------------------------------- #


def test_edgar_tools_is_a_lazy_tool_provider() -> None:
    client, _ = make_client()
    provider = EdgarTools(client=client)
    assert provider._is_lazy_tool_provider is True
    assert {t.name for t in provider.as_tools()} == {
        "sec_resolve_company", "sec_list_filings", "sec_get_filing_text"}


def test_edgar_tools_does_not_expose_company_facts() -> None:
    """CA-03's actual finding: financial FACTS must stay hub-only. This
    provider must never grow a facts tool, even as a convenience."""
    client, _ = make_client()
    provider = EdgarTools(client=client)
    names = {t.name for t in provider.as_tools()}
    assert not any("fact" in n for n in names)
    assert not hasattr(provider, "sec_company_facts")


def test_sec_resolve_company_matches_the_client() -> None:
    client, _ = make_client()
    provider = EdgarTools(client=client)
    out = provider.sec_resolve_company("aapl")
    assert out["matches"][0]["ticker"] == "AAPL"


def test_sec_list_filings_passes_the_form_filter() -> None:
    client, _ = make_client()
    provider = EdgarTools(client=client)
    out = provider.sec_list_filings("320193", form="10-K")
    assert out["cik"] == "320193"
    assert [f["form"] for f in out["filings"]] == ["10-K", "10-K"]


def test_sec_get_filing_text_stays_untrusted_and_carries_the_url() -> None:
    client, _ = make_client()
    provider = EdgarTools(client=client)
    out = provider.sec_get_filing_text("320193", "0000320193-24-000123")
    assert out["content_is_untrusted"] is True
    assert out["url"].endswith("aapl-20240928.htm")
    assert "Apple Inc." in out["content"]
    assert out["truncated"] is False


def test_sec_get_filing_text_carries_the_untrusted_note_beside_the_content() -> None:
    """A boolean flag is easy for a caller's own prompt to reinforce and just
    as easy to never read -- the warning should not depend solely on that."""
    client, _ = make_client()
    provider = EdgarTools(client=client)
    out = provider.sec_get_filing_text("320193", "0000320193-24-000123")
    assert "instruction" in out["note"].lower()


def test_sec_get_filing_text_truncates_long_filings_and_says_so() -> None:
    from lazytools.connectors.edgar.tools import MAX_FILING_CHARS

    long_html = "<html><body>" + ("Apple Inc. results. " * (MAX_FILING_CHARS // 10)) + "</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/submissions/CIK0000320193.json":
            return httpx.Response(200, json=SUBMISSIONS)
        return httpx.Response(200, text=long_html)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = EdgarClient("Test test@example.com", http=http, min_request_interval=0.0)
    provider = EdgarTools(client=client)
    out = provider.sec_get_filing_text("320193", "0000320193-24-000123")
    assert out["truncated"] is True
    assert len(out["content"]) == MAX_FILING_CHARS
