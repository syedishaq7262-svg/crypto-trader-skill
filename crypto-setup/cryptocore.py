# -*- coding: utf-8 -*-
"""cryptocore: the shared plumbing for crypto-trader-skill.

Standard library only, Python 3.8 or newer. Every skill in the pack imports
this one file. It holds:

  * the clock (pin it with --now and any run repeats exactly),
  * the crypto/ work folder and the config,
  * one HTTP client with retries, rate limits, record and replay,
  * three exchange clients that read free public data with no API key:
    Binance USD-M futures, Bybit linear perpetuals and Hyperliquid perps,
  * symbol resolution (btc, BTCUSDT, BTC/USDT, 1000PEPE and kPEPE all work),
  * indicators and small statistics,
  * plain-text formatting that Claude reads back to the user.

It never places an order and never asks for a key. Order signing lives in
crypto-broker only.
"""

import atexit
import email.utils
import gzip
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

VERSION = "1.1.0"
USER_AGENT = "crypto-trader-skill/%s (+https://github.com/crypto-trader-skill)" % VERSION


# ---------------------------------------------------------------- errors

class CryptoError(Exception):
    """A plain one-line problem to show the user as it is."""


class NetError(CryptoError):
    """The network or the exchange did not answer."""


class GeoBlocked(NetError):
    """The exchange refuses connections from this country or network."""


class RateLimited(NetError):
    """The exchange asked us to slow down and retries ran out."""


class OfflineMiss(NetError):
    """Offline mode has no saved answer for this request."""


# ---------------------------------------------------------------- clock

_PINNED_MS = None


def parse_when(text):
    """Parse a time the user typed. Returns epoch milliseconds (UTC).

    Accepts "2026-10-26 13:45", "2026-10-26T13:45:00Z", "2026-10-26",
    "2026-10-26 13:45+11:00", epoch seconds or epoch milliseconds.
    A time with no offset is UTC.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        v = float(text)
        return int(v if v > 1e11 else v * 1000)
    s = str(text).strip()
    if re.match(r"^\d{9,13}$", s):
        v = int(s)
        return v if v > 1e11 else v * 1000
    s = s.replace("UTC", "").replace("utc", "").replace("T", " ").strip()     # UTC first: its T is not a separator
    if s.endswith("Z"):
        s = s[:-1].strip()
    offset = None
    m = re.search(r"([+-])(\d{1,2}):?(\d{2})$", s)
    if m and len(s) > 10:
        sign = 1 if m.group(1) == "+" else -1
        offset = sign * (int(m.group(2)) * 60 + int(m.group(3)))
        s = s[:m.start()].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            dt = None
    if dt is None:
        raise CryptoError("could not read the time %r. Use YYYY-MM-DD HH:MM (UTC)." % text)
    dt = dt.replace(tzinfo=timezone.utc)
    if offset:
        dt = dt - timedelta(minutes=offset)
    return int(dt.timestamp() * 1000)


def set_now(value):
    """Pin the clock (None unpins). Every 'now' in the pack reads this."""
    global _PINNED_MS
    _PINNED_MS = parse_when(value) if value is not None else None


def now_ms():
    if _PINNED_MS is not None:
        return _PINNED_MS
    return int(time.time() * 1000)


def pinned():
    return _PINNED_MS is not None


EXCHANGE_HOSTS = ("fapi.binance.com", "api.bybit.com", "api.hyperliquid.xyz")


def exchange_now_ms():
    """'Now' on the exchanges' clock when this computer's clock runs ahead of it, so a bar the exchange is still
    building is never read as closed. A clock that runs behind only delays the newest bar a little."""
    now = now_ms()
    skew = getattr(HTTP, "skew_ms", None)
    if _PINNED_MS is not None or not skew or skew <= 1500:
        return now
    return now - int(skew)


def utc_dt(ms):
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def fmt_utc(ms, date=True, seconds=False):
    if ms is None:
        return "-"
    f = "%Y-%m-%d %H:%M" if date else "%H:%M"
    if seconds:
        f += ":%S"
    return utc_dt(ms).strftime(f) + " UTC"


# Standard offset (minutes) and daylight rule for common zones, used when Python has no time zone
# database (3.8, or Windows without the tzdata package). "us", "eu", "au", "nz" are the rules below.
FALLBACK_TZ = {
    "Australia/Melbourne": (600, "au"), "Australia/Sydney": (600, "au"), "Australia/Canberra": (600, "au"),
    "Australia/Hobart": (600, "au"), "Australia/Adelaide": (570, "au"), "Australia/Brisbane": (600, None),
    "Australia/Darwin": (570, None), "Australia/Perth": (480, None), "Pacific/Auckland": (720, "nz"),
    "Asia/Kolkata": (330, None), "Asia/Calcutta": (330, None), "Asia/Karachi": (300, None), "Asia/Dhaka": (360, None),
    "Asia/Dubai": (240, None), "Asia/Riyadh": (180, None), "Asia/Singapore": (480, None), "Asia/Kuala_Lumpur": (480, None),
    "Asia/Hong_Kong": (480, None), "Asia/Shanghai": (480, None), "Asia/Taipei": (480, None), "Asia/Manila": (480, None),
    "Asia/Tokyo": (540, None), "Asia/Seoul": (540, None), "Asia/Jakarta": (420, None), "Asia/Bangkok": (420, None),
    "Asia/Ho_Chi_Minh": (420, None), "Europe/London": (0, "eu"), "Europe/Dublin": (0, "eu"), "Europe/Lisbon": (0, "eu"),
    "Europe/Paris": (60, "eu"), "Europe/Berlin": (60, "eu"), "Europe/Madrid": (60, "eu"), "Europe/Rome": (60, "eu"),
    "Europe/Amsterdam": (60, "eu"), "Europe/Brussels": (60, "eu"), "Europe/Zurich": (60, "eu"), "Europe/Vienna": (60, "eu"),
    "Europe/Stockholm": (60, "eu"), "Europe/Oslo": (60, "eu"), "Europe/Copenhagen": (60, "eu"), "Europe/Warsaw": (60, "eu"),
    "Europe/Prague": (60, "eu"), "Europe/Athens": (120, "eu"), "Europe/Helsinki": (120, "eu"), "Europe/Kyiv": (120, "eu"),
    "Europe/Kiev": (120, "eu"), "Europe/Istanbul": (180, None), "Europe/Moscow": (180, None),
    "America/New_York": (-300, "us"), "America/Toronto": (-300, "us"), "America/Chicago": (-360, "us"),
    "America/Denver": (-420, "us"), "America/Phoenix": (-420, None), "America/Los_Angeles": (-480, "us"),
    "America/Sao_Paulo": (-180, None), "America/Mexico_City": (-360, None),
    "Africa/Lagos": (60, None), "Africa/Nairobi": (180, None), "Africa/Johannesburg": (120, None),
}
_TZ_UNKNOWN = set()


def _dst_on(rule, ms, std_minutes):
    """True when a fallback zone is on daylight time at ms, with the real switch instants:
    us 02:00 local (second Sunday of March to first Sunday of November), eu 01:00 UTC (last Sundays of
    March and October), au 02:00 local standard time (first Sunday of October to first Sunday of April),
    nz 02:00 local standard time (last Sunday of September to first Sunday of April)."""
    u = utc_dt(ms).replace(tzinfo=None)
    y = u.year
    std = timedelta(minutes=std_minutes)
    if rule == "us":
        start = _nth_sunday(y, 3, 2) + timedelta(hours=2) - std
        end = _nth_sunday(y, 11, 1) + timedelta(hours=1) - std          # 02:00 daylight is 01:00 standard
        return start <= u < end
    if rule == "eu":
        return _nth_sunday(y, 3, -1) + timedelta(hours=1) <= u < _nth_sunday(y, 10, -1) + timedelta(hours=1)
    if rule in ("au", "nz"):
        end = _nth_sunday(y, 4, 1) + timedelta(hours=2) - std             # 03:00 daylight is 02:00 standard
        start = (_nth_sunday(y, 10, 1) if rule == "au" else _nth_sunday(y, 9, -1)) + timedelta(hours=2) - std
        return u < end or u >= start
    return False


def _tz_offset_minutes(ms, tzname):
    """Offset of a display time zone at a moment, in minutes. '+11:00' or an IANA name."""
    if not tzname or tzname.upper() == "UTC":
        return 0
    m = re.match(r"^([+-])(\d{1,2}):?(\d{2})?$", tzname.strip())
    if m:
        sign = 1 if m.group(1) == "+" else -1
        return sign * (int(m.group(2)) * 60 + int(m.group(3) or 0))
    try:  # Python 3.9+ with a tz database
        zi = importlib.import_module("zoneinfo")
        off = utc_dt(ms).astimezone(zi.ZoneInfo(tzname)).utcoffset()
        return int(off.total_seconds() // 60)
    except Exception:
        pass
    if tzname in FALLBACK_TZ:
        std, rule = FALLBACK_TZ[tzname]
        return std + (60 if rule and _dst_on(rule, ms, std) else 0)
    _TZ_UNKNOWN.add(tzname)
    return 0


def tz_known(tzname):
    """Whether a display_tz value can be shown here (doctor reports it when not)."""
    _tz_offset_minutes(now_ms(), tzname)
    return tzname not in _TZ_UNKNOWN


def fmt_local(ms, tzname=None, date=True):
    """A time in the user's display zone, for example '21:00 (UTC+11)'."""
    if ms is None:
        return "-"
    tzname = tzname if tzname is not None else load_config().get("display_tz", "UTC")
    off = _tz_offset_minutes(ms, tzname)
    if off == 0:
        return fmt_utc(ms, date=date)
    dt = utc_dt(ms) + timedelta(minutes=off)
    sign = "+" if off >= 0 else "-"
    h, mnt = divmod(abs(off), 60)
    zone = "UTC%s%d" % (sign, h) + (":%02d" % mnt if mnt else "")
    return dt.strftime("%Y-%m-%d %H:%M" if date else "%H:%M") + " (%s)" % zone


def fmt_dur(ms):
    """2d 3h, 2h14m, 5m, 40s."""
    if ms is None:
        return "-"
    s = int(abs(ms) // 1000)
    sign = "-" if ms < 0 else ""
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%s%dd %dh" % (sign, d, h)
    if h:
        return "%s%dh%02dm" % (sign, h, m)
    if m:
        return "%s%dm" % (sign, m)
    return "%s%ds" % (sign, s)


# Sessions. Crypto never closes, but volume still gathers around these opens.
# Daylight saving is computed by hand so Python 3.8 and Windows need no tz data.

def _nth_sunday(year, month, n):
    """n-th Sunday of a month (n=-1: the last one)."""
    if n > 0:
        d = datetime(year, month, 1)
        first = d + timedelta(days=(6 - d.weekday()) % 7)
        return first + timedelta(days=7 * (n - 1))
    nxt = datetime(year + (month == 12), (month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def us_dst(y, mo, d):
    """US daylight time on a local date (second Sunday of March to first Sunday of November)."""
    day = datetime(y, mo, d)
    return _nth_sunday(y, 3, 2) <= day < _nth_sunday(y, 11, 1)


def uk_dst(y, mo, d):
    """UK summer time on a local date (last Sunday of March to last Sunday of October)."""
    day = datetime(y, mo, d)
    return _nth_sunday(y, 3, -1) <= day < _nth_sunday(y, 10, -1)


SESSIONS = {
    "utc": "the UTC day (00:00 UTC, when daily candles roll)",
    "tokyo": "Tokyo 09:00 (00:00 UTC)",
    "london": "London 08:00 (07:00 or 08:00 UTC)",
    "ny": "New York 09:30, the US stock open (13:30 or 14:30 UTC)",
}
SESSION_NAMES = {"utc": "UTC day", "tokyo": "Tokyo", "london": "London", "ny": "New York"}


def session_open_on(session, y, mo, d):
    """UTC epoch ms of a session's open on a calendar date."""
    base = datetime(y, mo, d, tzinfo=timezone.utc)
    if session in ("utc", "tokyo"):
        t = base
    elif session == "london":
        t = base + timedelta(hours=7 if uk_dst(y, mo, d) else 8)
    elif session == "ny":
        t = base + timedelta(hours=13 if us_dst(y, mo, d) else 14, minutes=30)
    else:
        raise CryptoError("unknown session %r. Use one of: %s" % (session, ", ".join(sorted(SESSIONS))))
    return int(t.timestamp() * 1000)


def last_session_open(session, at_ms=None):
    """The most recent open of a session at or before at_ms."""
    at_ms = now_ms() if at_ms is None else at_ms
    day = utc_dt(at_ms)
    for back in range(0, 4):
        dd = day - timedelta(days=back)
        t = session_open_on(session, dd.year, dd.month, dd.day)
        if t <= at_ms:
            return t
    raise CryptoError("no session open found")


# ---------------------------------------------------------------- work folder and config

DEFAULTS = {
    # where data comes from
    "venue": "auto",                 # auto, binance, bybit or hyperliquid
    "display_tz": "UTC",             # "+11:00" or an IANA name such as "Australia/Melbourne"
    "session": "utc",                # opening-range session: utc, tokyo, london or ny
    "min_quote_volume_usd": 20000000,
    "min_listing_days": 14,
    "max_symbols": 150,
    "exclude": [],
    # money and risk (crypto-budget, crypto-risk, crypto-liquidation, crypto-broker)
    "account_usd": 1000,
    "paper": True,
    "live_venue": "binance",
    "max_loss_per_trade_usd": 10,
    "max_daily_loss_usd": 30,
    "max_trades_per_day": 4,
    "max_open_positions": 2,
    "min_reward_risk": 1.5,
    "max_cost_r": 0.2,               # fees and slippage of a stop-out, in R; past it crypto-risk blocks. 0: off
    "require_stop": True,
    "max_leverage": 3,
    "max_margin_pct": 50,
    "margin_mode": "isolated",
    "allow_cross_margin": False,
    "allow_short": True,
    "min_liq_buffer": 2.0,
    "cooldown_after_losses": 2,
    "cooldown_hours": 12,
    "max_hold_hours": 24,
    "taker_fee_bps": None,           # None: the venue's base rate
    "maker_fee_bps": None,
    "slippage_bps": None,            # None: measured from the live order book when a plan is made
    "blocked": [],
    # news (crypto-news)
    "news_feeds": None,              # None: the built-in outlets; or a list of feed URLs, or [name, url] pairs
}

_CONFIG_CACHE = {}


def workdir():
    return os.environ.get("CRYPTO_DIR") or "crypto"


def wpath(*names):
    return os.path.join(workdir(), *names)


def read_json(path, default=None):
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, OSError):
        return default
    except ValueError as e:
        raise CryptoError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    """Atomic write: a reader never sees half a file."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def append_jsonl(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path):
    """Every record in a .jsonl log. A line that does not parse (one being written this instant) is skipped."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
    except (IOError, OSError):
        pass
    return out


def load_config(path=None):
    """DEFAULTS overlaid with crypto/config.json (or the given file)."""
    path = path or os.environ.get("CRYPTO_CONFIG") or wpath("config.json")
    key = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    hit = _CONFIG_CACHE.get(key)
    if hit and hit[0] == mtime:
        return dict(hit[1])
    cfg = dict(DEFAULTS)
    user = read_json(path, None) if mtime is not None else None
    if user:
        if not isinstance(user, dict):
            raise CryptoError("%s must hold a JSON object" % path)
        for k, v in user.items():
            if not k.startswith("_"):
                cfg[k] = v
    _CONFIG_CACHE[key] = (mtime, dict(cfg))
    return cfg


# ---------------------------------------------------------------- HTTP

_TRANSPORTS = {}


def _load_transport(path):
    """Tests swap the network for a fake exchange: CRYPTO_FAKE_EXCHANGE=tests/fake_exchange.py"""
    key = os.path.abspath(path)
    if key not in _TRANSPORTS:
        spec = importlib.util.spec_from_file_location("crypto_fake_exchange", key)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _TRANSPORTS[key] = mod.handle
    return _TRANSPORTS[key]


_BUCKETS = {}                 # host -> [weight left, at]: shared by every client in this process
_BUCKETS_LOCK = threading.Lock()
_BUCKETS_STATE = {"loaded": False, "used": False}


def _bucket_file():
    return wpath("cache", "ratelimit.json")


def _load_buckets(limits):
    """What the last runs (other tools, a minute ago) left of each host's budget, so a quick run of several tools
    in a row does not spend the same minute twice."""
    if _BUCKETS_STATE["loaded"]:
        return
    _BUCKETS_STATE["loaded"] = True
    try:
        saved = read_json(_bucket_file(), {}) or {}
    except CryptoError:
        saved = {}
    now = time.time()
    for host, cap in limits.items():
        b = saved.get(host)
        if isinstance(b, list) and len(b) == 2 and host not in _BUCKETS:
            try:
                level, at = float(b[0]), float(b[1])
            except (TypeError, ValueError):
                continue
            if 0 <= now - at < 120:
                _BUCKETS[host] = [min(float(cap), level), at]


def _save_buckets():
    if not _BUCKETS_STATE["used"]:
        return
    try:
        with _BUCKETS_LOCK:
            data = dict((h, [round(b[0], 2), b[1]]) for h, b in _BUCKETS.items())
        write_json(_bucket_file(), data)
    except (OSError, CryptoError):
        pass


atexit.register(_save_buckets)


class Http(object):
    """One client for every exchange call.

    Retries timeouts and 5xx with backoff, honours 429 and Retry-After, keeps
    each host under its rate limit with a token bucket, and can record every
    response to a folder (CRYPTO_RECORD=dir) or answer only from one
    (CRYPTO_OFFLINE=dir, used by the tests and by anyone replaying a session).
    """

    # weight per minute we allow ourselves, a little under each exchange's limit
    LIMITS = {
        "fapi.binance.com": 2000,
        "api.bybit.com": 6000,
        "api.hyperliquid.xyz": 1000,
        "stats-data.hyperliquid.xyz": 120,
        "api.alternative.me": 50,
    }

    def __init__(self):
        self.timeout = float(os.environ.get("CRYPTO_TIMEOUT", "15"))
        self.retries = int(os.environ.get("CRYPTO_RETRIES", "3"))
        self.offline = os.environ.get("CRYPTO_OFFLINE") or None
        self.record = os.environ.get("CRYPTO_RECORD") or None
        fake = os.environ.get("CRYPTO_FAKE_EXCHANGE")
        self.transport = _load_transport(fake) if fake else None
        self.pace = os.environ.get("CRYPTO_NO_PACE") is None
        self.stats = {"requests": 0, "bytes": 0, "seconds": 0.0, "retries": 0, "by_host": {}}
        self._lock = threading.Lock()
        self.skew_ms = None          # this computer's clock minus the exchanges' (from their Date headers)

    def _note_clock(self, host, rh):
        """Learn how far this computer's clock runs ahead of the exchange's, from a response's Date header."""
        if host not in EXCHANGE_HOSTS:
            return
        d = rh.get("Date") or rh.get("date")
        if not d:
            return
        try:
            server = email.utils.parsedate_to_datetime(d).timestamp() * 1000.0
        except (TypeError, ValueError, IndexError, OverflowError):
            return
        skew = time.time() * 1000.0 - server       # the header is whole seconds, so this reads up to 1 s high
        with self._lock:
            self.skew_ms = skew if self.skew_ms is None else min(self.skew_ms, skew)

    # -- rate limiting
    def _paced(self, host):
        return bool(self.LIMITS.get(host)) and self.pace and not self.offline and not self.transport

    def _take(self, host, weight):
        if not self._paced(host):
            return
        cap = self.LIMITS[host]
        _load_buckets(self.LIMITS)
        _BUCKETS_STATE["used"] = True
        while True:
            with _BUCKETS_LOCK:
                b = _BUCKETS.get(host)
                now = time.time()
                if b is None:
                    b = [float(cap), now]
                    _BUCKETS[host] = b
                b[0] = min(float(cap), b[0] + (now - b[1]) * cap / 60.0)
                b[1] = now
                if b[0] >= weight:
                    b[0] -= weight
                    return
                wait = (weight - b[0]) * 60.0 / cap
            time.sleep(min(max(wait, 0.05), 5.0))

    def _spent(self, host):
        """The host said 'too many requests': whatever we counted, its budget is empty now."""
        if not self._paced(host):
            return
        with _BUCKETS_LOCK:
            b = _BUCKETS.get(host)
            if b is not None:
                b[0] = min(b[0], 0.0)
                b[1] = time.time()
        _save_buckets()

    @staticmethod
    def key(method, url, params=None, body=None):
        q = urllib.parse.urlencode(sorted((params or {}).items()))
        b = json.dumps(body, sort_keys=True, separators=(",", ":")) if body is not None else ""
        raw = "%s %s?%s %s" % (method.upper(), url, q, b)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20], raw

    def _fixture_path(self, root, host, k):
        return os.path.join(root, host, k + ".json.gz")

    MAX_WAIT_S = 10.0          # a server that asks for a longer pause gets an answer now, not a frozen tool

    def request(self, method, url, params=None, body=None, headers=None, weight=1, raw=False,
                query=None, data=None, errors="raise", retry=True, private=False, timeout=None, retries=None):
        """Returns parsed JSON (or text with raw=True). Raises CryptoError subclasses.

        Signed account calls (crypto-broker) pass the exact `query` string or body `data`
        they signed, `private=True` (never recorded, never answered offline), `retry=False`
        for anything that places or changes an order (a retried timeout could place it twice),
        and errors="return" to get (status, body) back instead of an exception. Optional
        sources (news feeds, the macro calendar) pass a short `timeout` and few `retries`,
        so a slow website never holds up a market answer.
        """
        method = method.upper()
        host = urllib.parse.urlparse(url).netloc
        if private and self.offline and not self.transport:
            raise CryptoError("offline mode cannot reach your exchange account")
        k, desc = self.key(method, url, params if query is None else {"_q": query}, body)
        if self.offline and not private:
            p = self._fixture_path(self.offline, host, k)
            rec = read_json(p, None)
            if rec is None:
                raise OfflineMiss("offline mode: no saved response for %s" % desc[:240])
            return self._finish(host, rec["status"], rec.get("headers") or {}, rec["text"].encode("utf-8"), raw)
        full = url
        if query:
            full += ("&" if "?" in url else "?") + query
        elif params:
            full += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
        if data is not None:
            data = data.encode("utf-8") if isinstance(data, str) else data
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        attempt = 0
        max_retries = (self.retries if retries is None else min(retries, self.retries)) if retry else 0
        while True:
            self._take(host, weight)
            t0 = time.time()
            status, rh, payload, err = self._send(method, full, data, hdrs, timeout or self.timeout)
            dt = time.time() - t0
            with self._lock:
                self.stats["requests"] += 1
                self.stats["bytes"] += len(payload or b"")
                self.stats["seconds"] += dt
                hs = self.stats["by_host"].setdefault(host, {"requests": 0, "seconds": 0.0, "max": 0.0})
                hs["requests"] += 1
                hs["seconds"] += dt
                hs["max"] = max(hs["max"], dt)
            retry_after = None
            if err is None and status in (429, 418):
                if status == 418:
                    raise RateLimited("%s has temporarily banned this IP for too many requests (HTTP 418). Wait a few minutes." % host)
                ra = rh.get("Retry-After") or rh.get("retry-after")
                try:
                    retry_after = float(ra) if ra else None
                except ValueError:
                    retry_after = None
                err = "rate limited (HTTP 429)"
                self._spent(host)
            elif err is None and status >= 500:
                err = "HTTP %d" % status
            if err is None:
                self._note_clock(host, rh)
                break
            if retry_after is not None and retry_after > self.MAX_WAIT_S and errors != "return":
                raise RateLimited("%s asked us to wait %d seconds (too many requests). Try again after that."
                                  % (host, round(retry_after)))
            if attempt >= max_retries:
                if errors == "return" and status:
                    break
                if status in (429,):
                    raise RateLimited("%s kept answering 'too many requests'. Try again in a minute." % host)
                raise NetError("could not reach %s: %s" % (host, err))
            attempt += 1
            with self._lock:
                self.stats["retries"] += 1
            pause = retry_after if retry_after is not None else 0.6 * (2 ** (attempt - 1)) + random.random() * 0.3
            time.sleep(min(pause, 20.0))
        text = payload.decode("utf-8", "replace")
        if errors == "return":
            try:
                parsed = json.loads(text) if text.strip() else None
            except ValueError:
                parsed = {"_text": " ".join(text.split())[:300]}
            return status, parsed
        if self.record and status < 500 and not private:
            p = self._fixture_path(self.record, host, k)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            rec = {"request": desc, "status": status, "text": text}
            with gzip.open(p, "wt", encoding="utf-8") as fh:
                json.dump(rec, fh)
        return self._finish(host, status, rh, payload, raw)

    def _send(self, method, full, data, hdrs, timeout=None):
        """One attempt. Returns (status, headers, bytes, error-or-None)."""
        timeout = timeout or self.timeout
        if self.transport is not None:
            try:
                status, rh, payload = self.transport(method, full, data, hdrs)
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")
                return status, rh or {}, payload, None
            except (socket.timeout, TimeoutError) as e:
                return 0, {}, b"", "timed out"
            except ConnectionError as e:
                return 0, {}, b"", str(e) or "connection failed"
        req = urllib.request.Request(full, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
                rh = dict(resp.headers.items())
                status = resp.status
        except urllib.error.HTTPError as e:
            payload = e.read() or b""
            rh = dict(e.headers.items()) if e.headers else {}
            status = e.code
        except ssl.SSLError as e:
            return 0, {}, b"", ("SSL problem (%s). On macOS run 'Install Certificates.command' "
                                 "from your Python folder." % e)
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
                return 0, {}, b"", ("SSL certificate check failed. On macOS run 'Install Certificates.command' "
                                     "from your Python folder.")
            return 0, {}, b"", str(reason)
        except (socket.timeout, TimeoutError):
            return 0, {}, b"", "timed out after %ss" % self.timeout
        except (ConnectionError, OSError) as e:
            return 0, {}, b"", str(e) or "connection failed"
        enc = (rh.get("Content-Encoding") or rh.get("content-encoding") or "").lower()
        if enc == "gzip" or payload[:2] == b"\x1f\x8b":
            try:
                payload = gzip.decompress(payload)
            except (OSError, EOFError):
                pass
        return status, rh, payload, None

    def _finish(self, host, status, rh, payload, raw):
        text = payload.decode("utf-8", "replace") if isinstance(payload, bytes) else payload
        low = text[:600].lower()
        if status == 451 or (status == 403 and ("country" in low or "region" in low or "cloudfront" in low)):
            raise GeoBlocked("%s refuses connections from this location (HTTP %d)" % (host, status))
        if raw:
            if status >= 400:
                raise NetError("%s answered HTTP %d" % (host, status))
            return text
        try:
            data = json.loads(text) if text.strip() else None
        except ValueError:
            snippet = " ".join(text.split())[:120]
            raise NetError("%s answered HTTP %d with something that is not JSON: %s" % (host, status, snippet))
        if status >= 400:
            msg = None
            if isinstance(data, dict):
                msg = data.get("msg") or data.get("retMsg") or data.get("error") or data.get("message")
            raise CryptoError("%s refused the request (HTTP %d): %s" % (host, status, msg or " ".join(text.split())[:160]))
        return data

    def summary(self):
        s = self.stats
        if not s["requests"]:
            return "0 requests"
        parts = []
        for h, v in sorted(s["by_host"].items()):
            parts.append("%s %d req, avg %.2fs, max %.2fs" % (h, v["requests"], v["seconds"] / v["requests"], v["max"]))
        return "%d requests, %.1f KB, %d retries; %s" % (s["requests"], s["bytes"] / 1024.0, s["retries"], "; ".join(parts))


HTTP = Http()


def reset_http():
    """Build a fresh client (tests change the environment between runs)."""
    global HTTP
    HTTP = Http()
    return HTTP


def parallel(fn, items, workers=8):
    """Run fn over items on a thread pool. Returns [(item, result, error_or_None)] in input order."""
    items = list(items)
    if not items:
        return []
    out = [None] * len(items)

    def run(i):
        try:
            out[i] = (items[i], fn(items[i]), None)
        except CryptoError as e:
            out[i] = (items[i], None, e)
        except Exception as e:  # a parsing surprise on one symbol should not sink a scan
            out[i] = (items[i], None, CryptoError("%s: %s" % (type(e).__name__, e)))

    if workers <= 1 or len(items) == 1:
        for i in range(len(items)):
            run(i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(run, range(len(items))))
    return out


# ---------------------------------------------------------------- numbers

def fnum(x, default=None):
    if x is None or x == "":
        return default
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


INTERVAL_MS = {
    "1m": 60000, "3m": 180000, "5m": 300000, "15m": 900000, "30m": 1800000,
    "1h": 3600000, "2h": 7200000, "4h": 14400000, "6h": 21600000, "8h": 28800000,
    "12h": 43200000, "1d": 86400000, "3d": 259200000, "1w": 604800000,
}


def interval_ms(interval):
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise CryptoError("unknown interval %r. Use one of: %s" % (interval, ", ".join(INTERVAL_MS)))


# A bar is a tuple: (open_time_ms, open, high, low, close, base_volume, quote_volume)
T, O, H, L, C, V, QV = range(7)

STABLES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDE", "USDP", "PYUSD", "USD1", "RLUSD", "EUR", "EURI", "USDS"}
MULT_PREFIX = re.compile(r"^(1000000|100000|10000|1000|100|1M)(?=[A-Z])")
MULT_SUFFIX = re.compile(r"(?<=[A-Z])(1000000|100000|10000|1000)$")


def split_multiplier(base):
    """'1000PEPE' -> ('PEPE', 1000); 'SHIB1000' -> ('SHIB', 1000); 'kPEPE' -> ('PEPE', 1000)."""
    if len(base) > 2 and base[0] == "k" and base[1:].isupper():
        return base[1:], 1000
    m = MULT_PREFIX.match(base)
    if m and len(base) > len(m.group(1)) + 1:
        g = m.group(1)
        return base[len(g):], (1000000 if g == "1M" else int(g))
    m = MULT_SUFFIX.search(base)
    if m and len(base) > len(m.group(1)) + 1:
        return base[:m.start()], int(m.group(1))
    return base, 1


def clean_coin(text):
    """What the user typed -> a bare coin name: 'btc/usdt' -> 'BTC', '1000pepe' -> 'PEPE'."""
    s = str(text).strip()
    if len(s) > 2 and s[0] == "k" and s[1:].isupper():
        return s[1:]
    s = s.upper().replace(" ", "")
    for junk in (".P", "-PERP", "PERP", "-SWAP", ":USDT", ":USDC", "_PERP"):
        if s.endswith(junk):
            s = s[: -len(junk)]
    s = s.replace("/", "").replace("-", "").replace("_", "")
    for q in ("USDT", "USDC", "BUSD", "USD"):
        if s.endswith(q) and len(s) > len(q):
            s = s[: -len(q)]
            break
    coin, _ = split_multiplier(s)
    return coin


# ---------------------------------------------------------------- venues

class Venue(object):
    """Common shape. Subclasses fill in the exchange calls.

    market dict: symbol, coin, base, multiplier, quote, tick, step, min_qty,
                 min_notional, max_leverage, funding_hours, listed_ms, venue
    ticker dict: symbol, last, mark, index, change, high, low, quote_volume,
                 funding, funding_hours, next_funding_ms, oi_usd, bid, ask
    """

    name = "?"
    label = "?"
    default_top = 150
    taker_fee = 0.0005
    maker_fee = 0.0002
    max_bars = 1000
    has_oi_history = True
    has_long_short = True

    def __init__(self, http=None):
        self._http = http
        self._markets = None
        self._by_symbol = None
        self._tickers = None
        self._tickers_at = None

    @property
    def http(self):
        return self._http or HTTP

    # -- markets and symbols
    def markets(self):
        if self._markets is None:
            ms = self.fetch_markets()
            self._markets = sorted(ms, key=lambda m: m["symbol"])
            self._by_symbol = dict((m["symbol"], m) for m in self._markets)
        return self._markets

    def market(self, text):
        """Resolve what the user typed to one market, or raise a helpful error."""
        self.markets()
        raw = str(text).strip()
        for cand in (raw, raw.upper(), raw.upper().replace("/", "").replace("-", "")):
            if cand in self._by_symbol:
                return self._by_symbol[cand]
        coin = clean_coin(raw)
        hits = [m for m in self._markets if m["coin"] == coin]
        if not hits:
            close = sorted(set(m["coin"] for m in self._markets if m["coin"].startswith(coin[:2])))[:8]
            hint = (" Close matches: %s." % ", ".join(close)) if close else ""
            raise CryptoError("%s has no %s perpetual for %r.%s" % (self.label, self.quote_label(), raw, hint))
        hits.sort(key=lambda m: (m["multiplier"] != 1, m["symbol"]))
        return hits[0]

    def quote_label(self):
        return "USDT"

    # -- prices
    def tickers(self, fresh=False):
        if self._tickers is None or fresh:
            self._tickers = self.fetch_tickers()
            self._tickers_at = now_ms()
        return self._tickers

    def ticker(self, symbol):
        t = self.tickers().get(symbol)
        if t is None:
            raise CryptoError("%s has no ticker for %s" % (self.label, symbol))
        return t

    def bars(self, symbol, interval, count, end_ms=None):
        """The last `count` CLOSED bars ending at end_ms (default now), oldest first."""
        step = interval_ms(interval)
        end = exchange_now_ms() if end_ms is None else min(end_ms, exchange_now_ms())
        last_open = (end // step) * step - step   # the newest bar that has closed by `end`
        start = last_open - (count - 1) * step
        rows = self.fetch_bars(symbol, interval, start, last_open + step - 1)
        out = [b for b in rows if start <= b[T] <= last_open]
        dedup = {}
        for b in out:
            dedup[b[T]] = b
        return [dedup[t] for t in sorted(dedup)]

    def bars_between(self, symbol, interval, start_ms, end_ms):
        """Closed bars whose open time is in [start_ms, end_ms], capped at now."""
        step = interval_ms(interval)
        cap = (exchange_now_ms() // step) * step - step
        last = min((end_ms // step) * step, cap)
        first = (start_ms // step) * step
        if last < first:
            return []
        rows = self.fetch_bars(symbol, interval, first, last + step - 1)
        dedup = {}
        for b in rows:
            if first <= b[T] <= last:
                dedup[b[T]] = b
        return [dedup[t] for t in sorted(dedup)]

    # -- subclasses implement
    def fetch_markets(self):
        raise NotImplementedError

    def fetch_tickers(self):
        raise NotImplementedError

    def fetch_bars(self, symbol, interval, start_ms, end_ms):
        raise NotImplementedError

    def funding_history(self, symbol, start_ms, end_ms):
        return []

    def open_interest(self, symbol):
        t = self.tickers().get(symbol) or {}
        return t.get("oi_usd")

    def oi_history(self, symbol, period="1h", count=48):
        return None

    def long_short(self, symbol, period="1h", count=48):
        return None

    def taker_ratio(self, symbol, period="1h", count=48):
        return None

    def orderbook(self, symbol, depth=50):
        return [], []

    def risk_tiers(self, symbol):
        return None


def _paginate_forward(fetch_page, start_ms, end_ms, step_ms, page_size, guard=200):
    """Walk [start, end] in pages of page_size bars. fetch_page(s, e) returns bars."""
    out = []
    s = start_ms
    n = 0
    while s <= end_ms and n < guard:
        e = min(end_ms, s + step_ms * page_size - 1)
        page = fetch_page(s, e)
        n += 1
        if page:
            out.extend(page)
        s = e + 1
    return out


class Binance(Venue):
    name = "binance"
    label = "Binance USD-M futures"
    BASE = "https://fapi.binance.com"
    taker_fee = 0.0005
    maker_fee = 0.0002
    max_bars = 1500

    def get(self, path, params=None, weight=1):
        return self.http.request("GET", self.BASE + path, params=params, weight=weight)

    def fetch_markets(self):
        info = self.get("/fapi/v1/exchangeInfo", weight=1)
        intervals = {}
        try:
            for row in self.get("/fapi/v1/fundingInfo") or []:
                h = fnum(row.get("fundingIntervalHours"))
                if h:
                    intervals[row.get("symbol")] = h
        except CryptoError:
            pass
        return self.parse_markets(info, intervals)

    @staticmethod
    def parse_markets(info, intervals=None):
        intervals = intervals or {}
        out = []
        for s in (info or {}).get("symbols", []):
            if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
                continue
            if s.get("quoteAsset") != "USDT" or s.get("marginAsset", "USDT") != "USDT":
                continue
            if s.get("underlyingType", "COIN") != "COIN":
                continue
            base = s.get("baseAsset", "")
            coin, mult = split_multiplier(base)
            f = dict((x.get("filterType"), x) for x in s.get("filters", []))
            out.append({
                "venue": "binance", "symbol": s["symbol"], "base": base, "coin": coin, "multiplier": mult,
                "quote": "USDT",
                "tick": fnum((f.get("PRICE_FILTER") or {}).get("tickSize")),
                "step": fnum((f.get("LOT_SIZE") or {}).get("stepSize")),
                "min_qty": fnum((f.get("LOT_SIZE") or {}).get("minQty")),
                "min_notional": fnum((f.get("MIN_NOTIONAL") or {}).get("notional")),
                "max_leverage": None,
                "funding_hours": intervals.get(s["symbol"], 8.0),
                "listed_ms": int(s.get("onboardDate") or 0) or None,
                "tags": s.get("underlyingSubType") or [],
            })
        return out

    def fetch_tickers(self):
        day = self.get("/fapi/v1/ticker/24hr", weight=40)
        prem = self.get("/fapi/v1/premiumIndex", weight=10)
        book = self.get("/fapi/v1/ticker/bookTicker", weight=5)
        return self.parse_tickers(day, prem, book, self._funding_hours())

    def _funding_hours(self):
        try:
            return dict((m["symbol"], m["funding_hours"]) for m in self.markets())
        except CryptoError:
            return {}

    @staticmethod
    def parse_tickers(day, prem, book, hours=None):
        hours = hours or {}
        pm = dict((p.get("symbol"), p) for p in (prem or []))
        bk = dict((b.get("symbol"), b) for b in (book or []))
        out = {}
        for d in day or []:
            sym = d.get("symbol")
            p = pm.get(sym, {})
            b = bk.get(sym, {})
            last = fnum(d.get("lastPrice"))
            if not last:
                continue
            out[sym] = {
                "symbol": sym, "last": last,
                "mark": fnum(p.get("markPrice"), last), "index": fnum(p.get("indexPrice")),
                "change": (fnum(d.get("priceChangePercent"), 0.0) or 0.0) / 100.0,
                "open24": fnum(d.get("openPrice")),
                "high": fnum(d.get("highPrice")), "low": fnum(d.get("lowPrice")),
                "quote_volume": fnum(d.get("quoteVolume"), 0.0),
                "funding": fnum(p.get("lastFundingRate")),
                "funding_hours": hours.get(sym, 8.0),
                "next_funding_ms": int(p.get("nextFundingTime") or 0) or None,
                "oi_usd": None,
                "bid": fnum(b.get("bidPrice")), "ask": fnum(b.get("askPrice")),
                "time": int(d.get("closeTime") or 0) or None,
            }
        return out

    def fetch_bars(self, symbol, interval, start_ms, end_ms):
        interval_ms(interval)

        def page(s, e):
            n = int((e - s) // INTERVAL_MS[interval]) + 1
            lim = max(1, min(self.max_bars, n))
            w = 1 if lim < 100 else 2 if lim < 500 else 5 if lim <= 1000 else 10
            rows = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": interval,
                                                "startTime": s, "endTime": e, "limit": lim}, weight=w)
            return self.parse_bars(rows)
        return _paginate_forward(page, start_ms, end_ms, INTERVAL_MS[interval], self.max_bars)

    @staticmethod
    def parse_bars(rows):
        try:
            return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[7]))
                    for r in rows or []]
        except (TypeError, ValueError, IndexError):
            raise CryptoError("Binance sent a malformed candle; try again in a moment")

    def funding_history(self, symbol, start_ms, end_ms):
        out = []
        s = start_ms
        for _ in range(20):
            rows = self.get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": s, "endTime": end_ms, "limit": 1000})
            got = [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows or []]
            out.extend(got)
            if len(got) < 1000:
                break
            s = got[-1][0] + 1
        return sorted(set(out))

    def open_interest(self, symbol):
        r = self.get("/fapi/v1/openInterest", {"symbol": symbol})
        oi = fnum((r or {}).get("openInterest"))
        t = self.tickers().get(symbol) or {}
        px = t.get("mark") or t.get("last")
        return oi * px if (oi is not None and px) else None

    PERIODS = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")

    def _data(self, path, symbol, period, count):
        if period not in self.PERIODS:
            raise CryptoError("Binance data periods: %s" % ", ".join(self.PERIODS))
        end = (now_ms() // INTERVAL_MS[period]) * INTERVAL_MS[period]
        start = end - INTERVAL_MS[period] * (min(count, 500) - 1) - 1
        return self.get(path, {"symbol": symbol, "period": period, "limit": min(count, 500),
                               "startTime": start, "endTime": end}) or []

    def oi_history(self, symbol, period="1h", count=48):
        rows = self._data("/futures/data/openInterestHist", symbol, period, count)
        return [(int(r["timestamp"]), float(r["sumOpenInterestValue"])) for r in rows]

    def long_short(self, symbol, period="1h", count=48):
        rows = self._data("/futures/data/globalLongShortAccountRatio", symbol, period, count)
        return [(int(r["timestamp"]), float(r["longAccount"])) for r in rows]

    def top_long_short(self, symbol, period="1h", count=48):
        rows = self._data("/futures/data/topLongShortPositionRatio", symbol, period, count)
        return [(int(r["timestamp"]), float(r["longAccount"])) for r in rows]

    def taker_ratio(self, symbol, period="1h", count=48):
        rows = self._data("/futures/data/takerlongshortRatio", symbol, period, count)
        return [(int(r["timestamp"]), float(r["buySellRatio"])) for r in rows]

    def orderbook(self, symbol, depth=100):
        lim = min([x for x in (5, 10, 20, 50, 100, 500, 1000) if x >= depth] or [1000])
        w = 2 if lim <= 50 else 5 if lim <= 100 else 10 if lim <= 500 else 20
        r = self.get("/fapi/v1/depth", {"symbol": symbol, "limit": lim}, weight=w) or {}
        return ([(float(p), float(q)) for p, q in r.get("bids", [])],
                [(float(p), float(q)) for p, q in r.get("asks", [])])


TRADFI_TYPES = ("stock", "etf", "commodity", "forex")    # Bybit's symbolType for contracts on traditional markets


class Bybit(Venue):
    name = "bybit"
    label = "Bybit linear perpetuals"
    BASE = "https://api.bybit.com"
    taker_fee = 0.00055
    maker_fee = 0.0002
    max_bars = 1000
    BYBIT_INTERVAL = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "2h": "120",
                      "4h": "240", "6h": "360", "12h": "720", "1d": "D", "1w": "W"}
    PERIODS = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1d"}

    def get(self, path, params=None, weight=1):
        r = self.http.request("GET", self.BASE + path, params=params, weight=weight)
        if not isinstance(r, dict):
            raise NetError("Bybit answered something unexpected for %s" % path)
        code = r.get("retCode")
        if str(code) != "0":
            raise CryptoError("Bybit: %s (retCode %s)" % (r.get("retMsg"), code))
        return r.get("result") or {}

    def fetch_markets(self):
        rows = []
        cursor = None
        for _ in range(10):
            params = {"category": "linear", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = self.get("/v5/market/instruments-info", params)
            rows.extend(res.get("list") or [])
            cursor = res.get("nextPageCursor")
            if not cursor:
                break
        return self.parse_markets(rows)

    @staticmethod
    def parse_markets(rows):
        out = []
        for s in rows or []:
            if s.get("contractType") != "LinearPerpetual" or s.get("status") != "Trading":
                continue
            if s.get("quoteCoin") != "USDT" or s.get("settleCoin", "USDT") != "USDT":
                continue
            if s.get("isPreListing") in (True, "true"):
                continue
            if str(s.get("symbolType") or "").lower() in TRADFI_TYPES or s.get("marketRegion"):
                continue                     # a stock, ETF, commodity or forex contract: not a 24/7 crypto market
            base = s.get("baseCoin", "")
            coin, mult = split_multiplier(base)
            pf = s.get("priceFilter") or {}
            lf = s.get("lotSizeFilter") or {}
            lev = s.get("leverageFilter") or {}
            fi = fnum(s.get("fundingInterval"))
            out.append({
                "venue": "bybit", "symbol": s["symbol"], "base": base, "coin": coin, "multiplier": mult,
                "quote": "USDT", "tick": fnum(pf.get("tickSize")), "step": fnum(lf.get("qtyStep")),
                "min_qty": fnum(lf.get("minOrderQty")), "min_notional": fnum(lf.get("minNotionalValue")),
                "max_leverage": fnum(lev.get("maxLeverage")),
                "funding_hours": (fi / 60.0) if fi else 8.0,
                "listed_ms": int(s.get("launchTime") or 0) or None,
                "tags": [],
            })
        return out

    def fetch_tickers(self):
        res = self.get("/v5/market/tickers", {"category": "linear"})
        return self.parse_tickers(res.get("list") or [], self._hours())

    def _hours(self):
        try:
            return dict((m["symbol"], m["funding_hours"]) for m in self.markets())
        except CryptoError:
            return {}

    @staticmethod
    def parse_tickers(rows, hours=None):
        hours = hours or {}
        out = {}
        for d in rows:
            sym = d.get("symbol")
            last = fnum(d.get("lastPrice"))
            if not last:
                continue
            prev = fnum(d.get("prevPrice24h"))
            out[sym] = {
                "symbol": sym, "last": last, "mark": fnum(d.get("markPrice"), last), "index": fnum(d.get("indexPrice")),
                "change": fnum(d.get("price24hPcnt"), 0.0) or 0.0, "open24": prev,
                "high": fnum(d.get("highPrice24h")), "low": fnum(d.get("lowPrice24h")),
                "quote_volume": fnum(d.get("turnover24h"), 0.0),
                "funding": fnum(d.get("fundingRate")), "funding_hours": hours.get(sym, 8.0),
                "next_funding_ms": int(d.get("nextFundingTime") or 0) or None,
                "oi_usd": fnum(d.get("openInterestValue")),
                "bid": fnum(d.get("bid1Price")), "ask": fnum(d.get("ask1Price")), "time": None,
            }
        return out

    def fetch_bars(self, symbol, interval, start_ms, end_ms):
        code = self.BYBIT_INTERVAL.get(interval)
        if not code:
            raise CryptoError("Bybit has no %s candles" % interval)

        def page(s, e):
            n = int((e - s) // INTERVAL_MS[interval]) + 1
            res = self.get("/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": code,
                                                "start": s, "end": e, "limit": max(1, min(self.max_bars, n))})
            return self.parse_bars(res.get("list") or [])
        return _paginate_forward(page, start_ms, end_ms, INTERVAL_MS[interval], self.max_bars)

    @staticmethod
    def parse_bars(rows):
        try:
            out = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), float(r[6])) for r in rows]
        except (TypeError, ValueError, IndexError):
            raise CryptoError("Bybit sent a malformed candle; try again in a moment")
        out.sort(key=lambda b: b[0])   # Bybit sends newest first
        return out

    def funding_history(self, symbol, start_ms, end_ms):
        out = []
        e = end_ms
        for _ in range(30):
            res = self.get("/v5/market/funding/history", {"category": "linear", "symbol": symbol,
                                                          "startTime": start_ms, "endTime": e, "limit": 200})
            got = [(int(r["fundingRateTimestamp"]), float(r["fundingRate"])) for r in res.get("list") or []]
            out.extend(got)
            if len(got) < 200:
                break
            e = min(t for t, _ in got) - 1
            if e < start_ms:
                break
        return sorted(set(out))

    def _period(self, period):
        p = self.PERIODS.get(period)
        if not p:
            raise CryptoError("Bybit data periods: %s" % ", ".join(self.PERIODS))
        return p

    def oi_history(self, symbol, period="1h", count=48):
        step = INTERVAL_MS[period]
        end = (now_ms() // step) * step
        res = self.get("/v5/market/open-interest", {"category": "linear", "symbol": symbol,
                                                    "intervalTime": self._period(period), "limit": min(count, 200),
                                                    "startTime": end - step * (min(count, 200) - 1), "endTime": end})
        rows = [(int(r["timestamp"]), float(r["openInterest"])) for r in res.get("list") or []]
        rows.sort()
        t = self.tickers().get(symbol) or {}
        px = t.get("mark") or t.get("last") or 1.0
        return [(ts, v * px) for ts, v in rows]   # coins at today's price: fine for % change

    def long_short(self, symbol, period="1h", count=48):
        step = INTERVAL_MS[period]
        end = (now_ms() // step) * step
        res = self.get("/v5/market/account-ratio", {"category": "linear", "symbol": symbol,
                                                    "period": self._period(period), "limit": min(count, 500),
                                                    "startTime": end - step * (min(count, 500) - 1), "endTime": end})
        rows = [(int(r["timestamp"]), float(r["buyRatio"])) for r in res.get("list") or []]
        rows.sort()
        return rows

    def orderbook(self, symbol, depth=100):
        res = self.get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": min(max(depth, 1), 500)})
        return ([(float(p), float(q)) for p, q in res.get("b") or []],
                [(float(p), float(q)) for p, q in res.get("a") or []])

    def risk_tiers(self, symbol):
        res = self.get("/v5/market/risk-limit", {"category": "linear", "symbol": symbol})
        tiers = []
        for r in res.get("list") or []:
            tiers.append({"max_notional": fnum(r.get("riskLimitValue")), "mmr": fnum(r.get("maintenanceMargin")),
                          "max_leverage": fnum(r.get("maxLeverage")), "deduction": fnum(r.get("mmDeduction"), 0.0)})
        tiers.sort(key=lambda x: x["max_notional"] or 0)
        return tiers or None


class Hyperliquid(Venue):
    name = "hyperliquid"
    label = "Hyperliquid perps"
    URL = "https://api.hyperliquid.xyz/info"
    default_top = 40
    taker_fee = 0.00045
    maker_fee = 0.00015
    max_bars = 5000
    has_oi_history = False
    has_long_short = False
    HL_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "3d", "1w")

    def __init__(self, http=None):
        Venue.__init__(self, http)
        self._ctx = None

    def quote_label(self):
        return "USD"

    def post(self, body, weight=20):
        return self.http.request("POST", self.URL, body=body, weight=weight)

    def _meta_ctx(self):
        if self._ctx is None:
            self._ctx = self.post({"type": "metaAndAssetCtxs"}, weight=20)
        return self._ctx

    def fetch_markets(self):
        return self.parse_markets(self._meta_ctx())

    @staticmethod
    def parse_markets(meta_ctx):
        meta = meta_ctx[0] if isinstance(meta_ctx, list) else meta_ctx
        tables = {}
        for entry in (meta or {}).get("marginTables") or []:
            try:
                tables[int(entry[0])] = entry[1]
            except (TypeError, ValueError, IndexError):
                pass
        out = []
        for a in (meta or {}).get("universe") or []:
            name = a.get("name", "")
            if a.get("isDelisted") or ":" in name:
                continue
            coin, mult = split_multiplier(name)
            szd = int(a.get("szDecimals") or 0)
            out.append({
                "venue": "hyperliquid", "symbol": name, "base": name, "coin": coin, "multiplier": mult,
                "quote": "USD", "tick": None, "sz_decimals": szd, "step": 10 ** (-szd),
                "min_qty": 10 ** (-szd), "min_notional": 10.0,
                "max_leverage": fnum(a.get("maxLeverage")), "funding_hours": 1.0, "listed_ms": None,
                "only_isolated": bool(a.get("onlyIsolated")), "margin_table": tables.get(a.get("marginTableId")),
                "tags": [],
            })
        return out

    def fetch_tickers(self):
        if self._tickers is not None:
            self._ctx = None          # a refresh asks again
        return self.parse_tickers(self._meta_ctx(), now_ms())

    @staticmethod
    def parse_tickers(meta_ctx, at_ms):
        meta, ctxs = meta_ctx[0], meta_ctx[1]
        out = {}
        nxt = ((at_ms // 3600000) + 1) * 3600000
        for a, c in zip(meta.get("universe") or [], ctxs or []):
            name = a.get("name")
            mark = fnum(c.get("markPx"))
            if not mark or a.get("isDelisted"):
                continue
            prev = fnum(c.get("prevDayPx"))
            imp = c.get("impactPxs") or [None, None]
            oi = fnum(c.get("openInterest"), 0.0)
            out[name] = {
                "symbol": name, "last": fnum(c.get("midPx"), mark), "mark": mark, "index": fnum(c.get("oraclePx")),
                "change": (mark / prev - 1.0) if prev else 0.0, "open24": prev, "high": None, "low": None,
                "quote_volume": fnum(c.get("dayNtlVlm"), 0.0),
                "funding": fnum(c.get("funding")), "funding_hours": 1.0, "next_funding_ms": nxt,
                "oi_usd": oi * mark, "bid": fnum(imp[0]), "ask": fnum(imp[1]), "time": at_ms,
                "premium": fnum(c.get("premium")),
            }
        return out

    def fetch_bars(self, symbol, interval, start_ms, end_ms):
        if interval not in self.HL_INTERVALS:
            raise CryptoError("Hyperliquid has no %s candles" % interval)

        def page(s, e):
            n = int((e - s) // INTERVAL_MS[interval]) + 1
            w = 20 + int(n // 60)
            rows = self.post({"type": "candleSnapshot",
                              "req": {"coin": symbol, "interval": interval, "startTime": s, "endTime": e}}, weight=w)
            return self.parse_bars(rows)
        return _paginate_forward(page, start_ms, end_ms, INTERVAL_MS[interval], self.max_bars)

    @staticmethod
    def parse_bars(rows):
        out = []
        try:
            for r in rows or []:
                o, c, v = float(r["o"]), float(r["c"]), float(r["v"])
                out.append((int(r["t"]), o, float(r["h"]), float(r["l"]), c, v, v * (o + c) / 2.0))
        except (TypeError, ValueError, KeyError):
            raise CryptoError("Hyperliquid sent a malformed candle; try again in a moment")
        out.sort(key=lambda b: b[0])
        return out

    def funding_history(self, symbol, start_ms, end_ms):
        out = []
        s = start_ms
        for _ in range(40):
            rows = self.post({"type": "fundingHistory", "coin": symbol, "startTime": s, "endTime": end_ms},
                             weight=45) or []          # 20, plus 1 for every 20 rows of a full 500-row page
            got = [(int(r["time"]), float(r["fundingRate"])) for r in rows]
            out.extend(got)
            if len(got) < 500:
                break
            s = got[-1][0] + 1
        return sorted(set(out))

    def orderbook(self, symbol, depth=20):
        r = self.post({"type": "l2Book", "coin": symbol}, weight=2) or {}
        lv = r.get("levels") or [[], []]
        return ([(float(x["px"]), float(x["sz"])) for x in lv[0]], [(float(x["px"]), float(x["sz"])) for x in lv[1]])

    def predicted_fundings(self):
        """Next funding on Hyperliquid, Binance and Bybit for every coin, in one request."""
        rows = self.post({"type": "predictedFundings"}) or []
        return self.parse_predicted(rows)

    @staticmethod
    def parse_predicted(rows):
        out = {}
        names = {"HlPerp": "hyperliquid", "BinPerp": "binance", "BybitPerp": "bybit"}
        for row in rows:
            try:
                coin, venues = row[0], row[1]
            except (TypeError, IndexError):
                continue
            d = {}
            for v in venues or []:
                try:
                    vname, info = v[0], v[1]
                except (TypeError, IndexError):
                    continue
                if not info:
                    continue
                hours = fnum(info.get("fundingIntervalHours"), 1.0 if vname == "HlPerp" else 8.0)
                rate = fnum(info.get("fundingRate"))
                if rate is None:
                    continue
                d[names.get(vname, vname)] = {"rate": rate, "hours": hours,
                                              "next_ms": int(info.get("nextFundingTime") or 0) or None}
            out[coin] = d
        return out

    def risk_tiers(self, symbol):
        m = self.market(symbol)
        table = m.get("margin_table")
        tiers = []
        if table and table.get("marginTiers"):
            bounds = table["marginTiers"]
            for i, t in enumerate(bounds):
                lev = fnum(t.get("maxLeverage"))
                upper = fnum(bounds[i + 1].get("lowerBound")) if i + 1 < len(bounds) else None
                if lev:
                    tiers.append({"max_notional": upper, "mmr": 1.0 / (2.0 * lev), "max_leverage": lev, "deduction": 0.0})
        elif m.get("max_leverage"):
            lev = m["max_leverage"]
            tiers.append({"max_notional": None, "mmr": 1.0 / (2.0 * lev), "max_leverage": lev, "deduction": 0.0})
        return tiers or None

    def user_state(self, address):
        return self.post({"type": "clearinghouseState", "user": address}, weight=2)


VENUES = {"binance": Binance, "bybit": Bybit, "hyperliquid": Hyperliquid}
AUTO_ORDER = ("binance", "bybit", "hyperliquid")


def get_venue(name=None, quiet=False):
    """A venue by name. 'auto' tries Binance, then Bybit, then Hyperliquid, and remembers
    which one answered from this location for six hours (crypto/venue.json)."""
    name = (name or load_config().get("venue") or "auto").lower()
    if name in VENUES:
        return VENUES[name]()
    if name != "auto":
        raise CryptoError("unknown venue %r. Use auto, binance, bybit or hyperliquid." % name)
    memo = read_json(wpath("cache", "venue.json"), {}) or {}
    offline = bool(os.environ.get("CRYPTO_OFFLINE"))
    if not offline and memo.get("venue") in VENUES and now_ms() - memo.get("at", 0) < 6 * 3600000:
        return VENUES[memo["venue"]]()
    notes = []
    for vn in AUTO_ORDER:
        v = VENUES[vn]()
        try:
            v.markets()
        except GeoBlocked as e:
            notes.append("%s: blocked here" % v.label)
            continue
        except NetError as e:
            notes.append("%s: %s" % (v.label, e))
            continue
        v.auto_notes = notes
        if not offline:
            try:
                write_json(wpath("cache", "venue.json"), {"venue": vn, "at": now_ms(), "skipped": notes})
            except OSError:
                pass
        return v
    raise NetError("no exchange answered. %s" % "; ".join(notes))


# ---------------------------------------------------------------- universe and resampling

DAY_MS = 86400000


def build_universe(venue, cfg=None, min_volume=None, top=None, listing_days=None, symbols=None):
    """Tradable perps worth scanning, busiest first: [(market, ticker)], plus why others were left out.

    Leaves out stablecoins, coins listed fewer than min_listing_days ago, coins under
    min_quote_volume_usd of 24h volume and anything in the config's exclude list.
    An explicit list of symbols skips the filters.
    """
    cfg = cfg or load_config()
    tk = venue.tickers()
    if symbols:
        kept = []
        for s in symbols:
            m = venue.market(s)
            t = tk.get(m["symbol"])
            if t:
                kept.append((m, t))
        return kept, {}
    min_volume = cfg.get("min_quote_volume_usd", 0) if min_volume is None else min_volume
    listing_days = cfg.get("min_listing_days", 0) if listing_days is None else listing_days
    excl = set(str(x).upper() for x in (cfg.get("exclude") or []))
    kept = []
    why = {"stablecoin": 0, "listed recently": 0, "low volume": 0, "excluded in config": 0, "no price": 0}
    for m in venue.markets():
        if m["coin"].upper() in STABLES:
            why["stablecoin"] += 1
            continue
        if m["symbol"].upper() in excl or m["coin"].upper() in excl:
            why["excluded in config"] += 1
            continue
        t = tk.get(m["symbol"])
        if not t:
            why["no price"] += 1
            continue
        if m.get("listed_ms") and listing_days and now_ms() - m["listed_ms"] < listing_days * DAY_MS:
            why["listed recently"] += 1
            continue
        if (t.get("quote_volume") or 0) < (min_volume or 0):
            why["low volume"] += 1
            continue
        kept.append((m, t))
    kept.sort(key=lambda x: -(x[1].get("quote_volume") or 0))
    top = venue.default_top if top is None else top
    if top:
        kept = kept[:top]
    return kept, dict((k, v) for k, v in why.items() if v)


def resample(bars, interval, complete=True):
    """Build longer bars from shorter ones (5m -> 15m). Drops partial groups by default."""
    step = interval_ms(interval)
    if not bars:
        return []
    src = None
    for i in range(1, len(bars)):
        d = bars[i][T] - bars[i - 1][T]
        if d > 0:
            src = d if src is None else min(src, d)
    need = (step // src) if src else 1
    out = []
    cur = None
    for b in bars:
        k = (b[T] // step) * step
        if cur is None or cur[0] != k:
            if cur is not None:
                out.append(cur)
            cur = [k, b[O], b[H], b[L], b[C], b[V], b[QV], 1]
        else:
            cur[2] = max(cur[2], b[H])
            cur[3] = min(cur[3], b[L])
            cur[4] = b[C]
            cur[5] += b[V]
            cur[6] += b[QV]
            cur[7] += 1
    if cur is not None:
        out.append(cur)
    return [tuple(x[:7]) for x in out if (not complete) or x[7] >= need]


# ---------------------------------------------------------------- analytics several skills share

FNG_URL = "https://api.alternative.me/fng/"


def fear_greed(limit=31):
    """The Crypto Fear & Greed Index (alternative.me), newest first: [(ms, value, label)]."""
    r = HTTP.request("GET", FNG_URL, params={"limit": limit})
    rows = []
    for d in (r or {}).get("data") or []:
        try:
            rows.append((int(d["timestamp"]) * 1000, int(d["value"]), d.get("value_classification") or ""))
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: -x[0])
    return rows


def funding_stats(venue, symbol, days=30):
    """Where today's funding sits against the last N days, in yearly terms."""
    t = venue.ticker(symbol)
    hours = t.get("funding_hours") or 8.0
    now = now_ms()
    hist = venue.funding_history(symbol, now - days * DAY_MS, now)
    rates = [r for _, r in hist]
    cur = t.get("funding")
    last7 = [r for ts, r in hist if ts >= now - 7 * DAY_MS]
    return {
        "rate": cur, "hours": hours, "apr": annualize_funding(cur, hours),
        "next_ms": t.get("next_funding_ms"),
        "mean": mean(rates), "mean_apr": annualize_funding(mean(rates), hours) if rates else None,
        "mean7": mean(last7), "mean7_apr": annualize_funding(mean(last7), hours) if last7 else None,
        "rank": pct_rank(cur, rates) if rates and cur is not None else None,
        "negative": sum(1 for r in rates if r < 0), "n": len(rates), "days": days,
    }


def oi_change(venue, symbol):
    """Open interest now and its change over 24 hours and 7 days, when the venue keeps history."""
    now_oi = venue.open_interest(symbol)
    out = {"oi_usd": now_oi, "ch24": None, "ch7d": None}
    if not venue.has_oi_history:
        return out
    hist = venue.oi_history(symbol, "1h", 170) or []
    if hist:
        last = hist[-1][1]
        ago24 = [v for ts, v in hist if ts <= hist[-1][0] - DAY_MS]
        ago7 = [v for ts, v in hist if ts <= hist[-1][0] - 7 * DAY_MS + 3600000]
        if ago24 and ago24[-1]:
            out["ch24"] = last / ago24[-1] - 1.0
        if ago7 and ago7[-1]:
            out["ch7d"] = last / ago7[-1] - 1.0
        out["series"] = [v for _, v in hist[-48:]]
    return out


def positioning(venue, symbol):
    """Share of accounts long now and a day ago; top traders and taker flow where the venue has them."""
    out = {"long_share": None, "long_share_24h": None, "top_long_share": None, "taker_ratio": None}
    if not venue.has_long_short:
        return out
    ls = venue.long_short(symbol, "1h", 25) or []
    if ls:
        out["long_share"] = ls[-1][1]
        out["long_share_24h"] = ls[0][1] if len(ls) >= 24 else None
    if hasattr(venue, "top_long_short"):
        try:
            tl = venue.top_long_short(symbol, "1h", 2) or []
            out["top_long_share"] = tl[-1][1] if tl else None
        except CryptoError:
            pass
    try:
        tr = venue.taker_ratio(symbol, "1h", 4) or []
        out["taker_ratio"] = mean([v for _, v in tr]) if tr else None
    except CryptoError:
        pass
    return out


CROWD_READ = {
    "crowded longs": "Longs pay to hold while price goes nowhere. A drop can cascade through their stops (squeeze-down risk).",
    "crowded shorts": "Shorts pay to hold while open interest grows. A push up can force them to buy (short-squeeze fuel).",
    "hot funding": "Holding a long costs a lot: longs pay shorts at every funding.",
    "deeply negative funding": "Holding a short costs a lot: shorts pay longs at every funding.",
}


def crowd_read(label, long_share=None):
    """CROWD_READ's words, plus whether the accounts agree with what the funding says."""
    words = CROWD_READ[label]
    if label in ("hot funding", "deeply negative funding") and long_share is not None:
        side, other = ("long", "short") if label == "hot funding" else ("short", "long")
        share = long_share if side == "long" else 1.0 - long_share
        if round(share * 100) == 50:
            words += " Accounts are split evenly between long and short."
        elif share > 0.5:
            words += " Most accounts are %s too (%d%%)." % (side, round(share * 100))
        else:
            words += " Yet most accounts are %s (%d%%), so the %ss are likely fewer and bigger." % (
                other, round((1.0 - share) * 100), side)
    return words
FLOW_READ = {
    "new longs": "price up, open interest up",
    "shorts adding into a rise": "price up, open interest up, funding negative",
    "short covering": "price up, open interest down",
    "new shorts": "price down, open interest up",
    "longs adding into a drop": "price down, open interest up, funding high",
    "long unwinding": "price down, open interest down",
}


def crowding(apr, oi24, long_share, change24):
    """A plain label for who is paying whom, or None."""
    if apr is None:
        return None
    if apr >= 0.30 and (oi24 or 0) >= 0.10 and (long_share is None or long_share >= 0.6) and abs(change24 or 0) < 0.05:
        return "crowded longs"
    if apr <= -0.10 and (oi24 or 0) >= 0.10 and (long_share is None or long_share <= 0.5):
        return "crowded shorts"
    if apr >= 0.60:
        return "hot funding"
    if apr <= -0.30:
        return "deeply negative funding"
    return None


def flow(change24, oi24, apr=None, price_min=0.015, oi_min=0.05):
    """Price against open interest over 24 hours, read with funding when it disagrees."""
    if change24 is None or oi24 is None:
        return None
    if abs(change24) < price_min or abs(oi24) < oi_min:
        return None
    if change24 > 0 and oi24 > 0:
        return "shorts adding into a rise" if (apr is not None and apr <= -0.10) else "new longs"
    if change24 > 0 and oi24 < 0:
        return "short covering"
    if change24 < 0 and oi24 > 0:
        return "longs adding into a drop" if (apr is not None and apr >= 0.30) else "new shorts"
    return "long unwinding"


def book_stats(bids, asks, band=0.0025):
    """Spread and resting size within +/- band of the mid. 'reach' says if the book we got goes that far."""
    if not bids or not asks:
        return None
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2.0
    lo, hi = mid * (1 - band), mid * (1 + band)
    bid_usd = sum(p * q for p, q in bids if p >= lo)
    ask_usd = sum(p * q for p, q in asks if p <= hi)
    return {"mid": mid, "spread": (ba - bb) / mid, "band": band, "bid_usd": bid_usd, "ask_usd": ask_usd,
            "reach": bids[-1][0] <= lo and asks[-1][0] >= hi}


def walk_book(levels, usd):
    """Average fill price for a market order of `usd` notional against one side of the book.
    Returns (avg_price, filled_usd); filled_usd < usd means the book we have ran out."""
    left = usd
    cost = qty = 0.0
    for p, q in levels:
        take = min(q, left / p)
        cost += take * p
        qty += take
        left -= take * p
        if left <= 1e-9:
            break
    return ((cost / qty) if qty else None), cost


def market_regime(venue, cfg=None, breadth_n=30):
    """Market weather from BTC's trend and volatility, breadth, average funding and Fear & Greed."""
    cfg = cfg or load_config()
    btc = venue.market("BTC")
    d = venue.bars(btc["symbol"], "1d", 120)
    h = venue.bars(btc["symbol"], "1h", 24 * 30 + 1)
    c = closes(d)
    e20s = ema(c, 20)
    e20, e50 = e20s[-1], ema(c, 50)[-1]
    price = venue.ticker(btc["symbol"])["last"]
    out = {"btc": price, "btc_7d": c[-1] / c[-8] - 1.0 if len(c) > 8 else None,
           "btc_30d": c[-1] / c[-31] - 1.0 if len(c) > 31 else None,
           "btc_ema20": e20, "btc_ema50": e50}
    trend = 0
    if e20 and e50:
        if price > e20 and e20 > e50:
            trend = 1
        elif price < e20 and e20 < e50:
            trend = -1
    elif e20 and len(e20s) > 6 and e20s[-6]:          # under 50 days of history: the 20-day average's own slope
        if price > e20 > e20s[-6]:
            trend = 1
        elif price < e20 < e20s[-6]:
            trend = -1
    out["trend"] = trend
    hc = closes(h)
    out["vol7"] = realized_vol(hc[-169:], 8760)
    out["vol30"] = realized_vol(hc, 8760)
    uni, _ = build_universe(venue, cfg, top=breadth_n)
    tk = venue.tickers()

    def above(item):
        m = item[0]
        dd = venue.bars(m["symbol"], "1d", 30)
        cc_ = closes(dd)
        e = ema(cc_, 20)[-1]
        return None if e is None else (tk[m["symbol"]]["last"] > e)
    res = parallel(above, uni, workers=2 if venue.name == "hyperliquid" else 8)
    flags = [r for _, r, e in res if e is None and r is not None]
    out["breadth"] = (sum(1 for f in flags if f) / float(len(flags))) if flags else None
    out["breadth_n"] = len(flags)
    aprs = [annualize_funding(t["funding"], t.get("funding_hours")) for m, t in uni[:20] if t.get("funding") is not None]
    out["funding_apr"] = median(aprs)          # one coin at -1,800% a year must not set the market's mood
    out["funding_n"] = len(aprs)
    try:
        fg = fear_greed(8)
        out["fng"] = fg[0][1] if fg else None
        out["fng_label"] = fg[0][2] if fg else None
        out["fng_7d"] = fg[7][1] if len(fg) > 7 else None
    except CryptoError as e:
        out["fng"] = out["fng_label"] = out["fng_7d"] = None
        out["fng_error"] = str(e)
    hot = 0
    cold = 0
    if (out["funding_apr"] or 0) >= 0.30:
        hot += 1
    if out.get("fng") is not None and out["fng"] >= 75:
        hot += 1
    if (out["breadth"] or 0) >= 0.75:
        hot += 1
    if out["funding_apr"] is not None and out["funding_apr"] <= 0.0:
        cold += 1
    if out.get("fng") is not None and out["fng"] <= 25:
        cold += 1
    if out["breadth"] is not None and out["breadth"] <= 0.25:
        cold += 1
    score = trend + (1 if (out["breadth"] or 0.5) >= 0.6 else -1 if (out["breadth"] or 0.5) <= 0.4 else 0)
    if hot >= 2:
        label, read = "Overheated", "Crowded and greedy. Longs pay a lot to hold; sharp flushes are more likely."
    elif cold >= 2 and trend <= 0:
        label, read = "Fearful", "Selling is broad and funding is cheap or negative. Bounces are fast and short-lived."
    elif score >= 2:
        label, read = "Risk-on", "BTC trends up and most coins follow. Long breakouts have the wind behind them."
    elif score <= -2:
        label, read = "Risk-off", "BTC trends down and most coins follow. Long setups fight the tide."
    else:
        label, read = "Mixed", "No clear tide. Setups stand on their own; size down and be picky."
    out["label"], out["read"] = label, read
    return out


# ---------------------------------------------------------------- trading maths

STEP5 = 300000
HOUR_MS = 3600000
SIDES = ("long", "short")


def parse_hold(text, default_hours=None):
    """'24h', '90m', '2d', '8' (hours) -> hours as a float."""
    if text is None or text == "":
        return default_hours
    s = str(text).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([mhd]?)$", s)
    if not m:
        raise CryptoError("could not read the hold time %r. Use 90m, 8h or 2d." % text)
    v = float(m.group(1))
    unit = m.group(2) or "h"
    hours = v / 60.0 if unit == "m" else v * 24.0 if unit == "d" else v
    if hours <= 0 or hours > 24 * 14:
        raise CryptoError("a hold must be between a few minutes and 14 days")
    return hours


DEFAULT_SLIPPAGE = 0.0003      # when the book cannot be read: 3 basis points a side
MIN_SLIPPAGE = 0.00005         # never assume less than half a basis point


def cost_rates(venue, cfg=None):
    """Fees and slippage as fractions: taker, maker, slippage. Slippage is None when the
    config leaves it to be measured from the order book (see measure_slippage)."""
    cfg = cfg or load_config()
    tb, mb = cfg.get("taker_fee_bps"), cfg.get("maker_fee_bps")
    taker = (tb / 1e4) if tb is not None else venue.taker_fee
    maker = (mb / 1e4) if mb is not None else venue.maker_fee
    sb = cfg.get("slippage_bps")
    return {"taker": taker, "maker": maker, "slippage": (sb / 1e4) if sb is not None else None,
            "slippage_source": "your config" if sb is not None else None}


SLIPPAGE_MEMO_S = 300


def measure_slippage(venue, symbol, notional=10000.0):
    """Expected cost of crossing the book with a market order: half the spread plus the impact
    of `notional` USD, as a fraction of the mid. (fraction, words). One venue object reads each
    book at most once in 5 minutes (the scanner and the Scout's plans share the reading)."""
    memo = getattr(venue, "__dict__", {}).setdefault("_slippage_memo", {})
    got = memo.get((symbol, notional))
    if got and time.time() - got[0] < SLIPPAGE_MEMO_S:
        return got[1]
    res = _measure_slippage(venue, symbol, notional)
    memo[(symbol, notional)] = (time.time(), res)
    return res


def _measure_slippage(venue, symbol, notional):
    try:
        bids, asks = venue.orderbook(symbol, 100 if venue.name != "hyperliquid" else 20)
    except CryptoError:
        return DEFAULT_SLIPPAGE, "3 bps (the order book could not be read)"
    if not bids or not asks:
        return DEFAULT_SLIPPAGE, "3 bps (empty order book)"
    mid = (bids[0][0] + asks[0][0]) / 2.0
    buy, got_b = walk_book(asks, notional)
    sell, got_s = walk_book(bids, notional)
    if not buy or not sell or got_b < notional * 0.999 or got_s < notional * 0.999:
        return max(DEFAULT_SLIPPAGE, (asks[0][0] - bids[0][0]) / mid), "the book is thin: %s of depth seen" % fmt_usd(min(got_b, got_s))
    s = max((buy / mid - 1.0 + 1.0 - sell / mid) / 2.0, MIN_SLIPPAGE)
    return s, "%.1f bps, measured from the order book for a %s order" % (s * 1e4, fmt_usd(notional))


def plan_costs(plan, venue=None, cfg=None):
    """The costs a plan was made with (every later skill uses the same numbers)."""
    pc = (plan or {}).get("costs") or {}
    if pc.get("taker") is not None and pc.get("slippage") is not None:
        return {"taker": pc["taker"], "maker": pc.get("maker", pc["taker"]), "slippage": pc["slippage"]}
    c = cost_rates(venue, cfg) if venue is not None else {"taker": 0.0005, "maker": 0.0002, "slippage": None}
    if c["slippage"] is None:
        c["slippage"] = DEFAULT_SLIPPAGE
    return {"taker": c["taker"], "maker": c["maker"], "slippage": c["slippage"]}


def plan_id(plan):
    """A short hash naming one exact plan, so a Time Machine run is never matched to another plan."""
    keys = ("created_at", "venue", "symbol", "side", "entry", "stop", "target", "entry_time", "exit_by")
    canon = json.dumps([plan.get(k) for k in keys], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]


def funding_times(start_ms, end_ms, hours):
    """UTC-aligned funding times F with start < F <= end."""
    step = int(round((hours or 8.0) * HOUR_MS))
    f = (start_ms // step + 1) * step
    out = []
    while f <= end_ms:
        out.append(f)
        f += step
    return out


def side_sign(side):
    if side not in SIDES:
        raise CryptoError("side must be long or short, not %r" % side)
    return 1.0 if side == "long" else -1.0


def net_r(side, entry, exit_px, outcome, risk, costs, funding_paid=0.0):
    """One trade's result in R after costs.

    outcome 'target' exits with a resting limit (maker fee, no slippage); 'stop' and
    'open' (out at the time limit) exit at market (taker fee plus slippage). The entry
    is a market order. funding_paid is per coin, positive when this side paid it.
    """
    sg = 1.0 if side == "long" else -1.0
    gross = sg * (exit_px - entry) / risk
    c_in = (costs["taker"] + costs["slippage"]) * entry / risk
    if outcome == "target":
        c_out = costs["maker"] * exit_px / risk
    else:
        c_out = (costs["taker"] + costs["slippage"]) * exit_px / risk
    return gross - c_in - c_out - funding_paid / risk


def breakeven_share(target_r, entry, stop, target, risk, costs):
    """Share of decided trades the target must win to break even after fees and slippage."""
    c_in = (costs["taker"] + costs["slippage"]) * entry / risk
    win = target_r - c_in - costs["maker"] * target / risk
    loss = 1.0 + c_in + (costs["taker"] + costs["slippage"]) * stop / risk
    if win <= 0:
        return 1.0
    return loss / (win + loss)


def stop_cost_r(entry, stop, costs):
    """What fees and slippage take from a trade that ends at its stop, in R: a market entry and a market exit."""
    risk = abs(entry - stop)
    if not risk:
        return float("inf")
    return (costs["taker"] + costs["slippage"]) * (entry + stop) / risk


def cost_limit(cfg=None):
    """The cost rule's limit in R (max_cost_r), or None when it is off."""
    cfg = cfg or load_config()
    try:
        v = float(cfg.get("max_cost_r"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def over_cost_limit(cost_r, limit):
    """Judged as shown, to two decimals: 0.204R prints as 0.20R and is not over a 0.2R limit."""
    return limit is not None and round(cost_r, 2) > limit + 1e-9


# ---------------------------------------------------------------- the backtest's track record

TRACK_FILE = "track_record.json"
TRACK_WORDS = {"orb-utc": "UTC-day opening-range breakout", "orb-ny": "New York opening-range breakout",
               "breakout": "range breakout", "squeeze": "squeeze break", "trend": "trend pullback",
               "reversal": "RSI reversal", "volume": "volume spike"}
_TRACK = {}


def track_record():
    """The walk-forward backtest's results by setup and side: your own run's crypto/track_record.json when you
    have put one there, otherwise the copy that ships with the pack (crypto-setup/track_record.json)."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), TRACK_FILE)
    for path in (wpath(TRACK_FILE), here):
        try:
            key = (os.path.abspath(path), os.path.getmtime(path))
        except OSError:
            continue
        if key not in _TRACK:
            try:
                d = read_json(path, None)
            except CryptoError:
                d = None
            _TRACK[key] = d if (isinstance(d, dict) and d.get("setups")) else None
        if _TRACK[key]:
            return _TRACK[key]
    return None


def track_key(setup, session=None):
    """The backtest's name for a setup: the opening range is tested at the UTC day and New York opens only."""
    if setup == "orb":
        return "orb-" + (session or "utc")
    return setup


def track_entry(setup, side, session=None):
    tr = track_record()
    if not tr or side not in SIDES:
        return None
    return (tr.get("setups") or {}).get("%s|%s" % (track_key(setup, session), side))


def track_source(tr=None):
    """Where the track record comes from, in a few words: "Binance, 40 coins, 29 Jul to 27 Oct 2026"."""
    tr = tr or track_record()
    if not tr:
        return ""
    a, b = utc_dt(tr["from"]), utc_dt(tr["to"])
    span = "%d %s to %d %s %d" % (a.day, a.strftime("%b"), b.day, b.strftime("%b"), b.year)
    return "%s, %d coins, %s" % ((tr.get("venue_label") or tr.get("venue") or "").split(" ")[0], tr.get("coins") or 0, span)


def zero_note(e):
    """' (can't be told from zero)' when the 90% range of an average spans zero."""
    lo, hi = e.get("lo"), e.get("hi")
    return " (can't be told from zero)" if (lo is not None and hi is not None and lo < 0 < hi) else ""


def track_parts(setup, side, session=None):
    """('-0.10R a trade after costs over 3,393 trades', '+0.02R over 742 (can't be told from zero)'), the second
    None when nothing was judged; or None when the backtest has no record of this setup and side."""
    e = track_entry(setup, side, session)
    if not e or not e.get("n"):
        return None
    raw = "%+.2fR a trade after costs over %s trade%s%s" % (e["avg_r"], "{:,}".format(e["n"]), "" if e["n"] == 1 else "s",
                                                             zero_note(e))
    c = e.get("checked")
    if not c:
        return raw, None
    if not c.get("n"):
        return raw, "none passed"
    return raw, "%+.2fR over %s%s" % (c["avg_r"], "{:,}".format(c["n"]), zero_note(c))


def track_words(setup, side, session=None):
    """'-0.10R a trade after costs over 3,393 trades; with both checks +0.02R over 742', or None."""
    parts = track_parts(setup, side, session)
    if not parts:
        return None
    return parts[0] + (("; with both checks " + parts[1]) if parts[1] else "")


# Maintenance margin. Bybit and Hyperliquid publish theirs; Binance's brackets need a key,
# so the pack uses its tier-1 rates for BTC and ETH and a cautious 2.5% for everything else.
BINANCE_MMR = {"BTC": 0.004, "ETH": 0.005}
BINANCE_MMR_DEFAULT = 0.025


def mmr_for(venue, market, notional):
    """(maintenance margin rate, deduction in USD, where the number came from)."""
    tiers = None
    try:
        cache = getattr(venue, "_tier_cache", None)
        if cache is None:
            cache = venue._tier_cache = {}
        if market["symbol"] not in cache:
            cache[market["symbol"]] = venue.risk_tiers(market["symbol"])
        tiers = cache[market["symbol"]]
    except CryptoError:
        tiers = None
    if tiers:
        for t in tiers:
            if t.get("max_notional") is None or notional <= t["max_notional"]:
                return t["mmr"], t.get("deduction") or 0.0, "%s risk tiers" % venue.label.split(" ")[0]
        t = tiers[-1]
        return t["mmr"], t.get("deduction") or 0.0, "%s risk tiers" % venue.label.split(" ")[0]
    if venue.name == "binance":
        return BINANCE_MMR.get(market["coin"], BINANCE_MMR_DEFAULT), 0.0, "Binance tier-1 estimate"
    return 0.02, 0.0, "a cautious 2% estimate"


def liq_price(entry, side, leverage, mmr, fee=0.0, deduction=0.0, qty=None):
    """Isolated-margin liquidation price of a linear USDT perpetual.

    Liquidation comes when the position's margin plus its loss falls to the maintenance
    margin plus the fee to close it:
        long : P = (E (1 - 1/L) - d/q) / (1 - mmr - fee)
        short: P = (E (1 + 1/L) + d/q) / (1 + mmr + fee)
    d is the tier's maintenance deduction and q the size in coins.
    """
    if leverage <= 0:
        raise CryptoError("leverage must be positive")
    ded = (deduction / qty) if (deduction and qty) else 0.0
    if side == "long":
        p = (entry * (1.0 - 1.0 / leverage) - ded) / (1.0 - mmr - fee)
        return min(max(p, 0.0), entry)          # at or above the entry: the margin is under maintenance at once
    p = (entry * (1.0 + 1.0 / leverage) + ded) / (1.0 + mmr + fee)
    return max(p, entry)


def instant_liquidation(leverage, mmr, fee=0.0):
    """True when the initial margin (1/L) does not even cover the maintenance margin plus the closing fee."""
    return 1.0 / leverage <= mmr + fee


def max_safe_leverage(entry, stop, side, mmr, fee, buffer, cap=125):
    """The highest whole leverage whose liquidation sits at least `buffer` times the stop distance away."""
    stop_dist = abs(entry - stop) / entry
    best = 0
    for lev in range(1, cap + 1):
        lp = liq_price(entry, side, lev, mmr, fee)
        dist = abs(entry - lp) / entry if lp > 0 else 1.0
        if dist >= buffer * stop_dist:
            best = lev
        else:
            break
    return best


def floor_step(qty, step):
    if not step:
        return qty
    n = math.floor(qty / step + 1e-9)
    d = tick_decimals(step)
    return round(n * step, d)


def day_start(at_ms=None, tzname=None):
    """Start of the user's day (in display_tz) containing at_ms, as UTC ms."""
    at_ms = now_ms() if at_ms is None else at_ms
    tzname = tzname if tzname is not None else load_config().get("display_tz", "UTC")
    off = _tz_offset_minutes(at_ms, tzname) * 60000
    return ((at_ms + off) // DAY_MS) * DAY_MS - off


def read_positions():
    d = read_json(wpath("positions.json"), None) or {}
    return d.get("positions") or []


def read_ledger():
    return read_jsonl(wpath("ledger.jsonl"))


def today_stats(cfg=None, at_ms=None):
    """What the risk rules need from the trade log: today's entries, today's net result,
    open positions and the risk still on them, and the latest closed trades."""
    cfg = cfg or load_config()
    at_ms = now_ms() if at_ms is None else at_ms
    start = day_start(at_ms, cfg.get("display_tz", "UTC"))
    led = read_ledger()
    entries = [e for e in led if e.get("type") == "open" and start <= e.get("at", 0) <= at_ms]
    closes_ = [e for e in led if e.get("type") == "close"]
    today_closes = [e for e in closes_ if start <= e.get("at", 0) <= at_ms]
    realized = sum(fnum(e.get("pnl_usd"), 0.0) for e in today_closes)
    pos = read_positions()
    open_risk = sum(fnum(p.get("loss_at_stop_usd"), fnum(p.get("margin"), 0.0)) for p in pos)
    closes_.sort(key=lambda e: e.get("at", 0))
    return {"day_start": start, "entries": len(entries), "realized": realized, "open": pos,
            "open_risk": open_risk, "open_margin": sum(fnum(p.get("margin"), 0.0) for p in pos),
            "last_closes": closes_[-10:]}


def fingerprint(order):
    """A short hash of the fields that make an order what it is. crypto-broker matches on it."""
    keys = ("venue", "symbol", "side", "qty", "type", "limit", "stop", "target", "leverage", "margin_mode", "reduce_only")
    canon = json.dumps([order.get(k) for k in keys], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- indicators

def closes(bars):
    return [b[C] for b in bars]


def sma(xs, n):
    out = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs, n):
    out = [None] * len(xs)
    if len(xs) < n or n < 1:
        return out
    k = 2.0 / (n + 1)
    e = sum(xs[:n]) / float(n)
    out[n - 1] = e
    for i in range(n, len(xs)):
        e = xs[i] * k + e * (1.0 - k)
        out[i] = e
    return out


def rsi(xs, n=14):
    out = [None] * len(xs)
    if len(xs) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = xs[i] - xs[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def true_ranges(bars):
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b[H] - b[L])
        else:
            pc = bars[i - 1][C]
            tr.append(max(b[H] - b[L], abs(b[H] - pc), abs(b[L] - pc)))
    return tr


def atr(bars, n=14):
    tr = true_ranges(bars)
    out = [None] * len(bars)
    if len(tr) < n:
        return out
    a = sum(tr[:n]) / float(n)
    out[n - 1] = a
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def bollinger(xs, n=20, k=2.0):
    """(mid, upper, lower, width) lists; width = (upper - lower) / mid."""
    mid = sma(xs, n)
    up, lo, wd = [None] * len(xs), [None] * len(xs), [None] * len(xs)
    for i in range(n - 1, len(xs)):
        w = xs[i - n + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        up[i], lo[i] = m + k * sd, m - k * sd
        wd[i] = (up[i] - lo[i]) / m if m else None
    return mid, up, lo, wd


def vwap_from(bars, start_ms):
    """Volume-weighted average price of bars opening at or after start_ms."""
    pv = vv = 0.0
    for b in bars:
        if b[T] >= start_ms:
            tp = (b[H] + b[L] + b[C]) / 3.0
            pv += tp * b[V]
            vv += b[V]
    return pv / vv if vv > 0 else None


def log_returns(xs):
    return [math.log(xs[i] / xs[i - 1]) for i in range(1, len(xs)) if xs[i - 1] > 0 and xs[i] > 0]


def realized_vol(xs, per_year):
    r = log_returns(xs)
    if len(r) < 3:
        return None
    return stdev(r) * math.sqrt(per_year)


def pivots(bars, left=3, right=3):
    """Swing highs and lows: [(index, price, 'H'|'L')]."""
    out = []
    for i in range(left, len(bars) - right):
        h, lo = bars[i][H], bars[i][L]
        if all(h > bars[j][H] for j in range(i - left, i)) and all(h >= bars[j][H] for j in range(i + 1, i + right + 1)):
            out.append((i, h, "H"))
        if all(lo < bars[j][L] for j in range(i - left, i)) and all(lo <= bars[j][L] for j in range(i + 1, i + right + 1)):
            out.append((i, lo, "L"))
    return out


# ---------------------------------------------------------------- statistics

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / float(len(xs)) if xs else None


def stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / float(len(xs))
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def percentile(xs, q):
    """q in [0, 100], linear interpolation."""
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def pct_rank(x, xs):
    xs = [v for v in xs if v is not None]
    if not xs or x is None:
        return None
    return sum(1 for v in xs if v <= x) / float(len(xs))


def zscore(x, xs):
    m, s = mean(xs), stdev(xs)
    if x is None or m is None or not s:
        return None
    return (x - m) / s


def wilson(k, n, z=1.96):
    """Win rate k/n with a 95% Wilson interval: (p, low, high)."""
    if n <= 0:
        return (None, 0.0, 1.0)
    p = k / float(n)
    den = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / den
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / den
    return (p, max(0.0, centre - half), min(1.0, centre + half))


def corr(a, b):
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def beta(a, b):
    """Beta of returns a against returns b."""
    n = min(len(a), len(b))
    if n < 3:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    vb = sum((y - mb) ** 2 for y in b)
    if vb <= 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / vb


# ---------------------------------------------------------------- formatting

def tick_decimals(tick):
    s = ("%.12f" % tick).rstrip("0").rstrip(".")
    return len(s.split(".")[1]) if "." in s else 0


def decimals_for(price, tick=None):
    if tick:
        return min(tick_decimals(tick), 12)
    p = abs(price or 0)
    if p >= 1000:
        return 1
    if p >= 100:
        return 2
    if p >= 1:
        return 3
    if p == 0:
        return 2
    return min(10, 3 - int(math.floor(math.log10(p))))   # four significant figures


def fmt_price(p, tick=None):
    if p is None:
        return "-"
    d = decimals_for(p, tick)
    return "{:,.{d}f}".format(p, d=d)


def round_to_tick(p, tick, mode="nearest"):
    if not tick or p is None:
        return p
    q = p / tick
    if mode == "down":
        n = math.floor(q + 1e-9)
    elif mode == "up":
        n = math.ceil(q - 1e-9)
    else:
        n = round(q)
    d = decimals_for(p, tick)
    return round(n * tick, d)


def fmt_pct(x, dp=2, plus=True):
    if x is None:
        return "-"
    s = "{:{p}.{d}f}%".format(x * 100.0, p="+" if plus else "", d=dp)
    return s


def fmt_usd(x, dp=1):
    if x is None:
        return "-"
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e9:
        return "%s$%.*fB" % (sign, dp + 1, a / 1e9)
    if a >= 1e6:
        return "%s$%.*fM" % (sign, dp, a / 1e6)
    if a >= 1e3:
        return "%s$%.*fK" % (sign, dp, a / 1e3)
    return "%s$%.2f" % (sign, a)


def fmt_money(x):
    """$1,408.22 (exact dollars for order tickets)."""
    if x is None:
        return "-"
    return ("-$" if x < 0 else "$") + "{:,.2f}".format(abs(x))


def fmt_liq(liq, ref, tick=None):
    """'2.4512 (17.3% away)', or 'none above zero' (a 1x long is never liquidated above zero)."""
    if liq is None:
        return "-"
    if liq <= 0:
        return "none above zero"
    return "%s (%s away)" % (fmt_price(liq, tick), fmt_pct(abs(ref - liq) / ref, 1, False))


def fmt_qty(q, step=None):
    d = tick_decimals(step) if step else (0 if q >= 1000 else 2 if q >= 1 else 6)
    return "{:,.{d}f}".format(q, d=d)


def fmt_small_pct(x):
    """Spreads and price impact: more decimals when tiny."""
    if x is None:
        return "-"
    a = abs(x)
    if a < 0.00001:
        return "<0.001%"
    if a < 0.0001:
        return "%.4f%%" % (x * 100)
    return "%.3f%%" % (x * 100)


def fmt_funding(rate, hours=8.0):
    """'+0.0100%/8h (+10.9% a year)'."""
    if rate is None:
        return "-"
    per_year = rate * (24.0 / (hours or 8.0)) * 365.0
    return "%+.4f%%/%gh (%+.1f%% a year)" % (rate * 100.0, hours or 8.0, per_year * 100.0)


def annualize_funding(rate, hours=8.0):
    if rate is None:
        return None
    return rate * (24.0 / (hours or 8.0)) * 365.0


SPARKS = u"▁▂▃▄▅▆▇█"


def spark(xs, width=24):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return ""
    if len(xs) > width:
        step = len(xs) / float(width)
        xs = [xs[int(i * step)] for i in range(width - 1)] + [xs[-1]]
    lo, hi = min(xs), max(xs)
    if hi == lo:
        return SPARKS[3] * len(xs)
    return "".join(SPARKS[int((x - lo) / (hi - lo) * (len(SPARKS) - 1))] for x in xs)


def table(rows, headers, align=None):
    """Monospace table. align: string of 'l'/'r' per column."""
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    headers = [str(h) for h in headers]
    n = len(headers)
    align = align or ("l" + "r" * (n - 1))
    w = [len(h) for h in headers]
    for r in rows:
        for i in range(n):
            w[i] = max(w[i], len(r[i]) if i < len(r) else 0)

    def line(cells):
        out = []
        for i in range(n):
            c = cells[i] if i < len(cells) else ""
            out.append(c.ljust(w[i]) if align[i] == "l" else c.rjust(w[i]))
        return "  ".join(out).rstrip()
    return "\n".join([line(headers)] + [line(r) for r in rows])


def out(text=""):
    """Print, safely, on consoles that cannot show every character."""
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(enc, "replace").decode(enc) + "\n")


# ---------------------------------------------------------------- command-line plumbing

def add_common(p, venue=True):
    if venue:
        p.add_argument("--venue", default=None, help="auto (default), binance, bybit or hyperliquid")
    p.add_argument("--now", default=None, help='pin the clock, "YYYY-MM-DD HH:MM" in UTC, to repeat a run')
    p.add_argument("--offline", default=None, metavar="DIR", help="answer only from responses saved in DIR")
    p.add_argument("--record", default=None, metavar="DIR", help="save every response to DIR")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument("--no-write", action="store_true", help="do not write files in crypto/")
    p.add_argument("--stats", action="store_true", help="print request count and timings at the end")
    return p


def apply_common(args):
    if getattr(args, "offline", None):
        os.environ["CRYPTO_OFFLINE"] = args.offline
    if getattr(args, "record", None):
        os.environ["CRYPTO_RECORD"] = args.record
    reset_http()
    set_now(getattr(args, "now", None) or os.environ.get("CRYPTO_NOW") or None)


def run(main):
    """Call main(); turn a CryptoError into one plain line and exit code 1."""
    try:
        code = main()
    except CryptoError as e:
        sys.stderr.write("Problem: %s\n" % e)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Stopped.\n")
        return 130
    except BrokenPipeError:            # the reader (head, a pager) closed early: not an error
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    return 0 if code is None else code


def dump(obj):
    out(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
