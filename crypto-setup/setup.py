#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-setup: check the connection to each exchange, and set up crypto/config.json.

    python3 setup.py doctor     reachability, speed and clock for Binance, Bybit, Hyperliquid
    python3 setup.py init       write crypto/config.json from the example (never overwrites)
    python3 setup.py config     show the settings in force and where each one comes from

Every other skill imports cryptocore.py from this folder, so keep crypto-setup installed.
"""

import argparse
import os
import platform
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cryptocore as cc  # noqa: E402

EXAMPLE = os.path.join(HERE, "config.example.json")
KEY_VARS = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BYBIT_API_KEY", "BYBIT_API_SECRET")


def server_time(venue):
    """The exchange's clock in ms, or None when it has no public clock."""
    if venue.name == "binance":
        r = venue.get("/fapi/v1/time")
        return int(r["serverTime"])
    if venue.name == "bybit":
        r = venue.get("/v5/market/time")
        return int(r["timeNano"]) // 1000000
    return None


def probe(vn):
    v = cc.VENUES[vn]()
    row = {"venue": vn, "label": v.label, "status": "ok", "markets": None, "ms": None, "skew_s": None, "note": ""}
    t0 = time.time()
    try:
        row["markets"] = len(v.markets())
        row["ms"] = int((time.time() - t0) * 1000)
        if not cc.pinned():
            local0 = time.time()
            st = server_time(v)
            local1 = time.time()
            if st:
                row["skew_s"] = round((local0 + local1) / 2.0 - st / 1000.0, 2)
    except cc.GeoBlocked as e:
        row["status"], row["note"] = "blocked", "refuses connections from this location"
    except cc.CryptoError as e:
        row["status"], row["note"] = "down", str(e)
    return row


EXTRAS = [
    ("News feed (CoinDesk)", "GET", "https://www.coindesk.com/arc/outboundfeeds/rss/", True),
    ("Macro calendar", "GET", "https://nfs.faireconomy.media/ff_calendar_thisweek.json", False),
    ("Bybit announcements", "GET", "https://api.bybit.com/v5/announcements/index?locale=en-US&limit=1", False),
]


def probe_extra(item):
    """(label, ok, note) for a source a skill other than the exchanges reads."""
    label, method, url, raw = item
    t0 = time.time()
    try:
        cc.HTTP.request(method, url, raw=raw)
        return label, True, "answered in %d ms" % ((time.time() - t0) * 1000)
    except cc.CryptoError as e:
        return label, False, "%s (only the skill that reads it is affected)" % str(e)[:90]


def doctor(a):
    now = cc.now_ms()
    cc.out("Doctor | %s | Python %s on %s" % (cc.fmt_utc(now), platform.python_version(), platform.system() or "?"))
    if sys.version_info < (3, 8):
        cc.out("Python 3.8 or newer is needed. This is %s." % platform.python_version())
        return 1
    rows = [r for _, r, _ in cc.parallel(probe, list(cc.AUTO_ORDER), workers=3)]
    for r in rows:
        if r["status"] == "ok":
            extra = "%d markets, answered in %d ms" % (r["markets"], r["ms"])
            if r["skew_s"] is not None:
                extra += ", your clock is %+.1f s off" % r["skew_s"]
                if abs(r["skew_s"]) > 1.0:
                    extra += " (fix it before trading live: signed orders are refused past a few seconds)"
            cc.out("  %-24s ok       %s" % (r["label"], extra))
        else:
            cc.out("  %-24s %-8s %s" % (r["label"], r["status"], r["note"]))
    try:
        fg = cc.fear_greed(1)
        cc.out("  %-24s ok       %d (%s)" % ("Fear & Greed index", fg[0][1], fg[0][2]) if fg else "  Fear & Greed index      empty")
    except cc.CryptoError as e:
        cc.out("  %-24s down     %s" % ("Fear & Greed index", e))
    for label, ok, note in [r for _, r, _ in cc.parallel(probe_extra, EXTRAS, workers=3)]:
        cc.out("  %-24s %-8s %s" % (label, "ok" if ok else "down", note))
    wd = cc.workdir()
    try:
        os.makedirs(wd, exist_ok=True)
        probe_file = os.path.join(wd, ".write-test")
        with open(probe_file, "w") as fh:
            fh.write("ok")
        os.remove(probe_file)
        cc.out("  %-24s ok       %s" % ("Work folder", os.path.abspath(wd)))
    except OSError as e:
        cc.out("  %-24s problem  %s" % ("Work folder", e))
    cfgp = cc.wpath("config.json")
    cc.out("  %-24s %s" % ("Config", ("ok       " + cfgp) if os.path.exists(cfgp) else
                           "default  no crypto/config.json yet (python3 setup.py init writes one)"))
    have = [k for k in KEY_VARS if os.environ.get(k)]
    cc.out("  %-24s %s" % ("Exchange keys", ("set: " + ", ".join(have)) if have else
                           "none (fine: every skill except a live crypto-broker works without keys)"))
    best = [r for r in rows if r["status"] == "ok"]
    if not best:
        cc.out("No exchange answered from here. Check the internet connection and run this again. If the exchanges "
               "block this location, the pack cannot reach them from here.")
        return 1
    pick = best[0]
    if not a.no_write and not os.environ.get("CRYPTO_OFFLINE"):
        cc.write_json(cc.wpath("cache", "venue.json"), {"venue": pick["venue"], "at": now,
                                                        "skipped": ["%s: %s" % (r["label"], r["status"]) for r in rows if r["status"] != "ok"]})
    cc.out("Best venue from here: %s. With \"venue\": \"auto\" every skill uses it." % pick["label"])
    blocked = [r["label"] for r in rows if r["status"] == "blocked"]
    if blocked:
        cc.out("Blocked here: %s. That is the exchange's rule for this location, not a fault." % ", ".join(blocked))
    return 0


def init(a):
    dst = cc.wpath("config.json")
    if os.path.exists(dst) and not a.force:
        cc.out("%s already exists; nothing changed. Use --force to replace it with the example." % dst)
        return 0
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copyfile(EXAMPLE, dst)
    cfg = cc.load_config(dst)
    cc.out("Wrote %s. The money settings to check first:" % dst)
    for k in ("account_usd", "max_loss_per_trade_usd", "max_daily_loss_usd", "max_leverage", "margin_mode", "paper"):
        cc.out("  %-24s %s" % (k, cfg[k]))
    cc.out("Edit the file to match your account. Paper stays on until you set \"paper\": false yourself.")
    return 0


def show_config(a):
    path = cc.wpath("config.json")
    user = cc.read_json(path, {}) or {}
    cfg = cc.load_config()
    if a.json:
        cc.dump(cfg)
        return 0
    cc.out("Settings in force (%s)" % (path if user else "defaults; no crypto/config.json"))
    for k in sorted(cfg):
        src = "yours" if k in user else "default"
        cc.out("  %-24s %-28s %s" % (k, cc.json.dumps(cfg[k]), src))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="setup.py", description="Check exchanges and set up the config.")
    p.add_argument("command", nargs="?", default="doctor", choices=["doctor", "init", "config"])
    p.add_argument("--force", action="store_true", help="init: replace an existing config")
    cc.add_common(p, venue=False)
    a = p.parse_args(argv)
    cc.apply_common(a)
    if a.command == "init":
        return init(a)
    if a.command == "config":
        return show_config(a)
    return doctor(a)


if __name__ == "__main__":
    sys.exit(cc.run(main))
