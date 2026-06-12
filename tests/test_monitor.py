"""Offline tests: config validation, diff logic, message chunking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402

import monitor  # noqa: E402
from monitor import (  # noqa: E402
    DISCORD_LIMIT,
    TELEGRAM_LIMIT,
    chunk_alerts,
    diff_wallet,
    snapshot_wallet,
    validate_config,
)
from orcalayer import OrcaLayerError  # noqa: E402

VALID_CFG = {
    "api_key": "orc_test",
    "wallets": ["0x" + "a" * 40],
    "poll_interval": 300,
    "alert_threshold_pct": 10,
    "outputs": {"stdout": True},
}


def pos(tokens=100.0, value=50.0):
    return {
        "question": "Will X happen?",
        "outcome": "Yes",
        "tokens": tokens,
        "current_value": value,
        "avg_entry": 0.5,
    }


# ── validate_config ──────────────────────────────────────────────────────


def test_valid_config_passes():
    assert validate_config(VALID_CFG) == []


def test_missing_api_key():
    cfg = {**VALID_CFG, "api_key": ""}
    errors = validate_config(cfg)
    assert any("api_key" in e and "pricing" in e for e in errors)


def test_bad_wallet_address():
    cfg = {**VALID_CFG, "wallets": ["not-an-address"]}
    errors = validate_config(cfg)
    assert any("not-an-address" in e for e in errors)


def test_empty_wallets():
    cfg = {**VALID_CFG, "wallets": []}
    assert any("wallets" in e for e in validate_config(cfg))


def test_bad_poll_interval():
    for bad in (0, -5, "300", True, 1.5):
        errors = validate_config({**VALID_CFG, "poll_interval": bad})
        assert any("poll_interval" in e for e in errors), f"accepted {bad!r}"


def test_bad_threshold():
    for bad in (-1, "ten", False):
        errors = validate_config({**VALID_CFG, "alert_threshold_pct": bad})
        assert any("alert_threshold_pct" in e for e in errors), f"accepted {bad!r}"


def test_non_dict_config():
    assert validate_config([1, 2]) != []


# ── diff_wallet ──────────────────────────────────────────────────────────


def test_diff_new_closed_size():
    prev = {"cid1:token1": pos(100), "cid2:token2": pos(50)}
    cur = {"cid1:token1": pos(200), "cid3:token1": pos(10)}
    alerts = diff_wallet("0xabcdef12", "Whale", prev, cur, threshold_pct=10)
    kinds = sorted(a.split(" ")[0] for a in alerts)
    assert kinds == ["CLOSED", "NEW", "SIZE"]


def test_diff_below_threshold_silent():
    prev = {"cid1:token1": pos(100)}
    cur = {"cid1:token1": pos(105)}
    assert diff_wallet("0xabcdef12", "W", prev, cur, threshold_pct=10) == []


# ── chunk_alerts ─────────────────────────────────────────────────────────


def make_alerts(n):
    return [
        f"NEW position — Whale{i} (0xabcdef{i:02d}...)\n"
        f"  Will market number {i} resolve YES before the deadline?\n"
        f"  {i * 1000:,} tokens, value ${i * 500:,.2f}, entry 0.5"
        for i in range(n)
    ]


def test_single_short_batch_no_part_header():
    chunks = chunk_alerts(make_alerts(2), TELEGRAM_LIMIT)
    assert len(chunks) == 1
    assert not chunks[0].startswith("(")


def test_35_alerts_telegram_and_discord():
    alerts = make_alerts(35)
    for limit in (TELEGRAM_LIMIT, DISCORD_LIMIT):
        chunks = chunk_alerts(alerts, limit)
        assert len(chunks) > 1
        total = len(chunks)
        for i, chunk in enumerate(chunks, 1):
            assert len(chunk) <= limit, f"chunk {i} exceeds {limit}"
            assert chunk.startswith(f"({i}/{total})")
        # No alert lost or split mid-alert: every alert appears intact once.
        joined = "\n".join(chunks)
        for alert in alerts:
            assert alert in joined


def test_oversize_single_alert_truncated():
    huge = "NEW position — " + "x" * 10000
    chunks = chunk_alerts([huge], DISCORD_LIMIT)
    assert len(chunks) == 1
    assert len(chunks[0]) <= DISCORD_LIMIT
    assert chunks[0].endswith("...")


def test_telegram_pairing_validation():
    cfg = {**VALID_CFG, "outputs": {"telegram": {"bot_token": "x", "chat_id": ""}}}
    errors = validate_config(cfg)
    assert any("bot_token and chat_id" in e for e in errors)
    cfg_ok = {**VALID_CFG, "outputs": {"telegram": {"bot_token": "", "chat_id": ""}}}
    assert validate_config(cfg_ok) == []


# ── snapshot_wallet (fake client) ────────────────────────────────────────


class FakeClient:
    def __init__(self, positions_pages):
        self._pages = positions_pages
        self._calls = 0

    def wallet_overview(self, wallet):
        return {"name": "FakeWhale"}

    def wallet_positions(self, wallet, limit=500, offset=0):
        page = self._pages[self._calls] if self._calls < len(self._pages) else {"positions": []}
        self._calls += 1
        return page


def full_page(n, start=0):
    return [
        {"condition_id": f"cid{start + i}", "side": "token1", "question": "Q?",
         "outcome": "Yes", "tokens": 10.0, "current_value": 5.0, "avg_entry": 0.5}
        for i in range(n)
    ]


def test_missing_positions_key_raises_not_empty():
    client = FakeClient([{"error": "degraded"}])
    with pytest.raises(OrcaLayerError) as exc:
        snapshot_wallet(client, "0x" + "a" * 40)
    assert "positions" in str(exc.value)


def test_pagination_collects_all_pages():
    client = FakeClient([
        {"positions": full_page(500)},
        {"positions": full_page(120, start=500)},
    ])
    snap = snapshot_wallet(client, "0x" + "a" * 40)
    assert len(snap["positions"]) == 620
    assert client._calls == 2


# ── delivery error visibility ────────────────────────────────────────────


def test_bad_token_logged_not_silent(monkeypatch, caplog):
    def fake_post(url, json=None, timeout=None):
        return httpx.Response(
            401, json={"ok": False, "description": "Unauthorized: bot token invalid"}
        )

    monkeypatch.setattr(monitor.httpx, "post", fake_post)
    with caplog.at_level("WARNING"):
        monitor.send_alerts(
            ["test alert"],
            {"stdout": False, "telegram": {"bot_token": "bad", "chat_id": "1"}},
        )
    assert any(
        "telegram send failed" in r.message and "Unauthorized" in r.message
        for r in caplog.records
    )


def test_429_retried_then_delivered(monkeypatch):
    calls = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 0}})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(monitor.httpx, "post", fake_post)
    monkeypatch.setattr(monitor.time, "sleep", lambda s: None)
    monitor.send_alerts(
        ["test alert"],
        {"stdout": False, "telegram": {"bot_token": "t", "chat_id": "1"}},
    )
    assert len(calls) == 2
