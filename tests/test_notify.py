import unittest

from apm_notifier.notify import _health_error_summary


class HealthErrorSummaryTests(unittest.TestCase):
    def test_collapses_repeated_http_errors_without_long_urls(self) -> None:
        errors = (
            "https://example.com/a?long=query: HTTP Error 503: Service Temporarily Unavailable",
            "https://example.com/b?long=query: HTTP Error 503: Service Temporarily Unavailable",
        )

        summary = _health_error_summary(errors)

        self.assertEqual(summary, "HTTP 503: Service Temporarily Unavailable")
        self.assertNotIn("https://", summary)

    def test_summarizes_browser_timeout(self) -> None:
        summary = _health_error_summary(
            ("https://example.com: browser render failed after retry: timed out",)
        )
        self.assertEqual(summary, "Browser rendering timed out after retries")


if __name__ == "__main__":
    unittest.main()


class DigestTests(unittest.TestCase):
    def entry(self, name, **flags):
        from apm_notifier.notify import DigestEntry

        return DigestEntry(0, name, "Product Manager Intern", "Toronto, ON", "Summer 2027", "ACCEPT", "Core PM title", "https://example.com", **flags)

    def test_sorts_must_include_then_core_then_gta_then_newest(self) -> None:
        from apm_notifier.notify import sort_digest

        entries = [
            self.entry("old-gta", gta=True, newest="2026-11-01"),
            self.entry("new-plain", newest="2026-11-12"),
            self.entry("core", core_title=True, newest="2026-10-01"),
            self.entry("must", must_include=True, newest="2026-09-01"),
            self.entry("new-gta", gta=True, newest="2026-11-10"),
        ]
        self.assertEqual([item.company for item in sort_digest(entries)], ["must", "core", "new-gta", "old-gta", "new-plain"])

    def test_long_digests_are_split_under_the_telegram_limit(self) -> None:
        from apm_notifier.notify import chunk_messages, format_entry_html

        blocks = [format_entry_html(self.entry(f"Company {index}", must_include=True)) for index in range(80)]
        messages = chunk_messages("header", blocks)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 3800 for message in messages))
        self.assertTrue(all("MUST-INCLUDE" in block for block in blocks))
