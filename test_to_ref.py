"""Tests for ts_moves.cjs's to_ref op: module-level `let`s one component uses become `useRef`s inside it."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from test_rename import SCRIPT, TS_PKG, make_repo, tsc_errors

REACT = ('declare module "react" {\n  export function useRef<T>(v: T): { current: T }\n'
         '  export function useState<T>(v: T): [T, (v: T) => void]\n  export function useEffect(f: () => void, d?: any[]): void\n}\n')

SCREEN = '''import { useState, useEffect } from "react"

// the id of the item on screen
let id: number | null = -1
let visits = 0
let lastName
let shared = 0
export let exported = 0
let atModule = 1
console.log(atModule)

export function Screen(p: { itemId: number }) {
  const [n, setN] = useState(0)
  useEffect(() => {
    id = p.itemId
    visits++
    lastName = "x"
  }, [])
  const payload = { id, visits }
  shared = n + exported
  return <b onClick={() => setN(visits)}>{id}{payload.visits}{lastName}</b>
}

function helper() {
  return shared
}
'''


def to_ref(repo: Path, **payload) -> dict:
    r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"op": "to_ref", "repo": str(repo), **payload}),
                       cwd=repo, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class ToRefTests(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo({"src/react.d.ts": REACT, "src/screen.tsx": SCREEN})

    def test_state_one_component_uses_becomes_refs_and_the_rest_stays_with_reasons(self):
        before = tsc_errors(self.repo)
        d = to_ref(self.repo, file="src/screen.tsx")
        self.assertTrue(d["ok"], d)
        out = d["files"]["src/screen.tsx"]
        self.assertEqual([c["name"] for c in d["converted"]], ["id", "visits", "lastName"])
        self.assertIn('import { useState, useEffect, useRef } from "react"', out)
        self.assertIn("  // the id of the item on screen\n  const idRef = useRef<number | null>(-1)\n", out)   # the comment comes along
        self.assertIn("const visitsRef = useRef<number>(0)", out)
        self.assertIn("const lastNameRef = useRef<any>(undefined)", out)
        self.assertIn("idRef.current = p.itemId", out)
        self.assertIn("visitsRef.current++", out)
        self.assertIn("const payload = { id: idRef.current, visits: visitsRef.current }", out)   # shorthand kept its key
        self.assertNotIn("let id", out)
        self.assertNotIn("let visits", out)
        why = {s["name"]: s["why"] for s in d["skipped"]}
        self.assertIn("used by 2 top-level declarations", why["shared"])
        self.assertIn("exported", why["exported"])
        self.assertIn("module level", why["atModule"])
        self.assertTrue(next(c for c in d["converted"] if c["name"] == "id")["assigned"])
        (self.repo / "src/screen.tsx").write_text(out)
        self.assertLessEqual(tsc_errors(self.repo).count("error TS"), before.count("error TS"))   # fewer: `lastName` was an implicit any

    def test_only_the_named_ones(self):
        d = to_ref(self.repo, file="src/screen.tsx", names=["visits", "nope"])
        self.assertTrue(d["ok"], d)
        self.assertEqual([c["name"] for c in d["converted"]], ["visits"])
        self.assertIn("let id", d["files"]["src/screen.tsx"])
        self.assertIn({"name": "nope", "why": "no module-level let/var by that name"}, d["skipped"])

    def test_a_name_ending_in_ref_keeps_it_and_a_plain_function_is_refused(self):
        repo = make_repo({"src/react.d.ts": REACT, "src/a.tsx": (
            'import { useRef as keep } from "react"\n\nlet inputRef: { focus(): void } | null = null\nlet count = 0\n\n'
            'export const Form = () => {\n  const k = keep(1)\n  return <input ref={(r: any) => (inputRef = r)} onFocus={() => inputRef?.focus()} />\n}\n\n'
            'export function tally() {\n  return ++count\n}\n')})
        d = to_ref(repo, file="src/a.tsx")
        self.assertTrue(d["ok"], d)
        out = d["files"]["src/a.tsx"]
        self.assertIn("const inputRef = useRef<{ focus(): void } | null>(null)", out)
        self.assertIn("(inputRef.current = r)", out)
        self.assertIn("inputRef.current?.focus()", out)
        self.assertIn("import { useRef as keep, useRef } from \"react\"", out)
        self.assertIn("isn't a component", next(s["why"] for s in d["skipped"] if s["name"] == "count"))
        (repo / "src/a.tsx").write_text(out)
        self.assertEqual(tsc_errors(repo), "")

    def test_review_cases(self):
        repo = make_repo({"src/react.d.ts": REACT, "src/d.tsx": (
            "// @ts-nocheck-free header\nlet n = 0\nlet e = 1\nlet b = 0\nexport { e }\n\n"
            "export const Expr = () => <div onClick={() => { n++ }} />\n\n"
            "export function One() { e++; b++; return <i>{e}</i> }\n\n"
            "export function Two(o: { b?: number }) {\n  ({ b = 5 } = o)\n  return <i>{b}</i>\n}\n")})
        d = to_ref(repo, file="src/d.tsx")
        self.assertTrue(d["ok"], d)
        why = {x["name"]: x["why"] for x in d["skipped"]}
        self.assertIn("returns an expression", why["n"])                      # never a hook inside the onClick
        self.assertIn("exported", why["e"])                                    # `export { e }`
        self.assertIn("used by 2 top-level", why["b"])
        out = d["files"].get("src/d.tsx", "")
        self.assertEqual(out, "")                                              # nothing converted, nothing written
        repo = make_repo({"src/react.d.ts": REACT, "src/f.tsx": (
            "/* eslint-disable */\nlet b = 0\n\nexport function Two(o: { b?: number }) { ({ b = 5 } = o); return <i>{b}</i> }\n")})
        d = to_ref(repo, file="src/f.tsx")
        self.assertTrue(d["ok"], d)
        out = d["files"]["src/f.tsx"]
        self.assertTrue(out.startswith("/* eslint-disable */\n"), out)       # the header stays at the top
        self.assertIn("({ b: bRef.current = 5 } = o)", out)                   # the default kept
        self.assertIn("{\n  const bRef = useRef<number>(0);\n  ({ b", out)   # a one-line body still parses; `(` isn't a call
        (repo / "src/f.tsx").write_text(out)
        self.assertEqual(tsc_errors(repo), "")

    def test_a_comment_a_blank_line_apart_stays_where_it_is(self):
        repo = make_repo({"src/react.d.ts": REACT, "src/g.tsx": (
            'import { useState } from "react"\n\n/**\n * The screen and everything it does.\n */\n\nlet n = 0\n\n'
            'export function G() {\n  const [s] = useState(1)\n  n += s\n  return <i>{n}</i>\n}\n')})
        d = to_ref(repo, file="src/g.tsx")
        out = d["files"]["src/g.tsx"]
        self.assertIn(" * The screen and everything it does.\n */\n", out)
        self.assertIn("export function G() {\n  const nRef = useRef<number>(0)\n", out)   # no comment came along

    def test_a_clash_is_skipped(self):
        repo = make_repo({"src/react.d.ts": REACT, "src/b.tsx": (
            'let n = 0\n\nexport function Counter() {\n  const nRef = 1\n  n += nRef\n  return <i>{n}</i>\n}\n')})
        d = to_ref(repo, file="src/b.tsx")
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["converted"], [])
        self.assertIn("already a name", d["skipped"][0]["why"])

    def test_wrapped_components_and_hooks_count(self):
        repo = make_repo({"src/react.d.ts": REACT, "src/c.tsx": (
            'declare function observer<T>(f: T): T\nlet seen = false\nlet calls = 0\n\n'
            'export const Wrapped = observer(() => {\n  seen = true\n  return <i>{String(seen)}</i>\n})\n\n'
            'export function useCalls() {\n  calls += 1\n  return calls\n}\n')})
        d = to_ref(repo, file="src/c.tsx")
        self.assertTrue(d["ok"], d)
        self.assertEqual({c["name"]: c["component"] for c in d["converted"]}, {"seen": "Wrapped"})
        self.assertIn("isn't a component", d["skipped"][0]["why"])            # a hook's counter may be meant to be shared
        d = to_ref(repo, file="src/c.tsx", hooks=["useCalls"])               # unless the caller knows it's one screen's
        self.assertEqual({c["name"]: c["component"] for c in d["converted"]}, {"seen": "Wrapped", "calls": "useCalls"})
        (repo / "src/c.tsx").write_text(d["files"]["src/c.tsx"])
        self.assertEqual(tsc_errors(repo), "")


if __name__ == "__main__":
    unittest.main()
