#!/usr/bin/env python3
"""
Rebuild RZ TradingView alerts from the latest saved Pine script.

TradingView freezes a copy of Pine inside each alert. There is no edit API.
This clones each existing RZ alert onto the newest compiled version, then
deletes the old one. You do not open Alert Manager.

Unofficial private endpoints the website already uses. Cookies expire.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTH_PATH = HERE / "auth.json"
BOT_PATH = HERE / "bot.json"
ALERTS_BASE = "https://pricealerts.tradingview.com"
PINE_BASE = "https://pine-facade.tradingview.com"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
CTX = ssl.create_default_context()


def load_auth():
    if not AUTH_PATH.exists():
        sys.exit(f"Missing {AUTH_PATH}. Copy auth.example.json to auth.json and fill it.")
    auth = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
    need = ("username", "sessionid", "sessionid_sign", "device_t", "tv_ecuid")
    missing = [k for k in need if not str(auth.get(k, "")).strip()]
    if missing:
        sys.exit(f"auth.json missing: {', '.join(missing)}")
    return {k: str(auth[k]).strip() for k in need}


def cookie_header(auth):
    return (
        f"sessionid={auth['sessionid']}; "
        f"sessionid_sign={auth['sessionid_sign']}; "
        f"device_t={auth['device_t']}; "
        f"tv_ecuid={auth['tv_ecuid']}"
    )


def headers(auth, json_body=False):
    h = {
        "Accept": "*/*",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "User-Agent": UA,
        "Cookie": cookie_header(auth),
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def alert_qs(auth):
    return urllib.parse.urlencode(
        {
            "log_username": auth["username"],
            "maintenance_unset_reason": "initial_operated",
            "build_time": "2026-07-31T09:00:10",
        }
    )


def http_json(auth, method, url, body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers(auth, json_body=body is not None),
        method=method,
    )
    raw = ""
    last_err = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, context=CTX, timeout=120) as res:
                raw = res.read().decode("utf-8")
            last_err = None
            break
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            raise SystemExit(f"HTTP {e.code} {url}\n{raw[:800]}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
            continue
    if last_err is not None:
        raise SystemExit(f"HTTP retry failed {url}: {last_err}") from last_err
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"Non-JSON from {url}:\n{raw[:800]}")
    if isinstance(parsed, dict) and parsed.get("s") in ("error", "unauthorized"):
        if parsed.get("s") == "unauthorized" or "unauthor" in str(parsed).lower():
            raise SystemExit(
                "Session rejected. Run: python3 pull_tv_session.py"
            )
        raise SystemExit(f"TradingView error on {url}: {parsed}")
    return parsed


def alerts_call(auth, endpoint, body=None):
    url = f"{ALERTS_BASE}/{endpoint}?{alert_qs(auth)}"
    method = "GET" if body is None else "POST"
    parsed = http_json(auth, method, url, body)
    if isinstance(parsed, dict) and "r" in parsed:
        return parsed["r"]
    if isinstance(parsed, dict) and "d" in parsed:
        return parsed["d"]
    return parsed


def pine_get(auth, path):
    url = f"{PINE_BASE}{path}"
    parsed = http_json(auth, "GET", url)
    if isinstance(parsed, dict) and "r" in parsed:
        return parsed["r"]
    return parsed


def unwrap_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("r", "d", "alerts", "results", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def series_of(alert):
    cond = alert.get("condition") or {}
    if not cond and isinstance(alert.get("conditions"), list) and alert["conditions"]:
        cond = alert["conditions"][0]
    series = cond.get("series") or []
    return cond, (series[0] if series else {})


def hook_kind(alert):
    hook = str(alert.get("web_hook") or "")
    if "/tv?" in hook or hook.endswith("/tv"):
        return "bot"
    if "discord.com" in hook:
        return "discord"
    return "yes" if hook else "no"


def symbol_key(alert):
    return (alert.get("symbol") or "", str(alert.get("resolution") or ""))


def load_bot_webhook(cli_url=""):
    secret = ""
    if cli_url.strip():
        raw = cli_url.strip()
    elif BOT_PATH.exists():
        cfg = json.loads(BOT_PATH.read_text(encoding="utf-8"))
        raw = str(cfg.get("tunnel") or cfg.get("webhook") or "").strip()
        secret = str(cfg.get("secret") or "").strip()
    else:
        raw = ""
    if not raw:
        sys.exit(
            "No tunnel URL. Put it in bot.json as {\"tunnel\": \"https://….trycloudflare.com\", "
            "\"secret\": \"…\"} or pass --tunnel. Copy the https line from the VPS start_tunnel.bat window."
        )
    if not secret:
        sys.exit(
            "No webhook secret. Set \"secret\" in bot.json to match WEBHOOK_SECRET in Bot/start_rz.bat."
        )
    raw = raw.rstrip("/")
    if "/tv" in raw:
        return raw if "?k=" in raw else f"{raw}?k={secret}"
    return f"{raw}/tv?k={secret}"


def alert_title(alert):
    studies = (alert.get("presentation_data") or {}).get("studies") or {}
    if studies:
        desc = (next(iter(studies.values()), {}) or {}).get("description")
        if desc:
            return desc
    return alert.get("name") or ""


def is_rz_alert(alert, needle):
    cond, series = series_of(alert)
    hay = " ".join(
        [
            str(alert_title(alert)),
            str(alert.get("name") or ""),
            str(series.get("pine_id") or ""),
            str(series.get("study") or ""),
            json.dumps(cond, default=str),
        ]
    ).lower()
    return needle.lower() in hay


def list_saved_scripts(auth):
    raw = pine_get(auth, "/pine-facade/list?filter=saved")
    scripts = unwrap_list(raw) or (raw if isinstance(raw, list) else [])
    out = []
    for s in scripts:
        out.append(
            {
                "pine_id": s.get("scriptIdPart"),
                "version": s.get("version"),
                "name": s.get("scriptName"),
                "title": s.get("scriptTitle"),
            }
        )
    return out


def find_script(scripts, ref="RZ Chaudhrys"):
    needle = ref.lower()
    for s in scripts:
        if (s.get("name") or "").lower() == needle or (s.get("title") or "").lower() == needle:
            return s
    for s in scripts:
        if needle in (s.get("name") or "").lower() or needle in (s.get("title") or "").lower():
            return s
    return None


def fetch_inputs(auth, pine_id, version):
    data = pine_get(
        auth,
        f"/pine-facade/translate/{urllib.parse.quote(str(pine_id), safe='')}/"
        f"{urllib.parse.quote(str(version), safe='')}",
    )
    meta = (
        (data.get("result") or {}).get("metaInfo")
        if isinstance(data, dict)
        else None
    )
    if not meta and isinstance(data, dict):
        meta = data.get("metaInfo") or data.get("meta_info") or {}
    defaults = (meta or {}).get("defaults", {}).get("inputs")
    if not defaults:
        raise SystemExit(
            "Could not read Pine inputs from translate. Save/compile RZ in the "
            "Pine editor first, then rerun."
        )
    return defaults


def merge_inputs(defaults, old_inputs):
    # New Pine versions shift in_* indexes. Copying old values onto new
    # keys sends the wrong types and TradingView rejects the create.
    out = {}
    for key, val in defaults.items():
        if key.startswith("in_") or key in ("__profile", "__fast_calc", "pineFeatures"):
            out[key] = val
    if "pineFeatures" in defaults:
        out["pineFeatures"] = defaults["pineFeatures"]
    if "__profile" not in out:
        out["__profile"] = False
    if "__fast_calc" not in out:
        out["__fast_calc"] = bool((old_inputs or {}).get("__fast_calc", False))
    return out


def far_expiration():
    # TradingView rejects expirations more than ~31 days out.
    return (datetime.now(timezone.utc) + timedelta(days=31)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def build_create_body(old, pine_id, pine_version, defaults):
    cond, series = series_of(old)
    new_series = {
        "type": series.get("type") or "study",
        "study": series.get("study") or "Script@tv-scripting-101",
        "pine_id": pine_id,
        "pine_version": str(pine_version),
        "inputs": merge_inputs(defaults, series.get("inputs")),
    }
    new_cond = {
        "type": cond.get("type") or "pine_alert",
        "frequency": cond.get("frequency") or "60",
        "series": [new_series],
        "cross_interval": bool(cond.get("cross_interval", False)),
        "resolution": old.get("resolution") or cond.get("resolution"),
    }
    exp = far_expiration()
    payload = {
        "conditions": [new_cond],
        "symbol": old.get("symbol"),
        "resolution": old.get("resolution"),
        "message": old.get("message") if old.get("message") is not None else "",
        "sound_duration": old.get("sound_duration") or 0,
        "popup": False,
        "auto_deactivate": False,
        "email": bool(old.get("email", False)),
        "sms_over_email": bool(old.get("sms_over_email", False)),
        "mobile_push": bool(old.get("mobile_push", False)),
        "name": old.get("name") or "RZ Chaudhrys",
        "expiration": exp,
        "expiration_policy": {"time": exp, "policy": "fixed_date"},
        "type": old.get("type") or "indicator",
        "active": True,
        "ignore_warnings": True,
    }
    if old.get("sound_file"):
        payload["sound_file"] = old.get("sound_file")
    if old.get("web_hook"):
        payload["web_hook"] = old.get("web_hook")
    return {"payload": payload}


def wait_active(auth, alert_id, tries=40):
    for _ in range(tries):
        time.sleep(0.8)
        for a in unwrap_list(alerts_call(auth, "list_alerts")):
            if str(a.get("alert_id")) == str(alert_id) and a.get("active"):
                return True
    return False


WANTED_RES = ("1", "5", "15", "60")


def clone_alert_at_tf(auth, old, pine_id, pine_version, defaults, resolution, dry):
    title = alert_title(old) or old.get("name") or old.get("alert_id")
    print(f"  create {title}  tf={resolution}  hook={hook_kind(old)}")
    if dry:
        return True
    body = build_create_body(old, pine_id, pine_version, defaults)
    body["payload"]["resolution"] = str(resolution)
    if body["payload"].get("conditions"):
        body["payload"]["conditions"][0]["resolution"] = str(resolution)
    created = alerts_call(auth, "create_alert", body)
    new_id = None
    if isinstance(created, dict):
        new_id = created.get("alert_id") or (created.get("r") or {}).get("alert_id")
    if not new_id:
        print(f"    create response: {created}")
        return False
    if not wait_active(auth, new_id):
        print(f"    new alert {new_id} never went active — left in place, old alerts kept")
        return False
    print(f"    new id {new_id}")
    return True


def cmd_ensure_tfs(auth, needle, pine_id, pine_version, inputs, dry):
    raw = unwrap_list(alerts_call(auth, "list_alerts"))
    rows = [a for a in raw if is_rz_alert(a, needle)]
    groups = {}
    for a in rows:
        groups.setdefault((a.get("symbol"), hook_kind(a)), []).append(a)
    made = 0
    for (_symbol, _hook), alerts in groups.items():
        have = {str(a.get("resolution") or "") for a in alerts}
        template = next((a for a in alerts if str(a.get("resolution")) == "5"), alerts[0])
        for res in WANTED_RES:
            if res in have:
                continue
            if not clone_alert_at_tf(auth, template, pine_id, pine_version, inputs, res, dry):
                sys.exit(f"Failed creating tf={res} alert. Existing alerts left in place.")
            made += 1
            have.add(res)
    print(f"\nMissing-tf create: {made}")


def cmd_list(auth, needle):
    raw = unwrap_list(alerts_call(auth, "list_alerts"))
    rows = [a for a in raw if is_rz_alert(a, needle)]
    print(f"{len(rows)} RZ alert(s) of {len(raw)} total\n")
    for a in rows:
        _, series = series_of(a)
        print(
            f"  {a.get('alert_id')}  {alert_title(a) or a.get('name')}  "
            f"tf={a.get('resolution')}  pine_v={series.get('pine_version')}  "
            f"hook={hook_kind(a)}  "
            f"active={a.get('active')}"
        )


def cmd_dump(auth, needle):
    raw = unwrap_list(alerts_call(auth, "list_alerts"))
    rows = [a for a in raw if is_rz_alert(a, needle)]
    if not rows:
        sys.exit("No RZ alerts found.")
    out = HERE / "last_rz_alert.json"
    out.write_text(json.dumps(rows[0], indent=2), encoding="utf-8")
    print(f"Wrote {out}")


def load_bot_cfg():
    if not BOT_PATH.exists():
        return {}
    try:
        return json.loads(BOT_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def bot_flatten_all_before_refresh():
    """Close live tracked opens so alert recreate cannot orphan EXIT webhooks."""
    cfg = load_bot_cfg()
    tunnel = str(cfg.get("tunnel") or "").strip().rstrip("/")
    secret = str(cfg.get("secret") or "").strip()
    if not tunnel:
        sys.exit(
            "Cannot flatten before refresh: bot.json missing tunnel. "
            "Put {\"tunnel\": \"https://….trycloudflare.com\", \"secret\": \"…\"} "
            "or pass --skip-flatten."
        )
    if not secret:
        sys.exit(
            "Cannot flatten before refresh: bot.json missing secret "
            "(must match WEBHOOK_SECRET on the VPS), or pass --skip-flatten."
        )
    if tunnel.endswith("/tv"):
        tunnel = tunnel[: -len("/tv")]
    url = f"{tunnel}/flatten_all?k={urllib.parse.quote(secret)}"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"User-Agent": UA, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        sys.exit(f"flatten_all failed ({e}). Fix tunnel/bot or pass --skip-flatten.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(f"flatten_all non-JSON:\n{raw[:500]}")
    if not data.get("ok"):
        sys.exit(f"flatten_all rejected: {data}")
    n = int(data.get("n") or 0)
    closed = data.get("closed") or []
    print(f"Flattened {n} open trade(s) before refresh: {closed or '(none)'}\n")


def cmd_refresh(auth, needle, dry, skip_flatten=False):
    if not dry and not skip_flatten:
        bot_flatten_all_before_refresh()
    elif not dry and skip_flatten:
        print("WARNING: --skip-flatten — open trades may orphan after recreate.\n")
    scripts = list_saved_scripts(auth)
    script = find_script(scripts, "RZ Chaudhrys") or find_script(scripts, "RZ")
    if not script or not script.get("pine_id"):
        sys.exit(
            "Saved script 'RZ Chaudhrys' not found. Save it in the Pine editor "
            "on this TradingView account, then rerun."
        )
    pine_id = script["pine_id"]
    pine_version = script["version"]
    inputs = fetch_inputs(auth, pine_id, pine_version)
    print(f"Latest Pine: {script.get('title') or script.get('name')}  v{pine_version}")

    raw = unwrap_list(alerts_call(auth, "list_alerts"))
    rows = [a for a in raw if is_rz_alert(a, needle)]
    if not rows:
        sys.exit("No RZ alerts to refresh.")
    print(f"Refreshing {len(rows)} alert(s)\n")

    for old in rows:
        old_id = old.get("alert_id")
        title = alert_title(old) or old.get("name") or old_id
        _, series = series_of(old)
        old_v = series.get("pine_version")
        print(f"  {title}  {old_id}  v{old_v} -> v{pine_version}")
        if str(old_v) == str(pine_version):
            print("    already on latest, skip")
            continue
        if dry:
            continue
        body = build_create_body(old, pine_id, pine_version, inputs)
        created = alerts_call(auth, "create_alert", body)
        new_id = None
        if isinstance(created, dict):
            new_id = created.get("alert_id") or (created.get("r") or {}).get("alert_id")
        if not new_id:
            print(f"    create response: {created}")
            sys.exit("Create failed. Old alert left in place.")
        if not wait_active(auth, new_id):
            print(f"    restart {new_id}")
            alerts_call(auth, "restart_alerts", {"payload": {"alert_ids": [int(new_id)]}})
            if not wait_active(auth, new_id):
                sys.exit(f"New alert {new_id} never went active. Old {old_id} left in place.")
        if not old.get("active", True):
            alerts_call(auth, "stop_alerts", {"payload": {"alert_ids": [int(new_id)]}})
            print(f"    new id {new_id} paused (old was inactive), deleted {old_id}")
        else:
            print(f"    new id {new_id}, deleted {old_id}")
        alerts_call(auth, "delete_alerts", {"payload": {"alert_ids": [int(old_id)]}})
    print("\nDone. Check the Alerts list — same names/webhooks, new snapshot.")
    cmd_ensure_tfs(auth, needle, pine_id, pine_version, inputs, dry)


def cmd_create_bot(auth, needle, webhook, dry):
    scripts = list_saved_scripts(auth)
    script = find_script(scripts, "RZ Chaudhrys") or find_script(scripts, "RZ")
    if not script or not script.get("pine_id"):
        sys.exit("Saved script 'RZ Chaudhrys' not found.")
    pine_id = script["pine_id"]
    pine_version = script["version"]
    inputs = fetch_inputs(auth, pine_id, pine_version)
    print(f"Latest Pine: {script.get('title') or script.get('name')}  v{pine_version}")
    print(f"Bot webhook: {webhook}\n")

    raw = unwrap_list(alerts_call(auth, "list_alerts"))
    rows = [a for a in raw if is_rz_alert(a, needle)]
    existing_bot = {symbol_key(a) for a in rows if hook_kind(a) == "bot"}
    templates = []
    seen = set()
    for a in rows:
        key = symbol_key(a)
        if key in seen:
            continue
        seen.add(key)
        templates.append(a)

    if not templates:
        sys.exit("No RZ Discord/chart alerts to clone. Create those first.")

    made = 0
    for old in templates:
        key = symbol_key(old)
        title = alert_title(old) or old.get("name")
        if key in existing_bot:
            print(f"  skip {title} tf={old.get('resolution')} — bot alert already exists")
            continue
        print(f"  create {title} tf={old.get('resolution')} -> RZ Chaudhrys Bot")
        if dry:
            continue
        body = build_create_body(old, pine_id, pine_version, inputs)
        body["payload"]["name"] = "RZ Chaudhrys Bot"
        body["payload"]["web_hook"] = webhook
        body["payload"]["popup"] = False
        body["payload"]["active"] = True
        created = alerts_call(auth, "create_alert", body)
        new_id = None
        if isinstance(created, dict):
            new_id = created.get("alert_id") or (created.get("r") or {}).get("alert_id")
        if not new_id:
            print(f"    create response: {created}")
            sys.exit("Bot alert create failed.")
        if not wait_active(auth, new_id):
            sys.exit(f"Bot alert {new_id} never went active.")
        print(f"    new id {new_id}")
        made += 1
    print(f"\nDone. Created {made} bot alert(s). Discord alerts left as-is.")


def main():
    p = argparse.ArgumentParser(description="Refresh RZ TradingView alerts from latest Pine")
    p.add_argument("cmd", choices=("list", "dump", "refresh", "create-bot"))
    p.add_argument("--match", default="chaudhry", help="Alert filter text (default: chaudhry)")
    p.add_argument("--dry-run", action="store_true", help="Show work without creating/deleting")
    p.add_argument(
        "--skip-flatten",
        action="store_true",
        help="Do not market-close bot opens before refresh (can orphan EXIT alerts)",
    )
    p.add_argument("--tunnel", default="", help="Cloudflare tunnel base URL for create-bot")
    args = p.parse_args()
    auth = load_auth()
    if args.cmd == "list":
        cmd_list(auth, args.match)
    elif args.cmd == "dump":
        cmd_dump(auth, args.match)
    elif args.cmd == "create-bot":
        cmd_create_bot(auth, args.match, load_bot_webhook(args.tunnel), args.dry_run)
    else:
        cmd_refresh(auth, args.match, args.dry_run, skip_flatten=args.skip_flatten)


if __name__ == "__main__":
    main()
