"""
Hobbiesville monitor for Discord. A separate bot from the Walmart and Amazon ones (own repo, own Railway project).

LIGHT version: no Chrome, no Playwright, no virtual screen. Hobbiesville runs on Shopify, and every Shopify
product page has its data at <product link>.js (name, price, and whether each option can be bought).
The bot reads that with plain web requests, so it needs very little memory.

- Every request to Hobbiesville is spaced 8-15 seconds apart (random).
- An alert posts once when a watched product comes into stock, and re-arms when it sells out.
- It always tells you WHY (in alerts, status messages, and with !why).
- If DASHBOARD_URL and BOT_API_KEY are set, the dashboard supplies the product list (entries whose store
  starts with "hobbiesville") and the pause state, and gets a copy of every alert, error and status message.
- If Hobbiesville ever answers with a bot check or "too many requests", the bot pauses all requests and retries
  later. It never tries to get past the check.

Setup:              python3 -m pip install -r requirements.txt
Check one product:  python3 hobbiesville_monitor.py debug https://hobbiesville.com/products/<name>
Check a saved file: python3 hobbiesville_monitor.py parse saved.js
Run:                python3 hobbiesville_monitor.py

IMPORTANT for commands: in the Discord Developer Portal, open your app -> Bot ->
turn ON "Message Content Intent", then save. Without it the bot can't read commands.
"""
import os
import re
import sys
import json
import time
import random
import asyncio
import html as htmllib
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import unquote, urlsplit

# ====== PUT YOUR SETTINGS HERE (keep the quotes) ======
DISCORD_TOKEN = ""     # leave empty: set DISCORD_TOKEN in Railway Variables
CHANNEL_ID = ""        # leave empty: set CHANNEL_ID in Railway Variables
ROLE_ID = ""           # role to ping (leave "" for no ping)
PROXY = ""             # optional: http://user:pass@host:port (leave "" for none)
ALLOWED_USER_IDS = []  # optional: [123456789012345678]. Empty = anyone with "Manage Server" can use commands
# =======================================================

STORE = "Hobbiesville"
LOG_TAG = "[Hobbiesville] "    # put in front of every dashboard line, so it's clear which bot sent it
DASH_STORE = "hobbiesville"    # x-bot-store header + the dashboard store filter

GAP_MIN = 8                    # seconds between ANY two requests to Hobbiesville...
GAP_MAX = 15                   # ...random between these two numbers

# Each product is checked at most once per this many seconds. With a short list the bot idles between rounds.
MIN_SECONDS_PER_ROUND = int(os.environ.get("MIN_SECONDS_PER_ROUND", "60"))

# On a bot check / "too many requests": pause this many seconds, doubling each time up to BLOCK_RETRY_MAX.
BACKOFF_START = int(os.environ.get("BLOCK_RETRY_SECONDS", "300"))
BACKOFF_MAX = int(os.environ.get("BLOCK_RETRY_MAX", "1800"))
BLOCK_UPDATE_SECONDS = 600     # while a block lasts, post a "still blocked" update this often

# Starting watchlist when there's no dashboard (after that, use !watch / !remove in Discord - changes are saved)
WATCH_PRODUCTS = []

# On Railway, files vanish on every redeploy unless you attach a Volume (mount it at /data and set DATA_DIR=/data)
DATA_DIR = os.environ.get("DATA_DIR", ".")
STATE_FILE = os.path.join(DATA_DIR, "hobbiesville_state.json")
CONFIG_FILE = os.path.join(DATA_DIR, "hobbiesville_config.json")
BASE = "https://hobbiesville.com"
HOSTS = ("hobbiesville.com", "www.hobbiesville.com")

# An honest name for this bot. It doesn't pretend to be a browser.
USER_AGENT = "PokeCA-restock-monitor/1.0 (personal restock alerts, one request every 8-15 seconds)"

# With the dashboard connected, status messages go only to the dashboard and Discord gets only alerts.
# Set STATUS_IN_DISCORD=1 to also post them in Discord.
STATUS_IN_DISCORD = os.environ.get("STATUS_IN_DISCORD", "0") == "1"
CONFIG_EVERY_SECONDS = (50, 70)   # how often the bot asks the dashboard for its settings (random in this range)
ERROR_REPEAT_SECONDS = 600        # don't send the same error to the dashboard more often than this

LOCAL_TZ = ZoneInfo(os.environ.get("TIMEZONE", "America/Toronto"))   # Quebec time; Railway's clock is UTC
BLOCK_REASON = "Hobbiesville showed a bot check or 'too many requests', so this check was paused."


def local_now():
    return datetime.now(LOCAL_TZ)


class Blocked(Exception):
    """Hobbiesville (or its protection) refused the request instead of returning product data."""


# ------------------------------------------------------------ pure helpers
def get_proxy():
    """Return a usable proxy URL. Accepts both 'user:pass@host:port' and 'http://user:pass@host:port'."""
    raw = (os.environ.get("PROXY") or PROXY or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    return raw


HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,254}$")


def parse_handle(s):
    """Accepts a Hobbiesville product link or just its name part (the 'handle'). Returns the handle or None.
    https://hobbiesville.com/products/pokemon-151-booster-box?variant=1 -> pokemon-151-booster-box"""
    s = (s or "").strip().strip("`<>")
    if "/" in s or "." in s:
        u = urlsplit(s if "://" in s else "https://" + s)
        if (u.hostname or "").lower() not in HOSTS:
            return None
        m = re.search(r"/products/([^/?#]+)", u.path)
        if not m:
            return None
        s = unquote(m.group(1))
        for ext in (".js", ".json", ".oembed"):
            if s.endswith(ext):
                s = s[:-len(ext)]
    s = s.lower()
    return s if HANDLE_RE.match(s) else None


def product_url(handle):
    return f"{BASE}/products/{handle}"


def product_data_url(handle):
    return product_url(handle) + ".js"


def money(p):
    return f"${p:.2f}" if p is not None else "an unknown price"


def _price(raw):
    """Shopify's .js data gives prices in cents (4999). The .json data gives dollars as text ("49.99")."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return round(raw / 100.0, 2)
    try:
        return round(float(str(raw).replace(",", "")), 2)
    except ValueError:
        return None


def _image(data):
    img = data.get("featured_image")
    if isinstance(img, dict):
        img = img.get("src")
    if not img:
        im = data.get("image")
        img = im.get("src") if isinstance(im, dict) else None
    if not img:
        imgs = data.get("images") or []
        if imgs:
            img = imgs[0].get("src") if isinstance(imgs[0], dict) else imgs[0]
    if not isinstance(img, str) or not img:
        return None
    return "https:" + img if img.startswith("//") else img


PREORDER_WORDS = ("pre-order", "preorder", "pre order", "précommande", "precommande")


def parse_product_json(text):
    """Read Shopify product data (.js or .json format). None if it isn't readable product data."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("product"), dict):
        data = data["product"]
    if not isinstance(data, dict) or not data.get("title") or not isinstance(data.get("variants"), list):
        return None
    variants = []
    for v in data["variants"]:
        if not isinstance(v, dict):
            continue
        variants.append({
            "title": str(v.get("title") or ""),
            "price": _price(v.get("price")),
            "compare_at": _price(v.get("compare_at_price")),
            "available": v.get("available") is True,
            "known": isinstance(v.get("available"), bool),
        })
    if not variants:
        return None
    buyable = [v for v in variants if v["available"] and v["price"] is not None]
    pick = min(buyable, key=lambda v: v["price"]) if buyable else \
        min((v for v in variants if v["price"] is not None), key=lambda v: v["price"], default=variants[0])
    tags = data.get("tags") or []
    if isinstance(tags, str):
        tags = tags.split(",")
    words = (str(data["title"]) + " " + " ".join(str(t) for t in tags)).lower()
    option = pick["title"] if pick["title"].lower() != "default title" else None
    compare = pick["compare_at"] if pick["compare_at"] and pick["price"] and pick["compare_at"] > pick["price"] else None
    return {
        "name": " ".join(str(data["title"]).split()),
        "price": pick["price"],
        "compare_at": compare,
        "option": option,
        "in_stock": bool(buyable),
        "stock_known": any(v["known"] for v in variants),
        "options_total": len(variants),
        "options_in_stock": len(buyable),
        "preorder": any(w in words for w in PREORDER_WORDS),
        "image": _image(data),
    }


def page_title(text):
    m = re.search(r"<title[^>]*>(.*?)</title>", (text or "")[:6000], re.S | re.I)
    return " ".join(htmllib.unescape(m.group(1)).split())[:80] if m else ""


def page_excerpt(text, n=160):
    """The first visible words of a page, so a block message shows what the site actually displayed."""
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return " ".join(htmllib.unescape(t).split())[:n]


CHALLENGE_MARKERS = ("cf-chl", "challenge-platform", "just a moment", "attention required",
                     "verify you are human", "are you a robot", "captcha", "access denied",
                     "too many requests")


def classify_response(status, text, final_url=""):
    """What did Hobbiesville send back? Returns (kind, value):
    ("ok", product) / ("missing", None) / ("password", None) / ("blocked", detail) / ("unreadable", detail)"""
    path = urlsplit(final_url or "").path.lower().rstrip("/")
    if path.endswith("/password"):
        return "password", None
    if status == 404:
        return "missing", None          # a wrong link is never a bot check
    if status == 429:
        return "blocked", "HTTP 429 (too many requests)"
    v = parse_product_json(text) if status == 200 else None
    if v is not None:
        return "ok", v
    low = (text or "").lower()
    if status in (403, 503) or any(m in low for m in CHALLENGE_MARKERS):
        return "blocked", (f'HTTP {status}, page title "{page_title(text)}", '
                           f'page says: "{page_excerpt(text)}"')
    detail = f"HTTP {status}"
    if final_url and "/products/" not in path:
        detail += f", it sent me to {final_url}"
    return "unreadable", detail


def is_ok(v):
    """The one rule that decides an alert: readable, in stock, with a real price."""
    return bool(v and v.get("in_stock") and v.get("price") is not None)


def decide(prev, ok):
    """Alert once when it becomes buyable; silent while it stays buyable; re-arms when it sells out."""
    return "Restock" if ok and prev is not True else None


def reason_text(v, ok, status=None):
    if v is None and status == "missing":
        return ("Hobbiesville says this product doesn't exist (HTTP 404), so I won't alert. Check the link: "
                "it should look like hobbiesville.com/products/<name>.")
    if v is None and status == "password":
        return ("The Hobbiesville store is behind its password page right now (stores often do this "
                "just before a drop), so I can't read the product and won't alert.")
    if v is None:
        return (f"Couldn't read the product data ({status}), so I can't confirm the price and stock "
                f"and won't alert.")
    if ok:
        opt = f" ({v['option']})" if v.get("option") else ""
        was = f", was {money(v['compare_at'])}" if v.get("compare_at") else ""
        more = (f" {v['options_in_stock']} of {v['options_total']} options in stock."
                if v.get("options_total", 1) > 1 else "")
        pre = " Listed as a pre-order." if v.get("preorder") else ""
        return f"In stock on Hobbiesville at {money(v['price'])}{opt}{was}.{more}{pre}"
    if not v.get("stock_known"):
        return "The product data doesn't say whether it can be bought, so I won't alert."
    if not v.get("in_stock"):
        return "Sold out on Hobbiesville (every option shows as unavailable)."
    return "Shows as available, but I couldn't read the price, so I won't alert."


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(obj, f)
    except Exception as e:
        print(f"[warning] couldn't save {path}: {e}")


# ------------------------------------------------------------ plain web requests (no browser)
class PageResult:
    def __init__(self, text, status_code, url=""):
        self.text = text
        self.status_code = status_code
        self.url = url


class HttpSession:
    """Plain web requests with aiohttp (installed with discord.py). Cookies carry over between requests."""

    def __init__(self, proxy=None):
        self.proxy = proxy
        self._s = None

    async def _session(self):
        import aiohttp
        if self._s is None or self._s.closed:
            self._s = aiohttp.ClientSession(
                headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.8",
                         "Accept-Language": "en-CA,en;q=0.9"},
                timeout=aiohttp.ClientTimeout(total=20))
        return self._s

    async def get(self, url, headers=None):
        s = await self._session()
        async with s.get(url, headers=headers, proxy=self.proxy, allow_redirects=True) as r:
            return PageResult(await r.text(errors="replace"), r.status, str(r.url))

    async def post_json(self, url, headers, body):
        s = await self._session()
        async with s.post(url, headers=headers, json=body, proxy=None) as r:
            return PageResult(await r.text(errors="replace"), r.status, str(r.url))

    async def close(self):
        if self._s is not None and not self._s.closed:
            await self._s.close()
        self._s = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


def make_session(proxy=None):
    return HttpSession(proxy)


# ------------------------------------------------------------ dashboard link
class Dashboard:
    """Talks to your Lovable dashboard. Headers on every request: x-bot-key, plus x-bot-store: hobbiesville so the
    dashboard can give this bot its own Pause button and online indicator."""

    def __init__(self, base_url, key, session=None):
        self.base = (base_url or "").rstrip("/")
        self.headers = {"x-bot-key": key, "x-bot-store": DASH_STORE}
        self.session = session if session is not None else HttpSession()

    async def get_config(self):
        r = await self.session.get(f"{self.base}/api/public/bot/config", headers=self.headers)
        if r.status_code in (401, 403):
            raise RuntimeError(f"HTTP {r.status_code}: the dashboard rejected BOT_API_KEY (check the Railway variable)")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return json.loads(r.text)

    async def post_log(self, kind, content, sku=None):
        body = {"kind": kind, "content": (content or "")[:1500]}
        if sku:
            body["sku"] = sku
        r = await self.session.post_json(f"{self.base}/api/public/bot/log", self.headers, body)
        if r.status_code >= 300:
            raise RuntimeError(f"HTTP {r.status_code}")


class LogShipper:
    """Sends log lines to the dashboard in the background so Hobbiesville checks never wait on it."""

    def __init__(self, dashboard, sleep=asyncio.sleep):
        self.dash = dashboard
        self.sleep = sleep
        self.q = asyncio.Queue(maxsize=300)

    def push(self, kind, content, sku=None):
        item = (kind, content, sku)
        try:
            self.q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.q.get_nowait()       # drop the oldest
                self.q.put_nowait(item)
            except Exception:
                pass

    async def run(self):
        while True:
            kind, content, sku = await self.q.get()
            for attempt in range(3):
                try:
                    await self.dash.post_log(kind, content, sku)
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    print(f"[dashboard] couldn't send a log line ({type(e).__name__}: {e}), try {attempt + 1}/3")
                    await self.sleep(5 * (attempt + 1))


# ------------------------------------------------------------ the monitor
class Monitor:
    """All the logic. Talks to Discord only through send_alert / send_text callbacks."""

    def __init__(self, session, send_alert, send_text,
                 clock=time.monotonic, sleep=asyncio.sleep, rand=random.uniform,
                 dashboard=None, shipper=None, session_factory=None, send_error=None):
        self.session = session
        self.send_alert = send_alert
        self.send_text = send_text
        self.send_error = send_error
        self.session_factory = session_factory
        self.dashboard = dashboard
        self.shipper = shipper
        self.clock = clock
        self.sleep = sleep
        self.rand = rand
        self.synced_once = False
        self._err_seen = {}

        st = load_json(STATE_FILE)
        self.state = st if isinstance(st, dict) else {}
        cfg = load_json(CONFIG_FILE)
        if isinstance(cfg, dict) and isinstance(cfg.get("watch"), list):
            self.watch = [h for h in (parse_handle(s) for s in cfg["watch"]) if h]
        elif dashboard is not None:
            self.watch = []        # the dashboard decides; start empty until the first sync
        else:
            self.watch = [h for h in (parse_handle(x) for x in WATCH_PRODUCTS) if h]

        self.paused = False
        self.blocked_until = 0.0
        self.backoff = BACKOFF_START
        self.was_blocked = False
        self.blocks_in_row = 0
        self.block_started = self.last_block_note = 0.0
        self.block_places = {}
        self.good_pages = 0
        self.last_request = clock() - GAP_MAX
        self.requests_made = 0
        self.round = deque()
        self.round_open = False
        self.next_round_at = 0.0
        self.priority = deque()
        self.refresh = False
        self.reasons = {}      # handle -> (text, time string)
        self.names = {}
        self.orig = {}         # handle -> the exact text the dashboard has for it (sent back with logs)

    # ---- small helpers
    def dash_log(self, kind, content, sku=None):
        if self.shipper:
            self.shipper.push(kind, LOG_TAG + re.sub(r"[*`]", "", content), self.orig.get(sku, sku))

    def dash_error(self, key, content, sku=None):
        """Every error goes to the Discord error channel; the dashboard gets the same one at most every ERROR_REPEAT_SECONDS."""
        self.error_channel(content)
        now = self.clock()
        if now - self._err_seen.get(key, -1e9) < ERROR_REPEAT_SECONDS:
            return
        self._err_seen[key] = now
        self.dash_log("error", content, sku)

    def error_channel(self, text):
        """Copy an error to the Discord error channel (ERROR_CHANNEL_ID), without making the caller wait."""
        if self.send_error is None:
            return

        async def send():
            try:
                await self.send_error(text)
            except Exception as e:
                print(f"[discord] couldn't send to the error channel: {e}")
        try:
            asyncio.get_running_loop().create_task(send())
        except RuntimeError:
            pass

    async def _say(self, text, kind="info", sku=None):
        """Status messages. With the dashboard connected they go only there, so Discord just gets alerts."""
        print(f"[status] {text}")
        if not self.shipper or STATUS_IN_DISCORD:
            try:
                await self.send_text(text)
            except Exception as e:
                print(f"[discord] couldn't send message: {e}")
        self.dash_log(kind, text, sku)

    async def _alert(self, alert):
        try:
            await self.send_alert(alert)
        except Exception as e:
            print(f"[discord] couldn't send alert: {e}")
        v = alert["v"]
        self.dash_log("message", f"{alert['kind']}: {alert['name']} | {money(v['price'])} | "
                                 f"{alert['url']} | {alert['why']}", alert["sku"])

    def set_reason(self, handle, text):
        changed = self.reasons.get(handle, ("",))[0] != text
        self.reasons[handle] = (text, local_now().strftime("%I:%M:%S %p"))
        return changed

    def save_config(self):
        save_json(CONFIG_FILE, {"watch": self.watch})

    def round_estimate(self):
        jobs = len(self.watch)
        return max(int(jobs * (GAP_MIN + GAP_MAX) / 2), MIN_SECONDS_PER_ROUND if jobs else 0)

    async def wait_turn(self):
        gap = self.rand(GAP_MIN, GAP_MAX)
        wait = self.last_request + gap - self.clock()
        if wait > 0:
            await self.sleep(wait)
        self.last_request = self.clock()
        self.requests_made += 1

    def _drop(self, handle):
        self.round = deque(j for j in self.round if j[1] != handle)
        self.priority = deque(j for j in self.priority if j[1] != handle)

    # ---- rounds
    def build_round(self):
        jobs = [("item", h, False) for h in self.watch]
        random.shuffle(jobs)                  # different order every round, less robotic
        self.round = deque(jobs)
        self.next_round_at = self.clock() + MIN_SECONDS_PER_ROUND

    def next_job(self):
        if self.refresh:
            self.refresh = False
            self.build_round()
            self.round_open = True
        if self.priority:
            return self.priority.popleft()
        if not self.round:
            if self.round_open:
                save_json(STATE_FILE, self.state)
                self.round_open = False
            if self.clock() < self.next_round_at:
                return None              # idle: don't re-check the same products too soon
            self.build_round()
            self.round_open = True
        return self.round.popleft() if self.round else None

    async def run(self):
        while True:
            await self.tick()

    async def tick(self):
        job = None
        try:
            if self.paused:
                await self.sleep(1)
                return
            remaining = self.blocked_until - self.clock()
            if remaining > 0:
                await self.sleep(min(5, remaining))
                return
            job = self.next_job()
            if job is None:
                await self.sleep(5)
                return
            await self.wait_turn()
            if self.paused:                      # paused while we were waiting
                self.priority.appendleft(job)
                return
            await self.check(job[1], confirm=job[2])
            self.good_pages += 1
            if self.was_blocked:
                self.was_blocked = False
                self.backoff = BACKOFF_START
                mins = (self.clock() - self.block_started) / 60
                tries = (f" after {self.blocks_in_row} blocked tries over {mins:.0f} min"
                         if self.blocks_in_row > 1 else f" after {mins:.0f} min")
                await self._say(f"Back to normal{tries}: Hobbiesville is returning product data again, "
                                f"resuming checks. Got through on: {job[1]}.")
        except Blocked as b:
            await self.on_blocked(job, b)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[error] {type(e).__name__}: {e}")
            self.dash_error(f"exc:{type(e).__name__}", f"Unexpected error in the bot: {type(e).__name__}: {e}")
            await self.sleep(5)

    async def on_blocked(self, job, b):
        first_block = not self.was_blocked
        self.was_blocked = True
        self.blocks_in_row = 1 if first_block else self.blocks_in_row + 1
        where = job[1] if job else "unknown product"
        good, self.good_pages = self.good_pages, 0
        wait = f"{self.backoff / 60:.0f} min" if self.backoff >= 120 else f"{self.backoff:.0f} seconds"
        self.blocked_until = self.clock() + self.backoff
        if job is not None:
            self.priority.appendleft(job)
            self.set_reason(job[1], BLOCK_REASON)
        await self.reset_session()
        msg = (f"Hobbiesville refused my request (bot check or 'too many requests'). Blocked on: {where}, "
               f"after {good} request(s) worked. I'm pausing ALL requests for {wait}, then I'll retry on "
               f"my own. Hobbiesville returned: {b}.")
        print(f"[BLOCKED] (#{self.blocks_in_row} in a row) {msg}")
        self.error_channel(f"BLOCKED (#{self.blocks_in_row} in a row): {msg}")
        now = self.clock()
        if first_block:          # one message when a block starts, then an update every 10 minutes
            self.block_started = self.last_block_note = now
            await self._say(msg, kind="error")
        elif now - self.last_block_note >= BLOCK_UPDATE_SECONDS:
            self.last_block_note = now
            await self._say(f"Still blocked by Hobbiesville: {(now - self.block_started) / 60:.0f} min so far, "
                            f"{self.blocks_in_row} tries in a row. Next retry in {wait}.", kind="error")
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)

    async def reset_session(self):
        """After a block, start over with a fresh connection and no cookies."""
        if self.session_factory is None:
            return
        try:
            await self.session.close()
        except Exception:
            pass
        self.session = self.session_factory()

    async def fetch(self, handle):
        """(kind, value) for one product. Raises Blocked on a bot check."""
        try:
            r = await self.session.get(product_data_url(handle))
        except Exception as e:
            detail = (str(e).strip().splitlines() or [""])[0][:150]
            return "unreadable", f"error: {type(e).__name__}{': ' + detail if detail else ''}"
        kind, val = classify_response(r.status_code, r.text, r.url)
        if kind == "blocked":
            raise Blocked(val)
        return kind, val

    # ---- the product check (the only thing that can trigger an alert)
    async def check(self, handle, confirm=False):
        kind, val = await self.fetch(handle)
        if kind != "ok":
            reason = reason_text(None, False, val if kind == "unreadable" else kind)
            if self.set_reason(handle, reason):
                print(f"[not posted] {handle}: {reason}")
            if kind != "password":
                self.dash_error(f"unreadable:{handle}", f"{handle}: {reason}", handle)
            if confirm:
                await self._say(f"Added `{handle}` to the watchlist, but: {reason}", sku=handle)
            return

        v = val
        name = v["name"] or self.names.get(handle) or handle
        self.names[handle] = name
        ok = is_ok(v)
        key = f"watch:{handle}"
        prev = self.state.get(key)
        self.state[key] = ok
        prev_reason = self.reasons.get(handle, (None,))[0]
        reason = reason_text(v, ok)
        if self.set_reason(handle, reason):
            print(f"[watch] {handle}: {reason}")

        kind = decide(prev, ok)
        if kind:
            why = reason
            if prev_reason and prev_reason != reason and prev_reason != BLOCK_REASON:
                why += f" Before: {prev_reason}"
            elif prev is None:
                why += " (First check since I started watching it.)"
            await self._alert({"kind": kind, "name": name, "sku": handle, "v": v, "why": why,
                               "url": product_url(handle)})
        if confirm:
            await self._say(f"Now watching **{name}** (`{handle}`). Right now: {reason}", sku=handle)

    # ---- dashboard sync
    def apply_config(self, cfg):
        """Apply the dashboard's settings: pause state + the Hobbiesville list (store starting with 'hobbiesville')."""
        if not isinstance(cfg, dict):
            return
        paused = bool(cfg.get("paused"))
        raw = cfg.get("skus")
        if isinstance(raw, list):
            new, labels = [], {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
                store = str(item.get("store") or "").lower()
                if not store.startswith(DASH_STORE):
                    continue                      # Walmart / Amazon / EB Games belong to other bots
                original = str(item.get("sku") or "")
                handle = parse_handle(original)
                if not handle:
                    self.dash_error(f"badsku:{original}",
                                    f"Ignoring '{original}': that isn't a Hobbiesville product link "
                                    f"(it should look like https://hobbiesville.com/products/<name>).")
                    continue
                self.orig[handle] = original
                if handle not in new:
                    new.append(handle)
                label = str(item.get("label") or "").strip()
                if label:
                    labels[handle] = label
            for handle, label in labels.items():
                self.names.setdefault(handle, label)
            added = [x for x in new if x not in self.watch]
            removed = [x for x in self.watch if x not in new]
            self.watch = new
            for handle in added:
                self.priority.append(("item", handle, self.synced_once))   # confirm message only after the first sync
            for handle in removed:
                self._drop(handle)
            if added or removed:
                self.save_config()
                if self.synced_once:
                    self.dash_log("info", f"Watchlist updated from the dashboard: {len(added)} added, "
                                          f"{len(removed)} removed, {len(new)} total.")
        if paused != self.paused:
            self.paused = paused
            if self.synced_once or paused:
                note = ("Paused from the dashboard. No requests to Hobbiesville until you resume."
                        if paused else "Resumed from the dashboard.")
                print(f"[dashboard] {note}")
                self.dash_log("info", note)
        self.synced_once = True

    async def run_sync(self):
        """Every ~minute: ask the dashboard for settings (this also counts as the check-in)."""
        if not self.dashboard:
            return
        fails = 0
        while True:
            try:
                self.apply_config(await self.dashboard.get_config())
                if fails >= 3:
                    print("[dashboard] connection restored")
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fails += 1
                print(f"[dashboard] couldn't get settings ({type(e).__name__}: {e}); keeping the last settings")
            await self.sleep(self.rand(*CONFIG_EVERY_SECONDS))

    # ---- Discord commands (returns reply text, or None if not a command)
    def command(self, text):
        parts = (text or "").strip().split()
        if not parts or not parts[0].startswith("!"):
            return None
        cmd, args = parts[0][1:].lower(), parts[1:]

        if self.dashboard and cmd in ("watch", "add", "remove", "unwatch", "delete", "pause", "resume"):
            return "The dashboard controls the product list and pause/resume now. Make that change there."

        if cmd in ("watch", "add"):
            if not args:
                return "Usage: `!watch https://hobbiesville.com/products/<name>`. You can list several."
            added, already, bad = [], [], []
            for a in args:
                h = parse_handle(a)
                if not h:
                    bad.append(a)
                elif h in self.watch:
                    already.append(h)
                else:
                    self.watch.append(h)
                    self.priority.append(("item", h, True))
                    added.append(h)
            if added:
                self.save_config()
            out = []
            if added:
                tail = " Resume with `!resume` to get the confirmation." if self.paused else \
                       " I'll check it next and tell you what it is and whether it's in stock."
                out.append("Added: " + ", ".join(f"`{s}`" for s in added) + "." + tail)
            if already:
                out.append("Already watching: " + ", ".join(f"`{s}`" for s in already) + ".")
            if bad:
                out.append("Not a Hobbiesville product link: " + ", ".join(f"`{b}`" for b in bad) + ".")
            return " ".join(out)

        if cmd in ("remove", "unwatch", "delete"):
            if not args:
                return "Usage: `!remove <product link or name>`"
            removed, missing = [], []
            for a in args:
                h = parse_handle(a)
                if h and h in self.watch:
                    self.watch.remove(h)
                    self._drop(h)
                    removed.append(h)
                else:
                    missing.append(a)
            if removed:
                self.save_config()
            out = []
            if removed:
                out.append("Removed: " + ", ".join(f"`{s}`" for s in removed) + ".")
            if missing:
                out.append("Wasn't watching: " + ", ".join(f"`{m}`" for m in missing) + ".")
            return " ".join(out)

        if cmd == "list":
            if not self.watch:
                return "Not watching anything. Add one with `!watch <product link>`."
            lines = []
            for h in self.watch:
                name = (self.names.get(h) or "name unknown yet")[:50]
                r = self.reasons.get(h)
                lines.append(f"`{h}` {name}: {r[0] if r else 'not checked yet'}")
            return f"Watching {len(self.watch)}:\n" + "\n".join(lines)

        if cmd == "pause":
            self.paused = True
            return "Paused. I'm making NO requests to Hobbiesville until you send `!resume`."

        if cmd == "resume":
            self.paused = False
            return f"Resumed. Checks continue (each request {GAP_MIN}-{GAP_MAX} seconds apart)."

        if cmd == "refresh":
            self.refresh = True
            return (f"Refresh queued: I'll start a fresh round of every watched product now. The first request "
                    f"goes out within {GAP_MAX} seconds, and a full round takes about {self.round_estimate()} seconds.")

        if cmd == "status":
            if self.paused:
                state = "PAUSED from the dashboard" if self.dashboard else "PAUSED by command"
            elif self.was_blocked:
                state = (f"BLOCKED by Hobbiesville for {(self.clock() - self.block_started) / 60:.0f} min "
                         f"({self.blocks_in_row} tries in a row), retrying on its own")
            else:
                state = "running"
            since = int(self.clock() - self.last_request)
            return (f"Status: {state}\nWatching {len(self.watch)} product(s), each checked about every "
                    f"{self.round_estimate()}s (requests spaced {GAP_MIN}-{GAP_MAX}s apart).\n"
                    f"Method: light (plain web requests, no browser).\n"
                    f"Requests made since start: {self.requests_made}. Last request {since}s ago.\n"
                    f"Queued extra checks: {len(self.priority)}.")

        if cmd == "why":
            if not args:
                return "Usage: `!why <product link or name>`"
            h = parse_handle(args[0])
            if not h:
                return "That doesn't look like a Hobbiesville product link."
            r = self.reasons.get(h)
            if not r:
                where = "I'm watching it but haven't checked it yet." if h in self.watch else \
                        "I'm not watching it. Add it with `!watch`."
                return f"No decision recorded for `{h}` yet. {where}"
            return f"**{self.names.get(h, h)}** (`{h}`), last checked {r[1]}: {r[0]}"

        if cmd == "help":
            return ("**Commands** (Hobbiesville bot)\n"
                    "`!watch <link>` - start watching a product (hobbiesville.com/products/... link)\n"
                    "`!remove <link>` - stop watching it\n"
                    "`!list` - what I'm watching and why each isn't alerting\n"
                    "`!why <link>` - the reason for my latest decision on one product\n"
                    "`!pause` / `!resume` - stop or restart all requests\n"
                    "`!refresh` - start a fresh check round right now\n"
                    "`!status` - am I running, blocked or paused")
        return None


# ------------------------------------------------------------ debug tools
def print_verdict(kind, v):
    if kind != "ok":
        print(f"  Result: {kind} {v or ''}")
        print(f"  Why: {reason_text(None, False, v if kind == 'unreadable' else kind)}")
        return
    for k, val in v.items():
        if k != "image":
            print(f"  {k}: {val}")
    ok = is_ok(v)
    print(f"\n  Why: {reason_text(v, ok)}")
    print(f"  Would the bot alert right now? {'YES' if ok else 'no'}")


async def debug_product(raw):
    handle = parse_handle(raw)
    if not handle:
        print("That isn't a Hobbiesville product link (it should look like https://hobbiesville.com/products/<name>).")
        return
    async with make_session(get_proxy()) as s:
        url = product_data_url(handle)
        print(f"Loading {url} with plain web requests (no browser)...")
        r = await s.get(url)
        print(f"  HTTP {r.status_code}, ended up on {r.url}")
        kind, val = classify_response(r.status_code, r.text, r.url)
        if kind == "blocked":
            print(f"  BLOCKED: {val}")
            print("  Simple requests don't work right now. Wait 30-60 minutes and try again; if it keeps happening,"
                  " the bot would need the Chrome setup from the other bots.")
        else:
            print_verdict(kind, val)
        if kind != "ok":
            fname = f"debug_{handle}.txt"
            with open(fname, "w") as f:
                f.write(f"URL: {r.url}\nHTTP: {r.status_code}\n\n{r.text or ''}")
            print(f"  Saved what Hobbiesville sent to {fname}.")


def parse_file(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    print_verdict(*classify_response(200, text, ""))


# ------------------------------------------------------------ discord bot
def run_bot():
    import discord

    token = os.environ.get("DISCORD_TOKEN") or DISCORD_TOKEN
    channel_raw = os.environ.get("CHANNEL_ID") or CHANNEL_ID
    role_id = os.environ.get("ROLE_ID") or ROLE_ID or None
    proxy = get_proxy()
    dash_url = os.environ.get("DASHBOARD_URL", "").strip()
    dash_key = os.environ.get("BOT_API_KEY", "").strip()
    if not token or not channel_raw:
        sys.exit("Missing DISCORD_TOKEN or CHANNEL_ID. Set them in Railway Variables.")
    channel_id = int(channel_raw)
    error_channel_id = int(os.environ.get("ERROR_CHANNEL_ID", "").strip() or 0)   # every error is copied here

    intents = discord.Intents.default()
    intents.message_content = True      # needed to read !commands (enable it in the Developer Portal too)

    class Bot(discord.Client):
        async def setup_hook(self):
            dash = shipper = None
            if dash_url and dash_key:
                dash = Dashboard(dash_url, dash_key)
                shipper = LogShipper(dash)
            elif dash_url or dash_key:
                print("[dashboard] DASHBOARD_URL and BOT_API_KEY must BOTH be set. Running without the dashboard.")
            self.shipper = shipper

            def new_session():
                return make_session(proxy)
            self.mon = Monitor(new_session(), self.post_alert, self.post_text,
                               dashboard=dash, shipper=shipper, session_factory=new_session,
                               send_error=self.post_error if error_channel_id else None)
            self.bg = asyncio.create_task(self.run_monitor())

        async def run_monitor(self):
            try:
                await self.wait_until_ready()
                where = "dashboard connected" if self.mon.dashboard else "no dashboard, using Discord commands"
                if proxy:
                    safe = "(proxy set)"
                    try:
                        u = urlsplit(proxy)
                        safe = f"{u.scheme}://{u.username + ':****@' if u.username else ''}{u.hostname}:{u.port}"
                    except Exception:
                        pass
                    proxy_line = f"Proxy: `{safe}`"
                else:
                    proxy_line = "Proxy: **not set** (using Railway IP)"
                await self.mon._say(
                    f"Hobbiesville bot online ({where}).\n{proxy_line}\n"
                    f"Method: light (plain web requests to Shopify's product data, no browser).\n"
                    f"Alerts when a watched product comes into stock. Every request to Hobbiesville is "
                    f"{GAP_MIN}-{GAP_MAX}s apart. Type `!help` for commands.")
                jobs = [self.mon.run(), self.mon.run_sync()]
                if self.shipper:
                    jobs.append(self.shipper.run())
                await asyncio.gather(*jobs)
            except Exception as e:
                print(f"[fatal] monitor stopped: {type(e).__name__}: {e}")

        async def post_text(self, text):
            ch = self.get_channel(channel_id)
            if ch is not None:
                await ch.send(text[:1900])

        async def post_error(self, text):
            ch = self.get_channel(error_channel_id)
            if ch is None:
                print(f"[discord] error channel {error_channel_id} not found (check ERROR_CHANNEL_ID and the bot's access)")
                return
            await ch.send(f"[{local_now():%I:%M %p}] [Hobbiesville] {text}"[:1900])

        async def post_alert(self, a):
            v = a["v"]
            e = discord.Embed(title=(a["name"] or a["sku"])[:250], url=a["url"], color=0x2ECC71)
            e.set_author(name=f"Hobbiesville - {a['kind']}")
            e.add_field(name="Price", value=money(v["price"]))
            if v.get("compare_at"):
                e.add_field(name="Was", value=money(v["compare_at"]))
            if v.get("option"):
                e.add_field(name="Option", value=v["option"][:100])
            if v.get("options_total", 1) > 1:
                e.add_field(name="Options in stock", value=f"{v['options_in_stock']} of {v['options_total']}")
            if v.get("preorder"):
                e.add_field(name="Type", value="Pre-order")
            e.add_field(name="Why this alert", value=a["why"][:1000], inline=False)
            if v.get("image"):
                e.set_thumbnail(url=v["image"])
            e.set_footer(text=f"Hobbiesville - verified on the product data - {local_now():%I:%M:%S %p}")
            ping = f"<@&{role_id}> " if role_id else ""
            ch = self.get_channel(channel_id)
            if ch is not None:
                await ch.send(ping, embed=e)

        def allowed(self, user):
            if ALLOWED_USER_IDS:
                return user.id in ALLOWED_USER_IDS
            perms = getattr(user, "guild_permissions", None)
            return bool(perms and perms.manage_guild)

        async def on_message(self, message):
            if message.author.bot or message.channel.id != channel_id:
                return
            if not message.content.startswith("!"):
                return
            if not self.allowed(message.author):
                await message.reply("You don't have permission to control this bot.", mention_author=False)
                return
            reply = self.mon.command(message.content)
            if reply is None:
                return
            await message.reply(reply[:1900], mention_author=False)

    Bot(intents=intents).run(token)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "debug":
        asyncio.run(debug_product(sys.argv[2]))
    elif len(sys.argv) > 2 and sys.argv[1] == "parse":
        parse_file(sys.argv[2])
    else:
        run_bot()
