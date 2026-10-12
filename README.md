# PolySim

PolySim is a Discord bot for public, simulated betting on active Polymarket YES/NO markets. It uses play credits only. The bot reads market data and prices; it does not connect a wallet or place Polymarket orders.

## Features

- Browse the 10 most active events by 24-hour volume with `/markets`, or search events with `/markets query`. Results show each event's link, lifetime volume, and leading outcome; use the dropdown to open its bets and the buttons to page through results.
- Place a simulated position with `/bet market_id outcome stake` from an event's bet list.
- Each member starts with 10,000 credits per server. `/balance`, `/positions`, and `/leaderboard` are public.
- Positions buy shares at the current Polymarket best BUY price and settle automatically after Polymarket publishes a clear final outcome. Winning shares pay 1 credit each; a 50/50 resolution pays 0.50 per share.
- Open positions are valued at the current best SELL price for account and leaderboard totals. A position without a current sell quote is marked unpriced and excluded from its displayed total.
- `/admin reset` restores all existing server accounts to 10,000 credits. `/admin reset member:@name` restores one account. Both void that account's open positions while retaining the position history.

The bot uses Polymarket's public [market discovery](https://docs.polymarket.com/market-data/discover-markets), [price and order-book](https://docs.polymarket.com/market-data/prices-order-books), and [resolution](https://docs.polymarket.com/concepts/resolution) data. It currently accepts standard binary YES/NO markets.

## Requirements

- Python 3.11 or newer
- A Discord application with a bot token and the `applications.commands` and `bot` scopes
- A host that stays online and keeps the SQLite database file between restarts

## Setup and run

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

Set `DISCORD_TOKEN` in your shell or deployment environment. `DISCORD_DEV_GUILD_ID` is optional and makes slash commands sync to a test server immediately. Keep `.env` and the database out of source control. Start the bot with:

```bash
python -m polysim
```

By default, the database is stored at `data/polysim.sqlite3`. Set `DATABASE_PATH` to a persistent volume path for hosted deployments. `RESOLUTION_POLL_SECONDS` defaults to 60 and cannot be less than 10.

## Run with Docker

Create `.env` from the example and set `DISCORD_TOKEN`, then build and start the container:

```bash
cp .env.example .env
# Edit .env and set DISCORD_TOKEN
docker compose up --build -d
```

Compose keeps the SQLite database in the `polysim-data` named volume and restarts the bot
automatically if it exits. View logs with `docker compose logs -f`; stop it with
`docker compose down`. The `.env` file is supplied to the container and is excluded from the
Docker build context.

## Commands

| Command | Purpose |
| --- | --- |
| `/markets [query]` | Show trending events or search by keyword; use the dropdown to open event bets and buttons to paginate. |
| `/event event_id [page]` | List 15 event bets per page, with YES/NO prices and market IDs. |
| `/bet market_id outcome stake` | Spend available credits on YES or NO shares. Use a market ID shown by `/event`. |
| `/balance [member]` | Show available credits and open-position value. |
| `/positions [member]` | Show open, settled, and admin-voided positions; open bets show entry-to-current-sell odds and P/L. Select one of your open positions to sell a share count, a percentage (such as `25%`), or `all`. Partial sales leave the remainder open. Sold bets and sale proceeds appear in `/history`. |
| `/history [member] [limit]` | Show a member's most recent bet activity, ordered by sale/settlement time when applicable; includes proceeds for sold positions. |
| `/leaderboard` | Rank server members by available credits plus current open-position value. |
| `/admin reset [member]` | Reset one member or all existing accounts; requires Manage Server permission. |
| `/admin test-settlement` | Post a sample resolution notice in the current channel; requires Manage Server permission and does not change credits. |

All bot responses and settlement notices are public. Automatic settlement notices are sent to the channel where each bet was placed. A market remains unsettled while the API does not report an unambiguous final outcome.

## Development

Run the automated tests with:

```bash
pytest
ruff check .
```

The tests use local SQLite databases and mocked API responses; they do not need a Discord token or place external orders.
