@echo off
cd /d C:\Users\Administrator\Desktop\RZ

REM Must match TradingView bot webhook ?k= and Alerts/bot.json
set WEBHOOK_SECRET=CHANGE_ME

set DISCORD_BOT_TOKEN=
set DISCORD_USER_ID=
set DISCORD_CHANNEL_ID=
set DISCORD_ALERT_WEBHOOK=

start "" "C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
timeout /t 15 /nobreak
python C:\Users\Administrator\Desktop\RZ\rz_scalping.py
if not "%DISCORD_ALERT_WEBHOOK%"=="" curl.exe -s -H "Content-Type: application/json" -d "{\"content\":\"RZ bot process exited\"}" "%DISCORD_ALERT_WEBHOOK%"
echo RZ python exited
pause
