"""FastAPI app: webhook receiver + /healthz, and the lifespan that wires up
the reconciler, watchdog and Telegram bot."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from . import log
from .config import Config, load_config
from .db import Database
from .hl import HLClient
from .ingest import ingest
from .models import PayloadError
from .notify import Notifier
from .reconciler import Reconciler
from .state import State
from .telegram import TelegramBot
from .watchdog import Watchdog

logger = log.get("main")

MAX_BODY = 16 * 1024


class App:
    """Holds all long-lived components (attached to FastAPI state)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.state = State(self.db)
        self.notifier = Notifier(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.critical_repeat_s)
        self.hl = HLClient(cfg, self.notifier)
        self.reconciler = Reconciler(cfg, self.db, self.state, self.hl, self.notifier)
        self.watchdog = Watchdog(cfg, self.db, self.state, self.hl, self.notifier)
        self.telegram = TelegramBot(cfg, self.state, self.reconciler, self.watchdog, self.notifier, self.hl)
        self.tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.hl.start()
        self.tasks = [
            asyncio.create_task(self.reconciler.loop(), name="reconciler"),
            asyncio.create_task(self.watchdog.loop(), name="watchdog"),
            asyncio.create_task(self.telegram.loop(), name="telegram"),
        ]
        # Boot recovery: converge to the last persisted target immediately.
        self.reconciler.trigger("startup")
        mode = "DRY RUN" if self.cfg.dry_run else "LIVE"
        net = "testnet" if self.cfg.hl_testnet else "mainnet"
        target = self.state.get_target()
        await self.notifier.info(
            f"Bot started ({mode}, {net}). "
            + (f"Last target: {target.label()}" if target else "No target yet — waiting for first heartbeat.")
        )

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        await self.notifier.close()
        self.db.close()


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    log.setup_logging()
    cfg = load_config()
    app_obj = App(cfg)
    fastapi_app.state.app = app_obj
    await app_obj.start()
    logger.info("startup complete")
    try:
        yield
    finally:
        await app_obj.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.post("/webhook/{path_secret}")
async def webhook(path_secret: str, request: Request) -> Response:
    a: App = request.app.state.app
    if not hmac.compare_digest(path_secret, a.cfg.webhook_path_secret):
        return Response(status_code=404)

    body = await request.body()
    if len(body) > MAX_BODY:
        logger.warning("oversized webhook body dropped")
        return Response(status_code=413)

    try:
        status, target = ingest(body, a.cfg, a.db, a.state)
    except PayloadError as e:
        preview = body[:2048].decode("utf-8", errors="replace")
        a.db.insert_rejected(preview, e.reason)
        if e.critical:
            logger.error(f"payload rejected: {e.reason}")
            await a.notifier.critical(
                f"payload_{e.reason[:40]}",
                f"Webhook payload REJECTED: {e.reason}. Keeping previous target. "
                f"Payload: {preview[:400]}",
            )
            # ACK anyway — TradingView does not retry, and a 4xx only makes TV
            # eventually disable the alert.
            return Response(status_code=200)
        logger.warning(f"payload dropped: {e.reason}")
        return Response(status_code=403)

    if status == "duplicate":
        logger.info(f"duplicate signal seq={target.seq} dropped")
        return Response(status_code=200)

    logger.info(
        "heartbeat accepted",
        extra={"data": {"seq": target.seq, "changed": target.changed, "target": target.label()}},
    )
    a.reconciler.trigger("signal")
    return Response(status_code=200)


@app.get("/healthz")
async def healthz(request: Request) -> Response:
    a: App = request.app.state.app
    problems: list[str] = []
    try:
        a.db.kv_get("healthz_probe")
    except Exception as e:
        problems.append(f"db: {e}")
    if a.hl.last_ok is None:
        problems.append("hl: no successful API call yet")
    elif time.monotonic() - a.hl.last_ok > 1800:
        problems.append("hl: no successful API call in 30 min")
    age = a.state.heartbeat_age_s()
    if age is not None and age > a.cfg.heartbeat_stale_s:
        problems.append(f"heartbeat stale ({age / 3600:.1f}h)")
    if problems:
        return Response("; ".join(problems), status_code=503)
    return Response("ok", status_code=200)
