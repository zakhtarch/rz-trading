# Alerts

Rebuild TradingView alerts from the latest **saved** library script. Chart save ≠ live alerts (each alert freezes a snapshot).

Strategy Pine is private — local / TradingView library only, not in git.

## Setup

```bash
cd Alerts
python3 pull_tv_session.py          # writes auth.json (gitignored)
# or copy auth.example.json → auth.json and fill cookies
```

## Commands

```bash
python3 refresh_rz_alerts.py list
python3 refresh_rz_alerts.py refresh              # flatten opens, then recreate
python3 refresh_rz_alerts.py refresh --skip-flatten
python3 refresh_rz_alerts.py refresh --dry-run
```

## Bot webhooks

```bash
cp bot.example.json bot.json   # tunnel URL + secret
python3 refresh_rz_alerts.py create-bot
```

`bot.json` secret must match `WEBHOOK_SECRET` in `Bot/start_rz.bat`.

## Local-only files

`auth.json`, `bot.json`, `last_rz_alert.json`, `*.pine` — never commit.
