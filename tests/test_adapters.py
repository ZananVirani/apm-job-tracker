import json
import unittest

from apm_notifier.adapters import (
    DescriptionFetcher,
    GreenhouseAdapter,
    SimplifyAdapter,
    SuccessFactorsRssAdapter,
    WorkdayAdapter,
)
from apm_notifier.filtering import RoleFilter
from apm_notifier.models import FetchResult, Source


class RecordingClient:
    timeout_seconds = 5

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[tuple[str, str]] = []

    def fetch(self, url, extra_headers=None, method="GET", body=""):
        self.requests.append((url, body))
        text = self.handler(url, json.loads(body) if body else None)
        if isinstance(text, Exception):
            raise text
        return FetchResult(url, url, "application/json", text if isinstance(text, str) else json.dumps(text))


FACETS = [
    {"facetParameter": "workerSubType", "values": [
        {"descriptor": "Intern (Fixed Term)", "id": "intern-id", "count": 4},
        {"descriptor": "Internal Audit", "id": "audit-id", "count": 9},
        {"descriptor": "Regular", "id": "regular-id", "count": 400},
    ]},
    {"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locationCountry", "values": [
            {"descriptor": "Canada", "id": "ca-id", "count": 50},
            {"descriptor": "United States of America", "id": "us-id", "count": 70},
            {"descriptor": "United Kingdom", "id": "uk-id", "count": 20},
        ]},
    ]},
]


class WorkdayAdapterTests(unittest.TestCase):
    def test_discovers_intern_and_country_facets_and_keeps_partial_results(self) -> None:
        def handler(url, body):
            if body["limit"] == 1:
                return {"total": 500, "jobPostings": [], "facets": FACETS}
            if body["searchText"] == "co-op":
                return RuntimeError("HTTP Error 500")
            if body["searchText"]:
                return {"total": 0, "jobPostings": []}
            return {"total": 2, "jobPostings": [
                {"title": "Product Manager Intern", "externalPath": "/job/Toronto/PM-Intern_R1", "locationsText": "Toronto, ON", "bulletFields": ["R_1508413", "Campus"]},
                {"title": "Senior Product Manager", "externalPath": "/job/Toronto/Senior_R2", "locationsText": "Toronto, ON", "bulletFields": ["R2"]},
            ]}

        client = RecordingClient(handler)
        source = Source("x", "X", (), "", ats="workday", ats_options={"host": "x.wd3.myworkdayjobs.com", "tenant": "x", "site": "Ext"})
        result = WorkdayAdapter(client, RoleFilter(2027)).scan(source)
        facet_search = json.loads(client.requests[1][1])
        self.assertEqual(facet_search["appliedFacets"], {"locationCountry": ["ca-id", "us-id"], "workerSubType": ["intern-id"]})
        self.assertEqual([job.title for job in result.jobs], ["Product Manager Intern"])
        self.assertEqual(result.jobs[0].req_id, "R_1508413")
        self.assertEqual(result.jobs[0].detail_ref, "workday:x.wd3.myworkdayjobs.com:x:Ext:/job/Toronto/PM-Intern_R1")
        self.assertEqual(result.raw_count, 2)
        self.assertFalse(result.complete)
        self.assertTrue(any("co-op" in error for error in result.errors))

    def test_detail_fetch_reports_closed_and_country(self) -> None:
        detail = {"jobPostingInfo": {
            "jobDescription": "<p>Write PRDs</p>", "location": "Toronto, ON", "canApply": False,
            "endDate": "2026-11-22", "jobReqId": "R1", "timeType": "Full time",
            "jobRequisitionLocation": {"country": {"alpha2Code": "CA"}},
        }}
        fetcher = DescriptionFetcher(RecordingClient(lambda url, body: detail))
        result = fetcher.fetch_url("https://x.wd3.myworkdayjobs.com/en-US/Ext/job/Toronto/PM-Intern_R1")
        self.assertTrue(result.closed)
        self.assertEqual((result.country_codes, result.close_date, result.req_id), (("CA",), "2026-11-22", "R1"))


class OtherAdapterTests(unittest.TestCase):
    def test_greenhouse_includes_descriptions_and_requisition(self) -> None:
        payload = {"jobs": [
            {"id": 7, "title": "Product Manager Intern", "absolute_url": "https://job-boards.greenhouse.io/b/jobs/7",
             "location": {"name": "Toronto"}, "content": "&lt;p&gt;Own the roadmap&lt;/p&gt;", "requisition_id": "REQ-7",
             "first_published": "2026-11-01T00:00:00Z"},
            {"id": 8, "title": "Staff Engineer", "absolute_url": "https://job-boards.greenhouse.io/b/jobs/8", "location": {"name": "Toronto"}},
        ]}
        client = RecordingClient(lambda url, body: payload)
        result = GreenhouseAdapter(client, RoleFilter(2027)).scan(Source("b", "B", (), "", ats="greenhouse", ats_options={"board": "b"}))
        self.assertIn("content=true", client.requests[0][0])
        self.assertEqual(result.raw_count, 2)
        (job,) = result.jobs
        self.assertEqual((job.req_id, job.posted_date), ("REQ-7", "2026-11-01"))
        self.assertIn("<p>Own the roadmap</p>", job.description)

    def test_successfactors_rss_parses_title_location_and_description(self) -> None:
        rss = """<?xml version="1.0"?><rss version='2.0'><channel>
        <item><title><![CDATA[Product Analyst Co-op - Summer 2027 (Toronto, ON, CA, M5H 1J9)]]></title>
        <link>https://careers.example.com/job/Toronto-Product-Analyst/603109417/</link>
        <description><![CDATA[<p>Analyze product metrics.</p>]]></description><pubDate>Tue, 10 Nov 2026</pubDate></item>
        </channel></rss>"""
        client = RecordingClient(lambda url, body: rss)
        source = Source("s", "S", (), "", ats="successfactors", ats_options={"host": "careers.example.com", "queries": ["co-op"]})
        (job,) = SuccessFactorsRssAdapter(client, RoleFilter(2027)).scan(source).jobs
        self.assertEqual(job.title, "Product Analyst Co-op - Summer 2027")
        self.assertEqual(job.location, "Toronto, ON, CA, M5H 1J9")
        self.assertEqual(job.req_id, "603109417")
        self.assertIn("product metrics", job.description)

    def test_simplify_maps_terms_sponsorship_and_degrees(self) -> None:
        rows = [
            {"company_name": "Acme", "title": "Product Manager Intern", "active": True, "is_visible": True, "category": "Product",
             "terms": ["Summer 2027"], "url": "https://acme.example/jobs/1", "locations": ["Austin, TX"],
             "sponsorship": "Does Not Offer Sponsorship", "degrees": ["Master's", "MBA"], "date_posted": 1794441600},
            {"company_name": "Acme", "title": "Software Engineer Intern", "active": True, "category": "Software", "url": "https://acme.example/jobs/2"},
            {"company_name": "Acme", "title": "Product Manager Intern", "active": False, "category": "Product", "url": "https://acme.example/jobs/3"},
        ]
        client = RecordingClient(lambda url, body: rows)
        source = Source("simplify", "Simplify", (), "", ats="simplify", ats_options={"listings_url": "https://raw.example/listings.json"}, aggregator=True)
        (job,) = SimplifyAdapter(client, RoleFilter(2027)).scan(source).jobs
        self.assertEqual((job.company, job.term_hint), ("Acme", "Summer 2027"))
        self.assertIn("does not offer sponsorship", job.sponsorship_hint)
        self.assertIn("graduate degree", job.requirements)


if __name__ == "__main__":
    unittest.main()
