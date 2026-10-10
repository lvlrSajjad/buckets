// Every named function/const/arrow declaration (any depth): name, lines, whitespace-free text.
const ts = require(process.argv[2]); const fs = require("fs");
const out = {};
for (const f of process.argv.slice(3)) {
  const src = fs.readFileSync(f, "utf8"); const sf = ts.createSourceFile(f, src, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const list = []; const line = p => sf.getLineAndCharacterOfPosition(p).line + 1;
  const visit = (n, depth) => {
    let name = null;
    if ((ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n)) && n.name) name = n.name.getText();
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name) && n.initializer) name = n.name.getText();
    if (name) list.push({ name, depth, lo: line(n.getStart()), hi: line(n.getEnd()), text: n.getText().replace(/\s+/g, "") });
    ts.forEachChild(n, c => visit(c, depth + (name ? 1 : 0)));
  };
  visit(sf, 0); out[f] = list;
}
process.stdout.write(JSON.stringify(out));
