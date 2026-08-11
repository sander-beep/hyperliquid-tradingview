"""Configuration: config.yaml (non-secret) + environment (.env, secrets)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and not v:
        raise RuntimeError(f"missing required environment variable {name}")
    return v or ""


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # secrets / environment
    hl_api_wallet_key: str
    hl_account_address: str
    hl_testnet: bool
    dry_run: bool
    webhook_token: str
    webhook_path_secret: str
    telegram_bot_token: str
    telegram_chat_id: int
    healthchecks_url: str

    # config.yaml
    strategy_id: str
    symbol_map: dict[str, str | None]
    deploy_fraction: float
    threshold_pct: float
    threshold_usd: float
    min_order_usd: float
    slippage: float
    leg_retries: int
    max_legs: int
    weight_sum_tolerance: float
    reconcile_interval_s: int
    heartbeat_stale_s: int
    critical_repeat_s: int
    equity_drop_pause_pct: float
    armed_warn_day: int
    armed_nag_day: int
    armed_expiry_day: int
    daily_summary_utc: str
    deadman_ping_s: int
    db_path: str

    @property
    def universe(self) -> frozenset[str]:
        """All Hyperliquid coins the bot may ever hold."""
        return frozenset(c for c in self.symbol_map.values() if c)


def load_config(config_file: str | Path = "config.yaml") -> Config:
    with open(config_file) as f:
        y = yaml.safe_load(f)

    return Config(
        hl_api_wallet_key=_env("HL_API_WALLET_KEY", required=True),
        hl_account_address=_env("HL_ACCOUNT_ADDRESS", required=True),
        hl_testnet=_env_bool("HL_TESTNET", True),
        dry_run=_env_bool("DRY_RUN", False),
        webhook_token=_env("WEBHOOK_TOKEN", required=True),
        webhook_path_secret=_env("WEBHOOK_PATH_SECRET", required=True),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", required=True),
        telegram_chat_id=int(_env("TELEGRAM_CHAT_ID", required=True)),
        healthchecks_url=_env("HEALTHCHECKS_URL", ""),
        strategy_id=str(y["strategy_id"]),
        symbol_map={str(k): (str(v) if v is not None else None) for k, v in y["symbol_map"].items()},
        deploy_fraction=float(y["deploy_fraction"]),
        threshold_pct=float(y["rebalance_threshold_pct"]),
        threshold_usd=float(y["rebalance_threshold_usd"]),
        min_order_usd=float(y["min_order_usd"]),
        slippage=float(y["slippage"]),
        leg_retries=int(y["leg_retries"]),
        max_legs=int(y["max_legs"]),
        weight_sum_tolerance=float(y["weight_sum_tolerance"]),
        reconcile_interval_s=int(y["reconcile_interval_seconds"]),
        heartbeat_stale_s=int(float(y["heartbeat_stale_hours"]) * 3600),
        critical_repeat_s=int(float(y["critical_repeat_hours"]) * 3600),
        equity_drop_pause_pct=float(y["equity_drop_pause_pct"]),
        armed_warn_day=int(y["armed_warn_day"]),
        armed_nag_day=int(y["armed_nag_day"]),
        armed_expiry_day=int(y["armed_expiry_day"]),
        daily_summary_utc=str(y["daily_summary_utc"]),
        deadman_ping_s=int(y["deadman_ping_seconds"]),
        db_path=str(y["db_path"]),
    )
