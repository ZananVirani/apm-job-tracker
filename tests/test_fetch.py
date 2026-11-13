import subprocess
import io
import unittest
from unittest.mock import patch, MagicMock

from apm_notifier.fetch import BrowserRenderer, HttpClient
from urllib.request import Request


class BrowserRendererTests(unittest.TestCase):
    def test_linkedin_employer_feeds_share_a_request_budget(self) -> None:
        client = HttpClient(5, host_interval_seconds=0)
        with patch("apm_notifier.fetch.time.monotonic", side_effect=(100, 101, 103)), patch("apm_notifier.fetch.time.sleep") as sleep:
            client._pace("https://www.linkedin.com/jobs-guest/search?f_C=1")
            client._pace("https://uk.linkedin.com/jobs-guest/search?f_C=2")
            client._pace("https://jobs.example.com/jobs")
        sleep.assert_called_once_with(2.0)

    def test_each_host_is_paced_independently(self) -> None:
        client = HttpClient(5, host_interval_seconds=1.5)
        with patch("apm_notifier.fetch.time.monotonic", side_effect=(10, 10.5, 11.0, 12)), patch("apm_notifier.fetch.time.sleep") as sleep:
            client._pace("https://boards-api.greenhouse.io/v1/boards/a/jobs")
            client._pace("https://api.lever.co/v0/postings/b")
            client._pace("https://boards-api.greenhouse.io/v1/boards/c/jobs")
        sleep.assert_called_once_with(0.5)

    def test_robots_disallow_is_respected_and_cached(self) -> None:
        client = HttpClient(5, host_interval_seconds=0)
        robots = MagicMock(text="User-agent: *\nDisallow: /private/\n")
        with patch.object(client, "fetch", return_value=robots) as fetch:
            self.assertFalse(client.allowed_by_robots("https://careers.example.com/private/job/1"))
            self.assertTrue(client.allowed_by_robots("https://careers.example.com/jobs/2"))
        fetch.assert_called_once()

    def test_dayforce_search_bootstraps_anonymous_csrf_session(self) -> None:
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = io.BytesIO(b'{"csrfToken":"public-test-token"}')
        request = Request("https://jobs.dayforcehcm.com/api/geo/qfg/jobposting/search", data=b'{}', method="POST")
        with patch("apm_notifier.fetch.build_opener", return_value=opener):
            HttpClient(5)._open_request(request)
        self.assertEqual(opener.open.call_count, 2)
        self.assertEqual(request.get_header("X-csrf-token"), "public-test-token")

    def test_retries_a_transient_browser_timeout(self) -> None:
        renderer = BrowserRenderer(5)
        renderer.executable = "/bin/true"
        success = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='<a href="/jobs/1">Product Manager Intern</a>',
            stderr="",
        )
        with (
            patch(
                "apm_notifier.fetch.subprocess.run",
                side_effect=(subprocess.TimeoutExpired("chromium", 5), success),
            ) as run,
            patch("apm_notifier.fetch.time.sleep"),
        ):
            result = renderer.fetch("https://jobs.example.com")

        self.assertEqual(run.call_count, 2)
        self.assertIn("Product Manager Intern", result.text)


if __name__ == "__main__":
    unittest.main()
