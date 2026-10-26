"""Test helpers: load a skill's script (the folder names have hyphens), run it against the fake exchange."""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
FAKE = os.path.join(ROOT, "tests", "fake_exchange.py")
sys.path.insert(0, os.path.join(ROOT, "crypto-setup"))

import cryptocore as cc  # noqa: E402

WED = "2026-10-21 15:15"      # a Wednesday, 1h45m after the New York open (13:30 UTC in October)
_MODS = {}


def load(folder, name):
    key = (folder, name)
    if key not in _MODS:
        path = os.path.join(ROOT, folder, name + ".py")
        spec = importlib.util.spec_from_file_location("crypto_test_" + name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MODS[key] = mod
    return _MODS[key]


def real(name):
    return cc.read_json(os.path.join(FIX, "real", name))["response"]


class Session(object):
    """A clean environment: the fake exchange, a pinned clock, an empty crypto/ folder."""

    KEYS = ("CRYPTO_FAKE_EXCHANGE", "CRYPTO_DIR", "CRYPTO_NOW", "FAKE_NOW", "FAKE_EXCHANGE_MODE", "FAKE_SESSION",
            "CRYPTO_OFFLINE", "CRYPTO_RECORD", "CRYPTO_CONFIG", "CRYPTO_NO_PACE")
    count = 0

    def __init__(self, now=WED, mode="", fake=True, config=None):
        self.now, self.mode, self.fake, self.config = now, mode, fake, config

    def __enter__(self):
        self.saved = dict((k, os.environ.get(k)) for k in self.KEYS)
        for k in self.KEYS:
            os.environ.pop(k, None)
        self.dir = tempfile.mkdtemp(prefix="crypto-test-")
        Session.count += 1
        os.environ["FAKE_SESSION"] = str(Session.count)     # failure modes start fresh in every session
        os.environ["CRYPTO_DIR"] = self.dir
        os.environ["CRYPTO_NO_PACE"] = "1"
        if self.fake:
            os.environ["CRYPTO_FAKE_EXCHANGE"] = FAKE
        if self.now:
            os.environ["CRYPTO_NOW"] = self.now
            os.environ["FAKE_NOW"] = self.now
        if self.mode:
            os.environ["FAKE_EXCHANGE_MODE"] = self.mode
        if self.config is not None:
            cc.write_json(os.path.join(self.dir, "config.json"), self.config)
        cc.reset_http()
        cc.set_now(self.now)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        cc.set_now(None)
        cc.reset_http()
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, *names):
        return os.path.join(self.dir, *names)

    def run(self, folder, name, argv):
        """(exit code, stdout, stderr) of a skill's main(argv)."""
        mod = load(folder, name)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cc.run(lambda: mod.main(list(argv)))
        return code, out.getvalue(), err.getvalue()
