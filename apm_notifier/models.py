from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMETERS = {
    "gh_src",
    "lever-source",
    "source",
    "src",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def canonical_url(value: str) -> str:
    """Remove fragments and common tracking parameters without changing job IDs."""
    parts = urlsplit(value)
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in TRACKING_PARAMETERS
    ]
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            urlencode(query),
            "",
        )
    )


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    urls: tuple[str, ...]
    career_url: str
    enabled: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    url_template: str = ""
    render: bool = False
    request_method: str = "GET"
    request_bodies: tuple[str, ...] = ()
    verify_job_links: bool = False
    paginate: bool = False
    priority: bool = False
    # Structured job-board adapter ("greenhouse", "lever", "workday", ...).
    # Empty means the legacy URL list plus generic extraction is used.
    ats: str = ""
    ats_options: dict[str, Any] = field(default_factory=dict)
    # "must" companies are scanned every run; "additional" ones on a slower cadence.
    tier: str = "additional"
    added_on: str = ""
    discovered_via: str = ""
    # Aggregators report postings from many employers rather than one company.
    aggregator: bool = False

    @property
    def must_include(self) -> bool:
        return self.tier == "must"


@dataclass(frozen=True)
class Job:
    source_id: str
    company: str
    title: str
    url: str
    location: str = ""
    discovered_at: str = field(default_factory=utc_now)
    requirements: str = ""
    # Richer fields supplied by structured adapters; all optional.
    description: str = ""
    req_id: str = ""
    apply_url: str = ""
    employment_type: str = ""
    remote: bool | None = None
    country_codes: tuple[str, ...] = ()
    posted_date: str = ""
    close_date: str = ""
    term_hint: str = ""
    sponsorship_hint: str = ""
    detail_ref: str = ""

    @property
    def full_text(self) -> str:
        return "\n".join(part for part in (self.description, self.requirements) if part)

    @property
    def role_key(self) -> tuple[str, str]:
        return normalize_space(self.company).casefold(), canonical_url(self.url)

    @property
    def fingerprint(self) -> str:
        material = "|".join(
            (
                normalize_space(self.company).casefold(),
                normalize_space(self.title).casefold(),
                canonical_url(self.url),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    content_type: str
    text: str


@dataclass(frozen=True)
class SourceResult:
    source: Source
    jobs: tuple[Job, ...]
    fetched_urls: int
    errors: tuple[str, ...] = ()
    # Number of job records read before title filtering; zero across several
    # successful runs usually means a broken adapter rather than a quiet company.
    raw_count: int = -1
    expected_urls: int = -1

    @property
    def succeeded(self) -> bool:
        expected = self.expected_urls if self.expected_urls >= 0 else len(self.source.urls)
        return self.fetched_urls >= expected and not self.errors
