"""crypto-setup: the doctor, init and config."""

import os
import unittest

from _load import cc, Session

SECRET = "k3y-that-must-never-print-9f2c"


class Doctor(unittest.TestCase):
    def test_every_source_gets_a_line(self):
        with Session() as s:
            code, out, err = s.run("crypto-setup", "setup", ["doctor"])
            self.assertEqual(code, 0, out + err)
            for label in ("Binance USD-M futures", "Bybit linear perpetuals", "Hyperliquid perps", "Fear & Greed index",
                          "News feed (CoinDesk)", "Macro calendar", "Bybit announcements", "Work folder",
                          "Display time zone", "Config", "Exchange keys"):
                self.assertIn(label, out)
            self.assertNotIn(" down ", out)
            self.assertIn("UTC (UTC+0 now)", out)
            self.assertIn("none (fine: every skill except a live crypto-broker works without keys)", out)
            self.assertIn("Best venue from here: Binance USD-M futures", out)
            self.assertEqual(cc.read_json(s.path("cache", "venue.json"))["venue"], "binance")

    def test_display_time_zone(self):
        for tz, want in (("Australia/Melbourne", "Australia/Melbourne (UTC+11 now)"),     # daylight time since 4 October
                         ("America/New_York", "America/New_York (UTC-4 now)"),
                         ("+05:30", "+05:30 (UTC+5:30 now)"),
                         ("Mars/Olympus", "unknown  'Mars/Olympus' is not understood here, so times show in UTC.")):
            with Session(config={"display_tz": tz}) as s:
                code, out, _ = s.run("crypto-setup", "setup", ["doctor"])
                self.assertEqual(code, 0, out)
                self.assertIn(want, out)

    def test_names_the_keys_that_are_set_and_never_their_values(self):
        saved = os.environ.get("BINANCE_API_KEY")
        os.environ["BINANCE_API_KEY"] = SECRET
        try:
            with Session() as s:
                code, out, err = s.run("crypto-setup", "setup", ["doctor"])
            self.assertIn("set: BINANCE_API_KEY", out)
            self.assertNotIn(SECRET, out + err)
            self.assertNotIn(SECRET[:8], out + err)
        finally:
            if saved is None:
                os.environ.pop("BINANCE_API_KEY", None)
            else:
                os.environ["BINANCE_API_KEY"] = saved

    def test_one_exchange_blocked(self):
        with Session(mode="binance_geoblock") as s:
            code, out, _ = s.run("crypto-setup", "setup", ["doctor"])
            self.assertEqual(code, 0, out)
            self.assertIn("blocked  refuses connections from this location", out)
            self.assertIn("Best venue from here: Bybit linear perpetuals", out)
            self.assertIn("Blocked here: Binance USD-M futures. That is the exchange's rule for this location, not a fault.", out)
            self.assertEqual(cc.read_json(s.path("cache", "venue.json"))["venue"], "bybit")

    def test_no_exchange_answers(self):
        saved = os.environ.get("CRYPTO_RETRIES")
        os.environ["CRYPTO_RETRIES"] = "0"
        try:
            with Session(mode="binance_geoblock,bybit_geoblock,hyperliquid_down") as s:
                code, out, _ = s.run("crypto-setup", "setup", ["doctor"])
                self.assertEqual(code, 1, out)
                self.assertIn("No exchange answered from here.", out)
                self.assertIn("Hyperliquid perps        down", out)
                self.assertIn("Bybit announcements      down", out)       # on Bybit's own host, so blocked with it
                self.assertIn("(only the skill that reads it is affected)", out)
                self.assertNotIn("Best venue", out)
                self.assertFalse(os.path.exists(s.path("cache", "venue.json")))
                self.assertNotIn("VPN", out)
        finally:
            if saved is None:
                os.environ.pop("CRYPTO_RETRIES", None)
            else:
                os.environ["CRYPTO_RETRIES"] = saved
            cc.reset_http()


class InitAndConfig(unittest.TestCase):
    def test_init_writes_the_example_once(self):
        with Session() as s:
            code, out, _ = s.run("crypto-setup", "setup", ["init"])
            self.assertEqual(code, 0, out)
            self.assertIn("Paper stays on until you set \"paper\": false yourself.", out)
            cfg = cc.read_json(s.path("config.json"))
            self.assertIs(cfg["paper"], True)
            with open(s.path("config.json"), "a") as fh:
                fh.write(" ")
            code, out, _ = s.run("crypto-setup", "setup", ["init"])
            self.assertIn("already exists; nothing changed", out)
            with open(s.path("config.json")) as fh:
                self.assertTrue(fh.read().endswith(" "))                 # untouched
            code, out, _ = s.run("crypto-setup", "setup", ["init", "--force"])
            with open(s.path("config.json")) as fh:
                self.assertFalse(fh.read().endswith(" "))

    def test_config_says_where_each_setting_comes_from(self):
        with Session(config={"max_leverage": 2}) as s:
            code, out, _ = s.run("crypto-setup", "setup", ["config"])
            self.assertEqual(code, 0, out)
            line = [l for l in out.splitlines() if l.strip().startswith("max_leverage")][0]
            self.assertTrue(line.rstrip().endswith("yours"), line)
            line = [l for l in out.splitlines() if l.strip().startswith("paper")][0]
            self.assertTrue(line.rstrip().endswith("default"), line)
            self.assertIn("true", line)


if __name__ == "__main__":
    unittest.main()
