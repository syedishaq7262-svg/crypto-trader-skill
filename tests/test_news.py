"""crypto-news: feeds parsed safely, coins and tags found without false alarms, only what is new, the calendar."""

import json
import unittest

from _load import cc, load, Session

NEWS = ("crypto-news", "news")
N = load(*NEWS)
KNOWN = {"BTC", "ETH", "SOL", "BCH", "OP", "ARB", "DOGE", "LINK", "BRKX", "SUI", "ONE"}

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<title>t</title>
<item><title><![CDATA[Bitcoin Cash Jumps 12% as &amp; Miners Rotate]]></title><link>https://x.test/a</link>
<media:content url="https://x.test/a.jpg" medium="image"/><guid isPermaLink="false">g-1</guid>
<pubDate>Mon, 26 Oct 2026 11:53:07 +0000</pubDate><description><![CDATA[<p>Some <b>bold</b> text</p>]]></description>
<category domain="tag"><![CDATA[Markets]]></category><category>News</category></item>
<item><title>No date here</title><link>https://x.test/b</link></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title type="html">Ether Holds $3,000</title><link rel="alternate" href="https://y.test/1"/><id>tag:y,1</id>
<published>2026-10-26T08:30:00-04:00</published><summary type="html">&lt;p&gt;x&lt;/p&gt;</summary>
<category term="ETH"/></entry></feed>"""


class Parsing(unittest.TestCase):
    def test_rss(self):
        items = N.parse_feed(RSS, "X")
        self.assertEqual(len(items), 2)
        a = items[0]
        self.assertEqual(a["title"], "Bitcoin Cash Jumps 12% as & Miners Rotate")
        self.assertEqual(a["summary"], "Some bold text")
        self.assertEqual(a["id"], "g-1")
        self.assertEqual(a["at"], cc.parse_when("2026-10-26 11:53:07"))
        self.assertEqual(a["categories"], ["Markets", "News"])
        self.assertIsNone(items[1]["at"])
        self.assertEqual(items[1]["id"], "https://x.test/b")

    def test_atom(self):
        e = N.parse_feed(ATOM, "Y")[0]
        self.assertEqual((e["title"], e["link"], e["id"]), ("Ether Holds $3,000", "https://y.test/1", "tag:y,1"))
        self.assertEqual(e["at"], cc.parse_when("2026-10-26 12:30"))          # -04:00 read as an offset
        self.assertEqual(e["categories"], ["ETH"])

    def test_refusals(self):
        bomb = '<?xml version="1.0"?><!DOCTYPE z [<!ENTITY a "aaaa">]><rss><channel><title>&a;</title></channel></rss>'
        with self.assertRaises(cc.CryptoError) as e:
            N.parse_feed(bomb, "Z")
        self.assertIn("DOCTYPE", str(e.exception))
        for junk in ("<html><body>nope</body></html>", "not xml at all", "{\"json\": true}"):
            with self.assertRaises(cc.CryptoError):
                N.parse_feed(junk, "Z")
        with self.assertRaises(cc.CryptoError):
            N.parse_feed("<rss>" + "x" * (N.MAX_FEED_BYTES + 1), "Z")
        self.assertEqual(N.parse_feed(chr(0xFEFF) + RSS, "X")[0]["id"], "g-1")  # a byte-order mark is fine

    def test_dates(self):
        self.assertEqual(N.parse_date("Wed, 21 Oct 2026 15:15:00 GMT"), cc.parse_when("2026-10-21 15:15"))
        self.assertEqual(N.parse_date("2026-10-21T15:15:00Z"), cc.parse_when("2026-10-21 15:15"))
        self.assertEqual(N.parse_date("2026-10-21T15:15:00.123+05:30"), cc.parse_when("2026-10-21 09:45"))
        self.assertIsNone(N.parse_date("yesterday"))
        self.assertIsNone(N.parse_date(""))


class Tagging(unittest.TestCase):
    def test_coins(self):
        self.assertEqual(N.coins_in("Bitcoin Cash Jumps While Bitcoin Stalls", KNOWN), ["BCH", "BTC"])
        self.assertEqual(N.coins_in("Bitcoin Cash Jumps", KNOWN), ["BCH"])
        self.assertEqual(N.coins_in("Ether and SOL Lead; $OP Pops", KNOWN), ["ETH", "SOL", "OP"])
        self.assertEqual(N.coins_in("OP Labs Ships an Upgrade", KNOWN), [])              # two letters need a $
        self.assertEqual(N.coins_in("SEC Approves ETF as USD Slides", KNOWN), [])         # acronyms are not coins
        self.assertEqual(N.coins_in("THE NEW ERA FOR SOL AND ONE BIG TOP", KNOWN), [])    # shouting is not tickers
        self.assertEqual(N.coins_in("Chainlink Partners With a Bank", KNOWN), ["LINK"])
        self.assertEqual(N.coins_in("The link between rates and risk", KNOWN), [])
        self.assertEqual(N.coins_in("Delisting of BRKXUSDT Perpetual", KNOWN), ["BRKX"])
        self.assertEqual(N.coins_in("Solana Rallies", {"BTC"}), [])                      # only coins with a market

    def test_tags(self):
        self.assertIn("hack", N.tags_of("Hackers Drain $40M From a Lending Protocol"))
        t = N.tags_of("Exchange Will Delist Three Tokens")
        self.assertIn("delisting", t)
        self.assertNotIn("listing", t)
        self.assertIn("listing", N.tags_of("Exchange Lists a New Perpetual"))
        self.assertIn("regulatory", N.tags_of("SEC Sues a Lender"))
        self.assertNotIn("regulatory", N.tags_of("A section of the market"))            # case matters for SEC
        self.assertIn("macro", N.tags_of("Bitcoin Slips Before the Fed"))
        self.assertNotIn("macro", N.tags_of("Traders fed up with chop"))
        self.assertIn("depeg", N.tags_of("Stablecoin Depegs to $0.97"))
        self.assertIn("unlock", N.tags_of("Token Unlock Worth $90M This Week"))
        self.assertIn("regulatory", N.tags_of("Court Rules Against an Exchange"))       # words in any case
        self.assertIn("regulatory", N.tags_of("Regulators Fine a Broker"))
        self.assertIn("ETF", N.tags_of("Record Inflows Into Funds"))
        self.assertNotIn("ETF", N.tags_of("Etf is not how anyone writes it"))
        # headlines that once set off false alarms on real feeds
        self.assertEqual(N.tags_of("Strategy pauses bitcoin buys, repurchases $205 million of preferred shares"), [])
        self.assertEqual(N.tags_of("3 payment rules control Aave's native Bitcoin market, but only WBTC settles them"), [])
        self.assertEqual(N.tags_of("Hong Kong names Nasdaq-listed Solana Company a strategic enterprise"), [])
        self.assertIn("outage", N.tags_of("Binance pauses withdrawals on Solana amid congestion"))
        self.assertIn("listing", N.tags_of("Bybit to list XYZ perpetual"))
        self.assertIn("regulatory", N.tags_of("Exchange settles with SEC for $50 million"))

    def test_the_same_story_folds_into_one(self):
        base = cc.parse_when("2026-10-21 12:00")
        items = [{"id": "a", "source": "A", "title": "Bitcoin Slips as Traders Brace for Fed Rate Decision", "at": base,
                  "coins": ["BTC"]},
                 {"id": "b", "source": "B", "title": "Bitcoin Dips as Traders Brace for Fed Rate Decision", "at": base + 600000,
                  "coins": ["BTC"]},
                 {"id": "c", "source": "C", "title": "Bitcoin Dips as Traders Brace for Fed Rate Decision", "at": base,
                  "coins": ["ETH"]}]
        out = dict((i["id"], i) for i in N.cluster(items))
        self.assertEqual(out["a"]["also"], ["B"])
        self.assertEqual(out["b"]["dupe_of"], "a")
        self.assertNotIn("dupe_of", out["c"])                                          # different coin, different story


class Brief(unittest.TestCase):
    def test_brief_end_to_end(self):
        with Session() as s:
            s.run("crypto-exit", "exit", ["plan", "SOL", "--side", "long", "--manual", "--venue", "binance", "--hold", "8h"])
            code, out, err = s.run(*NEWS, ["--venue", "binance"])
            self.assertEqual(code, 0, err)
            self.assertIn("18:00 UTC     in 2h45m      USD CPI m/m (forecast 0.3%, previous 0.4%)", out)
            self.assertIn("! lands inside your SOL long plan", out)
            self.assertNotIn("ECB", out)                                                 # USD only by default
            self.assertNotIn("Unemployment Claims", out)                                 # medium impact
            self.assertIn("SOL (plan)", out)
            self.assertIn("SOL Climbs as Solana Funds Draw Fresh Inflows [ETF]", out)
            self.assertIn("! 1h00m ago Hackers Drain $40 Million", out)
            self.assertIn("(Cointelegraph +1 more)", out)
            self.assertNotIn("Bitcoin Slips as Traders", out)                            # folded into the first report
            self.assertNotIn("Ether Options", out)                                       # 30 hours old
            self.assertNotIn("An Item With No Date", out)
            self.assertIn("Read 5 of 7 feeds", out)
            self.assertIn("The Block: www.theblock.co answered HTTP 403", out)
            self.assertIn("CryptoSlate: the feed has a DOCTYPE", out)
            d = cc.read_json(s.path("news.json"))
            for k in ("items", "sources", "macro", "notices", "watch", "moves"):
                self.assertIn(k, d)
            code, out, err = s.run(*NEWS, ["--venue", "binance"])
            self.assertEqual(code, 1)                                                    # nothing new the second time
            self.assertIn("new since the last check", out)
            self.assertIn("nothing new", out)
            code, out, err = s.run(*NEWS, ["--venue", "binance", "--all"])
            self.assertIn("Hackers Drain", out)

    def test_a_delisting_of_your_coin_is_loud(self):
        with Session() as s:
            code, out, err = s.run(*NEWS, ["--coins", "THINX", "--venue", "binance"])
            self.assertIn("! 2h00m ago Delisting of THINXUSDT Perpetual Contract [THINX]", out)
            self.assertIn("! an exchange is delisting it", out)

    def test_moves_say_why(self):
        with Session() as s:
            code, out, err = s.run(*NEWS, ["--coins", "WIF,BTC", "--venue", "binance", "--json"])
            d = json.loads(out)
            for c, mv in d["moves"].items():
                self.assertEqual(mv["moving"], bool(mv["moving_why"]))
                self.assertAlmostEqual(abs(mv["ch24"]) > 0, True)

    def test_macro_and_notices_commands(self):
        with Session() as s:
            code, out, err = s.run(*NEWS, ["macro", "--countries", "USD,EUR"])
            self.assertEqual(code, 0, err)
            self.assertIn("ECB Press Conference", out)
            self.assertLess(out.index("Core PCE"), out.index("CPI m/m"))                # in time order
            self.assertNotIn("Bank Holiday", out)
            self.assertIn("Unofficial feed", out)
            code, out, err = s.run(*NEWS, ["macro", "--impact", "medium"])
            self.assertIn("Unemployment Claims", out)
            code, out, err = s.run(*NEWS, ["notices", "--venue", "binance"])
            self.assertIn("delisting  2h00m ago Delisting of THINXUSDT", out)
            self.assertIn("listing    5h00m ago New Listing: NEWXUSDT", out)
            self.assertNotIn("Old listing", out)                                         # 20 days old
            self.assertNotIn("TradFi", out)                                              # a stock contract, not crypto
            self.assertNotIn("Token Splash", out)                                        # a promotion, not a listing

    def test_one_source_down_never_sinks_the_rest(self):
        with Session(mode="bybit_geoblock") as s:
            code, out, err = s.run(*NEWS, ["--coins", "BTC", "--venue", "binance"])
            self.assertEqual(code, 0, err)
            self.assertIn("Exchange notices not read", out)
            self.assertIn("Market:", out)

    def test_your_own_feeds(self):
        with Session(config={"news_feeds": ["https://www.coindesk.com/arc/outboundfeeds/rss/"]}) as s:
            code, out, err = s.run(*NEWS, ["--coins", "BTC", "--venue", "binance"])
            self.assertIn("Read 1 of 1 feeds", out)
            self.assertNotIn("Cointelegraph", out)


if __name__ == "__main__":
    unittest.main()
