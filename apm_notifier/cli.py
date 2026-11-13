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
from .export import export_matches
from .monitor import Monitor, MonitorOptions
from .notify import NotificationManager
from .state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apm-notifier",
        description="Monitor company career sites for Summer 2027 product-management internships.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the continuous monitor")
    run_parser.add_argument("--once", action="store_true", help="Check once and exit")
    run_parser.add_argument("--interval", type=int, help="Override the check interval in seconds")

    check_parser = subparsers.add_parser("check", help="Fetch and classify sources without notifying or changing state")
    check_parser.add_argument("--all", action="store_true", help="Also print REJECT decisions")
    check_parser.add_argument("--source", action="append", default=[], help="Only check this source id (repeatable)")
    subparsers.add_parser("report", help="Watchlist health: zero matches vs adapter failures, plus discovered companies")
    export_parser = subparsers.add_parser("export", help="Write job_matches.csv/json from stored state")
    export_parser.add_argument("--dir", type=Path, help="Output directory (default: EXPORT_DIR)")
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


def _monitor(settings: Settings, store: StateStore, only: tuple[str, ...] = ()) -> Monitor:
    sources = load_sources(settings.sources_path)
    if only:
        unknown = set(only) - {source.id for source in sources}
        if unknown:
            raise ValueError(f"Unknown source id(s): {', '.join(sorted(unknown))}")
        sources = tuple(source for source in sources if source.id in only)
    return Monitor(
        sources=sources,
        client=HttpClient(settings.request_timeout_seconds, host_interval_seconds=settings.host_interval_seconds),
        role_filter=RoleFilter(settings.target_year),
        store=store,
        notifier=_notifier(settings),
        concurrency=settings.request_concurrency,
        alert_on_first_run=settings.alert_on_first_run,
        options=MonitorOptions(
            target_year=settings.target_year,
            graduation_year=settings.graduation_year,
            graduation_month=settings.graduation_month,
            additional_scan_minutes=settings.additional_scan_minutes,
            max_description_fetches=settings.max_description_fetches,
            description_refresh_hours=settings.description_refresh_hours,
            close_after_hours=settings.close_after_hours,
            zero_result_warn_runs=settings.zero_result_warn_runs,
            export_dir=settings.export_dir,
        ),
    )


def _report(settings: Settings, store: StateStore) -> None:
    sources = load_sources(settings.sources_path, include_disabled=True)
    health = {row["source_id"]: row for row in store.status_rows()}
    groups: dict[str, list[str]] = {"must": [], "additional": []}
    for source in sorted(sources, key=lambda item: item.name.casefold()):
        row = health.get(source.id)
        if not source.enabled:
            state = "DISABLED"
        elif row is None:
            state = "NOT SCANNED YET"
        elif row["consecutive_failures"]:
            state = f"ADAPTER FAILING x{row['consecutive_failures']}: {row['last_error'][:120]}"
        elif row["consecutive_empty_runs"] >= settings.zero_result_warn_runs:
            state = f"SUSPECT ADAPTER: zero postings for {row['consecutive_empty_runs']} runs"
        elif row["last_accept_review_count"]:
            state = f"OK, {row['last_accept_review_count']} matching (ACCEPT/REVIEW)"
        else:
            raw = row["last_raw_count"]
            read = f"{raw} postings read" if raw is not None and raw >= 0 else "listing read"
            state = f"OK, zero matching ({read}, {row['last_match_count']} candidates)"
        kind = source.ats or "legacy"
        groups.setdefault(source.tier, []).append(f"  {source.name} [{kind}]: {state}")
    for tier, label in (("must", "MUST-INCLUDE watchlist"), ("additional", "Additional companies")):
        print(f"\n{label}:")
        print("\n".join(groups.get(tier, [])) or "  (none)")
    registry = store.registry_rows()
    print(f"\nDiscovered companies (via aggregators): {len(registry)}")
    for row in registry[:40]:
        print(f"  {row['company']}: {row['relevant_postings']} relevant / {row['postings_seen']} seen, first {row['first_seen']} via {row['discovered_via']}")


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
                print(f"{source.name} [{source.tier}, {source.ats or 'legacy'}]: {source.career_url}")
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
            read_only=arguments.command in {"history", "report", "export"},
        )
        try:
            if arguments.command == "history":
                print(json.dumps([dict(row) for row in store.history_rows(arguments.company)], indent=2, ensure_ascii=False))
                return 0
            if arguments.command == "status":
                counts = store.counts()
                print(
                    f"Tracked postings: {counts['total']} (open: {counts['accept']} accept, {counts['review']} review; "
                    f"{counts['reject']} rejected for audit); pending notifications: {counts['pending']}"
                )
                for row in store.status_rows():
                    state = "OK" if row["consecutive_failures"] == 0 else f"FAIL x{row['consecutive_failures']}"
                    print(
                        f"{state:>8}  {row['source_name']}: candidates={row['last_match_count']} "
                        f"read={row['last_raw_count']} checked={row['last_checked_at']}"
                    )
                return 0
            if arguments.command == "report":
                _report(settings, store)
                return 0
            if arguments.command == "export":
                csv_path, json_path = export_matches(store.open_matches(), arguments.dir or settings.export_dir)
                print(f"Wrote {csv_path} and {json_path}")
                return 0
            monitor = _monitor(settings, store, tuple(getattr(arguments, "source", ()) or ()))
            if arguments.command == "check":
                summary = monitor.run_once(dry_run=True, show_rejects=arguments.all)
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
