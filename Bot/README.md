# RZ Bot

Flask + MetaTrader5 webhook listener and Discord switches. Runs on the **Windows VPS**, not on this Mac.

Copy this folder to `C:\Users\Administrator\Desktop\RZ\` on the VPS, then start the two batch files.

TradingView `alert()` → Cloudflare tunnel → `127.0.0.1:8787/tv` → Exness order. Pine `content` is forwarded to Discord if `DISCORD_ALERT_WEBHOOK` is set.

## Files

| File | Role |
|---|---|
| `rz_scalping.py` | Webhook + MT5 orders + CSV + `/flatten_all` |
| `rz_control.py` | Armed flag + lot map helpers + `rz` commands |
| `rz_discord.py` | Discord bot, notify, optional trade-channel forward |
| `symbol_map.json` | MT5 names, per-TF `enabled` + `bs` / `be-se` lots (hot-reload) |
| `start_rz.bat` | Secrets + start MT5 + start Python |
| `start_tunnel.bat` | Public URL for TradingView |

## VPS setup

1. Copy these files onto the VPS at `C:\Users\Administrator\Desktop\RZ\`.
2. MT5 open, logged in, Market Watch has `XAUUSDm`, `BTCUSDm`, `USOILm`.
3. Fill `start_rz.bat`: `WEBHOOK_SECRET`, Discord bot token / user / channel, optional trade-channel webhook.
4. MT5 path in `start_rz.bat` is `C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe`.
5. Double-click `start_rz.bat`. Leave that window open.
6. Double-click `start_tunnel.bat`. Leave that window open. Copy the `https://….trycloudflare.com` URL.
7. TradingView webhook: `https://THAT-URL/tv?k=YOUR_WEBHOOK_SECRET` on **Any alert() function call**.

`WEBHOOK_SECRET` in the bat must match the `?k=` on the alert.

## Discord commands

```
rz help
rz status
rz stop
rz start
```

`stop` blocks **new** entries only. Open trades still exit. Per-TF kill switches live in `symbol_map.json` (`enabled: false`).

## symbol_map.json

Each symbol has `mt5` plus `1` / `5` / `15` / `1hr` blocks:

```json
"5": { "enabled": true, "bs": 0.02, "be-se": 0.02 }
```

Edit lots or set `enabled` false — the bot reloads the file without a restart.

## HTTP

| Path | Role |
|---|---|
| `POST /tv?k=SECRET` | TradingView entry / exit / print JSON |
| `POST /flatten_all?k=SECRET` | Market-close every tracked open (alert refresh) |
| `GET /tv` | Health ping |

## After you change Python

Overwrite the VPS files, close the Python window, run `start_rz.bat` again. Tunnel can stay up.
