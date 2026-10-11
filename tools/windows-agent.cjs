#!/usr/bin/env node
// buckets agent: runs read-only checks (tsc, eslint, jest…) for buckets on the other machine, in its own copy of
// the repo that buckets keeps in sync. No dependencies: Node 22+ and git (on Windows, Git for Windows: commands
// run in its bash, so they read the same as on the Mac).
//
//   node windows-agent.cjs --root C:\buckets\repos --token-file C:\buckets\agent.token [--port 8766]
//
// Every request needs "Authorization: Bearer <token>". Endpoints:
//   GET  /status                      how busy this machine is (CPU, the local model server) and each copy's commit
//   POST /sync {repo, head, bundle?}  bring a copy to `head` (bundle: base64 git bundle with the missing commits;
//                                     a full bundle creates the copy); installs dependencies when the lockfile changed
//   POST /run  {repo, cmd, timeout_s} run a command in the copy; {exit, output, secs}
//   POST /exec {cmd, cwd?, shell?, timeout_s?, detach?}  any command on this machine, for the owner (`buckets win`):
//                                     shell "powershell" (the default on Windows), "bash" or "cmd"; detach starts
//                                     it and returns its pid (servers, long jobs)
//   POST /update {code}               replace this agent with a new version and restart (the start script loops)
// Runs and syncs go one at a time; /exec doesn't wait for them. The copies are this agent's own: it resets them.
"use strict";
const http = require("http");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { spawn, spawnSync } = require("child_process");

const VERSION = 2;
const args = Object.fromEntries(process.argv.slice(2).reduce((out, a, i, all) =>
  a.startsWith("--") ? [...out, [a.slice(2), all[i + 1]]] : out, []));
const PORT = Number(args.port || process.env.BUCKETS_AGENT_PORT || 8766);
const ROOT = path.resolve(args.root || process.env.BUCKETS_AGENT_ROOT || path.join(os.homedir(), "buckets-repos"));
const TOKEN = (args.token || (args["token-file"] ? fs.readFileSync(args["token-file"], "utf8") : "") ||
  process.env.BUCKETS_AGENT_TOKEN || "").trim();
const MODEL_URL = args["model-url"] || "http://127.0.0.1:8080";  // the local llama.cpp server, to know when it's busy
const BASH = process.platform === "win32"
  ? ["C:\\Program Files\\Git\\bin\\bash.exe", "C:\\Program Files (x86)\\Git\\bin\\bash.exe"].find(fs.existsSync)
  : "/bin/bash";

if (!TOKEN || TOKEN.length < 16) {
  console.error("buckets agent: a token of at least 16 characters is needed (--token-file)");
  process.exit(1);
}
if (!BASH) {
  console.error("buckets agent: Git for Windows' bash wasn't found; install Git for Windows");
  process.exit(1);
}
fs.mkdirSync(ROOT, { recursive: true });

// ---------------------------------------------------------------- one job at a time

let queue = Promise.resolve();
let current = null;  // what's running now, for /status
function serial(what, job) {
  const run = queue.then(async () => {
    current = { what, since: Date.now() };
    try { return await job(); } finally { current = null; }
  });
  queue = run.catch(() => {});
  return run;
}

// ---------------------------------------------------------------- busy

let cpu = { busy: 0, total: 0, percent: 0 };
function sampleCpu() {
  const t = os.cpus().reduce((acc, c) => {
    const total = Object.values(c.times).reduce((a, b) => a + b, 0);
    return { busy: acc.busy + total - c.times.idle, total: acc.total + total };
  }, { busy: 0, total: 0 });
  const dBusy = t.busy - cpu.busy, dTotal = t.total - cpu.total;
  cpu = { ...t, percent: dTotal > 0 ? Math.round(100 * dBusy / dTotal) : 0 };
}
sampleCpu();
setInterval(sampleCpu, 2000).unref();

function modelBusy() {
  return new Promise(resolve => {
    const req = http.get(MODEL_URL + "/slots", { timeout: 1500 }, res => {
      let body = "";
      res.on("data", d => body += d);
      res.on("end", () => {
        try {
          const slots = JSON.parse(body);
          resolve(Array.isArray(slots) ? slots.some(s => s.is_processing) : null);
        } catch { resolve(null); }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
  });
}

// ---------------------------------------------------------------- copies

const safeName = name => /^[\w.-]+$/.test(name || "") ? name : null;
const copyOf = name => path.join(ROOT, name);

function git(cwd, ...gitArgs) {
  const r = spawnSync("git", gitArgs, { cwd, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (r.status !== 0) throw new Error(`git ${gitArgs.join(" ")}: ${(r.stderr || r.stdout || "").trim().slice(-500)}`);
  return r.stdout;
}

function head(dir) {
  try { return git(dir, "rev-parse", "HEAD").trim(); } catch { return null; }
}

// What decides whether dependencies need installing again: the lockfile and every package.json.
function depsKey(dir) {
  const h = crypto.createHash("sha1");
  const files = git(dir, "ls-files", "yarn.lock", "package-lock.json", "pnpm-lock.yaml", "package.json", "*/package.json",
    "*/*/package.json").split("\n").filter(Boolean).sort();
  for (const f of files) h.update(f + "\0" + fs.readFileSync(path.join(dir, f)));
  return h.digest("hex");
}

function installCommand(dir) {
  if (fs.existsSync(path.join(dir, "yarn.lock"))) return "corepack yarn install";
  if (fs.existsSync(path.join(dir, "pnpm-lock.yaml"))) return "corepack pnpm install --frozen-lockfile";
  if (fs.existsSync(path.join(dir, "package-lock.json"))) return "npm ci";
  return null;
}

async function sync({ repo, head: want, bundle }) {
  const name = safeName(repo);
  if (!name || !/^[0-9a-f]{40}$/.test(want || "")) return { status: 400, body: { error: "repo and a full commit id are needed" } };
  const dir = copyOf(name);
  const file = bundle ? path.join(os.tmpdir(), `buckets-${name}-${Date.now()}.bundle`) : null;
  try {
    if (file) fs.writeFileSync(file, Buffer.from(bundle, "base64"));
    if (!fs.existsSync(path.join(dir, ".git"))) {
      if (!file) return { status: 409, body: { need: "full", error: `no copy of ${name} yet` } };
      git(ROOT, "clone", "-q", "--no-checkout", file, name);
      git(dir, "config", "core.autocrlf", "false");  // the same bytes as the Mac, or eslint and tests disagree
    } else if (file) {
      git(dir, "fetch", "-q", file, "+refs/heads/*:refs/remotes/mac/*");
    }
    try { git(dir, "cat-file", "-e", `${want}^{commit}`); } catch {
      return { status: 409, body: { need: "bundle", have: head(dir), error: `${want.slice(0, 10)} isn't here yet` } };
    }
    git(dir, "checkout", "-q", "--force", "--detach", want);
    git(dir, "clean", "-qfd");  // untracked files go; ignored ones (node_modules) stay
    const key = depsKey(dir), stamp = path.join(dir, ".git", "buckets-deps");
    let installed = null;
    if ((fs.existsSync(stamp) ? fs.readFileSync(stamp, "utf8") : "") !== key) {
      const cmd = installCommand(dir);
      if (cmd) {
        installed = await runIn(dir, cmd, 3600);
        if (installed.exit !== 0) return { status: 500, body: { error: "installing dependencies failed", ...installed } };
      }
      fs.writeFileSync(stamp, key);
    }
    return { status: 200, body: { ok: true, head: head(dir), installed: installed && { secs: installed.secs } } };
  } finally {
    if (file) fs.rmSync(file, { force: true });
  }
}

function runIn(dir, cmd, timeoutS) {
  return new Promise(resolve => {
    const started = Date.now();
    const child = spawn(BASH, ["-lc", cmd], { cwd: dir, env: { ...process.env, CI: "1", FORCE_COLOR: "0" },
      detached: process.platform !== "win32" });
    let out = "";
    const keep = d => { out = (out + d).slice(-200000); };
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"]);
      else try { process.kill(-child.pid, "SIGKILL"); } catch {}
    }, Math.max(1, timeoutS) * 1000);
    child.on("close", code => {
      clearTimeout(timer);
      resolve({ exit: timedOut ? 124 : code, timed_out: timedOut, output: out.slice(-20000),
                secs: Math.round((Date.now() - started) / 100) / 10 });
    });
  });
}

function shellFor(shell) {
  if (shell === "bash") return [BASH, ["-lc"]];
  if (process.platform !== "win32") return ["/bin/bash", ["-lc"]];
  if (shell === "cmd") return ["cmd.exe", ["/d", "/s", "/c"]];
  return ["powershell.exe", ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]];
}

function exec({ cmd, cwd, shell, timeout_s, detach }) {
  if (typeof cmd !== "string" || !cmd.trim()) return Promise.resolve({ status: 400, body: { error: "cmd is needed" } });
  const where = cwd ? path.resolve(cwd) : os.homedir();
  if (!fs.existsSync(where)) return Promise.resolve({ status: 400, body: { error: `no folder ${where}` } });
  const [exe, pre] = shellFor(shell);
  if (detach) {
    const log = path.join(os.tmpdir(), `buckets-exec-${Date.now()}.log`);
    const fd = fs.openSync(log, "a");
    const child = spawn(exe, [...pre, cmd], { cwd: where, detached: true, stdio: ["ignore", fd, fd], windowsHide: true });
    child.unref();
    return Promise.resolve({ status: 200, body: { pid: child.pid, log } });
  }
  return new Promise(resolve => {
    const started = Date.now();
    const child = spawn(exe, [...pre, cmd], { cwd: where, windowsHide: true, detached: process.platform !== "win32" });
    let out = "";
    const keep = d => { out = (out + d).slice(-200000); };
    child.stdout.on("data", keep);
    child.stderr.on("data", keep);
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      if (process.platform === "win32") spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"]);
      else try { process.kill(-child.pid, "SIGKILL"); } catch {}
    }, Math.max(1, Number(timeout_s) || 300) * 1000);
    child.on("close", code => {
      clearTimeout(timer);
      resolve({ status: 200, body: { exit: timedOut ? 124 : code, timed_out: timedOut, output: out.slice(-50000),
                                     secs: Math.round((Date.now() - started) / 100) / 10 } });
    });
  });
}

function update({ code }) {
  if (typeof code !== "string" || !code.includes("buckets agent")) return { status: 400, body: { error: "not an agent" } };
  const next = __filename.replace(/\.cjs$/, "") + ".next.cjs";  // node checks only files it knows the kind of
  fs.writeFileSync(next, code);
  const check = spawnSync(process.execPath, ["--check", next], { encoding: "utf8" });
  if (check.status !== 0) {
    fs.rmSync(next, { force: true });
    return { status: 400, body: { error: "the new version doesn't parse", detail: (check.stderr || "").slice(-500) } };
  }
  fs.copyFileSync(__filename, __filename + ".previous");
  fs.renameSync(next, __filename);
  setTimeout(() => process.exit(0), 300);  // the start script starts it again
  return { status: 200, body: { ok: true, restarting: true } };
}

async function run({ repo, cmd, timeout_s }) {
  const name = safeName(repo);
  if (!name || typeof cmd !== "string" || !cmd.trim()) return { status: 400, body: { error: "repo and cmd are needed" } };
  const dir = copyOf(name);
  if (!fs.existsSync(path.join(dir, ".git"))) return { status: 409, body: { need: "full", error: `no copy of ${name}` } };
  return { status: 200, body: { head: head(dir), ...(await runIn(dir, cmd, Number(timeout_s) || 900)) } };
}

// ---------------------------------------------------------------- the server

function authorized(req) {
  const given = Buffer.from(String(req.headers.authorization || "").replace(/^Bearer /, ""));
  const want = Buffer.from(TOKEN);
  return given.length === want.length && crypto.timingSafeEqual(given, want);
}

function send(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(text) });
  res.end(text);
}

function readBody(req, limit = 512 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const parts = [];
    req.on("data", d => {
      size += d.length;
      if (size > limit) { reject(new Error("too large")); req.destroy(); } else parts.push(d);
    });
    req.on("end", () => {
      try { resolve(parts.length ? JSON.parse(Buffer.concat(parts).toString("utf8")) : {}); } catch (e) { reject(e); }
    });
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  if (!authorized(req)) return send(res, 401, { error: "unauthorized" });
  try {
    const url = new URL(req.url, "http://agent");
    if (req.method === "GET" && url.pathname === "/status") {
      const copies = {};
      for (const name of fs.readdirSync(ROOT)) if (fs.existsSync(path.join(ROOT, name, ".git"))) copies[name] = head(copyOf(name));
      return send(res, 200, { ok: true, version: VERSION, platform: process.platform, cpu_percent: cpu.percent,
        cpus: os.cpus().length, free_mem_gb: Math.round(os.freemem() / 1e8) / 10, model_busy: await modelBusy(),
        running: current && { ...current, secs: Math.round((Date.now() - current.since) / 1000) }, copies });
    }
    if (req.method === "POST" && url.pathname === "/sync") {
      const body = await readBody(req);
      const r = await serial(`sync ${body.repo}`, () => sync(body));
      return send(res, r.status, r.body);
    }
    if (req.method === "POST" && url.pathname === "/exec") {
      const r = await exec(await readBody(req, 1024 * 1024));
      return send(res, r.status, r.body);
    }
    if (req.method === "POST" && url.pathname === "/update") {
      const r = update(await readBody(req, 8 * 1024 * 1024));
      return send(res, r.status, r.body);
    }
    if (req.method === "POST" && url.pathname === "/run") {
      const body = await readBody(req, 1024 * 1024);
      const r = await serial(`run ${String(body.cmd).slice(0, 60)}`, () => run(body));
      return send(res, r.status, r.body);
    }
    return send(res, 404, { error: "not found" });
  } catch (e) {
    return send(res, 500, { error: String(e.message || e).slice(0, 500) });
  }
});

server.listen(PORT, args.host || "0.0.0.0", () => {
  console.log(`buckets agent ${VERSION} on port ${PORT}, copies in ${ROOT}`);
});
