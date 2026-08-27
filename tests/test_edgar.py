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
            # Present in the real submissions JSON (verified live against
            # CIK0000320193), and the only field that answers "had this been
            # filed by the time the report claims to cover".
            "acceptanceDateTime": ["2024-11-01T18:01:14.000Z",
                                   "2024-08-02T16:30:00.000Z",
                                   "2023-11-03T18:08:05.000Z"],
            "primaryDocDescription": ["10-K", "8-K", "10-K"],
        }
    },
}

#: Transcribed from a real submission header (Apple's 2026 earnings 8-K):
#: the SGML tags arrive HTML-escaped inside an HTML page, which is why a
#: naive <DOCUMENT> parse of the raw bytes finds nothing at all.
INDEX_HEADERS = """<html><body><pre>
&lt;SEC-HEADER&gt;0000320193-24-000123.hdr.sgml
ACCESSION NUMBER: 0000320193-24-000123
&lt;DOCUMENT&gt;
&lt;TYPE&gt;8-K
&lt;SEQUENCE&gt;1
&lt;FILENAME&gt;aapl-8k.htm
&lt;DESCRIPTION&gt;8-K
&lt;/DOCUMENT&gt;
&lt;DOCUMENT&gt;
&lt;TYPE&gt;EX-99.1
&lt;SEQUENCE&gt;2
&lt;FILENAME&gt;a8-kex991.htm
&lt;DESCRIPTION&gt;EX-99.1
&lt;/DOCUMENT&gt;
&lt;DOCUMENT&gt;
&lt;TYPE&gt;GRAPHIC
&lt;SEQUENCE&gt;3
&lt;FILENAME&gt;logo.jpg
&lt;DESCRIPTION&gt;
&lt;/DOCUMENT&gt;
</pre></body></html>"""

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
        if path.endswith("-index-headers.html"):
            return httpx.Response(200, text=INDEX_HEADERS)
        if path.endswith("logo.jpg"):
            # A real JPEG's magic bytes, built from ints: a literal here
            # picks up the file's own encoding and stops being a JPEG.
            return httpx.Response(200, content=bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"jpegbody")
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
        # Added with the document-inventory work: the acceptance INSTANT,
        # which a date cannot answer "was this filed by 22:30?" with.
        "accepted_at": "2024-11-01T18:01:14.000Z",
        "primary_doc_description": "10-K",
    }


def test_list_filings_empty_report_date_becomes_none() -> None:
    client, _ = make_client()
    by_form = {f["form"]: f for f in client.list_filings("320193")}
    assert by_form["8-K"]["report_date"] is None


def test_fake_edgar_client_matches_the_real_filing_contract() -> None:
    """A real Codex-review finding: a consumer testing against the packaged
    FakeEdgarClient (lazytools.testing) instead of mocking EdgarClient
    directly must see the SAME keys the real client now returns -- code that
    reads filing["items"] should not KeyError only under the fake."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    for filing in fake.list_filings("320193"):
        assert "items" in filing
    by_form = {f["form"]: f for f in fake.list_filings("320193")}
    assert by_form["8-K"]["items"] == ["2.02", "9.01"]
    assert by_form["10-K"]["items"] == []


def test_fake_edgar_client_returns_each_filings_own_text() -> None:
    """A real Codex-review finding: the fake's get_filing() returned one
    shared string regardless of accession, so fetching the 8-K fixture came
    back saying 'Form 10-K'."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    dieci_k = fake.get_filing("320193", "0000320193-24-000123")
    otto_k = fake.get_filing("320193", "0000320193-24-000100")
    assert "10-K" in dieci_k["content"]
    assert "8-K" in otto_k["content"]
    assert dieci_k["content"] != otto_k["content"]


def test_fake_edgar_client_normalizes_accession_numbers() -> None:
    """A real Codex-review finding: the documented dashless accession form
    (that the real client accepts) raised ValueError against the fake."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    assert fake.get_filing("320193", "0000320193-24-000123") == \
        fake.get_filing("320193", "000032019324000123")


def test_fake_edgar_client_honors_an_explicit_primary_document() -> None:
    """A real Codex-review finding: a caller-supplied primary_document was
    silently ignored when building the returned url."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    filing = fake.get_filing("320193", "0000320193-24-000123",
                             primary_document="alternate.htm")
    assert filing["url"].endswith("/alternate.htm")
    assert filing["form"] is None


def test_fake_edgar_client_skips_the_filings_list_when_given_a_primary_document() -> None:
    """A real Codex-review finding: the real client never looks an accession
    up against list_filings when primary_document is supplied directly --
    the fake requiring a list match rejected calls the real client accepts."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    filing = fake.get_filing("320193", "0000320193-24-999999",
                             primary_document="unlisted.htm")
    assert filing["url"].endswith("/unlisted.htm")


def test_fake_edgar_client_returns_the_normalized_accession() -> None:
    """A real Codex-review finding: the real client always returns the
    normalized (dashed) accession, whatever form the caller passed in."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    filing = fake.get_filing("320193", "000032019324000123")
    assert filing["accession_no"] == "0000320193-24-000123"


def test_fake_edgar_client_does_not_return_apple_for_a_different_cik() -> None:
    """A real Codex-review finding: filings/facts used to be one flat blob
    served regardless of which CIK was asked for -- a caller's own bug (the
    wrong CIK, a copy-paste from a different company) silently returned
    Apple's data anyway, exactly the kind of mistake a fake exists to catch
    rather than hide. A validly-shaped but unregistered CIK (Microsoft's
    real one, not seeded here) must come back empty, not as Apple."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    assert fake.list_filings("789019") == []
    assert fake.company_facts("789019") == {}
    with pytest.raises(ValueError, match="not found"):
        fake.get_filing("789019", "0000320193-24-000123")


def test_fake_edgar_client_rejects_a_bad_cik() -> None:
    """A real Codex-review finding: none of the fake's CIK-taking methods
    validated their input, so a caller's own bug (a ticker passed where a
    CIK belongs) silently returned Apple data instead of failing the way the
    real client does."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    with pytest.raises(ValueError, match="invalid CIK"):
        fake.list_filings("not-a-cik")
    with pytest.raises(ValueError, match="invalid CIK"):
        fake.company_facts("not-a-cik")
    with pytest.raises(ValueError, match="invalid CIK"):
        fake.get_filing("not-a-cik", "0000320193-24-000123")


def test_fake_edgar_client_rejects_an_empty_query() -> None:
    """A real Codex-review finding: the real client raises on a blank query;
    the fake silently returned Apple regardless."""
    from lazytools.testing import FakeEdgarClient

    fake = FakeEdgarClient()
    with pytest.raises(ValueError, match="non-empty"):
        fake.resolve_company("   ")


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
        "sec_list_filing_documents",
        "sec_get_filing_document",
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


def test_list_filing_documents_reads_exhibit_types_from_the_sgml_header() -> None:
    """index.json cannot do this job.

    Measured live against Apple's 2026 earnings 8-K: index.json's ``type``
    field is the directory ICON name ("text.gif", "image2.gif") and there is
    no description at all, so an exhibit cannot be identified from it. The
    submission header carries the real TYPE/SEQUENCE/FILENAME/DESCRIPTION.
    """
    client, requested = make_client()
    docs = client.list_filing_documents("320193", "0000320193-24-000123")
    assert [d["type"] for d in docs] == ["8-K", "EX-99.1", "GRAPHIC"]
    assert [d["sequence"] for d in docs] == ["1", "2", "3"]
    assert any("-index-headers.html" in u for u in requested)
    exhibit = docs[1]
    assert exhibit["filename"] == "a8-kex991.htm"
    assert exhibit["media_type"] == "text/html"
    assert exhibit["url"].startswith("https://www.sec.gov/Archives/edgar/data/320193/")
    # An empty DESCRIPTION is None, not "" -- absent is not the same as blank.
    assert docs[2]["description"] is None


def test_the_earnings_release_is_reachable_where_the_primary_document_is_not() -> None:
    """The whole reason this exists: an earnings 8-K's primary document is
    the cover and the Item 2.02 statement; the revenue is in the exhibit."""
    client, _ = make_client()
    got = client.get_filing_document("320193", "0000320193-24-000123", "a8-kex991.htm")
    assert got["type"] == "EX-99.1"
    assert got["extraction_status"] == "ok"
    assert got["content_is_untrusted"] is True
    assert got["content"]


def test_a_filename_the_filing_does_not_contain_is_refused() -> None:
    """The caller names a document; it does not get to name a URL."""
    import pytest

    client, requested = make_client()
    before = len(requested)
    with pytest.raises(ValueError, match="not a document of filing"):
        client.get_filing_document("320193", "0000320193-24-000123",
                                   "../../../../etc/passwd")
    # It was rejected against the inventory, never fetched.
    assert not any("passwd" in u for u in requested[before:])


def test_a_binary_document_is_reported_unsupported_not_mangled() -> None:
    """Decoding a JPEG as UTF-8 yields a page of replacement characters that
    reads like a broken document rather than an unreadable one.

    And it is not downloaded at all: the inventory already said it was an
    image, so fetching it to throw it away would spend a request against the
    SEC's rate limit and the caller's deadline to learn what we knew. Its
    size is therefore unknown rather than invented -- review asked for the
    fetch to be skipped, and a fabricated size would be the same kind of
    claim-without-evidence this whole change is about.
    """
    client, requested = make_client()
    got = client.get_filing_document("320193", "0000320193-24-000123", "logo.jpg")
    assert got["extraction_status"] == "unsupported"
    assert got["media_type"] == "image/jpeg"
    assert got["content"] == ""
    assert got["size_bytes"] is None
    assert not any("logo.jpg" in url for url in requested)


def test_the_document_tools_cap_their_text_and_say_when_they_did() -> None:
    from lazytools.connectors.edgar.tools import MAX_FILING_CHARS, EdgarTools

    class _Lungo:
        def list_filing_documents(self, cik, accession_no):
            return [{"filename": "big.htm", "type": "EX-99.1", "description": None,
                     "sequence": "2", "media_type": "text/html", "url": "https://www.sec.gov/x"}]

        def get_filing_document(self, cik, accession_no, filename):
            return {"accession_no": accession_no, "filename": filename,
                    "type": "EX-99.1", "description": None,
                    "url": "https://www.sec.gov/x", "media_type": "text/html",
                    "content": "a" * (MAX_FILING_CHARS + 500),
                    "extraction_status": "ok", "size_bytes": 999,
                    "content_is_untrusted": True}

    tools = EdgarTools(client=_Lungo())
    out = tools.sec_get_filing_document("320193", "0000320193-24-000123", "big.htm")
    assert out["truncated"] is True
    assert len(out["content"]) == MAX_FILING_CHARS
    assert out["content_is_untrusted"] is True
    # No raw bytes anywhere in a model-facing result.
    assert all(not isinstance(v, (bytes, bytearray)) for v in out.values())


def test_an_unrecognised_format_is_unsupported_rather_than_decoded() -> None:
    """A denylist of known binaries lets the next format through.

    Review caught a .xlsx resolving to application/octet-stream, which was in
    no list of binaries, so it was decoded as UTF-8 and returned as
    successfully extracted text made entirely of replacement characters. The
    check is an allowlist now, so a format nobody anticipated is reported
    unreadable rather than mangled.
    """
    from lazytools.connectors.edgar.client import _media_type

    assert _media_type("results.xlsx") == "application/octet-stream"

    class _Foglio:
        def list_filing_documents(self, cik, accession_no):
            return [{"filename": "results.xlsx", "type": "EX-99.2",
                     "description": None, "sequence": "3",
                     "media_type": "application/octet-stream",
                     "url": "https://www.sec.gov/x/results.xlsx"}]

    client, _ = make_client()
    client.list_filing_documents = _Foglio().list_filing_documents  # type: ignore[method-assign]
    client._get = lambda url: bytes([0x50, 0x4B, 0x03, 0x04]) + b"zipbody"  # type: ignore[method-assign]

    got = client.get_filing_document("320193", "0000320193-24-000123", "results.xlsx")
    assert got["extraction_status"] == "unsupported"
    assert got["content"] == ""


def test_the_packaged_fake_satisfies_the_expanded_service() -> None:
    """The fake is shipped for consumers to inject.

    Review caught it left behind by this change: EdgarTools' new document
    tools raised AttributeError against it, and its filings omitted the
    fields the real client had started returning -- so code written against
    the fake passed and the same code failed in production, which is the one
    thing a test double must never do.
    """
    from lazytools.connectors.edgar.tools import EdgarTools
    from lazytools.testing.fake_clients import FakeEdgarClient

    tools = EdgarTools(client=FakeEdgarClient())

    filing = tools.sec_list_filings("0000320193", form="8-K")["filings"][0]
    assert filing["accepted_at"] == "2024-08-02T16:30:00.000Z"
    assert filing["primary_doc_description"] == "8-K"

    docs = tools.sec_list_filing_documents("0000320193", "0000320193-24-000100")["documents"]
    assert [d["type"] for d in docs] == ["8-K", "EX-99.1", "GRAPHIC"]

    exhibit = tools.sec_get_filing_document(
        "0000320193", "0000320193-24-000100", "aapl-ex991.htm")
    assert exhibit["extraction_status"] == "ok"
    assert "Revenue" in exhibit["content"]

    # And it refuses what the real client refuses, rather than serving it.
    import pytest

    with pytest.raises(ValueError, match="not a document of filing"):
        tools.sec_get_filing_document("0000320193", "0000320193-24-000100", "elsewhere.htm")

    # Unreadable media behaves the same way here as in production.
    logo = tools.sec_get_filing_document("0000320193", "0000320193-24-000100", "logo.jpg")
    assert logo["extraction_status"] == "unsupported"


def test_an_unparseable_header_fails_instead_of_reporting_an_empty_filing() -> None:
    """A submission always contains at least its primary document.

    So an empty inventory is this parse failing, and returning [] would tell
    the caller the opposite -- that the filing has no documents. Review
    surfaced a 1994 accession whose header 404s (which raises on its own);
    this covers a 200 whose body we cannot read.
    """
    import httpx
    import pytest

    from lazytools.connectors.edgar.client import EdgarClient

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("-index-headers.html"):
            return httpx.Response(200, text="<html><body>nothing here</body></html>")
        return httpx.Response(404)

    client = EdgarClient("Test Suite test@example.com",
                         http=httpx.Client(transport=httpx.MockTransport(handler)),
                         min_request_interval=0.0)
    with pytest.raises(RuntimeError, match="no documents parsed"):
        client.list_filing_documents("320193", "0000320193-24-000123")


def test_the_fake_cannot_describe_a_filing_as_documentless() -> None:
    """The real client raises rather than return an empty inventory, because
    no submission has zero documents. Review caught the fake still returning
    [] -- so a consumer could write a branch for a shape production never
    produces, and pass here."""
    import pytest

    from lazytools.testing.fake_clients import FakeEdgarClient

    fake = FakeEdgarClient()
    # Every canned filing has an inventory, including the 10-K.
    assert fake.list_filing_documents("0000320193", "0000320193-24-000123")
    assert fake.list_filing_documents("0000320193", "0000320193-24-000100")

    with pytest.raises(RuntimeError, match="no documents parsed"):
        fake.list_filing_documents("0000320193", "9999999999-99-999999")
