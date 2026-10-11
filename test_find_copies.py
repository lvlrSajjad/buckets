"""Tests for ts_moves.cjs's find_copies op (functions that are the shared ones under other names) and for
dedupe's "aliases" (a copy under another name replaced by the shared function, its uses renamed)."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from test_rename import SCRIPT, TS_PKG, make_repo, tsc_errors

SHARED = (
    "export function formatMoney(amount: number, cur: string): string {\n"
    "  return cur + amount.toFixed(2)\n"
    "}\n\n"
    "export const pad2 = (n: number): string => (n < 10 ? \"0\" + n : String(n))\n\n"
    "export const clamp = (v: number, lo: number, hi: number): number => Math.min(hi, Math.max(lo, v))\n"
)


def op(repo: Path, name: str, **payload) -> dict:
    r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"op": name, "repo": str(repo), **payload}),
                       cwd=repo, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


def find(repo: Path, **payload) -> dict:
    return op(repo, "find_copies", file="src/util.ts", **payload)


def dedupe(repo: Path, copies, **payload) -> dict:
    return op(repo, "dedupe", file="src/util.ts", copies=copies, **payload)


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class FindCopiesTests(unittest.TestCase):
    def found(self, d, file):
        return [c for c in d["report"]["copies"] if c["file"] == file]

    def test_renamed_identical_copy_with_other_parameter_names(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": "function fmtMoney(amount: number, cur: string): string {\n  return cur + amount.toFixed(2)\n}\n\nexport const a = fmtMoney(1, \"$\")\n",
            "src/b.ts": "export const money = (value: number, currency: string): string => currency + value.toFixed(2)\n",
            "src/c.ts": "export const unrelated = (x: number, y: number): number => x * y\n",
        })
        d = find(repo)
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["files"], {})
        self.assertEqual(d["report"]["shared"], ["formatMoney", "pad2", "clamp"])
        a = self.found(d, "src/a.ts")
        self.assertEqual([(c["name"], c["shared"], c["how"], c["line"], c["exported"]) for c in a],
                         [("fmtMoney", "formatMoney", "identical", 1, False)])
        # an arrow function is another form of the same thing only by behaviour, which is what finds it
        self.assertEqual([(c["name"], c["shared"]) for c in self.found(d, "src/b.ts")], [("money", "formatMoney")])
        self.assertEqual(self.found(d, "src/c.ts"), [])

    def test_identical_arrow_with_other_parameter_names(self):
        repo = make_repo({
            "src/util.ts": "export const formatMoney = (amount: number, cur: string): string => cur + amount.toFixed(2)\n",
            "src/b.ts": "export const money = (value: number, currency: string): string => currency + value.toFixed(2)\n",
        })
        d = find(repo)
        self.assertEqual([(c["name"], c["how"], c["exported"]) for c in d["report"]["copies"]], [("money", "identical", True)])

    def test_same_behaviour_written_differently(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": "function twoDigits(value: number): string {\n  if (value < 10) {\n    return \"0\" + value\n  }\n  return String(value)\n}\n",
        })
        d = find(repo, names=["pad2"])
        self.assertTrue(d["ok"], d)
        got = self.found(d, "src/a.ts")
        self.assertEqual([(c["name"], c["shared"], c["how"]) for c in got], [("twoDigits", "pad2", "same behaviour")])

    def test_different_body_is_near_not_a_copy(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": "export function formatMoney(amount: number, cur: string): string {\n  return amount.toFixed(1) + cur\n}\n",
            "src/b.ts": "export const grow = (v: number, lo: number, hi: number): number => Math.min(hi, lo) + v\n",
        })
        d = find(repo, names=["formatMoney", "clamp"])
        self.assertEqual(d["report"]["copies"], [])
        near = {(n["file"], n["name"], n["shared"]) for n in d["report"]["near"]}
        self.assertIn(("src/a.ts", "formatMoney", "formatMoney"), near)      # same name
        self.assertIn(("src/b.ts", "grow", "clamp"), near)                   # same parameters
        item = next(n for n in d["report"]["near"] if n["name"] == "formatMoney")
        self.assertIn("runs differently", item["why"])

    def test_same_text_using_another_declaration_is_not_a_copy(self):
        repo = make_repo({
            "src/util.ts": "const RATE = 2\n\nexport const double = (v: number): number => v * RATE\n",
            "src/a.ts": "const RATE = 3\n\nexport const twice = (v: number): number => v * RATE\n",
            "src/b.ts": "const RATE = 2\n\nexport const twice = (v: number): number => v * RATE\n",
        })
        d = find(repo)
        self.assertEqual([c["file"] for c in d["report"]["copies"]], ["src/b.ts"])
        self.assertEqual([n["file"] for n in d["report"]["near"]], ["src/a.ts"])
        self.assertIn("RATE is not the same thing", d["report"]["near"][0]["why"])

    def test_component_local_copies_are_reported_as_inner(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/view.tsx": (
                "export function View(props: { rate: number }) {\n"
                "  const fmt = (a: number, c: string): string => c + a.toFixed(2)\n"
                "  const scaled = (a: number, c: string): string => c + (a * props.rate).toFixed(2)\n"
                "  return <span>{fmt(1, \"$\")}{scaled(1, \"$\")}</span>\n"
                "}\n"),
        })
        d = find(repo)
        self.assertEqual(d["report"]["copies"], [])
        self.assertEqual([(c["name"], c["parent"], c["shared"]) for c in d["report"]["inner"]], [("fmt", "View", "formatMoney")])

    def test_files_option_limits_the_scan(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": "export const m1 = (v: number, c: string): string => c + v.toFixed(2)\n",
            "src/b.ts": "export const m2 = (v: number, c: string): string => c + v.toFixed(2)\n",
        })
        d = find(repo, files=["src/b.ts"])
        self.assertEqual([c["file"] for c in d["report"]["copies"]], ["src/b.ts"])
        self.assertEqual(d["report"]["scanned"], 1)

    def test_unknown_name_is_an_error(self):
        repo = make_repo({"src/util.ts": SHARED})
        d = find(repo, names=["nope"])
        self.assertFalse(d["ok"])
        self.assertIn("nope", d["error"])


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class DedupeAliasTests(unittest.TestCase):
    def apply(self, repo, d):
        for f, t in d["files"].items():
            (repo / f).write_text(t)

    def test_alias_replaces_the_copy_and_renames_every_use(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/b.tsx": (
                "function fmtMoney(amount: number, cur: string): string {\n  return cur + amount.toFixed(2)\n}\n\n"
                "const Row = (p: { format: (n: number, c: string) => string }) => <span>{p.format(1, \"$\")}</span>\n\n"
                "export const view = <div title={fmtMoney(1, \"$\")}><Row format={fmtMoney} /></div>\n"
                "export const all = [1, 2].map((n) => fmtMoney(n, \"E\"))\n"
                "export const api = { fmtMoney }\n"),
        })
        self.assertEqual(tsc_errors(repo), "")
        d = dedupe(repo, ["src/b.tsx"], names=[], aliases={"src/b.tsx": {"fmtMoney": "formatMoney"}})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["report"]["replaced"], ["src/b.tsx: fmtMoney"])
        self.assertEqual(d["report"]["refused"], [])
        text = d["files"]["src/b.tsx"]
        self.assertTrue(text.startswith('import { formatMoney } from "./util"\n'), text)
        self.assertNotIn("function fmtMoney", text)
        self.assertIn('title={formatMoney(1, "$")}', text)
        self.assertIn("<Row format={formatMoney} />", text)
        self.assertIn('.map((n) => formatMoney(n, "E"))', text)
        self.assertIn("{ fmtMoney: formatMoney }", text)       # the property keeps its name
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_alias_of_a_copy_written_differently_goes_through_the_behaviour_gate(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": ("function twoDigits(value: number): string {\n  if (value < 10) {\n    return \"0\" + value\n  }\n  return String(value)\n}\n\n"
                         "export const days = [1, 22].map(twoDigits)\n"),
        })
        d = dedupe(repo, ["src/a.ts"], names=[], aliases={"src/a.ts": {"twoDigits": "pad2"}})
        self.assertTrue(d["ok"], d)
        self.assertEqual(len(d["report"]["equivalent"]), 1)
        self.assertIn(".map(pad2)", d["files"]["src/a.ts"])
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_alias_of_a_differing_copy_is_reported_as_differing(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": "function fmtMoney(amount: number, cur: string): string {\n  return amount.toFixed(1) + cur\n}\n\nexport const a = fmtMoney(1, \"$\")\n",
        })
        d = dedupe(repo, ["src/a.ts"], names=[], aliases={"src/a.ts": {"fmtMoney": "formatMoney"}})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["files"], {})
        self.assertEqual([x["name"] for x in d["report"]["differ"]], ["fmtMoney"])

    def test_a_name_clash_refuses_that_copy_only(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/clash.ts": ("const formatMoney = \"mine\"\n\nfunction fmtMoney(amount: number, cur: string): string {\n  return cur + amount.toFixed(2)\n}\n\n"
                             "export const a = fmtMoney(1, formatMoney)\n"),
            "src/shadow.ts": ("function fmtMoney(amount: number, cur: string): string {\n  return cur + amount.toFixed(2)\n}\n\n"
                              "export function show(formatMoney: string) {\n  return fmtMoney(1, formatMoney)\n}\n"),
            "src/ok.ts": "function fmtMoney(amount: number, cur: string): string {\n  return cur + amount.toFixed(2)\n}\n\nexport const a = fmtMoney(1, \"$\")\n",
        })
        d = dedupe(repo, ["src/clash.ts", "src/shadow.ts", "src/ok.ts"], names=[],
                   aliases={f"src/{n}.ts": {"fmtMoney": "formatMoney"} for n in ("clash", "shadow", "ok")})
        self.assertTrue(d["ok"], d)
        self.assertEqual(sorted(r["file"] for r in d["report"]["refused"]), ["src/clash.ts", "src/shadow.ts"])
        self.assertIn("already declared", d["report"]["refused"][0]["reason"] + d["report"]["refused"][1]["reason"])
        self.assertEqual(set(d["files"]), {"src/ok.ts"})
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_an_existing_import_of_the_shared_function_is_not_duplicated(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": ('import { formatMoney, pad2 } from "./util"\n\n'
                         "function fmtMoney(amount: number, cur: string): string {\n  return cur + amount.toFixed(2)\n}\n\n"
                         "export const a = [fmtMoney(1, \"$\"), formatMoney(2, \"$\"), pad2(3)]\n"),
        })
        d = dedupe(repo, ["src/a.ts"], names=[], aliases={"src/a.ts": {"fmtMoney": "formatMoney"}})
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["files"]["src/a.ts"].count("import "), 1)
        self.assertIn('[formatMoney(1, "$"), formatMoney(2, "$"), pad2(3)]', d["files"]["src/a.ts"])
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_same_name_dedupe_without_aliases_still_works(self):
        repo = make_repo({
            "src/util.ts": SHARED,
            "src/a.ts": "function formatMoney(amount: number, cur: string): string {\n  return cur + amount.toFixed(2)\n}\n\nexport const a = formatMoney(1, \"$\")\n",
            "src/b.ts": "function formatMoney(amount: number, cur: string): string {\n  return amount.toFixed(1) + cur\n}\n\nexport const b = formatMoney(1, \"$\")\n",
        })
        d = dedupe(repo, ["src/a.ts", "src/b.ts"], names=["formatMoney"])
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["report"]["replaced"], ["src/a.ts: formatMoney"])
        self.assertEqual([x["file"] for x in d["report"]["differ"]], ["src/b.ts"])
        self.assertEqual(d["report"]["missing"], [])
        self.assertEqual(d["files"]["src/a.ts"], 'import { formatMoney } from "./util"\n\nexport const a = formatMoney(1, "$")\n')


if __name__ == "__main__":
    unittest.main()
