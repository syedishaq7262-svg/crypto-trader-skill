"""crypto-entry: the scanner's own rules, bar by bar, with the right status at every moment."""

import json
import unittest

from _load import cc, Session, load

ENTRY = ("crypto-entry", "entry")
SCAN = ("crypto-scanner", "scan")
EXIT = ("crypto-exit", "exit")
TM = ("crypto-timemachine", "timemachine")


def entry_json(s):
    return cc.read_json(s.path("entry.json"), None)


class Orb(unittest.TestCase):
    def test_replay_triggers_on_the_planted_bar_and_matches_the_scanner(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["watch", "ORBX", "--setup", "orb", "--session", "ny", "--venue", "binance",
                                            "--now", "2026-10-21 13:50"])
            self.assertEqual(code, 0, err)
            self.assertIn("Replaying closed bars", out)
            self.assertIn("FORMING   ORBX: The opening range is forming until 14:00 UTC", out)
            self.assertIn("WAITING   ORBX: Waiting for a 5-minute close above", out)
            self.assertIn("At 14:20 UTC the 5-minute bar closed at", out)
            self.assertIn("above the range high", out)
            self.assertIn("Nothing was placed", out)
            e = entry_json(s)
            self.assertEqual((e["hit"]["setup"], e["hit"]["side"]), ("orb", "long"))
            self.assertEqual(e["params"]["session"], "ny")
        with Session(now="2026-10-21 14:20") as s:
            s.run(*SCAN, ["--venue", "binance", "--session", "ny", "--setups", "orb", "--symbols", "ORBX"])
            hit = [h for h in cc.read_json(s.path("watchlist.json"))["hits"] if h["side"] == "long"][0]
        for k in ("trigger", "level", "stop_ref", "stop_ref_raw", "at", "rvol"):
            self.assertEqual(e["hit"][k], hit[k], k)

    def test_an_old_trigger_is_stale_and_writes_nothing(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["check", "ORBX", "--session", "ny", "--venue", "binance"])
            self.assertEqual(code, 1)
            self.assertIn("STALE | ORBX", out)
            self.assertIn("Triggered 55m ago: at 14:20 UTC", out)
            self.assertIn("still beyond the range", out)
            self.assertIn("A late entry is a different trade", out)
            self.assertIsNone(entry_json(s))
            code, out, err = s.run(*ENTRY, ["watch", "ORBX", "--session", "ny", "--venue", "binance", "--side", "long"])
            self.assertEqual(code, 2)
            self.assertIn("Nothing left to wait for (ORBX stale)", out)

    def test_one_side_only_keeps_waiting(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["check", "ORBX", "--session", "ny", "--venue", "binance", "--side", "short"])
            self.assertIn("WAITING | ORBX", out)
            self.assertIn("Waiting for a 5-minute close below", out)
            self.assertIn("until 17:30 UTC", out)

    def test_before_the_open_and_at_weekends(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["check", "ORBX", "--session", "ny", "--venue", "binance",
                                            "--now", "2026-10-21 13:20"])
            self.assertIn("FORMING | ORBX", out)
            self.assertIn("Not open yet: the next New York open is 13:30 UTC, in 10m", out)
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--session", "ny", "--now", "2026-10-24 15:00"])
            self.assertIn("2026-10-26 13:30 UTC, in 1d 22h", out)
            self.assertIn("no session at weekends", out)
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--session", "london", "--now", "2026-10-21 19:00"])
            self.assertIn("the next London open is 07:00 UTC, in 12h00m", out)

    def test_a_watch_from_before_the_open_waits_for_it(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["watch", "ORBX,BTC", "--session", "ny", "--venue", "binance",
                                            "--now", "2026-10-21 13:20", "--max-checks", "4"])
            self.assertEqual(code, 3)
            self.assertIn("13:25 UTC  FORMING   BTC: Not open yet", out)
            self.assertIn("13:30 UTC  FORMING   ORBX: The opening range is forming until 14:00 UTC.", out)
            self.assertIn("Stopped watching after 4 looks; still ORBX forming, BTC forming.", out)


class OtherSetups(unittest.TestCase):
    def test_each_setup_triggers_like_the_scanner(self):
        cases = [("BRKX", "breakout", "long", "above the 24h high"), ("BRKD", "breakout", "short", "below the 24h low"),
                 ("TRDX", "trend", "long", "back over its 20 average"), ("VOLX", "volume", "long", "its usual volume"),
                 ("RSIX", "reversal", "long", "1-hour RSI turned up")]
        with Session() as s:
            s.run(*SCAN, ["--venue", "binance"])
            hits = cc.read_json(s.path("watchlist.json"))["hits"]
            for coin, setup, side, words in cases:
                code, out, err = s.run(*ENTRY, ["check", coin, "--setup", setup, "--venue", "binance"])
                self.assertEqual(code, 0, coin + out + err)
                self.assertIn("TRIGGERED | %s" % coin, out)
                self.assertIn(words, out)
                e = entry_json(s)["hit"]
                h = [x for x in hits if x["coin"] == coin and x["setup"] == setup][0]
                self.assertEqual(e["side"], side)
                for k in ("trigger", "level", "stop_ref", "at", "status"):
                    self.assertEqual(e[k], h[k], (coin, k))

    def test_several_coins_one_entry_file(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["check", "BRKX,BRKD,BTC", "--setup", "breakout", "--venue", "bybit"])
            self.assertEqual(code, 0)
            self.assertEqual(out.count("TRIGGERED |"), 2)
            self.assertIn("crypto/entry.json holds BRKX (one trigger at a time).", out)
            self.assertEqual(entry_json(s)["hit"]["coin"], "BRKX")

    def test_squeeze_break_plans_and_replays_its_own_history(self):
        scan = load(*SCAN)
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["check", "SQZX", "--setup", "squeeze", "--venue", "binance"])
            self.assertEqual(code, 0, out + err)
            self.assertIn("the first close outside the bands after a squeeze", out)
            e = entry_json(s)["hit"]
            v = cc.get_venue("binance")
            m = v.market("SQZX")
            x = scan.Ctx(m, {}, [], v.bars(m["symbol"], "1h", 760), b15=[])
            direct = scan.setup_squeeze_break(x, scan.args_from({}), ["long", "short"])
            self.assertEqual([(h["side"], h["trigger"], h["at"]) for h in direct], [(e["side"], e["trigger"], e["at"])])
            code, out, err = s.run(*EXIT, ["plan", "SQZX"])
            self.assertEqual(code, 0, err)
            p = cc.read_json(s.path("plan.json"))
            self.assertEqual((p["setup"]["name"], p["side"], p["stop_spec"]), ("squeeze", e["side"], "setup"))
            self.assertIn("the middle band", out)
            code, out, err = s.run(*TM, ["run", "--replays", "500"])
            self.assertEqual(code, 0, err)
            self.assertIn("History: the same squeeze-break rule on the pool fired", out)

    def test_a_coiled_squeeze_plan_says_the_history_needs_the_break(self):
        with Session() as s:
            s.run(*SCAN, ["--venue", "binance", "--setups", "squeeze"])
            s.run(*EXIT, ["plan", "FUNDX", "--side", "long"])
            code, out, err = s.run(*TM, ["run", "--replays", "500"])
            self.assertIn("enters before the squeeze breaks", out)
            self.assertIn("/crypto-entry FUNDX --setup squeeze", out)

    def test_no_squeeze_and_no_trend_say_so(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--setup", "squeeze", "--venue", "binance", "--json"])
            d = json.loads(out)
            self.assertIn(d["status"], ("none", "waiting", "triggered", "stale"))
            if d["status"] == "none":
                self.assertIn("Nothing to time", d["words"])
                self.assertTrue(d["final"])

    def test_hyperliquid_has_no_tick_and_still_reads_cleanly(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["check", "VOLX", "--setup", "volume", "--venue", "hyperliquid"])
            self.assertEqual(code, 0, err)
            self.assertIn("closing at 0.6889", out)


class Level(unittest.TestCase):
    def cross(self, s):
        v = cc.get_venue("binance")
        bars = v.bars("BTCUSDT", "5m", 120)
        j = max(i for i in range(len(bars) - 30, len(bars)) if bars[i][cc.C] > bars[i - 1][cc.C] * 1.0002)
        return bars, j, (bars[j - 1][cc.C] + bars[j][cc.C]) / 2.0

    def test_a_cross_triggers_on_the_close(self):
        with Session() as s:
            bars, j, lvl = self.cross(s)
            when = cc.fmt_utc(bars[j][cc.T] + 300000).replace(" UTC", "")
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--above", "%.2f" % lvl, "--venue", "binance", "--now", when])
            self.assertEqual(code, 0, out + err)
            self.assertIn("TRIGGERED | BTC | your level, 5-minute closes", out)
            self.assertIn("above your", out)
            e = entry_json(s)
            self.assertEqual((e["hit"]["setup"], e["hit"]["side"], e["hit"]["stop_ref"]), ("level", "long", None))
            self.assertEqual(e["params"]["tf"], "5m")
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--above", "%.2f" % lvl, "--venue", "binance", "--now", when,
                                            "--rvol", "1000"])
            self.assertEqual(code, 1)
            self.assertIn("short of your 1000x, so that cross does not count", out)

    def test_a_level_planned_by_hand_gets_an_atr_stop(self):
        with Session() as s:
            bars, j, lvl = self.cross(s)
            when = cc.fmt_utc(bars[j][cc.T] + 300000).replace(" UTC", "")
            s.run(*ENTRY, ["check", "BTC", "--above", "%.2f" % lvl, "--venue", "binance", "--now", when])
            code, out, err = s.run(*EXIT, ["plan", "BTC", "--now", when])
            self.assertEqual(code, 0, err)
            p = cc.read_json(s.path("plan.json"))
            self.assertEqual(p["setup"]["name"], "level")
            self.assertTrue(p["stop_spec"].startswith("atr:"))
            code, out, err = s.run(*TM, ["run", "--replays", "500", "--now", when])
            self.assertIn("a hand-picked level has no past to re-run", out)

    def test_waiting_and_the_wrong_side_of_the_price(self):
        with Session() as s:
            v = cc.get_venue("binance")
            last = v.bars("BTCUSDT", "5m", 2)[-1][cc.C]
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--above", "%.1f" % (last * 1.01), "--venue", "binance"])
            self.assertIn("WAITING | BTC", out)
            self.assertIn("(1.00% away)", out)
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--above", "1", "--venue", "binance"])
            self.assertIn("NONE | BTC", out)
            self.assertIn("Pick a level over the price, or use --below", out)
            code, out, err = s.run(*ENTRY, ["watch", "BTC", "--above", "%.1f" % (last * 1.05), "--venue", "binance",
                                            "--max-checks", "2"])
            self.assertEqual(code, 3)
            self.assertIn("Stopped watching after 2 looks; still BTC waiting.", out)

    def test_refusals(self):
        with Session() as s:
            code, out, err = s.run(*ENTRY, ["level", "BTC"])
            self.assertIn("give a level", err)
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--above", "-5"])
            self.assertIn("positive price", err)
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--above", "100", "--side", "short"])
            self.assertIn("leaves nothing to watch", err)
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--or-minutes", "7"])
            self.assertIn("multiple of 5", err)
        with Session(config={"allow_short": False}) as s:
            code, out, err = s.run(*ENTRY, ["check", "BTC", "--below", "100"])
            self.assertIn("shorts are off", err)
            code, out, err = s.run(*ENTRY, ["check", "BRKD", "--setup", "breakout", "--venue", "binance"])
            self.assertNotIn("TRIGGERED", out)


if __name__ == "__main__":
    unittest.main()
