# Hobbiesville restock monitor (light version)

Discord bot that alerts when a watched hobbiesville.com product comes into stock.
Hobbiesville runs on Shopify, so the bot reads each product's data at `<product link>.js` with plain
web requests. No Chrome, no Playwright, no virtual screen.

## Test on your Mac first
```
python3 -m pip install -r requirements.txt
python3 hobbiesville_monitor.py debug https://www.hobbiesville.com/products/<name>
python3 hobbiesville_monitor.py scan https://www.hobbiesville.com/collections/pokemon-trading-cards
python3 -m unittest discover tests
```

## Railway
1. New Project -> Deploy from GitHub repo -> pick this repo (branch `main`).
2. Start Command: leave empty. Replicas: 1 (set in `railway.json`).
3. Variables: `DISCORD_TOKEN`, `CHANNEL_ID`, and optionally `DASHBOARD_URL`, `BOT_API_KEY`,
   `ROLE_ID`, `ERROR_CHANNEL_ID`, `MIN_SECONDS_PER_ROUND` (60), `BLOCK_RETRY_SECONDS` (300),
   `BLOCK_RETRY_MAX` (1800), `SCAN_COLLECTIONS` (pokemon-trading-cards; comma-separated, `0` = off),
   `SCAN_PAGES` (4 pages of 250 products), `TIMEZONE` (America/Toronto), `STATUS_IN_DISCORD` (0), `DATA_DIR` (.).

## Dashboard
Entries with store `hobbiesville` are watched. The `sku` field holds the product link
(or just the part after `/products/`). Requests carry `x-bot-store: hobbiesville`.
