from datetime import date
from pathlib import Path
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from apm_notifier.classify import ACCEPT, REVIEW
from apm_notifier.filtering import RoleFilter
from apm_notifier.models import FetchResult, Job, Source, SourceResult
from apm_notifier.monitor import Monitor, MonitorOptions, ProcessedJob, SourceOutcome
from apm_notifier.notify import NotificationOutcome
from apm_notifier.state import StateStore


TODAY = date(2026, 11, 12)
PM_TEXT = (
    "Own the roadmap for a feature area, write PRDs and user stories, prioritize the backlog, "
    "and partner with engineering and design on discovery. Open to students returning to school "
    "after the internship. Must be legally eligible to work in Canada."
)


def json_jobs(*jobs: dict) -> str:
    return json.dumps({"jobs": list(jobs)})


def listing(identifier: str, title: str = "Product Manager Intern - Summer 2027", location: str = "Toronto, ON", description: str = PM_TEXT) -> dict:
    return {
        "title": title,
        "url": f"https://jobs.example.com/jobs/{identifier}",
        "location": location,
        "description": description,
        "requisitionId": f"R-{identifier}",
    }


class StaticClient:
    timeout_seconds = 5

    def __init__(self, text: str, content_type: str = "application/json"):
        self.text = text
        self.content_type = content_type
        self.calls = 0

    def fetch(self, url, extra_headers=None, method="GET", body=""):
        self.calls += 1
        return FetchResult(url, url, self.content_type, self.text)


class FakeNotifier:
    configured_channels = ("fake",)

    def __init__(self, deliver: bool = True):
        self.digests: list[list] = []
        self.deliver = deliver

    def send_digest(self, entries):
        self.digests.append(list(entries))
        return NotificationOutcome(delivered=self.deliver, errors=() if self.deliver else ("unavailable",))

    def send_health(self, result):
        return NotificationOutcome(delivered=True)


def source(identifier: str = "example", name: str = "Example", **extra) -> Source:
    return Source(identifier, name, (f"https://jobs.example.com/{identifier}",), "https://jobs.example.com", **extra)


def monitor_for(sources, client, store, notifier, alert_on_first_run=True, **options) -> Monitor:
    options.setdefault("additional_scan_minutes", 0)
    return Monitor(
        tuple(sources), client, RoleFilter(2027), store, notifier, 1, alert_on_first_run,
        MonitorOptions(today=TODAY, **options),
    )


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.directory.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.directory.cleanup()


class DeliveryTests(StoreCase):
    def test_new_matches_are_digested_once(self) -> None:
        client = StaticClient(json_jobs(listing("1"), listing("2", title="Software Engineer Intern - Summer 2027")))
        notifier = FakeNotifier()
        monitor = monitor_for([source(tier="must")], client, self.store, notifier)
        first = monitor.run_once()
        second = monitor.run_once()
        self.assertEqual(first.alerts_delivered, 1)
        self.assertEqual(second.alerts_delivered, 0)
        self.assertEqual(len(notifier.digests), 1)
        entry = notifier.digests[0][0]
        self.assertEqual(entry.decision, ACCEPT)
        self.assertTrue(entry.must_include and entry.core_title and entry.gta)
        # The engineering intern is stored as an auditable reject, never alerted.
        self.assertEqual(self.store.counts()["reject"], 1)

    def test_different_postings_with_the_same_title_both_alert(self) -> None:
        client = StaticClient(json_jobs(listing("1"), listing("2")))
        notifier = FakeNotifier()
        monitor = monitor_for([source()], client, self.store, notifier)
        self.assertEqual(monitor.run_once().alerts_delivered, 2)
        self.assertEqual(monitor.run_once().alerts_delivered, 0)

    def test_aggregator_copy_of_a_direct_posting_alerts_once(self) -> None:
        direct = source("direct", "Example")
        aggregator = source("simplify", "Simplify", aggregator=True)
        direct_job = Job("direct", "Example", "Product Manager Intern - Summer 2027", "https://jobs.example.com/jobs/1", "Toronto, ON", description=PM_TEXT, req_id="R-1")
        copy = Job("simplify", "Example Inc.", "Product Manager Intern", "https://jobs.example.com/jobs/1?utm_source=simplify", "Toronto, ON", term_hint="Summer 2027")
        notifier = FakeNotifier()
        monitor = monitor_for([aggregator, direct], StaticClient("{}"), self.store, notifier)

        def check(src):
            job = copy if src.aggregator else direct_job
            return SourceOutcome(SourceResult(src, (job,), 1), [ProcessedJob(job, monitor._classify(job))])

        with patch.object(monitor, "_check_source", side_effect=check):
            self.assertEqual(monitor.run_once().alerts_delivered, 1)
            self.assertEqual(monitor.run_once().alerts_delivered, 0)
        self.assertEqual(self.store.counts()["total"], 1)

    def test_failed_delivery_stays_pending_for_the_next_run(self) -> None:
        client = StaticClient(json_jobs(listing("1")))
        notifier = FakeNotifier(deliver=False)
        monitor = monitor_for([source()], client, self.store, notifier)
        summary = monitor.run_once()
        self.assertEqual(summary.alerts_pending, 1)
        notifier.deliver = True
        self.assertEqual(monitor.run_once().alerts_delivered, 1)

    def test_baseline_mode_silences_existing_matches(self) -> None:
        client = StaticClient(json_jobs(listing("1")))
        notifier = FakeNotifier()
        monitor = monitor_for([source()], client, self.store, notifier, alert_on_first_run=False)
        self.assertEqual(monitor.run_once().alerts_delivered, 0)
        self.assertEqual(notifier.digests, [])
        self.assertTrue(self.store.is_initialized())
        self.assertEqual(self.store.counts()["pending"], 0)

    def test_dry_run_changes_nothing(self) -> None:
        notifier = FakeNotifier()
        monitor = monitor_for([source()], StaticClient(json_jobs(listing("1"))), self.store, notifier)
        with patch.object(monitor, "_print_dry_run"):
            summary = monitor.run_once(dry_run=True)
        self.assertEqual(summary.matches, 1)
        self.assertEqual(notifier.digests, [])
        self.assertEqual(self.store.counts()["total"], 0)
        self.assertFalse(self.store.is_initialized())

    def test_review_that_becomes_accept_alerts_as_a_change(self) -> None:
        silent = listing("1", location="New York, NY", description="Write PRDs, own the roadmap and prioritize the backlog.")
        sponsored = dict(silent, description=silent["description"] + " Visa sponsorship is available.")
        client = StaticClient(json_jobs(silent))
        notifier = FakeNotifier()
        monitor = monitor_for([source()], client, self.store, notifier)
        monitor.run_once()
        self.assertEqual(notifier.digests[0][0].decision, REVIEW)
        client.text = json_jobs(sponsored)
        monitor.run_once()
        self.assertEqual(notifier.digests[1][0].event, "changed: REVIEW -> ACCEPT")

    def _winter_then_summer(self, summer_id: str) -> FakeNotifier:
        winter = listing("W1", title="Intern - Product Management", description=PM_TEXT + " This 4-month co-op starts January 2027.")
        client = StaticClient(json_jobs(winter))
        notifier = FakeNotifier()
        monitor = monitor_for([source("eq", "EQ Bank", tier="must")], client, self.store, notifier, close_after_hours=1)
        monitor.run_once()
        self.assertEqual(notifier.digests, [])  # Winter term: stored as REJECT, no alert.
        # Weeks later the winter posting is gone and a Summer 2027 cohort is posted.
        self.store.connection.execute("UPDATE postings SET last_seen = '2000-01-01T00:00:00+00:00'")
        summer = listing(summer_id, title="Intern - Product Management", description=PM_TEXT + " This 4-month co-op starts May 2027.")
        client.text = json_jobs(summer)
        monitor.run_once()
        return notifier

    def test_summer_cohort_with_a_new_id_alerts_after_rejected_winter_posting(self) -> None:
        notifier = self._winter_then_summer("S1")
        self.assertEqual([(entry.decision, entry.event) for entry in notifier.digests[0]], [(ACCEPT, "new")])

    def test_summer_cohort_reusing_the_winter_posting_alerts(self) -> None:
        notifier = self._winter_then_summer("W1")
        self.assertEqual([(entry.decision, entry.event) for entry in notifier.digests[0]], [(ACCEPT, "new")])

    def test_cached_description_is_not_reused_for_a_same_title_repost(self) -> None:
        old = Job("wd", "Example", "Product Analyst Intern", "https://example.wd3.myworkdayjobs.com/Site/job/Toronto/A_R1", "Toronto, ON", req_id="R1", detail_ref="workday:h:t:s:/job/A_R1")
        repost = Job("wd", "Example", "Product Analyst Intern", "https://example.wd3.myworkdayjobs.com/Site/job/Toronto/A_R2", "Toronto, ON", req_id="R2", detail_ref="workday:h:t:s:/job/A_R2")
        client = StaticClient(json.dumps({"jobPostingInfo": {"jobDescription": PM_TEXT + " Starts January 2027 (winter co-op).", "canApply": True}}))
        monitor = monitor_for([source("wd", "Example", tier="must")], client, self.store, FakeNotifier())

        def scan(job):
            def check(src):
                item = monitor._process(job)
                return SourceOutcome(SourceResult(src, (item.job,), 1), [item])
            return check

        with patch.object(monitor, "_check_source", side_effect=scan(old)):
            monitor.run_once()
        client.text = json.dumps({"jobPostingInfo": {"jobDescription": PM_TEXT + " This 4-month co-op starts May 2027.", "canApply": True}})
        with patch.object(monitor, "_check_source", side_effect=scan(repost)):
            monitor.run_once()
        self.assertEqual(client.calls, 2)  # The repost's own description was fetched.
        self.assertEqual([row["decision"] for row in self.store.open_matches()], [ACCEPT])

    def test_notified_posting_that_disappears_alerts_as_closed(self) -> None:
        client = StaticClient(json_jobs(listing("1")))
        notifier = FakeNotifier()
        monitor = monitor_for([source()], client, self.store, notifier, close_after_hours=1)
        monitor.run_once()
        self.store.connection.execute("UPDATE postings SET last_seen = '2000-01-01T00:00:00+00:00'")
        client.text = json_jobs()
        monitor.run_once()
        self.assertEqual(notifier.digests[1][0].event, "closed")
        self.assertEqual(self.store.open_matches(), ())

    def test_priority_digest_is_sent_before_slow_source_finishes(self) -> None:
        slow_started = threading.Event()
        digest_sent = threading.Event()
        slow_finished = threading.Event()
        priority = source("tiktok", "TikTok", priority=True)
        slow = source("slow", "Slow")
        job = Job("tiktok", "TikTok", "Product Manager Intern - Summer 2027", "https://jobs.example.com/jobs/9", "Toronto, ON", description=PM_TEXT)
        notifier = FakeNotifier()
        monitor = Monitor((slow, priority), StaticClient("{}"), RoleFilter(2027), self.store, notifier, 2, True, MonitorOptions(today=TODAY))

        def check(src):
            if src.priority:
                self.assertTrue(slow_started.wait(timeout=3))
                return SourceOutcome(SourceResult(src, (job,), 1), [ProcessedJob(job, monitor._classify(job))])
            slow_started.set()
            self.assertTrue(digest_sent.wait(timeout=3), "Priority digest waited for the slow source")
            slow_finished.set()
            return SourceOutcome(SourceResult(src, (), 1), [])

        original = notifier.send_digest

        def send(entries):
            self.assertFalse(slow_finished.is_set())
            digest_sent.set()
            return original(entries)

        notifier.send_digest = send
        with patch.object(monitor, "_check_source", side_effect=check):
            summary = monitor.run_once()
        self.assertEqual(summary.alerts_delivered, 1)
        self.assertEqual(summary.sources_failed, 0)


class SchedulingAndDescriptionTests(StoreCase):
    def test_additional_sources_wait_for_their_interval(self) -> None:
        must = source("must", "Must Co", tier="must")
        extra = source("extra", "Extra Co")
        client = StaticClient(json_jobs())
        monitor = monitor_for([must, extra], client, self.store, FakeNotifier(), additional_scan_minutes=60)
        self.assertEqual({item.id for item in monitor.due_sources()}, {"must", "extra"})
        monitor.run_once()
        self.assertEqual({item.id for item in monitor.due_sources()}, {"must"})

    def test_descriptions_are_fetched_once_and_cached(self) -> None:
        job = Job("wd", "Example", "Product Analyst Intern", "https://example.wd3.myworkdayjobs.com/Site/job/Toronto/Analyst_R1", "Toronto, ON", detail_ref="workday:example.wd3.myworkdayjobs.com:example:Site:/job/Toronto/Analyst_R1")
        workday = source("wd", "Example", tier="must")
        detail = {"jobPostingInfo": {"jobDescription": PM_TEXT + " This is a 4-month co-op starting May 2027.", "location": "Toronto, ON", "canApply": True, "jobReqId": "R1"}}
        client = StaticClient(json.dumps(detail))
        monitor = monitor_for([workday], client, self.store, FakeNotifier())

        def check(src):
            item = monitor._process(job)
            return SourceOutcome(SourceResult(src, (item.job,), 1), [item])

        with patch.object(monitor, "_check_source", side_effect=check):
            monitor.run_once()
            monitor.run_once()
        self.assertEqual(client.calls, 1)
        row = self.store.open_matches()[0]
        self.assertEqual(row["decision"], ACCEPT)
        self.assertEqual(row["term_length"], "4 months")

    def test_description_budget_defers_postings(self) -> None:
        job = Job("wd", "Example", "Product Analyst Intern", "https://jobs.example.com/1", "Toronto, ON", detail_ref="workday:h:t:s:/job/1")
        monitor = monitor_for([source()], StaticClient("{}"), self.store, FakeNotifier(), max_description_fetches=0)
        self.assertIsNone(monitor._process(job))


class LegacyPaginationTests(unittest.TestCase):
    def check(self, source_obj, client):
        return Monitor((source_obj,), client, RoleFilter(2027), None, None, 1, False, MonitorOptions(today=TODAY))._check_source(source_obj)

    def test_disney_api_pages_are_unwrapped_and_all_scanned(self) -> None:
        from urllib.parse import parse_qs, urlsplit

        class DisneyClient(StaticClient):
            def fetch(self, url, extra_headers=None, method="GET", body=""):
                page = int(parse_qs(urlsplit(url).query)["CurrentPage"][0])
                title = "Senior Product Manager" if page == 1 else "Product Owner Co-op"
                html = f'<section data-current-page="{page}" data-total-pages="2"><a data-job-id="{page}" href="/en/job/toronto/owner/1/{page}"><h2>{title}</h2><span class="job-location">Toronto, Canada</span></a></section>'
                return FetchResult(url, url, "application/json", json.dumps({"results": html}))

        src = Source("disney", "Disney", ("https://www.disneycareers.com/en/search-jobs/results?CurrentPage=1",), "https://www.disneycareers.com", paginate=True)
        with patch("apm_notifier.monitor.Monitor._process", side_effect=lambda job: ProcessedJob(job, None)):
            outcome = self.check(src, DisneyClient(""))
        self.assertTrue(outcome.result.succeeded)
        self.assertEqual([item.job.url.rsplit("/", 1)[-1] for item in outcome.processed], ["2"])

    def test_html_next_page_is_scanned(self) -> None:
        class HtmlClient(StaticClient):
            def fetch(self, url, extra_headers=None, method="GET", body=""):
                text = '<a href="/jobs/old">Senior Product Manager</a><a href="/page2">Next</a>' if url.endswith("page1") else '<a href="/jobs/new">Product Owner Intern - Toronto</a>'
                return FetchResult(url, url, "text/html", text)

        src = Source("example", "Example", ("https://example.com/page1",), "https://example.com", paginate=True)
        with patch("apm_notifier.monitor.Monitor._process", side_effect=lambda job: ProcessedJob(job, None)):
            outcome = self.check(src, HtmlClient(""))
        self.assertTrue(outcome.result.succeeded)
        self.assertEqual(len(outcome.processed), 1)

    def test_linkedin_empty_fragment_is_a_valid_end_of_results(self) -> None:
        src = Source("revolut", "Revolut", ("https://www.linkedin.com/jobs-guest/jobs/api/search",), "https://www.revolut.com/careers", paginate=True)
        self.assertTrue(self.check(src, StaticClient("<!DOCTYPE html>\n\n<!---->  ", "text/html")).result.succeeded)

    def test_scans_later_pages_and_rejects_repeated_pages(self) -> None:
        for offset_key, total_key in (("offset", "total"), ("paginationStart", "maxCount")):
            for repeated in (False, True):
                with self.subTest(offset_key=offset_key, repeated=repeated):
                    src = Source(
                        id="example", name="Example", urls=("https://jobs.example.com/api",),
                        career_url="https://jobs.example.com", request_method="POST", paginate=True,
                        request_bodies=(json.dumps({offset_key: 0, "limit": 1}),),
                    )

                    class PagedClient(StaticClient):
                        def fetch(self, url, extra_headers=None, method="GET", body=""):
                            offset = json.loads(body)[offset_key]
                            title = "Senior Product Manager" if offset < 2 or repeated else "Product Manager Intern"
                            reported_total = 0 if total_key == "total" and offset else 3
                            payload = {total_key: reported_total, "jobPostings": [{"title": title, "url": "/jobs/1" if repeated else f"/jobs/{offset + 1}", "location": "New York", "description": PM_TEXT}]}
                            return FetchResult(url, url, "application/json", json.dumps(payload))

                    outcome = self.check(src, PagedClient(""))
                    self.assertEqual(outcome.result.succeeded, not repeated)
                    self.assertEqual(len(outcome.processed), 0 if repeated else 1)
                    if repeated:
                        self.assertIn("repeated a page", outcome.result.errors[0])

    def test_partial_source_fetch_is_not_healthy(self) -> None:
        src = Source("example", "Example", ("https://jobs.example.com/one", "https://jobs.example.com/two"), "https://jobs.example.com/")
        self.assertFalse(SourceResult(source=src, jobs=(), fetched_urls=1, errors=("second failed",)).succeeded)


if __name__ == "__main__":
    unittest.main()
