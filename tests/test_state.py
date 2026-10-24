from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from apm_notifier.models import Job, Source, SourceResult
from apm_notifier.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_history_filters_exact_company_ignoring_case_and_keeps_recorded_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                first = Job("tiktok", "TikTok", "Product Manager Graduate", "https://example.com/jobs/1")
                second = Job("tiktok", "tiktok", "Marketing Graduate", "https://example.com/jobs/2")
                other = Job("other", "TikTok Shop", "Product Graduate", "https://example.com/jobs/3")
                with patch("apm_notifier.state.utc_now", return_value="2026-10-21T10:00:00+00:00"):
                    store.record_jobs((first, other))
                with patch("apm_notifier.state.utc_now", return_value="2026-10-21T10:01:00+00:00"):
                    store.mark_notified(first)
                with patch("apm_notifier.state.utc_now", return_value="2026-10-23T10:00:00+00:00"):
                    store.record_jobs((first, second))

                history = [dict(row) for row in store.history_rows("TIKTOK")]
                self.assertEqual([row["url"] for row in history], [second.url, first.url])
                self.assertEqual(history[0]["notified_at"], None)
                self.assertEqual(history[1], {
                    "title": first.title,
                    "url": first.url,
                    "company": "TikTok",
                    "first_seen_at": "2026-10-21T10:00:00+00:00",
                    "last_seen_at": "2026-10-23T10:00:00+00:00",
                    "notified_at": "2026-10-21T10:01:00+00:00",
                })
                self.assertEqual(len(store.history_rows()), 3)
                self.assertEqual(store.history_rows("Tik"), ())
                self.assertEqual(store.history_rows("TikTok' OR 1=1 --"), ())
            finally:
                store.close()

    def test_read_only_history_cannot_mutate_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = StateStore(path)
            store.record_jobs((Job("example", "Example", "APM", "https://example.com/jobs/1"),))
            store.close()
            before = path.read_bytes()
            store = StateStore(path, read_only=True)
            try:
                self.assertEqual(len(store.history_rows()), 1)
                with self.assertRaises(sqlite3.OperationalError):
                    store.silence_pending()
            finally:
                store.close()
            self.assertEqual(path.read_bytes(), before)

    def test_same_posting_with_changed_title_keeps_delivery_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                original = Job("tiktok", "TikTok", "Product Manager Graduate - 2027 Start", "https://example.com/jobs/1")
                variant = Job("community-backcheck", " TikTok ", "Product Manager Graduate", "https://example.com/jobs/1?utm_source=tracker", "London")
                store.record_jobs((original,))
                store.mark_notified(original)
                store.record_jobs((variant,))
                self.assertEqual(store.pending_jobs(), ())
                self.assertEqual(store.job_counts(), (1, 0))
            finally:
                store.close()

    def test_priority_health_alert_waits_fifteen_minutes_and_repeats_weekly(self) -> None:
        source = Source("tiktok", "TikTok", ("https://example.com/jobs",), "https://example.com", priority=True)
        failure = SourceResult(source, (), 0, ("unavailable",))
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                for when, expected in (
                    ("2026-10-09T00:00:00+00:00", False),
                    ("2026-10-09T00:14:59+00:00", False),
                    ("2026-10-09T00:15:00+00:00", True),
                    ("2026-10-09T00:30:00+00:00", False),
                    ("2026-10-16T00:15:00+00:00", True),
                ):
                    with self.subTest(when=when), patch("apm_notifier.state.utc_now", return_value=when):
                        self.assertEqual(store.record_source_result(failure), expected)
            finally:
                store.close()

    def test_deduplicates_and_preserves_pending_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            try:
                job = Job(
                    source_id="example",
                    company="Example",
                    title="Product Manager Intern 2027",
                    url="https://example.com/jobs/1?utm_source=test",
                )
                duplicate = Job(
                    source_id="example",
                    company="Example",
                    title="Product Manager Intern 2027",
                    url="https://example.com/jobs/1",
                )
                store.record_jobs((job, duplicate))
                self.assertEqual(len(store.pending_jobs()), 1)
                store.mark_notified(job)
                self.assertEqual(store.pending_jobs(), ())
                store.record_jobs((duplicate,))
                self.assertEqual(store.pending_jobs(), ())
            finally:
                store.close()

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
                self.assertIn("failure_started_at", columns)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
