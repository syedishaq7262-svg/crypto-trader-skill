#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the whole pack against live data, read-only, and say what works from here.

    python3 tools/live_check.py                    every skill on the best venue from here, about 3 to 6 minutes
    python3 tools/live_check.py --all-venues       Binance, Bybit and Hyperliquid in turn
    python3 tools/live_check.py --quick            skip the slow ones (the scout, long replays)
    python3 tools/live_check.py --record rec/      also save every public response, to replay a problem offline

It works in a fresh temporary folder with the default config (paper trading),
so your own crypto/ folder, plans and positions are never touched. It reads
public market data only: no keys (it hides them from the run), no account
calls, no orders. The one paper trade it makes is simulated in that
temporary folder.

At the end it prints a table of checks and writes live_check_report.txt with
every command's full output. Exit code 0 when every check passed.
"""

import argparse
import contextlib
import importlib.util
import io
import os
import platform
import shutil
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "crypto-setup"))
import cryptocore as cc  # noqa: E402

KEY_VARS = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BYBIT_API_KEY", "BYBIT_API_SECRET")   # hidden while it runs


def code_id():
    """The pack's version and a short fingerprint of its code, so a report says exactly which code ran."""
    import hashlib
    import json
    h = hashlib.sha1()
    for folder in sorted(os.listdir(ROOT)):
        if folder.startswith("crypto-") or folder == "tools":
            for f in sorted(os.listdir(os.path.join(ROOT, folder))):
                if f.endswith(".py"):
                    with open(os.path.join(ROOT, folder, f), "rb") as fh:
                        h.update(f.encode("utf-8") + b"\0" + fh.read().replace(b"\r\n", b"\n"))
    try:
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            version = json.load(fh).get("version", "?")
    except (OSError, ValueError):
        version = "?"
    return "%s, code %s" % (version, h.hexdigest()[:8])


def share_news_cache(src, dst):
    """Hand the news, calendar and notices caches to the next venue's folder: those sites are the same for every
    venue, and the calendar's host allows only a few requests an hour."""
    a, b = os.path.join(src, "cache"), os.path.join(dst, "cache")
    if not os.path.isdir(a):
        return
    os.makedirs(b, exist_ok=True)
    for f in os.listdir(a):
        if f.startswith(("news-", "macro", "notices")) and f.endswith(".json"):
            shutil.copyfile(os.path.join(a, f), os.path.join(b, f))

SKILLS = {
    "setup": ("crypto-setup", "setup"), "scan": ("crypto-scanner", "scan"), "derivs": ("crypto-derivs", "derivs"),
    "coin": ("crypto-coin", "coin"), "exit": ("crypto-exit", "exit"), "tm": ("crypto-timemachine", "timemachine"),
    "budget": ("crypto-budget", "size"), "liq": ("crypto-liquidation", "liq"), "risk": ("crypto-risk", "check"),
    "broker": ("crypto-broker", "broker"), "alerts": ("crypto-alerts", "alerts"), "entry": ("crypto-entry", "entry"),
    "news": ("crypto-news", "news"), "whales": ("crypto-whales", "whales"), "scout": ("crypto-scout", "scout"),
    "journal": ("crypto-journal", "journal"), "lab": ("crypto-idealab", "lab"),
}
_MODS = {}


def mod(key):
    if key not in _MODS:
        folder, name = SKILLS[key]
        path = os.path.join(ROOT, folder, name + ".py")
        spec = importlib.util.spec_from_file_location("live_check_" + name, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _MODS[key] = m
    return _MODS[key]


class Check(object):
    def __init__(self, title, key, argv, ok_codes=(0,), expect=None):
        self.title, self.key, self.argv, self.ok_codes, self.expect = title, key, argv, ok_codes, expect
        self.code, self.out, self.err, self.secs, self.result, self.note = None, "", "", 0.0, "skip", ""


def run(check):
    if callable(check.argv):                        # arguments that depend on what the venue lists
        try:
            check.argv = check.argv()
        except cc.CryptoError as e:
            check.result, check.note, check.code = "FAIL", str(e)[:140], 1
            return check
    out, err = io.StringIO(), io.StringIO()
    t0 = time.time()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cc.run(lambda: mod(check.key).main(list(check.argv)))
        except Exception:                                   # a crash is the most important thing to report
            err.write(traceback.format_exc())
            code = 99
    check.secs = time.time() - t0
    check.code, check.out, check.err = code, out.getvalue(), err.getvalue()
    if code == 99 or "Traceback (most recent call last)" in check.err:
        check.result, check.note = "FAIL", "crashed: " + (check.err.strip().splitlines() or ["?"])[-1][:120]
    elif code not in check.ok_codes:
        msg = (check.err.strip().splitlines() or check.out.strip().splitlines() or ["exit %s" % code])[-1]
        check.result, check.note = "FAIL", msg[:140]
    else:
        check.result = "PASS"
        if check.expect:
            try:
                note = check.expect(check)
            except Exception as e:                          # a broken expectation is a failed check, never a crash
                note = "!could not read the result: %s" % e
            if note and note.startswith("!"):
                check.result, check.note = "FAIL", note[1:]
            else:
                check.note = note or ""
    return check


def first_line(text, word):
    for line in text.splitlines():
        if word in line:
            return line.strip()[:140]
    return ""


def wd(*names):
    return os.path.join(os.environ["CRYPTO_DIR"], *names)


# ---------------------------------------------------------------- what each result must show

def ex_scan(c):
    d = cc.read_json(wd("watchlist.json"), {})
    if not d.get("scanned"):
        return "!no coin was scanned"
    return "%d coins scanned, %d hits" % (d["scanned"], len(d.get("hits") or []))


def ex_universe(c):
    return first_line(c.out, "Kept") or "!no universe line"


def ex_coin(c):
    for word in ("Price ", "Funding ", "Levels "):
        if word not in c.out:
            return "!the card has no %s line" % word.strip()
    return first_line(c.out, "Price ")


def ex_plan(c):
    p = cc.read_json(wd("plan.json"), {})
    if not p.get("plan_id"):
        return "!no plan written"
    return "entry %s stop %s target %s, slippage %s" % (p["entry"], p["stop"], p["target"], p["costs"].get("slippage_source", "?"))


def ex_tm(c):
    d = cc.read_json(wd("timemachine.json"), {})
    if not (d.get("verdict") or {}).get("final"):
        return "!no verdict"
    return d["verdict"]["final"][:140]


def ex_budget(c):
    d = cc.read_json(wd("orders.json"), {})
    if not d.get("orders"):
        return "!no order written"
    o = d["orders"][0]
    return "%s %s %s, %dx, max loss $%.2f" % (o["side"], o["qty"], o["coin"], o["leverage"], o["loss_at_stop_usd"])


def ex_risk(c):
    v = first_line(c.out, "PASS") or first_line(c.out, "BLOCK")
    return v or "!no PASS or BLOCK line"


def ex_news(c):
    line = first_line(c.out, "Read ")
    if not line:
        return "!no feed summary"
    try:
        got = int(line.split("Read ")[1].split(" of ")[0])
    except (IndexError, ValueError):
        return line
    return ("!only %d feeds answered: %s" % (got, line)) if got < 3 else line


def ex_macro(c):
    rows = [l for l in c.out.splitlines() if l.startswith("  ") and " USD " in l]
    return "%d US releases listed" % len(rows) if rows else "no high-impact US release in the window (fine on a quiet week)"


def ex_whales_top(c):
    rows = [l for l in c.out.splitlines() if l[:3].strip().isdigit()]
    return ("%d traders listed" % len(rows)) if rows else "!no trader listed"


def ex_text(word):
    def f(c):
        return first_line(c.out, word) or "!the output has no %r" % word
    return f


# ---------------------------------------------------------------- the run

def multiplier_coin(venue):
    """A coin this venue prices per 1,000 (1000PEPE, kPEPE, ...): the trickiest symbols to get right."""
    ms = [m for m in cc.get_venue(venue).markets() if m.get("multiplier", 1) > 1]
    if not ms:
        raise cc.CryptoError("this venue lists no coin priced per 1,000")
    for want in ("PEPE", "BONK", "SHIB", "FLOKI"):
        for m in ms:
            if m["coin"] == want:
                return want
    return ms[0]["coin"]


def checks_for(venue, quick):
    v = ["--venue", venue]
    cs = [
        Check("scanner universe", "scan", ["universe", "--top", "60"] + v, expect=ex_universe),
        Check("scanner scan", "scan", ["--top", "40" if quick else "80"] + v, ok_codes=(0, 1), expect=ex_scan),
        Check("derivs radar", "derivs", ["radar", "--top", "30"] + v, expect=ex_text("Funding")),
        Check("derivs coin BTC", "derivs", ["coin", "BTC"] + v),
        Check("derivs regime", "derivs", ["regime"] + v, expect=ex_text("Regime")),
        Check("coin BTC", "coin", ["BTC"] + v, expect=ex_coin),
        Check("coin ETH", "coin", ["ETH"] + v, expect=ex_coin),
        Check("coin priced per 1,000", "coin", lambda: [multiplier_coin(venue)] + v, expect=ex_coin),
        Check("entry orb BTC,ETH,SOL", "entry", ["check", "BTC,ETH,SOL", "--setup", "orb", "--session", "ny"] + v, ok_codes=(0, 1)),
        Check("entry breakout SOL", "entry", ["check", "SOL", "--setup", "breakout"] + v, ok_codes=(0, 1)),
        Check("entry squeeze ETH", "entry", ["check", "ETH", "--setup", "squeeze"] + v, ok_codes=(0, 1)),
        Check("exit plan BTC long", "exit", ["plan", "BTC", "--side", "long", "--manual", "--stop", "pct:2"] + v, expect=ex_plan),
        Check("timemachine run", "tm", ["run", "--replays", "1000" if quick else "4000"], expect=ex_tm),
        Check("budget size", "budget", [], expect=ex_budget),
        Check("liquidation check", "liq", ["check"], ok_codes=(0, 1)),
        Check("liquidation at ETH 5x short", "liq", ["at", "ETH", "--side", "short", "--leverage", "5"] + v),
        Check("risk check", "risk", [], ok_codes=(0, 1), expect=ex_risk),
        Check("broker ticket", "broker", ["ticket"], ok_codes=(0, 1), expect=ex_text("Ticket")),
        Check("idea lab EMA cross", "lab", ["test", "--when", "crosses_above(ema(9), ema(21))", "--side", "long",
                                            "--coins", "top:3", "--days", "20"] + v, expect=ex_text("Verdict:")),
    ]
    return cs


def paper_trade(report, venue):
    """Place the checked order on paper if it is READY, then sync it and read it back."""
    out = []
    o = (cc.read_json(wd("orders.json"), {}) or {}).get("orders") or []
    if not o or (o[0].get("risk") or {}).get("verdict") != "PASS":
        return out
    fp = cc.fingerprint(o[0])
    out.append(Check("broker paper place", "broker", ["place", "--yes", fp], expect=ex_text("Placed (paper)")))
    out.append(Check("broker positions", "broker", ["positions"], expect=ex_text("PAPER")))
    out.append(Check("broker sync", "broker", ["sync"], expect=ex_text("Synced")))
    out.append(Check("alerts from the position", "alerts", ["add", "--from-positions"], expect=ex_text("Added")))
    out.append(Check("alerts check", "alerts", ["check"], ok_codes=(0, 1)))
    coin = o[0]["coin"]
    out.append(Check("broker paper close", "broker", ["close", coin, "--yes", coin], expect=ex_text("Closed")))
    return out


def common_checks(venue, quick):
    v = ["--venue", venue]
    cs = [
        Check("alerts quote", "alerts", ["quote", "BTC,ETH,SOL"] + v, expect=ex_text("BTC")),
        Check("news brief", "news", ["--coins", "BTC,ETH,SOL", "--all"] + v, ok_codes=(0, 1), expect=ex_news),
        Check("news macro", "news", ["macro"], expect=ex_macro),
        Check("news notices", "news", ["notices"] + v, expect=ex_text("Exchange notices")),
        Check("whales top", "whales", ["top", "--n", "10"], expect=ex_whales_top),
        Check("whales coin BTC", "whales", ["coin", "BTC", "--n", "15"], expect=ex_text("Whales on BTC")),
        Check("journal", "journal", [], ok_codes=(0, 1)),
    ]
    if not quick:
        cs.append(Check("scout", "scout", ["--top", "3", "--replays", "500"] + v, ok_codes=(0, 1),
                        expect=ex_text("tested on each coin")))
    return cs


def whale_wallet_check():
    rows = (cc.read_json(wd("cache", "hl_leaderboard.json"), {}) or {}).get("rows") or []
    if not rows:
        return []
    best = sorted(rows, key=lambda r: -r["perf"].get("month", {}).get("pnl", 0.0))[0]
    return [Check("whales wallet (the month's best)", "whales", ["wallet", best["address"]], expect=ex_text("Wallet"))]


def main(argv=None):
    p = argparse.ArgumentParser(prog="live_check.py", description="Run the pack against live data, read-only.")
    p.add_argument("--all-venues", action="store_true", help="check Binance, Bybit and Hyperliquid in turn")
    p.add_argument("--venue", default=None, help="check this venue only")
    p.add_argument("--quick", action="store_true", help="skip the scout and use fewer replays")
    p.add_argument("--record", default=None, metavar="DIR", help="save every public response to DIR")
    p.add_argument("--keep", action="store_true", help="keep the temporary work folder")
    p.add_argument("--report", default="live_check_report.txt", help="where to write the full report")
    a = p.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="crypto-live-check-")
    saved_env = dict((k, os.environ.get(k)) for k in ("CRYPTO_DIR", "CRYPTO_RECORD", "CRYPTO_OFFLINE", "CRYPTO_NOW",
                                                      "CRYPTO_CONFIG") + KEY_VARS)
    for k in saved_env:
        os.environ.pop(k, None)
    os.environ["CRYPTO_DIR"] = tmp
    if a.record:
        os.environ["CRYPTO_RECORD"] = os.path.abspath(a.record)
    cc.set_now(None)
    cc.reset_http()
    started = time.time()
    done = []
    lines = []

    def say(text=""):
        cc.out(text)                                   # never fails on a console that cannot show a character
        sys.stdout.flush()
        lines.append(text)

    say("Live check | crypto-trader-skill %s | %s | Python %s on %s %s" % (
        code_id(), cc.fmt_utc(cc.now_ms()), platform.python_version(), platform.system(), platform.release()))
    say("Work folder %s (temporary; your own crypto/ folder is not used)." % tmp)
    say("Exchange keys are hidden from this run: it reads public data and trades on paper only.")
    if os.environ.get("CRYPTO_FAKE_EXCHANGE"):
        say("CRYPTO_FAKE_EXCHANGE is set: this run uses the pack's test exchange, NOT live data.")
    try:
        doc = run(Check("setup doctor", "setup", ["doctor"], ok_codes=(0,)))
        done.append(doc)
        for line in doc.out.splitlines():
            say("  " + line)
        reachable = [vn for vn in cc.AUTO_ORDER
                     if any(line.strip().startswith(cc.VENUES[vn].label) and " ok " in line for line in doc.out.splitlines())]
        if a.venue:
            venues = [a.venue]
        elif a.all_venues:
            venues = reachable
        else:
            venues = reachable[:1]
            if "hyperliquid" in reachable and "hyperliquid" not in venues:
                venues.append("hyperliquid")            # its data paths differ the most: always worth a look
        if not venues:
            say("No venue answered from here, so nothing else can be checked.")
        prev = None
        for vn in venues:
            os.environ["CRYPTO_DIR"] = os.path.join(tmp, vn)       # each venue starts clean: no state carries over
            if prev:
                share_news_cache(os.path.join(tmp, prev), os.path.join(tmp, vn))
            prev = vn
            say("")
            say("== %s ==" % cc.VENUES[vn].label)
            for c in checks_for(vn, a.quick):
                run(c)
                done.append(c)
                say("%-4s %-32s %6.1fs  %s" % (c.result, c.title, c.secs, c.note))
            for c in paper_trade(lines, vn):
                run(c)
                done.append(c)
                say("%-4s %-32s %6.1fs  %s" % (c.result, c.title, c.secs, c.note))
        if venues:
            os.environ["CRYPTO_DIR"] = os.path.join(tmp, venues[0])
            say("")
            say("== News, notices, whales, journal, scout ==")
            for c in common_checks(venues[0], a.quick) + whale_wallet_check():
                run(c)
                done.append(c)
                say("%-4s %-32s %6.1fs  %s" % (c.result, c.title, c.secs, c.note))
    except KeyboardInterrupt:
        say("Stopped.")
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    failed = [c for c in done if c.result == "FAIL"]
    say("")
    say("%d of %d checks passed in %.0f seconds." % (len(done) - len(failed), len(done), time.time() - started))
    with open(a.report, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n\n")
        for c in done:
            fh.write("=" * 100 + "\n%s %s | %s | exit %s | %.1fs\n$ %s %s\n" % (
                c.result, c.title, SKILLS[c.key][0], c.code, c.secs, SKILLS[c.key][1] + ".py", " ".join(c.argv)))
            fh.write(c.out)
            if c.err.strip():
                fh.write("--- stderr ---\n" + c.err)
            fh.write("\n")
    say("Full output of every command: %s" % os.path.abspath(a.report))
    if failed:
        say("Something failed. Send the report%s to whoever is fixing it; it holds no keys and no account data." % (
            " and the recording folder" if a.record else ""))
    if a.keep:
        say("Kept the work folder: %s" % tmp)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
