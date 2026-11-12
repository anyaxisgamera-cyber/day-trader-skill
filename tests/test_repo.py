"""Invariants of the curated, research-only install (audited commit 3d0f4ef, 7 skills).

These replace the upstream repo-layout tests: the layout changed on purpose
(skills moved to .claude/skills/, four skills removed, plugin manifest removed).
"""

import hashlib
import json
import os
import re
import unittest

from _load import ROOT, SKILLS as SKILLS_DIR

KEPT = ["trader-scanner", "trader-news", "trader-timemachine", "trader-analyst",
        "trader-risk", "trader-budget", "trader-alerts"]
REMOVED = ["trader-broker", "trader-entry", "trader-exit", "trader-pelosi"]
REMOVED_SCRIPTS = ["ticket.py", "entry.py", "exit.py", "ptr.py", "claude_desktop_config.example.json"]

# sha256 of each tool as audited at 3d0f4ef. A change here must be re-reviewed.
AUDITED_SHA256 = {
    "trader-scanner/scan.py": "2cc0d99a4f21d2b8a28b6917d527ca17e3a71dea0951365d5f82c6b5afcac913",
    "trader-news/news.py": "bf9e12a9c5962de3f661bf334e76f4df417a39879ff067426b338b7a063c71f3",
    "trader-timemachine/timemachine.py": "5e73d566c78e5e42142371319491eef6257b6fc31fab5a87aa62506d03e9b5a1",
    "trader-analyst/facts.py": "6cd7e24588e7389268cd6af6315768bd9c292032773fe1a7364e07696d7ac6b9",
    "trader-risk/check.py": "6faf9372c20d30b33dbc6175d0d81935f8dc88bc8e94aacd2ed0ae6919aafa18",
    "trader-budget/size.py": "2dcb2ef604a92fb022ca210eb67a9b28ef82fefe5f7a2382ce2592461a6d3cba",
    "trader-alerts/alerts.py": "2db22d8e9b27ff1776600dd53664e21b55fc8787ed1abdd9e6f1e79bcc80c74f",
}


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            yield os.path.join(base, f)


def settings():
    with open(os.path.join(ROOT, ".claude", "settings.json"), encoding="utf-8") as fh:
        return json.load(fh)


class InstalledSkills(unittest.TestCase):
    def test_exactly_the_seven_skills_are_installed(self):
        found = sorted(d for d in os.listdir(SKILLS_DIR)
                       if os.path.isfile(os.path.join(SKILLS_DIR, d, "SKILL.md")))
        self.assertEqual(found, sorted(KEPT))

    def test_frontmatter_names_match_their_folders(self):
        for s in KEPT:
            fm, _ = frontmatter(os.path.join(SKILLS_DIR, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)

    def test_no_skill_grants_itself_tools(self):
        for s in KEPT:
            fm, _ = frontmatter(os.path.join(SKILLS_DIR, s, "SKILL.md"))
            self.assertNotIn("allowed-tools", fm, s)

    def test_tools_are_the_audited_bytes(self):
        for rel, want in AUDITED_SHA256.items():
            with open(os.path.join(SKILLS_DIR, rel), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), want, rel)


class RemovedSkills(unittest.TestCase):
    def test_removed_skill_folders_are_gone_everywhere(self):
        for base, dirs, _ in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d != ".git"]
            for d in dirs:
                self.assertNotIn(d, REMOVED, os.path.join(base, d))

    def test_removed_scripts_and_plugin_manifest_are_gone(self):
        names = {os.path.basename(p) for p in every_file()}
        for f in REMOVED_SCRIPTS:
            self.assertNotIn(f, names, f)
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".claude-plugin")))


class NoBrokerNoAutomation(unittest.TestCase):
    def test_every_skill_carries_the_installation_safety_block(self):
        for s in KEPT:
            _, text = frontmatter(os.path.join(SKILLS_DIR, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("## safety (this installation)", low, s)
            self.assertIn("no broker connection and no order execution", low, s)
            self.assertIn("never put it on `/loop`, a scheduled task", low, s)
            self.assertIn("not financial advice", low, s)

    def test_no_skill_tells_claude_to_schedule_or_loop_itself(self):
        for s in KEPT:
            _, text = frontmatter(os.path.join(SKILLS_DIR, s, "SKILL.md"))
            for bad in ("/loop 15m", "/loop 1h", "scheduled task each", "every few minutes from",
                        "call `place_stock_order`", "connect alpaca"):
                self.assertNotIn(bad, text.lower(), "%s: %s" % (s, bad))

    def test_alerts_is_manual_only(self):
        fm, _ = frontmatter(os.path.join(SKILLS_DIR, "trader-alerts", "SKILL.md"))
        self.assertIn("disable-model-invocation: true", fm)
        self.assertEqual(settings()["skillOverrides"]["trader-alerts"], "user-invocable-only")

    def test_settings_keep_normal_permission_mode(self):
        p = settings()["permissions"]
        self.assertEqual(p["defaultMode"], "default")
        self.assertEqual(p["disableBypassPermissionsMode"], "disable")
        self.assertEqual(p["disableAutoMode"], "disable")
        self.assertEqual(p.get("allow", []), [])
        self.assertIs(settings()["enableAllProjectMcpServers"], False)

    def test_settings_deny_brokers_and_scheduling(self):
        deny = set(settings()["permissions"]["deny"])
        for rule in ("mcp__alpaca", "mcp__alpaca__*", "mcp__ibkr", "CronCreate", "ScheduleWakeup",
                     "RemoteTrigger", "Monitor", "Bash(uvx *)", "Bash(claude mcp *)", "Bash(crontab *)"):
            self.assertIn(rule, deny, rule)
        self.assertEqual(settings()["skillOverrides"]["loop"], "off")

    def test_no_mcp_server_config_ships_in_the_repo(self):
        names = {os.path.basename(p) for p in every_file()}
        self.assertNotIn(".mcp.json", names)
        self.assertNotIn("claude_desktop_config.json", names)

    def test_paper_and_no_leverage_in_the_example_config(self):
        with open(os.path.join(SKILLS_DIR, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg["require_stop"], True)
        for k in ("allow_options", "allow_margin", "allow_short"):
            self.assertIs(cfg.get(k, False), False, k)


class Privacy(unittest.TestCase):
    def test_working_data_is_git_ignored(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as fh:
            lines = fh.read().split()
        self.assertIn("trader/", lines)
        self.assertIn(".claude/settings.local.json", lines)

    def test_no_secrets_or_personal_email_committed(self):
        key = re.compile(r"(?i)(alpaca_(api|secret)_key|api[_-]?key|secret[_-]?key)\s*[\"':=]+\s*[\"']?[A-Za-z0-9]{16,}")
        for path in every_file():
            if not path.endswith((".md", ".py", ".json", ".txt", ".csv", ".xml")):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)
            self.assertIsNone(key.search(text), rel)


class Compatibility(unittest.TestCase):
    def test_every_tool_runs_on_python_3_8(self):
        for s in KEPT:
            folder = os.path.join(SKILLS_DIR, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                where = "%s/%s" % (s, f)
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix("):
                    self.assertNotIn(bad, src, where)
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", where)
                for gen in ("list[", "dict[", "tuple["):
                    self.assertNotRegex(src, r"(?<![\w.])%s" % re.escape(gen), where)


if __name__ == "__main__":
    unittest.main()
