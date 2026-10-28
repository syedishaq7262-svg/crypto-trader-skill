"""Draws the README's results chart from the published backtest trades.

Every setup's average trade before and after fees, slippage and funding, read from the trade files in
backtest/ and drawn as one dot for each, light and dark. Nothing in the chart is typed in by hand: change the
trades and the chart changes with them.

  python3 tools/chart.py           # write docs/backtest-light.svg and docs/backtest-dark.svg
  python3 tools/chart.py --check   # exit 1 if the charts on disk are not the trades' (the tests run this)
"""

import argparse
import csv
import datetime
import glob
import gzip
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADE_FILES = os.path.join(ROOT, "backtest", "*-trades.csv.gz")
OUT = os.path.join(ROOT, "docs")
NAMES = (("squeeze", "Squeeze break"), ("reversal", "RSI reversal"), ("volume", "Volume spike"),
         ("breakout", "Range breakout"), ("trend", "Trend pullback"), ("orb-ny", "Opening range, New York"),
         ("orb-utc", "Opening range, UTC day"))
MINUS = "−"
FONT = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
THEMES = {   # surfaces, ink and the one accent; the gray is the de-emphasised "before" series
    "light": {"surface": "#fcfcfb", "ring": "#e1e0d9", "primary": "#0b0b0b", "secondary": "#52514e",
              "muted": "#6f6e69", "grid": "#e9e8e2", "zero": "#898781", "connector": "#c3c2b7",
              "before": "#898781", "after": "#2a78d6"},
    "dark": {"surface": "#1a1a19", "ring": "#2c2c2a", "primary": "#f0efec", "secondary": "#c3c2b7",
             "muted": "#9c9b94", "grid": "#2c2c2a", "zero": "#898781", "connector": "#4b4a46",
             "before": "#898781", "after": "#3987e5"},
}
W, X0, X1 = 840, 340, 800          # the plot runs from X0 to X1
LO, HI = -0.45, 0.15               # in R
ROW, GAP, TOP = 34, 14, 150


def read_trades(pattern=TRADE_FILES):
    trades = []
    for path in sorted(glob.glob(pattern)):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                trades.append({"setup": row["setup"], "before": float(row["r_before_costs"]),
                               "after": float(row["r_after_costs"]), "when": row["signal_utc"]})
    return trades


def summarise(trades):
    def avg(group, key):
        return math.fsum(t[key] for t in group) / len(group)        # fsum: the same answer on every Python

    rows = []
    for key, label in NAMES:
        g = [t for t in trades if t["setup"] == key]
        if g:
            rows.append({"label": label, "n": len(g), "before": avg(g, "before"), "after": avg(g, "after")})
    rows.sort(key=lambda r: -r["after"])
    total = {"label": "All setups", "n": len(trades), "before": avg(trades, "before"), "after": avg(trades, "after")}
    days = sorted(t["when"][:10] for t in trades)
    return {"all": total, "rows": rows, "first": days[0], "last": days[-1]}


def r_text(v):
    s = "%+.2f" % v
    if s in ("+0.00", "-0.00"):
        return "0.00R"
    return s.replace("-", MINUS) + "R"


def day_text(day, year=False):
    d = datetime.datetime.strptime(day, "%Y-%m-%d")
    return "%d %s%s" % (d.day, d.strftime("%b"), " %d" % d.year if year else "")


def title(s):
    lost = all(r["after"] < 0 for r in s["rows"])
    even = abs(s["all"]["before"]) < 0.05
    if lost and even:
        return "Before costs, about even. After costs, every setup lost."
    return "Each setup's average trade, before and after costs"


def x(v):
    return X0 + (v - LO) / (HI - LO) * (X1 - X0)


def svg(s, theme):
    t = THEMES[theme]
    rows = [s["all"]] + s["rows"]
    ys = [TOP + ROW / 2.0] + [TOP + ROW + GAP + ROW * i + ROW / 2.0 for i in range(len(s["rows"]))]
    bottom = ys[-1] + ROW / 2.0 + 4
    h = int(bottom + 70)
    head = title(s)
    span = "%s to %s" % (day_text(s["first"]), day_text(s["last"], year=True))
    num = 'style="font-variant-numeric: tabular-nums"'
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
         'aria-labelledby="title desc">' % (W, h, W, h),
         '<title id="title">%s</title>' % head,
         '<desc id="desc">Average result a trade in R, before and after costs, over %s walk-forward trades on '
         'Binance, %s. %s.</desc>' % ("{:,}".format(s["all"]["n"]), span, "; ".join(
             "%s: %s before costs, %s after, %s trades" % (r["label"], r_text(r["before"]), r_text(r["after"]),
                                                            "{:,}".format(r["n"])) for r in rows)),
         '<rect x="0.5" y="0.5" width="%d" height="%d" rx="10" fill="%s" stroke="%s"/>' % (
             W - 1, h - 1, t["surface"], t["ring"]),
         '<g font-family="%s">' % FONT,
         '<text x="28" y="42" font-size="20" font-weight="600" fill="%s">%s</text>' % (t["primary"], head),
         '<text x="28" y="67" font-size="13.5" fill="%s">Average result a trade, in R: 1R is the amount at risk.</text>'
         % t["secondary"],
         '<text x="28" y="87" font-size="13.5" fill="%s">%s walk-forward trades on Binance perpetuals with fake money, '
         '%s.</text>' % (t["secondary"], "{:,}".format(s["all"]["n"]), span)]
    ly = 118                                                         # the legend
    for cx, colour, words in ((34, t["before"], "before fees, slippage and funding"), (318, t["after"], "after them")):
        o.append('<circle cx="%d" cy="%d" r="5.5" fill="%s"/>' % (cx, ly, colour))
        o.append('<text x="%d" y="%.1f" font-size="13" fill="%s">%s</text>' % (cx + 12, ly + 4.5, t["secondary"], words))
    for i in range(-4, 2):                                           # grid, zero and the axis
        v = i / 10.0
        colour, width = (t["zero"], 1.25) if i == 0 else (t["grid"], 1)
        o.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="%s"/>' % (
            x(v), TOP - 6, x(v), bottom, colour, width))
        label = "0" if i == 0 else ("%+.1fR" % v).replace("-", MINUS)
        o.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s" text-anchor="middle" %s>%s</text>' % (
            x(v), bottom + 20, t["muted"], num, label))
    rule = ys[0] + ROW / 2.0 + GAP / 2.0                             # sets the total apart from the setups
    o.append('<line x1="28" y1="%.1f" x2="%d" y2="%.1f" stroke="%s" stroke-width="1"/>' % (rule, X1, rule, t["grid"]))
    for i, (r, y) in enumerate(zip(rows, ys)):
        total = i == 0
        ink = t["primary"] if total else t["secondary"]
        xb, xa = x(r["before"]), x(r["after"])
        o.append('<text x="28" y="%.1f" font-size="14" font-weight="%d" fill="%s">%s</text>' % (
            y + 5, 600 if total else 400, ink, r["label"]))
        o.append('<text x="%d" y="%.1f" font-size="12" fill="%s" text-anchor="end" %s>%s trades</text>' % (
            X0 - 26, y + 4.5, t["muted"], num, "{:,}".format(r["n"])))
        o.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2"/>' % (
            xb, y, xa, y, t["connector"]))
        for cx, colour in ((xb, t["before"]), (xa, t["after"])):
            o.append('<circle cx="%.1f" cy="%.1f" r="5.5" fill="%s" stroke="%s" stroke-width="2"/>' % (
                cx, y, colour, t["surface"]))
        o.append('<text x="%.1f" y="%.1f" font-size="12" font-weight="%d" fill="%s" text-anchor="end" %s>%s</text>' % (
            xa - 11, y + 4.5, 600 if total else 500, ink, num, r_text(r["after"])))
        if total:                                                    # the one "before" number worth printing
            o.append('<text x="%.1f" y="%.1f" font-size="12" fill="%s" %s>%s</text>' % (
                xb + 11, y + 4.5, t["muted"], num, r_text(r["before"])))
    o.append('<text x="28" y="%d" font-size="11.5" fill="%s">Every trade is in backtest/, with the reports. '
             'Past results say nothing certain about the future.</text>' % (h - 18, t["muted"]))
    o.append('</g>')
    o.append('</svg>')
    return "\n".join(o) + "\n"


def charts(trades=None):
    s = summarise(read_trades() if trades is None else trades)
    return {"backtest-%s.svg" % theme: svg(s, theme) for theme in ("light", "dark")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the charts on disk are not the trades'")
    ap.add_argument("--out", default=OUT, help="folder to write to (default docs/)")
    a = ap.parse_args(argv)
    stale = []
    for name, text in sorted(charts().items()):
        path = os.path.join(a.out, name)
        if a.check:
            try:
                with open(path, encoding="utf-8", newline="") as fh:
                    if fh.read() != text:
                        stale.append(name)
            except IOError:
                stale.append(name)
            continue
        if not os.path.isdir(a.out):
            os.makedirs(a.out)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("wrote %s" % os.path.relpath(path, ROOT))
    if stale:
        print("out of date: %s (run python3 tools/chart.py)" % ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
