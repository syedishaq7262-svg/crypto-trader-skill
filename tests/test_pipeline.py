"""The whole chain on every venue: scanner -> exit -> timemachine -> budget -> liquidation -> risk."""

import unittest

from _load import cc, Session


class Pipeline(unittest.TestCase):
    def test_every_venue(self):
        for venue in ("binance", "bybit", "hyperliquid"):
            with Session() as s:
                steps = [
                    ("crypto-scanner", "scan", ["--venue", venue, "--session", "ny"]),
                    ("crypto-exit", "exit", ["plan", "ORBX"]),
                    ("crypto-timemachine", "timemachine", ["run", "--replays", "1000"]),
                    ("crypto-budget", "size", []),
                    ("crypto-liquidation", "liq", ["check"]),
                    ("crypto-risk", "check", []),
                ]
                for folder, name, argv in steps:
                    code, out, err = s.run(folder, name, argv)
                    self.assertEqual(code, 0, "%s %s on %s: %s %s" % (folder, argv, venue, err, out[-400:]))
                plan = cc.read_json(s.path("plan.json"))
                tm = cc.read_json(s.path("timemachine.json"))
                o = cc.read_json(s.path("orders.json"))["orders"][0]
                self.assertEqual(plan["venue"], venue)
                self.assertEqual(tm["plan_ref"]["plan_id"], plan["plan_id"])
                self.assertEqual(o["plan_id"], plan["plan_id"])
                self.assertIsNotNone(o["timemachine"])
                self.assertEqual(o["liq"]["verdict"], "PASS")
                self.assertEqual(o["risk"]["verdict"], "PASS")
                self.assertEqual(o["risk"]["fingerprint"], cc.fingerprint(o))
                self.assertTrue(o["paper"])


if __name__ == "__main__":
    unittest.main()
