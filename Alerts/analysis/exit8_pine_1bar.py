#!/usr/bin/env python3
"""8 exits on Pine-faithful 1-bar book (incremental zones, B/S/BE/SE, Flip).

Baseline exits: Stop / TP / Flip (Pine).
Strategy only if it fires when TP/SL/Flip have not hit yet that bar.

HTF boxes: always 1H (zoneTf=60).
Gates: 1m/5m/15m→1H, 1H→4H. 1-bar B/S gate (live).
No combos. No publish.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from tv_ha_pull import load_or_pull

# --- constants ---
SWING, KEEP, MERGE = 8, 12, 0.7
EMA_F, EMA_S = 9, 15
FRESH_BESE, FRESH_BS = 10, 30
STOP_SWING = 10
MINTICK = {"XAU": 0.001, "BTC": 0.01, "OIL": 0.001}
ATR_CAP = {"1m": 0.85, "5m": 0.45, "15m": 0.45, "1h": 0.7}
TP_BESE = {"1m": 1.55, "5m": 1.25, "15m": 1.55, "1h": 1.55}
BESE_STOP_ATR = 1.1
TP_ATR_BS, TP_STOP_BS = 3.0, 4.0
STOP_TICKS = 2
LOTS = {
    "XAU": {"1m": 0.01, "5m": 0.02, "15m": 0.02, "1h": 0.02},
    "BTC": {"1m": 0.05, "5m": 0.10, "15m": 0.10, "1h": 0.10},
    "OIL": {"1m": 0.01, "5m": 0.02, "15m": 0.02, "1h": 0.02},
}
CONTRACT = {"XAU": 100.0, "BTC": 1.0, "OIL": 1000.0}
LOWER = {"1h": "15m", "15m": "5m", "5m": "1m", "1m": None}
WINDOWS = (7, 30, 60, 90)
SYMS = ("XAU", "BTC", "OIL")
TFS = ("1m", "5m", "15m", "1h")
FIRST4 = ("bs_same", "bs_lower", "tap_close", "reject")
LAST4 = ("ema1_same", "ema1_lower", "ema2_same", "ema2_lower")
COMBOS16 = tuple(f"{a}+{b}" for a in FIRST4 for b in LAST4)

MODES = (
    "baseline",
    "bs_same",
    "bs_lower",
    "tap_close",
    "reject",
    "ema1_same",
    "ema1_lower",
    "ema2_same",
    "ema2_lower",
)
OUT = Path(__file__).resolve().parent / "exit8_pine_1bar.json"


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr14(df):
    h, l, c = df["high"], df["low"], df["close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(14).mean()


def avail_days(df):
    if df is None or len(df) < 2:
        return 0.0
    return float((df.index[-1] - df.index[0]).total_seconds() / 86400)


class ZoneBook:
    def __init__(self):
        self.zH, self.zL, self.zT = [], [], []

    def push(self, hi, lo, t):
        for tt in self.zT:
            if tt == t:
                return
        for i in range(len(self.zL)):
            ov = max(0.0, min(hi, self.zH[i]) - max(lo, self.zL[i]))
            smaller = min(hi - lo, self.zH[i] - self.zL[i])
            if smaller > 0 and ov / smaller >= MERGE:
                self.zH[i] = max(hi, self.zH[i])
                self.zL[i] = min(lo, self.zL[i])
                return
        self.zH.append(hi)
        self.zL.append(lo)
        self.zT.append(t)
        while len(self.zL) > KEEP:
            self.zH.pop(0)
            self.zL.pop(0)
            self.zT.pop(0)

    def active(self):
        return list(zip(self.zT, self.zH, self.zL))


@dataclass
class Open:
    typ: str
    entry: float
    stop: float
    tp: float
    bar: int
    time: object
    taken: bool
    # reject arm
    rej_hi: float = np.nan
    rej_lo: float = np.nan
    # ema arm
    ema_armed: bool = False
    ema_streak: int = 0


@dataclass
class Log:
    typ: str
    reason: str
    net: float
    taken: bool
    entry_time: object


def prep_ohlc(df):
    out = df.copy()
    out["atr"] = atr14(out)
    out["ef"] = ema(out["close"], EMA_F)
    out["es"] = ema(out["close"], EMA_S)
    out["over"] = (out["close"] > out["ef"]) & (out["close"] > out["es"])
    out["under"] = (out["close"] < out["ef"]) & (out["close"] < out["es"])
    out["cross_up"] = (out["ef"] > out["es"]) & (out["ef"].shift(1) <= out["es"].shift(1))
    out["cross_dn"] = (out["ef"] < out["es"]) & (out["ef"].shift(1) >= out["es"].shift(1))
    return out


def htf_pack(htf, chart_index, htf_hours: float = 4.0, chart_hours: float = 1.0):
    """Map HTF EMA over/under onto chart bars (asof ffill).

    htf_hours/chart_hours kept for call-site compat; alignment matches the
    XAU/OIL-parity path (open-index ffill). Do not retune this for BTC alone.
    """
    h = prep_ohlc(htf)
    h["op"] = h["over"].shift(1)
    h["up"] = h["under"].shift(1)
    hx = h.reindex(chart_index, method="ffill")
    return {
        "over": hx["over"].fillna(False).to_numpy(bool),
        "under": hx["under"].fillna(False).to_numpy(bool),
        "two_up": (hx["over"] & hx["op"]).fillna(False).to_numpy(bool),
        "two_dn": (hx["under"] & hx["up"]).fillna(False).to_numpy(bool),
    }


def map_lower_flags(chart, lower):
    """As-of merge lower TF over/under/cross onto chart bars."""
    n = len(chart)
    if lower is None or len(lower) < 50:
        z = np.zeros(n, dtype=bool)
        return {k: z.copy() for k in ("over", "under", "cross_up", "cross_dn", "fire_b", "fire_s", "tap_sup", "tap_dem")}
    lo = prep_ohlc(lower)
    # For lower B/S fires we need zones on 1H — computed in main loop; here only EMA flags
    flags = {
        "over": np.zeros(n, dtype=bool),
        "under": np.zeros(n, dtype=bool),
        "cross_up": np.zeros(n, dtype=bool),
        "cross_dn": np.zeros(n, dtype=bool),
    }
    lts = lo.index.to_numpy()
    cts = chart.index.to_numpy()
    li = 0
    overs = lo["over"].to_numpy(bool)
    unders = lo["under"].to_numpy(bool)
    cu = lo["cross_up"].fillna(False).to_numpy(bool)
    cd = lo["cross_dn"].fillna(False).to_numpy(bool)
    for ci, ct in enumerate(cts):
        while li < len(lo) and lts[li] <= ct:
            li += 1
        j = max(0, li - 1)
        flags["over"][ci] = overs[j]
        flags["under"][ci] = unders[j]
        flags["cross_up"][ci] = cu[j]
        flags["cross_dn"][ci] = cd[j]
    return flags


def lot_for(name, tf, typ):
    return LOTS[name][tf]  # same BS/BESE lots in defaults for these


def cash(name, tf, typ, entry, exit_px, long):
    move = (exit_px - entry) if long else (entry - exit_px)
    return lot_for(name, tf, typ) * CONTRACT[name] * move


def run_mode(name, tf, chart, side_htf, zone_1h, lower, mode: str, since):
    """Full bar replay with optional early exit mode. Returns taken stats by type + total."""
    atr_cap = ATR_CAP[tf]
    tp_bese = TP_BESE[tf]
    mintick = MINTICK[name]
    buf = STOP_TICKS * mintick
    use_1h_gate = tf == "1h"

    chart = prep_ohlc(chart)
    zone = zone_1h  # 1H bars for zone birth (same series if tf==1h)
    # 1h chart gates on 4H; LTF charts gate on 1H — both via lookahead_off
    htf_h = 4.0 if use_1h_gate else 1.0
    chart_h = {"1m": 1 / 60, "5m": 5 / 60, "15m": 0.25, "1h": 1.0}[tf]
    side = htf_pack(side_htf, chart.index, htf_hours=htf_h, chart_hours=chart_h)

    n = len(chart)
    hi = chart["high"].to_numpy(float)
    lo = chart["low"].to_numpy(float)
    op = chart["open"].to_numpy(float)
    cl = chart["close"].to_numpy(float)
    atr = chart["atr"].to_numpy(float)
    overs = chart["over"].to_numpy(bool)
    unders = chart["under"].to_numpy(bool)
    cross_up = chart["cross_up"].fillna(False).to_numpy(bool)
    cross_dn = chart["cross_dn"].fillna(False).to_numpy(bool)
    sw_lo = chart["low"].rolling(STOP_SWING).min().to_numpy(float)
    sw_hi = chart["high"].rolling(STOP_SWING).max().to_numpy(float)
    idx = chart.index

    # lower flags
    lf = map_lower_flags(chart, lower)
    # For lower B/S we approximate: opposite EMA flip ready on lower — use over/under rising edge proxy
    # Better: detect box B/S on lower with shared 1H zones in a side pass
    lo_fire_b = np.zeros(n, dtype=bool)
    lo_fire_s = np.zeros(n, dtype=bool)
    need_lower = any(
        x in mode for x in ("bs_lower", "ema1_lower", "ema2_lower")
    ) or any(
        x in part for part in mode.split("+") for x in ("bs_lower", "ema1_lower", "ema2_lower")
    )
    if lower is not None and len(lower) > 50 and need_lower:
        lo_fire_b, lo_fire_s = lower_bs_fires(lower, zone_1h, atr_cap, chart.index)

    is_low = np.zeros(n, dtype=bool)
    is_high = np.zeros(n, dtype=bool)
    # zone series alignment: build is_low on zone_1h then map — simpler: if chart is 1h use chart; else build on zone and step
    z = prep_ohlc(zone_1h) if tf != "1h" else chart
    zh, zl, zo, zc = z["high"].to_numpy(float), z["low"].to_numpy(float), z["open"].to_numpy(float), z["close"].to_numpy(float)
    zatr = z["atr"].to_numpy(float)
    zidx = z.index
    zn = len(z)
    z_is_low = np.zeros(zn, dtype=bool)
    z_is_high = np.zeros(zn, dtype=bool)
    for i in range(1, zn):
        a = max(0, i - SWING)
        z_is_low[i] = zl[i] <= zl[a:i].min()
        z_is_high[i] = zh[i] >= zh[a:i].max()

    zb = ZoneBook()
    zi_next = 3  # zone bar cursor
    tagged = False
    tap_below = tap_above = False
    tag_hi = tag_lo = np.nan
    tag_bar = -10**9
    stick_up = stick_dn = False
    prev_b = prev_s = False
    opens: list[Open] = []
    logs: list[Log] = []

    def close_open(o, exit_px, reason):
        long = o.typ in ("B", "BE")
        net = cash(name, tf, o.typ, o.entry, exit_px, long)
        logs.append(Log(o.typ, reason, net, o.taken, o.time))

    def cap_below(level, price, max_dist):
        use = level if (level is not None and np.isfinite(level) and level < price) else np.nan
        raw = price - max_dist if not np.isfinite(use) else max(use, price - max_dist)
        return min(raw, price - mintick)

    def cap_above(level, price, max_dist):
        use = level if (level is not None and np.isfinite(level) and level > price) else np.nan
        raw = price + max_dist if not np.isfinite(use) else min(use, price + max_dist)
        return max(raw, price + mintick)

    for i in range(1, n):
        t = idx[i]
        # advance zone births up to this chart time
        while zi_next < zn and zidx[zi_next] <= t:
            i_z = zi_next
            min_h = zatr[i_z - 2] * atr_cap if i_z >= 2 and np.isfinite(zatr[i_z - 2]) else 0.0
            if i_z >= 3:
                if z_is_low[i_z - 2] and not z_is_low[i_z - 1]:
                    d_lo = zl[i_z - 2]
                    d_hi = max(min(zo[i_z - 2], zc[i_z - 2]), d_lo)
                    if d_hi - d_lo < min_h:
                        d_hi = d_lo + min_h
                    if d_hi - d_lo >= min_h:
                        zb.push(d_hi, d_lo, zidx[i_z - 2])
                if z_is_high[i_z - 2] and not z_is_high[i_z - 1]:
                    s_hi = zh[i_z - 2]
                    s_lo = min(max(zo[i_z - 2], zc[i_z - 2]), s_hi)
                    if s_hi - s_lo < min_h:
                        s_lo = s_hi - min_h
                    if s_hi - s_lo >= min_h:
                        zb.push(s_hi, s_lo, zidx[i_z - 2])
            zi_next += 1
        active = zb.active()

        bar_up, bar_dn = bool(side["over"][i]), bool(side["under"][i])
        two_up, two_dn = bool(side["two_up"][i]), bool(side["two_dn"][i])
        if two_up:
            stick_up, stick_dn = True, False
        elif two_dn:
            stick_up, stick_dn = False, True
        allow_L, allow_S = bar_up, bar_dn  # 1-bar gate
        bese_up = bar_up if stick_up else two_up
        bese_dn = bar_dn

        hit = False
        ta_ = tb_ = False
        tap_sup = tap_dem = False  # for exits: tap supply / demand
        for _, bhi, blo in active:
            if lo[i] <= bhi and hi[i] >= blo:
                hit = True
                tag_hi, tag_lo = bhi, blo
                if cl[i - 1] > bhi:
                    ta_ = True
                    tap_sup = True
                if cl[i - 1] < blo:
                    tb_ = True
                    tap_dem = True
        if hit:
            tagged = True
            tag_bar = i
            if tb_:
                tap_below, tap_above = True, False
            elif ta_:
                tap_above, tap_below = True, False

        fresh_bs = tagged and (i - tag_bar) <= FRESH_BS
        held_sup = np.isfinite(tag_hi) and cl[i] > tag_hi
        held_res = np.isfinite(tag_lo) and cl[i] < tag_lo
        b_ready = tagged and fresh_bs and tap_below and overs[i] and held_sup
        s_ready = tagged and fresh_bs and tap_above and unders[i] and held_res
        raw_b = b_ready and not prev_b
        raw_s = s_ready and not prev_s
        prev_b, prev_s = b_ready, s_ready
        raw_be = ta_ and overs[i] and not raw_b
        raw_se = tb_ and unders[i] and not raw_s
        fire_b, fire_s, fire_be, fire_se = raw_b, raw_s, raw_be, raw_se

        flatten_L = fire_s or fire_se
        flatten_S = fire_b or fire_be

        still = []
        for o in opens:
            long = o.typ in ("B", "BE")
            stop_hit = (cl[i] <= o.stop) if long else (cl[i] >= o.stop)
            tp_hit = (hi[i] >= o.tp) if long else (lo[i] <= o.tp)
            flip = (long and flatten_L) or ((not long) and flatten_S)

            # strategy signal this bar? (single mode or a+b combo — either leg)
            strat = None
            if mode != "baseline":
                legs = mode.split("+") if "+" in mode else [mode]

                def try_leg(leg):
                    if leg == "bs_same":
                        if long and fire_s:
                            return ("BS_EXIT", cl[i])
                        if (not long) and fire_b:
                            return ("BS_EXIT", cl[i])
                    elif leg == "bs_lower":
                        if long and lo_fire_s[i]:
                            return ("BS_EXIT", cl[i])
                        if (not long) and lo_fire_b[i]:
                            return ("BS_EXIT", cl[i])
                    elif leg == "tap_close":
                        if long and tap_sup:
                            return ("LIQ_EXIT", cl[i])
                        if (not long) and tap_dem:
                            return ("LIQ_EXIT", cl[i])
                    elif leg == "reject":
                        if long and tap_sup:
                            o.rej_hi, o.rej_lo = tag_hi, tag_lo
                        if (not long) and tap_dem:
                            o.rej_hi, o.rej_lo = tag_hi, tag_lo
                        if np.isfinite(o.rej_lo):
                            if long and cl[i] < o.rej_lo:
                                return ("LIQ_EXIT", cl[i])
                            if (not long) and cl[i] > o.rej_hi:
                                return ("LIQ_EXIT", cl[i])
                    elif leg.startswith("ema"):
                        need = 1 if "ema1" in leg else 2
                        if "lower" in leg:
                            under_ = bool(lf["under"][i])
                            over_ = bool(lf["over"][i])
                            c_dn = bool(lf["cross_dn"][i])
                            c_up = bool(lf["cross_up"][i])
                        else:
                            under_, over_ = bool(unders[i]), bool(overs[i])
                            c_dn, c_up = bool(cross_dn[i]), bool(cross_up[i])
                        side_ok = under_ if long else over_
                        cross = c_dn if long else c_up
                        # per-leg EMA arm state keyed on leg name
                        armed_attr = f"ema_armed_{leg}"
                        streak_attr = f"ema_streak_{leg}"
                        if not hasattr(o, armed_attr):
                            setattr(o, armed_attr, False)
                            setattr(o, streak_attr, 0)
                        if cross and side_ok:
                            setattr(o, armed_attr, True)
                            setattr(o, streak_attr, 1)
                        elif getattr(o, armed_attr) and side_ok:
                            setattr(o, streak_attr, getattr(o, streak_attr) + 1)
                        else:
                            setattr(o, armed_attr, False)
                            setattr(o, streak_attr, 0)
                        if getattr(o, armed_attr) and getattr(o, streak_attr) >= need:
                            return ("EMA_EXIT", cl[i])
                    return None

                for leg in legs:
                    hit_leg = try_leg(leg)
                    if hit_leg is not None:
                        strat = hit_leg
                        break

                # keep reject arm updates even if another leg already fired this bar
                if "reject" in legs and strat is not None and strat[0] != "LIQ_EXIT":
                    try_leg("reject")

            # first event: TV wired (TP/SL/Flip) beat strategy on same bar
            if tp_hit or stop_hit or flip:
                if tp_hit:
                    close_open(o, o.tp, "TP")
                elif stop_hit:
                    close_open(o, cl[i], "Stop")
                else:
                    close_open(o, cl[i], "Flip")
            elif strat is not None:
                close_open(o, strat[1], strat[0])
            else:
                still.append(o)
        opens = still

        if fire_b or fire_s:
            tagged = False
            tap_below = tap_above = False

        mixed = (fire_b or fire_be) and (fire_s or fire_se)
        sig_b = fire_b and not (mixed and not fire_b)
        sig_s = fire_s and not (mixed and fire_b)
        sig_be = fire_be and not mixed
        sig_se = fire_se and not mixed
        take_b = sig_b and allow_L
        take_s = sig_s and allow_S
        take_be = sig_be and bese_up
        take_se = sig_se and bese_dn

        a = atr[i] if np.isfinite(atr[i]) and atr[i] > 0 else 1.0
        max_stop = a * atr_cap
        bese_dist = a * BESE_STOP_ATR
        c = cl[i]

        if sig_b:
            swing_ok = sw_lo[i] if np.isfinite(sw_lo[i]) and sw_lo[i] < c else np.nan
            box_ok = tag_lo if np.isfinite(tag_lo) and tag_lo < c else np.nan
            cands = [x for x in (swing_ok, box_ok) if np.isfinite(x)]
            near = max(cands) if cands else np.nan
            stop = cap_below(near, c, max_stop) - buf
            tp = c + min(TP_ATR_BS * a, TP_STOP_BS * abs(c - stop))
            opens.append(Open("B", c, stop, tp, i, t, take_b))
        if sig_s:
            swing_ok = sw_hi[i] if np.isfinite(sw_hi[i]) and sw_hi[i] > c else np.nan
            box_ok = tag_hi if np.isfinite(tag_hi) and tag_hi > c else np.nan
            cands = [x for x in (swing_ok, box_ok) if np.isfinite(x)]
            near = min(cands) if cands else np.nan
            stop = cap_above(near, c, max_stop) + buf
            tp = c - min(TP_ATR_BS * a, TP_STOP_BS * abs(stop - c))
            opens.append(Open("S", c, stop, tp, i, t, take_s))
        if sig_be:
            stop = min(c - bese_dist - buf, c - mintick)
            tp = c + tp_bese * (c - stop)
            opens.append(Open("BE", c, stop, tp, i, t, take_be))
        if sig_se:
            stop = max(c + bese_dist + buf, c + mintick)
            tp = c - tp_bese * (stop - c)
            opens.append(Open("SE", c, stop, tp, i, t, take_se))

    taken = [L for L in logs if L.taken and L.entry_time >= since]
    by = {}
    for typ in ("B", "BE", "S", "SE"):
        xs = [L for L in taken if L.typ == typ]
        wins = sum(1 for L in xs if L.net > 0)
        by[typ] = {
            "n": len(xs),
            "wins": wins,
            "wr": round(100.0 * wins / len(xs), 1) if xs else None,
            "net_$": round(sum(L.net for L in xs), 2),
            "exits": _count([L.reason for L in xs]),
        }
    wins = sum(1 for L in taken if L.net > 0)
    by["ALL"] = {
        "n": len(taken),
        "wins": wins,
        "wr": round(100.0 * wins / len(taken), 1) if taken else None,
        "net_$": round(sum(L.net for L in taken), 2),
        "exits": _count([L.reason for L in taken]),
    }
    return by


def _count(xs):
    from collections import Counter
    return dict(Counter(xs))


def lower_bs_fires(lower, zone_1h, atr_cap, chart_index):
    """Detect B/S rising edges on lower TF with 1H zones; map onto chart bars."""
    lo = prep_ohlc(lower)
    z = prep_ohlc(zone_1h)
    zn = len(z)
    zh, zl, zo, zc = z["high"].to_numpy(float), z["low"].to_numpy(float), z["open"].to_numpy(float), z["close"].to_numpy(float)
    zatr = z["atr"].to_numpy(float)
    zidx = z.index
    z_is_low = np.zeros(zn, dtype=bool)
    z_is_high = np.zeros(zn, dtype=bool)
    for i in range(1, zn):
        a = max(0, i - SWING)
        z_is_low[i] = zl[i] <= zl[a:i].min()
        z_is_high[i] = zh[i] >= zh[a:i].max()

    zb = ZoneBook()
    zi_next = 3
    n = len(lo)
    fire_b = np.zeros(n, dtype=bool)
    fire_s = np.zeros(n, dtype=bool)
    tagged = False
    tap_below = tap_above = False
    tag_hi = tag_lo = np.nan
    tag_bar = -10**9
    prev_b = prev_s = False
    his, los, cls = lo["high"].to_numpy(float), lo["low"].to_numpy(float), lo["close"].to_numpy(float)
    overs, unders = lo["over"].to_numpy(bool), lo["under"].to_numpy(bool)
    lidx = lo.index

    for i in range(1, n):
        t = lidx[i]
        while zi_next < zn and zidx[zi_next] <= t:
            i_z = zi_next
            min_h = zatr[i_z - 2] * atr_cap if i_z >= 2 and np.isfinite(zatr[i_z - 2]) else 0.0
            if i_z >= 3:
                if z_is_low[i_z - 2] and not z_is_low[i_z - 1]:
                    d_lo, d_hi = zl[i_z - 2], max(min(zo[i_z - 2], zc[i_z - 2]), zl[i_z - 2])
                    if d_hi - d_lo < min_h:
                        d_hi = d_lo + min_h
                    if d_hi - d_lo >= min_h:
                        zb.push(d_hi, d_lo, zidx[i_z - 2])
                if z_is_high[i_z - 2] and not z_is_high[i_z - 1]:
                    s_hi, s_lo = zh[i_z - 2], min(max(zo[i_z - 2], zc[i_z - 2]), zh[i_z - 2])
                    if s_hi - s_lo < min_h:
                        s_lo = s_hi - min_h
                    if s_hi - s_lo >= min_h:
                        zb.push(s_hi, s_lo, zidx[i_z - 2])
            zi_next += 1
        active = zb.active()
        hit = False
        ta_ = tb_ = False
        for _, bhi, blo in active:
            if los[i] <= bhi and his[i] >= blo:
                hit = True
                tag_hi, tag_lo = bhi, blo
                if cls[i - 1] > bhi:
                    ta_ = True
                if cls[i - 1] < blo:
                    tb_ = True
        if hit:
            tagged = True
            tag_bar = i
            if tb_:
                tap_below, tap_above = True, False
            elif ta_:
                tap_above, tap_below = True, False
        fresh = tagged and (i - tag_bar) <= FRESH_BS
        b_ready = fresh and tap_below and overs[i] and np.isfinite(tag_hi) and cls[i] > tag_hi
        s_ready = fresh and tap_above and unders[i] and np.isfinite(tag_lo) and cls[i] < tag_lo
        fire_b[i] = b_ready and not prev_b
        fire_s[i] = s_ready and not prev_s
        prev_b, prev_s = b_ready, s_ready
        if fire_b[i] or fire_s[i]:
            tagged = False
            tap_below = tap_above = False

    # map to chart
    n_c = len(chart_index)
    out_b = np.zeros(n_c, dtype=bool)
    out_s = np.zeros(n_c, dtype=bool)
    li = 0
    lts = lidx.to_numpy()
    cts = chart_index.to_numpy()
    for ci, ct in enumerate(cts):
        prev = cts[ci - 1] if ci > 0 else None
        while li < n and lts[li] <= ct:
            if prev is None or lts[li] > prev:
                if fire_b[li]:
                    out_b[ci] = True
                if fire_s[li]:
                    out_s[ci] = True
            li += 1
    return out_b, out_s


def pick_window(df, days):
    end = df.index[-1]
    return df[df.index >= end - pd.Timedelta(days=days)].copy(), end - pd.Timedelta(days=days)


def main():
    print("=== load TV HA cache ===", flush=True)
    cache = {}
    for name in SYMS:
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            cache[(name, tf)] = load_or_pull(name, tf)
            print(f"  {name} {tf}: {len(cache[(name,tf)])} bars {avail_days(cache[(name,tf)]):.1f}d", flush=True)

    rows = []
    for name in SYMS:
        for tf in TFS:
            avail = avail_days(cache[(name, tf)])
            # largest non-trunc window
            cands = [d for d in WINDOWS if avail + 0.5 >= d] or [WINDOWS[0]]
            days = max(cands)
            chart_full = cache[(name, tf)]
            # warm start
            end = chart_full.index[-1]
            since = end - pd.Timedelta(days=days)
            warm = since - pd.Timedelta(days=200)
            chart = chart_full[chart_full.index >= warm].copy()
            zone = cache[(name, "1h")]
            zone = zone[zone.index >= warm].copy()
            side_tf = "4h" if tf == "1h" else "1h"
            side = cache[(name, side_tf)]
            side = side[side.index >= warm].copy()
            low_iv = LOWER[tf]
            lower = None
            if low_iv:
                lower = cache[(name, low_iv)]
                lower = lower[lower.index >= warm].copy()

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
                st = run_mode(name, tf, chart, side, zone, lower, mode, since)
                if mode == "baseline":
                    base = st
                    row["modes"][mode] = st
                    print(f"  baseline ALL n={st['ALL']['n']} ${st['ALL']['net_$']:+.2f} {st['ALL']['exits']}", flush=True)
                else:
                    d = round(st["ALL"]["net_$"] - base["ALL"]["net_$"], 2)
                    st["delta_$"] = d
                    row["modes"][mode] = st
                    print(f"  {mode:12} ${st['ALL']['net_$']:+.2f} Δ${d:+.2f} exits={st['ALL']['exits']}", flush=True)
            rows.append(row)

    # tables
    print("\n=== Δ$ table (1-bar Pine book) ===", flush=True)
    hdr = f"{'#':>2} {'cell':14} {'n':>4} {'base$':>10} | " + " ".join(f"{m:>10}" for m in MODES[1:])
    print(hdr, flush=True)
    for i, r in enumerate(rows, 1):
        b = r["modes"]["baseline"]["ALL"]
        cell = f"{r['symbol']} {r['tf']} {r['window_d']}d"
        bits = [f"{i:2} {cell:14} {b['n']:4d} {b['net_$']:+10.2f} |"]
        for m in MODES[1:]:
            bits.append(f"{r['modes'][m]['delta_$']:+10.2f}")
        print(" ".join(bits), flush=True)

    print("\n=== Instrument sums ===", flush=True)
    for name in SYMS:
        sub = [r for r in rows if r["symbol"] == name]
        b = sum(r["modes"]["baseline"]["ALL"]["net_$"] for r in sub)
        print(f"{name} baseline ${b:+.2f}", flush=True)
        best = None
        for m in MODES[1:]:
            s = sum(r["modes"][m]["ALL"]["net_$"] for r in sub)
            d = round(s - b, 2)
            print(f"  {m:12} ${s:+.2f} Δ${d:+.2f}", flush=True)
            if best is None or d > best[1]:
                best = (m, d)
        if best:
            print(f"  BEST {best[0]} Δ${best[1]:+.2f}", flush=True)

    out = {
        "params": {
            "entries": "Pine-faithful B/S/BE/SE, 1-bar B/S gate, incremental 1H zones",
            "baseline_exits": "Stop/TP/Flip",
            "strategy": "only if before Stop/TP/Flip same bar",
            "lots": LOTS,
            "contract": CONTRACT,
        },
        "rows": rows,
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nwrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
