from pathlib import Path
import stat
import tempfile
import unittest

from apm_notifier.config import load_sources, save_env_values


class SaveEnvironmentTests(unittest.TestCase):
    def test_loads_priority_source_option_and_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(
                '{"sources":[{"id":"priority","name":"Priority",'
                '"urls":["https://jobs.example.com"],"priority":true},'
                '{"id":"regular","name":"Regular","urls":["https://jobs.example.com"]}]}',
                encoding="utf-8",
            )
            priority, regular = load_sources(path)
            self.assertTrue(priority.priority)
            self.assertFalse(regular.priority)

    def test_creates_from_template_and_updates_selected_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / ".env.example"
            target = root / ".env"
            template.write_text("TOKEN=\nUNCHANGED=yes\n", encoding="utf-8")
            save_env_values(target, {"TOKEN": "secret", "CHAT_ID": "123"}, template)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "TOKEN=secret\nUNCHANGED=yes\n\nCHAT_ID=123\n",
            )
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_ats_shorthand_options_tier_and_registry_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            path.write_text(
                '{"sources":[{"id":"bree","name":"Bree","ats":"ashby","board":"bree","tier":"must",'
                '"added_on":"2026-11-12","discovered_via":"probe"},'
                '{"id":"off","name":"Off","urls":["https://x.example"],"enabled":false,"note":"why"}]}',
                encoding="utf-8",
            )
            (bree,) = load_sources(path)
            self.assertEqual((bree.ats, bree.ats_options, bree.tier), ("ashby", {"board": "bree"}, "must"))
            self.assertTrue(bree.must_include)
            self.assertEqual((bree.added_on, bree.discovered_via), ("2026-11-12", "probe"))
            self.assertEqual(len(load_sources(path, include_disabled=True)), 2)

    def test_rejects_unknown_adapter_and_tier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources.json"
            for entry in ('{"id":"a","name":"A","ats":"nope"}', '{"id":"a","name":"A","urls":["https://x"],"tier":"gold"}'):
                path.write_text(f'{{"sources":[{entry}]}}', encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_sources(path)

    def test_watchlist_covers_every_must_include_company(self) -> None:
        project_root = Path(__file__).resolve().parent.parent
        sources = load_sources(project_root / "config" / "sources.json", include_disabled=True)
        must = {source.id for source in sources if source.tier == "must"}
        expected = {
            "microsoft", "pointclickcare", "loblaw-digital", "interac", "1password", "dayforce", "eq-bank",
            "kensington-tours", "kabam", "bree", "wealthsimple", "telus", "telus-health", "telus-digital",
            "autodesk", "cibc", "cibc-campus", "mlse", "soti", "td", "shopify", "scotiabank", "telmax",
            "american-express",
        }
        self.assertTrue(expected <= must, expected - must)
        by_id = {source.id: source for source in sources}
        # Disabled entries must say why they could not be scanned.
        self.assertTrue(all(by_id[item].enabled or by_id[item].ats_options.get("note") is None for item in by_id))
        self.assertNotIn("newgrad-community-backcheck", by_id)
        self.assertTrue(by_id["simplify-summer-2027"].aggregator)
        enabled_types = {source.ats or "legacy" for source in sources if source.enabled}
        self.assertTrue({"greenhouse", "lever", "ashby", "workday", "smartrecruiters", "successfactors"} <= enabled_types)


if __name__ == "__main__":
    unittest.main()
