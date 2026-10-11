#!/usr/bin/env node
// Mechanical moves for TypeScript/JavaScript, so a model decides what goes where and buckets moves
// the code byte for byte instead of having it retyped.
//
//   extract  move a component's logic (or the named statements) into a new hook file: the hook's
//            parameters, its return value, the private helpers that must come along and the
//            imports on both sides are worked out from TypeScript's own scoping.
//   compare  pair the declarations of two variants of a file (identical / near / different / one side).
//   share    move declarations that are identical in two variants into one shared file.
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
const rel = (p) => path.relative(repo, p).split(path.sep).join('/');

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

// Every identifier in `node` that refers to something, with the symbol it refers to.
function references(checker, node) {
  const out = [];
  const visit = (n) => {
    if (ts.isIdentifier(n)) {
      const p = n.parent;
      const skip = (ts.isPropertyAccessExpression(p) && p.name === n) || (ts.isQualifiedName(p) && p.right === n)
        || (ts.isPropertyAssignment(p) && p.name === n) || (ts.isJsxAttribute(p) && p.name === n)
        || ((ts.isMethodDeclaration(p) || ts.isPropertyDeclaration(p) || ts.isPropertySignature(p)
             || ts.isMethodSignature(p) || ts.isGetAccessor(p) || ts.isSetAccessor(p)) && p.name === n)
        || (ts.isBindingElement(p) && p.propertyName === n) || ts.isImportSpecifier(p) || ts.isExportSpecifier(p)
        || (ts.isLabeledStatement(p) || ts.isBreakOrContinueStatement(p));
      if (!skip) {
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

function applyEdits(text, edits) {
  const sorted = [...edits].sort((a, b) => b[0] - a[0] || b[1] - a[1]);
  let out = text;
  let last = Infinity;
  for (const [s, e, r] of sorted) {
    if (e > last) throw new Error('overlapping edits');
    out = out.slice(0, s) + r + out.slice(e);
    last = s;
  }
  return out.replace(/\n{3,}/g, '\n\n');
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
    const js = ts.transpileModule(`(${fn.getText(sf)})`, { compilerOptions: { target: ts.ScriptTarget.ES2019 } }).outputText;
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

// Copies of a function in other files replaced by an import of the one that stays: each copy that is a
// top-level declaration identical to the original (as TypeScript prints it, `export` aside) goes.
function dedupe() {
  const home = abs(input.file);
  const { checker, sources } = program([input.file, ...input.copies]);
  const [H, ...C] = sources;
  const top = (sf, name) => sf.statements.find((s) => declaredBy(s).some((id) => id.text === name));
  const bare = (sf, st) => normalized(st, sf).replace(/^export/, '');
  const files = {}, replaced = [], differ = [], missing = [], equivalent = [];
  for (const name of input.names) {
    const orig = top(H, name);
    if (!orig) throw new Error(`\`${name}\` isn't a top-level declaration in ${input.file}`);
    if (!isExported(orig)) throw new Error(`\`${name}\` isn't exported from ${input.file}; export it (or relocate it there) first`);
  }
  for (const sf of C) {
    const edits = [], names = [];
    for (const name of input.names) {
      const copy = top(sf, name);
      if (!copy) { missing.push(`${rel(sf.fileName)}: ${name}`); continue; }
      let same = null;
      if (bare(sf, copy) !== bare(H, top(H, name))) {
        same = behaviour(checker, H, top(H, name), sf, copy);
        if (!same || !same.same) {
          const why = !same ? '' : `\n  runs differently: (${same.input}) gives ${same.a} from the shared one, ${same.b} from the copy`;
          differ.push({ file: rel(sf.fileName), name, diff: lineDiff(top(H, name).getText(H), copy.getText(sf), 14) + why });
          continue;
        }
        equivalent.push(`${rel(sf.fileName)}: ${name} (written differently, behaves the same on ${same.tried} sample inputs)`);
      }
      if (isExported(copy)) { differ.push({ file: rel(sf.fileName), name, diff: '(exported from there too; others may import it)' }); continue; }
      edits.push([fullStart(sf, copy), lineEnd(sf.text, copy.end), '']);
      names.push(name);
    }
    if (!names.length) continue;
    // imports only the removed copies used go too
    const removed = sf.statements.filter((st) => edits.some(([a]) => a === fullStart(sf, st)));
    const imp = importBindings(sf, checker);
    const keptRefs = sf.statements.filter((st) => !removed.includes(st) && !ts.isImportDeclaration(st)).flatMap((st) => references(checker, st));
    pruneImports(sf, new Set(keptRefs.filter(({ sym }) => imp.has(sym)).map(({ sym }) => imp.get(sym))), edits);
    files[rel(sf.fileName)] = mergeImports(applyEdits(sf.text, edits), [`import { ${names.join(', ')} } from "${importPath(sf.fileName, home)}"`], sf.fileName);
    replaced.push(`${rel(sf.fileName)}: ${names.join(', ')}`);
  }
  return { files, report: { replaced, differ, missing, equivalent } };
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

try {
  const ops = { extract, compare, share, share_hook: shareHook, unify, relocate, dedupe, same_behaviour: sameBehaviour, signature };
  if (!ops[input.op]) throw new Error(`unknown op ${input.op}`);
  process.stdout.write(JSON.stringify({ ok: true, ...ops[input.op]() }));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: e.message, stack: process.env.BUCKETS_DEBUG ? e.stack : undefined }));
}
