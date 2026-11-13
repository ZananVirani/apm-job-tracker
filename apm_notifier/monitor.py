from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .adapters import DescriptionFetcher, DescriptionResult, enrich, make_adapter
from .classify import ACCEPT, REJECT, REVIEW, Classification, classify, plain_text
from .export import export_matches
from .extract import CareerHTMLParser, LinkedInJobCardParser, extract_jobs, response_has_job_signal
from .fetch import BrowserRenderer, HttpClient
from .filtering import RoleFilter
from .models import FetchResult, Job, Source, SourceResult, utc_now
from .notify import DigestEntry, NotificationManager
from .state import CachedDescription, StateStore, company_key, hours_ago, posting_keys
from .tiktok import normalize_search_response


LOGGER = logging.getLogger("apm_notifier")


@dataclass(frozen=True)
class RunSummary:
    sources_ok: int
    sources_failed: int
    matches: int
    alerts_delivered: int
    alerts_pending: int
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ProcessedJob:
    job: Job
    result: Classification
    fetched_at: str | None = None
    detail: dict | None = None


@dataclass
class SourceOutcome:
    result: SourceResult
    processed: list[ProcessedJob]
    deferred: int = 0
    complete: bool = True


@dataclass
class MonitorOptions:
    target_year: int = 2027
    graduation_year: int = 2028
    graduation_month: int = 4
    additional_scan_minutes: int = 60
    max_description_fetches: int = 250
    description_refresh_hours: int = 24
    close_after_hours: int = 24
    zero_result_warn_runs: int = 3
    export_dir: Path | None = None
    today: date | None = None


class Monitor:
    def __init__(
        self,
        sources: tuple[Source, ...],
        client: HttpClient,
        role_filter: RoleFilter,
        store: StateStore,
        notifier: NotificationManager,
        concurrency: int,
        alert_on_first_run: bool,
        options: MonitorOptions | None = None,
    ) -> None:
        self.sources = sources
        self.client = client
        self.role_filter = role_filter
        self.store = store
        self.notifier = notifier
        self.concurrency = concurrency
        self.alert_on_first_run = alert_on_first_run
        self.options = options or MonitorOptions()
        self.browser = BrowserRenderer(client.timeout_seconds)
        self.descriptions = DescriptionFetcher(client)
        self._budget_lock = threading.Lock()
        self._fetch_budget = self.options.max_description_fetches
        self._description_cache: dict[str, CachedDescription] = {}
        self.last_outcomes: list[SourceOutcome] = []

    # --- Scheduling -----------------------------------------------------------------

    def due_sources(self, force_all: bool = False) -> tuple[Source, ...]:
        if force_all or self.store is None:
            return self.sources
        last_checked = self.store.source_last_checked()
        threshold = datetime.now(timezone.utc) - timedelta(minutes=self.options.additional_scan_minutes)
        due: list[Source] = []
        for source in self.sources:
            if source.must_include or source.priority:
                due.append(source)
                continue
            checked = last_checked.get(source.id)
            if not checked or datetime.fromisoformat(checked) <= threshold:
                due.append(source)
        return tuple(due)

    # --- Main cycle -------------------------------------------------------------------

    def run_once(self, dry_run: bool = False, force_all: bool = False, show_rejects: bool = False) -> RunSummary:
        run_started = utc_now()
        sources = self.due_sources(force_all or dry_run)
        skipped = len(self.sources) - len(sources)
        LOGGER.info("Checking %d career sources (%d additional sources not due yet)", len(sources), skipped)
        self._fetch_budget = self.options.max_description_fetches
        self._description_cache = self.store.description_cache() if self.store is not None and not dry_run else {}
        baseline_only = not dry_run and not self.store.is_initialized() and not self.alert_on_first_run
        delivered = 0
        outcomes: list[SourceOutcome] = []
        decisions: Counter[str] = Counter()
        claimed: set[int] = set()
        with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            ordered = sorted(sources, key=lambda source: (not source.priority, not source.must_include))
            futures = {executor.submit(self._check_source, source): source for source in ordered}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    outcome = future.result()
                except Exception as error:  # Defensive: one broken adapter must not stop the run.
                    outcome = SourceOutcome(SourceResult(source, (), 0, (str(error),), raw_count=-1, expected_urls=0), [], complete=False)
                outcomes.append(outcome)
                counts = Counter(item.result.decision for item in outcome.processed)
                decisions.update(counts)
                self._log_source(outcome, counts)
                if dry_run:
                    continue
                accept_review = counts[ACCEPT] + counts[REVIEW]
                if self.store.record_source_result(outcome.result, accept_review):
                    health = self.notifier.send_health(outcome.result)
                    if health.errors:
                        LOGGER.error("Health alert failed: %s", "; ".join(health.errors))
                self._record(outcome, claimed, run_started)
                if source.priority and not baseline_only:
                    delivered += self._deliver_pending()

        self.last_outcomes = outcomes
        succeeded = sum(outcome.result.succeeded for outcome in outcomes)
        failed = len(outcomes) - succeeded
        matches = decisions[ACCEPT] + decisions[REVIEW]
        if dry_run:
            self._print_dry_run(outcomes, show_rejects)
            return RunSummary(succeeded, failed, matches, 0, 0, dict(decisions))

        if baseline_only:
            self.store.silence_pending()
        if succeeded:
            self.store.mark_initialized()
        if self.notifier.configured_channels:
            delivered += self._deliver_pending()
        else:
            LOGGER.warning("No notification channel is configured; matches remain pending. Set Telegram or ntfy values in .env.")
        if self.options.export_dir is not None:
            csv_path, _ = export_matches(self.store.open_matches(), self.options.export_dir)
            LOGGER.info("Exported %d open ACCEPT/REVIEW posting(s) to %s", len(self.store.open_matches()), csv_path)
        pending = len(self.store.pending_postings())
        self._log_summary(outcomes, decisions, delivered, pending, skipped)
        return RunSummary(succeeded, failed, matches, delivered, pending, dict(decisions))

    def _record(self, outcome: SourceOutcome, claimed: set[int], run_started: str) -> None:
        source = outcome.result.source
        for item in outcome.processed:
            self.store.upsert_posting(
                item.job,
                item.result,
                source_name=source.name,
                aggregator=source.aggregator,
                company_priority=self._company_priority(item.job),
                description_fetched_at=item.fetched_at,
                detail=item.detail,
                claimed=claimed,
                now=run_started,
            )
            if source.aggregator and company_key(item.job.company) not in self._known_companies:
                if self.store.register_company(item.job.company, source.name, item.result.decision != REJECT, run_started):
                    LOGGER.info("Registered newly discovered company %s (via %s)", item.job.company, source.name)
        if outcome.result.succeeded and outcome.complete and not outcome.deferred:
            closed = self.store.close_missing(source.id, hours_ago(self.options.close_after_hours))
            if closed:
                LOGGER.info("%s: marked %d posting(s) closed after %dh unseen", source.name, closed, self.options.close_after_hours)

    @property
    def _structured_companies(self) -> set[str]:
        keys: set[str] = set()
        for source in self.sources:
            if source.ats and not source.aggregator:
                keys.add(company_key(source.name))
                keys.add(company_key(source.name.split("(")[0]))
        return keys

    @property
    def _known_companies(self) -> set[str]:
        return {company_key(source.name) for source in self.sources if not source.aggregator}

    def _company_priority(self, job: Job) -> str:
        key = company_key(job.company)
        for source in self.sources:
            if company_key(source.name) == key and not source.aggregator:
                return source.tier
        return "discovered"

    def _deliver_pending(self) -> int:
        if not self.notifier.configured_channels:
            return 0
        rows = self.store.pending_postings()
        if not rows:
            return 0
        entries = [self._digest_entry(row) for row in rows]
        outcome = self.notifier.send_digest(entries)
        if outcome.errors:
            LOGGER.error("Digest delivery error: %s", "; ".join(outcome.errors))
        if not outcome.delivered:
            return 0
        self.store.mark_notified([row["id"] for row in rows])
        LOGGER.info("Digest delivered with %d posting(s)", len(rows))
        return len(rows)

    @staticmethod
    def _digest_entry(row) -> DigestEntry:
        reasons = json.loads(row["reasons"] or "[]")
        reason = reasons[0] if reasons else ""
        term = " · ".join(filter(None, (row["term_start"], row["term_length"])))
        location = row["location"] + (f" ({row['location_note']})" if row["location_note"] else "")
        return DigestEntry(
            posting_id=row["id"],
            company=row["company"],
            title=row["title"],
            location=location,
            term=term,
            decision=row["decision"],
            reason=reason.removeprefix("REVIEW: "),
            url=row["apply_url"] or row["posting_url"],
            event=row["pending_event"] or "new",
            must_include=row["company_priority"] == "must",
            core_title=row["normalized_title_category"] == "core",
            gta=bool(row["priority_location"]),
            newest=row["posted_date"] or row["first_seen"],
        )

    # --- Per-source work (runs in worker threads) -------------------------------------

    def _check_source(self, source: Source) -> SourceOutcome:
        if source.ats:
            adapter = make_adapter(source.ats, self.client, self.role_filter)
            errors: list[str] = []
            try:
                scanned = adapter.scan(source)
                jobs, raw_count, complete = scanned.jobs, scanned.raw_count, scanned.complete
                errors.extend(scanned.errors)
            except Exception as error:
                jobs, raw_count, complete = [], -1, False
                errors.append(str(error))
            fetched, expected = (0, 1) if errors and not jobs else (1, 1)
        else:
            jobs, fetched, errors = self._scan_legacy(source)
            raw_count, complete, expected = -1, True, len(source.urls)
        unique: dict[tuple, Job] = {}
        for job in jobs:
            # Aggregator copies of employers whose full board is read by a structured
            # adapter add nothing but description fetches; the direct feed covers them.
            if source.aggregator and company_key(job.company) in self._structured_companies:
                continue
            unique.setdefault(posting_keys(job), job)
        processed: list[ProcessedJob] = []
        deferred = 0
        for job in unique.values():
            try:
                item = self._process(job)
            except Exception as error:
                # A single unreadable posting is retried next run; it does not make the feed unhealthy.
                LOGGER.warning("%s: description fetch failed for %s: %s", source.name, job.title, str(error)[:200])
                deferred += 1
                continue
            if item is None:
                deferred += 1
                continue
            processed.append(item)
        if deferred:
            LOGGER.info("%s: deferred %d posting(s) whose descriptions could not be fetched this run", source.name, deferred)
        result = SourceResult(
            source=source,
            jobs=tuple(item.job for item in processed),
            fetched_urls=fetched,
            errors=tuple(errors),
            raw_count=raw_count,
            expected_urls=expected,
        )
        return SourceOutcome(result, processed, deferred, complete)

    def _classify(self, job: Job) -> Classification:
        return classify(
            job,
            today=self.options.today,
            target_year=self.options.target_year,
            grad_year=self.options.graduation_year,
            grad_month=self.options.graduation_month,
        )

    def _process(self, job: Job) -> ProcessedJob | None:
        """Classify a job, fetching its full description first when the listing lacked one."""
        # Text supplied by an official listing API is authoritative even when short.
        has_text = bool(plain_text(job.full_text).strip())
        preliminary = self._classify(job)
        if has_text or preliminary.decision == REJECT:
            return ProcessedJob(job, preliminary)

        cached = self._cached_description(job)
        if cached is not None:
            fields = {key: (tuple(value) if key == "country_codes" else value) for key, value in cached.detail.items() if key != "text"}
            detail = DescriptionResult(text=cached.text, **fields)
            return self._finish(enrich(job, detail), detail, None)

        with self._budget_lock:
            if self._fetch_budget <= 0:
                return None
            self._fetch_budget -= 1
        detail = self.descriptions.fetch(job)
        fetched_at = utc_now()
        return self._finish(enrich(job, detail), detail, fetched_at)

    def _finish(self, job: Job, detail: DescriptionResult, fetched_at: str | None) -> ProcessedJob:
        result = self._classify(job)
        if detail.closed and result.status != "closed":
            result.status = "closed"
            result._flag(REJECT, "Posting is closed (the job board no longer accepts applications)")
        if not detail.text and detail.note:
            result.reasons.append(f"description unavailable: {detail.note}")
        stored_detail = {
            "location": detail.location,
            "employment_type": detail.employment_type,
            "close_date": detail.close_date,
            "closed": detail.closed,
            "req_id": detail.req_id,
            "country_codes": list(detail.country_codes),
            "remote": detail.remote,
            "note": detail.note,
        }
        return ProcessedJob(job, result, fetched_at or None, stored_detail if fetched_at else None)

    def _cached_description(self, job: Job) -> CachedDescription | None:
        if not self._description_cache:
            return None
        cutoff = hours_ago(self.options.description_refresh_hours)
        # Only exact identities (requisition id or URL) may reuse a description: a repost with
        # the same title and location (e.g. a Summer cohort after a Winter one) must be re-read.
        req_key, url_key, _ = posting_keys(job)
        for key in (req_key, url_key):
            entry = self._description_cache.get(key) if key else None
            if entry is not None and entry.fetched_at >= cutoff:
                return entry
        return None

    def _scan_legacy(self, source: Source) -> tuple[list[Job], int, list[str]]:
        jobs: dict[str, Job] = {}
        errors: list[str] = []
        fetched = 0
        for index, url in enumerate(source.urls):
            try:
                for response in self._source_responses(source, index, url):
                    for job in extract_jobs(response.text, response.content_type, source, response.final_url, self.role_filter):
                        if source.verify_job_links and not self.client.url_exists(job.url):
                            LOGGER.warning("Skipping confirmed dead back-check link: %s", job.url)
                            continue
                        jobs[job.fingerprint] = job
                fetched += 1
            except Exception as error:
                errors.append(str(error))
        return list(jobs.values()), fetched, errors

    def run_forever(self, interval_seconds: int) -> None:
        LOGGER.info("Starting monitor loop; checking every %d seconds", interval_seconds)
        while True:
            started = time.monotonic()
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("Monitor cycle failed")
            elapsed = time.monotonic() - started
            time.sleep(max(1, interval_seconds - elapsed))

    # --- Reporting --------------------------------------------------------------------

    @staticmethod
    def _log_source(outcome: SourceOutcome, counts: Counter[str]) -> None:
        result = outcome.result
        level = logging.INFO if result.succeeded else logging.WARNING
        raw = "?" if result.raw_count < 0 else str(result.raw_count)
        LOGGER.log(
            level,
            "%s: %s posting(s) read, %d candidate(s): %d accept / %d review / %d reject%s%s",
            result.source.name,
            raw,
            len(outcome.processed),
            counts[ACCEPT],
            counts[REVIEW],
            counts[REJECT],
            f", {outcome.deferred} deferred" if outcome.deferred else "",
            f"; errors: {'; '.join(result.errors)[:500]}" if result.errors else "",
        )

    def _log_summary(self, outcomes: list[SourceOutcome], decisions: Counter[str], delivered: int, pending: int, skipped: int) -> None:
        failed = [outcome.result for outcome in outcomes if not outcome.result.succeeded]
        LOGGER.info(
            "Run summary: %d companies scanned (%d not due), %d postings read, %d candidates: "
            "%d accept / %d review / %d reject; %d failed source(s); %d alert(s) delivered, %d pending",
            len(outcomes),
            skipped,
            sum(max(outcome.result.raw_count, 0) for outcome in outcomes),
            sum(len(outcome.processed) for outcome in outcomes),
            decisions[ACCEPT],
            decisions[REVIEW],
            decisions[REJECT],
            len(failed),
            delivered,
            pending,
        )
        for result in failed:
            LOGGER.warning("Source error — %s: %s", result.source.name, "; ".join(result.errors)[:300])
        for row in self.store.status_rows():
            if row["consecutive_empty_runs"] >= self.options.zero_result_warn_runs:
                LOGGER.warning(
                    "Possible broken adapter: %s returned zero postings for %d consecutive runs",
                    row["source_name"],
                    row["consecutive_empty_runs"],
                )

    @staticmethod
    def _print_dry_run(outcomes: list[SourceOutcome], show_rejects: bool) -> None:
        order = {ACCEPT: 0, REVIEW: 1, REJECT: 2}
        items = [item for outcome in outcomes for item in outcome.processed]
        visible = [item for item in items if show_rejects or item.result.decision != REJECT]
        print(f"\nDry run: {len(items)} candidate posting(s); showing {len(visible)}.")
        for item in sorted(visible, key=lambda entry: (order[entry.result.decision], entry.job.company, entry.job.title)):
            job, result = item.job, item.result
            term = " · ".join(filter(None, (result.term_start, result.term_length)))
            print(f"\n[{result.decision}] {job.company}: {job.title}")
            print(f"  {job.location or 'no location'}{' · ' + term if term else ''} · {result.category}{' · GTA' if result.priority_location else ''}")
            print(f"  reason: {result.reason}")
            for quoted in result.quotes[:3]:
                print(f'  quote: "{quoted}"')
            print(f"  {job.url}")
        print("\nPer-source results:")
        for outcome in sorted(outcomes, key=lambda entry: entry.result.source.name.casefold()):
            result = outcome.result
            counts = Counter(item.result.decision for item in outcome.processed)
            state = "OK" if result.succeeded else "FAILED"
            raw = "?" if result.raw_count < 0 else str(result.raw_count)
            print(
                f"  {state:6} {result.source.name}: read {raw}, candidates {len(outcome.processed)}, "
                f"A/R/R {counts[ACCEPT]}/{counts[REVIEW]}/{counts[REJECT]}"
                + (f", deferred {outcome.deferred}" if outcome.deferred else "")
                + (f" — {'; '.join(result.errors)[:200]}" if result.errors else "")
            )

    def _source_responses(self, source: Source, index: int, url: str) -> Iterator[FetchResult]:
        body = source.request_bodies[index] if source.request_bodies else ""
        previous_pages: set[str] = set()
        total: int | None = None
        page_url = url
        for _ in range(50):
            if source.render:
                if source.request_method != "GET" or body or source.paginate:
                    raise ValueError("Rendered sources only support unpaginated GET requests")
                response = self.browser.fetch(url)
            else:
                response = self.client.fetch(
                    page_url, source.headers, method=source.request_method, body=body,
                )
            response = normalize_search_response(response)
            if source.paginate and "linkedin.com/jobs-guest/" in url:
                cards = LinkedInJobCardParser()
                cards.feed(response.text)
                if not cards.cards:
                    empty_fragment = re.sub(r"<!DOCTYPE html>|<!--.*?-->", "", response.text, flags=re.IGNORECASE | re.DOTALL)
                    if not empty_fragment.strip():
                        return
                    if not response_has_job_signal(response.text, response.content_type):
                        raise ValueError(f"{page_url}: LinkedIn returned an unreadable search page")
                    return
                page_key = json.dumps(cards.cards)
                if page_key in previous_pages:
                    raise ValueError(f"{page_url}: pagination repeated a page before completion")
                previous_pages.add(page_key)
                yield response
                parts = urlsplit(page_url)
                query = dict(parse_qsl(parts.query))
                query["start"] = str(int(query.get("start", "0")) + len(cards.cards))
                page_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
                continue
            if source.paginate and "disneycareers.com" in urlsplit(url).netloc and "json" in response.content_type:
                payload = json.loads(response.text)
                html = payload.get("results")
                if not isinstance(html, str) or not response_has_job_signal(html, "text/html"):
                    raise ValueError(f"{page_url}: Disney returned unreadable job results")
                yield FetchResult(response.requested_url, response.final_url, "text/html", html)
                current = re.search(r'data-current-page="(\d+)"', html)
                total_pages = re.search(r'data-total-pages="(\d+)"', html)
                if not current or not total_pages:
                    raise ValueError(f"{page_url}: Disney omitted pagination metadata")
                page = int(current.group(1))
                if page >= int(total_pages.group(1)):
                    return
                if str(page) in previous_pages:
                    raise ValueError(f"{page_url}: pagination repeated a page before completion")
                previous_pages.add(str(page))
                parts = urlsplit(page_url)
                query = dict(parse_qsl(parts.query))
                query["CurrentPage"] = str(page + 1)
                page_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
                continue
            if not response_has_job_signal(response.text, response.content_type):
                raise ValueError(f"{url}: response contained no job records or explicit zero-result state")
            if not source.paginate:
                yield response
                return
            if source.request_method == "GET" and "json" not in response.content_type.casefold():
                if page_url in previous_pages:
                    raise ValueError(f"{page_url}: pagination repeated a page before completion")
                previous_pages.add(page_url)
                yield response
                parser = CareerHTMLParser()
                parser.feed(response.text)
                next_link = next((href for href, label in parser.anchors if label.casefold() in {"next", "next page"}), None)
                if not next_link:
                    return

                page_url = urljoin(response.final_url, next_link)
                continue
            request_body = json.loads(body)
            payload = json.loads(response.text)
            rows = payload.get("jobPostings")
            reported_total = payload.get("total", payload.get("maxCount"))
            if not isinstance(rows, list) or not isinstance(reported_total, int) or reported_total < 0:
                raise ValueError(f"{url}: invalid paginated job response")
            # Workday reports total=0 on later pages even while returning jobs.
            if total is None:
                total = reported_total
                if rows and total < len(rows):
                    raise ValueError(f"{url}: invalid first-page job count")
            offset_key = "paginationStart" if "paginationStart" in request_body else "offset"
            offset = request_body.get(offset_key, 0)
            if not rows and offset < total:
                raise ValueError(f"{url}: empty page before all {total} jobs were scanned")
            page_key = json.dumps(rows, sort_keys=True)
            if rows and page_key in previous_pages:
                raise ValueError(f"{url}: pagination repeated a page before completion")
            previous_pages.add(page_key)
            yield response
            if offset + len(rows) >= total:
                return
            request_body[offset_key] = offset + len(rows)
            body = json.dumps(request_body)
        raise ValueError(f"{url}: pagination exceeded 50 pages; scan incomplete")

