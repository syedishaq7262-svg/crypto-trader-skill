"""The repo itself: every skill folder is complete, safe, Python 3.8 clean and written plainly."""

import ast
import json
import os
import re
import unittest

from _load import ROOT

SKILLS = sorted(d for d in os.listdir(ROOT) if d.startswith("crypto-") and os.path.isdir(os.path.join(ROOT, d)))
NOT_ON_THE_ORDER_PATH = {"crypto-setup"}


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def text_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".txt", ".yml", ".yaml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_every_skill_folder_has_a_skill_and_a_tool(self):
        self.assertGreaterEqual(len(SKILLS), 4)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            self.assertTrue(os.path.isfile(os.path.join(folder, "SKILL.md")), s)
            self.assertTrue([f for f in os.listdir(folder) if f.endswith(".py")], s)

    def test_frontmatter(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 300, "%s description is too thin" % s)
            self.assertIn("${CLAUDE_SKILL_DIR}", text, s)

    def test_order_path_skills_carry_the_safety_block(self):
        for s in SKILLS:
            if s in NOT_ON_THE_ORDER_PATH:
                continue
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("typed an explicit yes", low, s)
            self.assertIn("never ask for keys in the chat", low, s)

    def test_every_tool_finds_the_shared_core(self):
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                if f.endswith(".py") and f != "cryptocore.py":
                    with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                        src = fh.read()
                    self.assertIn("import cryptocore as cc", src, "%s/%s" % (s, f))

    def test_python_3_8_syntax_and_modules(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                ast.parse(src, filename=f, feature_version=(3, 8))
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix("):
                    self.assertNotIn(bad, src, "%s/%s" % (s, f))
                self.assertNotRegex(src, r"(?<![\w.])(list|dict|tuple|set)\[[A-Za-z]", "%s/%s" % (s, f))

    def test_standard_library_only(self):
        import sys
        allowed = set(getattr(sys, "stdlib_module_names", ())) | set((
            "argparse ast base64 bisect collections concurrent contextlib datetime email gzip hashlib hmac html importlib io json "
            "math os platform random re shutil socket ssl statistics sys tempfile threading time urllib uuid xml zipfile").split())
        allowed.add("cryptocore")
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
                for node in ast.walk(tree):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name.split(".")[0] for a in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        names = [node.module.split(".")[0]]
                    for n in names:
                        self.assertIn(n, allowed, "%s/%s imports %s" % (s, f, n))

    def test_no_em_dashes(self):
        for path in text_files():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                self.assertNotIn(chr(0x2014), fh.read(), rel)

    def test_no_invisible_characters(self):
        bad = (chr(0xFEFF), chr(0x200B), chr(0x200C), chr(0x200D), chr(0x2060), chr(0x00A0))
        for path in text_files():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for ch in bad:
                self.assertNotIn(ch, text, "%s has an invisible character U+%04X" % (rel, ord(ch)))

    def test_safe_defaults_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "crypto-setup", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg["require_stop"], True)
        self.assertEqual(cfg["margin_mode"], "isolated")
        self.assertIs(cfg["allow_cross_margin"], False)
        self.assertLessEqual(cfg["max_leverage"], 3)
        for k in ("max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day", "min_reward_risk",
                  "min_liq_buffer", "cooldown_after_losses"):
            self.assertIn(k, cfg)

    def test_example_config_matches_the_core_defaults(self):
        import cryptocore as cc
        with open(os.path.join(ROOT, "crypto-setup", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k, v in cfg.items():
            if not k.startswith("_"):
                self.assertIn(k, cc.DEFAULTS, k)
                self.assertEqual(cc.DEFAULTS[k], v, k)


if __name__ == "__main__":
    unittest.main()
