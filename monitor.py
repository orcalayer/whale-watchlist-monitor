"""Whale watchlist monitor.

Polls the OrcaLayer API for a list of wallets and alerts on position
changes: new positions, closed positions, and size changes above a
configured threshold. Alerts go to stdout, Telegram and/or Discord.

Usage:
    cp config.example.yaml config.yaml   # edit wallets + API key
    python monitor.py
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

import httpx
import yaml

from orcalayer import OrcaLayer, OrcaLayerError

CONFIG_PATH = Path(__file__).parent / "config.yaml"
STATE_PATH = Path(__file__).parent / "state.json"


# ── State ────────────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ── Snapshot & diff ──────────────────────────────────────────────────────


def position_key(pos: dict) -> str:
    # condition_id + side identifies one leg of a market for this wallet.
    return f"{pos.get('condition_id', '')}:{pos.get('side', '')}"


def snapshot_wallet(client: OrcaLayer, wallet: str) -> dict:
    """One wallet's current positions, keyed for diffing.

    TODO(batch): replace per-wallet GETs with the planned batch endpoint
    (POST /api/public/v1/wallets/overview + /wallets/positions) once it
    ships — one request for the whole watchlist instead of 2 per wallet.
    """
    overview = client.wallet_overview(wallet)
    positions = client.wallet_positions(wallet, limit=500)
    name = (
        overview.get("profile", {}).get("name")
        or overview.get("name")
        or wallet[:10]
    )
    return {
        "name": name,
        "positions": {
            position_key(p): {
                "question": p.get("question", "?"),
                "outcome": p.get("outcome", "?"),
                "tokens": float(p.get("tokens") or 0),
                "current_value": float(p.get("current_value") or 0),
                "avg_entry": p.get("avg_entry"),
            }
            for p in positions.get("positions", [])
            if float(p.get("tokens") or 0) > 0
        },
    }


def diff_wallet(wallet: str, name: str, prev: dict, cur: dict, threshold_pct: float) -> list[str]:
    alerts: list[str] = []
    label = f"{name} ({wallet[:8]}...)"

    for key, pos in cur.items():
        if key not in prev:
            alerts.append(
                f"NEW position — {label}\n"
                f"  {pos['question']} [{pos['outcome']}]\n"
                f"  {pos['tokens']:,.0f} tokens, value ${pos['current_value']:,.2f}, "
                f"entry {pos['avg_entry']}"
            )
        else:
            old_tokens = prev[key]["tokens"]
            if old_tokens > 0:
                change_pct = (pos["tokens"] - old_tokens) / old_tokens * 100
                if abs(change_pct) >= threshold_pct:
                    direction = "increased" if change_pct > 0 else "reduced"
                    alerts.append(
                        f"SIZE {direction} {change_pct:+.1f}% — {label}\n"
                        f"  {pos['question']} [{pos['outcome']}]\n"
                        f"  {old_tokens:,.0f} -> {pos['tokens']:,.0f} tokens, "
                        f"value ${pos['current_value']:,.2f}"
                    )

    for key, pos in prev.items():
        if key not in cur:
            alerts.append(
                f"CLOSED position — {label}\n"
                f"  {pos['question']} [{pos['outcome']}]\n"
                f"  was {pos['tokens']:,.0f} tokens (${pos['current_value']:,.2f})"
            )

    return alerts


# ── Outputs ──────────────────────────────────────────────────────────────


def send_alerts(alerts: list[str], outputs: dict) -> None:
    text = "\n\n".join(alerts)

    if outputs.get("stdout", True):
        print(text, flush=True)

    tg = outputs.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        try:
            # Plain text (no parse_mode) — market questions may contain
            # characters that break HTML/Markdown parsing.
            httpx.post(
                f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                json={"chat_id": tg["chat_id"], "text": text[:4000]},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            print(f"[warn] telegram send failed: {exc}", file=sys.stderr)

    discord = outputs.get("discord") or {}
    if discord.get("webhook_url"):
        try:
            httpx.post(
                discord["webhook_url"],
                json={"content": text[:1900]},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            print(f"[warn] discord send failed: {exc}", file=sys.stderr)


# ── Main loop ────────────────────────────────────────────────────────────


def main() -> None:
    if not CONFIG_PATH.exists():
        sys.exit("config.yaml not found — copy config.example.yaml and edit it.")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    api_key = cfg.get("api_key") or ""
    if not api_key:
        sys.exit(
            "api_key missing in config.yaml. This tool requires an OrcaLayer "
            "Premium API key — https://orcalayer.com/pricing"
        )

    wallets = [w.lower() for w in cfg.get("wallets", [])]
    if not wallets:
        sys.exit("No wallets configured in config.yaml.")

    poll_interval = int(cfg.get("poll_interval", 300))
    threshold_pct = float(cfg.get("alert_threshold_pct", 10))
    outputs = cfg.get("outputs", {})

    client = OrcaLayer(api_key=api_key)
    state = load_state()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    print(
        f"Watching {len(wallets)} wallet(s), poll every {poll_interval}s, "
        f"size threshold {threshold_pct}%",
        flush=True,
    )

    while running:
        for wallet in wallets:
            try:
                snap = snapshot_wallet(client, wallet)
            except OrcaLayerError as exc:
                print(f"[warn] {wallet[:10]}: {exc}", file=sys.stderr)
                continue

            prev = state.get(wallet)
            if prev is None:
                # First sighting: record baseline silently, no alert flood.
                print(
                    f"[init] {snap['name']} ({wallet[:10]}): "
                    f"{len(snap['positions'])} open position(s) recorded",
                    flush=True,
                )
            else:
                alerts = diff_wallet(
                    wallet, snap["name"], prev["positions"], snap["positions"], threshold_pct
                )
                if alerts:
                    send_alerts(alerts, outputs)

            state[wallet] = snap
            save_state(state)

        for _ in range(poll_interval):
            if not running:
                break
            time.sleep(1)

    print("Stopped.", flush=True)


if __name__ == "__main__":
    main()
