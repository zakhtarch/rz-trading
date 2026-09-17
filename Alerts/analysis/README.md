# Analysis keepers

Small set of research scripts kept after pruning one-off experiments.

| Script | Role |
|---|---|
| `tv_ha_pull.py` | Pull TradingView Heikin Ashi bars for offline sims |
| `exit8_pine_1bar.py` | Pine-faithful entry/exit replay used as the closest baseline |
| `exit16_combos_xau_oil.py` | FIRST4 × EMA exit grid that informed live XAU/OIL defaults |

Run from this folder with a live or cached TV session as each script expects. Results JSON/logs and `tv_cache/` stay local (gitignored).
