#!/usr/bin/env python3
"""
Refresh auth.json from Chrome Profile 1 without Keychain or DevTools.

Copies that profile's Cookies DB into a throwaway headless Chrome, reads
TradingView cookies over CDP, writes auth.json. Do not print cookie values.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTH_PATH = HERE / "auth.json"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
COOKIE_SRC = (
    Path.home()
    / "Library/Application Support/Google/Chrome/Profile 1/Cookies"
)
TMP_DIR = Path("/tmp/rz-tv-chrome")
PORT = 9333
NEED = ("sessionid", "sessionid_sign", "device_t", "tv_ecuid")
USERNAME = "ZubairAkhtarChaudhry"


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def kill_helper() -> None:
    subprocess.run(
        ["pkill", "-f", f"user-data-dir={TMP_DIR}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def start_helper() -> subprocess.Popen:
    if not CHROME.exists():
        die(f"Chrome not found: {CHROME}")
    if not COOKIE_SRC.exists():
        die(f"Chrome Profile 1 Cookies not found: {COOKIE_SRC}")

    kill_helper()
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    dest = TMP_DIR / "Default"
    dest.mkdir(parents=True)
    shutil.copy2(COOKIE_SRC, dest / "Cookies")
    journal = COOKIE_SRC.with_name("Cookies-journal")
    if journal.exists():
        shutil.copy2(journal, dest / "Cookies-journal")

    proc = subprocess.Popen(
        [
            str(CHROME),
            f"--user-data-dir={TMP_DIR}",
            "--profile-directory=Default",
            f"--remote-debugging-port={PORT}",
            "--remote-allow-origins=*",
            "--headless=new",
            "--no-first-run",
            "--disable-extensions",
            "--disable-sync",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 20
    last_err = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else "") or ""
            die(f"Helper Chrome exited {proc.returncode}: {err[:300]}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/json/version", timeout=1
            ) as res:
                return proc
        except OSError as e:
            last_err = str(e)
            time.sleep(0.3)
    kill_helper()
    die(f"CDP did not come up on {PORT}: {last_err}")


def cdp_cookies() -> dict:
    try:
        from websocket import create_connection
    except ImportError:
        die("Missing websocket-client. Run: python3 -m pip install --user websocket-client")

    meta = json.load(
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=5)
    )
    ws = create_connection(meta["webSocketDebuggerUrl"], timeout=10)
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        data = json.loads(ws.recv())
    finally:
        ws.close()
    cookies = (data.get("result") or {}).get("cookies") or []
    tv = [c for c in cookies if "tradingview.com" in (c.get("domain") or "")]
    found = {
        c["name"]: c.get("value") or ""
        for c in tv
        if c.get("name") in NEED and c.get("value")
    }
    missing = [k for k in NEED if not found.get(k)]
    print(
        "cdp",
        "cookies",
        len(cookies),
        "tv",
        len(tv),
        "found",
        sorted(found),
        "missing",
        missing,
        "value_lens",
        {k: len(found.get(k, "")) for k in NEED},
    )
    if missing:
        die(f"Profile 1 is missing {missing}. Stay logged into TradingView on that Chrome profile.")
    return found


def write_auth(found: dict) -> None:
    prev = {}
    if AUTH_PATH.exists():
        try:
            prev = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = {}
    out = {
        "username": prev.get("username") or USERNAME,
        **{k: found[k] for k in NEED},
    }
    AUTH_PATH.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {AUTH_PATH} username={out['username']}")


def main() -> None:
    proc = start_helper()
    try:
        write_auth(cdp_cookies())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
        kill_helper()


if __name__ == "__main__":
    main()
