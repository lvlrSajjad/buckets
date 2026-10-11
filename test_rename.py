"""Tests for ts_moves.cjs's rename op: TypeScript's own rename across a project, with refusals."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("ts_moves.cjs")


def _find_typescript() -> Path:
    import os
    if os.environ.get("BUCKETS_TEST_TYPESCRIPT"):
        return Path(os.environ["BUCKETS_TEST_TYPESCRIPT"])
    if shutil.which("npm"):
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        if root and (Path(root) / "typescript").exists():
            return Path(root) / "typescript"
    return Path.home() / "Coding/ME/castellan/node_modules/typescript"


TS_PKG = _find_typescript()
TSCONFIG = json.dumps({"compilerOptions": {"jsx": "preserve", "strict": True, "noEmit": True, "skipLibCheck": True,
                                           "lib": ["es2020"], "module": "esnext", "moduleResolution": "bundler"},
                       "include": ["src"]})
JSX_DTS = "declare namespace JSX { interface Element {} interface IntrinsicElements { [k: string]: any } }\n"


def make_repo(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="buckets-rename-"))
    for name, text in {"tsconfig.json": TSCONFIG, "src/jsx.d.ts": JSX_DTS, **files}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    (root / "node_modules").mkdir(exist_ok=True)
    (root / "node_modules/typescript").symlink_to(TS_PKG)
    return root


def rename(repo: Path, **payload) -> dict:
    r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"op": "rename", "repo": str(repo), **payload}),
                       cwd=repo, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


def tsc_errors(repo: Path) -> str:
    r = subprocess.run(["node", str(TS_PKG / "bin" / "tsc"), "-p", "."], cwd=repo, capture_output=True, text=True)
    return r.stdout + r.stderr


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class RenameTests(unittest.TestCase):
    def apply(self, repo, d):
        for f, t in d["files"].items():
            (repo / f).write_text(t)

    def test_function_across_files_with_alias_and_reexport(self):
        repo = make_repo({
            "src/price.ts": "export function price(n: number): number {\n  return n * 2\n}\n",
            "src/a.ts": 'import { price } from "./price"\n\nexport const a = price(1) + price(2)\n',
            "src/b.ts": 'import { price as p } from "./price"\n\nexport const b = p(3)\n',
            "src/index.ts": 'export { price } from "./price"\nexport const keep = 1\n',
            "src/other.ts": "export const other = 5\n",
        })
        self.assertEqual(tsc_errors(repo), "")
        d = rename(repo, file="src/price.ts", name="price", to="cost")
        self.assertTrue(d["ok"], d)
        f = d["files"]
        self.assertIn("export function cost(n: number)", f["src/price.ts"])
        self.assertEqual(f["src/a.ts"], 'import { cost } from "./price"\n\nexport const a = cost(1) + cost(2)\n')
        self.assertIn('import { cost as p } from "./price"', f["src/b.ts"])      # the alias stays
        self.assertIn("p(3)", f["src/b.ts"])
        self.assertIn('export { cost as price } from "./price"', f["src/index.ts"])   # the public name stays
        self.assertEqual(set(f), {"src/price.ts", "src/a.ts", "src/b.ts", "src/index.ts"})   # other.ts untouched
        self.assertNotIn("src/other.ts", f)
        self.assertEqual(d["renamed"], {"from": "price", "to": "cost"})
        self.assertEqual(d["count"], len(d["where"]))
        self.assertIn("src/price.ts:1", d["where"])
        self.assertIn("src/a.ts:3", d["where"])
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_the_formatting_around_the_name_is_kept(self):
        repo = make_repo({"src/m.ts": "export   const  old   =   (n: number) =>   n  // the old one\n"
                                       "export const two = old( 1 ,2 )\n"})
        d = rename(repo, file="src/m.ts", name="old", to="fresh")
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["files"]["src/m.ts"], "export   const  fresh   =   (n: number) =>   n  // the old one\n"
                                                  "export const two = fresh( 1 ,2 )\n")

    def test_component_in_jsx_tags(self):
        repo = make_repo({
            "src/card.tsx": "export const Card = ({ children }: { children?: unknown }) => <div>{children}</div>\n",
            "src/page.tsx": 'import { Card } from "./card"\n\nexport const Page = () => (\n  <section>\n'
                            "    <Card>text</Card>\n    <Card />\n  </section>\n)\n",
        })
        d = rename(repo, file="src/card.tsx", name="Card", to="Panel")
        self.assertTrue(d["ok"], d)
        page = d["files"]["src/page.tsx"]
        self.assertIn("<Panel>text</Panel>", page)
        self.assertIn("    <Panel />", page)
        self.assertNotIn("Card", page)
        self.assertEqual(d["count"], 5)
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_a_lowercase_name_would_turn_a_component_into_a_tag(self):
        repo = make_repo({"src/card.tsx": "export const Card = () => <div />\n",
                          "src/page.tsx": 'import { Card } from "./card"\nexport const P = () => <Card />\n'})
        d = rename(repo, file="src/card.tsx", name="Card", to="card")
        self.assertFalse(d["ok"])
        self.assertIn("lowercase", d["error"])

    def test_shorthand_property_keeps_its_key(self):
        repo = make_repo({
            "src/value.ts": "export const old = 1\n",
            "src/use.ts": 'import { old } from "./value"\n\nexport const bag = { old }\nexport const n: number = bag.old + old\n',
        })
        d = rename(repo, file="src/value.ts", name="old", to="fresh")
        self.assertTrue(d["ok"], d)
        use = d["files"]["src/use.ts"]
        self.assertIn("{ old: fresh }", use)
        self.assertIn("bag.old + fresh", use)
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_types_and_interfaces(self):
        repo = make_repo({
            "src/types.ts": "export interface Item { id: number }\nexport type Items = Item[]\n",
            "src/use.ts": 'import type { Item } from "./types"\n\nexport const x: Item = { id: 1 }\n',
        })
        d = rename(repo, file="src/types.ts", name="Item", to="Entry")
        self.assertTrue(d["ok"], d)
        self.assertIn("type Items = Entry[]", d["files"]["src/types.ts"])
        self.assertIn('import type { Entry } from "./types"', d["files"]["src/use.ts"])
        self.apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_a_name_already_declared_in_the_file_is_refused(self):
        repo = make_repo({"src/m.ts": "export const old = 1\nexport const taken = 2\n"})
        d = rename(repo, file="src/m.ts", name="old", to="taken")
        self.assertFalse(d["ok"])
        self.assertIn("already declared or imported", d["error"])
        self.assertEqual((repo / "src/m.ts").read_text(), "export const old = 1\nexport const taken = 2\n")

    def test_a_name_already_imported_by_a_user_is_refused(self):
        repo = make_repo({
            "src/m.ts": "export const old = 1\n",
            "src/other.ts": "export const taken = 2\n",
            "src/use.ts": 'import { old } from "./m"\nimport { taken } from "./other"\n\nexport const v = old + taken\n',
        })
        d = rename(repo, file="src/m.ts", name="old", to="taken")
        self.assertFalse(d["ok"])
        self.assertIn("src/use.ts", d["error"])

    def test_a_local_variable_that_would_shadow_is_refused(self):
        repo = make_repo({
            "src/m.ts": "export const old = 1\n",
            "src/use.ts": 'import { old } from "./m"\n\nexport function f(fresh: number) {\n  return old + fresh\n}\n',
        })
        d = rename(repo, file="src/m.ts", name="old", to="fresh")
        self.assertFalse(d["ok"])
        self.assertIn("already declared or imported", d["error"])

    def test_a_global_of_the_standard_library_can_be_shadowed(self):
        repo = make_repo({"src/m.ts": "export const old = 1\nexport const v = old\n"})
        d = rename(repo, file="src/m.ts", name="old", to="status")
        self.assertTrue(d["ok"], d)

    def test_ambiguous_name_needs_a_line(self):
        repo = make_repo({"src/m.ts": "export function dup() {}\n\nexport const dup = 2\n\nexport const user = 1\n"})
        d = rename(repo, file="src/m.ts", name="dup", to="single")
        self.assertFalse(d["ok"])
        self.assertIn("declared more than once", d["error"])
        self.assertIn("(lines 1, 3)", d["error"])
        d = rename(repo, file="src/m.ts", name="dup", to="single", line=3)
        self.assertTrue(d["ok"], d)
        self.assertIn("export const single = 2", d["files"]["src/m.ts"])
        self.assertIn("export function dup()", d["files"]["src/m.ts"])

    def test_overloads_and_merged_declarations_are_one_declaration(self):
        repo = make_repo({"src/m.ts": "export function f(a: string): string\nexport function f(a: number): number\n"
                                       "export function f(a: any) { return a }\n"
                                       "export const Thing = 1\nexport type Thing = number\n"
                                       "export const x: Thing = f(Thing)\n"})
        d = rename(repo, file="src/m.ts", name="f", to="g")
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["files"]["src/m.ts"].count("function g"), 3)
        d = rename(repo, file="src/m.ts", name="Thing", to="Stuff")
        self.assertTrue(d["ok"], d)
        self.assertIn("const x: Stuff = f(Stuff)", d["files"]["src/m.ts"])

    def test_a_line_that_matches_nothing(self):
        repo = make_repo({"src/m.ts": "export const old = 1\n"})
        d = rename(repo, file="src/m.ts", name="old", to="fresh", line=9)
        self.assertFalse(d["ok"])
        self.assertIn("line 9", d["error"])

    def test_a_name_that_does_not_exist(self):
        repo = make_repo({"src/m.ts": "export const old = 1\n"})
        d = rename(repo, file="src/m.ts", name="nothing", to="fresh")
        self.assertFalse(d["ok"])
        self.assertIn("no top-level declaration named nothing", d["error"])

    def test_invalid_new_names_are_refused(self):
        repo = make_repo({"src/m.ts": "export const old = 1\n"})
        for bad in ("2fast", "a b", "class", "a-b", "", "old"):
            d = rename(repo, file="src/m.ts", name="old", to=bad)
            self.assertFalse(d["ok"], bad)

    def test_a_file_the_project_does_not_load(self):
        repo = make_repo({"lib/m.ts": "export const old = 1\n"})
        d = rename(repo, file="lib/m.ts", name="old", to="fresh")     # outside tsconfig's include: still renamed alone
        self.assertTrue(d["ok"], d)
        self.assertEqual(list(d["files"]), ["lib/m.ts"])

    def test_an_error_the_rename_would_add_is_refused(self):
        # the string "old" names an export by spelling: TypeScript can't know, and after the rename it no longer type-checks
        repo = make_repo({
            "src/m.ts": "export const old = 1\n",
            "src/use.ts": 'import * as m from "./m"\n\nexport const k: keyof typeof m = "old"\n',
        })
        self.assertEqual(tsc_errors(repo), "")
        d = rename(repo, file="src/m.ts", name="old", to="fresh")
        self.assertFalse(d["ok"], d)
        self.assertIn("would add 1 error", d["error"])
        self.assertIn("src/use.ts:3", d["error"])
        self.assertEqual((repo / "src/m.ts").read_text(), "export const old = 1\n")

    def test_errors_the_project_already_had_do_not_block_a_rename(self):
        repo = make_repo({
            "src/m.ts": "export const old = 1\nexport const bad: number = 'x'\n",
            "src/use.ts": 'import { old } from "./m"\n\nexport const v: string = old\n',
        })
        d = rename(repo, file="src/m.ts", name="old", to="fresh")
        self.assertTrue(d["ok"], d)

    def test_a_use_inside_node_modules_is_refused(self):
        repo = make_repo({
            "src/m.ts": "export const old = 1\n",
            "src/use.ts": 'import { v } from "pkg"\nexport const w = v\n',
            "node_modules/pkg/index.ts": 'import { old } from "../../src/m"\nexport const v = old\n',
            "node_modules/pkg/package.json": '{"name": "pkg", "main": "index.ts"}',
        })
        d = rename(repo, file="src/m.ts", name="old", to="fresh")
        self.assertFalse(d["ok"])
        self.assertIn("node_modules/pkg/index.ts", d["error"])

    def test_the_declaring_file_is_never_written_here(self):
        repo = make_repo({"src/m.ts": "export const old = 1\n"})
        rename(repo, file="src/m.ts", name="old", to="fresh")
        self.assertEqual((repo / "src/m.ts").read_text(), "export const old = 1\n")


if __name__ == "__main__":
    unittest.main()
