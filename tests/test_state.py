from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from apm_notifier.classify import classify
from apm_notifier.models import Job, Source, SourceResult
from apm_notifier.state import StateStore


TODAY = date(2026, 11, 12)
PM_TEXT = "Own the roadmap, write PRDs, prioritize the backlog with engineering and design. Returning students welcome."


def record(store, job, **kwargs):
    return store.upsert_posting(job, classify(job, TODAY), source_name=job.company, **kwargs)


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = StateStore(self.path)

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()

    def test_dedupes_by_requisition_then_url_then_title_location(self) -> None:
        base = Job("a", "Example Inc.", "Product Manager Intern", "https://example.com/jobs/1?utm_source=x", "Toronto, ON", description=PM_TEXT, req_id="R-1")
        same_req = Job("a", "Example", "Product Manager Intern (Summer 2027)", "https://example.com/other", "Toronto, ON", description=PM_TEXT, req_id="r-1")
        same_url = Job("b", "Example", "PM Intern", "https://example.com/jobs/1", "Toronto, Ontario", description=PM_TEXT)
        first = record(self.store, base)
        self.assertEqual(first.event, "new")
        record(self.store, same_req)
        record(self.store, same_url)
        self.assertEqual(self.store.counts()["total"], 1)
        self.assertEqual(len(self.store.pending_postings()), 1)

    def test_repost_with_new_id_is_a_new_posting_and_alerts_once(self) -> None:
        original = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/1", "Toronto, ON", description=PM_TEXT, req_id="R-1")
        repost = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/9", "Toronto, ON", description=PM_TEXT, req_id="R-9")
        record(self.store, original)
        self.store.mark_notified([row["id"] for row in self.store.pending_postings()])
        # Same company, title and location but a new requisition: a new posting, alerted.
        self.assertEqual(record(self.store, repost, claimed=set()).event, "new")
        self.assertEqual(self.store.counts()["total"], 2)
        self.store.mark_notified([row["id"] for row in self.store.pending_postings()])
        # Later sightings of either posting never alert again.
        self.assertIsNone(record(self.store, original, claimed=set()))
        self.assertIsNone(record(self.store, repost, claimed=set()))

    def test_each_posting_alerts_once_whatever_its_later_changes(self) -> None:
        review = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/1", "New York, NY", description=PM_TEXT, req_id="R-1")
        accept = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/1", "Toronto, ON", description=PM_TEXT, req_id="R-1")
        reject = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/1", "London, UK", description=PM_TEXT, req_id="R-1")
        self.assertEqual(record(self.store, review).event, "new")
        self.store.mark_notified([row["id"] for row in self.store.pending_postings()])
        for job in (accept, reject, review, accept):
            self.assertIsNone(record(self.store, job, claimed=set()))
        self.store.close_missing("a", "9999-01-01T00:00:00+00:00")
        self.assertIsNone(record(self.store, accept, claimed=set()))  # Reopened: still silent.
        self.assertEqual(self.store.pending_postings(), ())

    def test_undelivered_alert_survives_a_rejection_round_trip_only_as_new(self) -> None:
        review = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/1", "New York, NY", description=PM_TEXT, req_id="R-1")
        reject = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/1", "London, UK", description=PM_TEXT, req_id="R-1")
        record(self.store, review)
        self.assertIsNone(record(self.store, reject, claimed=set()))  # Never delivered, now rejected.
        self.assertEqual(self.store.pending_postings(), ())
        self.assertEqual(record(self.store, review, claimed=set()).event, "new")

    def test_title_location_only_links_copies_without_a_shared_identifier(self) -> None:
        direct = Job("a", "Example", "Product Manager Intern", "https://example.com/jobs/1", "Toronto, ON", description=PM_TEXT, req_id="R-1")
        copy = Job("agg", "Example", "Product Manager Intern", "https://aggregator.example/x/77", "Toronto, ON", description=PM_TEXT)
        record(self.store, direct)
        self.assertIsNone(record(self.store, copy, aggregator=True, claimed=set()))
        self.assertEqual(self.store.counts()["total"], 1)
        # An id-less posting whose page URL changes between scans stays one posting while open.
        first = Job("b", "Other", "Product Analyst Intern", "https://other.example/search?session=1", "Toronto, ON", description=PM_TEXT)
        moved = Job("b", "Other", "Product Analyst Intern", "https://other.example/search?session=2", "Toronto, ON", description=PM_TEXT)
        self.assertEqual(record(self.store, first).event, "new")
        self.store.mark_notified([row["id"] for row in self.store.pending_postings()])
        self.assertIsNone(record(self.store, moved, claimed=set()))
        self.assertEqual(self.store.counts()["total"], 2)

    def test_migration_drops_queued_status_changes_and_rekeys_merged_rows(self) -> None:
        now = "2026-11-01T00:00:00+00:00"
        columns = "source_id, company, company_key, title, req_key, url_key, ctl_key, posting_url, decision, first_seen, last_seen"
        with self.store.connection:
            self.store.connection.execute(
                f"INSERT INTO postings({columns}, pending_event, notified_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("walmart", "Walmart", "walmart", "Corporate Intern", None, "url:https://careers.walmart.com/us/en/results?searchQuery=intern",
                 "walmart|corporate intern|", "https://careers.walmart.com/us/en/results?searchQuery=intern#R-2697381",
                 "REVIEW", now, now, "changed: ACCEPT -> REVIEW", now),
            )
        self.store.close()
        self.store = StateStore(self.path)
        row = self.store.connection.execute("SELECT * FROM postings").fetchone()
        self.assertIsNone(row["pending_event"])
        self.assertEqual(row["req_key"], "walmart|req:r 2697381")
        later = Job("walmart", "Walmart", "Corporate Intern", "https://careers.walmart.com/us/en/jobs/R-2697381", "Bentonville, AR", req_id="R-2697381")
        record(self.store, later)
        self.assertEqual(self.store.counts()["total"], 1)

    def test_rejects_are_kept_for_audit_without_alerting(self) -> None:
        job = Job("a", "Example", "Software Engineer Intern", "https://example.com/jobs/2", "Toronto, ON")
        self.assertIsNone(record(self.store, job))
        row = self.store.postings("REJECT")[0]
        self.assertIn("Not a product role", row["reasons"])
        self.assertEqual(self.store.open_matches(), ())

    def test_legacy_alert_history_suppresses_repeat_alerts(self) -> None:
        self.store.connection.execute(
            "INSERT INTO jobs VALUES('f', 'tiktok', 'TikTok', 'Product Manager Intern', 'https://example.com/jobs/7', '', 'x', 'x', '2026-10-01')"
        )
        job = Job("tiktok", "TikTok", "Product Manager Intern - Summer 2027", "https://example.com/jobs/7?utm_source=a", "Toronto, ON", description=PM_TEXT)
        self.assertIsNone(record(self.store, job))
        self.assertEqual(self.store.counts()["pending"], 0)

    def test_history_filters_exact_company_ignoring_case(self) -> None:
        with patch("apm_notifier.state.utc_now", return_value="2026-10-21T10:00:00+00:00"):
            record(self.store, Job("t", "TikTok", "Product Manager Intern", "https://example.com/jobs/1", "Toronto, ON", description=PM_TEXT))
            record(self.store, Job("o", "TikTok Shop", "Product Intern", "https://example.com/jobs/3", "Toronto, ON", description=PM_TEXT))
        history = [dict(row) for row in self.store.history_rows("TIKTOK")]
        self.assertEqual([row["url"] for row in history], ["https://example.com/jobs/1"])
        self.assertEqual(history[0]["first_seen_at"], "2026-10-21T10:00:00+00:00")
        self.assertEqual(self.store.history_rows("TikTok' OR 1=1 --"), ())

    def test_read_only_history_cannot_mutate_the_database(self) -> None:
        record(self.store, Job("a", "Example", "APM Intern", "https://example.com/jobs/1", "Toronto, ON", description=PM_TEXT))
        self.store.close()
        before = self.path.read_bytes()
        self.store = StateStore(self.path, read_only=True)
        self.assertEqual(len(self.store.history_rows()), 1)
        with self.assertRaises(sqlite3.OperationalError):
            self.store.silence_pending()
        self.assertEqual(self.path.read_bytes(), before)

    def test_registry_records_new_companies_once(self) -> None:
        self.assertTrue(self.store.register_company("Acme AI", "Simplify", True, "2026-11-12T00:00:00+00:00"))
        self.assertFalse(self.store.register_company("Acme AI Inc.", "Simplify", False, "2026-11-13T00:00:00+00:00"))
        row = self.store.registry_rows()[0]
        self.assertEqual((row["postings_seen"], row["relevant_postings"], row["first_seen"]), (2, 1, "2026-11-12"))

    def test_zero_postings_streak_is_tracked(self) -> None:
        source = Source("x", "X", ("https://example.com",), "https://example.com")
        for _ in range(3):
            self.store.record_source_result(SourceResult(source, (), 1, raw_count=0))
        self.assertEqual(self.store.status_rows()[0]["consecutive_empty_runs"], 3)
        self.store.record_source_result(SourceResult(source, (), 1, raw_count=5))
        self.assertEqual(self.store.status_rows()[0]["consecutive_empty_runs"], 0)

    def test_priority_health_alert_waits_fifteen_minutes_and_repeats_weekly(self) -> None:
        source = Source("tiktok", "TikTok", ("https://example.com/jobs",), "https://example.com", priority=True)
        failure = SourceResult(source, (), 0, ("unavailable",))
        store = self.store
        if True:
            if True:
                for when, expected in (
                    ("2026-10-09T00:00:00+00:00", False),
                    ("2026-10-09T00:14:59+00:00", False),
                    ("2026-10-09T00:15:00+00:00", True),
                    ("2026-10-09T00:30:00+00:00", False),
                    ("2026-10-16T00:15:00+00:00", True),
                ):
                    with self.subTest(when=when), patch("apm_notifier.state.utc_now", return_value=when):
                        self.assertEqual(store.record_source_result(failure), expected)

    def test_health_alert_waits_for_six_hour_outage_and_resets_on_recovery(self) -> None:
        source = Source(
            id="example",
            name="Example",
            urls=("https://example.com/jobs",),
            career_url="https://example.com/careers",
        )
        failure = SourceResult(
            source=source,
            jobs=(),
            fetched_urls=0,
            errors=("HTTP Error 503: Service Temporarily Unavailable",),
        )
        success = SourceResult(source=source, jobs=(), fetched_urls=1)
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                with patch("apm_notifier.state.utc_now", return_value="2026-08-15T00:00:00+00:00"):
                    self.assertFalse(store.record_source_result(failure))
                with patch("apm_notifier.state.utc_now", return_value="2026-08-15T05:59:00+00:00"):
                    self.assertFalse(store.record_source_result(failure))
                with patch("apm_notifier.state.utc_now", return_value="2026-08-15T06:00:00+00:00"):
                    self.assertTrue(store.record_source_result(failure))
                with patch("apm_notifier.state.utc_now", return_value="2026-08-16T06:00:00+00:00"):
                    self.assertFalse(store.record_source_result(failure))
                with patch("apm_notifier.state.utc_now", return_value="2026-08-16T07:00:00+00:00"):
                    self.assertFalse(store.record_source_result(success))
                with patch("apm_notifier.state.utc_now", return_value="2026-08-16T08:00:00+00:00"):
                    self.assertFalse(store.record_source_result(failure))
            finally:
                store.close()

    def test_migrates_existing_source_health_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE source_health (
                    source_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    last_success_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_match_count INTEGER NOT NULL DEFAULT 0,
                    health_alerted_at TEXT
                )
                """
            )
            connection.commit()
            connection.close()

            store = StateStore(path)
            try:
                columns = {
                    row["name"]
                    for row in store.connection.execute(
                        "PRAGMA table_info(source_health)"
                    ).fetchall()
                }
                self.assertTrue({"failure_started_at", "consecutive_empty_runs", "last_raw_count"} <= columns)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
