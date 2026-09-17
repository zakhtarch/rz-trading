import asyncio
import json
import os
import threading
import urllib.request

from rz_control import handle_rz, set_notifier, status_text

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
DISCORD_ALERT_WEBHOOK = os.environ.get("DISCORD_ALERT_WEBHOOK", "").strip()

_client = None
_notify_ch = None


def notify(text):
    client = _client
    ch = _notify_ch
    if not client or not ch:
        return
    try:
        asyncio.run_coroutine_threadsafe(ch.send(text), client.loop)
    except Exception:
        pass


def blast(text):
    notify(text)
    forward_alert(text)


def forward_alert(content):
    if not DISCORD_ALERT_WEBHOOK or not content:
        return
    try:
        req = urllib.request.Request(
            DISCORD_ALERT_WEBHOOK,
            data=json.dumps({"content": content}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print("discord alert failed:", e)


def on_tv_payload(d):
    if d.get("content"):
        forward_alert(d.get("content"))


def start_discord_thread():
    set_notifier(notify)
    if not DISCORD_BOT_TOKEN:
        print("discord off — DISCORD_BOT_TOKEN not set")
        return
    if not DISCORD_USER_ID:
        print("discord off — set DISCORD_USER_ID or commands are ignored")

    def runner():
        global _client, _notify_ch
        try:
            import discord
        except ImportError:
            print("discord off — run: pip install discord.py")
            return

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        _client = client

        @client.event
        async def on_ready():
            global _notify_ch
            print("discord ready as", client.user)
            if DISCORD_CHANNEL_ID:
                _notify_ch = client.get_channel(int(DISCORD_CHANNEL_ID))
                if _notify_ch:
                    await _notify_ch.send("RZ discord control is up.\n" + status_text())

        @client.event
        async def on_message(message):
            global _notify_ch
            if message.author.bot:
                return
            if DISCORD_CHANNEL_ID and str(message.channel.id) != DISCORD_CHANNEL_ID:
                return
            if not message.content.lower().startswith("rz"):
                return
            if not DISCORD_USER_ID or str(message.author.id) != DISCORD_USER_ID:
                return
            if _notify_ch is None:
                _notify_ch = message.channel
            reply = handle_rz(message.content)
            if reply:
                await message.channel.send(reply)

        try:
            asyncio.run(client.start(DISCORD_BOT_TOKEN))
        except Exception as e:
            print("discord failed:", e)

    threading.Thread(target=runner, daemon=True).start()
