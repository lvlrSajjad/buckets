#!/usr/bin/env node
/**
 * A syntax-only index of a TypeScript/JavaScript repository, for buckets.
 *
 * Parses every source file with the repository's own `typescript` package (no type checking,
 * no build), and answers three questions as JSON on stdout:
 *
 *   outline <repo> [file...]   what each file declares: kind, name, exported, line range, signature
 *   uses <repo> <name> [file]  every place the symbol is imported and used, following re-exports;
 *                              with a file, only the symbol exported from that file
 *   surface <repo>             the exported declarations of every file (to diff two git refs)
 *   importers <repo> <file>... files that import any of these files directly
 *   broken <repo>              imports of repository files that name something the file doesn't export
 *                              (directly or through re-exports): a rename that missed a caller
 *
 * Workspace packages are resolved by their package.json name to their sources (src/index.ts
 * when present), so a use in packages/nestjs of a symbol from packages/core is found even when
 * the compiler itself only sees core's built dist/.
 *
 * File list: `git ls-files` (tracked and untracked, ignoring .gitignore'd), so node_modules and
 * build output stay out.
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const EXTENSIONS = ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs'];
const MAX_REEXPORT_DEPTH = 6;

function loadTypescript(repo) {
  try {
    return require(require.resolve('typescript', { paths: [repo] }));
  } catch {
    return require('typescript'); // a global install, if the repo has none
  }
}

function sourceFiles(repo) {
  const out = execFileSync('git', ['ls-files', '-co', '--exclude-standard'], { cwd: repo, maxBuffer: 64 << 20 });
  return out
    .toString()
    .split('\n')
    .filter((f) => f && EXTENSIONS.includes(path.extname(f)) && !f.endsWith('.d.ts') && !/(^|\/)(dist|build|node_modules)\//.test(f));
}

function lineOf(sf, pos) {
  return sf.getLineAndCharacterOfPosition(pos).line + 1;
}

function isExported(ts, node) {
  return !!(ts.getCombinedModifierFlags(node) & ts.ModifierFlags.Export);
}

/** The declaration's head without its body, on one line: `compare(a: X, b: Y): boolean`. */
function signature(ts, sf, node) {
  const start = node.getStart(sf);
  let end = node.end;
  if (node.body) end = node.body.getStart(sf);
  else if (ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node) || ts.isEnumDeclaration(node)) {
    end = node.members.pos;
  } else if (ts.isVariableStatement(node)) {
    const decl = node.declarationList.declarations[0];
    const init = decl && decl.initializer;
    if (init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) && init.body) end = init.body.getStart(sf);
    else if (init) end = init.getStart(sf) + Math.min(60, init.getWidth(sf));
  }
  return sf.text
    .slice(start, end)
    .replace(/^export\s+(default\s+)?/, '')
    .replace(/\s+/g, ' ')
    .replace(/\s*[{=]\s*$/, '')
    .trim()
    .slice(0, 200);
}

function declarations(ts, sf) {
  const out = [];
  const add = (node, kind, name, exported, members) =>
    out.push({
      kind,
      name,
      exported,
      line: lineOf(sf, node.getStart(sf)),
      end: lineOf(sf, node.end),
      signature: signature(ts, sf, node),
      ...(members && members.length ? { members } : {}),
    });
  for (const st of sf.statements) {
    const exp = isExported(ts, st);
    if (ts.isFunctionDeclaration(st) && st.name) add(st, 'function', st.name.text, exp);
    else if (ts.isClassDeclaration(st) && st.name) {
      const members = [];
      for (const m of st.members) {
        if ((ts.isMethodDeclaration(m) || ts.isGetAccessor(m) || ts.isConstructorDeclaration(m)) && m.body) {
          const name = m.name ? m.name.getText(sf) : 'constructor';
          if (ts.getCombinedModifierFlags(m) & ts.ModifierFlags.Private) continue;
          members.push({ name, line: lineOf(sf, m.getStart(sf)), end: lineOf(sf, m.end), signature: signature(ts, sf, m) });
        }
      }
      add(st, 'class', st.name.text, exp, members);
    } else if (ts.isInterfaceDeclaration(st)) add(st, 'interface', st.name.text, exp);
    else if (ts.isTypeAliasDeclaration(st)) add(st, 'type', st.name.text, exp);
    else if (ts.isEnumDeclaration(st)) add(st, 'enum', st.name.text, exp);
    else if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (ts.isIdentifier(d.name)) add(st, 'const', d.name.text, exp);
      }
    }
  }
  return out;
}

function importsOf(ts, sf) {
  const out = [];
  for (const st of sf.statements) {
    if (!ts.isImportDeclaration(st) || !ts.isStringLiteral(st.moduleSpecifier)) continue;
    const entry = { from: st.moduleSpecifier.text, line: lineOf(sf, st.getStart(sf)), names: [] };
    const clause = st.importClause;
    if (clause && clause.name) entry.names.push({ imported: 'default', local: clause.name.text });
    const bindings = clause && clause.namedBindings;
    if (bindings && ts.isNamedImports(bindings)) {
      for (const el of bindings.elements) {
        entry.names.push({ imported: (el.propertyName || el.name).text, local: el.name.text });
      }
    } else if (bindings && ts.isNamespaceImport(bindings)) {
      entry.namespace = bindings.name.text;
    }
    out.push(entry);
  }
  return out;
}

function reexportsOf(ts, sf) {
  const out = [];
  for (const st of sf.statements) {
    if (!ts.isExportDeclaration(st) || !st.moduleSpecifier || !ts.isStringLiteral(st.moduleSpecifier)) continue;
    const entry = { from: st.moduleSpecifier.text, all: !st.exportClause, names: [], line: lineOf(sf, st.getStart(sf)) };
    if (st.exportClause && ts.isNamedExports(st.exportClause)) {
      for (const el of st.exportClause.elements) {
        entry.names.push({ imported: (el.propertyName || el.name).text, exported: el.name.text });
      }
    }
    out.push(entry);
  }
  return out;
}

/** Lines where an identifier with this name appears (excluding its import line). */
function identifierLines(ts, sf, name) {
  const lines = new Set();
  const visit = (node) => {
    if (ts.isIdentifier(node) && node.text === name) lines.add(lineOf(sf, node.getStart(sf)));
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return [...lines].sort((a, b) => a - b);
}

function workspacePackages(repo, files) {
  const out = {};
  const manifests = execFileSync('git', ['ls-files', '-co', '--exclude-standard', '*package.json', 'package.json'], { cwd: repo })
    .toString()
    .split('\n')
    .filter((f) => f && !f.includes('node_modules/'));
  for (const m of manifests) {
    try {
      const pkg = JSON.parse(fs.readFileSync(path.join(repo, m), 'utf8'));
      if (pkg.name) out[pkg.name] = path.dirname(m) === '.' ? '' : path.dirname(m);
    } catch {
      /* not JSON we can read */
    }
  }
  return out;
}

function createIndex(repo) {
  const ts = loadTypescript(repo);
  const files = sourceFiles(repo);
  const fileSet = new Set(files);
  const packages = workspacePackages(repo, files);
  const parsed = new Map();

  const parse = (file) => {
    if (!parsed.has(file)) {
      const text = fs.readFileSync(path.join(repo, file), 'utf8');
      const kind = file.endsWith('x') ? ts.ScriptKind.TSX : undefined;
      parsed.set(file, ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind));
    }
    return parsed.get(file);
  };

  const tryFile = (base) => {
    const noJs = base.replace(/\.(m|c)?js$/, '');
    const candidates = [base, ...EXTENSIONS.map((e) => noJs + e), ...EXTENSIONS.map((e) => path.posix.join(base, 'index' + e))];
    return candidates.find((c) => fileSet.has(c));
  };

  /** A module specifier from `file` to a repository file, or undefined (external package). */
  const resolve = (file, spec) => {
    if (spec.startsWith('.')) return tryFile(path.posix.normalize(path.posix.join(path.posix.dirname(file), spec)));
    const parts = spec.split('/');
    const name = spec.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0];
    if (!(name in packages)) return undefined;
    const dir = packages[name];
    const sub = parts.slice(spec.startsWith('@') ? 2 : 1).join('/');
    const join = (...p) => path.posix.join(...p.filter(Boolean));
    if (sub) return tryFile(join(dir, 'src', sub)) || tryFile(join(dir, sub));
    return tryFile(join(dir, 'src/index')) || tryFile(join(dir, 'index'));
  };

  /** Files that export `name` from `origin`, directly or through re-exports, with the name used there. */
  const exportersOf = (origin, name) => {
    const found = new Map([[origin, new Set([name])]]);
    let frontier = [[origin, name]];
    for (let depth = 0; depth < MAX_REEXPORT_DEPTH && frontier.length; depth++) {
      const next = [];
      for (const file of files) {
        for (const re of reexportsOf(ts, parse(file))) {
          const target = resolve(file, re.from);
          for (const [from, n] of frontier) {
            if (target !== from) continue;
            const names = re.all ? [n] : re.names.filter((x) => x.imported === n).map((x) => x.exported);
            for (const exported of names) {
              if (!found.has(file)) found.set(file, new Set());
              if (!found.get(file).has(exported)) {
                found.get(file).add(exported);
                next.push([file, exported]);
              }
            }
          }
        }
      }
      frontier = next;
    }
    return found;
  };

  /** Every name a file exports: its own exported declarations, `export {…}` lists and re-exports. */
  const exportsCache = new Map();
  const exportsOf = (file, depth = 0) => {
    if (exportsCache.has(file)) return exportsCache.get(file);
    const sf = parse(file);
    const names = new Set(declarations(ts, sf).filter((d) => d.exported).map((d) => d.name));
    let complete = true;
    for (const st of sf.statements) {
      if (ts.isExportAssignment(st)) names.add('default');
      if (ts.isExportDeclaration(st) && st.exportClause && ts.isNamedExports(st.exportClause) && !st.moduleSpecifier) {
        for (const el of st.exportClause.elements) names.add(el.name.text);
      }
      if ((ts.isFunctionDeclaration(st) || ts.isClassDeclaration(st)) &&
          ts.getCombinedModifierFlags(st) & ts.ModifierFlags.Default) names.add('default');
    }
    for (const re of reexportsOf(ts, sf)) {
      const target = resolve(file, re.from);
      if (re.all) {
        if (!target || depth >= MAX_REEXPORT_DEPTH) complete = false; // can't see everything it passes on
        else {
          const inner = exportsOf(target, depth + 1);
          if (inner === null) complete = false;
          else inner.forEach((n) => n !== 'default' && names.add(n));
        }
      } else {
        // A named re-export only counts if the source really has the name.
        const inner = target && depth < MAX_REEXPORT_DEPTH ? exportsOf(target, depth + 1) : null;
        for (const el of re.names) {
          if (!target || inner === null || inner.has(el.imported)) names.add(el.exported);
        }
      }
    }
    const result = complete ? names : null;
    exportsCache.set(file, result);
    return result;
  };

  return {
    files,
    importers(targets) {
      // Importing a file's re-exporter (a package index.ts) counts as importing the file.
      const owner = new Map(targets.map((t) => [t, t]));
      for (let depth = 0, grew = true; grew && depth < MAX_REEXPORT_DEPTH; depth++) {
        grew = false;
        for (const file of files) {
          if (owner.has(file)) continue;
          for (const re of reexportsOf(ts, parse(file))) {
            const target = resolve(file, re.from);
            if (target && owner.has(target)) {
              owner.set(file, owner.get(target));
              grew = true;
              break;
            }
          }
        }
      }
      const out = {};
      for (const file of files) {
        for (const imp of importsOf(ts, parse(file))) {
          const target = resolve(file, imp.from);
          if (!target || !owner.has(target) || owner.has(file)) continue;
          const key = owner.get(target);
          (out[key] = out[key] || []).includes(file) || out[key].push(file);
        }
      }
      return out;
    },
    broken() {
      const out = [];
      for (const file of files) {
        const sf = parse(file);
        for (const imp of importsOf(ts, sf)) {
          const target = resolve(file, imp.from);
          if (!target) continue; // an external package: not ours to judge
          const exported = exportsOf(target);
          if (exported === null) continue; // re-exports from outside the repo; can't be sure
          for (const n of imp.names) {
            if (!exported.has(n.imported)) out.push({ file, line: imp.line, name: n.imported, from: imp.from, target });
          }
        }
        for (const re of reexportsOf(ts, sf)) {
          const target = resolve(file, re.from);
          const exported = target ? exportsOf(target) : null;
          if (exported === null) continue;
          for (const el of re.names) {
            if (!exported.has(el.imported)) {
              out.push({ file, line: re.line, name: el.imported, from: re.from, target, reexport: true });
            }
          }
        }
      }
      return out;
    },
    outline(only) {
      const out = {};
      for (const file of only && only.length ? only.filter((f) => fileSet.has(f)) : files) {
        out[file] = declarations(ts, parse(file));
      }
      return out;
    },
    surface() {
      const out = {};
      for (const file of files) {
        const exported = declarations(ts, parse(file)).filter((d) => d.exported);
        if (exported.length) out[file] = exported.map(({ kind, name, signature }) => ({ kind, name, signature }));
      }
      return out;
    },
    uses(name, origin) {
      const origins = origin
        ? [origin]
        : files.filter((f) => declarations(ts, parse(f)).some((d) => d.name === name && d.exported));
      const out = { name, definedIn: origins, uses: [] };
      for (const from of origins) {
        const exporters = exportersOf(from, name);
        // Files that pass the symbol on (`export { X } from './x.js'`) need updating on a rename too.
        for (const [file, names] of exporters) {
          if (file === from) continue;
          for (const re of reexportsOf(ts, parse(file))) {
            if (!exporters.has(resolve(file, re.from))) continue;
            const named = re.names.some((x) => names.has(x.exported));
            if (named || re.all) {
              out.uses.push({ file, importLine: re.line, local: [...names].join(','), lines: [re.line], via: from, reexport: true });
              break;
            }
          }
        }
        for (const file of files) {
          const sf = parse(file);
          for (const imp of importsOf(ts, sf)) {
            const target = resolve(file, imp.from);
            if (!target || !exporters.has(target)) continue;
            const exportedNames = exporters.get(target);
            for (const n of imp.names) {
              if (!exportedNames.has(n.imported)) continue;
              const lines = identifierLines(ts, sf, n.local).filter((l) => l !== imp.line);
              out.uses.push({ file, importLine: imp.line, local: n.local, lines, via: target });
            }
            if (imp.namespace) {
              const lines = [];
              const visit = (node) => {
                if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) &&
                    node.expression.text === imp.namespace && exportedNames.has(node.name.text)) {
                  lines.push(lineOf(sf, node.getStart(sf)));
                }
                ts.forEachChild(node, visit);
              };
              visit(sf);
              if (lines.length) out.uses.push({ file, importLine: imp.line, local: `${imp.namespace}.${name}`, lines, via: target });
            }
          }
          // Uses inside the defining file itself.
          if (file === from) {
            const lines = identifierLines(ts, sf, name);
            if (lines.length) out.uses.push({ file, importLine: null, local: name, lines, via: file });
          }
        }
      }
      return out;
    },
  };
}

function main() {
  const [cmd, repoArg, ...rest] = process.argv.slice(2);
  if (!cmd || !repoArg) {
    process.stderr.write('usage: ts_index.cjs outline|uses|surface <repo> [args]\n');
    process.exit(2);
  }
  const repo = path.resolve(repoArg);
  const index = createIndex(repo);
  let result;
  if (cmd === 'outline') result = index.outline(rest);
  else if (cmd === 'surface') result = index.surface();
  else if (cmd === 'uses') result = index.uses(rest[0], rest[1]);
  else if (cmd === 'broken') result = index.broken();
  else if (cmd === 'importers') result = index.importers(rest);
  else {
    process.stderr.write(`unknown command ${cmd}\n`);
    process.exit(2);
  }
  process.stdout.write(JSON.stringify(result));
}

if (require.main === module) main();
module.exports = { createIndex };
