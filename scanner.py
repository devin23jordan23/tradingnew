"""
scanner.py  (Schwab version)
-----------------------------
Main scanner loop. Runs every 60 seconds during market hours.
Checks all conditions per ticker, scores them, fires Telegram alerts.
"""

import time
import os
from datetime import datetime
from collections import defaultdict
import pytz

# ── Swap this one import to switch between Polygon and Schwab ──
from scanner.schwab_data import (
    get_candles, get_current_price, get_premarket_high,
    get_prior_day_data, get_open_price, calculate_vwap,
    calculate_ema, calculate_atr, get_average_volume,
    get_option_quote, get_session_high
)
from scanner.conditions import (
    check_above_premarket_high, check_ema_touch, check_vwap,
    check_elevated_volume, check_prior_day_high_break,
    check_half_atr_retrace, check_order_block, check_option_retrace,
    check_bull_flag, check_earnings_vwap_pullback, score_conditions
)
from scanner.alerts import (
    send_telegram, format_alert, send_startup_message, send_status
)

ET = pytz.timezone("America/New_York")

# ─────────────────────────────────────────────
# YOUR WATCHLIST — edit this anytime
# or use /watch TICKER from Telegram
# ─────────────────────────────────────────────
DEFAULT_WATCHLIST = [
    "NVDA", "AMD", "TSLA", "PLTR", "AMZN",
    "MU", "MSFT", "AAPL", "META", "DELL"
]

# Minimum confluence score to fire an alert
# 3 = B setup, 5 = A setup, 7 = A+ setup
MIN_SCORE_TO_ALERT = 3

# Minutes between repeat alerts for the same ticker + condition
ALERT_COOLDOWN_MINUTES = 15

# ─────────────────────────────────────────────


class Scanner:
    def __init__(self):
        self.watchlist            = list(DEFAULT_WATCHLIST)
        self.order_blocks         = {}   # {"NVDA": (low, high)}
        self.flagged_drives       = set()
        self.option_watches       = {}   # {"NVDA": "NVDA  260328C00180000"}
        self.option_session_highs = {}
        self.last_alert_time      = defaultdict(lambda: None)
        self.earnings_names       = set()
        self.pm_highs             = {}
        self.pm_high_date         = None
        self.prior_day_cache      = {}
        self.prior_day_date       = None

    def is_market_hours(self):
        now          = datetime.now(ET)
        market_open  = now.replace(hour=9,  minute=25, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=5,  second=0, microsecond=0)
        if now.weekday() >= 5:
            return False
        return market_open <= now <= market_close

    def refresh_daily_cache(self):
        today = datetime.now(ET).date()
        if self.pm_high_date != today:
            print("[SCANNER] Refreshing premarket highs...")
            self.pm_highs = {}
            for ticker in self.watchlist:
                self.pm_highs[ticker] = get_premarket_high(ticker)
                time.sleep(0.5)
            self.pm_high_date = today

        if self.prior_day_date != today:
            print("[SCANNER] Refreshing prior day data...")
            self.prior_day_cache = {}
            for ticker in self.watchlist:
                self.prior_day_cache[ticker] = get_prior_day_data(ticker)
                time.sleep(0.5)
            self.prior_day_date = today

    def can_alert(self, ticker, condition_key):
        key  = f"{ticker}:{condition_key}"
        last = self.last_alert_time[key]
        if last is None:
            return True
        elapsed = (datetime.now(ET) - last).total_seconds() / 60
        return elapsed >= ALERT_COOLDOWN_MINUTES

    def mark_alert(self, ticker, condition_key):
        self.last_alert_time[f"{ticker}:{condition_key}"] = datetime.now(ET)

    def scan_ticker(self, ticker):
        """Run all conditions for one ticker. Returns list of triggered details."""
        triggered = []

        candles = get_candles(ticker, multiplier=5, limit=60)
        if not candles:
            return []

        current_price = get_current_price(ticker)
        if not current_price:
            return []

        closes             = [c["close"] for c in candles]
        ema9               = calculate_ema(closes, 9)
        vwap               = calculate_vwap(candles)
        atr                = calculate_atr(ticker)
        avg_vol            = get_average_volume(candles)
        session_high       = get_session_high(candles)
        open_price         = get_open_price(ticker)
        pm_high            = self.pm_highs.get(ticker)
        prior_day          = self.prior_day_cache.get(ticker, {})
        current_candle_vol = candles[-1]["volume"] if candles else 0

        # 1. Above premarket high
        ok, detail = check_above_premarket_high(current_price, pm_high)
        if ok:
            triggered.append(detail)

        # 2. 9 EMA touch
        ok, detail = check_ema_touch(current_price, ema9)
        if ok:
            triggered.append(detail)

        # 3. VWAP
        if vwap:
            direction = "above" if current_price > vwap else "below"
            ok, detail = check_vwap(current_price, vwap, direction)
            if ok:
                triggered.append(detail)

        # 4. Elevated volume
        ok, detail = check_elevated_volume(current_candle_vol, avg_vol)
        if ok:
            triggered.append(detail)

        # 5. Prior day high break on volume
        ok, detail = check_prior_day_high_break(
            current_price, prior_day.get("high"),
            current_candle_vol, avg_vol
        )
        if ok:
            triggered.append(detail)

        # 6. Half ATR + 50% retrace
        if open_price and atr and session_high:
            ok, detail = check_half_atr_retrace(
                current_price, open_price, atr, session_high
            )
            if ok:
                triggered.append(detail)

        # 7. Order block (manually set via /ob command)
        if ticker in self.order_blocks:
            ob_low, ob_high = self.order_blocks[ticker]
            ok, detail = check_order_block(current_price, ob_low, ob_high)
            if ok:
                triggered.append(detail)

        # 8. Bull flag (only if you flagged an opening drive via /flag)
        if ticker in self.flagged_drives:
            ok, detail = check_bull_flag(candles[-6:])
            if ok:
                triggered.append(detail)

        # 9. Earnings gap → prior VWAP pullback
        if ticker in self.earnings_names:
            ok, detail = check_earnings_vwap_pullback(
                current_price, prior_day.get("vwap")
            )
            if ok:
                triggered.append(detail)

        # 10. Option contract retracement
        if ticker in self.option_watches:
            contract  = self.option_watches[ticker]
            opt_price = get_option_quote(contract)
            if opt_price:
                prev_high = self.option_session_highs.get(contract, 0)
                if opt_price > prev_high:
                    self.option_session_highs[contract] = opt_price
                session_opt_high = self.option_session_highs.get(contract)
                ok, detail = check_option_retrace(opt_price, session_opt_high)
                if ok:
                    triggered.append(detail)

        return triggered

    def process_command(self, command: str):
        """Handle Telegram commands sent from your phone."""
        parts = command.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()

        if cmd == "/ob" and len(parts) == 4:
            ticker        = parts[1].upper()
            low, high     = float(parts[2]), float(parts[3])
            self.order_blocks[ticker] = (low, high)
            send_telegram(f"🧱 Order block set for {ticker}: ${low} – ${high}")

        elif cmd == "/watch" and len(parts) == 2:
            ticker = parts[1].upper()
            if ticker not in self.watchlist:
                self.watchlist.append(ticker)
            send_telegram(f"✅ Added {ticker} to watchlist")

        elif cmd == "/remove" and len(parts) == 2:
            ticker         = parts[1].upper()
            self.watchlist = [t for t in self.watchlist if t != ticker]
            send_telegram(f"🗑️ Removed {ticker} from watchlist")

        elif cmd == "/status":
            send_status(self.watchlist, self.order_blocks, self.flagged_drives)

        elif cmd == "/flag" and len(parts) == 2:
            ticker = parts[1].upper()
            self.flagged_drives.add(ticker)
            send_telegram(f"🚩 Opening drive flagged for {ticker} — watching for bull flag")

        elif cmd == "/earnings" and len(parts) == 2:
            ticker = parts[1].upper()
            self.earnings_names.add(ticker)
            send_telegram(f"📋 {ticker} flagged as earnings — watching for prior VWAP pullback")

        elif cmd == "/option" and len(parts) == 3:
            ticker   = parts[1].upper()
            contract = parts[2]
            self.option_watches[ticker] = contract
            send_telegram(f"📉 Watching option contract for {ticker}: {contract}")

        elif cmd == "/clearob" and len(parts) == 2:
            ticker = parts[1].upper()
            self.order_blocks.pop(ticker, None)
            send_telegram(f"🗑️ Order block cleared for {ticker}")

        else:
            send_telegram(
                "❓ Unknown command.\n\n"
                "Available commands:\n"
                "/watch TICKER\n"
                "/remove TICKER\n"
                "/ob TICKER LOW HIGH\n"
                "/clearob TICKER\n"
                "/flag TICKER\n"
                "/earnings TICKER\n"
                "/option TICKER CONTRACT\n"
                "/status"
            )

    def run(self):
        """Main scanner loop."""
        print("[SCANNER] Starting up...")
        send_startup_message(self.watchlist)

        while True:
            if not self.is_market_hours():
                now = datetime.now(ET)
                print(f"[SCANNER] Outside market hours ({now.strftime('%H:%M ET')}). Sleeping 5 min...")
                time.sleep(300)
                continue

            self.refresh_daily_cache()

            for ticker in list(self.watchlist):
                try:
                    print(f"[SCANNER] Scanning {ticker}...")
                    triggered = self.scan_ticker(ticker)

                    if not triggered:
                        continue

                    score, grade = score_conditions(triggered)

                    if score < MIN_SCORE_TO_ALERT:
                        continue

                    cond_key = "|".join(d.get("condition", "") for d in triggered)

                    if not self.can_alert(ticker, cond_key):
                        continue

                    msg = format_alert(
                        ticker=ticker,
                        grade=grade,
                        score=score,
                        triggered_conditions=triggered
                    )
                    send_telegram(msg)
                    self.mark_alert(ticker, cond_key)

                except Exception as e:
                    print(f"[SCANNER] Error scanning {ticker}: {e}")

                time.sleep(0.5)

            print(f"[SCANNER] Cycle complete. Sleeping 60s...")
            time.sleep(60)
