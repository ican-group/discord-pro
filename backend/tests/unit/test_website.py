"""Restoration R1/R2: website knowledge sources.

Fetches go through the real ``outbound_guard.guarded_request`` with a fake DNS
resolver and an httpx MockTransport standing in for the internet (no live
network). Pages are ingested by the real Spec 012 ingestor and answered by the
real pipeline (strict mode included)."""
import asyncio
import unittest

from tests.unit._api_fixtures import HAVE_API

PUBLIC = {"docs.example.com": ["93.184.216.34"], "www.docs.example.com": ["93.184.216.34"],
          "other.example.net": ["93.184.216.35"], "evil.example.org": ["10.0.0.7"],
          "mixed.example.org": ["93.184.216.36", "127.0.0.1"], "meta.example.org": ["169.254.169.254"]}
PROD = {"APP_ENV": "production"}


class FakeWeb:
    """The 'internet': (host, path?query) -> (status, headers, body)."""

    def __init__(self):
        self.pages = {}
        self.requests = []

    def page(self, url, body, status=200, ctype="text/html; charset=utf-8", headers=None):
        from urllib.parse import urlsplit
        p = urlsplit(url)
        self.pages[(p.hostname, (p.path or "/") + (("?" + p.query) if p.query else ""))] = \
            (status, {"content-type": ctype, **(headers or {})}, body if isinstance(body, bytes) else body.encode())

    async def resolve(self, host, port):
        if host not in PUBLIC:
            raise OSError("NXDOMAIN")
        return PUBLIC[host]

    def client_factory(self, timeout):
        import httpx

        def handler(request):
            host = request.headers["host"]
            target = request.url.raw_path.decode()
            self.requests.append((host, target, request.url.host))
            status, headers, body = self.pages.get((host, target), (404, {"content-type": "text/html"}, b"nope"))
            return httpx.Response(status, headers=headers, content=body)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout, follow_redirects=False)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@unittest.skipUnless(HAVE_API, "httpx not installed (runs in the Docker test target)")
class FetcherTests(unittest.TestCase):
    def setUp(self):
        from runtime.website import Fetcher
        self.web = FakeWeb()
        self.f = Fetcher(PROD, resolver=self.web.resolve, client_factory=self.web.client_factory)

    def get(self, url):
        return run(self.f.get(url))

    def error(self, url):
        from runtime.website import FetchError
        with self.assertRaises(FetchError) as cm:
            self.get(url)
        return str(cm.exception)

    def test_public_page_is_fetched_through_the_pinned_address(self):
        self.web.page("https://docs.example.com/help", "<h1>Help</h1><p>Opening hours 9-5.</p>")
        final, media, body = self.get("https://docs.example.com/help")
        self.assertEqual((final, media), ("https://docs.example.com/help", "text/html"))
        self.assertIn(b"Opening hours", body)
        host, target, connected = self.web.requests[-1]
        self.assertEqual((host, target, connected), ("docs.example.com", "/help", "93.184.216.34"))

    def test_ordinary_redirects_are_followed_and_each_hop_rechecked(self):
        self.web.page("https://docs.example.com/", "", status=301, headers={"location": "https://www.docs.example.com/"})
        self.web.page("https://www.docs.example.com/", "", status=302, headers={"location": "/start?x=1"})
        self.web.page("https://www.docs.example.com/start?x=1", "<p>Start page</p>")
        final, _, body = self.get("https://docs.example.com/")
        self.assertEqual(final, "https://www.docs.example.com/start?x=1")
        self.assertEqual(len(self.web.requests), 3)

    def test_http_redirect_targets_are_upgraded_to_https(self):
        self.web.page("https://docs.example.com/a", "", status=301, headers={"location": "http://docs.example.com/b"})
        self.web.page("https://docs.example.com/b", "<p>B</p>")
        self.assertEqual(self.get("https://docs.example.com/a")[0], "https://docs.example.com/b")

    def test_redirects_into_private_networks_are_blocked(self):
        for target in ("https://evil.example.org/", "https://127.0.0.1/admin", "https://169.254.169.254/latest",
                       "https://mixed.example.org/", "https://meta.example.org/", "https://localhost/",
                       "https://bitbotpro-mongo.internal/"):
            self.web.page("https://docs.example.com/r", "", status=302, headers={"location": target})
            msg = self.error("https://docs.example.com/r")
            self.assertIn("private or internal network", msg, target)
        # the blocked hop was never requested
        self.assertTrue(all(r[2] == "93.184.216.34" for r in self.web.requests))

    def test_private_and_internal_start_urls_are_blocked(self):
        for url in ("https://evil.example.org/", "https://10.1.2.3/", "https://[::1]/", "https://mixed.example.org/"):
            self.assertIn("private or internal network", self.error(url))
        self.assertEqual(self.web.requests, [])

    def test_redirect_loops_are_bounded(self):
        self.web.page("https://docs.example.com/loop", "", status=302, headers={"location": "/loop"})
        self.assertEqual(self.error("https://docs.example.com/loop"), "Too many redirects.")
        self.assertLessEqual(len(self.web.requests), 6)

    def test_meaningful_errors(self):
        self.web.page("https://docs.example.com/denied", "x", status=403)
        self.assertIn("refused access (HTTP 403)", self.error("https://docs.example.com/denied"))
        self.assertIn("not found (HTTP 404)", self.error("https://docs.example.com/missing"))
        self.web.page("https://docs.example.com/down", "x", status=503)
        self.assertIn("server error (HTTP 503)", self.error("https://docs.example.com/down"))
        self.web.page("https://docs.example.com/img", b"\x89PNG", ctype="image/png")
        self.assertIn("Unsupported content type (image/png)", self.error("https://docs.example.com/img"))
        self.web.page("https://docs.example.com/big", b"a" * (2 * 1024 * 1024 + 10))
        self.assertIn("larger than 2 MB", self.error("https://docs.example.com/big"))
        self.assertIn("could not be found (DNS)", self.error("https://unknown.example.com/"))

    def test_non_utf8_pages_are_converted(self):
        self.web.page("https://docs.example.com/latin", "<p>Caf\xe9 cr\xe8me</p>".encode("latin-1"),
                      ctype="text/html; charset=iso-8859-1")
        self.assertIn("Café crème", self.get("https://docs.example.com/latin")[2].decode("utf-8"))

    def test_url_normalisation(self):
        from runtime.website import FetchError, normalise_url
        self.assertEqual(normalise_url("docs.example.com/help#top"), "https://docs.example.com/help")
        self.assertEqual(normalise_url("http://Docs.Example.com"), "https://docs.example.com/")
        self.assertEqual(normalise_url("https://docs.example.com/caf\xe9?q=a b"),
                         "https://docs.example.com/caf%C3%A9?q=a%20b")
        for bad in ("", "ftp://x.example.com/", "https://user:pw@docs.example.com/", "javascript:alert(1)"):
            with self.assertRaises(FetchError):
                normalise_url(bad)


@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class WebsiteBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from runtime.website import Fetcher, WebsiteImporter
        from tests.unit._api_fixtures import World
        from tests.unit.test_knowledge import FALLBACK, GroundedProvider
        self.FALLBACK = FALLBACK
        self.w = await World().build()
        self.web = FakeWeb()
        self.importer = WebsiteImporter(self.w.store, clock=self.w.S.clock, delay_seconds=0,
                                        fetcher=Fetcher(PROD, resolver=self.web.resolve,
                                                        client_factory=self.web.client_factory))
        self.provider = GroundedProvider()
        self.w.S.providers = {"anthropic": self.provider}
        await self.w.call("PATCH", f"/bots/{self.w.bA.id}/config", "owner",
                          {"changes": {"ai.credential_id": self.w.cA_ai.id}})
        self.web.page("https://docs.example.com/refunds",
                      "<html><head><title>Refunds</title><script>steal()</script></head><body>"
                      "<h1>Refund policy. Customers can request a refund within 21 days of delivery.</h1>"
                      "</body></html>")

    async def create(self, url="https://docs.example.com/refunds", who="admin", owner=None, **extra):
        owner = owner or f"enterprises/{self.w.eA.id}"
        return await self.w.call("POST", f"/{owner}/knowledge/sources", who,
                                 {"name": "Help site", "website": {"url": url, **extra}})

    async def source(self, sid):
        return (await self.w.call("GET", f"/knowledge/sources/{sid}", "admin")).json()

    async def documents(self, sid):
        return (await self.w.call("GET", f"/knowledge/sources/{sid}/documents", "admin")).json()["documents"]



class WebsiteSourceTests(WebsiteBase):
    async def test_import_status_content_and_strict_mode_answer(self):
        r = await self.create()
        self.assertEqual(r.status_code, 201, r.text)
        src = r.json()
        self.assertEqual((src["kind"], src["website"]["url"], src["website"]["state"]),
                         ("website", "https://docs.example.com/refunds", "queued"))
        self.assertEqual(self.web.requests, [])                    # never fetched inside the API request
        self.assertEqual(await self.importer.run_pending(), 1)
        src = await self.source(src["id"])
        self.assertEqual((src["website"]["state"], src["website"]["pages_imported"]), ("done", 1))
        self.assertIsNotNone(src["website"]["last_success_at"])
        self.assertIsNone(src["website"]["error"])
        docs = await self.documents(src["id"])
        self.assertEqual(len(docs), 1)
        self.assertEqual((docs[0]["status"], docs[0]["url"]), ("ready", "https://docs.example.com/refunds"))
        content = (await self.w.call("GET", f"/knowledge/documents/{docs[0]['id']}/content", "viewer")).json()
        self.assertIn("refund within 21 days", content["text"])
        self.assertNotIn("steal", content["text"])                 # scripts are never imported
        # approve the source for the bot and ask in strict mode: answered from the website, with citation
        from tests.unit.test_knowledge import KnowledgeBase
        await KnowledgeBase.approve(self, [src["id"]])
        import httpx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            r = await c.post(f"/api/public/instances/{self.w.iA_web.channel.public_id}/chat",
                             json={"message": "Within how many days can customers request a refund?"},
                             headers={"Origin": "https://shop.example"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("21 days", r.json()["reply"])
        self.assertNotEqual(r.json()["reply"], self.FALLBACK)

    async def test_refresh_updates_changed_pages_and_keeps_unchanged_ones(self):
        sid = (await self.create()).json()["id"]
        await self.importer.run_pending()
        doc = (await self.documents(sid))[0]
        r = await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "admin")
        self.assertEqual((r.status_code, r.json()["website"]["state"]), (200, "queued"))
        self.assertEqual((await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "admin")).status_code, 409)
        await self.importer.run_pending()
        same = (await self.documents(sid))[0]
        self.assertEqual((same["id"], same["content_version"]), (doc["id"], doc["content_version"]))
        self.web.page("https://docs.example.com/refunds", "<p>Customers can request a refund within 30 days.</p>")
        await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "admin")
        await self.importer.run_pending()
        new = (await self.documents(sid))[0]
        self.assertEqual(new["content_version"], doc["content_version"] + 1)
        text = (await self.w.call("GET", f"/knowledge/documents/{new['id']}/content", "admin")).json()["text"]
        self.assertIn("30 days", text)
        self.assertNotIn("21 days", text)

    async def test_failed_refresh_shows_the_error_and_keeps_the_previous_content(self):
        sid = (await self.create()).json()["id"]
        await self.importer.run_pending()
        first = await self.source(sid)
        self.web.page("https://docs.example.com/refunds", "gone", status=404)
        await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "admin")
        await self.importer.run_pending()
        src = await self.source(sid)
        self.assertEqual(src["website"]["state"], "failed")
        self.assertIn("not found (HTTP 404)", src["website"]["error"])
        self.assertEqual(src["website"]["last_success_at"], first["website"]["last_success_at"])
        docs = await self.documents(sid)
        self.assertEqual([d["status"] for d in docs], ["ready"])   # the old page is still answerable

    async def test_unreachable_or_private_site_fails_with_a_clear_error(self):
        sid = (await self.create(url="https://evil.example.org/")).json()["id"]
        await self.importer.run_pending()
        src = await self.source(sid)
        self.assertEqual(src["website"]["state"], "failed")
        self.assertIn("private or internal network", src["website"]["error"])
        self.assertEqual(await self.documents(sid), [])

    async def test_changing_the_url_replaces_the_old_page(self):
        sid = (await self.create()).json()["id"]
        await self.importer.run_pending()
        self.web.page("https://docs.example.com/shipping", "<p>Shipping takes 3 days.</p>")
        r = await self.w.call("PATCH", f"/knowledge/sources/{sid}", "admin",
                              {"website": {"url": "https://docs.example.com/shipping"}})
        self.assertEqual(r.status_code, 200, r.text)
        await self.importer.run_pending()
        docs = await self.documents(sid)
        self.assertEqual([d["url"] for d in docs], ["https://docs.example.com/shipping"])

    async def test_remove_website_source_deletes_its_pages(self):
        sid = (await self.create()).json()["id"]
        await self.importer.run_pending()
        self.assertEqual((await self.w.call("DELETE", f"/knowledge/sources/{sid}", "admin")).status_code, 200)
        self.assertEqual(await self.w.store.find("knowledge_documents", {"source_id": sid}), [])
        self.assertEqual(await self.w.store.find("knowledge_chunks", {"source_id": sid}), [])

    async def test_validation_permissions_and_tenancy(self):
        self.assertEqual((await self.create(url="ftp://docs.example.com/")).status_code, 422)
        self.assertEqual((await self.create(mode="everything")).status_code, 422)
        self.assertEqual((await self.create(mode="crawl", max_pages=5000)).status_code, 422)
        self.assertEqual((await self.create(who="viewer")).status_code, 403)
        self.assertIn((await self.create(who="owner_b")).status_code, (403, 404))
        sid = (await self.create()).json()["id"]
        self.assertIn((await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "owner_b")).status_code,
                      (403, 404))
        self.assertEqual((await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "viewer")).status_code, 403)
        await self.importer.run_pending()
        did = (await self.documents(sid))[0]["id"]
        self.assertIn((await self.w.call("GET", f"/knowledge/documents/{did}/content", "owner_b")).status_code,
                      (403, 404))
        # uploads into a website source are refused; document sources cannot be refreshed
        import httpx
        h = {**self.w.headers("admin"), "Content-Type": "text/plain", "X-Requested-With": "BitBotPro"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.w.app), base_url="http://test") as c:
            r = await c.put(f"/api/knowledge/sources/{sid}/documents/x.txt", content=b"x", headers=h)
        self.assertEqual(r.status_code, 409)
        doc_src = (await self.w.call("POST", f"/enterprises/{self.w.eA.id}/knowledge/sources", "admin",
                                     {"name": "Docs"})).json()
        self.assertEqual(doc_src["kind"], "document")
        self.assertEqual((await self.w.call("POST", f"/knowledge/sources/{doc_src['id']}/refresh",
                                            "admin")).status_code, 422)
        self.assertEqual((await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "admin")).status_code, 200)
        events = [e["action"] for e in await self.w.store.find("audit_events", {"enterprise_id": self.w.eA.id})]
        self.assertIn("knowledge.website_refresh", events)

    async def test_organisation_website_source(self):
        r = await self.create(owner=f"organisations/{self.w.oA.id}")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["organisation_id"], self.w.oA.id)

    async def test_disabled_source_is_not_fetched(self):
        sid = (await self.create()).json()["id"]
        await self.w.call("PATCH", f"/knowledge/sources/{sid}", "admin", {"status": "disabled"})
        self.assertEqual(await self.importer.run_pending(), 0)
        self.assertEqual(self.web.requests, [])



@unittest.skipUnless(HAVE_API, "FastAPI/httpx not installed (runs in the Docker test target)")
class CrawlTests(WebsiteBase):
    """R2: bounded same-site crawl with robots.txt and sitemap discovery."""

    def site(self):
        P = "https://docs.example.com"
        self.web.page(P + "/robots.txt", "User-agent: *\nDisallow: /private\nCrawl-delay: 2\n"
                      "Sitemap: https://docs.example.com/sitemap.xml\n", ctype="text/plain")
        self.web.page(P + "/sitemap.xml", "<urlset><url><loc>https://docs.example.com/faq</loc></url>"
                      "<url><loc>https://other.example.net/x</loc></url></urlset>", ctype="application/xml")
        self.web.page(P + "/", '<p>Home page.</p><a href="/about">About</a> <a href="/private/a">P</a> '
                      '<a href="https://other.example.net/">Elsewhere</a> <a href="/logo.png">logo</a> '
                      '<a href="/about?utm_source=x#team">About again</a> <a href="mailto:x@y.z">mail</a>')
        self.web.page(P + "/about", '<p>About us.</p><a href="/contact">Contact</a>')
        self.web.page(P + "/contact", "<p>Contact page.</p>")
        self.web.page(P + "/faq", '<p>FAQ page.</p><meta name="robots" content="nofollow"><a href="/hidden">h</a>')
        self.web.page(P + "/hidden", "<p>Hidden.</p>")
        self.web.page(P + "/private/a", "<p>Private.</p>")
        self.slept = []

        async def sleep(seconds):
            self.slept.append(seconds)
        self.importer.sleep = sleep
        self.importer.delay = 0.5

    async def urls(self, sid):
        return sorted(d["url"] for d in await self.documents(sid))

    async def test_crawl_follows_permitted_same_site_links_and_the_sitemap(self):
        self.site()
        r = await self.create(url="https://docs.example.com/", mode="crawl", max_pages=10)
        self.assertEqual(r.json()["website"]["max_pages"], 10)
        await self.importer.run_pending()
        P = "https://docs.example.com"
        self.assertEqual(await self.urls(r.json()["id"]), [P + "/", P + "/about", P + "/contact", P + "/faq"])
        fetched = {t for h, t, _ in self.web.requests}
        self.assertNotIn("/private/a", fetched)                     # robots.txt Disallow
        self.assertNotIn("/hidden", fetched)                        # nofollow page
        self.assertNotIn("/logo.png", fetched)
        self.assertTrue(all(h.endswith("docs.example.com") for h, _, _ in self.web.requests))   # never off-site
        self.assertEqual(set(self.slept), {2.0})                    # Crawl-delay honoured between pages
        src = await self.source(r.json()["id"])
        self.assertEqual((src["website"]["state"], src["website"]["pages_imported"]), ("done", 4))

    async def test_the_page_limit_bounds_the_crawl(self):
        self.site()
        sid = (await self.create(url="https://docs.example.com/", mode="crawl", max_pages=2)).json()["id"]
        await self.importer.run_pending()
        self.assertEqual(len(await self.documents(sid)), 2)
        pages = [t for h, t, _ in self.web.requests if t not in ("/robots.txt", "/sitemap.xml")]
        self.assertEqual(len(pages), 2)

    async def test_refresh_removes_pages_that_disappeared_and_keeps_failing_ones(self):
        self.site()
        sid = (await self.create(url="https://docs.example.com/", mode="crawl", max_pages=10)).json()["id"]
        await self.importer.run_pending()
        P = "https://docs.example.com"
        self.web.page(P + "/about", "<p>About us.</p>")             # the contact link is gone
        self.web.page(P + "/faq", "down", status=503)               # temporarily failing
        await self.w.call("POST", f"/knowledge/sources/{sid}/refresh", "admin")
        await self.importer.run_pending()
        self.assertEqual(await self.urls(sid), [P + "/", P + "/about", P + "/faq"])
        src = await self.source(sid)
        self.assertEqual(src["website"]["state"], "done")
        self.assertIn("server error (HTTP 503). Try again later. (https://docs.example.com/faq)",
                      src["website"]["error"])

    async def test_missing_robots_and_sitemap_still_crawl(self):
        P = "https://docs.example.com"
        self.web.page(P + "/", '<p>Home.</p><a href="/a">a</a>')
        self.web.page(P + "/a", "<p>A.</p>")
        sid = (await self.create(url=P + "/", mode="crawl", max_pages=5)).json()["id"]
        await self.importer.run_pending()
        self.assertEqual(await self.urls(sid), [P + "/", P + "/a"])

    async def test_noindex_pages_are_not_imported(self):
        P = "https://docs.example.com"
        self.web.page(P + "/", '<p>Home.</p><a href="/secret">s</a>')
        self.web.page(P + "/secret", '<meta name="robots" content="noindex"><p>Secret.</p>')
        sid = (await self.create(url=P + "/", mode="crawl", max_pages=5)).json()["id"]
        await self.importer.run_pending()
        self.assertEqual(await self.urls(sid), [P + "/"])

    def test_link_canonicalisation(self):
        from runtime.website import crawl_url
        start = "https://docs.example.com/help/"
        self.assertEqual(crawl_url("../a?utm_source=x&id=2#f", start), "https://docs.example.com/a?id=2")
        self.assertEqual(crawl_url("http://www.docs.example.com/b", start), "https://www.docs.example.com/b")
        for bad in ("https://other.example.net/", "/x.pdf.zip", "/img.JPG", "javascript:alert(1)", "mailto:a@b.c"):
            self.assertIsNone(crawl_url(bad, start), bad)


if __name__ == "__main__":
    unittest.main()
