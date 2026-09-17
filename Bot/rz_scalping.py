import atexit
import csv
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, abort, request
import MetaTrader5 as mt5

from rz_control import entry_skip_reason, lots_for, map_cfg, say, symbol_map

SECRET = os.environ["WEBHOOK_SECRET"]
MAGIC = 260904
STORE = Path(os.environ.get("TRADE_STORE", r"C:\Users\Administrator\Desktop\RZ\open_trades.json"))
LOG = Path(os.environ.get("TRADE_LOG", r"C:\Users\Administrator\Desktop\RZ\trades.csv"))
BEAT = Path(os.environ.get("TRADE_BEAT", r"C:\Users\Administrator\Desktop\RZ\heartbeat.json"))
STALE_SEC = 90

CSV_FIELDS = [
    "timestamp_entry", "timestamp_exit", "trade_id", "symbol", "symbol_mt5",
    "tf", "type", "dir", "size", "entry", "exit", "stop", "tp",
    "stop_reason", "ticket", "pnl", "pnl_pct",
]

app = Flask(__name__)
_book = threading.Lock()
_exec = threading.Lock()


def log(*parts):
    print("RZ", *parts, flush=True)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def side_of(d):
    typ = d.get("type", "")
    if typ in ("S", "SE"):
        return "short"
    if typ in ("B", "BE"):
        return "long"
    return d.get("dir") or "long"


def ensure_mt5():
    if not mt5.initialize():
        raise RuntimeError(mt5.last_error())


def load_book():
    if STORE.exists():
        return json.loads(STORE.read_text(encoding="utf-8"))
    return {}


def save_book(book):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(book, indent=2), encoding="utf-8")


def append_csv(row):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    new_file = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def write_exit_csv(rec, trade_id, exit_px, reason, when=None, pos=None):
    pnl, pnl_pct = pnl_row(rec, float(exit_px) if exit_px not in ("", None) else 0)
    append_csv({
        "timestamp_entry": rec.get("timestamp_entry", ""),
        "timestamp_exit": when or now_iso(),
        "trade_id": trade_id,
        "symbol": rec.get("symbol_tv") or "",
        "symbol_mt5": rec.get("symbol") or (getattr(pos, "symbol", "") if pos is not None else ""),
        "tf": rec.get("tf") or "",
        "type": rec.get("type") or "",
        "dir": rec.get("side") or "",
        "size": rec.get("size") or (getattr(pos, "volume", "") if pos is not None else ""),
        "entry": rec.get("entry") or (getattr(pos, "price_open", "") if pos is not None else ""),
        "exit": exit_px if exit_px not in ("", None) else "",
        "stop": rec.get("stop", ""),
        "tp": rec.get("tp", ""),
        "stop_reason": reason or "CLOSED",
        "ticket": rec.get("ticket") or (int(pos.ticket) if pos is not None else ""),
        "pnl": pnl,
        "pnl_pct": pnl_pct,
    })


def drop_open(trade_id):
    book = load_book()
    if trade_id in book:
        book.pop(trade_id, None)
        save_book(book)


def finish_open(trade_id, rec, exit_px, reason, when=None, pos=None):
    """Log one closed row, then drop the id from open_trades.json."""
    write_exit_csv(rec, trade_id, exit_px, reason, when=when, pos=pos)
    drop_open(trade_id)


def deal_reason_label(deal, fallback="CLOSED"):
    sl = getattr(mt5, "DEAL_REASON_SL", 4)
    tp = getattr(mt5, "DEAL_REASON_TP", 5)
    r = int(getattr(deal, "reason", 0) or 0)
    if r == sl:
        return "STOP"
    if r == tp:
        return "TP"
    return fallback or "CLOSED"


def find_close_deal(ticket):
    ticket = int(ticket or 0)
    if not ticket:
        return None
    deals = None
    try:
        deals = mt5.history_deals_get(position=ticket)
    except Exception:
        deals = None
    if not deals:
        end = datetime.now()
        start = end - timedelta(days=21)
        try:
            deals = mt5.history_deals_get(start, end)
        except Exception:
            deals = None
        if deals:
            deals = [
                d for d in deals
                if int(getattr(d, "position_id", 0) or 0) == ticket
                or int(getattr(d, "order", 0) or 0) == ticket
            ]
    if not deals:
        return None
    out_codes = {
        getattr(mt5, "DEAL_ENTRY_OUT", 1),
        getattr(mt5, "DEAL_ENTRY_INOUT", 2),
        getattr(mt5, "DEAL_ENTRY_OUT_BY", 3),
    }
    outs = [d for d in deals if int(getattr(d, "entry", -1)) in out_codes]
    if not outs:
        return None
    return sorted(outs, key=lambda d: (int(d.time), int(getattr(d, "ticket", 0) or 0)))[-1]


def entry_too_new(rec, sec=20):
    raw = rec.get("timestamp_entry") or ""
    try:
        dt = datetime.strptime(str(raw).replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() < sec


def live_tickets():
    ensure_mt5()
    tickets = set()
    for p in mt5.positions_get() or []:
        if p.magic != MAGIC:
            continue
        tickets.add(int(p.ticket))
        ident = int(getattr(p, "identifier", 0) or 0)
        if ident:
            tickets.add(ident)
    return tickets


def deal_age_sec(deal):
    t = int(getattr(deal, "time", 0) or 0)
    if not t:
        return 0
    return max(0.0, time.time() - t)


def broker_closed(trade_id, rec, live=None):
    """Live open gone on MT5: log CSV only if the close just happened. No history dump."""
    if not trade_id or not rec:
        return False
    if entry_too_new(rec):
        return False
    live = live_tickets() if live is None else live
    try:
        t = int(rec.get("ticket") or 0)
    except (TypeError, ValueError):
        t = 0
    if t and t in live:
        return False
    if find_pos(trade_id, rec.get("ticket")) is not None:
        return False
    deal = find_close_deal(rec.get("ticket"))
    if deal is not None and deal_age_sec(deal) <= 120:
        px = float(deal.price) if getattr(deal, "price", 0) else rec.get("entry")
        reason = deal_reason_label(deal, "CLOSED")
        finish_open(trade_id, rec, px, reason)
        log("broker close", trade_id, "ticket", rec.get("ticket"), reason, px)
        return True
    if deal is not None or not entry_too_new(rec, 300):
        drop_open(trade_id)
        log("drop stale open, no csv", trade_id)
    return False


def sweep_closed():
    try:
        ensure_mt5()
    except Exception as e:
        log("sweep mt5", e)
        return 0
    n = 0
    live = live_tickets()
    with _book:
        for trade_id, rec in list(load_book().items()):
            if broker_closed(trade_id, rec, live):
                n += 1
    return n


def blast(text):
    log(text)
    say(text)
    try:
        from rz_discord import blast as discord_blast
        discord_blast(text)
    except Exception:
        pass


def write_beat(alive=True):
    BEAT.parent.mkdir(parents=True, exist_ok=True)
    BEAT.write_text(json.dumps({"ts": time.time(), "iso": now_iso(), "alive": alive}), encoding="utf-8")


def check_stale_beat():
    if not BEAT.exists():
        return
    try:
        data = json.loads(BEAT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if data.get("alive") is False:
        blast("RZ bot was not running. Last stop " + str(data.get("iso") or ""))
        return
    ts = float(data.get("ts") or 0)
    if ts and time.time() - ts > STALE_SEC:
        blast("RZ bot was down. Last heartbeat " + str(data.get("iso") or ""))


def beat_loop():
    while True:
        write_beat(True)
        try:
            sweep_closed()
        except Exception as e:
            log("sweep", e)
        time.sleep(30)


def on_stop():
    write_beat(False)
    blast("RZ bot process stopped")


def find_pos(trade_id, ticket=0):
    ensure_mt5()
    poss = list(mt5.positions_get() or [])
    ticket = int(ticket or 0)
    needle = str(trade_id or "")
    if ticket:
        for p in poss:
            if p.magic == MAGIC and (p.ticket == ticket or p.identifier == ticket):
                return p
    if needle:
        for p in poss:
            if p.magic == MAGIC and needle in (p.comment or ""):
                return p
    return None


def resolve(raw):
    cfg = map_cfg(raw)
    if cfg is None:
        return None
    name = cfg["mt5"]
    info = mt5.symbol_info(name)
    if info is None:
        mt5.symbol_select(name, True)
        info = mt5.symbol_info(name)
    if info is None:
        return None
    return cfg


def lots(cfg, typ, tf):
    return lots_for(cfg, typ, tf)


def resolve_ticket(res, symbol):
    if res is None:
        return 0
    ticket = int(res.order or 0) or int(getattr(res, "deal", 0) or 0)
    if ticket:
        return ticket
    poss = mt5.positions_get(symbol=symbol) or []
    mine = [p for p in poss if p.magic == MAGIC]
    if not mine:
        return 0
    return int(sorted(mine, key=lambda p: p.time)[-1].ticket)


def num_or_none(raw):
    if raw in (None, "", "null"):
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return v


def tp_from_fill(side, fill, tp_dist):
    if not tp_dist:
        return 0.0
    return (fill + tp_dist) if side == "long" else (fill - tp_dist)


def set_sltp(symbol, ticket, sl_v, tp_v):
    if not ticket or (not sl_v and not tp_v):
        return None
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
        "sl": sl_v or 0.0,
        "tp": tp_v or 0.0,
    }
    res = mt5.order_send(req)
    log("sltp", getattr(res, "retcode", None), getattr(res, "comment", ""), "ticket", ticket, "sl", sl_v, "tp", tp_v)
    return res


def place(cfg, side, tp_dist, typ, trade_id, tf):
    """Market entry. Broker SL is unused — Pine exits (and TP distance) manage the trade."""
    ensure_mt5()
    symbol = cfg["mt5"]
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if side == "long" else tick.bid
    order = mt5.ORDER_TYPE_BUY if side == "long" else mt5.ORDER_TYPE_SELL
    vol = lots(cfg, typ, tf)
    if not trade_id:
        trade_id = f"auto-{typ}-{int(time.time())}"
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": vol,
        "type": order,
        "price": price,
        "sl": 0.0,
        "tp": 0.0,
        "deviation": 30,
        "magic": MAGIC,
        "comment": ("RZ " + typ + " " + trade_id)[:31],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res is None:
        log("mt5 none", mt5.last_error(), "id", trade_id)
        return None, trade_id, res
    ok = res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)
    log("mt5", res.retcode, getattr(res, "comment", ""), "ticket", int(res.order or 0), "id", trade_id)
    if not ok:
        log("mt5 reject", res)
    ticket = resolve_ticket(res, symbol)
    fill = float(res.price) if res.price else price
    tp_v = 0.0
    if ok and tp_dist:
        tp_v = tp_from_fill(side, fill, tp_dist)
        set_sltp(symbol, ticket, 0.0, tp_v)
    if ok:
        rec = {
            "ticket": ticket,
            "symbol_tv": cfg.get("_tv") or "",
            "symbol": symbol,
            "side": side,
            "type": typ,
            "tf": tf or "",
            "entry": fill,
            "stop": "",
            "tp": tp_v or "",
            "size": vol,
            "timestamp_entry": now_iso(),
        }
        with _book:
            book = load_book()
            book[trade_id] = rec
            save_book(book)
    return ticket, trade_id, res


def close_ticket(ticket):
    ensure_mt5()
    if not ticket:
        return False, None
    pos = None
    for p in mt5.positions_get() or []:
        if p.ticket == ticket or p.identifier == ticket:
            pos = p
            break
    if pos is None:
        return False, None
    tick = mt5.symbol_info_tick(pos.symbol)
    exit_px = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket,
        "price": exit_px,
        "deviation": 30,
        "magic": MAGIC,
        "comment": "RZ exit",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res is None:
        return False, None
    ok = res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)
    if not ok:
        return False, None
    if res.price:
        exit_px = float(res.price)
    return True, exit_px


def pnl_row(rec, exit_px):
    entry = float(rec.get("entry") or 0)
    side = rec.get("side")
    if not entry or not exit_px:
        return "", ""
    raw = (exit_px - entry) if side == "long" else (entry - exit_px)
    return round(raw, 5), round(raw / entry * 100.0, 4)


def same_book_symbol(rec, tv_sym):
    rec_tv = str(rec.get("symbol_tv") or "")
    tv_sym = str(tv_sym or "")
    if not rec_tv or not tv_sym:
        return False
    return rec_tv == tv_sym or tv_sym in rec_tv or rec_tv in tv_sym


def opposite_types(typ):
    if typ in ("B", "BE"):
        return ("S", "SE")
    if typ in ("S", "SE"):
        return ("B", "BE")
    return ()


def discord_exit(rec, trade_id, exit_px, reason):
    tf = rec.get("tf") or ""
    typ = rec.get("type") or ""
    side = "LONG" if rec.get("side") == "long" else "SHORT"
    entry = rec.get("entry")
    try:
        entry_f = float(entry)
        exit_f = float(exit_px)
        pct = (exit_f - entry_f) / entry_f * 100.0 if rec.get("side") == "long" else (entry_f - exit_f) / entry_f * 100.0
        sign = "+" if pct >= 0 else "−"
        body = (
            f"{tf} · {rec.get('symbol_tv') or rec.get('symbol') or ''}\n"
            f"{side}: {typ} . EXIT: {reason}\n"
            f"Entry {entry} → Exit {exit_px}\n"
            f"{sign}{abs(pct):.3f}%\n"
            f"Id {trade_id}"
        )
    except (TypeError, ValueError, ZeroDivisionError):
        body = f"{tf} · {typ} . EXIT: {reason}\nId {trade_id}"
    say(f"RZ flatten {tf} {typ} {reason} — {trade_id}")
    try:
        from rz_discord import forward_alert
        forward_alert(body)
    except Exception:
        pass


def flatten_tracked(tv_sym, tf, typ, reason="FLIP", want=None):
    want = tuple(want) if want is not None else opposite_types(typ)
    if not want:
        return []
    closed = []
    with _book:
        book = load_book()
        for trade_id, rec in list(book.items()):
            if rec.get("type") not in want:
                continue
            if (rec.get("tf") or "") != (tf or ""):
                continue
            if not same_book_symbol(rec, tv_sym):
                continue
            pos = find_pos(trade_id, rec.get("ticket"))
            did = False
            mt5_exit = None
            if pos is not None:
                did, mt5_exit = close_ticket(int(pos.ticket))
            exit_px = None
            if did:
                exit_px = mt5_exit if mt5_exit else rec.get("entry")
                finish_open(trade_id, rec, exit_px, reason, pos=pos)
                closed.append((trade_id, rec, exit_px))
            else:
                deal = find_close_deal(rec.get("ticket"))
                if deal is not None:
                    exit_px = float(deal.price) if getattr(deal, "price", 0) else rec.get("entry")
                    finish_open(trade_id, rec, exit_px, deal_reason_label(deal, reason))
                    closed.append((trade_id, rec, exit_px))
    for trade_id, rec, exit_px in closed:
        log("flatten", tv_sym, tf, typ, "closed", trade_id)
        discord_exit(rec, trade_id, exit_px, reason)
    return [c[0] for c in closed]


def flatten_all_open(reason="REFRESH"):
    """Close every tracked open before TV alert recreate (avoids orphan EXITs)."""
    with _book:
        items = list(load_book().items())
    closed = []
    for trade_id, rec in items:
        pos = find_pos(trade_id, rec.get("ticket"))
        did = False
        mt5_exit = None
        if pos is not None:
            did, mt5_exit = close_ticket(int(pos.ticket))
        if did:
            exit_px = mt5_exit if mt5_exit else rec.get("entry")
            finish_open(trade_id, rec, exit_px, reason, pos=pos)
            closed.append((trade_id, rec, exit_px))
            continue
        deal = find_close_deal(rec.get("ticket"))
        if deal is not None:
            exit_px = float(deal.price) if getattr(deal, "price", 0) else rec.get("entry")
            finish_open(trade_id, rec, exit_px, deal_reason_label(deal, reason))
            closed.append((trade_id, rec, exit_px))
            continue
        # Already flat at broker — drop book row so refresh stays clean
        exit_px = rec.get("entry")
        finish_open(trade_id, rec, exit_px, reason)
        closed.append((trade_id, rec, exit_px))
    for trade_id, rec, exit_px in closed:
        log("flatten_all", reason, "closed", trade_id, "was_tf", rec.get("tf"))
        discord_exit(rec, trade_id, exit_px, reason)
    return {"closed": [c[0] for c in closed], "n": len(closed)}


def handle_event(d):
    event = d.get("event")
    trade_id = d.get("tradeId") or ""
    typ = d.get("type", "BE")
    side = side_of(d)
    tv_sym = d.get("symbol")
    log("in", event, tv_sym, d.get("tf"), typ, "id", trade_id)
    cfg = resolve(tv_sym)
    if cfg is None:
        log("unknown symbol", tv_sym, "event", event, "type", typ)
        return {"ok": False, "error": "unknown symbol", "got": tv_sym}
    cfg = dict(cfg)
    cfg["_tv"] = tv_sym
    if event == "print":
        with _exec:
            closed = flatten_tracked(tv_sym, d.get("tf"), typ)
        log("print", tv_sym, d.get("tf"), typ, "closed", closed)
        return {"ok": True, "printed": typ, "closed": closed, "tf": d.get("tf")}
    if event == "entry":
        with _exec:
            flatten_tracked(tv_sym, d.get("tf"), typ)
            reason = entry_skip_reason(tv_sym, typ, d.get("tf"))
            if reason:
                log("skip", tv_sym, d.get("tf"), typ, reason, "id", trade_id)
                say(f"RZ skip {tv_sym} {d.get('tf') or ''} {typ} — {reason}")
                return {"ok": True, "skipped": reason, "tradeId": trade_id, "type": typ, "tf": d.get("tf")}
            log("place", tv_sym, d.get("tf"), typ, side, "id", trade_id)
            entry_px = num_or_none(d.get("entry"))
            tp_px = num_or_none(d.get("tp"))
            tp_dist = num_or_none(d.get("tpDist"))
            if tp_dist is None and entry_px and tp_px:
                tp_dist = abs(tp_px - entry_px)
            ticket, trade_id, res = place(cfg, side, tp_dist, typ, trade_id, d.get("tf"))
            log("placed", "ticket", ticket, "id", trade_id)
            return {"ok": True, "tradeId": trade_id, "side": side, "ticket": ticket, "mt5": str(res)}
    if event == "exit":
        reason = d.get("reason") or "CLOSED"
        with _book:
            rec = load_book().get(trade_id)
            if rec is None:
                log("exit ignore, not open", trade_id)
                return {"ok": True, "tradeId": trade_id, "closed": None, "found": False}
            pos = find_pos(trade_id, rec.get("ticket"))
            closed = False
            mt5_exit = None
            if pos is not None:
                closed, mt5_exit = close_ticket(int(pos.ticket))
            if closed:
                finish_open(trade_id, rec, mt5_exit if mt5_exit else rec.get("entry"), reason, pos=pos)
            else:
                deal = find_close_deal(rec.get("ticket"))
                if deal is not None:
                    px = float(deal.price) if getattr(deal, "price", 0) else d.get("exit") or rec.get("entry")
                    finish_open(trade_id, rec, px, deal_reason_label(deal, reason))
                    closed = True
                elif d.get("exit") not in (None, ""):
                    finish_open(trade_id, rec, d.get("exit"), reason)
                    closed = True
                else:
                    log("exit still open, left in book", trade_id)
            log("exit", trade_id, "closed", closed, "mt5", pos is not None, reason)
            return {"ok": True, "tradeId": trade_id, "closed": reason if closed else None, "found": pos is not None}
    log("bad event", event, "keys", list(d.keys()))
    return {"ok": False, "error": "bad event", "got": event, "body": d}


@app.get("/tv")
def tv_ping():
    return {"ok": True, "msg": "listener up — POST JSON here"}


@app.post("/flatten_all")
def flatten_all():
    """Market-close all tracked opens before TV alert refresh."""
    if request.headers.get("X-Secret") != SECRET and request.args.get("k") != SECRET:
        abort(401)
    with _exec:
        out = flatten_all_open(reason="REFRESH")
    log("http flatten_all", out)
    return {"ok": True, **out}


@app.post("/tv")
def tv():
    if request.headers.get("X-Secret") != SECRET and request.args.get("k") != SECRET:
        abort(401)
    raw = request.get_data(as_text=True)
    try:
        d = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad json", "raw": raw}
    try:
        from rz_discord import on_tv_payload
        on_tv_payload(d)
    except Exception:
        pass
    evs = d.get("events")
    if isinstance(evs, list) and evs:
        out = {"ok": True, "results": [handle_event(e) for e in evs]}
        log("http", out)
        return out
    out = handle_event(d)
    log("http", out)
    return out


if __name__ == "__main__":
    ensure_mt5()
    from rz_discord import start_discord_thread
    start_discord_thread()
    check_stale_beat()
    write_beat(True)
    threading.Thread(target=beat_loop, daemon=True).start()
    atexit.register(on_stop)
    app.run(host="127.0.0.1", port=8787, threaded=True)
