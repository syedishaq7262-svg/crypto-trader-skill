"""tools/live_check.py: the live-data check itself runs clean (here on the test exchange, at the real clock)."""

import contextlib
import io
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from _load import cc, load, Session

LC = load("tools", "live_check")


class LiveCheck(unittest.TestCase):
    def test_every_check_passes_on_the_test_exchange(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        saved = os.environ.get("BINANCE_API_KEY")
        os.environ["BINANCE_API_KEY"] = "must-stay-hidden"
        try:
            self._run(now)
            self.assertEqual(os.environ.get("BINANCE_API_KEY"), "must-stay-hidden")    # put back afterwards
        finally:
            if saved is None:
                os.environ.pop("BINANCE_API_KEY", None)
            else:
                os.environ["BINANCE_API_KEY"] = saved

    def _run(self, now):
        with Session(now=now) as s:
            report = os.path.join(s.dir, "report.txt")
            os.environ["FAKE_NOW"] = now
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = LC.main(["--quick", "--report", report])
            text = out.getvalue()
            self.assertEqual(code, 0, text)
            self.assertIn("NOT live data", text)                               # a test run can never pass for a live one
            self.assertRegex(text, r"Live check \| crypto-trader-skill \d+\.\d+\.\d+, code [0-9a-f]{8} \|")
            self.assertIn("checks passed", text)
            self.assertNotIn("FAIL", text)
            self.assertIn("Exchange keys are hidden from this run", text)
            self.assertNotIn("set: BINANCE_API_KEY", text)                    # the doctor never saw the key
            with open(report) as fh:
                full = fh.read()
            self.assertIn("$ scan.py universe", full)
            self.assertIn("Placed (paper)", full)
            self.assertNotIn(os.path.join(s.dir, "crypto"), full)              # it never wrote to a crypto/ folder here
            self.assertEqual(os.environ.get("CRYPTO_DIR"), s.dir)              # and it put the environment back


if __name__ == "__main__":
    unittest.main()
