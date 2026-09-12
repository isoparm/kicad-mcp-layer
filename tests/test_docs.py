"""The documentation library: fetch, classify refusals, index, read text, render pages."""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

from kicad_layer import docs
from kicad_layer.config import load_settings, set_settings
from kicad_layer.errors import LayerError


def _pdf_bytes(text: str = "Hello datasheet") -> bytes:
    """A one-page PDF with real text, made with pypdf so the test does not depend on a fixture file."""
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    page = w.add_blank_page(width=300, height=200)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    font_ref = w._add_object(font)
    resources = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    page[NameObject("/Resources")] = resources
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = w._add_object(content)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class _Handler(http.server.BaseHTTPRequestHandler):
    pdf = _pdf_bytes()

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/ds.pdf"):
            self._send(200, "application/pdf", self.pdf)
        elif self.path.startswith("/attach.pdf"):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="portal.pdf"')
            self.send_header("Content-Length", str(len(self.pdf)))
            self.end_headers()
            self.wfile.write(self.pdf)
        elif self.path.startswith("/portal"):
            # a scripted download portal: the link exists only after JavaScript runs, and there are decoys
            # the file name is assembled by the script, so no static scan of the HTML can see a .pdf link
            self._send(200, "text/html", b"<html><body><a href='/terms.html'>Terms</a><a href='/other.html'>Other datasheets</a><div id='dl'></div>"
                                        b"<script>setTimeout(()=>{const a=document.createElement('a');a.href='/att'+'ach.'+'pd'+'f';a.textContent='Download datasheet';"
                                        b"document.getElementById('dl').appendChild(a);},200)</script></body></html>")
        elif self.path.startswith("/viewer"):
            self._send(200, "text/html", b"<html><body><a href='/ds.pdf?dl=1'>Download</a></body></html>")
        elif self.path.startswith("/wall"):
            self._send(200, "text/html", b"<html><body>Please verify you are human</body></html>")
        elif self.path.startswith("/forbidden"):
            self._send(403, "text/html", b"<html>no</html>")
        elif self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/ds.pdf")
            self.end_headers()
        else:
            self._send(404, "text/plain", b"nope")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def library(tmp_path):
    set_settings(load_settings({"KICAD_LAYER_WORKSPACE": str(tmp_path), "KICAD_LAYER_CACHE_DIR": str(tmp_path / "cache"), "KICAD_LAYER_DOCS_DIR": str(tmp_path / "refs")}))
    try:
        yield tmp_path / "refs"
    finally:
        set_settings(None)


def test_fetch_indexes_with_provenance(server, library):
    e = docs.fetch(f"{server}/ds.pdf", title="Test datasheet", tags=["test", "poe"])
    assert (library / "datasheets" / "ds.pdf").is_file()
    assert e.content_type == "application/pdf" and e.pages == 1 and e.source_url.endswith("/ds.pdf")
    assert e.fetched and len(e.sha256) == 64 and e.id == e.sha256[:12]
    idx = docs.load_index()
    assert [x.id for x in idx] == [e.id]
    # fetching the same file again replaces the entry rather than duplicating it
    docs.fetch(f"{server}/ds.pdf", title="Test datasheet again")
    assert len(docs.load_index()) == 1


def test_fetch_follows_redirects_and_viewer_pages(server, library):
    e = docs.fetch(f"{server}/redirect", filename="via-redirect.pdf")
    assert e.content_type == "application/pdf"
    e2 = docs.fetch(f"{server}/viewer", filename="via-viewer.pdf")
    assert e2.content_type == "application/pdf" and e2.source_url.endswith("/viewer")


def test_fetch_refusals_are_classified(server, library):
    """The plain tier's classification, with the browser tier switched off."""
    with pytest.raises(LayerError) as ei:
        docs.fetch(f"{server}/wall", browser="never")
    assert ei.value.code == docs.DOC_NOT_PDF and "browser" in (ei.value.hint or "")
    with pytest.raises(LayerError) as ei:
        docs.fetch(f"{server}/forbidden", browser="never")
    assert ei.value.code == docs.DOC_FETCH_FAILED and "403" in str(ei.value)
    with pytest.raises(LayerError) as ei:
        docs.fetch(f"{server}/missing")  # a 404 never goes to the browser
    assert "404" in str(ei.value)
    assert docs.load_index() == []
    # an HTML page can be kept deliberately
    e = docs.fetch(f"{server}/wall", expect="any", filename="wall.html")
    assert e.content_type == "text/html"


@pytest.mark.skipif(not docs.browser_available(), reason="Playwright is not installed")
@pytest.mark.slow
def test_browser_tier_takes_the_scripted_download(server, library):
    """A portal page whose PDF link appears only after scripts run: the plain fetch sees no link and
    the browser tier takes the download; with browser='never' the plain failure stands."""
    with pytest.raises(LayerError) as ei:
        docs.fetch(f"{server}/portal", browser="never")
    assert ei.value.code == docs.DOC_NOT_PDF
    e = docs.fetch(f"{server}/portal", filename="from-portal.pdf")
    assert e.content_type == "application/pdf" and e.pages == 1
    assert "headless Chromium" in e.notes and e.source_url.endswith("/portal")
    assert (library / "datasheets" / "from-portal.pdf").read_bytes()[:5] == b"%PDF-"


def test_import_text_search_and_render(library, tmp_path):
    src = tmp_path / "saved-from-browser.pdf"
    src.write_bytes(_pdf_bytes("Ag5405 output 5.1 A"))
    e = docs.import_file(src, source_url="https://example.com/ag5405.pdf", tags=["silvertel"])
    assert (library / "datasheets" / "saved-from-browser.pdf").is_file() and src.is_file()
    entry, path = docs.resolve(e.id)
    assert entry is not None and path.is_file()
    pages = docs.extract_text(path)
    assert len(pages) == 1 and "5.1 A" in pages[0]
    hits = docs.find_in_text(pages, r"5\.1\s*A")
    assert hits and hits[0]["page"] == 1
    found = docs.search("ag5405")
    assert [x[0].id for x in found] == [e.id] and found[0][1][0]["page"] == 1
    png = docs.render_page(path, 1, scale=1.0)
    assert png.is_file() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with pytest.raises(LayerError):
        docs.render_page(path, 2)
    assert docs.parse_pages("1-2,5", 3) == [1, 2] and docs.parse_pages(None, 2) == [1, 2]


def test_find_merges_overlapping_windows_and_counts_hits():
    dense = "alpha " * 30 + "TRIP point B0 0.16 V; trip point B1 0.77 V; trip point B2 1.4 V " + "omega " * 30
    sparse = "trip point first" + " x" * 400 + "trip point second"
    hits = docs.find_in_text([dense, "nothing here", sparse], "trip point")
    assert [h["page"] for h in hits] == [1, 3, 3]
    assert hits[0]["hits"] == 3 and "B2 1.4 V" in hits[0]["text"]
    assert hits[1]["hits"] == 1 and hits[2]["hits"] == 1 and "second" in hits[2]["text"]
