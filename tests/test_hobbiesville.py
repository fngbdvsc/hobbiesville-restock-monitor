import os
import sys
import json
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import hobbiesville_monitor as hm  # noqa: E402

URL = "https://hobbiesville.com/products/pokemon-151-booster-bundle"
HANDLE = "pokemon-151-booster-bundle"


def js_product(available=True, price=4999, title="Pokemon 151 Booster Bundle", variants=None, tags=None):
    """A fake Shopify /products/<handle>.js answer (prices in cents)."""
    return json.dumps({
        "id": 1, "title": title, "handle": HANDLE, "tags": tags or [],
        "featured_image": "//hobbiesville.com/cdn/shop/files/151.jpg",
        "available": available,
        "variants": variants or [{"id": 11, "title": "Default Title", "price": price,
                                  "compare_at_price": None, "available": available}],
    })


CLOUDFLARE = ("<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
              "<div id='challenge-platform'>Checking your browser before accessing hobbiesville.com</div>"
              "<script>var cf_chl_opt={};</script></body></html>")
NOT_FOUND = "<html><head><title>404 Not Found</title></head><body>Page not found. Try our captcha-free search.</body></html>"
STORE_PAGE = "<html><head><title>Hobbiesville</title></head><body>Welcome</body></html>"


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)   # each: (status, text, final_url) or an Exception
        self.urls = []
        self.closed = False

    async def get(self, url, headers=None):
        self.urls.append(url)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        status, text, final = r
        return hm.PageResult(text, status, final or url)

    async def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def make_monitor(session, **kw):
    for f in (hm.STATE_FILE, hm.CONFIG_FILE):
        if os.path.exists(f):
            os.remove(f)
    alerts, texts = [], []
    clock = Clock()

    async def send_alert(a):
        alerts.append(a)

    async def send_text(t):
        texts.append(t)

    kw.setdefault("collections", [])
    mon = hm.Monitor(session, send_alert, send_text, clock=clock, sleep=clock.sleep,
                     rand=lambda a, b: a, **kw)
    return mon, alerts, texts, clock


def run(coro):
    return asyncio.run(coro)


class TestParseHandle(unittest.TestCase):
    def test_links(self):
        self.assertEqual(hm.parse_handle(URL), HANDLE)
        self.assertEqual(hm.parse_handle(URL + "?variant=4242"), HANDLE)
        self.assertEqual(hm.parse_handle("https://www.hobbiesville.com/collections/pokemon/products/" + HANDLE), HANDLE)
        self.assertEqual(hm.parse_handle("hobbiesville.com/products/" + HANDLE + "/"), HANDLE)
        self.assertEqual(hm.parse_handle(URL + ".js"), HANDLE)
        self.assertEqual(hm.parse_handle("<" + URL + ">"), HANDLE)
        self.assertEqual(hm.parse_handle(HANDLE), HANDLE)
        self.assertEqual(hm.parse_handle("Pokemon-151-Booster-Bundle"), HANDLE)

    def test_rejects(self):
        self.assertIsNone(hm.parse_handle("https://www.amazon.ca/dp/B0ABCD1234"))
        self.assertIsNone(hm.parse_handle("https://hobbiesville.com/collections/pokemon"))
        self.assertIsNone(hm.parse_handle("https://evil-hobbiesville.com/products/x"))
        self.assertIsNone(hm.parse_handle(""))
        self.assertIsNone(hm.parse_handle("two words"))


class TestParseProduct(unittest.TestCase):
    def test_in_stock_js(self):
        v = hm.parse_product_json(js_product(True, 4999))
        self.assertTrue(v["in_stock"])
        self.assertEqual(v["price"], 49.99)
        self.assertIsNone(v["option"])
        self.assertEqual(v["image"], "https://hobbiesville.com/cdn/shop/files/151.jpg")
        self.assertTrue(hm.is_ok(v))
        self.assertIn("In stock on Hobbiesville at $49.99", hm.reason_text(v, True))

    def test_sold_out(self):
        v = hm.parse_product_json(js_product(False))
        self.assertFalse(v["in_stock"])
        self.assertFalse(hm.is_ok(v))
        self.assertIn("Sold out", hm.reason_text(v, False))

    def test_json_format_dollars(self):
        data = {"product": {"title": "Elite Trainer Box", "image": {"src": "https://cdn.x/etb.jpg"},
                            "variants": [{"title": "Default Title", "price": "64.99", "available": True}]}}
        v = hm.parse_product_json(json.dumps(data))
        self.assertEqual(v["price"], 64.99)
        self.assertTrue(v["in_stock"])
        self.assertEqual(v["image"], "https://cdn.x/etb.jpg")

    def test_unknown_stock_does_not_alert(self):
        data = {"product": {"title": "Box", "variants": [{"title": "Default Title", "price": "10.00"}]}}
        v = hm.parse_product_json(json.dumps(data))
        self.assertFalse(hm.is_ok(v))
        self.assertIn("doesn't say", hm.reason_text(v, False))

    def test_multi_variant_cheapest_available(self):
        variants = [
            {"title": "Single pack", "price": 699, "available": False},
            {"title": "Case of 6", "price": 3999, "available": True},
            {"title": "Case of 12", "price": 7499, "available": True, "compare_at_price": 8999},
        ]
        v = hm.parse_product_json(js_product(True, variants=variants))
        self.assertEqual(v["price"], 39.99)
        self.assertEqual(v["option"], "Case of 6")
        self.assertEqual((v["options_in_stock"], v["options_total"]), (2, 3))
        self.assertIn("2 of 3 options in stock", hm.reason_text(v, True))

    def test_compare_at_and_preorder(self):
        variants = [{"title": "Default Title", "price": 5999, "compare_at_price": 6999, "available": True}]
        v = hm.parse_product_json(js_product(True, variants=variants, title="Mega Evolution ETB (Pre-Order)"))
        self.assertEqual(v["compare_at"], 69.99)
        self.assertTrue(v["preorder"])
        r = hm.reason_text(v, True)
        self.assertIn("was $69.99", r)
        self.assertIn("pre-order", r)

    def test_garbage(self):
        for bad in ("", "not json", "[]", '{"title": "x"}', STORE_PAGE, CLOUDFLARE):
            self.assertIsNone(hm.parse_product_json(bad))


class TestClassify(unittest.TestCase):
    def test_ok(self):
        kind, v = hm.classify_response(200, js_product(), URL + ".js")
        self.assertEqual(kind, "ok")
        self.assertEqual(v["name"], "Pokemon 151 Booster Bundle")

    def test_404_never_a_block(self):
        self.assertEqual(hm.classify_response(404, NOT_FOUND, URL + ".js")[0], "missing")

    def test_rate_limit(self):
        self.assertEqual(hm.classify_response(429, "Too Many Requests", URL + ".js")[0], "blocked")

    def test_cloudflare(self):
        kind, detail = hm.classify_response(403, CLOUDFLARE, URL + ".js")
        self.assertEqual(kind, "blocked")
        self.assertIn("Just a moment", detail)
        self.assertEqual(hm.classify_response(200, CLOUDFLARE, URL + ".js")[0], "blocked")

    def test_normal_page_with_cloudflare_scripts_is_not_a_check(self):
        home = ("<html><head><title>Hobbiesville</title>"
                "<script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script>"
                "<script src='https://www.google.com/recaptcha/api.js'></script></head>"
                "<body>Canada's Premier Hobby Shop</body></html>")
        self.assertFalse(hm.looks_like_check(200, home))
        self.assertTrue(hm.looks_like_check(200, CLOUDFLARE))
        self.assertTrue(hm.looks_like_check(429, ""))

    def test_password_page(self):
        self.assertEqual(hm.classify_response(200, STORE_PAGE, "https://hobbiesville.com/password")[0], "password")

    def test_redirected_somewhere_else(self):
        kind, detail = hm.classify_response(200, STORE_PAGE, "https://hobbiesville.com/")
        self.assertEqual(kind, "unreadable")
        self.assertIn("sent me to https://hobbiesville.com/", detail)


class TestMonitor(unittest.TestCase):
    def test_alert_once_then_rearm(self):
        s = FakeSession([(200, js_product(True), None)])
        mon, alerts, _, clock = make_monitor(s)
        mon.watch = [HANDLE]

        async def go():
            await mon.check(HANDLE)              # in stock -> alert
            await mon.check(HANDLE)              # still in stock -> silent
            s.responses = [(200, js_product(False), None)]
            await mon.check(HANDLE)              # sold out -> re-arm, no alert
            s.responses = [(200, js_product(True, 5499), None)]
            await mon.check(HANDLE)              # back -> alert again
        run(go())
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0]["kind"], "Restock")
        self.assertIn("First check", alerts[0]["why"])
        self.assertIn("$54.99", alerts[1]["why"])
        self.assertIn("Before: Sold out", alerts[1]["why"])
        self.assertEqual(alerts[1]["url"], hm.product_url(HANDLE))
        self.assertEqual(s.urls[0], "https://www.hobbiesville.com/products/" + HANDLE + ".js")

    def test_sold_out_never_alerts(self):
        s = FakeSession([(200, js_product(False), None)])
        mon, alerts, _, _ = make_monitor(s)
        run(mon.check(HANDLE))
        self.assertEqual(alerts, [])
        self.assertIn("Sold out", mon.reasons[HANDLE][0])

    def test_404_reason_and_error_no_alert(self):
        s = FakeSession([(404, NOT_FOUND, None)])
        errors = []

        async def send_error(t):
            errors.append(t)
        mon, alerts, _, _ = make_monitor(s, send_error=send_error)

        async def go():
            await mon.check(HANDLE)
            await asyncio.sleep(0)
        run(go())
        self.assertEqual(alerts, [])
        self.assertFalse(mon.was_blocked)
        self.assertIn("doesn't exist (HTTP 404)", mon.reasons[HANDLE][0])
        self.assertEqual(len(errors), 1)

    def test_network_error_is_not_a_block(self):
        s = FakeSession([ConnectionError("timed out")])
        mon, alerts, _, _ = make_monitor(s)
        run(mon.check(HANDLE))
        self.assertEqual(alerts, [])
        self.assertIn("ConnectionError", mon.reasons[HANDLE][0])

    def test_block_backoff_fresh_session_and_recovery(self):
        blocked = FakeSession([(429, "Too Many Requests", None)])
        fresh = FakeSession([(200, js_product(True), None)])
        mon, alerts, texts, clock = make_monitor(blocked, session_factory=lambda: fresh)
        mon.watch = [HANDLE]

        async def go():
            await mon.tick()                  # builds a round, gets blocked
            self.assertTrue(mon.was_blocked)
            self.assertTrue(blocked.closed)   # old connection and cookies thrown away
            self.assertIs(mon.session, fresh)
            self.assertEqual(mon.blocked_until - clock(), 300)
            self.assertEqual(mon.backoff, 600)
            self.assertIn("pausing ALL requests for 5 min", texts[-1])
            n = len(fresh.urls)
            await mon.tick()                  # still inside the pause: no request
            self.assertEqual(len(fresh.urls), n)
            for _ in range(200):
                if not mon.was_blocked:
                    break
                await mon.tick()
        run(go())
        self.assertFalse(mon.was_blocked)
        self.assertEqual(mon.backoff, 300)
        self.assertIn("Back to normal", texts[-1])
        self.assertEqual(len(alerts), 1)      # the retried check alerted, and the block itself never did
        self.assertGreaterEqual(clock() - 1000, 300)

    def test_spacing_between_requests(self):
        s = FakeSession([(200, js_product(False), None)])
        mon, _, _, clock = make_monitor(s)
        mon.watch = ["a", "b", "c"]
        times = []
        orig = s.get

        async def timed(url, headers=None):
            times.append(clock())
            return await orig(url, headers)
        s.get = timed

        async def go():
            for _ in range(10):
                await mon.tick()
        run(go())
        self.assertGreaterEqual(len(times), 3)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, hm.GAP_MIN)

    def test_round_minimum(self):
        s = FakeSession([(200, js_product(False), None)])
        mon, _, _, clock = make_monitor(s)
        mon.watch = ["a"]
        times = []
        orig = s.get

        async def timed(url, headers=None):
            times.append(clock())
            return await orig(url, headers)
        s.get = timed

        async def go():
            for _ in range(40):
                await mon.tick()
        run(go())
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, hm.MIN_SECONDS_PER_ROUND)

    def test_apply_config_filters_store(self):
        s = FakeSession([(200, js_product(False), None)])
        mon, _, _, _ = make_monitor(s, dashboard=object())
        mon.apply_config({"paused": False, "skus": [
            {"store": "walmart", "sku": "625FHPXN8BK2"},
            {"store": "amazon", "sku": "B0ABCD1234"},
            {"store": "Hobbiesville", "sku": URL + "?variant=1", "label": "151 bundle"},
            {"store": "hobbiesville", "sku": "https://www.amazon.ca/dp/B0ABCD1234"},
        ]})
        self.assertEqual(mon.watch, [HANDLE])
        self.assertEqual(mon.names[HANDLE], "151 bundle")
        self.assertEqual(mon.orig[HANDLE], URL + "?variant=1")
        mon.apply_config({"paused": True, "skus": []})
        self.assertTrue(mon.paused)
        self.assertEqual(mon.watch, [])

    def test_paused_makes_no_requests(self):
        s = FakeSession([(200, js_product(True), None)])
        mon, alerts, _, _ = make_monitor(s)
        mon.watch = [HANDLE]
        mon.paused = True

        async def go():
            for _ in range(5):
                await mon.tick()
        run(go())
        self.assertEqual(s.urls, [])

    def test_commands(self):
        mon, _, _, _ = make_monitor(FakeSession([(200, js_product(), None)]))
        self.assertIn("Added", mon.command("!watch " + URL))
        self.assertEqual(mon.watch, [HANDLE])
        self.assertIn("Already watching", mon.command("!watch " + HANDLE))
        self.assertIn("Not a Hobbiesville", mon.command("!watch https://www.amazon.ca/dp/B0ABCD1234"))
        self.assertIn("haven't checked", mon.command("!why " + URL))
        self.assertIn("Removed", mon.command("!remove " + URL))
        self.assertIn("Commands", mon.command("!help"))
        self.assertIn("running", mon.command("!status"))
        self.assertIsNone(mon.command("hello"))
        dash, _, _, _ = make_monitor(FakeSession([(200, js_product(), None)]), dashboard=object())
        self.assertIn("dashboard controls", dash.command("!watch " + URL))


USER_LINK = ("https://www.hobbiesville.com/collections/pokemon-trading-cards?_su_rec=Gaq0qHa4fSC9JyMc"
             "&_su_rec_id=f97bebf2-6b9a-494b-8dcc-29967430d12d-1794050472")
COLL = "pokemon-trading-cards"


def iso_hours_ago(h):
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def coll_product(handle, available, price="49.99", title=None, hours_ago=24 * 30, product_type="Sealed"):
    """One product the way /collections/<name>/products.json lists it (price in dollars, as text)."""
    return {"handle": handle, "title": title or handle.replace("-", " ").title(),
            "product_type": product_type, "published_at": iso_hours_ago(hours_ago),
            "images": [{"src": "https://cdn.shopify.com/" + handle + ".jpg"}],
            "variants": [{"title": "Default Title", "price": price, "available": available}]}


def coll_page(products):
    return (200, json.dumps({"products": products}), None)


class TestCollectionScan(unittest.TestCase):
    def test_parse_collection(self):
        self.assertEqual(hm.parse_collection(USER_LINK), COLL)
        self.assertEqual(hm.parse_collection(COLL), COLL)
        self.assertIsNone(hm.parse_collection("0"))
        self.assertIsNone(hm.parse_collection("https://www.amazon.ca/collections/x"))
        self.assertEqual(hm.scan_collections(f"{USER_LINK}, pokemon-booster-boxes ,0"),
                         [COLL, "pokemon-booster-boxes"])
        self.assertEqual(hm.scan_collections("0"), [])
        self.assertEqual(hm.collection_data_url(COLL, 2),
                         "https://www.hobbiesville.com/collections/pokemon-trading-cards/products.json?limit=250&page=2")

    def test_classify_collection(self):
        self.assertEqual(hm.classify_collection(*coll_page([coll_product("a", True)])[:2], "")[0], "ok")
        self.assertEqual(hm.classify_collection(404, NOT_FOUND, "")[0], "missing")
        self.assertEqual(hm.classify_collection(403, CLOUDFLARE, "")[0], "blocked")
        self.assertEqual(hm.classify_collection(200, js_product(), "")[0], "unreadable")

    def test_first_scan_learns_then_alerts(self):
        s = FakeSession([coll_page([coll_product("etb", False), coll_product("bundle", True)])])
        mon, alerts, _, _ = make_monitor(s)

        async def go():
            await mon.scan(COLL, 1)                     # first read: learn only
            self.assertEqual(alerts, [])
            s.responses = [coll_page([
                coll_product("etb", True, "64.99"),      # was sold out -> Restock
                coll_product("bundle", True),            # still in stock -> silent
                coll_product("brand-new-box", True, hours_ago=2),   # just published -> New Item
                coll_product("old-box", True),           # first seen but published long ago -> silent
                coll_product("charizard-ex-single", True, product_type="Pokemon Single"),  # single -> skipped
                coll_product("pikachu-sleeves", True, hours_ago=1),  # accessory -> skipped
            ])]
            await mon.scan(COLL, 1)
        run(go())
        kinds = sorted((a["kind"], a["sku"]) for a in alerts)
        self.assertEqual(kinds, [("New Item", "brand-new-box"), ("Restock", "etb")])
        restock = [a for a in alerts if a["kind"] == "Restock"][0]
        self.assertIn("collection scan", restock["why"])
        self.assertIn("$64.99", restock["why"])
        self.assertEqual(restock["url"], "https://www.hobbiesville.com/products/etb")
        self.assertEqual(restock["v"]["image"], "https://cdn.shopify.com/etb.jpg")
        new = [a for a in alerts if a["kind"] == "New Item"][0]
        self.assertIn("published 2 h ago", new["why"])

    def test_404_suggests_close_match_and_find(self):
        real = "pokemon-tcg-30th-celebration-mew-premium-collection"
        s = FakeSession([coll_page([coll_product(real, False, title="Pokemon TCG: 30th Celebration Mew Premium Collection"),
                                    coll_product("mega-evolution-etb", True)])])
        mon, _, _, _ = make_monitor(s)

        async def go():
            await mon.scan(COLL, 1)
            s.responses = [(404, NOT_FOUND, None)]
            await mon.check("pokemon-30th-celebration-mew-collection-pre-order")
        run(go())
        self.assertIn(f"Did you mean `{real}`", mon.reasons["pokemon-30th-celebration-mew-collection-pre-order"][0])
        self.assertNotIn("mega-evolution-etb", mon.reasons["pokemon-30th-celebration-mew-collection-pre-order"][0])
        reply = mon.command("!find mew")
        self.assertIn(real, reply)
        self.assertIn("Sold out", reply)
        self.assertIn("Found 1", reply)
        self.assertIn("No product", mon.command("!find charizard"))

    def test_saved_memory_means_no_learning_scan(self):
        s = FakeSession([coll_page([coll_product("etb", True)])])
        mon, alerts, _, _ = make_monitor(s)
        mon.fresh_start = False
        mon.state = {"stock:etb": False}
        run(mon.scan(COLL, 1))
        self.assertEqual([a["kind"] for a in alerts], ["Restock"])

    def test_scan_and_watch_share_one_alert(self):
        s = FakeSession([coll_page([coll_product(HANDLE, False)])])
        mon, alerts, _, _ = make_monitor(s)

        async def go():
            await mon.scan(COLL, 1)
            s.responses = [coll_page([coll_product(HANDLE, True)])]
            await mon.scan(COLL, 1)                     # restock alert from the scan
            s.responses = [(200, js_product(True), None)]
            await mon.check(HANDLE)                     # same restock seen on the product: no second alert
        run(go())
        self.assertEqual(len(alerts), 1)

    def test_full_page_queues_next_page(self):
        full = [coll_product(f"p{i}", False) for i in range(250)]
        s = FakeSession([coll_page(full)])
        mon, _, _, _ = make_monitor(s)
        run(mon.scan(COLL, 1))
        self.assertEqual(list(mon.round)[0], ("scan", COLL, 2))
        s.responses = [coll_page(full[:10])]
        run(mon.scan(COLL, 2))
        self.assertEqual(mon.scan_counts[COLL], 260)

    def test_missing_collection_reports_error(self):
        errors = []

        async def send_error(t):
            errors.append(t)
        mon, alerts, _, _ = make_monitor(FakeSession([(404, NOT_FOUND, None)]), send_error=send_error)

        async def go():
            await mon.scan(COLL, 1)
            await asyncio.sleep(0)
        run(go())
        self.assertEqual(alerts, [])
        self.assertIn("doesn't exist", errors[0])

    def test_block_on_scan_backs_off_and_retries(self):
        blocked = FakeSession([(429, "Too Many Requests", None)])
        fresh = FakeSession([coll_page([coll_product("etb", False)])])
        mon, _, texts, _ = make_monitor(blocked, session_factory=lambda: fresh, collections=[COLL])

        async def go():
            await mon.tick()
            self.assertTrue(mon.was_blocked)
            self.assertIn("collection list", texts[-1])
            for _ in range(200):
                if not mon.was_blocked:
                    break
                await mon.tick()
        run(go())
        self.assertFalse(mon.was_blocked)
        self.assertIn("/collections/pokemon-trading-cards/products.json", fresh.urls[0])
        self.assertIn(("pokemon-trading-cards", 1), mon.baselined)


class TestDashboard(unittest.TestCase):
    def test_headers_and_paths(self):
        calls = []

        class S:
            async def get(self, url, headers=None):
                calls.append(("get", url, headers))
                return hm.PageResult('{"paused": false, "skus": []}', 200, url)

            async def post_json(self, url, headers, body):
                calls.append(("post", url, headers, body))
                return hm.PageResult("{}", 200, url)

        d = hm.Dashboard("https://pokepulse-stock-scout.lovable.app/", "KEY", session=S())

        async def go():
            cfg = await d.get_config()
            await d.post_log("message", "[Hobbiesville] Restock", "x")
            return cfg
        self.assertEqual(run(go()), {"paused": False, "skus": []})
        self.assertEqual(calls[0][1], "https://pokepulse-stock-scout.lovable.app/api/public/bot/config")
        self.assertEqual(calls[0][2]["x-bot-store"], "hobbiesville")
        self.assertEqual(calls[1][3], {"kind": "message", "content": "[Hobbiesville] Restock", "sku": "x"})


if __name__ == "__main__":
    unittest.main()
