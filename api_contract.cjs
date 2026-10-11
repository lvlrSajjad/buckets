#!/usr/bin/env node
// Compare the HTTP calls a TypeScript/JavaScript client makes with a server's OpenAPI/Swagger spec.
//
// Input: one JSON object on stdin:
//   { "repo": "/abs/client", "spec": "/abs/openapi.json",
//     "include": ["src", "app/**/*.ts"],                  // optional; default: every ts/tsx/js/jsx outside tests
//     "call": { "methods": ["get","post","put","patch","delete"],
//               "object_keys": { "method": [...], "url": [...], "params": [...], "path_params": [...] } },
//     "base": "/api/v1",                                // optional; default: from the spec's servers / basePath
//     "hosts": ["api.example.com"] }                      // optional: hosts that are the API's own (others are "external")
// Output: one JSON object on stdout:
//   { ok, calls: [{ file, line, method, path, raw, keys, status, endpoint, unknown_keys, missing_required }],
//     unused_endpoints, summary }
//   loose: true on an ok call whose literal path word was matched to a spec path parameter (probably a missing route)
//   status: ok | no-endpoint | method-mismatch | unresolved | external (an absolute URL on another host)
//
// Calls are found with the TypeScript compiler (constants are followed across imports); nothing is written.
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

const HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete', 'head', 'options'];
const DEFAULT_CALL = {
  methods: ['get', 'post', 'put', 'patch', 'delete'],
  object_keys: {
    method: ['method'],
    url: ['url', 'uri', 'path', 'uriTemplate', 'endpoint'],
    params: ['params', 'data', 'body', 'query'],
    path_params: ['uriParams', 'pathParams'],
  },
};
const CONFIG_KEYS = new Set(['params', 'headers', 'timeout', 'signal', 'auth', 'responseType', 'cancelToken',
  'baseURL', 'withCredentials', 'validateStatus', 'transformResponse', 'transformRequest', 'paramsSerializer']);
const HTTPISH = /api|http|axios|apisauce|client/i;
const NOT_HTTP_TYPES = new Set(['Map', 'WeakMap', 'Set', 'URLSearchParams', 'Headers', 'FormData', 'Storage']);
const SKIP_DIRS = new Set(['node_modules', '.git', 'dist', 'build', 'coverage', '.next', 'Pods', '__tests__', '__mocks__',
  'test', 'tests', 'e2e']);
const SOURCE_EXT = /\.(ts|tsx|js|jsx|mjs|cjs)$/;
const NOT_SOURCE = /\.(d\.ts|test\.\w+|spec\.\w+|stories\.\w+)$/;
const UNKNOWN = '\u0000';  // stands for an interpolated part we can't resolve
const MAX_ALTERNATIVES = 16;

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

function walk(dir, out) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
  for (const e of entries) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (!SKIP_DIRS.has(e.name) && !e.name.startsWith('.')) walk(full, out);
    } else if (SOURCE_EXT.test(e.name) && !NOT_SOURCE.test(e.name)) out.push(full);
  }
}

function sourceFiles(repo, include) {
  const all = [];
  walk(repo, all);
  if (!include || !include.length) return all;
  const rels = all.map((f) => [f, path.relative(repo, f).split(path.sep).join('/')]);
  const tests = include.map((g) => {
    const norm = path.isAbsolute(g) ? path.relative(repo, g) : g.replace(/^\.\//, '');
    if (/[*?{]/.test(norm)) return globToRegex(norm);
    return new RegExp('^' + norm.replace(/\/+$/, '').replace(/[.+^$()|[\]\\]/g, '\\$&') + '(/|$)');
  });
  return rels.filter(([, r]) => tests.some((t) => t.test(r))).map(([f]) => f);
}

// ---------- paths ----------

function splitUrl(url) {
  let host = null;
  const m = /^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)(.*)$/i.exec(url);
  if (m) { host = m[1]; url = m[2]; }
  const hash = url.indexOf('#');
  if (hash >= 0) url = url.slice(0, hash);
  const q = url.indexOf('?');
  return { host, path: q >= 0 ? url.slice(0, q) : url, query: q >= 0 ? url.slice(q + 1) : null };
}

function normalizePath(p) {
  return p.replace(/\{[^}/]*\}/g, '{}').split('/')
    .map((s) => s.replace(/:[A-Za-z_][\w-]*/g, '{}'))
    .filter(Boolean).join('/');
}

const segments = (p) => (p ? p.split('/') : []);

function stripBase(p, base) {
  if (!base) return p;
  if (p === base) return '';
  return p.startsWith(base + '/') ? p.slice(base.length + 1) : p;
}

function stripVersionPrefix(p) {
  const segs = segments(p);
  while (segs.length > 1 && /^(api|v\d+)$/i.test(segs[0])) segs.shift();
  return segs.join('/');
}

// 2: the same word; 1: a parameter or a pattern fits; 0.5: an unresolved part taken for a word; 0: no. Words compare without case (Express and NestJS
// route that way by default); a part of the client's path we couldn't resolve may be any word of the spec's.
function matchSegment(c, s) {
  if (c === s) return c === '{}' ? 1 : 2;
  if (s === '{}') return 1;
  if (c === '{}') return 0.5;
  if (c.toLowerCase() === s.toLowerCase()) return 2;
  const part = c.includes('{}') ? [c, s] : s.includes('{}') ? [s, c] : null;
  if (!part) return 0;
  const re = new RegExp('^' + part[0].split('{}').map((x) => x.replace(/[.+*?^$()|[\]\\]/g, '\\$&')).join('[^/]+') + '$');
  return re.test(part[1]) ? 1 : 0;
}

// ---------- spec ----------

function loadSpec(file) {
  const spec = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!spec.paths || typeof spec.paths !== 'object') throw new Error('the spec has no "paths"');
  return spec;
}

function specBase(spec) {
  let candidates = [];
  if (Array.isArray(spec.servers)) candidates = spec.servers.map((s) => String(s.url || ''));
  else if (spec.basePath) candidates = [String(spec.basePath)];
  for (const c of candidates) {
    const p = normalizePath(splitUrl(c.replace(/\{[^}]*\}/g, 'x')).path);
    if (p) return p;
  }
  return '';
}

function specHosts(spec) {
  const urls = Array.isArray(spec.servers) ? spec.servers.map((s) => String(s.url || '')) : spec.host ? [spec.host] : [];
  return new Set(urls.map((u) => (splitUrl(u).host || u.split('/')[0]).toLowerCase()).filter(Boolean));
}

function deref(spec, node, seen = new Set()) {
  while (node && typeof node.$ref === 'string') {
    if (seen.has(node.$ref) || !node.$ref.startsWith('#/')) return null;
    seen.add(node.$ref);
    node = node.$ref.slice(2).split('/').reduce((o, k) => (o ? o[k.replace(/~1/g, '/').replace(/~0/g, '~')] : undefined), spec);
  }
  return node || null;
}

// Properties of an object schema: names, required names, and whether extra keys are allowed.
function schemaShape(spec, schema, shape = { props: new Set(), required: new Set(), open: false }, seen = new Set()) {
  schema = deref(spec, schema);
  if (!schema || seen.has(schema)) return shape;
  seen.add(schema);
  for (const part of schema.allOf || []) schemaShape(spec, part, shape, seen);
  for (const part of [...(schema.oneOf || []), ...(schema.anyOf || [])]) {
    const sub = schemaShape(spec, part, undefined, seen);
    sub.props.forEach((p) => shape.props.add(p));
    if (sub.open) shape.open = true;
  }
  for (const [name, prop] of Object.entries(schema.properties || {})) {
    shape.props.add(name);
    const p = deref(spec, prop);
    if ((schema.required || []).includes(name) && !(p && p.readOnly)) shape.required.add(name);
  }
  for (const name of schema.required || []) shape.props.add(name);
  if (schema.additionalProperties && schema.additionalProperties !== false) shape.open = true;
  return shape;
}

function queryShape(spec, params) {
  const shape = { props: new Set(), required: new Set(), open: false };
  for (const raw of params) {
    const p = deref(spec, raw);
    if (!p || p.in !== 'query') continue;
    const inner = p.schema ? schemaShape(spec, p.schema) : null;
    if (inner && inner.props.size && !(deref(spec, p.schema) || {}).type) {
      inner.props.forEach((n) => shape.props.add(n));  // an object parameter spread into keys (form style)
      if (p.required) inner.required.forEach((n) => shape.required.add(n));
    } else {
      shape.props.add(p.name);
      if (p.required) shape.required.add(p.name);
    }
  }
  return shape;
}

function bodyShape(spec, op, pathParams) {
  const params = [...(pathParams || []), ...(op.parameters || [])].map((p) => deref(spec, p)).filter(Boolean);
  const swaggerBody = params.find((p) => p.in === 'body');
  const rb = deref(spec, op.requestBody);
  let schema = swaggerBody && swaggerBody.schema;
  if (!schema && rb && rb.content) {
    const media = rb.content['application/json'] || Object.values(rb.content).find((m) => m && m.schema);
    schema = media && media.schema;
  }
  const shape = schema ? schemaShape(spec, schema) : { props: new Set(), required: new Set(), open: false };
  for (const p of params.filter((x) => x.in === 'formData')) {
    shape.props.add(p.name);
    if (p.required) shape.required.add(p.name);
  }
  return shape;
}

function buildEndpoints(spec, base) {
  const out = [];
  for (const [specPath, item] of Object.entries(spec.paths)) {
    if (!item || typeof item !== 'object') continue;
    const norm = stripBase(normalizePath(specPath), base);
    const shared = (item.parameters || []);
    for (const m of HTTP_METHODS) {
      const op = item[m];
      if (!op || typeof op !== 'object') continue;
      out.push({
        id: `${m.toUpperCase()} ${specPath}`, method: m, specPath, segs: segments(norm),
        query: queryShape(spec, [...shared, ...(op.parameters || [])]),
        body: bodyShape(spec, op, shared),
      });
    }
  }
  return out;
}

function findEndpoint(endpoints, method, clientPath, base) {
  const attempts = [stripBase(clientPath, base)];
  const versionless = stripVersionPrefix(attempts[0]);
  if (versionless !== attempts[0]) attempts.push(versionless);
  for (const attempt of attempts) {
    const segs = segments(attempt);
    const hits = [];
    for (const ep of endpoints) {
      if (ep.segs.length !== segs.length) continue;
      let score = 0;
      let ok = true;
      let loose = false;  // a word of the client's path was taken for a path parameter
      for (let i = 0; i < segs.length && ok; i++) {
        const s = matchSegment(segs[i], ep.segs[i]);
        if (!s) ok = false; else score += s === 2 ? 1000 : s;
        if (s === 1 && ep.segs[i] === '{}' && /^[A-Za-z][\w-]{2,}$/.test(segs[i])) loose = true;
        if (s === 0.5) loose = true;  // an unresolved part taken for a word
      }
      if (ok) hits.push({ ep, score, loose });
    }
    if (!hits.length) continue;
    const best = Math.max(...hits.map((h) => h.score));
    const ordered = [...hits].sort((a, b) => b.score - a.score);
    const chosen = ordered.find((h) => h.score === best && h.ep.method === method) || ordered.find((h) => h.ep.method === method);
    return chosen ? { endpoint: chosen.ep, loose: chosen.loose } : { mismatch: [...new Set(hits.map((h) => h.ep))] };
  }
  return {};
}

// ---------- extraction ----------

function makeExtractor(ts, checker, cfg) {
  const stripped = (n) => {
    while (n && (ts.isParenthesizedExpression(n) || ts.isAsExpression(n) || ts.isNonNullExpression(n) ||
                 ts.isTypeAssertionExpression(n) || (ts.isSatisfiesExpression && ts.isSatisfiesExpression(n)))) n = n.expression;
    return n;
  };

  function cross(a, b) {
    if (a.length * b.length > MAX_ALTERNATIVES) return [a[0] + b[0]];
    const out = [];
    for (const x of a) for (const y of b) out.push(x + y);
    return out;
  }

  const unique = (arr) => [...new Set(arr)];

  function symbolOf(node) {
    let sym;
    if (ts.isElementAccessExpression(node)) sym = checker.getSymbolAtLocation(node.argumentExpression);
    else if (ts.isPropertyAccessExpression(node)) sym = checker.getSymbolAtLocation(node.name);
    else if (node.parent && ts.isShorthandPropertyAssignment(node.parent) && node.parent.name === node) {
      sym = checker.getShorthandAssignmentValueSymbol(node.parent);
    } else sym = checker.getSymbolAtLocation(node);
    if (sym && sym.flags & ts.SymbolFlags.Alias) {
      try { sym = checker.getAliasedSymbol(sym); } catch (e) { /* keep the alias */ }
    }
    return sym;
  }

  const declOf = (sym) => sym && (sym.valueDeclaration || (sym.declarations || [])[0]);
  const isConst = (decl) => ts.isVariableDeclaration(decl) && (ts.getCombinedNodeFlags(decl) & ts.NodeFlags.Const) !== 0;

  function literalTypes(node) {
    const t = checker.getTypeAtLocation(node);
    if (!t) return null;
    if (t.isStringLiteral()) return [t.value];
    if (t.isUnion() && t.types.every((x) => x.isStringLiteral())) return t.types.map((x) => x.value).slice(0, MAX_ALTERNATIVES);
    return null;
  }

  function returnsOf(fn) {
    if (!fn.body) return [];
    if (!ts.isBlock(fn.body)) return [fn.body];
    const out = [];
    const visit = (n) => {
      if (ts.isFunctionLike(n)) return;
      if (ts.isReturnStatement(n) && n.expression) out.push(n.expression);
      ts.forEachChild(n, visit);
    };
    ts.forEachChild(fn.body, visit);
    return out;
  }

  function functionOf(decl) {
    if (!decl) return null;
    if (ts.isFunctionLike(decl) && decl.body) return decl;
    if (ts.isVariableDeclaration(decl) && decl.initializer) {
      const init = stripped(decl.initializer);
      if (ts.isArrowFunction(init) || ts.isFunctionExpression(init)) return init;
    }
    return null;
  }

  // Every string an expression can evaluate to, with UNKNOWN for interpolated parts; null when nothing is known.
  function resolve(node, ctx = { env: new Map(), depth: 0, seen: new Set() }) {
    node = stripped(node);
    if (!node || ctx.depth > 8) return null;
    const deeper = { ...ctx, depth: ctx.depth + 1 };
    if (ts.isStringLiteralLike(node) || ts.isNumericLiteral(node)) return [node.text];
    if (ts.isTemplateExpression(node)) {
      let acc = [node.head.text];
      for (const span of node.templateSpans) {
        acc = cross(cross(acc, resolve(span.expression, deeper) || [UNKNOWN]), [span.literal.text]);
      }
      return unique(acc);
    }
    if (ts.isBinaryExpression(node)) {
      const op = node.operatorToken.kind;
      if (op === ts.SyntaxKind.PlusToken) {
        const l = resolve(node.left, deeper);
        const r = resolve(node.right, deeper);
        if (!l && !r) return null;
        return unique(cross(l || [UNKNOWN], r || [UNKNOWN]));
      }
      if (op === ts.SyntaxKind.BarBarToken || op === ts.SyntaxKind.QuestionQuestionToken) {
        const l = resolve(node.left, deeper);
        const r = resolve(node.right, deeper);
        return l || r ? unique([...(l || [UNKNOWN]), ...(r || [UNKNOWN])]) : null;
      }
      return null;
    }
    if (ts.isConditionalExpression(node)) {
      const a = resolve(node.whenTrue, deeper);
      const b = resolve(node.whenFalse, deeper);
      return a || b ? unique([...(a || [UNKNOWN]), ...(b || [UNKNOWN])]) : null;
    }
    if (ts.isIdentifier(node) || ts.isPropertyAccessExpression(node) || ts.isElementAccessExpression(node)) {
      const viaSymbol = fromSymbol(node, deeper);
      if (viaSymbol) return viaSymbol;
    }
    if (ts.isCallExpression(node)) {
      const viaReplace = fromReplace(node, deeper);
      if (viaReplace) return viaReplace;
      const viaCall = fromCall(node, deeper);
      if (viaCall) return viaCall;
    }
    return literalTypes(node);
  }

  function fromSymbol(node, ctx) {
    const sym = symbolOf(node);
    if (!sym) return null;
    const bound = ctx.env.get(sym);
    if (bound) return resolve(bound.node, bound.ctx);
    const decl = declOf(sym);
    if (!decl || ctx.seen.has(decl)) return null;
    const next = { ...ctx, seen: new Set([...ctx.seen, decl]) };
    if (ts.isVariableDeclaration(decl) && isConst(decl) && decl.initializer) return resolve(decl.initializer, next);
    if (ts.isVariableDeclaration(decl) && ts.isIdentifier(decl.name)) return fromMutable(decl, sym, next);
    if ((ts.isPropertyAssignment(decl) || ts.isPropertyDeclaration(decl)) && decl.initializer) return resolve(decl.initializer, next);
    if (ts.isEnumMember(decl)) {
      const v = checker.getConstantValue(decl);
      return typeof v === 'string' ? [v] : null;
    }
    if (ts.isShorthandPropertyAssignment(decl)) {
      const value = checker.getShorthandAssignmentValueSymbol(decl);
      const vd = declOf(value);
      return vd && ts.isVariableDeclaration(vd) && isConst(vd) && vd.initializer ? resolve(vd.initializer, next) : null;
    }
    return null;
  }

  // A `let` that is reassigned in its own function: every value it is given, an assignment that
  // reads the variable itself seeing the value it had at its declaration.
  function fromMutable(decl, sym, ctx) {
    const scope = ts.findAncestor(decl, (n) => ts.isFunctionLike(n) || ts.isSourceFile(n));
    if (!scope) return null;
    const values = [];
    const bound = new Map(ctx.env);
    if (decl.initializer) {
      values.push(decl.initializer);
      bound.set(sym, { node: decl.initializer, ctx });
    }
    const visit = (n) => {
      if (n !== scope && ts.isFunctionLike(n)) return;
      if (ts.isBinaryExpression(n) && ts.isIdentifier(n.left) && checker.getSymbolAtLocation(n.left) === sym) {
        const k = n.operatorToken.kind;
        if (k === ts.SyntaxKind.EqualsToken) values.push(n.right);
        else if (k === ts.SyntaxKind.PlusEqualsToken) values.push(ts.factory.createBinaryExpression(n.left, ts.SyntaxKind.PlusToken, n.right));
      }
      ts.forEachChild(n, visit);
    };
    ts.forEachChild(scope, visit);
    const inner = { ...ctx, env: bound };
    const results = values.map((v) => (v.pos < 0 ? resolvePlus(v, inner) : resolve(v, inner)));
    if (!results.length || results.every((r) => !r)) return null;
    return unique(results.flatMap((r) => r || [UNKNOWN])).slice(0, MAX_ALTERNATIVES);
  }

  // A synthesized `x + y` (from `x += y`) has no source positions, so its parts are resolved by hand.
  function resolvePlus(node, ctx) {
    const l = resolve(node.left, ctx);
    const r = resolve(node.right, ctx);
    return l || r ? unique(cross(l || [UNKNOWN], r || [UNKNOWN])) : null;
  }

  // `TEMPLATE.replace(":id", value)`: the placeholder becomes an interpolated part.
  function fromReplace(node, ctx) {
    const callee = node.expression;
    if (!ts.isPropertyAccessExpression(callee) || !/^replace(All)?$/.test(callee.name.text) || node.arguments.length < 2) return null;
    const target = resolve(callee.expression, ctx);
    const needle = resolve(node.arguments[0], ctx);
    if (!target || !needle || needle.length !== 1 || needle[0].includes(UNKNOWN) || !needle[0]) return null;
    const value = resolve(node.arguments[1], ctx) || [UNKNOWN];
    return unique(target.flatMap((t) => value.map((v) => t.split(needle[0]).join(v))));
  }

  // A call of a small function (a URL builder) is evaluated with its arguments bound to its parameters.
  function fromCall(node, ctx) {
    const sym = symbolOf(ts.isPropertyAccessExpression(node.expression) ? node.expression : node.expression);
    const fn = functionOf(declOf(sym));
    if (!fn || ctx.seen.has(fn)) return null;
    const env = new Map(ctx.env);
    fn.parameters.forEach((p, i) => {
      const psym = ts.isIdentifier(p.name) ? checker.getSymbolAtLocation(p.name) : null;
      if (psym && node.arguments[i]) env.set(psym, { node: node.arguments[i], ctx });
    });
    const inner = { env, depth: ctx.depth + 1, seen: new Set([...ctx.seen, fn]) };
    const results = returnsOf(fn).map((r) => resolve(r, inner));
    if (!results.length || results.every((r) => !r)) return null;
    return unique(results.flatMap((r) => r || [UNKNOWN]));
  }

  // ----- keys -----

  function followConst(node, depth = 0) {
    node = stripped(node);
    if (depth < 4 && node && ts.isIdentifier(node)) {
      const decl = declOf(symbolOf(node));
      if (decl && ts.isVariableDeclaration(decl) && isConst(decl) && decl.initializer) return followConst(decl.initializer, depth + 1);
    }
    return node;
  }

  const literalOf = (node) => {
    const n = node && followConst(node);
    return n && ts.isObjectLiteralExpression(n) ? n : null;
  };

  function propName(p) {
    const n = p.name;
    if (!n) return null;
    if (ts.isIdentifier(n) || ts.isStringLiteralLike(n) || ts.isNumericLiteral(n)) return n.text;
    if (ts.isComputedPropertyName(n)) {
      const r = resolve(n.expression);
      return r && r.length === 1 && !r[0].includes(UNKNOWN) ? r[0] : undefined;
    }
    return null;
  }

  // Names of an object literal's properties; null when a spread or a computed name hides some.
  function keysOf(node) {
    if (!node) return [];
    let n = followConst(node);
    if (ts.isIdentifier(n) && n.text === 'undefined') return [];
    if (n.kind === ts.SyntaxKind.NullKeyword) return [];
    if (ts.isCallExpression(n) && /JSON\.stringify$/.test(n.expression.getText()) && n.arguments[0]) n = followConst(n.arguments[0]);
    if (!ts.isObjectLiteralExpression(n)) return null;
    const keys = [];
    for (const p of n.properties) {
      if (ts.isSpreadAssignment(p)) return null;
      const name = propName(p);
      if (name === undefined || name === null) return null;
      keys.push(name);
    }
    return keys;
  }

  const propNode = (obj, name) => {
    const p = obj.properties.find((x) => (ts.isPropertyAssignment(x) || ts.isShorthandPropertyAssignment(x)) && propName(x) === name);
    return p ? (ts.isPropertyAssignment(p) ? p.initializer : p.name) : null;
  };

  // `{ params: {...}, headers }` as axios-style config: its params/data are the real keys.
  function configShaped(obj, allowData) {
    if (!obj || !obj.properties.length) return false;
    return obj.properties.every((p) => {
      const name = ts.isSpreadAssignment(p) ? null : propName(p);
      return name && (CONFIG_KEYS.has(name) || (allowData && name === 'data'));
    });
  }

  function directKeys(method, args) {
    const [, a1, a2] = args;
    if (method === 'get' || method === 'head') {
      const lit = literalOf(a1);
      if (lit && configShaped(lit, false)) return { query: keysOf(propNode(lit, 'params')), body: [] };
      return { query: keysOf(a1), body: [] };
    }
    if (method === 'delete') {
      const lit = literalOf(a1);
      if (lit && configShaped(lit, true)) return { query: keysOf(propNode(lit, 'params')), body: keysOf(propNode(lit, 'data')) };
      const cfg = literalOf(a2);
      return { query: keysOf(a1), body: cfg ? keysOf(propNode(cfg, 'data')) : a2 ? null : [] };
    }
    const cfg = literalOf(a2);
    return { query: cfg ? keysOf(propNode(cfg, 'params')) : a2 ? null : [], body: keysOf(a1) };
  }

  function merge(lists) {
    return lists.some((l) => l === null) ? null : [...new Set(lists.flat())];
  }

  function objectKeysChannels(method, obj, paramKeys) {
    const present = paramKeys.filter((k) => propNode(obj, k));
    const hidden = obj.properties.some((p) => ts.isSpreadAssignment(p));  // a spread may carry any of them
    const queryish = hidden ? [null] : [];
    const bodyish = hidden ? [null] : [];
    for (const k of present) {
      const node = propNode(obj, k);
      if (method === 'get' || method === 'head' || /param|query/i.test(k)) queryish.push(keysOf(node));
      else bodyish.push(keysOf(node));
    }
    return { query: merge(queryish), body: merge(bodyish) };
  }

  function pathParamKeys(obj, names) {
    const out = [];
    for (const name of names) {
      const arr = literalArray(propNode(obj, name));
      for (const el of arr) {
        const lit = literalOf(el);
        const k = lit && (propNode(lit, 'key') || propNode(lit, 'name'));
        const r = k && resolve(k);
        if (r && r.length === 1 && !r[0].includes(UNKNOWN)) out.push(r[0]);
      }
    }
    return out;
  }

  function literalArray(node) {
    const n = node && followConst(node);
    return n && ts.isArrayLiteralExpression(n) ? [...n.elements] : [];
  }

  // ----- calls -----

  function isPlainContainer(recv) {
    const type = checker.getTypeAtLocation(recv);
    return !!(type && type.symbol && NOT_HTTP_TYPES.has(type.symbol.name));
  }

  function receiverLooksHttp(recv) {
    const text = recv.getText();
    return HTTPISH.test(text.length > 200 ? text.slice(0, 200) : text);
  }

  const looksLikePath = (s) => /^[\w\-.~%{}:@!$&'()*+,;=\u0000/]+(\?.*)?$/.test(s) && s.includes('/');

  function methodsOf(node) {
    const r = node && resolve(node);
    if (!r) return null;
    const ms = unique(r.map((s) => s.toLowerCase()).filter((s) => HTTP_METHODS.includes(s)));
    return ms.length ? ms : null;
  }

  function callsFromObject(call, obj, sf) {
    const keys = cfg.object_keys;
    const urlKey = keys.url.find((k) => propNode(obj, k));
    const methodKey = keys.method.find((k) => propNode(obj, k));
    if (!urlKey || !methodKey) return null;
    const urls = resolve(propNode(obj, urlKey));
    const methods = methodsOf(propNode(obj, methodKey));
    const pathKeys = pathParamKeys(obj, keys.path_params || []);
    if (!methods && !urls) return null;
    const out = [];
    for (const m of methods || [null]) {
      out.push({
        node: call, method: m, urls: urls && urls.map((u) => pathKeys.reduce((s, k) => s.split(k).join('{}'), u)),
        raw: propNode(obj, urlKey).getText(),
        ...(m ? objectKeysChannels(m, obj, keys.params) : { query: null, body: null }),
      });
    }
    return out;
  }

  function callsFromDirect(call) {
    const callee = call.expression;
    let methods;
    let recv;
    if (ts.isPropertyAccessExpression(callee) && cfg.methods.includes(callee.name.text)) {
      methods = [callee.name.text];
      recv = callee.expression;
    } else if (ts.isElementAccessExpression(callee) && !ts.isStringLiteralLike(callee.argumentExpression)) {
      methods = (methodsOf(callee.argumentExpression) || []).filter((m) => cfg.methods.includes(m));
      recv = callee.expression;
      if (!methods.length) return null;
    } else if (ts.isPropertyAccessExpression(callee) && callee.name.text === 'fetch' || ts.isIdentifier(callee) && callee.text === 'fetch') {
      return callsFromFetch(call);
    } else return null;
    if (!call.arguments.length) return null;
    const urlNode = call.arguments[0];
    if (isPlainContainer(recv)) return null;
    const httpish = receiverLooksHttp(recv);
    const urls = resolve(urlNode);
    if (!httpish && !(urls && urls.some(looksLikePath))) return null;
    if (httpish && urls && !urls.some((u) => /\S/.test(u.replace(UNKNOWN, '')))) return null;
    const raw = urlNode.getText();
    if (!urls && methods.length > 1) return [{ node: call, method: methods.join('|'), urls, raw, query: null, body: null }];
    return methods.map((m) => ({ node: call, method: m, urls, raw, ...directKeys(m, call.arguments) }));
  }

  function callsFromFetch(call) {
    const first = call.arguments.length > 1 && methodsOf(call.arguments[0]);
    if (first && first.length === 1 && ts.isStringLiteralLike(stripped(call.arguments[0]))) {  // blob-style fetch(method, url, headers, body)
      const urls = resolve(call.arguments[1]);
      return [{ node: call, method: first[0], urls, raw: call.arguments[1].getText(), query: [], body: first[0] === 'get' ? [] : keysOf(call.arguments[3]) }];
    }
    const init = literalOf(call.arguments[1]);
    const methods = init && propNode(init, 'method') ? methodsOf(propNode(init, 'method')) : ['get'];
    const urls = resolve(call.arguments[0]);
    if (!urls || !methods) return null;
    const bodyNode = init && propNode(init, 'body');
    return methods.map((m) => ({
      node: call, method: m, urls, raw: call.arguments[0].getText(),
      query: [], body: m === 'get' ? [] : keysOf(bodyNode),
    }));
  }

  function callsOf(call, sf) {
    for (const arg of call.arguments) {
      if (!ts.isObjectLiteralExpression(arg)) continue;
      const found = callsFromObject(call, arg, sf);
      if (found) return found;
    }
    return callsFromDirect(call) || [];
  }

  return { callsOf };
}

// ---------- main ----------

function mergeCall(user) {
  const u = user || {};
  const ok = u.object_keys || {};
  return {
    methods: u.methods || DEFAULT_CALL.methods,
    object_keys: { ...DEFAULT_CALL.object_keys, ...ok },
  };
}

function programFor(ts, repo, files) {
  const cfgPath = ts.findConfigFile(repo, ts.sys.fileExists);
  let options = { jsx: ts.JsxEmit.Preserve };
  if (cfgPath) {
    const parsed = ts.parseJsonConfigFileContent(ts.readConfigFile(cfgPath, ts.sys.readFile).config, ts.sys, path.dirname(cfgPath));
    options = parsed.options;
  }
  return ts.createProgram(files, { ...options, allowJs: true, noEmit: true, skipLibCheck: true, types: [] });
}

function evaluate(call, endpoints, base, hosts) {
  const sentQuery = call.query;
  const out = {
    method: call.method ? call.method.toUpperCase() : null, raw: call.raw, path: null,
    keys: null, status: 'unresolved', endpoint: null, unknown_keys: [], missing_required: [],
  };
  const results = [];
  for (const value of call.urls || [null]) {
    const r = { ...out };
    results.push(r);
    if (!value || !call.method || value.startsWith(UNKNOWN) || !value.replace(UNKNOWN, '').trim()) continue;
    const { host, path: rawPath, query } = splitUrl(value.split(UNKNOWN).join('{}'));
    r.path = normalizePath(rawPath);
    if (host) r.host = host;
    let urlKeys = [];
    if (query) {
      const names = query.split('&').filter(Boolean).map((kv) => kv.split('=')[0]);
      urlKeys = names.some((n) => n.includes('{}')) ? null : names;
    }
    const q = sentQuery === null || urlKeys === null ? null : [...new Set([...sentQuery, ...urlKeys])];
    const body = call.body;
    r.keys = q === null || body === null ? null : [...new Set([...q, ...body])];
    const found = findEndpoint(endpoints, call.method, r.path, base);
    if (found.endpoint) {
      const ep = found.endpoint;
      r.status = 'ok';
      r.endpoint = ep.id;
      if (found.loose) r.loose = true;
      r.matched = ep;
      checkKeys(r, ep, q, body);
    } else if (found.mismatch) {
      r.status = 'method-mismatch';
      r.allowed = found.mismatch.map((e) => e.id);
    } else if (host && hosts.size && !hosts.has(host.toLowerCase())) {
      r.status = 'external';
    } else {
      r.status = 'no-endpoint';
    }
  }
  return results;
}

function checkKeys(r, ep, query, body) {
  const unknown = [];
  const missing = [];
  const check = (sent, shape, active) => {
    if (sent === null || !active) return;
    if (shape.props.size && !shape.open) unknown.push(...sent.filter((k) => !shape.props.has(k)));
    missing.push(...[...shape.required].filter((k) => !sent.includes(k)));
  };
  check(query, ep.query, true);
  check(body, ep.body, ep.method !== 'get' && ep.method !== 'head');
  r.unknown_keys = [...new Set(unknown)];
  r.missing_required = [...new Set(missing)];
}

function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const repo = path.resolve(input.repo || '.');
  if (!fs.existsSync(repo)) throw new Error(`no such repo: ${repo}`);
  if (!input.spec) throw new Error('"spec" (path to the OpenAPI/Swagger JSON) is required');
  const spec = loadSpec(path.resolve(repo, input.spec));
  const ts = loadTs(repo);
  const cfg = mergeCall(input.call);
  const base = input.base !== undefined ? normalizePath(String(input.base)) : specBase(spec);
  const endpoints = buildEndpoints(spec, base);
  const hosts = new Set([...specHosts(spec), ...(input.hosts || []).map((h) => String(h).toLowerCase())]);

  const files = sourceFiles(repo, input.include);
  if (!files.length) throw new Error('no source files found (check "include")');
  const prog = programFor(ts, repo, files);
  const checker = prog.getTypeChecker();
  const extractor = makeExtractor(ts, checker, cfg);
  const realRepo = fs.realpathSync(repo);
  const rel = (p) => {
    let r = path.relative(repo, p);
    if (r.startsWith('..')) r = path.relative(realRepo, p);
    return r.split(path.sep).join('/');
  };

  const calls = [];
  const used = new Set();
  const wanted = new Set(files.map((f) => path.resolve(f)));
  for (const sf of prog.getSourceFiles()) {
    if (!wanted.has(path.resolve(sf.fileName))) continue;
    const visit = (node) => {
      if (ts.isCallExpression(node)) {
        for (const raw of extractor.callsOf(node, sf)) {
          const line = sf.getLineAndCharacterOfPosition(raw.node.getStart(sf)).line + 1;
          for (const r of evaluate(raw, endpoints, base, hosts)) {
            if (r.matched) used.add(r.matched.id);
            delete r.matched;
            calls.push({ file: rel(sf.fileName), line, ...r });
          }
        }
      }
      ts.forEachChild(node, visit);
    };
    visit(sf);
  }
  calls.sort((a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : a.line - b.line));

  const unused = endpoints.filter((e) => !used.has(e.id)).map((e) => e.id);
  const count = (s) => calls.filter((c) => c.status === s).length;
  const summary = {
    files: files.length, calls: calls.length, ok: count('ok'), 'no-endpoint': count('no-endpoint'),
    'method-mismatch': count('method-mismatch'), unresolved: count('unresolved'), external: count('external'),
    loose: calls.filter((c) => c.loose).length,
    with_unknown_keys: calls.filter((c) => c.unknown_keys.length).length,
    with_missing_required: calls.filter((c) => c.missing_required.length).length,
    endpoints: endpoints.length, unused_endpoints: unused.length, base,
  };
  return { ok: true, calls, unused_endpoints: unused, summary };
}

try {
  process.stdout.write(JSON.stringify(main()));
} catch (e) {
  process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
}
