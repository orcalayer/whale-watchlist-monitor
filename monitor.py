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
import logging
import re
import signal
import sys
import time
from pathlib import Path

import httpx
import yaml

from orcalayer import OrcaLayer, OrcaLayerError

CONFIG_PATH = Path(__file__).parent / "config.yaml"
STATE_PATH = Path(__file__).parent / "state.json"

TELEGRAM_LIMIT = 4096
DISCORD_LIMIT = 2000
# Reserve room for a "(NN/NN)\n\n" part header added when a batch splits.
PART_HEADER_RESERVE = 12

WALLET_RE = re.compile(r"0x[0-9a-fA-F]{40}")

logger = logging.getLogger("whale-monitor")


# ── Config ───────────────────────────────────────────────────────────────


def validate_config(cfg: object) -> list[str]:
    """Human-readable config errors, empty list when the config is valid."""
    if not isinstance(cfg, dict):
        return ["config.yaml: top level must be a mapping (key: value pairs)"]
    errors: list[str] = []

    api_key = cfg.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        errors.append(
            "api_key: required (string). This tool needs an OrcaLayer Premium "
            "API key — https://orcalayer.com/pricing"
        )

    wallets = cfg.get("wallets")
    if not isinstance(wallets, list) or not wallets:
        errors.append("wallets: must be a non-empty list of 0x wallet addresses")
    else:
        for w in wallets:
            if not isinstance(w, str) or not WALLET_RE.fullmatch(w.strip()):
                errors.append(
                    f"wallets: {w!r} is not a valid address "
                    "(expected 0x followed by 40 hex characters)"
                )

    poll = cfg.get("poll_interval", 300)
    if isinstance(poll, bool) or not isinstance(poll, int) or poll <= 0:
        errors.append(
            f"poll_interval: must be a positive integer number of seconds, got {poll!r}"
        )

    threshold = cfg.get("alert_threshold_pct", 10)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or threshold < 0
    ):
        errors.append(
            f"alert_threshold_pct: must be a non-negative number (percent), got {threshold!r}"
        )

    outputs = cfg.get("outputs", {})
    if outputs is not None and not isinstance(outputs, dict):
        errors.append("outputs: must be a mapping (stdout / telegram / discord)")
    elif isinstance(outputs, dict):
        tg = outputs.get("telegram")
        if tg is not None and not isinstance(tg, dict):
            errors.append("outputs.telegram: must be a mapping with bot_token and chat_id")
        discord = outputs.get("discord")
        if discord is not None and not isinstance(discord, dict):
            errors.append("outputs.discord: must be a mapping with webhook_url")

    return errors


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


def chunk_alerts(alerts: list[str], limit: int) -> list[str]:
    """Pack alerts into messages of at most ``limit`` characters.

    Splits only on alert boundaries; a single alert longer than the limit
    is truncated with an ellipsis. When more than one chunk results, each
    is prefixed with a part header like ``(2/3)``.
    """
    budget = limit - PART_HEADER_RESERVE
    chunks: list[str] = []
    current = ""
    for alert in alerts:
        if len(alert) > budget:
            alert = alert[: budget - 3] + "..."
        if current and len(current) + 2 + len(alert) > budget:
            chunks.append(current)
            current = alert
        else:
            current = f"{current}\n\n{alert}" if current else alert
    if current:
        chunks.append(current)

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"({i}/{total})\n\n{c}" for i, c in enumerate(chunks, 1)]
    return chunks


def send_alerts(alerts: list[str], outputs: dict) -> None:
    if outputs.get("stdout", True):
        print("\n\n".join(alerts), flush=True)

    tg = outputs.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        for message in chunk_alerts(alerts, TELEGRAM_LIMIT):
            try:
                # Plain text (no parse_mode) — market questions may contain
                # characters that break HTML/Markdown parsing.
                httpx.post(
                    f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage",
                    json={"chat_id": tg["chat_id"], "text": message},
                    timeout=15,
                )
            except httpx.HTTPError as exc:
                logger.warning("telegram send failed: %s", exc)

    discord = outputs.get("discord") or {}
    if discord.get("webhook_url"):
        for message in chunk_alerts(alerts, DISCORD_LIMIT):
            try:
                httpx.post(
                    discord["webhook_url"],
                    json={"content": message},
                    timeout=15,
                )
            except httpx.HTTPError as exc:
                logger.warning("discord send failed: %s", exc)


# ── Main loop ────────────────────────────────────────────────────────────


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not CONFIG_PATH.exists():
        sys.exit("config.yaml not found — copy config.example.yaml and edit it.")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))

    errors = validate_config(cfg)
    if errors:
        for err in errors:
            logger.error("config: %s", err)
        sys.exit(f"config.yaml has {len(errors)} error(s) — see above.")

    wallets = [w.strip().lower() for w in cfg["wallets"]]
    poll_interval = int(cfg.get("poll_interval", 300))
    threshold_pct = float(cfg.get("alert_threshold_pct", 10))
    outputs = cfg.get("outputs") or {}

    client = OrcaLayer(api_key=cfg["api_key"])
    state = load_state()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    logger.info(
        "watching %d wallet(s), poll every %ds, size threshold %s%%",
        len(wallets), poll_interval, threshold_pct,
    )

    while running:
        for wallet in wallets:
            try:
                snap = snapshot_wallet(client, wallet)
            except OrcaLayerError as exc:
                logger.warning("%s: %s", wallet[:10], exc)
                continue

            prev = state.get(wallet)
            if prev is None:
                # First sighting: record baseline silently, no alert flood.
                logger.info(
                    "%s (%s): baseline of %d open position(s) recorded",
                    snap["name"], wallet[:10], len(snap["positions"]),
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

    logger.info("stopped")


if __name__ == "__main__":
    main()
