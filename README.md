# RSPS → Hyperliquid Portfolio Bot

Keeps a dedicated Hyperliquid account converged to the allocation published by
the TradingView RSPS strategy. Design: see [SPECS.md](SPECS.md). Core idea:
**TradingView publishes the full target allocation every 12h bar (heartbeat);
the bot's only job is to make the account converge to the last known target,
forever.**

```
TradingView ── JSON heartbeat every 12h ──► Caddy (HTTPS) ──► FastAPI ──► SQLite
                                                                 │
                              Telegram ◄── Watchdog      Reconciler ──► Hyperliquid
                                                                        (perps, 1x, IOC)
```

## Layout

| Path | What |
|---|---|
| `app/` | The bot: `main.py` (webhook + wiring), `ingest.py`, `models.py` (validation), `plan.py` (target math), `reconciler.py`, `hl.py` (Hyperliquid wrapper), `watchdog.py`, `telegram.py`, `notify.py`, `db.py`, `state.py`, `config.py` |
| `pine/webhook-heartbeat.pine` | Final Pine alert block + install instructions |
| `config.yaml` | Non-secret config (symbol map, thresholds) |
| `.env.example` | Secrets template |
| `scripts/` | `provision-vps.sh`, `deploy.sh`, `backup.sh` |
| `tests/` | Unit + replay tests (mocked exchange) |

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Deployment (Hetzner VPS, Ubuntu 24.04)

1. **DNS**: point an A record (e.g. `bot.example.com`) at the VPS.
2. **Provision** (as root, once): copy the repo or run
   `bash scripts/provision-vps.sh` — installs Docker, ufw (SSH+80+443 only),
   fail2ban, unattended-upgrades, ssh hardening, backup cron.
3. **Clone** to `/opt/rsps-bot` (the backup cron expects this path).
4. **Secrets**: `cp .env.example .env`, fill in everything, `chmod 600 .env`.
   - Create the Hyperliquid **API/agent wallet** at
     <https://app.hyperliquid.xyz/API> (trade-only; the master key never
     touches the server). Put its private key in `HL_API_WALLET_KEY` and the
     master address in `HL_ACCOUNT_ADDRESS`.
   - Generate `WEBHOOK_TOKEN` (`openssl rand -hex 32`) and
     `WEBHOOK_PATH_SECRET` (`openssl rand -hex 16`).
   - Telegram: create a bot via @BotFather (`TELEGRAM_BOT_TOKEN`), get your
     numeric id via @userinfobot (`TELEGRAM_CHAT_ID`).
   - Create a check at healthchecks.io with a 15-minute grace period and put
     its ping URL in `HEALTHCHECKS_URL`.
5. **Deploy**: `./scripts/deploy.sh` (that's `git pull && docker compose up -d --build`).
6. **TradingView**: apply the two edits in `pine/webhook-heartbeat.pine` to the
   RSPS indicator, set the Webhook Token input to `WEBHOOK_TOKEN`, create ONE
   alert ("Any alert() function call", message `{{message}}`, webhook
   `https://<domain>/webhook/<WEBHOOK_PATH_SECRET>`), then send `/armed` to the
   bot.

## Rollout (from SPECS §12 — the bot earns trust before real size)

1. Tests (`pytest`) — done in CI/locally.
2. **Testnet soak**: `HL_TESTNET=true`, real TV webhooks, several days.
3. **Mainnet dry-run**: `HL_TESTNET=false`, `DRY_RUN=true` ~1 week — it
   Telegrams the orders it *would* place.
4. **Mainnet small** ($1–5k) for 2+ weeks incl. one rotation.
5. Full capital (just deposit; the bot sizes from live equity).

Exit criterion each phase: zero unexplained divergence between intended and
actual allocation.

## Telegram commands

`/status` · `/positions` · `/reconcile` · `/pause` · `/resume` ·
`/armed` (reset the TV alert-expiry countdown) · `/help`

## Operations

- **Logs**: `docker compose logs -f app` (structured JSON, rotated 10MB×5).
  Every decision is also queryable in SQLite: `sqlite3 data/rsps.db 'select * from runs order by id desc limit 5;'`
- **Health**: `https://<domain>/healthz` (DB, HL API reachability, heartbeat age).
- **Backups**: nightly cron → `backups/` (14 kept). Weekly off-box copy:
  `scp vps:/opt/rsps-bot/backups/$(ssh vps 'ls -1t /opt/rsps-bot/backups | head -1') ./`
  DB loss is non-fatal — the next heartbeat restores the target.
- **Config change**: edit `config.yaml` → `docker compose up -d --build`.
- **TV alert expiry** (Essential plan, ~60 days): the watchdog CRITICALs after
  13h without a heartbeat and nags from day 53. After re-creating the alert,
  send `/armed`.
