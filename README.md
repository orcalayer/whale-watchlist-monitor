# whale-watchlist-monitor

[![CI](https://github.com/orcalayer/whale-watchlist-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/orcalayer/whale-watchlist-monitor/actions/workflows/ci.yml)

Watch a list of Polymarket wallets and get alerts when they open, close or resize positions. Built on the [OrcaLayer API](https://orcalayer.com) via the [orcalayer](https://github.com/orcalayer/orcalayer-python) Python client. **Requires an OrcaLayer Premium API key** — get one at [orcalayer.com/pricing](https://orcalayer.com/pricing).

## What it does

Every `poll_interval` seconds the monitor fetches each watched wallet's overview and open positions, diffs them against the previous snapshot (stored locally in `state.json`) and sends an alert when it sees:

- a **new position** (market + outcome the wallet did not hold before),
- a **closed position** (held before, gone now),
- a **size change** of at least `alert_threshold_pct` percent.

Alerts can go to stdout, a Telegram chat, a Discord webhook, or any combination.

## Quickstart

```
git clone https://github.com/orcalayer/whale-watchlist-monitor
cd whale-watchlist-monitor
pip install -r requirements.txt
cp config.example.yaml config.yaml   # add your API key and wallets
python monitor.py
```

First run records a baseline silently; alerts start from the second poll.

## Configuration

See [config.example.yaml](config.example.yaml). Telegram needs a bot token from @BotFather and a chat id; Discord needs a channel webhook URL.

Picking wallets to watch: the [smart-whale leaderboard](https://orcalayer.com/leaderboard) is a good starting point, or use the `orcalayer` client's `leaderboard()` method.

## Notes

- State lives in `state.json` next to the script. Delete it to re-baseline.
- Large alert batches are split into multiple messages at alert boundaries, respecting Telegram (4096) and Discord (2000) length limits, with part numbering.
- `config.yaml` is validated at startup; field-level errors are printed before exit.
- The monitor makes 2 requests per wallet per poll (single GETs for now; a batch endpoint is planned and the code carries a TODO for it). The Premium limit of 600 requests/min comfortably covers watchlists of dozens of wallets at a 5-minute interval.

## Disclaimer

This tool reports on-chain trading activity for informational purposes only. It is not financial advice. Prediction markets involve risk and you can lose money. Past activity of any wallet does not predict future results.

## License

MIT. See [LICENSE](LICENSE).
