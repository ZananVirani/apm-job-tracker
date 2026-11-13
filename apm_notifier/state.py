from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from .classify import ACCEPT, REJECT, REVIEW, Classification
from .models import Job, SourceResult, canonical_url, normalize_space, utc_now


HEALTH_ALERT_AFTER = timedelta(hours=6)
PRIORITY_HEALTH_ALERT_AFTER = timedelta(minutes=15)
HEALTH_ALERT_REPEAT = timedelta(days=7)
NOTIFIABLE = {ACCEPT, REVIEW}


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Alert history from versions before the classifier; used only to avoid repeat alerts.
CREATE TABLE IF NOT EXISTS jobs (
    fingerprint TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    source_is_aggregator INTEGER NOT NULL DEFAULT 0,
    company TEXT NOT NULL,
    company_key TEXT NOT NULL,
    title TEXT NOT NULL,
    req_key TEXT,
    url_key TEXT,
    ctl_key TEXT NOT NULL,
    normalized_title_category TEXT NOT NULL DEFAULT '',
    category_label TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    remote_status TEXT NOT NULL DEFAULT '',
    term_start TEXT NOT NULL DEFAULT '',
    term_length TEXT NOT NULL DEFAULT '',
    posting_url TEXT NOT NULL,
    apply_url TEXT NOT NULL DEFAULT '',
    req_id TEXT NOT NULL DEFAULT '',
    posted_date TEXT NOT NULL DEFAULT '',
    close_date TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    description_text TEXT NOT NULL DEFAULT '',
    description_hash TEXT NOT NULL DEFAULT '',
    description_fetched_at TEXT,
    detail_json TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    matched_quotes TEXT NOT NULL DEFAULT '[]',
    priority_location INTEGER NOT NULL DEFAULT 0,
    location_note TEXT NOT NULL DEFAULT '',
    company_priority TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    pending_event TEXT,
    notified_at TEXT,
    notified_decision TEXT,
    notified_status TEXT
);
CREATE INDEX IF NOT EXISTS postings_req_key ON postings(req_key);
CREATE INDEX IF NOT EXISTS postings_url_key ON postings(url_key);
CREATE INDEX IF NOT EXISTS postings_ctl_key ON postings(ctl_key);
CREATE INDEX IF NOT EXISTS postings_source ON postings(source_id, status);

CREATE TABLE IF NOT EXISTS company_registry (
    company_key TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    discovered_via TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    postings_seen INTEGER NOT NULL DEFAULT 0,
    relevant_postings INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    last_success_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_match_count INTEGER NOT NULL DEFAULT 0,
    health_alerted_at TEXT,
    failure_started_at TEXT
);
"""
HEALTH_COLUMNS = {
    "failure_started_at": "TEXT",
    "last_raw_count": "INTEGER NOT NULL DEFAULT -1",
    "consecutive_empty_runs": "INTEGER NOT NULL DEFAULT 0",
    "last_relevant_at": "TEXT",
    "last_accept_review_count": "INTEGER NOT NULL DEFAULT 0",
}

COMPANY_SUFFIX = re.compile(
    r"\b(?:inc|incorporated|ltd|limited|llc|llp|corp|corporation|co|company|plc|group|holdings|technologies|the)\b"
)


def company_key(company: str) -> str:
    lowered = normalize_space(company).casefold().replace("&", " and ")
    lowered = re.sub(r"[^a-z0-9 ]+", " ", lowered)
    return normalize_space(COMPANY_SUFFIX.sub(" ", lowered)) or lowered


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", normalize_space(value).casefold()).strip()


def posting_keys(job: Job) -> tuple[str | None, str | None, str]:
    company = company_key(job.company)
    req_key = f"{company}|req:{_norm(job.req_id)}" if _norm(job.req_id) else None
    url_key = f"url:{canonical_url(job.url)}" if job.url.startswith("http") else None
    first_location = re.split(r"[;|]", job.location)[0] if job.location else ""
    ctl_key = f"{company}|{_norm(job.title)}|{_norm(first_location)}"
    return req_key, url_key, ctl_key


def description_hash(text: str) -> str:
    return hashlib.sha256(normalize_space(text).encode("utf-8")).hexdigest()[:16] if text else ""


@dataclass(frozen=True)
class CachedDescription:
    text: str
    fetched_at: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class PostingEvent:
    posting_id: int
    event: str


class StateStore:
    def __init__(self, path: Path, read_only: bool = False) -> None:
        if read_only:
            if not path.is_file():
                raise FileNotFoundError(f"Notification history does not exist: {path}")
            self.connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        else:
            if str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self.connection.executescript(SCHEMA)
            self._migrate_schema()
            self.connection.commit()

    def _migrate_schema(self) -> None:
        """Add fields to histories created by older versions."""
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(source_health)").fetchall()}
        for name, definition in HEALTH_COLUMNS.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE source_health ADD COLUMN {name} {definition}")

    def close(self) -> None:
        self.connection.close()

    def is_initialized(self) -> bool:
        row = self.connection.execute("SELECT value FROM meta WHERE key = 'initialized'").fetchone()
        return bool(row and row["value"] == "true")

    def mark_initialized(self) -> None:
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES('initialized', 'true') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        self.connection.commit()

    # --- Postings -------------------------------------------------------------------

    def _find(self, keys: tuple[str | None, str | None, str], claimed: set[int]) -> sqlite3.Row | None:
        req_key, url_key, ctl_key = keys
        for column, value in (("req_key", req_key), ("url_key", url_key)):
            if value:
                row = self.connection.execute(f"SELECT * FROM postings WHERE {column} = ? ORDER BY id LIMIT 1", (value,)).fetchone()
                if row is not None:
                    return row
        # Reposts of the same role usually get a new requisition id and URL but keep the
        # title and location. Distinct live postings with identical titles stay separate
        # because a row can only be claimed once per scan.
        for row in self.connection.execute("SELECT * FROM postings WHERE ctl_key = ? ORDER BY id", (ctl_key,)):
            if row["id"] not in claimed:
                return row
        return None

    def description_cache(self) -> dict[str, CachedDescription]:
        cache: dict[str, CachedDescription] = {}
        rows = self.connection.execute(
            "SELECT req_key, url_key, ctl_key, description_text, description_fetched_at, detail_json "
            "FROM postings WHERE description_fetched_at IS NOT NULL"
        )
        for row in rows:
            entry = CachedDescription(
                text=row["description_text"],
                fetched_at=row["description_fetched_at"],
                detail=json.loads(row["detail_json"] or "{}"),
            )
            for key in (row["req_key"], row["url_key"]):
                if key:
                    cache.setdefault(key, entry)
        return cache

    def legacy_notified(self, job: Job) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM jobs WHERE notified_at IS NOT NULL AND "
            "(url = ? OR (lower(company) = lower(?) AND lower(title) = lower(?))) LIMIT 1",
            (job.url, job.company, job.title),
        ).fetchone()
        if row:
            return True
        canonical = canonical_url(job.url)
        for legacy in self.connection.execute("SELECT url FROM jobs WHERE notified_at IS NOT NULL AND company = ? COLLATE NOCASE", (job.company,)):
            if canonical_url(legacy["url"]) == canonical:
                return True
        return False

    def upsert_posting(
        self,
        job: Job,
        result: Classification,
        *,
        source_name: str,
        aggregator: bool = False,
        company_priority: str = "",
        description_fetched_at: str | None = None,
        detail: dict[str, Any] | None = None,
        claimed: set[int] | None = None,
        now: str | None = None,
    ) -> PostingEvent | None:
        """Insert or update a posting and return the notification event it creates, if any."""
        now = now or utc_now()
        claimed = claimed if claimed is not None else set()
        keys = posting_keys(job)
        existing = self._find(keys, claimed)
        if existing is not None:
            claimed.add(existing["id"])
            if aggregator and not existing["source_is_aggregator"] and existing["source_id"] != job.source_id:
                # A direct employer feed owns this posting; the aggregator only confirms it.
                self.connection.execute("UPDATE postings SET last_seen = ? WHERE id = ?", (now, existing["id"]))
                return None

        fields = {
            "source_id": job.source_id,
            "source_is_aggregator": int(aggregator),
            "company": job.company,
            "company_key": company_key(job.company),
            "title": job.title,
            "req_key": keys[0],
            "url_key": keys[1],
            "ctl_key": keys[2],
            "normalized_title_category": result.category,
            "category_label": result.category_label,
            "location": job.location,
            "remote_status": result.remote_status,
            "term_start": result.term_start,
            "term_length": result.term_length,
            "posting_url": job.url,
            "apply_url": job.apply_url or job.url,
            "req_id": job.req_id,
            "posted_date": job.posted_date[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", job.posted_date) else job.posted_date,
            "close_date": result.close_date,
            "source": source_name,
            "decision": result.decision,
            "reasons": json.dumps(result.reasons, ensure_ascii=False),
            "matched_quotes": json.dumps(result.quotes, ensure_ascii=False),
            "priority_location": int(result.priority_location),
            "location_note": result.location_note,
            "company_priority": company_priority,
            "last_seen": now,
            "status": result.status,
        }
        text = job.full_text
        if description_fetched_at is not None or text:
            fields["description_text"] = text[:20_000]
            fields["description_hash"] = description_hash(text)
        if description_fetched_at is not None:
            fields["description_fetched_at"] = description_fetched_at
            fields["detail_json"] = json.dumps(detail or {}, ensure_ascii=False)

        with self.connection:
            if existing is None:
                fields["first_seen"] = now
                notified = self.legacy_notified(job) and result.decision in NOTIFIABLE
                if notified:
                    fields.update(notified_at=now, notified_decision=result.decision, notified_status=result.status)
                pending = None if notified else self._event_for(None, None, result.decision, result.status)
                fields["pending_event"] = pending
                columns = ", ".join(fields)
                placeholders = ", ".join("?" for _ in fields)
                cursor = self.connection.execute(f"INSERT INTO postings({columns}) VALUES({placeholders})", tuple(fields.values()))
                posting_id = int(cursor.lastrowid)
                claimed.add(posting_id)
            else:
                posting_id = int(existing["id"])
                # Keep the identifiers the posting was first stored under so every later
                # sighting (by requisition id, original URL or title+location) still matches.
                for column in ("req_key", "url_key", "ctl_key"):
                    if existing[column]:
                        fields[column] = existing[column]
                pending = self._event_for(existing["notified_decision"], existing["notified_status"], result.decision, result.status)
                if pending is None and existing["pending_event"] and result.decision in NOTIFIABLE and result.status == "open":
                    pending = existing["pending_event"]  # Keep an undelivered alert pending.
                fields["pending_event"] = pending
                assignments = ", ".join(f"{column} = ?" for column in fields)
                self.connection.execute(f"UPDATE postings SET {assignments} WHERE id = ?", (*fields.values(), posting_id))
        return PostingEvent(posting_id, pending) if pending else None

    @staticmethod
    def _event_for(notified_decision: str | None, notified_status: str | None, decision: str, status: str) -> str | None:
        was_notified = notified_decision in NOTIFIABLE
        if decision in NOTIFIABLE and status == "open":
            if not was_notified:
                return "new"
            if notified_status == "closed":
                return "reopened"
            if notified_decision != decision:
                return f"changed: {notified_decision} -> {decision}"
            return None
        if not was_notified:
            return None
        if status == "closed" and notified_status != "closed":
            return "closed"
        if decision == REJECT and notified_decision != REJECT:
            return f"changed: {notified_decision} -> REJECT"
        return None

    def close_missing(self, source_id: str, cutoff: str, now: str | None = None) -> int:
        """Close open postings from a healthy source that have not been seen since ``cutoff``."""
        now = now or utc_now()
        rows = self.connection.execute(
            "SELECT * FROM postings WHERE source_id = ? AND status = 'open' AND last_seen < ?",
            (source_id, cutoff),
        ).fetchall()
        with self.connection:
            for row in rows:
                pending = self._event_for(row["notified_decision"], row["notified_status"], row["decision"], "closed")
                self.connection.execute(
                    "UPDATE postings SET status = 'closed', pending_event = ? WHERE id = ?",
                    (pending, row["id"]),
                )
        return len(rows)

    def pending_postings(self) -> tuple[sqlite3.Row, ...]:
        return tuple(self.connection.execute("SELECT * FROM postings WHERE pending_event IS NOT NULL").fetchall())

    def mark_notified(self, posting_ids: list[int], now: str | None = None) -> None:
        now = now or utc_now()
        with self.connection:
            for posting_id in posting_ids:
                self.connection.execute(
                    "UPDATE postings SET notified_at = ?, notified_decision = decision, notified_status = status, "
                    "pending_event = NULL WHERE id = ?",
                    (now, posting_id),
                )

    def silence_pending(self) -> None:
        pending = [row["id"] for row in self.pending_postings()]
        self.mark_notified(pending)

    def open_matches(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                "SELECT * FROM postings WHERE status = 'open' AND decision IN ('ACCEPT', 'REVIEW') "
                "ORDER BY decision, company, title"
            ).fetchall()
        )

    def postings(self, decision: str = "", company: str = "") -> tuple[sqlite3.Row, ...]:
        query = "SELECT * FROM postings WHERE 1 = 1"
        parameters: list[str] = []
        if decision:
            query += " AND decision = ?"
            parameters.append(decision.upper())
        if company:
            query += " AND company = ? COLLATE NOCASE"
            parameters.append(company)
        query += " ORDER BY last_seen DESC, company, title"
        return tuple(self.connection.execute(query, parameters).fetchall())

    # --- Company registry -------------------------------------------------------------

    def register_company(self, company: str, discovered_via: str, relevant: bool, now: str | None = None) -> bool:
        """Record an employer discovered via an aggregator; return True if it is new."""
        now = now or utc_now()
        key = company_key(company)
        existing = self.connection.execute("SELECT 1 FROM company_registry WHERE company_key = ?", (key,)).fetchone()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO company_registry(company_key, company, discovered_via, first_seen, last_seen, postings_seen, relevant_postings)
                VALUES(?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(company_key) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    postings_seen = company_registry.postings_seen + 1,
                    relevant_postings = company_registry.relevant_postings + excluded.relevant_postings
                """,
                (key, company, discovered_via, now[:10], now[:10], int(relevant)),
            )
        return existing is None

    def registry_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                "SELECT * FROM company_registry ORDER BY relevant_postings DESC, postings_seen DESC, company"
            ).fetchall()
        )

    # --- Source health ----------------------------------------------------------------

    def source_last_checked(self) -> dict[str, str]:
        return {
            row["source_id"]: row["last_checked_at"]
            for row in self.connection.execute("SELECT source_id, last_checked_at FROM source_health")
        }

    def record_source_result(self, result: SourceResult, accept_review: int = 0) -> bool:
        """Persist health and return True when a throttled health alert is due."""
        now = utc_now()
        previous = self.connection.execute(
            "SELECT consecutive_failures, health_alerted_at, failure_started_at, consecutive_empty_runs, last_relevant_at "
            "FROM source_health WHERE source_id = ?",
            (result.source.id,),
        ).fetchone()
        prior_failures = int(previous["consecutive_failures"]) if previous else 0
        previous_alert = previous["health_alerted_at"] if previous else None
        previous_failure_start = previous["failure_started_at"] if previous else None
        empty_runs = int(previous["consecutive_empty_runs"]) if previous else 0
        last_relevant = previous["last_relevant_at"] if previous else None

        if result.succeeded:
            failures = 0
            last_success = now
            last_error = ""
            failure_started_at = None
            previous_alert = None
            empty_runs = empty_runs + 1 if result.raw_count == 0 else 0
        else:
            failures = prior_failures + 1
            last_success = None
            last_error = "; ".join(result.errors)[:1000] or "unknown source failure"
            failure_started_at = previous_failure_start or now
        if accept_review:
            last_relevant = now

        should_alert = (
            not result.succeeded
            and self._outage_is_long_enough(
                failure_started_at,
                now,
                PRIORITY_HEALTH_ALERT_AFTER if result.source.priority else HEALTH_ALERT_AFTER,
            )
            and self._alert_is_due(previous_alert, now)
        )
        alerted_at = now if should_alert else previous_alert
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO source_health(
                    source_id, source_name, last_checked_at, last_success_at,
                    consecutive_failures, last_error, last_match_count, health_alerted_at,
                    failure_started_at, last_raw_count, consecutive_empty_runs, last_relevant_at,
                    last_accept_review_count
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_name = excluded.source_name,
                    last_checked_at = excluded.last_checked_at,
                    last_success_at = COALESCE(excluded.last_success_at, source_health.last_success_at),
                    consecutive_failures = excluded.consecutive_failures,
                    last_error = excluded.last_error,
                    last_match_count = excluded.last_match_count,
                    health_alerted_at = excluded.health_alerted_at,
                    failure_started_at = excluded.failure_started_at,
                    last_raw_count = CASE WHEN excluded.consecutive_failures = 0 THEN excluded.last_raw_count ELSE source_health.last_raw_count END,
                    consecutive_empty_runs = excluded.consecutive_empty_runs,
                    last_relevant_at = excluded.last_relevant_at,
                    last_accept_review_count = CASE WHEN excluded.consecutive_failures = 0 THEN excluded.last_accept_review_count ELSE source_health.last_accept_review_count END
                """,
                (
                    result.source.id,
                    result.source.name,
                    now,
                    last_success,
                    failures,
                    last_error,
                    len(result.jobs),
                    alerted_at,
                    failure_started_at,
                    result.raw_count,
                    empty_runs,
                    last_relevant,
                    accept_review,
                ),
            )
        return should_alert

    @staticmethod
    def _outage_is_long_enough(
        failure_started_at: str, now: str, threshold: timedelta = HEALTH_ALERT_AFTER,
    ) -> bool:
        started = datetime.fromisoformat(failure_started_at)
        checked = datetime.fromisoformat(now)
        return checked - started >= threshold

    @staticmethod
    def _alert_is_due(previous_alert: str | None, now: str) -> bool:
        if not previous_alert:
            return True
        then = datetime.fromisoformat(previous_alert)
        checked = datetime.fromisoformat(now)
        return checked - then >= HEALTH_ALERT_REPEAT

    def status_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.connection.execute(
                """
                SELECT source_id, source_name, last_checked_at, last_success_at,
                       consecutive_failures, last_match_count, last_error, last_raw_count,
                       consecutive_empty_runs, last_relevant_at, last_accept_review_count
                FROM source_health
                ORDER BY consecutive_failures DESC, source_name
                """
            ).fetchall()
        )

    def history_rows(self, company: str = "") -> tuple[sqlite3.Row, ...]:
        """Return stored postings with discovery and notification timestamps."""
        query = """
            SELECT title, posting_url AS url, company, decision, status,
                   first_seen AS first_seen_at, last_seen AS last_seen_at, notified_at
            FROM postings
        """
        parameters: tuple[str, ...] = ()
        if company:
            query += " WHERE company = ? COLLATE NOCASE"
            parameters = (company,)
        query += " ORDER BY first_seen DESC, company COLLATE NOCASE, title, posting_url"
        return tuple(self.connection.execute(query, parameters).fetchall())

    def counts(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN pending_event IS NOT NULL THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN decision = 'ACCEPT' AND status = 'open' THEN 1 ELSE 0 END) AS accept,
                   SUM(CASE WHEN decision = 'REVIEW' AND status = 'open' THEN 1 ELSE 0 END) AS review,
                   SUM(CASE WHEN decision = 'REJECT' THEN 1 ELSE 0 END) AS reject
            FROM postings
            """
        ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "pending", "accept", "review", "reject")}


def hours_ago(hours: int, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=hours)).replace(microsecond=0).isoformat()


__all__ = ["CachedDescription", "PostingEvent", "StateStore", "company_key", "description_hash", "hours_ago", "posting_keys"]
