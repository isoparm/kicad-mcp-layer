"""The documentation layer: fetch datasheets and reference documents into an indexed library,
read their text, and render pages to images so drawings and tables can be looked at.

Every document keeps its provenance (source URL, fetch date, size, SHA-256) in
``<docs_dir>/index.json``. Fetching uses browser-grade request headers because many
manufacturer and distributor sites refuse plain scripted clients; when a site still refuses,
the failure is classified so the caller can fall back to a real browser and then ``import_file``
the result. Nothing here modifies design files.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from kicad_layer.config import settings
from kicad_layer.errors import DOC_FETCH_FAILED, DOC_NOT_PDF, INVALID_ARGUMENT, NOT_FOUND_IN_DESIGN, LayerError
from kicad_layer.paths import resolve_in_workspace

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}
INDEX_NAME = "index.json"
TEXT_DIR = ".text"
PAGES_DIR = ".pages"


@dataclass
class DocEntry:
    id: str
    file: str  # relative to docs_dir, forward slashes
    title: str
    source_url: str | None
    fetched: str
    size: int
    sha256: str
    content_type: str
    pages: int | None = None
    tags: list[str] = field(default_factory=list)
    notes: str = ""


# --------------------------------------------------------------------------------------
# library location and index
# --------------------------------------------------------------------------------------


def docs_dir() -> Path:
    """Where documents live: ``KICAD_LAYER_DOCS_DIR`` or ``<workspace>/research/references``."""
    s = settings()
    override = getattr(s, "docs_dir", None)
    d = Path(override) if override else s.workspace_root / "research" / "references"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path() -> Path:
    return docs_dir() / INDEX_NAME


def load_index() -> list[DocEntry]:
    p = _index_path()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return []
    out = []
    for r in raw.get("documents", []):
        try:
            out.append(DocEntry(**{k: r.get(k) for k in DocEntry.__dataclass_fields__ if k in r}))
        except TypeError:
            continue
    return out


def save_index(entries: list[DocEntry]) -> None:
    p = _index_path()
    p.write_text(json.dumps({"version": 1, "documents": [asdict(e) for e in entries]}, indent=2), encoding="utf-8", newline='\n')


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _doc_id(sha: str) -> str:
    return sha[:12]


def _sniff(data: bytes) -> str:
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if data[:4] == b"PK\x03\x04":
        return "application/zip"
    head = data[:2048].lower()
    if b"<html" in head or b"<!doctype html" in head:
        return "text/html"
    return "application/octet-stream"


def _safe_name(name: str) -> str:
    name = urllib.parse.unquote(name)
    name = re.sub(r"[^A-Za-z0-9._ ()+-]+", "-", name).strip(" .-")
    return name or "document"


def _register(path: Path, *, title: str, source_url: str | None, content_type: str, tags: list[str], notes: str) -> DocEntry:
    base = docs_dir()
    sha = _sha256(path)
    entries = [e for e in load_index() if e.file != path.relative_to(base).as_posix()]
    pages = None
    if content_type == "application/pdf":
        try:
            pages = page_count(path)
        except Exception:  # noqa: BLE001 - a broken PDF is still worth keeping
            pages = None
    entry = DocEntry(id=_doc_id(sha), file=path.relative_to(base).as_posix(), title=title, source_url=source_url, fetched=time.strftime("%Y-%m-%d"),
                     size=path.stat().st_size, sha256=sha, content_type=content_type, pages=pages, tags=sorted(set(tags)), notes=notes)
    entries.append(entry)
    entries.sort(key=lambda e: e.file.lower())
    save_index(entries)
    return entry


# --------------------------------------------------------------------------------------
# fetching and importing
# --------------------------------------------------------------------------------------


def _open(url: str, *, referer: str | None, timeout_s: float):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPRedirectHandler())
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    return opener.open(req, timeout=timeout_s)


def _find_pdf_link(html: bytes, base_url: str) -> str | None:
    text = html.decode("utf-8", "replace")
    m = re.search(r'http-equiv=["\']refresh["\'][^>]*url=([^"\'>\s]+)', text, flags=re.IGNORECASE)
    if m:
        return urllib.parse.urljoin(base_url, m.group(1))
    links = re.findall(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', text, flags=re.IGNORECASE)
    if len(links) == 1:
        return urllib.parse.urljoin(base_url, links[0])
    return None


def browser_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def browser_fetch(url: str, dest: Path, *, timeout_s: float = 90.0) -> tuple[bytes, str, str]:
    """Load ``url`` in headless Chromium and come back with the PDF it leads to.

    Handles the three shapes seen in practice: the URL is the PDF itself; the page starts a
    download on load; the page is a viewer or portal with a link or button that yields the PDF
    (scripted, so a plain client never sees it). Returns (bytes, final_url, how). Raises
    LayerError(DOC_NOT_PDF) when no PDF can be found and DOC_FETCH_FAILED on browser trouble.
    """
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright

    ms = int(timeout_s * 1000)
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except PwError as exc:
            raise LayerError(DOC_FETCH_FAILED, f"headless Chromium did not start: {exc}", hint="run `python -m playwright install chromium` in the kicad-mcp-layer environment") from None
        try:
            context = browser.new_context(user_agent=USER_AGENT, accept_downloads=True, locale="en-US", viewport={"width": 1280, "height": 900})
            downloads: list = []
            # portals often start the download from a script, sometimes in a popup window: watch every page
            context.on("page", lambda pg: pg.on("download", lambda d: downloads.append(d)))
            page = context.new_page()
            page.on("download", lambda d: downloads.append(d))
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=ms)
            except PwError as exc:
                # a navigation that turns into a download aborts the goto; the download event still fires
                if "Download is starting" not in str(exc) and not downloads:
                    raise LayerError(DOC_FETCH_FAILED, f"browser could not load {url}: {str(exc).splitlines()[0]}") from None
                response = None
            if response is not None and not downloads:
                ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype == "application/pdf" or response.url.lower().endswith(".pdf"):
                    body = response.body()
                    if body[:5] == b"%PDF-":
                        return body, response.url, "direct"
            # a portal or viewer: let scripts settle and give a delayed download a chance
            try:
                page.wait_for_load_state("networkidle", timeout=min(ms, 20000))
            except PwTimeout:
                pass
            deadline = time.time() + 12
            while not downloads and time.time() < deadline:
                page.wait_for_timeout(500)
            if downloads:
                d = downloads[0]
                return Path(d.path()).read_bytes(), d.url, "download started by the page"
            candidates = page.evaluate(
                """() => {
                    const out = [];
                    for (const a of document.querySelectorAll('a[href]')) {
                        const href = a.href || '';
                        const text = (a.innerText || a.getAttribute('aria-label') || '').trim();
                        const score = (/\\.pdf(\\?|$)/i.test(href) ? 3 : 0) + (/download/i.test(text + ' ' + href) ? 2 : 0) + (/datasheet|data sheet/i.test(text) ? 1 : 0);
                        if (score > 0) out.push({href, text: text.slice(0, 80), score});
                    }
                    for (const b of document.querySelectorAll('button')) {
                        const text = (b.innerText || b.getAttribute('aria-label') || '').trim();
                        if (/download/i.test(text)) out.push({href: '', text: text.slice(0, 80), score: 2, button: true});
                    }
                    return out.sort((x, y) => y.score - x.score).slice(0, 6);
                }"""
            )
            def _download_of(info):
                """The download an expect_download wait produced, or None; the wait is cancelled when
                the action inside it raised, which Playwright reports as a CancelledError."""
                try:
                    return info.value
                except BaseException:  # noqa: BLE001 - PwTimeout, PwError or asyncio.CancelledError alike
                    return None

            for cand in candidates:
                href = cand.get("href") or ""
                if href and not cand.get("button"):
                    resp = None
                    with page.expect_download(timeout=15000) as dl_info:
                        try:
                            resp = page.goto(href, wait_until="domcontentloaded", timeout=ms)
                        except PwError:
                            pass  # a navigation that becomes a download aborts with "Download is starting"
                    d = _download_of(dl_info)
                    if d is not None:
                        return Path(d.path()).read_bytes(), d.url, f"link {href}"
                    if resp is not None and (resp.headers.get("content-type") or "").startswith("application/pdf"):
                        body = resp.body()
                        if body[:5] == b"%PDF-":
                            return body, resp.url, f"link {href}"
                    try:
                        page.go_back(wait_until="domcontentloaded", timeout=ms)
                    except PwError:
                        pass
                else:
                    with page.expect_download(timeout=15000) as dl_info:
                        try:
                            page.get_by_role("button", name=re.compile("download", re.I)).first.click(timeout=5000)
                        except PwError:
                            pass
                    d = _download_of(dl_info)
                    if d is not None:
                        return Path(d.path()).read_bytes(), d.url, f"button {cand.get('text')!r}"
            raise LayerError(DOC_NOT_PDF, f"the browser reached {page.url} but found no PDF to download",
                             hint="the file may sit behind a login or a form; open it yourself, save it, then doc_import the file")
        finally:
            browser.close()


def fetch(url: str, *, subdir: str = "datasheets", filename: str | None = None, title: str | None = None, tags: list[str] | None = None,
          notes: str = "", referer: str | None = None, timeout_s: float = 120.0, expect: str = "pdf", browser: str = "auto", _origin: str | None = None) -> DocEntry:
    """Download a document into the library and index it.

    Follows redirects, sends browser-grade headers, and when an HTML page comes back where a
    PDF was expected, follows a single unambiguous PDF link or meta refresh on that page. If
    that still yields no PDF, or the site refuses the plain client, ``browser="auto"`` loads the
    page in headless Chromium and takes the download the page offers; ``"always"`` starts there,
    ``"never"`` skips it. Raises LayerError(DOC_FETCH_FAILED / DOC_NOT_PDF) with a classified hint.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LayerError(INVALID_ARGUMENT, f"unsupported URL scheme {parsed.scheme!r}")
    if browser == "always":
        return _fetch_with_browser(url, subdir=subdir, filename=filename, title=title, tags=tags, notes=notes, timeout_s=timeout_s)
    try:
        return _fetch_plain(url, subdir=subdir, filename=filename, title=title, tags=tags, notes=notes, referer=referer, timeout_s=timeout_s, expect=expect, _origin=_origin)
    except LayerError as exc:
        if browser == "auto" and expect == "pdf" and exc.code in (DOC_NOT_PDF, DOC_FETCH_FAILED) and "404" not in str(exc) and browser_available():
            try:
                return _fetch_with_browser(url, subdir=subdir, filename=filename, title=title, tags=tags, notes=notes, timeout_s=timeout_s)
            except LayerError as exc2:
                raise LayerError(exc2.code, f"plain fetch: {_plain_message(exc)}; browser tier: {_plain_message(exc2)}", hint=exc2.hint) from None
        raise


def _plain_message(exc: LayerError) -> str:
    """The message of a LayerError without its code prefix and hint, for chaining."""
    text = str(exc)
    text = re.sub(r"^\[[A-Z_]+\]\s*", "", text)
    return text.split(" Hint:")[0].strip()


def _fetch_with_browser(url: str, *, subdir: str, filename: str | None, title: str | None, tags: list[str] | None, notes: str, timeout_s: float) -> DocEntry:
    if not browser_available():
        raise LayerError(DOC_FETCH_FAILED, "the browser tier needs Playwright", hint="pip install 'kicad-mcp-layer[browser]' then `python -m playwright install chromium`")
    dest_dir = docs_dir() / _safe_name(subdir) if subdir else docs_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    data, final_url, how = browser_fetch(url, dest_dir, timeout_s=min(timeout_s, 120.0))
    if filename is None:
        tail = Path(urllib.parse.urlparse(final_url).path).name or Path(urllib.parse.urlparse(url).path).name
        filename = _safe_name(tail)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
    dest = dest_dir / _safe_name(filename)
    dest.write_bytes(data)
    note = (notes + "; " if notes else "") + f"fetched with headless Chromium ({how})"
    return _register(dest, title=title or dest.stem, source_url=url, content_type="application/pdf", tags=tags or [], notes=note)


def _fetch_plain(url: str, *, subdir: str, filename: str | None, title: str | None, tags: list[str] | None, notes: str, referer: str | None, timeout_s: float, expect: str,
                 _origin: str | None) -> DocEntry:
    parsed = urllib.parse.urlparse(url)
    try:
        with _open(url, referer=referer, timeout_s=timeout_s) as r:
            data = r.read()
            final_url = r.geturl()
            declared = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    except urllib.error.HTTPError as exc:
        hint = {403: "the site refuses scripted clients; the browser tier is tried next, or open the URL in a browser, save the file, then doc_import it",
                404: "the URL is gone; search for a current link", 429: "rate limited; wait and retry"}.get(exc.code, "retry later or fetch it in a browser and doc_import the file")
        raise LayerError(DOC_FETCH_FAILED, f"HTTP {exc.code} from {parsed.netloc}: {exc.reason}", hint=hint) from None
    except ssl.SSLError as exc:
        raise LayerError(DOC_FETCH_FAILED, f"TLS problem talking to {parsed.netloc}: {exc}", hint="the site's certificate is bad; find a mirror (distributor sites often host the same PDF) or fetch it in a browser and doc_import the file") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LayerError(DOC_FETCH_FAILED, f"could not reach {parsed.netloc}: {exc}", hint="check the URL, retry, or fetch it in a browser and doc_import the file", retryable=True) from None

    kind = _sniff(data)
    if expect == "pdf" and kind == "text/html":
        link = _find_pdf_link(data, final_url)
        if link and link != url:
            return _fetch_plain(link, subdir=subdir, filename=filename, title=title, tags=tags, notes=notes, referer=final_url, timeout_s=timeout_s, expect=expect, _origin=_origin or url)
        raise LayerError(DOC_NOT_PDF, f"{parsed.netloc} returned an HTML page instead of a PDF ({len(data):,} bytes)",
                         hint="the site puts the file behind a viewer, a script or a bot check; the browser tier is tried next, or open it in a browser, save the PDF, then doc_import it. Pass expect='any' to keep the HTML")
    if filename is None:
        tail = Path(urllib.parse.urlparse(final_url).path).name or Path(parsed.path).name
        filename = _safe_name(tail)
        if kind == "application/pdf" and not filename.lower().endswith(".pdf"):
            filename += ".pdf"
    dest_dir = docs_dir() / _safe_name(subdir) if subdir else docs_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_name(filename)
    dest.write_bytes(data)
    if _origin and _origin != url:
        notes = (notes + "; " if notes else "") + f"file downloaded from {url}"
    return _register(dest, title=title or dest.stem, source_url=_origin or url, content_type=kind if kind != "application/octet-stream" else (declared or kind), tags=tags or [], notes=notes)


def import_file(path: Path, *, subdir: str = "datasheets", source_url: str | None = None, title: str | None = None, tags: list[str] | None = None,
                notes: str = "", move: bool = False) -> DocEntry:
    """Bring a file already on disk (saved from a browser, for instance) into the library."""
    path = Path(path)
    if not path.is_file():
        raise LayerError(NOT_FOUND_IN_DESIGN, f"{path} does not exist")
    data = path.read_bytes()[:4096]
    kind = _sniff(data)
    dest_dir = docs_dir() / _safe_name(subdir) if subdir else docs_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_name(path.name)
    if dest.resolve() != path.resolve():
        if move:
            path.replace(dest)
        else:
            dest.write_bytes(path.read_bytes())
    return _register(dest, title=title or dest.stem, source_url=source_url, content_type=kind, tags=tags or [], notes=notes)


# --------------------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------------------


def resolve(doc: str) -> tuple[DocEntry | None, Path]:
    """A document by index id, by library-relative file, or by any path inside the workspace."""
    entries = load_index()
    for e in entries:
        if e.id == doc or e.file == doc or Path(e.file).name == doc or e.title == doc:
            return e, docs_dir() / e.file
    p = Path(doc)
    if not p.is_absolute():
        candidate = docs_dir() / doc
        if candidate.is_file():
            return None, candidate
    path = resolve_in_workspace(doc, must_exist=True)
    return None, path


def _pdf_reader(path: Path):
    """pypdf, with its per-object repair chatter kept out of the server's stderr."""
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)
    return PdfReader(str(path))


def page_count(path: Path) -> int:
    return len(_pdf_reader(path).pages)


def _text_cache(path: Path) -> Path:
    d = docs_dir() / TEXT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_doc_id(_sha256(path))}.json"


def extract_text(path: Path, *, refresh: bool = False) -> list[str]:
    """Text per page (pypdf), cached beside the library. Image-only pages come back empty."""
    if _sniff(path.read_bytes()[:8]) != "application/pdf":
        return [path.read_text(encoding="utf-8", errors="replace")]
    cache = _text_cache(path)
    if cache.is_file() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    reader = _pdf_reader(path)
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page must not lose the rest
            pages.append("")
    cache.write_text(json.dumps(pages), encoding="utf-8", newline='\n')
    return pages


def parse_pages(spec: str | None, n: int) -> list[int]:
    """'1-3,7' -> [1, 2, 3, 7], clamped to the document; None means every page."""
    if not spec:
        return list(range(1, n + 1))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a), int(b)
        else:
            lo = hi = int(part)
        out.extend(range(max(1, lo), min(n, hi) + 1))
    return sorted(set(out))


def find_in_text(pages: list[str], pattern: str, *, context: int = 160, limit: int = 20) -> list[dict]:
    """Case-insensitive regex hits with the page number and surrounding text.

    Hits whose context windows overlap on a page are merged into one window, so a term that
    occurs five times in a table comes back as one snippet, not five copies of the same lines;
    ``hits`` says how many matches the window holds. At most ``limit`` windows.
    """
    rx = re.compile(pattern, flags=re.IGNORECASE)
    hits: list[dict] = []
    for i, text in enumerate(pages, start=1):
        windows: list[list[int]] = []  # [start, end, count] per merged window on this page
        for m in rx.finditer(text):
            a, b = max(0, m.start() - context), min(len(text), m.end() + context)
            if windows and a <= windows[-1][1]:
                windows[-1][1] = max(windows[-1][1], b)
                windows[-1][2] += 1
            else:
                windows.append([a, b, 1])
        for a, b, n in windows:
            hits.append({"page": i, "hits": n, "text": re.sub(r"\s+", " ", text[a:b]).strip()})
            if len(hits) >= limit:
                return hits
    return hits


def render_page(path: Path, page: int, *, scale: float = 2.0) -> Path:
    """Render one page to PNG (pdfium) so drawings, pinouts and tables can be looked at."""
    import pypdfium2 as pdfium

    n = page_count(path)
    if not 1 <= page <= n:
        raise LayerError(INVALID_ARGUMENT, f"page {page} is out of range; the document has {n} page(s)")
    out_dir = docs_dir() / PAGES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{_doc_id(_sha256(path))}-p{page}-x{scale:g}.png"
    if not out.is_file():
        pdf = pdfium.PdfDocument(str(path))
        image = pdf[page - 1].render(scale=scale).to_pil()
        image.save(out)
    return out


def search(query: str, *, limit: int = 20) -> list[tuple[DocEntry, list[dict]]]:
    """Documents whose title, file name, tags, notes or text mention the query."""
    rx = re.compile(re.escape(query), flags=re.IGNORECASE)
    out = []
    for e in load_index():
        meta = " ".join([e.title, e.file, " ".join(e.tags), e.notes, e.source_url or ""])
        hits: list[dict] = []
        path = docs_dir() / e.file
        if path.is_file() and e.content_type == "application/pdf":
            try:
                hits = find_in_text(extract_text(path), re.escape(query), limit=3)
            except Exception:  # noqa: BLE001
                hits = []
        if rx.search(meta) or hits:
            out.append((e, hits))
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------------------
# the section and table index of a document, and the fact sheet of a part
# --------------------------------------------------------------------------------------

SECTIONS_DIR = ".sections"
_TOC_LINE = re.compile(r"^\s*(?P<title>[A-Z0-9][^.]{2,90}?)\s*\.{3,}\s*(?P<page>\d{1,3})\s*$")
_CAPTION = re.compile(r"^\s*(?P<kind>Table|Figure|Fig\.)\s+(?P<num>\d+[A-Za-z]?)[.:]?\s+(?P<rest>\S.{2,90})$")
_NUMBERED = re.compile(r"^\s*(?P<num>\d{1,2}(?:\.\d{1,2}){0,2})\s+(?P<title>[A-Z][A-Za-z0-9 ,/()&+-]{3,70})\s*$")
_HEADING_WORDS = ("pin", "electrical characteristics", "absolute maximum", "recommended operating", "package", "ordering", "application",
                  "typical operating", "mechanical", "dimension", "timing", "thermal", "features", "description", "layout", "block diagram",
                  "functional diagram", "specification", "operating", "connection")


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip(" .:").lower()


def outline(path: Path) -> list[dict]:
    """The PDF's bookmarks as {title, page, level}."""
    reader = _pdf_reader(path)
    out: list[dict] = []

    def walk(items, level: int) -> None:
        for it in items:
            if isinstance(it, list):
                walk(it, level + 1)
                continue
            try:
                out.append({"title": re.sub(r"\s+", " ", str(it.title)).strip(), "page": reader.get_destination_page_number(it) + 1, "level": level})
            except Exception:  # noqa: BLE001 - a broken destination is not worth losing the rest
                continue

    try:
        walk(reader.outline, 0)
    except Exception:  # noqa: BLE001
        return []
    return out


def sections(path: Path, *, refresh: bool = False) -> list[dict]:
    """Every place a reader might want to jump to, sorted by page: bookmarks, the contents page, headings
    found in the text, and table and figure captions. Each is {title, page, kind}; kind is bookmark,
    contents, heading, table or figure. Cached beside the text cache."""
    cache = docs_dir() / SECTIONS_DIR / f"{_doc_id(_sha256(path))}.json"
    if cache.is_file() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    pages = extract_text(path, refresh=refresh)
    found: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def add(title: str, page: int, kind: str) -> None:
        key = (_norm_title(title), page)
        if title and key not in seen and 1 <= page <= len(pages):
            seen.add(key)
            found.append({"title": re.sub(r"\s+", " ", title).strip(" ."), "page": page, "kind": kind})

    for b in outline(path):
        add(b["title"], b["page"], "bookmark")
    for i, text in enumerate(pages[:6], start=1):
        for line in text.splitlines():
            m = _TOC_LINE.match(line)
            if m:
                add(m.group("title"), int(m.group("page")), "contents")
    seen_caption: set[str] = set()
    for i, text in enumerate(pages, start=1):
        for line in text.splitlines():
            if "...." in line:
                continue
            m = _CAPTION.match(line)
            if m:
                key = f"{m.group('kind')[:3].lower()}{m.group('num')}"
                if key not in seen_caption:
                    seen_caption.add(key)
                    kind = "table" if m.group("kind").lower().startswith("t") else "figure"
                    add(f"{m.group('kind')} {m.group('num')}. {m.group('rest')}", i, kind)
                continue
            m = _NUMBERED.match(line)
            if m:
                add(f"{m.group('num')} {m.group('title')}", i, "heading")
                continue
            s = line.strip()
            if 3 < len(s) <= 60 and not s.endswith(".") and s[0].isupper() and any(w in s.lower() for w in _HEADING_WORDS) and len(s.split()) <= 6:
                add(s, i, "heading")
    found.sort(key=lambda d: (d["page"], d["kind"] != "bookmark", d["title"]))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(found), encoding="utf-8", newline="\n")
    return found


def parts_dir() -> Path:
    """Where the fact sheets live: ``KICAD_LAYER_PARTS_DIR`` or ``parts/`` beside the documentation library."""
    env = os.environ.get("KICAD_LAYER_PARTS_DIR")
    return Path(env).expanduser() if env else docs_dir().parent / "parts"


def _slug(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "-", part.strip()).strip("-")


def fact_sheet_path(part: str) -> Path | None:
    """The sheet for ``part``: an exact file, else the one whose name matches case-insensitively or by prefix (``MAX98357A`` finds ``MAX98357A-MAX98357B.md``)."""
    d = parts_dir()
    if not d.is_dir():
        return None
    slug = _slug(part)
    exact = d / f"{slug}.md"
    if exact.is_file():
        return exact
    low = slug.lower()
    cands = sorted(p for p in d.glob("*.md") if p.stem.lower() == low or p.stem.lower().startswith(low) or low.startswith(p.stem.lower()))
    return cands[0] if cands else None


def fact_sheet(part: str, section: str | None = None, find: str | None = None) -> tuple[Path | None, str, list[str]]:
    """(path, text, headings): the sheet, or its one ``## section`` (case-insensitive prefix), or with ``find`` only
    the lines matching that regular expression (each prefixed by its section), and every heading the sheet has."""
    path = fact_sheet_path(part)
    if path is None:
        return None, "", []
    text = path.read_text(encoding="utf-8")
    headings = [l[3:].strip() for l in text.splitlines() if l.startswith("## ")]
    want = section.strip().lower() if section else None
    rx = re.compile(find, re.IGNORECASE) if find else None
    out: list[str] = []
    keep = want is None
    current = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            if want is not None:
                keep = current.lower().startswith(want)
            if keep and rx is None:
                out.append(line)
            continue
        if not keep:
            continue
        if rx is None:
            out.append(line)
        elif rx.search(line):
            out.append(f"[{current}] {line.strip()}" if current else line.strip())
    return path, "\n".join(out).strip(), headings


FACT_SHEET_TEMPLATE = """# {part}

Manufacturer: {manufacturer}. Source: {title} ({file}), library id {doc_id}. Written {date}.
Every number carries the page it was read from (p.N is the PDF page, as doc_text and doc_page count it).
Tables were checked against the rendered page, not just the extracted text.

## Pins
| pin | name | function | p. |
|---|---|---|---|

## Limits
Absolute maximum ratings and recommended operating conditions that a design meets or checks: supply, inputs, temperature, current.

## Values
The numbers a design is built on: thresholds, tolerances, resistor and capacitor values the datasheet prescribes, formulas, with the condition each holds under.

## Recommended circuit
What the typical application circuit connects to which pin, with values and the page of the figure.

## Package
Outline name and dimensions, exposed pad, land-pattern notes, mechanical keep-outs, orientation of pin 1.

## Notes
Anything that bit or would bite: pins that must stay open, polarity, sequencing, layout rules, errata.
"""


def fact_sheet_brief(part: str, entry: DocEntry | None) -> str:
    """How a fact sheet gets written: the instructions handed to whoever writes it (a subagent, normally)."""
    src = f"{entry.title} ({entry.file}, id {entry.id}, {entry.pages} pages)" if entry else "the part's datasheet in the documentation library (doc_list finds it)"
    target = parts_dir() / f"{_slug(part)}.md"
    return "\n".join([
        f"No fact sheet for {part}. Write one at {target} from {src}, then ask again.",
        "Method: doc_sections gives the pages of the pin table, the limits, the characteristics and the application circuit;",
        "doc_text with pages reads them; doc_page renders any table or drawing so the numbers are checked against the picture.",
        "Keep the template's headings, cite the page on every row, keep it under 150 lines, and write nothing the datasheet does not say.",
        "Template:", FACT_SHEET_TEMPLATE,
    ])


def main(argv: list[str]) -> int:
    """The document library from the command line, for scripts and subagents without the MCP server::

        python -m kicad_layer.docs list [QUERY]
        python -m kicad_layer.docs sections DOC [--find RE]
        python -m kicad_layer.docs text DOC PAGES            e.g. 15 or 7,15-16
        python -m kicad_layer.docs find DOC RE
        python -m kicad_layer.docs page DOC N                renders the page, prints the PNG path
        python -m kicad_layer.docs facts PART [SECTION] [--find RE]
    """
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not argv:
        print(main.__doc__)
        return 2
    cmd, args = argv[0], [a for a in argv[1:] if not a.startswith("--")]
    if cmd == "list":
        found = search(args[0]) if args else [(e, []) for e in load_index()]
        for e, hits in found:
            print(f"{e.id}  {e.pages or '?':>3} pages  {e.file}  {e.title}" + (f"  (p.{hits[0]['page']})" if hits else ""))
        return 0
    if cmd == "facts":
        find = argv[argv.index("--find") + 1] if "--find" in argv else None
        path, text, headings = fact_sheet(args[0], args[1] if len(args) > 1 else None, find)
        if path is None:
            try:
                entry, _ = resolve(args[0])
            except LayerError:
                entry = None
            print(fact_sheet_brief(args[0], entry))
            return 1
        print(text if text else f"{path}: no section starting with {args[1]!r}; sections: {', '.join(headings)}")
        return 0
    if len(args) < 1:
        print(main.__doc__)
        return 2
    entry, path = resolve(args[0])
    if cmd == "sections":
        rx = re.compile(argv[argv.index("--find") + 1], re.I) if "--find" in argv else None
        for s in sections(path):
            if rx is None or rx.search(s["title"]):
                print(f"p.{s['page']:<4} {s['kind']:8s} {s['title']}")
        return 0
    if cmd == "text" and len(args) >= 2:
        pages = extract_text(path)
        for n in parse_pages(args[1], len(pages)):
            print(f"===== page {n}\n{pages[n - 1]}")
        return 0
    if cmd == "find" and len(args) >= 2:
        for h in find_in_text(extract_text(path), args[1]):
            print(f"p.{h['page']} ({h['hits']} hit{'s' if h['hits'] > 1 else ''}): {h['text']}")
        return 0
    if cmd == "page" and len(args) >= 2:
        print(render_page(path, int(args[1])))
        return 0
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
