"""
main.py
-------
Entry point. Starts scanner loop AND listens for Telegram commands.
"""

import threading
import time
import requests
import os
from scanner.scanner import Scanner

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

    # Start Telegram command listener in background
    cmd_thread = threading.Thread(
        target=listen_for_commands,
        args=(scanner,),
        daemon=True
    )
    cmd_thread.start()
    print("[MAIN] Telegram command listener started.")

    # Start main scanner loop
    scanner.run()
