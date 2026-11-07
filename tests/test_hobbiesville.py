import os
import sys
import json
import asyncio
import tempfile
import unittest

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
        self.assertEqual(alerts[1]["url"], URL)
        self.assertEqual(s.urls[0], URL + ".js")

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
