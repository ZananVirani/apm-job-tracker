import json
import unittest

from apm_notifier.extract import extract_jobs, response_has_job_signal
from apm_notifier.filtering import RoleFilter
from apm_notifier.models import Source


class ExtractJobsTests(unittest.TestCase):
    def test_api_failure_with_null_jobs_is_not_a_healthy_zero(self) -> None:
        self.assertFalse(response_has_job_signal('{"data":{"jobs":null},"errors":[{"message":"404"}]}', "application/json"))
        self.assertTrue(response_has_job_signal('{"data":{"jobs":[]}}', "application/json"))

    def test_disney_card_does_not_treat_posting_date_as_cohort_year(self) -> None:
        html = '''<ul><li><a href="/en/job/orlando/apm/1/2" data-job-id="2">
        <h2>Associate Product Manager</h2><span class="job-brand">Disney</span>
        <span class="job-location">Orlando, Florida</span><span class="job-date-posted">Oct. 8, 2026</span></a></li></ul>'''
        source = Source("disney", "Disney", ("https://www.disneycareers.com/en/search-jobs/product",), "https://www.disneycareers.com")
        jobs = extract_jobs(html, "text/html", source, source.urls[0], RoleFilter(2027))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Associate Product Manager")

    def test_community_table_preserves_employer_and_posting_links(self) -> None:
        html = '''<table><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th></tr>
        <tr><td>🔥 Example</td><td>Junior Product Owner</td><td>London<br>UK</td><td><a href="https://example.com/jobs/1?ref=Simplify&amp;utm_source=test">Apply</a></td></tr>
        <tr><td>↳</td><td>Associate Product Builder</td><td>Toronto</td><td><a href="https://example.com/jobs/2">Apply</a></td></tr>
        <tr><td>Closed</td><td>Associate Product Manager</td><td>Toronto</td><td>🔒</td></tr></table>'''
        source = Source("newgrad-community-backcheck", "Tracker", ("https://example.com/list",), "https://example.com/list")
        self.assertTrue(response_has_job_signal(html, "text/plain"))
        jobs = extract_jobs(html, "text/plain", source, source.urls[0], RoleFilter(2027))
        self.assertEqual(len(jobs), 2)
        self.assertEqual([j.company for j in jobs], ["Example", "Example"])
        self.assertNotIn("ref=Simplify", jobs[0].url)

    def test_checks_secondary_country_and_workday_office(self) -> None:
        source = Source("example", "Example", ("https://example.wd1.myworkdayjobs.com/jobs",), "https://example.com", url_template="https://example.wd1.myworkdayjobs.com/Jobs/{path}")
        payload = {"jobPostings": [
            {"title": "Graduate Product Owner", "externalPath": "/job/London-England/owner_1", "locationsText": "2 Locations"},
            {"title": "Associate Product Manager", "externalPath": "/job/Singapore/pm_2", "location": "Singapore", "jobLocation": {"address": {"addressLocality": "Small Town", "addressCountry": "GB"}}},
        ]}
        jobs = extract_jobs(json.dumps(payload), "application/json", source, source.urls[0], RoleFilter(2027))
        self.assertEqual(len(jobs), 2)
        self.assertIn("London", jobs[0].location)
        self.assertIn("GB", jobs[1].location)

    def test_extracts_dayforce_posting_id_and_all_locations(self) -> None:
        source = Source(
            id="questrade", name="Questrade", urls=("https://jobs.dayforcehcm.com/api/geo/qfg/jobposting/search",),
            career_url="https://jobs.dayforcehcm.com/en-US/qfg/CANDIDATEPORTAL",
            url_template="https://jobs.dayforcehcm.com/en-US/qfg/CANDIDATEPORTAL/jobs/{id}",
        )
        payload = {"jobPostings": [{
            "jobPostingId": 17940, "jobReqId": 4309, "jobTitle": "Associate Product Manager",
            "postingLocations": [{"formattedAddress": "Toronto, ON, Canada"}, {"formattedAddress": "North York, ON, Canada"}],
        }], "maxCount": 1}
        jobs = extract_jobs(json.dumps(payload), "application/json", source, source.urls[0], RoleFilter(2027))
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].url.endswith("/jobs/17940"))
        self.assertIn("North York", jobs[0].location)

    def setUp(self) -> None:
        self.source = Source(
            id="example",
            name="Example",
            urls=("https://jobs.example.com/search",),
            career_url="https://jobs.example.com/",
        )
        self.filter = RoleFilter(2027)

    def test_extracts_matching_anchor_only(self) -> None:
        html = """
        <html><body>
          <a href="/jobs/123">Product Manager Intern — Summer 2027 — New York</a>
          <a href="/jobs/456">Senior Product Manager</a>
        </body></html>
        """
        jobs = extract_jobs(html, "text/html", self.source, "https://jobs.example.com/search", self.filter)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, "https://jobs.example.com/jobs/123")

    def test_extracts_nested_json_jobs(self) -> None:
        payload = {
            "results": [
                {
                    "jobTitle": "Product Marketing Intern 2027",
                    "externalPath": "/jobs/pm-7",
                    "location": {"name": "Toronto"},
                },
                {"jobTitle": "Senior Product Marketing Manager", "externalPath": "/jobs/pm-8"},
            ]
        }
        jobs = extract_jobs(
            json.dumps(payload),
            "application/json",
            self.source,
            "https://jobs.example.com/api/search",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertIn("Toronto", jobs[0].location)
        self.assertEqual(jobs[0].url, "https://jobs.example.com/jobs/pm-7")

    def test_extracts_product_marketing_intern_but_not_generic_marketing_role(self) -> None:
        source = Source(
            id="mastercard",
            name="Mastercard",
            urls=("https://careers.example.com/search",),
            career_url="https://careers.example.com/",
        )
        payload = {
            "jobs": [
                {
                    "title": "Partner Marketing Specialist",
                    "applyUrl": "https://careers.example.com/jobs/R-288655",
                    "location": "Toronto, Ontario, Canada",
                },
                {
                    "title": "Product Marketing Intern",
                    "applyUrl": "https://careers.example.com/jobs/R-288656",
                    "location": "Toronto, Ontario, Canada",
                    "description": "Support go-to-market launches for new features.",
                    "requisitionId": "R-288656",
                },
            ]
        }
        jobs = extract_jobs(
            json.dumps(payload),
            "application/json",
            source,
            "https://careers.example.com/search",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Marketing Intern")
        self.assertEqual(jobs[0].req_id, "R-288656")
        self.assertIn("go-to-market", jobs[0].description)

    def test_extracts_ashby_embedded_job_board(self) -> None:
        source = Source(
            id="lime",
            name="Lime",
            urls=("https://jobs.ashbyhq.com/Lime?embed=js",),
            career_url="https://www.li.me/about/careers",
            url_template="https://jobs.ashbyhq.com/Lime/{id}",
        )
        html = """
        <script>
        window.__appData = {"jobBoard":{"jobPostings":[
          {"id":"lime-123","title":"Marketing Intern — Summer 2027",
           "locationName":"Toronto, Canada"}
        ]}};
        </script>
        """
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            source,
            "https://jobs.ashbyhq.com/Lime?embed=js",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Toronto, Canada")
        self.assertEqual(jobs[0].url, "https://jobs.ashbyhq.com/Lime/lime-123")

    def test_extracts_microsoft_positions_api_job(self) -> None:
        payload = {
            "data": {
                "positions": [
                    {
                        "id": 1970393556953113,
                        "name": "Product Manager: Internship Opportunities",
                        "locations": ["United States, Washington, Redmond"],
                        "positionUrl": "/careers/job/1970393556953113",
                    },
                    {
                        "id": 1970393556928915,
                        "name": "Senior Product Manager / Product Manager",
                        "locations": ["United States, Washington, Redmond"],
                        "positionUrl": "/careers/job/1970393556928915",
                    },
                ]
            }
        }
        jobs = extract_jobs(
            json.dumps(payload),
            "application/json",
            self.source,
            "https://apply.careers.microsoft.com/api/pcsx/search",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Manager: Internship Opportunities")
        self.assertEqual(jobs[0].location, "United States, Washington, Redmond")
        self.assertEqual(
            jobs[0].url,
            "https://apply.careers.microsoft.com/careers/job/1970393556953113",
        )

    def test_cleans_microsoft_rendered_job_card(self) -> None:
        html = """
        <a href="/careers/job/1970393556953113">
          Product Manager: Internship Opportunities
          United States, Washington, Redmond
          Posted 16 hours ago
        </a>
        """
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://apply.careers.microsoft.com/careers?query=product",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Manager: Internship Opportunities")
        self.assertEqual(jobs[0].location, "United States, Washington, Redmond")
        self.assertEqual(
            jobs[0].url,
            "https://apply.careers.microsoft.com/careers/job/1970393556953113",
        )

    def test_keeps_foreign_roles_for_the_classifier_to_reject(self) -> None:
        payload = {
            "results": [
                {
                    "jobTitle": "Product Marketing Intern 2027",
                    "externalPath": "/jobs/singapore",
                    "location": "SG, Singapore",
                },
                {
                    "jobTitle": "Product Marketing Intern 2027",
                    "externalPath": "/jobs/canada",
                    "location": "Toronto, Canada",
                },
            ]
        }
        jobs = extract_jobs(
            json.dumps(payload),
            "application/json",
            self.source,
            "https://jobs.example.com/api/search",
            self.filter,
        )
        # Location eligibility is decided (and audited) by the classifier, not the extractor.
        self.assertEqual(
            sorted(job.url for job in jobs),
            ["https://jobs.example.com/jobs/canada", "https://jobs.example.com/jobs/singapore"],
        )

    def test_extracts_json_ld_job_posting(self) -> None:
        payload = {
            "@context": "https://schema.org",
            "@type": "JobPosting",
            "title": "Associate Product Manager",
            "url": "https://jobs.example.com/jobs/apm",
            "jobLocation": {"address": {"addressLocality": "New York"}},
        }
        html = f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        jobs = extract_jobs(html, "text/html", self.source, "https://jobs.example.com", self.filter)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "New York")

    def test_extracts_eightfold_code_block(self) -> None:
        payload = {
            "positions": [
                {
                    "name": "Product Manager Intern 2027",
                    "canonicalPositionUrl": "https://jobs.example.com/jobs/ef-1",
                    "location": "Remote - US",
                }
            ]
        }
        encoded = json.dumps(payload).replace('"', "&#34;")
        html = f'<code id="smartApplyData" style="display:none">{encoded}</code>'
        jobs = extract_jobs(html, "text/html", self.source, "https://jobs.example.com", self.filter)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, "https://jobs.example.com/jobs/ef-1")

    def test_extracts_phenom_assignment(self) -> None:
        payload = {
            "data": {
                "jobs": [
                    {
                        "title": "Product Marketing Intern — 2027",
                        "applyUrl": "https://jobs.example.com/jobs/ph-1",
                        "city": "Toronto",
                    }
                ]
            }
        }
        html = f"<script>var phApp = {{}}; phApp.ddo = {json.dumps(payload)}; phApp.experimentData = {{}};</script>"
        jobs = extract_jobs(html, "text/html", self.source, "https://jobs.example.com", self.filter)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, "https://jobs.example.com/jobs/ph-1")

    def test_extracts_lever_location_from_categories(self) -> None:
        payload = [
            {
                "text": "Product Marketing Intern — Summer 2027",
                "hostedUrl": "https://jobs.lever.co/example/pm-1",
                "categories": {"team": "Marketing", "location": "London, UK"},
            }
        ]
        jobs = extract_jobs(
            json.dumps(payload),
            "application/json",
            self.source,
            "https://api.lever.co/v0/postings/example",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "London, UK")

    def test_extracts_workday_job_with_public_url_template(self) -> None:
        source = Source(
            id="salesforce",
            name="Salesforce",
            urls=("https://example.wd/jobs",),
            career_url="https://example.com/careers",
            url_template="https://example.wd/Careers/{path}",
        )
        payload = {
            "jobPostings": [
                {
                    "title": "Associate Product Manager (starting summer 2027)",
                    "externalPath": "/job/San-Francisco/APM_123",
                    "locationsText": "California - San Francisco",
                }
            ]
        }
        jobs = extract_jobs(
            json.dumps(payload),
            "application/json",
            source,
            "https://example.wd/jobs",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, "https://example.wd/Careers/job/San-Francisco/APM_123")

    def test_extracts_company_from_community_markdown_table(self) -> None:
        markdown = """
        | Company | Role | Location | Apply | Added |
        | --- | --- | --- | --- | --- |
        | Databricks | Product Management Intern (Summer 2027) | San Francisco, CA | [apply](https://example.com/db) | 2026-07-16 |
        | Salesforce | Associate Product Manager Intern 🔒 | San Francisco, CA | [apply](https://example.com/sf) | - |
        | Other | Software Engineer Intern | Seattle, WA | [apply](https://example.com/swe) | - |
        """
        jobs = extract_jobs(markdown, "text/plain", self.source, self.source.career_url, self.filter)
        by_company = {job.company: job for job in jobs}
        # Closed (🔒) rows are skipped; every open intern row is kept for classification.
        self.assertEqual(set(by_company), {"Databricks", "Other"})
        self.assertEqual(by_company["Databricks"].url, "https://example.com/db")

    def test_shopify_careers_links_are_job_signals(self) -> None:
        html = '<a href="/careers/product-manager-intern_62d779d3-51c8-4b0a-ab2e-1a440276bb37">Product Manager Intern Remote - Americas</a>'
        self.assertTrue(response_has_job_signal(html, "text/html"))

    def test_job_signal_rejects_empty_spa_shell(self) -> None:
        self.assertFalse(response_has_job_signal("<html><a href='/careers'>Careers</a></html>", "text/html"))
        self.assertTrue(
            response_has_job_signal(
                "<html><a href='/jobs/123'>Product Manager Intern</a></html>",
                "text/html",
            )
        )
        self.assertTrue(response_has_job_signal('{"jobs": []}', "application/json"))

    def test_extracts_rendered_yc_product_card(self) -> None:
        html = """
        <div class="flex cursor-pointer flex-col">
          <div><img class="logo" alt="Dedalus Labs"></div>
          <div><a href="/jobs/98000" target="job">Product Manager Summer 2027 Intern</a></div>
          <div><p class="job-details">
            <span>Intern</span><span>San Francisco, CA, US</span><span>$4K monthly</span>
          </p></div>
        </div>
        """
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://www.workatastartup.com/jobs/l/product-manager",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].company, "Dedalus Labs")
        self.assertEqual(jobs[0].location, "San Francisco, CA, US")
        self.assertEqual(jobs[0].url, "https://www.workatastartup.com/jobs/98000")

    def test_extracts_rendered_meta_job_card(self) -> None:
        html = """
        <a href="/profile/job_details/123" target="_blank">
          <div><h3>Rotational Product Manager — 2027</h3></div>
          <div><span>Menlo Park, CA</span><span>Product Management</span></div>
        </a>
        """
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://www.metacareers.com/jobsearch/",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Rotational Product Manager — 2027")
        self.assertEqual(jobs[0].location, "Menlo Park, CA")
        self.assertEqual(jobs[0].url, "https://www.metacareers.com/profile/job_details/123")

    def test_extracts_clean_tiktok_graduate_job_card(self) -> None:
        html = """
        <a href="https://lifeattiktok.com/search/7673256057754290437">
          <div><span class="font-bold">
            Creative Product Manager Graduate (Creative and Brand Innovation) - 2027 Start
          </span></div>
          <div>
            <span>San Jose</span><span>Product - Product manager</span>
            <span>Regular</span><span>Bachelor/Master</span>
          </div>
        </a>
        """
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://lifeattiktok.com/search?keyword=product%20manager%20graduate",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            jobs[0].title,
            "Creative Product Manager Graduate (Creative and Brand Innovation) - 2027 Start",
        )
        self.assertEqual(jobs[0].location, "San Jose")
        self.assertEqual(
            jobs[0].url,
            "https://lifeattiktok.com/search/7673256057754290437",
        )

    def test_extracts_rendered_walmart_job_card(self) -> None:
        html = """
        <div data-testid="job-card" data-job-id="WD-123" role="link">
          <div><span data-testid="job-title">Product Marketing Intern — Summer 2027</span></div>
          <div><span data-testid="category">Marketing</span><span>Hoboken, NJ</span></div>
        </div>
        """
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://careers.walmart.com/us/en/results?searchQuery=product%20marketing%20intern",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Hoboken, NJ")
        self.assertTrue(jobs[0].url.endswith("#WD-123"))

    def test_extracts_ea_avature_job_card_with_location(self) -> None:
        html = """
        <article class="article article--result article--non-toggle">
          <h3>
            <a class="link link_result" href="https://jobs.ea.com/en_US/careers/JobDetail/Product-Manager-Intern/123">
              Product Manager Intern — Summer 2027
            </a>
          </h3>
          <div class="article__header__text__subtitle">
            <span class="list-item-location">Vancouver, Canada</span>
            <span class="list-item-id">Role ID 123</span>
          </div>
        </article>
        """
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://jobs.ea.com/en_US/careers/SearchJobs/?search=product",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Manager Intern — Summer 2027")
        self.assertEqual(jobs[0].location, "Vancouver, Canada")
        self.assertTrue(jobs[0].url.endswith("/Product-Manager-Intern/123"))

    def test_extracts_shopify_react_router_job_feed(self) -> None:
        devalue_payload = [
            {"_1": 2},
            "loaderData",
            {"_3": 4},
            "($locale)/careers",
            {"_5": 6},
            "jobPostingsWithJobs",
            [7],
            {"_8": 9},
            "jobPosting",
            {"_10": 11, "_12": 13, "_14": 15},
            "title",
            "Product Marketing Intern — Summer 2027",
            "locationName",
            "Toronto, Canada",
            "externalLink",
            "https://www.shopify.com/careers?ashby_jid=shopify-123",
        ]
        serialized_chunk = json.dumps(json.dumps(devalue_payload))
        html = (
            "<script>window.__reactRouterContext.streamController.enqueue("
            f"{serialized_chunk});</script>"
        )
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://www.shopify.com/careers",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Marketing Intern — Summer 2027")
        self.assertEqual(jobs[0].location, "Toronto, Canada")
        self.assertEqual(
            jobs[0].url,
            "https://www.shopify.com/careers?ashby_jid=shopify-123",
        )

    def test_extracts_linkedin_public_job_card_with_location(self) -> None:
        source = Source(
            id="linkedin",
            name="LinkedIn",
            urls=("https://www.linkedin.com/jobs-guest/jobs/api/search",),
            career_url="https://careers.linkedin.com/",
        )
        html = """
        <li>
          <div class="base-search-card base-search-card--link job-search-card">
            <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/product-analyst-intern-at-linkedin-445?position=1&amp;trackingId=abc">
              <span class="sr-only">Product Analyst Intern — 2027</span>
            </a>
            <div class="base-search-card__info">
              <h3 class="base-search-card__title">Product Analyst Intern — 2027</h3>
              <div class="base-search-card__metadata">
                <span class="job-search-card__location">Toronto, Ontario, Canada</span>
              </div>
            </div>
          </div>
        </li>
        """
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            source,
            source.urls[0],
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].location, "Toronto, Ontario, Canada")
        self.assertEqual(
            jobs[0].url,
            "https://www.linkedin.com/jobs/view/product-analyst-intern-at-linkedin-445",
        )

    def test_extracts_snap_nested_job_api(self) -> None:
        source = Source(
            id="snap",
            name="Snap",
            urls=("https://careers.snap.com/api/jobs",),
            career_url="https://careers.snap.com/jobs",
        )
        payload = {
            "body": [
                {
                    "_source": {
                        "id": "R0047000",
                        "title": "Marketing Intern — Summer 2027",
                        "absolute_url": "https://wd1.myworkdaysite.com/recruiting/snapchat/snap/job/Toronto/Marketing-Intern_R0047000",
                        "primary_location": "Toronto",
                        "offices": [{"location": "Toronto, Canada"}],
                    }
                }
            ],
            "aggs": {},
        }
        encoded = json.dumps(payload)
        self.assertTrue(response_has_job_signal(encoded, "application/json"))
        jobs = extract_jobs(
            encoded,
            "application/json",
            source,
            source.urls[0],
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertIn("Toronto", jobs[0].location)
        self.assertEqual(jobs[0].title, "Marketing Intern — Summer 2027")

    def test_extracts_google_init_data_without_browser_rendering(self) -> None:
        matching_row = [
            "123456789012345678",
            "Product Manager Intern — Summer 2027",
            "https://www.google.com/about/careers/applications/signin?jobId=token",
            None,
            None,
            None,
            None,
            "Google",
            "en-US",
            [["Toronto, ON, Canada", ["111 Richmond St W, Toronto, ON"], "Toronto"]],
        ]
        unrelated_row = [
            "876543210987654321",
            "Senior Product Manager",
            "https://www.google.com/about/careers/applications/signin?jobId=other",
            None,
            None,
            None,
            None,
            "Google",
            "en-US",
            [["Mountain View, CA, USA"]],
        ]
        payload = json.dumps([[matching_row, unrelated_row], None, 2, 2])
        html = (
            "<script>AF_initDataCallback({key: 'ds:1', hash: '2', data:"
            f"{payload}"
            "});</script>"
        )
        self.assertTrue(response_has_job_signal(html, "text/html"))
        jobs = extract_jobs(
            html,
            "text/html",
            self.source,
            "https://www.google.com/about/careers/applications/jobs/results/?q=product",
            self.filter,
        )
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Product Manager Intern — Summer 2027")
        self.assertEqual(jobs[0].location, "Toronto, ON, Canada")
        self.assertEqual(
            jobs[0].url,
            "https://www.google.com/about/careers/applications/jobs/results/123456789012345678",
        )

    def test_google_empty_results_are_still_a_healthy_response(self) -> None:
        html = (
            "<script>AF_initDataCallback("
            "{key: 'ds:1', hash: '2', data:[[],null,0,0]});</script>"
        )
        self.assertTrue(response_has_job_signal(html, "text/html"))


if __name__ == "__main__":
    unittest.main()
