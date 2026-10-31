#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crypto-idealab: a trading idea of your own, tested on the past after costs, before any money is at risk.

    python3 lab.py words                                         the words a rule can use
    python3 lab.py check --when "rsi(14) < 30 and close > ema(200)"
                                                                 read a rule without testing it
    python3 lab.py test --when "rsi(14) < 30 and close > ema(200)" --side long
    python3 lab.py test --when "funding < -0.01" --side long --coins SOL,ETH --days 365 --stop pct:3 --hold 3d
    python3 lab.py test --idea crypto/ideas/rsi-dip.json         a saved idea again (flags change it)
    python3 lab.py list                                          the ideas tested so far and how each did

How a test works. The rule is read at the close of every bar, from that bar and the bars before it, never a later
one. When it is true and no trade is open on that coin, a trade opens at the next bar's open, at the market. The
stop, the target, the time limit and an exit rule of your own close it, bar by bar: a bar that touches both the
stop and the target counts as the stop, and a bar that opens past the stop fills at its open. Every trade pays the
exchange's fees, slippage measured from the coin's order book today, and the funding actually charged while it
was open. Fake money: nothing reads or touches an account.

Then the honest part: the average with a 90% range, before and after costs, both halves of the period, the trades
within the cost rule, and random entries at the same hours with the same exits: could luck have done as well?
Every distinct rule tested is counted, because try enough versions of an idea and one will look good by chance.
"""

import argparse
import ast
import bisect
import collections
import csv
import gzip
import hashlib
import json
import math
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
for _d in (HERE, os.path.join(HERE, os.pardir, "crypto-setup")):
    if os.path.isfile(os.path.join(_d, "cryptocore.py")):
        sys.path.insert(0, os.path.abspath(_d))
        break
else:
    sys.stderr.write("Problem: cryptocore.py not found. Install the crypto-setup skill next to this one.\n")
    sys.exit(2)
import cryptocore as cc  # noqa: E402

T, O, H, L, C, V = cc.T, cc.O, cc.H, cc.L, cc.C, cc.V
DAY = 86400000
HOUR = 3600000
TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
DAYS_DEFAULT = {"5m": 30, "15m": 60, "1h": 180, "4h": 365, "1d": 730}
DAYS_MAX = {"5m": 120, "15m": 240, "1h": 730, "4h": 1460, "1d": 2190}
MAX_RULE_CHARS = 400
MAX_RULE_NODES = 160
MIN_TRADES = 30
RANDOM_PER_TRADE = 50
LUCK_REPS = 2000
MAX_COINS = 60
MIN_STOP = 0.0005              # a stop closer than 0.05% of the price is no stop at all
IDEAS = "ideas"
TRIED = "tried.jsonl"
DEFAULTS = {"side": None, "timeframe": "1h", "stop": "atr:2", "target": "r:2", "hold": "24h", "exit_when": None,
            "coins": "top:10", "days": None, "end": None, "venue": None, "name": None}


class RuleError(cc.CryptoError):
    pass


# ---------------------------------------------------------------- the words a rule can use

VALUES = collections.OrderedDict([
    ("open", "the bar's open"),
    ("high", "the bar's high"),
    ("low", "the bar's low"),
    ("close", "the bar's close"),
    ("price", "the same as close"),
    ("volume", "the bar's volume, in coins"),
    ("hour", "the hour the bar opened, UTC (0 to 23)"),
    ("weekday", "the day the bar opened, UTC (0 Monday to 6 Sunday)"),
    ("funding", "the last funding rate charged, in percent as the exchange shows it (0.01 means 0.01%)"),
    ("btc_close", "BTC's close on the same bar"),
    ("day_open", "the open of the current UTC day"),
    ("prev_day_high", "yesterday's high (UTC day)"),
    ("prev_day_low", "yesterday's low (UTC day)"),
    ("prev_day_close", "yesterday's close (UTC day)"),
])

# name: (kind, default value, [(parameter, default or None when it must be given, low, high, whole number)], words)
#   series: an optional value first (close unless said), then numbers; bars: this coin's bars, numbers only;
#   expr: any value first, then numbers; pair: two values


def _N(d, lo=1, hi=1000):
    return ("n", d, lo, hi, True)


_BB = [_N(20, 2), ("k", 2.0, 0.1, 10.0, False)]
_MACD = [("fast", 12, 1, 500, True), ("slow", 26, 2, 1000, True)]
FUNCS = collections.OrderedDict([
    ("sma", ("series", "close", [_N(None)], "simple average of the last n values: sma(50), sma(volume, 20)")),
    ("ema", ("series", "close", [_N(None)], "exponential average: ema(200), ema(btc_close, 50)")),
    ("rsi", ("series", "close", [_N(14, 2, 500)], "relative strength index, 0 to 100: rsi(14)")),
    ("change", ("series", "close", [_N(1)], "percent change over the last n bars: change(24) > 5 is up more than 5%")),
    ("highest", ("series", "high", [_N(20)], "the highest value of the n bars BEFORE this one: close > highest(20)")),
    ("lowest", ("series", "low", [_N(20)], "the lowest value of the n bars before this one: close < lowest(20)")),
    ("stdev", ("series", "close", [_N(20, 2)], "standard deviation of the last n values")),
    ("bb_upper", ("series", "close", _BB, "upper Bollinger band: bb_upper(20, 2)")),
    ("bb_lower", ("series", "close", _BB, "lower Bollinger band: bb_lower(20, 2)")),
    ("bb_width", ("series", "close", _BB, "band width, in percent of the middle band (low means a squeeze)")),
    ("macd", ("series", "close", _MACD, "MACD line: macd(12, 26)")),
    ("macd_signal", ("series", "close", _MACD + [("signal", 9, 1, 500, True)], "MACD signal line: macd_signal(12, 26, 9)")),
    ("macd_hist", ("series", "close", _MACD + [("signal", 9, 1, 500, True)], "MACD line minus its signal line")),
    ("atr", ("bars", None, [_N(14)], "average true range of this coin, in price: close - open > 2 * atr(14)")),
    ("vol_ratio", ("bars", None, [_N(20)], "this bar's volume over the average of the n bars before: vol_ratio(20) > 3")),
    ("ago", ("expr", None, [_N(1)], "a value n bars ago: rsi(14) > ago(rsi(14), 1)")),
    ("crosses_above", ("pair", None, [], "true on the bar where the first moves from at or below the second to above it")),
    ("crosses_below", ("pair", None, [], "true on the bar where the first moves from at or above the second to below it")),
    ("abs", ("expr", None, [], "a value without its sign: abs(change(1)) > 3")),
    ("min", ("pair", None, [], "the smaller of two values")),
    ("max", ("pair", None, [], "the larger of two values")),
])
EXAMPLES = [
    ("rsi(14) < 30 and close > ema(200)", "an oversold dip in an uptrend"),
    ("crosses_above(ema(9), ema(21))", "a fast average crossing above a slow one"),
    ("close > highest(20) and vol_ratio(20) > 2", "a break of the last 20 bars' high on twice the usual volume"),
    ("funding < -0.01", "shorts paying longs more than 0.01% a funding period"),
    ("btc_close > ema(btc_close, 200) and change(24) < -5", "down 5% in a day (on 1h bars) while BTC is in an uptrend"),
    ("close > prev_day_high and hour >= 13 and hour < 16", "a break of yesterday's high in the first hours of the US session"),
]
OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
CMPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}
CMP_FN = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
          "==": lambda a, b: a == b, "!=": lambda a, b: a != b}


def _num(x):
    return ("%d" % x) if float(x) == int(x) else ("%g" % x)


class Rule(object):
    """A rule, read safely: Python's own parser makes the tree, and only the words above, numbers, comparisons,
    and/or/not and + - * / may appear in it. Nothing in a rule is ever run as code."""

    def __init__(self, text, what="rule"):
        self.what = what
        self.source = " ".join(str(text or "").split())
        if not self.source:
            raise RuleError("the %s is empty. Run lab.py words for what a rule can say." % what)
        if len(self.source) > MAX_RULE_CHARS:
            raise RuleError("the %s is longer than %d characters; split the idea into a simpler rule" % (what, MAX_RULE_CHARS))
        src = re.sub(r"\b(AND|OR|NOT)\b", lambda m: m.group(1).lower(), self.source)
        src = re.sub(r"(?<![<>=!])=(?!=)", "==", src)
        try:
            tree = ast.parse(src, mode="eval")
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            raise RuleError("could not read the %s %r. Write it like: rsi(14) < 30 and close > ema(200)" % (what, self.source))
        self.nodes = 0
        self.needs = set()
        kind, self.ir = self._check(tree.body)
        if kind != "bool":
            raise RuleError("the %s %r is a number, not a condition. Compare it to something: %s > 50" % (
                what, self.source, self.source))
        if not _reads_market(self.ir):
            raise RuleError("the %s %r does not read the market: it is always true or always false" % (what, self.source))
        self.text = render(self.ir)

    # -- checking, and building a small tree of tuples (ir) that the evaluator walks
    def _check(self, node):
        self.nodes += 1
        if self.nodes > MAX_RULE_NODES:
            raise RuleError("the %s has too many parts; keep it to a few conditions" % self.what)
        if isinstance(node, ast.BoolOp):
            parts = []
            for v in node.values:
                k, ir = self._check(v)
                if k != "bool":
                    raise RuleError("'and' and 'or' join conditions, like rsi(14) < 30 and close > ema(200); %r is a number"
                                    % render(ir))
                parts.append(ir)
            return "bool", ("and" if isinstance(node.op, ast.And) else "or", tuple(parts))
        if isinstance(node, ast.UnaryOp):
            k, ir = self._check(node.operand)
            if isinstance(node.op, ast.Not):
                if k != "bool":
                    raise RuleError("'not' goes in front of a condition, like not (rsi(14) > 70)")
                return "bool", ("not", ir)
            if isinstance(node.op, (ast.USub, ast.UAdd)) and k == "num":
                if isinstance(node.op, ast.UAdd):
                    return "num", ir
                return "num", (("num", -ir[1]) if ir[0] == "num" else ("neg", ir))
            raise RuleError("could not read %r in the %s" % (self.source, self.what))
        if isinstance(node, ast.BinOp):
            if type(node.op) not in OPS:
                raise RuleError("a rule can do arithmetic with + - * / only")
            kl, a = self._check(node.left)
            kr, b = self._check(node.right)
            if kl != "num" or kr != "num":
                raise RuleError("+ - * / work on numbers, not on conditions")
            return "num", ("bin", OPS[type(node.op)], a, b)
        if isinstance(node, ast.Compare):
            ops = []
            for op in node.ops:
                if type(op) not in CMPS:
                    raise RuleError("compare with < <= > >= == or !=")
                ops.append(CMPS[type(op)])
            items = []
            for x in [node.left] + list(node.comparators):
                k, ir = self._check(x)
                if k != "num":
                    raise RuleError("compare numbers, like rsi(14) < 30, not conditions")
                items.append(ir)
            return "bool", ("cmp", tuple(ops), tuple(items))
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise RuleError("%r is not a number a rule can use" % (v,))
            return "num", ("num", float(v))
        if isinstance(node, ast.Name):
            name = node.id
            if name in VALUES:
                self._note(name)
                return "num", ("val", "close" if name == "price" else name)
            if name in FUNCS:
                return self._call(name, [])
            raise RuleError("%r is not a word a rule can use.%s Run lab.py words for the list." % (name, _close_word(name)))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCS:
                what = node.func.id if isinstance(node.func, ast.Name) else "that"
                if what in VALUES:
                    raise RuleError("%s is a value, not a function: write %s, without brackets" % (what, what))
                raise RuleError("%r is not a function a rule can use.%s Run lab.py words for the list." % (
                    what, _close_word(what)))
            if node.keywords:
                raise RuleError("write %s's numbers in order, without names: %s" % (node.func.id, _example(node.func.id)))
            return self._call(node.func.id, list(node.args))
        raise RuleError("a rule can use numbers, the words from lab.py words, comparisons, and, or, not and + - * /; "
                        "%r has something else in it" % self.source)

    def _note(self, name):
        if name == "funding":
            self.needs.add("funding")
        elif name.startswith("btc_"):
            self.needs.add("btc")
        elif name.startswith(("day_", "prev_day_")):
            self.needs.add("day")

    def _constant(self, node, name, kind="series"):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self._constant(node.operand, name, kind)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if kind == "bars":
            raise RuleError("%s reads this coin's own bars and takes plain numbers only: %s" % (name, _example(name)))
        raise RuleError("%s takes plain numbers after the value: %s" % (name, _example(name)))

    @staticmethod
    def _is_number(node):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            node = node.operand
        return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)

    def _call(self, name, args):
        self.nodes += len(args)
        kind, default, params, _ = FUNCS[name]
        if kind == "pair":
            if len(args) != 2:
                raise RuleError("%s takes two values: %s" % (name, _example(name)))
            ka, a = self._check(args[0])
            kb, b = self._check(args[1])
            if ka != "num" or kb != "num":
                raise RuleError("%s compares two values, not conditions" % name)
            return ("bool" if name.startswith("crosses") else "num"), ("pair", name, a, b)
        series = None
        if kind == "series":
            if args and not self._is_number(args[0]):
                k, series = self._check(args.pop(0))
                if k != "num":
                    raise RuleError("%s works on a value, not a condition" % name)
            elif default:
                self._note(default)
        elif kind == "expr":
            if not args:
                raise RuleError("%s needs a value first: %s" % (name, _example(name)))
            k, series = self._check(args.pop(0))
            if k != "num":
                raise RuleError("%s works on a value, not a condition" % name)
        if len(args) > len(params):
            raise RuleError("%s takes %s: %s" % (name, _count_words(len(params)), _example(name)))
        values = []
        for i, (pname, dflt, lo, hi, whole) in enumerate(params):
            v = self._constant(args[i], name, kind) if i < len(args) else dflt
            if v is None:
                raise RuleError("%s needs its %s: %s" % (name, pname, _example(name)))
            if whole and float(v) != int(v):
                raise RuleError("%s's %s is a number of bars, a whole number: %s" % (name, pname, _example(name)))
            if not lo <= v <= hi:
                raise RuleError("%s's %s must be between %s and %s" % (name, pname, _num(lo), _num(hi)))
            values.append(int(v) if whole else float(v))
        if name.startswith("macd") and values[0] >= values[1]:
            raise RuleError("macd's fast length must be shorter than its slow length: macd(12, 26)")
        return "num", ("fn", name, series, tuple(values))

    def lookback(self, step):
        """Bars of history the rule needs before its first honest reading (averages settle over about 3x their length)."""
        return min(_lookback(self.ir, step), 5000)


def _reads_market(ir):
    tag = ir[0]
    if tag == "num":
        return False
    if tag == "val":
        return True
    if tag in ("neg", "not"):
        return _reads_market(ir[1])
    if tag in ("bin", "pair"):
        return _reads_market(ir[2]) or _reads_market(ir[3])
    if tag == "cmp":
        return any(_reads_market(x) for x in ir[2])
    if tag in ("and", "or"):
        return any(_reads_market(x) for x in ir[1])
    kind = FUNCS[ir[1]][0]
    if kind == "bars":
        return True
    return (kind == "series") if ir[2] is None else _reads_market(ir[2])


def _count_words(n):
    return "no numbers" if n == 0 else "one number" if n == 1 else "%d numbers" % n


def _example(name):
    words = FUNCS.get(name, (None, None, None, ""))[3]
    m = re.search(r":\s*(.+)$", words or "")
    return m.group(1) if m else "%s(...)" % name


def _close_word(name):
    import difflib
    hits = difflib.get_close_matches(str(name).lower(), list(VALUES) + list(FUNCS), n=1, cutoff=0.6)
    return (" Did you mean %s?" % hits[0]) if hits else ""


def render(ir, top=True):
    tag = ir[0]
    if tag == "num":
        return _num(ir[1])
    if tag == "val":
        return ir[1]
    if tag == "neg":
        return "-" + render(ir[1], False)
    if tag == "bin":
        s = "%s %s %s" % (render(ir[2], False), ir[1], render(ir[3], False))
        return s if top else "(%s)" % s
    if tag == "cmp":
        out = render(ir[2][0], True)
        for op, x in zip(ir[1], ir[2][1:]):
            out += " %s %s" % (op, render(x, True))
        return out
    if tag in ("and", "or"):
        parts = []
        for x in ir[1]:
            s = render(x, False)
            parts.append("(%s)" % s if x[0] in ("and", "or") and x[0] != tag else s)
        return (" %s " % tag).join(parts)
    if tag == "not":
        inner = render(ir[1], False)
        return "not (%s)" % inner if ir[1][0] in ("and", "or", "cmp") else "not " + inner
    if tag == "pair":
        return "%s(%s, %s)" % (ir[1], render(ir[2]), render(ir[3]))
    if tag == "fn":
        name, series, params = ir[1], ir[2], ir[3]
        bits = [] if series is None else [render(series)]
        bits += [_num(p) for p in params]
        return "%s(%s)" % (name, ", ".join(bits))
    return "?"


def _lookback(ir, step):
    tag = ir[0]
    per_day = max(1, DAY // step)
    if tag == "num":
        return 0
    if tag == "val":
        return 2 * per_day + 1 if ir[1].startswith(("day_", "prev_day_")) else 0
    if tag == "neg" or tag == "not":
        return _lookback(ir[1], step)
    if tag == "bin":
        return max(_lookback(ir[2], step), _lookback(ir[3], step))
    if tag in ("cmp",):
        return max(_lookback(x, step) for x in ir[2])
    if tag in ("and", "or"):
        return max(_lookback(x, step) for x in ir[1])
    if tag == "pair":
        return max(_lookback(ir[2], step), _lookback(ir[3], step)) + 1
    name, series, p = ir[1], ir[2], ir[3]
    inner = _lookback(series, step) if series is not None else 0
    own = {"ema": 3 * p[0] if p else 0, "rsi": 3 * p[0] if p else 0, "atr": 3 * p[0] if p else 0,
           "highest": p[0] + 1 if p else 0, "lowest": p[0] + 1 if p else 0, "vol_ratio": p[0] + 1 if p else 0}.get(name)
    if own is None:
        if name == "macd":
            own = 3 * p[1]
        elif name in ("macd_signal", "macd_hist"):
            own = 3 * p[1] + 3 * p[2]
        else:
            own = p[0] if p else 0
    return inner + own


# ---------------------------------------------------------------- series arithmetic (lists with None for "not yet")

def _segments(xs):
    """(start, stop) of each run of values with no None in it."""
    out, s = [], None
    for i, x in enumerate(xs):
        if x is None:
            if s is not None:
                out.append((s, i))
                s = None
        elif s is None:
            s = i
    if s is not None:
        out.append((s, len(xs)))
    return out


def _per_segment(xs, fn):
    out = [None] * len(xs)
    for a, b in _segments(xs):
        out[a:b] = fn(xs[a:b])
    return out


def _roll_std(xs, n):
    out = [None] * len(xs)
    s = s2 = 0.0
    for i, x in enumerate(xs):
        s += x
        s2 += x * x
        if i >= n:
            y = xs[i - n]
            s -= y
            s2 -= y * y
        if i >= n - 1:
            if i % 256 == 0:                       # keep the running sums honest over long series
                w = xs[i - n + 1:i + 1]
                s, s2 = sum(w), sum(v * v for v in w)
            m = s / n
            out[i] = math.sqrt(max(0.0, s2 / n - m * m))
    return out


def _prev_extreme(xs, n, biggest):
    """The max (or min) of the n values before each one, not counting it."""
    out = [None] * len(xs)
    dq = collections.deque()
    for i in range(len(xs)):
        if i >= 1:
            j = i - 1
            while dq and ((xs[dq[-1]] <= xs[j]) if biggest else (xs[dq[-1]] >= xs[j])):
                dq.pop()
            dq.append(j)
        while dq and dq[0] < i - n:
            dq.popleft()
        if i >= n:
            out[i] = xs[dq[0]]
    return out


def _map1(a, fn):
    if not isinstance(a, list):
        return fn(a)
    return [None if x is None else fn(x) for x in a]


def _map2(a, b, fn):
    la, lb = isinstance(a, list), isinstance(b, list)
    if not la and not lb:
        return fn(a, b)
    n = len(a) if la else len(b)
    out = [None] * n
    for i in range(n):
        x = a[i] if la else a
        y = b[i] if lb else b
        if x is not None and y is not None:
            out[i] = fn(x, y)
    return out


def _div(x, y):
    return None if y == 0 else x / y


BIN_FN = {"+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y, "/": _div}


class Frame(object):
    """One coin's bars, and everything a rule can read from them, worked out bar by bar from the past only."""

    def __init__(self, bars, step, funding=None, btc_bars=None):
        self.bars = bars
        self.step = step
        self.n = len(bars)
        self.times = [b[T] for b in bars]
        self.funding = sorted(funding or [])
        self.btc_bars = btc_bars
        self.memo = {}

    def value(self, name):
        if name in self.memo:
            return self.memo[name]
        b = self.bars
        if name in ("open", "high", "low", "close", "volume"):
            col = {"open": O, "high": H, "low": L, "close": C, "volume": V}[name]
            out = [x[col] for x in b]
        elif name == "hour":
            out = [float((t % DAY) // HOUR) for t in self.times]
        elif name == "weekday":
            out = [float(cc.utc_dt(t).weekday()) for t in self.times]
        elif name == "funding":
            ft = [t for t, _ in self.funding]
            out = []
            for t in self.times:
                k = bisect.bisect_right(ft, t + self.step) - 1           # the last payment at or before the bar's close
                out.append(self.funding[k][1] * 100.0 if k >= 0 else None)
        elif name == "btc_close":
            by_t = dict((x[T], x[C]) for x in (self.btc_bars or []))
            out = [by_t.get(t) for t in self.times]
        elif name in ("day_open", "prev_day_high", "prev_day_low", "prev_day_close"):
            self._days()
            out = self.memo[name]
        else:
            raise RuleError("unknown value %s" % name)
        self.memo[name] = out
        return out

    def _days(self):
        """The UTC day's open and yesterday's high, low and close, from complete days only."""
        n = self.n
        d_open, p_hi, p_lo, p_cl = [None] * n, [None] * n, [None] * n, [None] * n
        cur_day, cur, prev = None, None, None
        last_t = None
        for i, x in enumerate(self.bars):
            day = x[T] // DAY
            if day != cur_day:
                if cur is not None and cur["complete"] and last_t is not None and last_t + self.step == cur_day * DAY + DAY:
                    prev = cur if day == cur_day + 1 else None
                else:
                    prev = None
                cur_day = day
                cur = {"open": x[O], "high": x[H], "low": x[L], "close": x[C], "complete": x[T] == day * DAY}
            else:
                cur["high"] = max(cur["high"], x[H])
                cur["low"] = min(cur["low"], x[L])
                cur["close"] = x[C]
            last_t = x[T]
            if cur["complete"]:
                d_open[i] = cur["open"]
            if prev is not None:
                p_hi[i], p_lo[i], p_cl[i] = prev["high"], prev["low"], prev["close"]
        self.memo.update({"day_open": d_open, "prev_day_high": p_hi, "prev_day_low": p_lo, "prev_day_close": p_cl})

    def series(self, ir):
        v = self.eval(ir)
        return v if isinstance(v, list) else [v] * self.n

    def eval(self, ir):
        got = self.memo.get(ir)
        if got is not None:
            return got
        tag = ir[0]
        if tag == "num":
            return ir[1]
        if tag == "val":
            res = self.value(ir[1])
        elif tag == "neg":
            res = _map1(self.eval(ir[1]), lambda x: -x)
        elif tag == "bin":
            res = _map2(self.eval(ir[2]), self.eval(ir[3]), BIN_FN[ir[1]])
        elif tag == "cmp":
            vals = [self.eval(x) for x in ir[2]]
            res = None
            for op, a, b in zip(ir[1], vals, vals[1:]):
                part = _map2(a, b, CMP_FN[op])
                res = part if res is None else _map2(res, part, lambda x, y: x and y)
        elif tag in ("and", "or"):
            res = None
            for x in ir[1]:
                v = self.series(x)
                if res is None:
                    res = list(v)
                    continue
                if tag == "and":
                    res = [False if (p is False or q is False) else (None if (p is None or q is None) else True)
                           for p, q in zip(res, v)]
                else:
                    res = [True if (p is True or q is True) else (None if (p is None or q is None) else False)
                           for p, q in zip(res, v)]
        elif tag == "not":
            res = _map1(self.eval(ir[1]), lambda x: not x)
        elif tag == "pair":
            res = self._pair(ir[1], self.series(ir[2]), self.series(ir[3]))
        elif tag == "fn":
            res = self._fn(ir[1], ir[2], ir[3])
        else:
            raise RuleError("could not read the rule")
        self.memo[ir] = res
        return res

    def _pair(self, name, a, b):
        n = self.n
        if name in ("min", "max"):
            f = min if name == "min" else max
            return [None if (x is None or y is None) else f(x, y) for x, y in zip(a, b)]
        out = [None] * n
        up = name == "crosses_above"
        for i in range(1, n):
            x0, y0, x1, y1 = a[i - 1], b[i - 1], a[i], b[i]
            if None in (x0, y0, x1, y1):
                continue
            out[i] = (x0 <= y0 and x1 > y1) if up else (x0 >= y0 and x1 < y1)
        return out

    def _fn(self, name, series, p):
        n = self.n
        if name == "atr":
            return cc.atr(self.bars, p[0])
        if name == "vol_ratio":
            vol = self.value("volume")
            mean = cc.sma(vol, p[0])
            return [None] + [_div(vol[i], mean[i - 1]) if mean[i - 1] else None for i in range(1, n)]
        if series is None:
            series = ("val", FUNCS[name][1]) if FUNCS[name][1] else None
        xs = self.series(series)
        if name == "abs":
            return _map1(xs, abs)
        if name == "ago":
            k = p[0]
            return ([None] * k + xs[:-k]) if k < n else [None] * n
        if name == "sma":
            return _per_segment(xs, lambda s: cc.sma(s, p[0]))
        if name == "ema":
            return _per_segment(xs, lambda s: cc.ema(s, p[0]))
        if name == "rsi":
            return _per_segment(xs, lambda s: cc.rsi(s, p[0]))
        if name == "stdev":
            return _per_segment(xs, lambda s: _roll_std(s, p[0]))
        if name == "change":
            k = p[0]
            return [None if (i < k or xs[i] is None or xs[i - k] is None or not xs[i - k])
                    else (xs[i] / xs[i - k] - 1.0) * 100.0 for i in range(n)]
        if name in ("highest", "lowest"):
            return _per_segment(xs, lambda s: _prev_extreme(s, p[0], name == "highest"))
        if name.startswith("bb_"):
            mid = _per_segment(xs, lambda s: cc.sma(s, p[0]))
            sd = _per_segment(xs, lambda s: _roll_std(s, p[0]))
            up = _map2(mid, _map1(sd, lambda x: p[1] * x), lambda m, d: m + d)
            lo = _map2(mid, _map1(sd, lambda x: p[1] * x), lambda m, d: m - d)
            if name == "bb_upper":
                return up
            if name == "bb_lower":
                return lo
            return _map2(_map2(up, lo, lambda u, l_: u - l_), mid, lambda w, m: (w / m * 100.0) if m else None)
        if name.startswith("macd"):
            fast = _per_segment(xs, lambda s: cc.ema(s, p[0]))
            slow = _per_segment(xs, lambda s: cc.ema(s, p[1]))
            line = _map2(fast, slow, lambda a, b: a - b)
            if name == "macd":
                return line
            sig = _per_segment(line, lambda s: cc.ema(s, p[2]))
            return sig if name == "macd_signal" else _map2(line, sig, lambda a, b: a - b)
        raise RuleError("unknown function %s" % name)

    def signals(self, rule):
        return [x is True for x in self.series(rule.ir)]


# ---------------------------------------------------------------- exits

def parse_stop(text):
    s = str(text or "").strip().lower().replace(" ", "")
    m = re.match(r"^(pct|atr|swing):(\d+(?:\.\d+)?)%?$", s)
    if not m:
        raise cc.CryptoError("could not read the stop %r. Use pct:2 (2%% away), atr:2 (2 ATRs away) or swing:10 "
                             "(beyond the lowest low or highest high of the last 10 bars)" % text)
    kind, v = m.group(1), float(m.group(2))
    lim = {"pct": (0.05, 50.0), "atr": (0.1, 20.0), "swing": (2, 500)}[kind]
    if not lim[0] <= v <= lim[1]:
        raise cc.CryptoError("a %s stop must be between %s and %s" % (kind, _num(lim[0]), _num(lim[1])))
    if kind == "swing" and v != int(v):
        raise cc.CryptoError("a swing stop counts bars: a whole number, like swing:10")
    return (kind, int(v) if kind == "swing" else v)


def parse_target(text):
    s = str(text or "").strip().lower().replace(" ", "")
    if s in ("none", "no", "off", ""):
        return None
    m = re.match(r"^(r|pct):(\d+(?:\.\d+)?)%?$", s) or re.match(r"^(\d+(?:\.\d+)?)(r)$", s)
    if not m:
        raise cc.CryptoError("could not read the target %r. Use r:2 (twice the risk), pct:3 (3%% away) or none" % text)
    kind, v = (m.group(1), float(m.group(2))) if m.group(1) in ("r", "pct") else ("r", float(m.group(1)))
    lim = {"r": (0.1, 20.0), "pct": (0.05, 500.0)}[kind]
    if not lim[0] <= v <= lim[1]:
        raise cc.CryptoError("a %s target must be between %s and %s" % (kind, _num(lim[0]), _num(lim[1])))
    return (kind, v)


def stop_words(stop, side):
    kind, v = stop
    below = "below" if side == "long" else "above"
    if kind == "pct":
        return "%s%% %s the entry" % (_num(v), below)
    if kind == "atr":
        return "%s ATR(14) %s the entry" % (_num(v), below)
    return "%s the %s of the last %d bars" % (below.replace("below", "under").replace("above", "over"),
                                              "lowest low" if side == "long" else "highest high", v)


def target_words(target, side):
    if target is None:
        return "no target"
    kind, v = target
    return ("target %sR" % _num(v)) if kind == "r" else ("target %s%% %s the entry" % (_num(v), "above" if side == "long" else "below"))


# ---------------------------------------------------------------- one trade

def play(frame, i, side, stop, target, hold_bars, exit_sig, atr14, costs, fund_ts, fund_rs):
    """A trade signalled at the close of bar i, in at the open of bar i + 1. A dict, or "skip" (no honest stop) or
    "unfinished" (the data ends first)."""
    bars, n = frame.bars, frame.n
    if i + 1 >= n:
        return "unfinished"
    long_ = side == "long"
    sg = 1.0 if long_ else -1.0
    entry = bars[i + 1][O]
    kind, v = stop
    if kind == "pct":
        stop_px = entry * (1.0 - sg * v / 100.0)
    elif kind == "atr":
        a = atr14[i]
        if a is None:
            return "skip"
        stop_px = entry - sg * v * a
    else:
        if i - v + 1 < 0:
            return "skip"
        win = bars[i - v + 1:i + 1]
        stop_px = min(b[L] for b in win) if long_ else max(b[H] for b in win)
    if sg * (entry - stop_px) <= entry * MIN_STOP:
        return "skip"
    risk = abs(entry - stop_px)
    if target is None:
        tgt = None
    elif target[0] == "r":
        tgt = entry + sg * target[1] * risk
    else:
        tgt = entry * (1.0 + sg * target[1] / 100.0)
    step = frame.step
    out = None
    for j in range(i + 1, n):
        b = bars[j]
        if long_:
            if b[O] <= stop_px:
                out = (b[O], "stop", j, b[T])
            elif b[L] <= stop_px:
                out = (stop_px, "stop", j, b[T])
            elif tgt is not None and b[O] >= tgt:
                out = (b[O], "target", j, b[T])
            elif tgt is not None and b[H] >= tgt:
                out = (tgt, "target", j, b[T])
        else:
            if b[O] >= stop_px:
                out = (b[O], "stop", j, b[T])
            elif b[H] >= stop_px:
                out = (stop_px, "stop", j, b[T])
            elif tgt is not None and b[O] <= tgt:
                out = (b[O], "target", j, b[T])
            elif tgt is not None and b[L] <= tgt:
                out = (tgt, "target", j, b[T])
        if out:
            break
        if j - i >= hold_bars:
            out = (b[C], "time", j, b[T] + step)
            break
        if exit_sig is not None and exit_sig[j]:
            if j + 1 >= n:
                return "unfinished"
            out = (bars[j + 1][O], "rule", j + 1, bars[j + 1][T])
            break
    if out is None:
        return "unfinished"
    xp, outcome, k, t_exit = out
    t_in = bars[i + 1][T]
    paid = 0.0
    if fund_ts:
        a, z = bisect.bisect_right(fund_ts, t_in), bisect.bisect_right(fund_ts, t_exit)
        for q in range(a, z):
            m = bisect.bisect_right(frame.times, fund_ts[q] - step) - 1          # the bar that closed at the payment
            px = bars[m][C] if m >= 0 else entry
            paid += fund_rs[q] * px
    paid *= sg
    r = cc.net_r(side, entry, xp, "target" if outcome == "target" else "stop", risk, costs, paid)
    return {"i": i, "entry_at": t_in, "exit_at": t_exit, "entry": entry, "stop": stop_px, "target": tgt, "exit": xp,
            "outcome": outcome, "r": r, "gross_r": sg * (xp - entry) / risk, "funding_r": -paid / risk,
            "stop_cost_r": cc.stop_cost_r(entry, stop_px, costs), "bars": k - i, "exit_index": k,
            "risk_pct": risk / entry * 100.0}


def slot_of(t, step):
    if step >= DAY:
        return 0
    return (t % DAY) // max(step, HOUR)


# ---------------------------------------------------------------- one coin

def run_coin(job):
    """Every signal on one coin, played one trade at a time, and random entries for the luck test."""
    (m, data, rule, exit_rule, side, stop, target, hold_bars, step, w_start, w_end, costs, seed, per_trade) = job
    frame = Frame(data["bars"], step, data["funding"], data.get("btc"))
    sig = frame.signals(rule)
    ex = frame.signals(exit_rule) if exit_rule else None
    atr14 = cc.atr(frame.bars, 14)
    fund_ts = [t for t, _ in frame.funding]
    fund_rs = [r for _, r in frame.funding]
    res = {"coin": m["coin"], "symbol": m["symbol"], "fired": 0, "overlap": 0, "skipped": 0, "unfinished": 0,
           "trades": [], "random": [], "bars": frame.n,
           "first": frame.times[0] if frame.times else None, "last": frame.times[-1] if frame.times else None}
    if not frame.n:
        return res
    first_i = bisect.bisect_left(frame.times, w_start - step)              # bars whose close is inside the window
    last_i = bisect.bisect_left(frame.times, w_end) - 1
    busy = -1
    for i in range(max(first_i, 0), last_i + 1):
        if not sig[i]:
            continue
        res["fired"] += 1
        if i < busy:
            res["overlap"] += 1
            continue
        tr = play(frame, i, side, stop, target, hold_bars, ex, atr14, costs, fund_ts, fund_rs)
        if tr == "skip":
            res["skipped"] += 1
            continue
        if tr == "unfinished":
            res["unfinished"] += 1
            continue
        tr["coin"], tr["symbol"], tr["signal_at"] = m["coin"], m["symbol"], frame.times[i] + step
        res["trades"].append(tr)
        busy = tr["exit_index"]                          # the next signal may come from the bar the trade left on
    if res["trades"] and per_trade:
        rng = random.Random("%s|%s|%d" % (seed, m["symbol"], len(res["trades"])))
        by_slot = collections.defaultdict(list)
        for i in range(max(first_i, 1), last_i + 1):
            by_slot[slot_of(frame.times[i], step)].append(i)
        memo = {}
        for tr in res["trades"]:
            cand = by_slot.get(slot_of(frame.times[tr["i"]], step)) or []
            got, tries = [], 0
            while cand and len(got) < per_trade and tries < per_trade * 4:
                tries += 1
                k = cand[int(rng.random() * len(cand))]
                if k not in memo:
                    memo[k] = play(frame, k, side, stop, target, hold_bars, ex, atr14, costs, fund_ts, fund_rs)
                x = memo[k]
                if isinstance(x, dict):
                    got.append(x["r"])
            res["random"].append(got)
    for tr in res["trades"]:
        tr.pop("exit_index", None)
    return res


# ---------------------------------------------------------------- statistics

def summary(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins = sum(1 for r in rs if r > 0)
    p, lo, hi = cc.wilson(wins, n)
    avg = sum(rs) / n
    sd = math.sqrt(sum((r - avg) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    half = 1.645 * sd / math.sqrt(n)
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r <= 0)
    eq = peak = dd = 0.0
    streak = worst = 0
    for t in sorted(trades, key=lambda t: (t["exit_at"], t["entry_at"])):
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if t["r"] <= 0 else 0
        worst = max(worst, streak)
    outcomes = collections.Counter(t["outcome"] for t in trades)
    return {"n": n, "wins": wins, "win_rate": p, "win_lo": lo, "win_hi": hi, "avg_r": avg, "lo": avg - half,
            "hi": avg + half, "avg_gross_r": sum(t["gross_r"] for t in trades) / n, "total_r": sum(rs),
            "profit_factor": (gains / losses) if losses else None, "max_dd_r": dd, "worst_streak": worst,
            "target": outcomes.get("target", 0), "stop": outcomes.get("stop", 0), "time": outcomes.get("time", 0),
            "rule": outcomes.get("rule", 0), "avg_funding_r": sum(t["funding_r"] for t in trades) / n}


def luck_test(trades, groups, seed):
    """Random entries at the same hours with the same exits: their average, and how often one random trade for each
    real one did as well as the rule (the luck test)."""
    pairs = [(t, g) for t, g in zip(trades, groups) if g]
    if len(pairs) < 3:
        return None
    flat = [r for _, g in pairs for r in g]
    base = sum(flat) / len(flat)
    obs = sum(t["r"] for t, _ in pairs) / len(pairs)
    rng = random.Random(seed + 202)
    m = len(pairs)
    hits = 0
    for _ in range(LUCK_REPS):
        s = 0.0
        for _, g in pairs:
            s += g[int(rng.random() * len(g))]
        if s / m >= obs:
            hits += 1
    rs = [t["r"] for t, _ in pairs]
    diffs = sorted(sum(rs[int(rng.random() * m)] for _ in range(m)) / m - base for _ in range(LUCK_REPS))
    return {"n": len(flat), "avg_r": base, "luck_p": (hits + 1) / float(LUCK_REPS + 1), "edge": obs - base,
            "edge_lo": diffs[int(LUCK_REPS * 0.05)], "edge_hi": diffs[int(LUCK_REPS * 0.95) - 1]}


def verdict(s, luck, halves):
    """(short label, the verdict line)."""
    n = s.get("n", 0)
    if not n:
        return "nothing to judge", "Verdict: nothing to judge. The rule never opened a trade on these coins in this period."
    if n < MIN_TRADES:
        return "too few trades", ("Verdict: too few trades to judge (%d). Test more coins or a longer period: a handful "
                                  "of trades says almost nothing." % n)
    avg, lo, hi = s["avg_r"], s["lo"], s["hi"]
    if hi < 0:
        return "no edge", ("Verdict: no edge. The idea lost %s a trade after costs, and even the top of its 90%% range "
                           "is below zero." % cc.fmt_r(-avg).lstrip("+"))
    if lo <= 0:
        return "no edge shown", ("Verdict: no edge shown. After costs it averaged %s a trade, and that can't be told "
                                 "from zero." % cc.fmt_r(avg))
    if luck is None or luck["luck_p"] > 0.05:
        p = ("luck matched it %s of the time" % _pct(luck["luck_p"])) if luck else "there were too few to compare"
        return "no edge from the rule", ("Verdict: no edge from the rule. It made %s a trade after costs, but random "
                                         "entries at the same hours with the same exits did about as well (%s): the "
                                         "exits or the market's direction did the work, not the rule." % (cc.fmt_r(avg), p))
    h1, h2 = halves
    if not (h1.get("n") and h2.get("n") and h1["avg_r"] > 0 and h2["avg_r"] > 0):
        return "not consistent", ("Verdict: not consistent. It made %s a trade after costs and beat random entries, but "
                                  "not in both halves of the period (%s, then %s)." % (
                                      cc.fmt_r(avg), cc.fmt_r(h1.get("avg_r") or 0.0), cc.fmt_r(h2.get("avg_r") or 0.0)))
    return "worth a paper trial", ("Verdict: worth a paper trial, not money. It made %s a trade after costs (90%% range "
                                   "%s to %s), beat random entries (luck matched it %s of the time) and made money in both "
                                   "halves. Test it on a period it has not seen (--end) and paper trade it before risking "
                                   "anything." % (cc.fmt_r(avg), cc.fmt_r(lo), cc.fmt_r(hi), _pct(luck["luck_p"])))


def _pct(x):
    if x < 0.001:
        return "under 0.1%"
    return ("%.1f%%" % (x * 100)) if x < 0.1 else ("%.0f%%" % (x * 100))


# ---------------------------------------------------------------- data

def _cache_path(venue, symbol, tf, kind):
    return cc.wpath("cache", "lab", "%s-%s-%s-%s.json.gz" % (venue.name, symbol, tf, kind))


def _read_cache(path):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return None


def _write_cache(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError:
        pass


def cached_bars(venue, symbol, tf, start, end, use_cache):
    """Closed bars with open times in [start, end]. Closed bars never change, so what was downloaded before is kept
    in crypto/cache/lab/ and only the missing ends are fetched."""
    step = cc.interval_ms(tf)
    path = _cache_path(venue, symbol, tf, "bars") if use_cache else None
    have = (_read_cache(path) if path else None) or {"bars": [], "from": None, "to": None}
    rows = dict((b[0], tuple(b)) for b in have["bars"])
    f0, t0 = have.get("from"), have.get("to")
    spans = []
    if f0 is None or t0 is None or start < f0 or end > t0:
        if f0 is None or end < f0 or start > t0:
            spans.append((start, end))
        else:
            if start < f0:
                spans.append((start, f0 - step))
            if end > t0:
                spans.append((t0 + step, end))
    for a, b in spans:
        for x in venue.bars_between(symbol, tf, a, b):
            rows[x[T]] = tuple(x)
    if spans and path:
        cap = (cc.exchange_now_ms() // step) * step - step
        got_from = min([start] + ([f0] if f0 is not None else []))
        got_to = min(max([end] + ([t0] if t0 is not None else [])), cap)
        _write_cache(path, {"bars": [list(rows[t]) for t in sorted(rows)], "from": got_from, "to": got_to})
    return [rows[t] for t in sorted(rows) if start <= t <= end]


def cached_funding(venue, symbol, start, end, use_cache):
    path = _cache_path(venue, symbol, "x", "funding") if use_cache else None
    have = (_read_cache(path) if path else None) or {"rows": [], "from": None, "to": None}
    rows = dict((int(t), r) for t, r in have["rows"])
    f0, t0 = have.get("from"), have.get("to")
    spans = []
    if f0 is None or t0 is None or end < f0 or start > t0:
        spans.append((start, end))
    else:
        if start < f0:
            spans.append((start, f0 - 1))
        if end > t0:
            spans.append((t0 + 1, end))
    for a, b in spans:
        for t, r in venue.funding_history(symbol, a, b) or []:
            rows[int(t)] = float(r)
    if spans and path:
        cap = cc.exchange_now_ms()
        _write_cache(path, {"rows": [[t, rows[t]] for t in sorted(rows)],
                            "from": min([start] + ([f0] if f0 is not None else [])),
                            "to": min(max([end] + ([t0] if t0 is not None else [])), cap)})
    return [(t, rows[t]) for t in sorted(rows) if start <= t <= end]


def pick_coins(venue, spec, cfg):
    s = str(spec or "top:10").strip()
    m = re.match(r"^top:?(\d+)$", s.lower())
    if m:
        k = int(m.group(1))
        if not 1 <= k <= MAX_COINS:
            raise cc.CryptoError("test between 1 and %d coins at a time" % MAX_COINS)
        kept, _ = cc.build_universe(venue, cfg, top=k)
        return [x[0] for x in kept]
    out, seen = [], set()
    for word in re.split(r"[,\s]+", s):
        if not word:
            continue
        mk = venue.market(word)
        if mk["symbol"] not in seen:
            seen.add(mk["symbol"])
            out.append(mk)
    if not out:
        raise cc.CryptoError("no coins to test: use --coins top:10 or a list like SOL,ETH")
    if len(out) > MAX_COINS:
        raise cc.CryptoError("test at most %d coins at a time" % MAX_COINS)
    return out


# ---------------------------------------------------------------- the idea

def idea_from(a):
    """The idea from an --idea file, with any flags on top. Checked and filled with defaults."""
    idea = dict(DEFAULTS)
    if a.idea:
        got = cc.read_json(a.idea, None)
        if not isinstance(got, dict):
            raise cc.CryptoError("could not read the idea file %s" % a.idea)
        src = got.get("idea") if isinstance(got.get("idea"), dict) else got
        for k in list(DEFAULTS) + ["when"]:
            if src.get(k) is not None:
                idea[k] = src[k]
    for k, flag in (("side", "side"), ("timeframe", "tf"), ("stop", "stop"), ("target", "target"), ("hold", "hold"),
                    ("exit_when", "exit_when"), ("coins", "coins"), ("days", "days"), ("end", "end"),
                    ("venue", "venue"), ("name", "name")):
        v = getattr(a, flag, None)
        if v is not None:
            idea[k] = v
    if getattr(a, "when", None):
        idea["when"] = a.when
    if not idea.get("when"):
        raise cc.CryptoError("say when to enter: --when \"rsi(14) < 30\". Run lab.py words for what a rule can say.")
    if idea["side"] not in ("long", "short"):
        raise cc.CryptoError("say which side the idea trades: --side long or --side short")
    if idea["timeframe"] not in TIMEFRAMES:
        raise cc.CryptoError("the bars must be one of %s" % ", ".join(TIMEFRAMES))
    tf = idea["timeframe"]
    idea["days"] = int(idea["days"] or DAYS_DEFAULT[tf])
    if not 7 <= idea["days"] <= DAYS_MAX[tf]:
        raise cc.CryptoError("on %s bars test between 7 and %d days" % (tf, DAYS_MAX[tf]))
    parse_stop(idea["stop"])
    parse_target(idea["target"])
    hours = cc.parse_hold(idea["hold"], 24.0)
    if hours * HOUR < cc.interval_ms(tf):
        raise cc.CryptoError("the time limit (%s) is shorter than one %s bar" % (idea["hold"], tf))
    Rule(idea["when"])
    if idea.get("exit_when"):
        Rule(idea["exit_when"], "exit rule")
    return idea


def fingerprint(idea):
    keys = ("when", "side", "timeframe", "stop", "target", "hold", "exit_when", "coins", "days", "end")
    canon = json.dumps([(" ".join(str(idea.get(k)).split()) if idea.get(k) is not None else None) for k in keys])
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def slug_of(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return (s[:48].rstrip("-") or "idea")


def _day(ms, year=False):
    d = cc.utc_dt(ms)
    return "%d %s%s" % (d.day, d.strftime("%b"), (" %d" % d.year) if year else "")


def test_idea(idea, cfg, venue, seed=19, workers=4, use_cache=True, per_trade=RANDOM_PER_TRADE):
    tf = idea["timeframe"]
    step = cc.interval_ms(tf)
    rule = Rule(idea["when"])
    exit_rule = Rule(idea["exit_when"], "exit rule") if idea.get("exit_when") else None
    stop, target = parse_stop(idea["stop"]), parse_target(idea["target"])
    hours = cc.parse_hold(idea["hold"], 24.0)
    hold_bars = max(1, int(math.ceil(hours * HOUR / float(step))))
    now = cc.exchange_now_ms()
    end = cc.parse_when(idea["end"]) if idea.get("end") else now
    if end > now + 60000:
        raise cc.CryptoError("the test window cannot end in the future")
    end = (min(end, now) // step) * step
    w_start = end - idea["days"] * DAY
    warm = max(rule.lookback(step), exit_rule.lookback(step) if exit_rule else 0,
               stop[1] if stop[0] == "swing" else 0, 60) + 2
    after = hold_bars + 2                                    # bars after the window so trades near its end can finish
    t0 = w_start - warm * step
    t1 = min(end + after * step, (now // step) * step - step)
    coins = pick_coins(venue, idea["coins"], cfg)
    needs = rule.needs | (exit_rule.needs if exit_rule else set())
    btc = None
    if "btc" in needs:
        bm = venue.market("BTC")
        btc = cached_bars(venue, bm["symbol"], tf, t0, t1, use_cache)
    rates = cc.cost_rates(venue, cfg)

    def fetch(mk):
        bars = cached_bars(venue, mk["symbol"], tf, t0, t1, use_cache)
        fund = cached_funding(venue, mk["symbol"], t0, t1, use_cache)
        slip = rates["slippage"]
        if slip is None:
            slip, _ = cc.measure_slippage(venue, mk["symbol"])
        return {"bars": bars, "funding": fund, "btc": btc,
                "costs": {"taker": rates["taker"], "maker": rates["maker"], "slippage": slip}}

    got = cc.parallel(fetch, coins, workers=workers)
    results, problems = [], []
    for mk, data, err in got:
        if err is not None:
            problems.append("%s: %s" % (mk["coin"], err))
            continue
        job = (mk, data, rule, exit_rule, idea["side"], stop, target, hold_bars, step, w_start, end, data["costs"],
               seed, per_trade)
        r = run_coin(job)
        r["costs"] = data["costs"]
        results.append(r)
    trades = sorted([t for r in results for t in r["trades"]], key=lambda t: (t["entry_at"], t["coin"]))
    groups = []
    by_id = {}
    for r in results:
        for t, g in zip(r["trades"], r["random"] or [[]] * len(r["trades"])):
            by_id[id(t)] = g
    groups = [by_id.get(id(t), []) for t in trades]
    s = summary(trades)
    luck = luck_test(trades, groups, seed) if s.get("n", 0) >= 3 else None
    mid = w_start + (end - w_start) // 2
    halves = (summary([t for t in trades if t["entry_at"] < mid]), summary([t for t in trades if t["entry_at"] >= mid]))
    limit = cc.cost_limit(cfg)
    within = [t for t in trades if not cc.over_cost_limit(t["stop_cost_r"], limit)] if limit else list(trades)
    over = [t for t in trades if limit and cc.over_cost_limit(t["stop_cost_r"], limit)]
    label, line = verdict(s, luck, halves)
    per_coin = []
    for r in results:
        cs = summary(r["trades"])
        short_hist = r["first"] is not None and r["first"] > t0 + 2 * DAY
        per_coin.append({"coin": r["coin"], "n": cs.get("n", 0), "avg_r": cs.get("avg_r"), "win_rate": cs.get("win_rate"),
                         "fired": r["fired"], "from": r["first"], "short_history": short_hist,
                         "slippage": r["costs"]["slippage"]})
    per_coin.sort(key=lambda x: (-x["n"], x["coin"]))
    return {"idea": idea, "rule": rule.text, "exit_rule": exit_rule.text if exit_rule else None, "venue": venue.label,
            "venue_name": venue.name, "window": {"start": w_start, "end": end, "mid": mid}, "hold_bars": hold_bars,
            "coins": [mk["coin"] for mk in coins], "problems": problems, "summary": s, "luck": luck,
            "halves": [halves[0], halves[1]], "cost_rule": {"limit": limit, "within": summary(within), "over": summary(over)},
            "per_coin": per_coin, "verdict": label, "verdict_line": line,
            "counts": {"fired": sum(r["fired"] for r in results), "overlap": sum(r["overlap"] for r in results),
                       "skipped": sum(r["skipped"] for r in results), "unfinished": sum(r["unfinished"] for r in results)},
            "costs": {"taker": rates["taker"], "maker": rates["maker"],
                      "slippage_median": cc.median([r["costs"]["slippage"] for r in results]) if results else None,
                      "slippage_source": rates.get("slippage_source")},
            "trades": trades, "seed": seed, "per_trade": per_trade}


# ---------------------------------------------------------------- the lab notebook: what was tried

def tried_path():
    return cc.wpath(IDEAS, TRIED)


def tried_count():
    seen = set()
    for e in cc.read_jsonl(tried_path()):
        if e.get("fingerprint"):
            seen.add(e["fingerprint"])
    return len(seen)


def note_tried(d, fp):
    if fp in set(e.get("fingerprint") for e in cc.read_jsonl(tried_path())):
        return False
    s = d["summary"]
    cc.append_jsonl(tried_path(), {"fingerprint": fp, "at": cc.now_ms(), "name": d["idea"].get("name"), "rule": d["rule"],
                                   "side": d["idea"]["side"], "n": s.get("n", 0), "avg_r": s.get("avg_r"),
                                   "luck_p": (d["luck"] or {}).get("luck_p"), "verdict": d["verdict"]})
    return True


def save_idea(d, fp):
    idea = d["idea"]
    name = idea.get("name") or d["rule"]
    slug = slug_of(name)
    s = d["summary"]
    path = cc.wpath(IDEAS, slug + ".json")
    cc.write_json(path, {"idea": idea, "fingerprint": fp, "tested_at": cc.now_ms(), "tested_utc": cc.fmt_utc(cc.now_ms()),
                         "rule": d["rule"], "exit_rule": d["exit_rule"], "venue": d["venue_name"],
                         "window": d["window"], "result": {k: s.get(k) for k in ("n", "win_rate", "avg_r", "lo", "hi",
                                                                                  "avg_gross_r", "profit_factor")},
                         "luck": d["luck"], "verdict": d["verdict"], "verdict_line": d["verdict_line"]})
    csv_path = cc.wpath(IDEAS, slug + "-trades.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["coin", "side", "signal_utc", "entry_utc", "entry", "stop", "target", "exit_utc", "exit", "outcome",
                    "r_after_costs", "r_before_costs", "funding_r", "stop_cost_r", "bars_held"])
        for t in d["trades"]:
            w.writerow([t["coin"], idea["side"], cc.fmt_utc(t["signal_at"]), cc.fmt_utc(t["entry_at"]), "%.10g" % t["entry"],
                        "%.10g" % t["stop"], "" if t["target"] is None else "%.10g" % t["target"],
                        cc.fmt_utc(t["exit_at"]), "%.10g" % t["exit"], t["outcome"], "%.4f" % t["r"],
                        "%.4f" % t["gross_r"], "%.4f" % t["funding_r"], "%.4f" % t["stop_cost_r"], t["bars"]])
    return path, csv_path


# ---------------------------------------------------------------- printing

def report_lines(d, tried=None):
    idea, s, w = d["idea"], d["summary"], d["window"]
    side = idea["side"]
    title = idea.get("name") or d["rule"]
    out = ["Idea Lab | %s | %s | %s bars | %s | %d coin%s | %s to %s (%d days)" % (
        title, side, idea["timeframe"], d["venue"], len(d["coins"]), "" if len(d["coins"]) == 1 else "s",
        _day(w["start"]), _day(w["end"], True), idea["days"])]
    out.append("Rule: go %s at the next bar's open when  %s" % (side, d["rule"]))
    exits = [stop_words(parse_stop(idea["stop"]), side), target_words(parse_target(idea["target"]), side),
             "out after %s" % idea["hold"]]
    if d["exit_rule"]:
        exits.append("or at the next open after  %s" % d["exit_rule"])
    out.append("Exits: stop %s; %s." % (exits[0], "; ".join(exits[1:])))
    c = d["costs"]
    slip = ("%.1f bps a side from %s" % (c["slippage_median"] * 1e4, c["slippage_source"])) if c.get("slippage_source") else (
        "median %.1f bps a side, measured from each coin's order book today" % ((c["slippage_median"] or 0) * 1e4))
    out.append("Costs: fees %s a side (%s at the target), slippage %s, and the funding charged while each trade was open."
               % (_fee(c["taker"]), _fee(c["maker"]), slip))
    for p in d["problems"]:
        out.append("  Left out: %s" % p)
    short = [x["coin"] for x in d["per_coin"] if x["short_history"]]
    if short:
        out.append("  Less history than the window on: %s (listed later, or the exchange keeps less)." % ", ".join(short))
    out.append("")
    k = d["counts"]
    if not s.get("n"):
        out.append("No trades. The rule was true %d time%s in the window%s." % (
            k["fired"], "" if k["fired"] == 1 else "s",
            (", but every one was skipped (%d with no honest stop, %d too close to the end of the data)" % (
                k["skipped"], k["unfinished"])) if k["fired"] else ""))
        out.append(d["verdict_line"])
        return out + _tail(d, tried)
    with_trades = sum(1 for x in d["per_coin"] if x["n"])
    out.append("Trades: %s on %d coin%s. The rule was true %s times; %s came while a trade was open, %s had no honest stop, "
               "%s ran past the end of the data." % (
                   "{:,}".format(s["n"]), with_trades, "" if with_trades == 1 else "s",
                   "{:,}".format(k["fired"]), "{:,}".format(k["overlap"]), "{:,}".format(k["skipped"]),
                   "{:,}".format(k["unfinished"])))
    parts = ["target first %s" % "{:,}".format(s["target"]) if parse_target(idea["target"]) else None,
             "stopped %s" % "{:,}".format(s["stop"]), "out at the time limit %s" % "{:,}".format(s["time"]),
             ("closed by the exit rule %s" % "{:,}".format(s["rule"])) if d["exit_rule"] else None]
    out.append("  %s." % ", ".join(p for p in parts if p))
    out.append("  Won (above zero after costs) %s (95%% range %s to %s)." % (
        _pct1(s["win_rate"]), _pct1(s["win_lo"]), _pct1(s["win_hi"])))
    out.append("  Average %s a trade after costs (90%% range %s to %s); %s before costs, so costs took %s." % (
        cc.fmt_r(s["avg_r"]), cc.fmt_r(s["lo"]), cc.fmt_r(s["hi"]), cc.fmt_r(s["avg_gross_r"]),
        cc.fmt_r(s["avg_gross_r"] - s["avg_r"]).lstrip("+")))
    pf = ("%.2f" % s["profit_factor"]) if s["profit_factor"] is not None else "-"
    out.append("  Profit factor %s. Funding %s a trade on average. Deepest drawdown %.1fR; longest losing streak %d." % (
        pf, cc.fmt_r(s["avg_funding_r"]), s["max_dd_r"], s["worst_streak"]))
    h1, h2 = d["halves"]
    out.append("Halves: %s to %s %s; %s to %s %s." % (
        _day(w["start"]), _day(w["mid"]), _half(h1), _day(w["mid"]), _day(w["end"]), _half(h2)))
    cr = d["cost_rule"]
    if cr["limit"]:
        wi, ov = cr["within"], cr["over"]
        if ov.get("n"):
            out.append("Within the cost rule (a stop-out costs at most %gR): %s trade%s, %s a trade (90%% range %s to %s). "
                       "The other %s, with stops too tight for their costs, averaged %s." % (
                           cr["limit"], "{:,}".format(wi.get("n", 0)), "" if wi.get("n") == 1 else "s",
                           cc.fmt_r(wi["avg_r"]) if wi.get("n") else "-", cc.fmt_r(wi["lo"]) if wi.get("n") else "-",
                           cc.fmt_r(wi["hi"]) if wi.get("n") else "-", "{:,}".format(ov["n"]), cc.fmt_r(ov["avg_r"])))
        else:
            out.append("Within the cost rule: every trade (no stop-out cost more than %gR)." % cr["limit"])
    lk = d["luck"]
    if lk:
        out.append("Random entries at the same hours, with the same stop, target and exits (%s): %s a trade." % (
            "{:,}".format(lk["n"]), cc.fmt_r(lk["avg_r"])))
        if abs(lk["edge"]) < 0.005:
            how = "did about as well as them"
        else:
            how = "%s them by %s a trade" % ("beat" if lk["edge"] > 0 else "did worse than",
                                            cc.fmt_r(abs(lk["edge"])).lstrip("+"))
        out.append("  The rule %s (90%% range of the difference %s to %s); luck matched it %s of the time." % (
            how, cc.fmt_r(lk["edge_lo"]), cc.fmt_r(lk["edge_hi"]), _pct(lk["luck_p"])))
    rows = []
    for x in d["per_coin"][:12]:
        if x["n"]:
            rows.append([x["coin"], "{:,}".format(x["n"]), _pct1(x["win_rate"]), cc.fmt_r(x["avg_r"])])
    if rows:
        out.append("")
        out.append("By coin (after costs):")
        out.append("\n".join("  " + l for l in cc.table(rows, ["coin", "trades", "won", "avg R"]).splitlines()))
        more = [x for x in d["per_coin"][12:] if x["n"]]
        if more:
            out.append("  ...and %d more coin%s." % (len(more), "" if len(more) == 1 else "s"))
    out.append("")
    out.append(d["verdict_line"])
    return out + _tail(d, tried)


def _tail(d, tried):
    out = []
    if tried:
        if tried <= 1:
            out.append("This is the first rule tested in this lab.")
        else:
            out.append("Rules tested in this lab so far: %d. Luck alone lets about 1 in 20 rules with no edge pass the luck "
                       "test, so the more versions are tried, the less one good result means." % tried)
    out.append("The coins are today's; ones that faded or were delisted are missing. A test on the past with fake money, "
               "not a forecast. Seed %d." % d["seed"])
    return out


def _fee(x):
    return ("%.3f" % (x * 100)).rstrip("0").rstrip(".") + "%"


def _pct1(x):
    return "-" if x is None else "%.1f%%" % (x * 100)


def _half(h):
    if not h.get("n"):
        return "no trades"
    return "%s (%s trade%s)" % (cc.fmt_r(h["avg_r"]), "{:,}".format(h["n"]), "" if h["n"] == 1 else "s")


def words_lines():
    out = ["Idea Lab: the words a rule can use", "",
           "Values, read at each bar's close (a rule never sees a later bar):"]
    for k, v in VALUES.items():
        out.append("  %-15s %s" % (k, v))
    out.append("")
    out.append("Functions (n counts bars; a value in front is optional, close unless said):")
    for k, (kind, dflt, params, words) in FUNCS.items():
        sig = []
        if kind in ("series",):
            sig.append("[value]")
        elif kind == "expr":
            sig.append("value")
        elif kind == "pair":
            sig += ["a", "b"]
        for pname, d, _, _, _ in params:
            sig.append(pname if d is None else "%s=%s" % (pname, _num(d)))
        out.append("  %-34s %s" % ("%s(%s)" % (k, ", ".join(sig)), words))
    out.append("")
    out.append("Join with < <= > >= == !=, and, or, not, + - * / and brackets.")
    out.append("")
    out.append("Examples:")
    for rule, what in EXAMPLES:
        out.append("  %-54s %s" % (rule, what))
    out.append("")
    out.append("Exits: --stop pct:2 | atr:2 | swing:10, --target r:2 | pct:3 | none, --hold 24h (up to 14d), "
               "--exit-when \"rule\".")
    out.append("Bars: --tf 5m | 15m | 1h | 4h | 1d. Coins: --coins top:10 or SOL,ETH. Period: --days, and --end for an "
               "earlier window.")
    return out


# ---------------------------------------------------------------- commands

def cmd_words(a):
    if a.json:
        cc.dump({"values": VALUES, "functions": dict((k, {"kind": v[0], "default": v[1], "params": [p[0] for p in v[2]],
                                                          "words": v[3]}) for k, v in FUNCS.items()),
                 "examples": EXAMPLES})
        return 0
    for line in words_lines():
        cc.out(line)
    return 0


def cmd_check(a):
    if not a.when:
        raise cc.CryptoError("give a rule to read: lab.py check --when \"rsi(14) < 30\"")
    rule = Rule(a.when)
    ex = Rule(a.exit_when, "exit rule") if a.exit_when else None
    tf = a.tf or "1h"
    if tf not in TIMEFRAMES:
        raise cc.CryptoError("the bars must be one of %s" % ", ".join(TIMEFRAMES))
    step = cc.interval_ms(tf)
    if a.json:
        cc.dump({"rule": rule.text, "exit_rule": ex.text if ex else None, "needs": sorted(rule.needs | (ex.needs if ex else set())),
                 "warmup_bars": max(rule.lookback(step), ex.lookback(step) if ex else 0)})
        return 0
    cc.out("Rule reads as:  %s" % rule.text)
    if ex:
        cc.out("Exit rule:      %s" % ex.text)
    needs = rule.needs | (ex.needs if ex else set())
    extra = {"funding": "the funding history", "btc": "BTC's bars too", "day": "whole UTC days"}
    if needs:
        cc.out("It also needs: %s." % ", ".join(extra[x] for x in sorted(needs)))
    warm = max(rule.lookback(step), ex.lookback(step) if ex else 0)
    cc.out("It reads %s bars of %s history before its first honest signal; the lab downloads them before the window."
           % ("{:,}".format(warm), tf))
    cc.out("Rule OK.")
    return 0


def cmd_test(a, cfg):
    idea = idea_from(a)
    venue = cc.get_venue(idea.get("venue") or a.venue)
    use_cache = not a.no_write and not a.no_cache
    d = test_idea(idea, cfg, venue, seed=a.seed, workers=a.workers, use_cache=use_cache,
                  per_trade=0 if a.no_luck else RANDOM_PER_TRADE)
    fp = fingerprint(idea)
    tried = None
    if not a.no_write:
        note_tried(d, fp)
        tried = tried_count()
        path, csv_path = save_idea(d, fp)
        d["saved"] = [path, csv_path]
        full = dict(d)
        full["trades"] = d["trades"][-2000:]
        cc.write_json(cc.wpath("idealab.json"), full)
    d["tried"] = tried
    if a.json:
        out = dict(d)
        out["trades"] = d["trades"][-200:]
        cc.dump(out)
        return 0
    for line in report_lines(d, tried):
        cc.out(line)
    if d.get("saved"):
        cc.out("Saved in your crypto folder: %s/%s, and every trade in %s/%s." % (
            IDEAS, os.path.basename(d["saved"][0]), IDEAS, os.path.basename(d["saved"][1])))
    return 0


def cmd_list(a):
    folder = cc.wpath(IDEAS)
    rows = []
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if not f.endswith(".json"):
                continue
            x = cc.read_json(os.path.join(folder, f), None) or {}
            if not isinstance(x, dict) or "idea" not in x:
                continue
            r = x.get("result") or {}
            lk = x.get("luck") or {}
            rows.append([(x["idea"].get("name") or x.get("rule") or f)[:40], x["idea"].get("side", ""),
                         x["idea"].get("timeframe", ""), "{:,}".format(r.get("n") or 0),
                         cc.fmt_r(r["avg_r"]) if r.get("avg_r") is not None else "-",
                         _pct(lk["luck_p"]) if lk.get("luck_p") is not None else "-", x.get("verdict", ""),
                         (x.get("tested_utc") or "")[:10]])
    if a.json:
        cc.dump({"ideas": rows, "tried": tried_count()})
        return 0
    if not rows:
        cc.out("No ideas tested yet. Try: lab.py test --when \"rsi(14) < 30 and close > ema(200)\" --side long")
        return 0
    cc.out("Idea Lab: %d idea%s saved, %d distinct rule%s tested." % (
        len(rows), "" if len(rows) == 1 else "s", tried_count(), "" if tried_count() == 1 else "s"))
    cc.out(cc.table(rows, ["idea", "side", "bars", "trades", "avg R", "luck", "verdict", "tested"], "lllrrrll"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="lab.py", description="Test a trading idea of your own on the past, after costs.")
    p.add_argument("command", nargs="?", default="test", choices=["test", "check", "words", "list"])
    p.add_argument("--when", default=None, help='when to enter, as a rule: "rsi(14) < 30 and close > ema(200)"')
    p.add_argument("--side", default=None, choices=["long", "short"])
    p.add_argument("--tf", default=None, help="bars: 5m, 15m, 1h (default), 4h or 1d")
    p.add_argument("--stop", default=None, help="pct:2, atr:2 (default) or swing:10")
    p.add_argument("--target", default=None, help="r:2 (default), pct:3 or none")
    p.add_argument("--hold", default=None, help="time limit: 90m, 24h (default), 3d; at most 14d")
    p.add_argument("--exit-when", dest="exit_when", default=None, help="an exit rule, read at each bar's close")
    p.add_argument("--coins", default=None, help="top:10 (default: the busiest 10) or a list like SOL,ETH")
    p.add_argument("--days", type=int, default=None, help="days to test (default by bars: 1h 180, 4h 365, 5m 30)")
    p.add_argument("--end", default=None, help="end the window here instead of now (YYYY-MM-DD), for data the idea has not seen")
    p.add_argument("--name", default=None, help="a name for the idea")
    p.add_argument("--idea", default=None, help="a saved idea file to run again")
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--no-luck", action="store_true", help="skip the random-entry luck test")
    p.add_argument("--no-cache", action="store_true", help="download everything again")
    cc.add_common(p)
    a = p.parse_args(argv)
    cc.apply_common(a)
    if a.command == "words":
        return cmd_words(a)
    if a.command == "check":
        return cmd_check(a)
    if a.command == "list":
        return cmd_list(a)
    return cmd_test(a, cc.load_config())


if __name__ == "__main__":
    sys.exit(cc.run(main))
