from __future__ import annotations

from dataclasses import dataclass
from html import escape
import json
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

from .fetch import trusted_ssl_context
from .models import SourceResult


def _health_error_summary(errors: tuple[str, ...]) -> str:
    summaries: list[str] = []
    for error in errors:
        http_error = re.search(r"HTTP Error (\d+): ([^;]+)", error)
        if http_error:
            summary = f"HTTP {http_error.group(1)}: {http_error.group(2).strip()}"
        elif "browser render" in error.casefold() and "timed out" in error.casefold():
            summary = "Browser rendering timed out after retries"
        elif "timed out" in error.casefold():
            summary = "Network request timed out after retries"
        elif "response contained no job records" in error.casefold():
            summary = "Career page returned no readable job data"
        else:
            summary = re.sub(r"^https?://\S+\s*", "Career feed: ", error).strip()
        if summary and summary not in summaries:
            summaries.append(summary)
    return "; ".join(summaries)[:400] or "Unknown career-feed error"


@dataclass(frozen=True)
class DigestEntry:
    posting_id: int
    company: str
    title: str
    location: str
    term: str
    decision: str
    reason: str
    url: str
    event: str = "new"
    must_include: bool = False
    core_title: bool = False
    gta: bool = False
    newest: str = ""

    @property
    def tags(self) -> list[str]:
        tags: list[str] = []
        if self.must_include:
            tags.append("MUST-INCLUDE")
        if self.core_title:
            tags.append("core title")
        if self.gta:
            tags.append("GTA")
        return tags


DECISION_ICONS = {"ACCEPT": "✅", "REVIEW": "🟡", "REJECT": "⛔"}
TELEGRAM_LIMIT = 3800


def sort_digest(entries: list[DigestEntry]) -> list[DigestEntry]:
    """Order by must-include company > core title > GTA > newest."""
    newest_first = sorted(entries, key=lambda entry: entry.newest, reverse=True)
    return sorted(newest_first, key=lambda entry: (not entry.must_include, not entry.core_title, not entry.gta))


def format_entry_html(entry: DigestEntry) -> str:
    event = "" if entry.event == "new" else f"🔁 <i>{escape(entry.event)}</i>\n"
    tags = f" · {escape(' · '.join(entry.tags))}" if entry.tags else ""
    details = " · ".join(filter(None, (entry.location, entry.term)))
    return (
        f"{event}{DECISION_ICONS.get(entry.decision, '')} <b>{escape(entry.decision)}</b>{tags}\n"
        f"<b>{escape(entry.company)}</b> — {escape(entry.title)}\n"
        + (f"📍 {escape(details)}\n" if details else "")
        + (f"💬 {escape(entry.reason)}\n" if entry.reason else "")
        + f'<a href="{escape(entry.url, quote=True)}">Apply</a>'
    )


def format_entry_plain(entry: DigestEntry) -> str:
    event = "" if entry.event == "new" else f"[{entry.event}] "
    tags = f" [{', '.join(entry.tags)}]" if entry.tags else ""
    details = " | ".join(filter(None, (entry.location, entry.term)))
    return f"{event}{entry.decision}{tags}: {entry.company} - {entry.title}\n{details}\n{entry.reason}\n{entry.url}"


def chunk_messages(header: str, blocks: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current and current != header:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


@dataclass(frozen=True)
class NotificationOutcome:
    delivered: bool
    errors: tuple[str, ...] = ()


class NotificationManager:
    def __init__(
        self,
        telegram_bot_token: str,
        telegram_chat_id: str,
        ntfy_topic: str,
        ntfy_server: str,
        timeout_seconds: int,
    ) -> None:
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.ntfy_topic = ntfy_topic
        self.ntfy_server = ntfy_server
        self.timeout_seconds = timeout_seconds
        self.ssl_context = trusted_ssl_context()

    @property
    def configured_channels(self) -> tuple[str, ...]:
        channels: list[str] = []
        if self.telegram_bot_token and self.telegram_chat_id:
            channels.append("Telegram")
        if self.ntfy_topic:
            channels.append("ntfy")
        return tuple(channels)

    def send_digest(self, entries: list[DigestEntry]) -> NotificationOutcome:
        """Send one digest (split across messages when long); delivered if every part arrives."""
        ordered = sort_digest(entries)
        accepted = sum(entry.decision == "ACCEPT" for entry in ordered)
        reviews = sum(entry.decision == "REVIEW" for entry in ordered)
        changes = sum(entry.event != "new" for entry in ordered)
        summary = f"{accepted} accept, {reviews} review" + (f", {changes} status change(s)" if changes else "")
        header = f"🚨 <b>Internship digest</b> — {escape(summary)}"
        telegram_messages = chunk_messages(header, [format_entry_html(entry) for entry in ordered])
        plain_messages = chunk_messages(f"Internship digest: {summary}", [format_entry_plain(entry) for entry in ordered])
        errors: list[str] = []
        delivered = False
        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                for message in telegram_messages:
                    self._send_telegram(message)
                delivered = True
            except Exception as error:
                errors.append(f"Telegram: {error}")
        if self.ntfy_topic:
            try:
                for message in plain_messages:
                    self._send_ntfy(message, ordered[0].url if ordered else "", "Internship digest")
                delivered = True
            except Exception as error:
                errors.append(f"ntfy: {error}")
        return NotificationOutcome(delivered=delivered, errors=tuple(errors))

    def send_health(self, result: SourceResult) -> NotificationOutcome:
        detail = _health_error_summary(result.errors)
        duration = "15 minutes" if result.source.priority else "6 hours"
        telegram_text = (
            "⚠️ <b>APM Notifier coverage degraded</b>\n\n"
            f"<b>{escape(result.source.name)}</b> could not be fully checked for at least {duration}.\n"
            "Retries are continuing automatically. Check the career page for new roles while coverage recovers.\n\n"
            f"Latest error: {escape(detail)}\n\n"
            f'<a href="{escape(result.source.career_url, quote=True)}">Open career page</a>'
        )
        plain_text = (
            f"{result.source.name} could not be fully checked for at least {duration}.\n"
            "Retries continue automatically. Check the career page while coverage recovers.\n"
            f"Latest error: {detail}"
        )
        return self._send_all(
            telegram_text,
            plain_text,
            result.source.career_url,
            "APM Notifier source warning",
        )

    def send_test(self) -> NotificationOutcome:
        return self.send_digest(
            [
                DigestEntry(
                    posting_id=0,
                    company="Example Tech Company",
                    title="Associate Product Manager Intern — Summer 2027",
                    location="Toronto, ON",
                    term="Summer 2027 · 4 months",
                    decision="ACCEPT",
                    reason="Core PM title; Canada (GTA); open to returning students",
                    url="https://example.com/apply",
                    must_include=True,
                    core_title=True,
                    gta=True,
                )
            ]
        )

    def _send_all(self, telegram_text: str, plain_text: str, click_url: str, title: str) -> NotificationOutcome:
        errors: list[str] = []
        delivered = False
        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                self._send_telegram(telegram_text)
                delivered = True
            except Exception as error:  # Channel failures must not stop other channels.
                errors.append(f"Telegram: {error}")
        if self.ntfy_topic:
            try:
                self._send_ntfy(plain_text, click_url, title)
                delivered = True
            except Exception as error:
                errors.append(f"ntfy: {error}")
        return NotificationOutcome(delivered=delivered, errors=tuple(errors))

    def _send_telegram(self, message: str) -> None:
        endpoint = f"https://api.telegram.org/bot{quote(self.telegram_bot_token, safe=':')}/sendMessage"
        body = json.dumps(
            {
                "chat_id": self.telegram_chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        request = Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "Telegram rejected the message"))

    def _send_ntfy(self, message: str, click_url: str, title: str) -> None:
        endpoint = f"{self.ntfy_server}/{quote(self.ntfy_topic, safe='')}"
        request = Request(
            endpoint,
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("ascii", errors="ignore").decode("ascii") or "APM Notifier",
                "Priority": "4",
                "Tags": "rotating_light,briefcase",
                "Click": click_url,
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds, context=self.ssl_context) as response:
            if response.status >= 300:
                raise RuntimeError(f"ntfy returned HTTP {response.status}")
