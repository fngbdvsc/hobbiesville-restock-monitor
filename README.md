# Hobbiesville restock monitor

Discord bot that alerts when a watched or scanned hobbiesville.com product comes into stock, or a new one is listed.
Hobbiesville runs on Shopify. The bot opens the homepage in a real Chrome (a normal window on a virtual screen),
then reads the store's public product data (`<product link>.js` and `/collections/<name>/products.json`).
Plain web requests without a browser (`FETCHER=light`) were refused from Railway with HTTP 429.

## Test on your Mac first
```
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
python3 hobbiesville_monitor.py debug https://www.hobbiesville.com/products/<name>
python3 hobbiesville_monitor.py scan https://www.hobbiesville.com/collections/pokemon-trading-cards
python3 -m unittest discover tests
```

## Railway
1. New Project -> Deploy from GitHub repo -> pick this repo (branch `main`).
2. Start Command: leave empty (the Dockerfile starts the virtual screen, then the bot). Replicas: 1 (set in `railway.json`).
3. Variables: `DISCORD_TOKEN`, `CHANNEL_ID`, and optionally `DASHBOARD_URL`, `BOT_API_KEY`,
   `ROLE_ID`, `ERROR_CHANNEL_ID`, `MIN_SECONDS_PER_ROUND` (60), `BLOCK_RETRY_SECONDS` (300),
   `BLOCK_RETRY_MAX` (1800), `SCAN_COLLECTIONS` (pokemon-trading-cards; comma-separated, `0` = off),
   `SCAN_PAGES` (4 pages of 250 products), `TIMEZONE` (America/Toronto), `STATUS_IN_DISCORD` (0), `DATA_DIR` (.),
   `FETCHER` (browser; `light` = no Chrome), `PROXY` (none).

## Dashboard
Entries with store `hobbiesville` are watched. The `sku` field holds the product link
(or just the part after `/products/`). Requests carry `x-bot-store: hobbiesville`.
