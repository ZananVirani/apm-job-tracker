from dataclasses import replace
import json
from pathlib import Path
import unittest

from apm_notifier.config import load_sources
from apm_notifier.filtering import RoleFilter
from apm_notifier.models import FetchResult
from apm_notifier.monitor import Monitor
from apm_notifier.tiktok import normalize_search_response


API = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts"


def api_response(rows, count, code=0):
    return FetchResult(API, API, "application/json", json.dumps({
        "code": code, "data": {"job_post_list": rows, "count": count},
    }))


def posting(identifier, title, city="San Jose", country="United States of America", requirements="Minimum Qualifications:\n- Completing a Bachelor's degree."):
    return {
        "id": identifier, "title": title, "requirement": requirements,
        "city_info": {"name": None, "en_name": city, "i18n_name": city,
                      "parent": {"name": None, "en_name": country, "location_type": 1}},
    }


class TikTokTests(unittest.TestCase):
    def setUp(self):
        self.source = next(s for s in load_sources(Path(__file__).resolve().parent.parent / "config/sources.json") if s.id == "tiktok")
        self.filter = RoleFilter(2027)

    def test_configuration_scans_the_full_campus_internship_feed(self):
        self.assertFalse(self.source.render)
        self.assertTrue(self.source.paginate)
        self.assertTrue(self.source.priority)
        bodies = [json.loads(body) for body in self.source.request_bodies]
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["recruitment_id_list"], ["2"])
        self.assertFalse(bodies[0]["keyword"] or bodies[0]["location_code_list"] or bodies[0]["job_category_id_list"])

    def test_all_pages_are_classified_with_inline_qualifications(self):
        requirements = "Minimum Qualifications:\n- Currently pursuing a Bachelor's degree, graduating in 2028 or later."
        rows = [posting(str(i), "Senior Product Manager") for i in range(12)]
        rows += [
            posting("pm", "Product Manager Intern (TikTok Shop) - 2027 Summer", requirements=requirements),
            posting("toronto", "Product Strategy Intern - 2027 Summer", city="Toronto", country="Canada", requirements=requirements),
            posting("swe", "Software Engineer Intern - 2027 Summer", requirements=requirements),
            posting("masters", "Product Manager Intern - 2027 Summer", requirements="Minimum Qualifications:\n- Currently pursuing a Master's degree in Business."),
            posting("outside", "Product Manager Intern - 2027 Summer", city="Singapore", country="Singapore", requirements=requirements),
            posting("old", "Product Manager Intern - 2026 Summer"),
        ]

        class Client:
            timeout_seconds = 5

            def __init__(self):
                self.offsets = []

            def fetch(self, url, extra_headers=None, method="GET", body=""):
                # Qualifications come from the API itself; no detail round-trips.
                if url != API:
                    raise AssertionError("Unexpected job detail fetch")
                offset = json.loads(body)["offset"]
                self.offsets.append(offset)
                return api_response(rows[offset:offset + 12], len(rows))

        client = Client()
        outcome = Monitor((self.source,), client, self.filter, None, None, 1, True)._check_source(self.source)
        self.assertTrue(outcome.result.succeeded, outcome.result.errors)
        self.assertEqual(client.offsets, [0, 12])
        decisions = {item.job.url.rsplit("/", 1)[-1]: item.result.decision for item in outcome.processed}
        # Senior roles and 2026 cohorts never reach the classifier.
        self.assertEqual(
            decisions,
            {"pm": "REVIEW", "toronto": "ACCEPT", "swe": "REJECT", "masters": "REJECT", "outside": "REJECT"},
        )

    def test_api_errors_are_not_healthy_empty_results(self):
        for payload in ({"code": 1, "data": {"job_post_list": [], "count": 0}},
                        {"code": 0, "data": None},
                        {"code": 0, "data": {"job_post_list": [], "count": None}},
                        {"code": 0, "data": {"job_post_list": [{}], "count": 1}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                normalize_search_response(FetchResult(API, API, "application/json", json.dumps(payload)))

    def test_incomplete_or_repeated_pages_report_failure(self):
        source = replace(self.source, urls=(API,), request_bodies=(self.source.request_bodies[0],))
        for repeated in (False, True):
            class Client:
                timeout_seconds = 5
                def fetch(self, url, extra_headers=None, method="GET", body=""):
                    rows = [posting("one", "Product Growth Intern - 2027 Summer")]
                    if json.loads(body)["offset"] and not repeated:
                        rows = []
                    return api_response(rows, 2)
            outcome = Monitor((source,), Client(), self.filter, None, None, 1, True)._check_source(source)
            self.assertFalse(outcome.result.succeeded)
            self.assertEqual(len(outcome.processed), 1)

    def test_explicit_empty_api_result_is_healthy(self):
        class Client:
            timeout_seconds = 5
            def fetch(self, url, extra_headers=None, method="GET", body=""):
                return api_response([], 0)
        outcome = Monitor((self.source,), Client(), self.filter, None, None, 1, True)._check_source(self.source)
        self.assertTrue(outcome.result.succeeded, outcome.result.errors)
        self.assertEqual(outcome.processed, [])
