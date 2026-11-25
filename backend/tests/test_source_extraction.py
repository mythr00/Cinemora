"""GitHub Skills Programme (2026-11-25): source full-text
extraction (trafilatura / pypdf / pdfplumber) and its wiring into
the bible research pass.

What is pinned:
- the SSRF guard refuses non-public hosts, bad schemes and
  credentialled URLs (resolver stubbed — no real DNS);
- HTML extraction returns the article's main text, not the
  page furniture; PDF extraction reads a real PDF's text;
- extract_from_url never raises and records typed outcomes;
- in enrich_bible_with_research, a successful extraction WIDENS
  the evidence the support labelling reads (a fact unsupported by
  the snippet becomes supported by the extracted text), a failed
  one changes nothing, and with the pass disabled the bible is
  byte-for-byte the pre-programme behaviour;
- near-duplicate source titles are stamped with their RapidFuzz
  score as evidence for review — never merged or dropped.
"""

import socket

import pytest

from services import source_extraction
from services import story_understanding as story


# ---------------------------------------------------------------------------
# SSRF guard (resolver stubbed)
# ---------------------------------------------------------------------------

def _stub_resolver(monkeypatch, mapping):
    def fake_getaddrinfo(host, port, proto=0, *args, **kwargs):
        ip = mapping[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_guard_refuses_literal_private_and_loopback():
    assert source_extraction.guard_url("http://127.0.0.1/x") == "host_not_public"
    assert source_extraction.guard_url("http://192.168.1.5/x") == "host_not_public"
    assert source_extraction.guard_url("http://[::1]/x") == "host_not_public"
    assert source_extraction.guard_url("https://8.8.8.8/x") is None


def test_guard_refuses_schemes_and_credentials():
    assert source_extraction.guard_url("ftp://example.org/x") == "scheme_not_allowed"
    assert source_extraction.guard_url("file:///etc/passwd") == "scheme_not_allowed"
    assert source_extraction.guard_url(
        "https://user:secret@example.org/x") == "url_credentials_refused"
    assert source_extraction.guard_url("") == "scheme_not_allowed"


def test_guard_checks_resolved_addresses(monkeypatch):
    _stub_resolver(monkeypatch, {
        "good.example": "93.184.216.34",
        "internal.example": "10.0.0.7",
    })
    assert source_extraction.guard_url("https://good.example/a") is None
    assert source_extraction.guard_url(
        "https://internal.example/a") == "host_not_public"


def test_guard_dns_failure_is_refusal(monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert source_extraction.guard_url("https://missing.example/") == "dns_failed"


# ---------------------------------------------------------------------------
# Extractors over fixtures
# ---------------------------------------------------------------------------

_ARTICLE_HTML = """<!DOCTYPE html>
<html><head><title>The Night the Wall Fell - City Archive</title></head>
<body>
<nav>Home | Sections | Sport | Weather | Contact the newsroom</nav>
<aside>Subscribe now for unlimited access to our complete archive.</aside>
<article>
<h1>The Night the Wall Fell</h1>
<p>On 9 November 1989 the Berlin Wall fell and crowds crossed freely
into the west for the first time in a generation. Border guards,
uncertain of their orders, opened the checkpoints at Bornholmer
Strasse shortly before midnight.</p>
<p>Historians estimate that tens of thousands of East Berliners
crossed that night. The crossings continued through the weekend,
and within days citizens began dismantling sections of the wall
with hammers and chisels, keeping fragments as souvenirs of the
night the division of the city effectively ended.</p>
<p>The wall had stood since August 1961, when the East German
government sealed the border around West Berlin overnight.</p>
</article>
<footer>Copyright the City Archive. All rights reserved.</footer>
</body></html>"""


def test_extract_html_returns_main_text():
    result = source_extraction.extract_html(_ARTICLE_HTML, url="https://x.example/a")
    assert result["status"] == "extracted"
    assert result["extractor"] == "trafilatura"
    assert "Bornholmer" in result["text"]
    assert "Subscribe now" not in result["text"]
    assert result["char_count"] >= len(result["text"])
    assert result["title"] and "Wall" in result["title"]


def test_extract_html_without_article_fails_recorded():
    result = source_extraction.extract_html("<html><body></body></html>")
    assert result["status"] == "failed"
    assert result["reason"] == "no_main_text_extracted"


def _make_pdf(lines):
    content = "BT /F1 14 Tf 56 760 Td "
    for i, line in enumerate(lines):
        if i:
            content += "0 -22 Td "
        content += f"({line}) Tj "
    content += "ET"
    stream = content.encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for offset in offsets:
        pdf += f"{offset:010d} 00000 n \n".encode()
    pdf += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return pdf


def test_extract_pdf_reads_text():
    pdf = _make_pdf([
        "The harbour was built in 1903 and doubled the port capacity",
        "of the city within five years of opening. Contemporary",
        "records describe the opening ceremony in unusual detail,",
        "noting the crowds, the shipping, and the speeches given.",
    ])
    result = source_extraction.extract_pdf(pdf)
    assert result["status"] == "extracted"
    assert result["extractor"] in ("pypdf", "pdfplumber")
    assert "1903" in result["text"]


def test_extract_pdf_garbage_fails_recorded():
    result = source_extraction.extract_pdf(b"%PDF- not really a pdf")
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# Entry point: typed outcomes, never raises
# ---------------------------------------------------------------------------

def test_extract_from_url_html_via_stub_fetch():
    def fetch(url):
        return 200, "text/html; charset=utf-8", _ARTICLE_HTML.encode()

    result = source_extraction.extract_from_url(
        "https://good.example/a", _fetch_fn=fetch)
    assert result["status"] == "extracted"
    assert "Bornholmer" in result["text"]


def test_extract_from_url_http_error_and_refusal():
    def fetch_404(url):
        return 404, "text/html", b"nope"

    result = source_extraction.extract_from_url(
        "https://good.example/missing", _fetch_fn=fetch_404)
    assert result["status"] == "failed"
    assert result["reason"] == "http_404"

    def fetch_refused(url):
        raise source_extraction._Refused("host_not_public")

    result = source_extraction.extract_from_url(
        "http://internal.example/", _fetch_fn=fetch_refused)
    assert result["status"] == "refused"
    assert result["reason"] == "host_not_public"

    def fetch_boom(url):
        raise ConnectionError("down")

    result = source_extraction.extract_from_url(
        "https://good.example/", _fetch_fn=fetch_boom)
    assert result["status"] == "failed"
    assert result["reason"].startswith("fetch_error:")


def test_extraction_enabled_is_opt_in(monkeypatch):
    monkeypatch.delenv("CINEMORA_SOURCE_EXTRACTION_ENABLED", raising=False)
    assert source_extraction.extraction_enabled() is False
    monkeypatch.setenv("CINEMORA_SOURCE_EXTRACTION_ENABLED", "1")
    assert source_extraction.extraction_enabled() is True


# ---------------------------------------------------------------------------
# Wiring into enrich_bible_with_research
# ---------------------------------------------------------------------------

def _bible():
    entities = [{
        "id": "berlin_wall",
        "canonical": "Berlin Wall",
        "type": "object",
        "role": "main subject",
        "aliases": ["the Wall"],
        "pronoun": "it",
        "description": "The Berlin Wall was erected in 1961.",
        "sources": [],
    }]
    return {
        "entities": entities,
        "by_id": {e["id"]: e for e in entities},
        "timeline": [
            {"when": "1961", "what": "The Berlin Wall was erected in 1961"},
        ],
        "primary_subject_id": "berlin_wall",
    }


_SCRIPT = "The Berlin Wall was erected in 1961."
_SENTENCES = [_SCRIPT]

# A snippet that does NOT support the fact (no year, no token
# majority) — support can only come from the extracted text.
_THIN_SOURCE = {
    "title": "A barrier divided the city",
    "url": "https://example.org/wall-overview",
    "snippet": "A barrier divided the city for decades afterwards.",
    "source": "google_serper",
}


def _fake_search(query):
    return [dict(_THIN_SOURCE)]


def test_extraction_widens_evidence_and_is_recorded(monkeypatch):
    monkeypatch.setattr(story, "search_web", _fake_search)

    def extractor(url):
        return {
            "status": "extracted", "extractor": "trafilatura",
            "text": ("Archival record: construction of the Berlin Wall "
                     "began in 1961, dividing families overnight."),
            "title": "Wall archive", "date": None,
            "char_count": 96, "reason": "",
        }

    monkeypatch.setattr(story, "extract_source_text", extractor)
    bible = story.enrich_bible_with_research(_bible(), _SCRIPT, _SENTENCES)

    entity = bible["by_id"]["berlin_wall"]
    source = entity["sources"][0]
    assert source["extraction"]["status"] == "extracted"
    assert source["extraction"]["extractor"] == "trafilatura"
    assert "1961" in source["extracted_text"]
    # The widened basis is what the labelling read:
    assert entity["support"] == "externally_supported"
    assert bible["timeline"][0]["support"] == "externally_supported"
    meta = bible["research_meta"]
    assert meta["extraction"]["enabled"] is True
    assert meta["extraction"]["extracted"] == 1


def test_failed_extraction_leaves_snippet_basis_unchanged(monkeypatch):
    monkeypatch.setattr(story, "search_web", _fake_search)
    monkeypatch.setattr(
        story, "extract_source_text",
        lambda url: {"status": "failed", "extractor": "trafilatura",
                     "text": "", "title": None, "date": None,
                     "char_count": 0, "reason": "http_404"})
    bible = story.enrich_bible_with_research(_bible(), _SCRIPT, _SENTENCES)
    entity = bible["by_id"]["berlin_wall"]
    assert entity["sources"][0]["extraction"]["status"] == "failed"
    assert "extracted_text" not in entity["sources"][0]
    assert entity["support"] == "script_asserted"
    assert bible["research_meta"]["extraction"]["failed"] == 1


def test_extraction_disabled_by_default_records_disabled(monkeypatch):
    monkeypatch.setattr(story, "search_web", _fake_search)
    monkeypatch.delenv("CINEMORA_SOURCE_EXTRACTION_ENABLED", raising=False)
    bible = story.enrich_bible_with_research(_bible(), _SCRIPT, _SENTENCES)
    entity = bible["by_id"]["berlin_wall"]
    assert "extraction" not in entity["sources"][0]
    assert bible["research_meta"]["extraction"]["enabled"] is False
    assert entity["support"] == "script_asserted"


def test_duplicate_titles_stamped_not_merged(monkeypatch):
    def search(query):
        return [
            {"title": "Berlin Wall erected in 1961",
             "url": "https://a.example/wall",
             "snippet": "The Berlin Wall was erected in 1961.",
             "source": "google_serper"},
            {"title": "Berlin Wall erected in 1961",
             "url": "https://b.example/wall-mirror",
             "snippet": "The Berlin Wall was erected in 1961.",
             "source": "google_serper"},
        ]

    monkeypatch.setattr(story, "search_web", search)
    bible = story.enrich_bible_with_research(_bible(), _SCRIPT, _SENTENCES)
    sources = bible["by_id"]["berlin_wall"]["sources"]
    assert len(sources) == 2  # nothing merged or dropped
    assert "title_match" not in sources[0]
    assert sources[1]["title_match"]["of_url"] == "https://a.example/wall"
    assert sources[1]["title_match"]["score"] >= 92.0
    assert bible["research_meta"]["title_duplicates_found"] == 1
