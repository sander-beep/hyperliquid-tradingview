# SPECS — TradingView RSPS → Hyperliquid Portfolio Automation

**Status:** Approved design, not yet built.
**Goal:** The Hyperliquid account always holds exactly the allocation the TradingView RSPS strategy dictates — automatically, continuously verified, with every failure detected, retried, and reported.

---

## 1. Decisions already made (with the user)

| Decision | Choice |
|---|---|
| Instruments | Hyperliquid **perps at 1x**, long-only, as spot substitutes |
| Gold sleeve | **PAXG-USD perp** on Hyperliquid (signal is XAUT-based; tiny basis accepted) |
| Allocation mode in use | **80% strongest / 20% second** (bot must handle all three modes anyway) |
| Account scope | Hyperliquid account **100% dedicated** to this bot; any foreign position is an anomaly |
| Capital at launch | **< $100k** → single aggressive IOC limit orders per leg are sufficient |
| Hosting | **Small VPS** (Hetzner, ~€5/mo), Docker |
| Notifications | **Telegram** (primary and only channel for v1) |
| TradingView plan | **Essential** — webhooks work, but **alerts expire after ~2 months** (must be detected + nagged) |

---

## 2. Core design principle: reconcile state, don't react to events

The single biggest failure mode of TV→exchange bots is *event-driven diffing*: the alert says "switch to SOL", the webhook gets lost once, and the portfolio is silently wrong for weeks.

We eliminate that class of bug by changing the contract:

> **TradingView publishes the full target allocation every 12h bar (a heartbeat). The bot's only job is to make the account converge to the last known target, forever.**

Consequences:

- A missed webhook self-heals on the next heartbeat (≤ 12h later) — and the watchdog screams about it in the meantime.
- A crashed bot self-heals on restart: it re-reads the last persisted target and reconciles.
- A partially filled rebalance self-heals: the reconciler runs again and closes the gap.
- An expired TradingView alert (the Essential-plan 2-month expiry) is *detected within one bar* because heartbeats stop.

---

## 3. System overview

```
TradingView (Essential plan)
  RSPS indicator, 12h chart
  ONE alert: "Any alert() function call" + webhook
        │  JSON heartbeat every 12h bar (+ on change, same payload)
        ▼  HTTPS POST
┌─────────────────────────── VPS (Hetzner CX22, Ubuntu 24.04) ───────────────────────────┐
│  Caddy (auto-HTTPS, :443) ──► FastAPI webhook receiver                                 │
│                                   │ validate secret + schema, persist, ACK < 1s        │
│                                   ▼                                                    │
│  SQLite (WAL) ◄──────────── Reconciler (async loop)                                    │
│   signals, orders,             │ target weights vs live positions → order plan         │
│   runs, heartbeats             ▼                                                       │
│                             Executor ──► Hyperliquid API (official Python SDK,         │
│                                          API/agent wallet — trade-only, no withdraw)   │
│  Watchdog (heartbeat staleness, equity sanity, alert-expiry countdown)                 │
│  Telegram bot (notifications + /status /positions /pause /resume /reconcile /armed)    │
└────────────────────────────────────────────────────────────────────────────────────────┘
         ▲
  healthchecks.io (external dead-man switch: pings if the whole VPS goes dark)
```

One Python service (asyncio), one Docker Compose file, one SQLite database. No microservices, no message queues — at this size, fewer moving parts *is* the robustness strategy.

**Stack:** Python 3.12, FastAPI, `hyperliquid-python-sdk` (official), SQLite, Caddy, Docker Compose, Telegram Bot API.

---

## 4. Pine script changes (small, additive)

The existing alert (`RSPS allocation changed -> 80% SOLUSD / 20% BTCUSD`) is text-based and change-only. We replace it with a structured JSON alert **fired on every bar**, built from the script's actual variables (`best_asset`, `second_asset`, `third_asset`, weights, `invested`, `gold_active`) — never parsed from the human-readable label.

New alert block (sketch — final code written at build time):

```pinescript
// ── WEBHOOK HEARTBEAT ─────────────────────────────────────────────
// Fires once at the first tick of every 12h bar (same timing the
// original change-alert used, matching the backtest's entry timing),
// carrying the FULL target allocation for the bar now underway.
f_leg(string sym, float w) => "{\"sym\":\"" + sym + "\",\"w\":" + str.tostring(w) + "}"

string targets =
     (not invested[0] or best_asset == "USD") ?
         (gold_active ? f_leg("GOLD", 1.0) : f_leg("USD", 1.0)) :
     triple     ? f_leg(f_remove_exchange_name(best_asset), w_primary) + "," + f_leg(f_remove_exchange_name(second_asset), w_secondary) + "," + f_leg(f_remove_exchange_name(third_asset), w_tertiary) :
     aggressive ? f_leg(f_remove_exchange_name(best_asset), w_primary) + "," + f_leg(f_remove_exchange_name(second_asset), w_secondary) :
                  f_leg(f_remove_exchange_name(best_asset), 1.0)

string payload = "{\"v\":1,\"strategy\":\"RSPS-1\",\"token\":\"" + WEBHOOK_TOKEN + "\"" +
     ",\"seq\":" + str.tostring(time) +
     ",\"bar_time\":\"" + str.format_time(time, "yyyy-MM-dd'T'HH:mm:ss'Z'", "UTC") + "\"" +
     ",\"changed\":" + (alloc_changed ? "true" : "false") +
     ",\"targets\":[" + targets + "]}"

if backtest
    alert(payload, alert.freq_once_per_bar)
```

Notes:

- **Timing preserved.** The original dynamic alert fired at the first tick of the bar where the new position takes effect (`alloc_label[1]`, `freq_once_per_bar`). The heartbeat keeps exactly that timing and allocation-indexing; it just also fires when nothing changed. The final implementation must use the same `[1]`-indexed variables the original alert logic used so live behavior matches the backtest.
- **One alert only** on TradingView: condition = *Any alert() function call*, message = `{{message}}`, webhook URL = `https://<vps-domain>/webhook`. Fits comfortably in the Essential plan's alert quota.
- `token` is a long random shared secret (auth — Pine can't do HMAC; secret-in-body over TLS plus TradingView IP allowlisting is the practical ceiling).
- `seq` = bar open time in ms → natural idempotency key and ordering key.
- Symbols are sent in TV vocabulary (`SOLUSD`, `GOLD`, `USD`); the **bot** owns the mapping to Hyperliquid coins. Pine stays dumb.

### Alert-expiry problem (Essential plan)

Two-layer defense:

1. **Detection (automatic, primary):** heartbeats arrive every 12h. If none arrives for **13h**, the watchdog sends a Telegram CRITICAL: *"No heartbeat for 13h — TradingView alert likely expired or stopped. Re-arm it."* It re-alerts every 6h until heartbeats resume. Meanwhile the portfolio simply holds the last known target (the strategy holds positions for days/weeks, so a stale-but-recent target is the correct fallback).
2. **Prevention (proactive):** a `/armed` Telegram command records when you (re)created the alert. The bot reminds you at day 53 and daily from day 57: *"TV alert expires ~day 60 — re-arm now."*

---

## 5. Symbol mapping & target model

Config (`config.yaml`), the only place TV vocabulary meets Hyperliquid vocabulary:

```yaml
symbol_map:
  BTCUSD:  BTC
  ETHUSD:  ETH
  SOLUSD:  SOL
  XRPUSD:  XRP
  BNBUSD:  BNB
  DOGEUSD: DOGE
  GOLD:    PAXG      # trend-managed gold sleeve → PAXG-USD perp
  USD:     null      # cash → no position, hold USDC margin
```

- An **unknown symbol** in a payload → reject the signal, keep the previous target, Telegram CRITICAL. (This is what happens if the strategy's ticker inputs are edited without updating the bot.)
- Weights must be > 0, sum to 1.0 ± 0.001, max 4 legs → else reject + CRITICAL.
- **Deployment fraction:** targets apply to `account_equity × deploy_fraction` (default **0.98**). The ~2% buffer absorbs fees/funding and keeps 1x cross positions away from 100% margin utilization. At 1x, liquidation risk is effectively nil, but we never run at the exact ceiling.

---

## 6. The reconciler (heart of the system)

Runs as a single-flight async job (never two concurrent runs), triggered by:

1. A new accepted signal (webhook),
2. A **15-minute periodic tick** (safety net — catches drift, missed fills, restarts),
3. Manual `/reconcile` from Telegram.

Algorithm per run:

1. Fetch account state from Hyperliquid (`clearinghouseState`): equity, margin, open positions, open orders.
2. **Anomaly checks** (account is 100% bot-owned):
   - Position in a coin outside the universe → CRITICAL, do not touch it automatically, pause trading until acknowledged.
   - Any short position → CRITICAL + flatten it (strategy is long-only; a short can only be a bug or foreign interference).
   - Equity dropped > 10% since the previous run → CRITICAL, pause, require `/resume`. (A 12h bar can be violent, but >10% between 15-min ticks means something is wrong.)
3. Cancel any stale open orders from previous runs.
4. Compute target notional per coin = `weight × equity × deploy_fraction` at mark price.
5. Diff vs current positions → per-coin delta. Trade only deltas where |Δ| > **max(2% of equity, $15)** — below that, drift is cheaper than churn (HL min order is $10 notional; taker fee ≈ 0.045%).
6. Order the trades **sells first, then buys** (frees margin before consuming it).
7. Execute each leg (see §7), then re-fetch positions and verify convergence. If not converged, retry the residual (bounded, see §8).
8. Persist the run (inputs, plan, fills, outcome) to SQLite; Telegram summary if anything traded or failed.

Idempotency: signals are deduped on `seq`; reconcile runs are pure functions of (last target, live account state), so re-running after any crash is always safe.

---

## 7. Execution

- 1x leverage set explicitly per coin at startup (cross margin).
- Each leg: **IOC limit order** priced aggressively through the book with a **slippage cap of 0.5%** from mid (SDK `market_open`/`market_close` with `slippage=0.005`). At <$100k, every coin in the basket including PAXG absorbs this in one order.
- Unfilled remainder after IOC → retry up to 3× with fresh price; still unfilled → leave to the next reconcile pass and WARN on Telegram.
- Sizes rounded to the coin's `szDecimals` (from exchange meta, fetched at startup and cached); prices rounded to valid tick. Rounding always toward *smaller* position size.
- Fill results (avg price, fee) recorded per order in SQLite.

If capital later grows past ~$250k, add TWAP slicing for the thin books (PAXG, DOGE) — noted as a future flag, not built in v1.

---

## 8. Failure modes → designed responses

| # | Failure | Detection | Response |
|---|---|---|---|
| 1 | TV alert expires (Essential plan) | No heartbeat 13h | CRITICAL every 6h until re-armed; hold last target; `/armed` countdown reminders at day 53+ |
| 2 | Single webhook lost (TV no-retry, network blip) | Gap in `seq` | Self-heals on next heartbeat; INFO note in daily summary |
| 3 | TradingView sends duplicate | `seq` already seen | Dropped silently (idempotent) |
| 4 | Hyperliquid API down / timing out | Request errors | Exponential backoff (1s→60s, jittered), indefinitely; WARN after 5 min, CRITICAL after 30 min; reconcile resumes automatically |
| 5 | Order rejected / partial fill | Fill check per leg | 3 in-run retries → residual handled by next 15-min pass |
| 6 | Bot crashes mid-rebalance | Docker `restart: always` | On boot: load last target from SQLite → immediate reconcile; sells-first ordering means a half-done run is never over-leveraged |
| 7 | VPS dies entirely | **healthchecks.io** dead-man ping (bot pings every 5 min; missing pings → email+Telegram from the *external* service) | Manual intervention; positions are held, not orphaned — worst case is a stale allocation, bounded by you being notified within ~10 min |
| 8 | Bad payload (schema, weights, unknown symbol, bad token) | Validation at ingress | Reject, keep previous target, CRITICAL (except bad token → log + drop, don't spam) |
| 9 | Foreign position / short appears in account | Reconcile anomaly check | CRITICAL + auto-pause (foreign) or auto-flatten (short) |
| 10 | Equity anomaly (>10% drop between runs) | Reconcile pre-check | Auto-pause + CRITICAL, requires `/resume` |
| 11 | Clock/timing skew, TV lateness | Heartbeat timestamps logged | Watchdog threshold has 1h grace (13h, not 12h) |
| 12 | Secrets leak / server compromise | — | **API (agent) wallet only** — it can trade but can never withdraw funds; master key never touches the VPS |

---

## 9. Security

- **Hyperliquid API/agent wallet**: generated from the master account, authorized for trading only. Withdrawals are cryptographically impossible from the VPS. Master wallet key stays in your hardware/wallet, never on the server.
- Secrets (`API wallet key`, webhook token, Telegram token) in a root-owned `.env` (mode 600), injected via Docker env — never in git.
- Webhook hardening: HTTPS only (Caddy + Let's Encrypt), long random path (`/webhook/<random>`), token check, and firewall allowlist of TradingView's published webhook egress IPs (52.89.214.238, 34.212.75.30, 54.218.53.128, 52.32.178.7 — re-verified at build time).
- VPS: SSH keys only, no password auth; ufw (443 + SSH only); fail2ban; unattended-upgrades.
- Telegram commands accepted **only** from your numeric chat ID.

---

## 10. Telegram interface

**Outbound:**
- Rebalance executed: old → new allocation, fills, avg prices, fees, slippage.
- Daily summary (09:00 UTC): equity, PnL, current vs target allocation, funding paid, last heartbeat age, alert-expiry countdown.
- WARN / CRITICAL as described above; CRITICALs repeat until resolved or acknowledged.

**Inbound commands:** `/status` (heartbeat age, target, positions, drift, equity) · `/positions` · `/reconcile` (force run) · `/pause` / `/resume` (kill switch — pause halts trading, never touches positions) · `/armed` (reset alert-expiry countdown) · `/help`.

---

## 11. Operations

- **Deploy:** git repo → VPS via a one-command deploy script (`git pull && docker compose up -d --build`). Compose: `restart: always`; Docker starts on boot.
- **Health:** `/healthz` endpoint (checks DB, HL API reachability, heartbeat age) polled by the dead-man ping.
- **Logs:** structured JSON to stdout → Docker log rotation (10MB × 5). Every decision (signal in, plan, order, fill, skip-reason) is logged and queryable in SQLite forever.
- **Backups:** nightly SQLite snapshot to the VPS disk + weekly copy off-box. (Loss of the DB is non-fatal: the next heartbeat restores the target; history is what's being protected.)
- **Config changes** (thresholds, symbol map) via `config.yaml` + restart; secrets via `.env`.

---

## 12. Testing & rollout plan

Phased — the bot earns trust before it touches real size:

1. **Unit tests**: payload validation, symbol mapping, target math, diff/threshold logic, rounding (golden tests per coin), sells-before-buys ordering, dedupe.
2. **Replay test**: feed a recorded sequence of heartbeats (including gaps, duplicates, malformed payloads, mid-sequence "crash") through the full pipeline against a mocked exchange; assert final positions.
3. **Hyperliquid testnet**: full system end-to-end with real TradingView webhooks firing at the testnet deployment for several days. Verifies TV timing, alert quota, TLS, Telegram, watchdog.
4. **Mainnet dry-run mode** (`DRY_RUN=true`): connected to the real account read-only; logs and Telegrams the orders it *would* place, for ~1 week across at least one real allocation change.
5. **Mainnet small** (~$1–5k) for 2+ weeks including at least one rotation.
6. **Full capital.** Scale-up is just a deposit; the bot sizes from live equity.

Exit criteria for each phase: zero unexplained divergence between intended and actual allocation.

---

## 13. Known accepted trade-offs

- **Funding rates**: 1x long perps pay (or receive) funding vs true spot. At this basket's typical rates this is roughly comparable to CEX spot friction; funding paid is tracked and shown in the daily summary so it's a measured cost, not a surprise.
- **XAUT signal vs PAXG execution**: the gold trend signal is computed on XAUT, the position is PAXG. Both track the same ounce; basis noise is negligible at trend timescale.
- **No look-back on missed rotations**: if the entire system is down across a rotation, the bot converges to the *current* target on recovery — it does not attempt to replay intermediate states (correct behavior: only the present target matters).
- **TradingView is a single point of signal truth.** We do not re-implement the strategy server-side to cross-check it. The heartbeat + watchdog design bounds the damage of TV failure to "hold last allocation + you're notified within 13h," which is acceptable for a 12h-bar strategy.

---

## 14. Build plan (order of work)

1. Repo scaffold: Python project, Docker Compose (app + Caddy), config/secrets layout, SQLite schema.
2. Core domain: payload schema + validation, symbol map, target/diff math + unit tests.
3. Hyperliquid client wrapper: meta cache, rounding, IOC execution, retries — against **testnet**.
4. Reconciler + watchdog + state persistence + replay tests.
5. Telegram bot (outbound first, then commands).
6. Pine script alert change (final version of §4 sketch) + single TV alert setup.
7. VPS provisioning (scripted, documented), deploy, testnet soak.
8. Dry-run on mainnet → small capital → full capital per §12.

Estimated code size: ~1,500–2,500 lines of Python plus tests — deliberately small enough to be fully auditable.
