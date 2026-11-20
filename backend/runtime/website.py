"""Website knowledge sources (restoration R1/R2, approved 2026-11-19).

A knowledge source of ``kind="website"`` holds a start URL and an import mode
(``single`` page or bounded same-site ``crawl``). The worker fetches the pages
and hands each one to the normal Spec 012 ``Ingestor`` as a document of the
source, so extraction, chunking, versioning, document ACLs, approval,
retrieval and the strict-mode evidence gate are exactly those of uploads.

**Every fetch goes through ``outbound_guard.guarded_request``**: https only
(production), one DNS resolution whose every answer must be public, the
connection pinned to that address (no DNS rebinding), TLS verified, size cap,
no automatic redirects. Redirects are followed here *manually*, at most
``MAX_REDIRECTS`` hops, and every hop is a new ``guarded_request`` (so each
target is checked again). Error messages are fixed categories; page content
never appears in errors or logs. No anti-bot bypass is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime, timedelta
from html import unescape as html_unescape
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

import outbound_guard as og

from core.hierarchy import ConcurrentModification
from runtime.knowledge_store import DOCUMENTS, MAX_UPLOAD_BYTES, SOURCES, IngestError, Ingestor

USER_AGENT = "BitBotProKnowledgeBot/1.0 (+https://bitbotpro.io)"
MAX_REDIRECTS = 5
FETCH_TIMEOUT = 15.0
MODES = ("single", "crawl")
DEFAULT_MAX_PAGES = 25
MAX_PAGES_LIMIT = 200
JOB_LEASE_SECONDS = 600
SYSTEM_ACTOR = "system:website-import"
_HTML = ("text/html", "application/xhtml+xml")
_TYPES = {"text/html": "text/html", "application/xhtml+xml": "text/html", "text/plain": "text/plain",
          "text/markdown": "text/markdown", "application/pdf": "application/pdf", "application/json": "application/json",
          "text/csv": "text/csv"}


class FetchError(Exception):
    """A safe, user-facing reason a page could not be imported."""


# ═════════════════════════════════ URLs ═════════════════════════════════

def normalise_url(raw: Any) -> str:
    """The canonical https URL a user asked for (scheme added or upgraded to
    https, fragment removed). Raises FetchError for anything else."""
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 2000:
        raise FetchError("Enter a website address (for example https://example.com/help).")
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise FetchError("Only http(s) website addresses are supported.")
    if parts.username or parts.password:
        raise FetchError("Website addresses must not contain a user name or password.")
    return ascii_url(urlunsplit(("https", parts.netloc, parts.path or "/", parts.query, "")))


def ascii_url(url: str) -> str:
    """IDNA host, percent-encoded path and query (the guard accepts ASCII only)."""
    parts = urlsplit(url)
    try:
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (UnicodeError, ValueError):
        raise FetchError("This website address is not valid.")
    netloc = host + (f":{port}" if port and port != 443 else "")
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%+,;:@/?!$'()*-._~")
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))


def same_site(a: str, b: str) -> bool:
    ha, hb = (urlsplit(a).hostname or "").lower(), (urlsplit(b).hostname or "").lower()
    strip = lambda h: h[4:] if h.startswith("www.") else h                     # noqa: E731
    return bool(ha) and strip(ha) == strip(hb)


def document_name(url: str) -> str:
    """A stable, valid document name for a page URL (the URL itself is stored
    on the document and shown to users)."""
    p = urlsplit(url)
    base = (p.hostname or "page") + (p.path.rstrip("/") or "") + (("?" + p.query) if p.query else "")
    name = re.sub(r"[^A-Za-z0-9._\-()]+", "_", base).strip("_.") or "page"
    if len(name) > 150 or name != base.replace("/", "_"):
        name = f"{name[:150]}-{hashlib.sha256(url.encode()).hexdigest()[:8]}"
    return name


_SKIP_EXT = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|ico|bmp|tiff?|css|js|mjs|map|zip|gz|tgz|tar|rar|7z|exe|dmg|"
                       r"msi|iso|mp[34]|m4[av]|mov|avi|wmv|webm|ogg|wav|flac|woff2?|ttf|otf|eot|xml|rss|atom|"
                       r"xlsx?|pptx?|docx?|apk)$", re.I)
_TRACKING = re.compile(r"^(?:utm_[a-z]+|fbclid|gclid|mc_[a-z]+|ref|_hs[a-z]+)$", re.I)


def crawl_url(link: str, start: str) -> Optional[str]:
    """The canonical form of a same-site page link worth crawling, else None."""
    try:
        u = urldefrag(urljoin(start, html_unescape(link.strip())))[0]
        p = urlsplit(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or not same_site(u, start) or _SKIP_EXT.search(p.path):
        return None
    query = "&".join(q for q in p.query.split("&") if q and not _TRACKING.match(q.split("=", 1)[0]))
    try:
        return ascii_url(urlunsplit(("https", p.netloc, p.path or "/", query, "")))
    except FetchError:
        return None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: List[str] = []
        self.follow = True
        self.index = True
        self.base: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href") and "nofollow" not in a.get("rel", "").lower():
            if len(self.links) < 2000:
                self.links.append(a["href"])
        elif tag == "base" and a.get("href") and self.base is None:
            self.base = a["href"]
        elif tag == "meta" and a.get("name", "").lower() in ("robots", "bitbotproknowledgebot"):
            content = a.get("content", "").lower()
            self.follow = self.follow and "nofollow" not in content and "none" not in content
            self.index = self.index and "noindex" not in content and "none" not in content


def page_links(body: bytes, page_url: str) -> Tuple[List[str], bool, bool]:
    """(absolute links, may follow, may index) of an HTML page."""
    p = _Links()
    try:
        p.feed(body.decode("utf-8", "replace")[:MAX_UPLOAD_BYTES])
        p.close()
    except Exception:                                  # malformed HTML: whatever was parsed so far
        pass
    base = urljoin(page_url, p.base) if p.base else page_url
    return [urljoin(base, link) for link in p.links], p.follow, p.index


# ═════════════════════════════════ fetching ═════════════════════════════════

def _charset(content_type: str, data: bytes) -> Optional[str]:
    m = re.search(r"charset=[\"']?([A-Za-z0-9_\-:.]+)", content_type or "", re.I)
    if m:
        return m.group(1)
    m = re.search(rb"<meta[^>]+charset=[\"']?([A-Za-z0-9_\-:.]+)", data[:4096], re.I)
    return m.group(1).decode("ascii", "ignore") if m else None


def to_utf8(data: bytes, content_type: str) -> bytes:
    """Re-encode a text response as UTF-8 (the ingestor's text format)."""
    for enc in (_charset(content_type, data), "utf-8", "cp1252"):
        if not enc:
            continue
        try:
            return data.decode(enc).encode("utf-8")
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1").encode("utf-8")


def _http_error(status: int) -> str:
    if status in (401, 403):
        return f"The website refused access (HTTP {status}). It may block automated access or require a login."
    if status == 404 or status == 410:
        return f"The page was not found (HTTP {status})."
    if status == 429:
        return "The website is rate limiting requests (HTTP 429). Try again later."
    if status >= 500:
        return f"The website reported a server error (HTTP {status}). Try again later."
    return f"The website answered HTTP {status}."


class Fetcher:
    """Guarded page fetches with manual, re-validated redirects."""

    def __init__(self, env: Mapping[str, str], *, resolver=None, client_factory=None):
        self.env = env
        self.resolver = resolver
        self.client_factory = client_factory

    async def get(self, url: str, *, accept: str = "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
                  max_size: int = MAX_UPLOAD_BYTES, raw: bool = False) -> Tuple[str, str, bytes]:
        """Returns ``(final_url, media_type, body)``; raises FetchError. ``raw``
        (robots.txt, sitemaps) skips the page-type check and re-encoding."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                r = await og.guarded_request("GET", current, env=self.env, timeout=FETCH_TIMEOUT,
                                             max_size=max_size,
                                             headers={"User-Agent": USER_AGENT, "Accept": accept},
                                             resolver=self.resolver, client_factory=self.client_factory)
            except og.OutboundBlocked as exc:
                msg = str(exc)                                  # fixed guard messages, never upstream content
                if msg in (og.DESTINATION_NOT_ALLOWED_ERROR, "Internal host names are not allowed."):
                    raise FetchError("This address points to a private or internal network and cannot be imported.")
                if "could not be resolved" in msg:
                    raise FetchError("The website's host name could not be found (DNS).")
                raise FetchError(msg)
            except og.OutboundError as exc:
                raise FetchError(str(exc))
            status = r["status"]
            if status in (301, 302, 303, 307, 308):
                location = (r["headers"].get("location") or "").strip()
                if not location:
                    raise FetchError(f"The website answered a redirect (HTTP {status}) without a target.")
                nxt = urldefrag(urljoin(current, location))[0]
                if urlsplit(nxt).scheme == "http":                  # upgrade; the guard requires https
                    nxt = "https" + nxt[4:]
                try:
                    current = ascii_url(nxt)                       # re-checked by the guard on the next hop
                except FetchError:
                    raise FetchError("The website redirected to an invalid address.")
                continue
            if status >= 400:
                raise FetchError(_http_error(status))
            if status != 200:
                raise FetchError(f"The website answered HTTP {status}.")
            if r["truncated"]:
                raise FetchError(f"The page is larger than {max_size // (1024 * 1024)} MB.")
            media = (r["headers"].get("content-type") or "").split(";")[0].strip().lower()
            if raw:
                return current, media, r["content"]
            if media not in _TYPES:
                raise FetchError(f"Unsupported content type ({media or 'unknown'}); "
                                 "pages must be HTML, text, Markdown, CSV, JSON or PDF.")
            body = r["content"]
            if media != "application/pdf":
                body = to_utf8(body, r["headers"].get("content-type") or "")
            return current, _TYPES[media], body
        raise FetchError("Too many redirects.")


# ═════════════════════════════════ jobs ═════════════════════════════════

def website_view(doc: dict) -> Optional[Dict[str, Any]]:
    w = doc.get("website")
    if not w:
        return None
    run = doc.get("website_run") or {}
    return {"url": w.get("url"), "mode": w.get("mode"), "max_pages": w.get("max_pages"),
            "state": run.get("state") or "queued", "error": run.get("error"),
            "pages_found": run.get("pages_found", 0), "pages_done": run.get("pages_done", 0),
            "pages_imported": run.get("pages_imported", 0), "pages_failed": run.get("pages_failed", 0),
            "last_attempt_at": run.get("started_at"), "last_finished_at": run.get("finished_at"),
            "last_success_at": run.get("last_success_at")}


def initial_run(now: str, previous: Optional[dict] = None) -> Dict[str, Any]:
    return {"state": "queued", "queued_at": now, "lease_until": None, "error": None,
            "pages_found": 0, "pages_done": 0, "pages_imported": 0, "pages_failed": 0,
            "started_at": None, "finished_at": None,
            "last_success_at": (previous or {}).get("last_success_at")}


class WebsiteImporter:
    """Runs queued website imports (worker). One source at a time per worker;
    a lease keeps two workers from running the same source."""

    def __init__(self, store, *, clock: Callable[[], str], fetcher: Fetcher, delay_seconds: float = 0.5,
                 sleep=asyncio.sleep):
        self.store = store
        self.clock = clock
        self.fetcher = fetcher
        self.delay = delay_seconds
        self.sleep = sleep

    async def run_pending(self, *, limit: int = 5) -> int:
        now = self.clock()
        done = 0
        for src in await self.store.find(SOURCES, {"kind": "website", "website_run.state": ["queued", "running"]},
                                         limit=limit * 4):
            run = src.get("website_run") or {}
            if run.get("state") == "running" and run.get("lease_until") and \
                    datetime.fromisoformat(now) < datetime.fromisoformat(run["lease_until"]):
                continue
            if src.get("status") != "active":
                continue
            if await self.run_source(src["id"]):
                done += 1
            if done >= limit:
                break
        return done

    async def _claim(self, source_id: str) -> Optional[dict]:
        src = await self.store.get(SOURCES, source_id)
        if src is None or src.get("kind") != "website":
            return None
        run = dict(src.get("website_run") or {})
        lease = (datetime.fromisoformat(self.clock()) + timedelta(seconds=JOB_LEASE_SECONDS)).isoformat()
        run.update(state="running", lease_until=lease, started_at=self.clock(), error=None,
                   pages_found=0, pages_done=0, pages_imported=0, pages_failed=0, finished_at=None)
        try:
            await self.store.update(SOURCES, source_id, {"website_run": run}, expect_version=src["version"])
        except ConcurrentModification:
            return None
        return await self.store.get(SOURCES, source_id)

    async def _progress(self, source_id: str, **fields) -> None:
        src = await self.store.get(SOURCES, source_id)
        if src is None:
            return
        run = {**(src.get("website_run") or {}), **fields}
        if run.get("state") == "running":
            run["lease_until"] = (datetime.fromisoformat(self.clock())
                                  + timedelta(seconds=JOB_LEASE_SECONDS)).isoformat()
        await self.store.update(SOURCES, source_id, {"website_run": run})

    async def _site_rules(self, start: str):
        """robots.txt for the site (missing or unreadable = everything allowed) and
        the sitemap locations it names."""
        from urllib.robotparser import RobotFileParser
        p = urlsplit(start)
        robots_url = f"https://{p.netloc}/robots.txt"
        rp = RobotFileParser()
        sitemaps = [f"https://{p.netloc}/sitemap.xml"]
        try:
            _, _, body = await self.fetcher.get(robots_url, accept="text/plain", max_size=500_000, raw=True)
            text = body.decode("utf-8", "replace")
            rp.parse(text.splitlines())
            named = [m.strip() for m in re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)]
            sitemaps = list(dict.fromkeys(named + sitemaps))
        except FetchError:
            rp.parse([])
        return rp, sitemaps[:5]

    async def _sitemap_urls(self, sitemaps: List[str], start: str, limit: int) -> List[str]:
        out: List[str] = []
        queue, fetched = list(sitemaps), 0
        while queue and fetched < 6 and len(out) < limit:
            sm = queue.pop(0)
            try:
                sm = ascii_url(sm)
                if not same_site(sm, start):
                    continue
                _, _, body = await self.fetcher.get(sm, accept="application/xml,text/xml", raw=True)
            except FetchError:
                continue
            fetched += 1
            text = body.decode("utf-8", "replace")
            locs = [html_unescape(x.strip()) for x in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.S | re.I)]
            if re.search(r"<sitemapindex", text, re.I):
                queue.extend(locs[:10])
                continue
            for loc in locs:
                u = crawl_url(loc, start)
                if u and u not in out:
                    out.append(u)
        # pages under the start address first
        prefix = start.rsplit("/", 1)[0] + "/"
        return sorted(out, key=lambda u: not u.startswith(prefix))[:limit]

    async def run_source(self, source_id: str) -> bool:
        src = await self._claim(source_id)
        if src is None:
            return False
        ingest = Ingestor(self.store, clock=self.clock)
        w = src["website"]
        crawl = w.get("mode") == "crawl"
        start = w["url"]
        limit = int(w.get("max_pages") or 1) if crawl else 1
        imported: Dict[str, str] = {}                 # page url -> document id
        failed: set = set()
        errors: List[str] = []
        frontier: List[str] = [start]
        seen = {start}
        rules, delay = None, self.delay
        if crawl:
            rules, sitemaps = await self._site_rules(start)
            try:
                delay = max(self.delay, min(float(rules.crawl_delay(USER_AGENT) or 0), 5.0))
            except (TypeError, ValueError):
                pass
            for u in await self._sitemap_urls(sitemaps, start, limit * 3):
                if u not in seen:
                    seen.add(u)
                    frontier.append(u)
        attempted = 0
        await self._progress(source_id, pages_found=len(seen))
        while frontier and attempted < limit:
            url = frontier.pop(0)
            if rules is not None and url != start and not rules.can_fetch(USER_AGENT, url):
                continue                              # the site asks robots not to fetch it
            if attempted and delay:
                await self.sleep(delay)
            current = await self.store.get(SOURCES, source_id)
            if current is None or current.get("status") != "active":
                return True                           # deleted or disabled while running: stop quietly
            attempted += 1
            try:
                final, media, body = await self.fetcher.get(url)
                if crawl and url != start and not same_site(final, start):
                    raise FetchError("The page redirected to another website and was skipped.")
                follow, index = True, True
                if media == "text/html":
                    links, follow, index = page_links(body, final)
                    if crawl and follow:
                        for link in links:
                            u = crawl_url(link, start)
                            if u and u not in seen and len(seen) < limit * 10:
                                seen.add(u)
                                frontier.append(u)
                if not index:
                    raise FetchError("The page asks not to be indexed (noindex) and was skipped.")
                doc = await ingest.submit(current, name=document_name(url), content_type=media, data=body,
                                          actor_id=SYSTEM_ACTOR)
                await self.store.update(DOCUMENTS, doc["id"], {"url": url, "final_url": final,
                                                               "fetched_at": self.clock()})
                if doc.get("status") == "failed":
                    failed.add(url)
                    errors.append(((doc.get("job") or {}).get("error") or "Processing failed.")
                                  + (f" ({url})" if crawl else ""))
                else:
                    imported[url] = doc["id"]
            except (FetchError, IngestError) as exc:
                failed.add(url)
                errors.append(f"{exc} ({url})" if crawl else str(exc))
            await self._progress(source_id, pages_found=len(seen), pages_done=attempted,
                                 pages_imported=len(imported), pages_failed=len(failed))
        if imported:
            # Pages no longer part of the site (or of the configured URL) are removed, but
            # only after a run that imported something (an outage never wipes a source),
            # and a page that merely failed this time keeps its previous version.
            keep = set(imported.values())
            for d in await self.store.find(DOCUMENTS, {"enterprise_id": src["enterprise_id"], "source_id": source_id}):
                if d["id"] not in keep and d.get("url") not in failed:
                    await ingest.delete_document(d["id"])
        now = self.clock()
        latest = await self.store.get(SOURCES, source_id) or src
        run = dict(latest.get("website_run") or {})
        run.update(state="done" if imported else "failed", lease_until=None, finished_at=now,
                   error=("; ".join(errors[:3]) + (f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""))[:600]
                   if errors else None)
        if imported:
            run["last_success_at"] = now
        await self.store.update(SOURCES, source_id, {"website_run": run, "updated_at": now})
        return True

    async def run_forever(self, stop: asyncio.Event, *, interval_seconds: float = 5.0) -> None:
        import logging
        log = logging.getLogger("bitbotpro.website")
        while not stop.is_set():
            try:
                await self.run_pending()
            except Exception:                         # never kill the worker; the lease lets it retry
                log.exception("website import pass failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                pass


__all__ = ["Fetcher", "FetchError", "WebsiteImporter", "normalise_url", "same_site", "document_name",
           "website_view", "initial_run", "MODES", "DEFAULT_MAX_PAGES", "MAX_PAGES_LIMIT"]
