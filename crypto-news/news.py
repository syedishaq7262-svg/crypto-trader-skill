#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-news: what is new for the coins you are watching, before a chart becomes a trade.

    python3 news.py                     your coins (plan, positions, watchlist) + market headlines + macro + notices
    python3 news.py --coins BTC,SOL     these coins
    python3 news.py --hours 6 --all     the last 6 hours, including items already shown
    python3 news.py macro               the economic calendar: US high-impact events this week
    python3 news.py notices             exchange listing and delisting notices

Headlines come from the big crypto outlets' public RSS feeds, exchange notices from
Bybit's public announcements API, the calendar from the Forex Factory weekly feed.
Headlines are leads, not facts. Nothing here is advice.
"""

import argparse
import email.utils
import hashlib
import html
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, os.pardir, "crypto-setup")):
    if os.path.isfile(os.path.join(_d, "cryptocore.py")):
        sys.path.insert(0, os.path.abspath(_d))
        break
else:
    sys.stderr.write("Problem: cryptocore.py not found. Install the crypto-setup skill next to this one.\n")
    sys.exit(2)
import cryptocore as cc  # noqa: E402

C, T = cc.C, cc.T
MIN, HOUR, DAY = 60000, 3600000, 86400000
FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed"),
    ("BeInCrypto", "https://beincrypto.com/feed/"),
]
MACRO_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NOTICES_URL = "https://api.bybit.com/v5/announcements/index"
FEED_CACHE_MS = 5 * MIN             # a /loop every minute still asks each outlet once per 5 minutes
MACRO_CACHE_MS = HOUR               # the calendar feed allows only a few requests an hour
NOTICE_CACHE_MS = 10 * MIN
MAX_FEED_BYTES = 5 * 1024 * 1024
BOM_AND_SPACE = chr(0xFEFF) + " \r\n\t"
SEEN_DAYS = 7

# Coin names as headlines write them (case-sensitive). Words that are also ordinary English
# (Near, Optimism, Stellar, Maker, Curve, Compound...) are left out on purpose: tickers catch those.
NAMES = [
    ("Bitcoin Cash", "BCH"), ("Ethereum Classic", "ETC"), ("Shiba Inu", "SHIB"), ("Internet Computer", "ICP"),
    ("Pi Network", "PI"), ("The Sandbox", "SAND"),
    ("Bitcoin", "BTC"), ("Ethereum", "ETH"), ("Ether", "ETH"), ("Solana", "SOL"), ("Ripple", "XRP"), ("Dogecoin", "DOGE"),
    ("Cardano", "ADA"), ("Avalanche", "AVAX"), ("Chainlink", "LINK"), ("Polkadot", "DOT"), ("Litecoin", "LTC"),
    ("Toncoin", "TON"), ("Tron", "TRX"), ("Pepe", "PEPE"), ("Aptos", "APT"), ("Arbitrum", "ARB"), ("Polygon", "POL"),
    ("Hyperliquid", "HYPE"), ("Uniswap", "UNI"), ("Aave", "AAVE"), ("Ethena", "ENA"), ("Celestia", "TIA"),
    ("Injective", "INJ"), ("Bonk", "BONK"), ("dogwifhat", "WIF"), ("Filecoin", "FIL"), ("Cosmos", "ATOM"),
    ("Monero", "XMR"), ("Kaspa", "KAS"), ("Worldcoin", "WLD"), ("Ondo", "ONDO"), ("Pendle", "PENDLE"),
    ("Starknet", "STRK"), ("Lido", "LDO"), ("Algorand", "ALGO"), ("Hedera", "HBAR"), ("VeChain", "VET"),
    ("Tezos", "XTZ"), ("Zcash", "ZEC"), ("Bittensor", "TAO"), ("Berachain", "BERA"), ("Floki", "FLOKI"),
    ("Jito", "JTO"), ("Pyth", "PYTH"), ("LayerZero", "ZRO"), ("EigenLayer", "EIGEN"), ("Raydium", "RAY"),
    ("THORChain", "RUNE"), ("Synthetix", "SNX"), ("Decentraland", "MANA"), ("ApeCoin", "APE"), ("Fantom", "FTM"),
    ("Sui", "SUI"), ("Binance Coin", "BNB"),
]
NOT_TICKERS = set((
    "ETF ETFS SEC USD USDT USDC CEO CFO CTO NFT NFTS DAO API IPO FED GDP CPI PCE FOMC CFTC DOJ IRS FBI EU UK US USA "
    "AI ATH TVL DEX CEX OTC KYC AML FUD FOMO HODL DEFI RWA GAAP Q1 Q2 Q3 Q4 THE AND FOR NOT BUT ARE WAS HAS NOW NEW "
    "TOP BIG WHY HOW WHO CAN GET OUT YOU ITS LLC INC LTD PLC NYSE NASDAQ ECB BOJ IMF").split())
TAGS = [   # (tag, acronyms that must match their case, words in any case)
    ("hack", None, r"hack(?:ed|ers?|s)?|exploit(?:ed|er|s)?|drain(?:ed|s)?|stolen|breach|attacker|rug ?pull"),
    ("depeg", None, r"de-?peg(?:ged|s)?"),
    ("outage", None, r"outage|halt(?:ed|s)?|paus(?:e|ed|es)|suspend(?:s|ed)?|downtime"),
    ("delisting", None, r"delist(?:s|ed|ing)?"),
    ("listing", None, r"(?<!de)list(?:s|ed|ing)|launches perpetual|new perpetual"),
    ("unlock", None, r"unlock(?:s|ed)?|vesting"),
    ("regulatory", r"SEC|CFTC|DOJ|MiCA", r"regulators?|regulation|lawsuit|sue[sd]?|court|charged|charges|bans?|banned|"
                                         r"sanction(?:s|ed)?|settle(?:s|d|ment)|enforcement|subpoena"),
    ("ETF", r"ETFs?", r"inflows?|outflows?"),
    ("macro", r"Fed|FOMC|CPI|PCE|GDP", r"Powell|inflation|rate (?:cut|hike)s?|interest rates?|jobs report|payrolls|"
                                       r"tariffs?|Treasury yields?|recession"),
    ("liquidations", None, r"liquidat(?:ion|ions|ed)|short squeeze|long squeeze"),
    ("network", None, r"upgrade|hard fork|mainnet|testnet|airdrop|halving"),
    ("deal", None, r"acquir(?:e|es|ed|ing)|acquisition|merger|partnership|partners with|funding round|raises \$"),
    ("treasury", None, r"treasury|buys (?:another )?[\d,.]+ (?:BTC|bitcoin|ETH)"),
    ("whale", None, r"whales?"),
    ("opinion", None, r"price prediction|price analysis|could|might|here's why|what to expect|analyst says|forecast"),
]
IMPORTANT = ("hack", "depeg", "outage", "delisting", "unlock")
STOPWORDS = set("that this with from have will after amid over into what when their about more than been were they "
                "says said could would just".split())


# ---------------------------------------------------------------- parsing feeds

def local(tag):
    return tag.split("}", 1)[-1] if isinstance(tag, str) else ""


def clean(text, limit=300):
    if not text:
        return ""
    t = html.unescape(re.sub(r"<[^>]+>", " ", text))
    t = " ".join(t.split())
    return (t[:limit - 3].rstrip() + "...") if len(t) > limit else t


def parse_date(text):
    """RFC 822 (RSS) or ISO 8601 (Atom, the calendar) to epoch ms; None when unreadable."""
    if not text:
        return None
    s = text.strip()
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d)(?::(\d\d))?(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?$", s)
    if not m:
        return None
    y, mo, d, hh, mi, ss, tz = m.groups()
    dt = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), tzinfo=timezone.utc)
    off = 0
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        off = sign * (int(digits[:2]) * 60 + int(digits[2:4]))
    return int(dt.timestamp() * 1000) - off * MIN


def parse_feed(text, source):
    """RSS 2.0, RSS 1.0 or Atom to a list of items. Refuses documents with a DOCTYPE (entity tricks)."""
    if len(text) > MAX_FEED_BYTES:
        raise cc.CryptoError("the feed is %d MB, so it was not read" % (len(text) // (1024 * 1024)))
    if re.search(r"<!DOCTYPE|<!ENTITY", text[:50000], re.I):
        raise cc.CryptoError("the feed has a DOCTYPE, so it was not read (a safety rule)")
    try:
        root = ET.fromstring(text.lstrip(BOM_AND_SPACE))
    except ET.ParseError as e:
        raise cc.CryptoError("not a readable feed (%s)" % e)
    items = []
    kind = local(root.tag)
    if kind in ("rss", "RDF"):
        for it in root.iter():
            if local(it.tag) != "item":
                continue
            f = {}
            cats = []
            for ch in it:
                n = local(ch.tag)
                if n == "category" and ch.text:
                    cats.append(clean(ch.text, 60))
                elif n not in f:
                    f[n] = ch
            title = clean(f["title"].text if "title" in f else "", 220)
            link = (f["link"].text or "").strip() if "link" in f else ""
            guid = (f["guid"].text or "").strip() if "guid" in f else ""
            when = None
            for k in ("pubDate", "date", "published", "updated"):
                if k in f and f[k].text:
                    when = parse_date(f[k].text)
                    if when:
                        break
            desc = clean(f["description"].text if "description" in f else "")
            items.append({"title": title, "link": link, "guid": guid, "at": when, "summary": desc, "categories": cats})
    elif kind == "feed":
        for e in root:
            if local(e.tag) != "entry":
                continue
            f, cats, link = {}, [], ""
            for ch in e:
                n = local(ch.tag)
                if n == "link":
                    if ch.get("rel", "alternate") == "alternate" and not link:
                        link = ch.get("href", "")
                elif n == "category":
                    if ch.get("term"):
                        cats.append(clean(ch.get("term"), 60))
                elif n not in f:
                    f[n] = ch
            when = None
            for k in ("published", "updated"):
                if k in f and f[k].text:
                    when = parse_date(f[k].text)
                    if when:
                        break
            body = f.get("summary") if "summary" in f else f.get("content")
            items.append({"title": clean(f["title"].text if "title" in f else "", 220), "link": link,
                          "guid": (f["id"].text or "").strip() if "id" in f else "", "at": when,
                          "summary": clean(body.text if body is not None else ""), "categories": cats})
    else:
        raise cc.CryptoError("a %s document, not RSS or Atom" % (kind or "strange"))
    out = []
    for it in items:
        if not it["title"]:
            continue
        it["source"] = source
        it["id"] = it["guid"] or it["link"] or hashlib.sha1((source + it["title"]).encode("utf-8")).hexdigest()[:16]
        out.append(it)
    return out


# ---------------------------------------------------------------- tagging

def name_patterns():
    return [(re.compile(r"(?<![A-Za-z])%s(?:'s)?(?![A-Za-z])" % re.escape(n)), c) for n, c in NAMES]


_NAMES = name_patterns()
_TAGS = [(t, re.compile(r"(?<![A-Za-z])(?:%s)(?![A-Za-z])" % cs) if cs else None,
          re.compile(r"(?<![A-Za-z])(?:%s)(?![A-Za-z])" % ci, re.I) if ci else None) for t, cs, ci in TAGS]
_TICKER = re.compile(r"(?<![A-Za-z0-9])(\$?)([A-Z][A-Z0-9]{1,9})(?![a-z0-9])")


def coins_in(text, known):
    """The coins a headline names: '$SOL' or 'SOL' (three letters or more, in caps) or a coin's name."""
    found = []
    rest = text
    for rx, coin in _NAMES:
        if rx.search(rest):
            if coin in known or not known:
                found.append(coin)
            rest = rx.sub(" ", rest)
    letters = [ch for ch in text if ch.isalpha()]
    shouting = letters and sum(1 for ch in letters if ch.isupper()) > 0.6 * len(letters)
    for dollar, tok in _TICKER.findall(rest):
        tok = tok[:-4] if tok.endswith("USDT") and len(tok) > 5 else tok
        base = cc.clean_coin(tok)
        if base not in known:
            continue
        if dollar or (len(tok) >= 3 and tok not in NOT_TICKERS and not shouting):
            found.append(base)
    out = []
    for c in found:
        if c not in out:
            out.append(c)
    return out


def tags_of(text):
    tags = []
    for t, cs, ci in _TAGS:
        if (cs and cs.search(text)) or (ci and ci.search(text)):
            tags.append(t)
    if "delisting" in tags and "listing" in tags:
        tags.remove("listing")
    return tags


def tokens(title):
    return set(w for w in re.findall(r"[a-z0-9]{4,}", title.lower()) if w not in STOPWORDS)


def cluster(items):
    """Fold the same story from several outlets into its first report ('also: Cointelegraph')."""
    items = sorted(items, key=lambda i: i["at"] or 0)
    heads = []
    for it in items:
        tk = tokens(it["title"])
        home = None
        for h in heads:
            if abs((it["at"] or 0) - (h["at"] or 0)) > 12 * HOUR or set(h["coins"]) != set(it["coins"]):
                continue
            inter = len(tk & h["_tokens"])
            union = len(tk | h["_tokens"]) or 1
            if inter / float(union) >= 0.5:
                home = h
                break
        if home is None:
            it["_tokens"] = tk
            it["also"] = []
            heads.append(it)
        else:
            home["also"].append(it["source"])
            it["dupe_of"] = home["id"]
    for h in heads:
        h.pop("_tokens", None)
    return items


# ---------------------------------------------------------------- fetching

def cached(name, max_age, fetch, note_old=True):
    """(data, note): a fresh copy from fetch(), or the saved one when it is young enough or fetch fails."""
    path = cc.wpath("cache", name)
    saved = cc.read_json(path, None)
    now = cc.now_ms()
    if saved and 0 <= now - saved.get("at", 0) <= max_age:
        return saved["data"], None
    try:
        data = fetch()
    except cc.CryptoError as e:
        if saved and now - saved.get("at", 0) <= 7 * DAY:
            return saved["data"], "%s (showing the copy from %s)" % (e, cc.fmt_local(saved["at"]))
        raise
    cc.write_json(path, {"at": now, "data": data})
    return data, None


def fetch_feed(name, url):
    def get():
        text = cc.HTTP.request("GET", url, raw=True, headers={
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"})
        return parse_feed(text, name)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return cached("news-%s.json" % slug, FEED_CACHE_MS, get)


def read_feeds(feeds):
    results = cc.parallel(lambda f: fetch_feed(f[0], f[1]), feeds, workers=8)
    items, sources = [], []
    for (name, url), res, err in results:
        if err is not None:
            sources.append({"name": name, "url": url, "ok": False, "error": str(err)})
            continue
        data, note = res
        items.extend(data)
        sources.append({"name": name, "url": url, "ok": True, "items": len(data), "note": note})
    return items, sources


def macro_calendar():
    def get():
        data = cc.HTTP.request("GET", MACRO_URL)
        if not isinstance(data, list):
            raise cc.CryptoError("the calendar feed answered something unexpected")
        out = []
        for e in data:
            if not isinstance(e, dict):
                continue
            at = parse_date(str(e.get("date") or ""))
            if at is None:
                continue
            out.append({"title": clean(str(e.get("title") or ""), 120), "country": str(e.get("country") or "").upper(),
                        "at": at, "impact": str(e.get("impact") or ""), "forecast": str(e.get("forecast") or ""),
                        "previous": str(e.get("previous") or ""), "actual": str(e.get("actual") or "")})
        return out
    return cached("macro.json", MACRO_CACHE_MS, get)


def notices(known):
    def get():
        out = []
        for kind in ("delistings", "new_crypto"):
            res = cc.HTTP.request("GET", NOTICES_URL, params={"locale": "en-US", "type": kind, "limit": 20})
            if not isinstance(res, dict) or res.get("retCode") not in (0, None):
                raise cc.CryptoError("Bybit's announcements answered %s" % (res.get("retMsg") if isinstance(res, dict) else "?"))
            for n in (res.get("result") or {}).get("list") or []:
                at = n.get("dateTimestamp") or n.get("publishTime") or n.get("startDateTimestamp")
                try:
                    at = int(at)
                except (TypeError, ValueError):
                    continue
                out.append({"title": clean(n.get("title") or "", 200), "url": n.get("url") or "", "at": at, "kind": kind,
                            "tags": [str(t) for t in (n.get("tags") or [])][:8], "source": "Bybit"})
        return out
    data, note = cached("notices.json", NOTICE_CACHE_MS, get)
    for n in data:
        n["coins"] = coins_in(n["title"], known)
        if not n["coins"]:          # 'Delisting of XYZUSDT Perpetual Contract': the symbol is the coin
            for tok in re.findall(r"\b([A-Z0-9]{2,15})(?:USDT|USDC|PERP)\b", n["title"]):
                n["coins"].append(cc.clean_coin(tok))
    return data, note


def feeds_from(cfg):
    """(name, url) for each feed: the config's news_feeds (URLs or [name, url] pairs), else the built-in outlets."""
    feeds = cfg.get("news_feeds") or FEEDS
    return [(f[0], f[1]) if isinstance(f, (list, tuple)) else (re.sub(r"^https?://(www\.)?", "", f).split("/")[0], f)
            for f in feeds]


def coin_headlines(coin, known, cfg, hours=24.0):
    """Headlines naming one coin in the last `hours`, each story once, newest first; and how many feeds answered.
    crypto-coin puts these on its card."""
    items, sources = read_feeds(feeds_from(cfg))
    now = cc.now_ms()
    mine = []
    for it in items:
        if not it["at"] or not (now - int(hours * HOUR) <= it["at"] <= now + 10 * MIN):
            continue
        it["coins"] = coins_in(it["title"] + " " + it["summary"][:200], known)
        if coin in it["coins"]:
            it["tags"] = tags_of(it["title"] + " " + it["summary"])
            it["important"] = bool(set(it["tags"]) & set(IMPORTANT)) or "regulatory" in it["tags"]
            mine.append(it)
    heads = [it for it in cluster(mine) if not it.get("dupe_of")]
    heads.sort(key=lambda it: -it["at"])
    return heads, sum(1 for s in sources if s["ok"]), len(sources)


def macro_soon(hours=24.0, countries=("USD",)):
    """High-impact releases from 2 hours ago to `hours` ahead, oldest first ([] when the calendar cannot be read)."""
    try:
        events, _ = macro_calendar()
    except cc.CryptoError:
        return None
    now = cc.now_ms()
    rows = [e for e in events if e["impact"] == "High" and e["country"] in countries
            and now - 2 * HOUR <= e["at"] <= now + int(hours * HOUR)]
    return sorted(rows, key=lambda e: e["at"])


# ---------------------------------------------------------------- coins and moves

def watched(a):
    """The coins worth news, with why: --coins, else the plan, open positions, the entry, the scout, the watchlist."""
    out = []

    def add(coin, why):
        c = cc.clean_coin(coin)
        for w in out:
            if w["coin"] == c:
                if why not in w["why"]:
                    w["why"].append(why)
                return
        out.append({"coin": c, "why": [why]})
    if a.coins:
        for c in a.coins.split(","):
            if c.strip():
                add(c.strip(), "asked")
        return out
    plan = cc.read_json(cc.wpath("plan.json"), None)
    if plan and plan.get("coin"):
        add(plan["coin"], "plan")
    for p in cc.read_positions():
        add(p["coin"], "open position")
    e = cc.read_json(cc.wpath("entry.json"), None)
    if e and e.get("hit") and cc.now_ms() - (e.get("as_of") or 0) < 6 * HOUR:
        add(e["hit"]["coin"], "entry")
    sc = cc.read_json(cc.wpath("scout.json"), None)
    if sc and cc.now_ms() - (sc.get("as_of") or 0) < 6 * HOUR:
        for c in (sc.get("candidates") or [])[:3]:
            add(c["coin"], "scout")
    w = cc.read_json(cc.wpath("watchlist.json"), None)
    if w and cc.now_ms() - (w.get("as_of") or 0) < 6 * HOUR:
        for h in w.get("hits") or []:
            if len(out) >= 10:
                break
            add(h["coin"], "watchlist")
    return out


def move_of(venue, coin):
    m = venue.market(coin)
    b = venue.bars(m["symbol"], "1h", 24 * 8 + 1)
    if len(b) < 30:
        return None
    c = cc.closes(b)
    ch1 = c[-1] / c[-2] - 1.0
    ch24 = c[-1] / c[-25] - 1.0
    usual1 = cc.median([abs(c[i] / c[i - 1] - 1.0) for i in range(1, len(c))]) or 0.0
    usual24 = cc.median([abs(c[i] / c[i - 24] - 1.0) for i in range(24, len(c))]) or 0.0
    why = []
    if usual1 and abs(ch1) >= 3.0 * usual1 and abs(ch1) >= 0.01:
        why.append("the last hour moved %.1fx its usual hour" % (abs(ch1) / usual1))
    if usual24 and abs(ch24) >= 2.0 * usual24 and abs(ch24) >= 0.03:
        why.append("24h moved %.1fx its usual day" % (abs(ch24) / usual24))
    return {"symbol": m["symbol"], "price": c[-1], "ch1": ch1, "ch24": ch24, "usual1": usual1, "usual24": usual24,
            "moving": bool(why), "moving_why": why, "tick": m.get("tick")}


# ---------------------------------------------------------------- the brief

def ago(ms, now):
    d = now - ms
    if d < 0:
        return "in " + cc.fmt_dur(-d)
    return cc.fmt_dur(d) + " ago"


def collect(a, cfg):
    now = cc.now_ms()
    venue, vnote = None, None
    try:
        venue = cc.get_venue(a.venue)
        known = set(m["coin"] for m in venue.markets())
    except cc.CryptoError as e:
        vnote = "Prices not read (%s): no moves, and coins are matched by name only." % e
        known = set(c for _, c in NAMES)
    watch = watched(a)
    for w in watch:
        known.add(w["coin"])
    items, sources = read_feeds(feeds_from(cfg))
    window = now - int(a.hours * HOUR)
    undated = sum(1 for it in items if not it["at"])
    items = [it for it in items if it["at"] and window <= it["at"] <= now + 10 * MIN]
    for it in items:
        it["coins"] = coins_in(it["title"] + " " + it["summary"][:200], known)
        it["tags"] = tags_of(it["title"] + " " + it["summary"])
    items = cluster(items)
    seen_path = cc.wpath("state", "news_seen.json")
    seen = cc.read_json(seen_path, {}) or {}
    seen = dict((k, v) for k, v in seen.items() if now - v < SEEN_DAYS * DAY)
    last_check = cc.read_json(cc.wpath("state", "news_last.json"), {}).get("at")
    for it in items:
        it["new"] = it["id"] not in seen
        mine = set(it["coins"]) & set(w["coin"] for w in watch)
        it["important"] = bool(set(it["tags"]) & set(IMPORTANT)) or bool(mine and "regulatory" in it["tags"])
    macro, mnote = [], None
    try:
        macro, mnote = macro_calendar()
    except cc.CryptoError as e:
        mnote = "Calendar not read: %s" % e
    notes_list, nnote = [], None
    try:
        notes_list, nnote = notices(known)
    except cc.CryptoError as e:
        nnote = "Exchange notices not read: %s" % e
    for n in notes_list:
        nid = "bybit:%s:%s" % (n["at"], n["title"][:40])
        n["id"] = nid
        n["new"] = nid not in seen
        n["important"] = n["kind"] == "delistings" and bool(set(n["coins"]) & set(w["coin"] for w in watch))
    moves = {}
    if venue is not None and watch:
        res = cc.parallel(lambda w: move_of(venue, w["coin"]), watch, workers=4)
        for w, mv, err in res:
            if err is None and mv:
                moves[w["coin"]] = mv
            elif err is not None:
                w["error"] = str(err)
    return {"as_of": now, "venue": venue.name if venue else None, "last_check": last_check, "hours": a.hours,
            "watch": watch, "moves": moves, "items": items, "sources": sources, "undated": undated,
            "macro": macro, "macro_note": mnote, "notices": notes_list, "notices_note": nnote, "venue_note": vnote,
            "seen": seen}


def mark_seen(d):
    seen = d.pop("seen")
    now = d["as_of"]
    for it in d["items"]:
        seen.setdefault(it["id"], now)
    for n in d["notices"]:
        seen.setdefault(n["id"], now)
    cc.write_json(cc.wpath("state", "news_seen.json"), seen)
    cc.write_json(cc.wpath("state", "news_last.json"), {"at": now})


def macro_rows(d, a, horizon_ms=48 * HOUR):
    now = d["as_of"]
    levels = ("High",) if a.impact == "high" else ("High", "Medium")
    countries = set(c.strip().upper() for c in a.countries.split(",") if c.strip())
    rows = [e for e in d["macro"] if e["impact"] in levels and (not countries or e["country"] in countries)
            and now - 6 * HOUR <= e["at"] <= now + horizon_ms]
    rows.sort(key=lambda e: e["at"])
    return rows


def trade_windows():
    """(label, until) for what is open or planned: a macro event inside one matters most."""
    out = []
    for p in cc.read_positions():
        if p.get("exit_by"):
            out.append(("your open %s %s" % (p["coin"], p["side"]), p["exit_by"]))
    plan = cc.read_json(cc.wpath("plan.json"), None)
    if plan and plan.get("exit_by") and plan["exit_by"] > cc.now_ms():
        out.append(("your %s %s plan" % (plan["coin"], plan["side"]), plan["exit_by"]))
    return out


def when_label(ms, now):
    """'18:00 UTC' today, 'Thu 18:15 UTC' on another day (in the display time zone)."""
    tz = cc.load_config().get("display_tz", "UTC")
    day = lambda x: (cc.utc_dt(x) + cc.timedelta(minutes=cc._tz_offset_minutes(x, tz))).strftime("%a %d")
    t = cc.fmt_local(ms, date=False)
    return t if day(ms) == day(now) else "%s %s" % (day(ms).split(" ")[0], t)


def item_line(it, now, show_coins=True):
    bang = "!" if it["important"] else " "
    tags = (" [%s]" % ", ".join(it["tags"][:3])) if it["tags"] else ""
    coins = (" " + ",".join(it["coins"][:3])) if (show_coins and it["coins"]) else ""
    src = it["source"] + ((" +%d more" % len(it["also"])) if it.get("also") else "")
    return "  %s %-9s %s%s%s  (%s)" % (bang, ago(it["at"], now), it["title"], tags, coins, src)


def print_brief(d, a):
    now = d["as_of"]
    last = d.get("last_check")
    cc.out("News | %s | %s" % (cc.fmt_local(now), ("new since the last check, %s ago" % cc.fmt_dur(now - last))
                                if (last and not a.all) else "the last %gh" % d["hours"]))
    show = [it for it in d["items"] if not it.get("dupe_of") and (a.all or it["new"])]
    show.sort(key=lambda it: -(it["at"] or 0))
    # macro
    rows = macro_rows(d, a)
    windows = trade_windows()
    if rows or d.get("macro_note"):
        cc.out("")
        cc.out("Macro calendar (%s, %s impact, next 48h):" % (a.countries.upper() or "all", a.impact))
        for e in rows:
            when = "out %s" % ago(e["at"], now) if e["at"] <= now else ago(e["at"], now)
            extra = ", ".join(x for x in ("forecast %s" % e["forecast"] if e["forecast"] else "",
                                         "previous %s" % e["previous"] if e["previous"] else "",
                                         "actual %s" % e["actual"] if e["actual"] else "") if x)
            cc.out("  %-13s %-13s %s %s%s" % (when_label(e["at"], now), when, e["country"], e["title"],
                                             (" (%s)" % extra) if extra else ""))
            for label, until in windows:
                if now <= e["at"] <= until:
                    cc.out("    ! lands inside %s (out by %s): a big candle can jump a stop." % (label, cc.fmt_local(until)))
        if not rows and not d.get("macro_note"):
            cc.out("  none")
        if d.get("macro_note"):
            cc.out("  " + d["macro_note"])
    # notices
    nts = [n for n in d["notices"] if (a.all or n["new"]) and now - n["at"] <= (7 * DAY if n["kind"] == "delistings" else 2 * DAY)]
    watch_coins = set(w["coin"] for w in d["watch"])
    if nts or d.get("notices_note"):
        cc.out("")
        cc.out("Exchange notices (Bybit):")
        for n in sorted(nts, key=lambda n: (-int(n["important"]), -n["at"]))[:8]:
            cc.out("  %s %-9s %s%s" % ("!" if n["important"] else " ", ago(n["at"], now), n["title"],
                                     (" [%s]" % ", ".join(n["coins"])) if n["coins"] else ""))
        if d.get("notices_note"):
            cc.out("  " + d["notices_note"])
    # your coins: the ones with news, a big move or money on them get a block; the rest share one line
    if d["watch"]:
        cc.out("")
        cc.out("Your coins:")
        quiet = []
        for w in d["watch"]:
            mv = d["moves"].get(w["coin"])
            mine = [it for it in show if w["coin"] in it["coins"]]
            delisting = any(n["important"] and w["coin"] in n["coins"] for n in nts)
            money = set(w["why"]) & {"plan", "open position", "asked", "entry"}
            if not (mine or delisting or money or (mv and mv["moving"])):
                quiet.append(w["coin"])
                continue
            head = "  %s (%s)" % (w["coin"], ", ".join(w["why"]))
            if mv:
                head += "  %s 24h, %s 1h" % (cc.fmt_pct(mv["ch24"], 1), cc.fmt_pct(mv["ch1"], 1))
                if mv["moving"]:
                    head += "  MOVING: " + "; ".join(mv["moving_why"])
            elif w.get("error"):
                head += "  (price not read: %s)" % w["error"][:60]
            cc.out(head)
            for it in mine[:4]:
                cc.out(item_line(it, now, show_coins=False))
            if not mine:
                cc.out("    no %sheadline in the last %gh%s" % ("new " if not a.all else "", d["hours"],
                                                              ": a move with no story is worth saying out loud"
                                                              if mv and mv["moving"] else ""))
            if delisting:
                cc.out("    ! an exchange is delisting it: positions get closed at the delisting time.")
        if quiet:
            cc.out("  Quiet (no %sheadline, no unusual move): %s" % ("new " if not a.all else "", ", ".join(quiet)))
    # market
    rest = [it for it in show if not (set(it["coins"]) & watch_coins) and "opinion" not in it["tags"]]
    rest.sort(key=lambda it: (-int(it["important"]), -(it["at"] or 0)))
    cc.out("")
    cc.out("Market:")
    for it in rest[:a.max]:
        cc.out(item_line(it, now))
    if not rest:
        cc.out("  nothing new" if not a.all else "  nothing in the window")
    # footer
    ok = [s for s in d["sources"] if s["ok"]]
    bad = [s for s in d["sources"] if not s["ok"]]
    line = "Read %d of %d feeds" % (len(ok), len(d["sources"]))
    if bad:
        line += " (%s)" % "; ".join("%s: %s" % (s["name"], s["error"][:70]) for s in bad)
    cc.out("")
    cc.out(line + ". %d new item%s." % (sum(1 for it in show if it["new"]), "" if len(show) == 1 else "s"))
    if d.get("venue_note"):
        cc.out(d["venue_note"])
    cc.out("Headlines are leads, not facts: open the story before acting on one.")


def build_parser():
    p = argparse.ArgumentParser(prog="news.py", description="News for the coins you watch, the macro calendar, exchange notices.")
    p.add_argument("command", nargs="?", default="brief", choices=["brief", "macro", "notices"])
    p.add_argument("--coins", default=None, help="these coins, e.g. BTC,SOL (default: plan, positions, watchlist)")
    p.add_argument("--hours", type=float, default=24.0, help="look back this far (default 24)")
    p.add_argument("--all", action="store_true", help="include items already shown")
    p.add_argument("--max", type=int, default=8, help="market headlines to show (default 8)")
    p.add_argument("--countries", default="USD", help="macro: currencies, e.g. USD,EUR,CNY (default USD)")
    p.add_argument("--impact", default="high", choices=["high", "medium"], help="macro: high only, or medium too")
    p.add_argument("--days", type=float, default=7.0, help="macro / notices: how far ahead or back (default 7)")
    p.add_argument("--no-mark", action="store_true", help="do not mark items as seen")
    cc.add_common(p)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    cc.apply_common(a)
    cfg = cc.load_config()
    if a.hours <= 0 or a.hours > 24 * 14:
        raise cc.CryptoError("--hours must be between 0 and 336")
    now = cc.now_ms()
    if a.command == "macro":
        data, note = macro_calendar()
        d = {"as_of": now, "macro": data}
        rows = macro_rows(d, a, horizon_ms=int(a.days * DAY))
        if a.json:
            cc.dump({"as_of": now, "events": rows, "note": note})
            return 0
        cc.out("Macro calendar | %s | %s, %s impact | the feed covers this week" % (cc.fmt_local(now), a.countries.upper(), a.impact))
        whens = [("released %s" % ago(e["at"], now)) if e["at"] <= now else ago(e["at"], now) for e in rows]
        wide = max([len(w) for w in whens] or [0])
        for e, when in zip(rows, whens):
            extra = ", ".join(x for x in ("forecast " + e["forecast"] if e["forecast"] else "",
                                          "previous " + e["previous"] if e["previous"] else "") if x)
            cc.out(("  %-*s  %s  %s %s  %s" % (wide, when, cc.fmt_local(e["at"]), e["country"], e["title"], extra)).rstrip())
        if not rows:
            cc.out("  nothing at that impact in the window")
        if note:
            cc.out(note)
        cc.out("Unofficial feed: check the time on the agency's own calendar before trading around it.")
        return 0
    if a.command == "notices":
        try:
            known = set(m["coin"] for m in cc.get_venue(a.venue).markets())
        except cc.CryptoError:
            known = set()
        data, note = notices(known)
        rows = sorted([n for n in data if now - n["at"] <= a.days * DAY], key=lambda n: -n["at"])
        if a.json:
            cc.dump({"as_of": now, "notices": rows, "note": note})
            return 0
        cc.out("Exchange notices (Bybit) | %s | last %g days" % (cc.fmt_local(now), a.days))
        for n in rows:
            cc.out("  %-10s %-9s %s%s" % (n["kind"].replace("new_crypto", "listing").replace("delistings", "delisting"),
                                         ago(n["at"], now), n["title"], (" [%s]" % ", ".join(n["coins"])) if n["coins"] else ""))
        if not rows:
            cc.out("  none")
        if note:
            cc.out(note)
        return 0
    d = collect(a, cfg)
    fresh = [it for it in d["items"] if it["new"] and not it.get("dupe_of")] + [n for n in d["notices"] if n["new"]]
    if not a.no_write:
        mark_seen(d)
        cc.write_json(cc.wpath("news.json"), d)
    else:
        d.pop("seen", None)
    if a.json:
        cc.dump(d)
    else:
        print_brief(d, a)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(cc.run(main))
