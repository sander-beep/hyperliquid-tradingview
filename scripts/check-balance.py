#!/usr/bin/env python3
"""Print spot vs perps balances for the configured account, on both networks.

Run on the VPS (where .env lives):
    docker compose run --rm bot python scripts/check-balance.py
or locally with HL_ACCOUNT_ADDRESS exported.
"""
from __future__ import annotations

import os
import sys

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.utils import constants


def show(label: str, base_url: str, address: str) -> None:
    info = Info(base_url, skip_ws=True)
    perp = info.user_state(address)
    spot = info.spot_user_state(address)

    equity = float(perp["marginSummary"]["accountValue"])
    withdrawable = float(perp.get("withdrawable", 0) or 0)
    spot_bal = {b["coin"]: float(b["total"]) for b in spot.get("balances", []) if float(b["total"]) != 0}

    print(f"\n=== {label} ===")
    print(f"  perps accountValue : {equity:,.2f} USDC   (this is what the bot uses as equity)")
    print(f"  perps withdrawable : {withdrawable:,.2f} USDC")
    print(f"  spot balances      : {spot_bal or 'none'}")
    if equity == 0 and spot_bal.get("USDC", 0) > 0:
        print("  -> Funds are in SPOT. Transfer USDC Spot -> Perps in the Hyperliquid UI.")


def main() -> int:
    address = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip()
    if not address:
        print("HL_ACCOUNT_ADDRESS is not set", file=sys.stderr)
        return 1
    print(f"HL_ACCOUNT_ADDRESS = {address}")
    print(f"HL_TESTNET         = {os.environ.get('HL_TESTNET', '(unset)')}")

    key = os.environ.get("HL_API_WALLET_KEY", "").strip()
    if key:
        agent = Account.from_key(key).address
        print(f"API wallet address = {agent}")
        if agent.lower() == address.lower():
            print("  -> MISMATCH: HL_ACCOUNT_ADDRESS is the API wallet, not your master")
            print("     account. Set it to the account that actually holds the USDC.")

    show("TESTNET", constants.TESTNET_API_URL, address)
    show("MAINNET", constants.MAINNET_API_URL, address)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
