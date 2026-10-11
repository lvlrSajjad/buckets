#!/usr/bin/env node
// Read a NestJS backend's HTTP routes from its code and write them as an OpenAPI 3 document that
// api_contract.cjs can use as its "spec" (an exported swagger file goes stale; the controllers do not).
//
// Input: one JSON object on stdin:
//   { "repo": "/abs/backend",
//     "include": ["src"],                                // optional; default: src if it exists, else the repo
//     "out": "/abs/spec.json" }                           // optional: also write the spec to this file
// Output: one JSON object on stdout:
//   { ok, spec: { openapi, info, servers?, paths, components }, summary: { controllers, routes },
//     unresolved: [{ file, line, why }] }
//   or { ok: false, error }
//
// What is read (with the TypeScript compiler, so constants, enums and DTO types are followed across files):
//   @Controller() with no argument, a path, a list of paths or { path, version }; @Get/@Post/@Put/@Patch/@Delete/
//   @Head/@Options/@Sse and @All (every method) with no argument, a path, a list or a constant; routes of a base class
//   a controller extends. The file that calls NestFactory.create gives app.setGlobalPrefix(...) (the prefix is part
//   of every path, and the spec's "servers" names it) and app.enableVersioning({ type: URI, defaultVersion, prefix })
//   (a "v1" segment from @Controller({ version }), @Version or the default). RouterModule.register is not applied;
//   it is listed in "unresolved".
//   @Param, @Query('name'), @Query() dto (each property is a query parameter), @Body() dto, @Body('field'); @Headers,
//   @Res and custom decorators are ignored (@Req / @Request lets a handler read any query key: see "open").
//   A DTO's properties come through extends chains and PartialType, PickType, OmitType, IntersectionType; a property is optional with "?", a default value, @IsOptional(),
//   @ValidateIf(), @ApiPropertyOptional() or @ApiProperty({ required: false }). Interfaces, type aliases and
//   mapped types are read from the checker's properties.
//   An "open" shape (any, object, Record, an index signature, an unknown base class) has no unknown keys: its
//   schema carries additionalProperties: true. A query with an open shape, or a handler that takes @Req(), drops
//   the route's query parameters (api_contract cannot say "these and others").
// Not read: modules (RouterModule paths, lazy modules), versioning by header or media type, custom routing.
//
// Nothing is written unless "out" is given.
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

const HTTP_DECORATORS = { Get: ['get'], Post: ['post'], Put: ['put'], Patch: ['patch'], Delete: ['delete'],
  Head: ['head'], Options: ['options'], Sse: ['get'],
  All: ['get', 'post', 'put', 'patch', 'delete', 'head', 'options'] };
const REQUEST_METHODS = { GET: 'get', POST: 'post', PUT: 'put', PATCH: 'patch', DELETE: 'delete', HEAD: 'head', OPTIONS: 'options' };
const MAPPED_TYPES = new Set(['PartialType', 'PickType', 'OmitType', 'IntersectionType']);
const OPTIONAL_DECORATORS = new Set(['IsOptional', 'ApiPropertyOptional', 'ValidateIf']);
const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'build', 'coverage', '__tests__', '__mocks__', 'e2e']);
const SOURCE_EXT = /\.(ts|tsx|mts|cts)$/;
const NOT_SOURCE = /\.(d\.ts|test\.\w+|spec\.\w+|e2e-spec\.\w+|stories\.\w+)$/;
const MAX_ALTERNATIVES = 16;
const UNKNOWN = '\u0000';

// ---------- files ----------

function globToRegex(glob) {
  let re = '';
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i];
    if (c === '*' && glob[i + 1] === '*') {
      if (glob[i + 2] === '/') { re += '(?:.*/)?'; i += 2; } else { re += '.*'; i += 1; }
    } else if (c === '*') re += '[^/]*';
    else if (c === '?') re += '[^/]';
    else if (c === '{') re += '(?:';
    else if (c === '}') re += ')';
    else if (c === ',' && /\{[^}]*$/.test(glob.slice(0, i))) re += '|';
    else re += c.replace(/[.+^$()|[\]\\]/g, '\\$&');
  }
  return new RegExp('^' + re + '$');
}

function walk(dir, top, out) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
  for (const e of entries) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      const topTests = dir === top && (e.name === 'test' || e.name === 'tests');  // a backend's own test folder, not a module
      if (!SKIP_DIRS.has(e.name) && !e.name.startsWith('.') && !topTests) walk(full, top, out);
    } else if (SOURCE_EXT.test(e.name) && !NOT_SOURCE.test(e.name)) out.push(full);
  }
}

function sourceFiles(repo, include) {
  const all = [];
  walk(repo, repo, all);
  const rels = all.map((f) => [f, path.relative(repo, f).split(path.sep).join('/')]);
  let list = include;
  if (!list || !list.length) list = fs.existsSync(path.join(repo, 'src')) ? ['src'] : [];
  if (!list.length) return all.sort();
  const tests = list.map((g) => {
    const norm = path.isAbsolute(g) ? path.relative(repo, g) : g.replace(/^\.\//, '');
    if (/[*?{]/.test(norm)) return globToRegex(norm);
    return new RegExp('^' + norm.replace(/\/+$/, '').replace(/[.+^$()|[\]\\]/g, '\\$&') + '(/|$)');
  });
  return rels.filter(([, r]) => tests.some((t) => t.test(r))).map(([f]) => f).sort();
}

function programFor(ts, repo, files) {
  const cfgPath = path.join(repo, 'tsconfig.json');
  let options = { target: ts.ScriptTarget.ES2017, module: ts.ModuleKind.CommonJS, experimentalDecorators: true };
  if (fs.existsSync(cfgPath)) {
    const parsed = ts.parseJsonConfigFileContent(ts.readConfigFile(cfgPath, ts.sys.readFile).config, ts.sys, repo);
    options = parsed.options;
  }
  return ts.createProgram(files, { ...options, noEmit: true, skipLibCheck: true, types: [], incremental: false, composite: false });
}

// ---------- paths ----------

function convertSegment(s) {
  if (/^\*\w+$/.test(s)) return '{' + s.slice(1) + '}';
  if (s.includes('*')) return '{wildcard}';
  return s.replace(/:([A-Za-z_]\w*)(\([^)]*\))?/g, '{$1}');
}

// "a{/:b}{/:c}" (an optional group, path-to-regexp 8) is every spelling with and without its groups.
function expandGroups(p) {
  const m = /\{([^{}]*)\}/.exec(p);
  if (!m) return [p];
  const pre = p.slice(0, m.index);
  const post = p.slice(m.index + m[0].length);
  return [...new Set([...expandGroups(pre + m[1] + post), ...expandGroups(pre + post)])].slice(0, MAX_ALTERNATIVES);
}

// Every spelling of a route's parts: "x/:id?" is both "x/{id}" and "x".
function joinPaths(parts) {
  let combos = [[]];
  for (const part of parts) {
    const spellings = expandGroups(part);
    combos = combos.flatMap((c) => spellings.map((s) => [...c, s])).slice(0, MAX_ALTERNATIVES * 4);
  }
  const out = new Set();
  for (const combo of combos) {
    let variants = [[]];
    for (const seg of combo.flatMap((p) => p.split('/')).filter(Boolean)) {
      const optional = /^:[A-Za-z_]\w*(\([^)]*\))?\?$/.test(seg);
      const text = convertSegment(optional ? seg.slice(0, -1) : seg);
      const next = variants.map((v) => [...v, text]);
      variants = optional && variants.length < MAX_ALTERNATIVES ? [...variants, ...next] : next;
    }
    for (const v of variants) out.add('/' + v.join('/'));
  }
  return [...out];
}

const assemble = (parts) => '/' + parts.flatMap((p) => p.split('/')).filter(Boolean).join('/');
const pathParamNames = (p) => [...p.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
const sameRoute = (a, b) => a.replace(/\{[^}]*\}/g, '{}').replace(/\/+$/, '') === b.replace(/\{[^}]*\}/g, '{}').replace(/\/+$/, '');

// ---------- analysis ----------

function analyze(ts, prog, files, repo) {
  const checker = prog.getTypeChecker();
  const realRepo = (() => { try { return fs.realpathSync(repo); } catch (e) { return repo; } })();
  const rel = (p) => {
    let r = path.relative(repo, p);
    if (r.startsWith('..')) r = path.relative(realRepo, p);
    return r.split(path.sep).join('/');
  };
  const wanted = new Set(files.map((f) => path.resolve(f)));
  const sourceFilesIn = () => prog.getSourceFiles().filter((sf) => wanted.has(path.resolve(sf.fileName)))
    .sort((a, b) => (a.fileName < b.fileName ? -1 : 1));
  const unresolved = [];
  const seenReports = new Set();
  const report = (node, why) => {
    const sf = node.getSourceFile();
    const line = sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
    const key = `${sf.fileName}:${line}:${why}`;
    if (seenReports.has(key)) return;
    seenReports.add(key);
    unresolved.push({ file: rel(sf.fileName), line, why });
  };
  const where = (node) => {
    const sf = node.getSourceFile();
    return `${rel(sf.fileName)}:${sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1}`;
  };

  const stripped = (n) => {
    while (n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n) ||
                 ts.isTypeAssertionExpression(n) || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(n)))) n = n.expression;
    return n;
  };
  const unique = (arr) => [...new Set(arr)];
  const declOf = (sym) => sym && (sym.valueDeclaration || (sym.declarations || [])[0]);

  function symbolOf(node) {
    let sym;
    if (ts.isElementAccessExpression(node)) sym = checker.getSymbolAtLocation(node.argumentExpression);
    else if (ts.isPropertyAccessExpression(node)) sym = checker.getSymbolAtLocation(node.name);
    else sym = checker.getSymbolAtLocation(node);
    if (sym && sym.flags & ts.SymbolFlags.Alias) {
      try { sym = checker.getAliasedSymbol(sym); } catch (e) { /* keep the alias */ }
    }
    return sym;
  }

  // ----- strings -----

  function cross(a, b) {
    if (a.length * b.length > MAX_ALTERNATIVES) return [a[0] + b[0]];
    const out = [];
    for (const x of a) for (const y of b) out.push(x + y);
    return out;
  }

  // Every string an expression can be; null when it is not a constant.
  function strings(node, depth = 0) {
    node = stripped(node);
    if (!node || depth > 8) return null;
    if (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) return [node.text];
    if (ts.isTemplateExpression(node)) {
      let acc = [node.head.text];
      for (const span of node.templateSpans) {
        const part = strings(span.expression, depth + 1);
        if (!part) return null;
        acc = cross(cross(acc, part), [span.literal.text]);
      }
      return unique(acc);
    }
    if (ts.isBinaryExpression(node) && node.operatorToken.kind === ts.SyntaxKind.PlusToken) {
      const l = strings(node.left, depth + 1);
      const r = strings(node.right, depth + 1);
      return l && r ? unique(cross(l, r)) : null;
    }
    if (ts.isIdentifier(node) || ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const sym = symbolOf(node);
      const decl = declOf(sym);
      if (decl) {
        if (ts.isEnumMember(decl)) {
          const v = checker.getConstantValue(decl);
          return typeof v === 'string' || typeof v === 'number' ? [String(v)] : null;
        }
        if (ts.isVariableDeclaration(decl) && decl.initializer && (ts.getCombinedNodeFlags(decl) & ts.NodeFlags.Const)) return strings(decl.initializer, depth + 1);
        if ((ts.isPropertyAssignment(decl) || ts.isPropertyDeclaration(decl)) && decl.initializer) return strings(decl.initializer, depth + 1);
      }
    }
    const t = checker.getTypeAtLocation(node);
    if (t && t.isStringLiteral()) return [t.value];
    if (t && t.isUnion() && t.types.every((x) => x.isStringLiteral())) return t.types.map((x) => x.value).slice(0, MAX_ALTERNATIVES);
    return null;
  }

  // Strings of an expression that is one string or a list of them (a list may hold constants and spreads of lists);
  // elements that are not constants are reported.
  function stringList(node, why) {
    const n = stripped(node);
    if (!n) return [];
    if (ts.isArrayLiteralExpression(n)) {
      return n.elements.flatMap((el) => (ts.isSpreadElement(el) ? stringList(el.expression, why) : stringList(el, why)));
    }
    const direct = strings(n);
    if (direct) return direct;
    if (ts.isIdentifier(n) || ts.isPropertyAccessExpression(n)) {
      const decl = declOf(symbolOf(n));
      if (decl && ts.isVariableDeclaration(decl) && decl.initializer && ts.isArrayLiteralExpression(stripped(decl.initializer))) {
        return stringList(decl.initializer, why);
      }
    }
    report(n, why);
    return [];
  }

  // ----- decorators -----

  const decoratorsOf = (n) => (ts.canHaveDecorators && ts.getDecorators ? (ts.canHaveDecorators(n) ? ts.getDecorators(n) : undefined) : n.decorators) || [];
  const calleeName = (e) => (ts.isIdentifier(e) ? e.text : ts.isPropertyAccessExpression(e) ? e.name.text : null);

  function decoInfo(d) {
    const e = stripped(d.expression);
    if (ts.isCallExpression(e)) return { name: calleeName(stripped(e.expression)), args: [...e.arguments], node: d };
    return { name: calleeName(e), args: [], node: d };
  }

  const decoNamed = (node, name) => decoratorsOf(node).map(decoInfo).filter((d) => d.name === name);

  const isNeutral = (n) => {
    n = stripped(n);
    return !!n && ((ts.isIdentifier(n) && n.text === 'VERSION_NEUTRAL') || (ts.isPropertyAccessExpression(n) && n.name.text === 'VERSION_NEUTRAL'));
  };

  // Versions in a decorator argument: strings, with null for VERSION_NEUTRAL.
  function versionList(node) {
    const n = stripped(node);
    if (!n) return [];
    if (ts.isArrayLiteralExpression(n)) return n.elements.flatMap((el) => versionList(el));
    if (isNeutral(n)) return [null];
    const r = strings(n);
    if (r) return r;
    report(n, 'the version is not a constant');
    return [];
  }

  const propertyOf = (obj, name) => {
    const p = obj.properties.find((x) => (ts.isPropertyAssignment(x) || ts.isShorthandPropertyAssignment(x)) && x.name && (ts.isIdentifier(x.name) || ts.isStringLiteralLike(x.name)) && x.name.text === name);
    return p ? (ts.isPropertyAssignment(p) ? p.initializer : p.name) : null;
  };

  // ----- shapes of DTOs and other types -----

  // A shape: props (name -> { required, schema }) and open (extra keys are allowed).
  const emptyShape = () => ({ props: new Map(), open: false });
  const openShape = () => ({ props: new Map(), open: true });

  function decoratorMakesOptional(d) {
    if (OPTIONAL_DECORATORS.has(d.name)) return true;
    if (d.name === 'ApiProperty' && d.args[0]) {
      const o = stripped(d.args[0]);
      const req = o && ts.isObjectLiteralExpression(o) ? propertyOf(o, 'required') : null;
      return !!req && req.kind === ts.SyntaxKind.FalseKeyword;
    }
    return false;
  }

  function declRequired(decl) {
    if (decl.questionToken) return false;
    if (ts.isPropertyDeclaration(decl) && decl.initializer) return false;  // a default: the client need not send it
    if (decoratorsOf(decl).map(decoInfo).some(decoratorMakesOptional)) return false;
    const t = decl.type;
    if (t && ts.isUnionTypeNode(t) && t.types.some((x) => x.kind === ts.SyntaxKind.UndefinedKeyword)) return false;
    return true;
  }

  function propertyKey(decl) {
    const n = decl.name;
    if (!n) return null;
    if (ts.isIdentifier(n) || ts.isStringLiteralLike(n) || ts.isNumericLiteral(n)) return n.text;
    if (ts.isComputedPropertyName(n)) {
      const r = strings(n.expression);
      return r && r.length === 1 ? r[0] : null;
    }
    return null;
  }

  const isDefaultLib = (sym) => (sym.declarations || []).length > 0 && sym.declarations.every((d) => prog.isSourceFileDefaultLibrary(d.getSourceFile()));
  const typeNameOf = (t) => (t.symbol ? t.symbol.name : '');
  const isArrayType = (t) => (checker.isArrayType && checker.isArrayType(t)) || (checker.isTupleType && checker.isTupleType(t)) || /^(Readonly)?Array$/.test(typeNameOf(t));

  function notNullish(t) {
    if (t.isUnion()) {
      const rest = t.types.filter((x) => !(x.flags & (ts.TypeFlags.Null | ts.TypeFlags.Undefined | ts.TypeFlags.Void)));
      return rest;
    }
    return (t.flags & (ts.TypeFlags.Null | ts.TypeFlags.Undefined)) ? [] : [t];
  }

  function hasIndexSignature(t) {
    if (checker.getIndexInfosOfType) return checker.getIndexInfosOfType(t).length > 0;
    return !!(checker.getIndexTypeOfType(t, ts.IndexKind.String) || checker.getIndexTypeOfType(t, ts.IndexKind.Number));
  }

  // The shallow JSON schema of a property's type (api_contract only reads the names and the required list).
  function primitiveSchema(t) {
    if (!t) return {};
    const parts = notNullish(t);
    if (parts.length !== 1) {
      const kinds = unique(parts.map((p) => JSON.stringify(primitiveSchema(p))));
      return kinds.length === 1 && parts.length ? JSON.parse(kinds[0]) : {};
    }
    const p = parts[0];
    if (p.flags & ts.TypeFlags.StringLike && !(p.flags & ts.TypeFlags.EnumLike)) return { type: 'string' };
    if (p.flags & ts.TypeFlags.NumberLike && !(p.flags & ts.TypeFlags.EnumLike)) return { type: 'number' };
    if (p.flags & ts.TypeFlags.BooleanLike) return { type: 'boolean' };
    if (p.flags & ts.TypeFlags.Object && isArrayType(p)) return { type: 'array' };
    return {};
  }

  const classCache = new Map();
  const classOf = (sym) => (sym && sym.declarations || []).find((d) => ts.isClassDeclaration(d));

  function mergeInto(shape, sub) {
    for (const [k, v] of sub.props) shape.props.set(k, v);
    if (sub.open) shape.open = true;
  }

  const allOptional = (sub) => {
    const out = { props: new Map(), open: sub.open };
    for (const [k, v] of sub.props) out.props.set(k, { ...v, required: false });
    return out;
  };

  // The shape of what a class extends: a class, or PartialType(X) / PickType(X, [...]) / OmitType(X, [...]) / IntersectionType(A, B).
  function heritageShape(expr, seen) {
    expr = stripped(expr);
    if (ts.isCallExpression(expr)) {
      const fn = calleeName(stripped(expr.expression));
      if (MAPPED_TYPES.has(fn)) {
        const args = [...expr.arguments];
        if (fn === 'IntersectionType') {
          const out = emptyShape();
          for (const a of args) mergeInto(out, heritageShape(a, seen));
          return out;
        }
        const base = args[0] ? heritageShape(args[0], seen) : openShape();
        if (fn === 'PartialType') return allOptional(base);
        const keys = args[1] ? stringList(args[1], 'a key of the mapped type is not a constant') : [];
        const out = { props: new Map(), open: base.open };
        for (const [k, v] of base.props) {
          if (fn === 'PickType' ? keys.includes(k) : !keys.includes(k)) out.props.set(k, v);
        }
        return out;
      }
    }
    if (ts.isIdentifier(expr) || ts.isPropertyAccessExpression(expr)) {
      const decl = classOf(symbolOf(expr));
      if (decl) return classShape(decl, seen);
    }
    // a mixin or anything else: what TypeScript says an instance has, or an open shape
    const t = checker.getTypeAtLocation(expr);
    const sig = t && t.getConstructSignatures && t.getConstructSignatures()[0];
    return sig ? shapeOfType(sig.getReturnType(), seen) : openShape();
  }

  function classShape(decl, seen = new Set()) {
    if (classCache.has(decl)) return classCache.get(decl);
    if (seen.has(decl)) return openShape();
    seen = new Set([...seen, decl]);
    const shape = emptyShape();
    const ext = (decl.heritageClauses || []).find((h) => h.token === ts.SyntaxKind.ExtendsKeyword);
    if (ext && ext.types[0]) mergeInto(shape, heritageShape(ext.types[0].expression, seen));
    for (const m of decl.members) {
      if (ts.isIndexSignatureDeclaration(m)) { shape.open = true; continue; }
      if (!ts.isPropertyDeclaration(m) || (ts.getCombinedModifierFlags(m) & ts.ModifierFlags.Static)) continue;
      const key = propertyKey(m);
      if (key === null) { shape.open = true; continue; }
      const required = declRequired(m);
      let schema = {};
      try { schema = primitiveSchema(m.type ? checker.getTypeFromTypeNode(m.type) : checker.getTypeAtLocation(m)); } catch (e) { /* shallow only */ }
      shape.props.set(key, { required, schema });
    }
    classCache.set(decl, shape);
    return shape;
  }

  // The shape of an object type: a class with its decorators, or the properties the checker sees.
  function shapeOfType(t, seen = new Set()) {
    if (!t) return openShape();
    if (t.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown | ts.TypeFlags.NonPrimitive | ts.TypeFlags.TypeParameter | ts.TypeFlags.Index | ts.TypeFlags.IndexedAccess)) return openShape();
    const parts = notNullish(t);
    if (!parts.length) return openShape();
    if (parts.length > 1 || t.isUnion()) {  // alternatives: every property is optional
      const out = emptyShape();
      for (const p of parts) mergeInto(out, allOptional(shapeOfType(p, seen)));
      return out;
    }
    const p = parts[0];
    if (!(p.flags & (ts.TypeFlags.Object | ts.TypeFlags.Intersection))) return openShape();
    if (p.symbol && !(p.getObjectFlags && p.getObjectFlags() & ts.ObjectFlags.Mapped) && !/^__/.test(p.symbol.name) && isDefaultLib(p.symbol)) return openShape();
    const cls = p.symbol && classOf(p.symbol);
    if (cls) return classShape(cls, seen);
    const shape = emptyShape();
    if (hasIndexSignature(p)) shape.open = true;
    for (const sym of checker.getPropertiesOfType(p)) {
      if (sym.flags & ts.SymbolFlags.Method) continue;
      const d = declOf(sym);
      const required = !(sym.flags & ts.SymbolFlags.Optional) && (!d || !(ts.isPropertySignature(d) || ts.isPropertyDeclaration(d)) || declRequired(d));
      let schema = {};
      try { schema = primitiveSchema(checker.getTypeOfSymbolAtLocation(sym, d || sym.declarations[0])); } catch (e) { /* shallow only */ }
      shape.props.set(sym.name, { required, schema });
    }
    if (!shape.props.size && !shape.open) shape.open = true;  // {} says nothing
    return shape;
  }

  function toSchema(shape) {
    const out = { type: 'object', properties: {} };
    const required = [];
    for (const [k, v] of shape.props) {
      out.properties[k] = v.schema;
      if (v.required) required.push(k);
    }
    if (required.length) out.required = required;
    if (shape.open || !shape.props.size) out.additionalProperties = true;
    return out;
  }

  const components = {};
  const componentNames = new Map();
  function componentFor(decl) {
    if (componentNames.has(decl)) return componentNames.get(decl);
    const base = (decl.name && decl.name.text) || 'Anonymous';
    let name = base;
    for (let i = 2; components[name]; i++) name = `${base}_${i}`;
    componentNames.set(decl, name);
    components[name] = toSchema(classShape(decl));
    return name;
  }

  // The JSON schema of a request body's type.
  function bodySchema(t) {
    if (!t) return { type: 'object', additionalProperties: true };
    const parts = notNullish(t);
    if (parts.length === 1) {
      const p = parts[0];
      if (p.flags & (ts.TypeFlags.Any | ts.TypeFlags.Unknown | ts.TypeFlags.NonPrimitive | ts.TypeFlags.TypeParameter)) return { type: 'object', additionalProperties: true };
      if (p.flags & ts.TypeFlags.Object && isArrayType(p)) return { type: 'array', items: {} };
      const prim = primitiveSchema(p);
      if (prim.type) return prim;
      const cls = p.symbol && p.flags & ts.TypeFlags.Object && classOf(p.symbol);
      if (cls && !isDefaultLib(p.symbol)) return { $ref: `#/components/schemas/${componentFor(cls)}` };
    } else if (parts.length > 1 && parts.every((x) => x.flags & ts.TypeFlags.Object && !isArrayType(x))) {
      return { anyOf: parts.map(bodySchema) };
    } else if (parts.length > 1) {
      return primitiveSchema(t).type ? primitiveSchema(t) : { type: 'object', additionalProperties: true };
    }
    return toSchema(shapeOfType(t));
  }

  const typeOfParam = (p) => {
    try { return p.type ? checker.getTypeFromTypeNode(p.type) : checker.getTypeAtLocation(p); } catch (e) { return null; }
  };

  // ----- app settings (the file that calls NestFactory.create) -----

  const callsOf = (sf, test) => {
    const out = [];
    const visit = (n) => {
      if (ts.isCallExpression(n) && test(n)) out.push(n);
      ts.forEachChild(n, visit);
    };
    visit(sf);
    return out;
  };

  const isNestFactoryCreate = (c) => ts.isPropertyAccessExpression(c.expression) && c.expression.name.text === 'create' &&
    ts.isIdentifier(c.expression.expression) && c.expression.expression.text === 'NestFactory';
  const isMethodCall = (name) => (c) => ts.isPropertyAccessExpression(c.expression) && c.expression.name.text === name;

  function appSettings() {
    const settings = { prefix: '', exclude: [], versioning: { uri: false, prefix: 'v', defaults: [null] } };
    const sfs = sourceFilesIn();
    const hosts = sfs.filter((sf) => callsOf(sf, isNestFactoryCreate).length);
    const main = hosts.find((sf) => /^main\.\w+$/.test(path.basename(sf.fileName))) || hosts[0];
    for (const sf of sfs) {
      if (sf === main) continue;
      for (const c of callsOf(sf, (x) => isMethodCall('setGlobalPrefix')(x) || isMethodCall('enableVersioning')(x))) {
        report(c, `${c.expression.name.text} is called outside the file that calls NestFactory.create, so it is not applied`);
      }
    }
    for (const sf of sfs) {
      for (const c of callsOf(sf, (x) => ts.isPropertyAccessExpression(x.expression) && x.expression.name.text === 'register' &&
        ts.isIdentifier(x.expression.expression) && x.expression.expression.text === 'RouterModule')) {
        report(c, 'RouterModule.register is used: the module paths it adds are not applied, so those routes are listed without them');
      }
    }
    if (!main) return settings;
    for (const c of callsOf(main, isMethodCall('setGlobalPrefix'))) {
      const r = c.arguments[0] && strings(c.arguments[0]);
      if (!r || r.length !== 1) { report(c, 'the global prefix is not a constant'); continue; }
      settings.prefix = r[0];
      const opts = c.arguments[1] && stripped(c.arguments[1]);
      const ex = opts && ts.isObjectLiteralExpression(opts) ? propertyOf(opts, 'exclude') : null;
      const list = ex && stripped(ex);
      if (list && ts.isArrayLiteralExpression(list)) {
        for (const el of list.elements) {
          const e = stripped(el);
          if (ts.isObjectLiteralExpression(e)) {
            const p = propertyOf(e, 'path');
            const m = propertyOf(e, 'method');
            const mn = m && stripped(m);
            const methodName = mn && ts.isPropertyAccessExpression(mn) ? mn.name.text : null;
            for (const s of (p && strings(p)) || []) settings.exclude.push({ path: s, methods: methodName && methodName !== 'ALL' ? [REQUEST_METHODS[methodName]].filter(Boolean) : null });
          } else for (const s of strings(e) || []) settings.exclude.push({ path: s, methods: null });
        }
      }
    }
    for (const c of callsOf(main, isMethodCall('enableVersioning'))) {
      const opts = c.arguments[0] && stripped(c.arguments[0]);
      if (!opts || !ts.isObjectLiteralExpression(opts)) { report(c, 'the versioning options are not an object literal'); continue; }
      const type = propertyOf(opts, 'type');
      const tn = type && stripped(type);
      let uri = null;
      if (tn && ts.isPropertyAccessExpression(tn)) uri = tn.name.text === 'URI';
      else if (tn && ts.isNumericLiteral(tn)) uri = tn.text === '0';
      if (uri === null) { report(c, 'the versioning type is not a constant'); continue; }
      const v = settings.versioning;
      v.uri = uri;
      const pre = propertyOf(opts, 'prefix');
      if (pre) {
        const pn = stripped(pre);
        if (pn.kind === ts.SyntaxKind.FalseKeyword) v.prefix = '';
        else v.prefix = (strings(pn) || ['v'])[0];
      }
      const def = propertyOf(opts, 'defaultVersion');
      if (def) { const l = versionList(def); v.defaults = l.length ? l : [null]; }
    }
    return settings;
  }

  // ----- controllers and routes -----

  function baseOf(decl) {
    const ext = (decl.heritageClauses || []).find((h) => h.token === ts.SyntaxKind.ExtendsKeyword);
    const e = ext && ext.types[0] && stripped(ext.types[0].expression);
    return e && (ts.isIdentifier(e) || ts.isPropertyAccessExpression(e)) ? classOf(symbolOf(e)) : null;
  }

  function controllerOf(decl) {
    const d = decoNamed(decl, 'Controller')[0];
    if (!d) return null;
    const ctrl = { decl, name: decl.name ? decl.name.text : 'Controller', paths: [''], versions: null, node: d.node };
    const arg = d.args[0] && stripped(d.args[0]);
    if (arg && ts.isObjectLiteralExpression(arg)) {
      const p = propertyOf(arg, 'path');
      if (p) ctrl.paths = stringList(p, 'the controller path is not a constant');
      const v = propertyOf(arg, 'version');
      if (v) ctrl.versions = versionList(v);
    } else if (arg) {
      ctrl.paths = stringList(arg, 'the controller path is not a constant');
    }
    const ver = decoNamed(decl, 'Version')[0];
    if (ver && ver.args[0]) ctrl.versions = versionList(ver.args[0]);
    return ctrl;
  }

  // Query parameters, the body and path parameters a method declares.
  function readParameters(method) {
    const out = { query: [], queryOpen: false, bodyWhole: null, bodyNamed: {}, upload: false, pathNames: [] };
    for (const p of method.parameters) {
      for (const d of decoratorsOf(p).map(decoInfo)) {
        const first = d.args[0];
        const named = first ? strings(first) : null;
        const typeOf = typeOfParam(p);
        if (d.name === 'Param') {
          if (named) out.pathNames.push(...named);
        } else if (d.name === 'Query') {
          if (named) {
            for (const n of named) out.query.push({ name: n, in: 'query', required: false, schema: primitiveSchema(typeOf) });
          } else {
            if (first && !(checker.getTypeAtLocation(first).flags & ts.TypeFlags.StringLike)) { /* a pipe, not a name */ } else if (first) { report(first, 'the query parameter name is not a constant'); continue; }
            const shape = shapeOfType(typeOf);
            if (shape.open) out.queryOpen = true;
            for (const [k, v] of shape.props) out.query.push({ name: k, in: 'query', required: v.required, schema: v.schema });
          }
        } else if (d.name === 'Body') {
          if (named) {
            for (const n of named) out.bodyNamed[n] = primitiveSchema(typeOf);
          } else if (!first || !(checker.getTypeAtLocation(first).flags & ts.TypeFlags.StringLike)) {
            out.bodyWhole = bodySchema(typeOf);
          } else report(first, 'the body field name is not a constant');
        } else if (d.name === 'UploadedFile' || d.name === 'UploadedFiles') {
          out.upload = true;
        } else if (d.name === 'Req' || d.name === 'Request') {
          out.queryOpen = true;  // the handler holds the raw request: it may read any query key
        }
      }
    }
    return out;
  }

  function requestBodyOf(info) {
    const named = Object.keys(info.bodyNamed).length ? { type: 'object', properties: info.bodyNamed } : null;
    let schema = null;
    if (info.bodyWhole && named) schema = { allOf: [info.bodyWhole, named] };
    else schema = info.bodyWhole || named;
    if (!schema && !info.upload) return null;
    if (info.upload) {
      schema = schema ? { allOf: [schema, { type: 'object', additionalProperties: true }] } : { type: 'object', additionalProperties: true };
    }
    return { required: true, content: { [info.upload ? 'multipart/form-data' : 'application/json']: { schema } } };
  }

  function buildSpec() {
    const settings = appSettings();
    const paths = {};
    let controllers = 0;
    let routes = 0;

    function addRoutes(ctrl, method, memberName) {
      const verbs = [];
      const methodPaths = [];
      for (const d of decoratorsOf(method).map(decoInfo)) {
        if (!HTTP_DECORATORS[d.name]) continue;
        verbs.push(...HTTP_DECORATORS[d.name]);
        const ps = d.args[0] ? stringList(d.args[0], 'the route path is not a constant') : [''];
        methodPaths.push(...(d.args[0] && !ps.length ? [] : ps));
      }
      if (!verbs.length) return;
      const info = readParameters(method);
      const mv = decoNamed(method, 'Version')[0];
      let versions = mv && mv.args[0] ? versionList(mv.args[0]) : ctrl.versions;
      if (!settings.versioning.uri) versions = [null];
      else if (!versions || !versions.length) versions = settings.versioning.defaults;
      const body = requestBodyOf(info);
      for (const cp of ctrl.paths) {
        for (const mp of unique(methodPaths)) {
          const bare = joinPaths([cp, mp]);
          for (const b of bare) {
            const excluded = settings.exclude.some((e) => sameRoute('/' + e.path.replace(/^\/+/, ''), b));
            for (const version of unique(versions)) {
              const segs = [excluded ? '' : settings.prefix, version === null || !settings.versioning.uri ? '' : settings.versioning.prefix + version, b];
              for (const full of [assemble(segs)]) {
                const names = unique([...pathParamNames(full), ...info.pathNames]);
                const parameters = [
                  ...names.map((n) => ({ name: n, in: 'path', required: true, schema: { type: 'string' } })),
                  ...(info.queryOpen ? [] : info.query),
                ];
                const seenQuery = new Set();
                const deduped = parameters.filter((p) => { const k = p.in + p.name; if (seenQuery.has(k)) return false; seenQuery.add(k); return true; });
                for (const verb of unique(verbs)) {
                  const item = paths[full] || (paths[full] = {});
                  if (item[verb]) continue;  // the first controller to declare a route wins
                  item[verb] = {
                    operationId: `${ctrl.name}_${memberName}`, tags: [ctrl.name], parameters: deduped,
                    ...(body && verb !== 'get' && verb !== 'head' ? { requestBody: body } : {}),
                    responses: { 200: { description: '' } }, 'x-source': where(method),
                  };
                  routes++;
                }
              }
            }
          }
        }
      }
    }

    for (const sf of sourceFilesIn()) {
      const visit = (n) => {
        if (ts.isClassDeclaration(n) && !(ts.getCombinedModifierFlags(n) & ts.ModifierFlags.Abstract)) {
          const ctrl = controllerOf(n);
          if (ctrl) {
            controllers++;
            const done = new Set();
            for (let c = n, guard = 0; c && guard < 10; c = baseOf(c), guard++) {  // a base class's routes come with it
              for (const m of c.members) {
                if (!ts.isMethodDeclaration(m)) continue;
                const name = propertyKey(m);
                if (name === null || done.has(name)) continue;
                done.add(name);
                addRoutes(ctrl, m, name);
              }
            }
          }
        }
        ts.forEachChild(n, visit);
      };
      visit(sf);
    }

    const spec = { openapi: '3.0.0', info: { title: 'Routes read from NestJS code', version: '0' }, paths, components: { schemas: components } };
    if (settings.prefix) spec.servers = [{ url: '/' + settings.prefix.replace(/^\/+|\/+$/g, '') }];
    return { spec, summary: { controllers, routes }, unresolved };
  }

  return { buildSpec };
}

// ---------- main ----------

function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const repo = path.resolve(input.repo || '.');
  if (!fs.existsSync(repo)) throw new Error(`no such repo: ${repo}`);
  const ts = loadTs(repo);
  const files = sourceFiles(repo, input.include);
  if (!files.length) throw new Error('no source files found (check "include")');
  const prog = programFor(ts, repo, files);
  const result = analyze(ts, prog, files, repo).buildSpec();
  if (input.out) fs.writeFileSync(path.resolve(repo, String(input.out)), JSON.stringify(result.spec, null, 1));
  return { ok: true, ...result };
}

try {
  process.stdout.write(JSON.stringify(main()));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
}
