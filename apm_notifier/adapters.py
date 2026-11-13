"""Structured job-board adapters behind one interface.

Each adapter reads a public, official job-board API and returns normalized
``Job`` objects (title, location, URLs, requisition id and, where the board
provides it, the full description). ``DescriptionFetcher`` fills in the
description for boards whose listing endpoints omit it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from html import unescape
import json
import logging
import re
import threading
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .classify import plain_text
from .fetch import HttpClient
from .filtering import RoleFilter
from .models import Job, Source, normalize_space


LOGGER = logging.getLogger("apm_notifier")
INTERN_QUERIES = ("intern", "internship", "co-op", "student", "summer 2027")
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
COUNTRY_NAMES = {
    "canada": "CA",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "ca": "CA",
}
INTERN_FACET = re.compile(r"\b(?:intern(?:ship)?s?|co-?op|students?|campus|summer|stagiaire|university)\b", re.IGNORECASE)
NOT_INTERN_FACET = re.compile(r"\binternal\b|\binternational\b", re.IGNORECASE)


@dataclass
class AdapterResult:
    jobs: list[Job] = field(default_factory=list)
    raw_count: int = 0
    errors: list[str] = field(default_factory=list)
    requests: int = 0
    complete: bool = True


def _country_code(value: str) -> str:
    lowered = normalize_space(value).casefold()
    if lowered in COUNTRY_NAMES:
        return COUNTRY_NAMES[lowered]
    return value.strip().upper() if len(value.strip()) == 2 else ""


def _epoch_date(value: Any) -> str:
    if isinstance(value, (int, float)) and value > 0:
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
    if isinstance(value, str):
        return value[:10]
    return ""


class Adapter:
    name = ""

    def __init__(self, client: HttpClient, role_filter: RoleFilter) -> None:
        self.client = client
        self.role_filter = role_filter

    def scan(self, source: Source) -> AdapterResult:
        raise NotImplementedError

    def _get_json(self, url: str, result: AdapterResult, headers: dict[str, str] | None = None) -> Any:
        result.requests += 1
        response = self.client.fetch(url, {"Accept": "application/json", **(headers or {})})
        return json.loads(response.text)

    def _post_json(self, url: str, body: Any, result: AdapterResult) -> Any:
        result.requests += 1
        response = self.client.fetch(url, JSON_HEADERS, method="POST", body=json.dumps(body))
        return json.loads(response.text)

    def _keep(self, title: str) -> bool:
        return self.role_filter.matches(title)

    @staticmethod
    def _query_failed(result: AdapterResult, query: str, error: Exception) -> None:
        """Keep what other queries found; a failed query only marks the scan incomplete."""
        result.errors.append(f"query {query!r}: {error}")
        result.complete = False


class GreenhouseAdapter(Adapter):
    name = "greenhouse"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        board = source.ats_options["board"]
        payload = self._get_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true", result)
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"Greenhouse board {board} returned no job list")
        result.raw_count = len(rows)
        for row in rows:
            title = normalize_space(row.get("title", ""))
            if not self._keep(title):
                continue
            offices = "; ".join(
                normalize_space(office.get("location") or office.get("name") or "")
                for office in row.get("offices") or []
                if isinstance(office, dict)
            )
            location = "; ".join(filter(None, dict.fromkeys((normalize_space((row.get("location") or {}).get("name", "")), offices))))
            metadata = " ".join(
                f"{item.get('name')}: {item.get('value')}"
                for item in row.get("metadata") or []
                if isinstance(item, dict) and item.get("value")
            )
            result.jobs.append(
                Job(
                    source_id=source.id,
                    company=source.name,
                    title=title,
                    url=row.get("absolute_url") or f"https://job-boards.greenhouse.io/{board}/jobs/{row.get('id')}",
                    location=location,
                    description=unescape(row.get("content") or ""),
                    requirements=metadata,
                    req_id=str(row.get("requisition_id") or row.get("id") or ""),
                    posted_date=str(row.get("first_published") or row.get("updated_at") or "")[:10],
                )
            )
        return result


class LeverAdapter(Adapter):
    name = "lever"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        board = source.ats_options["board"]
        rows = self._get_json(f"https://api.lever.co/v0/postings/{board}?mode=json", result)
        if not isinstance(rows, list):
            raise ValueError(f"Lever board {board} returned no job list")
        result.raw_count = len(rows)
        for row in rows:
            result.jobs.extend(self._job(source, board, row))
        return result

    def _job(self, source: Source, board: str, row: dict[str, Any]) -> list[Job]:
        title = normalize_space(row.get("text", ""))
        if not self._keep(title):
            return []
        categories = row.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location", "")]
        lists = "\n".join(
            f"{item.get('text', '')}\n{item.get('content', '')}" for item in row.get("lists") or [] if isinstance(item, dict)
        )
        description = "\n".join(filter(None, (row.get("descriptionPlain") or row.get("description"), lists, row.get("additionalPlain") or row.get("additional"))))
        country = row.get("country") or ""
        return [
            Job(
                source_id=source.id,
                company=source.name,
                title=title,
                url=row.get("hostedUrl") or f"https://jobs.lever.co/{board}/{row.get('id')}",
                apply_url=row.get("applyUrl", ""),
                location="; ".join(normalize_space(item) for item in locations if item),
                description=description,
                req_id=str(row.get("id", "")),
                employment_type=str(categories.get("commitment") or ""),
                remote=(row.get("workplaceType") == "remote") or None,
                country_codes=(country,) if country else (),
                posted_date=_epoch_date(row.get("createdAt")),
            )
        ]


class AshbyAdapter(Adapter):
    name = "ashby"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        board = source.ats_options["board"]
        payload = self._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}", result)
        rows = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"Ashby board {board} returned no job list")
        result.raw_count = len(rows)
        for row in rows:
            title = normalize_space(row.get("title", ""))
            if row.get("isListed") is False or not self._keep(title):
                continue
            locations = [row.get("location", "")] + [
                item.get("location", "") for item in row.get("secondaryLocations") or [] if isinstance(item, dict)
            ]
            countries: list[str] = []
            for item in [row] + list(row.get("secondaryLocations") or []):
                address = ((item or {}).get("address") or {}).get("postalAddress") or {}
                code = _country_code(str(address.get("addressCountry", "")))
                if code:
                    countries.append(code)
            result.jobs.append(
                Job(
                    source_id=source.id,
                    company=source.name,
                    title=title,
                    url=row.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{row.get('id')}",
                    apply_url=row.get("applyUrl", ""),
                    location="; ".join(dict.fromkeys(normalize_space(item) for item in locations if item)),
                    description=row.get("descriptionPlain") or row.get("descriptionHtml") or "",
                    req_id=str(row.get("id", "")),
                    employment_type=str(row.get("employmentType") or ""),
                    remote=bool(row.get("isRemote")) or None,
                    country_codes=tuple(dict.fromkeys(countries)),
                    posted_date=str(row.get("publishedAt") or "")[:10],
                )
            )
        return result


class SmartRecruitersAdapter(Adapter):
    name = "smartrecruiters"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        company = source.ats_options["board"]
        seen: set[str] = set()
        for query in source.ats_options.get("queries", INTERN_QUERIES):
            try:
                self._scan_query(source, company, query, seen, result)
            except Exception as error:
                self._query_failed(result, query, error)
        return result

    def _scan_query(self, source: Source, company: str, query: str, seen: set[str], result: AdapterResult) -> None:
        offset = 0
        for _ in range(10):
            url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?" + urlencode(
                {"q": query, "limit": 100, "offset": offset}
            )
            payload = self._get_json(url, result)
            rows = payload.get("content")
            if not isinstance(rows, list):
                raise ValueError(f"SmartRecruiters {company} returned no job list")
            for row in rows:
                identifier = str(row.get("id", ""))
                if identifier in seen:
                    continue
                seen.add(identifier)
                result.raw_count += 1
                title = normalize_space(row.get("name", ""))
                if not self._keep(title):
                    continue
                location = row.get("location") or {}
                result.jobs.append(
                    Job(
                        source_id=source.id,
                        company=source.name,
                        title=title,
                        url=f"https://jobs.smartrecruiters.com/{company}/{identifier}",
                        location=normalize_space(location.get("fullLocation") or ", ".join(filter(None, (location.get("city"), location.get("region"), location.get("country", "").upper())))),
                        req_id=str(row.get("refNumber") or identifier),
                        employment_type=str((row.get("typeOfEmployment") or {}).get("label") or ""),
                        remote=bool(location.get("remote")) or None,
                        country_codes=(str(location.get("country", "")).upper(),) if location.get("country") else (),
                        posted_date=str(row.get("releasedDate") or "")[:10],
                        detail_ref=f"smartrecruiters:{company}:{identifier}",
                    )
                )
            offset += len(rows)
            if not rows or offset >= int(payload.get("totalFound") or 0):
                break


def _walk_workday_facets(facets: Iterable[Any], parameter: str = "") -> Iterable[tuple[str, str, str]]:
    for facet in facets or []:
        if not isinstance(facet, dict):
            continue
        current = facet.get("facetParameter") or parameter
        for value in facet.get("values") or []:
            if not isinstance(value, dict):
                continue
            if "values" in value:
                yield from _walk_workday_facets([value], current)
            elif value.get("id"):
                yield current, normalize_space(value.get("descriptor", "")), str(value["id"])


class WorkdayAdapter(Adapter):
    """Workday CxS search with automatic country and intern facet discovery."""

    name = "workday"
    page_size = 20

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        options = source.ats_options
        host, tenant, site = options["host"], options["tenant"], options["site"]
        base = f"https://{host}/wday/cxs/{tenant}/{site}"
        first = self._post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, result)
        if not isinstance(first.get("jobPostings"), list):
            raise ValueError(f"Workday {tenant}/{site} returned no job list")

        country_facets: dict[str, list[str]] = {}
        intern_facets: dict[str, list[str]] = {}
        for parameter, descriptor, identifier in _walk_workday_facets(first.get("facets")):
            if parameter in {"locationCountry", "Location_Country"} and _country_code(descriptor) in {"CA", "US"}:
                country_facets.setdefault(parameter, []).append(identifier)
            elif (
                parameter not in {"locations", "locationCountry", "Location_Country", "Location_Region_State_Province", "locationRegionStateProvince"}
                and INTERN_FACET.search(descriptor)
                and not NOT_INTERN_FACET.search(descriptor)
            ):
                intern_facets.setdefault(parameter, []).append(identifier)
        base_facets = dict(options.get("facets") or {})
        if options.get("restrict_countries", True):
            base_facets.update(country_facets)

        searches: list[tuple[dict[str, list[str]], str, int]] = []
        if intern_facets:
            # One complete listing per intern-like facet (facets of one parameter OR together).
            for parameter, identifiers in intern_facets.items():
                searches.append(({**base_facets, parameter: identifiers}, "", int(options.get("max_pages", 25))))
        keyword_pages = int(options.get("keyword_pages", 3))
        for query in options.get("queries", INTERN_QUERIES):
            searches.append((base_facets, query, keyword_pages))

        seen: set[str] = set()
        for facets, query, max_pages in searches:
            try:
                self._search(source, base, facets, query, max_pages, seen, result)
            except Exception as error:
                self._query_failed(result, query or "intern facet listing", error)
        return result

    def _search(self, source: Source, base: str, facets: dict, query: str, max_pages: int, seen: set[str], result: AdapterResult) -> None:
        options = source.ats_options
        host, tenant, site = options["host"], options["tenant"], options["site"]
        offset = 0
        total: int | None = None
        for page in range(max_pages):
            payload = self._post_json(
                f"{base}/jobs",
                {"appliedFacets": facets, "limit": self.page_size, "offset": offset, "searchText": query},
                result,
            )
            rows = payload.get("jobPostings")
            if not isinstance(rows, list):
                raise ValueError(f"Workday {tenant}/{site} returned an invalid page")
            if total is None:
                total = int(payload.get("total") or 0)
            for row in rows:
                path = row.get("externalPath", "")
                if not path or path in seen:
                    continue
                seen.add(path)
                result.raw_count += 1
                title = normalize_space(row.get("title", ""))
                if not self._keep(title):
                    continue
                bullets = [str(item) for item in row.get("bulletFields") or []]
                req_id = next((item for item in bullets if re.search(r"\d{3,}", item)), "")
                location = normalize_space(row.get("locationsText", ""))
                office = re.search(r"/job/([^/]+)/", path)
                if re.match(r"^\d+\s+locations?$", location, re.IGNORECASE) and office:
                    location = f"{location}; {office.group(1).replace('-', ' ')}"
                result.jobs.append(
                    Job(
                        source_id=source.id,
                        company=source.name,
                        title=title,
                        url=f"https://{host}/{site}{path}",
                        location=location,
                        req_id=req_id,
                        remote=("remote" in str(row.get("remoteType", "")).casefold()) or None,
                        detail_ref=f"workday:{host}:{tenant}:{site}:{path}",
                    )
                )
            offset += len(rows)
            if not rows or offset >= total:
                break
            if page == max_pages - 1 and not query:
                # A facet listing that does not finish leaves the scan incomplete.
                result.complete = False


class MicrosoftAdapter(Adapter):
    name = "microsoft"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        seen: set[str] = set()
        for query in source.ats_options.get("queries", ("product manager intern", "program manager intern")):
            try:
                self._scan_query(source, query, seen, result)
            except Exception as error:
                self._query_failed(result, query, error)
        return result

    def _scan_query(self, source: Source, query: str, seen: set[str], result: AdapterResult) -> None:
        start = 0
        for _ in range(int(source.ats_options.get("max_pages", 3))):
            url = "https://apply.careers.microsoft.com/api/pcsx/search?" + urlencode(
                {"domain": "microsoft.com", "query": query, "location": "", "start": start, "sort_by": "relevance"}
            )
            payload = self._get_json(url, result)
            data = payload.get("data") or {}
            rows = data.get("positions")
            if not isinstance(rows, list):
                raise ValueError("Microsoft careers API returned no positions")
            for row in rows:
                identifier = str(row.get("id", ""))
                if identifier in seen:
                    continue
                seen.add(identifier)
                result.raw_count += 1
                title = normalize_space(row.get("name", ""))
                if not self._keep(title):
                    continue
                codes = tuple(
                    dict.fromkeys(str(code).split("-")[0].upper() for code in row.get("standardizedLocations") or [] if code)
                )
                result.jobs.append(
                    Job(
                        source_id=source.id,
                        company=source.name,
                        title=title,
                        url=f"https://apply.careers.microsoft.com/careers/job/{identifier}",
                        location="; ".join(row.get("locations") or []),
                        req_id=str(row.get("displayJobId") or identifier),
                        remote=(str(row.get("workLocationOption", "")).casefold() == "remote") or None,
                        country_codes=codes,
                        posted_date=_epoch_date(row.get("postedTs")),
                        detail_ref=f"microsoft:{identifier}",
                    )
                )
            start += len(rows)
            if not rows or start >= int(data.get("count") or 0):
                break


class EightfoldAdapter(Adapter):
    name = "eightfold"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        host, domain = source.ats_options["host"], source.ats_options["domain"]
        seen: set[str] = set()
        for query in source.ats_options.get("queries", ("intern", "internship", "product manager")):
            try:
                self._scan_query(source, host, domain, query, seen, result)
            except Exception as error:
                self._query_failed(result, query, error)
        return result

    def _scan_query(self, source: Source, host: str, domain: str, query: str, seen: set[str], result: AdapterResult) -> None:
        start = 0
        for _ in range(5):
            url = f"https://{host}/api/apply/v2/jobs?" + urlencode({"domain": domain, "query": query, "start": start, "num": 50})
            payload = self._get_json(url, result)
            rows = payload.get("positions")
            if not isinstance(rows, list):
                raise ValueError(f"Eightfold {host} returned no positions")
            for row in rows:
                identifier = str(row.get("id", ""))
                if identifier in seen:
                    continue
                seen.add(identifier)
                result.raw_count += 1
                title = normalize_space(row.get("name") or row.get("posting_name") or "")
                if not self._keep(title):
                    continue
                result.jobs.append(
                    Job(
                        source_id=source.id,
                        company=source.name,
                        title=title,
                        url=row.get("canonicalPositionUrl") or f"https://{host}/careers/job/{identifier}",
                        location="; ".join(row.get("locations") or [row.get("location", "")]),
                        req_id=str(row.get("display_job_id") or row.get("ats_job_id") or identifier),
                        description=row.get("job_description") or "",
                        posted_date=_epoch_date(row.get("t_create")),
                        detail_ref=f"eightfold:{host}:{domain}:{identifier}",
                    )
                )
            start += len(rows)
            if not rows or start >= int(payload.get("count") or 0):
                break


class SuccessFactorsRssAdapter(Adapter):
    """SAP SuccessFactors career sites publish keyword searches as RSS feeds."""

    name = "successfactors"
    ITEM = re.compile(r"<item>(.*?)</item>", re.DOTALL)

    @staticmethod
    def _field(item: str, name: str) -> str:
        match = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", item, re.DOTALL)
        return unescape(match.group(1)).strip() if match else ""

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        host = source.ats_options["host"]
        locale = source.ats_options.get("locale", "en_US")
        seen: set[str] = set()
        for query in source.ats_options.get("queries", INTERN_QUERIES):
            url = f"https://{host}/services/rss/job/?" + urlencode({"locale": locale, "keywords": query})
            result.requests += 1
            try:
                response = self.client.fetch(url, {"Accept": "application/rss+xml, application/xml"})
                if "<rss" not in response.text[:500]:
                    raise ValueError(f"{host} did not return an RSS feed")
            except Exception as error:
                self._query_failed(result, query, error)
                continue
            for item in self.ITEM.findall(response.text):
                link = self._field(item, "link")
                if not link or link in seen:
                    continue
                seen.add(link)
                result.raw_count += 1
                raw_title = normalize_space(self._field(item, "title"))
                location_match = re.search(r"\(([^()]*)\)\s*$", raw_title)
                title = raw_title[: location_match.start()].strip() if location_match else raw_title
                if not self._keep(title):
                    continue
                req = re.search(r"/(\d{6,})/?$", link)
                result.jobs.append(
                    Job(
                        source_id=source.id,
                        company=source.name,
                        title=title,
                        url=link,
                        location=location_match.group(1) if location_match else "",
                        description=self._field(item, "description"),
                        req_id=req.group(1) if req else "",
                        posted_date=self._field(item, "pubDate"),
                    )
                )
        return result


class AtlassianAdapter(Adapter):
    name = "atlassian"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        rows = self._get_json("https://www.atlassian.com/endpoint/careers/listings", result)
        if not isinstance(rows, list):
            raise ValueError("Atlassian listings endpoint returned no list")
        result.raw_count = len(rows)
        for row in rows:
            title = normalize_space(row.get("title", ""))
            if not self._keep(title):
                continue
            post = row.get("portalJobPost") or {}
            result.jobs.append(
                Job(
                    source_id=source.id,
                    company=source.name,
                    title=title,
                    url=post.get("portalUrl") or row.get("applyUrl") or "",
                    apply_url=row.get("applyUrl", ""),
                    location="; ".join(normalize_space(item) for item in row.get("locations") or []),
                    description="\n".join(str(row.get(key) or "") for key in ("overview", "responsibilities", "qualifications")),
                    req_id=str(post.get("id") or row.get("id") or ""),
                    posted_date=str(post.get("updatedDate") or "")[:10],
                )
            )
        return result


SIMPLIFY_SPONSORSHIP = {
    "Does Not Offer Sponsorship": "Simplify reports this role does not offer sponsorship.",
    "U.S. Citizenship is Required": "Simplify reports U.S. citizenship is required.",
    "Offers Sponsorship": "Simplify reports visa sponsorship is available.",
}


class SimplifyAdapter(Adapter):
    """Secondary discovery from the Simplify Summer internship listings data."""

    name = "simplify"

    def scan(self, source: Source) -> AdapterResult:
        result = AdapterResult()
        url = source.ats_options["listings_url"]
        rows = self._get_json(url, result)
        if not isinstance(rows, list):
            raise ValueError("Simplify listings were not a list")
        result.raw_count = len(rows)
        categories = {item.casefold() for item in source.ats_options.get("categories", ("product", "product management"))}
        from .filtering import title_category

        for row in rows:
            if not row.get("active") or row.get("is_visible") is False:
                continue
            title = normalize_space(row.get("title", ""))
            in_category = str(row.get("category", "")).casefold() in categories
            if not self._keep(title) or not (in_category or title_category(title).category in {"core", "adjacent"}):
                continue
            degrees = [str(item) for item in row.get("degrees") or []]
            requirements: list[str] = []
            if degrees and not any("bachelor" in item.casefold() for item in degrees):
                requirements.append(f"Must be pursuing a graduate degree (Simplify lists: {', '.join(degrees)}).")
            result.jobs.append(
                Job(
                    source_id=source.id,
                    company=normalize_space(row.get("company_name", "")),
                    title=title,
                    url=row.get("url", ""),
                    location="; ".join(row.get("locations") or []),
                    requirements=" ".join(requirements),
                    term_hint=", ".join(item for item in row.get("terms") or [] if item and item != "N/A"),
                    sponsorship_hint=SIMPLIFY_SPONSORSHIP.get(str(row.get("sponsorship", "")), ""),
                    posted_date=_epoch_date(row.get("date_posted")),
                )
            )
        return result


ADAPTERS: dict[str, type[Adapter]] = {
    adapter.name: adapter
    for adapter in (
        GreenhouseAdapter,
        LeverAdapter,
        AshbyAdapter,
        SmartRecruitersAdapter,
        WorkdayAdapter,
        MicrosoftAdapter,
        EightfoldAdapter,
        SuccessFactorsRssAdapter,
        AtlassianAdapter,
        SimplifyAdapter,
    )
}


# --- Description resolution ----------------------------------------------------------


@dataclass(frozen=True)
class DescriptionResult:
    text: str
    location: str = ""
    employment_type: str = ""
    close_date: str = ""
    closed: bool = False
    req_id: str = ""
    country_codes: tuple[str, ...] = ()
    remote: bool | None = None
    note: str = ""


JSON_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


class DescriptionFetcher:
    """Fetch the full posting text for jobs whose listing omitted it."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client
        self._ashby_boards: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def fetch(self, job: Job) -> DescriptionResult:
        ref = job.detail_ref
        if ref.startswith("workday:"):
            _, host, tenant, site, path = ref.split(":", 4)
            return self._workday(host, tenant, site, path)
        if ref.startswith("smartrecruiters:"):
            _, company, identifier = ref.split(":", 2)
            return self._smartrecruiters(company, identifier)
        if ref.startswith("microsoft:"):
            return self._microsoft(ref.split(":", 1)[1])
        if ref.startswith("eightfold:"):
            _, host, domain, identifier = ref.split(":", 3)
            payload = json.loads(self.client.fetch(f"https://{host}/api/apply/v2/jobs/{identifier}?domain={quote(domain)}", {"Accept": "application/json"}).text)
            return DescriptionResult(text=str(payload.get("job_description") or ""))
        return self.fetch_url(job.url)

    def fetch_url(self, url: str) -> DescriptionResult:
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        path = parts.path
        greenhouse = re.search(r"^/([^/]+)/jobs/(\d+)", path) if host.endswith("greenhouse.io") else None
        gh_jid = parse_qs(parts.query).get("gh_jid", [""])[0]
        if greenhouse:
            return self._greenhouse(greenhouse.group(1), greenhouse.group(2))
        if host == "jobs.lever.co":
            match = re.match(r"^/([^/]+)/([0-9a-f-]{36})", path)
            if match:
                row = json.loads(self.client.fetch(f"https://api.lever.co/v0/postings/{match.group(1)}/{match.group(2)}", {"Accept": "application/json"}).text)
                lists = "\n".join(f"{item.get('text', '')}\n{item.get('content', '')}" for item in row.get("lists") or [])
                return DescriptionResult(
                    text="\n".join(filter(None, (row.get("descriptionPlain"), lists, row.get("additionalPlain")))),
                    location=(row.get("categories") or {}).get("location", ""),
                    employment_type=(row.get("categories") or {}).get("commitment", ""),
                )
        if host == "jobs.ashbyhq.com":
            match = re.match(r"^/([^/]+)/([0-9a-f-]{36})", path)
            if match:
                return self._ashby(match.group(1), match.group(2))
        if host.endswith(".myworkdayjobs.com"):
            match = re.match(r"^(?:/[a-z]{2}-[A-Z]{2})?/([^/]+)(/job/.+)$", path)
            if match:
                tenant = host.split(".")[0]
                return self._workday(host, tenant, match.group(1), match.group(2))
        if host == "jobs.smartrecruiters.com":
            match = re.match(r"^/([^/]+)/(\d+)", path)
            if match:
                return self._smartrecruiters(match.group(1), match.group(2))
        if gh_jid and gh_jid.isdigit():
            board = parse_qs(parts.query).get("gh_src", [""])[0]
            if board and not board.isdigit():
                return self._greenhouse(board, gh_jid)
        return self._generic(url)

    def _greenhouse(self, board: str, identifier: str) -> DescriptionResult:
        row = json.loads(self.client.fetch(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{identifier}", {"Accept": "application/json"}).text)
        return DescriptionResult(
            text=unescape(row.get("content") or ""),
            location=(row.get("location") or {}).get("name", ""),
            req_id=str(row.get("requisition_id") or ""),
        )

    def _ashby(self, board: str, identifier: str) -> DescriptionResult:
        with self._lock:
            jobs = self._ashby_boards.get(board)
        if jobs is None:
            payload = json.loads(self.client.fetch(f"https://api.ashbyhq.com/posting-api/job-board/{board}", {"Accept": "application/json"}).text)
            jobs = {str(row.get("id")): row for row in payload.get("jobs") or []}
            with self._lock:
                self._ashby_boards[board] = jobs
        row = jobs.get(identifier)
        if row is None:
            return DescriptionResult(text="", closed=True, note="no longer listed on the Ashby board")
        return DescriptionResult(
            text=row.get("descriptionPlain") or "",
            location=row.get("location", ""),
            employment_type=row.get("employmentType", ""),
            remote=bool(row.get("isRemote")) or None,
        )

    def _workday(self, host: str, tenant: str, site: str, path: str) -> DescriptionResult:
        payload = json.loads(self.client.fetch(f"https://{host}/wday/cxs/{tenant}/{site}{path}", {"Accept": "application/json"}).text)
        info = payload.get("jobPostingInfo") or {}
        locations = [info.get("location", "")] + list(info.get("additionalLocations") or [])
        country = ((info.get("jobRequisitionLocation") or {}).get("country") or {}).get("alpha2Code", "")
        if not country:
            country = _country_code(str((info.get("country") or {}).get("descriptor", "")))
        return DescriptionResult(
            text=info.get("jobDescription") or "",
            location="; ".join(filter(None, locations)),
            employment_type=str(info.get("timeType") or ""),
            close_date=str(info.get("endDate") or ""),
            closed=info.get("canApply") is False or info.get("posted") is False,
            req_id=str(info.get("jobReqId") or ""),
            country_codes=(country,) if country else (),
            remote=("remote" in str(info.get("remoteType", "")).casefold()) or None,
        )

    def _smartrecruiters(self, company: str, identifier: str) -> DescriptionResult:
        payload = json.loads(self.client.fetch(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{identifier}", {"Accept": "application/json"}).text)
        sections = ((payload.get("jobAd") or {}).get("sections") or {})
        text = "\n".join(
            f"{(sections.get(key) or {}).get('title', '')}\n{(sections.get(key) or {}).get('text', '')}"
            for key in ("jobDescription", "qualifications", "additionalInformation")
        )
        return DescriptionResult(
            text=text,
            employment_type=str((payload.get("typeOfEmployment") or {}).get("label") or ""),
            closed=payload.get("active") is False,
        )

    def _microsoft(self, identifier: str) -> DescriptionResult:
        payload = json.loads(
            self.client.fetch(
                f"https://apply.careers.microsoft.com/api/pcsx/position_details?position_id={identifier}&domain=microsoft.com&hl=en",
                {"Accept": "application/json"},
            ).text
        )
        data = payload.get("data") or {}
        return DescriptionResult(
            text=str(data.get("jobDescription") or ""),
            employment_type=" ".join(str(data.get(key) or "") for key in ("efcustomTextEmploymentType", "efcustomTextRoletype")),
        )

    def _generic(self, url: str) -> DescriptionResult:
        if not url.startswith(("http://", "https://")):
            return DescriptionResult(text="", note="no fetchable URL")
        if not self.client.allowed_by_robots(url):
            return DescriptionResult(text="", note="robots.txt disallows fetching this page")
        response = self.client.fetch(url, {"Accept": "text/html,application/xhtml+xml"})
        for block in JSON_LD.findall(response.text):
            try:
                payload = json.loads(block.strip())
            except json.JSONDecodeError:
                continue
            for item in payload if isinstance(payload, list) else payload.get("@graph", [payload]) if isinstance(payload, dict) else []:
                if isinstance(item, dict) and item.get("@type") in {"JobPosting", ["JobPosting"]}:
                    locations = item.get("jobLocation") or []
                    if isinstance(locations, dict):
                        locations = [locations]
                    names = []
                    for location in locations:
                        address = (location or {}).get("address") or {}
                        if isinstance(address, dict):
                            names.append(", ".join(filter(None, (address.get("addressLocality"), address.get("addressRegion"), str(address.get("addressCountry") or "")))))
                    employment = item.get("employmentType") or ""
                    return DescriptionResult(
                        text=str(item.get("description") or ""),
                        location="; ".join(name for name in names if name),
                        employment_type=" ".join(employment) if isinstance(employment, list) else str(employment),
                        close_date=str(item.get("validThrough") or "")[:10],
                        remote=(item.get("jobLocationType") == "TELECOMMUTE") or None,
                    )
        text = plain_text(re.sub(r"<(?:nav|header|footer)\b.*?</(?:nav|header|footer)>", " ", response.text, flags=re.IGNORECASE | re.DOTALL))
        if len(text) < 400:
            return DescriptionResult(text="", note="page did not contain a readable description")
        return DescriptionResult(text=text[:20_000])


def enrich(job: Job, detail: DescriptionResult) -> Job:
    """Merge detail-page facts into a listing job without discarding listing data."""
    location = job.location
    if detail.location and (not location or re.match(r"^\d+\s+locations?", location, re.IGNORECASE)):
        location = detail.location
    elif detail.location and detail.location not in location:
        location = f"{location}; {detail.location}"
    return replace(
        job,
        description=detail.text or job.description,
        location=location,
        employment_type=job.employment_type or detail.employment_type,
        close_date=job.close_date or detail.close_date,
        req_id=job.req_id or detail.req_id,
        country_codes=tuple(dict.fromkeys(job.country_codes + detail.country_codes)),
        remote=job.remote if job.remote is not None else detail.remote,
    )


def make_adapter(name: str, client: HttpClient, role_filter: RoleFilter) -> Adapter:
    try:
        return ADAPTERS[name](client, role_filter)
    except KeyError:
        raise ValueError(f"Unknown ats adapter: {name}") from None


__all__ = [
    "ADAPTERS",
    "AdapterResult",
    "DescriptionFetcher",
    "DescriptionResult",
    "enrich",
    "make_adapter",
]
