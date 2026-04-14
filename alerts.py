"""
alerts.py - Telegram alert formatting and delivery.
"""

import os
import requests
from datetime import datetime
import pytz

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ET               = pytz.timezone("America/New_York")


def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT - NO TELEGRAM]\n{message}")
        return
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print(f"[ALERT SENT] {datetime.now(ET).strftime('%H:%M:%S')}")
    except Exception as e:
        print(f"[ALERT ERROR] {e}")


def format_alert(ticker, grade, score, triggered_conditions, time_et=None):
    if time_et is None:
        time_et = datetime.now(ET).strftime("%I:%M %p ET")

    grade_emoji = {"A+": "🔥", "A": "✅", "B": "⚠️", "BELOW_THRESHOLD": "❌"}.get(grade, "📊")

    lines = [
        f"{grade_emoji} <b>SETUP ALERT — {ticker}</b>",
        f"Grade: <b>{grade}</b>  |  Confluence: {score} pts",
        f"{'━' * 28}",
    ]

    icons = {
        "above pm high": "🔼", "9 ema": "📈", "vwap": "📊",
        "volume": "🔥", "prior day": "💥", "retrace": "🎯",
        "order block": "🧱", "option": "📉", "bull flag": "🚩", "earnings": "📋"
    }

    for detail in triggered_conditions:
        cond = detail.get("condition", "")
        icon = next((v for k, v in icons.items() if k in cond.lower()), "•")

        extra = ""
        cl    = cond.lower()
        if "pm high"   in cl: extra = f"  PM High: ${detail.get('pm_high')}  |  Now: ${detail.get('current')}"
        elif "ema"     in cl: extra = f"  EMA: ${detail.get('ema')}  |  Now: ${detail.get('current')}  ({detail.get('diff_pct')}% away)"
        elif "vwap"    in cl: extra = f"  VWAP: ${detail.get('vwap')}  |  Now: ${detail.get('current')}  ({detail.get('diff_pct'):+}%)"
        elif "volume"  in cl: extra = f"  {detail.get('ratio')}x avg volume"
        elif "prior"   in cl: extra = f"  PDH: ${detail.get('prior_day_high')}  |  Vol: {detail.get('vol_ratio')}x avg"
        elif "retrace" in cl and "option" not in cl: extra = f"  High: ${detail.get('session_high')}  |  Pullback: {detail.get('pullback_pct')}%"
        elif "order"   in cl: extra = f"  Zone: {detail.get('ob_zone')}  |  Now: ${detail.get('current')}"
        elif "option"  in cl: extra = f"  Contract high: ${detail.get('session_high')}  →  Now: ${detail.get('current')}  ({detail.get('retrace_level')} retrace)"
        elif "flag"    in cl: extra = f"  {detail.get('higher_lows_count')} higher lows  |  Compressing: {detail.get('compressing')}"
        elif "earnings" in cl: extra = f"  Prior VWAP: ${detail.get('prior_vwap')}  |  Now: ${detail.get('current')}"

        lines.append(f"{icon} <b>{cond}</b>")
        if extra:
            lines.append(f"   {extra.strip()}")

    lines.append(f"{'━' * 28}")
    lines.append(f"⏰ {time_et}")
    lines.append(f"👉 Manual review → place trade if confirmed")
    return "\n".join(lines)


def send_startup_message(watchlist):
    tickers = ", ".join(watchlist)
    msg = (
        f"🤖 <b>Trading Scanner Online</b>\n"
        f"{'━' * 28}\n"
        f"Watching: <b>{tickers}</b>\n"
        f"Scanning every 60 seconds\n"
        f"Market hours: 9:30 AM – 4:00 PM ET\n"
        f"{'━' * 28}\n"
        f"Commands:\n"
        f"  /ob TICKER LOW HIGH\n"
        f"  /watch TICKER\n"
        f"  /remove TICKER\n"
        f"  /status\n"
        f"  /flag TICKER\n"
        f"  /earnings TICKER\n"
        f"  /option TICKER CONTRACT\n"
    )
    send_telegram(msg)


def send_status(watchlist, order_blocks, flagged_drives):
    lines = ["📋 <b>Scanner Status</b>", f"{'━' * 28}",
             f"Watching: {', '.join(watchlist) if watchlist else 'none'}"]
    if order_blocks:
        lines.append("🧱 Order blocks:")
        for t, z in order_blocks.items():
            lines.append(f"   {t}: ${z[0]} – ${z[1]}")
    if flagged_drives:
        lines.append(f"🚩 Opening drives flagged: {', '.join(flagged_drives)}")
    send_telegram("\n".join(lines))
