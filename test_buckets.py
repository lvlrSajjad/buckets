"""Tests for the parts of buckets that don't need a model: run with `python3 -m unittest -v`."""
import ast
import contextlib
import io
import json
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
        b = buckets.Buckets(self.b.cfg, self.repo, "make run double x in src/m.py and add a test in test_m.py")
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
        varied = " ".join(f"Step {i} looks at a different part of the code, number {i * 7}." for i in range(60))
        self.assertFalse(buckets.thinking_loops(varied))
        circle = varied[:1500] + " Wait, I need to double check whether fill_in drops the paths here. " * 4
        self.assertTrue(buckets.thinking_loops(circle))

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
            "app/screens/work-order-details/screen.tsx": 'export const S = () => <Row title={"Original ETA"} />\n',
            "app/models/work-order-details/model.ts": "export const M = { createdAt: 1, originalEta: 2 }\n",
            "app/screens/invoice/screen.tsx": "export const I = 1\n"})
        self.assertEqual(buckets.word_runs("Does work order details have created at? Hide `x` and a/b.c"),
                         [["work", "order", "details"], ["created", "at"]])
        self.assertEqual(buckets.spellings(["created", "at"]), ["created-at", "created_at", "createdAt", "CreatedAt"])
        leads = buckets.request_leads(repo, "Hide the Original ETA from the work order details",
                                      buckets.list_files(repo, []))
        self.assertIn('Paths spelling "work order details" (2 files)', leads)
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
        doc += "## Work orders\nThe work order details screen lives in screens/work-order-details.\n"
        repo = make_repo({"AGENTS.md": doc, "CLAUDE.md": "@AGENTS.md\n",
                          ".github/skills/work-order-details/SKILL.md": "# Skill\nHow details rows work.\n",
                          ".github/skills/linting/SKILL.md": "# Lint\n"})
        g = buckets.guidance(repo, {}, "Hide a field from the work order details")
        self.assertIn("--- AGENTS.md (the parts that match this request) ---", g)
        self.assertIn("The work order details screen lives in", g)              # the matching section
        self.assertIn("Other sections, not shown: Topic 0", g)
        self.assertIn("--- .github/skills/work-order-details/SKILL.md", g)    # the skill its words spell
        self.assertNotIn("skills/linting", g)
        self.assertLess(len(g), 9000)

    def test_related_repos_are_searched_and_read_never_changed(self):
        api = make_repo({"src/filter/workorder.list.config.ts": "export const C = [{ filterKey: 'priorityLevel' }]\n"})
        repo = make_repo({"app/screens/work-order-filters/screen.tsx": "export const S = 1\n"})
        cfg = buckets.deep_merge(buckets.DEFAULT_CONFIG, {"mode": "local", "memory": False})
        b = buckets.Buckets(cfg, repo, "add the priority level to the work order filters")
        b.rc["related"] = {"api": str(api)}
        facts = b.facts()
        self.assertIn("in `api` (a related repo", facts)
        self.assertIn("api:src/filter/workorder.list.config.ts:1:", facts)
        self.assertEqual(buckets.claude_command({"bin": "claude", "budget_usd": 1}, "sonnet", None, True, [api])[-2:],
                         ["--add-dir", str(api)])

    def test_a_type_and_a_value_with_one_name_arent_an_api_change(self):
        b = buckets.Buckets.__new__(buckets.Buckets)
        surface = {"h.tsx": [{"kind": "interface", "name": "H", "signature": "interface H"},
                             {"kind": "const", "name": "H", "signature": "const H = () =>"}]}
        b.surface_before, b.repo = surface, make_repo({"a.txt": "x"})
        b._index = mock.Mock(surface=lambda: {"h.tsx": list(reversed(surface["h.tsx"]))}, uses=lambda *a: {"uses": []})
        self.assertEqual(b.api_impact("HEAD"), {"removed": [], "changed": [], "added": [], "untouched": []})


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
            self.assertEqual(fake.labels, ["intent", "files", "tasks", "done", "files"])
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


if __name__ == "__main__":
    unittest.main()
