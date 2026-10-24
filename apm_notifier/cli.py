from __future__ import annotations

import argparse
import getpass
import json
import logging
from pathlib import Path
import sqlite3
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from .config import PROJECT_ROOT, Settings, load_sources, save_env_values
from .fetch import HttpClient, trusted_ssl_context
from .filtering import RoleFilter
from .monitor import Monitor
from .notify import NotificationManager
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apm-notifier",
        description="Monitor company career pages for APM, PM intern, and product marketing intern roles.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the continuous monitor")
    run_parser.add_argument("--once", action="store_true", help="Check once and exit")
    run_parser.add_argument("--interval", type=int, help="Override the check interval in seconds")

    subparsers.add_parser("check", help="Fetch sources without notifying or changing state")
    subparsers.add_parser("setup-telegram", help="Securely configure Telegram and send a test alert")
    subparsers.add_parser("test-notification", help="Send a sample phone notification")
    subparsers.add_parser("sources", help="List configured sources")
    subparsers.add_parser("status", help="Show persisted source health and alert counts")
    history_parser = subparsers.add_parser("history", help="Read stored job and notification timestamps as JSON")
    history_parser.add_argument("--company", default="", help="Filter by exact company name, ignoring case")
    return parser


def _telegram_api(token: str, method: str) -> dict:
    endpoint = f"https://api.telegram.org/bot{quote(token, safe=':')}/{method}"
    try:
        with urlopen(endpoint, timeout=20, context=trusted_ssl_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code == 401:
            raise ValueError("Telegram rejected the token. Generate a new token in @BotFather and try again.") from None
        raise ValueError(f"Telegram returned HTTP {error.code}.") from None
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not reach Telegram: {getattr(error, 'reason', error)}") from None
    if not payload.get("ok"):
        raise ValueError(str(payload.get("description", "Telegram rejected the request")))
    return payload


def _latest_private_chat_id(payload: dict) -> str:
    for update in reversed(payload.get("result", [])):
        for key in ("message", "edited_message", "channel_post"):
            chat = update.get(key, {}).get("chat", {})
            if chat.get("type") == "private" and chat.get("id") is not None:
                return str(chat["id"])
    return ""


def _setup_telegram() -> int:
    print("Telegram setup keeps the token hidden and stores it only in this project's .env file.")
    token = getpass.getpass("Paste the replacement BotFather token (hidden): ").strip()
    if not token or ":" not in token:
        raise ValueError("That does not look like a Telegram bot token.")
    identity = _telegram_api(token, "getMe").get("result", {})
    username = str(identity.get("username", "")).strip()
    bot_label = f"@{username}" if username else "your new bot"
    print(f"Verified {bot_label}.")
    print(f"Open https://t.me/{username}, press Start, and send 'hello'." if username else "Open the bot, press Start, and send 'hello'.")

    chat_id = ""
    for _ in range(3):
        input("Press Enter after you have sent the message: ")
        chat_id = _latest_private_chat_id(_telegram_api(token, "getUpdates"))
        if chat_id:
            break
        print("No private message appeared yet. Send the bot another message, then try again.")
    if not chat_id:
        raise ValueError("Could not discover your chat ID after three attempts.")

    save_env_values(
        PROJECT_ROOT / ".env",
        {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id},
        template_path=PROJECT_ROOT / ".env.example",
    )
    notifier = NotificationManager(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        ntfy_topic="",
        ntfy_server="https://ntfy.sh",
        timeout_seconds=20,
    )
    outcome = notifier.send_test()
    if not outcome.delivered:
        raise ValueError("Telegram was configured, but the test notification could not be delivered.")
    print("Success: Telegram is configured and the test notification was sent.")
    print("Start monitoring with: python3 -m apm_notifier run")
    return 0


def _notifier(settings: Settings) -> NotificationManager:
    return NotificationManager(
        telegram_bot_token=settings.telegram_bot_token,
        telegram_chat_id=settings.telegram_chat_id,
        ntfy_topic=settings.ntfy_topic,
        ntfy_server=settings.ntfy_server,
        timeout_seconds=settings.request_timeout_seconds,
    )


def _monitor(settings: Settings, store: StateStore) -> Monitor:
    sources = load_sources(settings.sources_path)
    return Monitor(
        sources=sources,
        client=HttpClient(settings.request_timeout_seconds),
        role_filter=RoleFilter(settings.target_year, settings.excluded_years, settings.allowed_countries),
        store=store,
        notifier=_notifier(settings),
        concurrency=settings.request_concurrency,
        alert_on_first_run=settings.alert_on_first_run,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if arguments.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        if arguments.command == "setup-telegram":
            return _setup_telegram()
        settings = Settings.from_environment()
        if arguments.command == "sources":
            for source in load_sources(settings.sources_path):
                print(f"{source.name}: {source.career_url}")
            return 0
        if arguments.command == "test-notification":
            notifier = _notifier(settings)
            if not notifier.configured_channels:
                print("No phone channel configured. Set Telegram or ntfy values in .env.", file=sys.stderr)
                return 2
            outcome = notifier.send_test()
            if outcome.errors:
                print("; ".join(outcome.errors), file=sys.stderr)
            return 0 if outcome.delivered else 1

        store = StateStore(
            Path(":memory:") if arguments.command == "check" else settings.state_path,
            read_only=arguments.command == "history",
        )
        try:
            if arguments.command == "history":
                print(json.dumps([dict(row) for row in store.history_rows(arguments.company)], indent=2, ensure_ascii=False))
                return 0
            if arguments.command == "status":
                total, pending = store.job_counts()
                print(f"Tracked matches: {total}; pending notifications: {pending}")
                for row in store.status_rows():
                    state = "OK" if row["consecutive_failures"] == 0 else f"FAIL x{row['consecutive_failures']}"
                    print(
                        f"{state:>8}  {row['source_name']}: matches={row['last_match_count']} "
                        f"checked={row['last_checked_at']}"
                    )
                return 0
            monitor = _monitor(settings, store)
            if arguments.command == "check":
                summary = monitor.run_once(dry_run=True)
                return 0 if summary.sources_ok else 1
            if arguments.command == "run":
                if arguments.once:
                    summary = monitor.run_once()
                    return 0 if summary.sources_ok else 1
                interval = arguments.interval or settings.interval_seconds
                if interval < 60:
                    parser.error("--interval must be at least 60 seconds")
                monitor.run_forever(interval)
                return 0
        finally:
            store.close()
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    return 0
