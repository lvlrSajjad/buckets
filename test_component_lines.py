"""Tests for ts_moves.cjs's component op given a JSX chunk by lines instead of a render helper."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from test_rename import SCRIPT, TS_PKG, make_repo, tsc_errors

REACT = 'declare module "react" {\n  export function useState<T>(v: T): [T, (v: T) => void]\n}\n'
SCREEN = '''import { useState } from "react"

type Item = { id: number; name: string }

export function ListScreen(p: { items: Item[]; title: string }) {
  const [open, setOpen] = useState(false)
  const heading = p.title.toUpperCase()
  return (
    <div>
      <header onClick={() => setOpen(!open)}>
        <b>{heading}</b>
      </header>
      {p.items.map((item) => (
        <section key={item.id}>
          <i>{item.name}</i>
          <span>{open ? "open" : "closed"}</span>
        </section>
      ))}
      <p>one</p>
      <p>two</p>
    </div>
  )
}
'''


def component(repo: Path, **payload) -> dict:
    r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"op": "component", "repo": str(repo), **payload}),
                       cwd=repo, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


def apply(repo: Path, d: dict):
    for f, t in d["files"].items():
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(t)


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class ComponentByLines(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo({"src/list.tsx": SCREEN,   # React's own JSX types let any element take a key
                               "src/attrs.d.ts": "declare namespace JSX { interface IntrinsicAttributes { key?: string | number } }\n"})
        types = self.repo / "node_modules/@types/react"   # where a real project's React types are
        types.mkdir(parents=True)
        (types / "index.d.ts").write_text(REACT.split("\n", 1)[1].rsplit("}", 1)[0])
        self.assertEqual(tsc_errors(self.repo), "")

    def test_an_element_becomes_a_component_with_props(self):
        d = component(self.repo, file="src/list.tsx", function="ListScreen", lines=[10, 12], into="src/heading.tsx", name="Heading")
        self.assertTrue(d["ok"], d)
        screen = d["files"]["src/list.tsx"]
        self.assertIn("<Heading", screen)
        self.assertIn("heading={heading}", screen)
        self.assertIn("setOpen={setOpen}", screen)
        self.assertNotIn("renderHeading", screen)                             # the helper was only a step
        self.assertIn("<b>{heading}</b>", d["files"]["src/heading.tsx"])
        apply(self.repo, d)
        self.assertEqual(tsc_errors(self.repo), "")

    def test_a_chunk_inside_a_map_takes_the_item(self):
        d = component(self.repo, file="src/list.tsx", function="ListScreen", lines=[14, 17], into="src/row.tsx", name="Row")
        self.assertTrue(d["ok"], d)
        screen = d["files"]["src/list.tsx"]
        self.assertIn("<Row key={item.id} item={item} open={open} />", screen)   # the key belongs on <Row>
        row = d["files"]["src/row.tsx"]
        self.assertIn("{ item: Item; open: boolean }", row)
        self.assertIn("  return (\n    <section>\n      <i>{item.name}</i>\n", row)          # one pair of parens, re-indented
        self.assertNotIn("key=", row)
        apply(self.repo, d)
        self.assertEqual(tsc_errors(self.repo), "")

    def test_a_run_of_siblings_goes_in_a_fragment(self):
        d = component(self.repo, file="src/list.tsx", function="ListScreen", lines=[19, 20], into="src/pair.tsx", name="Pair")
        self.assertTrue(d["ok"], d)
        self.assertIn("<Pair />", d["files"]["src/list.tsx"])
        self.assertIn("<>", d["files"]["src/pair.tsx"])
        apply(self.repo, d)
        self.assertEqual(tsc_errors(self.repo), "")

    def test_the_same_block_written_again_becomes_the_same_element(self):
        row = '      <section>\n        <b>{p.title}</b>\n      </section>\n'
        repo = make_repo({"src/s.tsx": 'export function Skel(p: { title: string; items: string[] }) {\n  return (\n    <div>\n'
                                       + row + row.replace("      ", "        ") + '      {p.items.map((title) => (\n        <section>\n'
                                       '          <b>{title}</b>\n        </section>\n      ))}\n' + row + '    </div>\n  )\n}\n'})
        d = component(repo, file="src/s.tsx", function="Skel", lines=[4, 6], into="src/row.tsx", name="Row")
        self.assertTrue(d["ok"], d)
        screen = d["files"]["src/s.tsx"]
        self.assertEqual(screen.count("<Row p={p} />"), 3, screen)              # the copy indented differently too
        self.assertIn("<b>{title}</b>", screen)                                 # a different `title`: not a copy
        self.assertEqual(len(d["report"]["copies"]), 2)
        apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_a_name_the_file_already_has_is_refused(self):
        d = component(self.repo, file="src/list.tsx", function="ListScreen", lines=[10, 12], into="src/item.tsx", name="Item")
        self.assertFalse(d["ok"])
        self.assertIn("`Item` is already a name", d["error"])

    def test_props_are_passed_not_re_derived_from_the_component_props(self):
        repo = make_repo({"src/p.tsx": 'declare function useTheme(): { c: string }\n'
                                       'export function Card(p: { user: { name: string } }) {\n  const theme = useTheme()\n'
                                       '  const user = useTheme() && p.user\n  return (\n    <div>\n      <b>{user.name}</b>\n'
                                       '      <i>{theme.c}</i>\n    </div>\n  )\n}\n'})
        d = component(repo, file="src/p.tsx", function="Card", lines=[6, 9], into="src/body.tsx", name="Body")
        self.assertTrue(d["ok"], d)
        self.assertIn("user={user}", d["files"]["src/p.tsx"])                   # passed: it comes from the props
        self.assertNotIn("p.user", d["files"]["src/body.tsx"])
        self.assertIn("const theme = useTheme()", d["files"]["src/body.tsx"])   # a context value is re-derived

    def test_lines_that_cut_an_element_are_refused_with_the_elements_near(self):
        d = component(self.repo, file="src/list.tsx", function="ListScreen", lines=[10, 11], into="src/x.tsx", name="X")
        self.assertFalse(d["ok"])
        self.assertIn("spans exactly lines 10-11", d["error"])
        self.assertIn("10-12", d["error"])


if __name__ == "__main__":
    unittest.main()
