#!/usr/bin/env python3
"""Diagnose "equity is $0": where the USDC actually is, on both networks.

The bot's equity is user_state(HL_ACCOUNT_ADDRESS).marginSummary.accountValue
on the network HL_TESTNET selects — i.e. the PERPS balance of the master
account. It reads $0 whenever the money is somewhere else: spot, a subaccount,
a vault, a different address, or the other network.

Run on the VPS (scripts/ is not in the image, so bind-mount it):

    cd /opt/rsps-bot
    docker compose run --rm --no-deps -v ./scripts:/srv/scripts \
        app python scripts/check-balance.py

or locally with HL_ACCOUNT_ADDRESS exported.
"""
from __future__ import annotations

import os
import sys

from hyperliquid.info import Info
from hyperliquid.utils import constants

try:
    from eth_account import Account
except ImportError:  # pragma: no cover - only used for the agent-key check
    Account = None


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def probe(label: str, base_url: str, address: str) -> dict:
    """Print everything that could be holding USDC on one network."""
    print(f"\n=== {label} ===")
    info = Info(base_url, skip_ws=True)
    found = {"perps": 0.0, "spot": 0.0, "subaccounts": 0.0, "vaults": 0.0}

    try:
        perp = info.user_state(address)
        found["perps"] = _f(perp["marginSummary"]["accountValue"])
        n_pos = len([p for p in perp.get("assetPositions", [])
                     if _f(p.get("position", {}).get("szi")) != 0])
        print(f"  perps accountValue : {found['perps']:>12,.2f} USDC   <- THIS is the bot's equity")
        print(f"  perps withdrawable : {_f(perp.get('withdrawable')):>12,.2f} USDC")
        print(f"  open positions     : {n_pos}")
    except Exception as e:
        print(f"  perps query FAILED: {e}")

    try:
        spot = info.spot_user_state(address)
        bal = {b["coin"]: _f(b["total"]) for b in spot.get("balances", []) if _f(b["total"]) != 0}
        found["spot"] = bal.get("USDC", 0.0)
        print(f"  spot balances      : {bal or 'none'}")
    except Exception as e:
        print(f"  spot query FAILED: {e}")

    try:
        subs = info.query_sub_accounts(address) or []
        for s in subs:
            eq = _f(s.get("clearinghouseState", {}).get("marginSummary", {}).get("accountValue"))
            found["subaccounts"] += eq
            print(f"  subaccount {s.get('name', '?')} ({s.get('subAccountUser', '?')}): {eq:,.2f} USDC perps")
        if not subs:
            print("  subaccounts        : none")
    except Exception as e:
        print(f"  subaccount query failed (ok to ignore): {e}")

    try:
        vaults = info.user_vault_equities(address) or []
        for v in vaults:
            eq = _f(v.get("equity"))
            found["vaults"] += eq
            print(f"  vault {v.get('vaultAddress', '?')}: {eq:,.2f} USDC")
        if not vaults:
            print("  vault deposits     : none")
    except Exception as e:
        print(f"  vault query failed (ok to ignore): {e}")

    return found


def main() -> int:
    address = os.environ.get("HL_ACCOUNT_ADDRESS", "").strip()
    if not address:
        print("HL_ACCOUNT_ADDRESS is not set", file=sys.stderr)
        return 1

    raw_testnet = os.environ.get("HL_TESTNET")
    # Mirrors app/config.py: unset/empty defaults to TESTNET.
    testnet = True if raw_testnet in (None, "") else raw_testnet.strip().lower() in ("1", "true", "yes", "on")

    print(f"HL_ACCOUNT_ADDRESS = {address}")
    print(f"HL_TESTNET         = {raw_testnet!r} -> bot is reading {'TESTNET' if testnet else 'MAINNET'}"
          + ("  (UNSET/EMPTY: defaults to testnet!)" if raw_testnet in (None, "") else ""))

    key = os.environ.get("HL_API_WALLET_KEY", "").strip()
    agent = None
    if key and Account is not None:
        try:
            agent = Account.from_key(key).address
            print(f"API wallet address = {agent}")
            if agent.lower() == address.lower():
                print("  !! MISMATCH: HL_ACCOUNT_ADDRESS is the API/agent wallet, not the master")
                print("     account that holds the USDC. Set it to your master address.")
        except Exception as e:
            print(f"  could not derive API wallet address: {e}")

    results = {
        "TESTNET": probe("TESTNET", constants.TESTNET_API_URL, address),
        "MAINNET": probe("MAINNET", constants.MAINNET_API_URL, address),
    }

    # ---- verdict ----------------------------------------------------------
    print("\n=== VERDICT ===")
    bot_net = "TESTNET" if testnet else "MAINNET"
    other_net = "MAINNET" if testnet else "TESTNET"
    here = results[bot_net]

    if here["perps"] > 0:
        print(f"  Perps equity on {bot_net} is {here['perps']:,.2f} USDC — the bot should see this.")
        print("  If it still reports $0, the running container has different env than this run.")
        return 0

    if here["spot"] > 0:
        print(f"  Money is in SPOT on {bot_net} ({here['spot']:,.2f} USDC).")
        print("  FIX: Hyperliquid UI -> Portfolio -> Transfer -> Spot to Perps.")
    if here["subaccounts"] > 0:
        print(f"  Money is in a SUBACCOUNT on {bot_net} ({here['subaccounts']:,.2f} USDC).")
        print("  FIX: transfer to the master account, or point HL_ACCOUNT_ADDRESS at the subaccount.")
    if here["vaults"] > 0:
        print(f"  Money is in a VAULT on {bot_net} ({here['vaults']:,.2f} USDC).")
        print("  FIX: withdraw from the vault back to perps.")
    if sum(results[other_net].values()) > 0:
        print(f"  Money is on {other_net} (total {sum(results[other_net].values()):,.2f} USDC), "
              f"but the bot is pointed at {bot_net}.")
        print(f"  FIX: set HL_TESTNET={'false' if other_net == 'MAINNET' else 'true'} in .env, "
              "then ./scripts/deploy.sh")
    if not any(sum(r.values()) for r in results.values()):
        print("  Nothing found on either network for this address.")
        print("  The address is probably wrong — check it against the wallet in the Hyperliquid UI.")

    # Agent approval is a separate failure (orders reject), but check it while we're here.
    if agent and here["perps"] == 0:
        try:
            info = Info(constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL, skip_ws=True)
            approved = [a.get("address", "").lower() for a in (info.extra_agents(address) or [])]
            if approved and agent.lower() not in approved:
                print(f"  NOTE: API wallet {agent} is not in the approved agents for this account "
                      f"on {bot_net} — trades would be rejected too.")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
