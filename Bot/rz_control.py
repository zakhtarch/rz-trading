import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNTIME = Path(os.environ.get("TRADE_RUNTIME", r"C:\Users\Administrator\Desktop\RZ\runtime.json"))
MAP_PATH = Path(os.environ.get("SYMBOL_MAP", str(HERE / "symbol_map.json")))

_map_cache = {}
_map_mtime = None
_notify = None

TF_KEYS = ("1", "5", "15", "1hr")

HELP_TEXT = """```
rz help      this list
rz status    map + armed
rz stop      no new trades. open trades still exit
rz start     take new trades again

lots live in symbol_map.json
each tf has enabled + bs / be-se lots.
enabled false = skip that tf. open trades still exit.
```"""


def set_notifier(fn):
    global _notify
    _notify = fn


def say(text):
    if _notify:
        try:
            _notify(text)
        except Exception:
            pass


def map_cfg(tv_sym):
    """Match the ticker TradingView sends (USOIL) or the chart id (TVC:USOIL)."""
    m = symbol_map()
    raw = str(tv_sym or "").strip()
    if not raw:
        return None
    if raw in m:
        return m[raw]
    short = raw.split(":")[-1] if ":" in raw else raw
    if short in m:
        return m[short]
    u = short.upper()
    if "USOIL" in u or "UKOIL" in u or "WTICO" in u:
        return m.get("USOIL") or m.get("TVC:USOIL") or m.get("WTICOUSD")
    return None


def symbol_map():
    """Reload symbol_map.json when the file changes. Bad saves keep the last good map."""
    global _map_cache, _map_mtime
    try:
        mtime = MAP_PATH.stat().st_mtime
    except OSError:
        if _map_cache:
            return _map_cache
        raise
    if _map_cache and _map_mtime == mtime:
        return _map_cache
    try:
        data = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print("symbol_map.json reload failed, keeping last map:", e)
        if _map_cache:
            return _map_cache
        raise
    if not isinstance(data, dict) or not data:
        print("symbol_map.json empty, keeping last map")
        if _map_cache:
            return _map_cache
        raise ValueError("symbol_map.json is empty")
    _map_cache = data
    _map_mtime = mtime
    return _map_cache


def default_runtime():
    return {"armed": True}


def load_runtime():
    rt = default_runtime()
    if RUNTIME.exists():
        try:
            saved = json.loads(RUNTIME.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        if isinstance(saved, dict) and "armed" in saved:
            rt["armed"] = bool(saved["armed"])
    return rt


def save_runtime(rt):
    RUNTIME.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME.write_text(json.dumps({"armed": bool(rt.get("armed", True))}, indent=2), encoding="utf-8")


def tf_key(tf):
    t = (tf or "").strip().lower().replace(" ", "")
    if t in ("1", "1m"):
        return "1"
    if t in ("5", "5m"):
        return "5"
    if t in ("15", "15m"):
        return "15"
    if t in ("60", "1h", "1hr", "h"):
        return "1hr"
    return None


def family(typ):
    return "bs" if typ in ("B", "S") else "be-se"


def tf_block(cfg, tf):
    key = tf_key(tf)
    if not key or not isinstance(cfg, dict):
        return None
    block = cfg.get(key)
    if key == "1" and not isinstance(block, dict):
        block = cfg.get("5")
    if not isinstance(block, dict):
        return None
    return block


def parse_lot(raw):
    try:
        lot = float(raw)
    except (TypeError, ValueError):
        return None
    if lot <= 0:
        return None
    return lot


def slot_state(cfg, tf, typ):
    block = tf_block(cfg, tf)
    if block is None:
        return "bad", None
    if block.get("enabled") is False:
        return "killed", None
    lot = parse_lot(block.get(family(typ)))
    if lot is None:
        return "bad", None
    return "ok", lot


def lots_for(cfg, typ, tf):
    state, lot = slot_state(cfg, tf, typ)
    if state != "ok":
        raise ValueError(f"no lot for {typ} {tf}")
    return lot


def entry_skip_reason(tv_sym, typ, tf):
    rt = load_runtime()
    if not rt.get("armed", True):
        return "stopped"
    cfg = map_cfg(tv_sym)
    if cfg is None:
        return "unknown symbol"
    state, _lot = slot_state(cfg, tf, typ)
    if state == "killed":
        return "killed"
    if state != "ok":
        return "bad slot"
    return None


def status_text():
    rt = load_runtime()
    lines = ["RZ  " + ("STARTED" if rt.get("armed", True) else "STOPPED  (exits only)")]
    for name, cfg in symbol_map().items():
        lines.append(name)
        for key in TF_KEYS:
            block = cfg.get(key) if isinstance(cfg, dict) else None
            if not isinstance(block, dict):
                lines.append(f"  {key}  missing")
                continue
            on = "off" if block.get("enabled") is False else "on"
            bits = []
            for fam in ("bs", "be-se"):
                lot = parse_lot(block.get(fam))
                bits.append(f"{fam}={lot if lot is not None else 'bad'}")
            lines.append(f"  {key}  {on}  " + "  ".join(bits))
    return "```\n" + "\n".join(lines) + "\n```"


def handle_rz(text):
    parts = text.strip().split()
    if not parts or parts[0].lower() != "rz":
        return None
    args = [p.lower() for p in parts[1:]]
    if not args or args[0] in ("help", "?"):
        return HELP_TEXT
    if args[0] == "status":
        return status_text()
    if args[0] == "stop":
        rt = load_runtime()
        rt["armed"] = False
        save_runtime(rt)
        return status_text()
    if args[0] == "start":
        rt = load_runtime()
        rt["armed"] = True
        save_runtime(rt)
        return status_text()
    return "unknown command. `rz help`"
