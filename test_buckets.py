"""Tests for the parts of buckets that don't need a model: run with `python3 -m unittest -v`."""
import ast
import contextlib
import io
import json
import sys
import os
from datetime import datetime, timedelta
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import buckets

# Tests must never touch the real config, opencode config or run folders.
_SANDBOX = Path(tempfile.mkdtemp(prefix="buckets-tests-"))
buckets.CONFIG_PATH = _SANDBOX / "config.json"
buckets.OPENCODE_CONFIG = _SANDBOX / "opencode.json"
buckets.STATE_DIR = _SANDBOX / "state"
buckets.LOCK_PATH = _SANDBOX / "active.lock"


def make_repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"],
                ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True)
    return root


class ExtractJson(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(buckets.extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_with_prose(self):
        reply = 'Here you go:\n```json\n{"mode": "code", "reason": "x"}\n```\nThanks'
        self.assertEqual(buckets.extract_json(reply)["mode"], "code")

    def test_think_block_and_braces_in_strings(self):
        reply = '<think>maybe {"no": 1}</think>{"s": "a } b { c", "n": {"k": [1, 2]}} trailing'
        self.assertEqual(buckets.extract_json(reply), {"s": "a } b { c", "n": {"k": [1, 2]}})

    def test_escaped_quote(self):
        self.assertEqual(buckets.extract_json(r'{"s": "say \"hi\" }"}'), {"s": 'say "hi" }'})

    def test_code_fences_inside_strings(self):
        reply = json.dumps({"tasks": [{"instructions": "Use this:\n```ts\nconst a = {b: 1};\n```\nThen stop."}]})
        self.assertEqual(buckets.extract_json(reply)["tasks"][0]["instructions"].count("```"), 2)
        self.assertEqual(buckets.extract_json("Sure:\n" + reply)["tasks"][0]["instructions"].count("```"), 2)

    def test_whole_reply_fenced_with_inner_fences(self):
        inner = json.dumps({"s": "```py\nx = 1\n```"})
        self.assertEqual(buckets.extract_json(f"```json\n{inner}\n```"), {"s": "```py\nx = 1\n```"})

    def test_cut_off(self):
        with self.assertRaises(ValueError):
            buckets.extract_json('{"a": [1, 2')

    def test_none(self):
        with self.assertRaises(ValueError):
            buckets.extract_json("no json here")


class Tasks(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo({"src/a.ts": "export const a = 1;\n"})

    def task(self, **kw):
        t = {"id": "x", "file": "src/a.ts", "action": "edit", "instructions": "do it", "done_when": "done"}
        t.update(kw)
        return t

    def test_ids_renumbered_and_paths_cleaned(self):
        out = buckets.validate_tasks([self.task(file="./src/a.ts"), self.task(file="src/b.ts", action="create")],
                                     self.repo, 8, "T")
        self.assertEqual([t["id"] for t in out], ["T1", "T2"])
        self.assertEqual(out[0]["file"], "src/a.ts")

    def test_actions_fixed_to_match_disk(self):
        out = buckets.validate_tasks([self.task(file="src/new.ts", action="edit"),
                                      self.task(action="create")], self.repo, 8, "T")
        self.assertEqual([t["action"] for t in out], ["create", "edit"])

    def test_delete_of_missing_file_dropped(self):
        self.assertEqual(buckets.validate_tasks([self.task(file="gone.ts", action="delete")], self.repo, 8, "T"), [])

    def test_escaping_paths_rejected(self):
        for bad in ("../x.ts", "/etc/passwd"):
            with self.assertRaises(ValueError):
                buckets.validate_tasks([self.task(file=bad)], self.repo, 8, "T")

    def test_max_tasks_and_read_first_filtered(self):
        out = buckets.validate_tasks([self.task(read_first=["src/a.ts", "missing.ts"])] * 5, self.repo, 2, "R2T")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["id"], "R2T2")
        self.assertEqual(out[0]["read_first"], ["src/a.ts"])


class CoderPrompt(unittest.TestCase):
    def test_formats_a_validated_task(self):
        repo = make_repo({"src/a.ts": "x\n", "src/b.ts": "y\n"})
        task = buckets.validate_tasks([{"id": "x", "file": "src/a.ts", "action": "edit", "instructions": "add {braces}",
                                        "done_when": "it works", "read_first": ["src/b.ts"]}], repo, 8, "T")[0]
        text = buckets.coder_prompt("goal", task)
        self.assertIn("YOUR TASK (T1): edit the file `src/a.ts`.", text)
        self.assertIn("add {braces}", text)
        self.assertIn("Open these first for context: `src/b.ts`", text)
        task["read_first"] = []
        self.assertNotIn("Open these first", buckets.coder_prompt("goal", task))


class LeaveBranch(unittest.TestCase):
    def test_empty_branch_deleted(self):
        repo = make_repo({"a.ts": "1\n"})
        start = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        buckets.git(repo, "checkout", "-q", "-b", "buckets/x")
        self.assertIn("deleted", buckets.leave_branch(repo, "buckets/x", start, base))
        self.assertNotIn("buckets/x", buckets.git(repo, "branch"))

    def test_branch_with_work_kept(self):
        repo = make_repo({"a.ts": "1\n"})
        start = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        buckets.git(repo, "checkout", "-q", "-b", "buckets/x")
        (repo / "a.ts").write_text("2\n")
        buckets.git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "w")
        self.assertIn("committed on buckets/x", buckets.leave_branch(repo, "buckets/x", start, base))
        self.assertEqual(buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), start)


class GitScope(unittest.TestCase):
    def test_changed_paths_and_revert(self):
        repo = make_repo({"keep.ts": "1\n", "other.ts": "2\n", "dir/x.ts": "3\n"})
        (repo / "keep.ts").write_text("changed\n")
        (repo / "other.ts").write_text("stray\n")
        (repo / "dir/x.ts").unlink()
        (repo / "new dir").mkdir()
        (repo / "new dir/n.ts").write_text("new\n")
        self.assertEqual(sorted(buckets.changed_paths(repo)), ["dir/x.ts", "keep.ts", "new dir/n.ts", "other.ts"])
        buckets.revert_paths(repo, ["other.ts", "dir/x.ts", "new dir/n.ts"])
        self.assertEqual(buckets.changed_paths(repo), ["keep.ts"])
        self.assertEqual((repo / "other.ts").read_text(), "2\n")
        self.assertTrue((repo / "dir/x.ts").exists())


class RepoReading(unittest.TestCase):
    def test_list_files_skips_lockfiles_binaries_and_excludes(self):
        repo = make_repo({"a.ts": "x\ny\n", "pnpm-lock.yaml": "lock", "logo.png": "png",
                          "gen/out.ts": "z", "bin.dat": "a\0b"})
        files = buckets.list_files(repo, ["gen/*"])
        self.assertEqual(files, {"a.ts": 2})

    def test_batches_respect_budget(self):
        blocks = ["x" * 300, "y" * 300, "z" * 300]  # ~100 tokens each
        self.assertEqual([len(b) for b in buckets.batches(blocks, 210)], [2, 1])
        self.assertEqual(len(buckets.batches(["x" * 3000], 10)), 1)  # oversize block still goes alone


class Checks(unittest.TestCase):
    def test_pnpm_scripts(self):
        repo = make_repo({"package.json": json.dumps({"scripts": {
            "typecheck": "tsc", "lint": "eslint .", "test": 'echo "Error: no test specified" && exit 1'}}),
            "pnpm-lock.yaml": ""})
        self.assertEqual(buckets.detect_checks(repo, {}),
                         {"typecheck": "pnpm -s run typecheck", "lint": "pnpm -s run lint"})

    def test_repo_config_overrides(self):
        repo = make_repo({"package.json": json.dumps({"scripts": {"test": "vitest"}})})
        self.assertEqual(buckets.detect_checks(repo, {"checks": {"t": "make t"}}), {"t": "make t"})
        self.assertEqual(buckets.detect_checks(repo, {"checks": {}}), {})

    def test_go(self):
        repo = make_repo({"go.mod": "module x\n"})
        self.assertEqual(set(buckets.detect_checks(repo, {})), {"vet", "test"})


class Config(unittest.TestCase):
    def test_deep_merge(self):
        merged = buckets.deep_merge({"a": {"b": 1, "c": 2}, "d": 3}, {"a": {"c": 9}})
        self.assertEqual(merged, {"a": {"b": 1, "c": 9}, "d": 3})


class CheckOutput(unittest.TestCase):
    def results(self):
        return {"typecheck": {"ok": False, "output": "x" * 50 + "src/a.ts(3,5): error TS2322"},
                "lint": {"ok": True, "output": "clean"},
                "coverage": {"ok": True, "output": "All files | 81.2"}}

    def test_failing_and_listed_checks_shown(self):
        text = buckets.check_context(self.results(), ["coverage"], 1000)
        self.assertIn("typecheck (FAILS)", text)
        self.assertIn("coverage (passes)", text)
        self.assertNotIn("lint", text)

    def test_output_trimmed_from_the_start(self):
        text = buckets.check_context(self.results(), [], 20)
        self.assertTrue(text.endswith("error TS2322"))

    def test_paths_in_text(self):
        files = {"packages/core/src/a.ts": 3, "packages/core/src/b.ts": 3, "src/c.ts": 1, "x/c.ts": 1}
        out = ("packages/core typecheck: src/a.ts(3,5): error TS2322\n"
               "/abs/packages/core/src/b.ts:12:4 warning\n"
               "c.ts:1 ambiguous\nsrc/c.ts:2 exact\n")
        self.assertEqual(buckets.paths_in_text(out, files),
                         ["packages/core/src/a.ts", "packages/core/src/b.ts", "src/c.ts"])


class Examples(unittest.TestCase):
    def test_commit_and_file(self):
        repo = make_repo({"a.ts": "export const a = 1;\n"})
        (repo / "b.ts").write_text("export const b = 2;\n")
        buckets.git(repo, "add", "b.ts")
        buckets.git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "add b feature")
        text = buckets.example_material(repo, ["HEAD", "a.ts"], 10000)
        self.assertIn("add b feature", text)
        self.assertIn("+export const b = 2;", text)
        self.assertIn("=== a.ts ===", text)

    def test_folder_example(self):
        repo = make_repo({"pkg/a.ts": "export const a = 1;\n", "pkg/b.ts": "export const b = 2;\n", "x.ts": "x\n"})
        text = buckets.example_material(repo, ["pkg/"], 10000)
        self.assertIn("=== example folder pkg (2 files) ===", text)
        self.assertIn("=== pkg/a.ts ===", text)
        self.assertNotIn("x.ts", text)
        self.assertEqual(buckets.valid_examples(repo, ["pkg/"]), ["pkg"])

    def test_unknown_ref_stops(self):
        repo = make_repo({"a.ts": "1\n"})
        with self.assertRaises(SystemExit):
            buckets.example_material(repo, ["no-such-ref"], 1000)


class Shortcuts(unittest.TestCase):
    DIFF = """diff --git a/src/a.ts b/src/a.ts
+++ b/src/a.ts
+  const x = y as any;
+  // @ts-ignore
+  const ok: Answer = compute();
-  const old = z as any;
+++ b/test/a.test.ts
+  it.skip('works', () => {});
+  xit('also', () => {});
+  it('fine', () => {});
"""

    def test_leftover_debugging_code_is_flagged_unless_allowed(self):
        diff = ("--- a/src/a.ts\n+++ b/src/a.ts\n+  console.log('here', x)\n+  debugger;\n+  console.error('real')\n"
                "-  console.log('old')\n+  console.log('old')\n")
        hits = buckets.find_shortcuts(diff, [])
        self.assertEqual(len(hits), 2)
        self.assertTrue(all("debugging" in h for h in hits))
        self.assertEqual(buckets.find_shortcuts(diff, ["debug"]), [])

    def test_code_moved_to_another_file_isnt_a_new_shortcut(self):
        diff = ("--- a/src/screen.tsx\n+++ b/src/screen.tsx\n-  const x = y as any;\n-  // @ts-ignore\n"
                "--- /dev/null\n+++ b/src/use-screen.ts\n+  const x = y as any\n+  // @ts-ignore\n+  const z = w as any\n")
        hits = buckets.find_shortcuts(diff, [])
        self.assertEqual(len(hits), 1)
        self.assertIn("const z = w as any", hits[0])
        joined = ("--- a/src/screen.tsx\n+++ b/src/screen.tsx\n-const f = (\n-  q: Q,\n-  a: any,\n-) => {\n"
                  "--- /dev/null\n+++ b/src/use-screen.ts\n+export const f = (q: Q, a: any) => {\n")
        self.assertEqual(buckets.find_shortcuts(joined, []), [])           # a formatter joined it; `export` added

    def test_flags_added_lines_only(self):
        hits = buckets.find_shortcuts(self.DIFF, [])
        # src/a.ts adds one `as any` and removes one, so only the @ts-ignore counts there.
        self.assertEqual(len(hits), 3)
        self.assertFalse(any("uses `any`" in h for h in hits))
        self.assertTrue(any("@ts-ignore" in h for h in hits))
        self.assertEqual(sum("skips or narrows tests" in h for h in hits), 2)

    def test_edited_line_that_already_had_any_is_not_flagged(self):
        diff = ("+++ b/src/s.ts\n"
                "-  scopeFor(request: any, subject: SubjectType) {\n"
                "+  scopeFor(request: any, subject: SubjectKind) {\n")
        self.assertEqual(buckets.find_shortcuts(diff, []), [])

    def test_new_any_on_top_of_existing_is_flagged(self):
        diff = ("+++ b/src/s.ts\n"
                "-  f(a: any) {\n"
                "+  f(a: any, b: any) {\n"
                "+  const c = d as any;\n")
        hits = buckets.find_shortcuts(diff, [])
        self.assertEqual(len(hits), 1)
        self.assertIn("as any", hits[0])

    def test_allowed_keys(self):
        hits = buckets.find_shortcuts(self.DIFF, ["any", "skip-tests"])
        self.assertEqual(len(hits), 1)
        self.assertIn("type checker", hits[0])


class ClaudeMode(unittest.TestCase):
    CFG = {"bin": "claude", "budget_usd": 3.0}

    def test_command_is_read_only_and_capped(self):
        cmd = buckets.claude_command(self.CFG, "sonnet", {"type": "object"}, tools=True)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read,Grep,Glob")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "3.0")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), {"type": "object"})

    def test_command_without_tools(self):
        cmd = buckets.claude_command(self.CFG, "haiku", None, tools=False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", cmd)
        self.assertNotIn("--json-schema", cmd)

    def test_parse_structured_output(self):
        out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                          "structured_output": {"mode": "code", "reason": "x"},
                          "total_cost_usd": 0.0123, "num_turns": 3, "duration_ms": 4200})
        payload, cost, summary = buckets.parse_claude(out, "plan", {"type": "object"})
        self.assertEqual(payload["mode"], "code")
        self.assertAlmostEqual(cost, 0.0123)
        self.assertIn("3 turns", summary)

    def test_parse_json_in_result_text(self):
        out = json.dumps({"subtype": "success", "is_error": False, "result": 'Here: {"done": true}'})
        self.assertEqual(buckets.parse_claude(out, "review", {"type": "object"})[0], {"done": True})

    def test_parse_text_result(self):
        out = "warning on stderr\n" + json.dumps({"subtype": "success", "is_error": False, "result": "# Report"})
        self.assertEqual(buckets.parse_claude(out, "answer", None)[0], "# Report")

    def test_not_logged_in_is_an_error(self):
        out = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "Not logged in · Please run /login", "total_cost_usd": 0})
        with self.assertRaises(buckets.ModelError) as e:
            buckets.parse_claude(out, "plan", None)
        self.assertIn("Not logged in", str(e.exception))

    def test_budget_stop_is_an_error(self):
        out = json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "result": ""})
        with self.assertRaises(buckets.ModelError):
            buckets.parse_claude(out, "plan", None)

    def test_schema_give_up_is_its_own_error(self):
        out = json.dumps({"subtype": "error_max_structured_output_retries", "is_error": True, "result": None})
        with self.assertRaises(buckets.SchemaGaveUp):
            buckets.parse_claude(out, "plan", {"type": "object"})

    def test_create_tasks_tell_the_coder(self):
        repo = make_repo({"a.ts": "1\n"})
        task = buckets.validate_tasks([{"id": "x", "file": "new.ts", "action": "create",
                                        "instructions": "make it", "done_when": "exists"}], repo, 8, "T")[0]
        self.assertIn("doesn't exist yet", buckets.coder_prompt("goal", task))

    def test_mode_selection(self):
        repo = make_repo({".buckets.json": json.dumps({"mode": "claude"})})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {})
        self.assertEqual(buckets.Buckets(cfg, repo, "x").mode, "claude")
        cfg["mode_override"] = "local"
        self.assertEqual(buckets.Buckets(cfg, repo, "x").mode, "local")


def commit_all(repo, msg):
    buckets.git(repo, "add", "-A")
    buckets.git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg)
    return buckets.git(repo, "rev-parse", "HEAD").strip()


class Routing(unittest.TestCase):
    def test_validate_route(self):
        r = buckets.validate_route({"intent": "pick", "difficulty": "light", "reason": "r", "plan_only": False,
                                    "examples": [], "commits": ["abc"], "onto": "release"})
        self.assertEqual((r["intent"], r["commits"], r["onto"]), ("pick", ["abc"], "release"))
        with self.assertRaises(ValueError):
            buckets.validate_route({"intent": "build", "difficulty": "light"})

    def test_path_rules(self):
        cfg = {"repos": {"~/Coding/ME/*": {"mode": "local"}, "/tmp/x*": {"mode": "claude"}}}
        self.assertEqual(buckets.path_rules(cfg, Path.home() / "Coding/ME/castellan"), {"mode": "local"})
        self.assertEqual(buckets.path_rules(cfg, Path("/elsewhere/repo")), {})

    def test_repo_file_beats_path_rule(self):
        repo = make_repo({".buckets.json": json.dumps({"mode": "claude"})})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"repos": {str(repo): {"mode": "local"}}})
        self.assertEqual(buckets.Buckets(cfg, repo, "x").mode, "claude")

    def test_light_jobs_stay_local(self):
        self.assertEqual(buckets.choose_mode(buckets.DEFAULT_CONFIG, "light")[0], "local")

    def test_valid_examples(self):
        repo = make_repo({"src/a.ts": "1\n"})
        sha = buckets.git(repo, "rev-parse", "HEAD").strip()
        self.assertEqual(buckets.valid_examples(repo, ["./src/a.ts", sha[:7], "nope", "`src/missing.ts`"]),
                         ["src/a.ts", sha[:7]])


class CherryPickHelpers(unittest.TestCase):
    def test_marker_lines(self):
        f = Path(tempfile.mkdtemp()) / "f.ts"
        f.write_text("a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> abc (msg)\nconst x = '=======';\n")
        self.assertEqual(buckets.marker_lines(f), [2, 4, 6])

    def test_expand_commits_and_ranges(self):
        repo = make_repo({"a.ts": "1\n"})
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        (repo / "a.ts").write_text("2\n")
        c1 = commit_all(repo, "two")
        (repo / "a.ts").write_text("3\n")
        c2 = commit_all(repo, "three")
        got = buckets.expand_commits(repo, [f"{base}..{c2}", c1])
        self.assertEqual(got, [(c1, "two"), (c2, "three")])
        with self.assertRaises(SystemExit):
            buckets.expand_commits(repo, ["no-such-commit"])

    def test_merge_commits_refused(self):
        repo = make_repo({"a.ts": "1\n"})
        main = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        buckets.git(repo, "checkout", "-q", "-b", "side")
        (repo / "b.ts").write_text("b\n")
        commit_all(repo, "side")
        buckets.git(repo, "checkout", "-q", main)
        (repo / "c.ts").write_text("c\n")
        commit_all(repo, "main")
        buckets.git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-ff", "-m", "m", "side")
        with self.assertRaises(SystemExit):
            buckets.expand_commits(repo, ["HEAD"])

    def test_snapshot_scope(self):
        repo = make_repo({"a.ts": "1\n", "b.ts": "1\n", "c.ts": "1\n"})
        (repo / "a.ts").write_text("staged by a cherry-pick\n")
        before = buckets.snapshot(repo, buckets.changed_paths(repo) + ["b.ts"])
        (repo / "b.ts").write_text("the task\n")       # the task's file
        (repo / "a.ts").write_text("stray edit\n")     # already changed, then touched again
        (repo / "c.ts").write_text("stray edit\n")     # clean before
        (repo / "d.ts").write_text("new stray\n")
        touched = buckets.touched_since(repo, before)
        self.assertEqual(touched, ["a.ts", "b.ts", "c.ts", "d.ts"])
        buckets.restore(repo, before, ["a.ts", "c.ts", "d.ts"])
        self.assertEqual((repo / "a.ts").read_text(), "staged by a cherry-pick\n")
        self.assertEqual((repo / "c.ts").read_text(), "1\n")
        self.assertFalse((repo / "d.ts").exists())
        self.assertEqual(buckets.touched_since(repo, before), ["b.ts"])


class CherryPickClean(unittest.TestCase):
    def test_clean_and_already_applied_picks(self):
        repo = make_repo({"a.ts": "1\n", "b.ts": "1\n", ".buckets.json": json.dumps({"checks": {}})})
        main = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        buckets.git(repo, "checkout", "-q", "-b", "feature")
        (repo / "a.ts").write_text("2\n")
        c1 = commit_all(repo, "change a")
        (repo / "b.ts").write_text("2\n")
        c2 = commit_all(repo, "change b")
        buckets.git(repo, "checkout", "-q", main)
        (repo / "b.ts").write_text("2\n")  # main already has c2's change
        commit_all(repo, "same change to b")
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local"})
        b = buckets.Buckets(cfg, repo, "pick")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = b.pick([c1, c2])
        self.assertEqual(code, 0)
        self.assertIn("| clean |", out.getvalue())
        self.assertIn("| skipped | already applied |", out.getvalue())
        self.assertEqual(buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), main)
        branch = [l.strip() for l in buckets.git(repo, "branch").splitlines() if "buckets/pick-" in l][0]
        self.assertEqual(buckets.git(repo, "show", f"{branch}:a.ts"), "2\n")
        self.assertIn(f"cherry picked from commit {c1}", buckets.git(repo, "log", "-1", "--format=%b", branch))


class ParallelSurvey(unittest.TestCase):
    def test_both_readers_share_the_queue_and_failures_go_to_the_mac(self):
        repo = make_repo({"a.ts": "1\n"})
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local"}), repo, "x")
        mac, win = {"name": "mac"}, {"name": "windows"}
        b.planner = mac
        b.readers = lambda n: [("mac", mac), ("windows", win)]
        calls = []

        def fake_chat(cfg, run, label, system, user, **kw):
            calls.append((cfg["name"], label))
            if cfg is win and "batch 3" in user:
                raise buckets.ModelError("windows fell over")
            time.sleep(0.05)
            return f"notes for {user.split('BATCH:')[1].strip()} by {cfg['name']}"

        real = buckets.chat
        buckets.chat = fake_chat
        try:
            groups = [[f"BATCH: batch {i}"] for i in range(1, 6)]
            notes = b.survey("info", groups, 999)
        finally:
            buckets.chat = real
        self.assertEqual([n.split(" by ")[0] for n in notes], [f"notes for batch {i}" for i in range(1, 6)])
        self.assertEqual(notes[2], "notes for batch 3 by mac")       # redone on the Mac
        self.assertIn("windows", {who for who, _ in calls})
        self.assertIn("mac", {who for who, _ in calls})


class Research(unittest.TestCase):
    def test_errand_answer_drops_tool_lines_and_stops_at_end(self):
        out = ("> buckets-reader · qwen3-coder:30b\n→ Read src/a.ts\n✗ Glob failed\nError: timeout\n"
               "`src/a.ts:3` defines it:\n    const a = 1;\nEND\nleftover chatter")
        self.assertEqual(buckets.errand_answer(out), "`src/a.ts:3` defines it:\n    const a = 1;")

    def test_ground_truth_reads_real_lines_and_flags_bad_refs(self):
        repo = make_repo({"packages/core/src/a.ts": "".join(f"line {i}\n" for i in range(1, 21))})
        files = buckets.list_files(repo, [])
        answer = ("see `packages/core/src/a.ts:10` and src/a.ts:15-16, also `src/missing.ts:3`, "
                  "and packages/core/src/a.ts:99")
        facts = buckets.ground_truth(answer, repo, files)
        self.assertIn("--- packages/core/src/a.ts:8-12 (from disk) ---", facts)
        self.assertIn("   10| line 10", facts)
        self.assertIn("--- packages/core/src/a.ts:13-18 (from disk) ---", facts)  # package-relative path
        self.assertIn("!! `src/missing.ts:3` cited, but no such file", facts)
        self.assertIn("has only 20 lines", facts)

    def test_split_where(self):
        self.assertEqual(buckets.split_where([]), [[]])
        self.assertEqual(buckets.split_where(["a", "b"]), [["a", "b"]])
        self.assertEqual(buckets.split_where(["a", "b", "c", "d", "", "e"]), [["a", "b", "c", "d"], ["e"]])

    def test_read_spec(self):
        repo = make_repo({"packages/core/src/a.ts": "".join(f"line {i}\n" for i in range(1, 501))})
        files = buckets.list_files(repo, [])
        whole = buckets.read_spec(repo, files, "packages/core/src/a.ts", 100000)
        self.assertTrue(whole.startswith("=== packages/core/src/a.ts ==="))
        self.assertIn("  500| line 500", whole)
        part = buckets.read_spec(repo, files, "src/a.ts:10-12", 100000)  # package-relative path
        self.assertEqual(part, "=== packages/core/src/a.ts:10-12 ===\n   10| line 10\n   11| line 11\n   12| line 12")
        self.assertIn("   7| line 7", buckets.read_spec(repo, files, "`packages/core/src/a.ts:7`", 100000))
        self.assertIn("1-400", buckets.read_spec(repo, files, "packages/core/src/a.ts:1-9999", 100000))
        self.assertTrue(buckets.read_spec(repo, files, "nope.ts:3", 1000).startswith("!!"))
        self.assertIn("only 500 lines", buckets.read_spec(repo, files, "packages/core/src/a.ts:900", 1000))

    def test_prompt_too_big_is_refused_before_sending(self):
        with self.assertRaises(buckets.PromptTooBig):
            buckets.chat({"url": "http://127.0.0.1:9", "model": "m", "num_ctx": 1000}, None, "plan", "sys",
                         "x" * 3000, max_tokens=100)

    def test_reading_strategy(self):
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {}), make_repo({"a.ts": "1\n"}), "x")
        b.difficulty = "heavy"
        self.assertEqual(b.reading(), "direct")                        # the default now (field notes, runs B/G)
        b.cfg["reading"] = "auto"
        self.assertEqual(b.reading(), "research")                      # auto: research for heavy jobs
        b.cfg["reading"] = "direct"
        self.assertEqual(b.reading(), "direct")


def _find_typescript() -> Path:
    """A `typescript` package for the code-index tests: $BUCKETS_TEST_TYPESCRIPT, a global install,
    or a checkout that has one. The tests are skipped when there's none."""
    import os
    if os.environ.get("BUCKETS_TEST_TYPESCRIPT"):
        return Path(os.environ["BUCKETS_TEST_TYPESCRIPT"])
    if shutil.which("npm"):
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        if root and (Path(root) / "typescript").exists():
            return Path(root) / "typescript"
    return Path.home() / "Coding/ME/castellan/node_modules/typescript"


TS_PKG = _find_typescript()


class CodeIndexTests(unittest.TestCase):
    def test_py_outline(self):
        repo = make_repo({"m.py": "import os\n\ndef run(a: int, b=2) -> str:\n    return ''\n\n"
                                  "class Box(Base):\n    def __init__(self):\n        pass\n\n"
                                  "    def open(self, key):\n        pass\n\n    def _hidden(self):\n        pass\n\n"
                                  "def _private():\n    pass\n"})
        out = buckets.py_outline(repo / "m.py")
        self.assertEqual([(d["name"], d["line"], d["end"], d["exported"]) for d in out],
                         [("run", 3, 4, True), ("Box", 6, 14, True), ("_private", 16, 17, False)])
        self.assertEqual(out[0]["signature"], "def run(a: int, b=2) -> str")
        self.assertEqual([m["name"] for m in out[1]["members"]], ["__init__", "open"])

    def test_render_outline_falls_back(self):
        files = {"a.ts": 10}
        outline = {"a.ts": [{"kind": "function", "name": "f", "exported": False, "line": 1, "end": 3,
                             "signature": "function f(x: number)"},
                            {"kind": "type", "name": "T", "exported": True, "line": 5, "end": 5, "signature": "type T = string"}]}
        full = buckets.render_outline(files, outline)
        self.assertIn("  1-3  function f(x: number)", full)
        exported_only = buckets.render_outline(files, outline, limit=60)
        self.assertNotIn("function f", exported_only)
        self.assertIn("type T", exported_only)
        self.assertEqual(buckets.render_outline(files, outline, limit=5), "a.ts (10 lines)")

    @unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and castellan's typescript package")
    def test_typescript_uses_and_broken_imports_across_packages(self):
        repo = make_repo({
            "package.json": json.dumps({"name": "root", "private": True}),
            "packages/a/package.json": json.dumps({"name": "@x/a"}),
            "packages/a/src/thing.ts": "export function thing(n: number): number {\n  return n;\n}\n",
            "packages/a/src/index.ts": "export { thing } from './thing.js';\nexport { gone } from './thing.js';\n",
            "packages/b/package.json": json.dumps({"name": "@x/b"}),
            "packages/b/src/use.ts": "import { thing, gone } from '@x/a';\n\nexport const v = thing(1);\n",
        })
        (repo / "node_modules").mkdir()
        (repo / "node_modules/typescript").symlink_to(TS_PKG)
        (repo / ".gitignore").write_text("node_modules\n")
        index = buckets.CodeIndex(repo, buckets.list_files(repo, []))
        self.assertTrue(index.ts)
        outline = index.outline()
        self.assertEqual(outline["packages/a/src/thing.ts"][0]["signature"], "function thing(n: number): number")
        uses = index.uses("thing", "packages/a/src/thing.ts")
        files = {(u["file"], bool(u.get("reexport"))) for u in uses["uses"]}
        self.assertIn(("packages/b/src/use.ts", False), files)
        self.assertIn(("packages/a/src/index.ts", True), files)
        broken = {(b["file"], b["name"]) for b in index.broken()}
        self.assertEqual(broken, {("packages/a/src/index.ts", "gone"), ("packages/b/src/use.ts", "gone")})
        good, text = buckets.index_check(repo)
        self.assertFalse(good)
        self.assertIn("packages/b/src/use.ts:1: imports `gone` from '@x/a'", text)


    @unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and castellan's typescript package")
    def test_api_impact_finds_untouched_callers(self):
        repo = make_repo({
            ".gitignore": "node_modules\n",
            "packages/a/package.json": json.dumps({"name": "@x/a"}),
            "packages/a/src/thing.ts": "export function thing(n: number): number {\n  return n;\n}\n"
                                       "export const gone = 1;\n",
            "packages/a/src/index.ts": "export { thing, gone } from './thing.js';\n",
            "packages/b/package.json": json.dumps({"name": "@x/b"}),
            "packages/b/src/use.ts": "import { thing } from '@x/a';\n\nexport const v = thing(1);\n",
        })
        (repo / "node_modules").mkdir()
        (repo / "node_modules/typescript").symlink_to(TS_PKG)
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local"}), repo, "x")
        b.surface_before = b.index.surface()
        (repo / "packages/a/src/thing.ts").write_text(
            "export function thing(n: number, m: number): number {\n  return n + m;\n}\n"
            "export function added(): void {}\n")
        (repo / "packages/a/src/index.ts").write_text("export { thing } from './thing.js';\n")
        commit_all(repo, "change thing")
        api = b.api_impact(base)
        self.assertEqual([c["name"] for c in api["changed"]], ["thing"])
        self.assertIn("(n: number, m: number)", api["changed"][0]["after"])
        self.assertEqual(api["removed"], ["`gone` from `packages/a/src/thing.ts`"])
        self.assertTrue(any("`added`" in a for a in api["added"]))
        self.assertEqual(len(api["untouched"]), 1)
        self.assertIn("packages/b/src/use.ts", api["untouched"][0])
        text = buckets.impact_text(api)
        self.assertIn("Callers of changed exports that this work didn't update:", text)


class Mechanical(unittest.TestCase):
    FILES = {"packages/core/src/a.ts": 30, "packages/core/src/b.ts": 30, "src/m.py": 30, "test/a.test.ts": 30}

    def test_parse_tsc_both_formats(self):
        out = ("src/a.ts(12,5): error TS2322: Type 'string' is not assignable.\n"
               "packages/core/src/b.ts:3:1 - error TS2305: Module has no exported member 'X'.\n")
        self.assertEqual(buckets.parse_diagnostics(out, self.FILES), [
            ("packages/core/src/a.ts", 12, "error TS2322: Type 'string' is not assignable."),
            ("packages/core/src/b.ts", 3, "error TS2305: Module has no exported member 'X'.")])

    def test_parse_eslint_stylish(self):
        out = ("/Users/me/repo/packages/core/src/a.ts\n"
               "  4:7   error    'x' is assigned a value but never used  no-unused-vars\n"
               "  9:1   warning  Unexpected console statement            no-console\n\n"
               "✖ 2 problems (1 error, 1 warning)\n")
        got = buckets.parse_diagnostics(out, self.FILES)
        self.assertEqual([(f, ln) for f, ln, _ in got], [("packages/core/src/a.ts", 4), ("packages/core/src/a.ts", 9)])
        self.assertTrue(got[0][2].startswith("error: 'x' is assigned"))

    def test_parse_ruff_and_test_frames(self):
        out = ("src/m.py:7:1: F401 `os` imported but unused\n"
               " FAIL  test/a.test.ts > adds > works\n"
               "AssertionError: expected 1 to be 2\n"
               " ❯ test/a.test.ts:14:20\n")
        got = buckets.parse_diagnostics(out, self.FILES)
        self.assertEqual(got[0], ("src/m.py", 7, "F401 `os` imported but unused"))
        self.assertEqual(got[1][:2], ("test/a.test.ts", 14))
        self.assertIn("adds > works · AssertionError: expected 1 to be 2", got[1][2])

    def test_diagnostics_text_attaches_source(self):
        repo = make_repo({"packages/core/src/a.ts": "line1\nconst bad: number = 'x';\n"})
        text = buckets.diagnostics_text("src/a.ts(2,7): error TS2322: nope", repo, {"packages/core/src/a.ts": 2})
        self.assertIn("    2: error TS2322: nope\n        | const bad: number = 'x';", text)

    def test_grep_text(self):
        repo = make_repo({"src/a.ts": "const lte = 1;\nconst other = 2;\n", "src/b.ts": "lte here\n"})
        (repo / "src/new.ts").write_text("untracked lte\n")
        out = buckets.grep_text(repo, "lte", ["src"])
        self.assertIn("(exact, 3 matches)", out)
        self.assertIn("src/a.ts:1:const lte = 1;", out)
        self.assertIn("src/new.ts:1:untracked lte", out)
        self.assertIn("no matches", buckets.grep_text(repo, "zzz", []))
        self.assertIn("GREP /a(/", buckets.grep_text(repo, "a(", []))  # a bad regex reports git's error

    def test_fill_fixer(self):
        files = ["a.ts", "b.py", "c.md"]
        self.assertEqual(buckets.fill_fixer("eslint --fix {js_files}", files), "eslint --fix a.ts")
        self.assertEqual(buckets.fill_fixer("prettier --write {files}", files), "prettier --write a.ts b.py c.md")
        self.assertIsNone(buckets.fill_fixer("gofmt -w {go_files}", files))
        self.assertEqual(buckets.fill_fixer("ruff format {py_files}", ["my file.py"]), "ruff format 'my file.py'")

    def test_auto_fix_commits_only_changed_files(self):
        repo = make_repo({"a.ts": "x  =  1\n", "b.ts": "y  =  2\n",
                          ".buckets.json": json.dumps({"fixers": ["sed -i '' 's/  =  / = /' {js_files}"]})})
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        (repo / "a.ts").write_text("x  =  10\n")
        commit_all(repo, "coder change")
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local"}), repo, "x")
        b.auto_fix(base)
        self.assertEqual((repo / "a.ts").read_text(), "x = 10\n")
        self.assertEqual((repo / "b.ts").read_text(), "y  =  2\n")     # not changed by this work: left alone
        self.assertEqual(buckets.git(repo, "log", "-1", "--format=%s").strip(), "buckets: auto-fix (sed)")
        self.assertEqual(buckets.changed_paths(repo), [])

    def test_coverage_text_istanbul_and_python(self):
        repo = make_repo({"src/a.ts": "x\n" * 20, "src/m.py": "x\n" * 20})
        (repo / "coverage").mkdir()
        (repo / "coverage/coverage-final.json").write_text(json.dumps({str(repo / "src/a.ts"): {
            "path": str(repo / "src/a.ts"),
            "statementMap": {"0": {"start": {"line": 1}}, "1": {"start": {"line": 5}}, "2": {"start": {"line": 6}},
                             "3": {"start": {"line": 9}}},
            "s": {"0": 3, "1": 0, "2": 0, "3": 0}}}))
        (repo / "coverage.json").write_text(json.dumps({"files": {"src/m.py": {
            "missing_lines": [2, 3, 4, 10], "summary": {"percent_covered": 60.0}}}}))
        text = buckets.coverage_text(repo, {"src/a.ts": 20, "src/m.py": 20})
        self.assertIn("src/a.ts (25% of statements covered): lines 5-6, 9", text)
        self.assertIn("src/m.py (60% of statements covered): lines 2-4, 10", text)
        self.assertEqual(buckets.coverage_text(make_repo({"a.ts": "1\n"}), {"a.ts": 1}), "")


class FakeOllama:
    """A tiny stand-in for Ollama's /api/chat: thinks forever when asked to think, else answers."""

    def __init__(self):
        import http.server
        import threading as th
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    if body["think"]:
                        for i in range(10000):
                            self.wfile.write(json.dumps({"message": {"thinking": f"idea {i} "}}).encode() + b"\n")
                            self.wfile.flush()
                    else:
                        self.wfile.write(json.dumps({"message": {"content": "ANSWER"}}).encode() + b"\n")
                        self.wfile.write(json.dumps({"done": True, "done_reason": "stop", "eval_count": 1}).encode() + b"\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        th.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class ThinkingBudget(unittest.TestCase):
    def test_thinking_is_cut_at_the_budget_and_the_notes_are_passed_on(self):
        fake = FakeOllama()
        try:
            run = buckets.Run(make_repo({"a.ts": "1\n"}))
            cfg = {"url": fake.url, "model": "m", "num_ctx": 65536, "think": True, "think_budget": 25}
            answer = buckets.chat(cfg, run, "plan", "sys", "make a plan", max_tokens=1000, timeout=30)
        finally:
            fake.close()
        self.assertEqual(answer, "ANSWER")
        self.assertEqual([r["think"] for r in fake.requests], [True, False])
        follow_up = fake.requests[1]["messages"][1]["content"]
        self.assertTrue(follow_up.startswith("make a plan\n\nYour notes from thinking"))
        self.assertIn("idea 24", follow_up)
        self.assertNotIn("idea 30", follow_up)
        self.assertTrue(any(p.name.endswith("plan-thinking-cut.txt") for p in run.dir.iterdir()))


class FactsFirst(unittest.TestCase):
    def test_request_names_and_helpers(self):
        self.assertEqual(buckets.request_names("make `open()` and canAll( work in SubjectKind, not the db"),
                         ["open", "canAll", "SubjectKind"])
        decls = [{"name": "Box", "line": 1, "end": 20, "members": [{"name": "open", "line": 5, "end": 9}]}]
        self.assertEqual(buckets.enclosing(decls, 7)["name"], "open")
        self.assertEqual(buckets.enclosing(decls, 2)["name"], "Box")
        self.assertIsNone(buckets.enclosing(decls, 30))
        self.assertEqual(buckets.related_tests("src/conditions.ts", ["src/rule.ts", "test/ability.test.ts",
                                                                       "test/conditions.test.ts"]),
                         ["test/conditions.test.ts", "test/ability.test.ts"])

    def test_facts_from_failing_checks_and_request_names(self):
        repo = make_repo({"src/m.py": "def run(x):\n    y = x\n    return y + 1\n\n"
                                      "class Box:\n    def open(self, key):\n        return key\n"})
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local"}), repo,
                            "make `open` return the key upper-cased")
        b.checks_seen = "src/m.py:3: AssertionError: expected 2"
        facts = b.facts()
        self.assertIn("Where a check fails (src/m.py:3: AssertionError: expected 2)", facts)
        self.assertIn("    3| " + "    return y + 1", facts)
        self.assertIn("`open`, named in the request, is defined in src/m.py", facts)
        self.assertIn("    6|     def open(self, key):", facts)


class TargetedReading(unittest.TestCase):
    """Long files are read through their outline: the planner names the parts it needs, the coder is
    shown the code its task is about."""

    BIG = ("import os\n\n"
           + "".join(f"def filler_{i}(x):\n    return x + {i}\n\n\n" for i in range(120))
           + "class Box:\n    @property\n    def size(self):\n        return 3\n\n"
           + "    def _open(self, key):\n        return key.upper()\n")

    def setUp(self):
        self.repo = make_repo({"src/big.py": self.BIG, "src/small.py": "def tiny():\n    return 1\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "targeted_min_lines": 400})
        self.b = buckets.Buckets(cfg, self.repo, "make Box._open strip the key")
        self.b.on_windows = True                                         # no network: chores use the planner

    def test_request_names_include_snake_case(self):
        names = buckets.request_names("make GitAssistant.fill_in drop paths, next to "
                                      "test_missing_init in GitAssistantTests")
        for name in ("GitAssistant", "fill_in", "test_missing_init", "GitAssistantTests"):
            self.assertIn(name, names)

    def test_locate_symbols_and_ranges(self):
        files, outline = self.b.index.files, self.b.index.outline()
        size = buckets.locate(self.repo, files, outline, "Box.size")
        self.assertRegex(size, r"^src/big\.py:\d+-\d+$")
        start = int(size.split(":")[1].split("-")[0])
        self.assertEqual(self.BIG.splitlines()[start - 1].strip(), "@property")  # decorators included
        self.assertEqual(buckets.locate(self.repo, files, outline, "_open"),       # private methods too
                         buckets.locate(self.repo, files, outline, "src/big.py:Box._open"))
        self.assertEqual(buckets.locate(self.repo, files, outline, "10-12", "src/big.py"), "src/big.py:10-12")
        self.assertEqual(buckets.locate(self.repo, files, outline, "`tiny()`"), "src/small.py:1-2")
        self.assertIsNone(buckets.locate(self.repo, files, outline, "no_such_thing"))
        self.assertIsNone(buckets.locate(self.repo, files, outline, "nowhere.py:Box"))

    def test_long_files_are_read_through_their_outline(self):
        def fake(cfg, run, label, system, user, schema, validate, **kw):
            if label == "pick-files":
                return validate({"files": ["src/big.py", "src/small.py"]})
            self.assertEqual(label, "focus")
            self.assertRegex(user, r"\d+-\d+ Box._open")                    # the outline, with lines
            return validate({"read": ["Box._open", "Box.missing"]})
        b = buckets.Buckets(self.b.cfg, self.repo, "make opening a box strip the key")   # names no code
        b.on_windows = True
        b.survey = lambda *a: self.fail("nothing should need a survey")
        with mock.patch.object(buckets, "ask_json", fake), contextlib.redirect_stderr(io.StringIO()) as err:
            text = b.material("code")
        self.assertIn("return key.upper()", text)                         # the part it asked for
        self.assertIn("def tiny():", text)                                # short files stay whole
        self.assertNotIn("return x + 7", text)                            # the rest of the long file isn't
        self.assertIn("couldn't find: Box.missing", err.getvalue())

    def test_no_reading_when_the_facts_show_the_named_code(self):
        b = buckets.Buckets(self.b.cfg, self.repo, "make Box.size return 4")
        b.on_windows = True
        def fake(cfg, run, label, system, user, schema, validate, **kw):
            self.assertEqual(label, "pick-files")                         # no "focus" call
            return validate({"files": ["src/big.py"]})
        with mock.patch.object(buckets, "ask_json", fake), contextlib.redirect_stderr(io.StringIO()) as err:
            text = b.material("code")
        self.assertIn("the facts already show the code the request names", err.getvalue())
        self.assertIn("return 3", text)                                   # Box.size, from the facts

    def test_a_long_class_comes_as_its_outline_and_last_member(self):
        repo = make_repo({"k.py": "class K:\n    x = 1\n\n" + "".join(
            f"    def m{i}(self):\n        return {i}\n\n" for i in range(80))})
        b = buckets.Buckets(self.b.cfg, repo, "x")
        where = buckets.locate(repo, b.index.files, b.index.outline(), "K")
        text = b.part(where)
        self.assertIn("`K` is 242 lines; its members", text)
        self.assertIn("K.m40", text)                                      # in the outline
        self.assertIn("        return 79", text)                          # the last member, in full
        self.assertNotIn("        return 40", text)

    def test_the_coder_is_shown_its_code(self):
        task = buckets.validate_tasks([{"file": "src/big.py", "action": "edit", "instructions": "strip it",
                                        "done_when": "stripped", "where": "Box._open"}], self.repo, 8, "T")[0]
        self.assertEqual(task["where"], "Box._open")
        focus = self.b.task_focus(task)
        self.assertIn("has 489 lines", focus)
        self.assertIn("|         return key.upper()", focus)
        self.assertNotIn("return x + 7", focus)
        self.assertIn("return key.upper()", buckets.coder_prompt("goal", task, focus))
        self.assertIn("don't read it whole", self.b.task_focus({**task, "where": "", "instructions": "x"}))
        # No "where": the places the instructions name are shown instead.
        named = self.b.task_focus({**task, "where": "", "instructions": "In `Box._open` (line 489), strip the "
                                                                          "key, like filler_3 does."})
        self.assertIn("return key.upper()", named)
        self.assertIn("def filler_3(x):", named)
        self.assertNotIn("def filler_50(x):", named)
        self.assertEqual(self.b.task_focus({**task, "file": "src/small.py", "where": ""}), "")


class FasterRuns(unittest.TestCase):
    """Planned edits applied without a coder, thinking loops cut, facts that make reading needless,
    and checks that aren't repeated on the same code."""

    def setUp(self):
        self.repo = make_repo({"src/m.py": "def run(x):\n    y = x\n    return y + 1\n\n\n"
                                           "def other():\n    return 2\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "opencode_bin": "no-such-opencode",
                                                          "offload": {"escalate_repeated_failure": False}})
        self.b = buckets.Buckets(cfg, self.repo, "x")

    def task(self, edits, file="src/m.py", action="edit"):
        return buckets.validate_tasks([{"file": file, "action": action, "instructions": "do it",
                                        "done_when": "done", "where": "", "edits": edits}], self.repo, 8, "T")[0]

    def test_apply_edits(self):
        text = ("class Box:\n    def size(self):\n        return 3\n\n    def open(self, key):\n"
                "        return key\n\n\ndef run(x):\n    y = x\n    return y + 1\n")
        decls = buckets.py_decls_text
        # after a method, written flush left: indented like its neighbours, a blank line between
        out = buckets.apply_edits(text, [{"at": "Box.size", "mode": "after",
                                          "code": "def area(self):\n    return 9"}], decls)
        self.assertIn("        return 3\n\n    def area(self):\n        return 9\n\n    def open", out)
        # a whole function replaced by name
        out = buckets.apply_edits(text, [{"at": "run", "mode": "replace", "code": "def run(x):\n    return x"}], decls)
        self.assertTrue(out.endswith("def run(x):\n    return x\n"))
        # lines copied with the excerpt's numbers and without their indentation: still found
        out = buckets.apply_edits(text, [{"at": "   10| y = x", "mode": "replace", "code": "   10|     y = -x"}], decls)
        self.assertIn("    y = -x\n", out)
        # code that starts deeper than it later goes (the end of one method, then a new one): kept as written
        out = buckets.apply_edits(text, [{"at": "        return 3", "mode": "replace",
                                          "code": "        return 4\n\n    def zero(self):\n        return 0"}], decls)
        self.assertIn("        return 4\n\n    def zero(self):\n        return 0\n", out)
        # after a plain line (a dict entry): no blank line added
        out = buckets.apply_edits("X = {\n    'a': 1,\n    'c': 3,\n}\n", [{"at": "    'a': 1,", "mode": "after",
                                                                         "code": "    'b': 2,"}], decls)
        self.assertEqual(out, "X = {\n    'a': 1,\n    'b': 2,\n    'c': 3,\n}\n")
        # "after" a quoted def line (exact, or misremembered): after the whole method, never inside it
        for quote in ("    def size(self):", "    def size(self):\n        return 30"):
            out = buckets.apply_edits(text, [{"at": quote, "mode": "after", "code": "def area(self):\n    return 9"}],
                                      decls)
            self.assertIn("        return 3\n\n    def area(self):\n        return 9\n\n    def open", out)
        # a misremembered short quote can't replace the whole function
        with self.assertRaisesRegex(ValueError, "isn't a name or lines in the file"):
            buckets.apply_edits(text, [{"at": "def run(x):\n    y = 2", "mode": "replace", "code": "def run(x):"}], decls)
        # a bare line number or range: those lines
        self.assertIn("    y = x\n    return y + 1\n    print(y)",
                      buckets.apply_edits(text, [{"at": "11", "mode": "after", "code": "    print(y)"}], decls))
        with self.assertRaisesRegex(ValueError, "aren't in the file"):
            buckets.apply_edits(text, [{"at": "90-99", "mode": "replace", "code": "x"}], decls)
        with self.assertRaisesRegex(ValueError, "isn't a name or lines in the file"):
            buckets.apply_edits(text, [{"at": "Box.nothing", "mode": "after", "code": "x = 1"}], decls)
        with self.assertRaisesRegex(ValueError, "appears 2 times"):
            buckets.apply_edits("a\nb\na\n", [{"at": "a\n", "mode": "replace", "code": "c"}])

    def test_append_at_the_end_of_a_class_dict_or_braced_block(self):
        py = ("OPS = {\n    \"a\": (1, True),\n    \"b\": (\n        2, False)\n}\n\n\n"
              "class T:\n    def setUp(self):\n        self.x = 1\n\n    def test_a(self):\n        f(\n"
              "            1,\n        )\n\n\nX = 1\n")
        out = buckets.apply_edits(py, [{"at": "OPS", "mode": "append", "code": "\"c\": (3, False),"},
                                       {"at": "T", "mode": "append",   # a whitespace-only first line, as models write
                                        "code": "    \ndef test_b(self):\n    self.assertEqual(self.x, 1)\n  \n"}],
                                  buckets.py_decls_text)
        self.assertIn("        2, False),\n    \"c\": (3, False),\n}", out)        # comma added, before the brace
        self.assertIn("            1,\n        )\n\n    def test_b(self):\n        self.assertEqual(self.x, 1)\n\n\nX = 1",
                      out)                                                       # after the call's ")", not inside
        ast.parse(out)
        ts = "export class Box {\n  open(): void {\n    go();\n  }\n}\n"
        decls = lambda _t: [{"name": "Box", "line": 1, "end": 5, "signature": "class Box"}]
        out = buckets.apply_edits(ts, [{"at": "Box", "mode": "append", "code": "close(): void {\n  stop();\n}"}], decls)
        self.assertEqual(out, "export class Box {\n  open(): void {\n    go();\n  }\n\n  close(): void {\n"
                              "    stop();\n  }\n}\n")
        with self.assertRaisesRegex(ValueError, "append needs the name"):
            buckets.apply_edits(py, [{"at": "X = 1", "mode": "append", "code": "y"}], buckets.py_decls_text)

    def test_a_file_the_request_names_gets_a_task(self):
        (self.repo / "test_m.py").write_text("def test_run():\n    pass\n")
        b = buckets.Buckets(buckets.deep_merge(self.b.cfg, {"lookups": False}), self.repo,
                            "make run double x in src/m.py and add a test in test_m.py")
        b.on_windows, b.material = True, lambda mode: "material"
        task = {"file": "src/m.py", "action": "edit", "instructions": "x", "done_when": "y", "where": "", "edits": []}
        labels = []
        def fake(cfg, run, label, system, user, schema, validate, **kw):
            labels.append(label)
            tasks = [task] if label == "plan" else [task, {**task, "file": "test_m.py"}]
            return validate({"summary": "s", "tasks": tasks})
        with mock.patch.object(buckets, "ask_json", fake), contextlib.redirect_stderr(io.StringIO()) as err:
            plan = b.plan({}, {})
        self.assertEqual(labels, ["plan", "plan-complete"])
        self.assertEqual([t["file"] for t in plan["tasks"]], ["src/m.py", "test_m.py"])
        self.assertIn("no task for test_m.py", err.getvalue())

    def test_merge_from_the_chat_and_the_git_assistant(self):
        lines = iter(["buckets merge", 'buckets "fix it"', "last", "exit"])
        seen = []
        with mock.patch("builtins.input", lambda prompt="": next(lines)), \
                mock.patch.object(buckets, "main", lambda argv: seen.append(argv)), \
                contextlib.redirect_stderr(io.StringIO()):
            buckets.chat_loop([])
        self.assertEqual(seen, [["merge"], ["--", "fix it"], ["last"]])
        self.assertIn("run_merge", buckets.GIT_OPS)

    def test_planned_edits_skip_the_coder(self):
        task = self.task([{"at": "run", "mode": "after", "code": "def double(x):\n    return 2 * x"},
                          {"at": "    return y + 1", "mode": "replace", "code": "    return y + 2"}])
        with mock.patch.object(buckets, "run_proc", side_effect=AssertionError("no coder")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            result = self.b.code_task(task)
        self.assertEqual(result["status"], "done")
        self.assertIn("applied the planned edits directly", err.getvalue())
        text = (self.repo / "src/m.py").read_text()
        self.assertIn("    return y + 2\n\n\ndef double(x):\n    return 2 * x\n\n\ndef other", text)
        self.assertEqual(buckets.git(self.repo, "status", "--porcelain").strip(), "")    # committed

    def test_edits_that_dont_fit_go_to_the_coder(self):
        calls = []
        def fake_proc(cmd, cwd, timeout, **kw):
            calls.append(cmd)
            return 0, "DONE: nothing", False
        for edits in ([{"at": "nope", "mode": "after", "code": "x = 1"}],                  # not in the file
                      [{"at": "run", "mode": "replace", "code": "def run(x):\n    return (x +"}]):  # doesn't parse
            with mock.patch.object(buckets, "run_proc", fake_proc), contextlib.redirect_stderr(io.StringIO()) as err:
                self.b.code_task(self.task(edits))
            self.assertIn("the planned edits don't apply", err.getvalue())
        self.assertEqual(len(calls), 2 * self.b.cfg["coder"]["attempts"])
        self.assertIn("--standalone", calls[0])
        self.assertIn("return y + 1", (self.repo / "src/m.py").read_text())   # untouched

    def test_a_new_file_from_one_edit(self):
        task = self.task([{"at": "", "mode": "replace", "code": "def new():\n    return 3\n"}], "src/n.py", "create")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.b.code_task(task)["status"], "done")
        self.assertEqual((self.repo / "src/n.py").read_text(), "def new():\n    return 3\n")

    def test_thinking_loops(self):
        varied = " ".join(f"Step {i} looks at a different part of the work, number {i * 7}." for i in range(70))
        self.assertFalse(buckets.thinking_loops(varied))
        circle = varied[:3200] + " Wait, I need to double check whether the step drops the paths here. " * 4
        self.assertTrue(buckets.thinking_loops(circle))
        # quoting the same line of code again while reasoning about it isn't a loop
        quoted = varied[:3200] + "".join(f"\nLooking at it again:\n    print(f\"Reviewing: {{d['request']}}\")\nAttempt {i} checks scope." for i in range(4))
        self.assertFalse(buckets.thinking_loops(quoted))

    def test_checks_arent_repeated_on_the_same_code(self):
        counter = Path(tempfile.mkdtemp()) / "n"
        checks = {"test": f"echo x >> {counter}"}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            first = buckets.run_checks(self.repo, checks, self.b.run, "a", 60)
            second = buckets.run_checks(self.repo, checks, self.b.run, "b", 60)
        self.assertEqual(counter.read_text().count("x"), 1)
        self.assertEqual(first, second)
        self.assertIn("cached: same code as an earlier run", err.getvalue())
        (self.repo / "src/m.py").write_text("changed\n")                   # not committed: run it
        with contextlib.redirect_stderr(io.StringIO()):
            buckets.run_checks(self.repo, checks, self.b.run, "c", 60)
        self.assertEqual(counter.read_text().count("x"), 2)


class ProjectMemoryTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo({
            "pkg/__init__.py": "",
            "pkg/core.py": '"""The core: parses requests. More text."""\nfrom . import util\n\n\nclass Parser:\n'
                           '    def parse(self):\n        return util.clean()\n',
            "pkg/util.py": "def clean():\n    return 1\n",
            "web/app.ts": "// The web entry point.\nimport { x } from './lib';\nexport const y = x;\n",
            "web/lib.ts": "export const x = 1;\n"})
        self.b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local"}), self.repo, "x")

    def test_the_map_is_mechanical_and_follows_the_files(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            mem = self.b.memory
        self.assertIn("started .buckets/ here", err.getvalue())
        core = mem.files["pkg/core.py"]
        self.assertEqual(core["purpose"], "The core: parses requests.")         # from the docstring
        self.assertEqual(core["declares"], ["Parser"])
        self.assertEqual(core["imports"], ["pkg/util.py"])
        self.assertEqual(mem.files["web/app.ts"]["imports"], ["web/lib.ts"])
        self.assertEqual(mem.files["web/app.ts"]["purpose"], "The web entry point.")
        text = mem.render()
        self.assertIn("- pkg/util.py (2 lines) Declares clean. Used by pkg/core.py.", text)
        self.assertIn("pkg/core.py", (self.repo / ".buckets/map.md").read_text())
        self.assertEqual((self.repo / ".buckets/.gitignore").read_text(), "journal.md\n")
        self.assertEqual(buckets.changed_paths(self.repo), [])                  # its own notes don't block runs
        # a model-written purpose survives until... the file changes and gets a docstring
        mem.files["pkg/util.py"].update(purpose="Small helpers.", source="model")
        mem.save()
        (self.repo / "pkg/util.py").write_text("def clean():\n    return 2\n")
        mem2 = buckets.ProjectMemory(self.repo)
        self.assertEqual(mem2.refresh(self.b.index.files, {}), 1)
        self.assertEqual(mem2.files["pkg/util.py"]["purpose"], "Small helpers.")

    def test_the_map_fits_its_budget(self):
        mem = buckets.ProjectMemory(self.repo)
        mem.refresh(self.b.index.files, {})
        self.assertIn("Declares", mem.render(10 ** 6))
        self.assertNotIn("Declares", mem.render(60))                           # shorter lines first
        folders = mem.render(25, focus=["web/app.ts"])
        self.assertIn("- pkg/ (3 files", folders)                              # other folders, one line each
        self.assertIn("- web/app.ts", folders)                                 # the focus folder, file by file

    def test_the_journal_and_learning(self):
        mem = buckets.ProjectMemory(self.repo)
        mem.refresh(self.b.index.files, {})
        for i in range(25):
            mem.note("code", "done", f"request {i}", ["pkg/core.py"] if i == 10 else [], f"summary {i}")
        entries = mem.journal_entries()
        self.assertEqual(len(entries), 20)                                     # the latest 20 are kept
        recent = mem.recent()
        self.assertIn("request 24", recent)
        self.assertNotIn("request 10", recent)
        self.assertNotIn("request 4\n", "\n".join(entries))                  # dropped: only 20 are kept
        self.assertIn("request 22", recent)
        self.assertIn("request 10\n", mem.recent(["pkg/core.py"]))             # older, but it touched that file
        self.b.run.talked = True
        self.b.on_windows = True
        def fake(cfg, run, label, system, user, schema, validate, **kw):
            self.assertRegex(user, r"=== pkg/util.py \(\d+ lines; declares: clean\) ===")  # first lines, not the file
            return validate({"files": [{"file": "pkg/util.py", "purpose": "Helpers for the core. Extra."}]})
        with mock.patch.object(buckets, "ask_json", fake), contextlib.redirect_stderr(io.StringIO()):
            mem.learn(self.b, ["pkg/util.py", "pkg/core.py"])                   # core already has a docstring
        self.assertEqual(buckets.ProjectMemory(self.repo).files["pkg/util.py"]["purpose"], "Helpers for the core.")

    def test_the_planner_gets_recent_work_and_the_map(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.b.memory.note("code", "done", "add Parser.parse", ["pkg/core.py"], "Parser parses now.")
            ctx = self.b.context()
        self.assertIn("Recent work in this repo", ctx)
        self.assertIn("Asked: add Parser.parse", ctx)
        self.assertIn("Project map (from .buckets/map.md)", ctx)
        self.assertNotIn("Project map", self.b.context(with_map=False))


class FakeLlamaServer:
    """A stand-in for llama.cpp's server: /v1/models, and /v1/chat/completions streamed as SSE with
    reasoning_content, content and timings. Ollama's /api/tags isn't there (404)."""

    def __init__(self, thoughts: int = 5):
        import http.server
        import threading as th
        self.bodies = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                payload = json.dumps({"object": "list", "data": [{"id": "big:35b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.bodies.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = []
                if body["chat_template_kwargs"]["enable_thinking"]:
                    chunks += [{"choices": [{"delta": {"reasoning_content": f"step {i}. "}}]} for i in range(thoughts)]
                answer = json.dumps({"ok": True})
                chunks += [{"choices": [{"delta": {"content": answer[:5]}}]},
                           {"choices": [{"delta": {"content": answer[5:]}, "finish_reason": "stop"}],
                            "timings": {"prompt_n": 120, "prompt_ms": 100.0, "predicted_n": 7,
                                        "predicted_ms": 200.0, "cache_n": 30}}]
                for c in chunks:
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        th.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class LlamaCppServer(unittest.TestCase):
    def setUp(self):
        self.run = buckets.Run(make_repo({"a.py": "x = 1\n"}))

    def test_a_call_with_thinking_and_a_schema(self):
        fake = FakeLlamaServer()
        try:
            cfg = {"url": fake.url, "model": "big:35b", "api": "llamacpp", "num_ctx": 65536, "think": True}
            schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            with contextlib.redirect_stderr(io.StringIO()):
                got = buckets.ask_json(cfg, self.run, "plan", "sys", "go", schema, lambda d: d)
                self.assertEqual(got, {"ok": True})
                self.assertEqual(buckets.server_models(fake.url), {"big:35b"})
        finally:
            fake.close()
        sent = fake.bodies[0]
        self.assertTrue(sent["stream"])
        self.assertTrue(sent["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(sent["response_format"]["json_schema"]["schema"], schema)  # applies after the thinking
        saved = json.loads(next(self.run.dir.glob("*plan.json")).read_text())
        self.assertEqual(saved["thinking"], "step 0. step 1. step 2. step 3. step 4. ")
        self.assertEqual(saved["stats"]["prompt_eval_count"], 120)
        self.assertEqual(saved["stats"]["prompt_eval_duration"], 100_000_000)  # ms -> ns, like Ollama's
        self.assertEqual(saved["stats"]["prompt_eval_cached_count"], 30)

    def test_the_thinking_budget_and_the_schema_after_a_cut(self):
        fake = FakeLlamaServer(thoughts=50)
        try:
            cfg = {"url": fake.url, "model": "big:35b", "api": "llamacpp", "num_ctx": 65536, "think": True,
                   "think_budget": 10}
            schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
            with contextlib.redirect_stderr(io.StringIO()) as err:
                got = buckets.ask_json(cfg, self.run, "plan", "sys", "go", schema, lambda d: d)
        finally:
            fake.close()
        self.assertEqual(got, {"ok": True})
        self.assertIn("thought for 10 tokens (the budget)", err.getvalue())
        again = fake.bodies[-1]
        self.assertFalse(again["chat_template_kwargs"]["enable_thinking"])
        self.assertEqual(again["response_format"]["json_schema"]["schema"], schema)   # enforced now
        self.assertIn("Your notes from thinking", again["messages"][1]["content"])
        buckets.unload(cfg)                                      # a no-op for llama.cpp, no error


class BigRepoTools(unittest.TestCase):
    """What the first big-repo run (a 2,000-file React Native monorepo) needed."""

    def test_request_words_find_paths_and_code(self):
        repo = make_repo({
            "app/screens/order-details/screen.tsx": 'export const S = () => <Row title={"Original ETA"} />\n',
            "app/models/order-details/model.ts": "export const M = { createdAt: 1, originalEta: 2 }\n",
            "app/screens/invoice/screen.tsx": "export const I = 1\n"})
        self.assertEqual(buckets.word_runs("Does order details have created at? Hide `x` and a/b.c"),
                         [["order", "details"], ["created", "at"]])
        self.assertEqual(buckets.spellings(["created", "at"]), ["created-at", "created_at", "createdAt", "CreatedAt"])
        leads = buckets.request_leads(repo, "Hide the Original ETA from the order details",
                                      buckets.list_files(repo, []))
        self.assertIn('Paths spelling "order details" (2 files)', leads)
        self.assertNotIn("invoice", leads)
        self.assertIn('Code and text matching "original eta"', leads)
        self.assertIn('screen.tsx:1: export const S = () => <Row title={"Original ETA"} />', leads)   # UI text
        self.assertIn("model.ts:1: export const M = { createdAt: 1, originalEta: 2 }", leads)            # identifier

    def test_checks_that_rewrite_files_arent_detected(self):
        repo = make_repo({"package.json": json.dumps({"workspaces": ["apps/*"], "scripts": {
            "lint": "yarn workspaces foreach --all run lint", "test": "jest"}}),
            "yarn.lock": "", "apps/a/package.json": json.dumps({"scripts": {"lint": "eslint . --fix && yarn format"}})})
        self.assertEqual(buckets.detect_checks(repo, {}), {"test": "yarn run test"})

    def test_fixers_in_workspace_packages(self):
        repo = make_repo({"package.json": json.dumps({"workspaces": ["packages/*"]}),
                          "packages/shared/package.json": json.dumps({"prettier": {"semi": False}}),
                          "packages/shared/eslint.config.js": "",
                          "node_modules/.bin/eslint": "", "node_modules/prettier/bin/prettier.cjs": ""})
        fixers = buckets.detect_fixers(repo, {})
        self.assertEqual(fixers, [
            "cd packages/shared && ../../node_modules/.bin/eslint --fix --quiet --no-warn-ignored {js_files@packages/shared}",
            "cd packages/shared && node ../../node_modules/prettier/bin/prettier.cjs --write --ignore-unknown {files@packages/shared}"])
        self.assertEqual(buckets.fill_fixer(fixers[1], ["packages/shared/app/a.tsx", "apps/x/b.ts"]),
                         "cd packages/shared && node ../../node_modules/prettier/bin/prettier.cjs --write --ignore-unknown app/a.tsx")
        self.assertIsNone(buckets.fill_fixer(fixers[0], ["README.md"]))
        self.assertEqual([buckets.fixer_name(f) for f in fixers], ["eslint", "prettier"])

    def test_leftover_blank_lines_go_and_nothing_else_moves(self):
        repo = make_repo({"s.tsx": "<A>\n  <Row\n    x={1}\n  />\n  <B />\n\n\n  <C />\n</A>\n"})
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        (repo / "s.tsx").write_text("<A>\n\n  <B />\n\n\n  <C />\n</A>\n")        # a row removed, a blank left
        commit_all(repo, "remove a row")
        self.assertTrue(buckets.drop_leftover_blanks(repo, base, "s.tsx"))
        self.assertEqual((repo / "s.tsx").read_text(), "<A>\n  <B />\n\n\n  <C />\n</A>\n")  # its own blanks kept

    def test_the_review_sees_the_whole_changed_function(self):
        body = "def review(repo):\n    d = find(repo)\n" + "".join(f"    x{i} = {i}\n" for i in range(12)) + "    return d\n"
        repo = make_repo({"m.py": body + "\n\ndef other():\n    return 1\n"})
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        (repo / "m.py").write_text(body.replace("    return d\n", "    print(d['request'])\n    return d\n")
                                   + "\n\ndef other():\n    return 1\n")
        commit_all(repo, "print the request")
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False}), repo, "x")
        text = b.changed_code(base)
        self.assertIn("    2|     d = find(repo)", text)              # where d is set, 13 lines above the change
        self.assertNotIn("def other", text)

    def test_the_review_sees_where_the_requests_phrases_still_are(self):
        repo = make_repo({"s.tsx": '<Row title={"Original ETA"} />\n<Row title={"Tracking Code"} />\n'})
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        (repo / "s.tsx").write_text('<Row title={"Tracking Code"} />\n')
        commit_all(repo, "half done")
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False}), repo,
                            "Hide the Original ETA and Tracking Code fields")
        self.assertIn('"tracking code" still in s.tsx:1', b.phrases_now(base))
        self.assertNotIn("original eta", b.phrases_now(base))

    def test_edit_anchors_given_as_paths_and_coders_shown_quoted_text(self):
        src = '<A>\n  <Row title={"x"} />\n</A>\n'
        with self.assertRaisesRegex(ValueError, "empty"):
            buckets.apply_edits(src, [{"at": "app/screens/s.tsx", "mode": "after", "code": "y"}], buckets.py_decls_text)
        self.assertEqual(buckets.apply_edits(src, [{"at": 'app/s.tsx:  <Row title={"x"} />', "mode": "replace",
                                                    "code": '  <Row title={"z"} />'}], None), src.replace('"x"', '"z"'))
        repo = make_repo({"s.tsx": "".join(f"<Row n={i} />\n" for i in range(60)) + '<Row title={"Original ETA"} />\n'})
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False}), repo, "x")
        focus = b.task_focus({"file": "s.tsx", "action": "edit", "where": "", "edits": [],
                              "instructions": 'Remove the DetailRow with title "Original ETA".'})
        self.assertIn('61| <Row title={"Original ETA"} />', focus)

    def test_long_agent_docs_and_skills_are_cut_to_what_the_request_needs(self):
        doc = "# AGENTS\nIntro.\n" + "".join(f"## Topic {i}\n" + "filler words here. " * 40 + "\n" for i in range(30))
        doc += "## Orders\nThe order details screen lives in screens/order-details.\n"
        repo = make_repo({"AGENTS.md": doc, "CLAUDE.md": "@AGENTS.md\n",
                          ".github/skills/order-details/SKILL.md": "# Skill\nHow details rows work.\n",
                          ".github/skills/linting/SKILL.md": "# Lint\n"})
        g = buckets.guidance(repo, {}, "Hide a field from the order details")
        self.assertIn("--- AGENTS.md (the parts that match this request) ---", g)
        self.assertIn("The order details screen lives in", g)              # the matching section
        self.assertIn("Other sections, not shown: Topic 0", g)
        self.assertIn("--- .github/skills/order-details/SKILL.md", g)    # the skill its words spell
        self.assertNotIn("skills/linting", g)
        self.assertLess(len(g), 9000)

    def test_related_repos_are_searched_and_read_never_changed(self):
        api = make_repo({"src/filter/workorder.list.config.ts": "export const C = [{ filterKey: 'priorityLevel' }]\n"})
        repo = make_repo({"app/screens/order-filters/screen.tsx": "export const S = 1\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, repo, "add the priority level to the order filters")
        b.rc["related"] = {"api": str(api)}
        facts = b.facts()
        self.assertIn("in `api` (a related repo", facts)
        self.assertIn("api:src/filter/workorder.list.config.ts:1:", facts)
        self.assertEqual(buckets.claude_command({"bin": "claude", "budget_usd": 1}, "sonnet", None, True, [api])[-2:],
                         ["--add-dir", str(api)])

    def test_a_claude_review_is_suggested_when_the_change_has_the_risk_signs(self):
        repo = make_repo({"a.ts": "1\n", "b.ts": "1\n"})
        buckets.git(repo, "switch", "-q", "-c", "buckets/x")
        for f in ("a.ts", "b.ts", "c.ts"):
            (repo / f).write_text("2\n")
        commit_all(repo, "change")
        main = "master" if buckets.branch_exists(repo, "master") else "main"
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False}), repo, "x")
        with mock.patch.object(buckets, "claude_ready", lambda cfg: True):
            why = buckets.review_reasons(b, True, "buckets/x", main)
            self.assertEqual(why, "it adds 1 new file, 3 files that have to fit together")
            b.mode = "claude"
            self.assertEqual(buckets.review_reasons(b, True, "buckets/x", main), "")       # Claude planned it already
        with mock.patch.object(buckets, "claude_ready", lambda cfg: False):
            b.mode = "local"
            self.assertEqual(buckets.review_reasons(b, False, "buckets/x", main), "")      # no Claude to suggest

    def test_buckets_review_reads_the_branch_in_its_own_worktree(self):
        repo = make_repo({"a.ts": "1\n"})
        start = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        buckets.git(repo, "switch", "-q", "-c", "buckets/x")
        (repo / "a.ts").write_text("2\n")
        commit_all(repo, "change")
        buckets.git(repo, "switch", "-q", start)
        run = buckets.Run(repo)
        (run.dir / "meta.json").write_text(json.dumps({"repo": str(repo), "kind": "code", "request": "make a 2",
                                                       "done": True, "branch": "buckets/x", "merge_into": start}))
        seen = {}
        def fake(cfg, run, label, prompt, where, **kw):
            seen.update(prompt=prompt, a=(where / "a.ts").read_text())
            return "Verdict: looks good", 0.12
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {})
        with mock.patch.object(buckets, "claude_call", fake), mock.patch.object(buckets, "claude_ready", lambda c: True), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(buckets.cmd_review(cfg, repo), 0)
        self.assertEqual(seen["a"], "2\n")                     # it read the branch's code, not the checkout's
        self.assertIn("make a 2", seen["prompt"])
        self.assertIn("-1\n+2", seen["prompt"])
        self.assertIn("Verdict: looks good", out.getvalue())
        self.assertEqual(buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), start)
        self.assertNotIn("buckets-review", buckets.git(repo, "worktree", "list"))

    def test_buckets_review_prints_run_info_before_claude(self):
        repo = make_repo({"a.ts": "1\n"})
        start = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        buckets.git(repo, "switch", "-q", "-c", "buckets/y")
        (repo / "a.ts").write_text("2\n")
        commit_all(repo, "change")
        buckets.git(repo, "switch", "-q", start)
        run = buckets.Run(repo)
        long_request = "x" * 150
        (run.dir / "meta.json").write_text(json.dumps({"repo": str(repo), "kind": "code", "request": long_request,
                                                        "done": True, "branch": "buckets/y", "merge_into": start}))
        def fake(cfg, run, label, prompt, where, **kw):
            return "Verdict: looks good", 0.12
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {})
        with mock.patch.object(buckets, "claude_call", fake), mock.patch.object(buckets, "claude_ready", lambda c: True), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(buckets.cmd_review(cfg, repo), 0)
        output = out.getvalue()
        self.assertIn(long_request[:100], output)
        self.assertIn("buckets/y", output)

    def test_a_type_and_a_value_with_one_name_arent_an_api_change(self):
        b = buckets.Buckets.__new__(buckets.Buckets)
        surface = {"h.tsx": [{"kind": "interface", "name": "H", "signature": "interface H"},
                             {"kind": "const", "name": "H", "signature": "const H = () =>"}]}
        b.surface_before, b.repo = surface, make_repo({"a.txt": "x"})
        b._index = mock.Mock(surface=lambda: {"h.tsx": list(reversed(surface["h.tsx"]))}, uses=lambda *a: {"uses": []})
        self.assertEqual(b.api_impact("HEAD"), {"removed": [], "changed": [], "added": [], "untouched": []})


class Lookups(unittest.TestCase):
    """Planning as a conversation: the planner reads, searches and asks the reader machine before
    it answers with the plan."""

    def setUp(self):
        self.repo = make_repo({"src/m.py": "def run(x):\n    return x + 1\n\n\ndef other():\n    return run(2)\n",
                               "src/n.py": "from src.m import run\n\nVALUE = run(3)\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        self.b = buckets.Buckets(cfg, self.repo, "make run add 2")
        self.b.on_windows = True

    def test_the_planner_looks_things_up_then_plans(self):
        turns, asked = [], []
        plan = {"summary": "s", "tasks": [{"file": "src/m.py", "action": "edit", "instructions": "x", "done_when": "y",
                                           "where": "run", "edits": []}]}
        def fake_turn(cfg, run, label, messages, **kw):
            turns.append((label, [m["role"] for m in messages], kw.get("tools") is not None))
            if len(turns) == 1:
                return "", "", [{"id": "1", "name": "read", "arguments": {"what": "run"}},
                                {"id": "2", "name": "search", "arguments": {"pattern": "run\\("}},
                                {"id": "3", "name": "ask", "arguments": {"question": "Who calls run?",
                                                                          "files": ["src/n.py", "nope.py"]}}], False
            tool_results = {m["name"]: m["content"] for m in messages if m["role"] == "tool"}
            self.assertIn("    1| def run(x):", tool_results["read"])              # exact code
            self.assertIn("src/n.py:3:VALUE = run(3)", tool_results["search"])      # git grep
            self.assertIn("src/n.py:3", tool_results["ask"])                        # the reader's answer...
            self.assertIn("VALUE = run(3)", tool_results["ask"])                    # ...with the real line attached
            return json.dumps(plan), "", [], False
        def fake_chat(cfg, run, label, system, user, **kw):
            asked.append(user)
            return "- `src/n.py:3`: VALUE = run(3) calls it"
        with mock.patch.object(buckets, "chat_turn", fake_turn), mock.patch.object(buckets, "chat", fake_chat), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            got = self.b.plan_with_lookups("PLAN PROMPT", lambda d: buckets.validate_plan(d, self.repo, 8))
        self.assertEqual(got["tasks"][0]["file"], "src/m.py")
        self.assertEqual([t[0] for t in turns], ["plan-1", "plan-2"])
        self.assertEqual(turns[1][1], ["system", "user", "assistant", "tool", "tool", "tool"])
        self.assertIn("=== src/n.py ===", asked[0])                                  # the reader got the file
        self.assertNotIn("nope.py", asked[0])
        self.assertIn("answered: Who calls run?", err.getvalue())

    def test_a_cut_thought_and_a_bad_answer_end_in_the_forced_plan(self):
        seen = []
        def fake_turn(cfg, run, label, messages, **kw):
            seen.append((label, kw.get("think"), kw.get("schema") is not None))
            if label == "plan-1":
                return "", "my notes", [], True                                    # thinking cut
            if label == "plan-2":
                return "not json at all", "", [], False
            return json.dumps({"summary": "s", "tasks": []}), "", [], False
        with mock.patch.object(buckets, "chat_turn", fake_turn), contextlib.redirect_stderr(io.StringIO()):
            got = self.b.plan_with_lookups("P", lambda d: buckets.validate_plan(d, self.repo, 8))
        self.assertEqual(got["tasks"], [])
        self.assertEqual(seen, [("plan-1", True, False), ("plan-2", False, False), ("plan-final", False, True)])

    def test_a_plan_rejected_twice_stops_with_the_reason(self):
        bad = json.dumps({"summary": "s", "tasks": [{"file": "/abs.py", "action": "edit", "instructions": "x",
                                                     "done_when": "", "edits": []}]})
        seen = []
        def fake_turn(cfg, run, label, messages, **kw):
            seen.append(label)
            return bad, "", [], False
        with mock.patch.object(buckets, "chat_turn", fake_turn), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(buckets.ModelError, "rejected too .*must be relative"):
                self.b.plan_with_lookups("P", lambda d: buckets.validate_plan(d, self.repo, 8))
        self.assertEqual(seen[-2:], ["plan-final", "plan-final-retry"])

    def test_tool_calls_on_the_wire(self):
        history = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read", "arguments": {"what": "x"}}]},
                   {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "code"}]
        ollama = buckets.wire_messages(history, "ollama")
        self.assertEqual(ollama[0]["tool_calls"], [{"function": {"name": "read", "arguments": {"what": "x"}}}])
        self.assertEqual(ollama[1], {"role": "tool", "content": "code", "tool_name": "read"})
        openai = buckets.wire_messages(history, "openai")
        self.assertEqual(openai[0]["tool_calls"][0]["function"]["arguments"], '{"what": "x"}')
        self.assertEqual(openai[1]["tool_call_id"], "c1")


class ReviewSkip(unittest.TestCase):
    BASE = {"typecheck": {"ok": False}, "test": {"ok": True}}
    NOW = {"typecheck": {"ok": True}, "test": {"ok": True}}
    DONE = [{"status": "done"}]

    def test_settles_only_when_everything_lines_up(self):
        self.assertTrue(buckets.checks_settle_job(self.DONE, self.BASE, self.NOW, [], None))
        self.assertFalse(buckets.checks_settle_job(self.DONE, self.NOW, self.NOW, [], None))   # nothing was failing
        self.assertFalse(buckets.checks_settle_job(self.DONE, self.BASE, self.BASE, [], None))  # still failing
        self.assertFalse(buckets.checks_settle_job([{"status": "failed"}], self.BASE, self.NOW, [], None))
        self.assertFalse(buckets.checks_settle_job(self.DONE, self.BASE, self.NOW, ["`a.ts` uses `any`"], None))
        self.assertFalse(buckets.checks_settle_job(self.DONE, self.BASE, self.NOW, [], {"untouched": ["x"]}))
        self.assertFalse(buckets.checks_settle_job([], self.BASE, self.NOW, [], None))


class AgentRules(unittest.TestCase):
    def test_every_section_heading_and_one_rule_each(self):
        long_intro = "Background. " * 600
        doc = (f"# AGENTS.md\n{long_intro}\n## Localization: strings live in `localization/*.json`\nSome context here.\n"
               "Never hard-code user-facing text in a component.\n\n## Reuse before you build\nOnly create a new util "
               "when nothing existing covers it.\n```ts\nnever = 'in code'\n```\n## Release builds\nUse the script.\n")
        repo = make_repo({"AGENTS.md": doc})
        rules = buckets.agent_rules(repo)
        self.assertIn("- Localization: strings live in localization/*.json: Never hard-code user-facing text in a component.", rules.replace("`", ""))
        self.assertIn("- Reuse before you build: Only create a new util when nothing existing covers it.", rules)
        self.assertIn("- Release builds", rules)
        self.assertNotIn("in code", rules)
        g = buckets.guidance(repo, {}, "add a field")
        self.assertIn("every section's rules, in short", g)
        self.assertEqual(buckets.agent_rules(make_repo({"README.md": "x"})), "")


class Localization(unittest.TestCase):
    def test_new_strings_without_a_locale_entry(self):
        repo = make_repo({"app/localization/en_us.json": '{"SAVE": "Save", "REMEMBER ME": "Remember me"}',
                          "app/localization/fr_ca.json": '{"SAVE": "Enregistrer", "REMEMBER ME": "Souviens-toi"}',
                          "app/screen.tsx": "export const S = () => null\n"})
        (repo / "app/screen.tsx").write_text('export const S = () => (\n  <>\n    <Button text="Save" />\n'
                                             '    <Text>Pick a vendor</Text>\n    <Text text="Remember me" testID="remember_me" />\n'
                                             '    {translate("Not found yet")}\n  </>\n)\n')
        (repo / "app/localization/en_us.json").write_text('{"SAVE": "Save", "REMEMBER ME": "Remember me", "PICK A VENDOR": "Pick a vendor"}')
        diff = buckets.git(repo, "diff")
        gaps = buckets.i18n_gaps(repo, diff, buckets.git(repo, "ls-files").split())
        self.assertEqual(len(gaps), 2, gaps)
        self.assertIn('shows "Not found yet" but `app/localization/en_us.json` has no "NOT FOUND YET" entry', gaps[0])
        self.assertIn('`app/localization/fr_ca.json` lacks 1 key(s) added to `app/localization/en_us.json`: "PICK A VENDOR"', gaps[1])
        moved = ("--- a/app/a.tsx\n+++ b/app/a.tsx\n-    <Text>Old untranslated words</Text>\n"
                 "--- /dev/null\n+++ b/app/shared.tsx\n+    <Text>Old untranslated words</Text>\n")
        self.assertEqual(buckets.i18n_gaps(repo, moved, buckets.git(repo, "ls-files").split()), [])


class RedCI(unittest.TestCase):
    LOG = ("lint\tRun yarn lint\t2026-10-11T01:02:03.4567890Z ##[group]Run yarn lint\n"
           "lint\tRun yarn lint\t2026-10-11T01:02:04.0000000Z src/a.ts(12,3): error TS2304: Cannot find name 'x'.\n"
           "lint\tRun yarn lint\t2026-10-11T01:02:05.0000000Z \x1b[31mError: Process completed with exit code 2.\x1b[0m\n")

    def test_the_failed_run_becomes_check_output(self):
        repo = make_repo({"src/a.ts": "export const a = 1\n"})
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"memory": False}), repo, "CI is red, fix it")
        head = buckets.git(repo, "rev-parse", "HEAD").strip()
        runs = json.dumps([{"databaseId": 7, "conclusion": "failure", "status": "completed", "name": "lint",
                            "headSha": head, "displayTitle": "fix things", "url": "https://x/runs/7"}])
        def fake_gh(self, *args, check=True):
            return runs if args[:2] == ("run", "list") else self_log
        self_log = self.LOG
        with mock.patch.object(buckets.GitAssistant, "gh", fake_gh), contextlib.redirect_stderr(io.StringIO()):
            got = b.ci_failures()
        self.assertIn("CI run that failed: lint: fix things (https://x/runs/7)\n--- lint / Run yarn lint ---", got)
        self.assertIn("src/a.ts(12,3): error TS2304: Cannot find name 'x'.", got)
        self.assertIn("Error: Process completed with exit code 2.", got)
        self.assertNotIn("2026-10-11T", got)
        self.assertNotIn("##[group]", got)
        self.assertTrue(buckets.CI_WORDS.search("the pipeline is failing"))
        self.assertFalse(buckets.CI_WORDS.search("add a city field"))


class FollowUps(unittest.TestCase):
    """A follow-up request starts from the previous run's unmerged branch, so its work is there."""

    def setUp(self):
        self.repo = make_repo({"src/cart.ts": "export const total = 1\n", "src/user.ts": "export const name = 'a'\n"})
        self.main = buckets.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        buckets.git(self.repo, "switch", "-q", "-c", "buckets/add-tax")
        (self.repo / "src/cart.ts").write_text("export const total = 1\nexport const tax = 2\n")
        commit_all(self.repo, "add tax")
        buckets.git(self.repo, "switch", "-q", self.main)

    def record(self, minutes_ago=5, branch="buckets/add-tax"):
        run = buckets.Run(self.repo)
        when = (datetime.now() - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        (run.dir / "meta.json").write_text(json.dumps({"repo": str(self.repo), "kind": "code", "request": "add a tax to the cart",
                                                       "done": True, "branch": branch, "merge_into": self.main, "finished": when}))

    def test_what_counts_as_a_follow_up(self):
        self.record()
        got = buckets.earlier_unmerged(self.repo, "now round the tax in src/cart.ts")
        self.assertEqual((got["branch"], got["overlap"]), ("buckets/add-tax", ["cart", "cart.ts", "src/cart.ts"]))
        self.assertIsNotNone(buckets.earlier_unmerged(self.repo, "also show it in the header"))   # follow-up words, recent
        self.assertIsNone(buckets.earlier_unmerged(self.repo, "rename the user module"))         # unrelated
        self.record(minutes_ago=300)
        self.assertIsNone(buckets.earlier_unmerged(self.repo, "also show it in the header"))     # too long ago
        self.assertIsNotNone(buckets.earlier_unmerged(self.repo, "fix cart.ts"))                 # names its file

    def test_merged_or_elsewhere_is_not_built_on(self):
        self.record()
        buckets.git(self.repo, "merge", "-q", "--ff-only", "buckets/add-tax")
        self.assertIsNone(buckets.earlier_unmerged(self.repo, "fix cart.ts"))                    # nothing unmerged

    def test_merge_tidies_the_branch_it_built_on(self):
        self.record()
        buckets.git(self.repo, "switch", "-q", "-c", "buckets/round-tax", "buckets/add-tax")
        (self.repo / "src/cart.ts").write_text("export const total = 1\nexport const tax = 2.0\n")
        commit_all(self.repo, "round")
        buckets.git(self.repo, "switch", "-q", self.main)
        run = buckets.Run(self.repo)
        (run.dir / "meta.json").write_text(json.dumps({"repo": str(self.repo), "kind": "code", "request": "round the tax",
                                                       "done": True, "branch": "buckets/round-tax", "merge_into": self.main,
                                                       "builds_on": "buckets/add-tax"}))
        os.utime(run.dir / "meta.json")
        msg = buckets.merge_run(self.repo)
        self.assertIn("buckets/add-tax, which it built on, is in too and was deleted", msg)
        self.assertIn("tax = 2.0", (self.repo / "src/cart.ts").read_text())
        self.assertFalse(buckets.branch_exists(self.repo, "buckets/add-tax"))


class RoundRollback(unittest.TestCase):
    def test_a_round_that_breaks_a_passing_check_is_undone(self):
        repo = make_repo({"a.txt": "start\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False, "max_rounds": 3,
                                                          "offload": {"double_review": False}})
        b = buckets.Buckets(cfg, repo, "change a.txt")
        checks = {"test": f"{sys.executable} -c \"import sys; sys.exit('BROKEN' in open('a.txt').read())\""}
        writes = iter(["good\n", "BROKEN\n"])
        def fake_code(task, commit=True):
            (repo / "a.txt").write_text(next(writes))
            commit_all(repo, task["id"])
            return {**task, "status": "done", "note": "DONE"}
        follow = [{"id": "R2T1", "file": "a.txt", "action": "edit", "instructions": "x", "done_when": "", "edits": [],
                   "read_first": [], "where": ""}]
        def fake_review(*a, **k):
            return {"done": False, "summary": "needs more", "issues": ["more"], "tasks": follow}
        task = {**follow[0], "id": "T1"}
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        baseline = {"test": {"ok": True, "output": "", "tail": ""}}
        with mock.patch.object(b, "code_task", fake_code), mock.patch.object(b, "double_review", fake_review), \
                mock.patch.object(b, "auto_fix", lambda base: None), contextlib.redirect_stderr(io.StringIO()) as err:
            rounds, verdict, now = b.rounds({"summary": "s"}, [task], base, checks, baseline)
        self.assertEqual(len(rounds), 2)
        self.assertEqual((repo / "a.txt").read_text(), "good\n")                  # round 2 undone
        self.assertTrue(now["test"]["ok"])
        self.assertFalse(verdict["done"])
        self.assertIn("Round 2 broke test and was undone", verdict["summary"])
        self.assertIn("round 2 broke test", err.getvalue())


class MemoryOutside(unittest.TestCase):
    def test_outside_keeps_the_repo_untouched(self):
        repo = make_repo({"src/a.py": "def run():\n    return 1\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": "outside"})
        b = buckets.Buckets(cfg, repo, "x")
        with contextlib.redirect_stderr(io.StringIO()):
            mem = b.memory
        self.assertIsNotNone(mem)
        self.assertFalse((repo / ".buckets").exists())
        self.assertTrue(str(mem.dir).startswith(str(buckets.STATE_DIR)))
        self.assertEqual(buckets.git(repo, "status", "--porcelain").strip(), "")
        self.assertIsNone(buckets.memory_place(repo, False))
        self.assertEqual(buckets.memory_place(repo, True), repo / ".buckets")


class CleanStops(unittest.TestCase):
    def test_an_unattended_stop_keeps_half_done_edits_on_the_branch_and_goes_back(self):
        repo = make_repo({"a.txt": "1\n"})
        main = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base = buckets.git(repo, "rev-parse", "HEAD").strip()
        buckets.git(repo, "switch", "-q", "-c", "buckets/x")
        (repo / "a.txt").write_text("half\n")
        old = buckets.UNATTENDED["on"]
        try:
            buckets.UNATTENDED["on"] = False
            self.assertIn("you're still on that branch", buckets.leave_branch(repo, "buckets/x", main, base))
            buckets.UNATTENDED["on"] = True
            msg = buckets.leave_branch(repo, "buckets/x", main, base)
        finally:
            buckets.UNATTENDED["on"] = old
        self.assertIn("committed on that branch", msg)
        self.assertEqual(buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), main)
        self.assertEqual((repo / "a.txt").read_text(), "1\n")
        self.assertEqual(buckets.git(repo, "stash", "list").strip(), "")          # the stash list is untouched
        self.assertIn("unfinished task", buckets.git(repo, "log", "-1", "--format=%s", "buckets/x"))


class Undo(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo({"a.ts": "1\n"})
        self.main = buckets.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        self.start = buckets.git(self.repo, "rev-parse", "HEAD").strip()
        buckets.git(self.repo, "switch", "-q", "-c", "buckets/two")
        (self.repo / "a.ts").write_text("2\n")
        commit_all(self.repo, "two")
        buckets.git(self.repo, "switch", "-q", self.main)
        run = buckets.Run(self.repo)
        (run.dir / "meta.json").write_text(json.dumps({"repo": str(self.repo), "kind": "code", "request": "make it 2",
                                                       "done": True, "branch": "buckets/two", "merge_into": self.main}))
        buckets.merge_run(self.repo)

    def test_undo_moves_back_and_keeps_the_work(self):
        msg = buckets.undo_merge(self.repo)
        self.assertIn("moved", msg)
        self.assertEqual(buckets.git(self.repo, "rev-parse", "HEAD").strip(), self.start)
        self.assertEqual((self.repo / "a.ts").read_text(), "1\n")
        self.assertTrue(buckets.branch_exists(self.repo, "buckets/two-undone"))
        with self.assertRaisesRegex(RuntimeError, "no merge by buckets"):
            buckets.undo_merge(self.repo)                                        # already undone

    def test_undo_reverts_when_more_came_after(self):
        (self.repo / "b.ts").write_text("x\n")
        commit_all(self.repo, "more")
        msg = buckets.undo_merge(self.repo)
        self.assertIn("revert", msg)
        self.assertEqual((self.repo / "a.ts").read_text(), "1\n")
        self.assertTrue((self.repo / "b.ts").exists())                          # what came after stays


class DoubleReview(unittest.TestCase):
    """Two machines review each round; a lone "undefined"-style claim while the checks pass is confirmed
    before it can send working code back for rewriting."""

    def setUp(self):
        self.repo = make_repo({"src/m.py": "def run(x):\n    return x + 1\n"})
        self.base = buckets.git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "src/m.py").write_text("def run(x):\n    d = x * 2\n    return d + 1\n")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "change"], cwd=self.repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {
            "mode": "local", "memory": False, "lookups": False,
            "planner": {"url": "http://mac:11434", "model": "m"},
            "thinker": {"url": "http://win:8080", "model": "m", "api": "llamacpp"}})
        self.b = buckets.Buckets(cfg, self.repo, "make run double first")
        self.b._thinker_up = True
        self.plan = {"summary": "s"}

    def verdict(self, done, issues=(), files=()):
        return {"done": done, "summary": "ok" if done else "not yet", "issues": list(issues),
                "tasks": [{"id": "R2T1", "file": f, "action": "edit", "instructions": "i", "done_when": "w"} for f in files]}

    def run_review(self, mac, win, now, confirm=None):
        turns = []
        def fake_ask(cfg, run, label, system, user, schema, validate, **kw):
            return validate(win if "win" in cfg["url"] else mac)
        def fake_turn(cfg, run, label, messages, **kw):
            turns.append((cfg["url"], [m["role"] for m in messages], messages[-1]["content"]))
            return json.dumps(confirm), "", [], False
        with mock.patch.object(buckets, "ask_json", fake_ask), mock.patch.object(buckets, "chat_turn", fake_turn), \
                contextlib.redirect_stderr(io.StringIO()):
            got = self.b.double_review(self.plan, [], self.base, now, now, 1, [], None)
        return got, turns

    def test_the_review_hears_what_was_dropped(self):
        self.b.dropped_tasks = ["edit src/m.py: unify the quotes (dropped: the move/share does it)"]
        seen = []
        def fake_ask(cfg, run, label, system, user, schema, validate, **kw):
            seen.append(user)
            return validate(self.verdict(True))
        with mock.patch.object(buckets, "ask_json", fake_ask), contextlib.redirect_stderr(io.StringIO()):
            self.b.review(self.plan, [], self.base, {}, {}, 1, [], None)
        self.assertIn("Planned but not run (these changes were NOT made):\n- edit src/m.py: unify the quotes", seen[0])
        self.assertIn("a task\n  that failed, was blocked or was dropped did not happen", seen[0])

    def test_both_done(self):
        got, turns = self.run_review(self.verdict(True), self.verdict(True), {"test": {"ok": True}})
        self.assertTrue(got["done"])
        self.assertEqual(turns, [])

    def test_a_lone_false_undefined_claim_is_dismissed(self):
        mac = self.verdict(False, ["`d` is undefined in `run`"], ["src/m.py"])
        got, turns = self.run_review(mac, self.verdict(True), {"test": {"ok": True}}, {"real": [], "why": "set on line 2"})
        self.assertTrue(got["done"])
        self.assertIn("didn't hold up", got["summary"])
        url, roles, ask = turns[0]
        self.assertEqual(url, "http://win:8080")                        # the reviewer who said done judges
        self.assertEqual(roles, ["system", "user", "assistant", "user"])  # its own review is the context
        self.assertIn("src/m.py:2:    d = x * 2", ask)                  # with where `d` appears now

    def test_a_confirmed_claim_wins(self):
        mac = self.verdict(False, ["`d` is undefined in `run`"], ["src/m.py"])
        got, _ = self.run_review(mac, self.verdict(True), {"test": {"ok": True}}, {"real": [1], "why": "w"})
        self.assertFalse(got["done"])
        self.assertEqual(got["tasks"][0]["file"], "src/m.py")

    def test_claims_the_checks_decide_count_as_definite(self):
        for claim in ("a critical React Hooks violation: useStores() is called twice", "this breaks the rules of hooks",
                      "`Button` was removed from the imports, a runtime error", "type error in the hook"):
            self.assertTrue(buckets.DEFINITE_CLAIM.search(claim), claim)
        self.assertFalse(buckets.DEFINITE_CLAIM.search("the request also asked for a docstring"))

    def test_a_lone_objection_is_weighed_by_the_other_reviewer(self):
        win = self.verdict(False, ["the request also asked for a docstring"], ["src/m.py"])
        got, turns = self.run_review(self.verdict(True), win, {"test": {"ok": True}}, {"real": [1], "why": "it did"})
        self.assertFalse(got["done"])
        self.assertEqual(len(turns), 1)
        got, _ = self.run_review(self.verdict(True), win, {"test": {"ok": True}}, {"real": [], "why": "style"})
        self.assertTrue(got["done"])
    def test_failing_checks_need_no_confirming(self):
        mac = self.verdict(False, ["`d` is undefined"], ["src/m.py"])
        got, turns = self.run_review(mac, self.verdict(True), {"test": {"ok": False, "output": "F", "tail": "F"}})
        self.assertFalse(got["done"])
        self.assertEqual(turns, [])

    def test_both_not_done_merges_tasks(self):
        mac = self.verdict(False, ["a"], ["src/m.py"])
        win = self.verdict(False, ["b"], ["src/m.py", "src/n.py"])
        got, _ = self.run_review(mac, win, {"test": {"ok": True}})
        self.assertEqual([t["file"] for t in got["tasks"]], ["src/m.py", "src/n.py"])
        self.assertEqual([t["id"] for t in got["tasks"]], ["R2T1", "R2T2"])
        self.assertIn("b", got["issues"][1])

    def test_one_review_when_there_is_no_second_machine(self):
        self.b.cfg["thinker"]["url"] = "http://mac:11434"
        self.assertIsNone(self.b.second_reviewer())
        self.b.cfg["thinker"]["url"] = "http://win:8080"
        self.b.mode = "claude"
        self.assertIsNone(self.b.second_reviewer())

    def test_weak_tests(self):
        diff = ("+++ b/src/m.py\n@@ -1,2 +1,3 @@ def run(x):\n+    d = x * 2\n"
                "+++ b/test_m.py\n@@ -0,0 +1,12 @@\n"
                "+    def test_calls(self):\n+        with mock.patch('src.m.run') as r:\n+            go()\n"
                "+        r.assert_called_once_with(2)\n"
                "+    def test_value(self):\n+        self.assertEqual(run(1), 3)\n"
                "+    def test_nothing(self):\n+        run(1)\n"
                "+++ b/src/a.test.ts\n@@ -0,0 +1,3 @@\n"
                "+it('saves', () => {\n+  save();\n+  expect(api.post).toHaveBeenCalledWith('x');\n")
        found = "\n".join(buckets.weak_tests(diff))
        self.assertIn("`test_calls` (`test_m.py`): all 1 assertion(s) only check that mocks were called", found)
        self.assertIn("`test_calls` (`test_m.py`) mocks `run`, which this change edits", found)
        self.assertIn("`test_nothing` (`test_m.py`) has no assertions", found)
        self.assertIn("`saves` (`src/a.test.ts`): all 1", found)
        self.assertNotIn("test_value", found)


class Moves(unittest.TestCase):
    """Mechanical moves: the code moves exactly; parameters, returns, helpers and imports come from
    TypeScript's scoping."""
    SCREEN = """import React, { useEffect, useState } from "react"
import { Text } from "./ui"
import { fetchThing, formatThing } from "./api"

const clean = (s: string) => s.trim()
const label = (s: string) => `[${s}]`

export const ThingScreen = ({ id, title }: { id: number; title: string }) => {
  const [thing, setThing] = useState<string>("")
  // load once per id
  useEffect(() => {
    fetchThing(id).then((t) => setThing(clean(t)))
  }, [id])
  const onSave = () => setThing(label(thing))
  const renderTitle = () => <Text>{label(title)}</Text>
  const late = 3
  const usesLate = () => late + 1

  return (
    <>
      {renderTitle()}
      <Text onPress={onSave}>{formatThing(thing)}</Text>
    </>
  )
}
"""

    def setUp(self):
        if not (TS_PKG.exists() and shutil.which("node")):
            self.skipTest("needs node and a typescript package")
        self.repo = make_repo({"src/thing-screen.tsx": self.SCREEN, "src/ui.tsx": "export const Text = (p: any) => null\n",
                               "src/api.ts": "export const fetchThing = async (id: number) => ''\nexport const formatThing = (s: string) => s\n",
                               "tsconfig.json": '{"compilerOptions": {"jsx": "react-jsx", "strict": true, "noEmit": true, "skipLibCheck": true}}'})
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/typescript").symlink_to(TS_PKG)
        (self.repo / ".git/info/exclude").write_text("node_modules\n")

    def test_extract_moves_the_logic_into_a_hook(self):
        d = buckets.ts_moves(self.repo, {"op": "extract", "file": "src/thing-screen.tsx", "function": "ThingScreen",
                                         "select": "logic", "into": "src/use-thing.ts", "name": "useThing"})
        self.assertTrue(d["ok"], d)
        hook, screen = d["files"]["src/use-thing.ts"], d["files"]["src/thing-screen.tsx"]
        r = d["report"]
        self.assertEqual([p["name"] for p in r["params"]], ["id"])          # title is only used by what stays
        self.assertEqual(r["returns"], ["thing", "onSave"])                  # setThing isn't used outside
        self.assertEqual(r["helpers_moved"], ["clean", "label"])
        self.assertEqual(r["helpers_shared"], ["label"])                     # the render helper still uses it
        self.assertIn("export function useThing({ id }: { id: number }) {", hook)
        self.assertIn("  // load once per id\n  useEffect(() => {\n    fetchThing(id).then((t) => setThing(clean(t)))\n  }, [id])\n", hook)
        self.assertIn('import { useEffect, useState } from "react"', hook)
        self.assertIn('import { fetchThing } from "./api"', hook)
        self.assertIn("export const label = ", hook)
        self.assertNotIn("export const clean", hook)
        self.assertIn("  const { thing, onSave } = useThing({ id })\n  const renderTitle", screen)
        self.assertIn('import { useThing, label } from "./use-thing"', screen)
        self.assertIn('import { formatThing } from "./api"', screen)          # fetchThing went with the logic
        self.assertNotIn("useEffect", screen)
        self.assertNotIn("const clean", screen)
        self.assertIn("const late = 3", hook)                                  # pure logic moves too...
        self.assertNotIn("usesLate", screen)

    def test_a_statement_that_needs_what_stays_after_the_call_stays(self):
        src = self.SCREEN.replace("  const late = 3\n", "").replace("  return (", "  const late = renderTitle() ? 3 : 4\n\n  return (")
        (self.repo / "src/thing-screen.tsx").write_text(src)
        d = buckets.ts_moves(self.repo, {"op": "extract", "file": "src/thing-screen.tsx", "function": "ThingScreen",
                                         "select": "logic", "into": "src/use-thing.ts", "name": "useThing"})
        self.assertTrue(d["ok"], d)
        self.assertIn("const usesLate = () => late + 1", d["files"]["src/thing-screen.tsx"])
        self.assertTrue(any("usesLate" in n for n in d["report"]["notes"]))

    def test_move_task_through_buckets(self):
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "split the thing screen")
        task = buckets.validate_tasks([{"id": "T1", "file": "src/thing-screen.tsx", "action": "move", "where": "ThingScreen",
                                        "into": "src/use-thing.ts", "name": "useThing", "select": ["logic"],
                                        "instructions": "", "done_when": "", "edits": []}], self.repo, 8, "T")[0]
        self.assertEqual(task["select"], "logic")
        with contextlib.redirect_stderr(io.StringIO()):
            got = b.code_task(task)
        self.assertEqual(got["status"], "done", got)
        self.assertIn("returns 2 values", got["note"])
        self.assertTrue((self.repo / "src/use-thing.ts").is_file())
        self.assertIn("useThing", buckets.git(self.repo, "log", "-1", "--format=%B"))
        self.assertEqual(buckets.git(self.repo, "status", "--porcelain").strip(), "")

    def test_a_move_request_gets_the_move_even_when_the_plan_retypes_it(self):
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Split src/thing-screen.tsx: move its logic into a hook useThing in src/use-thing.ts")
        with contextlib.redirect_stderr(io.StringIO()):
            hint = b.move_hint()
            self.assertEqual((hint["task"]["where"], hint["task"]["name"], hint["task"]["select"]), ("ThingScreen", "useThing", "logic"))
            self.assertIn("A mechanical move fits this request", b.facts())
            retyped = {"summary": "s", "tasks": [
                {"id": "T1", "file": "src/use-thing.ts", "action": "create", "instructions": "write the hook", "edits": []},
                {"id": "T2", "file": "src/thing-screen.tsx", "action": "edit", "instructions": "call it", "edits": []},
                {"id": "T3", "file": "src/api.ts", "action": "edit", "instructions": "also this", "edits": []}]}
            got = b.prefer_move(retyped)
        self.assertEqual([(t["id"], t["action"], t["file"]) for t in got["tasks"]],
                         [("T1", "move", "src/thing-screen.tsx"), ("T2", "edit", "src/api.ts")])
        planned = {"summary": "s", "tasks": [dict(hint["task"]), {"id": "T2", "file": "src/thing-screen.tsx", "action": "edit",
                                                                    "instructions": "call the hook", "edits": []}]}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual([t["action"] for t in b.prefer_move(planned)["tasks"]], ["move"])
        self.assertIsNone(buckets.Buckets(cfg, self.repo, "explain src/thing-screen.tsx").move_hint())
        component = buckets.Buckets(cfg, self.repo, "Split the title part of src/thing-screen.tsx into a component in src/thing-title.tsx")
        self.assertIsNone(component.move_hint())                              # not a hook: no logic move suggested
        sparse = {"summary": "s", "tasks": [{"id": "T1", "file": "src/thing-screen.tsx", "action": "move",
                                             "instructions": "into: src/use-thing.ts", "done_when": "", "where": "", "edits": []}]}
        filled = buckets.validate_plan(b.fill_moves(sparse), self.repo, 8)["tasks"][0]
        self.assertEqual((filled["into"], filled["name"], filled["where"]), ("src/use-thing.ts", "useThing", "ThingScreen"))

    def test_share_and_compare(self):
        a = 'import { fmt } from "../util"\n\nconst same = (x: number) => fmt(x * 2)\nconst diff = 1\n\nexport const A = () => same(diff)\n'
        b2 = 'import { fmt } from "../util"\n\nconst same = (x: number) =>\n  fmt(x * 2)\nconst diff = 2\n\nexport const B = () => same(diff)\n'
        for f, t in {"src/a/screen.ts": a, "src/b/screen.ts": b2, "src/util.ts": "export const fmt = (n: number) => `${n}`\n"}.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        c = buckets.ts_moves(self.repo, {"op": "compare", "a": "src/a/screen.ts", "b": "src/b/screen.ts"})
        status = {p["name"]: p["status"] for p in c["pairs"]}
        self.assertEqual(status["same"], "identical")                          # formatting doesn't count
        self.assertIn(status["diff"], ("near", "different"))                    # 1 vs 2: not identical
        self.assertEqual((status["A"], status["B"]), ("only_a", "only_b"))
        d = buckets.ts_moves(self.repo, {"op": "share", "files": ["src/a/screen.ts", "src/b/screen.ts"],
                                         "names": ["same", "diff"], "into": "src/shared.ts"})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["report"]["shared"], ["same"])
        self.assertEqual(d["report"]["skipped"][0]["name"], "diff")
        self.assertIn('import { fmt } from "./util"', d["files"]["src/shared.ts"])
        self.assertIn("export const same", d["files"]["src/shared.ts"])
        for f in ("src/a/screen.ts", "src/b/screen.ts"):
            self.assertIn('import { same } from "../shared"', d["files"][f])
            self.assertNotIn("fmt", d["files"][f])                             # its only user moved out

    def test_a_share_request_gets_the_share(self):
        a = 'import { fmt } from "../util"\n\nconst same = (x: number) => fmt(x * 2)\nconst diff = 1\n\nexport const A = () => same(diff)\n'
        b2 = a.replace("const diff = 1", "const diff = [2, 3]").replace("export const A", "export const B")
        for f, t in {"src/app/screen.ts": a, "src/app-pro/screen.ts": b2, "src/util.ts": "export const fmt = (n: number) => `${n}`\n"}.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "variants"], cwd=self.repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Merge src/app/screen.ts and src/app-pro/screen.ts: share what they have in common in src/screen-shared.ts")
        with contextlib.redirect_stderr(io.StringIO()):
            hints = b.share_hints()
            self.assertEqual(hints[0]["task"]["select"], ["same"])
            self.assertEqual(hints[0]["task"]["file"], "src/screen-shared.ts")
            self.assertIn("A mechanical share fits these two files", b.facts())
            retyped = {"summary": "s", "tasks": [{"id": "T1", "file": "src/screen-shared.ts", "action": "create",
                                                  "instructions": "write it", "done_when": "", "where": "", "edits": []},
                                                 {"id": "T2", "file": "src/app/screen.ts", "action": "edit",
                                                  "instructions": "import it", "done_when": "", "where": "", "edits": []}]}
            got = b.prefer_move(retyped)
        self.assertEqual([t["action"] for t in got["tasks"]], ["share"])
        with contextlib.redirect_stderr(io.StringIO()):
            extra = b.prefer_move({"summary": "s", "tasks": [dict(got["tasks"][0]), {"id": "T2", "file": "src/app/screen.ts",
                                   "action": "edit", "instructions": "import it", "done_when": "", "where": "", "edits": []}]})
        self.assertEqual([t["action"] for t in extra["tasks"]], ["share"])
        sparse = {"summary": "s", "tasks": [{"id": "T1", "file": "src/screen-shared.ts", "action": "share", "instructions": "",
                                             "done_when": "", "where": "", "edits": []}]}
        task = buckets.validate_plan(b.fill_moves(sparse), self.repo, 8)["tasks"][0]
        self.assertEqual((task["from"], task["select"]), (["src/app/screen.ts", "src/app-pro/screen.ts"], ["same"]))

    def test_share_hook_and_inner_statements(self):
        comp = ('import { useState } from "react"\nimport { useStores } from "../stores"\n\n'
                'export const Screen = ({ id }: { id: number }) => {\n  const { auth } = useStores()\n'
                '  const [count, setCount] = useState(0)\n  const label = `#${id}: ${auth.name}`\n'
                '  const bump = () => setCount(count + 1)\n  %s\n  return <div onClick={bump}>{label}{count}</div>\n}\n')
        files = {"src/stores.ts": "export const useStores = () => ({ auth: { name: 'x' } })\n",
                 "src/x/a/screen.tsx": comp % "const onlyA = 1", "src/x/b/screen.tsx": comp % "const onlyB = 2"}
        for f, t in files.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "variants"], cwd=self.repo, check=True)
        d = buckets.ts_moves(self.repo, {"op": "share_hook", "files": ["src/x/a/screen.tsx", "src/x/b/screen.tsx"], "function": "Screen",
                                         "names": ["count", "label", "bump"], "into": "src/x/use-screen-shared.ts", "name": "useScreenShared"})
        self.assertTrue(d["ok"], d)
        hook = d["files"]["src/x/use-screen-shared.ts"]
        self.assertIn("export function useScreenShared({ id }: { id: number }) {\n  const { auth } = useStores()\n", hook)  # re-derived
        self.assertIn("const [count, setCount] = useState(0)", hook)
        for f in ("src/x/a/screen.tsx", "src/x/b/screen.tsx"):
            self.assertIn("const { count, label, bump } = useScreenShared({ id })", d["files"][f])
            self.assertNotIn("useStores", d["files"][f])                       # its only user moved, so it went
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "x")
        task = buckets.validate_tasks([{"id": "T1", "file": "src/x/screen-shared.ts", "action": "share",
                                        "from": ["src/x/a/screen.tsx", "src/x/b/screen.tsx"], "select": ["count", "label", "bump"],
                                        "instructions": "", "done_when": "", "edits": []}], self.repo, 8, "T")[0]
        with contextlib.redirect_stderr(io.StringIO()):
            got = b.code_task(task)
        self.assertEqual(got["status"], "done", got)
        self.assertIn("export function useScreenShared(", (self.repo / "src/x/screen-shared.ts").read_text())  # in the named shared file
        bad = buckets.ts_moves(self.repo, {"op": "share_hook", "files": ["src/x/a/screen.tsx", "src/x/b/screen.tsx"], "function": "Screen",
                                           "names": ["onlyA"], "into": "src/x/use-other.ts", "name": "useOther"})
        self.assertFalse(bad["ok"])

    def test_unify_then_share(self):
        a = 'import { fmt } from "../util"\n\nconst same = (x: number) => fmt(x * 2)\nconst near = (x: number) => fmt(x ?? 0)\n\nexport const A = () => same(near(1))\n'
        b2 = a.replace("fmt(x ?? 0)", "fmt(x)").replace("export const A", "export const B")
        for f, t in {"src/a/screen.ts": a, "src/b/screen.ts": b2, "src/util.ts": "export const fmt = (n: number) => `${n}`\n"}.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "variants"], cwd=self.repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "x")
        tasks = buckets.validate_tasks([
            {"id": "T1", "file": "src/b/screen.ts", "action": "unify", "from": ["src/a/screen.ts"], "select": ["near"],
             "instructions": "use the null-safe version", "done_when": "", "edits": []},
            {"id": "T2", "file": "src/shared.ts", "action": "share", "from": ["src/a/screen.ts", "src/b/screen.ts"], "select": ["same"],
             "instructions": "", "done_when": "", "edits": []}], self.repo, 8, "T")
        with contextlib.redirect_stderr(io.StringIO()):
            got = [b.code_task(t) for t in tasks]
        self.assertEqual([g["status"] for g in got], ["done", "done"], got)
        shared = (self.repo / "src/shared.ts").read_text()
        self.assertIn("export const near = (x: number) => fmt(x ?? 0)", shared)   # unified, then shared too
        self.assertIn("export const same", shared)

    def test_an_edit_that_unifies_a_near_copy_becomes_unify(self):
        a = 'const same = (x: number) => x * 2\nconst near = (x: number) => String(x ?? 0)\n\nexport const A = () => same(1) + near(2)\n'
        b2 = a.replace("String(x ?? 0)", "String(x)").replace("export const A", "export const B")
        for f, t in {"src/app/screen.ts": a, "src/app-pro/screen.ts": b2}.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "variants"], cwd=self.repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Merge src/app/screen.ts and src/app-pro/screen.ts into src/screen-shared.ts")
        plan = {"summary": "s", "tasks": [
            {"id": "T1", "file": "src/screen-shared.ts", "action": "share", "from": ["src/app/screen.ts", "src/app-pro/screen.ts"],
             "select": ["same"], "instructions": "", "done_when": "", "where": "", "edits": [], "read_first": []},
            {"id": "T2", "file": "src/app-pro/screen.ts", "action": "edit", "instructions": "Unify `near` to match the a version: x ?? 0",
             "done_when": "", "where": "", "edits": [{"at": "String(x)", "mode": "replace", "code": "String(x ?? 0)"}], "read_first": []}]}
        with contextlib.redirect_stderr(io.StringIO()):
            hints = b.share_hints()
            if not any(p["status"] == "near" and p["name"] == "near" for p in hints[0]["compare"]["pairs"]):
                self.skipTest("the two `near` versions don't count as a near copy here")
            got = b.prefer_move(plan)
        self.assertEqual([(t["id"], t["action"]) for t in got["tasks"]], [("T1", "unify"), ("T2", "share")])
        self.assertEqual((got["tasks"][0]["from"], got["tasks"][0]["select"]), (["src/app/screen.ts"], ["near"]))
        bad = {"summary": "s", "tasks": [{"id": "T1", "file": "src/app-pro/screen.ts", "action": "unify", "from": ["/"], "select": ["near"],
                                          "instructions": "", "done_when": "", "where": "", "edits": []}]}
        fixed = buckets.validate_plan(b.fill_moves(bad), self.repo, 8)["tasks"][0]
        self.assertEqual(fixed["from"], ["src/app/screen.ts"])

    def test_relocate_moves_to_a_util_and_repoints_importers(self):
        files = {
            "src/cart.ts": 'import { round } from "./math"\n\nconst pad = (s: string) => s.padStart(6)\n'
                           'export const formatPrice = (n: number) => pad(`$${round(n)}`)\n'
                           'export const total = (xs: number[]) => formatPrice(xs.reduce((a, b) => a + b, 0))\n',
            "src/math.ts": "export const round = (n: number) => Math.round(n * 100) / 100\n",
            "src/page.ts": 'import { formatPrice, total } from "./cart"\nexport const show = () => formatPrice(1) + total([2])\n',
            "src/index.ts": 'export { formatPrice } from "./cart"\n',
        }
        for f, t in files.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "cart"], cwd=self.repo, check=True)
        d = buckets.ts_moves(self.repo, {"op": "relocate", "file": "src/cart.ts", "into": "src/utils/price.ts",
                                         "names": ["formatPrice"], "importers": ["src/page.ts", "src/index.ts"]})
        self.assertTrue(d["ok"], d)
        r, f = d["report"], d["files"]
        self.assertEqual(sorted(r["moved"]), ["formatPrice", "pad"])               # its private helper came along
        self.assertIn('import { round } from "../math"', f["src/utils/price.ts"])
        self.assertIn("export const formatPrice", f["src/utils/price.ts"])
        self.assertNotIn("const pad", f["src/cart.ts"])
        self.assertIn('import { formatPrice } from "./utils/price"', f["src/cart.ts"])  # total still uses it
        self.assertNotIn("round", f["src/cart.ts"])                                 # its import went with it
        self.assertIn('import { total } from "./cart"', f["src/page.ts"])
        self.assertIn('import { formatPrice } from "./utils/price"', f["src/page.ts"])
        self.assertIn('export { formatPrice } from "./utils/price"', f["src/index.ts"])
        for path, text in f.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text)
        tsc = subprocess.run(["node", str(TS_PKG / "bin" / "tsc"), "--noEmit", "--strict", "--ignoreConfig", "src/cart.ts", "src/page.ts",
                              "src/index.ts", "src/math.ts", "src/utils/price.ts"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(tsc.returncode, 0, tsc.stdout + tsc.stderr)

    def test_relocate_then_dedupe_the_copies(self):
        fmt = "const fmt = (s: string): string => {\n  return s.trim()\n}\n"
        files = {"src/time/helper.ts": "export " + fmt + "export const other = 1\n",
                 "src/time/a.ts": 'import { other } from "./helper"\n\n' + fmt + "export const A = () => fmt(' x ') + other\n",
                 "src/time/b.ts": "const fmt = (s: string): string => s.toUpperCase()\nexport const B = () => fmt('y')\n",
                 "src/utils/dates.ts": "export const today = () => 1\n"}
        for f, t in files.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "time"], cwd=self.repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Move `fmt` from src/time/helper.ts into src/utils/dates.ts and update everything that uses it")
        with contextlib.redirect_stderr(io.StringIO()):
            rh = b.relocate_hint()
            self.assertEqual((rh["task"]["into"], rh["copies"]), ("src/utils/dates.ts", ["src/time/a.ts", "src/time/b.ts"]))
            plan = b.prefer_move({"summary": "s", "tasks": [dict(rh["task"])]})
            self.assertEqual([t["action"] for t in plan["tasks"]], ["relocate", "dedupe"])
            got = [b.code_task(t) for t in buckets.validate_tasks(plan["tasks"], self.repo, 8, "T")]
        self.assertEqual([g["status"] for g in got], ["done", "done"], got)
        a = (self.repo / "src/time/a.ts").read_text()
        self.assertNotIn("const fmt", a)
        self.assertIn('import { fmt } from "../utils/dates"', a)
        self.assertIn("const fmt = (s: string): string => s.toUpperCase()", (self.repo / "src/time/b.ts").read_text())  # differs: kept
        self.assertIn("copies that DIFFER", got[1]["note"])
        self.assertIn("export const fmt", (self.repo / "src/utils/dates.ts").read_text())

    def test_differing_copies_unify_into_the_shared_one_then_dedupe(self):
        shared = "export const fmt = (s: string): string => {\n  return s.trim()\n}\n"
        safer = "const fmt = (s: string): string => {\n  if (!s) return ''\n  return s.trim()\n}\n"
        files = {"src/utils/text.ts": shared, "src/a.ts": safer + "export const A = () => fmt(' x ')\n",
                 "src/b.ts": safer + "export const B = () => fmt('y')\n"}
        for f, t in files.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "copies"], cwd=self.repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "x")
        base = buckets.git(self.repo, "rev-parse", "HEAD").strip()
        dd = buckets.validate_tasks([{"id": "T1", "file": "src/utils/text.ts", "action": "dedupe", "select": ["fmt"],
                                      "instructions": "", "done_when": "", "edits": []}], self.repo, 8, "T")[0]
        with contextlib.redirect_stderr(io.StringIO()):
            first = b.code_task(dd)
        self.assertEqual(first["status"], "failed")
        self.assertIn("+ if (!s) return ''", first["note"])                         # the difference is shown
        # a hand edit that removes a differing copy is flagged for the reviewers
        (self.repo / "src/a.ts").write_text('import { fmt } from "./utils/text"\nexport const A = () => fmt(\' x \')\n')
        commit_all(self.repo, "hand edit")
        self.assertIn("which behaves differently: (null) gives <throws TypeError> now", b.removed_copies_text(base))
        subprocess.run(["git", "reset", "-q", "--hard", base], cwd=self.repo, check=True)
        # the right way: unify the safer copy into the shared one (keeping its export), then dedupe
        with self.assertRaises(ValueError):                                          # no "from" until it's filled
            buckets.validate_tasks([{"id": "T2", "file": "src/utils/text.ts", "action": "unify", "select": ["fmt"],
                                     "instructions": "use the safer version from src/a.ts", "done_when": "", "edits": []}], self.repo, 8, "T")
        filled = buckets.validate_tasks(b.fill_moves({"tasks": [{"id": "T2", "file": "src/utils/text.ts", "action": "unify",
                     "select": ["fmt"], "instructions": "use the safer version from src/a.ts", "done_when": "", "edits": []}]})["tasks"],
                                        self.repo, 8, "T")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.code_task(filled[0])["status"], "done")
            second = b.code_task(dd)
        self.assertIn("export const fmt", (self.repo / "src/utils/text.ts").read_text())
        self.assertIn("if (!s) return ''", (self.repo / "src/utils/text.ts").read_text())
        self.assertEqual(second["status"], "done", second)
        for f in ("src/a.ts", "src/b.ts"):
            self.assertIn('import { fmt } from "./utils/text"', (self.repo / f).read_text())

    def test_dedupe_runs_differently_written_copies_to_compare_them(self):
        plain = ('export const toHMS = (hhmmss: string): string => {\n  const [hoursStr, minutesStr, secondsStr] = hhmmss.split(":")\n'
                 '  const hours = Number.parseInt(hoursStr, 10)\n  const minutes = Number.parseInt(minutesStr, 10)\n'
                 '  const seconds = Number.parseInt(secondsStr || "0", 10)\n  return `${hours}h ${minutes}m ${seconds}s`\n}\n')
        guarded = plain.replace('=> {\n', '=> {\n  if (!hhmmss) return "0h 0m 0s"\n', 1)
        copy1 = ('const toHMS = (hhmmss: string): string => {\n  if (!hhmmss) {\n    return "0h 0m 0s"\n  }\n'
                 '  const [h, m, s] = hhmmss.split(":")\n  return `${Number.parseInt(h, 10)}h ${Number.parseInt(m, 10)}m ${Number.parseInt(s || "0", 10)}s`\n}\n')
        copy2 = copy1.replace('if (!hhmmss) {\n    return "0h 0m 0s"\n  }', 'if (!hhmmss) return "0h 0m 0s"')
        files = {"src/utils/time.ts": plain, "src/a.ts": copy1 + "export const A = () => toHMS('')\n",
                 "src/b.ts": copy2 + "export const B = () => toHMS('1:2')\n"}
        for f, t in files.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        payload = {"op": "dedupe", "file": "src/utils/time.ts", "names": ["toHMS"], "copies": ["src/a.ts", "src/b.ts"]}
        d = buckets.ts_moves(self.repo, payload)
        self.assertEqual(d["report"]["replaced"], [])
        self.assertIn('runs differently: ("") gives "NaNh NaNm 0s" from the shared one, "0h 0m 0s" from the copy',
                      d["report"]["differ"][0]["diff"])
        (self.repo / "src/utils/time.ts").write_text(guarded)                     # the shared one gets the guard
        d = buckets.ts_moves(self.repo, payload)
        self.assertEqual(sorted(d["report"]["replaced"]), ["src/a.ts: toHMS", "src/b.ts: toHMS"], d["report"])
        self.assertTrue(all("behaves the same on" in e for e in d["report"]["equivalent"]))
        self.assertIn('import { toHMS } from "./utils/time"', d["files"]["src/a.ts"])

    def test_relocate_deletes_an_emptied_source_and_reviews_hear_usage(self):
        files = {"src/time/helper.ts": "export const fmt = (s: string): string => s.trim()\n",
                 "src/a.ts": "const fmt = (s: string): string => (s ? s.trim() : '')\nexport const A = () => fmt(' x ')\n",
                 "src/utils/dates.ts": "export const today = () => 1\n"}
        for f, t in files.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "h"], cwd=self.repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Move `fmt` from src/time/helper.ts into src/utils/dates.ts, including copies")
        with contextlib.redirect_stderr(io.StringIO()):
            rh = b.relocate_hint()
            usage = b.usage_before_text()
            wrong = b.fill_moves({"tasks": [{"id": "T2", "file": "src/a.ts", "action": "dedupe", "select": ["fmt"]}]})
            got = b.code_task(buckets.validate_tasks([dict(rh["task"])], self.repo, 8, "T")[0])
        self.assertIn("the local copy in src/a.ts", usage)
        self.assertIn("had no users", usage)
        self.assertEqual(wrong["tasks"][0]["file"], "src/utils/dates.ts")            # aimed at the shared one
        self.assertIn("was deleted", got["note"])
        self.assertFalse((self.repo / "src/time/helper.ts").exists())
        delete = buckets.validate_tasks([{"id": "T3", "file": "src/a.ts", "action": "delete", "instructions": "",
                                          "done_when": "", "edits": []}], self.repo, 8, "T")[0]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(b.code_task(delete)["status"], "done")
        self.assertFalse((self.repo / "src/a.ts").exists())

    def test_signature_changes_reach_every_use(self):
        files = {
            "sig/jsx.d.ts": "declare namespace JSX { interface Element {} interface IntrinsicElements { [k: string]: any } }\n",
            "sig/price.ts": "export const price = (n: number, digits?: number) => n.toFixed(digits ?? 2)\n",
            "sig/badge.tsx": "export const Badge = ({ label }: { label: string }) => <b>{label}</b>\n",
            "sig/page.tsx": 'import { price } from "./price"\nimport { Badge } from "./badge"\n'
                            'export const Page = () => <div>{price(1)}{price(2, 1)}<Badge label="x" /></div>\n',
        }
        for f, t in files.items():
            (self.repo / f).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / f).write_text(t)
        d = buckets.ts_moves(self.repo, {"op": "signature", "file": "sig/price.ts", "name": "price", "importers": ["sig/page.tsx"],
                                         "param": {"name": "currency", "type": "string", "default": '"USD"', "value": '"EUR"'}})
        self.assertTrue(d["ok"], d)
        self.assertIn('digits?: number, currency: string = "USD")', d["files"]["sig/price.ts"])
        self.assertIn('price(1, undefined, "EUR")', d["files"]["sig/page.tsx"])          # the skipped optional is filled
        self.assertIn('price(2, 1, "EUR")', d["files"]["sig/page.tsx"])
        for f, t in d["files"].items():
            (self.repo / f).write_text(t)
        c = buckets.ts_moves(self.repo, {"op": "signature", "file": "sig/badge.tsx", "name": "Badge", "importers": ["sig/page.tsx"],
                                         "param": {"name": "tone", "type": '"info" | "warn"', "default": '"info"', "value": '"warn"'}})
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["report"]["kind"], "component")
        self.assertIn('{ label, tone = "info" }: { label: string; tone?: "info" | "warn" }', c["files"]["sig/badge.tsx"])
        self.assertIn('<Badge label="x" tone={"warn"} />', c["files"]["sig/page.tsx"])
        for f, t in c["files"].items():
            (self.repo / f).write_text(t)
        tsc = subprocess.run(["node", str(TS_PKG / "bin" / "tsc"), "--noEmit", "--strict", "--ignoreConfig", "--jsx", "preserve",
                              "sig/jsx.d.ts", "sig/price.ts", "sig/badge.tsx", "sig/page.tsx"], cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(tsc.returncode, 0, tsc.stdout + tsc.stderr)

    def test_signature_values_written_jsx_style_are_unwrapped(self):
        (self.repo / "s.ts").write_text("export const f = (n: number) => n\n")
        t = buckets.validate_tasks([{"id": "T1", "file": "s.ts", "action": "signature", "where": "f", "instructions": "",
                                     "done_when": "", "edits": [], "param": {"name": "d", "type": "number", "default": "{2000}",
                                                                             "value": "{ 3000 }"}}], self.repo, 8, "T")[0]
        self.assertEqual((t["param"]["default"], t["param"]["value"]), ("2000", "3000"))
        t = buckets.validate_tasks([{"id": "T1", "file": "s.ts", "action": "signature", "where": "f", "instructions": "",
                                     "done_when": "", "edits": [], "param": {"name": "d", "default": "default: 2000"}}], self.repo, 8, "T")[0]
        self.assertEqual(t["param"]["default"], "2000")
        t = buckets.validate_tasks([{"id": "T1", "file": "s.ts", "action": "signature", "where": "f", "instructions": "",
                                     "done_when": "", "edits": [], "param": {"name": "d", "type": "number", "default": "number = 2000",
                                                                             "value": "d = 3000"}}], self.repo, 8, "T")[0]
        self.assertEqual((t["param"]["default"], t["param"]["value"]), ("2000", "3000"))
        bad = buckets.ts_moves(self.repo, {"op": "signature", "file": "s.ts", "name": "f", "param": {"name": "d", "default": "2000 2000"}})
        self.assertFalse(bad["ok"])
        self.assertIn("isn't valid TypeScript", bad["error"])
        t = buckets.validate_tasks([{"id": "T1", "file": "s.ts", "action": "signature", "where": "f", "instructions": "",
                                     "done_when": "", "edits": [], "param": {"name": "o", "value": "{ a: 1 }"}}], self.repo, 8, "T")[0]
        self.assertEqual(t["param"]["value"], "{ a: 1 }")                         # an object literal stays

    def test_edits_leave_the_files_own_blank_lines_alone(self):
        (self.repo / "keep.ts").write_text("export const f = (n: number) => n\n\n\n\nexport const g = 1\n\n\n\nexport const h = f(1)\n")
        d = buckets.ts_moves(self.repo, {"op": "signature", "file": "keep.ts", "name": "f",
                                         "param": {"name": "d", "type": "number", "default": "2", "value": "3"}})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["files"]["keep.ts"],
                         "export const f = (n: number, d: number = 2) => n\n\n\n\nexport const g = 1\n\n\n\nexport const h = f(1, 3)\n")

    def test_a_render_helper_becomes_a_component(self):
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Turn the title part of src/thing-screen.tsx into a Title component")
        with contextlib.redirect_stderr(io.StringIO()):
            ch = b.component_hint()
        self.assertEqual(ch["helpers"], ["renderTitle"])
        self.assertEqual((ch["task"]["select"], ch["task"]["name"], ch["task"]["into"]), (["renderTitle"], "Title", "src/title.tsx"))
        with contextlib.redirect_stderr(io.StringIO()):
            got = b.code_task(ch["task"])
        self.assertEqual(got["status"], "done", got)
        screen = (self.repo / "src/thing-screen.tsx").read_text()
        comp = (self.repo / "src/title.tsx").read_text()
        self.assertIn("<Title title={title} />", screen)
        self.assertNotIn("renderTitle", screen)
        self.assertIn("export const Title = ({ title }: { title: string }) =>", comp)
        self.assertIn("<Text>{label(title)}</Text>", comp)
        with self.assertRaisesRegex(ValueError, "component"):
            buckets.validate_tasks([{**ch["task"], "name": "lowercase"}], self.repo, 8, "T")

    def test_a_component_task_without_its_new_file_gets_the_dry_runs(self):
        # The planner sometimes names the helper and leaves "into" empty; the dry run knows the file.
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Turn the title part of src/thing-screen.tsx into a Title component")
        sparse = lambda helper: {"summary": "s", "tasks": [{"id": "T1", "file": "src/thing-screen.tsx", "action": "component",
                                                            "into": "", "name": "", "where": "", "select": [helper],
                                                            "instructions": "extract it", "done_when": "", "edits": []}]}
        with contextlib.redirect_stderr(io.StringIO()):
            task = buckets.validate_plan(b.fill_moves(sparse("renderTitle")), self.repo, 8)["tasks"][0]
        self.assertEqual((task["into"], task["name"], task["where"], task["select"]),
                         ("src/title.tsx", "Title", "ThingScreen", ["renderTitle"]))
        with contextlib.redirect_stderr(io.StringIO()):                # another helper: its file isn't known, so no guess
            self.assertEqual(b.fill_moves(sparse("renderOther"))["tasks"][0]["into"], "")

    def test_the_component_hint_reads_the_prose_and_tolerates_misspelled_helpers(self):
        screen = ("export const S = () => {\n  const renderOrderSummaryCard = () => <i />\n  const renderProfilPicture = () => <b />\n"
                  "  return <div>{renderOrderSummaryCard()}{renderProfilPicture()}</div>\n}\n")
        (self.repo / "src/order-screen.tsx").write_text(screen)
        commit_all(self.repo, "screen")
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, self.repo, "Turn the profile picture part of src/order-screen.tsx into a ProfilePicture component")
        with contextlib.redirect_stderr(io.StringIO()):
            ch = b.component_hint()
        self.assertEqual(ch["task"]["select"], ["renderProfilPicture"])

    def test_shared_hook_names(self):
        self.assertEqual(buckets.shared_hook_names("a/b/billing-shared.tsx", "a/b/x/use-billing-screen.tsx"),
                         ("a/b/use-billing-screen-shared.ts", "useBillingScreenShared"))
        self.assertEqual(buckets.shared_hook_names("a/quote-shared.tsx", "a/x/quote-details-screen.tsx")[1], "useQuoteDetailsScreenShared")

    def test_validation(self):
        bad = {"id": "T1", "file": "src/thing-screen.tsx", "action": "move", "where": "ThingScreen", "into": "src/ui.tsx",
               "name": "useThing", "select": [], "instructions": "", "done_when": "", "edits": []}
        with self.assertRaisesRegex(ValueError, "must be a new"):
            buckets.validate_tasks([bad], self.repo, 8, "T")
        with self.assertRaisesRegex(ValueError, "TypeScript or JavaScript"):
            buckets.validate_tasks([{**bad, "file": "README.md", "into": "src/x.ts"}], self.repo, 8, "T")
        ok = {"id": "T2", "file": "src/ui.tsx", "action": "edit", "instructions": "x", "done_when": "", "edits": []}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual([t["action"] for t in buckets.validate_tasks([{**bad, "into": ""}, ok], self.repo, 8, "T")], ["edit"])
        with self.assertRaisesRegex(ValueError, "share"):
            buckets.validate_tasks([{**bad, "action": "share", "from": ["src/ui.tsx"], "select": ["x"]}], self.repo, 8, "T")


class Offload(unittest.TestCase):
    MODELS = {"planner": {"model": "advisor-m"}, "reader": {"model": "reader-m"},
              "thinker": {"model": "thinker-m", "opencode_model": "prov/thinker-m"},
              "coder": {"model": "coder-m", "opencode_model": "prov/coder-m"}}

    def make(self, windows_up=True, **offload):
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", **self.MODELS,
                                                          **({"offload": offload} if offload else {})})
        b = buckets.Buckets(cfg, make_repo({"a.ts": "1\n"}), "x")
        b._windows_up = windows_up
        return b

    def test_chores_go_to_windows_when_it_is_up(self):
        self.assertEqual(self.make().chores["model"], "reader-m")
        self.assertFalse(self.make().chores["think"])
        self.assertEqual(self.make(windows_up=False).chores["model"], "advisor-m")
        self.assertEqual(self.make(chores=False).chores["model"], "advisor-m")

    def test_windows_reads_first_and_the_mac_joins_only_for_big_jobs(self):
        b = self.make()
        self.assertEqual([n for n, _ in b.readers(2)], ["windows"])
        self.assertEqual([n for n, _ in b.readers(3)], ["windows", "mac"])
        self.assertEqual([n for n, _ in self.make(windows_up=False).readers(5)], ["mac"])
        self.assertEqual([n for n, _ in self.make(survey="shared").readers(2)], ["mac", "windows"])

    def test_plan_on_windows_without_the_thinker_uses_the_coder_model(self):
        b = self.make()
        b.plan_on_windows()
        self.assertEqual(b.planner["model"], "reader-m")
        self.assertEqual([n for n, _ in b.readers(6)], ["windows"])

    def test_thinker_mode_keeps_everything_on_one_windows_model(self):
        b = self.make()
        b._thinker_up = True
        b.plan_on_windows(thinker=True)
        self.assertEqual(b.planner["model"], "thinker-m")
        self.assertTrue(b.planner["think"])
        self.assertEqual(b.chores["model"], "thinker-m")                # no swap for chores
        self.assertFalse(b.chores["think"])
        self.assertEqual([(n, c["model"]) for n, c in b.readers(4)], [("windows", "thinker-m")])
        self.assertEqual(b.errand_model, "prov/thinker-m")                # look-ups too

    def test_stuck_means_nothing_improved(self):
        self.assertTrue(buckets.stuck({"test"}, {"test"}))
        self.assertTrue(buckets.stuck({"test", "lint"}, {"test"}))     # worse
        self.assertFalse(buckets.stuck({"test"}, {"test", "typecheck"}))  # typecheck got fixed
        self.assertFalse(buckets.stuck(set(), {"test"}))               # all passing

    def test_escalation_hands_planning_to_the_mac_once(self):
        b = self.make()
        b._thinker_up, b._mac_up = True, True
        b._claude_allowed = False                                      # no Claude: the advisor is the top
        b.plan_on_windows(thinker=True)
        self.assertTrue(b.escalate_to_mac("round 2 made no progress on test"))
        self.assertEqual(b.planner["model"], "advisor-m")
        self.assertFalse(b.escalate_to_mac("again"))                   # only once
        b2 = self.make()
        b2._thinker_up, b2._mac_up = True, False
        b2._claude_allowed = False
        b2.plan_on_windows(thinker=True)
        self.assertFalse(b2.escalate_to_mac("Mac is off"))             # nowhere to go

    def test_machine_labels(self):
        self.assertEqual(buckets.machine_label({"url": "http://localhost:11434"}), "this machine")
        self.assertEqual(buckets.machine_label({"url": "http://gpu-box.lan:11434"}), "gpu-box.lan")
        self.assertEqual(buckets.machine_label({"url": "http://10.0.0.5:11434", "label": "Windows"}), "Windows")
        self.assertTrue(buckets.planner_on_worker({"planner": "windows"}))   # the older value still works
        self.assertFalse(buckets.planner_on_worker({"planner": "advisor"}))

    def test_unload_tolerates_a_missing_server(self):
        buckets.unload({"url": "http://127.0.0.1:9", "model": "m"})  # must not raise


class Skeletons(unittest.TestCase):
    TSC = ("packages/core/test/lint.test.ts(73,9): error TS2554: Expected 1 arguments, but got 0.\n"
           "src/a.ts(2,7): error TS2322: Type 'string' is not assignable to type 'number'.\n")

    def repo(self):
        return make_repo({"packages/core/test/lint.test.ts": "x\n" * 72 + "        rule(),\n",
                          "src/a.ts": "line1\nconst n: number = 'x';\n"})

    def test_kinds(self):
        files = {"src/a.ts": 2, "test/a.test.ts": 20}
        out = "src/a.ts(2,7): error TS2322: bad\n FAIL  test/a.test.ts > t\n ❯ test/a.test.ts:14:20\n"
        self.assertEqual([k for *_, k in buckets.parse_diagnostics(out, files, kinds=True)], ["check", "frame"])
        self.assertEqual(len(buckets.parse_diagnostics(out, files)[0]), 3)

    def test_skeleton_names_exact_lines_and_code(self):
        repo = self.repo()
        files = buckets.list_files(repo, [])
        tasks = buckets.skeleton_tasks({"typecheck": {"ok": False, "output": self.TSC}}, repo, files, "T", 8)
        self.assertEqual([t["file"] for t in tasks], ["packages/core/test/lint.test.ts", "src/a.ts"])
        self.assertIn("- line 73 (typecheck): error TS2554: Expected 1 arguments, but got 0.", tasks[0]["instructions"])
        self.assertIn("current code: rule(),", tasks[0]["instructions"])
        self.assertEqual(tasks[1]["id"], "T2")

    def test_only_mechanical_when_no_test_failures(self):
        repo = self.repo()
        files = buckets.list_files(repo, [])
        tsc_only = {"typecheck": {"ok": False, "output": self.TSC}, "lint": {"ok": True, "output": ""}}
        self.assertTrue(buckets.failures_only_mechanical(tsc_only, files))
        with_tests = {**tsc_only, "test": {"ok": False, "output": " FAIL  src/a.ts > x\n ❯ src/a.ts:2:3\n"}}
        self.assertFalse(buckets.failures_only_mechanical(with_tests, files))
        self.assertFalse(buckets.failures_only_mechanical({"t": {"ok": False, "output": "boom"}}, files))

    def test_follow_ups_get_the_exact_errors(self):
        repo = self.repo()
        files = buckets.list_files(repo, [])
        skeleton = buckets.skeleton_tasks({"typecheck": {"ok": False, "output": self.TSC}}, repo, files, "R2S", 8)
        review = [{"id": "R2T1", "file": "packages/core/test/lint.test.ts", "action": "edit",
                   "instructions": "Fix the rule() call.", "done_when": "typecheck passes", "read_first": []}]
        out = buckets.with_exact_errors(review, skeleton, 8)
        self.assertEqual(len(out), 2)
        self.assertIn("The exact errors the checks report in this file:\n- line 73", out[0]["instructions"])
        self.assertEqual(out[1]["file"], "src/a.ts")
        self.assertEqual(review[0]["instructions"], "Fix the rule() call.")  # not modified in place


class ResearchHypotheses(unittest.TestCase):
    def test_enough_with_open_hypotheses_gets_one_more_round(self):
        repo = make_repo({"src/a.ts": "export const a = 1;\n"})
        b = buckets.Buckets(buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local"}), repo, "fix the bug")
        replies = iter([
            {"enough": True, "read": [], "uses": [], "grep": [], "questions": [],
             "hypotheses": [{"cause": "wrong operator in compare()", "status": "open", "evidence": "-"},
                            {"cause": "SQL side", "status": "ruled_out", "evidence": "scope.ts:88"}]},
            {"enough": False, "read": ["src/a.ts"], "uses": [], "grep": [], "questions": [], "hypotheses": []},
            {"enough": True, "read": [], "uses": [], "grep": [], "questions": [],
             "hypotheses": [{"cause": "wrong operator in compare()", "status": "confirmed", "evidence": "a.ts:1"}]},
        ])
        prompts = []
        real = buckets.ask_json
        buckets.ask_json = lambda cfg, run, label, system, user, schema, validate, **kw: (
            prompts.append((label, user, kw.get("think"))) or next(replies))
        try:
            notes = b.research("code", buckets.list_files(repo, []), "guidance")
        finally:
            buckets.ask_json = real
        self.assertEqual([p[0] for p in prompts], ["research-1", "research-2", "research-3"])
        self.assertTrue(all(p[2] for p in prompts))                       # research rounds think now
        self.assertIn("SYSTEM: you said enough, but these hypotheses are still open: wrong operator", prompts[1][1])
        self.assertIn("[open] wrong operator in compare()", prompts[1][1])
        self.assertIn("READ src/a.ts (exact)", notes)
        self.assertIn("- [confirmed] wrong operator in compare() (a.ts:1)", notes)


class DocsHistoryPlanFile(unittest.TestCase):
    def test_package_docs_from_node_modules(self):
        repo = make_repo({"package.json": "{}", "packages/a/package.json": json.dumps({"name": "a"})})
        pkg = repo / "packages/a/node_modules/lib"
        pkg.mkdir(parents=True)
        (pkg / "package.json").write_text(json.dumps({"name": "lib", "version": "2.3.4", "types": "index.d.ts"}))
        (pkg / "README.md").write_text("# lib\nDoes things.\n")
        (pkg / "index.d.ts").write_text("/** Less or equal. */\nexport declare function lte(v: number): Op;\n")
        whole = buckets.package_docs(repo, "lib")
        self.assertIn("DOCS lib@2.3.4 (the installed copy, exact)", whole)
        self.assertIn("Does things.", whole)
        one = buckets.package_docs(repo, "lib#lte")
        self.assertIn("export declare function lte(v: number): Op;", one)
        self.assertEqual(one.count("--- lib/"), 1)                       # one excerpt, no overlap
        self.assertIn("no declaration of `nope`", buckets.package_docs(repo, "lib#nope"))
        self.assertIn("not installed", buckets.package_docs(repo, "missing-pkg-xyz"))

    def test_history_text(self):
        repo = make_repo({"a.ts": "one\ntwo\nthree\n"})
        (repo / "a.ts").write_text("one\nTWO\nthree\n")
        commit_all(repo, "change line two")
        out = buckets.history_text(repo, buckets.list_files(repo, []), "a.ts:2-2")
        self.assertIn("HISTORY a.ts:2-2", out)
        self.assertIn("change line two", out)
        self.assertIn("TWO", out)
        self.assertIn("no such file", buckets.history_text(repo, {}, "nope.ts:1"))

    def test_plan_file_is_validated(self):
        repo = make_repo({"src/a.ts": "1\n"})
        plan = buckets.validate_plan({"summary": "", "tasks": [
            {"file": "src/a.ts", "action": "edit", "instructions": "do it", "done_when": "done"},
            {"file": "src/new.ts", "action": "edit", "instructions": "make it", "done_when": "exists"}]}, repo, 8)
        self.assertEqual([(t["id"], t["action"]) for t in plan["tasks"]], [("T1", "edit"), ("T2", "create")])


class Setup(unittest.TestCase):
    def cfg(self):
        return buckets.deep_merge(buckets.DEFAULT_CONFIG, {
            "thinker": {"url": "http://gpu:11434", "model": "think-m", "label": "gpu"},
            "coder": {"url": "http://gpu:11434", "model": "code-m", "label": "gpu"},
            "reader": {"url": "http://gpu:11434", "model": "think-m", "label": "gpu"},
            "planner": {"url": "http://localhost:11434", "model": "big-m"}})

    def test_install_opencode_merges_and_reuses_providers(self):
        path = Path(tempfile.mkdtemp()) / "opencode.json"
        path.write_text(json.dumps({"theme": "dark", "provider": {"mine": {
            "npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://gpu:11434/v1"}, "models": {}}},
            "agent": {"build": {"model": "x/y"}}}))
        cfg = self.cfg()
        ids = buckets.install_opencode(cfg, path)
        oc = json.loads(path.read_text())
        self.assertEqual(oc["theme"], "dark")                               # untouched
        self.assertEqual(oc["agent"]["build"], {"model": "x/y"})            # untouched
        self.assertEqual(ids["http://gpu:11434"], "mine")                   # existing provider reused
        self.assertIn("buckets_this_machine", oc["provider"])               # new one for localhost
        self.assertEqual(set(oc["provider"]["mine"]["models"]), {"think-m", "code-m"})
        self.assertEqual(cfg["coder"]["opencode_model"], "mine/code-m")
        coder = oc["agent"]["buckets-coder"]
        self.assertEqual((coder["permission"]["edit"], coder["permission"]["bash"]), ("allow", "deny"))
        self.assertEqual(oc["agent"]["buckets-reader"]["permission"]["edit"], "deny")
        self.assertTrue(path.with_suffix(".json.bak").exists())

    def test_setup_without_questions_keeps_the_current_setup(self):
        home = Path(tempfile.mkdtemp())
        cfg_path, oc_path = home / "buckets.json", home / "opencode.json"
        cfg_path.write_text(json.dumps({k: v for k, v in self.cfg().items() if k in ("thinker", "coder", "reader", "planner")}))
        saved = (buckets.CONFIG_PATH, buckets.OPENCODE_CONFIG, buckets.doctor, buckets.server_models)
        buckets.CONFIG_PATH, buckets.OPENCODE_CONFIG = cfg_path, oc_path
        buckets.doctor = lambda cfg, repo: 0
        buckets.server_models = lambda url: None
        try:
            self.assertEqual(buckets.cmd_setup(yes=True), 0)
        finally:
            buckets.CONFIG_PATH, buckets.OPENCODE_CONFIG, buckets.doctor, buckets.server_models = saved
        written = json.loads(cfg_path.read_text())
        self.assertEqual((written["thinker"]["model"], written["thinker"]["label"]), ("think-m", "gpu"))
        self.assertEqual(written["coder"]["model"], "code-m")
        self.assertEqual(written["planner"]["model"], "big-m")
        self.assertEqual(written["offload"]["planner"], "worker")
        self.assertIn("buckets-coder", json.loads(oc_path.read_text())["agent"])


class FakeModels:
    """A stand-in for Ollama that answers each buckets step from the JSON schema it asks for."""

    REPLIES = [
        ("intent", {"intent": "code", "difficulty": "light", "reason": "a small fix", "plan_only": False,
                    "checks_define_done": False, "examples": [], "commits": [], "onto": ""}),
        ("dirs", {"dirs": ["."]}),
        ("files", {"files": ["calc.ts"], "reason": "the function"}),
        ("done", {"done": True, "summary": "add() now returns the sum; the check passes.", "issues": [], "tasks": []}),
        ("tasks", {"summary": "Fix add().", "tasks": [{"id": "T1", "file": "calc.ts", "action": "edit",
                                                       "instructions": "In add(), return a + b instead of a - b.",
                                                       "done_when": "add returns the sum", "read_first": []}]}),
    ]

    def __init__(self):
        import http.server
        import threading as th
        self.labels = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, payload: bytes, kind="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self.reply(json.dumps({"models": [{"name": "fake-m:latest"}]}).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                if self.path != "/api/chat":
                    return self.reply(b"{}")
                props = set((body.get("format") or {}).get("properties", {}))
                answer = next((r for key, r in outer.REPLIES if key in props), None)
                outer.labels.append(next((key for key, _ in outer.REPLIES if key in props), "text"))
                content = json.dumps(answer) if answer else "A plain answer."
                lines = [{"message": {"content": content}}, {"done": True, "done_reason": "stop", "eval_count": 5}]
                self.reply(b"".join(json.dumps(l).encode() + b"\n" for l in lines), "application/x-ndjson")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        th.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


FAKE_OPENCODE = """#!/usr/bin/env python3
import re, sys
prompt = sys.argv[-1]
m = re.search(r"YOUR TASK \\((\\w+)\\): \\w+ the file `([^`]+)`", prompt)
path = m.group(2)
text = open(path).read().replace("a - b", "a + b")
open(path, "w").write(text)
print("> buckets-coder · fake-m")
print("→ Read " + path)
print("← Edit " + path)
print("DONE: add() returns a + b")
"""


class EndToEnd(unittest.TestCase):
    def test_a_whole_run_then_merge(self):
        fake = FakeModels()
        repo = make_repo({"calc.ts": "export function add(a: number, b: number) {\n  return a - b;\n}\n",
                          ".buckets.json": json.dumps({"checks": {"sum": "grep -q 'a + b' calc.ts"},
                                                       "fixers": [], "index_check": False})})
        start = buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        tools = Path(tempfile.mkdtemp())
        (tools / "opencode").write_text(FAKE_OPENCODE)
        (tools / "opencode").chmod(0o755)
        role = {"url": fake.url, "model": "fake-m", "label": "the fake box"}
        cfg = {"planner": role, "reader": role, "thinker": {**role, "opencode_model": "p/fake-m"},
               "coder": {**role, "opencode_model": "p/fake-m"}, "opencode_bin": str(tools / "opencode"),
               "notify": False, "mode": "local"}
        buckets.CONFIG_PATH.write_text(json.dumps(cfg))
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                code = buckets.main(["-C", str(repo), "-y", "--local", "fix add so it returns the sum"])
            self.assertEqual(code, 0, err.getvalue()[-2000:])
            self.assertIn("Result:** done", out.getvalue())
            self.assertIn("all on the fake box", err.getvalue())             # the label shows in messages
            self.assertEqual(buckets.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip(), start)
            branch = [l.strip() for l in buckets.git(repo, "branch").splitlines() if "buckets/" in l][0]
            self.assertIn("a + b", buckets.git(repo, "show", f"{branch}:calc.ts"))
            self.assertIn("a - b", (repo / "calc.ts").read_text())            # your branch untouched
            # the last call notes what calc.ts is for, in the project map
            # the planning conversation's first turn has no forced format ("text"), then the plan itself
            self.assertEqual(fake.labels, ["intent", "files", "text", "tasks", "done", "files"])
            journal = (repo / ".buckets/journal.md").read_text()
            self.assertIn("Asked: fix add so it returns the sum", journal)
            self.assertIn("Changed: calc.ts", journal)
            self.assertIn("calc.ts", json.loads((repo / ".buckets/map.json").read_text())["files"])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(buckets.main(["-C", str(repo), "merge"]), 0)   # .buckets/ doesn't block it
            self.assertIn("a + b", (repo / "calc.ts").read_text())
            self.assertNotIn("buckets/", buckets.git(repo, "branch"))
        finally:
            fake.close()
            buckets.CONFIG_PATH.unlink(missing_ok=True)


class Escalation(unittest.TestCase):
    def make(self, files=None, rc=None, **cfg_over):
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "planner": {"model": "advisor-m"},
                                                          "thinker": {"model": "thinker-m"}, **cfg_over})
        repo = make_repo(files or {"a.ts": "1\n", **({".buckets.json": json.dumps(rc)} if rc else {})})
        b = buckets.Buckets(cfg, repo, "x")
        b._thinker_up = b._mac_up = True
        b.plan_on_windows(thinker=True)
        return b

    def test_ladder_thinker_then_advisor_then_claude_within_the_limit(self):
        b = self.make()
        b._claude_allowed = True
        self.assertEqual(b.escalate("one")[0], "advisor")
        b.planner = dict(b.cfg["planner"])                    # the advisor is planning now
        self.assertEqual(b.escalate("two")[0], "claude")
        self.assertIsNone(b.escalate("three"))                # the limit is 2
        self.assertEqual(len(b.escalations), 2)
        self.assertIn("one → the advisor", b.escalations[0])

    def test_nowhere_to_go(self):
        b = self.make(rc={"mode": "local"})
        b._mac_up = False
        self.assertFalse(b.claude_allowed())                  # local-only repo: never Claude
        self.assertIsNone(b.escalate("stuck"))

    def test_a_too_long_answer_gets_one_short_retry(self):
        calls = []

        def fake_chat(cfg, run, label, system, user, **kw):
            calls.append((label, kw.get("think")))
            if len(calls) == 1:
                raise buckets.ReplyTooLong("too long")
            return '{"ok": true}'

        real, buckets.chat = buckets.chat, fake_chat
        try:
            out = buckets.ask_json({}, None, "plan", "s", "u", {}, lambda d: d, think=True)
        finally:
            buckets.chat = real
        self.assertEqual(out, {"ok": True})
        self.assertEqual(calls, [("plan", True), ("plan-short", False)])

    def test_rewrite_tasks_merges_new_instructions(self):
        b = self.make(files={"src/a.ts": "x\n" * 80})
        tasks = [{"id": "R2T1", "file": "src/a.ts", "action": "edit", "instructions": "- line 73 (typecheck): bad",
                  "done_when": "ok", "read_first": []},
                 {"id": "R2T2", "file": "src/a.ts", "action": "edit", "instructions": "other", "done_when": "ok",
                  "read_first": []}]
        seen = {}

        def fake(target, label, prompt, schema, validate):
            seen["prompt"] = prompt
            return validate({"tasks": [{"id": "R2T1", "instructions": "Change line 73 to `rule({})`.",
                                        "done_when": "typecheck passes"}]})

        b.ask_escalated = fake
        out = b.rewrite_tasks(tasks, "the same error is still there")
        self.assertEqual(out[0]["instructions"], "Change line 73 to `rule({})`.")
        self.assertEqual(out[1]["instructions"], "other")
        self.assertIn("=== src/a.ts:65-80 ===", seen["prompt"])      # around line 73 (the file ends at 80)

    def test_gate(self):
        b = self.make()
        v = b.gate({"done": True, "issues": [], "tasks": []}, [], None, {"t": {"ok": False}}, {"t": {"ok": True}})
        self.assertFalse(v["done"])
        self.assertIn("checks regressed: t", v["issues"])

    def test_second_opinion_on_sensitive_files_can_overrule_done(self):
        b = self.make(files={"src/auth.ts": "export const a = 1;\n", ".buckets.json": json.dumps({"checks": {}})})
        base = buckets.git(b.repo, "rev-parse", "HEAD").strip()
        (b.repo / "src/auth.ts").write_text("export const a = 2;\n")
        commit_all(b.repo, "change auth")
        b.cfg["max_rounds"] = 1
        b.code_task = lambda t, commit=True: {**t, "status": "done", "note": ""}
        b.auto_fix = lambda base: None
        b.api_impact = lambda base: None
        reviews = []

        def fake_review(plan, results, base, baseline, now, rnd, shortcuts, api=None, target=None, note=""):
            reviews.append((target[0] if target else "thinker", "second opinion" in note))
            if target:
                return {"done": False, "summary": "token check is wrong", "issues": ["x"], "tasks": []}
            return {"done": True, "summary": "fine", "issues": [], "tasks": []}

        b.review = fake_review
        task = {"id": "T1", "file": "src/auth.ts", "action": "edit", "instructions": "i", "done_when": "d", "read_first": []}
        with contextlib.redirect_stderr(io.StringIO()):
            _, verdict, _ = b.rounds({"summary": "s", "tasks": [task]}, [task], base, {}, {})
        self.assertEqual(reviews, [("thinker", False), ("advisor", True)])
        self.assertFalse(verdict["done"])
        self.assertIn("sensitive files changed: src/auth.ts", b.escalations[0])


FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
open(os.environ["FAKE_GH_LOG"], "a").write(json.dumps(args) + "\\n")
if args[:2] == ["pr", "create"]:
    print("https://github.com/me/repo/pull/7")
elif args[:2] == ["pr", "list"]:
    print(json.dumps([{"number": 7, "title": "feat: x", "headRefName": "feat/x", "baseRefName": "main",
                       "isDraft": False, "url": "https://github.com/me/repo/pull/7"}]))
elif args[:2] == ["pr", "view"] and "statusCheckRollup" in " ".join(args):
    print(json.dumps({"number": 7, "title": "feat: x", "state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE",
                      "reviewDecision": None, "url": "https://github.com/me/repo/pull/7", "statusCheckRollup": [
                          {"name": "test", "conclusion": "FAILURE"}, {"name": "lint", "conclusion": "SUCCESS"},
                          {"name": "build", "status": "IN_PROGRESS"}]}))
elif args[:2] == ["pr", "view"]:
    print(json.dumps({"number": 7}))
elif args[:2] == ["pr", "checks"]:
    print(json.dumps([{"name": "test", "state": "FAILURE", "link": "https://github.com/me/repo/actions/runs/99/job/1"}]))
elif args[:2] == ["run", "view"]:
    print("test\\tRun\\tsrc/a.ts(2,7): error TS2322: Type 'string' is not assignable.")
"""


class GitAssistantTests(unittest.TestCase):
    def setUp(self):
        import os
        self.remote = Path(tempfile.mkdtemp()) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        self.repo = make_repo({"src/a.ts": "const a = 1;\n", ".buckets.json": json.dumps({"checks": {}})})
        buckets.git(self.repo, "branch", "-M", "main")
        buckets.git(self.repo, "remote", "add", "origin", str(self.remote))
        buckets.git(self.repo, "push", "-q", "-u", "origin", "main")
        tools = Path(tempfile.mkdtemp())
        (tools / "gh").write_text(FAKE_GH)
        (tools / "gh").chmod(0o755)
        self.log = tools / "gh.log"
        os.environ["FAKE_GH_LOG"] = str(self.log)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "gh_bin": str(tools / "gh")})
        self.b = buckets.Buckets(cfg, self.repo, "x")
        self.g = buckets.GitAssistant(self.b)

    def gh_calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_branch_commit_squash_push(self):
        self.g.op_branch({"name": "feat/x"}, False)
        for n in (2, 3):
            (self.repo / "src/a.ts").write_text(f"const a = {n};\n")
            self.g.op_commit({"message": f"wip {n}"}, False)
        out = self.g.op_squash({"message": "feat: x"}, False)
        self.assertIn("2 commits →", out)
        self.assertEqual(buckets.git(self.repo, "rev-list", "--count", "origin/main..HEAD").strip(), "1")
        self.g.op_push({}, False)
        self.assertIn("feat/x", subprocess.run(["git", "branch"], cwd=self.remote, capture_output=True, text=True).stdout)
        with self.assertRaises(RuntimeError):
            self.g.op_commit({"message": "nothing"}, False)              # nothing to commit

    def test_rebase_conflict_is_aborted_cleanly(self):
        self.g.op_branch({"name": "feat/y"}, False)
        (self.repo / "src/a.ts").write_text("const a = 'mine';\n")
        self.g.op_commit({"message": "mine"}, False)
        other = Path(tempfile.mkdtemp()) / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        (other / "src/a.ts").write_text("const a = 'theirs';\n")
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "theirs"], cwd=other, check=True)
        subprocess.run(["git", "push", "-q"], cwd=other, check=True)
        with self.assertRaises(RuntimeError) as e:
            self.g.op_rebase({}, False)
        self.assertIn("conflicts in src/a.ts; rebase aborted", str(e.exception))
        self.assertEqual(self.g.current(), "feat/y")
        self.assertEqual((self.repo / "src/a.ts").read_text(), "const a = 'mine';\n")

    def test_prune_and_delete_safety(self):
        self.g.op_branch({"name": "done-branch"}, False)
        self.g.op_switch({"name": "main"}, False)
        self.g.op_branch({"name": "open-branch"}, False)
        (self.repo / "src/a.ts").write_text("const a = 9;\n")
        self.g.op_commit({"message": "unmerged"}, False)
        self.g.op_switch({"name": "main"}, False)
        self.assertEqual(self.g.op_prune_merged({}, False), "deleted done-branch")
        with self.assertRaises(RuntimeError):
            self.g.op_branch_delete({"name": "open-branch"}, False)      # unmerged: needs force and -y
        with self.assertRaises(RuntimeError):
            self.g.op_branch_delete({"name": "main"}, True)              # never the base
        self.g.op_branch_delete({"name": "open-branch", "force": True}, True)
        self.assertFalse(buckets.branch_exists(self.repo, "open-branch"))

    def test_pr_operations_use_gh(self):
        self.assertIn("pull/7", self.g.op_pr_create({"title": "feat: x", "body": "b", "draft": True}, False))
        self.assertIn("#7 feat: x (feat/x)", self.g.op_pr_list({}, False))
        status = self.g.op_pr_status({}, False)
        self.assertIn("CI: 1 passed, 1 failed, 1 pending", status)
        self.assertIn("failing: test", status)
        failures = self.g.op_pr_failures({"number": 7}, False)
        self.assertIn("src/a.ts", failures)
        self.assertIn("error TS2322", failures)
        create = next(c for c in self.gh_calls() if c[:2] == ["pr", "create"])
        self.assertIn("--draft", create)
        self.assertEqual(create[create.index("--base") + 1], "main")

    def test_pr_from_run_squashes_onto_the_remote_base(self):
        # A finished run: its branch has two buckets commits on top of main.
        buckets.git(self.repo, "switch", "-q", "-c", "buckets/fix-a")
        (self.repo / "src/a.ts").write_text("const a = 2;\n")
        commit_all(self.repo, "buckets T1: edit src/a.ts")
        (self.repo / "src/b.ts").write_text("export const b = 1;\n")
        commit_all(self.repo, "buckets T2: create src/b.ts")
        buckets.git(self.repo, "switch", "-q", "main")
        (self.b.run.dir / "meta.json").write_text(json.dumps({
            "repo": str(self.repo), "kind": "code", "request": "fix a", "done": True,
            "branch": "buckets/fix-a", "merge_into": "main", "report": "r"}))
        self.g.pr_text = lambda request, rng, diff: {"title": "fix: a", "body": "## Summary\n- fixes a",
                                                     "branch": "fix/a"}
        out = self.g.op_pr_from_run({}, True)
        self.assertIn("fix/a → https://github.com/me/repo/pull/7", out)
        self.assertEqual(self.g.current(), "main")                        # back where it started
        self.assertEqual(buckets.git(self.repo, "rev-list", "--count", "origin/main..fix/a").strip(), "1")
        self.assertEqual(buckets.git(self.repo, "log", "-1", "--format=%s", "fix/a").strip(), "fix: a")
        self.assertEqual(sorted(buckets.git(self.repo, "diff", "--name-only", "origin/main..fix/a").split()),
                         ["src/a.ts", "src/b.ts"])

    def test_handle_asks_the_model_then_runs_the_steps(self):
        real = buckets.ask_json
        buckets.ask_json = lambda cfg, run, label, system, user, schema, validate, **kw: validate(
            {"summary": "Make the branch and commit.", "ops": [
                {"op": "branch", "args": {"name": "chore/z"}},
                {"op": "commit", "args": {"message": "chore: z"}}]})
        (self.repo / "src/a.ts").write_text("const a = 5;\n")
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(self.g.handle("make a branch and commit", yes=True), 0)
                self.assertEqual(self.g.handle("x", yes=False), 2)        # no terminal, no -y: nothing changes
        finally:
            buckets.ask_json = real
        self.assertIn("2. commit(message='chore: z')", err.getvalue())
        self.assertEqual(buckets.git(self.repo, "log", "-1", "--format=%s").strip(), "chore: z")

    def test_from_a_plain_folder_to_a_github_repo(self):
        folder = Path(tempfile.mkdtemp()) / "tool"
        folder.mkdir()
        (folder / "tool.py").write_text("print('hi')\n")
        (folder / "__pycache__").mkdir()
        (folder / "__pycache__/tool.pyc").write_text("x")
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "gh_bin": self.g.cfg["gh_bin"]})
        g = buckets.GitAssistant(buckets.Buckets(cfg, folder, "x"))
        self.assertIn("NOT a git repository yet", g.state())
        with self.assertRaises(RuntimeError):
            g.op_repo_create({"name": "tool"}, True) if g.is_repo() else (_ for _ in ()).throw(RuntimeError("no repo"))
        g.op_init({}, False)
        self.assertTrue(g.is_repo())
        self.assertEqual(g.op_gitignore({"entries": ["__pycache__/", "*.pyc", "__pycache__/"]}, False),
                         "added __pycache__/, *.pyc")
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=folder, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=folder, check=True)
        with self.assertRaises(RuntimeError):
            g.op_repo_create({"name": "tool"}, True)                     # no commit yet
        g.op_commit({"message": "chore: initial import"}, False)
        self.assertEqual(sorted(buckets.git(folder, "ls-files").split()), [".gitignore", "tool.py"])
        g.op_repo_create({"name": "tool", "description": "a tool"}, True)
        create = next(c for c in self.gh_calls() if c[:2] == ["repo", "create"])
        self.assertIn("--private", create)                                # private unless asked
        self.assertEqual(create[create.index("--source") + 1], ".")
        self.assertIn("--push", create)

    def plain_folder(self):
        folder = Path(tempfile.mkdtemp()) / "tool"
        folder.mkdir()
        (folder / "tool.py").write_text("print('hi')\n")
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "gh_bin": self.g.cfg["gh_bin"]})
        g = buckets.GitAssistant(buckets.Buckets(cfg, folder, "x"))
        g.env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                      "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        return folder, g

    def plan(self, ops, prompts=None):
        """Stand in for the model: always answer with these steps, keeping the prompts it was sent."""
        def fake(cfg, run, label, system, user, schema, validate, **kw):
            if prompts is not None:
                prompts.append(user)
            return validate({"summary": "Publish it.", "ops": ops})
        return mock.patch.object(buckets, "ask_json", fake)

    def test_a_repo_with_no_commits_yet(self):
        folder, g = self.plain_folder()
        g.op_init({}, False)
        self.assertEqual(g.current(), "main")                            # used to crash on the unborn HEAD
        state = g.state()
        self.assertIn("Current branch: main", state)
        self.assertIn("(none yet: commit before repo_create)", state)

    def test_missing_init_and_first_commit_are_filled_in(self):
        folder, g = self.plain_folder()
        with self.plan([{"op": "repo_create", "args": {"visibility": "public"}}]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.handle("publish this", yes=True), 0)
        self.assertEqual(buckets.git(folder, "log", "-1", "--format=%s").strip(), "chore: initial commit")
        create = next(c for c in self.gh_calls() if c[:2] == ["repo", "create"])
        self.assertEqual(create[2], "tool")                               # the folder's name by default
        self.assertIn("--public", create)

    def test_first_commit_has_no_paths(self):
        folder, g = self.plain_folder()
        ops = [{"op": "commit", "args": {"message": "initial", "paths": ["a.txt"]}}]
        result = g.fill_in(ops)
        commit_step = next(s for s in result if s["op"] == "commit")
        self.assertNotIn("paths", commit_step["args"])

    def test_go_ahead_runs_the_plan_that_wasnt_answered(self):
        folder, g = self.plain_folder()
        ops = [{"op": "init", "args": {}}, {"op": "gitignore", "args": {"entries": ["__pycache__/"]}}]
        with self.plan(ops), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.handle("make this a git repo", yes=False), 2)   # no terminal: not confirmed
        self.assertFalse(g.is_repo())
        self.assertEqual(buckets.pending_git(folder)["request"], "make this a git repo")
        with self.plan([]) as _, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(g.handle("yes go ahead", yes=False), 0)
        self.assertIn("going ahead with the steps for: make this a git repo", err.getvalue())
        self.assertTrue(g.is_repo())
        self.assertIsNone(buckets.pending_git(folder))                   # done: nothing to follow up on

    def test_a_follow_up_is_planned_with_the_request_before_it(self):
        folder, g = self.plain_folder()
        prompts = []
        with self.plan([], prompts), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(g.handle("create a public GitHub repo for this", yes=False), 1)
            g.handle("name it buckets", yes=False)
        self.assertIn("create a public GitHub repo for this\nFollow-up: name it buckets", prompts[1])

    def test_answering_with_a_change_plans_again(self):
        folder, g = self.plain_folder()
        prompts, answers = [], iter(["make it public and call it tool-cli", "y"])
        plans = iter([[{"op": "repo_create", "args": {}}],
                      [{"op": "repo_create", "args": {"name": "tool-cli", "visibility": "public"}}]])
        def fake(cfg, run, label, system, user, schema, validate, **kw):
            prompts.append(user)
            return validate({"summary": "Publish it.", "ops": next(plans)})
        with mock.patch.object(buckets, "ask_json", fake), mock.patch.object(buckets, "interactive", lambda: True), \
                mock.patch.object(buckets, "ask_line", lambda q, timeout=30: next(answers)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(g.handle("publish this", yes=False), 0)
        self.assertIn("publish this\nFollow-up: make it public and call it tool-cli", prompts[1])
        self.assertIn("→ github.com/", err.getvalue())                    # the target is spelled out
        create = next(c for c in self.gh_calls() if c[:2] == ["repo", "create"])
        self.assertEqual(create[2:4], ["tool-cli", "--public"])

    def test_affirmations(self):
        for text in ("y", "yes", "Yes, go ahead!", "ok do it", "sure"):
            self.assertTrue(buckets.is_affirmation(text), text)
        for text in ("yes but make it private", "name it buckets", "", "no"):
            self.assertFalse(buckets.is_affirmation(text), text)

    def test_never_init_the_home_folder(self):
        folder, g = self.plain_folder()
        with mock.patch.object(buckets.Path, "home", return_value=folder):
            with self.assertRaises(RuntimeError):
                g.op_init({}, False)
        self.assertFalse(g.is_repo())

    def test_chat_runs_each_line_as_a_request(self):
        lines = iter(["make this a git repo", "", "yes go ahead", "exit", "never reached"])
        seen = []
        def fake_main(argv):
            seen.append(argv)
            if argv[-1] == "yes go ahead":
                buckets.die("a failing request doesn't end the chat")
        with mock.patch("builtins.input", lambda prompt="": next(lines)), \
                mock.patch.object(buckets, "main", fake_main), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(buckets.chat_loop(["-v"]), 0)
        self.assertEqual(seen, [["-v", "--", "make this a git repo"], ["-v", "--", "yes go ahead"]])

    def test_error_excerpt_and_ci_rerun(self):
        lines = ["Job\tInit\t2026-10-09T21:40:37.3Z ##[group]Starting postgres service container",
                 "Job\tInit\t2026-10-09T21:40:52.5Z Error response from daemon: pull limit",
                 "Job\tInit\t2026-10-09T21:41:33.7Z ##[error]Docker pull failed with exit code 1",
                 "Job\tInit\t2026-10-09T21:41:34.0Z ##[endgroup]"]
        self.assertEqual(buckets.error_excerpt(lines).splitlines(), [
            "##[group]Starting postgres service container", "Error response from daemon: pull limit",
            "##[error]Docker pull failed with exit code 1"])
        self.assertEqual(self.g.op_ci_rerun({"number": 7}, True), "#7: re-running the failed jobs of run(s) 99")
        self.assertIn(["run", "rerun", "99", "--failed"], self.gh_calls())

    def test_unknown_operations_are_rejected(self):
        with self.assertRaises(ValueError):
            buckets.validate_git_ops({"summary": "", "ops": [{"op": "rm_rf", "args": {}}]})

    def test_op_status(self):
        from unittest.mock import patch
        with patch.object(self.g, "git", return_value="") as mock_git:
            self.g.op_status({}, False)
        mock_git.assert_called_with("status", "-sb")


    def test_op_log(self):
        from unittest.mock import patch
        with patch.object(self.g, "git", return_value="log output") as mock_git:
            self.g.op_log({}, False)
        mock_git.assert_called_with("log", "--oneline", "-n", "10")

    def test_op_branches(self):
        from unittest.mock import patch
        with patch.object(self.g, "git", return_value="") as mock_git:
            self.g.op_branches({}, False)
        mock_git.assert_called_with("branch", "-vv")

    def test_op_remotes(self):
        from unittest.mock import patch
        with patch.object(self.g, "git", return_value="") as mock_git:
            self.g.op_remotes({}, False)
        mock_git.assert_called_with("remote", "-v")

    def test_op_tags(self):
        from unittest.mock import patch
        # Test default count (10)
        mock_git = patch.object(self.g, "git", return_value="tag1\ntag2\ntag3").start()
        result = self.g.op_tags({}, False)
        mock_git.assert_called_with("tag", "--sort=-creatordate", "--format=%(refname:short) %(creatordate:short)")
        self.assertEqual(result, "tag1\ntag2\ntag3")
        mock_git.stop()

        # Test custom count
        mock_git = patch.object(self.g, "git", return_value="t1\nt2\nt3\nt4\nt5").start()
        result = self.g.op_tags({"count": 2}, False)
        self.assertEqual(result, "t1\nt2")
        mock_git.stop()


class OpencodeFolder(unittest.TestCase):
    def test_each_repo_path_gets_its_own_opencode_data_folder(self):
        a, b = buckets.opencode_env(Path("/work/app")), buckets.opencode_env(Path("/tmp/clones/app"))
        self.assertNotEqual(a["XDG_DATA_HOME"], b["XDG_DATA_HOME"])
        self.assertTrue(a["XDG_DATA_HOME"].startswith(str(buckets.STATE_DIR / "opencode")))
        self.assertEqual(a["PATH"], os.environ["PATH"])
        self.assertEqual(a["PWD"], str(Path("/work/app").resolve()))  # opencode reads its folder from $PWD

    def test_run_proc_passes_the_environment(self):
        for on_line in (None, lambda line: None):
            code, out, _ = buckets.run_proc(["sh", "-c", "echo $XDG_DATA_HOME"], Path("."), 10, on_line=on_line,
                                            env={**os.environ, "XDG_DATA_HOME": "/here"})
            self.assertEqual(out.strip(), "/here")


class LiveEvents(unittest.TestCase):
    def setUp(self):
        self.saved = dict(buckets.LIVE)
        self.repo = make_repo({"a.txt": "a"})
        self.run = buckets.Run(self.repo, "do the thing", "local")
        self.events = self.run.dir / "events.jsonl"

    def tearDown(self):
        buckets.LIVE.update(self.saved)

    def lines(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()]

    def test_a_run_starts_its_events_with_the_request_and_records_what_it_says(self):
        buckets.say("Step 1/5 · Understanding your request")
        first, said = self.lines()
        self.assertEqual((first["ev"], first["request"], first["mode"], first["run"]),
                         ("run", "do the thing", "local", self.run.dir.name))
        self.assertTrue(said["text"].endswith("Step 1/5 · Understanding your request"))

    def test_a_model_call_is_a_session_with_a_start_progress_and_end(self):
        def stream(url, body, label, *a, **kw):
            buckets.live_progress(5, 3)
            buckets.live_progress(6, 3)  # within the second: not shown
            return "answer", "", {"prompt_eval_count": 120, "eval_count": 6}
        wrapped = buckets.live_stream(stream)
        wrapped("http://192.168.1.9:8080", {"model": "m", "think": True, "messages": [{"content": "x" * 400}]}, "plan")
        call, progress, end = self.lines()[1:]
        self.assertEqual((call["ev"], call["label"], call["machine"], call["think"]), ("call", "plan", "windows", True))
        self.assertEqual((progress["id"], progress["tokens"]), (call["id"], 5))
        self.assertEqual((end["ev"], end["ok"], end["tokens_in"], end["tokens_out"]), ("call_end", True, 120, 6))

    def test_a_failed_call_ends_as_failed(self):
        def stream(url, body, label, *a, **kw):
            raise buckets.StreamCut("gone")
        with self.assertRaises(buckets.StreamCut):
            buckets.live_stream(stream)("http://localhost:11434", {"messages": []}, "review")
        call, end = self.lines()[1:]
        self.assertEqual((call["machine"], end["ok"], end["error"]), ("mac", False, "StreamCut"))

    def test_the_server_reads_whole_lines_from_an_offset_and_lists_runs(self):
        with mock.patch.dict(os.environ, {"BUCKETS_REGRESS_STATE": str(_SANDBOX / "no-suite")}):
            buckets.say("one")
            got = buckets.live_events(self.run.dir.name, 0)
            self.assertEqual([e["ev"] for e in got["lines"]], ["run", "say"])
            with self.events.open("a") as f:
                f.write('{"t": 1, "ev": "say", "text": "half')  # still being written
            more = buckets.live_events(self.run.dir.name, got["next"])
            self.assertEqual(more, {"lines": [], "next": got["next"]})
            self.assertEqual(buckets.live_events("../etc", 0)["lines"], [])
            runs = buckets.live_runs()
            mine = next(r for r in runs if r["run"] == self.run.dir.name)
            self.assertEqual((mine["request"], mine["done"]), ("do the thing", "running"))


class NativeCoder(unittest.TestCase):
    """The coder as buckets' own tool loop: read, search, edit (tolerant of indentation), write; only the task's file."""

    def setUp(self):
        self.repo = make_repo({"src/m.py": "def run(x):\n    y = x\n    return y + 1\n",
                               "src/n.py": "from src.m import run\n\nVALUE = run(3)\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False,
                                                          "coder": {"engine": "native"}})
        self.b = buckets.Buckets(cfg, self.repo, "make run add 2")
        self.task = {"id": "T1", "file": "src/m.py", "action": "edit", "instructions": "make run add 2",
                     "done_when": "run adds 2", "read_first": [], "where": "", "edits": []}

    def tool(self, name, **args):
        return self.b.coder_tool(self.task, {"id": "1", "name": name, "arguments": args})

    def test_replace_once_exact_then_by_indentation(self):
        self.assertEqual(buckets.replace_once("a\nb\nc", "b", "B")[0], "a\nB\nc")
        text = "def f():\n    if x:\n        go()\n    done()\n"
        got, first, last = buckets.replace_once(text, "if x:\n    go()", "if x:\n    go(1)\n    go(2)")
        self.assertEqual(got, "def f():\n    if x:\n        go(1)\n        go(2)\n    done()\n")
        self.assertEqual((first, last), (2, 4))
        with self.assertRaisesRegex(ValueError, "appears 2 times"):
            buckets.replace_once("a\na\n", "a", "b")
        with self.assertRaisesRegex(ValueError, "closest line is 2"):
            buckets.replace_once("x = 1\nreturn total + 1\n", "return totl + 1", "y")

    def test_tools_read_search_and_change_only_the_task_file(self):
        self.assertIn("    3|     return y + 1", self.tool("read", path="src/m.py", offset=2, limit=5))
        self.assertIn("src/n.py:3", self.tool("search", pattern="run\\("))
        self.assertIn("you may change only", self.tool("edit", path="src/n.py", old="VALUE", new="V"))
        self.assertIn("outside the repository", self.tool("read", path="../../etc/passwd"))
        self.assertIn("wouldn't parse", self.tool("edit", path="src/m.py", old="y + 1", new="y +"))
        out = self.tool("edit", path="src/m.py", old="return y + 1", new="return y + 2")
        self.assertIn("    3|     return y + 2", out)
        self.assertIn("return y + 2", (self.repo / "src/m.py").read_text())
        self.assertIn("exists; change it with edit", self.tool("write", path="src/m.py", content="x"))

    def test_the_loop_edits_then_finishes_with_the_done_line(self):
        turns = []
        def fake_turn(cfg, run, label, messages, **kw):
            turns.append((label, kw.get("tools") is buckets.CODER_TOOLS))
            if len(turns) == 1:
                return "", "", [{"id": "a", "name": "edit", "arguments": {"path": "src/m.py", "old": "return y + 1",
                                                                          "new": "return y + 2"}}], False
            self.assertIn("Edited `src/m.py`", messages[-1]["content"])
            return "DONE: run adds 2", "", [], False
        with mock.patch.object(buckets, "chat_turn", fake_turn), contextlib.redirect_stderr(io.StringIO()):
            got = self.b.code_task(self.task)
        self.assertEqual(got["status"], "done")
        self.assertEqual([t[0] for t in turns], ["code-T1-1", "code-T1-2"])
        self.assertTrue(all(t[1] for t in turns))
        self.assertIn("buckets T1", subprocess.run(["git", "log", "-1", "--format=%s"], cwd=self.repo,
                                                   capture_output=True, text=True).stdout)

    def test_a_reply_without_tools_or_a_done_line_is_nudged_once(self):
        replies = iter([("I will change it.", [], ), ("Still thinking about it.", [])])
        def fake_turn(cfg, run, label, messages, **kw):
            text, calls = next(replies)
            return text, "", calls, False
        with mock.patch.object(buckets, "chat_turn", fake_turn):
            log, timed_out = self.b.native_coder(self.task, "PROMPT", time.time() + 60)
        self.assertEqual(log.splitlines(), ["I will change it.", "Still thinking about it."])
        self.assertFalse(timed_out)

    def test_old_tool_results_are_shortened_to_fit(self):
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                    {"role": "tool", "content": "x" * 90000}, {"role": "tool", "content": "y" * 900}]
        self.b.fit_messages(messages, {"num_ctx": 32768, "max_tokens": 8000})
        self.assertIn("shortened", messages[2]["content"])
        self.assertEqual(messages[3]["content"], "y" * 900)


class OneRunAtATime(unittest.TestCase):
    """One buckets run per machine; a second stops with what's running, or queues with --wait."""

    HOLD = """
import sys, time
sys.path.insert(0, {here!r})
import buckets
from pathlib import Path
buckets.LOCK_PATH = Path({lock!r})
with buckets.one_run(Path({repo!r}), "split the big screen"):
    print("held", flush=True)
    time.sleep({secs})
"""

    def setUp(self):
        self.repo = make_repo({"a.txt": "a"})
        self.lock = _SANDBOX / f"lock-{time.time_ns()}"
        self.saved = buckets.LOCK_PATH
        buckets.LOCK_PATH = self.lock

    def tearDown(self):
        buckets.LOCK_PATH = self.saved

    def hold(self, secs):
        code = self.HOLD.format(here=str(Path(buckets.__file__).parent), lock=str(self.lock), repo=str(self.repo), secs=secs)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        self.addCleanup(p.kill)
        return p

    def test_a_second_run_stops_and_names_the_one_going(self):
        self.hold(30)
        d = buckets.active_run()
        self.assertEqual((d["request"], Path(d["repo"]).resolve()), ("split the big screen", self.repo.resolve()))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            with buckets.one_run(self.repo, "another job"):
                self.fail("must not start")
        self.assertIn('another buckets run is going: "split the big screen"', err.getvalue())
        self.assertIn("--wait", err.getvalue())

    def test_with_wait_it_starts_when_the_other_finishes(self):
        p = self.hold(2)
        start = time.time()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with buckets.one_run(self.repo, "another job", wait=True):
                self.assertEqual(buckets.active_run()["pid"], os.getpid())  # now it's this run
        self.assertGreater(time.time() - start, 1)
        self.assertIn("waiting for the other buckets run", err.getvalue())
        p.wait()
        self.assertIsNone(buckets.active_run())

    def test_a_crashed_run_leaves_no_lock(self):
        p = self.hold(30)
        p.kill()
        p.wait()
        self.assertIsNone(buckets.active_run())
        with buckets.one_run(self.repo, "next"):
            pass

    def test_merge_and_discard_wait_for_a_run_in_the_same_repo_only(self):
        self.hold(30)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            buckets.busy_here(self.repo)
        buckets.busy_here(make_repo({"b.txt": "b"}))  # another repo: fine


class ConfirmEvidence(unittest.TestCase):
    def test_a_name_no_longer_in_the_changed_files_is_evidence_not_a_crash(self):
        repo = make_repo({"src/screen.tsx": "const renderOld = () => null\n"})
        (repo / "src/screen.tsx").write_text("const New = () => null\n")
        subprocess.run(["git", "commit", "-qam", "rename"], cwd=repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, repo, "x")
        seen = []
        def fake_turn(cfg, run, label, messages, **kw):
            seen.append(messages[-1]["content"])
            return '{"real": [], "why": "it was removed"}', "", [], False
        with mock.patch.object(buckets, "chat_turn", fake_turn), contextlib.redirect_stderr(io.StringIO()):
            got = b.confirm_claims(cfg["planner"], "PROMPT", {"done": True}, ["`renderOld` is still used"], 1)
        self.assertEqual(got, [])
        self.assertIn("(not found in the changed files)", seen[0])


class WindowsAgent(unittest.TestCase):
    """`buckets win` against a real agent (tools/windows-agent.cjs), started here on a free port."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node is needed")
        cls.tmp = Path(tempfile.mkdtemp())
        cls.agent = cls.tmp / "windows-agent.cjs"  # a copy: `update` rewrites the agent's own file
        shutil.copy2(Path(buckets.__file__).parent / "tools" / "windows-agent.cjs", cls.agent)
        (cls.tmp / "token").write_text("t" * 32 + "\n")
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        cls.proc = subprocess.Popen(["node", str(cls.agent), "--root", str(cls.tmp / "copies"), "--token-file",
                                     str(cls.tmp / "token"), "--port", str(cls.port), "--host", "127.0.0.1",
                                     "--model-url", "http://127.0.0.1:1"], stdout=subprocess.PIPE, text=True)
        cls.proc.stdout.readline()  # listening
        cls.cfg = {"agent": {"url": f"http://127.0.0.1:{cls.port}", "token_file": str(cls.tmp / "token")}}

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.proc.stdout.close()

    def win(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = buckets.cmd_win(self.cfg, list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_status_and_commands_with_their_exit_codes(self):
        code, _, err = self.win("status")
        self.assertEqual(code, 0)
        self.assertIn("agent 3 on", err)
        self.assertEqual(self.win("echo", "hello from the other machine")[:2], (0, "hello from the other machine\n"))
        self.assertEqual(self.win("--bash", "--cwd", str(self.tmp), "pwd; exit 3")[:2], (3, str(self.tmp.resolve()) + "\n"))
        code, _, err = self.win("--timeout", "1", "sleep 5")
        self.assertEqual(code, 124)
        self.assertIn("stopped after 1s", err)

    def test_detached_commands_return_at_once(self):
        start = time.time()
        code, _, err = self.win("--detach", "sleep 3; echo done")
        self.assertEqual(code, 0)
        self.assertLess(time.time() - start, 2)
        self.assertIn("started (pid", err)

    def test_a_wrong_token_or_no_agent_is_said_plainly(self):
        bad = self.tmp / "bad"
        bad.write_text("x" * 32)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            buckets.cmd_win({"agent": {**self.cfg["agent"], "token_file": str(bad)}}, ["status"])
        self.assertIn("401", err.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            buckets.cmd_win({"agent": {"url": "http://127.0.0.1:1", "token_file": str(bad)}}, ["status"])
        self.assertIn("didn't answer", err.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()) as err:
            buckets.cmd_win({}, ["status"])
        self.assertIn("buckets win setup", err.getvalue())

    def test_an_update_replaces_the_agent_and_keeps_the_previous_one(self):
        tmp = Path(tempfile.mkdtemp())
        agent = tmp / "windows-agent.cjs"
        shutil.copy2(self.agent, agent)
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen(["node", str(agent), "--root", str(tmp / "copies"), "--token-file", str(self.tmp / "token"),
                                 "--port", str(port), "--host", "127.0.0.1"], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        proc.stdout.readline()
        cfg = {"agent": {"url": f"http://127.0.0.1:{port}", "token_file": str(self.tmp / "token")}}
        with self.assertRaisesRegex(buckets.AgentError, "400"):
            buckets.agent_call(cfg, "POST", "/update", {"code": "// buckets agent\nthis is not javascript ((("})
        new = agent.read_text().replace("const VERSION = 3;", "const VERSION = 4;")
        self.assertEqual(buckets.agent_call(cfg, "POST", "/update", {"code": new})["restarting"], True)
        self.assertEqual(proc.wait(timeout=5), 0)  # the start script's loop starts it again
        self.assertIn("const VERSION = 4;", agent.read_text())
        self.assertIn("const VERSION = 3;", (tmp / "windows-agent.cjs.previous").read_text())

    def checks_repo(self):
        repo = make_repo({"a.txt": "a\n", "b.txt": "ok\n"})
        run = buckets.Run(repo)
        return repo, run, {"lint": "test -f a.txt", "test": "grep -q ok b.txt"}

    def test_split_checks_run_there_from_a_copy_kept_in_sync(self):
        repo, run, checks = self.checks_repo()
        agent = {**self.cfg["agent"], "checks": "split", "remote": ["test"]}
        with contextlib.redirect_stderr(io.StringIO()):
            got = buckets.run_checks(repo, checks, run, "t1", 60, agent=agent)
        self.assertEqual({n: r["ok"] for n, r in got.items()}, {"lint": True, "test": True})
        self.assertEqual(list(got), ["lint", "test"])
        log = next(run.dir.glob("*check-t1-test.log")).read_text()
        self.assertIn("(on windows, exit 0)", log)
        copy = self.tmp / "copies" / repo.name
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=copy, capture_output=True, text=True).stdout,
                         subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout)
        (repo / "b.txt").write_text("failed\n")
        subprocess.run(["git", "commit", "-qam", "break"], cwd=repo, check=True)
        with contextlib.redirect_stderr(io.StringIO()):
            got = buckets.run_checks(repo, checks, run, "t2", 60, agent=agent)  # only the new commit goes over
        self.assertEqual(got["test"]["ok"], False)
        self.assertIn("failed", (copy / "b.txt").read_text())

    def test_shadow_checks_keep_this_machines_result_and_log_differences(self):
        repo, run, checks = self.checks_repo()
        checks["where"] = f'[ "$(pwd -P)" = "{repo.resolve()}" ]'  # true only here
        agent = {**self.cfg["agent"], "checks": "shadow"}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = buckets.run_checks(repo, checks, run, "s", 60, agent=agent)
        self.assertTrue(all(r["ok"] for r in got.values()))
        self.assertIn("where: passes here but fails on the other machine", err.getvalue())
        rows = [json.loads(l) for l in (buckets.STATE_DIR / "checks-shadow.jsonl").read_text().splitlines()]
        mine = [r for r in rows if r["repo"] == repo.name]
        self.assertEqual({r["check"]: (r["mac_ok"], r["win_ok"]) for r in mine},
                         {"lint": (True, True), "test": (True, True), "where": (True, False)})

    def test_overflow_sends_checks_there_only_while_this_machine_is_busy(self):
        repo, run, checks = self.checks_repo()
        agent = {**self.cfg["agent"], "checks": "overflow", "remote": ["test"], "idle_cpu": 101}
        tree = buckets.clean_tree(repo)
        with mock.patch.object(buckets.os, "getloadavg", return_value=(0.5, 0, 0)):
            self.assertEqual(buckets.remote_checks(agent, checks, repo, tree), ("off", []))
        with mock.patch.object(buckets.os, "getloadavg", return_value=(99.0, 0, 0)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(buckets.remote_checks(agent, checks, repo, tree), ("split", ["test"]))
        self.assertIn("this machine is busy", err.getvalue())
        with mock.patch.object(buckets.os, "getloadavg", return_value=(99.0, 0, 0)):  # that one isn't idle either
            self.assertEqual(buckets.remote_checks({**agent, "idle_cpu": -1}, checks, repo, tree), ("off", []))

    def test_uncommitted_code_or_no_agent_keeps_the_checks_here(self):
        repo, run, checks = self.checks_repo()
        (repo / "new.txt").write_text("x")
        self.assertEqual(buckets.remote_checks({**self.cfg["agent"], "checks": "split"}, checks, repo,
                                               buckets.clean_tree(repo)), ("off", []))
        (repo / "new.txt").unlink()
        gone = {"url": "http://127.0.0.1:1", "token_file": self.cfg["agent"]["token_file"], "checks": "split",
                "remote": ["test"]}
        self.assertEqual(buckets.remote_checks(gone, checks, repo, buckets.clean_tree(repo)), ("off", []))
        with contextlib.redirect_stderr(io.StringIO()):
            got = buckets.run_checks(repo, checks, run, "local", 60, agent=gone)
        self.assertEqual({n: r["ok"] for n, r in got.items()}, {"lint": True, "test": True})

    def test_setup_makes_a_private_token_and_the_folder_to_carry(self):
        token = _SANDBOX / f"agent-{time.time_ns()}.token"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            buckets.cmd_win({"agent": {"url": "", "token_file": str(token)}}, ["setup"])
        folder = buckets.CONFIG_PATH.parent / "windows-setup"
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["agent.token", "setup-buckets-agent.ps1", "windows-agent.cjs"])
        self.assertEqual(oct(token.stat().st_mode & 0o777), "0o600")
        self.assertGreaterEqual(len(token.read_text().strip()), 32)
        self.assertIn('"agent": {"url"', err.getvalue())
        shared = Path(tempfile.mkdtemp()) / "share" / "buckets-setup"
        with contextlib.redirect_stderr(io.StringIO()):
            buckets.cmd_win({"agent": {"url": "x", "token_file": str(token)}}, ["setup", "--to", str(shared)])
        self.assertEqual((shared / "agent.token").read_text(), token.read_text())
        self.assertTrue((shared / "setup-buckets-agent.ps1").exists())


class PreferDedupe(unittest.TestCase):
    """Follow-ups that hand-edit copies an earlier dedupe found different become that dedupe again."""

    def setUp(self):
        self.repo = make_repo({"src/utils/time.ts": "export const fmt = (s: string) => s\n",
                               "src/a.tsx": "const fmt = (s: string) => s || '0'\n", "src/b.tsx": "const fmt = (s: string) => s || '0'\n",
                               "src/c.tsx": "export const other = 1\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        self.b = buckets.Buckets(cfg, self.repo, "move fmt into src/utils/time.ts")
        self.b.dedupes = [{"id": "T2", "action": "dedupe", "file": "src/utils/time.ts", "select": ["fmt"],
                           "from": ["src/a.tsx", "src/b.tsx"], "status": "failed"}]

    def edit(self, tid, f, text):
        return {"id": tid, "file": f, "action": "edit", "instructions": text, "done_when": "", "edits": [],
                "read_first": [], "where": ""}

    def test_hand_edits_of_the_copies_become_one_dedupe_after_the_shared_fix(self):
        tasks = [self.edit("R2T1", "src/utils/time.ts", "make fmt return '0' for an empty string"),
                 self.edit("R2T2", "src/a.tsx", "remove the local fmt and import it from utils/time"),
                 self.edit("R2T3", "src/b.tsx", "replace the local `fmt` copy with the import"),
                 self.edit("R2T4", "src/c.tsx", "rename other")]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = self.b.prefer_dedupe(tasks)
        self.assertEqual([(t["id"], t["action"], t["file"]) for t in got],
                         [("R2T1", "edit", "src/utils/time.ts"), ("R2T2", "dedupe", "src/utils/time.ts"),
                          ("R2T4", "edit", "src/c.tsx")])
        self.assertEqual((got[1]["select"], got[1]["from"]), (["fmt"], ["src/a.tsx", "src/b.tsx"]))
        self.assertIn("become a dedupe of fmt", err.getvalue())

    def test_edits_that_dont_touch_the_copied_names_stay(self):
        tasks = [self.edit("R2T1", "src/a.tsx", "fix the header text")]
        self.assertEqual(self.b.prefer_dedupe(tasks), tasks)
        self.b.dedupes = []
        tasks = [self.edit("R2T1", "src/a.tsx", "remove the local fmt")]
        self.assertEqual(self.b.prefer_dedupe(tasks), tasks)


class Reflexes(unittest.TestCase):
    """A file an edit leaves unparseable is caught within the task (roadmap item 30), before checks and a review
    round spend minutes on it: planned edits that would break it go to the coder, and a coder that breaks it
    is told where and fixes it in the same task."""

    def setUp(self):
        if not (TS_PKG.exists() and shutil.which("node")):
            self.skipTest("needs node and a typescript package")
        self.good = "export const Row = () => (\n  <HStack>\n    <Text>Hi</Text>\n  </HStack>\n);\n"
        self.broken = "export const Row = () => (\n  <HStack>{\n    <Text>Hi</Text>\n  </HStack>\n);\n"
        self.repo = make_repo({"src/row.tsx": self.good, "src/m.py": "def run(x):\n    return x\n", ".gitignore": "node_modules\n"})
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/typescript").symlink_to(TS_PKG)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False, "coder": {"engine": "native"},
                                                          "offload": {"escalate_repeated_failure": False}})
        self.b = buckets.Buckets(cfg, self.repo, "x")

    def test_syntax_errors_say_where(self):
        self.assertEqual(buckets.syntax_errors(self.repo, "src/row.tsx", self.good), [])
        errors = buckets.syntax_errors(self.repo, "src/row.tsx", self.broken)
        self.assertTrue(errors and errors[0].startswith("line "), errors)
        self.assertEqual(buckets.syntax_errors(self.repo, "src/m.py", "def f(:\n"), ["line 1: invalid syntax"])
        self.assertIsNone(buckets.syntax_errors(self.repo, "notes.md", "# {"))
        # only breakage the edit brought is blamed on it
        self.assertEqual(buckets.breaks_syntax(self.repo, "src/row.tsx", self.broken, self.broken + "\n"), [])
        self.assertTrue(buckets.breaks_syntax(self.repo, "src/row.tsx", self.good, self.broken))

    def test_planned_edits_that_break_a_tsx_file_go_to_the_coder(self):
        task = buckets.validate_tasks([{"file": "src/row.tsx", "action": "edit", "instructions": "wrap it",
                                        "done_when": "d", "where": "", "edits": [
                                            {"at": "  <HStack>", "mode": "replace", "code": "  <HStack>{"}]}],
                                      self.repo, 8, "T")[0]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(self.b.apply_planned(task, commit=True))
        self.assertIn("the result doesn't parse (line ", err.getvalue())
        self.assertEqual((self.repo / "src/row.tsx").read_text(), self.good)    # untouched

    def test_the_native_coder_cant_leave_a_tsx_file_broken(self):
        task = {"id": "T1", "file": "src/row.tsx", "action": "edit", "instructions": "i", "done_when": "d",
                "read_first": [], "where": "", "edits": []}
        out = self.b.coder_tool(task, {"id": "1", "name": "edit", "arguments": {
            "path": "src/row.tsx", "old": "  <HStack>\n", "new": "  <HStack>{\n"}})
        self.assertIn("not applied, the file wouldn't parse (line ", out)
        self.assertEqual((self.repo / "src/row.tsx").read_text(), self.good)

    def test_a_coder_that_breaks_the_file_fixes_it_in_the_same_task(self):
        task = {"id": "T1", "file": "src/row.tsx", "action": "edit", "instructions": "say hello",
                "done_when": "d", "read_first": [], "where": "", "edits": []}
        prompts = []
        def fake_coder(task, prompt, deadline, tick=None, head=""):
            prompts.append(prompt)
            fixed = len(prompts) > 1
            (self.repo / "src/row.tsx").write_text((self.good if fixed else self.broken).replace("Hi", "Hello"))
            return "DONE: said hello", False
        with mock.patch.object(buckets.Buckets, "native_coder", side_effect=fake_coder, autospec=False), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            got = self.b.code_task(task)
        self.assertEqual(got["status"], "done")
        self.assertEqual(len(prompts), 2)
        self.assertIn("the file no longer parses:\n- line ", prompts[1])
        self.assertIn("left the file unparseable", err.getvalue())
        self.assertIn("<Text>Hello</Text>", (self.repo / "src/row.tsx").read_text())
        self.assertEqual(buckets.syntax_errors(self.repo, "src/row.tsx", (self.repo / "src/row.tsx").read_text()), [])
        self.assertEqual(buckets.git(self.repo, "status", "--porcelain").strip(), "")    # committed once, fixed


class RepeatedLookups(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo({"src/m.py": "def run(x):\n    return x + 1\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        self.b = buckets.Buckets(cfg, self.repo, "make run add 2")

    def test_a_round_of_only_repeats_ends_the_look_ups(self):
        plan = {"summary": "s", "tasks": [{"file": "src/m.py", "action": "edit", "instructions": "x", "done_when": "y",
                                           "where": "run", "edits": []}]}
        turns = []
        def fake_turn(cfg, run, label, messages, **kw):
            turns.append((label, kw.get("tools") is not None))
            if len(turns) <= 2:  # the same read twice
                return "", "", [{"id": str(len(turns)), "name": "read", "arguments": {"what": "src/m.py:1-2"}}], False
            self.assertIn("you looked this up already", messages[-2]["content"])
            self.assertIn("Everything you asked for is above", messages[-1]["content"])
            return json.dumps(plan), "", [], False
        with mock.patch.object(buckets, "chat_turn", fake_turn), contextlib.redirect_stderr(io.StringIO()):
            got = self.b.plan_with_lookups("PLAN PROMPT", lambda d: buckets.validate_plan(d, self.repo, 8))
        self.assertEqual(got["tasks"][0]["file"], "src/m.py")
        self.assertEqual(turns, [("plan-1", True), ("plan-2", True), ("plan-3", False)])  # no more tools offered


class RenameAction(unittest.TestCase):
    """The "rename" plan action: the hint from the request, the mechanical rename, and hand edits swapped for it."""

    def setUp(self):
        if not (TS_PKG.exists() and shutil.which("node")):
            self.skipTest("needs node and a typescript package")
        self.repo = make_repo({
            "src/format.ts": "export const fmt = (s: string) => s.trim()\n",
            "src/a.ts": "import { fmt } from './format'\nexport const a = fmt(' a ')\n",
            "src/b.ts": "import { fmt } from './format'\nexport const b = { fmt }\n",
            "tsconfig.json": '{"compilerOptions": {"strict": true, "noEmit": true, "skipLibCheck": true}}'})
        (self.repo / "node_modules").mkdir()
        (self.repo / "node_modules/typescript").symlink_to(TS_PKG)
        (self.repo / ".git/info/exclude").write_text("node_modules\n")
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        self.b = buckets.Buckets(cfg, self.repo, "rename fmt to formatText everywhere")

    def test_the_request_gets_a_checked_dry_run_and_the_task_renames_everywhere(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            hint = self.b.rename_hint()
        self.assertTrue(hint["report"]["ok"], hint["report"])
        self.assertEqual({k: hint["task"][k] for k in ("file", "action", "where", "name")},
                         {"file": "src/format.ts", "action": "rename", "where": "fmt", "name": "formatText"})
        self.assertIn("a mechanical rename fits: fmt → formatText", err.getvalue())
        self.assertIn('"action": "rename"', self.b.facts())
        with contextlib.redirect_stderr(io.StringIO()):
            got = self.b.code_task(dict(hint["task"]))
        self.assertEqual(got["status"], "done", got)
        self.assertIn("in 3 file(s)", got["note"])
        self.assertIn("formatText(' a ')", (self.repo / "src/a.ts").read_text())
        self.assertIn("{ fmt: formatText }", (self.repo / "src/b.ts").read_text())  # the property keeps its name
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True,
                                        text=True).stdout.strip(), "")

    def test_a_clash_is_refused_with_the_reason(self):
        b = buckets.Buckets(self.b.cfg, self.repo, "rename fmt to a")
        with contextlib.redirect_stderr(io.StringIO()):
            hint = b.rename_hint()
            got = b.code_task(dict(hint["task"]))
        self.assertFalse(hint["report"]["ok"])
        self.assertEqual(got["status"], "failed")
        self.assertIn("already declared", got["note"])
        self.assertIn("refused", b.facts())

    def test_hand_edits_of_the_uses_become_the_rename(self):
        edit = lambda tid, f: {"id": tid, "file": f, "action": "edit", "instructions": "rename fmt to formatText here",
                               "done_when": "", "edits": [], "read_first": [], "where": ""}
        plan = {"summary": "s", "tasks": [edit("T1", "src/format.ts"), edit("T2", "src/a.ts"), edit("T3", "src/b.ts")]}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = self.b.prefer_move(plan)
        self.assertEqual([(t["id"], t["action"], t["file"]) for t in got["tasks"]], [("T1", "rename", "src/format.ts")])
        self.assertIn("renaming mechanically instead", err.getvalue())


class Measure(unittest.TestCase):
    def test_numbers_are_compared_line_by_line_between_two_versions(self):
        repo = make_repo({"count.txt": "3\n", ".gitignore": "node_modules/\n"})
        (repo / "node_modules").mkdir()
        (repo / "node_modules/size.txt").write_text("120\n")
        subprocess.run(["git", "tag", "before"], cwd=repo, check=True)
        (repo / "count.txt").write_text("4\n")
        subprocess.run(["git", "commit", "-qam", "more"], cwd=repo, check=True)
        cmd = 'echo "Tests: $(cat count.txt) passed, 0 failed"; echo "bundle $(cat node_modules/size.txt) kB"'
        out = io.StringIO()
        old = os.getcwd()
        os.chdir(repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = buckets.cmd_measure({}, ["--runs", "2", "--against", "before", "--branch", "HEAD", cmd])
        finally:
            os.chdir(old)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("| Tests: # passed, # failed [1] | 3 | 4 | +1 (+33%) |", text)
        self.assertIn("| Tests: # passed, # failed [2] | 0 | 0 | same |", text)
        self.assertIn("| bundle # kB | 120 | 120 | same |", text)  # node_modules was linked into both
        self.assertIn("2 run(s) each", text)
        self.assertEqual(subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True)
                         .stdout.count("\n"), 1)  # the worktrees are gone

    def test_number_lines_keeps_templates_and_skips_noise(self):
        got = buckets.number_lines("Tests: 2 failed, 1543 passed\n    at foo (a.ts:12:5)\nTime: 7.9 s\nno numbers\n")
        self.assertEqual(got, {"Tests: # failed, # passed": [2.0, 1543.0], "Time: # s": [7.9]})


class Each(unittest.TestCase):
    def test_targets_fill_the_request_and_fields(self):
        self.assertEqual(buckets.fill_target("merge {1} and {2} in {}", "a.tsx b.tsx"), "merge a.tsx and b.tsx in a.tsx b.tsx")
        self.assertEqual(buckets.fill_target("fix {3}", "a b"), "fix {3}")

    def test_each_target_is_its_own_run_and_the_table_says_how_each_went(self):
        repo = make_repo({"a.ts": "1\n"})
        (repo / "list.txt").write_text("b.ts\n# a comment\nc.ts\n")
        calls = []
        def fake_main(argv):
            calls.append((argv, os.environ.get("BUCKETS_FOLLOW_UPS")))
            text = argv[-1]
            run = buckets.STATE_DIR / "runs" / f"each-{time.time_ns()}"
            run.mkdir(parents=True)
            (run / "meta.json").write_text(json.dumps({"repo": str(repo.resolve()), "kind": "code", "request": text,
                                                       "done": "b.ts" not in text, "branch": f"buckets/{len(calls)}"}))
            return 0 if "b.ts" not in text else 2
        out, old = io.StringIO(), os.getcwd()
        os.chdir(repo)
        try:
            with mock.patch.object(buckets, "main", fake_main), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(io.StringIO()), mock.patch.dict(os.environ, {}):
                code = buckets.cmd_each(["remove the debug logs in {}", "a.ts", "--from", "list.txt", "--local"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 2)
        self.assertEqual([c[0] for c in calls], [["--local", "-y", "--wait", f"remove the debug logs in {f}"]
                                                 for f in ("a.ts", "b.ts", "c.ts")])
        self.assertTrue(all(c[1] == "never" for c in calls))  # runs don't build on each other
        text = out.getvalue()
        self.assertIn("| a.ts | done |", text)
        self.assertIn("| b.ts | not done |", text)
        self.assertIn("2 of 3 done", text)


class UpdateBranch(unittest.TestCase):
    """--update: the base merged into a new branch made from the branch, conflicts resolved, the branch untouched."""

    def setUp(self):
        self.repo = make_repo({"a.txt": "one\ntwo\nthree\n", "b.txt": "b\n"})
        g = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True, text=True).stdout
        self.g = g
        g("branch", "-M", "main")
        g("checkout", "-q", "-b", "feature")
        (self.repo / "a.txt").write_text("one\nTWO from feature\nthree\n")
        g("commit", "-qam", "feature change")
        g("checkout", "-q", "main")
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        self.b = buckets.Buckets(cfg, self.repo, "bring feature up to date with main")

    def run_update(self):
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return self.b.update_branch("feature", "main")

    def test_a_clean_merge_needs_no_model(self):
        (self.repo / "b.txt").write_text("b from main\n")
        self.g("commit", "-qam", "main change")
        feature = self.g("rev-parse", "feature")
        with mock.patch.object(buckets, "ask_json", side_effect=AssertionError("no model needed")):
            self.assertEqual(self.run_update(), 0)
        new = [b for b in self.g("branch", "--list", "buckets/update-*").split() if b != "*"][0]
        self.assertEqual(self.g("show", f"{new}:b.txt"), "b from main\n")
        self.assertIn("TWO from feature", self.g("show", f"{new}:a.txt"))
        self.assertEqual(self.g("rev-parse", "feature"), feature)  # the branch itself didn't move
        self.assertEqual(self.g("rev-parse", "--abbrev-ref", "HEAD").strip(), "main")

    def test_conflicts_are_planned_with_both_sides_changes(self):
        (self.repo / "a.txt").write_text("one\nTWO from main\nthree\n")
        self.g("commit", "-qam", "main change")
        prompts = []
        def fake_ask(cfg, run, label, system, prompt, schema, validate, **kw):
            prompts.append(prompt)
            return validate({"summary": "s", "tasks": [{"file": "a.txt", "action": "edit", "instructions": "combine",
                                                        "done_when": "no markers", "read_first": [], "edits": []}]})
        def fake_code(task, commit=True):
            (self.repo / "a.txt").write_text("one\nTWO from feature and main\nthree\n")
            return {**task, "status": "done", "note": "DONE"}
        def fake_rounds(plan, tasks, base, checks, baseline, **kw):
            return [], {"done": True, "summary": "both kept", "issues": [], "tasks": []}, kw["now"]
        with mock.patch.object(buckets, "ask_json", fake_ask), mock.patch.object(self.b, "code_task", fake_code), \
                mock.patch.object(self.b, "rounds", fake_rounds):
            self.assertEqual(self.run_update(), 0)
        self.assertIn("TWO from main", prompts[0])        # what main changed
        self.assertIn("TWO from feature", prompts[0])     # what the branch changed
        self.assertIn("<<<<<<<", prompts[0])               # the file with its markers
        new = [b for b in self.g("branch", "--list", "buckets/update-*").split() if b != "*"][0]
        self.assertEqual(self.g("show", f"{new}:a.txt"), "one\nTWO from feature and main\nthree\n")
        self.assertEqual(len(self.g("log", "-1", "--format=%P", new).split()), 2)  # a merge commit

    def test_an_up_to_date_branch_is_left_alone(self):
        self.g("merge", "-q", "feature")  # main has the feature, and the feature has everything main has
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.b.update_branch("feature", "main"), 0)
        self.assertIn("already has everything", err.getvalue())


class RedCIOnAPullRequest(unittest.TestCase):
    def test_the_run_starts_from_the_pull_requests_branch(self):
        repo = make_repo({"a.txt": "a\n"})
        subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True)
        subprocess.run(["git", "branch", "fix-thing"], cwd=repo, check=True)
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, repo, "the CI is red on PR 42, fix the failing checks")
        seen = []
        with mock.patch.object(buckets, "update_target", return_value=("fix-thing", "HEAD")) as target, \
                mock.patch.object(b, "_code", lambda plan_only: seen.append(
                    (subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, capture_output=True,
                                    text=True).stdout.strip(), b.start_branch)) or 0), \
                contextlib.redirect_stderr(io.StringIO()):
            b.code()
        self.assertEqual(target.call_args[0][1], "42")
        self.assertEqual(seen, [("fix-thing", "fix-thing")])  # read and planned from it, merging into it
        b2 = buckets.Buckets(cfg, repo, "add a test for PR 42's helper")  # a PR without CI words
        self.assertIsNone(b2.ci_pr_branch())


class CoverageFacts(unittest.TestCase):
    FAKE_JEST = """import json, os, sys
args = sys.argv[1:]
out = next(a.split("=", 1)[1] for a in args if a.startswith("--coverageDirectory="))
cover = [a.split("=", 1)[1] for a in args if a.startswith("--collectCoverageFrom=")]
assert "--findRelatedTests" in args and "--coverage" in args, args
data = {}
for rel in cover:
    path = os.path.abspath(rel)
    data[path] = {"path": path,
                  "statementMap": {"0": {"start": {"line": 2}}, "1": {"start": {"line": 5}}, "2": {"start": {"line": 6}}},
                  "s": {"0": 3, "1": 0, "2": 0},
                  "fnMap": {"0": {"name": "a", "loc": {"start": {"line": 1}, "end": {"line": 3}}},
                            "1": {"name": "b", "loc": {"start": {"line": 4}, "end": {"line": 7}}}},
                  "f": {"0": 2, "1": 0}}
os.makedirs(out, exist_ok=True)
open(os.path.join(out, "coverage-final.json"), "w").write(json.dumps(data))
"""

    def test_the_planner_gets_what_the_tests_dont_reach(self):
        repo = make_repo({"pkg/src/m.ts": "export function a() {\n  return 1\n}\nexport function b(x: number) {\n  if (x) return 2\n  return 3\n}\n",
                          "tools/jest.py": self.FAKE_JEST})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, repo, "add tests for pkg/src/m.ts to cover what isn't tested")
        b.rc = {"checks": {"test": f"cd pkg && {sys.executable} ../tools/jest.py --silent"}}
        with contextlib.redirect_stderr(io.StringIO()):
            got = b.coverage_hint()
        self.assertIn("- pkg/src/m.ts: 33% of statements covered", got)
        self.assertIn("functions no test calls: b (lines 4-7)", got)
        self.assertIn("lines not run: 5-6", got)
        self.assertEqual(buckets.Buckets(cfg, repo, "rename a to c in pkg/src/m.ts").coverage_hint(), "")


class Bisect(unittest.TestCase):
    def test_the_first_failing_commit_is_found_without_moving_the_checkout(self):
        repo = make_repo({"value.txt": "ok\n"})
        for n in range(1, 9):
            (repo / "value.txt").write_text("ok\n" if n < 6 else "broken\n")
            (repo / f"f{n}.txt").write_text(str(n))
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", f"commit {n}"], cwd=repo, check=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout
        out, old = io.StringIO(), os.getcwd()
        os.chdir(repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = buckets.cmd_bisect({}, ["grep -qx ok value.txt"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 0)
        self.assertIn("First bad commit", out.getvalue())
        self.assertIn("commit 6", out.getvalue())
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout, head)
        self.assertEqual(subprocess.run(["git", "worktree", "list"], cwd=repo, capture_output=True, text=True).stdout.count("\n"), 1)


class Decisions(unittest.TestCase):
    def test_judgment_calls_are_picked_from_the_run_log(self):
        run = Path(tempfile.mkdtemp())
        (run / "run.log").write_text("[00:01] Step 3/5 · Reading the code and planning\n"
                                     "[07:08]   as unify: 2 edit task(s) that make near copies match, copied exactly instead\n"
                                     "[07:08]   dropped T4, T5: the move already does that\n"
                                     "[07:12]   \u2713 T1 unify (4s): made onAddressPress identical\n"
                                     "[13:41]   \u26a0 round 2 broke typecheck, which passed before it; undid that round and stopped here\n")
        self.assertEqual(buckets.decisions(run), [
            "as unify: 2 edit task(s) that make near copies match, copied exactly instead",
            "dropped T4, T5: the move already does that",
            "round 2 broke typecheck, which passed before it; undid that round and stopped here"])


if __name__ == "__main__":
    unittest.main()
