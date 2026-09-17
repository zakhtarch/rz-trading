# RZ-Trading

Execution stack for a private TradingView strategy: **liquidity-region taps** + **momentum confirmation**, with **targets** and **rule-based exits**.

The Pine strategy source is **not** in this repository.

## Architecture

```
TradingView (private Pine)
        │  alert() JSON
        ├──────────────────► Discord (optional notify)
        │
        └─ Cloudflare tunnel ──► Bot :8787/tv ──► MetaTrader 5 (Exness)
                                      │
                                      └─ /flatten_all  (before alert refresh)
```

| Layer | Location | Job |
|---|---|---|
| Strategy | TradingView library (private) | Signals + exit logic |
| Alert tooling | `Alerts/` (Mac) | Auth session, rebuild frozen TV alerts |
| Execution | `Bot/` (Windows VPS) | Webhook → MT5 orders, Discord controls, CSV log |
| Tunnel | `cloudflared` on VPS | Public HTTPS → local Flask |

TradingView alerts **freeze** a script snapshot. Updating the library does not update live alerts until `Alerts/refresh_rz_alerts.py refresh` rebuilds them.

## Anatomy

```
RZ-Trading/
├── README.md
├── Alerts/                      # Mac — TradingView alert rebuild
│   ├── pull_tv_session.py       # browser cookies → auth.json (local)
│   ├── refresh_rz_alerts.py     # list / refresh / create-bot
│   ├── auth.example.json
│   ├── bot.example.json
│   └── analysis/                # offline research helpers
│       ├── tv_ha_pull.py
│       ├── exit8_pine_1bar.py
│       └── exit16_combos_xau_oil.py
└── Bot/                         # VPS — copy to Desktop\RZ
    ├── rz_scalping.py           # Flask + MT5 + trade book/CSV
    ├── rz_control.py            # armed flag, lots, rz commands
    ├── rz_discord.py            # Discord control + forward
    ├── symbol_map.json          # MT5 symbols, TF enable, lots
    ├── requirements.txt
    ├── start_rz.bat             # env + MT5 + python
    └── start_tunnel.bat         # cloudflared → :8787
```

### `Alerts/`

| File | Role |
|---|---|
| `pull_tv_session.py` | Writes local `auth.json` from a logged-in Chrome profile |
| `refresh_rz_alerts.py` | Rebuilds Discord + bot alerts onto the latest library version |
| `*.example.json` | Templates for auth / tunnel config (copy, fill, never commit) |
| `analysis/` | Kept backtest helpers (no strategy source) |

Local-only (gitignored): `auth.json`, `bot.json`, `*.pine`, alert dumps.

### `Bot/`

| File | Role |
|---|---|
| `rz_scalping.py` | `POST /tv` entries/exits, `POST /flatten_all`, MT5, CSV |
| `rz_control.py` | `rz start\|stop\|status`, lot lookup from `symbol_map.json` |
| `rz_discord.py` | Discord bot + optional alert webhook forward |
| `symbol_map.json` | Per-symbol MT5 name + `1`/`5`/`15`/`1hr` lots and `enabled` |
| `start_rz.bat` | Secrets + launch MT5 + Flask |
| `start_tunnel.bat` | Expose `127.0.0.1:8787` |

Runtime files on the VPS (gitignored): `open_trades.json`, `trades.csv`, `runtime.json`, `heartbeat.json`.

## Strategy overview (no internals)

- **Entries** — taps of mapped liquidity zones with momentum alignment.
- **Management** — profit targets plus structured exits (chart TF or lower TF).
- **Lower-TF exits** — trigger on the **lower timeframe bar close**.
- **Sizing** — bot-side lots and per-TF kill switches in `symbol_map.json` (hot-reload).

Exact parameters live in the private Pine and your VPS map.

## Quick start

**VPS bot**

1. Copy `Bot/` → `C:\Users\Administrator\Desktop\RZ\`
2. Set secrets in `start_rz.bat` (`WEBHOOK_SECRET`, Discord vars).
3. Run `start_rz.bat`, then `start_tunnel.bat`.
4. Point TradingView bot alerts at `https://<tunnel>/tv?k=<WEBHOOK_SECRET>`.

**Mac alerts**

```bash
cd Alerts
cp auth.example.json auth.json   # or: python3 pull_tv_session.py
cp bot.example.json bot.json     # paste tunnel URL + matching secret
python3 refresh_rz_alerts.py list
python3 refresh_rz_alerts.py refresh
```

Details: [`Alerts/README.md`](Alerts/README.md), [`Bot/README.md`](Bot/README.md).

## Secrets

Never commit real credentials. Ignored by design:

- `Alerts/auth.json`, `Alerts/bot.json`, `Alerts/last_rz_alert.json`
- Strategy `.pine` files
- Filled Discord tokens / live webhook URLs

Use the `*.example.json` templates. Keep `WEBHOOK_SECRET` in sync between `bot.json` and `start_rz.bat`.
