#!/usr/bin/env python3
"""Pull Heiken Ashi OHLCV from TradingView (same HA symbols as live RZ alerts).

Uses auth.json session → homepage auth_token → data websocket.
Caches pickles under analysis/tv_cache/.
No Pine publish.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from refresh_rz_alerts import CTX, UA, cookie_header, load_auth  # noqa: E402

CACHE = HERE / "tv_cache"
CACHE.mkdir(exist_ok=True)

SYMBOLS = {
    "XAU": "OANDA:XAUUSD",
    "BTC": "COINBASE:BTCUSD",
    "OIL": "CXM:USOIL",
}
# TV resolution strings
RES = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
}


def _auth_token() -> str:
    auth = load_auth()
    import urllib.request

    req = urllib.request.Request(
        "https://www.tradingview.com/",
        headers={"User-Agent": UA, "Cookie": cookie_header(auth), "Accept": "text/html"},
    )
    with urllib.request.urlopen(req, context=CTX, timeout=60) as res:
        html = res.read().decode("utf-8", "replace")
    m = re.search(r'"auth_token"\s*:\s*"([^"]+)"', html)
    if not m:
        raise SystemExit("No auth_token — rerun pull_tv_session.py")
    return m.group(1)


def _encode(method, params) -> str:
    payload = json.dumps({"m": method, "p": params})
    return f"~m~{len(payload)}~m~{payload}"


def _ha_symbol(tv_sym: str) -> str:
    obj = {
        "symbol": {
            "symbol": tv_sym,
            "adjustment": "splits",
            "session": "regular",
            "currency-id": "USD",
        },
        "type": "BarSetHeikenAshi@tv-basicstudies-60!",
        "inputs": {},
    }
    return "=" + json.dumps(obj, separators=(",", ":"))


def pull_ha(tv_sym: str, resolution: str, max_rounds: int = 60) -> pd.DataFrame:
    """Fetch as much HA history as TV will give for symbol/resolution."""
    import websocket

    token = _auth_token()
    bars: list = []
    state = {"more": 0, "last_n": -1, "stall": 0, "err": None}

    def on_message(ws, message):
        parts = message.split("~m~")
        i = 1
        while i < len(parts):
            try:
                int(parts[i])
            except Exception:
                i += 1
                continue
            i += 1
            if i >= len(parts):
                break
            payload = parts[i]
            i += 1
            if payload.startswith("~h~"):
                ws.send(f"~m~{len(payload)}~m~{payload}")
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            m = obj.get("m")
            p = obj.get("p")
            if m == "timescale_update":
                data = p[1] if isinstance(p, list) and len(p) > 1 else {}
                for _k, v in (data or {}).items():
                    if isinstance(v, dict) and "s" in v:
                        for bar in v["s"]:
                            bars.append(bar.get("v") if isinstance(bar, dict) else bar)
            elif m == "series_completed":
                by_t = {b[0]: b for b in bars if isinstance(b, (list, tuple)) and len(b) >= 5}
                n = len(by_t)
                if n == state["last_n"]:
                    state["stall"] += 1
                else:
                    state["stall"] = 0
                    state["last_n"] = n
                if state["stall"] >= 2 or state["more"] >= max_rounds:
                    ws.close()
                    return
                state["more"] += 1
                ws.send(_encode("request_more_data", ["cs_rz", "sds_1", 5000]))
            elif m in ("symbol_error", "series_error"):
                state["err"] = p
                ws.close()

    def on_open(ws):
        cs = "cs_rz"
        ws.send(_encode("set_auth_token", [token]))
        ws.send(_encode("chart_create_session", [cs, ""]))
        ws.send(_encode("resolve_symbol", [cs, "sds_sym_1", _ha_symbol(tv_sym)]))
        ws.send(_encode("create_series", [cs, "sds_1", "s1", "sds_sym_1", resolution, 5000]))

    ws = websocket.WebSocketApp(
        "wss://data.tradingview.com/socket.io/websocket?from=chart%2F&date=2026-07-31T09%3A00%3A10",
        on_open=on_open,
        on_message=on_message,
        header=[f"Origin: https://www.tradingview.com", f"User-Agent: {UA}"],
    )
    ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE}, ping_interval=20)
    if state["err"]:
        raise RuntimeError(f"TV error {tv_sym} {resolution}: {state['err']}")
    by_t = {b[0]: b for b in bars if isinstance(b, (list, tuple)) and len(b) >= 5}
    rows = sorted(by_t.values(), key=lambda x: x[0])
    if not rows:
        raise RuntimeError(f"No bars for {tv_sym} {resolution}")
    idx = pd.to_datetime([r[0] for r in rows], unit="s", utc=True)
    df = pd.DataFrame(
        {
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) if len(r) > 5 else 0.0 for r in rows],
        },
        index=idx,
    )
    df.index.name = "time"
    return df


def cache_path(name: str, tf: str) -> Path:
    return CACHE / f"{name}_{tf}_ha.pkl"


def load_or_pull(name: str, tf: str, force: bool = False) -> pd.DataFrame:
    path = cache_path(name, tf)
    if path.exists() and not force:
        return pd.read_pickle(path)
    tv_sym = SYMBOLS[name]
    res = RES[tf]
    print(f"pull {name} {tf} ({tv_sym} HA {res})…", flush=True)
    t0 = time.time()
    df = pull_ha(tv_sym, res)
    df.to_pickle(path)
    days = (df.index[-1] - df.index[0]).total_seconds() / 86400
    print(f"  → {len(df)} bars, {days:.1f}d in {time.time()-t0:.1f}s → {path.name}", flush=True)
    return df


def pull_all(force: bool = False) -> dict:
    out = {}
    for name in SYMBOLS:
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            out[(name, tf)] = load_or_pull(name, tf, force=force)
    return out


if __name__ == "__main__":
    force = "--force" in sys.argv
    pull_all(force=force)
    print("done", flush=True)
