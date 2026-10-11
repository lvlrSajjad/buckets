"""Tests for regress.py, with a stand-in for buckets (no models, no servers)."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import regress

FAKE = textwrap.dedent('''
    import json, os, subprocess, sys, time
    from pathlib import Path
    request = sys.argv[-1]
    def git(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout
    def finish(done, branch, text, kind="code"):
        run = Path(os.environ["BUCKETS_STATE"]) / "runs" / f"r{time.time_ns()}"
        run.mkdir(parents=True)
        (run / "report.md").write_text(text)
        (run / "meta.json").write_text(json.dumps({"request": request, "kind": kind, "done": done, "branch": branch,
                                                   "report": str(run / "report.md")}))
        print(f"[00:07]   report: {run / 'report.md'}")
    if "question" in request:
        finish(None, None, "## Answer\\nalpha and beta-two", "info")
    elif "stop" in request:
        sys.exit(130)
    else:
        mine = [b for b in git("for-each-ref", "--format=%(refname:short)", "refs/heads/buckets").split()]
        if mine and "now" in request:
            git("checkout", "-q", mine[-1])
        branch = f"buckets/b{len(mine)}"
        git("checkout", "-q", "-b", branch)
        name = "world.txt" if "world" in request else "hello.txt"
        Path(name).write_text("hello\\n" * 3)
        git("add", name)
        git("commit", "-q", "-m", request)
        git("checkout", "-q", "main")
        if "touch source" in request:
            src = git("remote", "get-url", "origin").strip()
            subprocess.run(["git", "-C", src, "branch", "intruder"], check=True)
        if "dirty" in request:
            Path("left-behind.txt").write_text("x")
        finish("broken" not in request, branch, "done")
''')


def redirect_quiet():
    import io
    from contextlib import redirect_stdout
    return redirect_stdout(io.StringIO())


class Suite(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "app"
        self.src.mkdir()
        g = lambda *a: subprocess.run(["git", *a], cwd=self.src, capture_output=True, text=True, check=True).stdout
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@example.com")
        g("config", "user.name", "T")
        (self.src / "a.txt").write_text("a\n")
        (self.src / ".gitignore").write_text("deps/\n")
        (self.src / "deps").mkdir()
        g("add", ".")
        g("commit", "-q", "-m", "first")
        g("stash", "list")
        self.commit = g("rev-parse", "HEAD").strip()
        self.fake = self.tmp / "fake_buckets.py"
        self.fake.write_text(FAKE)
        self.saved = regress.STATE, regress.CONFIG, regress.MAIN_STATE
        regress.STATE, regress.CONFIG, regress.MAIN_STATE = self.tmp / "state", self.tmp / "config.json", self.tmp / "main-state"
        regress.CONFIG.write_text(json.dumps({"repos": {str(self.tmp.resolve() / "*"): {"checks": {"test": "true"}}}}))

    def tearDown(self):
        regress.STATE, regress.CONFIG, regress.MAIN_STATE = self.saved

    def case(self, name, request, expect, **more):
        d = self.tmp / "cases"
        d.mkdir(exist_ok=True)
        c = {"repo": str(self.src), "commit": self.commit, "request": request, "expect": expect,
             "buckets": [sys.executable, str(self.fake)], "links": ["deps"], **more}
        (d / f"{name}.json").write_text(json.dumps(c))
        return d

    def run_suite(self, *names):
        from contextlib import redirect_stdout
        import io
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = regress.main(["--cases", str(self.tmp / "cases"), *names])
        return code, buf.getvalue()

    def test_a_case_that_holds_passes_and_leaves_the_repo_as_found(self):
        before = regress.snapshot(self.src)
        self.case("hello", "add hello", {"done": True, "exists": ["hello.txt"], "absent": ["nope.txt"],
                                         "matches": [{"file": "*.txt", "regex": "^hello$", "min": 3, "max": 3}],
                                         "max_lines": {"hello.txt": 3}, "changed_within": ["*.txt"],
                                         "commands": ["test -f hello.txt"]})
        code, out = self.run_suite()
        self.assertEqual(code, 0, out)
        r = regress.results()[-1]
        self.assertTrue(r["ok"], r["checks"])
        self.assertEqual(len(r["checks"]), 7)
        self.assertEqual(regress.snapshot(self.src), before)
        self.assertIn("1/1 passed", out)

    def test_the_clone_is_reset_between_cases_and_reused(self):
        self.case("hello", "add hello", {"done": True, "exists": ["hello.txt"]})
        self.run_suite()
        clone = next((regress.STATE / "clones").glob("*/app"))
        self.assertEqual(subprocess.run(["git", "branch", "--list", "buckets/*"], cwd=clone, capture_output=True,
                                        text=True).stdout.strip(), "")
        self.assertFalse((clone / "hello.txt").exists())
        self.assertTrue((clone / "deps").is_symlink())
        code, _ = self.run_suite()
        self.assertEqual(code, 0)
        self.assertEqual(len(list((regress.STATE / "clones").glob("*/app"))), 1)
        cfg = json.loads((regress.STATE / "config.json").read_text())
        self.assertEqual(cfg["repos"][str(clone)], {"checks": {"test": "true"}, "memory": "outside"})

    def test_failed_expectations_are_reported(self):
        self.case("hello", "add hello broken", {"done": True, "exists": ["missing.txt"],
                                                "changed_within": ["src/*"], "commands": ["false"]})
        code, out = self.run_suite()
        r = regress.results()[-1]
        self.assertFalse(r["ok"])
        failing = {x["check"] for x in r["checks"] if not x["ok"]}
        self.assertEqual(failing, {"done", "exists missing.txt", "changed only where expected", "$ false"})
        self.assertIn("0/1 passed", out)

    def test_follow_ups_are_checked_on_the_last_branch(self):
        self.case("both", "add hello", {"done": True, "exists": ["hello.txt", "world.txt"]},
                  follow_ups=["now add world"])
        code, out = self.run_suite()
        r = regress.results()[-1]
        self.assertTrue(r["ok"], r["checks"])
        self.assertEqual(len(r["steps"]), 2)

    def test_answers_are_checked_for_the_names_they_must_give(self):
        self.case("q", "a question", {"answer": {"contains": ["alpha", "beta-two", "gamma"], "min": 2}})
        self.run_suite()
        r = regress.results()[-1]
        self.assertTrue(r["ok"], r["checks"])
        self.assertIn("missing gamma", r["checks"][0]["detail"])
        self.case("q", "a question", {"answer": {"contains": ["alpha", "beta"]}})  # beta is only part of beta-two
        self.run_suite()
        self.assertFalse(regress.results()[-1]["ok"])

    def test_a_stopped_run_fails(self):
        self.case("s", "stop now", {"done": True})
        self.run_suite()
        r = regress.results()[-1]
        self.assertEqual([x["check"] for x in r["checks"]], ["finished"])
        self.assertFalse(r["ok"])

    def test_leftovers_in_the_clone_fail_the_case(self):
        self.case("d", "add hello dirty", {"done": True})
        self.run_suite()
        self.assertIn("clean after the run", {x["check"] for x in regress.results()[-1]["checks"] if not x["ok"]})

    def test_a_change_to_the_source_repo_stops_the_suite(self):
        self.case("a-evil", "add hello touch source", {"done": True})
        self.case("b-next", "add hello", {"done": True})
        code, out = self.run_suite()
        self.assertEqual(code, 3)
        self.assertIn("STOPPED", out)
        self.assertEqual([r["case"] for r in regress.results()], ["a-evil"])

    def test_the_summary_compares_with_the_suite_before(self):
        rows = [{"suite": "1", "case": "x", "ok": True, "minutes": 5.0, "checks": [], "buckets": "a"},
                {"suite": "2", "case": "x", "ok": False, "minutes": 7.0, "buckets": "b",
                 "checks": [{"check": "done", "ok": False}]},
                {"suite": "2", "case": "x", "ok": True, "minutes": 6.0, "buckets": "b", "checks": []}]
        text = regress.summary(rows)
        self.assertIn("| x | 1/2 | 6.5 min | – | 1/1, 5.0 min | done |", text)
        self.assertIn("buckets b", text)

    def test_overrides_reach_the_config_and_phases_come_from_the_events(self):
        self.assertEqual(regress.parse_sets(["coder.engine=native", "coder.turns=9", "x=a b"]),
                         {"coder": {"engine": "native", "turns": 9}, "x": "a b"})
        self.case("hello", "add hello", {"done": True}, config={"coder": {"attempts": 1}})
        with redirect_quiet():
            regress.main(["--cases", str(self.tmp / "cases"), "--set", "coder.engine=native"])
        clone = next((regress.STATE / "clones").glob("*/app"))
        cfg = json.loads((regress.STATE / "config.json").read_text())
        self.assertEqual(cfg["coder"], {"attempts": 1, "engine": "native"})
        self.assertEqual(regress.results()[-1]["overrides"], {"coder": {"engine": "native"}})
        run = self.tmp / "run"
        run.mkdir()
        (run / "events.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"ev": "call", "id": 1, "label": "code-T1-1"}, {"ev": "call_end", "id": 1, "ok": True, "secs": 4.0},
            {"ev": "call", "id": 2, "label": "code-R2T1-1"}, {"ev": "call_end", "id": 2, "ok": False, "secs": 2.5},
            {"ev": "call", "id": 3, "label": "plan-1"}, {"ev": "call_end", "id": 3, "ok": True, "secs": 30},
            {"ev": "tool", "id": 1, "name": "tsc"}, {"ev": "tool_end", "id": 1, "ok": True, "secs": 10}]))
        self.assertEqual(regress.phases(run), {"code": {"secs": 6.5, "n": 2, "failed": 1},
                                               "plan": {"secs": 30.0, "n": 1, "failed": 0},
                                               "tsc": {"secs": 10.0, "n": 1, "failed": 0}})

    def test_bad_case_files_are_refused(self):
        d = self.tmp / "cases"
        d.mkdir()
        (d / "bad.json").write_text(json.dumps({"repo": ".", "request": "x", "expect": {"exist": []}}))
        with self.assertRaises(SystemExit) as e:
            regress.load_cases([d])
        self.assertIn("missing commit", str(e.exception))
        self.assertIn("unknown expectation exist", str(e.exception))


if __name__ == "__main__":
    unittest.main()
