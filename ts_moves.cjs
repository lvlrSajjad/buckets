#!/usr/bin/env node
// Mechanical moves for TypeScript/JavaScript, so a model decides what goes where and buckets moves
// the code byte for byte instead of having it retyped.
//
//   extract  move a component's logic (or the named statements) into a new hook file: the hook's
//            parameters, its return value, the private helpers that must come along and the
//            imports on both sides are worked out from TypeScript's own scoping.
//   compare  pair the declarations of two variants of a file (identical / near / different / one side).
//   share    move declarations that are identical in two variants into one shared file.
//   syntax   whether a file's text parses (a reflex after an edit; no types).
//   rename   a top-level declaration gets a new name in every file of the project (TypeScript's own rename).
//   find_copies  functions in other files that are a shared file's exported functions under other names.
//   dedupe   copies replaced by an import of the shared function ("aliases" lets a copy under another name go too).
//   fold     two identical variant files become one in their parent folder; every importer is repointed.
//
// Input: one JSON object on stdin with "op" and the op's fields; output: one JSON object on stdout.
// Files in the output are full new texts (paths relative to the repo); nothing is written here.
'use strict';
const fs = require('fs');
const path = require('path');

function loadTs(repo) {
  try {
    return require(require.resolve('typescript', { paths: [repo] }));
  } catch (e) {
    return require('typescript');
  }
}

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const repo = path.resolve(input.repo || '.');
const ts = loadTs(repo);
const abs = (p) => path.resolve(repo, p);
const realRepo = (() => { try { return fs.realpathSync(repo); } catch (e) { return repo; } })();
function rel(p) {
  let r = path.relative(repo, p);
  if (r.startsWith('..')) r = path.relative(realRepo, p);  // TypeScript may report a symlinked directory by its real path
  return r.split(path.sep).join('/');
}

function program(files) {
  const roots = files.map(abs);
  const cfgPath = ts.findConfigFile(path.dirname(roots[0]), ts.sys.fileExists);
  let options = { allowJs: true, jsx: ts.JsxEmit.Preserve };
  if (cfgPath) {
    const cfg = ts.parseJsonConfigFileContent(ts.readConfigFile(cfgPath, ts.sys.readFile).config, ts.sys,
                                              path.dirname(cfgPath));
    options = cfg.options;
  }
  const prog = ts.createProgram(roots, { ...options, allowJs: true, noEmit: true });
  const sources = roots.map((r) => prog.getSourceFile(r));
  const missing = roots.filter((r, i) => !sources[i]);
  if (missing.length) throw new Error(`TypeScript didn't load ${missing.map(rel).join(', ')} (excluded by its tsconfig?)`);
  return { prog, checker: prog.getTypeChecker(), sources };
}

const lineOf = (sf, pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;
const lineStart = (text, pos) => text.lastIndexOf('\n', pos - 1) + 1;
function lineEnd(text, pos) {
  const i = text.indexOf('\n', pos);
  return i < 0 ? text.length : i + 1;
}

// Where a statement's text starts, with the comments above it.
function fullStart(sf, node) {
  const comments = ts.getLeadingCommentRanges(sf.text, node.pos) || [];
  return lineStart(sf.text, comments.length ? comments[0].pos : node.getStart(sf));
}

function hasJsx(node) {
  let found = false;
  const visit = (n) => {
    if (found) return;
    if (ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n) || ts.isJsxFragment(n)) found = true;
    else ts.forEachChild(n, visit);
  };
  visit(node);
  return found;
}

// A return that belongs to the function itself, not to a function nested in the statement.
function hasOwnReturn(node) {
  let found = false;
  const visit = (n) => {
    if (found || ts.isFunctionLike(n) || ts.isClassLike(n)) return;
    if (ts.isReturnStatement(n)) found = true;
    else ts.forEachChild(n, visit);
  };
  visit(node);
  return found;
}

function bindingNames(name, out = []) {
  if (ts.isIdentifier(name)) out.push(name);
  else for (const el of name.elements || []) if (!ts.isOmittedExpression(el)) bindingNames(el.name, out);
  return out;
}

// The names a statement declares at its own level.
function declaredBy(stmt) {
  if (ts.isVariableStatement(stmt)) return stmt.declarationList.declarations.flatMap((d) => bindingNames(d.name));
  if ((ts.isFunctionDeclaration(stmt) || ts.isClassDeclaration(stmt) || ts.isEnumDeclaration(stmt)
       || ts.isInterfaceDeclaration(stmt) || ts.isTypeAliasDeclaration(stmt)) && stmt.name) return [stmt.name];
  return [];
}

function isExported(stmt) {
  return (ts.getCombinedModifierFlags ? ts.getCombinedModifierFlags(stmt) : 0) & ts.ModifierFlags.Export
    || (stmt.modifiers || []).some((m) => m.kind === ts.SyntaxKind.ExportKeyword);
}

// An identifier that names a member, a key or a label rather than referring to a binding.
function isNonReference(n) {
  const p = n.parent;
  return (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isQualifiedName(p) && p.right === n)
    || (ts.isPropertyAssignment(p) && p.name === n) || (ts.isJsxAttribute(p) && p.name === n)
    || ((ts.isMethodDeclaration(p) || ts.isPropertyDeclaration(p) || ts.isPropertySignature(p)
         || ts.isMethodSignature(p) || ts.isGetAccessor(p) || ts.isSetAccessor(p)) && p.name === n)
    || (ts.isBindingElement(p) && p.propertyName === n) || ts.isImportSpecifier(p) || ts.isExportSpecifier(p)
    || (ts.isLabeledStatement(p) || ts.isBreakOrContinueStatement(p));
}

// Every identifier in `node` that refers to something, with the symbol it refers to.
function references(checker, node) {
  const out = [];
  const visit = (n) => {
    if (ts.isIdentifier(n)) {
      const p = n.parent;
      if (!isNonReference(n)) {
        const sym = ts.isShorthandPropertyAssignment(p) && p.name === n
          ? checker.getShorthandAssignmentValueSymbol(p) : checker.getSymbolAtLocation(n);
        if (sym) out.push({ node: n, sym });
      }
    }
    ts.forEachChild(n, visit);
  };
  visit(node);
  return out;
}

function isAssigned(id) {
  let n = id;
  while (ts.isParenthesizedExpression(n.parent)) n = n.parent;
  const p = n.parent;
  if (ts.isBinaryExpression(p) && p.left === n && p.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && p.operatorToken.kind <= ts.SyntaxKind.LastAssignment) return true;
  return (ts.isPrefixUnaryExpression(p) || ts.isPostfixUnaryExpression(p))
    && (p.operator === ts.SyntaxKind.PlusPlusToken || p.operator === ts.SyntaxKind.MinusMinusToken);
}

function topStatement(sf, node) {
  let n = node;
  while (n.parent && n.parent !== sf) n = n.parent;
  return n.parent === sf ? n : null;
}

const within = (node, outer) => node.getSourceFile() === outer.getSourceFile() && node.pos >= outer.pos && node.end <= outer.end;

// The component (or function) by name: a function declaration, or a variable whose value is a
// function, possibly wrapped (`observer(function X() {...})`, `memo(() => {...})`).
function findFunction(sf, name) {
  let found = null;
  const isFn = (n) => (ts.isFunctionExpression(n) || ts.isArrowFunction(n) || ts.isFunctionDeclaration(n))
    && n.body && ts.isBlock(n.body);
  const firstFn = (n) => (isFn(n) ? n : ts.forEachChild(n, firstFn));
  const visit = (n) => {
    if (found) return;
    if (ts.isFunctionDeclaration(n) && n.name && n.name.text === name && isFn(n)) found = n;
    else if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name) && n.name.text === name && n.initializer)
      found = firstFn(n.initializer);
    else if (ts.isFunctionExpression(n) && n.name && n.name.text === name && isFn(n)) found = n;
    if (!found) ts.forEachChild(n, visit);
  };
  visit(sf);
  return found;
}

function moduleSpecifier(spec, fromFile, toFile) {
  if (!spec.startsWith('.')) return spec;
  let r = path.relative(path.dirname(toFile), path.resolve(path.dirname(fromFile), spec)).split(path.sep).join('/');
  return r.startsWith('.') ? r : './' + r;
}

function importPath(fromFile, toFile) {
  const r = moduleSpecifier('./' + path.basename(toFile).replace(/\.(tsx?|jsx?)$/, ''), toFile, fromFile);
  return r;
}

// Import declarations rebuilt with only the given symbols (by import declaration), for another file.
function importsFor(sf, used, fromFile, toFile) {
  const lines = [];
  for (const stmt of sf.statements) {
    if (!ts.isImportDeclaration(stmt) || !stmt.importClause) continue;
    const clause = stmt.importClause;
    const spec = moduleSpecifier(stmt.moduleSpecifier.text, fromFile, toFile);
    const typeOnly = clause.isTypeOnly ? 'type ' : '';
    const parts = [];
    if (clause.name && used.has(clause.name)) parts.push(clause.name.text);
    const nb = clause.namedBindings;
    if (nb && ts.isNamespaceImport(nb) && used.has(nb.name)) parts.push(`* as ${nb.name.text}`);
    const named = nb && ts.isNamedImports(nb) ? nb.elements.filter((e) => used.has(e.name)) : [];
    if (named.length) parts.push(`{ ${named.map((e) => e.getText(sf)).join(', ')} }`);
    if (parts.length) lines.push(`import ${typeOnly}${parts.join(', ')} from "${spec}"`);
  }
  return lines;
}

// Import lines added to a file's text: names join an existing import of the same module (same
// type-only-ness) instead of a second import line; anything else goes after the last import.
function mergeImports(text, lines, fileName = 'x.tsx') {
  let out = text;
  for (const line of lines) {
    const sf = ts.createSourceFile(fileName, out, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const add = ts.createSourceFile('l.ts', line, ts.ScriptTarget.Latest, true).statements[0];
    if (!add || !ts.isImportDeclaration(add) || !add.importClause) continue;
    const spec = add.moduleSpecifier.text;
    const ac = add.importClause;
    const addNamed = ac.namedBindings && ts.isNamedImports(ac.namedBindings) ? ac.namedBindings.elements.map((e) => e.getText()) : [];
    const same = sf.statements.find((st) => ts.isImportDeclaration(st) && st.importClause && st.moduleSpecifier.text === spec
      && !!st.importClause.isTypeOnly === !!ac.isTypeOnly && !(st.importClause.namedBindings && ts.isNamespaceImport(st.importClause.namedBindings))
      && !(ac.namedBindings && ts.isNamespaceImport(ac.namedBindings)) && !(st.importClause.name && ac.name));
    if (same) {
      const sc = same.importClause;
      const had = sc.namedBindings && ts.isNamedImports(sc.namedBindings) ? sc.namedBindings.elements.map((e) => e.getText(sf)) : [];
      const named = [...had, ...addNamed.filter((n) => !had.includes(n))];
      const def = sc.name ? sc.name.text : ac.name ? ac.name.text : '';
      const parts = [def, named.length ? `{ ${named.join(', ')} }` : ''].filter(Boolean);
      out = out.slice(0, same.getStart(sf)) + `import ${sc.isTypeOnly ? 'type ' : ''}${parts.join(', ')} from ${same.moduleSpecifier.getText(sf)}`
        + out.slice(same.end);
    } else {
      const at = afterImports(sf);
      out = out.slice(0, at) + line + '\n' + out.slice(at);
    }
  }
  return out;
}

// Remove import specifiers nothing references any more (React stays: JSX may need it).
function pruneImports(sf, stillUsed, edits) {
  for (const stmt of sf.statements) {
    if (!ts.isImportDeclaration(stmt) || !stmt.importClause) continue;
    const clause = stmt.importClause;
    const keep = [];
    if (clause.name && (stillUsed.has(clause.name) || clause.name.text === 'React')) keep.push(clause.name.text);
    const nb = clause.namedBindings;
    if (nb && ts.isNamespaceImport(nb) && stillUsed.has(nb.name)) keep.push(`* as ${nb.name.text}`);
    const named = nb && ts.isNamedImports(nb) ? nb.elements : [];
    const keptNamed = named.filter((e) => stillUsed.has(e.name));
    const total = (clause.name ? 1 : 0) + (nb ? (ts.isNamespaceImport(nb) ? 1 : named.length) : 0);
    const kept = keep.length + keptNamed.length;
    if (kept === total) continue;
    if (!kept) {
      edits.push([fullStart(sf, stmt), lineEnd(sf.text, stmt.end), '']);
      continue;
    }
    if (keptNamed.length) keep.push(`{ ${keptNamed.map((e) => e.getText(sf)).join(', ')} }`);
    edits.push([stmt.getStart(sf), stmt.end,
                `import ${clause.isTypeOnly ? 'type ' : ''}${keep.join(', ')} from ${stmt.moduleSpecifier.getText(sf)}`]);
  }
}

// Edits applied from the end of the file backwards. Blank lines are tidied only where a removal left
// more than one in a row: the file's own blank lines elsewhere stay as they were.
function applyEdits(text, edits) {
  const sorted = [...edits].sort((a, b) => b[0] - a[0] || b[1] - a[1]);
  let out = text;
  let last = Infinity;
  for (const [s, e, r] of sorted) {
    if (e > last) throw new Error('overlapping edits');
    out = out.slice(0, s) + r + out.slice(e);
    if (r === '' || r.trim() === '') {
      let a = s, b = s + r.length;
      while (a > 0 && /[ \t\n]/.test(out[a - 1])) a--;
      while (b < out.length && /[ \t\n]/.test(out[b])) b++;
      const gap = out.slice(a, b);
      const lines = (gap.match(/\n/g) || []).length;
      if (lines > 2) out = out.slice(0, a) + (a === 0 ? '' : '\n\n') + gap.replace(/^[\s\S]*\n/, '') + out.slice(b);
    }
    last = s;
  }
  return out;
}

function afterImports(sf) {
  const imports = sf.statements.filter(ts.isImportDeclaration);
  return imports.length ? lineEnd(sf.text, imports[imports.length - 1].end) : 0;
}

// The symbols each import declaration binds, as identifier nodes keyed by symbol.
function importBindings(sf, checker) {
  const map = new Map();
  for (const stmt of sf.statements) {
    if (!ts.isImportDeclaration(stmt) || !stmt.importClause) continue;
    const c = stmt.importClause;
    const ids = [];
    if (c.name) ids.push(c.name);
    if (c.namedBindings && ts.isNamespaceImport(c.namedBindings)) ids.push(c.namedBindings.name);
    if (c.namedBindings && ts.isNamedImports(c.namedBindings)) ids.push(...c.namedBindings.elements.map((e) => e.name));
    for (const id of ids) {
      const sym = checker.getSymbolAtLocation(id);
      if (sym) map.set(sym, id);
    }
  }
  return map;
}

function typeText(checker, decl, at, target) {
  let t = checker.typeToString(checker.getTypeAtLocation(decl), at, ts.TypeFormatFlags.NoTruncation);
  t = t.replace(/import\("([^"]+)"\)/g, (m, p) => {
    if (!path.isAbsolute(p)) return m;
    const r = moduleSpecifier('./' + path.basename(p), p, target);
    return `import("${r.replace(/\.(d\.ts|tsx?|jsx?)$/, '')}")`;
  });
  return t;
}

// ---------------------------------------------------------------- extract

// What moving a component's statements into a hook means for one file: which statements move and
// which must stay, the module helpers that come along, the hook's parameters and returns, and
// builders for the hook's text and the file's new text.
function analyze(sf, checker, relFile, fnName, select, target, hook) {
  const file = abs(relFile);
  const text = sf.text;
  const fn = findFunction(sf, fnName);
  if (!fn) throw new Error(`no function or component named ${fnName} in ${relFile}`);
  const body = fn.body.statements;
  const component = topStatement(sf, fn);

  // What moves: the named statements and line ranges, or (select "logic") every statement that
  // renders nothing and doesn't return.
  const wanted = new Set(Array.isArray(select) ? select.filter((s) => !/^\d+(-\d+)?$/.test(s)) : []);
  const ranges = Array.isArray(select) ? select.filter((s) => /^\d+(-\d+)?$/.test(s))
    .map((s) => s.split('-').map(Number)).map(([a, b]) => [a, b || a]) : [];
  const picked = (stmt) => {
    if (select === 'logic') return !hasJsx(stmt) && !hasOwnReturn(stmt);
    const line = lineOf(sf, stmt.getStart(sf));
    return declaredBy(stmt).some((id) => wanted.has(id.text)) || ranges.some(([a, b]) => line >= a && line <= b);
  };
  const moved = new Set(body.filter(picked));
  const notes = [];

  const refsOf = new Map(body.map((s) => [s, references(checker, s)]));
  const declStmt = (decl) => body.find((s) => within(decl, s));
  const params = new Set(fn.parameters.flatMap((p) => bindingNames(p.name)).map((id) => checker.getSymbolAtLocation(id)));

  // Keep a statement when moving it would break: it uses a kept local declared after where the
  // hook is called, assigns to a kept local, or kept code assigns to what it declares.
  for (let changed = true; changed;) {
    changed = false;
    const first = body.find((s) => moved.has(s));
    if (!first) break;
    for (const s of body) {
      if (!moved.has(s)) continue;
      for (const { node, sym } of refsOf.get(s)) {
        const d = sym.declarations && sym.declarations[0];
        if (!d || params.has(sym)) {
          if (d && isAssigned(node)) { moved.delete(s); changed = true; notes.push(`kept \`${firstName(s)}\`: it assigns to the parameter \`${node.text}\``); break; }
          continue;
        }
        const ds = declStmt(d);
        if (!ds || moved.has(ds) || !within(d, fn.body)) continue;
        if (ds.pos >= first.pos || isAssigned(node)) {
          moved.delete(s); changed = true;
          notes.push(`kept \`${firstName(s)}\` in the component: it uses \`${node.text}\`, which stays${ds.pos >= first.pos ? ' and is set after the hook call' : ' and is assigned here'}`);
          break;
        }
      }
    }
    for (const s of body) {
      if (moved.has(s)) continue;
      for (const { node, sym } of refsOf.get(s)) {
        const d = sym.declarations && sym.declarations[0];
        const ds = d && declStmt(d);
        if (ds && moved.has(ds) && isAssigned(node)) {
          moved.delete(ds); changed = true;
          notes.push(`kept \`${node.text}\` in the component: code that stays assigns to it`);
        }
      }
    }
  }
  function firstName(s) {
    const ids = declaredBy(s);
    return ids.length ? ids[0].text : sf.text.slice(s.getStart(sf), s.getStart(sf) + 40).split('\n')[0];
  }
  const movedList = body.filter((s) => moved.has(s));
  if (!movedList.length) throw new Error('nothing to move: no statement matched, or every match has to stay');
  const keptList = body.filter((s) => !moved.has(s));
  let first = movedList[0];
  // A hook can't be called after an early return. When one comes before the moved statements, the call
  // goes before it, if what moves past the return only declares functions (so running it earlier changes
  // nothing) and what the hook needs is set by then.
  const early = body.slice(0, body.indexOf(first)).find((st) => !moved.has(st) && hasOwnReturn(st));
  let callBefore = null;
  if (early) {
    const pure = (st) => ts.isFunctionDeclaration(st) || (ts.isVariableStatement(st) && (st.declarationList.flags & ts.NodeFlags.Const)
      && st.declarationList.declarations.every((d) => d.initializer && (ts.isArrowFunction(d.initializer) || ts.isFunctionExpression(d.initializer))));
    const late = movedList.filter((st) => st.pos > early.pos);
    if (!late.every(pure)) throw new Error(`the statements to move come after an early return (line ${lineOf(sf, early.getStart(sf))}), and a hook can't be called there`);
    callBefore = early;
  }

  // Module-level statements the moved code needs, transitively; they move to the hook's file.
  const imports = importBindings(sf, checker);
  const modNeeded = new Set();
  const usedImports = new Set();
  const queue = movedList.flatMap((s) => refsOf.get(s));
  while (queue.length) {
    const { sym } = queue.pop();
    const d = sym.declarations && sym.declarations[0];
    if (!d) continue;
    if (imports.has(sym)) { usedImports.add(imports.get(sym)); continue; }
    if (d.getSourceFile() !== sf) continue;
    const top = topStatement(sf, d);
    if (!top || top === component || modNeeded.has(top) || ts.isImportDeclaration(top)) continue;
    if (within(d, fn)) continue;
    modNeeded.add(top);
    queue.push(...references(checker, top));
  }
  const moduleMoved = sf.statements.filter((s) => modNeeded.has(s));
  for (const s of moduleMoved) {
    if (isExported(s)) throw new Error(`the moved code needs \`${firstName(s)}\`, which this file exports; move it by hand first`);
  }

  // What stays in the file and still uses moved things.
  const keptRefs = [
    ...keptList.flatMap((s) => refsOf.get(s)),
    ...fn.parameters.flatMap((p) => references(checker, p)),
    ...sf.statements.filter((s) => !modNeeded.has(s) && s !== component && !ts.isImportDeclaration(s))
      .flatMap((s) => references(checker, s)),
    ...references(checker, component).filter(({ node }) => !within(node, fn.body)),
  ];
  const movedDecls = new Map();
  for (const s of movedList) for (const id of declaredBy(s)) movedDecls.set(checker.getSymbolAtLocation(id), id);
  const usedMoved = new Set(keptRefs.filter(({ sym }) => movedDecls.has(sym)).map(({ sym }) => sym));
  const returns = [...movedDecls.entries()].filter(([sym]) => usedMoved.has(sym)).map(([, id]) => id.text);
  const moduleDecls = new Map();
  for (const s of moduleMoved) for (const id of declaredBy(s)) moduleDecls.set(checker.getSymbolAtLocation(id), id);
  for (const { node, sym } of keptRefs) {
    if (moduleDecls.has(sym) && isAssigned(node)) throw new Error(`\`${node.text}\` would move to the hook's file, but code that stays assigns to it`);
  }
  const sharedModule = [...new Set(keptRefs.filter(({ sym }) => moduleDecls.has(sym)).map(({ sym }) => moduleDecls.get(sym).text))];

  // Parameters: kept locals (and the component's parameters) the moved code uses.
  const paramSyms = new Map();
  for (const s of movedList) {
    for (const { sym } of refsOf.get(s)) {
      const d = sym.declarations && sym.declarations[0];
      if (!d || d.getSourceFile() !== sf || !within(d, fn) || movedDecls.has(sym)) continue;
      const ds = declStmt(d);
      if (ds && moved.has(ds)) continue;
      if (!ds && !params.has(sym)) continue; // a local of a nested function
      paramSyms.set(sym, d);
    }
  }
  // A parameter set by a statement that's safe to run again (a context hook like useStores(), or a
  // destructure of something that is) is re-derived inside the hook instead of passed: no long
  // inferred store types in the signature. Stateful hooks (useState, useRef…) never are.
  const STATEFUL = /^use(State|Ref|Reducer|Memo|Callback|Transition|Id|Optimistic|ActionState|SyncExternalStore)$/;
  const rederivable = new Map(); // statement -> names the hook needs from it
  const canRederive = (stmt, seen = new Set()) => {
    if (!stmt || seen.has(stmt) || !ts.isVariableStatement(stmt) || stmt.declarationList.declarations.length !== 1) return false;
    if (callBefore && stmt.pos >= callBefore.pos) return false; // set after an early return: it relies on that guard
    const decl = stmt.declarationList.declarations[0];
    if (!decl.initializer || hasJsx(stmt) || (stmt.declarationList.flags & ts.NodeFlags.Let)) return false;
    seen.add(stmt);
    let ok = true;
    const visit = (n) => {
      if (!ok) return;
      if (ts.isFunctionLike(n)) { ok = false; return; }
      if (ts.isCallExpression(n)) {
        const callee = ts.isIdentifier(n.expression) ? n.expression.text : '';
        if (!/^use[A-Z]/.test(callee) || STATEFUL.test(callee)) { ok = false; return; }
      }
      if (ts.isNewExpression(n) || ts.isAwaitExpression(n)) { ok = false; return; }
      ts.forEachChild(n, visit);
    };
    visit(decl.initializer);
    if (!ok) return false;
    for (const { sym } of references(checker, decl.initializer)) {
      const d = sym.declarations && sym.declarations[0];
      if (!d || d.getSourceFile() !== sf || !within(d, fn.body)) continue;
      const ds = declStmt(d);
      if (ds && !moved.has(ds) && !canRederive(ds, seen)) return false;
    }
    return true;
  };
  const prelude = new Set();
  const needFrom = (stmt, name) => {
    if (!rederivable.has(stmt)) rederivable.set(stmt, new Set());
    rederivable.get(stmt).add(name);
    const decl = stmt.declarationList.declarations[0];
    for (const { sym } of references(checker, decl.initializer)) {
      const d = sym.declarations && sym.declarations[0];
      if (!d || d.getSourceFile() !== sf || !within(d, fn.body)) continue;
      const ds = declStmt(d);
      if (ds && !moved.has(ds)) needFrom(ds, sym.name);
    }
    prelude.add(stmt);
  };
  for (const [sym, d] of [...paramSyms.entries()]) {
    const ds = declStmt(d);
    if (ds && ds.pos < first.pos && canRederive(ds)) {
      needFrom(ds, sym.name);
      paramSyms.delete(sym);
    }
  }
  const preludeText = body.filter((st) => prelude.has(st)).map((st) => {
    const decl = st.declarationList.declarations[0];
    const want = rederivable.get(st);
    const ind = text.slice(lineStart(text, st.getStart(sf)), st.getStart(sf)).match(/^\s*/)[0];
    if (ts.isObjectBindingPattern(decl.name)) {
      const keep = decl.name.elements.filter((e) => bindingNames(e.name).some((id) => want.has(id.text)));
      return `${ind}const { ${keep.map((e) => e.getText(sf)).join(', ')} } = ${decl.initializer.getText(sf)}\n`;
    }
    return text.slice(lineStart(text, st.getStart(sf)), lineEnd(text, st.end));
  }).join('');
  for (const st of prelude) for (const { sym } of references(checker, st.declarationList.declarations[0].initializer)) {
    if (imports.has(sym)) usedImports.add(imports.get(sym));
  }
  // A prop's type comes from the component's own props type (`Props["navigation"]`), with that type's
  // names imported; another local keeps its printed type when it's plain, else `any` with a note.
  let propsType = fn.parameters[0] && fn.parameters[0].type;
  if (!propsType) {
    let n = fn;
    while (n && !ts.isVariableDeclaration(n)) n = n.parent;
    const t = n && n.type;
    if (t && ts.isTypeReferenceNode(t) && /(^|\.)(FunctionComponent|FC|VFC|ComponentType)$/.test(t.typeName.getText(sf))
        && t.typeArguments && t.typeArguments.length) propsType = t.typeArguments[0];
  }
  const propNames = new Set(fn.parameters[0] ? bindingNames(fn.parameters[0].name).map((id) => id.text) : []);
  const typeOf = (sym, d) => {
    if (propsType && propNames.has(sym.name) && ts.isBindingElement(d)) {
      const key = d.propertyName ? d.propertyName.getText(sf) : sym.name;
      const member = ts.isTypeLiteralNode(propsType)
        && propsType.members.find((m) => ts.isPropertySignature(m) && m.name.getText(sf) === key && m.type);
      const typeNode = member ? member.type : propsType;
      for (const { sym: s2 } of references(checker, typeNode)) if (imports.has(s2)) usedImports.add(imports.get(s2));
      if (member) return member.type.getText(sf) + (member.questionToken && member.type.kind !== ts.SyntaxKind.AnyKeyword ? ' | undefined' : '');
      return `${propsType.getText(sf)}["${key}"]`;
    }
    if (d.type) {
      for (const { sym: s2 } of references(checker, d.type)) if (imports.has(s2)) usedImports.add(imports.get(s2));
      return d.type.getText(sf);
    }
    const t = typeText(checker, d, fn, target);
    if (!/import\(/.test(t) && t.length <= 120) return t;
    notes.push(`\`${sym.name}\` is passed in typed \`any\`: its inferred type is too long to write out`);
    return 'any';
  };
  const paramList = [...paramSyms.entries()].map(([sym, d]) => ({ name: sym.name, type: typeOf(sym, d) }));
  if (callBefore) {
    for (const [sym, d] of paramSyms) {
      const ds = declStmt(d);
      if (ds && ds.pos >= callBefore.pos) throw new Error(`the hook would need \`${sym.name}\`, which is set after the early return at line ${lineOf(sf, callBefore.getStart(sf))}`);
    }
  }
  for (const { node, sym } of movedList.flatMap((s) => refsOf.get(s))) {
    if (paramSyms.has(sym) && isAssigned(node)) throw new Error(`the moved code assigns to \`${sym.name}\`, which stays`);
  }

  // Imports the moved code needs, rebuilt for the new file.
  for (const s of moduleMoved) for (const { sym } of references(checker, s)) if (imports.has(sym)) usedImports.add(imports.get(sym));
  const hookImports = importsFor(sf, usedImports, file, target);
  const stillUsed = new Set();
  for (const { sym } of keptRefs) if (imports.has(sym)) stillUsed.add(imports.get(sym));

  const indent = text.slice(lineStart(text, first.getStart(sf)), first.getStart(sf)).match(/^\s*/)[0];
  const chunk = (s) => text.slice(fullStart(sf, s), lineEnd(text, s.end)).replace(/\n*$/, '\n');
  const exported = (s, names) => {
    const t = chunk(s);
    if (!declaredBy(s).some((id) => names.includes(id.text))) return t;
    const at = s.getStart(sf) - fullStart(sf, s);
    return t.slice(0, at) + 'export ' + t.slice(at);
  };
  // The hook's file: imports, the helpers that came along (exported when a component still uses
  // them), then the hook with the moved statements exactly as they were.
  const hookText = (rets, exportNames) => {
    const sig = paramList.length
      ? `{ ${paramList.map((p) => p.name).join(', ')} }: { ${paramList.map((p) => `${p.name}: ${p.type}`).join('; ')} }` : '';
    const ret = rets.length ? `\n${indent}return {\n${rets.map((r) => `${indent}  ${r},`).join('\n')}\n${indent}}\n` : '';
    return [hookImports.join('\n'), ...moduleMoved.map((s) => exported(s, exportNames)),
            `export function ${hook}(${sig}) {\n` + preludeText + movedList.map(chunk).join('') + ret + '}\n'].filter(Boolean).join('\n');
  };
  // The component's file: the moved statements become one call; helpers and unused imports go.
  const fileText = (importPathTo) => {
    const edits = [];
    const call = `${hook}(${paramList.length ? `{ ${paramList.map((p) => p.name).join(', ')} }` : ''})`;
    const callText = returns.length > 3
      ? `${indent}const {\n${returns.map((r) => `${indent}  ${r},`).join('\n')}\n${indent}} = ${call}\n`
      : returns.length ? `${indent}const { ${returns.join(', ')} } = ${call}\n` : `${indent}${call}\n`;
    if (callBefore) {
      for (const s of movedList) edits.push([fullStart(sf, s), lineEnd(text, s.end), '']);
      edits.push([fullStart(sf, callBefore), fullStart(sf, callBefore), callText]);
    } else {
      for (const s of movedList) edits.push([fullStart(sf, s), lineEnd(text, s.end), s === first ? callText : '']);
    }
    for (const s of moduleMoved) edits.push([fullStart(sf, s), lineEnd(text, s.end), '']);
    edits.push(...unusedEdits);
    pruneImports(sf, stillUsed, edits);
    const names = [hook, ...sharedModule];
    return mergeImports(applyEdits(text, edits), [`import { ${names.join(', ')} } from "${importPath(file, importPathTo)}"`], file);
  };
  // Names a kept destructure set only for the moved code are now unused (lint errors): drop them,
  // and the whole statement when nothing is left and running it has no effect.
  const movedUses = new Set(movedList.flatMap((st) => refsOf.get(st)).map(({ sym }) => sym));
  const removedStmts = new Set();
  const dropped = new Map(); // statement -> elements dropped
  for (let changed = true; changed;) {
    changed = false;
    const live = keptRefs.filter(({ node }) => ![...removedStmts].some((st) => within(node, st))
      && ![...dropped.values()].some((els) => els.some((e) => within(node, e))));
    for (const st of keptList) {
      if (removedStmts.has(st) || !ts.isVariableStatement(st) || st.declarationList.declarations.length !== 1) continue;
      const decl = st.declarationList.declarations[0];
      if (!ts.isObjectBindingPattern(decl.name)) continue;
      const already = dropped.get(st) || [];
      const unused = (id) => {
        const sym = checker.getSymbolAtLocation(id);
        return !live.some(({ node, sym: s2 }) => s2 === sym && node !== id);
      };
      const lost = (id) => movedUses.has(checker.getSymbolAtLocation(id))
        || [...removedStmts, ...already].length > 0; // made unused by the move or by an earlier removal
      const more = decl.name.elements.filter((e) => !already.includes(e)
        && bindingNames(e.name).every((id) => unused(id) && lost(id)));
      if (!more.length) continue;
      dropped.set(st, [...already, ...more]);
      if (dropped.get(st).length === decl.name.elements.length && canRederive(st)) removedStmts.add(st);
      changed = true;
    }
  }
  // Imports still used once the unused statements and names are gone.
  const finalRefs = keptRefs.filter(({ node }) => ![...removedStmts].some((st) => within(node, st))
    && ![...dropped.values()].some((els) => els.some((e) => within(node, e))));
  stillUsed.clear();
  for (const { sym } of finalRefs) if (imports.has(sym)) stillUsed.add(imports.get(sym));
  const unusedEdits = [];
  for (const [st, els] of dropped) {
    if (removedStmts.has(st)) { unusedEdits.push([fullStart(sf, st), lineEnd(text, st.end), '']); continue; }
    const decl = st.declarationList.declarations[0];
    const keep = decl.name.elements.filter((e) => !els.includes(e));
    if (keep.length) unusedEdits.push([decl.name.getStart(sf), decl.name.end, `{ ${keep.map((e) => e.getText(sf)).join(', ')} }`]);
  }
  const hasJsxMoved = movedList.some(hasJsx) || moduleMoved.some(hasJsx);
  return {
    sf, movedList, keptList, moduleMoved, sharedModule, paramList, returns, notes, hookText, fileText, hasJsxMoved,
    report: {
      moved: movedList.length, kept: keptList.length,
      moved_lines: movedList.reduce((n, s) => n + lineOf(sf, s.end) - lineOf(sf, s.getStart(sf)) + 1, 0),
      params: paramList, returns, helpers_moved: moduleMoved.map(firstName), helpers_shared: sharedModule, notes,
      rederived: [...prelude].map(firstName),
    },
  };
}

function withJsxExtension(target, hasJsx, notes) {
  if (/\.ts$/.test(target) && hasJsx) {
    notes.push(`the moved code has JSX, so the new file is ${rel(target.replace(/\.ts$/, '.tsx'))}`);
    return target.replace(/\.ts$/, '.tsx');
  }
  return target;
}

function extract() {
  const target = abs(input.into);
  if (fs.existsSync(target)) throw new Error(`${input.into} already exists`);
  const { checker, sources: [sf] } = program([input.file]);
  const a = analyze(sf, checker, input.file, input.function, input.select, target, input.name);
  const finalTarget = withJsxExtension(target, a.hasJsxMoved, a.notes);
  return { files: { [input.file]: a.fileText(finalTarget), [rel(finalTarget)]: a.hookText(a.returns, a.sharedModule) },
           report: a.report };
}

// The same statements in two variants of a component (identical as TypeScript prints them) become one
// hook: written from the first copy, returning what either variant still uses; each variant calls it.
function shareHook() {
  const [fa, fb] = input.files;
  const target = abs(input.into);
  const existing = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : null;
  const { checker, sources: [A, B] } = program([fa, fb]);
  const fnB = input.function_b || input.function;
  // Each variant may have to keep some statements back (what's around them differs): share what both
  // can move, shrinking the set until both move the same statements.
  let names = [...input.names];
  let a, b;
  const movedNames = (x) => new Set(x.movedList.flatMap((st) => declaredBy(st).map((id) => id.text)));
  for (let round = 0; round < 10; round++) {
    a = analyze(A, checker, fa, input.function, names, target, input.name);
    b = analyze(B, checker, fb, fnB, names, target, input.name);
    const ma = movedNames(a), mb = movedNames(b);
    // A name counts only when the statement declaring it is the same in both (one destructure can
    // declare several names, and the variants' may differ).
    const stmtText = (x, sf) => {
      const m = new Map();
      for (const st of x.movedList) for (const id of declaredBy(st)) m.set(id.text, normalized(st, sf));
      return m;
    };
    const ta = stmtText(a, A), tb = stmtText(b, B);
    const both = names.filter((n) => ma.has(n) && mb.has(n) && ta.get(n) === tb.get(n));
    if (both.length === names.length) break;
    if (!both.length) throw new Error('none of these statements can move in both variants');
    names = both;
  }
  const key = (list, sf) => list.map((s) => normalized(s, sf));
  const ka = key(a.movedList, A), kb = key(b.movedList, B);
  if (ka.length !== kb.length || ka.some((x, i) => x !== kb[i])) {
    const firstDiff = a.movedList.find((s, i) => ka[i] !== kb[i]) || a.movedList[0];
    throw new Error(`the statements aren't the same in both variants (first difference near \`${(declaredBy(firstDiff)[0] || {}).text || '?'}\`); make them identical first`);
  }
  const helpersB = new Map(b.moduleMoved.map((s) => [declaredBy(s).map((id) => id.text).join(','), normalized(s, B)]));
  for (const s of a.moduleMoved) {
    const k = declaredBy(s).map((id) => id.text).join(',');
    if (helpersB.has(k) && helpersB.get(k) !== normalized(s, A)) throw new Error(`the helper \`${k}\` differs between the variants`);
    if (!helpersB.has(k)) throw new Error(`the helper \`${k}\` exists in only one variant`);
  }
  const pa = a.paramList.map((p) => p.name).sort().join(','), pb = b.paramList.map((p) => p.name).sort().join(',');
  if (pa !== pb) throw new Error(`the hook would take different parameters in the two variants (${pa || 'none'} vs ${pb || 'none'})`);
  const returns = [...a.returns, ...b.returns.filter((r) => !a.returns.includes(r))];
  const exportNames = [...new Set([...a.sharedModule, ...b.sharedModule])];
  const finalTarget = existing === null ? withJsxExtension(target, a.hasJsxMoved, a.notes) : target;
  let hookFile = a.hookText(returns, exportNames);
  if (existing !== null) {  // add to the shared file: its imports merged, the helpers and the hook after its code
    const lines = hookFile.split('\n');
    const imports = lines.filter((l) => l.startsWith('import ') && !l.includes(`"${importPath(target, target)}"`));
    const rest = lines.filter((l) => !l.startsWith('import ')).join('\n').replace(/^\n+/, '');
    hookFile = mergeImports(existing.replace(/\n*$/, '\n\n') + rest, imports, target);
  }
  return {
    files: { [fa]: a.fileText(finalTarget), [fb]: b.fileText(finalTarget), [rel(finalTarget)]: hookFile },
    report: { ...a.report, returns, returns_a: a.returns, returns_b: b.returns, helpers_shared: exportNames,
              names, left: input.names.filter((n) => !names.includes(n)) },
  };
}

// ---------------------------------------------------------------- compare and share

function declarations(sf) {
  const list = [];
  const visit = (n, depth, parent) => {
    let name = null;
    if ((ts.isFunctionDeclaration(n) || ts.isMethodDeclaration(n) || ts.isClassDeclaration(n)
         || ts.isInterfaceDeclaration(n) || ts.isTypeAliasDeclaration(n) || ts.isEnumDeclaration(n)) && n.name) name = n.name.getText(sf);
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name) && n.initializer) name = n.name.getText(sf);
    if (name) {
      const stmt = ts.isVariableDeclaration(n) ? n.parent.parent : n;
      list.push({ name, depth, parent, node: n, stmt, lo: lineOf(sf, stmt.getStart(sf)), hi: lineOf(sf, stmt.end),
                  flat: normalized(stmt, sf) });
    }
    ts.forEachChild(n, (c) => visit(c, depth + (name ? 1 : 0), name || parent));
  };
  visit(sf, 0, null);
  return list;
}

// A declaration as TypeScript prints it, without spacing: two copies that differ only in formatting
// (semicolons, line breaks, quotes' spacing) print the same.
const printer = ts.createPrinter({ removeComments: true });
function normalized(node, sf) {
  try {
    return printer.printNode(ts.EmitHint.Unspecified, node, sf).replace(/\s+/g, '');
  } catch (e) {
    return node.getText(sf).replace(/\s+/g, '');
  }
}

function similarity(a, b) {
  if (a === b) return 1;
  // Dice coefficient on character trigrams: fast and close enough to rank declarations.
  const grams = (s) => { const m = new Map(); for (let i = 0; i < s.length - 2; i++) { const g = s.slice(i, i + 3); m.set(g, (m.get(g) || 0) + 1); } return m; };
  const ga = grams(a), gb = grams(b);
  let common = 0;
  for (const [g, n] of ga) common += Math.min(n, gb.get(g) || 0);
  return (2 * common) / Math.max(1, a.length - 2 + b.length - 2);
}

function lineDiff(a, b, limit = 30) {
  const A = a.split('\n').map((l) => l.trim()).filter(Boolean), B = b.split('\n').map((l) => l.trim()).filter(Boolean);
  const inB = new Set(B), inA = new Set(A);
  const out = [...A.filter((l) => !inB.has(l)).map((l) => `- ${l}`), ...B.filter((l) => !inA.has(l)).map((l) => `+ ${l}`)];
  return out.slice(0, limit).join('\n') + (out.length > limit ? `\n… ${out.length - limit} more` : '');
}

function compare() {
  const { sources: [A, B] } = program([input.a, input.b]);
  const key = (d) => `${d.parent || ''}/${d.name}`;
  const da = new Map(declarations(A).filter((d) => d.depth <= 2).map((d) => [key(d), d]));
  const db = new Map(declarations(B).filter((d) => d.depth <= 2).map((d) => [key(d), d]));
  const pairs = [];
  for (const [k, a] of da) {
    const b = db.get(k);
    if (!b) { pairs.push({ name: a.name, parent: a.parent, status: 'only_a', a: [a.lo, a.hi] }); continue; }
    const r = similarity(a.flat, b.flat);
    const status = r === 1 ? 'identical' : r >= 0.85 ? 'near' : 'different';
    const mutable = ts.isVariableStatement(a.stmt) && !(a.stmt.declarationList.flags & ts.NodeFlags.Const);
    const p = { name: a.name, parent: a.parent, depth: a.depth, status, similarity: Math.round(r * 100) / 100, a: [a.lo, a.hi], b: [b.lo, b.hi], mutable,
                module_level: a.depth === 0 && b.depth === 0 };
    if (status === 'near') p.diff = lineDiff(a.stmt.getText(A), b.stmt.getText(B));
    pairs.push(p);
  }
  for (const [k, b] of db) if (!da.has(k)) pairs.push({ name: b.name, parent: b.parent, status: 'only_b', b: [b.lo, b.hi] });
  return { a: input.a, b: input.b, lines: [A.getLineAndCharacterOfPosition(A.end).line + 1, B.getLineAndCharacterOfPosition(B.end).line + 1], pairs };
}

function share() {
  const [fa, fb] = input.files;
  const into = abs(input.into);
  const { checker, prog, sources: [A, B] } = program([fa, fb]);
  const existing = fs.existsSync(into) ? fs.readFileSync(into, 'utf8') : '';
  const topDecl = (sf, name) => sf.statements.find((s) => declaredBy(s).some((id) => id.text === name));
  const resolved = (sf, id) => {
    // Where an imported name really comes from, so "../../utils" in one file and "../utils" in the
    // other count as the same import.
    for (const stmt of sf.statements) {
      if (!ts.isImportDeclaration(stmt)) continue;
      const r = ts.resolveModuleName(stmt.moduleSpecifier.text, sf.fileName, prog.getCompilerOptions(), ts.sys);
      if (stmt.importClause && within(id, stmt.importClause)) return `${r.resolvedModule ? r.resolvedModule.resolvedFileName : stmt.moduleSpecifier.text}#${id.text}`;
    }
    return null;
  };
  const moving = new Set(input.names);
  const skipped = [];
  // Close over module-level helpers both copies need; they must be identical too.
  for (let changed = true; changed;) {
    changed = false;
    for (const name of [...moving]) {
      const sa = topDecl(A, name), sb = topDecl(B, name);
      // Module state is fine to move when everything that assigns it moves too.
      const assignedOutside = (sf, st) => ts.isVariableStatement(st) && !(st.declarationList.flags & ts.NodeFlags.Const)
        && declaredBy(st).some((id) => {
          const sym = checker.getSymbolAtLocation(id);
          return sf.statements.some((other) => other !== st && !declaredBy(other).some((d) => moving.has(d.text))
            && references(checker, other).some(({ node, sym: s2 }) => s2 === sym && isAssigned(node)));
        });
      const why = !sa || !sb ? 'not a module-level declaration in both files'
        : assignedOutside(A, sa) || assignedOutside(B, sb) ? 'module state the screen assigns to (an import can\'t be assigned)'
        : normalized(sa, A) !== normalized(sb, B) ? 'not identical in both files'
        : isExported(sa) || isExported(sb) ? 'exported from the screen; other files may import it from there' : null;
      if (why) { moving.delete(name); skipped.push({ name, why }); changed = true; continue; }
      for (const { sym } of references(checker, sa)) {
        const d = sym.declarations && sym.declarations[0];
        const top = d && d.getSourceFile() === A ? topStatement(A, d) : null;
        if (top && top !== sa && !ts.isImportDeclaration(top)) {
          for (const id of declaredBy(top)) if (!moving.has(id.text) && !skipped.some((s) => s.name === id.text)) { moving.add(id.text); changed = true; }
        }
      }
    }
  }
  // A name that depends on a skipped one can't move either.
  for (let changed = true; changed;) {
    changed = false;
    for (const name of [...moving]) {
      const sa = topDecl(A, name);
      const deps = references(checker, sa).map(({ sym }) => sym.declarations && sym.declarations[0])
        .filter((d) => d && d.getSourceFile() === A).map((d) => topStatement(A, d)).filter((t) => t && t !== sa && !ts.isImportDeclaration(t));
      if (deps.some((t) => declaredBy(t).some((id) => !moving.has(id.text)))) {
        moving.delete(name); skipped.push({ name, why: 'needs a declaration that can\'t be shared' }); changed = true;
      }
    }
  }
  if (!moving.size) return { files: {}, report: { shared: [], skipped } };
  const files = {};
  const movedA = A.statements.filter((s) => declaredBy(s).some((id) => moving.has(id.text)));
  const movedB = B.statements.filter((s) => declaredBy(s).some((id) => moving.has(id.text)));
  // Imports for the shared file come from the first copy; each must resolve the same in the other.
  const impA = importBindings(A, checker), impB = importBindings(B, checker);
  const used = new Set();
  for (const s of movedA) for (const { sym } of references(checker, s)) if (impA.has(sym)) used.add(impA.get(sym));
  for (const s of movedB) {
    for (const { node, sym } of references(checker, s)) {
      if (!impB.has(sym)) continue;
      const other = [...used].find((id) => id.text === node.text);
      if (other && resolved(A, other) !== resolved(B, impB.get(sym))) throw new Error(`\`${node.text}\` comes from different modules in the two files`);
    }
  }
  const chunk = (sf, s) => {
    const t = sf.text.slice(fullStart(sf, s), lineEnd(sf.text, s.end)).replace(/\n*$/, '\n');
    const at = s.getStart(sf) - fullStart(sf, s);
    return isExported(s) ? t : t.slice(0, at) + 'export ' + t.slice(at);
  };
  const header = importsFor(A, used, abs(fa), into).filter((l) => !existing.includes(l));
  files[rel(into)] = (header.length ? header.join('\n') + '\n\n' : '') + (existing ? existing.replace(/\n*$/, '\n\n') : '')
    + movedA.map((s) => chunk(A, s)).join('\n');
  for (const [sf, file, movedHere] of [[A, fa, movedA], [B, fb, movedB]]) {
    const edits = movedHere.map((s) => [fullStart(sf, s), lineEnd(sf.text, s.end), '']);
    const imp = importBindings(sf, checker);
    const removed = new Set(movedHere);
    const keptRefs = sf.statements.filter((s) => !removed.has(s) && !ts.isImportDeclaration(s)).flatMap((s) => references(checker, s));
    const stillUsed = new Set(keptRefs.filter(({ sym }) => imp.has(sym)).map(({ sym }) => imp.get(sym)));
    pruneImports(sf, stillUsed, edits);
    const needed = [...moving].filter((n) => keptRefs.some(({ node }) => node.text === n));
    files[file] = needed.length
      ? mergeImports(applyEdits(sf.text, edits), [`import { ${needed.join(', ')} } from "${importPath(abs(file), into)}"`], file)
      : applyEdits(sf.text, edits);
  }
  return { files, report: { shared: [...moving], skipped } };
}

// Near copies made identical: each named declaration in `into` is replaced, exactly, by the same
// declaration from `from` (the model chose which version both should use). Then `share` can take it.
function unify() {
  const { sources: [S, T] } = program([input.from, input.into]);
  const key = (d) => `${d.parent || ''}/${d.name}`;
  const ds = declarations(S).filter((d) => d.depth <= 1), dt = declarations(T).filter((d) => d.depth <= 1);
  const edits = [], done = [], missing = [];
  for (const name of input.names) {
    const t = dt.find((d) => d.name === name);
    const src = t && ds.find((d) => key(d) === key(t));
    if (!t || !src) { missing.push(name); continue; }
    if (src.flat === t.flat) continue;
    let text = S.text.slice(src.stmt.getStart(S), src.stmt.end);
    if (isExported(t.stmt) && !isExported(src.stmt)) text = 'export ' + text;  // keep the target's export
    if (!isExported(t.stmt) && isExported(src.stmt)) text = text.replace(/^export\s+/, '');
    const ind = (sf, st) => sf.text.slice(lineStart(sf.text, st.getStart(sf)), st.getStart(sf));
    const from = ind(S, src.stmt), to = ind(T, t.stmt);
    const body = from === to ? text : text.split('\n').map((l, i) => (i && l.startsWith(from) ? to + l.slice(from.length) : l)).join('\n');
    edits.push([t.stmt.getStart(T), t.stmt.end, body]);
    done.push(name);
  }
  if (!done.length) throw new Error(missing.length ? `not found in both files: ${missing.join(', ')}` : 'already identical');
  return { files: { [input.into]: applyEdits(T.text, edits) }, report: { unified: done, missing } };
}

// Top-level declarations moved into another file (a util), exported there; the source file and every
// importer import them from it. Private helpers they need come along.
function relocate() {
  const file = abs(input.file), into = abs(input.into);
  const importers = (input.importers || []).filter((f) => abs(f) !== file && abs(f) !== into);
  const { checker, prog, sources } = program([input.file, ...importers]);
  const sf = sources[0];
  const others = sources.slice(1);
  const top = (name) => sf.statements.find((s) => declaredBy(s).some((id) => id.text === name));
  const moving = new Set();
  const missing = input.names.filter((n) => !top(n));
  if (missing.length) throw new Error(`not top-level declarations in ${input.file}: ${missing.join(', ')}`);
  const queue = input.names.map(top);
  while (queue.length) {  // what they need from this file comes along
    const st = queue.pop();
    if (moving.has(st)) continue;
    moving.add(st);
    for (const { sym } of references(checker, st)) {
      const d = sym.declarations && sym.declarations[0];
      if (!d || d.getSourceFile() !== sf) continue;
      const t = topStatement(sf, d);
      if (t && t !== st && !ts.isImportDeclaration(t) && !moving.has(t)) queue.push(t);
    }
  }
  const movedList = sf.statements.filter((s) => moving.has(s));
  const names = movedList.flatMap((s) => declaredBy(s).map((id) => id.text));
  const imports = importBindings(sf, checker);
  const used = new Set();
  for (const st of movedList) for (const { sym } of references(checker, st)) if (imports.has(sym)) used.add(imports.get(sym));
  const kept = sf.statements.filter((s) => !moving.has(s) && !ts.isImportDeclaration(s));
  const keptRefs = kept.flatMap((s) => references(checker, s));
  for (const { node, sym } of keptRefs) {
    const d = sym.declarations && sym.declarations[0];
    if (d && movedList.some((m) => within(d, m)) && isAssigned(node)) throw new Error(`\`${node.text}\` is assigned in ${input.file}; it can't move`);
  }
  const stillHere = [...new Set(keptRefs.filter(({ sym }) => { const d = sym.declarations && sym.declarations[0];
    return d && movedList.some((m) => declaredBy(m).some((id) => checker.getSymbolAtLocation(id) === sym)); }).map(({ sym }) => sym.name))];
  const stillUsed = new Set(keptRefs.filter(({ sym }) => imports.has(sym)).map(({ sym }) => imports.get(sym)));
  // the util: imports merged, the declarations exported
  const chunk = (s) => {
    const t = sf.text.slice(fullStart(sf, s), lineEnd(sf.text, s.end)).replace(/\n*$/, '\n');
    if (isExported(s)) return t;
    const at = s.getStart(sf) - fullStart(sf, s);
    return t.slice(0, at) + 'export ' + t.slice(at);
  };
  const existing = fs.existsSync(into) ? fs.readFileSync(into, 'utf8') : '';
  const body = (existing ? existing.replace(/\n*$/, '\n\n') : '') + movedList.map(chunk).join('\n');
  const files = { [rel(into)]: mergeImports(body, importsFor(sf, used, file, into), into) };
  // the source file
  const edits = movedList.map((s) => [fullStart(sf, s), lineEnd(sf.text, s.end), '']);
  pruneImports(sf, stillUsed, edits);
  let srcText = applyEdits(sf.text, edits);
  if (stillHere.length) srcText = mergeImports(srcText, [`import { ${stillHere.join(', ')} } from "${importPath(file, into)}"`], file);
  files[input.file] = srcText;
  // every importer: the moved names now come from the util
  const left = [], touched = [];
  const resolvesTo = (spec, from) => {
    const r = ts.resolveModuleName(spec, from, prog.getCompilerOptions(), ts.sys);
    return r.resolvedModule && path.resolve(r.resolvedModule.resolvedFileName) === file;
  };
  for (const o of others) {
    const oEdits = [], add = [];
    for (const st of o.statements) {
      if (!(ts.isImportDeclaration(st) || ts.isExportDeclaration(st)) || !st.moduleSpecifier) continue;
      if (!resolvesTo(st.moduleSpecifier.text, o.fileName)) continue;
      const clause = ts.isImportDeclaration(st) ? st.importClause : null;
      const nb = clause ? clause.namedBindings : st.exportClause;
      if (nb && ts.isNamespaceImport(nb)) { left.push(`${rel(o.fileName)} imports the whole module as ${nb.name.text}`); continue; }
      if (!nb || !(ts.isNamedImports(nb) || ts.isNamedExports(nb))) continue;
      const els = nb.elements;
      const mine = els.filter((e) => names.includes((e.propertyName || e.name).text));
      if (!mine.length) continue;
      const rest = els.filter((e) => !mine.includes(e));
      const kw = ts.isImportDeclaration(st) ? 'import' : 'export';
      const typeOnly = (clause && clause.isTypeOnly) || st.isTypeOnly ? ' type' : '';
      const def = clause && clause.name ? clause.name.text : '';
      if (!rest.length && !def) oEdits.push([fullStart(o, st), lineEnd(o.text, st.end), '']);
      else oEdits.push([st.getStart(o), st.end, `${kw}${typeOnly} ${[def, rest.length ? `{ ${rest.map((e) => e.getText(o)).join(', ')} }` : ''].filter(Boolean).join(', ')} from ${st.moduleSpecifier.getText(o)}`]);
      const line = `${kw}${typeOnly} { ${mine.map((e) => e.getText(o)).join(', ')} } from "${importPath(o.fileName, into)}"`;
      if (kw === 'export') oEdits.push([lineEnd(o.text, st.end), lineEnd(o.text, st.end), line + '\n']);
      else add.push(line);
    }
    if (!oEdits.length && !add.length) continue;
    files[rel(o.fileName)] = add.length ? mergeImports(applyEdits(o.text, oEdits), add, o.fileName) : applyEdits(o.text, oEdits);
    touched.push(rel(o.fileName));
  }
  return { files, report: { moved: names, importers_updated: touched, still_used_here: stillHere, left } };
}

// Whether two versions of a small pure function behave the same: both are compiled and run in a sandbox
// on sample inputs picked from their parameter types. null when it can't tell (not a self-contained
// function, or it uses time, randomness or anything outside itself).
const SAFE_GLOBALS = new Set(['Number', 'String', 'Boolean', 'Math', 'JSON', 'parseInt', 'parseFloat', 'isNaN',
  'isFinite', 'Array', 'Object', 'undefined', 'NaN', 'Infinity', 'RegExp', 'Error', 'TypeError', 'console']);
const SAMPLES = {
  string: ['', '0', '00:00:00', '01:02:03', '1:2', '12:34', '99:99:99', 'abc', ' 1:02:03 ', '1:2:3:4', '-1:05'],
  number: [0, 1, -1, 2.5, 59, 61, 3600000, 90061000, NaN, Infinity],
  boolean: [true, false],
};
function behaviour(checker, sfA, a, sfB, b) {
  const fnOf = (st) => {
    if (ts.isFunctionDeclaration(st)) return st;
    if (!ts.isVariableStatement(st) || st.declarationList.declarations.length !== 1) return null;
    const init = st.declarationList.declarations[0].initializer;
    return init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) ? init : null;
  };
  const fa = fnOf(a), fb = fnOf(b);
  if (!fa || !fb || fa.parameters.length !== fb.parameters.length || fa.parameters.length > 3) return null;
  const selfContained = (sf, st, fn) => references(checker, st).every(({ node, sym }) => {
    const d = sym.declarations && sym.declarations[0];
    if (d && d.getSourceFile() === sf && within(d, st)) return true;  // its own params and locals
    if (d && d.getSourceFile() === sf && declaredBy(st).some((id) => id === d.name)) return true;
    return SAFE_GLOBALS.has(node.text) && !(d && d.getSourceFile() === sf);
  });
  if (!selfContained(sfA, a, fa) || !selfContained(sfB, b, fb)) return null;
  if (/\bDate\b|Math\.random|fetch\(|setTimeout|await\b/.test(a.getText(sfA) + b.getText(sfB))) return null;
  const vm = require('vm');
  const compile = (sf, st, fn) => {
    const js = ts.transpileModule(`(${fn.getText(sf).replace(/^export\s+(default\s+)?/, '')})`, { compilerOptions: { target: ts.ScriptTarget.ES2019 } }).outputText;
    return vm.runInNewContext(js, { Number, String, Boolean, Math, JSON, parseInt, parseFloat, isNaN, isFinite, Array,
                                    Object, RegExp, Error, TypeError, console: { log() {}, warn() {}, error() {} } }, { timeout: 200 });
  };
  let A, B;
  try { A = compile(sfA, a, fa); B = compile(sfB, b, fb); } catch (e) { return null; }
  const kinds = fa.parameters.map((p) => {
    const t = p.type ? p.type.getText(sfA) : '';
    if (/^string/.test(t)) return [...SAMPLES.string, undefined];
    if (/^number/.test(t)) return [...SAMPLES.number, undefined];
    if (/^boolean/.test(t)) return SAMPLES.boolean;
    return [...SAMPLES.string.slice(0, 5), ...SAMPLES.number.slice(0, 5), null, undefined];
  });
  const inputs = kinds.reduce((acc, xs) => acc.flatMap((prev) => xs.map((x) => [...prev, x])), [[]]).slice(0, 120);
  const run = (f, args) => { try { const v = f(...args); return JSON.stringify(v === undefined ? '<undefined>' : v); } catch (e) { return `<throws ${e && e.name}>`; } };
  for (const args of inputs) {
    const ra = run(A, args), rb = run(B, args);
    if (ra !== rb) return { same: false, tried: inputs.length, input: JSON.stringify(args).slice(1, -1), a: ra, b: rb };
  }
  return { same: true, tried: inputs.length };
}

// ---------------------------------------------------------------- find_copies

const isFnExpr = (n) => !!n && (ts.isArrowFunction(n) || ts.isFunctionExpression(n));
const bare = (sf, st) => normalized(st, sf).replace(/^export/, '');

// The functions a statement declares: `function f() {}` or `const f = () => {}`.
function functionsIn(st) {
  if (ts.isFunctionDeclaration(st)) return st.name && st.body ? [{ name: st.name.text, fn: st, stmt: st }] : [];
  if (!ts.isVariableStatement(st)) return [];
  return st.declarationList.declarations.filter((d) => ts.isIdentifier(d.name) && isFnExpr(d.initializer))
    .map((d) => ({ name: d.name.text, fn: d.initializer, stmt: st }));
}

// The function bodies of a top-level statement that may hold local functions: a component, possibly
// wrapped (`memo(() => {...})`).
function bodiesOf(st) {
  const fns = functionsIn(st).map((f) => f.fn);
  if (ts.isVariableStatement(st)) {
    for (const d of st.declarationList.declarations) if (d.initializer && ts.isCallExpression(d.initializer)) fns.push(...d.initializer.arguments.filter(isFnExpr));
  }
  return fns.filter((f) => f.body && ts.isBlock(f.body));
}

const realParams = (fn) => fn.parameters.filter((p) => !(ts.isIdentifier(p.name) && p.name.text === 'this'));
const intrinsicTag = (n) => /^[a-z]/.test(n.text) && (ts.isJsxOpeningElement(n.parent) || ts.isJsxClosingElement(n.parent) || ts.isJsxSelfClosingElement(n.parent)) && n.parent.tagName === n;

// What a function is made of: its size, the names it declares and the ones it uses from outside.
function profile(fn, own) {
  const declared = new Set(), used = new Set();
  let nodes = 0;
  const visit = (n) => {
    if (n.kind === ts.SyntaxKind.ExportKeyword || n.kind === ts.SyntaxKind.DefaultKeyword) return;
    nodes++;
    if (ts.isParameter(n) || ts.isVariableDeclaration(n)) bindingNames(n.name).forEach((id) => declared.add(id.text));
    else if ((ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n) || ts.isClassDeclaration(n) || ts.isClassExpression(n)
              || ts.isTypeParameterDeclaration(n)) && n.name) declared.add(n.name.text);
    else if (ts.isIdentifier(n) && !isNonReference(n) && !intrinsicTag(n)) used.add(n.text);
    ts.forEachChild(n, visit);
  };
  visit(fn);
  const free = new Set([...used].filter((u) => !declared.has(u) && u !== own));
  const params = realParams(fn).flatMap((p) => bindingNames(p.name).map((id) => id.text));
  return { nodes, declared, free, params: params.join(','), arity: realParams(fn).length };
}

// The function's text with its parameters and its own name renamed to fixed placeholders, printed
// without spacing: copies that differ in those names alone print the same.
function canon(sf, fn, own) {
  const map = new Map();
  for (const p of realParams(fn)) for (const id of bindingNames(p.name)) map.set(id.text, `$${map.size}`);
  if (own && !map.has(own)) map.set(own, '$self');
  const edits = [];
  const visit = (n) => {
    if (ts.isIdentifier(n) && map.has(n.text) && !intrinsicTag(n)) {
      const p = n.parent, to = map.get(n.text);
      const keyed = (ts.isShorthandPropertyAssignment(p) || (ts.isBindingElement(p) && !p.propertyName)) && p.name === n;
      if (keyed) edits.push([n.getStart(sf), n.end, `${n.text}: ${to}`]);
      else if (!isNonReference(n)) edits.push([n.getStart(sf), n.end, to]);
    }
    ts.forEachChild(n, visit);
  };
  visit(fn);
  const at = fn.getStart(sf);
  const text = applyEdits(fn.getText(sf), edits.map(([s, e, r]) => [s - at, e - at, r]));
  const decl = ts.isFunctionDeclaration(fn);
  const parsed = ts.createSourceFile(sf.fileName, decl ? text : `(${text})`, ts.ScriptTarget.Latest, true);
  return (parsed.statements.length ? normalized(parsed.statements[0], parsed) : text.replace(/\s+/g, '')).replace(/^export(default)?(?=function|async)/, '');
}

function resolvedModule(env, sf, spec) {
  env.cache = env.cache || new Map();
  const key = `${path.dirname(sf.fileName)}|${spec}`;
  if (!env.cache.has(key)) {
    const r = ts.resolveModuleName(spec, sf.fileName, env.options, ts.sys).resolvedModule;
    env.cache.set(key, r ? norm(r.resolvedFileName) : spec.startsWith('.') ? norm(path.resolve(path.dirname(sf.fileName), spec)) : spec);
  }
  return env.cache.get(key);
}

// What each top-level name of a file is: an import (from where, under which name) or a declaration (its text).
const topScopes = new WeakMap();
function topScope(env, sf) {
  if (topScopes.has(sf)) return topScopes.get(sf);
  const out = new Map();
  for (const st of sf.statements) {
    if (ts.isImportDeclaration(st) && st.importClause) {
      const c = st.importClause, from = resolvedModule(env, sf, st.moduleSpecifier.text), nb = c.namedBindings;
      if (c.name) out.set(c.name.text, `import ${from} default`);
      if (nb && ts.isNamespaceImport(nb)) out.set(nb.name.text, `import ${from} *`);
      if (nb && ts.isNamedImports(nb)) for (const e of nb.elements) out.set(e.name.text, `import ${from} ${(e.propertyName || e.name).text}`);
    } else {
      for (const id of declaredBy(st)) out.set(id.text, `declaration ${bare(sf, st)}`);
    }
  }
  topScopes.set(sf, out);
  return out;
}

// The names two functions of the same text use that are not the same thing in their two files
// (another import, another declaration, a variable of the component around one of them).
function differingFree(env, a, b) {
  const names = new Set([...a.prof.free, ...b.prof.free]);
  const identity = (side, n) => (side.closure && side.closure.has(n) ? `closure ${side.sf.fileName} ${n}` : topScope(env, side.sf).get(n) || `global ${n}`);
  return [...names].filter((n) => a.prof.free.has(n) !== b.prof.free.has(n) || identity(a, n) !== identity(b, n));
}
const sameFree = (env, a, b) => !differingFree(env, a, b).length;

const typeKind = (p) => (p.type && /^(string|number|boolean)\b/.exec(p.type.getText()) || [''])[0];

// Whether a pair may be run against each other: small, pure-looking, same arity and parameter kinds.
function behavable(a, b) {
  const plain = (c) => c.prof.arity >= 1 && c.prof.arity <= 3 && !c.fn.asteriskToken && !(c.fn.modifiers || []).some((m) => m.kind === ts.SyntaxKind.AsyncKeyword)
    && [...c.prof.free].every((n) => SAFE_GLOBALS.has(n));
  if (!plain(a) || !plain(b)) return false;
  const ratio = a.prof.nodes / b.prof.nodes;
  if (ratio < 0.25 || ratio > 4) return false;
  const ka = realParams(a.fn).map(typeKind), kb = realParams(b.fn).map(typeKind);
  return ka.every((k, i) => !k || !kb[i] || k === kb[i]);
}

function stmtAt(sf, pos) {
  let found = null;
  const visit = (n) => {
    if (found) return;
    if ((ts.isFunctionDeclaration(n) || ts.isVariableStatement(n)) && n.getStart(sf) === pos) found = n;
    else ts.forEachChild(n, visit);
  };
  visit(sf);
  return found;
}

const sourceFiles = (list) => list.filter((f) => /\.(tsx?|jsx?)$/.test(f) && !f.endsWith('.d.ts') && !f.split('/').includes('node_modules'));

// The functions in other files that are the exported functions of a shared file under other names: the same
// text once parameter names and the function's own name are set aside ("identical"), or behaving the same
// on sample inputs when both are small self-contained functions ("same behaviour"). `near` are the ones
// with the same name or the same parameters but a body that differs. Nothing is changed.
function findCopies() {
  const home = norm(abs(input.file));
  const { options, files: projectFiles } = projectOf(home);
  const env = { options };
  const parse = (f) => ts.createSourceFile(f, fs.readFileSync(f, 'utf8'), ts.ScriptTarget.Latest, true);
  const H = parse(home);
  const isDefault = (st) => (st.modifiers || []).some((m) => m.kind === ts.SyntaxKind.DefaultKeyword);
  const exported = H.statements.filter((st) => isExported(st) && !isDefault(st)).flatMap(functionsIn);
  const wanted = input.names && input.names.length ? input.names : null;
  const picked = wanted ? wanted.map((n) => exported.find((f) => f.name === n) || (() => { throw new Error(`\`${n}\` isn't an exported function in ${input.file}`); })()) : exported;
  const shared = picked.map((f) => {
    const prof = profile(f.fn, f.name);
    return { ...f, sf: H, prof, canon: canon(H, f.fn, f.name), closure: null, line: lineOf(H, f.stmt.getStart(H)) };
  });
  const byArity = new Map();
  for (const s of shared) byArity.set(s.prof.arity, [...(byArity.get(s.prof.arity) || []), s]);
  const scan = sourceFiles((input.files ? input.files.map((f) => norm(abs(f))) : projectFiles).map(norm)).filter((f) => f !== home && fs.existsSync(f));
  const copies = [], inner = [], near = new Map(), queue = [];
  const entry = (s, c, extra) => ({ file: rel(c.sf.fileName), name: c.name, shared: s.name, line: c.line, ...(c.parent ? { parent: c.parent } : {}), ...extra });
  const judge = (s, c) => {
    const sameName = c.name === s.name, sameParams = s.prof.arity > 0 && c.prof.params === s.prof.params;
    let how = null, why = 'a different body';
    if (c.prof.nodes === s.prof.nodes && canon(c.sf, c.fn, c.name) === s.canon) {
      const diff = differingFree(env, s, c);
      if (!diff.length) how = 'identical';
      else why = `the same text, but ${diff.slice(0, 4).join(', ')} ${diff.length > 1 ? 'are' : 'is'} not the same thing there`;
    }
    if (how) return (c.parent ? inner : copies).push(entry(s, c, { how, exported: !!isExported(c.stmt) && !c.parent }));
    if (behavable(s, c)) queue.push({ s, c, near: sameName || sameParams });
    else if (sameName || sameParams) addNear(s, c, why, sameName);
  };
  const addNear = (s, c, why, sameName, extra = {}) => {
    const sim = similarity(s.canon, canon(c.sf, c.fn, c.name));
    if (!sameName && sim < 0.5) return;
    near.set(`${c.sf.fileName}:${c.line}:${s.name}`, entry(s, c, { why, similarity: Math.round(sim * 100) / 100, ...extra }));
  };
  const consider = (c) => {
    const options_ = byArity.get(realParams(c.fn).length);
    if (!options_) return;
    c.line = lineOf(c.sf, c.stmt.getStart(c.sf));
    c.prof = profile(c.fn, c.name);
    for (const s of options_) judge(s, c);
  };
  for (const f of scan) {
    const text = fs.readFileSync(f, 'utf8');
    if (!/=>|function/.test(text)) continue;
    const sf = ts.createSourceFile(f, text, ts.ScriptTarget.Latest, true);
    for (const st of sf.statements) {
      for (const c of functionsIn(st)) consider({ ...c, sf, parent: null, closure: null });
      for (const body of bodiesOf(st)) {
        const outer = profile(body), parent = (functionsIn(st)[0] || {}).name || (declaredBy(st)[0] || {}).text || '';
        for (const inside of body.body.statements) for (const c of functionsIn(inside)) consider({ ...c, sf, parent, closure: outer.declared });
      }
    }
  }
  const cap = 500;
  queue.sort((a, b) => b.near - a.near);
  const tried = queue.slice(0, cap);
  if (tried.length) {
    const roots = [home, ...new Set(tried.map((q) => q.c.sf.fileName))];
    const prog = ts.createProgram(roots, { allowJs: true, jsx: ts.JsxEmit.Preserve, noEmit: true, noResolve: true, types: [], skipLibCheck: true });
    const checker = prog.getTypeChecker(), HP = prog.getSourceFile(home);
    for (const { s, c, near: wasNear } of tried) {
      const sfP = prog.getSourceFile(c.sf.fileName);
      const a = stmtAt(HP, s.stmt.getStart(H)), b = sfP && stmtAt(sfP, c.stmt.getStart(c.sf));
      const r = a && b ? behaviour(checker, HP, a, sfP, b) : null;
      if (r && r.same) (c.parent ? inner : copies).push(entry(s, c, { how: 'same behaviour', tried: r.tried, exported: !!isExported(c.stmt) && !c.parent }));
      else if (wasNear) addNear(s, c, r ? `runs differently: (${r.input}) gives ${r.a} from the shared one, ${r.b} from this one` : 'a different body', c.name === s.name);
    }
  }
  const order = (x, y) => x.file.localeCompare(y.file) || x.line - y.line;
  const done = new Set([...copies, ...inner].map((e) => `${e.file}:${e.line}:${e.shared}`));
  const nearList = [...near.values()].filter((e) => !done.has(`${e.file}:${e.line}:${e.shared}`)).sort(order);
  return { files: {}, report: { copies: copies.sort(order), inner: inner.sort(order), near: nearList, shared: shared.map((s) => s.name),
                               scanned: scan.length, behaviour_checked: tried.length, behaviour_skipped: queue.length - tried.length } };
}

// The function a statement declares under `name` (`function f() {}` or `const f = () => {}`), if it is one.
function functionNamed(st, name) {
  return functionsIn(st).find((f) => f.name === name) || null;
}

const norm = (f) => path.resolve(f).split(path.sep).join('/');
function realNorm(f) {
  try { return norm(fs.realpathSync(f)); } catch (e) { return norm(f); }
}

// Whether a copy under another name is the shared function: the same text once parameters and the
// function's own name are renamed alike, and everything else it uses is the same thing in both files.
function renamedIdentical(env, H, orig, sf, copy, shared, local) {
  const a = functionNamed(orig, shared), b = functionNamed(copy, local);
  if (!a || !b) return false;
  const pa = profile(a.fn, shared), pb = profile(b.fn, local);
  return pa.nodes === pb.nodes && canon(H, a.fn, shared) === canon(sf, b.fn, local)
    && sameFree(env, { sf: H, name: shared, prof: pa }, { sf, name: local, prof: pb });
}

// A copy's name renamed to the shared one through TypeScript's own rename of that local symbol: the
// edits for the places outside the removed declaration, or why not (a clash, an export, a use elsewhere).
function renameCopy(ls, sf, copy, local, shared, H, orig) {
  const prog = ls.getProgram(), checker = prog.getTypeChecker();
  const id = declaredBy(copy).find((d) => d.text === local);
  const locs = ls.findRenameLocations(sf.fileName, id.getStart(sf), false, false, { providePrefixAndSuffixTextForRename: true });
  if (!locs || !locs.length) return { refused: `TypeScript found nothing to rename for ${local}` };
  const outside = locs.filter((l) => !(l.fileName === sf.fileName && l.textSpan.start >= copy.getStart(sf) && l.textSpan.start < copy.end));
  const other = outside.find((l) => l.fileName !== sf.fileName);
  if (other) return { refused: `${local} is also used in ${rel(other.fileName)}; not renaming` };
  const sharedImport = (d) => {
    if (!ts.isImportSpecifier(d) || (d.propertyName || d.name).text !== shared) return false;
    const sym = checker.getSymbolAtLocation(d.name), target = sym && checker.getAliasedSymbol(sym);
    return !!target && (target.declarations || []).some((x) => x.getSourceFile() === H);
  };
  const { at, inScope } = visibleNames(prog, checker, shared);
  const clashAt = (d) => (d && !sharedImport(d) ? `\`${shared}\` is already declared or imported at ${rel(d.getSourceFile().fileName)}:${lineOf(d.getSourceFile(), d.getStart())}` : null);
  const top = inScope(sf);
  let reason = clashAt(top);
  const lower = /^[a-z]/.test(shared);
  const edits = [];
  for (const l of outside) {
    const n = nodeAt(sf, l.textSpan.start), p = n.parent;
    if (!ts.isIdentifier(n)) continue;
    if (ts.isExportSpecifier(p)) return { refused: `${local} is exported from ${rel(sf.fileName)} by name; others may import it` };
    reason = reason || clashAt(isExternalName(n) ? null : at(n));
    if (lower && (ts.isJsxOpeningElement(p) || ts.isJsxClosingElement(p) || ts.isJsxSelfClosingElement(p)) && p.tagName === n)
      return { refused: `\`${shared}\` starts with a lowercase letter, so JSX would read <${shared}> as a built-in tag` };
    edits.push([l.textSpan.start, l.textSpan.start + l.textSpan.length, `${l.prefixText || ''}${shared}${l.suffixText || ''}`]);
  }
  return reason ? { refused: `${reason}, in scope where ${local} is used` } : { edits, imported: !!top };
}

// The program of the shared file and its copies, as a language service (renames need one).
function serviceProgram(files) {
  const names = files.map(norm);
  const { options } = projectOf(names[0]);
  const ls = service(names, options);
  const prog = ls.getProgram();
  const sources = names.map((n) => prog.getSourceFile(n));
  const missing = names.filter((n, i) => !sources[i]);
  if (missing.length) throw new Error(`TypeScript didn't load ${missing.map(rel).join(', ')} (excluded by its tsconfig?)`);
  return { ls, checker: prog.getTypeChecker(), sources, options };
}

// Copies of a function in other files replaced by an import of the one that stays: each copy that is a
// top-level declaration identical to the original (as TypeScript prints it, `export` aside) goes.
// "aliases" ({copy file: {local name: shared name}}) lets a copy under another name go too: the local
// name is renamed to the shared one in that file, or the copy is refused.
function dedupe() {
  const aliases = new Map(Object.entries(input.aliases || {}).map(([f, m]) => [realNorm(abs(f)), m]));
  const all = [input.file, ...input.copies];
  const env = aliases.size ? serviceProgram(all) : { ...program(all), options: { allowJs: true } };
  const { checker, sources } = env;
  const [H, ...C] = sources;
  const top = (sf, name) => sf.statements.find((s) => declaredBy(s).some((id) => id.text === name));
  const files = {}, replaced = [], differ = [], missing = [], equivalent = [], refused = [];
  const mustExport = (name) => {
    const orig = top(H, name);
    if (!orig) throw new Error(`\`${name}\` isn't a top-level declaration in ${input.file}`);
    if (!isExported(orig)) throw new Error(`\`${name}\` isn't exported from ${input.file}; export it (or relocate it there) first`);
  };
  input.names.forEach(mustExport);
  for (const m of aliases.values()) Object.values(m).forEach(mustExport);
  for (const sf of C) {
    const mine = aliases.get(realNorm(sf.fileName)) || {};
    const pairs = [...input.names.map((n) => [n, n]), ...Object.entries(mine).filter(([l, s]) => l !== s)];
    const edits = [], removed = [], imports = new Set();
    for (const [local, shared] of pairs) {
      const copy = top(sf, local), orig = top(H, shared);
      if (!copy) { if (!Object.values(mine).includes(local)) missing.push(`${rel(sf.fileName)}: ${local}`); continue; }
      const identical = local === shared ? bare(sf, copy) === bare(H, orig) : renamedIdentical(env, H, orig, sf, copy, shared, local);
      if (!identical) {
        const same = behaviour(checker, H, orig, sf, copy);
        if (!same || !same.same) {
          const why = !same ? '' : `\n  runs differently: (${same.input}) gives ${same.a} from the shared one, ${same.b} from the copy`;
          differ.push({ file: rel(sf.fileName), name: local, diff: lineDiff(orig.getText(H), copy.getText(sf), 14) + why });
          continue;
        }
        equivalent.push(`${rel(sf.fileName)}: ${local} (written differently, behaves the same on ${same.tried} sample inputs)`);
      }
      if (isExported(copy)) { differ.push({ file: rel(sf.fileName), name: local, diff: '(exported from there too; others may import it)' }); continue; }
      let imported = false;
      if (local !== shared) {
        const r = renameCopy(env.ls, sf, copy, local, shared, H, orig);
        if (r.refused) { refused.push({ file: rel(sf.fileName), name: local, shared, reason: r.refused }); continue; }
        edits.push(...r.edits);
        imported = r.imported;
      }
      edits.push([fullStart(sf, copy), lineEnd(sf.text, copy.end), '']);
      removed.push(copy);
      if (!imported) imports.add(shared);
    }
    if (!removed.length) continue;
    // imports only the removed copies used go too
    const imp = importBindings(sf, checker);
    const keptRefs = sf.statements.filter((st) => !removed.includes(st) && !ts.isImportDeclaration(st)).flatMap((st) => references(checker, st));
    pruneImports(sf, new Set(keptRefs.filter(({ sym }) => imp.has(sym)).map(({ sym }) => imp.get(sym))), edits);
    const lines = imports.size ? [`import { ${[...imports].join(', ')} } from "${importPath(sf.fileName, H.fileName)}"`] : [];
    files[rel(sf.fileName)] = mergeImports(applyEdits(sf.text, edits), lines, sf.fileName);
    replaced.push(`${rel(sf.fileName)}: ${removed.map((st) => declaredBy(st)[0].text).join(', ')}`);
  }
  return { files, report: { replaced, differ, missing, equivalent, refused } };
}

// A function or component gets a new parameter (or prop) everywhere: the definition (optional, or with a
// default, so existing callers keep working), and every call or JSX use when a value is given.
function signature() {
  const file = abs(input.file);
  const users = (input.importers || []).filter((f) => abs(f) !== file);
  const { checker, sources } = program([input.file, ...users]);
  const sf = sources[0];
  const p = input.param;  // {name, type, default?, value?}
  if (!p || !/^[A-Za-z_$][\w$]*$/.test(p.name || '')) throw new Error('param needs a name');
  for (const [k, v] of [['default', p.default], ['value', p.value], ['type', p.type]]) {  // never write code that doesn't parse
    if (v === undefined || v === null || v === '') continue;
    const probe = k === 'type' ? `let x: ${v}` : `let x = (${v})`;
    const t = ts.createSourceFile('probe.tsx', probe, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    if (t.parseDiagnostics && t.parseDiagnostics.length) throw new Error(`the ${k} \`${v}\` isn't valid TypeScript`);
  }
  let decl = null, fn = null;
  for (const st of sf.statements) {
    if (ts.isFunctionDeclaration(st) && st.name && st.name.text === input.name) { decl = st.name; fn = st; }
    if (ts.isVariableStatement(st)) for (const d of st.declarationList.declarations) {
      if (ts.isIdentifier(d.name) && d.name.text === input.name && d.initializer) {
        decl = d.name;
        const visit = (n) => { if (!fn && (ts.isArrowFunction(n) || ts.isFunctionExpression(n))) fn = n; else if (!fn) ts.forEachChild(n, visit); };
        visit(d.initializer);  // observer(function X(...)), memo(() => ...)
      }
    }
  }
  if (!decl || !fn) throw new Error(`no function or component named ${input.name} at the top level of ${input.file}`);
  const target = checker.getSymbolAtLocation(decl);
  const isTarget = (id) => {
    let sym = checker.getSymbolAtLocation(id);
    if (sym && sym.flags & ts.SymbolFlags.Alias) sym = checker.getAliasedSymbol(sym);
    return sym === target;
  };
  const files = {}, sites = [], skipped = [];
  const edits = new Map(sources.map((s) => [s, []]));
  const component = fn.parameters.length <= 1 && fn.parameters[0] && ts.isObjectBindingPattern(fn.parameters[0].name);
  const opt = p.default === undefined || p.default === null || p.default === '';
  if (component) {
    // the prop: in the props type, and in the destructure
    const par = fn.parameters[0];
    const member = `${p.name}${opt ? '?' : '?'}: ${p.type || 'unknown'}`;
    let typeNode = par.type;
    if (!typeNode) {
      let n = fn; while (n && !ts.isVariableDeclaration(n)) n = n.parent;
      const t = n && n.type;
      if (t && ts.isTypeReferenceNode(t) && t.typeArguments && t.typeArguments.length) typeNode = t.typeArguments[0];
    }
    if (typeNode && ts.isTypeLiteralNode(typeNode)) {
      edits.get(sf).push([typeNode.members.end, typeNode.members.end, `${typeNode.members.length ? '; ' : ' '}${member}`]);
    } else if (typeNode && ts.isTypeReferenceNode(typeNode)) {
      const tsym = checker.getSymbolAtLocation(typeNode.typeName);
      const td = tsym && tsym.declarations && tsym.declarations[0];
      if (td && td.getSourceFile() === sf && ts.isInterfaceDeclaration(td)) {
        edits.get(sf).push([td.members.end, td.members.end, `\n  ${member}`]);
      } else if (td && td.getSourceFile() === sf && ts.isTypeAliasDeclaration(td) && ts.isTypeLiteralNode(td.type)) {
        edits.get(sf).push([td.type.members.end, td.type.members.end, `\n  ${member}`]);
      } else skipped.push(`the props type ${typeNode.getText(sf)} isn't declared in ${input.file}; add \`${member}\` to it`);
    } else skipped.push('the component has no props type to extend');
    const els = par.name.elements;
    const add = opt ? p.name : `${p.name} = ${p.default}`;
    edits.get(sf).push([els.end, els.end, `${els.length ? ', ' : ''}${add}`]);
  } else {
    const params = fn.parameters;
    const text = `${p.name}${opt ? '?' : ''}${p.type ? `: ${p.type}` : ''}${opt ? '' : ` = ${p.default}`}`;
    edits.get(sf).push([params.end, params.end, `${params.length ? ', ' : ''}${text}`]);
  }
  // every use
  const index = component ? null : fn.parameters.length;  // the new parameter's position
  for (const o of sources) {
    const visit = (n) => {
      if (!component && ts.isCallExpression(n)) {
        const callee = ts.isPropertyAccessExpression(n.expression) ? n.expression.name : n.expression;
        if (ts.isIdentifier(callee) && isTarget(callee)) {
          sites.push(`${rel(o.fileName)}:${lineOf(o, n.getStart(o))}`);
          if (p.value !== undefined && p.value !== null && p.value !== '') {
            const have = n.arguments.length;
            if (have > index) skipped.push(`${rel(o.fileName)}:${lineOf(o, n.getStart(o))} already passes more arguments`);
            else {
              const fill = Array(index - have).fill('undefined');
              const args = [...fill, p.value].join(', ');
              edits.get(o).push([n.arguments.end, n.arguments.end, `${have ? ', ' : ''}${args}`]);
            }
          }
        }
      }
      if (component && (ts.isJsxSelfClosingElement(n) || ts.isJsxOpeningElement(n)) && ts.isIdentifier(n.tagName) && isTarget(n.tagName)) {
        sites.push(`${rel(o.fileName)}:${lineOf(o, n.getStart(o))}`);
        if (p.value !== undefined && p.value !== null && p.value !== '') {
          const has = n.attributes.properties.some((a) => ts.isJsxAttribute(a) && a.name.getText(o) === p.name);
          if (has) skipped.push(`${rel(o.fileName)}:${lineOf(o, n.getStart(o))} already sets ${p.name}`);
          else edits.get(o).push([n.attributes.end, n.attributes.end, ` ${p.name}={${p.value}}`]);
        }
      }
      ts.forEachChild(n, visit);
    };
    visit(o);
  }
  for (const [o, e] of edits) if (e.length) files[rel(o.fileName)] = applyEdits(o.text, e);
  return { files, report: { kind: component ? 'component' : 'function', sites, skipped } };
}

// A render helper inside a component (`const renderRow = (item) => <...>`) becomes a component in its own
// file: its parameters and the component's values it uses become props (context values like useStores()
// are re-derived inside), its body moves exactly, and every `renderRow(x)` becomes `<Row item={x} ... />`.
function component() {
  const file = abs(input.file), target = abs(input.into);
  if (fs.existsSync(target)) throw new Error(`${input.into} already exists`);
  if (!/^[A-Z][\w$]*$/.test(input.name || '')) throw new Error('the component name must start with a capital letter');
  const { checker, sources: [sf] } = program([input.file]);
  const text = sf.text;
  const fn = findFunction(sf, input.function);
  if (!fn) throw new Error(`no function or component named ${input.function} in ${input.file}`);
  const body = fn.body.statements;
  const helperStmt = body.find((st) => declaredBy(st).some((id) => id.text === input.helper));
  if (!helperStmt) throw new Error(`\`${input.helper}\` isn't declared directly inside ${input.function}`);
  let hfn = null;
  if (ts.isFunctionDeclaration(helperStmt)) hfn = helperStmt;
  else if (ts.isVariableStatement(helperStmt)) {
    const init = helperStmt.declarationList.declarations[0].initializer;
    if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init))) hfn = init;
  }
  if (!hfn) throw new Error(`\`${input.helper}\` isn't a function`);
  if (!hasJsx(hfn)) throw new Error(`\`${input.helper}\` doesn't render anything`);
  const helperSym = checker.getSymbolAtLocation(declaredBy(helperStmt)[0]);
  const hparams = hfn.parameters.map((p) => {
    if (!ts.isIdentifier(p.name)) throw new Error(`\`${input.helper}\` takes a destructured parameter; name it first`);
    return p;
  });
  // every use of the helper must be a call (it becomes an element)
  const calls = [];
  const visitUses = (n) => {
    if (ts.isIdentifier(n) && n !== declaredBy(helperStmt)[0] && checker.getSymbolAtLocation(n) === helperSym) {
      if (ts.isCallExpression(n.parent) && n.parent.expression === n) calls.push(n.parent);
      else throw new Error(`\`${input.helper}\` is used as a value at line ${lineOf(sf, n.getStart(sf))} (not called); it can't become an element there`);
    }
    ts.forEachChild(n, visitUses);
  };
  visitUses(sf);
  const declStmt = (d) => body.find((st) => within(d, st));
  const params = new Set(fn.parameters.flatMap((p) => bindingNames(p.name)).map((id) => checker.getSymbolAtLocation(id)));
  // what the helper uses from the component: props
  const free = new Map();
  for (const { sym } of references(checker, helperStmt)) {
    const d = sym.declarations && sym.declarations[0];
    if (!d || d.getSourceFile() !== sf || within(d, helperStmt) || !within(d, fn)) continue;
    if (sym === helperSym) continue;
    free.set(sym, d);
  }
  // context values re-derived inside instead of passed
  const STATEFUL = /^use(State|Ref|Reducer|Memo|Callback|Transition|Id|Optimistic|ActionState|SyncExternalStore)$/;
  const safe = (st, seen = new Set()) => {
    if (!st || seen.has(st) || !ts.isVariableStatement(st) || st.declarationList.declarations.length !== 1) return false;
    const d = st.declarationList.declarations[0];
    if (!d.initializer || hasJsx(st) || !(st.declarationList.flags & ts.NodeFlags.Const)) return false;
    seen.add(st);
    let ok = true;
    const v = (n) => {
      if (!ok) return;
      if (ts.isFunctionLike(n) || ts.isNewExpression(n) || ts.isAwaitExpression(n)) { ok = false; return; }
      if (ts.isCallExpression(n)) { const c = ts.isIdentifier(n.expression) ? n.expression.text : ''; if (!/^use[A-Z]/.test(c) || STATEFUL.test(c)) { ok = false; return; } }
      ts.forEachChild(n, v);
    };
    v(d.initializer);
    if (!ok) return false;
    for (const { sym } of references(checker, d.initializer)) {
      const dd = sym.declarations && sym.declarations[0];
      if (dd && dd.getSourceFile() === sf && within(dd, fn.body) && !safe(declStmt(dd), seen)) return false;
    }
    return true;
  };
  const prelude = new Map();  // statement -> names needed
  const need = (st, name) => {
    if (!prelude.has(st)) prelude.set(st, new Set());
    prelude.get(st).add(name);
    for (const { sym } of references(checker, st.declarationList.declarations[0].initializer)) {
      const dd = sym.declarations && sym.declarations[0];
      if (dd && dd.getSourceFile() === sf && within(dd, fn.body)) need(declStmt(dd), sym.name);
    }
  };
  for (const [sym, d] of [...free]) {
    const ds = declStmt(d);
    if (ds && safe(ds)) { need(ds, sym.name); free.delete(sym); }
  }
  // types for the props
  const imports = importBindings(sf, checker);
  const usedImports = new Set();
  const notes = [];
  const typeOf = (sym, d) => {
    if (d.type) { for (const { sym: s2 } of references(checker, d.type)) if (imports.has(s2)) usedImports.add(imports.get(s2)); return d.type.getText(sf); }
    const t = typeText(checker, d, fn, target);
    if (!/import\(/.test(t) && t.length <= 120) return t;
    notes.push(`\`${sym.name}\` is typed \`any\`: its inferred type is too long to write out`);
    return 'any';
  };
  const props = [
    ...hparams.map((p) => ({ name: p.name.text, type: p.type ? p.type.getText(sf) : typeText(checker, p, fn, target), optional: !!(p.questionToken || p.initializer) })),
    ...[...free].map(([sym, d]) => ({ name: sym.name, type: typeOf(sym, d), optional: false })),
  ];
  for (const p of hparams) if (p.type) for (const { sym: s2 } of references(checker, p.type)) if (imports.has(s2)) usedImports.add(imports.get(s2));
  for (const pr of props) if (/import\(/.test(pr.type) || pr.type.length > 120) { notes.push(`\`${pr.name}\` is typed \`any\``); pr.type = 'any'; }
  const importedNames = new Map([...imports.values()].map((id) => [id.text, id]));
  const importTypesOf = (t) => { for (const w of t.match(/[A-Za-z_$][\w$]*/g) || []) if (importedNames.has(w)) usedImports.add(importedNames.get(w)); };
  // module-level helpers it needs come along (exported back when the screen still uses them)
  const component_ = topStatement(sf, fn);
  const modNeeded = new Set();
  const moduleState = new Map();
  const queue = [...references(checker, helperStmt), ...[...prelude.keys()].flatMap((st) => references(checker, st))];
  while (queue.length) {
    const { sym } = queue.pop();
    const d = sym.declarations && sym.declarations[0];
    if (!d) continue;
    if (imports.has(sym)) { usedImports.add(imports.get(sym)); continue; }
    if (d.getSourceFile() !== sf || within(d, fn)) continue;
    const top = topStatement(sf, d);
    if (!top || top === component_ || modNeeded.has(top) || ts.isImportDeclaration(top)) continue;
    if (isExported(top)) continue;  // imported from the screen below
    if (ts.isVariableStatement(top) && !(top.declarationList.flags & ts.NodeFlags.Const)) {
      // module state: the screen may assign it, so it stays; the component gets its value as a prop
      if (!moduleState.has(sym)) moduleState.set(sym, d);
      continue;
    }
    modNeeded.add(top);
    queue.push(...references(checker, top));
  }
  for (const [sym, d] of moduleState) {
    const usedByMoved = [...modNeeded].some((st) => references(checker, st).some(({ sym: s2 }) => s2 === sym));
    if (usedByMoved) throw new Error(`\`${sym.name}\` is module state the screen keeps, and a helper that would move along reads it; untangle that first`);
    props.push({ name: sym.name, type: d.type ? d.type.getText(sf) : 'any', optional: false });
    free.set(sym, d);
    notes.push(`\`${sym.name}\` is module state in the screen; the component gets its value as a prop`);
  }
  for (const pr of props) importTypesOf(pr.type);  // types the props mention come from the screen's imports
  const exportedNeeded = new Set();
  for (const { sym } of [...references(checker, helperStmt)]) {
    const d = sym.declarations && sym.declarations[0];
    const top = d && d.getSourceFile() === sf ? topStatement(sf, d) : null;
    if (top && top !== component_ && isExported(top) && !within(d, fn)) for (const id of declaredBy(top)) exportedNeeded.add(id.text);
  }
  if (exportedNeeded.size) throw new Error(`the helper uses ${[...exportedNeeded].join(', ')}, exported from ${input.file}; move those to a util first`);
  const moduleMoved = sf.statements.filter((st) => modNeeded.has(st));
  const keptRefs = sf.statements.filter((st) => !modNeeded.has(st) && !ts.isImportDeclaration(st))
    .flatMap((st) => references(checker, st)).filter(({ node }) => !within(node, helperStmt));
  const movedNames = new Map();
  for (const st of moduleMoved) for (const id of declaredBy(st)) movedNames.set(checker.getSymbolAtLocation(id), id.text);
  const sharedBack = [...new Set(keptRefs.filter(({ sym }) => movedNames.has(sym)).map(({ sym }) => movedNames.get(sym)))];
  // the component's file
  const chunk = (st, exp) => {
    const t = text.slice(fullStart(sf, st), lineEnd(text, st.end)).replace(/\n*$/, '\n');
    if (!exp || isExported(st)) return t;
    const at = st.getStart(sf) - fullStart(sf, st);
    return t.slice(0, at) + 'export ' + t.slice(at);
  };
  const preludeText = body.filter((st) => prelude.has(st)).map((st) => {
    const d = st.declarationList.declarations[0], want = prelude.get(st);
    if (ts.isObjectBindingPattern(d.name)) {
      const keep = d.name.elements.filter((e) => bindingNames(e.name).some((id) => want.has(id.text)));
      return `  const { ${keep.map((e) => e.getText(sf)).join(', ')} } = ${d.initializer.getText(sf)}\n`;
    }
    return '  ' + st.getText(sf) + '\n';
  }).join('');
  const sig = props.length ? `{ ${props.map((p) => p.name).join(', ')} }: { ${props.map((p) => `${p.name}${p.optional ? '?' : ''}: ${p.type}`).join('; ')} }` : '';
  const hbody = hfn.body;
  const bodyText = ts.isBlock(hbody) ? hbody.getText(sf) : `{\n${preludeText}  return (\n    ${hbody.getText(sf)}\n  )\n}`;
  const finalBody = ts.isBlock(hbody) && preludeText ? `{\n${preludeText}${hbody.getText(sf).slice(1)}` : bodyText;
  const compText = [importsFor(sf, usedImports, file, target).join('\n'), ...moduleMoved.map((st) => chunk(st, sharedBack.includes(declaredBy(st)[0].text))),
                    `export const ${input.name} = (${sig}) => ${finalBody}\n`].filter(Boolean).join('\n');
  // the screen: calls become elements, the helper goes
  const edits = [[fullStart(sf, helperStmt), lineEnd(text, helperStmt.end), '']];
  for (const st of moduleMoved) edits.push([fullStart(sf, st), lineEnd(text, st.end), '']);
  for (const c of calls) {
    const attrs = [];
    hparams.forEach((p, i) => { if (c.arguments[i]) attrs.push(`${p.name.text}={${c.arguments[i].getText(sf)}}`); });
    for (const [sym] of free) attrs.push(`${sym.name}={${sym.name}}`);
    edits.push([c.getStart(sf), c.end, `<${input.name}${attrs.length ? ' ' + attrs.join(' ') : ''} />`]);
  }
  const imp = importBindings(sf, checker);
  const stillUsed = new Set(keptRefs.filter(({ sym }) => imp.has(sym)).map(({ sym }) => imp.get(sym)));
  pruneImports(sf, stillUsed, edits);
  const screen = mergeImports(applyEdits(text, edits), [`import { ${[input.name, ...sharedBack].join(', ')} } from "${importPath(file, target)}"`], file);
  // a key on the helper's root belongs on the element where it's used
  const root = ts.isBlock(hbody) ? null : hbody;
  const keyed = root && (ts.isJsxElement(root) ? root.openingElement : ts.isJsxSelfClosingElement(root) ? root : null);
  if (keyed && keyed.attributes.properties.some((a) => ts.isJsxAttribute(a) && a.name.getText(sf) === 'key'))
    notes.push('the helper set `key` on its root element; in a list, React needs the key on the new element where it is used');
  return { files: { [input.file]: screen, [rel(target)]: compText },
           report: { props: props.map((p) => p.name), prop_types: props.map((p) => `${p.name}${p.optional ? '?' : ''}: ${p.type}`), calls: calls.length, rederived: [...prelude.keys()].map((st) => declaredBy(st)[0] ? declaredBy(st)[0].text : '?'),
                     helpers_moved: moduleMoved.map((st) => declaredBy(st)[0].text), notes } };
}

// Does `name` in one text behave like `name` in another (e.g. the shared version now vs a copy as it was)?
function sameBehaviour() {
  const os = require('os');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'buckets-behaviour-'));
  try {
    const fa = path.join(dir, 'a.ts'), fb = path.join(dir, 'b.ts');
    fs.writeFileSync(fa, input.a_text); fs.writeFileSync(fb, input.b_text);
    const prog = ts.createProgram([fa, fb], { noEmit: true, allowJs: true, jsx: ts.JsxEmit.Preserve, noResolve: true });
    const checker = prog.getTypeChecker();
    const A = prog.getSourceFile(fa), B = prog.getSourceFile(fb);
    const top = (sf) => sf.statements.find((s) => declaredBy(s).some((id) => id.text === input.name));
    const ta = top(A), tb = top(B);
    if (!ta || !tb) return { result: null, why: `\`${input.name}\` not found in both` };
    return { result: behaviour(checker, A, ta, B, tb) };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

// Does a file's text parse? Syntax only (no program, no types), so it takes milliseconds: used as a reflex
// after an edit, before any check or review sees it.
function syntax() {
  const file = input.file, text = input.text != null ? input.text : fs.readFileSync(abs(file), 'utf8');
  const ext = path.extname(file).toLowerCase();
  const kind = { '.tsx': ts.ScriptKind.TSX, '.jsx': ts.ScriptKind.JSX, '.js': ts.ScriptKind.JSX, '.mjs': ts.ScriptKind.JS,
                 '.cjs': ts.ScriptKind.JS }[ext] || ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, false, kind);
  const errors = (sf.parseDiagnostics || []).slice(0, 5).map((d) => {
    const { line, character } = sf.getLineAndCharacterOfPosition(d.start || 0);
    return { line: line + 1, col: character + 1, message: ts.flattenDiagnosticMessageText(d.messageText, ' ') };
  });
  return { errors, count: (sf.parseDiagnostics || []).length };
}

// What a language service reads: the project's files from disk, with some texts replaced in memory.
function service(files, options, overrides = new Map()) {
  // an override of null is a file deleted
  const text = (f) => (overrides.has(f) ? overrides.get(f) ?? undefined : ts.sys.fileExists(f) ? ts.sys.readFile(f) : undefined);
  const host = {
    getScriptFileNames: () => files,
    getScriptVersion: (f) => (overrides.has(f) ? '2' : '1'),
    getScriptSnapshot: (f) => { const t = text(f); return t === undefined ? undefined : ts.ScriptSnapshot.fromString(t); },
    getCurrentDirectory: () => repo,
    getCompilationSettings: () => options,
    getDefaultLibFileName: (o) => ts.getDefaultLibFilePath(o),
    useCaseSensitiveFileNames: () => ts.sys.useCaseSensitiveFileNames,
    fileExists: (f) => (overrides.has(f) ? overrides.get(f) !== null : ts.sys.fileExists(f)),
    readFile: text,
    readDirectory: ts.sys.readDirectory,
    directoryExists: (d) => ts.sys.directoryExists(d) || [...overrides].some(([f, t]) => t !== null && f.startsWith(d.replace(/\/?$/, '/'))),
    getDirectories: ts.sys.getDirectories,
  };
  return ts.createLanguageService(host, ts.createDocumentRegistry());
}

// The files of the project the declaring file belongs to (its tsconfig's, else every source file in the repo).
function projectOf(file) {
  const cfgPath = ts.findConfigFile(path.dirname(file), ts.sys.fileExists);
  let options = { allowJs: true, jsx: ts.JsxEmit.Preserve }, names = [];
  if (cfgPath) {
    const cfg = ts.parseJsonConfigFileContent(ts.readConfigFile(cfgPath, ts.sys.readFile).config, ts.sys, path.dirname(cfgPath));
    options = cfg.options;
    names = cfg.fileNames;
  }
  if (!names.length) {
    const walk = (dir) => {
      for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
        if (e.name === 'node_modules' || e.name.startsWith('.')) continue;
        const full = path.join(dir, e.name);
        if (e.isDirectory()) walk(full);
        else if (/\.(tsx?|jsx?)$/.test(e.name) && !e.name.endsWith('.d.ts')) names.push(full.split(path.sep).join('/'));
      }
    };
    walk(repo);
  }
  const all = new Set([...names.map((n) => path.resolve(n).split(path.sep).join('/')), file.split(path.sep).join('/')]);
  return { options: { ...options, allowJs: true, noEmit: true }, files: [...all] };
}

function nodeAt(sf, pos) {
  let found = sf;
  const visit = (n) => {
    if (pos < n.getStart(sf) || pos >= n.end) return;
    found = n;
    ts.forEachChild(n, visit);
  };
  ts.forEachChild(sf, visit);
  return found;
}

// A name used as a member or as an external name: it doesn't bind in the scope around it.
function isExternalName(n) {
  const p = n.parent;
  return (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isQualifiedName(p) && p.right === n)
    || (ts.isImportSpecifier(p) && p.propertyName === n) || ts.isImportTypeNode(p)
    || (ts.isExportSpecifier(p) && (!!p.parent.parent.moduleSpecifier || (!!p.propertyName && p.name === n)));
}

function scopeOf(n) {
  for (let p = n.parent; p; p = p.parent) {
    if (ts.isBlock(p) || ts.isSourceFile(p) || ts.isModuleBlock(p) || ts.isCaseBlock(p) || ts.isFunctionLike(p)
        || ts.isClassLike(p) || ts.isCatchClause(p) || ts.isForStatement(p) || ts.isForInStatement(p) || ts.isForOfStatement(p)) return p;
  }
  return n.getSourceFile();
}

// The renamable top-level declarations called `name`: [{id, stmt}], one per symbol.
function renameCandidates(sf, checker, name) {
  const found = [];
  for (const stmt of sf.statements) {
    for (const id of declaredBy(stmt)) if (id.text === name) found.push({ id, stmt });
    if (ts.isExportDeclaration(stmt) && !stmt.moduleSpecifier && stmt.exportClause && ts.isNamedExports(stmt.exportClause)) {
      for (const el of stmt.exportClause.elements) if (el.propertyName && el.name.text === name) found.push({ id: el.name, stmt });
    }
  }
  const seen = new Set();
  return found.filter(({ id }) => {
    const key = checker.getSymbolAtLocation(id) || id;
    return seen.has(key) ? false : (seen.add(key), true);
  });
}

// What `to` would clash with: the declaration of that name visible in a scope (default-library globals are
// shadowable, so they don't count). `at(n)` is the scope where an identifier binds, `inScope(scope)` a scope.
function visibleNames(prog, checker, to) {
  const scopes = new Map();
  const inScope = (scope) => {
    if (!scopes.has(scope)) {
      const names = new Map();
      for (const s of checker.getSymbolsInScope(scope, ts.SymbolFlags.Value | ts.SymbolFlags.Type | ts.SymbolFlags.Namespace | ts.SymbolFlags.Alias)) {
        const d = s.declarations && s.declarations[0];
        if (d && !prog.isSourceFileDefaultLibrary(d.getSourceFile())) names.set(s.name, d);
      }
      scopes.set(scope, names);
    }
    return scopes.get(scope).get(to);
  };
  const at = (n) => {
    const p = n.parent;
    return inScope(scopeOf((ts.isFunctionLike(p) || ts.isClassLike(p)) && p.name === n ? p : n));  // a declared name binds outside its own body
  };
  return { at, inScope };
}

function diagnosticKeys(ls, names, undo) {
  const keys = new Map();
  for (const f of names) {
    for (const d of [...ls.getSyntacticDiagnostics(f), ...ls.getSemanticDiagnostics(f)]) {
      const msg = undo(ts.flattenDiagnosticMessageText(d.messageText, ' '));
      const key = `${rel(f)}|${d.code}|${msg}`;
      const at = d.file ? `${rel(f)}:${lineOf(d.file, d.start || 0)}` : rel(f);
      keys.set(key, [...(keys.get(key) || []), `${at}: TS${d.code} ${msg}`]);
    }
  }
  return keys;
}

// A top-level declaration gets a new name in every file of the project, through TypeScript's own rename
// (imports, re-exports, JSX tags, type references, shorthand properties). Refused, with nothing changed,
// on a name clash, a location outside the repo, or when the result has an error the project didn't have.
function rename() {
  const file = abs(input.file).split(path.sep).join('/'), name = input.name, to = input.to;
  if (!name || !to) throw new Error('rename needs name and to');
  const probe = ts.createSourceFile('probe.ts', `let ${to} = 0`, ts.ScriptTarget.Latest, true);
  if (!ts.isIdentifierText(to, ts.ScriptTarget.Latest) || (probe.parseDiagnostics || []).length) throw new Error(`\`${to}\` isn't a valid identifier`);
  if (to === name) throw new Error('the new name is the old one');
  const { options, files } = projectOf(file);
  const ls = service(files, options);
  const prog = ls.getProgram(), checker = prog.getTypeChecker();
  const sf = prog.getSourceFile(file);
  if (!sf) throw new Error(`TypeScript didn't load ${input.file} (excluded by its tsconfig?)`);
  let found = renameCandidates(sf, checker, name);
  if (!found.length) throw new Error(`no top-level declaration named ${name} in ${input.file}`);
  const lines = (c) => [lineOf(sf, c.id.getStart(sf)), lineOf(sf, c.stmt.getStart(sf))];
  if (input.line) found = found.filter((c) => lines(c).includes(input.line));
  if (!found.length) throw new Error(`${name} isn't declared at line ${input.line} of ${input.file}`);
  if (found.length > 1) throw new Error(`${name} is declared more than once in ${input.file} (lines ${found.map((c) => lines(c)[1]).join(', ')}); pass "line"`);
  const target = found[0].id;
  const info = ls.getRenameInfo(file, target.getStart(sf), { allowRenameOfImportPath: false });
  if (!info.canRename) throw new Error(`TypeScript can't rename ${name}: ${info.localizedErrorMessage}`);
  const locs = ls.findRenameLocations(file, target.getStart(sf), false, false, { providePrefixAndSuffixTextForRename: true });
  if (!locs || !locs.length) throw new Error(`TypeScript found nothing to rename for ${name}`);
  const unique = new Map(locs.map((l) => [`${l.fileName}:${l.textSpan.start}`, l]));
  const places = [...unique.values()];
  const where = places.map((l) => {
    const f = path.resolve(l.fileName), r = path.relative(repo, f);
    if (r.startsWith('..') || path.isAbsolute(r) || r.split(path.sep).includes('node_modules')) throw new Error(`${name} is also used in ${r.startsWith('..') ? f : r} (node_modules or outside the repo); not renaming`);
    return `${rel(f)}:${lineOf(prog.getSourceFile(l.fileName), l.textSpan.start)}`;
  });
  // a clash: `to` already visible where the new name would bind (default-library globals are shadowable)
  const { at: visible } = visibleNames(prog, checker, to);
  const lowerJsx = /^[a-z]/.test(to);
  for (const l of places) {
    const n = nodeAt(prog.getSourceFile(l.fileName), l.textSpan.start);
    if (!ts.isIdentifier(n)) continue;
    const clash = isExternalName(n) ? null : visible(n);
    if (clash) throw new Error(`\`${to}\` is already declared or imported at ${rel(clash.getSourceFile().fileName)}:${lineOf(clash.getSourceFile(), clash.getStart())}, in scope where ${name} is used (${rel(l.fileName)}:${lineOf(n.getSourceFile(), n.getStart())})`);
    const p = n.parent;
    if (lowerJsx && (ts.isJsxOpeningElement(p) || ts.isJsxClosingElement(p) || ts.isJsxSelfClosingElement(p)) && p.tagName === n)
      throw new Error(`\`${to}\` starts with a lowercase letter, so JSX would read <${to}> as a built-in tag`);
  }
  // the new texts
  const byFile = new Map();
  for (const l of places) byFile.set(l.fileName, [...(byFile.get(l.fileName) || []), l]);
  const files_ = {}, overrides = new Map();
  for (const [f, ls_] of byFile) {
    const o = prog.getSourceFile(f);
    const text = applyEdits(o.text, ls_.map((l) => [l.textSpan.start, l.textSpan.start + l.textSpan.length, `${l.prefixText || ''}${to}${l.suffixText || ''}`]));
    if (text === o.text) continue;
    files_[rel(f)] = text;
    overrides.set(f, text);
  }
  // the safety net: no error the project didn't have before
  const spelled = new RegExp(`(?<![\\w$])${name.replace(/[$]/g, '\\$&')}(?![\\w$])`);  // files still spelling the old name can break too
  const changed = [...overrides.keys(), ...files.filter((f) => !overrides.has(f) && spelled.test(ts.sys.readFile(f) || ''))];
  const esc = to.replace(/[$]/g, '\\$&');
  const undo = (m) => m.replace(new RegExp(`(?<![\\w$])${esc}(?![\\w$])`, 'g'), name);
  const before = diagnosticKeys(ls, changed, (m) => m);
  const after = diagnosticKeys(service(files, options, overrides), changed, undo);
  const fresh = [];
  for (const [key, list] of after) fresh.push(...list.slice((before.get(key) || []).length));
  if (fresh.length) throw new Error(`renaming ${name} to ${to} would add ${fresh.length} error(s): ${fresh.slice(0, 3).join('; ')}`);
  return { files: files_, count: places.length, renamed: { from: name, to }, where: where.sort() };
}

// Imports of one module written twice (both copies' imports now name the folded file) become one declaration,
// when both are plain named imports.
function mergeDuplicateImports(fileName, text) {
  const sf = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true);
  const seen = new Map(), edits = [];
  for (const st of sf.statements) {
    if (!ts.isImportDeclaration(st) || !st.importClause || st.importClause.name || st.importClause.isTypeOnly) continue;
    const nb = st.importClause.namedBindings;
    if (!nb || !ts.isNamedImports(nb)) continue;
    const spec = st.moduleSpecifier.text, first = seen.get(spec);
    if (!first) { seen.set(spec, { st, names: nb.elements.map((e) => e.getText(sf)) }); continue; }
    first.names.push(...nb.elements.map((e) => e.getText(sf)).filter((n) => !first.names.includes(n)));
    edits.push([lineStart(text, st.getStart(sf)), lineEnd(text, st.getEnd()), '']);
  }
  for (const { st, names } of seen.values()) {
    const nb = st.importClause.namedBindings;
    if (names.length > nb.elements.length) edits.push([nb.getStart(sf), nb.getEnd(), `{ ${names.join(', ')} }`]);
  }
  return edits.length ? applyEdits(text, edits) : text;
}

// Two variants of one file that are the same (`screens/x/app/a.tsx` and `screens/x/app-pro/a.tsx`) become one
// file (`screens/x/a.tsx` by default), through TypeScript's own file-rename edits: the file's relative imports
// and every importer of either copy are repointed. Refused, with nothing changed, when the texts differ, when
// their imports point at different files once moved (byte-identical copies that read their own siblings), when
// the target exists, or when the result has an error the project didn't have.
function fold() {
  const [a, b] = (input.files || []).map((f) => abs(f).split(path.sep).join('/'));
  if (!a || !b || a === b) throw new Error('fold needs "files": the two copies');
  for (const f of [a, b]) if (!ts.sys.fileExists(f)) throw new Error(`${rel(f)} doesn't exist`);
  let into = input.into;
  if (!into) {
    if (path.basename(a) !== path.basename(b) || path.dirname(path.dirname(a)) !== path.dirname(path.dirname(b)))
      throw new Error('the copies aren\'t the same name in sibling folders; pass "into"');
    into = path.join(path.dirname(path.dirname(a)), path.basename(a));
  }
  into = abs(into).split(path.sep).join('/');
  if (ts.sys.fileExists(into)) throw new Error(`${rel(into)} already exists`);
  if (path.extname(into) !== path.extname(a)) throw new Error(`${rel(into)} must keep the ${path.extname(a)} extension`);
  if (/\.d\.ts$/.test(a)) throw new Error('declaration files are read by the tsconfig where they are; not folding');
  const textA = ts.sys.readFile(a), textB = ts.sys.readFile(b);
  // the same code: indentation and blank lines aside (the first copy's formatting is kept)
  const same = (t) => t.split('\n').map((l) => l.trim()).filter(Boolean).join('\n');
  if (same(textA) !== same(textB)) {
    const la = same(textA).split('\n'), lb = same(textB).split('\n');
    const i = la.findIndex((l, n) => l !== lb[n]);
    const at = textA.split('\n').map((l, n) => [l.trim(), n]).filter(([l]) => l)[i];
    throw new Error(`the copies differ (first at line ${at ? at[1] + 1 : '(end)'} of ${rel(a)}: ${JSON.stringify((la[i] || '').trim().slice(0, 80))} vs ${JSON.stringify((lb[i] || '').trim().slice(0, 80))})`);
  }
  const { options, files } = projectOf(a);
  const ls = service([...new Set([...files, b])], options);
  const prog = ls.getProgram();
  const fmt = ts.getDefaultFormatCodeSettings();
  const prefs = { importModuleSpecifierPreference: 'relative', allowTextChangesInNewFiles: true };
  const own = { [a]: null, [b]: null }, byFile = new Map();
  for (const f of [a, b]) {
    for (const ch of ls.getEditsForFileRename(f, into, fmt, prefs)) {
      const name = path.resolve(ch.fileName).split(path.sep).join('/');
      const edits = ch.textChanges.map((c) => [c.span.start, c.span.start + c.span.length, c.newText]);
      if (name === a || name === b) { if (name === f) own[f] = edits; continue; }
      const r = path.relative(repo, name);
      if (r.startsWith('..') || r.split(path.sep).includes('node_modules')) throw new Error(`${rel(f)} is imported from outside the repo (${name})`);
      byFile.set(name, [...(byFile.get(name) || []), ...edits]);
    }
  }
  // nothing imports a config file (babel.config.js, a per-app env file): a tool finds it by where it is
  if (!byFile.size) throw new Error(`no file imports ${rel(a)} or ${rel(b)}, so a tool (or the app's entry point) finds them by where they are; not folding`);
  // the moved texts must read the same files: copies that import their own siblings aren't one file
  const movedA = applyEdits(textA, own[a] || []), movedB = applyEdits(textB, own[b] || []);
  if (same(movedA) !== same(movedB)) {
    const la = same(movedA).split('\n'), lb = same(movedB).split('\n');
    const i = la.findIndex((l, n) => l !== lb[n]);
    throw new Error(`the copies import different files once moved (line ${i + 1}: ${(la[i] || '').trim().slice(0, 100)} vs ${(lb[i] || '').trim().slice(0, 100)}); fold the files they import first, or share instead`);
  }
  const files_ = { [rel(into)]: movedA }, overrides = new Map([[a, null], [b, null], [into, movedA]]);
  for (const [f, edits] of byFile) {
    const spans = edits.map((e) => `${e[0]}:${e[1]}`);
    const uniq = edits.filter((e, i) => spans.indexOf(`${e[0]}:${e[1]}`) === i);
    const sorted = [...uniq].sort((x, y) => x[0] - y[0]);
    if (sorted.some((e, i) => i && e[0] < sorted[i - 1][1])) throw new Error(`overlapping import edits in ${rel(f)}`);
    const o = prog.getSourceFile(f), text = mergeDuplicateImports(f, applyEdits(o ? o.text : ts.sys.readFile(f), uniq));
    if (text === (o ? o.text : ts.sys.readFile(f))) continue;
    files_[rel(f)] = text;
    overrides.set(f, text);
  }
  // files that still name either copy's path (a path alias, jest.mock) break silently; check them too
  const stem = path.basename(a).replace(/\.[jt]sx?$/, '');
  const spelled = new RegExp(`['"\`][^'"\`\\n]*/${stem.replace(/[.$]/g, '\\$&')}(\\.[jt]sx?)?['"\`]`);
  const mention = files.filter((f) => !overrides.has(f) && spelled.test(ts.sys.readFile(f) || ''));
  const leftovers = [];
  for (const f of mention) {
    const t = ts.sys.readFile(f) || '';
    for (const m of t.matchAll(new RegExp(spelled.source, 'g'))) {
      const spec = m[0].slice(1, -1);
      if (!spec.startsWith('.')) continue;
      const target = path.resolve(path.dirname(f), spec).split(path.sep).join('/');
      if ([a, b].some((x) => x === target || x.replace(/\.[jt]sx?$/, '') === target)) leftovers.push(`${rel(f)}: ${spec}`);
    }
  }
  if (leftovers.length) throw new Error(`still named by path where TypeScript doesn't repoint (${leftovers.slice(0, 4).join('; ')}); not folding`);
  // the safety net: no error the project didn't have before (the copy's own errors now live at the new path)
  const after_ = [...new Set([...files.filter((f) => f !== a && f !== b), into])];
  const was = (k) => k.replace(`${rel(a)}|`, `${rel(into)}|`);
  const before = new Map();
  for (const [k, list] of diagnosticKeys(ls, [a, ...byFile.keys(), ...mention], (m) => m)) before.set(was(k), list);
  const after = diagnosticKeys(service(after_, options, overrides), [into, ...byFile.keys(), ...mention], (m) => m);
  const fresh = [];
  for (const [key, list] of after) fresh.push(...list.slice((before.get(key) || []).length));
  if (fresh.length) throw new Error(`folding would add ${fresh.length} error(s): ${fresh.slice(0, 3).join('; ')}`);
  return { files: files_, deleted: [rel(a), rel(b)], into: rel(into), formatting_differed: textA !== textB, importers_updated: [...byFile.keys()].filter((f) => files_[rel(f)]).map(rel).sort() };
}

try {
  const ops = { extract, compare, share, share_hook: shareHook, unify, relocate, dedupe, find_copies: findCopies, same_behaviour: sameBehaviour, signature, component, syntax, rename, fold };
  if (!ops[input.op]) throw new Error(`unknown op ${input.op}`);
  process.stdout.write(JSON.stringify({ ok: true, ...ops[input.op]() }));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: e.message, stack: process.env.BUCKETS_DEBUG ? e.stack : undefined }));
}
