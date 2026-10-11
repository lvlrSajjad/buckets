"""Tests for ts_moves.cjs's fold op: two identical variant files become one, every importer repointed."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from test_rename import SCRIPT, TS_PKG, make_repo, tsc_errors

HELPER = 'import { fmt } from "../../../util/fmt"\n\nexport function label(n: number): string {\n  return fmt(n) + "!"\n}\n'


def fold(repo: Path, **payload) -> dict:
    r = subprocess.run(["node", str(SCRIPT)], input=json.dumps({"op": "fold", "repo": str(repo), **payload}),
                       cwd=repo, capture_output=True, text=True, timeout=120)
    return json.loads(r.stdout)


def apply(repo: Path, d: dict):
    for f in d["deleted"]:
        (repo / f).unlink()
    for f, t in d["files"].items():
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        (repo / f).write_text(t)


def variants(extra: dict | None = None, b_helper: str = HELPER) -> Path:
    return make_repo({
        "src/util/fmt.ts": "export const fmt = (n: number) => n.toFixed(2)\n",
        "src/screens/x/app/helper.ts": HELPER,
        "src/screens/x/pro/helper.ts": b_helper,
        "src/screens/x/app/screen.ts": 'import { label } from "./helper"\n\nexport const s = label(1)\n',
        "src/screens/x/pro/screen.ts": 'import { label } from "./helper"\nimport { fmt } from "../../../util/fmt"\n\nexport const s = label(2) + fmt(3)\n',
        **(extra or {}),
    })


@unittest.skipUnless(TS_PKG.exists() and shutil.which("node"), "needs node and a typescript package")
class FoldTests(unittest.TestCase):
    def test_identical_copies_become_one_in_the_parent_folder(self):
        repo = variants({"src/other.ts": 'import { label } from "./screens/x/pro/helper"\n\nexport const o = label(4)\n'})
        self.assertEqual(tsc_errors(repo), "")
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["into"], "src/screens/x/helper.ts")
        self.assertEqual(sorted(d["deleted"]), ["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        f = d["files"]
        self.assertIn('from "../../util/fmt"', f["src/screens/x/helper.ts"].replace("'", '"'))   # its own import repointed
        self.assertIn('"../helper"', f["src/screens/x/app/screen.ts"].replace("'", '"'))
        self.assertIn('"../helper"', f["src/screens/x/pro/screen.ts"].replace("'", '"'))
        self.assertIn('"./screens/x/helper"', f["src/other.ts"].replace("'", '"'))
        self.assertEqual(d["importers_updated"], ["src/other.ts", "src/screens/x/app/screen.ts", "src/screens/x/pro/screen.ts"])
        apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_one_file_importing_both_copies_gets_one_import(self):
        repo = variants({"src/index.ts": '// the screens\nimport { label as appLabel } from "./screens/x/app/helper"\n'
                                         '// the pro one\nimport { label as proLabel } from "./screens/x/pro/helper"\n'
                                         'import { fmt } from "./util/fmt"\n\nexport const all = [appLabel(1), proLabel(2), fmt(3)]\n'})
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        self.assertTrue(d["ok"], d)
        text = d["files"]["src/index.ts"].replace("'", '"')
        self.assertEqual(text.count("screens/x/helper"), 1, text)
        self.assertIn('import { label as appLabel, label as proLabel } from "./screens/x/helper"', text)
        self.assertIn("// the pro one", text)
        apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")

    def test_into_elsewhere(self):
        repo = variants()
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"], into="src/shared/label.ts")
        self.assertTrue(d["ok"], d)
        apply(repo, d)
        self.assertTrue((repo / "src/shared/label.ts").exists())
        self.assertEqual(tsc_errors(repo), "")

    def test_trailing_whitespace_differences_are_still_one_file(self):
        repo = variants(b_helper=HELPER.replace("{\n", "{   \n") + "\n\n")
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        self.assertTrue(d["ok"], d)

    def test_formatting_differences_are_still_one_file(self):
        repo = variants(b_helper=HELPER.replace("  return", "    return").replace("\n\n", "\n"))
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        self.assertTrue(d["ok"], d)
        self.assertTrue(d["formatting_differed"])

    def test_files_nothing_imports_are_refused(self):
        cfg = "module.exports = { presets: [] }\n"
        repo = make_repo({"src/apps/app/babel.config.js": cfg, "src/apps/pro/babel.config.js": cfg})
        d = fold(repo, files=["src/apps/app/babel.config.js", "src/apps/pro/babel.config.js"])
        self.assertFalse(d["ok"])
        self.assertIn("no file imports", d["error"])

    def test_declaration_files_are_refused(self):
        dts = "declare module '*.png'\n"
        repo = make_repo({"src/apps/app/custom.d.ts": dts, "src/apps/pro/custom.d.ts": dts})
        d = fold(repo, files=["src/apps/app/custom.d.ts", "src/apps/pro/custom.d.ts"])
        self.assertFalse(d["ok"])
        self.assertIn("declaration files", d["error"])

    def test_different_copies_are_refused(self):
        repo = variants(b_helper=HELPER.replace('"!"', '"?"'))
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        self.assertFalse(d["ok"])
        self.assertIn("differ", d["error"])
        self.assertIn("line 4", d["error"])

    def test_copies_reading_their_own_siblings_are_refused(self):
        # byte-identical, but "./theme" is a different file in each folder
        local = 'import { color } from "./theme"\n\nexport const tint = () => color\n'
        repo = make_repo({
            "src/screens/x/app/theme.ts": 'export const color = "red"\n',
            "src/screens/x/pro/theme.ts": 'export const color = "blue"\n',
            "src/screens/x/app/tint.ts": local,
            "src/screens/x/pro/tint.ts": local,
            "src/screens/x/app/use.ts": 'import { tint } from "./tint"\nexport const u = tint()\n',
        })
        d = fold(repo, files=["src/screens/x/app/tint.ts", "src/screens/x/pro/tint.ts"])
        self.assertFalse(d["ok"])
        self.assertIn("import different files", d["error"])

    def test_existing_target_is_refused(self):
        repo = variants({"src/screens/x/helper.ts": "export const taken = 1\n"})
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        self.assertFalse(d["ok"])
        self.assertIn("already exists", d["error"])

    def test_a_path_typescript_does_not_repoint_is_refused(self):
        repo = variants({"src/mocked.ts": 'declare const jest: any\njest.mock("./screens/x/app/helper")\nexport const m = 1\n'})
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper.ts"])
        self.assertFalse(d["ok"])
        self.assertIn("src/mocked.ts", d["error"])

    def test_different_names_need_into(self):
        repo = variants({"src/screens/x/pro/helper2.ts": HELPER})
        d = fold(repo, files=["src/screens/x/app/helper.ts", "src/screens/x/pro/helper2.ts"])
        self.assertFalse(d["ok"])
        self.assertIn('pass "into"', d["error"])

    def test_tsx_component_with_jsx_importers(self):
        comp = 'export function Badge(p: { n: number }) {\n  return <b>{p.n}</b>\n}\n'
        repo = make_repo({
            "src/screens/y/app/badge.tsx": comp,
            "src/screens/y/pro/badge.tsx": comp,
            "src/screens/y/app/page.tsx": 'import { Badge } from "./badge"\n\nexport const P = () => <Badge n={1} />\n',
            "src/screens/y/pro/page.tsx": 'import { Badge } from "./badge"\n\nexport const P = () => <Badge n={2} />\n',
        })
        self.assertEqual(tsc_errors(repo), "")
        d = fold(repo, files=["src/screens/y/app/badge.tsx", "src/screens/y/pro/badge.tsx"])
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["into"], "src/screens/y/badge.tsx")
        apply(repo, d)
        self.assertEqual(tsc_errors(repo), "")


if __name__ == "__main__":
    unittest.main()
