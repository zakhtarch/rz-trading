#!/usr/bin/env python3
"""16 exit combos on XAU + OIL only (BTC tabled).

FIRST4 × LAST4:
  bs_same, bs_lower, tap_close, reject
  × ema1_same, ema1_lower, ema2_same, ema2_lower

Baseline Stop/TP/Flip; strategy if before those. 1-bar book.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import exit8_pine_1bar as m
from tv_ha_pull import load_or_pull

OUT = Path(__file__).resolve().parent / "exit16_combos_xau_oil.json"
SYMS = ("XAU", "OIL")
MODES = ("baseline",) + m.COMBOS16


def main():
    print("=== load TV HA (XAU, OIL) ===", flush=True)
    cache = {}
    for name in SYMS:
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            cache[(name, tf)] = load_or_pull(name, tf)
            print(
                f"  {name} {tf}: {len(cache[(name, tf)])} bars "
                f"{m.avail_days(cache[(name, tf)]):.1f}d",
                flush=True,
            )

    rows = []
    for name in SYMS:
        for tf in m.TFS:
            avail = m.avail_days(cache[(name, tf)])
            cands = [d for d in m.WINDOWS if avail + 0.5 >= d] or [m.WINDOWS[0]]
            days = max(cands)
            chart_full = cache[(name, tf)]
            end = chart_full.index[-1]
            since = end - pd.Timedelta(days=days)
            warm = max(since - pd.Timedelta(days=200), chart_full.index[0])
            chart = chart_full[chart_full.index >= warm].copy()
            zone = cache[(name, "1h")]
            zone = zone[zone.index >= max(warm, zone.index[0])].copy()
            side_tf = "4h" if tf == "1h" else "1h"
            side = cache[(name, side_tf)]
            side = side[side.index >= max(warm, side.index[0])].copy()
            low_iv = m.LOWER[tf]
            lower = None
            if low_iv:
                lower = cache[(name, low_iv)]
                lower = lower[lower.index >= max(warm, lower.index[0])].copy()

            print(f"\n{name} {tf} {days}d …", flush=True)
            row = {
                "symbol": name,
                "tf": tf,
                "window_d": days,
                "avail_d": round(avail, 1),
                "modes": {},
            }
            base = None
            for mode in MODES:
                st = m.run_mode(name, tf, chart, side, zone, lower, mode, since)
                if mode == "baseline":
                    base = st
                    row["modes"][mode] = st
                    print(
                        f"  baseline ALL n={st['ALL']['n']} "
                        f"${st['ALL']['net_$']:+.2f} {st['ALL']['exits']}",
                        flush=True,
                    )
                else:
                    d = round(st["ALL"]["net_$"] - base["ALL"]["net_$"], 2)
                    st["delta_$"] = d
                    row["modes"][mode] = st
                    print(
                        f"  {mode:28} ${st['ALL']['net_$']:+.2f} Δ${d:+.2f} "
                        f"exits={st['ALL']['exits']}",
                        flush=True,
                    )
            rows.append(row)

    # compact Δ$ matrix per cell
    print("\n=== Δ$ vs baseline (XAU+OIL, 16 combos) ===", flush=True)
    short = [c.replace("bs_", "b").replace("tap_close", "tap").replace("reject", "rej")
             .replace("ema1_same", "e1s").replace("ema1_lower", "e1l")
             .replace("ema2_same", "e2s").replace("ema2_lower", "e2l")
             for c in m.COMBOS16]
    hdr = f"{'cell':14} {'n':>4} {'base$':>9} |" + "".join(f"{s:>8}" for s in short)
    print(hdr, flush=True)
    for r in rows:
        b = r["modes"]["baseline"]["ALL"]
        cell = f"{r['symbol']} {r['tf']} {r['window_d']}d"
        bits = [f"{cell:14} {b['n']:4d} {b['net_$']:+9.2f} |"]
        for c in m.COMBOS16:
            bits.append(f"{r['modes'][c]['delta_$']:+8.2f}")
        print("".join(bits), flush=True)

    print("\n=== Instrument sums (Δ$) ===", flush=True)
    for name in SYMS:
        sub = [r for r in rows if r["symbol"] == name]
        b = sum(r["modes"]["baseline"]["ALL"]["net_$"] for r in sub)
        print(f"{name} baseline ${b:+.2f}", flush=True)
        ranked = []
        for c in m.COMBOS16:
            s = sum(r["modes"][c]["ALL"]["net_$"] for r in sub)
            d = round(s - b, 2)
            ranked.append((d, c, s))
            print(f"  {c:28} ${s:+.2f} Δ${d:+.2f}", flush=True)
        ranked.sort(reverse=True)
        print(f"  BEST {ranked[0][1]} Δ${ranked[0][0]:+.2f}", flush=True)
        print(f"  WORST {ranked[-1][1]} Δ${ranked[-1][0]:+.2f}", flush=True)

    print("\n=== Combined XAU+OIL ranking ===", flush=True)
    b_all = sum(r["modes"]["baseline"]["ALL"]["net_$"] for r in rows)
    ranked = []
    for c in m.COMBOS16:
        s = sum(r["modes"][c]["ALL"]["net_$"] for r in rows)
        ranked.append((round(s - b_all, 2), c, s))
    ranked.sort(reverse=True)
    print(f"baseline ${b_all:+.2f}", flush=True)
    for d, c, s in ranked:
        print(f"  {c:28} ${s:+.2f} Δ${d:+.2f}", flush=True)

    out = {
        "params": {
            "symbols": list(SYMS),
            "combos": list(m.COMBOS16),
            "rule": "FIRST4 × LAST4; Stop/TP/Flip first; either leg exits",
            "btc": "tabled",
        },
        "rows": rows,
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
