import json
import os
import re
import unittest

from _load import ROOT

# v3, the India edition: the thirteen folders
SKILLS = ["trader-kite", "trader-scanner", "trader-news", "trader-entry", "trader-exit",
          "trader-timemachine", "trader-risk", "trader-budget", "trader-broker", "trader-alerts",
          "trader-insiders", "trader-results", "trader-auto"]
ABOUT = ("13 Claude skills for day trading NSE stocks through Zerodha: scan every chart, track the news, "
         "time every entry and exit, replay every outcome before you risk a rupee, and run the whole loop "
         "unattended inside hard limits.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-kite", "trader-scanner", "trader-entry", "trader-exit", "trader-timemachine",
              "trader-budget", "trader-insiders", "trader-risk", "trader-broker", "trader-alerts",
              "trader-auto"]
V1_ONLY = ["trader-copycat", "trader-screener", "trader-tax", "trader-rebalance"]
# the US edition's sources and words: none may survive outside the tests and the README's history note
US_ONLY = ["finance.yahoo.com", "sec.gov", "nasdaqtrader", "alpaca", "disclosures-clerk.house.gov",
           "efdsearch.senate.gov", "stooq.com", "America/New_York", "pattern-day-trader", "trader-pelosi",
           "trader-analyst"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_thirteen_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_holds_only_the_pack(self):
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git") and d != "__pycache__")
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "tests"] + SKILLS))

    def test_secrets_and_state_are_ignored_by_git(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            lines = [l.strip() for l in fh.read().splitlines()]
        for must in (".env", "trader/"):
            self.assertIn(must, lines, must)

    def test_no_us_source_survives_outside_the_tests(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                low = fh.read().lower()
            for old in US_ONLY:
                self.assertNotIn(old.lower(), low, "%s still mentions %s" % (rel, old))

    def test_shared_code_is_imported_not_copied(self):
        # every skill script reaches kite.py / nse.py through trader-kite, never through a copy
        for s in SKILLS:
            if s == "trader-kite":
                continue
            for f in os.listdir(os.path.join(ROOT, s)):
                self.assertNotIn(f, ("kite.py", "nse.py"), "%s carries a copy of %s" % (s, f))
                if f.endswith(".py"):
                    with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                        src = fh.read()
                    if "import kite" in src or "import nse" in src:
                        self.assertIn('"trader-kite"', src, "%s/%s imports kite without the sys.path line" % (s, f))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-skill")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-skill")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-skill")

    def test_readme_carries_the_reel_copy(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Day Trader skill, India edition"))
        for line in (ABOUT,
                     "https://github.com/krishanag/day-trader-skill install skill",
                     "/plugin marketplace add krishanag/day-trader-skill",
                     "/plugin install day-trader-skill@day-trader-skill",
                     "## Fine print"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):  # fixtures are real data, kept as published
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)  # built, so this file passes too

    def test_no_v1_only_skill_is_left_behind(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith("tests" + os.sep) or rel == "README.md":
                continue  # the README points v1 users to the v1 commit
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in V1_ONLY:
                self.assertNotIn(old, text, rel)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (the README promises 3.8)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix(",
                            "match ", "list[", "dict[", "tuple["):
                    if bad == "match ":
                        self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                    elif bad.endswith("["):
                        self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(bad), "%s/%s" % (s, f))
                    else:
                        self.assertNotIn(bad, src, "%s/%s" % (s, f))

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("budget_inr", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_before", "no_entries_after", "flat_by",
                  "product", "order_style", "exchange"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertEqual(cfg["product"], "MIS")
        self.assertEqual(cfg["exchange"], "NSE")
        self.assertEqual(cfg["order_style"], "co")
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_fno", False), False)
        self.assertIs(cfg.get("allow_bse", False), False)
        self.assertIs(cfg.get("use_mis_leverage", False), False)
        self.assertLess(cfg["flat_by"], "15:12", "flat_by must beat Zerodha's CAS square-off")

    def test_paper_trading_is_the_default(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertEqual(cfg["broker"], "zerodha")


if __name__ == "__main__":
    unittest.main()
