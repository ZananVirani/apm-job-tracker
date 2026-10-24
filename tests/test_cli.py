from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from apm_notifier.cli import main
from apm_notifier.models import Job
from apm_notifier.state import StateStore


class HistoryCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        logging_setup = patch("apm_notifier.cli.logging.basicConfig")
        logging_setup.start()
        self.addCleanup(logging_setup.stop)

    def test_history_prints_json_without_constructing_monitor_or_notifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            store = StateStore(path)
            job = Job("tiktok", "TikTok", "Product Manager Graduate", "https://example.com/jobs/1")
            store.record_jobs((job, Job("example", "Example", "APM", "https://example.com/jobs/2")))
            store.silence_pending()
            expected = [dict(row) for row in store.history_rows("TikTok")]
            store.close()
            original = path.read_bytes()
            output = StringIO()
            with (
                patch("apm_notifier.cli.Settings.from_environment", return_value=SimpleNamespace(state_path=path)),
                patch("apm_notifier.cli._monitor") as monitor,
                patch("apm_notifier.cli._notifier") as notifier,
                patch("apm_notifier.cli.urlopen") as network,
                redirect_stdout(output),
            ):
                self.assertEqual(main(["history", "--company", "tiktok"]), 0)
            self.assertEqual(json.loads(output.getvalue()), expected)
            self.assertIsNotNone(expected[0]["notified_at"])
            self.assertNotIn("delivered", output.getvalue())
            self.assertEqual(path.read_bytes(), original)
            monitor.assert_not_called()
            notifier.assert_not_called()
            network.assert_not_called()

    def test_missing_history_is_reported_without_creating_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "state.sqlite3"
            error = StringIO()
            with (
                patch("apm_notifier.cli.Settings.from_environment", return_value=SimpleNamespace(state_path=path)),
                redirect_stderr(error),
            ):
                self.assertEqual(main(["history"]), 2)
            self.assertIn("Notification history does not exist", error.getvalue())
            self.assertFalse(path.parent.exists())


if __name__ == "__main__":
    unittest.main()
