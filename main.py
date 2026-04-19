"""
main.py - Entry point for trading scanner.
Handles import regardless of folder nesting.
"""

import sys
import os
import threading
import time
import requests

# ── Fix import path regardless of folder structure ──────────
# This makes Python find the scanner module whether files are
# at /app/scanner/ or /app/scanner/scanner/
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

# Also add parent directory in case of nesting
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

# Try importing Scanner — handle both flat and nested structures
try:
    from scanner.scanner import Scanner
    print("[MAIN] Imported Scanner from scanner.scanner")
except ImportError:
    try:
        from scanner import Scanner
        print("[MAIN] Imported Scanner from scanner directly")
    except ImportError as e:
        print(f"[MAIN] Import failed: {e}")
        print(f"[MAIN] Python path: {sys.path}")
        print(f"[MAIN] Files in current dir: {os.listdir(current_dir)}")
        raise

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")


def listen_for_commands(scanner_instance):
    """Poll Telegram for incoming commands from your phone."""
    offset = None
    while True:
        try:
            url    = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"timeout": 30, "offset": offset}
            r      = requests.get(url, params=params, timeout=35)
            data   = r.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "")
                if text.startswith("/"):
                    print(f"[COMMAND] Received: {text}")
                    scanner_instance.process_command(text)

        except Exception as e:
            print(f"[COMMAND LISTENER] Error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    scanner = Scanner()

    cmd_thread = threading.Thread(
        target=listen_for_commands,
        args=(scanner,),
        daemon=True
    )
    cmd_thread.start()
    print("[MAIN] Telegram command listener started.")

    scanner.run()
