#!/usr/bin/env python3
"""buckets: empty the pool with many small buckets.

A local orchestrator for two machines. A planner model reads the repo, splits the
job into one-file tasks and reviews the result; a coder model runs each task in its
own opencode session. The script owns the control flow, git and the checks; the
models only make judgments, and always answer in JSON the script validates.

    buckets "how does scope resolution work?"      # auto: info or code
    buckets --do "add a sub() helper with tests"   # force code mode
    buckets --ask "where are permissions cached?"  # force info mode
    buckets --doctor                               # check servers, models, checks
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path, PurePosixPath

VERSION = "0.1.0"

CONFIG_PATH = Path(os.environ.get("BUCKETS_CONFIG", Path.home() / ".config/buckets/config.json"))
STATE_DIR = Path(os.environ.get("BUCKETS_STATE", Path.home() / ".local/state/buckets"))

DEFAULT_CONFIG = {
    # Model roles. Each points at an Ollama server ("url") and a model there; "label" is what the
    # machine is called in messages ("this machine" for localhost, else its host name). Run
    # `buckets setup` to write your own config for your machines and models.
    "planner": {                    # the advisor: a stronger model for very hard or stuck jobs
        "url": "http://localhost:11434",
        "model": "qwen3.6:27b-q4_K_M",
        "num_ctx": 65536,
        "think": True,
        "think_budget": 2500,
        "temperature": 0.3,
    },
    "thinker": {                    # plans and reviews by default
        "url": "http://localhost:11434",
        "model": "qwen3.6:35b-a3b",
        "opencode_model": "buckets_local/qwen3.6:35b-a3b",
        "num_ctx": 65536,
        "think": True,
        "think_budget": 6000,
        "temperature": 0.3,
    },
    "coder": {                      # writes the code, through opencode
        "url": "http://localhost:11434",
        "model": "qwen3.6:35b-a3b",
        "opencode_model": "buckets_local/qwen3.6:35b-a3b",
        "opencode_agent": "buckets-coder",
        "timeout_s": 900,
        "attempts": 2,
    },
    "reader": {                     # chores and summarizing code
        "url": "http://localhost:11434",
        "model": "qwen3.6:35b-a3b",
        "num_ctx": 65536,
        "temperature": 0.2,
    },
    "claude": {
        "bin": "claude",
        "model": "sonnet",
        "classify_model": "haiku",
        "budget_usd": 3.0,
        "timeout_s": 1800,
    },
    "mode": "auto",
    "opus_policy": "ask",
    "repos": {},                    # e.g. {"~/code/oss/*": {"mode": "local"}} keeps those repos free
    "reading": "direct",            # "research" for question rounds (worse on bug hunts so far)
    "targeted_min_lines": 400,      # files this long are never read whole: outline, then the exact parts
    "light_think_budget": 1500,     # thinking tokens for planning and reviewing light jobs (~40s on the worker)
    "research": {
        "rounds": 3,
        "questions": 5,
        "reads": 6,
        "think_budget": 1500,
        "errands_per_round": 8,
        "budget_tokens": 30000,
        "errand_timeout_s": 600,
        "opencode_agent": "buckets-reader",
    },
    "offload": {
        "chores": True,             # request routing, file picking, condensing: on the reader model
        "survey": "windows-first",  # the reader's machine reads code first; the advisor's helps with 3+ batches
        "light_jobs": True,         # light jobs are planned and reviewed by the thinker
        "unload_mac": True,         # unload the advisor's model when a run ends (frees its memory)
        "planner": "worker",        # "worker": the thinker plans; "advisor": the planner model above plans
        "escalate_very_hard": True, # very hard jobs go to the advisor
        "escalate_when_stuck": True,  # a round with no progress on failing checks hands review to the advisor
        "escalate_unsure_research": True,  # research ends with no confirmed cause: the advisor plans
        "escalate_repeated_failure": True,  # the coder fails a task twice: the advisor rewrites it
        "escalate_review_disagreement": True,  # reviewer and checks disagree: the advisor reviews
        "double_review": True,      # the worker reviews too, at the same time; a lone "undefined"-style claim is confirmed
        "second_opinion": ["*auth*", "*password*", "*secret*", "*token*", "*crypto*", "*payment*",
                           "*billing*", "*migration*"],  # changed files like these get an advisor review
        "max_escalations": 2,       # per run
        "claude_ladder": True,      # after the advisor, Claude, where Claude is allowed for the repo
    },
    "notify": True,
    "gh_bin": "gh",
    "git": {"remote": "origin", "commit_trailer": ""},
    "opencode_bin": "opencode",
    "max_rounds": 3,
    "max_tasks": 8,
    "max_files": 24,
    "file_budget_tokens": 28000,
    "batch_tokens": 18000,
    "max_file_chars": 60000,
    "map_max_entries": 500,
    "diff_budget_chars": 60000,
    "check_output_chars": 8000,
    "example_chars": 24000,
    "check_timeout_s": 900,
    "request_timeout_s": 2400,
}

LOCKFILES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "poetry.lock",
    "uv.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "composer.lock", "Gemfile.lock",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".icns", ".pdf", ".zip", ".gz", ".tgz", ".tar",
    ".mcpb", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".mp3", ".wav", ".so", ".dylib",
    ".dll", ".exe", ".bin", ".jar", ".class", ".pyc", ".o", ".a", ".wasm", ".sqlite", ".db",
}
GUIDANCE_FILES = ["AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"]


# ---------------------------------------------------------------- output


_status_on = False
_status_lock = threading.Lock()
_LOG: Path | None = None  # the run's plain-text log; set when a run starts
VERBOSE = False

STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "magenta": "35", "cyan": "36"}


def color_on() -> bool:
    return sys.stderr.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"


def paint(text: str, *styles: str) -> str:
    if not styles or not color_on():
        return text
    return f"\x1b[{';'.join(STYLES[s] for s in styles)}m{text}\x1b[0m"


def ok(text: str = "✓") -> str:
    return paint(text, "green")


def bad(text: str = "✗") -> str:
    return paint(text, "red")


def warn(text: str = "⚠") -> str:
    return paint(text, "yellow")


def money(usd: float) -> str:
    return paint(f"${usd:.2f}", "magenta")


def clear_status() -> None:
    global _status_on
    with _status_lock:
        if _status_on:
            sys.stderr.write("\r\x1b[K")
            sys.stderr.flush()
            _status_on = False


def status(text: str) -> None:
    global _status_on
    if sys.stderr.isatty():
        with _status_lock:
            sys.stderr.write("\r\x1b[K" + text[: shutil.get_terminal_size().columns - 1])
            sys.stderr.flush()
            _status_on = True


_T0 = time.time()


def elapsed(since: float | None = None) -> str:
    secs = int(time.time() - (_T0 if since is None else since))
    m, s = divmod(secs, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _log(line: str) -> None:
    if _LOG:
        with open(_LOG, "a") as f:
            f.write(strip_ansi(line) + "\n")


def say(msg: str) -> None:
    clear_status()
    m, s = divmod(int(time.time() - _T0), 60)
    print(f"{paint(f'[{m:02d}:{s:02d}]', 'dim')} {msg}", file=sys.stderr, flush=True)
    _log(f"[{m:02d}:{s:02d}] {msg}")


def detail(msg: str) -> None:
    """Technical detail (token counts, timings): always in the run log, on screen with --verbose."""
    if VERBOSE:
        say(paint(msg, "dim"))
    else:
        _log(f"        {msg}")


def heading(n: int, total: int, title: str) -> None:
    clear_status()
    print(file=sys.stderr)
    say(paint(f"Step {n}/{total} · {title}", "bold", "cyan"))


class Ticker:
    """A status line whose clock keeps running while we wait on a model or a coder session."""

    def __init__(self, text: str):
        self.text, self.t0 = text, time.time()
        self._stop, self._thread = threading.Event(), None

    def __enter__(self):
        if sys.stderr.isatty():
            status(f"{self.text} · {elapsed(self.t0)}")
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self):
        while not self._stop.wait(1):
            status(f"{self.text} · {elapsed(self.t0)}")

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        clear_status()


FRIENDLY = [("route", "Reading your request"), ("pick-dirs", "Choosing folders to read"),
            ("pick-files", "Choosing files to read"), ("survey", "Reading code"),
            ("research", "Planner is deciding what to look up"), ("condense", "Condensing notes"),
            ("plan", "Planner is planning"), ("answer", "Planner is writing the answer"),
            ("second-review", "The second reviewer is checking the work"),
            ("confirm", "The other reviewer is checking a claim"), ("review", "Planner is reviewing the work"),
            ("conflict", "Planner is working out the conflict")]


def friendly(label: str) -> str:
    return next((text for key, text in FRIENDLY if label.startswith(key)), label)


def notify(title: str, message: str) -> None:
    """A macOS notification, so a long run's end isn't missed while you're in another window."""
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return
    quote = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(["osascript", "-e", f'display notification "{quote(message)}" with title "{quote(title)}"'],
                   capture_output=True)


def die(msg: str) -> None:
    clear_status()
    print(f"buckets: {msg}", file=sys.stderr)
    sys.exit(1)


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


# ---------------------------------------------------------------- config


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    try:
        return deep_merge(DEFAULT_CONFIG, json.loads(CONFIG_PATH.read_text()))
    except json.JSONDecodeError as e:
        die(f"{CONFIG_PATH} is not valid JSON: {e}")


def path_rules(cfg: dict, repo: Path) -> dict:
    """Settings from the config's "repos" globs (e.g. everything under ~/Coding/ME is local-only)."""
    merged: dict = {}
    for pattern, rule in (cfg.get("repos") or {}).items():
        if fnmatch.fnmatch(str(repo), os.path.expanduser(pattern)):
            merged.update(rule)
    return merged


def repo_config(repo: Path) -> dict:
    p = repo / ".buckets.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        die(f"{p} is not valid JSON: {e}")


class Run:
    """One invocation's working folder: every model call, coder log and the report."""

    def __init__(self, repo: Path):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = STATE_DIR / "runs" / f"{repo.name}-{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = 0
        self.talked = False  # a local model answered during this run (so one is up)

    _lock = threading.Lock()

    def save(self, name: str, content) -> Path:
        with self._lock:  # survey batches save from two threads
            self.n += 1
            p = self.dir / f"{self.n:02d}-{name}"
        p.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
        return p


# ---------------------------------------------------------------- processes and git


def run_proc(cmd, cwd: Path, timeout: float, shell: bool = False,
             input: str | None = None, on_line=None) -> tuple[int, str, bool]:
    """Run a command in its own process group; kill the whole group on timeout.
    With `on_line`, output is read as it arrives and each line is passed to it (for live progress)."""
    if on_line is not None:
        return _run_streaming(cmd, cwd, timeout, on_line)
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, text=True, errors="replace",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(input=input, timeout=timeout)
        return proc.returncode, out, False
    except KeyboardInterrupt:
        # The child has its own session, so Ctrl-C doesn't reach it; stop it ourselves.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
                proc.wait(timeout=5)
                break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                continue
        raise
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                out, _ = proc.communicate(timeout=10)
                break
            except subprocess.TimeoutExpired:
                out = ""
        return -1, out or "", True


def _kill_group(proc) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
            proc.wait(timeout=5)
            return
        except ProcessLookupError:
            return
        except subprocess.TimeoutExpired:
            continue


def _run_streaming(cmd, cwd: Path, timeout: float, on_line) -> tuple[int, str, bool]:
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
    lines: list[str] = []

    def pump():
        for line in proc.stdout:
            lines.append(line)
            try:
                on_line(line)
            except Exception:  # progress display must never break the run
                pass

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except KeyboardInterrupt:
        _kill_group(proc)
        raise
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        reader.join(5)
        return -1, "".join(lines), True
    reader.join(5)
    return proc.returncode, "".join(lines), False


ACTION_WORDS = {"read": "reading", "edit": "editing", "write": "writing", "grep": "searching",
                "glob": "looking for files", "list": "listing", "patch": "editing"}


def coder_action(line: str) -> str | None:
    """What an opencode session is doing, from its `→ Read x` / `← Edit x` lines."""
    line = strip_ansi(line).strip()
    if line.startswith("✗"):
        return "a step failed; it's trying again"
    m = re.match(r"^[→←]\s+(\w+)\s*(.*)$", line)
    if not m:
        return None
    verb, rest = m.group(1).lower(), m.group(2).strip()
    return f"{ACTION_WORDS.get(verb, verb)} {rest}".strip()[:90]


def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def dirty_tree(repo: Path) -> str:
    """Why a job can't start: which files are uncommitted, and the git chores that clear them."""
    paths = sorted({p.split("/")[0] + ("/" if "/" in p else "") for p in changed_paths(repo)})
    shown = ", ".join(paths[:6]) + (f" and {len(paths) - 6} more" if len(paths) > 6 else "")
    return (f"the working tree has uncommitted changes ({shown}); commit or stash them first, e.g. "
            f'buckets "commit everything" or buckets "add __pycache__/ to .gitignore"')


def changed_paths(repo: Path) -> list[str]:
    """Paths that differ from HEAD, including untracked files, as repo-relative paths. buckets' own
    notes (.buckets/) don't count: updating them mustn't block the next run."""
    return [p for p in all_changed_paths(repo) if not p.startswith(MEMORY_DIR + "/")]


def all_changed_paths(repo: Path) -> list[str]:
    raw = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    parts, out, i = raw.split("\0"), [], 0
    while i < len(parts):
        entry = parts[i]
        if len(entry) > 3:
            out.append(entry[3:])
            if entry[0] in "RC":  # renames carry the old path as the next field
                out.append(parts[i + 1])
                i += 1
        i += 1
    return out


def require_identity(repo: Path) -> None:
    """Commits need a git identity; say so up front instead of failing mid-run."""
    if subprocess.run(["git", "var", "GIT_COMMITTER_IDENT"], cwd=repo, capture_output=True).returncode != 0:
        die(f"git has no user.name/user.email for {repo}; set them (git config user.name …) and try again")


def in_head(repo: Path, path: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"], cwd=repo, capture_output=True).returncode == 0


def revert_paths(repo: Path, paths: list[str]) -> None:
    for p in paths:
        if in_head(repo, p):
            git(repo, "checkout", "HEAD", "--", p)
        else:
            (repo / p).unlink(missing_ok=True)


def snapshot(repo: Path, paths) -> dict[str, bytes | None]:
    """Contents of `paths` right now (None for a missing file), to compare and restore later."""
    out: dict[str, bytes | None] = {}
    for p in paths:
        f = repo / p
        out[p] = f.read_bytes() if f.is_file() else None
    return out


def touched_since(repo: Path, before: dict) -> list[str]:
    """Paths whose content changed since `before`; paths not in it count if they now differ from HEAD."""
    now = set(changed_paths(repo)) | set(before)
    out = []
    for p in sorted(now):
        f = repo / p
        current = f.read_bytes() if f.is_file() else None
        if p in before:
            if current != before[p]:
                out.append(p)
        else:
            out.append(p)
    return out


def restore(repo: Path, before: dict, paths: list[str]) -> None:
    """Put `paths` back: to the snapshot if they were in it, otherwise to HEAD."""
    for p in paths:
        if p not in before:
            revert_paths(repo, [p])
            continue
        f = repo / p
        if before[p] is None:
            f.unlink(missing_ok=True)
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(before[p])


MARKER = re.compile(r"^(<{7}|={7}|>{7})(\s|$)")


def marker_lines(path: Path) -> list[int]:
    """Line numbers that still hold conflict markers."""
    if not path.is_file():
        return []
    return [i for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1) if MARKER.match(line)]


def expand_commits(repo: Path, refs: list[str]) -> list[tuple[str, str]]:
    """Commits and ranges (a..b) to (sha, subject) in apply order. Merge commits are refused."""
    out: list[tuple[str, str]] = []
    for ref in refs:
        if ".." in ref:
            r = subprocess.run(["git", "rev-list", "--reverse", ref], cwd=repo, capture_output=True, text=True)
            shas = r.stdout.split()
            if r.returncode != 0:
                die(f"{ref}: not a commit range in this repo")
        else:
            r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=repo,
                               capture_output=True, text=True)
            if r.returncode != 0:
                die(f"{ref}: not a commit in this repo")
            shas = [r.stdout.strip()]
        for sha in shas:
            if len(git(repo, "rev-list", "--parents", "-n", "1", sha).split()) > 2:
                die(f"{sha[:10]} is a merge commit; pick the commits it merged instead, or do this one by hand")
            if sha not in (c for c, _ in out):
                out.append((sha, git(repo, "log", "-1", "--format=%s", sha).strip()))
    return out


def valid_examples(repo: Path, refs: list[str]) -> list[str]:
    """Keep only examples that are files in the repo, commits or ranges; drop the rest quietly."""
    keep = []
    for ref in refs:
        ref = ref.strip().strip("`")
        if not ref:
            continue
        if (repo / clean_path(ref)).exists() and clean_path(ref):
            keep.append(clean_path(ref).rstrip("/"))
            continue
        ends = ref.split("..") if ".." in ref else [ref]
        if all(subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{e}^{{commit}}"], cwd=repo,
                              capture_output=True).returncode == 0 for e in ends if e):
            keep.append(ref)
    return keep


TOOL_LINE = re.compile(r"^(>|→|⚙|✗|✓|Error:)")
REF = re.compile(r"([\w@.+\-/]+\.[A-Za-z]\w*):(\d+)(?:\s*[-–]\s*(\d+))?")


def errand_answer(out: str, limit: int = 3000) -> str:
    """The answer text from an `opencode run` transcript: drop tool-call lines, stop at END."""
    lines = []
    for line in strip_ansi(out).splitlines():
        if line.strip() == "END":
            break
        if TOOL_LINE.match(line.strip()):
            continue
        lines.append(line)
    text = "\n".join(lines).strip()
    return text[:limit] + ("\n… (cut off)" if len(text) > limit else "")


def ground_truth(answer: str, repo: Path, files, max_refs: int = 12, max_lines: int = 40) -> str:
    """The real lines behind every `path:line` the helper cited, read from disk, so the planner
    never depends on the helper's quoting. References to files that don't exist are flagged."""
    out, seen = [], set()
    for m in REF.finditer(answer):
        if len(seen) >= max_refs:
            break
        path, start = clean_path(m.group(1)), int(m.group(2))
        end = int(m.group(3)) if m.group(3) else start
        if path not in files:
            hits = [f for f in files if f.endswith("/" + path)]
            if len(hits) != 1:
                if (path, start) not in seen:
                    seen.add((path, start))
                    out.append(f"!! `{path}:{start}` cited, but no such file in the repository")
                continue
            path = hits[0]
        if (path, start) in seen:
            continue
        seen.add((path, start))
        lines = (repo / path).read_text(errors="replace").splitlines()
        if start > len(lines):
            out.append(f"!! `{path}:{start}` cited, but the file has only {len(lines)} lines")
            continue
        lo, hi = max(1, start - 2), min(len(lines), max(end, start) + 2, start + max_lines)
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(lo, hi + 1))
        out.append(f"--- {path}:{lo}-{hi} (from disk) ---\n{body}")
    return "\n".join(out)


SPEC = re.compile(r"^(.+?)(?::(\d+)(?:\s*[-–]\s*(\d+))?)?$")


def read_spec(repo: Path, files, spec: str, max_chars: int, max_lines: int = 400) -> str:
    """A planner `read` request ("path" or "path:start-end"), served exactly from disk."""
    m = SPEC.match(clean_path(spec))
    if not m:
        return f"!! can't read `{spec}`"
    path, start, end = m.group(1), m.group(2), m.group(3)
    if path not in files:
        hits = [f for f in files if f.endswith("/" + path)]
        if len(hits) != 1:
            return f"!! `{path}`: no such file in the repository" + (" (ambiguous)" if hits else "")
        path = hits[0]
    if not start:
        return numbered(repo, path, max_chars)
    lines = (repo / path).read_text(errors="replace").splitlines()
    lo = max(1, int(start))
    hi = min(len(lines), int(end) if end else lo, lo + max_lines - 1)
    if lo > len(lines):
        return f"!! `{path}` has only {len(lines)} lines"
    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(lo, hi + 1))
    return f"=== {path}:{lo}-{hi} ===\n{body}"[:max_chars]


def split_where(where: list[str], size: int = 4) -> list[list[str]]:
    """Big look-in lists become several errands ("audit part 1 of n") so each session stays small."""
    where = [w for w in where if w]
    if len(where) <= size:
        return [where]
    return [where[i:i + size] for i in range(0, len(where), size)]


def slug(text: str, n: int = 32) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:n].rstrip("-") or "task"


# ---------------------------------------------------------------- model calls


class ModelError(RuntimeError):
    pass


class ThinkingCut(Exception):
    """The model used up its thinking budget before answering; carries what it thought so far."""

    def __init__(self, thinking: str, tokens: int, looped: bool = False):
        super().__init__(f"thinking stopped after {tokens} tokens")
        self.thinking, self.tokens, self.looped = thinking, tokens, looped


CODE_LINE = re.compile(r"""[{}()\[\];=]|^\s*(def|class|import|from|return|if|for|const|let|function)\b|["'`].*["'`]""")


def thinking_loops(text: str) -> bool:
    """Qwen sometimes thinks in circles, writing the same sentences again and again. True when the
    latest stretch of its prose already appeared earlier, or one long sentence has come up three
    times. Quoted code doesn't count: re-reading the same line of code while reasoning about it is
    work, not a loop (a review was cut at 400 tokens for quoting one line twice)."""
    prose = re.sub(r"```.*?(```|$)", " ", text, flags=re.S)
    prose = "\n".join(l for l in prose.splitlines() if not CODE_LINE.search(l))
    if len(prose) < 3000:
        return False
    tail = prose[-200:]
    if tail.strip() and tail in prose[:-200]:
        return True
    sentences = [x.strip().lower() for x in re.split(r"(?<=[.!?])\s+|\n+", prose) if len(x.strip()) >= 40]
    return bool(sentences) and max(sentences.count(x) for x in set(sentences)) >= 3


class StreamCut(RuntimeError):
    pass


class PromptTooBig(ModelError):
    """A prompt that wouldn't fit the model's context window."""


class ReplyTooLong(ModelError):
    """The reply ran into the output limit without finishing."""


class SchemaGaveUp(ModelError):
    """Claude couldn't fit its answer to the JSON schema (the CLI's structured-output retries ran out)."""


def _stream_chat(url: str, body: dict, label: str, timeout: float, quiet: bool = False,
                 think_budget: int = 0) -> tuple[str, str, dict]:
    req = urllib.request.Request(
        url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    content, thinking, final, ntok, nthink, calls = [], [], {}, 0, 0, []
    tick = Ticker(f"  {friendly(label)}: reading") if not quiet else contextlib.nullcontext()
    with tick, urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            if not raw.strip():
                continue
            msg = json.loads(raw)
            if "error" in msg:
                # A crashed runner is worth retrying (Ollama restarts it); a bad request isn't.
                if "context" in msg["error"] and "exceed" in msg["error"]:
                    raise ModelError(f"{label}: {msg['error']}")
                raise StreamCut(f"{label}: {msg['error'][:300]}")
            m = msg.get("message") or {}
            if m.get("thinking"):
                thinking.append(m["thinking"])
                ntok += 1
                nthink += 1
                guard_thinking(thinking, nthink, content, think_budget)  # leaving the `with` closes the request
            if m.get("content"):
                content.append(m["content"])
                ntok += 1
            for c in m.get("tool_calls") or []:  # Ollama sends each call whole
                fn = c.get("function") or {}
                args = fn.get("arguments") or {}
                calls.append({"id": c.get("id") or f"call_{len(calls) + 1}", "name": fn.get("name", ""),
                              "arguments": args if isinstance(args, dict) else loads_or_empty(args)})
            if msg.get("done"):
                final = msg
                break
            if not quiet:
                tick.text = f"  {friendly(label)}: {'writing' if content else 'thinking'} ({ntok} tokens)"
    if not final:
        raise StreamCut(f"{label}: the reply stopped early (did the model server crash?)")
    stats = {k: v for k, v in final.items() if k != "message"}
    stats["tool_calls"] = calls
    return "".join(content), "".join(thinking), stats


def loads_or_empty(text) -> dict:
    try:
        value = json.loads(text) if isinstance(text, str) else text
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def guard_thinking(thinking: list[str], nthink: int, content: list[str], think_budget: int) -> None:
    """Stop thinking at the budget, or as soon as it goes in circles."""
    if think_budget and nthink >= think_budget and not content:
        raise ThinkingCut("".join(thinking), nthink)
    if nthink % 100 == 0 and not content and thinking_loops("".join(thinking)):
        raise ThinkingCut("".join(thinking), nthink, looped=True)


def wire_messages(messages: list[dict], api: str) -> list[dict]:
    """A conversation with tool calls as each server takes it. buckets keeps assistant turns as
    {"tool_calls": [{"id", "name", "arguments": {...}}]} and tool results as {"role": "tool",
    "tool_call_id", "name", "content"}; plain messages pass through."""
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            if api == "openai":
                calls = [{"id": c["id"], "type": "function",
                          "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in m["tool_calls"]]
            else:
                calls = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif m.get("role") == "tool":
            out.append({"role": "tool", "content": m["content"],
                        **({"tool_call_id": m["tool_call_id"]} if api == "openai" else {"tool_name": m.get("name", "")})})
        else:
            out.append(m)
    return out


def openai_body(body: dict) -> dict:
    """buckets' (Ollama-shaped) request as llama.cpp's server takes it. The JSON schema applies after
    the thinking, as with Ollama (checked: it thinks first, then answers in the schema)."""
    out = {"model": body["model"], "messages": wire_messages(body["messages"], "openai"), "stream": True,
           "max_tokens": body["options"]["num_predict"], "temperature": body["options"].get("temperature", 0.3),
           "chat_template_kwargs": {"enable_thinking": bool(body["think"])}}
    if body.get("tools"):
        out["tools"] = body["tools"]
    if body.get("format"):
        out["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": body["format"]}}
    return out


def _stream_chat_llamacpp(url: str, body: dict, label: str, timeout: float, quiet: bool = False,
                          think_budget: int = 0) -> tuple[str, str, dict]:
    """The same call against llama.cpp's server (OpenAI-style, streamed): its reasoning and content,
    with its timings mapped onto Ollama's stats, so the rest of buckets doesn't care which it was."""
    req = urllib.request.Request(url.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(openai_body(body)).encode(),
                                 headers={"Content-Type": "application/json"})
    content, thinking, ntok, nthink, finish, timings, parts = [], [], 0, 0, None, {}, {}
    tick = Ticker(f"  {friendly(label)}: reading") if not quiet else contextlib.nullcontext()
    with tick, urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            msg = json.loads(data)
            if "error" in msg:
                err = msg["error"].get("message", str(msg["error"])) if isinstance(msg["error"], dict) else str(msg["error"])
                if "context" in err and "exceed" in err:
                    raise ModelError(f"{label}: {err}")
                raise StreamCut(f"{label}: {err[:300]}")
            timings = msg.get("timings") or timings
            for choice in msg.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    thinking.append(delta["reasoning_content"])
                    ntok += 1
                    nthink += 1
                    guard_thinking(thinking, nthink, content, think_budget)
                if delta.get("content"):
                    content.append(delta["content"])
                    ntok += 1
                for c in delta.get("tool_calls") or []:  # streamed in pieces, keyed by index
                    part = parts.setdefault(c.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    fn = c.get("function") or {}
                    part["id"] = c.get("id") or part["id"]
                    part["name"] += fn.get("name") or ""
                    part["arguments"] += fn.get("arguments") or ""
                finish = choice.get("finish_reason") or finish
            if not quiet:
                tick.text = f"  {friendly(label)}: {'writing' if content else 'thinking'} ({ntok} tokens)"
    if finish is None:
        raise StreamCut(f"{label}: the reply stopped early (did the model server crash?)")
    ms = lambda k: int(float(timings.get(k, 0)) * 1e6)  # noqa: E731
    stats = {"done_reason": "length" if finish == "length" else "stop", "load_duration": 0,
             "prompt_eval_count": timings.get("prompt_n", 0), "prompt_eval_duration": ms("prompt_ms"),
             "prompt_eval_cached_count": timings.get("cache_n", 0),
             "eval_count": timings.get("predicted_n", ntok), "eval_duration": ms("predicted_ms"),
             "total_duration": ms("prompt_ms") + ms("predicted_ms"),
             "tool_calls": [{"id": c["id"] or f"call_{i + 1}", "name": c["name"], "arguments": loads_or_empty(c["arguments"])}
                            for i, c in sorted(parts.items())]}
    return "".join(content), "".join(thinking), stats


def chat(cfg: dict, run: Run, label: str, system: str, user: str, *, schema: dict | None = None,
         think: bool | None = None, max_tokens: int = 8192, timeout: float = 2400, quiet: bool = False,
         think_budget: int | None = None) -> str:
    think = cfg.get("think", False) if think is None else think
    # Ollama silently drops the start of a prompt that doesn't fit (where the instructions are),
    # so refuse instead of letting the model answer without its task.
    need = est_tokens(system) + est_tokens(user) + max_tokens
    if need > cfg.get("num_ctx", 32768):
        raise PromptTooBig(f"{label}: about {need} tokens with the answer, but the context is "
                           f"{cfg.get('num_ctx', 32768)}; it would be cut silently")
    body = {
        "model": cfg["model"], "stream": True, "think": think, "keep_alive": cfg.get("keep_alive", "10m"),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"num_ctx": cfg.get("num_ctx", 32768), "temperature": cfg.get("temperature", 0.3),
                    "num_predict": max_tokens},
    }
    if schema:
        body["format"] = schema
    budget = (cfg.get("think_budget", 0) if think_budget is None else think_budget) if think else 0
    stream = _stream_chat_llamacpp if cfg.get("api") == "llamacpp" else _stream_chat
    for attempt in range(1, 4):
        try:
            content, thinking, stats = stream(cfg["url"], body, label, timeout, quiet,
                                              budget if body["think"] else 0)
        except ThinkingCut as cut:
            # Thinking without a cap can run for many minutes on the Mac. Stop at the budget and ask
            # for the answer, handing the notes over; the shared prompt prefix comes from the cache.
            run.save(f"{label}-thinking-cut.txt", cut.thinking)
            room = cfg.get("num_ctx", 32768) - max_tokens - est_tokens(system) - est_tokens(user) - 300
            notes = cut.thinking[-max(room, 0) * 3:]
            say(f"  {friendly(label)}: " + (f"was thinking in circles after {cut.tokens} tokens" if cut.looped
                                             else f"thought for {cut.tokens} tokens (the budget)")
                + "; asking for the answer now")
            body["think"] = False
            body["options"]["num_predict"] = min(max_tokens, 8000)
            body["messages"][1]["content"] = (user + "\n\nYour notes from thinking about this so far (cut short, "
                                              "but use them):\n" + notes + "\n\nNow give your answer. No more "
                                              "reasoning: reply with only the answer itself, and keep each task's "
                                              "instructions under 300 words. Cover everything the request asks "
                                              "for, including parts your notes hadn't reached yet.")
            continue
        except urllib.error.HTTPError as e:
            reason = e.read().decode(errors="replace")[:300]
            if e.code < 500:
                raise ModelError(f"{label}: HTTP {e.code}: {reason}") from None
            err = f"HTTP {e.code}: {reason}"
        except (StreamCut, urllib.error.URLError, ConnectionError, TimeoutError) as e:
            err = str(e)
        else:
            run.save(f"{label}.json", {"request": body, "thinking": thinking, "content": content, "stats": stats})
            run.talked = True
            if stats.get("done_reason") == "length":
                if body["think"]:
                    say(f"  {warn()} {friendly(label)}: ran out of room while thinking; trying again without")
                    body["think"] = False
                    continue
                raise ReplyTooLong(f"{label}: the reply hit the {body['options']['num_predict']}-token limit")
            secs = stats.get("total_duration", 0) / 1e9
            detail(f"{label}: {stats.get('prompt_eval_count', '?')} tokens in, "
                   f"{stats.get('eval_count', '?')} out, {secs:.0f}s")
            return content
        say(f"  {warn()} {friendly(label)}: attempt {attempt} failed ({err[:160]}); retrying")
        time.sleep(5 * attempt)
    raise ModelError(f"{label}: failed after 3 attempts")


def chat_turn(cfg: dict, run: Run, label: str, messages: list[dict], *, tools: list | None = None,
              schema: dict | None = None, think: bool = True, think_budget: int = 0, max_tokens: int = 8000,
              timeout: float = 2400) -> tuple[str, str, list[dict], bool]:
    """One turn of a conversation that may call tools: (content, thinking, tool calls, cut). `cut`
    means the thinking was stopped at its budget or in a loop; `thinking` then holds the notes."""
    need = sum(est_tokens(m.get("content") or "") for m in messages) + max_tokens
    if need > cfg.get("num_ctx", 32768):
        raise PromptTooBig(f"{label}: about {need} tokens with the answer, but the context is {cfg.get('num_ctx', 32768)}")
    llama = cfg.get("api") == "llamacpp"
    body = {"model": cfg["model"], "stream": True, "think": think, "keep_alive": cfg.get("keep_alive", "10m"),
            "messages": messages if llama else wire_messages(messages, "ollama"),
            "options": {"num_ctx": cfg.get("num_ctx", 32768), "temperature": cfg.get("temperature", 0.3),
                        "num_predict": max_tokens}}
    if tools:
        body["tools"] = tools
    if schema:
        body["format"] = schema
    stream = _stream_chat_llamacpp if llama else _stream_chat
    err = ""
    for attempt in range(1, 4):
        try:
            content, thinking, stats = stream(cfg["url"], body, label, timeout, False, think_budget if think else 0)
        except ThinkingCut as cut:
            run.save(f"{label}-thinking-cut.txt", cut.thinking)
            say(f"  {friendly(label)}: " + (f"was thinking in circles after {cut.tokens} tokens" if cut.looped
                                             else f"thought for {cut.tokens} tokens (the budget)"))
            return "", cut.thinking, [], True
        except (StreamCut, urllib.error.URLError, ConnectionError, TimeoutError) as e:
            err = str(e)
            say(f"  {warn()} {friendly(label)}: attempt {attempt} failed ({err[:160]}); retrying")
            time.sleep(5 * attempt)
            continue
        run.save(f"{label}.json", {"request": body, "thinking": thinking, "content": content, "stats": stats})
        run.talked = True
        detail(f"{label}: {stats.get('prompt_eval_count', '?')} tokens in, {stats.get('eval_count', '?')} out, "
               f"{stats.get('total_duration', 0) / 1e9:.0f}s")
        return content, thinking, stats.get("tool_calls") or [], False
    raise ModelError(f"{label}: failed after 3 attempts ({err[:200]})")


LOOKUP_TOOLS = [
    {"type": "function", "function": {
        "name": "read", "description": "Exact code from the repository, by a name from the code "
        "(\"GitAssistant.op_log\", \"parse_args\", \"GIT_OPS\") or a line range (\"path:120-180\").",
        "parameters": {"type": "object", "properties": {"what": {"type": "string"}}, "required": ["what"]}}},
    {"type": "function", "function": {
        "name": "search", "description": "Lines matching a regular expression across the repository "
        "(or under one path), with file and line numbers.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                       "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "ask", "description": "Another machine reads the given files in full and answers your "
        "question with file:line references: for how something works, where something is handled, or what "
        "pattern the code follows. Up to 4 files.",
        "parameters": {"type": "object", "properties": {"question": {"type": "string"},
                                                        "files": {"type": "array", "items": {"type": "string"}}},
                       "required": ["question", "files"]}}},
]

LOOKUP_NOTE = """

Before you plan, you may look things up with the tools, but only what the material above doesn't
show and the plan needs: read (exact code by name or line range), search (a pattern across the
repository) and ask (another machine reads whole files and answers a question). Ask for several at
once when you can. When you have what you need, reply with the plan as the JSON object, with no tool
call."""

ASK_PROMPT = """Answer the question from these files. Be brief: at most 8 lines, each with a `path:line`
or `path:start-end` reference (the numbers are shown on every line), most relevant first, then at
most 2 sentences of summary. Don't paste code. If the files don't answer it, say what's missing.

Question: {question}

{files}"""


def extract_json(text: str):
    """Pull the first JSON object out of a model reply, tolerating fences and stray prose."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Only unwrap a fence around the whole reply: instructions inside the JSON often hold code fences.
    fence = re.match(r"```(?:json)?\s*(.*?)```\s*$", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("the JSON object in the reply is cut off")


def ask_json(cfg: dict, run: Run, label: str, system: str, user: str, schema: dict, validate,
             **kw):
    try:
        reply = chat(cfg, run, label, system, user, schema=schema, **kw)
    except ReplyTooLong as e:
        say(f"  {warn()} {friendly(label)}: the answer ran too long; asking for a short one")
        kw["think"] = False
        reply = chat(cfg, run, f"{label}-short", system, user + "\n\nYour previous reply ran past the length limit "
                     "and was cut off. Reply with only the JSON object, concisely: no reasoning, and under 300 "
                     "words per instruction.", schema=schema, **kw)
    try:
        return validate(extract_json(reply))
    except (ValueError, KeyError, TypeError) as e:
        say(f"  {label}: the reply wasn't usable ({e}); asking once more")
        kw["think"] = False
        retry = f"{user}\n\nYour previous reply was rejected: {e}\nReply with only the corrected JSON object."
        return validate(extract_json(chat(cfg, run, f"{label}-retry", system, retry, schema=schema, **kw)))


READ_TOOLS = "Read,Grep,Glob"


def claude_command(c: dict, model: str, schema: dict | None, tools: bool, add_dirs=()) -> list[str]:
    """Headless Claude Code: read-only tools, no MCP servers or skills, a hard spending cap; related
    repos added as directories it may read."""
    cmd = [c["bin"], "-p", "--model", model, "--output-format", "json", "--no-session-persistence",
           "--strict-mcp-config", "--disable-slash-commands", "--max-budget-usd", str(c["budget_usd"]),
           "--tools", READ_TOOLS if tools else ""]
    if tools:
        cmd += ["--allowedTools", READ_TOOLS]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    if tools and add_dirs:
        cmd += ["--add-dir", *map(str, add_dirs)]
    return cmd


def parse_claude(out: str, label: str, schema: dict | None):
    """Pull (payload, cost, summary) out of `claude -p --output-format json`."""
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        d = extract_json(out)
    if d.get("subtype") == "error_max_structured_output_retries":
        raise SchemaGaveUp(f"{label}: Claude couldn't fit its answer to the JSON schema")
    if d.get("is_error") or d.get("subtype") != "success":
        raise ModelError(f"{label}: Claude stopped ({d.get('subtype')}): {str(d.get('result'))[:300]}")
    payload = d.get("result")
    if schema:
        payload = d.get("structured_output") or extract_json(payload or "")
    cost = float(d.get("total_cost_usd") or 0)
    return payload, cost, f"{d.get('num_turns', '?')} turns, ${cost:.3f}, {int(d.get('duration_ms', 0) / 1000)}s"


def claude_call(cfg: dict, run: Run, label: str, prompt: str, repo: Path, *, schema: dict | None = None,
                model: str | None = None, tools: bool = True, add_dirs=()):
    c = cfg["claude"]
    model = model or c["model"]
    with Ticker(f"  {friendly(label)} (Claude {model})"):
        code, out, timed_out = run_proc(claude_command(c, model, schema, tools, add_dirs), repo, c["timeout_s"],
                                        input=prompt)
    run.save(f"{label}-claude.json", f"{prompt}\n\n===== output =====\n{out}")
    if timed_out:
        raise ModelError(f"{label}: Claude timed out after {c['timeout_s']}s")
    payload, cost, summary = parse_claude(out, label, schema)
    detail(f"{label}: Claude {model}, {summary}")
    secs = re.search(r"(\d+)s$", summary)
    say(f"  {ok()} {friendly(label)} · Claude {model} · {secs.group(1) + 's' if secs else ''} · {money(cost)}")
    return payload, cost


# ---------------------------------------------------------------- repo reading


def list_files(repo: Path, exclude: list[str]) -> dict[str, int]:
    """Text files git knows about (tracked or untracked, not ignored) -> line count."""
    out = {}
    for f in git(repo, "ls-files", "-co", "--exclude-standard").splitlines():
        p = repo / f
        if os.path.basename(f) in LOCKFILES or p.suffix.lower() in BINARY_EXT or f.startswith(MEMORY_DIR + "/"):
            continue
        if any(fnmatch.fnmatch(f, g) for g in exclude):
            continue
        try:
            if not p.is_file() or p.stat().st_size > 400_000:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        out[f] = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
    return out


def dir_summary(files: dict[str, int], depth: int = 2) -> dict[str, list[int]]:
    dirs: dict[str, list[int]] = {}
    for f, n in files.items():
        d = "/".join(f.split("/")[:-1][:depth]) or "."
        acc = dirs.setdefault(d, [0, 0])
        acc[0] += 1
        acc[1] += n
    return dirs


INDEX_SCRIPT = Path(__file__).resolve().parent / "ts_index.cjs"
MOVES_SCRIPT = Path(__file__).resolve().parent / "ts_moves.cjs"


def ts_moves(repo: Path, payload: dict, timeout: int = 300) -> dict:
    """Run one ts_moves.cjs operation (extract, compare, share); {"ok": False, "error"} on any failure."""
    if not shutil.which("node") or not MOVES_SCRIPT.exists():
        return {"ok": False, "error": "node (and ts_moves.cjs) are needed for mechanical moves"}
    try:
        r = subprocess.run(["node", str(MOVES_SCRIPT)], input=json.dumps({**payload, "repo": str(repo)}),
                           cwd=repo, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.stdout.strip() else {"ok": False, "error": r.stderr.strip()[-300:] or "no output"}
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return {"ok": False, "error": str(e)[:300]}
TS_EXT = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


def py_sig(node) -> str:
    if isinstance(node, ast.ClassDef):
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        return f"class {node.name}" + (f"({bases})" if bases else "")
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"def {node.name}({ast.unparse(node.args)}){ret}"[:200]


def py_decls(path: Path) -> list[dict]:
    """Every class, function and method of a Python file (private ones too) with its qualified name
    ("GitAssistant.fill_in") and lines, decorators included: what a task or a read can point at."""
    try:
        return py_decls_text(path.read_text(errors="replace"))
    except OSError:
        return []


def py_decls_text(source: str) -> list[dict]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    out = []

    def walk(body, prefix: str) -> None:
        for node in body:
            if not prefix and isinstance(node, (ast.Assign, ast.AnnAssign)):  # GIT_OPS = {...}
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name) and node.end_lineno - node.lineno >= 2:
                        out.append({"name": t.id, "line": node.lineno, "end": node.end_lineno,
                                    "signature": f"{t.id} = …"})
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                out.append({"name": prefix + node.name, "line": start, "end": node.end_lineno,
                            "signature": py_sig(node)})
                if isinstance(node, ast.ClassDef):
                    walk(node.body, f"{prefix}{node.name}.")

    walk(tree.body, "")
    return out


def decls_of(repo: Path, path: str, outline: dict) -> list[dict]:
    """A file's declarations with qualified names: fresh from the AST for Python, else the code index."""
    if path.endswith(".py"):
        return py_decls(repo / path)
    out = []
    for d in outline.get(path, []):
        out.append({"name": d["name"], "line": d["line"], "end": d["end"], "signature": d["signature"]})
        out += [{"name": f"{d['name']}.{m['name']}", "line": m["line"], "end": m["end"], "signature": m["signature"]}
                for m in d.get("members", [])]
    return out


def find_decl(decls: list[dict], name: str) -> dict | None:
    """`name` exactly ("GitAssistant.fill_in"), or the one declaration whose last part it is ("fill_in")."""
    exact = [d for d in decls if d["name"] == name]
    if exact:
        return exact[0]
    tail = [d for d in decls if d["name"].endswith("." + name)]
    return tail[0] if len(tail) == 1 else None


def locate(repo: Path, files: dict, outline: dict, spec: str, in_file: str = "") -> str | None:
    """A symbol ("fill_in", "GitAssistant.fill_in", "path:Class.method") or a range ("path:12-40",
    or "12-40" with `in_file`) as "path:start-end"; None when it isn't found or is ambiguous."""
    spec = clean_path(spec.strip().strip("`")).removesuffix("()")
    path, _, rest = spec.rpartition(":") if ":" in spec else ("", "", spec)
    path = path or in_file
    if path and path not in files:
        hits = [f for f in files if f.endswith("/" + path)]
        if len(hits) != 1:
            return None
        path = hits[0]
    if re.fullmatch(r"\d+(\s*[-–]\s*\d+)?", rest):
        return f"{path}:{rest.replace(' ', '')}" if path else None
    if path:
        d = find_decl(decls_of(repo, path, outline), rest)
        return f"{path}:{d['line']}-{d['end']}" if d else None
    found = [(f, d) for f in files if f.endswith(".py") or f in outline
             for d in [find_decl(decls_of(repo, f, outline), rest)] if d]
    return f"{found[0][0]}:{found[0][1]['line']}-{found[0][1]['end']}" if len(found) == 1 else None


def outline_text(decls: list[dict]) -> str:
    return "\n".join(f"  {d['line']}-{d['end']} {d['name']}" for d in decls)


def py_outline(path: Path) -> list[dict]:
    """Top-level functions and classes (with public methods) of a Python file, from its AST."""
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, ValueError):
        return []
    sig = py_sig

    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append({"kind": "function", "name": node.name, "exported": not node.name.startswith("_"),
                        "line": node.lineno, "end": node.end_lineno, "signature": sig(node)})
        elif isinstance(node, ast.ClassDef):
            members = [{"name": m.name, "line": m.lineno, "end": m.end_lineno, "signature": sig(m)}
                       for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and (not m.name.startswith("_") or m.name == "__init__")]
            out.append({"kind": "class", "name": node.name, "exported": not node.name.startswith("_"),
                        "line": node.lineno, "end": node.end_lineno, "signature": sig(node), "members": members})
    return out


class CodeIndex:
    """What the code declares and where, read mechanically: TypeScript/JavaScript through ts_index.cjs
    (with the repo's own `typescript` package), Python through `ast`. Everything here is optional;
    without it buckets falls back to plain file lists."""

    def __init__(self, repo: Path, files: dict[str, int]):
        self.repo, self.files = repo, files
        self.ts = self._ts_available()
        self._outline: dict | None = None

    def _ts_available(self) -> bool:
        if not shutil.which("node") or not INDEX_SCRIPT.exists():
            return False
        if not any(f.endswith(TS_EXT) for f in self.files):
            return False
        r = subprocess.run(["node", "-e", "require.resolve('typescript', {paths: [process.argv[1]]})", str(self.repo)],
                           capture_output=True)
        return r.returncode == 0

    def _node(self, cmd: str, *args: str):
        try:
            r = subprocess.run(["node", str(INDEX_SCRIPT), cmd, str(self.repo), *args], capture_output=True,
                               text=True, timeout=180)
            if r.returncode == 0:
                return json.loads(r.stdout)
            detail(f"index {cmd} failed: {r.stderr.strip()[:300]}")
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            detail(f"index {cmd} failed: {e}")
        return None

    def outline(self) -> dict[str, list]:
        if self._outline is None:
            out = (self._node("outline") or {}) if self.ts else {}
            for f in self.files:
                if f.endswith(".py"):
                    out[f] = py_outline(self.repo / f)
            self._outline = out
        return self._outline

    def surface(self) -> dict | None:
        return self._node("surface") if self.ts else None

    def uses(self, name: str, origin: str | None = None) -> dict | None:
        return self._node("uses", name, *([origin] if origin else [])) if self.ts else None

    def broken(self) -> list | None:
        return self._node("broken") if self.ts else None

    def importers(self, targets: list[str]) -> dict:
        return (self._node("importers", *targets) or {}) if self.ts and targets else {}


def enclosing(decls: list[dict], line: int) -> dict | None:
    """The innermost declaration (a method before its class) that contains `line`."""
    for d in decls:
        if d["line"] <= line <= d["end"]:
            for m in d.get("members", []):
                if m["line"] <= line <= m["end"]:
                    return m
            return d
    return None


REQUEST_NAMES = re.compile(r"`([A-Za-z_$][\w$]*)(?:\(\))?`|\b([A-Za-z_$][\w$]*)\(|"
                           r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+|[a-z]+(?:[A-Z][a-z0-9]*)+|[a-z]+_[a-z0-9_]+|[A-Z][A-Z0-9]*_[A-Z0-9_]+)\b")


def request_names(text: str) -> list[str]:
    """Code-like names in a request: `backticked`, called(), CamelCase, camelCase or snake_case words."""
    seen: list[str] = []
    for m in REQUEST_NAMES.finditer(text):
        name = next(g for g in m.groups() if g)
        if len(name) >= 3 and name not in seen:
            seen.append(name)
    for m in re.finditer(r"\b[A-Z]\w*\.(_*[A-Za-z]\w*)\b", text):  # Box.size, GitAssistant._open
        if len(m.group(1)) >= 3 and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def related_tests(source: str, importers: list[str]) -> list[str]:
    """Test files for a source file, closest first: same name (conditions.ts → conditions.test.ts),
    then other tests that import it."""
    stem = Path(source).name.split(".")[0]
    tests = [f for f in importers if re.search(r"(^|/)(test|tests|__tests__|spec)/|\.(test|spec)\.", f)]
    return sorted(tests, key=lambda f: (Path(f).name.split(".")[0] != stem, len(f)))


def render_outline(files: dict[str, int], outline: dict, limit: int = 48000) -> str:
    """The file list plus what each file declares and on which lines, so the planner can ask for
    exact ranges. Falls back to exported names only, then to the plain list, when it's too big."""
    def build(full: bool) -> str:
        lines = []
        for f, n in sorted(files.items()):
            lines.append(f"{f} ({n} lines)")
            for d in outline.get(f, []):
                if not (d["exported"] or (full and d["kind"] in ("function", "class"))):
                    continue
                lines.append(f"  {d['line']}-{d['end']}  {d['signature'][:110]}")
                if full:
                    for m in d.get("members", []):
                        lines.append(f"    {m['line']}-{m['end']}  {m['signature'][:90]}")
        return "\n".join(lines)

    for full in (True, False):
        text = build(full)
        if len(text) <= limit:
            return text
    return render_map(files)


def render_map(files: dict[str, int]) -> str:
    return "\n".join(f"{f} ({n} lines)" for f, n in sorted(files.items()))


def doc_sections(text: str, request: str, budget: int) -> str:
    """A long markdown doc cut to `budget` characters: its opening, then the sections whose heading or
    text mention the request's phrases most (in any code spelling), in the doc's own order."""
    if len(text) <= budget:
        return text
    parts = re.split(r"(?m)^(?=#{1,3} )", text)
    head, sections = parts[0], parts[1:]
    if not head.strip() and sections and sections[0].startswith("# "):  # the title and its intro are the opening
        head = sections.pop(0)
    if not sections:
        return text[:budget]
    terms = {x.lower() for r in word_runs(request) for i in range(len(r)) for j in range(i + 1, len(r) + 1)
             for words in [r[i:j]] if len(words) > 1 or len(words[0]) >= 5 for x in spellings(words)}
    terms |= {n.lower() for n in request_names(request)}

    def score(sec: str) -> int:
        low = sec.lower()
        title = low.split("\n", 1)[0]
        return sum(low.count(t) + 5 * (t in title) for t in terms)

    ranked = sorted(range(len(sections)), key=lambda i: -score(sections[i]))
    keep, used = set(), len(head[: budget // 3])
    for i in ranked:
        if score(sections[i]) == 0 or used + len(sections[i]) > budget:
            continue
        keep.add(i)
        used += len(sections[i])
    picked = [sections[i] for i in sorted(keep)]
    titles = [sec.split("\n", 1)[0].strip("# ") for i, sec in enumerate(sections) if i not in keep]
    return (head[: budget // 3] + "".join(picked)
            + (f"\n(Other sections, not shown: {'; '.join(titles[:30])})" if titles else ""))


RULE_WORDS = re.compile(r"\b(must|never|always|do not|don't|only|required|instead of|not allowed|prefer)\b", re.I)


def agent_rules(repo: Path, budget: int = 5000) -> str:
    """The repo's agent doc (AGENTS.md, CLAUDE.md…) as a short list of its rules, so every plan and review
    sees all of them even when the doc is too long to read whole: each section's heading (often a rule
    itself) and its first sentences that say must / never / always / don't / only."""
    doc = next((repo / n for n in GUIDANCE_FILES if (repo / n).is_file()), None)
    if not doc:
        return ""
    text = doc.read_text(errors="replace")
    if text.strip().startswith("@") and len(text) < 200:
        target = repo / text.strip()[1:].split()[0]
        text = target.read_text(errors="replace") if target.is_file() else ""
    found = []
    for sec in re.split(r"(?m)^(?=## )", text)[1:]:
        title, _, body = sec.partition("\n")
        body = re.sub(r"```.*?```", "", body, flags=re.S)
        plain = re.sub(r"\*\*|__|`", "", re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body))
        sentences = [x.strip(" -*>|") for x in re.split(r"(?<=[.!?])\s+|\n\s*[-*] |\n\n", plain)]
        rule = next((re.sub(r"\s+", " ", x) for x in sentences if RULE_WORDS.search(x) and 30 <= len(x) <= 400
                     and x[:1].isupper()), "")
        found.append((re.sub(r"\s+", " ", title.strip("# ").strip()), rule))
    if not found:
        return ""
    # Every heading whole (many are rules themselves); the room left is shared by one sentence each.
    room = max(60, (budget - sum(len(t) + 4 for t, _ in found)) // len(found))
    lines = [f"- {t}" + (f": {r if len(r) <= room else r[:room - 1].rsplit(' ', 1)[0] + '…'}" if r else "")
             for t, r in found]
    return "\n".join(lines)[:budget]


def guidance(repo: Path, rc: dict, request: str = "") -> str:
    """The repo's own guidance: its agent doc (AGENTS.md, CLAUDE.md…), cut to the parts that matter
    for this request when it's long, the skill docs whose names the request spells (.github/skills/
    work-order-details/ for "work order details"), the README's start, and .buckets.json notes."""
    parts = []
    for name in GUIDANCE_FILES:
        p = repo / name
        if p.exists():
            text = p.read_text(errors="replace")
            if text.strip().startswith("@") and len(text) < 200:  # CLAUDE.md that just says @AGENTS.md
                continue
            rules = agent_rules(repo) if len(text) > 6000 else ""
            if rules:
                parts.append(f"--- {name}: every section's rules, in short (follow all of them) ---\n{rules}")
            parts.append(f"--- {name}{' (the parts that match this request)' if len(text) > 6000 else ''} ---\n"
                         + doc_sections(text, request, 6000))
            break
    if request:
        wanted = {n.lower() for r in word_runs(request) for i in range(len(r)) for j in range(i + 1, len(r) + 1)
                  for n in spellings(r[i:j])}
        for doc in sorted(repo.glob("*/skills/*/SKILL.md")) + sorted(repo.glob("*/*/skills/*/SKILL.md")):
            if doc.parent.name.lower() in wanted:
                parts.append(f"--- {doc.relative_to(repo)} (the parts that match this request) ---\n"
                             + doc_sections(doc.read_text(errors="replace"), request, 4000))
    readme = next((repo / n for n in ("README.md", "README.rst", "README") if (repo / n).exists()), None)
    if readme:
        parts.append(f"--- {readme.name} (start) ---\n{readme.read_text(errors='replace')[:3000]}")
    if rc.get("notes"):
        parts.append(f"--- notes from .buckets.json ---\n{rc['notes']}")
    return "\n\n".join(parts) or "(no README or agent guidance found)"


def numbered(repo: Path, path: str, max_chars: int) -> str:
    text = (repo / path).read_text(errors="replace")
    cut = len(text) > max_chars
    lines = text[:max_chars].splitlines()
    body = "\n".join(f"{i:>5}| {line}" for i, line in enumerate(lines, 1))
    tail = f"\n… (cut off after {len(lines)} lines)" if cut else ""
    return f"=== {path} ===\n{body}{tail}"


def est_tokens(text: str) -> int:
    return len(text) // 3 + 1


def batches(blocks: list[str], budget: int) -> list[list[str]]:
    out, cur, size = [], [], 0
    for b in blocks:
        t = est_tokens(b)
        if cur and size + t > budget:
            out.append(cur)
            cur, size = [], 0
        cur.append(b)
        size += t
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- checks


WRITES = re.compile(r"--fix\b|--write\b|\b(yarn|npm run|pnpm( run)?|bun run)\s+format\b")


def script_writes(repo: Path, scripts: dict, name: str) -> bool:
    """Whether a package script changes files when run, here or (for "run it in every workspace"
    scripts) in any workspace's script of the same name."""
    if WRITES.search(scripts.get(name, "")):
        return True
    if not re.search(r"foreach|workspaces|\s-r\b|--recursive|\bturbo\b|\bnx\b|lerna", scripts.get(name, "")):
        return False
    try:
        spaces = json.loads((repo / "package.json").read_text()).get("workspaces") or []
    except (OSError, json.JSONDecodeError):
        return False
    spaces = spaces.get("packages", []) if isinstance(spaces, dict) else spaces
    for pattern in spaces:
        for pkg in repo.glob(f"{pattern.rstrip('/')}/package.json"):
            try:
                if WRITES.search((json.loads(pkg.read_text()).get("scripts") or {}).get(name, "")):
                    return True
            except (OSError, json.JSONDecodeError):
                continue
    return False


def detect_checks(repo: Path, rc: dict) -> dict[str, str]:
    if rc.get("checks") is not None:
        return dict(rc["checks"])
    checks: dict[str, str] = {}
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {}) or {}
        except json.JSONDecodeError:
            scripts = {}
        if (repo / "pnpm-lock.yaml").exists():
            runner = "pnpm -s run {}"
        elif (repo / "yarn.lock").exists():
            runner = "yarn run {}"
        elif (repo / "bun.lockb").exists() or (repo / "bun.lock").exists():
            runner = "bun run {}"
        else:
            runner = "npm run -s {}"
        for name, options in (("typecheck", ["typecheck", "type-check", "check-types", "tsc"]),
                              ("lint", ["lint"]), ("test", ["test"])):
            hit = next((o for o in options if o in scripts), None)
            if hit and "no test specified" not in scripts[hit]:
                if script_writes(repo, scripts, hit):
                    continue  # a check must only look: this one rewrites files (eslint --fix, prettier --write)
                checks[name] = runner.format(hit)
        if "typecheck" not in checks and (repo / "tsconfig.json").exists():
            checks["typecheck"] = "npx --no-install tsc --noEmit -p ."
    py = repo / "pyproject.toml"
    if py.exists() or (repo / "setup.py").exists():
        text = py.read_text() if py.exists() else ""
        venv_py = repo / ".venv/bin/python"
        python = str(venv_py) if venv_py.exists() else "python3"
        if shutil.which("ruff") or "ruff" in text:
            checks["lint"] = "ruff check ."
        if "[tool.mypy]" in text:
            checks["typecheck"] = f"{python} -m mypy ."
        if (repo / "tests").is_dir() or "pytest" in text:
            checks["test"] = f"{python} -m pytest -q -x"
    if (repo / "go.mod").exists():
        checks["vet"] = "go vet ./..."
        checks["test"] = "go test ./..."
    if (repo / "Cargo.toml").exists():
        checks["check"] = "cargo check --quiet"
        checks["test"] = "cargo test --quiet"
    return checks


INDEX_CHECK = "imports"


def index_check(repo: Path) -> tuple[bool, str]:
    broken = CodeIndex(repo, list_files(repo, [])).broken()
    if broken is None:
        return True, "(code index unavailable)"
    lines = [f"{b['file']}:{b['line']}: {'re-exports' if b.get('reexport') else 'imports'} `{b['name']}` from "
             f"'{b['from']}', which doesn't export it" for b in broken]
    return not broken, "\n".join(lines) or "all imports of repository files resolve"


CHECKS_CACHE_HOURS = 12


def clean_tree(repo: Path) -> str:
    """The file tree of HEAD when the working tree matches it exactly ("" otherwise): checks on the
    same tree give the same result, so a run that starts where the last one ended needn't repeat them."""
    if changed_paths(repo):
        return ""
    return git(repo, "rev-parse", "HEAD^{tree}", check=False).strip()


def run_checks(repo: Path, checks: dict[str, str], run: Run, tag: str, timeout: float) -> dict:
    results, cached = {}, []
    tree = clean_tree(repo)
    cache_path = STATE_DIR / "checks-cache.json"
    try:
        cache = json.loads(cache_path.read_text()) if tree else {}
    except (OSError, json.JSONDecodeError):
        cache = {}
    for name, cmd in checks.items():
        key = f"{repo}|{tree}|{cmd}"
        hit = cache.get(key) if tree and cmd != "(code index)" else None
        if hit and time.time() - hit.get("at", 0) < CHECKS_CACHE_HOURS * 3600:
            results[name] = {k: v for k, v in hit.items() if k != "at"}
            cached.append(name)
            run.save(f"check-{tag}-{name}.log", f"$ {cmd}\n(cached: the same code was checked {elapsed(hit['at'])} ago)"
                                                 f"\n\n{hit['output']}")
            continue
        if cmd == "(code index)":
            start = time.time()
            good, out = index_check(repo)
            run.save(f"check-{tag}-{name}.log", out)
            results[name] = {"ok": good, "seconds": round(time.time() - start, 1), "tail": out[-3000:],
                             "output": out[-20000:]}
            continue
        start = time.time()
        with Ticker(f"  Running {name}: {cmd}"):
            code, out, timed_out = run_proc(cmd, repo, timeout, shell=True)
        out = strip_ansi(out)
        run.save(f"check-{tag}-{name}.log", f"$ {cmd}\n(exit {code})\n\n{out}")
        results[name] = {"ok": code == 0 and not timed_out, "seconds": round(time.time() - start, 1),
                         "tail": ("(timed out)\n" if timed_out else "") + out[-3000:],
                         "output": ("(timed out)\n" if timed_out else "") + out[-20000:]}
        if tree and not timed_out:
            cache[key] = {**results[name], "at": time.time()}
    if tree and len(results) > len(cached):
        keep = sorted(cache.items(), key=lambda kv: kv[1].get("at", 0))[-200:]
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(dict(keep)))
    say("  checks: " + ("  ".join(f"{n} {ok() if r['ok'] else bad()}" for n, r in results.items())
                        or warn("none configured (add them to .buckets.json)"))
        + (paint(f"  (cached: same code as an earlier run)", "dim") if cached and len(cached) == len(results)
           else paint(f"  ({', '.join(cached)} cached)", "dim") if cached else ""))
    return results


DIAG_PATTERNS = [
    # tsc: src/a.ts(12,5): error TS2322: …   and   src/a.ts:12:5 - error TS2322: …
    re.compile(r"(?P<path>[\w@.+\-/]+\.\w+)\((?P<line>\d+),(?P<col>\d+)\):\s*(?P<msg>.+)"),
    re.compile(r"(?P<path>[\w@.+\-/]+\.\w+):(?P<line>\d+):(?P<col>\d+)\s+-\s+(?P<msg>.+)"),
    # ruff, go, eslint unix/compact, mypy, pytest: path:line[:col]: message
    re.compile(r"(?P<path>[\w@.+\-/]+\.\w+):(?P<line>\d+)(?::(?P<col>\d+))?:\s+(?P<msg>\S.*)"),
    # vitest/jest stack frames: ❯ test/a.test.ts:140:23  or  at … (src/a.ts:12:5)
    re.compile(r"(?:❯|at\s.*\()\s*(?P<path>[\w@.+\-/]+\.\w+):(?P<line>\d+):(?P<col>\d+)\)?\s*$"),
]
ESLINT_ROW = re.compile(r"^\s+(?P<line>\d+):(?P<col>\d+)\s+(?P<sev>error|warning)\s+(?P<msg>.+?)\s*$")


def resolve_path(p: str, files) -> str:
    """A path from tool output to a repo file: exact, absolute into the repo, or package-relative."""
    p = clean_path(p)
    if p in files:
        return p
    inside = [f for f in files if p.endswith("/" + f)]
    if inside:
        return max(inside, key=len)
    hits = [f for f in files if f.endswith("/" + p)]
    return hits[0] if len(hits) == 1 else ""


def parse_diagnostics(text: str, files, kinds: bool = False) -> list[tuple]:
    """(file, line, message) for every problem a checker printed that points into the repo.
    Understands tsc, ESLint (stylish and compact), Vitest/Jest frames, pytest, ruff, mypy and go.
    With `kinds`, a fourth field says "check" (a type/lint error at that line) or "frame" (a stack
    frame of a failing test: the line is where it failed, not necessarily what's wrong)."""
    out, current, test, error = [], "", "", ""
    for raw in strip_ansi(text).splitlines():
        line = raw.rstrip()
        failing = re.match(r"^\s*(?:FAIL|×|✗)\s+(\S.*)$", line)
        if failing:  # a failing test's name, for the stack frames that follow
            test, error = failing.group(1).strip()[:120], ""
            continue
        thrown = re.match(r"^\s*(\w*(?:Error|Exception)\b:?.*)$", line)
        if thrown and not error:
            error = thrown.group(1).strip()[:150]
        row = ESLINT_ROW.match(line)
        if row and current:  # ESLint stylish: rows under a file-name line
            out.append((current, int(row["line"]), f"{row['sev']}: {row['msg']}", "check"))
            continue
        if line and not line.startswith(" ") and resolve_path(line.strip(), files):
            current = resolve_path(line.strip(), files)
            continue
        for pat in DIAG_PATTERNS:
            m = pat.search(line)
            if m:
                f = resolve_path(m["path"], files)
                if f:
                    msg = (m.groupdict().get("msg") or " · ".join(x for x in (test, error) if x)
                           or "(stack frame)").strip()
                    kind = "frame" if pat is DIAG_PATTERNS[3] else "check"
                    out.append((f, int(m["line"]), msg[:200], kind))
                break
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item if kinds else item[:3])
    return unique


def skeleton_tasks(results: dict, repo: Path, files, prefix: str, max_tasks: int) -> list[dict]:
    """One task per file with the exact type/lint errors the checks report there and the current
    code at each line. Test failures (stack frames) are left out: where a test fails isn't
    necessarily what's wrong, so those still need the planner."""
    by_file: dict[str, set] = {}
    for name, r in results.items():
        if r["ok"]:
            continue
        for f, ln, msg, kind in parse_diagnostics(r["output"], files, kinds=True):
            if kind == "check" and (repo / f).is_file():
                by_file.setdefault(f, set()).add((ln, msg, name))
    tasks = []
    for f, items in list(by_file.items())[:max_tasks]:
        src = (repo / f).read_text(errors="replace").splitlines()
        lines = []
        for ln, msg, name in sorted(items)[:25]:
            code = src[ln - 1].strip()[:140] if 0 < ln <= len(src) else ""
            lines.append(f"- line {ln} ({name}): {msg}" + (f"\n    current code: {code}" if code else ""))
        tasks.append({"id": f"{prefix}{len(tasks) + 1}", "file": f, "action": "edit", "read_first": [],
                      "instructions": "Fix these problems the checks report in this file, at these exact lines:\n"
                                      + "\n".join(lines) + "\nFix each one properly (no `any`, no ignore comments) "
                                      "and change only what these errors need.",
                      "done_when": "The checks no longer report these lines."})
    return tasks


def failures_only_mechanical(results: dict, files) -> bool:
    """Every failing check points at type/lint errors (no failing tests): fixable from the errors alone."""
    failing = [r for r in results.values() if not r["ok"]]
    if not failing:
        return False
    for r in failing:
        diags = parse_diagnostics(r["output"], files, kinds=True)
        if not diags or any(kind == "frame" for *_, kind in diags):
            return False
    return True


def with_exact_errors(tasks: list[dict], skeleton: list[dict], max_tasks: int) -> list[dict]:
    """Follow-up tasks plus the checks' exact errors: added to the task for that file, or as a new
    task when the reviewer didn't plan one. A coder fixes a named line far more reliably."""
    out = [dict(t) for t in tasks]
    for sk in skeleton:
        match = next((t for t in out if t["file"] == sk["file"]), None)
        errors = sk["instructions"].split("\n", 1)[1].rsplit("\nFix each one", 1)[0]
        if match:
            match["instructions"] += "\n\nThe exact errors the checks report in this file:\n" + errors
        elif len(out) < max_tasks:
            out.append(dict(sk))
    return out


def diagnostics_text(text: str, repo: Path, files, limit: int = 40) -> str:
    """Problems grouped by file, each with its source line from disk."""
    diags = parse_diagnostics(text, files)
    if not diags:
        return ""
    by_file: dict[str, list] = {}
    for f, ln, msg in diags:
        by_file.setdefault(f, []).append((ln, msg))
    out, shown = [f"Problems by file ({len(diags)} in {len(by_file)} file(s)):"], 0
    for f, items in by_file.items():
        out.append(f"  {f}")
        src = (repo / f).read_text(errors="replace").splitlines() if (repo / f).is_file() else []
        for ln, msg in sorted(items):
            if shown >= limit:
                break
            code = src[ln - 1].strip()[:120] if 0 < ln <= len(src) else ""
            out.append(f"    {ln}: {msg}" + (f"\n        | {code}" if code else ""))
            shown += 1
    if len(diags) > limit:
        out.append(f"  … {len(diags) - limit} more")
    return "\n".join(out)


def coverage_text(repo: Path, files, top: int = 25) -> str:
    """Uncovered lines per file from the last coverage run: Istanbul's coverage-final.json (Vitest,
    Jest, nyc) or coverage.py's coverage.json. Empty when there's no report."""
    def ranges(lines: list[int]) -> str:
        out, start, prev = [], None, None
        for n in sorted(set(lines)):
            if start is None:
                start = prev = n
            elif n == prev + 1:
                prev = n
            else:
                out.append(f"{start}-{prev}" if prev != start else str(start))
                start = prev = n
        if start is not None:
            out.append(f"{start}-{prev}" if prev != start else str(start))
        return ", ".join(out)

    rows = []
    reports = [p for pat in ("coverage/coverage-final.json", "*/coverage/coverage-final.json",
                             "*/*/coverage/coverage-final.json") for p in repo.glob(pat) if "node_modules" not in p.parts]
    for report in reports:
        try:
            data = json.loads(report.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for abs_path, cov in data.items():
            f = resolve_path(cov.get("path", abs_path), files)
            if not f:
                continue
            total = len(cov.get("s", {}))
            missed = [cov["statementMap"][k]["start"]["line"] for k, n in cov.get("s", {}).items() if n == 0]
            if missed:
                rows.append((len(missed), f, round(100 * (total - len(missed)) / max(total, 1)), ranges(missed)))
    py = repo / "coverage.json"
    if py.is_file():
        try:
            for path, cov in json.loads(py.read_text()).get("files", {}).items():
                f = resolve_path(path, files)
                missed = cov.get("missing_lines") or []
                if f and missed:
                    pct = round(cov.get("summary", {}).get("percent_covered", 0))
                    rows.append((len(missed), f, pct, ranges(missed)))
        except (json.JSONDecodeError, OSError):
            pass
    if not rows:
        return ""
    rows.sort(reverse=True)
    out = ["Uncovered lines (from the last coverage run; most uncovered first):"]
    out += [f"  {f} ({pct}% of statements covered): lines {r}" for _, f, pct, r in rows[:top]]
    if len(rows) > top:
        out.append(f"  … {len(rows) - top} more files")
    return "\n".join(out)


def check_context(results: dict, show: list[str], limit: int, repo: Path | None = None, files=None) -> str:
    """What failing checks (and any listed in `show`, e.g. coverage) said, for the planner: the
    problems parsed into file → line → message with the source line, then the end of the output."""
    parts = []
    for name, r in results.items():
        if not r["ok"] or name in show:
            state = "passes" if r["ok"] else "FAILS"
            parsed = diagnostics_text(r["output"], repo, files) if repo and files else ""
            raw = r["output"][-(limit // 2 if parsed else limit):]
            parts.append(f"--- {name} ({state}) ---\n" + (f"{parsed}\n\nEnd of the output:\n" if parsed else "") + raw)
    return "\n\n".join(parts)


def grep_text(repo: Path, pattern: str, paths: list[str], limit: int = 60) -> str:
    """A planner `grep` request, served by git grep (extended regex, tracked and untracked files)."""
    paths = [clean_path(p) for p in paths if clean_path(p)]
    r = subprocess.run(["git", "grep", "-n", "-I", "--untracked", "-E", "-e", pattern, "--", *paths],
                       cwd=repo, capture_output=True, text=True, errors="replace")
    where = f" in {', '.join(paths)}" if paths else ""
    if r.returncode == 1:
        return f"GREP /{pattern}/{where}: no matches"
    if r.returncode != 0:
        return f"GREP /{pattern}/{where}: {r.stderr.strip()[:200]}"
    lines = r.stdout.splitlines()
    body = "\n".join(f"  {l[:200]}" for l in lines[:limit])
    more = f"\n  … {len(lines) - limit} more matches" if len(lines) > limit else ""
    return f"GREP /{pattern}/{where} (exact, {len(lines)} match{'es' if len(lines) != 1 else ''}):\n{body}{more}"


STOP = set("""a an the of from to for and or but not no is are was were be been do does did have has
had we i you it its this that these those there here what which who how why when where can could should
would will shall may might must please just also only some any all each every more most other into onto
with without as if then than so about over under again new old our your my their them they he
she his her me us via per hide show add remove delete make change fix update set get let use using need
needs want wants field fields value values file files screen page view thing things way part one two
""".split())


def word_runs(text: str) -> list[list[str]]:
    """Runs of meaningful words in a request ("hide the work order details" -> [["work", "order",
    "details"]]); code names, paths and quoted strings are left to the other lookups."""
    text = re.sub(r"`[^`]*`|\"[^\"]*\"|'[^']*'|\S*[/\\.]\S*", " | ", text)
    runs, cur = [], []
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*|[^A-Za-z0-9\s]+", text):
        w = tok.lower()
        if not tok[0].isalpha() or w in STOP or len(w) < 2:
            if cur:
                runs.append(cur)
            cur = []
        else:
            cur.append(w)
    return [r for r in runs + ([cur] if cur else []) if r]


def spellings(words: list[str]) -> list[str]:
    """How a phrase is written in code: kebab, snake, camel and Pascal case."""
    camel = words[0] + "".join(w.capitalize() for w in words[1:])
    return list(dict.fromkeys(["-".join(words), "_".join(words), camel, camel[0].upper() + camel[1:]]))


def request_leads(repo: Path, request: str, files, limit_files: int = 12, prefix: str = "") -> str:
    """Where the request's own words appear, found without a model: folder and file names that spell
    a phrase of it (work order details -> work-order-details/), and code or UI text that does
    (created at -> createdAt, created_at, "Created At")."""
    out = []
    runs = word_runs(request)
    phrases = sorted({tuple(r[i:j]) for r in runs for i in range(len(r)) for j in range(i + 2, min(len(r), i + 4) + 1)},
                     key=len, reverse=True)
    taken: set[str] = set()
    placed: set[tuple] = set()  # phrases that name folders or files: places, not things to search for
    for words in phrases:
        names = {n.lower() for n in spellings(list(words))}
        hits = [f for f in files if f not in taken and any(
            part.lower().split(".")[0] in names or part.lower() in names for part in f.split("/"))]
        if hits:
            placed.update(words[i:j] for i in range(len(words)) for j in range(i + 1, len(words) + 1))
            taken.update(hits)
            shown = sorted(hits, key=len)[:limit_files]
            out.append(f"Paths spelling \"{' '.join(words)}\" ({len(hits)} file{'s' if len(hits) != 1 else ''}): "
                       + ", ".join(prefix + f for f in shown) + (" …" if len(hits) > limit_files else ""))
    for words in [p for p in phrases if len(p) <= 3 and p not in placed]:
        pattern = "|".join(re.escape(x) for x in spellings(list(words))) + "|" + "[[:space:]]+".join(map(re.escape, words))
        r = subprocess.run(["git", "grep", "-n", "-I", "-i", "-w", "-E", "-e", f"({pattern})"], cwd=repo,
                           capture_output=True, text=True, errors="replace")
        lines = [l for l in r.stdout.splitlines() if not l.startswith((MEMORY_DIR + "/",))]
        if not lines or len(lines) > 400:  # nothing, or too common a phrase to point anywhere
            continue
        by_file: dict[str, list[str]] = {}
        for l in lines:
            f, _, rest = l.partition(":")
            by_file.setdefault(f, []).append(rest)
        # hits in the request's place (the files its words name) first, then the busiest files
        rows = [f"  {prefix}{f}:{h.split(':', 1)[0]}: {h.split(':', 1)[1].strip()[:110]}"
                for f in sorted(by_file, key=lambda f: (f not in taken, -len(by_file[f])))[:6] for h in by_file[f][:2]]
        out.append(f"Code and text matching \"{' '.join(words)}\" ({len(lines)} line{'s' if len(lines) != 1 else ''} in "
                   f"{len(by_file)} file{'s' if len(by_file) != 1 else ''}):\n" + "\n".join(rows))
    return "\n\n".join(out)


def find_package_dir(repo: Path, name: str) -> Path | None:
    """An installed npm package's folder: the repo's node_modules, then each workspace package's."""
    for base in [repo, *sorted(p.parent for p in repo.glob("*/package.json")),
                 *sorted(p.parent for p in repo.glob("*/*/package.json"))]:
        d = base / "node_modules" / name
        if (d / "package.json").is_file():
            return d.resolve()
    return None


def package_docs(repo: Path, spec: str, limit: int = 9000) -> str:
    """A planner `docs` request ("typeorm" or "typeorm#LessThanOrEqual"), served from the installed
    copy, so it matches the version this repo actually uses. Python modules use their docstrings."""
    name, _, symbol = spec.strip().strip("`").partition("#")
    d = find_package_dir(repo, name)
    if d is None:
        return python_docs(repo, name, symbol, limit)
    pkg = json.loads((d / "package.json").read_text(errors="replace"))
    head = f"DOCS {name}@{pkg.get('version', '?')} (the installed copy, exact)"
    if symbol:
        r = subprocess.run(["grep", "-rn", "--include=*.d.ts", "-E", rf"\b{re.escape(symbol)}\b", "."], cwd=d,
                           capture_output=True, text=True, errors="replace")
        hits = [l for l in r.stdout.splitlines() if "node_modules" not in l][:20]
        if not hits:
            return f"{head}: no declaration of `{symbol}` in its type definitions"
        out, used, shown = [], 0, []
        for hit in hits[:10]:
            path, ln = hit.split(":", 2)[:2]
            if any(p == path and lo <= int(ln) <= hi for p, lo, hi in shown):
                continue  # already inside an excerpt
            lines = (d / path).read_text(errors="replace").splitlines()
            lo, hi = max(1, int(ln) - 6), min(len(lines), int(ln) + 8)
            shown.append((path, lo, hi))
            block = f"--- {name}/{path.lstrip('./')}:{lo}-{hi} ---\n" + "\n".join(lines[lo - 1:hi])
            if used + len(block) > limit:
                break
            out.append(block)
            used += len(block)
        return f"{head}, `{symbol}`:\n" + "\n".join(out)
    readme = next((d / n for n in ("README.md", "readme.md", "Readme.md") if (d / n).is_file()), None)
    types = pkg.get("types") or pkg.get("typings")
    parts = [head]
    if readme:
        parts.append("README (start):\n" + readme.read_text(errors="replace")[: limit // 2])
    if types and (d / types).is_file():
        parts.append(f"Type definitions ({types}, start):\n" + (d / types).read_text(errors="replace")[: limit // 2])
    return "\n\n".join(parts) + "\n(Ask for `name#Symbol` to see one declaration.)"


def python_docs(repo: Path, name: str, symbol: str, limit: int) -> str:
    venv = repo / ".venv/bin/python"
    py = str(venv) if venv.exists() else "python3"
    code = ("import importlib, inspect, sys\n"
            "m = importlib.import_module(sys.argv[1])\n"
            "o = getattr(m, sys.argv[2]) if sys.argv[2] else m\n"
            "v = getattr(m, '__version__', '?')\n"
            "print(f'DOCS {sys.argv[1]}@{v} (the installed copy, exact)')\n"
            "try: print(sys.argv[2] + str(inspect.signature(o)) if sys.argv[2] else '')\n"
            "except (TypeError, ValueError): pass\n"
            "print(inspect.getdoc(o) or '(no docstring)')\n")
    r = subprocess.run([py, "-c", code, name, symbol], cwd=repo, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        return f"DOCS {name}: not installed in this repo (no node_modules/{name}, no Python module)"
    return r.stdout[:limit]


def history_text(repo: Path, files, spec: str, limit: int = 4000) -> str:
    """A planner `history` request ("path:start-end"): who last changed those lines and the commits
    that touched them, from git."""
    m = SPEC.match(clean_path(spec))
    path = resolve_path(m.group(1), files) if m else ""
    if not path:
        return f"HISTORY {spec}: no such file"
    start = int(m.group(2) or 1)
    end = int(m.group(3) or (m.group(2) and start) or min(files.get(path, 1), 400))
    log = subprocess.run(["git", "log", f"-L{start},{end}:{path}", "-s", "-n", "8", "--date=short",
                          "--format=%h %ad %an: %s"], cwd=repo, capture_output=True, text=True, errors="replace")
    blame = subprocess.run(["git", "blame", f"-L{start},{min(end, start + 39)}", "--date=short", path],
                           cwd=repo, capture_output=True, text=True, errors="replace")
    body = (f"HISTORY {path}:{start}-{end} (exact, from git)\nCommits that changed these lines, newest first:\n"
            + (log.stdout.strip() or "(none found)") + "\nLast change per line:\n" + blame.stdout.strip())
    return body[:limit]


FIXER_GROUPS = {"js_files": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts"),
                "py_files": (".py",), "go_files": (".go",), "files": None}


ESLINT_CONFIGS = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts", ".eslintrc",
                  ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json", ".eslintrc.yml", ".eslintrc.yaml")
PRETTIER_CONFIGS = (".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.yaml",
                    ".prettierrc.yml", "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs")


def js_fixers(repo: Path, d: str, pkg: str) -> list[str]:
    """ESLint (errors only, so a touched file doesn't collect unrelated warning fixes) and Prettier
    for one folder with its own config, run from that folder with the tools installed there or at
    the root; Prettier through node, since its launcher isn't always executable."""
    here, out = repo / d, []
    up = "/".join(".." for _ in Path(d).parts) if d != "." else "."
    cd = f"cd {shlex.quote(d)} && " if d != "." else ""
    at = f"@{d}" if d != "." else ""
    tool = lambda rel: next((f"{loc}/{rel}" for loc, base in ((".", here), (up, repo)) if (base / rel).exists()), None)
    eslint = tool("node_modules/.bin/eslint")
    if eslint and any((here / n).exists() for n in ESLINT_CONFIGS):
        out.append(f"{cd}{eslint} --fix --quiet --no-warn-ignored {{js_files{at}}}")
    prettier = tool("node_modules/prettier/bin/prettier.cjs") or tool("node_modules/prettier/bin-prettier.js")
    if prettier and (any((here / n).exists() for n in PRETTIER_CONFIGS) or '"prettier":' in pkg):
        out.append(f"{cd}node {prettier} --write --ignore-unknown {{files{at}}}")
    return out


def detect_fixers(repo: Path, rc: dict) -> list[str]:
    """Formatters and lint auto-fixers the repo already uses. `.buckets.json` "fixers" overrides;
    placeholders {files}, {js_files}, {py_files}, {go_files} become the changed files ({js_files@dir}:
    the ones under dir, relative to it)."""
    if rc.get("fixers") is not None:
        return list(rc["fixers"])
    has = lambda *names: any((repo / n).exists() for n in names)
    bin_dir = repo / "node_modules/.bin"
    pkg = (repo / "package.json").read_text(errors="replace") if (repo / "package.json").exists() else ""
    out = js_fixers(repo, ".", pkg)
    # A monorepo keeps its lint and format configs in the workspace packages (packages/shared/...).
    try:
        spaces = json.loads(pkg).get("workspaces") or [] if pkg else []
    except json.JSONDecodeError:
        spaces = []
    spaces = spaces.get("packages", []) if isinstance(spaces, dict) else spaces
    for pattern in spaces:
        for pj in sorted(repo.glob(f"{pattern.rstrip('/')}/package.json")):
            d = pj.parent.relative_to(repo).as_posix()
            out += js_fixers(repo, d, pj.read_text(errors="replace"))
    if shutil.which("ruff") and has("pyproject.toml", "ruff.toml", ".ruff.toml"):
        out += ["ruff check --fix --quiet {py_files}", "ruff format --quiet {py_files}"]
    if shutil.which("gofmt") and has("go.mod"):
        out.append("gofmt -w {go_files}")
    return out


def fixer_name(cmd: str) -> str:
    """"prettier" for `cd packages/x && node ../../node_modules/prettier/bin/prettier.cjs --write …`."""
    words = re.sub(r"^cd \S+ && ", "", cmd).split()
    tool = words[1] if words[0] == "node" and len(words) > 1 else words[0]
    return re.sub(r"\.(c|m)?js$", "", tool.split("/")[-1])


def drop_leftover_blanks(repo: Path, base: str, path: str) -> bool:
    """Where this work deleted lines and left only blank lines in their place, remove those blanks
    (a removed JSX row tends to leave one behind). Nothing else in the file is touched."""
    diff = git(repo, "diff", "-U0", f"{base}..HEAD", "--", path, check=False)
    drop: set[int] = set()
    for hunk in re.split(r"(?m)^(?=@@ )", diff)[1:]:
        m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", hunk)
        removed, start, added = int(m.group(1) or 1), int(m.group(2)), int(m.group(3) if m.group(3) is not None else 1)
        plus = [l[1:] for l in hunk.splitlines()[1:] if l.startswith("+")]
        if removed and added and all(not l.strip() for l in plus):
            drop.update(range(start, start + added))
    if not drop:
        return False
    lines = (repo / path).read_text(errors="replace").split("\n")
    (repo / path).write_text("\n".join(l for i, l in enumerate(lines, 1) if i not in drop))
    return True


def formatted_before(repo: Path, base: str, path: str, cmd: str) -> bool:
    """Whether a Prettier fixer would leave the file's version at `base` as it is: a file that wasn't
    formatted before isn't reformatted top to bottom because this work changed two lines in it."""
    m = re.match(r"(?:cd (\S+) && )?node (\S+)", cmd)
    if not m:
        return True
    d = (m.group(1) or ".").strip("'\"")
    old = subprocess.run(["git", "show", f"{base}:{path}"], cwd=repo, capture_output=True, text=True, errors="replace")
    if old.returncode != 0:
        return True  # a new file: format it
    rel = path[len(d) + 1:] if d != "." else path
    r = subprocess.run(["node", m.group(2), "--stdin-filepath", rel], cwd=repo / d, input=old.stdout,
                       capture_output=True, text=True, errors="replace")
    return r.returncode == 0 and r.stdout == old.stdout


def fill_fixer(cmd: str, files: list[str]) -> str | None:
    """The command with its file placeholders filled; None when a placeholder has no files."""
    def fill(m):
        key, d = m.group(1), m.group(2)
        exts = FIXER_GROUPS[key]
        chosen = [f for f in files if exts is None or f.endswith(exts)]
        if d:
            chosen = [f[len(d) + 1:] for f in chosen if f.startswith(d + "/")]
        if not chosen:
            raise LookupError
        return " ".join(shlex.quote(f) for f in chosen)
    try:
        return re.sub(r"\{(" + "|".join(FIXER_GROUPS) + r")(?:@([^}]+))?\}", fill, cmd)
    except LookupError:
        return None


def paths_in_text(text: str, files) -> list[str]:
    """Repo files named in tool output, e.g. `src/a.ts(3,5): error` or `src/a.ts:12:4`.

    Monorepo tools often print paths relative to a package, so a unique suffix match counts too.
    """
    found: list[str] = []
    for m in re.finditer(r"[\w@.+\-/]+\.[A-Za-z]\w*", text):
        p = clean_path(m.group(0))
        if p not in files:
            inside = [f for f in files if p.endswith("/" + f)]  # absolute path into the repo
            hits = [f for f in files if f.endswith("/" + p)]    # path relative to a package
            p = max(inside, key=len) if inside else hits[0] if len(hits) == 1 else ""
        if p and p not in found:
            found.append(p)
    return found


def example_material(repo: Path, refs: list[str], limit: int) -> str:
    """`--like` examples: files are shown whole, anything else is treated as a commit or range."""
    parts = []
    for ref in refs:
        if (repo / ref).is_file():
            parts.append(numbered(repo, clean_path(ref), limit))
            continue
        if (repo / ref).is_dir():
            # A folder: its source files, smallest first, until the budget runs out.
            files = sorted(list_files(repo, []).items(), key=lambda kv: kv[1])
            inside = [f for f, _ in files if f.startswith(clean_path(ref).rstrip("/") + "/")]
            used, shown = 0, []
            for f in inside:
                block = numbered(repo, f, limit)
                if used + len(block) > limit:
                    break
                shown.append(block)
                used += len(block)
            skipped = len(inside) - len(shown)
            parts.append(f"=== example folder {clean_path(ref).rstrip('/')} ({len(inside)} files"
                         + (f", {skipped} not shown" if skipped else "") + ") ===\n" + "\n\n".join(shown))
            continue
        r = subprocess.run(["git", "show", "--stat", "--patch", "--format=commit %H%n%s%n%n%b", ref],
                           cwd=repo, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            die(f"--like {ref}: not a file in the repo, a commit or a range")
        text = r.stdout
        if len(text) > limit:
            text = text[:limit] + f"\n… (cut off; {len(text) - limit} more characters)"
        parts.append(f"=== example change {ref} ===\n{text}")
    return "\n\n".join(parts)


SHORTCUTS = {
    "ts-ignore": (r"@ts-(ignore|nocheck|expect-error)", "silences the type checker"),
    "eslint-disable": (r"eslint-disable", "turns off the linter"),
    "any": (r"\bas any\b|:\s*any\b|<any>", "uses `any`"),
    "skip-tests": (r"\b(it|test|describe)\.(skip|only|todo)\b|\bx(it|describe|test)\(|pytest\.mark\.skip|t\.Skip\(",
                   "skips or narrows tests"),
    "py-ignore": (r"#\s*type:\s*ignore|#\s*noqa", "silences a Python checker"),
    "nolint": (r"//\s*nolint|#\[allow\(", "silences the linter"),
}


def same_line(text: str) -> str:
    """A line as a formatter would leave it either way: no spacing differences, no trailing ; or ,."""
    return re.sub(r"\s+", " ", text).strip().rstrip(";,").strip()


def find_shortcuts(diff: str, allowed: list[str], generated: set | frozenset = frozenset()) -> list[str]:
    """Added lines that silence checks or skip tests instead of fixing the problem.

    Counted per file and pattern against the removed lines, so editing a line that already had
    an `any` (a rename, say) isn't flagged; only shortcuts the change adds are.
    """
    added: dict[tuple[str, str], list[str]] = {}
    removed: dict[tuple[str, str], int] = {}
    moved: dict[tuple[str, str], int] = {}  # (pattern, line) removed anywhere: code that moved between files
    file = "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if line.startswith("--- "):
            continue
        if not line.startswith(("+", "-")):
            continue
        for key, (pattern, _) in SHORTCUTS.items():
            if key in allowed or not re.search(pattern, line[1:]):
                continue
            if line[0] == "+":
                added.setdefault((file, key), []).append(line[1:].strip())
            else:
                removed[(file, key)] = removed.get((file, key), 0) + 1
                k = (key, same_line(line[1:]))
                moved[k] = moved.get(k, 0) + 1
    # Across the whole diff: when the removed lines held at least as many of a pattern as the added
    # ones, the change moved or rewrapped them (a formatter joins lines, a move adds `export`); only
    # the excess can be new.
    # `generated`: lines buckets wrote itself from existing types (a moved hook's signature).
    diff = "\n".join(l for l in diff.splitlines() if not (l.startswith("+") and same_line(l[1:]) in generated))
    plus = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    minus = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    excess = {key: len(re.findall(pattern, plus)) - len(re.findall(pattern, minus))
              for key, (pattern, _) in SHORTCUTS.items()}
    hits = []
    for (file, key), lines in added.items():
        fresh = []
        for text in lines:
            if moved.get((key, same_line(text)), 0) > 0:  # the same line left another file: moved, not added
                moved[(key, same_line(text))] -= 1
                removed[(file, key)] = max(0, removed.get((file, key), 0) - (1 if (file, key) in removed else 0))
            else:
                fresh.append(text)
        for text in fresh[removed.get((file, key), 0):]:
            if excess[key] > 0:
                excess[key] -= len(re.findall(SHORTCUTS[key][0], text)) or 1
                hits.append(f"`{file}` {SHORTCUTS[key][1]}: {text[:120]}")
    return hits


# ---------------------------------------------------------------- prompts and schemas


TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "file": {"type": "string"},
        "action": {"type": "string", "enum": ["edit", "create", "delete", "move", "share"]},
        "instructions": {"type": "string"},
        "done_when": {"type": "string"},
        "read_first": {"type": "array", "items": {"type": "string"}},
        "where": {"type": "string"},
        "edits": {"type": "array", "items": {
            "type": "object", "properties": {"at": {"type": "string"},
                                             "mode": {"type": "string", "enum": ["append", "after", "before", "replace"]},
                                             "code": {"type": "string"}},
            "required": ["at", "mode", "code"]}},
        "into": {"type": "string"},
        "name": {"type": "string"},
        "select": {"type": "array", "items": {"type": "string"}},
        "from": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "file", "action", "instructions", "done_when", "where", "edits"],
}
ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["info", "code", "pick", "git"]},
        "difficulty": {"type": "string", "enum": ["light", "heavy", "very_hard"]},
        "reason": {"type": "string"},
        "plan_only": {"type": "boolean"},
        "checks_define_done": {"type": "boolean"},
        "examples": {"type": "array", "items": {"type": "string"}},
        "commits": {"type": "array", "items": {"type": "string"}},
        "onto": {"type": "string"},
    },
    "required": ["intent", "difficulty", "reason", "plan_only", "checks_define_done", "examples", "commits", "onto"],
}
DIRS_SCHEMA = {
    "type": "object",
    "properties": {"dirs": {"type": "array", "items": {"type": "string"}}},
    "required": ["dirs"],
}
FILES_SCHEMA = {
    "type": "object",
    "properties": {"files": {"type": "array", "items": {"type": "string"}}, "reason": {"type": "string"}},
    "required": ["files"],
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "tasks": {"type": "array", "items": TASK_SCHEMA}},
    "required": ["summary", "tasks"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "tasks": {"type": "array", "items": TASK_SCHEMA},
    },
    "required": ["done", "summary", "issues", "tasks"],
}

SYSTEM = ("You are the planner in a local two-model software team. You read code and make decisions; "
          "you never edit files yourself. Be precise and concrete. When asked for JSON, reply with "
          "only the JSON object.")

ROUTE_PROMPT = """Read this request to a coding assistant and fill in the routing form.

intent: "info" (explain, review, survey, answer; nothing changes), "code" (files must be
created, edited or deleted), "pick" (cherry-pick existing commits onto a branch), or "git"
(branches, commits, squashing, rebasing, pushing, pull requests, CI status and other git or GitHub
housekeeping, with no code to write). Questions about pull requests, CI runs and their logs,
branches or commits are "git", not "info" ("info" is for questions about the code itself). Adding
entries to .gitignore, committing or pushing files that already exist, and questions about uncommitted
or untracked files are "git" too: no code gets written. If the user asks to fix, change or make
something work, it's "code", even when they also ask why.
difficulty:
- "light": mechanical or well-specified work: tests for existing code, renames, docs, small
  features with clear instructions, fixes whose cause is known, simple questions.
- "heavy": needs judgment: bugs with an unknown cause, changes across several modules, design
  choices, vague requirements, security-sensitive logic, questions about how a system behaves.
- "very_hard": architecture-level or subtle cross-cutting problems (concurrency, data
  consistency, large refactors) where a mistake is expensive.
plan_only: true only if the user asks to see a plan first, or says not to change anything yet.
checks_define_done: true only if the request is just to make the existing checks pass (fix type
errors, lint errors or failing tests) with no other change in behaviour.
examples: commits, ranges (a..b) or file paths the user explicitly says to copy, mirror or
follow ("like X", "the same way as Y"), exactly as written. Files the request says to change or
add tests to are NOT examples. Otherwise [].
commits: for "pick", the commits or ranges to cherry-pick, exactly as written; otherwise [].
onto: for "pick", the target branch if one is named; otherwise "".

Request:
{request}"""

DIRS_PROMPT = """The repository is large. Pick the directories (at most 6) whose files you need to see to {goal}.
Use paths exactly as listed.

Request:
{request}

Directories (files, lines):
{dirs}"""

FILES_PROMPT = """Pick the files you must read to {goal}. At most {max_files}.
Prefer source over generated files. Include the entry points, types and tests for the area the
request touches. If check output names files, include them. If an example change is given,
include the files that correspond to it in this request. Use paths exactly as listed.

Request:
{request}
{context}
Repository guidance:
{guidance}

Files:
{map}"""

SURVEY_PROMPT = """You are surveying part of a repository to help {goal}.
Write compact notes (under 500 words) on what in THESE files matters for the request: relevant
functions, types and exports with their signatures, how they connect, and the conventions they
follow. Give each fact its location as `path:line` (the files are numbered). Only describe what
you can see here.

Request:
{request}

{files}"""

FOCUS_SCHEMA = {
    "type": "object",
    "properties": {"read": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}},
    "required": ["read"],
}

FOCUS_PROMPT = """You're about to {goal}, but these files are too long to read whole. Below is each
file's outline: what it declares, with line ranges. Name the parts you need to see exactly, at most
{reads}: the code that will change, the code it calls or that calls it, and an existing example to
copy (for new tests, the nearest similar test and the helpers it uses). Give each as a name from the
outline ("GitAssistant.fill_in", "path:Class.method") or a line range ("path:120-180").

Request:
{request}

Already shown to you in full (don't ask for these again): {shown}

{outlines}

Parts of related repos named in the facts can be read too, as "name:path:start-end"."""

ANSWER_PROMPT = """Answer the request using the repository material below.
Write a clear markdown report. Cite code as `path:line`. If the material doesn't contain the
answer, say what's missing instead of guessing.

Request:
{request}

Repository guidance:
{guidance}

{material}"""

TASK_RULES = """Task rules (a small coding model runs each task in a fresh session; it sees only the task
text and the files it opens itself, and it cannot run commands):
- Each task changes exactly ONE file: action "edit", "create" or "delete" (or "move"/"share", below).
- Moving code between files (splitting a big file, pulling a component's logic into a hook, putting
  code that two files both have into one shared file): don't retype it, and don't put the moved code
  in "edits". Plan action "move" (TypeScript/JavaScript): "file" is the file the code is in, "where"
  the component or function it's in, "into" the new file, "name" the new hook's name, and "select"
  ["logic"] (every statement that renders nothing: state, refs, effects, handlers) or the names or
  line ranges ("120-180") of the statements to move. The system moves the code exactly and works out
  the hook's parameters, what it returns, the helpers that must come along and the imports. Action
  "share": "file" is the shared file (new or existing), "from" the two files that have the same code,
  "select" the names of the identical top-level declarations to move there; both files then import
  them. Plan other tasks only for what a move can't do (renaming, making two near-copies identical).
- Instructions must be self-contained and concrete: name the functions, types and exports to add
  or change, their signatures, and where in the file they go. Include exact code when it's short.
- "read_first" lists other files the coder should open for context (types it must use, patterns to
  copy). Keep it short.
- "where": for an edit, the function, method or class the change goes in, or next to for new code
  ("GitAssistant.fill_in", "parse_args", "GitAssistantTests.test_x"), or a line range ("120-180").
  The coder is shown exactly that code instead of reading the whole file, so give the place that
  holds what it needs. Say in the instructions exactly what to change there. "" for a new file.
- "edits": when you know the exact code, put it here too and the system applies it directly (much
  faster than a coder session; the coder takes over if an edit doesn't apply). Each edit: "at" is a
  name from the outline ("GitAssistant.op_gitignore", "parse_args") or a few complete lines copied
  exactly from the code shown, never the file's path; "mode" is "append" (add the code at the end of the named class, dict
  or list: a new method, test or entry; the system finds the end), "after" (add the code after it),
  "before", or "replace" (the code replaces it: for a name, the whole function or class); "code" is
  the new code, with its indentation. Example: {{"at": "GitAssistant.op_gitignore", "mode": "after",
  "code": "    def op_status(self, a, yes):\\n        return self.git(\\"status\\", \\"-sb\\")"}}.
  A new file: one edit with "at" "" and "mode" "replace". The edits must do the WHOLE task (the
  system marks it done once they apply); if you can't write all of it exactly, give [] and let the
  coder do it from the instructions.
- Order tasks so dependencies come first.
- If the change alters behavior and the repo has tests, include a task that adds or updates tests.
- Fix problems properly. Never plan `any`, `@ts-ignore`, lint-disable comments, or skipped,
  deleted or weakened tests as a fix.
- No tasks for formatting, running checks or committing; the system does those.
- Keep each task small enough for one short session: if one file needs a big change (more than
  ~150 changed lines), split it into several tasks on that file, in order ("part 1 of 3: …").
  For files over ~800 lines, say which line range the coder should read. When you know where a
  function or class lives (e.g. `compare()` at conditions.ts:314-330), say so in the instructions.
- At most {max_tasks} tasks."""

PLAN_PROMPT = """Plan the change below.

{rules}
If the request is bigger than {max_tasks} tasks, plan the first coherent slice and say so in
"summary". If it's unclear or impossible, return no tasks and explain in "summary".
If check output is given below, the tasks must fix every error in it that the request covers.
If an example change is given, follow its structure: touch the same kinds of files in the same
way, and put the example's files in "read_first" so the coder can copy the pattern.
Use task ids T1, T2, ...

Request:
{request}

Checks the system runs after the tasks: {checks}
Baseline (before any change): {baseline}

Repository guidance:
{guidance}

{material}"""

REVIEW_PROMPT = """Review the work done for this request and decide whether it is fully and correctly done.

Request:
{request}

Plan summary: {summary}

Task results:
{results}

Checks (baseline -> now):
{checks}
{shortcuts}{impact}
Diff from the starting point:
{diff}

Rules:
- A check that passed at baseline and fails now is a regression; the work is NOT done until it's fixed.
- Shortcuts don't count as fixes: `any`, `@ts-ignore`, lint-disable comments, and skipped, deleted
  or weakened tests. If the diff adds any, the work is NOT done; plan tasks to fix the real problem.
- For every caller listed as not updated after an API change, do the best thing: plan a task to
  update it, keep the old form working (an optional parameter, an overload or an alias), or say
  in the summary why it's safe. Leaving one unexplained means NOT done.
- Tests must check what the code returns or does (values, messages, effects), not only how many
  results there are or that something exists. When the request is about tests, list weak
  assertions as issues and plan tasks to strengthen them.
- Look for: parts of the request that are missing, wrong logic, broken imports or exports,
  leftover or duplicated code, files that should have changed but didn't, tasks that failed.
- If not done, write follow-up tasks that fix only what's wrong. Don't redo correct work.
{agent_rules}{rules}
Reply with done, a short summary, the issues you found, and the follow-up tasks (empty when done)."""

DEFINITE_CLAIM = re.compile(r"\b(undefined|not defined|never defined|isn't defined|never called|not called anywhere|"
                            r"doesn't exist|does not exist|NameError|not imported|missing import|unresolved|"
                            r"has no attribute|no such (?:function|method|attribute|variable))\b", re.I)

CONFIRM_PROMPT = """Every check passes, but the other reviewer claims these definite errors in the same change:
{claims}

Where the names they mention appear in the changed files now (git grep):
{evidence}

Check each claim against the code shown above. A claim is real only if the code shows it: a name
set earlier in the function, imported, or defined elsewhere is not undefined. Reply with the numbers
of the claims that are real (an empty list if none) and one sentence on why."""

CONFIRM_SCHEMA = {"type": "object", "properties": {"real": {"type": "array", "items": {"type": "integer"}},
                                                   "why": {"type": "string"}}, "required": ["real", "why"]}

CODER_PROMPT = """You are working in a git repository. Complete exactly one small task, then stop.

Overall goal (context only, don't do more than your task): {request}

YOUR TASK ({id}): {action} the file `{file}`.
{instructions}

Done when: {done_when}
{read_hint}
Rules:
- Change only `{file}`. Don't create, edit or delete any other file.
- Paths are relative to the repository root. Open files directly by path; don't search for them.
- You can't run shell commands; don't try. Type checks, lint and tests run after you finish.
- Follow the existing code style. Make the smallest change that does the task.
- Fix problems properly. Never use `any`, `as any`, `@ts-ignore`, `@ts-expect-error`, lint-disable
  comments, or skip, delete or weaken tests to make checks pass. If you can't fix it properly,
  reply BLOCKED.
- When finished, reply with one line: "DONE: <what you changed>" or "BLOCKED: <why>"."""


CONFLICT_PROMPT = """A cherry-pick hit conflicts. Plan how to resolve them.

Commit being applied ({sha}): {subject}
{body}

Conflicted files: {files}

For each conflicted file write ONE task (action "edit") that tells the coder exactly what the
final file must contain at each conflict block: keep one side, or combine them (give the exact
combined code). Keep the intent of the commit being applied, adapted to the target branch.
The result must contain no conflict markers (<<<<<<<, =======, >>>>>>>).
{rules}

The commit's own change to these files:
{patch}

The files now, with conflict markers (between <<<<<<< and ======= is the target branch,
between ======= and >>>>>>> is the commit being applied):
{files_text}"""

RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "enough": {"type": "boolean"},
        "read": {"type": "array", "items": {"type": "string"}},
        "uses": {"type": "array", "items": {"type": "string"}},
        "docs": {"type": "array", "items": {"type": "string"}},
        "history": {"type": "array", "items": {"type": "string"}},
        "grep": {"type": "array", "items": {
            "type": "object",
            "properties": {"pattern": {"type": "string"},
                           "paths": {"type": "array", "items": {"type": "string"}}},
            "required": ["pattern", "paths"]}},
        "questions": {"type": "array", "items": {
            "type": "object",
            "properties": {"ask": {"type": "string"},
                           "where": {"type": "array", "items": {"type": "string"}}},
            "required": ["ask", "where"]}},
        "hypotheses": {"type": "array", "items": {
            "type": "object",
            "properties": {"cause": {"type": "string"},
                           "status": {"type": "string", "enum": ["open", "confirmed", "ruled_out"]},
                           "evidence": {"type": "string"}},
            "required": ["cause", "status", "evidence"]}},
    },
    "required": ["enough", "read", "uses", "grep", "docs", "history", "questions", "hypotheses"],
}

RESEARCH_PROMPT = """You are preparing to {goal}. You can't open files yourself, but you have these
ways to learn the code (all exact and instant except "questions"):

1. "read": files or line ranges you want to see. The system shows you their exact text at once.
   Write "path" for a whole file or "path:start-end" for lines. The file list below gives each
   function's line range, so ask for just the lines you need.
2. "uses": symbol names, or "name@path" for the one defined in that file. The system lists every
   place each is imported and used, across packages, exactly and at once.
3. "grep": a regular expression (extended syntax) and optional paths to limit it to. The system
   returns every matching line with its location, exactly and at once.
4. "docs": a dependency's documentation from the installed copy (the version this repo uses):
   "typeorm" for its README and types, or "typeorm#LessThanOrEqual" for one declaration.
5. "history": "path:start-end" for the commits that changed those lines and who changed each line.
6. "questions": searches and scans for a helper model that reads the repository quickly. It can
   look across several files and answers with short located leads ("path:line: what's there"),
   and the system attaches the real lines behind every lead. Its descriptions are hints; judge
   from the real lines. Use it for what you can't get by path: where something is defined or
   used, which tests cover something, scanning a set of files for a pattern or a likely cause.
   In "where", list files or folders to look in, if you know them. Don't ask it to decide.

Request:
{request}

Repository guidance:
{guidance}

Files:
{map}

{findings}
For a bug or a failure, keep "hypotheses": the possible causes (at least two until one is
confirmed), each "open", "confirmed" or "ruled_out", with the evidence (a `path:line` you've seen).
Look up whatever would confirm or rule out the open ones. Don't stop at the first plausible place:
trace the failing behaviour to the code that decides it. For other requests, leave it empty.

If you know enough to {goal} with exact names, signatures and locations (and, for a bug, one cause
is confirmed by code you've seen and the others are ruled out), reply "enough": true
with every request list empty. Otherwise ask for what fills the gaps: up to {reads} each of
reads, uses, greps, docs and history, and {questions} questions. Prefer "read", "uses" and
"grep" (exact, instant) over questions (slower, approximate); keep questions for what a literal
search can't find. Don't repeat anything already above."""

ERRAND_PROMPT = """Answer ONE question about this repository by searching and reading it. You can read,
grep, glob and list; you can't change anything.

Question: {ask}
{where}
Answer with located leads, one per line, most relevant first, at most 12:
- `path:line` or `path:start-end`: what's there, or why it matters (one short sentence)
Then, if it helps, one line starting "Summary:" with at most 3 sentences.

Rules:
- Paths relative to the repository root. Line numbers must be real: the system shows the actual
  lines behind every lead, so a wrong number is caught.
- Don't paste code; the system attaches it.
- Search first (grep, glob), then read only what you need.
- If nothing matches, say so and where you looked.
- Finish with a line containing only END."""

CONDENSE_PROMPT = """These are findings from reading a repository to {goal}. Condense them into
compact notes that keep every fact needed for that: exact names, signatures, `path:line`
locations, and short code quotes that matter. Drop repetition and anything irrelevant.

Request:
{request}

Findings:
{findings}"""

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "string"}, "instructions": {"type": "string"}, "done_when": {"type": "string"}},
        "required": ["id", "instructions", "done_when"]}}},
    "required": ["tasks"],
}

REWRITE_PROMPT = """A smaller coding model keeps failing these tasks ({why}). Rewrite each task's instructions
so it can't miss: say exactly which lines to change and give the exact new code. Keep the task ids.
The code shown is the file as it is now.

Overall request: {request}

{tasks}"""

SECOND_OPINION = """
You are giving a second opinion. The first reviewer said this work is done, but it changes
sensitive code ({files}). Check it carefully for correctness and security problems before agreeing.
"""

CLAUDE_INTRO = """You are the planner and reviewer in a team where a smaller local model writes the code.
You're in the repository root with read-only tools (Read, Grep, Glob). Explore only as much as the
request needs: read the relevant files, follow imports, look at neighbouring tests. Be precise:
the coder will follow your words literally."""

CLAUDE_ANSWER_PROMPT = CLAUDE_INTRO + """

Answer the request as a clear markdown report. Cite code as `path:line`. Don't change anything.

Request:
{request}
{context}"""

CLAUDE_PLAN_PROMPT = CLAUDE_INTRO + """

Plan the change below for the coder.

{rules}
If the request is bigger than {max_tasks} tasks, plan the first coherent slice and say so in
"summary". If it's unclear or impossible, return no tasks and explain in "summary".
If check output is given, the tasks must fix every error in it that the request covers.
If an example is given, follow its structure and put its files in "read_first".
Because the coder is weaker than you, put the exact code in the instructions for new or changed
logic. For mechanical edits (renames, import updates, repeated patterns), list the exact
replacements instead (old -> new, with line numbers) rather than pasting whole files. Keep
each task's instructions under about 400 words.
Use task ids T1, T2, ...

Request:
{request}

Checks the system runs after the tasks: {checks}
Baseline (before any change): {baseline}
{context}"""


# ---------------------------------------------------------------- validation


def clean_path(p: str) -> str:
    p = p.strip().strip("`").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


MOVABLE = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts")
MOVE_REQUEST = re.compile(r"\b(mov\w*|extract\w*|split\w*|separat\w*|pull\w*)\b", re.I)
MOVE_HINT = """A mechanical move fits this request: this task moves the code exactly, with no retyping:
{task}
Dry run: it moves {moved} statements ({lines} lines) of `{where}` into `{name}` in `{into}`. The hook takes
{params} and returns {count} values ({returns}); private helpers that move along: {helpers}.{notes}
Plan this as T1 (copy it). Add tasks only for what the request asks beyond the move; checks run after it."""
MERGE_WORDS = re.compile(r"\b(merg\w*|shar\w*|duplicat\w*|common|diverg\w*|join\w*|unif\w*|dedup\w*|variants?)\b", re.I)


def shared_hook_names(shared_file: str, variant: str) -> tuple[str, str]:
    """Where the shared hook of two variants goes and what it's called: next to the shared file,
    `use-<screen>-shared.ts` and `use<Screen>Shared`."""
    stem = re.sub(r"\.(tsx?|jsx?)$", "", PurePosixPath(variant).name)
    folder = PurePosixPath(shared_file).parent
    pascal = "".join(w[:1].upper() + w[1:] for w in re.split(r"[-_.]", stem) if w)
    return str(folder / f"use-{stem}-shared.ts"), f"use{pascal}Shared"


def variant_pairs(request: str, files: list[str]) -> list[tuple[str, str]]:
    """Files that are two variants of one file: the same name in sibling folders (`screens/x/ecotrak/a.tsx`
    and `screens/x/ecotrak-techs/a.tsx`), among the files and folders the request names."""
    by_place: dict[tuple[str, str], list[str]] = {}
    for f in files:
        parts = f.split("/")
        if len(parts) >= 3 and f.endswith(MOVABLE):
            by_place.setdefault(("/".join(parts[:-2]), parts[-1]), []).append(f)
    groups = [sorted(g, key=len) for g in by_place.values() if len(g) == 2]
    sides = {g.split("/")[-2] for group in groups for g in group}  # "ecotrak", "ecotrak-techs": not a place
    words = {w.strip("`'\".,:;()").rstrip("/") for w in re.split(r"\s+", request)} - sides
    def named(f: str) -> bool:
        return f in words or any("/" + w + "/" in "/" + f for w in words
                                 if len(w) > 3 and ("-" in w or "/" in w or "_" in w))
    return sorted(tuple(g) for g in groups if any(named(x) for x in g))


def variants_text(cmp: dict, limit: int = 3500) -> str:
    """The mechanical comparison of two variants, for the planner: what can be shared as it is, what
    differs a little (with the differing lines), and what only one side has."""
    pairs = cmp["pairs"]
    def names(status, top=None):
        return [p for p in pairs if p["status"] == status and (top is None or p.get("module_level") == top)]
    out = [f"Two variants of one file, compared declaration by declaration: `{cmp['a']}` ({cmp['lines'][0]} lines) "
           f"and `{cmp['b']}` ({cmp['lines'][1]} lines)."]
    same_top = names("identical", True)
    if same_top:
        out.append("Identical top-level declarations (a \"share\" task can move them into one file): "
                   + ", ".join(f"`{p['name']}`" for p in same_top))
    same_inner = [p for p in names("identical") if p.get("depth") == 1]
    if same_inner:
        out.append("Identical inside a component or function (they use its state; move that logic into a "
                   "shared hook first): " + ", ".join(f"`{p['parent']}.{p['name']}`" for p in same_inner[:30]))
    near = sorted(names("near"), key=lambda p: -p["similarity"])
    for p in near[:8]:
        out.append(f"Near copy `{p.get('parent') + '.' if p.get('parent') else ''}{p['name']}` ({p['similarity']:.0%} alike; "
                   f"a {cmp['a'].split('/')[-2]}:{p['a'][0]}-{p['a'][1]}, b {cmp['b'].split('/')[-2]}:{p['b'][0]}-{p['b'][1]}); "
                   f"lines only in a (-) or b (+):\n" + "\n".join("  " + l for l in p.get("diff", "").splitlines()[:10]))
    if len(near) > 8:
        out.append(f"… {len(near) - 8} more near copies: " + ", ".join(f"`{p['name']}`" for p in near[8:30]))
    diff = names("different")
    if diff:
        out.append("Same name, different code: " + ", ".join(f"`{p['name']}` ({p['similarity']:.0%})" for p in diff[:20]))
    for side, key in (("a", "only_a"), ("b", "only_b")):
        only = names(key)
        if only:
            out.append(f"Only in {side}: " + ", ".join(f"`{p['name']}`" for p in only[:25]) + (" …" if len(only) > 25 else ""))
    text = "\n".join(out)
    return text[:limit] + ("\n… (cut off)" if len(text) > limit else "")


def validate_move(t: dict, f: str, repo: Path, task_id: str) -> dict:
    """A mechanical move ("move": a component's logic into a new hook file; "share": identical
    declarations of two files into one), checked before anything runs."""
    def inside(p: str) -> str:
        p = clean_path(str(p or ""))
        if not p or p.startswith("/") or ".." in p.split("/"):
            raise ValueError(f"path {p!r} in task {t.get('id', task_id)} must be relative to the repo root")
        return p
    select = [str(x).strip() for x in t.get("select") or [] if str(x).strip()]
    base = {"id": task_id, "action": t["action"], "instructions": str(t.get("instructions") or "").strip(),
            "done_when": str(t.get("done_when") or "").strip(), "read_first": [], "edits": []}
    if t["action"] == "move":
        into, name, where = inside(t.get("into")), str(t.get("name") or "").strip(), str(t.get("where") or "").strip()
        if not f.endswith(MOVABLE) or not (repo / f).is_file():
            raise ValueError(f"move: {f!r} must be an existing TypeScript or JavaScript file")
        if not into.endswith(MOVABLE) or (repo / into).exists():
            raise ValueError(f"move: \"into\" ({into!r}) must be a new .ts/.tsx/.js file")
        if not re.fullmatch(r"[A-Za-z_$][\w$]*", name) or not re.fullmatch(r"[A-Za-z_$][\w$]*", where.split(".")[-1]):
            raise ValueError("move: \"name\" (the new hook) and \"where\" (the component it comes from) must be names")
        return {**base, "file": f, "into": into, "name": name, "where": where.split(".")[-1],
                "select": "logic" if not select or [x.lower() for x in select] == ["logic"] else select}
    sources = [inside(x) for x in t.get("from") or []]
    if len(sources) != 2 or not all((repo / x).is_file() and x.endswith(MOVABLE) for x in sources):
        raise ValueError("share: \"from\" must name the two existing TypeScript/JavaScript files that both have the code")
    if not f.endswith(MOVABLE) or not select:
        raise ValueError("share: \"file\" is the shared .ts/.tsx file and \"select\" the names to move into it")
    return {**base, "file": f, "from": sources, "select": select, "where": ""}


def validate_tasks(tasks: list, repo: Path, max_tasks: int, prefix: str) -> list[dict]:
    out = []
    for t in tasks[:max_tasks]:
        f = clean_path(str(t["file"]))
        if not f or f.startswith("/") or ".." in f.split("/"):
            raise ValueError(f"task file path {t['file']!r} must be relative to the repo root")
        action = t["action"]
        if action in ("move", "share"):
            try:
                out.append(validate_move(t, f, repo, f"{prefix}{len(out) + 1}"))
            except ValueError as e:
                if len(tasks) == 1:
                    raise
                say(f"  {warn()} dropped a {action} task that can't run ({str(e)[:140]})")
            continue
        exists = (repo / f).is_file()
        if action in ("edit", "delete") and not exists:
            if action == "delete":
                continue
            action = "create"
        if action == "create" and exists:
            action = "edit"
        reads = [clean_path(r) for r in (t.get("read_first") or []) if (repo / clean_path(r)).is_file()]
        out.append({"id": f"{prefix}{len(out) + 1}", "file": f, "action": action,
                    "instructions": str(t["instructions"]).strip(),
                    "done_when": str(t["done_when"]).strip(), "read_first": reads[:6],
                    "where": str(t.get("where") or "").strip(),
                    "edits": [{"at": str(e.get("at") or ""), "code": str(e["code"]),
                               "mode": e.get("mode") if e.get("mode") in ("append", "after", "before") else "replace"}
                              for e in t.get("edits") or [] if isinstance(e, dict) and "code" in e]})
    return out


def validate_route(data: dict) -> dict:
    if data.get("intent") not in ("info", "code", "pick", "git"):
        raise ValueError('"intent" must be "info", "code", "pick" or "git"')
    if data.get("difficulty") not in ("light", "heavy", "very_hard"):
        raise ValueError('"difficulty" must be "light", "heavy" or "very_hard"')
    return {"intent": data["intent"], "difficulty": data["difficulty"], "reason": str(data.get("reason", "")),
            "plan_only": bool(data.get("plan_only")),
            "checks_define_done": bool(data.get("checks_define_done")),
            "examples": [str(x) for x in data.get("examples") or []],
            "commits": [str(x) for x in data.get("commits") or []], "onto": str(data.get("onto") or "")}


def validate_plan(data: dict, repo: Path, max_tasks: int, prefix: str = "T") -> dict:
    if not isinstance(data.get("tasks"), list):
        raise ValueError('"tasks" must be a list')
    return {"summary": str(data["summary"]), "tasks": validate_tasks(data["tasks"], repo, max_tasks, prefix)}


# ---------------------------------------------------------------- project memory (.buckets/)


MEMORY_DIR = ".buckets"
JS_IMPORT = re.compile(r"""(?:from\s+|import\s*\(?\s*|require\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]""")
JS_EXT = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js")


def first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:limit]


def described(repo: Path, path: str) -> str:
    """What a file says it is, for free: a Python module docstring, or a JS/TS file's leading comment."""
    try:
        text = (repo / path).read_text(errors="replace")[:4000]
    except OSError:
        return ""
    if path.endswith(".py"):
        try:
            doc = ast.get_docstring(ast.parse((repo / path).read_text(errors="replace")))
        except (SyntaxError, ValueError):
            doc = None
        return first_sentence(doc) if doc else ""
    if path.endswith((".md", ".rst", ".txt")):  # the first paragraph that isn't a heading, list or table
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if para and not re.match(r"[#=\-*|>`<!\[]", para):
                return first_sentence(para)
        return ""
    m = re.match(r"\s*(?:#![^\n]*\n\s*)?(?:/\*\*?(.*?)\*/|((?://[^\n]*\n\s*)+))", text, re.S)
    if not m:
        return ""
    body = m.group(1) or m.group(2) or ""
    body = re.sub(r"^\s*(\*|//)\s?", "", body, flags=re.M)
    return "" if re.search(r"copyright|license|eslint|@ts-", body, re.I) else first_sentence(body)


def file_imports(repo: Path, path: str, files) -> list[str]:
    """Repo files this file imports: Python modules by name, JS/TS by relative path."""
    out: list[str] = []
    try:
        text = (repo / path).read_text(errors="replace")
    except OSError:
        return out
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return out
        here = Path(path).parent

        def add(mod: Path) -> None:
            for cand in (f"{mod.as_posix()}.py", f"{mod.as_posix()}/__init__.py"):
                cand = cand.removeprefix("./")
                if cand in files and cand != path and cand not in out:
                    out.append(cand)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    add(Path(a.name.replace(".", "/")))
                    add(here / a.name.replace(".", "/"))  # a script importing its neighbour
            elif isinstance(node, ast.ImportFrom):
                base = here if node.level else Path()
                for _ in range(max(node.level - 1, 0)):
                    base = base.parent
                mod = base / node.module.replace(".", "/") if node.module else base
                if node.module:
                    add(mod)
                for a in node.names:  # from . import util: a submodule
                    add(mod / a.name)
        return out
    if path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
        for spec in JS_IMPORT.findall(text):
            base = os.path.normpath(os.path.join(os.path.dirname(path), spec))
            hit = next((base + e for e in JS_EXT if base + e in files), None)
            if hit and hit != path and hit not in out:
                out.append(hit)
    return out


class ProjectMemory:
    """`.buckets/` in the repo, so runs stop rediscovering the project:
    - map.json / map.md: one line per file (what it is, what it declares, what it imports and what
      imports it), kept in step with the files by content hash. The structure is mechanical; the
      "what it is" comes from docstrings, or is written once by a model the first time a run
      involves that file. Commit these: everyone's buckets gets the project knowledge.
    - journal.md: the latest runs in a few lines each (what was asked, what changed, the outcome),
      so "add a test for that" has a "that". Personal; git-ignored.
    Both are cut to a token budget before a model sees them."""

    def __init__(self, repo: Path):
        self.repo, self.dir = repo, repo / MEMORY_DIR
        try:
            self.data = json.loads((self.dir / "map.json").read_text())
        except (OSError, json.JSONDecodeError):
            self.data = {"files": {}}
        self.files: dict = self.data.setdefault("files", {})

    def refresh(self, files: dict[str, int], outline: dict) -> int:
        """Bring the map in step with the files; returns how many entries changed (no model involved)."""
        changed = 0
        for f, n in files.items():
            try:
                digest = hashlib.sha1((self.repo / f).read_bytes()).hexdigest()[:12]
            except OSError:
                continue
            old = self.files.get(f)
            if old and old["hash"] == digest:
                continue
            # what matters most first: classes, then the largest functions (not the first names in the file)
            top = [d for d in decls_of(self.repo, f, outline) if "." not in d["name"]]
            top.sort(key=lambda d: (not d["signature"].startswith("class"), d["line"] - d["end"]))
            decls = [d["name"] for d in top]
            mine = described(self.repo, f)
            learned = old and old.get("source") == "model" and old.get("purpose")
            self.files[f] = {"hash": digest, "lines": n, "declares": decls[:12],
                             "imports": file_imports(self.repo, f, files),
                             "purpose": mine or (old["purpose"] if learned else ""),
                             "source": "docstring" if mine else ("model" if learned else "")}
            changed += 1
        for gone in [f for f in self.files if f not in files]:
            del self.files[gone]
            changed += 1
        if changed:
            self.save()
        return changed

    def save(self) -> None:
        self.dir.mkdir(exist_ok=True)
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text("journal.md\n")
        (self.dir / "map.json").write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
        (self.dir / "map.md").write_text("# Project map\n\nWritten by buckets: what each file is and how they "
                                         "connect. Kept in step with the files by content hash.\n\n"
                                         + self.render(10 ** 9) + "\n")

    def render(self, budget: int = 1500, focus=()) -> str:
        """The map within `budget` tokens: one line per file with what it declares and connects to;
        shorter lines when that's too long; then one line per folder, with the focus files' folders
        still listed file by file."""
        used_by: dict[str, list[str]] = {}
        for f, e in self.files.items():
            for imp in e.get("imports", []):
                used_by.setdefault(imp, []).append(f)

        def line(f: str, full: bool) -> str:
            e = self.files[f]
            text = f"- {f} ({e['lines']} line{'s' if e['lines'] != 1 else ''})" + (f": {e['purpose']}" if e.get("purpose") else "")
            if full:
                if e.get("declares"):
                    text += f" Declares {', '.join(e['declares'][:8])}{', …' if len(e['declares']) > 8 else ''}."
                if e.get("imports"):
                    text += f" Imports {', '.join(e['imports'][:4])}."
                if used_by.get(f):
                    text += f" Used by {', '.join(sorted(used_by[f])[:4])}{', …' if len(used_by[f]) > 4 else ''}."
            return text

        names = sorted(self.files)
        for full in (True, False):
            text = "\n".join(line(f, full) for f in names)
            if est_tokens(text) <= budget:
                return text
        dirs: dict[str, list[str]] = {}
        for f in names:
            dirs.setdefault(os.path.dirname(f) or ".", []).append(f)
        near = {os.path.dirname(f) or "." for f in focus}
        out = []
        for d, fs in sorted(dirs.items()):
            if d in near:
                out += [line(f, False) for f in fs]
            else:
                about = [self.files[f]["purpose"] for f in fs if self.files[f].get("purpose")][:2]
                out.append(f"- {d}/ ({len(fs)} files, {sum(self.files[f]['lines'] for f in fs)} lines)"
                           + (f": {' / '.join(about)}" if about else ""))
        text = "\n".join(out)
        return text[: budget * 3] + ("\n… (cut to fit)" if len(text) > budget * 3 else "")

    def learn(self, b, files: list[str], limit: int = 6) -> None:
        """One short model call: a sentence on what each involved, undescribed file is, from its
        declarations and first lines (not the whole file)."""
        todo = [f for f in dict.fromkeys(files) if f in self.files and not self.files[f].get("purpose")][:limit]
        if not todo or not getattr(b.run, "talked", False):  # only when a model is known to be up
            return
        blocks = []
        for f in todo:
            head = "\n".join((self.repo / f).read_text(errors="replace").splitlines()[:25])[:1500]
            e = self.files[f]
            blocks.append(f"=== {f} ({e['lines']} lines; declares: {', '.join(e['declares'][:12]) or 'nothing'}) ===\n{head}")

        def check(d):
            got = {clean_path(str(x.get("file", ""))): str(x.get("purpose", "")).strip()
                   for x in d.get("files") or [] if isinstance(x, dict)}
            return {f: first_sentence(v, 240) for f, v in got.items() if f in todo and v}

        say(f"  Noting what {len(todo)} file{'s are' if len(todo) != 1 else ' is'} for, in {MEMORY_DIR}/map.md")
        got = ask_json(b.chores, b.run, "map", SYSTEM, PURPOSE_PROMPT.format(files="\n\n".join(blocks)),
                       PURPOSE_SCHEMA, check, think=False, max_tokens=900, timeout=b.timeout)
        for f, purpose in got.items():
            self.files[f].update(purpose=purpose, source="model")
        if got:
            self.save()

    def journal_entries(self) -> list[str]:
        try:
            text = (self.dir / "journal.md").read_text()
        except OSError:
            return []
        return [e.strip() for e in re.split(r"(?m)^(?=## )", text) if e.startswith("## ")]

    def note(self, kind: str, outcome: str, request: str, changed: list[str], summary: str) -> None:
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = (f"## {when} · {kind} · {outcome}\nAsked: {' '.join(request.split())[:300]}\n"
                 + (f"Changed: {', '.join(changed[:12])}{' …' if len(changed) > 12 else ''}\n" if changed else "")
                 + (f"Result: {' '.join(summary.split())[:500]}\n" if summary.strip() else ""))
        entries = (self.journal_entries() + [entry.strip()])[-20:]
        self.dir.mkdir(exist_ok=True)
        if not (self.dir / ".gitignore").exists():
            (self.dir / ".gitignore").write_text("journal.md\n")
        (self.dir / "journal.md").write_text("# buckets journal\n\nThe latest runs here, newest last.\n\n"
                                             + "\n\n".join(entries) + "\n")

    def recent(self, mentions=(), budget: int = 600) -> str:
        """The last few runs, plus older ones that touched the files in `mentions`, within `budget`."""
        entries = self.journal_entries()
        if not entries:
            return ""
        keep = entries[-3:]
        older = [e for e in entries[:-3] if any(m in e for m in mentions)]
        picked = older[-3:] + keep
        while picked and est_tokens("\n\n".join(picked)) > budget:
            picked.pop(0)
        return "\n\n".join(picked)


PURPOSE_SCHEMA = {
    "type": "object",
    "properties": {"files": {"type": "array", "items": {
        "type": "object", "properties": {"file": {"type": "string"}, "purpose": {"type": "string"}},
        "required": ["file", "purpose"]}}},
    "required": ["files"],
}

PURPOSE_PROMPT = """For each file below, write one sentence (at most 25 words) on what it is for in this
project: its role, not a list of its functions. You see what it declares and its first lines.

{files}"""


# ---------------------------------------------------------------- the pipeline


class Buckets:
    def __init__(self, cfg: dict, repo: Path, request: str, like: list[str] | None = None):
        self.cfg, self.repo, self.request = cfg, repo, request
        self.like = like or []
        self.checks_seen = ""
        self.rc = {**path_rules(cfg, repo), **repo_config(repo)}
        self.mode = cfg.get("mode_override") or self.rc.get("mode") or cfg["mode"]
        if self.mode not in ("local", "claude", "auto"):
            die(f'mode must be "local", "claude" or "auto", not {self.mode!r}')
        self.cost = 0.0
        self.difficulty = "light"
        self.total = 5
        self.run = Run(repo)
        self.planner = cfg["planner"]
        self.timeout = cfg["request_timeout_s"]
        self.run.save("request.md", request)

    def heading(self, n: int, title: str) -> None:
        heading(n, self.total, title)

    @property
    def memory(self) -> ProjectMemory | None:
        """The repo's `.buckets/` notes, brought up to date with the files (off with "memory": false)."""
        if not hasattr(self, "_memory"):
            self._memory = None
            if self.rc.get("memory", self.cfg.get("memory", True)):
                fresh = not (self.repo / MEMORY_DIR / "map.json").exists()
                self._memory = ProjectMemory(self.repo)
                self._memory.refresh(self.index.files, self.index.outline())
                if fresh and self._memory.files:
                    say(f"  {ok()} started {MEMORY_DIR}/ here: a map of the project and a journal of runs "
                        f"(commit map.md and map.json to share the map; the journal stays yours)")
        return self._memory

    @property
    def related(self) -> dict[str, Path]:
        """Other repos this one depends on that runs may read, never change: `"related": {"ecotrak-api":
        "~/Coding/ET/web_api/ecotrak-api"}` in .buckets.json or a config "repos" rule."""
        return {name: Path(os.path.expanduser(str(p))).resolve() for name, p in (self.rc.get("related") or {}).items()
                if Path(os.path.expanduser(str(p))).is_dir()}

    def related_files(self, name: str) -> dict[str, int]:
        if not hasattr(self, "_related_files"):
            self._related_files = {}
        if name not in self._related_files:
            self._related_files[name] = list_files(self.related[name], [])
        return self._related_files[name]

    @property
    def index(self) -> CodeIndex:
        if not hasattr(self, "_index"):
            self._index = CodeIndex(self.repo, list_files(self.repo, self.rc.get("exclude", [])))
        return self._index

    def code_map(self, files: dict[str, int]) -> str:
        outline = self.index.outline()
        return render_outline(files, outline) if outline else render_map(files)[:60000]

    def checks_with_index(self, checks: dict[str, str]) -> dict:
        """The repo's checks plus, for TypeScript/JavaScript, a mechanical one: imports that name
        something the imported file doesn't export (a rename that missed a caller, across packages)."""
        return {**checks, **({INDEX_CHECK: "(code index)"} if self.index.ts and self.rc.get("index_check", True) else {})}

    # -- reading

    def goal(self, mode: str) -> str:
        return "answer the request" if mode == "info" else "plan the requested code change"

    def claude(self, label: str, prompt: str, schema: dict | None = None, validate=None, **kw):
        """A Claude call with one retry when its JSON doesn't validate; adds to the run's cost."""
        try:
            kw.setdefault("add_dirs", list(self.related.values()))
            data, cost = claude_call(self.cfg, self.run, label, prompt, self.repo, schema=schema, **kw)
        except SchemaGaveUp:
            # Long answers sometimes can't be forced into the schema; ask for plain JSON text instead.
            say(f"  {label}: structured output failed; asking for plain JSON instead")
            text, cost = claude_call(self.cfg, self.run, f"{label}-text",
                                     f"{prompt}\n\nReply with only one JSON object matching this JSON "
                                     f"schema, no prose:\n{json.dumps(schema)}", self.repo, **kw)
            self.cost += cost
            data = extract_json(text or "")
            return validate(data) if validate else data
        self.cost += cost
        if not validate:
            return data
        try:
            return validate(data)
        except (ValueError, KeyError, TypeError) as e:
            say(f"  {label}: the reply wasn't usable ({e}); asking once more")
            data, cost = claude_call(self.cfg, self.run, f"{label}-retry",
                                     f"{prompt}\n\nYour previous reply was rejected: {e}", self.repo,
                                     schema=schema, **kw)
            self.cost += cost
            return validate(data)

    def route(self) -> dict:
        """Read the request: what kind of job, how hard, and any examples or commits it names."""
        prompt = ROUTE_PROMPT.format(request=self.request)
        if self.cfg.get("router") == "haiku" or server_models(self.chores["url"]) is None:
            data = self.claude("route", prompt, ROUTE_SCHEMA, validate_route,
                               model=self.cfg["claude"]["classify_model"], tools=False)
        else:
            data = ask_json(self.chores, self.run, "route", SYSTEM, prompt, ROUTE_SCHEMA, validate_route,
                            think=False, max_tokens=600, timeout=self.timeout)
        self.run.save("route.json", data)
        return data

    def facts(self) -> str:
        """Facts gathered mechanically before the planner starts, so it reads answers instead of
        thinking its way to them: the code around each failing check, the definitions and uses of
        names in the request, and the tests closest to those files."""
        if hasattr(self, "_facts"):
            return self._facts
        files, outline = self.index.files, self.index.outline()
        limit, parts, shown, sources = self.cfg.get("facts_chars", 24000), [], set(), []

        def show(path: str, decl: dict, why: str) -> None:
            key = (path, decl["line"])
            if key in shown:
                return
            if decl["end"] - decl["line"] > 120:
                members = decl.get("members") or []
                if members:  # too long to show: say where its parts are, so the planner can ask for one
                    shown.add(key)
                    parts.append(f"{why} (lines {decl['line']}-{decl['end']}, too long to show whole); its parts:\n"
                                 + "\n".join(f"  {m['line']}-{m['end']}  {m['signature'][:100]}" for m in members[:60]))
                return
            shown.add(key)
            parts.append(f"{why}:\n" + read_spec(self.repo, files, f"{path}:{decl['line']}-{decl['end']}",
                                                     self.cfg["max_file_chars"]))
            sources.append(path)

        for f, line, msg in parse_diagnostics(self.checks_seen, files)[:10]:
            # The enclosing function, or, for code that isn't in one (a test's it(...) callback), the
            # lines leading up to the failure.
            decl = enclosing(outline.get(f, []), line)
            decl = decl or {"line": max(1, line - 15), "end": line + 3}
            show(f, decl, f"Where a check fails ({f}:{line}: {msg[:100]})")
        private: dict[str, list] = {}
        for name in request_names(self.request)[:12]:
            for f, decls in outline.items():
                found = False
                for d in decls:
                    hits = [d] if d["name"] == name else [m for m in d.get("members", []) if m["name"] == name]
                    found = found or bool(hits)
                    for hit in hits:
                        show(f, hit, f"`{name}`, named in the request, is defined in {f}")
                        used = self.index.uses(name, f) if d["name"] == name and d["exported"] else None
                        if used and used["uses"]:
                            where = sorted({u["file"] for u in used["uses"]} - {f})
                            parts.append(f"`{name}` is used in {len(where)} other file(s): {', '.join(where[:10])}"
                                         + (" …" if len(where) > 10 else ""))
                if not found and f.endswith(".py"):  # the outline leaves out private methods
                    if f not in private:
                        private[f] = py_decls(self.repo / f)
                    for hit in [d for d in private[f] if d["name"].split(".")[-1] == name][:2]:
                        show(f, hit, f"`{name}`, named in the request, is defined in {f}")
        leads = request_leads(self.repo, self.request, files)
        if leads:
            parts.append("Where the request's words appear (found mechanically):\n" + leads)
        hint = self.move_hint()
        if hint:
            r, t = hint["report"], hint["task"]
            parts.append(MOVE_HINT.format(
                task=json.dumps({k: t[k] for k in ("file", "action", "where", "into", "name", "select")}),
                moved=r["moved"], lines=r["moved_lines"], where=t["where"], name=t["name"], into=t["into"],
                params=", ".join(f"`{p['name']}`" for p in r["params"]) or "no parameters", count=len(r["returns"]),
                returns=", ".join(r["returns"][:12]) + (" …" if len(r["returns"]) > 12 else ""),
                helpers=", ".join(r["helpers_moved"]) or "none",
                notes=("\nKept in place: " + "; ".join(r["notes"][:6])) if r["notes"] else ""))
        for h in self.share_hints():
            parts.append(variants_text(h["compare"]))
            if h["task"]:
                parts.append("A mechanical share fits these two files: this task moves their identical top-level "
                             "declarations into one file, exactly, and makes both import it:\n"
                             + json.dumps({k: h["task"][k] for k in ("file", "action", "from", "select")})
                             + "\nPlan it (copy it); add tasks only for what the request asks beyond it.")
        for name, path in self.related.items():  # read-only neighbours (the API this app calls…)
            theirs = request_leads(path, self.request, self.related_files(name), prefix=f"{name}:")
            if theirs:
                parts.append(f"Where the request's words appear in `{name}` (a related repo at {path}; read-only, "
                             f"read its parts as \"{name}:path:start-end\"):\n" + theirs)
        sources = list(dict.fromkeys(s for s in sources if not re.search(r"\.(test|spec)\.", s)))[:6]
        importers = self.index.importers(sources)
        for src in sources:
            tests = related_tests(src, importers.get(src, []))[:3]
            if tests:
                parts.append(f"Tests closest to {src}: {', '.join(tests)}")
        text = "\n\n".join(parts)
        self._facts = text[:limit] + ("\n… (cut off)" if len(text) > limit else "")
        if self._facts:
            say(f"  {ok()} gathered {len(shown)} code excerpt(s) and related tests mechanically, before planning")
        return self._facts

    def ci_failures(self) -> str:
        """The newest failed CI run on this branch (GitHub Actions, through gh with the folder's account):
        the end of each failed step's log, as check output for the planner. "" when there's none."""
        g = GitAssistant(self)
        try:
            branch = g.current()
            runs = json.loads(g.gh("run", "list", "--branch", branch, "--limit", "8", "--json",
                                   "databaseId,conclusion,status,name,headSha,displayTitle,url", check=False) or "[]")
        except (RuntimeError, OSError, ValueError):
            return ""
        failed = [r for r in runs if r.get("conclusion") in ("failure", "timed_out", "startup_failure")]
        if not failed:
            return ""
        r = failed[0]
        log = trim_ci_log(g.gh("run", "view", str(r["databaseId"]), "--log-failed", check=False))
        head = git(self.repo, "rev-parse", "HEAD").strip()
        stale = "" if head.startswith(r["headSha"][:12]) else (f" (that run was for {r['headSha'][:8]}; this branch is "
                                                               f"now at {head[:8]}, so some of it may be fixed already)")
        say(f"  {ok()} read the failed CI run on {branch}: {r['name']}{stale}")
        return f"CI run that failed: {r['name']}: {r['displayTitle']} ({r['url']}){stale}\n{log}"

    def move_hint(self) -> dict | None:
        """When the request asks to move code out of one existing file into a new one: the move task that
        does it mechanically, checked with a dry run. None when the request isn't that or the dry run fails."""
        if hasattr(self, "_move_hint"):
            return self._move_hint
        self._move_hint = None
        if not MOVE_REQUEST.search(self.request):
            return None
        sources = [f for f in paths_in_text(self.request, self.index.files) if f.endswith(MOVABLE)]
        targets = [clean_path(m) for m in re.findall(r"[\w@.+\-/]+\.(?:tsx?|jsx?)\b", self.request)]
        targets = [t for t in targets if t not in sources and not (self.repo / t).exists()]
        if len(sources) != 1 or not targets:
            return None
        src, into = sources[0], targets[0]
        if "/" not in into:
            into = str(PurePosixPath(src).parent / into)
        named = re.search(r"\buse[A-Z]\w*", self.request)
        stem = PurePosixPath(into).stem
        name = named.group(0) if named else re.sub(r"[-_.](\w)", lambda m: m.group(1).upper(), stem)
        decls = self.index.outline().get(src, [])
        if not decls or not re.fullmatch(r"[A-Za-z_$][\w$]*", name):
            return None
        main = max(decls, key=lambda d: d["end"] - d["line"])
        task = {"id": "T1", "file": src, "action": "move", "where": main["name"], "into": into, "name": name,
                "select": ["logic"], "instructions": f"Move the logic of {main['name']} into {name} (done mechanically).",
                "done_when": "The component calls the hook; checks pass.", "edits": []}
        d = ts_moves(self.repo, {"op": "extract", "file": src, "function": main["name"], "select": "logic",
                                 "into": into, "name": name})
        if not d.get("ok"):
            detail(f"a mechanical move doesn't fit ({d.get('error', '')[:160]})")
            return None
        say(f"  {ok()} a mechanical move fits: {d['report']['moved_lines']} lines of {main['name']} → {name} (dry run)")
        self._move_hint = {"task": validate_move(task, src, self.repo, "T1"), "report": d["report"]}
        return self._move_hint

    def share_hints(self) -> list[dict]:
        """For a merge/share request naming files that exist in two variants: each pair's comparison and,
        when they have identical top-level declarations, the share task that moves those into one file."""
        if hasattr(self, "_share_hints"):
            return self._share_hints
        self._share_hints = []
        if not MERGE_WORDS.search(self.request):
            return []
        files = list(self.index.files)
        named_new = [clean_path(m) for m in re.findall(r"[\w@.+\-/]+\.(?:tsx?|jsx?)\b", self.request)
                     if not (self.repo / clean_path(m)).exists() and "/" in m]
        for a, b in variant_pairs(self.request, files)[:4]:
            cmp = ts_moves(self.repo, {"op": "compare", "a": a, "b": b}, timeout=120)
            if not cmp.get("ok"):
                continue
            same = [p["name"] for p in cmp["pairs"] if p["status"] == "identical" and p.get("module_level")]
            inner = [p for p in cmp["pairs"] if p["status"] == "identical" and p.get("depth") == 1]
            parent = str(PurePosixPath(a).parent.parent)
            into = next((n for n in named_new if n.startswith(parent + "/")), None) \
                or f"{parent}/{PurePosixPath(a).stem}-shared{PurePosixPath(a).suffix}"
            parents = {p["parent"] for p in inner}
            if inner and len(parents) == 1:
                hook_file, hook = shared_hook_names(f"{parent}/x.ts", a)
                dry = ts_moves(self.repo, {"op": "share_hook", "files": [a, b], "function": parents.pop(),
                                           "names": [p["name"] for p in inner], "into": hook_file, "name": hook}, timeout=120)
                if dry.get("ok"):
                    same += [p["name"] for p in inner]
                else:
                    detail(f"the identical statements inside the components can't be shared yet ({dry.get('error', '')[:160]})")
            task = None
            if same:
                task = {"id": "T1", "file": into, "action": "share", "from": [a, b], "select": same,
                        "instructions": f"Move what both variants share ({', '.join(same)}) into {into}.",
                        "done_when": "Both variants import the shared code; checks pass.", "edits": [], "where": ""}
                say(f"  {ok()} {a.split('/')[-3]}: {len(same)} identical declaration(s) can be shared mechanically")
            self._share_hints.append({"compare": cmp, "task": task})
        return self._share_hints

    def fill_moves(self, data: dict) -> dict:
        """Move and share tasks often come back with their details in the instructions instead of the
        fields; fill what's missing from the dry-run suggestions when the task is about the same files."""
        if not isinstance(data, dict):
            return data
        hint = self.move_hint()
        for t in data.get("tasks") or [] if hint else []:
            if isinstance(t, dict) and t.get("action") == "move" and clean_path(str(t.get("file", ""))) == hint["task"]["file"]:
                for k in ("into", "name", "where"):
                    t[k] = t.get(k) or hint["task"][k]
                t["select"] = t.get("select") or ["logic"]
        for t in data.get("tasks") or []:
            if not (isinstance(t, dict) and t.get("action") == "share"):
                continue
            for h in self.share_hints():
                ht = h["task"]
                if ht and (clean_path(str(t.get("file", ""))) == ht["file"] or set(t.get("from") or []) == set(ht["from"])):
                    t["file"] = t.get("file") or ht["file"]
                    t["from"] = t.get("from") or ht["from"]
                    t["select"] = t.get("select") or ht["select"]
        return data

    def prefer_move(self, plan: dict) -> dict:
        """A plan that would retype the moved code (a new file with no exact code) gets the mechanical
        move instead; the planner's other tasks stay. Next to a move or share, edit tasks on its files go,
        even with exact edits: it already rewires them (one such task spent 9 minutes changing nothing,
        another replaced each shared declaration by hand), and what's left is for the review."""
        moves = [t for t in plan["tasks"] if t["action"] in ("move", "share")]
        if moves:
            mine = {f for t in moves for f in ([t["file"], t["into"]] if t["action"] == "move" else [t["file"], *t["from"]])}
            extra = [t for t in plan["tasks"] if t["action"] in ("edit", "create") and t["file"] in mine]
            if extra:
                say(f"  {paint('dropped', 'dim')} {', '.join(t['id'] for t in extra)}: the move already does "
                    f"that; anything left over is for the review, after the checks")
            keep = [t for t in plan["tasks"] if t not in extra]
            return {**plan, "tasks": [{**t, "id": f"T{n}"} for n, t in enumerate(keep, 1)]}
        if not any(t["action"] == "share" for t in plan["tasks"]):
            for h in self.share_hints():
                ht = h["task"]
                if ht and any(t["file"] == ht["file"] and not t.get("edits") for t in plan["tasks"]):
                    say(f"  {warn()} the plan would retype the shared code; sharing it mechanically instead")
                    rest = [t for t in plan["tasks"] if t["file"] != ht["file"]]
                    fixed = validate_move(dict(ht), ht["file"], self.repo, "T1")
                    return self.prefer_move({**plan, "tasks": [fixed] + rest})  # and drop edits that redo it
        hint = self.move_hint()
        if not hint:
            return plan
        into, src = hint["task"]["into"], hint["task"]["file"]
        if not any(t["file"] == into and not t.get("edits") for t in plan["tasks"]):
            return plan
        say(f"  {warn()} the plan would retype the moved code; doing it as a mechanical move instead")
        rest = [t for t in plan["tasks"] if t["file"] not in (into, src) or t.get("edits")]
        return self.prefer_move({**plan, "tasks": [hint["task"]] + rest})

    def context(self, with_map: bool = True) -> str:
        """Check output, mechanically gathered facts, `--like` examples and the project's notes
        (map and recent work from .buckets/), for the planner."""
        parts = []
        mem = self.memory
        if mem:
            named = paths_in_text(self.request, self.index.files)
            recent = mem.recent(named + request_names(self.request), self.cfg.get("journal_tokens", 600))
            if recent:
                parts.append(f"Recent work in this repo (from {MEMORY_DIR}/journal.md; \"that\" or \"it\" in the "
                             "request may refer to it):\n" + recent)
            if with_map:
                parts.append(f"Project map (from {MEMORY_DIR}/map.md):\n"
                             + mem.render(self.cfg.get("map_tokens", 1500), named))
        if self.checks_seen:
            parts.append("Check output before any change:\n" + self.checks_seen)
        facts = self.facts()
        if facts:
            parts.append("Facts gathered mechanically (exact, from disk and the code index):\n" + facts)
        if self.like:
            parts.append("Example to follow:\n" + example_material(self.repo, self.like, self.cfg["example_chars"]))
        return "\n\n".join(parts)

    def material(self, mode: str) -> str:
        """Pick files, read them, and survey in batches when they don't fit one call."""
        ctx = self.context()
        self.involved = getattr(self, "involved", [])
        if ctx:
            self.run.save("context.md", ctx)
        files = list_files(self.repo, self.rc.get("exclude", []))
        if not files:
            return "(the repository has no readable text files)"
        named_up_front = [f for f in paths_in_text(self.request, files) if f in files]
        if len(files) > self.cfg["map_max_entries"] and not (named_up_front and self.difficulty == "light"):
            dirs = dir_summary(files)
            listing = "\n".join(f"{d} ({c} files, {n} lines)" for d, (c, n) in sorted(dirs.items()))
            chosen = ask_json(self.chores, self.run, "pick-dirs", SYSTEM,
                              DIRS_PROMPT.format(goal=self.goal(mode), request=self.request, dirs=listing),
                              DIRS_SCHEMA, lambda d: d, think=False, max_tokens=600, timeout=self.timeout)
            keep = [clean_path(d).rstrip("/") for d in chosen["dirs"]]
            files = {f: n for f, n in files.items()
                     if any(f == d or f.startswith(d + "/") or d == "." for d in keep)}
            say(f"  narrowed to {len(files)} files in: {', '.join(keep)}")
        gd = guidance(self.repo, self.rc, self.request)
        if self.mode != "claude" and self.reading() == "research":
            head = f"{ctx}\n\n" if ctx else ""
            return head + self.research(mode, files, gd)

        def check_files(d):
            picked = [clean_path(f) for f in d["files"]]
            good = [f for f in picked if f in files][: self.cfg["max_files"]]
            if not good:
                raise ValueError("none of the picked files are in the list")
            return good

        named = [f for f in paths_in_text(self.request, files) if f in files]
        if named and self.difficulty == "light":
            picked = named[: self.cfg["max_files"]]  # the request says where; no need to ask
        else:
            mem, bare = self.memory, self.context(with_map=False)
            listing = (mem.render(self.cfg.get("pick_map_tokens", 4000), named) if mem else self.code_map(files))
            picked = ask_json(self.chores, self.run, "pick-files", SYSTEM,
                              FILES_PROMPT.format(goal=self.goal(mode), max_files=self.cfg["max_files"],
                                                  request=self.request, guidance=gd,
                                                  context=f"\n{bare[:12000]}\n" if bare else "",
                                                  map=listing),
                              FILES_SCHEMA, check_files, think=False, max_tokens=1500, timeout=self.timeout)
        # Files named in check output are read whether or not the model picked them.
        named = [f for f in paths_in_text(self.checks_seen, files) if f not in picked]
        picked = (picked + named)[: max(self.cfg["max_files"], len(picked))]
        self.involved = list(dict.fromkeys(self.involved + picked))
        long = [f for f in picked if self.too_long(f)]
        whole = [f for f in picked if f not in long]
        if whole:
            say(f"  reading {len(whole)} files: {', '.join(whole[:8])}{' …' if len(whole) > 8 else ''}")
        blocks = [numbered(self.repo, f, self.cfg["max_file_chars"]) for f in whole]
        parts = self.focus(mode, long, ctx) if long else ""
        total = sum(est_tokens(b) for b in blocks)
        head = f"{ctx}\n\n" if ctx else ""
        if total + est_tokens(parts) <= self.cfg["file_budget_tokens"]:
            return head + "Files:\n\n" + "\n\n".join(blocks + ([parts] if parts else []))
        groups = batches(blocks, self.cfg["batch_tokens"])
        notes = self.survey(mode, groups, total) if groups else []
        return (head + "Survey notes (one section per batch of files):\n\n" + "\n\n---\n\n".join(notes)
                + (f"\n\n{parts}" if parts else ""))

    def think_budget(self) -> dict:
        """Light jobs get a short think (the worker thinks at ~37 tokens/s); others keep the model's own."""
        budget = self.cfg.get("light_think_budget", 0)
        return {"think_budget": budget} if budget and self.difficulty == "light" else {}

    def part(self, where: str, longest: int = 150) -> str:
        """A part of a file, exactly. A class too long to show (asked for by name) comes as its first
        lines, the outline of its members and its last member in full: where new members usually go."""
        m = SPEC.match(where)
        path, lo, hi = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        if hi - lo <= longest:
            return read_spec(self.repo, self.index.files, where, self.cfg["max_file_chars"])
        decls = decls_of(self.repo, path, self.index.outline())
        cls = next((d for d in decls if d["line"] == lo and d["end"] == hi), None)
        members = [d for d in decls if cls and d["name"].startswith(cls["name"] + ".")
                   and "." not in d["name"][len(cls["name"]) + 1:]]
        if not members:
            # One long function (a 1,500-line React screen): the stretches around the lines that
            # match the request's words, else its first lines.
            spots = self.hot_spots(path, lo, hi)
            if spots:
                return "\n".join(read_spec(self.repo, self.index.files, f"{path}:{a}-{b}", self.cfg["max_file_chars"])
                                 for a, b in spots) + f"\n  … (`{path}` {lo}-{hi} is one long declaration; these are " \
                                                       f"the parts that match the request)"
            return read_spec(self.repo, self.index.files, f"{path}:{lo}-{lo + longest}", self.cfg["max_file_chars"])
        last = members[-1]
        setup = [m for m in members[:-1] if m["name"].split(".")[-1] in ("setUp", "setup", "__init__", "beforeEach")]
        return (read_spec(self.repo, self.index.files, f"{path}:{lo}-{min(lo + 6, members[0]['line'] - 1)}",
                          self.cfg["max_file_chars"])
                + f"\n  … `{cls['name']}` is {hi - lo + 1} lines; its members (name one to see it):\n"
                + outline_text(members) + "\n"
                + "".join(read_spec(self.repo, self.index.files, f"{path}:{m['line']}-{m['end']}",
                                    self.cfg["max_file_chars"]) + "\n" for m in setup[:1])
                + read_spec(self.repo, self.index.files, f"{path}:{last['line']}-{last['end']}", self.cfg["max_file_chars"]))

    def hot_spots(self, path: str, lo: int, hi: int, around: int = 12, budget: int = 220,
                  text: str | None = None) -> list[tuple[int, int]]:
        """Line ranges in `path` between lo and hi around the lines that mention the request's (or
        `text`'s) phrases (in any code spelling), quoted strings or code names, merged, within `budget`."""
        text = self.request if text is None else text
        terms = [x for words in {tuple(r[i:j]) for r in word_runs(text) for i in range(len(r))
                                 for j in range(i + 2, len(r) + 1)}
                 for x in spellings(list(words)) + [" ".join(words)]]
        terms += [q for pair in re.findall(r'"([^"]{3,})"|`([^`]{3,})`', text) for q in pair if q]
        terms += [n for n in request_names(text) if len(n) >= 4]
        if not terms:
            return []
        lines = (self.repo / path).read_text(errors="replace").splitlines()
        span = range(lo, min(hi, len(lines)) + 1)
        found = {t: [i for i in span if re.search(re.escape(t).replace(r"\ ", r"\s+"), lines[i - 1], re.I)]
                 for t in set(terms)}
        # Rare terms first ("Original ETA", once) and none of the file's own subject (a term on
        # dozens of its lines, like workOrderDetails in the work order details screen).
        hits = sorted({i for t, where in sorted(found.items(), key=lambda kv: len(kv[1]))
                       if 0 < len(where) <= 8 for i in where})
        spans: list[list[int]] = []
        for h in hits:
            a, b = max(lo, h - around), min(hi, h + around)
            if spans and a <= spans[-1][1] + 1:
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        out, used = [], 0
        for a, b in spans:
            if used + (b - a + 1) > budget:
                break
            out.append((a, b))
            used += b - a + 1
        return out

    def facts_cover(self, long: list[str], ctx: str) -> bool:
        """The mechanically gathered facts already show every part of the long files that the request
        names (and something of each), so asking the planner which parts to read would add nothing."""
        shown: dict[str, list[tuple[int, int]]] = {}
        for head in re.findall(r"^=== (.+?) ===$", ctx, re.M):
            m = SPEC.match(head)
            if m and m.group(2):
                shown.setdefault(m.group(1), []).append((int(m.group(2)), int(m.group(3) or m.group(2))))
        if not all(shown.get(f) for f in long):
            return False
        outline = self.index.outline()
        for name in request_names(self.request):
            for f in long:
                hit = locate(self.repo, {f: self.index.files[f]}, outline, name, f)
                if not hit:
                    continue
                m = SPEC.match(hit)
                lo, hi = int(m.group(2)), int(m.group(3) or m.group(2))
                # (decorators: the locator starts a few lines before the facts do)
                if hi - lo <= 120 and not any(a <= lo + 5 and hi <= b for a, b in shown[f]):
                    return False
        return True

    def too_long(self, path: str) -> bool:
        """Files this long are read through their outline, never whole (when there is an outline)."""
        limit = self.cfg.get("targeted_min_lines", 0)
        return bool(limit) and self.index.files.get(path, 0) >= limit and bool(
            decls_of(self.repo, path, self.index.outline()))

    def focus(self, mode: str, long: list[str], ctx: str) -> str:
        """Long files: the planner sees what each declares on which lines, names the parts it needs
        (the code to change, what it calls, an example to copy) and gets exactly those."""
        outline, files, n = self.index.outline(), self.index.files, self.cfg["research"]["reads"] + 2
        sizes = ", ".join(f"{f} ({files[f]} lines)" for f in long)
        if self.facts_cover(long, ctx):
            say(f"  {ok()} {', '.join(long)}: the facts already show the code the request names; reading nothing more")
            return f"(The long files, {sizes}, are shown only through the facts above.)"
        covered = set()
        if self.move_hint():
            covered.add(self.move_hint()["task"]["file"])
        for h in self.share_hints():
            if h["task"]:
                covered |= set(h["task"]["from"])
        if covered and set(long) <= covered:
            # The mechanical move or share in the facts does the work; the planner only needs the shape.
            say(f"  {ok()} {', '.join(long)}: a mechanical move covers these; the planner gets their outlines, not their code")
            return (f"The long files ({sizes}) are covered by the mechanical task in the facts. Their outlines "
                    "(read a part with the read tool if the plan needs more):\n\n"
                    + "\n\n".join(f"=== {f} ===\n" + outline_text(decls_of(self.repo, f, outline)) for f in long))
        maps = "\n\n".join(f"=== {f} ({files[f]} lines; outline: lines, name, signature) ===\n"
                            + outline_text(decls_of(self.repo, f, outline)) for f in long)
        say(f"  {', '.join(long)}: too long to read whole, so the planner picks the parts it needs from the outline")

        def check(d):
            return [str(x).strip() for x in d.get("read") or [] if str(x).strip()][:n]

        shown = re.findall(r"^=== (.+?) ===$", ctx, re.M)
        wanted = ask_json(self.chores, self.run, "focus", SYSTEM,  # reading with a tiny answer: the fast reader
                          FOCUS_PROMPT.format(goal=self.goal(mode), request=self.request, reads=n,
                                              shown=", ".join(shown) or "nothing", outlines=maps),
                          FOCUS_SCHEMA, check, think=False, max_tokens=1500, timeout=self.timeout)
        found, missing, theirs = [], [], []
        for spec in wanted:
            name = spec.split(":", 1)[0]
            if name in self.related and ":" in spec:  # a related repo's part: read it there
                text = read_spec(self.related[name], self.related_files(name), spec.split(":", 1)[1],
                                 self.cfg["max_file_chars"])
                (theirs if not text.startswith("!!") else missing).append((spec, f"(from the related repo `{name}`)\n{text}"))
                continue
            where = locate(self.repo, files, outline, spec, long[0] if len(long) == 1 else "")
            (found if where else missing).append((spec, where))
        texts = [self.part(where) for _, where in found] + [t for _, t in theirs]
        found += theirs
        if found:
            say(f"  {ok()} read exactly: {', '.join(spec for spec, _ in found)}")
        if missing:
            say(f"  {warn()} couldn't find: {', '.join(spec for spec, _ in missing)}")
        return (f"Parts of the long files ({sizes}; only these parts are shown), exactly as they are:\n\n"
                + ("\n\n".join(texts) or "(none)"))

    def reading(self) -> str:
        """How the local planner learns the code: read picked files directly, or ask the Windows
        helper fetch questions in rounds ("research"). Auto: research for heavy jobs."""
        choice = self.cfg.get("reading", "auto")
        if choice == "auto":
            return "research" if self.difficulty in ("heavy", "very_hard") else "direct"
        return choice

    def errand(self, ask: str, where: list[str], label: str) -> str:
        """One fetch question for the Windows helper, in a fresh read-only opencode session."""
        r, c = self.cfg["research"], self.cfg["coder"]
        prompt = ERRAND_PROMPT.format(ask=ask, where=("Look in: " + ", ".join(f"`{w}`" for w in where) + "\n")
                                      if where else "")
        model = getattr(self, "errand_model", c["opencode_model"])
        cmd = [self.cfg["opencode_bin"], "run", "--standalone", "--agent", r["opencode_agent"], "-m", model,
               "--auto", "--title", f"buckets {label}", prompt]
        start = time.time()
        with Ticker(f"  {cap(self.worker)} is looking up: {ask[:60]}") as tick:
            def show(line):
                action = coder_action(line)
                if action:
                    tick.text = f"  {cap(self.worker)} is looking up: {ask[:40]} · {action}"
            _, out, timed_out = run_proc(cmd, self.repo, r["errand_timeout_s"], on_line=show)
        self.run.save(f"{label}.log", f"{prompt}\n\n===== output =====\n{strip_ansi(out)}")
        answer = errand_answer(out) or ("(no answer: timed out)" if timed_out else "(no answer)")
        mark = warn() if answer.startswith("(no answer") else ok()
        say(f"  {mark} {cap(self.worker)} answered: {ask[:70]} ({elapsed(start)})")
        detail(f"{label}: {len(answer)} chars")
        return answer

    def research(self, mode: str, files: dict[str, int], gd: str) -> str:
        """Ping-pong: the planner asks fetch questions, the Windows helper answers them from the
        code, the script attaches the real lines for every cited location; repeat until enough."""
        r = self.cfg["research"]
        findings: list[str] = []
        hypotheses: list[dict] = []
        pushed_back = False
        file_map = self.code_map(files)
        say(f"  The planner asks questions; exact lookups and {self.worker} answer them ({r['rounds']} rounds at most)")
        room = self.planner.get("num_ctx", 32768) - 2000 - est_tokens(RESEARCH_PROMPT + gd + file_map + self.request)
        budget = min(r["budget_tokens"], max(4000, room - 2000))
        for rnd in range(1, r["rounds"] + 1):
            if findings and est_tokens("\n\n".join(findings)) > budget:
                # The handoff: the next round starts from compact notes instead of everything so far.
                say("  Condensing what's been found so far, to stay within the planner's memory")
                findings = [chat(self.chores, self.run, f"condense-{rnd}", SYSTEM,
                                 CONDENSE_PROMPT.format(goal=self.goal(mode), request=self.request,
                                                        findings="\n\n".join(findings)[: budget * 6]),
                                 think=False, max_tokens=4000, timeout=self.timeout)]
            known = ("What you've learned so far (text marked 'exact' or 'from disk' is the real code):\n\n"
                     + "\n\n".join(findings)) if findings else "Nothing has been looked at yet."
            if hypotheses:
                known += "\n\nYour hypotheses so far:\n" + "\n".join(
                    f"- [{h['status']}] {h['cause']} ({h['evidence']})" for h in hypotheses)
            data = ask_json(self.planner, self.run, f"research-{rnd}", SYSTEM,
                            RESEARCH_PROMPT.format(goal=self.goal(mode), request=self.request, guidance=gd,
                                                   map=file_map, findings=known, questions=r["questions"],
                                                   reads=r["reads"]),
                            RESEARCH_SCHEMA, lambda d: d, think=True, think_budget=r.get("think_budget", 1500),
                            max_tokens=4000, timeout=self.timeout)
            hypotheses = [h for h in data.get("hypotheses") or [] if str(h.get("cause", "")).strip()] or hypotheses
            still_open = [h["cause"] for h in hypotheses if h.get("status") == "open"]
            reads = [str(x).strip() for x in data.get("read") or [] if str(x).strip()][: r["reads"]]
            uses = [str(x).strip().strip("`") for x in data.get("uses") or [] if str(x).strip()][: r["reads"]]
            greps = [g for g in data.get("grep") or [] if str(g.get("pattern", "")).strip()][: r["reads"]]
            docs = [str(x).strip() for x in data.get("docs") or [] if str(x).strip()][: r["reads"]]
            history = [str(x).strip() for x in data.get("history") or [] if str(x).strip()][: r["reads"]]
            questions = [q for q in data.get("questions") or [] if str(q.get("ask", "")).strip()]
            if not self.index.ts and uses:  # no index: ask Windows instead
                questions += [{"ask": f"Where is `{u}` defined and used? List each place.", "where": []} for u in uses]
                uses = []
            if data.get("enough") and still_open and rnd < r["rounds"] and not pushed_back:
                # "Enough" with causes still unchecked is how a plan ends up blaming the wrong file.
                pushed_back = True
                findings.append("SYSTEM: you said enough, but these hypotheses are still open: "
                                + "; ".join(still_open) + ". Look up what would confirm or rule them out.")
                say(f"  {warn()} The planner said it had enough, but {len(still_open)} possible cause(s) "
                    "are unchecked; one more round")
                continue
            if data.get("enough") or not (reads or uses or greps or docs or history or questions):
                say(f"  {ok()} The planner has what it needs (after {rnd - 1} round{'s' if rnd != 2 else ''})")
                break
            errands = [(q["ask"].strip(), part) for q in questions[: r["questions"]]
                       for part in split_where([clean_path(w) for w in q.get("where") or []])]
            errands = errands[: r["errands_per_round"]]
            say(f"  Round {rnd}: the planner wants to read {len(reads)} file(s) or range(s)"
                f" and asks {self.worker} {len(questions)} question(s)")
            for spec in reads:
                findings.append(f"READ {spec} (exact):\n"
                                + read_spec(self.repo, files, spec, self.cfg["max_file_chars"]))
            if reads:
                say(f"  {ok()} shown exactly: {', '.join(reads)}")
            for spec in uses:
                findings.append(self.uses_text(spec, files))
            if uses:
                say(f"  {ok()} uses found by the code index: {', '.join(uses)}")
            for g in greps:
                findings.append(grep_text(self.repo, g["pattern"], g.get("paths") or []))
            if greps:
                say(f"  {ok()} searched exactly: {', '.join('/' + g['pattern'] + '/' for g in greps)}")
            for spec in docs:
                findings.append(package_docs(self.repo, spec))
            for spec in history:
                findings.append(history_text(self.repo, files, spec))
            if docs or history:
                say(f"  {ok()} looked up {', '.join([*('docs ' + d for d in docs), *('history ' + h for h in history)])}")
            for n, (ask, where) in enumerate(errands, 1):
                answer = self.errand(ask, where, f"errand-{rnd}.{n}")
                facts = ground_truth(answer, self.repo, files)
                scope = f" (looking in {', '.join(where)})" if where else ""
                findings.append(f"Q: {ask}{scope}\nA: {answer}" + (f"\n{facts}" if facts else ""))
        if findings and est_tokens("\n\n".join(findings)) > budget:
            say("  Condensing the findings for planning")
            findings = [chat(self.chores, self.run, "condense-final", SYSTEM,
                             CONDENSE_PROMPT.format(goal=self.goal(mode), request=self.request,
                                                    findings="\n\n".join(findings)[: budget * 6]),
                             think=False, max_tokens=4000, timeout=self.timeout)]
        confirmed = [h for h in hypotheses if h.get("status") == "confirmed"]
        still = [h for h in hypotheses if h.get("status") == "open"]
        self.research_unsure = bool(hypotheses) and (not confirmed or len(still) >= 2)
        if hypotheses:
            findings.append("Hypotheses at the end of research:\n" + "\n".join(
                f"- [{h['status']}] {h['cause']} ({h['evidence']})" for h in hypotheses))
        return ("Research notes: what the planner read (exact) and the helper's leads (with the real "
                "lines from disk).\n\n" + "\n\n".join(findings))

    def uses_text(self, spec: str, files: dict[str, int]) -> str:
        """Where a symbol is used, from the code index, with each line's text from disk."""
        name, _, origin = spec.partition("@")
        found = self.index.uses(name.strip(), clean_path(origin) or None) if name.strip() else None
        if not found:
            return f"USES {spec}: (the code index couldn't answer)"
        out = [f"USES of `{name}` (exact, from the code index; defined in {', '.join(found['definedIn']) or '?'}):"]
        for u in found["uses"][:40]:
            lines = (self.repo / u["file"]).read_text(errors="replace").splitlines()
            for ln in ([u["importLine"]] if u["importLine"] else []) + u["lines"][:6]:
                if 0 < ln <= len(lines):
                    out.append(f"  {u['file']}:{ln}: {lines[ln - 1].strip()[:140]}")
        if len(found["uses"]) > 40:
            out.append(f"  … {len(found['uses']) - 40} more files")
        return "\n".join(out)

    @property
    def worker(self) -> str:
        """The worker machine's name in messages (where the reader, thinker and coder run)."""
        return machine_label(self.cfg["reader"])

    @property
    def home(self) -> str:
        """The advisor's machine's name in messages."""
        return machine_label(self.cfg["planner"])

    @property
    def windows_up(self) -> bool:
        """Whether the Windows model answers (checked once per run)."""
        if not hasattr(self, "_windows_up"):
            reader = self.cfg.get("reader")
            models = server_models(reader["url"]) if reader else None
            self._windows_up = bool(models is not None and reader["model"] in models)
        return self._windows_up

    @property
    def thinker_up(self) -> bool:
        """Whether the Windows reasoning model is there (checked once per run)."""
        if not hasattr(self, "_thinker_up"):
            t = self.cfg.get("thinker")
            models = server_models(t["url"]) if t else None
            self._thinker_up = bool(models is not None and t["model"] in models)
        return self._thinker_up

    @property
    def chores(self) -> dict:
        """Who does the no-thinking chores (routing, picking files, condensing notes): whichever
        Windows model is planning (so nothing swaps), else the Windows coder when it's up, since
        the Mac is the machine you're working on and it's slower."""
        if getattr(self, "on_windows", False):
            return {**self.planner, "think": False}
        if self.cfg.get("offload", {}).get("chores") and self.windows_up:
            return {**self.cfg["reader"], "think": False}
        return self.planner

    @property
    def mac_up(self) -> bool:
        """Whether the Mac's planner (the advisor) answers."""
        if not hasattr(self, "_mac_up"):
            models = server_models(self.cfg["planner"]["url"])
            self._mac_up = bool(models is not None and self.cfg["planner"]["model"] in models)
        return self._mac_up

    def planner_is_advisor(self) -> bool:
        return (self.planner.get("url"), self.planner.get("model")) == (self.cfg["planner"]["url"], self.cfg["planner"]["model"])

    def claude_allowed(self) -> bool:
        """The ladder's last step: Claude, unless the repo or the run is local-only."""
        if not hasattr(self, "_claude_allowed"):
            local_only = self.cfg.get("mode_override") == "local" or self.rc.get("mode") == "local"
            self._claude_allowed = (bool(self.cfg.get("offload", {}).get("claude_ladder")) and not local_only
                                    and claude_ready(self.cfg))
        return self._claude_allowed

    def escalate(self, reason: str):
        """The next step up the ladder (thinker → advisor → Claude), within the per-run limit.
        Returns ("advisor", cfg), ("claude", None) or None, and records why."""
        log = self.__dict__.setdefault("escalations", [])
        if len(log) >= self.cfg.get("offload", {}).get("max_escalations", 2):
            detail(f"escalation skipped (limit reached): {reason}")
            return None
        if self.mode != "claude" and not self.planner_is_advisor() and self.mac_up:
            target, who = ("advisor", dict(self.cfg["planner"])), f"the advisor ({self.home})"
        elif self.mode != "claude" and self.claude_allowed():
            target, who = ("claude", None), f"Claude {self.cfg['claude']['model']}"
        else:
            detail(f"escalation skipped (nowhere to go): {reason}")
            return None
        log.append(f"{reason} → {who}")
        say(f"  {warn('⇪')} {paint(f'Asking {who}', 'bold')}: {reason}")
        return target

    def ask_escalated(self, target, label: str, prompt: str, schema: dict, validate):
        if target[0] == "claude":
            return self.claude(label, CLAUDE_INTRO + "\n\n" + prompt, schema, validate)
        return ask_json(target[1], self.run, label, SYSTEM, prompt, schema, validate, max_tokens=12000,
                        timeout=self.timeout)

    def escalate_to_mac(self, reason: str) -> bool:
        """No progress: hand planning and review to the next step up for the rest of the run."""
        target = self.escalate(reason)
        if not target:
            return False
        if target[0] == "claude":
            self.mode = "claude"
        else:
            self.planner, self.on_windows = target[1], False
        self.escalated = reason
        return True

    def rewrite_tasks(self, tasks: list[dict], why: str) -> list[dict]:
        """Ask one step up the ladder to rewrite tasks the coder keeps failing, with the exact change."""
        target = self.escalate(why)
        if not target:
            return tasks
        files = self.index.files
        blocks = []
        for t in tasks:
            lines = [int(n) for n in re.findall(r"line (\d+)", t["instructions"])][:6]
            ranges = [f"{t['file']}:{max(1, n - 8)}-{n + 8}" for n in lines] or [t["file"]]
            code = "\n".join(read_spec(self.repo, files, r, 6000) for r in ranges[:3])
            blocks.append(f"Task {t['id']} on `{t['file']}`:\n{t['instructions']}\nDone when: {t['done_when']}\n\n{code}")
        prompt = REWRITE_PROMPT.format(request=self.request, why=why, tasks="\n\n---\n\n".join(blocks))

        def validate(d):
            new = {str(x["id"]): x for x in d["tasks"]}
            return [{**t, "instructions": str(new[t["id"]]["instructions"]), "done_when": str(new[t["id"]]["done_when"])}
                    if t["id"] in new else t for t in tasks]

        try:
            return self.ask_escalated(target, "rewrite", prompt, REWRITE_SCHEMA, validate)
        except ModelError as e:
            say(f"  {warn()} the rewrite didn't come back ({str(e)[:120]}); keeping the tasks as they were")
            return tasks

    def plan_on_windows(self, thinker: bool = False) -> None:
        """Plan and review on the Windows laptop: with the reasoning model (`thinker`), or for light
        jobs with the coder model. Look-ups then use the same model, so Ollama doesn't swap per question."""
        if thinker:
            self.planner = dict(self.cfg["thinker"])
            self.errand_model = self.cfg["thinker"]["opencode_model"]
        else:
            self.planner = {**self.cfg["reader"], "think": False}
        self.on_windows = True

    def readers(self, batches_needed: int) -> list[tuple[str, dict]]:
        """Who reads survey batches. Windows first (faster, and otherwise idle); the Mac helps only
        when there are enough batches to be worth slowing your work machine down."""
        on_windows = getattr(self, "on_windows", False)
        windows = ("windows", {**self.planner, "think": False} if on_windows else self.cfg["reader"]) \
            if (self.windows_up or on_windows) else None
        mac = ("mac", self.planner) if not getattr(self, "on_windows", False) else None
        if self.cfg.get("offload", {}).get("survey") == "windows-first" and windows:
            return [windows] + ([mac] if mac and batches_needed >= 3 else [])
        return [w for w in (mac, windows if batches_needed > 1 else None) if w] or [("mac", self.planner)]

    def survey(self, mode: str, groups: list[list[str]], total: int) -> list[str]:
        """Summarize batches of files. Batches go in a queue that each reader pulls from, so the
        faster machine takes more; anything a reader fails on is redone on the Mac."""
        prompts = [SURVEY_PROMPT.format(goal=self.goal(mode), request=self.request, files="\n\n".join(g))
                   for g in groups]
        workers = self.readers(len(groups))
        where = " and ".join({"mac": self.home, "windows": self.worker}[n] for n, _ in workers)
        say(f"  That's about {total // 1000}k tokens of code, so it's read in {len(groups)} parts on {where}")
        started = time.time()
        notes: list[str | None] = [None] * len(groups)
        lock, queue = threading.Lock(), list(range(len(groups)))

        def work(name: str, cfg: dict) -> None:
            while True:
                with lock:
                    if not queue:
                        return
                    i = queue.pop(0)
                try:
                    notes[i] = chat(cfg, self.run, f"survey-{i + 1}-{name}", SYSTEM, prompts[i], think=False,
                                    max_tokens=2500, timeout=self.timeout, quiet=len(workers) > 1)
                    say(f"  {ok()} part {i + 1}/{len(groups)} read on {self.worker if name == 'windows' else self.home}"
                        f" ({elapsed(started)} in)")
                except (ModelError, OSError) as e:
                    say(f"  {warn()} part {i + 1} failed on {self.worker if name == 'windows' else self.home} "
                        f"({str(e)[:120]}); {self.home} will redo it")
                    return

        threads = [threading.Thread(target=work, args=w, daemon=True) for w in workers]
        with Ticker(f"  Reading {len(groups)} parts") if len(workers) > 1 else contextlib.nullcontext():
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        for i, note in enumerate(notes):
            if note is None:
                notes[i] = chat(self.planner, self.run, f"survey-{i + 1}-mac", SYSTEM, prompts[i], think=False,
                                max_tokens=2500, timeout=self.timeout)
        return [n or "" for n in notes]

    # -- info mode

    def answer(self) -> str:
        if self.mode == "claude":
            self.heading(2, "Claude is reading the code and writing the answer")
            ctx = self.context()
            report = self.claude("answer", CLAUDE_ANSWER_PROMPT.format(
                request=self.request, context=f"\n{ctx}" if ctx else ""))
        else:
            self.heading(2, "Reading the code")
            material = self.material("info")
            self.heading(3, "Writing the answer")
            report = chat(self.planner, self.run, "answer", SYSTEM,
                          ANSWER_PROMPT.format(request=self.request, guidance=guidance(self.repo, self.rc, self.request),
                                               material=material),
                          max_tokens=16000, timeout=self.timeout)
        path = self.run.dir / "report.md"
        path.write_text(report)
        print(report)
        finish(self, "info", None, None, None, path)
        return report

    # -- code mode

    def plan(self, checks: dict, baseline: dict) -> dict:
        show = self.rc.get("show_output", [n for n in checks if "coverage" in n])
        self.checks_seen = check_context(baseline, show, self.cfg["check_output_chars"], self.repo, self.index.files)
        wants_coverage = "coverag" in self.request.lower() or any("coverag" in n for n in checks)
        cover = coverage_text(self.repo, self.index.files) if wants_coverage else ""
        if cover:
            self.checks_seen = (self.checks_seen + "\n\n" + cover).strip()
        ci = self.ci_failures() if CI_WORDS.search(self.request) else ""
        if ci:
            self.checks_seen = (self.checks_seen + "\n\n" + ci).strip()
        baseline_text = ", ".join(f"{n} {'passes' if r['ok'] else 'FAILS'}" for n, r in baseline.items()) or "n/a"
        if self.mode == "claude":
            self.heading(3, "Claude is reading the code and planning")
            ctx = self.context()
            plan = self.claude(
                "plan", CLAUDE_PLAN_PROMPT.format(
                    rules=TASK_RULES.format(max_tasks=self.cfg["max_tasks"]), max_tasks=self.cfg["max_tasks"],
                    request=self.request, checks=", ".join(checks) or "none", baseline=baseline_text,
                    context=f"\n{ctx}" if ctx else ""),
                PLAN_SCHEMA, lambda d: validate_plan(d, self.repo, self.cfg["max_tasks"]))
            plan = self.prefer_move(plan)
            self.run.save("plan.json", plan)
            return plan
        self.heading(3, "Reading the code and planning")
        material = self.material("code")
        prompt = PLAN_PROMPT.format(rules=TASK_RULES.format(max_tasks=self.cfg["max_tasks"]),
                                    max_tasks=self.cfg["max_tasks"], request=self.request,
                                    checks=", ".join(checks) or "none", baseline=baseline_text,
                                    guidance=guidance(self.repo, self.rc, self.request), material=material)
        validate = lambda d: validate_plan(self.fill_moves(d), self.repo, self.cfg["max_tasks"])
        target = (self.escalate("research ended without a confirmed cause")
                  if getattr(self, "research_unsure", False)
                  and self.cfg.get("offload", {}).get("escalate_unsure_research") else None)
        if target:
            plan = self.ask_escalated(target, "plan-escalated", prompt, PLAN_SCHEMA, validate)
        elif self.cfg.get("lookups", True):
            plan = self.plan_with_lookups(prompt, validate)
        else:
            plan = ask_json(self.planner, self.run, "plan", SYSTEM, prompt, PLAN_SCHEMA, validate,
                            max_tokens=16000, timeout=self.timeout, **self.think_budget())
        plan = self.prefer_move(plan)
        covered = {f for t in plan["tasks"] for f in [t["file"], t.get("into") or "", *(t.get("from") or [])]}
        missing = [f for f in paths_in_text(self.request, self.index.files) if plan["tasks"] and f not in covered]
        if missing:
            say(f"  {warn()} the plan has no task for {', '.join(missing)}, which the request names; asking for the rest")
            again = ask_json(self.planner, self.run, "plan-complete", SYSTEM,
                             prompt + "\n\nYour plan so far:\n" + json.dumps(plan, indent=1)
                             + f"\n\nIt has no task for {', '.join(missing)}, which the request names. Reply with "
                               "the complete plan: the tasks above and the missing ones.",
                             PLAN_SCHEMA, validate, think=False, max_tokens=16000, timeout=self.timeout)
            if len(again["tasks"]) > len(plan["tasks"]):
                plan = self.prefer_move(again)
        self.run.save("plan.json", plan)
        return plan

    def plan_with_lookups(self, prompt: str, validate) -> dict:
        """Planning as a conversation: the planner may read, search, or ask the reader machine about
        files, in rounds, before answering with the plan. Reads and searches are exact and instant;
        an `ask` goes to the fast reader (Windows), so the planner's machine doesn't read whole files."""
        budget = self.think_budget().get("think_budget", self.planner.get("think_budget", 2500))
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt + LOOKUP_NOTE}]
        think, asked = True, 0
        for turn in range(1, self.cfg.get("lookup_rounds", 4) + 2):
            last = turn > self.cfg.get("lookup_rounds", 4)
            content, thinking, calls, cut = chat_turn(self.planner, self.run, f"plan-{turn}", messages,
                                                      tools=None if last else LOOKUP_TOOLS, think=think,
                                                      think_budget=budget, max_tokens=16000, timeout=self.timeout)
            if cut:  # notes so far; decide without more thinking
                messages.append({"role": "user", "content": "Your thinking so far (cut short, but use it):\n"
                                 + thinking[-6000:] + "\n\nNo more thinking: call the tools you still need, or "
                                 "reply with the plan as the JSON object."})
                think = False
                continue
            if calls and not last:
                messages.append({"role": "assistant", "content": content, "tool_calls": calls})
                for call in calls[:6]:
                    result = self.lookup(call, asked)
                    asked += call["name"] == "ask"
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": result[:8000]})
                for call in calls[6:]:
                    messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"],
                                     "content": "(not run: at most 6 look-ups per round)"})
                think = False  # thinking once is enough; later rounds read the answers and decide
                continue
            try:
                return validate(extract_json(content))
            except (ValueError, KeyError, TypeError) as e:
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": f"That wasn't a usable plan ({e}). Reply with only the "
                                 "plan as the JSON object."})
                break
        ask = messages + [{"role": "user", "content": "Reply now with only the plan as the JSON object."}]
        content, _, _, _ = chat_turn(self.planner, self.run, "plan-final", ask, schema=PLAN_SCHEMA, think=False,
                                     max_tokens=16000, timeout=self.timeout)
        try:
            return validate(extract_json(content))
        except (ValueError, KeyError, TypeError) as e:
            say(f"  {warn()} the plan was rejected ({str(e)[:120]}); asking once more")
            content, _, _, _ = chat_turn(self.planner, self.run, "plan-final-retry", ask + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"That plan was rejected: {e}\nReply with only the corrected plan as the JSON object."}],
                schema=PLAN_SCHEMA, think=False, max_tokens=16000, timeout=self.timeout)
            return validate(extract_json(content))

    def lookup(self, call: dict, asked: int) -> str:
        """Run one of the planner's look-ups: read and search here, exactly; ask on the reader."""
        args, name = call["arguments"], call["name"]
        if name == "read":
            what = str(args.get("what") or "")
            where = locate(self.repo, self.index.files, self.index.outline(), what) or \
                (clean_path(what) if SPEC.match(clean_path(what)) and clean_path(what).split(":")[0] in self.index.files else None)
            text = self.part(where) if where and ":" in where else (
                read_spec(self.repo, self.index.files, where, self.cfg["max_file_chars"]) if where else
                f"!! `{what}` isn't a name or a file:range in this repository")
            say(f"  {ok()} {machine_label(self.planner)} read {what[:70]}")
            return text
        if name == "search":
            pattern, path = str(args.get("pattern") or ""), str(args.get("path") or "")
            say(f"  {ok()} {machine_label(self.planner)} searched /{pattern[:50]}/" + (f" in {path}" if path else ""))
            return grep_text(self.repo, pattern, [path] if path else [], limit=40)
        if name == "ask":
            if asked >= self.cfg.get("lookup_asks", 6):
                return "(not asked: the limit of questions for this plan is reached)"
            question = str(args.get("question") or "")
            files = [f for f in (clean_path(str(x)) for x in args.get("files") or []) if f in self.index.files][:4]
            if not files:
                return "!! name the files to read (paths from the repository)"
            start = time.time()
            text = ASK_PROMPT.format(question=question, files="\n\n".join(
                self.file_for_question(f, question) for f in files))
            answer = chat(self.chores, self.run, f"ask-{asked + 1}", SYSTEM, text, think=False, max_tokens=900,
                          timeout=self.timeout, quiet=True)
            say(f"  {ok()} {self.worker} answered: {question[:60]} ({elapsed(start)})")
            facts = ground_truth(answer, self.repo, self.index.files)
            return answer + (f"\n\n{facts}" if facts else "")
        return f"!! no tool called {name}"

    def file_for_question(self, path: str, question: str) -> str:
        """A file for the reader to answer from: whole when it's short, else its outline plus the
        stretches around the question's terms."""
        lines = self.index.files.get(path, 0)
        if lines <= 600:
            return numbered(self.repo, path, self.cfg["max_file_chars"])
        decls = decls_of(self.repo, path, self.index.outline())
        spots = self.hot_spots(path, 1, lines, text=question, budget=300)
        return (f"=== {path} ({lines} lines; outline) ===\n" + outline_text(decls)[:6000] + "\n"
                + "\n".join(read_spec(self.repo, self.index.files, f"{path}:{a}-{b}", self.cfg["max_file_chars"])
                            for a, b in spots))

    def code_task(self, task: dict, commit: bool = True) -> dict:
        """One file's task: the planner's exact edits applied directly when they fit, else a coder
        session. Anything else the coder touches is put back as it was."""
        self.involved = list(dict.fromkeys(getattr(self, "involved", []) + [task["file"]]))
        if task["action"] in ("move", "share"):
            return self.apply_move(task, commit)
        if task.get("edits"):
            done = self.apply_planned(task, commit)
            if done:
                return done
        c = self.cfg["coder"]
        prompt = coder_prompt(self.request, task, self.task_focus(task))
        cmd = [self.cfg["opencode_bin"], "run", "--standalone", "--agent", c["opencode_agent"], "-m", c["opencode_model"],
               "--auto", "--title", f"buckets {task['id']} {task['file']}", prompt]
        note = ""
        for attempt in range(1, c["attempts"] + 1):
            before = snapshot(self.repo, changed_paths(self.repo) + [task["file"]])
            start = time.time()
            head = f"  {task['id']} · coder on {task['file']}" + (f" (try {attempt})" if attempt > 1 else "")
            with Ticker(f"{head} · starting") as tick:
                def show(line):
                    action = coder_action(line)
                    if action:
                        tick.text = f"{head} · {action}"
                code, out, timed_out = run_proc(cmd, self.repo, c["timeout_s"], on_line=show)
            out = strip_ansi(out)
            self.run.save(f"coder-{task['id']}-a{attempt}.log", f"{prompt}\n\n===== output =====\n{out}")
            touched = touched_since(self.repo, before)
            stray = [p for p in touched if p != task["file"]]
            if stray:
                restore(self.repo, before, stray)
                say(f"  {warn()} {task['id']}: undid changes outside its file: {', '.join(stray)}")
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            final = next((l for l in reversed(lines) if l.startswith(("DONE", "BLOCKED"))), "")
            secs = int(time.time() - start)
            if task["file"] in touched:
                if commit:
                    git(self.repo, "add", "-A", "--", task["file"])
                    git(self.repo, "commit", "--no-verify", "-q", "-m",
                        f"buckets {task['id']}: {task['action']} {task['file']}\n\n{task['instructions'][:1500]}")
                note = final.removeprefix("DONE:").strip() or f"changed {task['file']}"
                say(f"  {ok()} {task['id']} {task['file']} ({secs}s): {paint(note[:150], 'dim')}")
                return {**task, "status": "done", "note": final}
            if final.startswith("BLOCKED"):
                say(f"  {warn()} {task['id']} {task['file']}: {final}")
                return {**task, "status": "blocked", "note": final}
            note = "timed out" if timed_out else (final or "no change made")
            if (attempt < c["attempts"] and not timed_out
                    and self.cfg.get("offload", {}).get("escalate_repeated_failure")):
                task = self.rewrite_tasks([task], f"{task['id']} made no change on its first try")[0]
                prompt = coder_prompt(self.request, task, self.task_focus(task))
                cmd[-1] = prompt
            errors = [l for l in lines if l.startswith("Error:")]
            if errors:
                note += f" ({errors[-1][:200]})"
            say(f"  {warn()} {task['id']} {task['file']}, try {attempt}: {note}")
        return {**task, "status": "failed", "note": note}

    def apply_planned(self, task: dict, commit: bool) -> dict | None:
        """Apply the planner's edits without a coder session; None (file untouched) when they don't
        apply cleanly or leave a Python file that doesn't parse, so the coder does the task instead."""
        path, start = self.repo / task["file"], time.time()
        before = path.read_text(errors="replace") if path.is_file() else None
        try:
            edits = task["edits"]
            if task["action"] == "create" and len(edits) == 1 and not edits[0]["at"].strip():
                after = edits[0]["code"]
            elif before is None:
                raise ValueError("the file doesn't exist")
            else:
                f, outline = task["file"], self.index.outline()
                decls_for = py_decls_text if f.endswith(".py") else (lambda _text: decls_of(self.repo, f, outline))
                after = apply_edits(before, edits, decls_for)
            if after == before:
                raise ValueError("the edits change nothing")
            if task["file"].endswith(".py"):
                try:
                    ast.parse(after)
                except SyntaxError as e:
                    raise ValueError(f"the result doesn't parse (line {e.lineno}: {e.msg})")
        except ValueError as e:
            say(f"  {warn()} {task['id']}: the planned edits don't apply ({str(e)[:120]}); the coder does it")
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(after)
        if commit:
            git(self.repo, "add", "-A", "--", task["file"])
            git(self.repo, "commit", "--no-verify", "-q", "-m",
                f"buckets {task['id']}: {task['action']} {task['file']}\n\n{task['instructions'][:1500]}")
        note = f"applied the planned edit{'s' if len(task['edits']) != 1 else ''} directly"
        say(f"  {ok()} {task['id']} {task['file']} ({int(time.time() - start)}s): {paint(note, 'dim')}")
        return {**task, "status": "done", "note": f"DONE: {note}"}

    def apply_move(self, task: dict, commit: bool) -> dict:
        """A move or share done mechanically by ts_moves.cjs: the code is moved exactly, never retyped.
        When it can't be done, the task fails with the reason, for the review to plan around."""
        start = time.time()
        if task["action"] == "move":
            payload = {"op": "extract", "file": task["file"], "function": task["where"], "select": task["select"],
                       "into": task["into"], "name": task["name"]}
        else:
            return self.apply_share(task, commit, start)
        d = ts_moves(self.repo, payload)
        if not d.get("ok"):
            why = d.get("error", "it didn't run")
            say(f"  {warn()} {task['id']}: the {task['action']} couldn't be done ({why[:160]})")
            return {**task, "status": "failed", "note": f"BLOCKED: {why}"}
        written = []
        for f, text in d["files"].items():
            path = (self.repo / f).resolve()
            if self.repo.resolve() not in path.parents:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            written.append(f)
        self.involved = list(dict.fromkeys(self.involved + written))
        r = d["report"]
        if task["action"] == "move":  # its signature is written from the component's own types
            self.generated_lines = getattr(self, "generated_lines", set()) | {
                same_line(l) for f in written for l in d["files"][f].splitlines() if f"function {task['name']}(" in l}
            note = (f"moved {r['moved']} statements ({r['moved_lines']} lines) of {task['where']} into "
                    f"{task['name']} in {', '.join(f for f in written if f != task['file'])}; parameters: "
                    f"{', '.join(p['name'] for p in r['params']) or 'none'}; returns {len(r['returns'])} values; "
                    f"helpers moved along: {', '.join(r['helpers_moved']) or 'none'}"
                    + (f"; notes: {'; '.join(r['notes'])}" if r["notes"] else ""))
        else:
            note = (f"moved {', '.join(r['shared']) or 'nothing'} into {task['file']}"
                    + (f"; not moved: {'; '.join(x['name'] + ' (' + x['why'] + ')' for x in r['skipped'])}" if r["skipped"] else ""))
        if not written or (task["action"] == "share" and not r["shared"]):
            say(f"  {warn()} {task['id']}: nothing was moved ({note[:160]})")
            return {**task, "status": "failed", "note": f"BLOCKED: {note}"}
        if commit:
            git(self.repo, "add", "-A", "--", *written)
            git(self.repo, "commit", "--no-verify", "-q", "-m", f"buckets {task['id']}: {task['action']} ({note[:200]})\n\n{note}")
        say(f"  {ok()} {task['id']} {task['action']} ({int(time.time() - start)}s): {paint(note[:220], 'dim')}")
        return {**task, "status": "done", "note": f"DONE: {note}"}

    def apply_share(self, task: dict, commit: bool, start: float) -> dict:
        """Identical top-level declarations of two variants go into the shared file; identical statements
        inside their components go into a shared hook next to it. Each part is mechanical."""
        a, b = task["from"]
        cmp = ts_moves(self.repo, {"op": "compare", "a": a, "b": b}, timeout=120)
        if not cmp.get("ok"):
            return {**task, "status": "failed", "note": f"BLOCKED: {cmp.get('error', 'compare failed')}"}
        same = {p["name"]: p for p in cmp["pairs"] if p["status"] == "identical"}
        top = [n for n in task["select"] if same.get(n, {}).get("module_level")]
        inner = [n for n in task["select"] if same.get(n, {}).get("depth") == 1]
        skipped = [n for n in task["select"] if n not in top and n not in inner]
        notes, written, kept_why = [], [], []

        def write(d: dict) -> None:
            for f, text in d["files"].items():
                path = (self.repo / f).resolve()
                if self.repo.resolve() in path.parents:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                    written.append(f)
        if top:
            d = ts_moves(self.repo, {"op": "share", "files": [a, b], "names": top, "into": task["file"]})
            if d.get("ok") and d["report"]["shared"]:
                write(d)
                notes.append(f"moved {', '.join(d['report']['shared'])} into {task['file']}")
                kept_why = d["report"]["skipped"]
                state = [n for n in d["report"]["shared"] if re.search(rf"(?m)^\s*(?:export\s+)?(?:let|var)\s+{re.escape(n)}\b",
                                                                        d["files"].get(task["file"], ""))]
                if state:
                    notes.append(f"{', '.join(state)} is module state (a module-level `let`): it moved with everything that "
                                 "assigns it, so each screen reads and writes it as before, but both variants now share one "
                                 "copy (only matters if both are mounted at once; each app normally ships one variant)")
            else:
                notes.append(f"top-level share not done ({d.get('error') or 'nothing could move'})")
        if inner:
            parents = {same[n]["parent"] for n in inner}
            hook_file, hook = task["file"], shared_hook_names(task["file"], a)[1]  # in the shared file the request named
            d = ts_moves(self.repo, {"op": "share_hook", "files": [a, b], "function": parents.pop(), "names": inner,
                                     "into": hook_file, "name": hook}) if len(parents) == 1 else \
                {"ok": False, "error": "the statements are in different functions"}
            if d.get("ok"):
                write(d)
                r = d["report"]
                moved_names = r.get("names") or inner
                if r.get("left"):
                    notes.append(f"kept in each component (they depend on code that differs): {', '.join(r['left'][:10])}")
                notes.append(f"moved {len(moved_names)} identical statements of the components ({', '.join(moved_names[:8])}) "
                             f"into {hook} in {hook_file}; "
                             f"parameters: {', '.join(p['name'] for p in r['params']) or 'none'}; re-derived inside: "
                             f"{', '.join(r.get('rederived') or []) or 'nothing'}")
                self.generated_lines = getattr(self, "generated_lines", set()) | {
                    same_line(l) for l in d["files"][[f for f in d["files"] if f not in (a, b)][0]].splitlines()
                    if f"function {hook}(" in l}
            else:
                notes.append(f"shared hook not made ({d.get('error', '')[:200]})")
        if skipped:
            notes.append(f"not moved (not identical in both): {', '.join(skipped[:12])}")
        if kept_why:
            notes.append("left in place: " + "; ".join(f"{x['name']} ({x['why']})" for x in kept_why[:8]))
        note = "; ".join(notes)
        written = list(dict.fromkeys(written))
        if not written:
            say(f"  {warn()} {task['id']}: nothing was shared ({note[:160]})")
            return {**task, "status": "failed", "note": f"BLOCKED: {note}"}
        self.involved = list(dict.fromkeys(self.involved + written))
        if commit:
            git(self.repo, "add", "-A", "--", *written)
            git(self.repo, "commit", "--no-verify", "-q", "-m", f"buckets {task['id']}: share ({note[:200]})\n\n{note}")
        say(f"  {ok()} {task['id']} share ({int(time.time() - start)}s): {paint(note[:220], 'dim')}")
        return {**task, "status": "done", "note": f"DONE: {note}"}

    def changed_code(self, base: str, budget: int = 14000) -> str:
        """The whole of every function or method the change touches, as it is now: a diff's few
        lines of context can hide where a name is defined (a review called a variable undefined
        that was set ten lines above the hunk)."""
        out, used = [], 0
        for f in git(self.repo, "diff", "--name-only", f"{base}..HEAD").split():
            if not (self.repo / f).is_file():
                continue
            decls = [d for d in decls_of(self.repo, f, self.index.outline()) if not d["signature"].endswith("= …")]
            diff = git(self.repo, "diff", "-U0", f"{base}..HEAD", "--", f, check=False)
            touched = {int(m.group(1)) for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)", diff, re.M)}
            spans = set()
            for line in touched:
                around = [d for d in decls if d["line"] <= line <= d["end"] and d["end"] - d["line"] <= 150]
                if around:
                    best = min(around, key=lambda d: d["end"] - d["line"])
                    spans.add((best["line"], best["end"], best["name"]))
            for lo, hi, name in sorted(spans):
                text = read_spec(self.repo, self.index.files, f"{f}:{lo}-{hi}", self.cfg["max_file_chars"])
                if used + len(text) > budget:
                    break
                out.append(text)
                used += len(text)
        return ("\nThe changed functions in full, as they are now (to check names and context the diff "
                "doesn't show):\n" + "\n\n".join(out) + "\n") if out else ""

    def phrases_now(self, base: str) -> str:
        """Where the request's phrases and quoted strings appear in the changed files now, found
        mechanically: for "remove X" the proof that X is gone (or isn't), for "add X" that it's there."""
        changed = [f for f in git(self.repo, "diff", "--name-only", f"{base}..HEAD").split() if (self.repo / f).is_file()]
        terms = sorted({" ".join(r[i:j]) for r in word_runs(self.request) for i in range(len(r))
                        for j in range(i + 2, min(len(r), i + 4) + 1)}
                       | {a or b for a, b in re.findall(r'"([^"]{3,})"|`([^`]{3,})`', self.request)})
        if not changed or not terms:
            return ""
        rows, texts = [], {f: (self.repo / f).read_text(errors="replace").splitlines() for f in changed}
        for t in terms:
            rx = re.compile(re.escape(t).replace(r"\ ", r"[\s_-]*"), re.I)
            hits = [(f, i, line) for f, lines in texts.items() for i, line in enumerate(lines, 1) if rx.search(line)]
            if len(hits) > 6:  # the files' own subject (work order details in its own screen), not a thing to find
                terms = [x for x in terms if x != t]
                continue
            rows += [f"- \"{t}\" still in {f}:{i}: {line.strip()[:120]}" for f, i, line in hits]
        if not rows:
            return ("\nThe request's phrases (" + ", ".join(f'"{t}"' for t in terms[:8]) + ") no longer appear in "
                    "the changed files (checked mechanically).\n")
        return ("\nWhere the request's phrases appear in the changed files now (checked mechanically):\n"
                + "\n".join(rows[:30]) + "\n")

    def task_focus(self, task: dict) -> str:
        """The code a task is about, read now (earlier tasks may have moved it), so the coder starts at
        its place instead of reading a long file to find it: the task's "where", or else the places its
        instructions name ("`fill_in`", "lines 120-140")."""
        path = self.repo / task["file"]
        if task["action"] != "edit" or not path.is_file():
            return ""
        lines = path.read_text(errors="replace").splitlines()
        spots = self.task_spots(task, len(lines))
        if spots:
            blocks, budget = [], 250
            for lo, hi in spots:
                lo, hi = max(1, lo - 3), min(len(lines), hi + 3, lo + budget)
                budget -= hi - lo + 1
                blocks.append(f"=== {task['file']}:{lo}-{hi} ===\n"
                              + "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(lo, hi + 1)))
                if budget <= 0:
                    break
            return (f"The code this task is about, exactly as it is now (`{task['file']}` has {len(lines)} lines; "
                    f"you don't need to read the rest unless the task needs it, and then read only those lines "
                    f"with offset and limit):\n" + "\n".join(blocks))
        if len(lines) >= self.cfg.get("targeted_min_lines", 0) > 0:
            return (f"`{task['file']}` has {len(lines)} lines: don't read it whole. Search for the names in "
                    f"your task first, then read only the lines around them (offset and limit).")
        return ""

    def task_spots(self, task: dict, n: int) -> list[tuple[int, int]]:
        """Line ranges in the task's file: its "where", else up to 3 places its instructions name (each
        under 150 lines, so a whole class doesn't crowd out the method that matters)."""
        f, outline = task["file"], self.index.outline()
        where = task.get("where", "").removeprefix(f + ":")
        hit = locate(self.repo, {f: n}, outline, where, f) if where else None
        size = (lambda m: int(m.group(3) or m.group(2)) - int(m.group(2)))(SPEC.match(hit)) if hit else 0
        if where and size <= 150:
            specs, longest = [where], 10 ** 6
        else:  # no "where", or a whole long class: the places the instructions name
            text = task["instructions"]
            specs = [f"{a}-{b or a}" for a, b in re.findall(r"\blines? (\d+)(?:\s*[-–]\s*(\d+))?", text)]
            specs += re.findall(r"\b([A-Z]\w*\.[A-Za-z_]\w*)\b", text) + request_names(text)
            # Quoted UI text or strings ("Original ETA"): the lines they're on, with some context
            lines = (self.repo / f).read_text(errors="replace").splitlines()
            for q in [a or b for a, b in re.findall(r'"([^"\n]{3,80})"|\'([^\'\n]{3,80})\'', text)][:6]:
                where_q = [i for i, l in enumerate(lines, 1) if q in l][:2]
                specs += [f"{max(1, i - 6)}-{min(n, i + 8)}" for i in where_q]
            longest = 150
        spots: list[tuple[int, int]] = []
        for spec in specs:
            hit = locate(self.repo, {f: n}, outline, spec, f)
            if not hit:
                continue
            m = SPEC.match(hit)
            lo, hi = int(m.group(2)), int(m.group(3) or m.group(2))
            if hi - lo < longest and lo <= n and not any(a <= lo and hi <= b for a, b in spots):
                spots = [(a, b) for a, b in spots if not (lo <= a and b <= hi)] + [(lo, hi)]
            if len(spots) == 3:
                break
        merged: list[tuple[int, int]] = []
        for lo, hi in sorted(spots):  # neighbours become one block
            if merged and lo <= merged[-1][1] + 7:
                merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
            else:
                merged.append((lo, hi))
        return merged

    def diff_text(self, base: str) -> str:
        stat = git(self.repo, "diff", "--stat", f"{base}..HEAD")
        full = git(self.repo, "diff", f"{base}..HEAD")
        budget = self.cfg["diff_budget_chars"]
        if len(full) > budget:
            full = full[:budget] + f"\n… (diff cut off; {len(full) - budget} more characters)"
        return f"{stat}\n{full}" if full.strip() else "(no changes)"

    def auto_fix(self, base: str) -> None:
        """Run the repo's own formatters and lint auto-fixers on the files this work changed, and
        commit what they change, so review rounds aren't spent on formatting."""
        fixers = detect_fixers(self.repo, self.rc)
        changed = [f for f in git(self.repo, "diff", "--name-only", f"{base}..HEAD").split() if (self.repo / f).is_file()]
        if not changed:
            return
        ran = ["leftover blank lines"] if [f for f in changed if drop_leftover_blanks(self.repo, base, f)] else []
        for cmd in fixers:
            files = [f for f in changed if "prettier" not in cmd or formatted_before(self.repo, base, f, cmd)]
            filled = fill_fixer(cmd, files)
            if not filled:
                continue
            with Ticker(f"  Tidying: {cmd.split()[0].split('/')[-1]}"):
                code, out, _ = run_proc(filled, self.repo, 300, shell=True)
            self.run.save(f"fixer-{len(ran) + 1}.log", f"$ {filled}\n(exit {code})\n\n{strip_ansi(out)}")
            ran.append(fixer_name(cmd))
        fixed = [p for p in changed_paths(self.repo) if p in changed]
        stray = [p for p in changed_paths(self.repo) if p not in changed]
        if stray:  # fixers only get the changed files, but undo anything else to be safe
            revert_paths(self.repo, stray)
        if fixed:
            git(self.repo, "add", "-A", "--", *fixed)
            git(self.repo, "commit", "--no-verify", "-q", "-m", f"buckets: auto-fix ({', '.join(ran)})")
            say(f"  {ok()} auto-fixers ({', '.join(ran)}) tidied {len(fixed)} file(s)")
        elif ran:
            detail(f"auto-fixers ran ({', '.join(ran)}); nothing to change")

    def api_impact(self, base: str) -> dict | None:
        """Compare every file's exports now with the start: changed signatures, removed and added
        exports, and callers of the changed ones that this work didn't touch."""
        before = getattr(self, "surface_before", None)
        after = self.index.surface() if before is not None else None
        if before is None or after is None:
            return None
        touched = set(git(self.repo, "diff", "--name-only", f"{base}..HEAD").split())
        removed, changed, added, untouched = [], [], [], []
        # kind and name together: TypeScript lets a type and a value share a name (interface X + const X)
        for f, decls in before.items():
            now = {(d.get("kind"), d["name"]): d for d in after.get(f, [])}
            for d in decls:
                n = now.get((d.get("kind"), d["name"]))
                if n is None:
                    removed.append(f"`{d['name']}` from `{f}`")
                elif n["signature"] != d["signature"]:
                    changed.append({"file": f, "name": d["name"], "before": d["signature"], "after": n["signature"]})
        for f, decls in after.items():
            had = {(d.get("kind"), d["name"]) for d in before.get(f, [])}
            added += [f"`{d['name']}` in `{f}`: {d['signature'][:100]}" for d in decls
                      if (d.get("kind"), d["name"]) not in had]
        for c in changed:
            for u in (self.index.uses(c["name"], c["file"]) or {"uses": []})["uses"]:
                if u["file"] != c["file"] and u["file"] not in touched and not u.get("reexport"):
                    lines = ", ".join(map(str, u["lines"][:5])) or "?"
                    untouched.append(f"`{u['file']}` (lines {lines}) uses `{c['name']}`, whose signature changed")
        return {"removed": removed, "changed": changed, "added": added, "untouched": untouched}

    def review(self, plan: dict, results: list, base: str, baseline: dict, now: dict, rnd: int,
               shortcuts: list[str], api: dict | None = None, target=None, note: str = "",
               reviewer: dict | None = None, prompt_out: list | None = None) -> dict:
        """One model's verdict. `reviewer` reviews instead of the planner (quietly: it runs beside
        the planner's review); `prompt_out` receives the prompt, for a follow-up question."""
        res = "\n".join(f"- {r['id']} {r['action']} {r['file']}: {r['status']} {r['note']}" for r in results)
        chk = []
        for name, r in now.items():
            before = baseline.get(name, {}).get("ok")
            line = f"- {name}: {'pass' if before else 'FAIL'} -> {'pass' if r['ok'] else 'FAIL'}"
            if not r["ok"]:
                parsed = diagnostics_text(r["output"], self.repo, self.index.files)
                line += (f"\n{parsed}" if parsed else "") + f"\n  output (end):\n{r['tail']}"
            chk.append(line)
        prompt = REVIEW_PROMPT.format(
            request=self.request, summary=plan["summary"], results=res,
            checks="\n".join(chk) or "(no checks configured)",
            shortcuts=("\nShortcuts found in the diff (must be replaced with real fixes):\n"
                       + "\n".join(f"- {h}" for h in shortcuts) + "\n") if shortcuts else "",
            impact=impact_text(api) + self.phrases_now(base) + self.weak_tests_text(base) + self.i18n_text(base)
                   + self.changed_code(base),
            diff=self.diff_text(base), rules=TASK_RULES.format(max_tasks=self.cfg["max_tasks"]),
            agent_rules=self.agent_rules_text())

        def validate(d):
            return {"done": bool(d["done"]), "summary": str(d["summary"]),
                    "issues": [str(i) for i in d.get("issues") or []],
                    "tasks": validate_tasks(d.get("tasks") or [], self.repo, self.cfg["max_tasks"], f"R{rnd + 1}T")}

        moved = [r for r in results if r.get("action") in ("move", "share") and r.get("status") == "done"]
        if moved:
            prompt += ("\n\nCode moved mechanically, byte for byte (" + "; ".join(r["note"].removeprefix("DONE: ")[:300] for r in moved)
                       + "). Judge the move: does everything still connect, and is behaviour unchanged? Problems that were "
                       "already in the moved code before (types, untranslated strings, style) are not this change's to fix "
                       "unless the request asks: don't list them as issues (the smallest change that does the request wins).")
        prompt = note + prompt
        if prompt_out is not None:
            prompt_out.append(prompt)
        if reviewer:
            return ask_json(reviewer, self.run, f"second-review-{rnd}-{machine_label(reviewer).lower().replace(' ', '-')}",
                            SYSTEM, prompt, REVIEW_SCHEMA, validate, max_tokens=16000, timeout=self.timeout,
                            quiet=True, **self.think_budget())
        if target:
            return self.ask_escalated(target, f"review-{rnd}-escalated", prompt, REVIEW_SCHEMA, validate)
        if self.mode == "claude":
            return self.claude(f"review-{rnd}", CLAUDE_INTRO + "\nYou may open files to verify the diff.\n\n"
                               + prompt, REVIEW_SCHEMA, validate)
        return ask_json(self.planner, self.run, f"review-{rnd}", SYSTEM, prompt, REVIEW_SCHEMA, validate,
                        max_tokens=16000, timeout=self.timeout, **self.think_budget())

    def i18n_text(self, base: str) -> str:
        gaps = i18n_gaps(self.repo, git(self.repo, "diff", f"{base}..HEAD"), self.index.files)
        return ("\nLocale entries, checked mechanically (new user-facing strings need one):\n"
                + "\n".join(f"- {g}" for g in gaps) + "\n") if gaps else ""

    def agent_rules_text(self) -> str:
        rules = agent_rules(self.repo)
        return ("- The repo's own rules (from its agent doc). The change must follow every one that applies; "
                "list each it breaks as an issue, naming the rule:\n" + rules + "\n") if rules else ""

    def weak_tests_text(self, base: str) -> str:
        found = weak_tests(git(self.repo, "diff", "-U0", f"{base}..HEAD"))
        return ("\nNew tests, checked mechanically (tests like these pass whatever the code does):\n"
                + "\n".join(f"- {f}" for f in found) + "\n") if found else ""

    def second_reviewer(self) -> dict | None:
        """The worker's model, when the planner is on another machine and the worker answers: it reviews
        at the same time, so a second look costs no extra time."""
        if self.mode == "claude" or getattr(self, "on_windows", False):
            return None
        if not self.cfg.get("offload", {}).get("double_review"):
            return None
        t = self.cfg.get("thinker")
        if not t or t["url"].rstrip("/") == self.planner["url"].rstrip("/") or not self.thinker_up:
            return None
        # The round waits for both, so it thinks no longer than the planner does.
        return {**t, "think_budget": self.planner.get("think_budget") or t.get("think_budget", 0)}

    def double_review(self, plan: dict, results: list, base: str, baseline: dict, now: dict, rnd: int,
                      shortcuts: list[str], api: dict | None = None) -> dict:
        """The planner and the worker review at once. Any issue wins, except a lone claim of a definite
        error ("undefined", "never called") while every check passes: the other reviewer confirms it
        first, since one such false claim once broke working code (field notes 19)."""
        second = self.second_reviewer()
        if not second:
            return self.review(plan, results, base, baseline, now, rnd, shortcuts, api)
        self.review_base = base
        out: dict = {}
        prompts: list = []

        def work() -> None:
            try:
                out["second"] = self.review(plan, results, base, baseline, now, rnd, shortcuts, api, reviewer=second)
            except (ModelError, OSError) as e:
                out["second"] = e
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        first = self.review(plan, results, base, baseline, now, rnd, shortcuts, api, prompt_out=prompts)
        thread.join()
        other = out.get("second")
        if not isinstance(other, dict):
            say(f"  {warn()} the second review on {machine_label(second)} didn't come back ({str(other)[:120]})")
            return first
        return self.merge_reviews(first, other, second, now, prompts[0] if prompts else "", rnd)

    def merge_reviews(self, first: dict, other: dict, second: dict, now: dict, prompt: str, rnd: int) -> dict:
        home, away = self.home, machine_label(second)
        if first["done"] == other["done"]:
            say(f"  Both reviews ({home} and {away}) say " + ("done" if first["done"] else "not done"))
            if first["done"]:
                return first
            files = {t["file"] for t in first["tasks"]}
            extra = [t for t in other["tasks"] if t["file"] not in files]
            tasks = (first["tasks"] + extra)[:self.cfg["max_tasks"]]
            return {**first, "issues": first["issues"] + [f"({away}) {i}" for i in other["issues"]
                                                          if i not in first["issues"]],
                    "tasks": [{**t, "id": f"R{rnd + 1}T{n}"} for n, t in enumerate(tasks, 1)]}
        dissent, agree = (first, other) if not first["done"] else (other, first)
        who, judge = (home, second) if dissent is first else (away, self.planner)
        say(f"  The reviews differ: {who} found problems, the other says done")
        definite = [i for i in dissent["issues"] if DEFINITE_CLAIM.search(i)]
        if not all(r["ok"] for r in now.values()) or not definite or len(definite) < len(dissent["issues"]):
            return dissent  # any issue wins unless it is only claims the other side should confirm
        real = self.confirm_claims(judge, prompt, agree, definite, rnd)
        if real:
            return {**dissent, "issues": real + [i for i in dissent["issues"] if i not in definite]}
        say(f"  {ok()} {who}'s claim{'s' if len(definite) > 1 else ''} didn't hold up; keeping the work")
        return {**agree, "summary": agree["summary"] + f" (A second reviewer's claim was checked and didn't "
                                                       f"hold up: {'; '.join(definite)[:300]})"}

    def confirm_claims(self, judge: dict, prompt: str, verdict: dict, claims: list[str], rnd: int) -> list[str]:
        """Ask the reviewer who said done whether the other's definite-error claims are real, with where
        the names they mention appear now. Its own review is the conversation so far, so the long
        prompt comes from its cache. Anything unclear counts the claims as real."""
        touched = [f for f in git(self.repo, "diff", "--name-only", f"{getattr(self, 'review_base', 'HEAD~1')}..HEAD").split() if (self.repo / f).is_file()]
        names = sorted({n for c in claims for n in re.findall(r"`([A-Za-z_][\w.]*)`", c)})[:6]
        lines = []
        for n in names:
            hits = git(self.repo, "grep", "-n", "-w", "-e", n.split(".")[-1], "--", *touched).splitlines() if touched else []
            lines.append(f"`{n}`:\n" + ("\n".join(f"  {h[:160]}" for h in hits[:8]) or "  (not found in the changed files)"))
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt},
                    {"role": "assistant", "content": json.dumps(verdict)},
                    {"role": "user", "content": CONFIRM_PROMPT.format(
                        claims="\n".join(f"{n}. {c}" for n, c in enumerate(claims, 1)),
                        evidence="\n".join(lines) or "(no names quoted)")}]
        try:
            content, notes, _, cut = chat_turn(judge, self.run, f"confirm-{rnd}", messages, schema=CONFIRM_SCHEMA,
                                               think=True, think_budget=judge.get("think_budget") or 2000,
                                               max_tokens=4000, timeout=self.timeout)
            d = extract_json(content)
            picked = {int(x) for x in d["real"]}
            detail(f"confirm-{rnd}: {d.get('why', '')[:200]}")
        except (ModelError, OSError, ValueError, KeyError, TypeError) as e:
            say(f"  {warn()} couldn't confirm the claims ({str(e)[:100]}); treating them as real")
            return claims
        return [c for n, c in enumerate(claims, 1) if n in picked]

    def gate(self, verdict: dict, shortcuts: list, api, now: dict, baseline: dict) -> dict:
        """The mechanical gates a reviewer can't overrule: shortcuts and regressed checks."""
        verdict["shortcuts"], verdict["api"] = shortcuts, api
        if shortcuts and verdict["done"]:
            verdict["done"] = False
            verdict["issues"].append(f"{len(shortcuts)} shortcut(s) added instead of real fixes")
        regressions = [n for n, r in now.items() if baseline.get(n, {}).get("ok") and not r["ok"]]
        if regressions and verdict["done"]:
            verdict["done"] = False
            verdict["issues"].append(f"checks regressed: {', '.join(regressions)}")
        return verdict

    def rounds(self, plan: dict, tasks: list, base: str, checks: dict, baseline: dict,
               shortcut_base: str | None = None, now: dict | None = None,
               code_step: int = 4, review_step: int = 5) -> tuple[list, dict | None, dict]:
        """Code the tasks, run checks, review; repeat with follow-up tasks until done or out of rounds.
        `now` is the latest check results when they differ from the baseline (after cherry-picks)."""
        repo, rounds, verdict, now = self.repo, [], None, now or baseline
        failing_before = {n for n, r in now.items() if not r["ok"]}
        errors_before: set = set()
        for rnd in range(1, self.cfg["max_rounds"] + 1):
            self.rounds_used = rnd
            if tasks:
                self.heading(code_step, "Coding" + (f" the follow-ups (round {rnd})" if rnd > 1 else "")
                             + f": {len(tasks)} task{'s' if len(tasks) != 1 else ''} on {machine_label(self.cfg['coder'])}")
            results = [self.code_task(t) for t in tasks]
            rounds.append(results)
            self.heading(review_step, "Checks and review" + (f" (round {rnd})" if rnd > 1 else ""))
            if tasks:
                self.auto_fix(shortcut_base or base)
            if tasks:  # nothing changed otherwise, so the last check results still hold
                now = run_checks(repo, checks, self.run, f"round{rnd}", self.cfg["check_timeout_s"])
            failing = {n for n, r in now.items() if not r["ok"]}
            if (rnd >= 2 and tasks and stuck(failing, failing_before)
                    and self.cfg.get("offload", {}).get("escalate_when_stuck")):
                self.escalate_to_mac(f"round {rnd} made no progress on {', '.join(sorted(failing))}")
            failing_before = failing
            shortcuts = find_shortcuts(git(repo, "diff", "-U0", f"{shortcut_base or base}..HEAD"),
                                       self.rc.get("allow_shortcuts", []), getattr(self, "generated_lines", set()))
            if shortcuts:
                say(f"  {warn()} {len(shortcuts)} shortcut(s) like `any` or skipped tests were added (see the report)")
            api = self.api_impact(base)
            if api and any(api.values()):
                say(f"  API changes: {len(api['changed'])} changed, {len(api['removed'])} removed, "
                    f"{len(api['added'])} added"
                    + (f" · {warn()} {len(api['untouched'])} caller(s) not updated" if api["untouched"] else ""))
            if getattr(self, "checks_define_done", False) and checks_settle_job(results, baseline, now, shortcuts, api):
                say(f"  {ok()} Review skipped: this job is defined by the checks, and they all pass now")
                verdict = {"done": True, "issues": [], "tasks": [],
                           "summary": "Every check that failed at the start passes now, with no regressions, "
                                      "shortcuts or untouched callers; the checks define this job, so no model "
                                      "review was needed."}
            else:
                verdict = self.double_review(plan, results, base, baseline, now, rnd, shortcuts, api)
            said_done = verdict["done"]
            verdict = self.gate(verdict, shortcuts, api, now, baseline)
            offload = self.cfg.get("offload", {})
            # Rule 6: the reviewer and the checks disagree, or it found problems but planned no fixes.
            disagree = (said_done and not verdict["done"]) or (not verdict["done"] and not verdict["tasks"])
            if disagree and offload.get("escalate_review_disagreement"):
                why = ("the reviewer said done but the checks disagree" if said_done
                       else "the reviewer found problems but planned no fixes")
                target = self.escalate(why)
                if target:
                    verdict = self.gate(self.review(plan, results, base, baseline, now, rnd, shortcuts, api, target),
                                        shortcuts, api, now, baseline)
            # Rule 7: a second opinion before "done" when sensitive files changed.
            if verdict["done"]:
                touched = git(repo, "diff", "--name-only", f"{shortcut_base or base}..HEAD").split()
                patterns = list(offload.get("second_opinion") or []) + list(self.rc.get("sensitive") or [])
                hot = [f for f in touched if any(fnmatch.fnmatch(f.lower(), pat.lower()) for pat in patterns)]
                target = self.escalate(f"sensitive files changed: {', '.join(hot[:4])}") if hot else None
                if target:
                    second = self.review(plan, results, base, baseline, now, rnd, shortcuts, api, target,
                                         note=SECOND_OPINION.format(files=", ".join(hot[:8])))
                    if not second["done"]:
                        verdict = self.gate(second, shortcuts, api, now, baseline)
            say(f"  {ok() + ' ' + paint('Done', 'green', 'bold') if verdict['done'] else warn() + ' ' + paint('Not done yet', 'yellow', 'bold')}: "
                f"{verdict['summary'][:300]}")
            if verdict["done"]:
                break
            skeleton = skeleton_tasks(now, repo, self.index.files, f"R{rnd + 1}S", self.cfg["max_tasks"])
            tasks = with_exact_errors(verdict["tasks"], skeleton, self.cfg["max_tasks"])
            # Rule 5b: the same error in the same file after the coder said it was done: rewrite those tasks.
            errors_now = {(sk["file"], m) for sk in skeleton for m in re.findall(r"\): (.+)", sk["instructions"])}
            repeated = {f for f, m in errors_now & errors_before}
            errors_before = errors_now
            if repeated and offload.get("escalate_repeated_failure"):
                again = [t for t in tasks if t["file"] in repeated]
                fixed = {t["id"]: t for t in self.rewrite_tasks(again, f"the same error is still in {', '.join(sorted(repeated))}")}
                tasks = [fixed.get(t["id"], t) for t in tasks]
            if len(tasks) > len(verdict["tasks"]) or skeleton:
                detail(f"follow-ups: {len(verdict['tasks'])} from the review, exact errors added for "
                       f"{len(skeleton)} file(s)")
            if not tasks:
                say(f"  {warn()} The reviewer gave no follow-up tasks, so stopping here")
                break
        return rounds, verdict, now

    # -- cherry-pick mode

    def resolve_conflicts(self, sha: str, subject: str, files: list[str]) -> tuple[bool, str]:
        """Plan and apply a resolution for each conflicted file; True when no markers are left."""
        repo = self.repo
        for f in files:
            path = repo / f
            if not path.is_file():
                return False, f"`{f}` was deleted on one side (modify/delete conflict); resolve it by hand"
            if b"\0" in path.read_bytes()[:8192]:
                return False, f"`{f}` is a binary file; resolve it by hand"
        body = git(repo, "log", "-1", "--format=%b", sha).strip()
        patch = git(repo, "show", "--format=", sha, "--", *files)
        if len(patch) > 20000:
            patch = patch[:20000] + "\n… (cut off)"
        files_text = "\n\n".join(numbered(repo, f, self.cfg["max_file_chars"]) for f in files)
        prompt = CONFLICT_PROMPT.format(sha=sha[:10], subject=subject, body=body or "(no description)",
                                        files=", ".join(files), patch=patch, files_text=files_text,
                                        rules=TASK_RULES.format(max_tasks=len(files)))

        def validate(d):
            plan = validate_plan(d, repo, len(files) + 2, f"C{sha[:7]}-")
            plan["tasks"] = [t for t in plan["tasks"] if t["file"] in files]
            for f in files:  # every conflicted file gets a task, even if the planner skipped one
                if not any(t["file"] == f for t in plan["tasks"]):
                    plan["tasks"].append({"id": f"C{sha[:7]}-{f}", "file": f, "action": "edit",
                                          "instructions": f"Resolve every conflict block in `{f}`, keeping the "
                                                          f"intent of commit {sha[:10]} ({subject}).",
                                          "done_when": "No conflict markers remain.", "read_first": []})
            return plan

        label = f"conflict-{sha[:7]}"
        if self.mode == "claude":
            plan = self.claude(label, CLAUDE_INTRO + "\n\n" + prompt, PLAN_SCHEMA, validate)
        else:
            plan = ask_json(self.planner, self.run, label, SYSTEM, prompt, PLAN_SCHEMA, validate,
                            max_tokens=12000, timeout=self.timeout)
        for t in plan["tasks"]:
            for attempt in range(2):
                self.code_task(t, commit=False)
                left = marker_lines(repo / t["file"])
                if not left:
                    break
                t = {**t, "instructions": t["instructions"] + "\n\nConflict markers are still present at "
                     f"lines {', '.join(map(str, left[:12]))}. Remove every one of them."}
            else:
                return False, f"conflict markers still in `{t['file']}` after two tries"
        return True, "resolved " + ", ".join(f"`{f}`" for f in files)

    def pick(self, refs: list[str], onto: str = "") -> int:
        repo = self.repo
        if changed_paths(repo):
            die(dirty_tree(repo))
        require_identity(repo)
        commits = expand_commits(repo, refs)
        if not commits:
            die("no commits to cherry-pick")
        target = onto or git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        base = git(repo, "rev-parse", "--verify", f"{target}^{{commit}}").strip()
        start_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if start_branch == "HEAD":
            start_branch = git(repo, "rev-parse", "HEAD").strip()
        self.heading(2, f"Applying {len(commits)} commit{'s' if len(commits) != 1 else ''} onto {target}")
        branch = f"buckets/pick-{slug(target, 20)}-{datetime.now().strftime('%m%d-%H%M')}"
        git(repo, "checkout", "-q", "-b", branch, base)
        say(f"  working on a new branch: {paint(branch, 'cyan')}")
        checks = self.checks_with_index(detect_checks(repo, self.rc))
        baseline = run_checks(repo, checks, self.run, "baseline", self.cfg["check_timeout_s"])
        self.surface_before = self.index.surface()

        picks, resolved = [], []
        try:
            for sha, subj in commits:
                r = subprocess.run(["git", "cherry-pick", "-x", sha], cwd=repo, capture_output=True, text=True)
                if r.returncode == 0:
                    picks.append({"sha": sha, "subject": subj, "status": "clean", "note": ""})
                    say(f"  {ok()} {sha[:7]} {subj[:60]}")
                    continue
                conflicted = git(repo, "diff", "--name-only", "--diff-filter=U").split("\n")
                conflicted = [f for f in conflicted if f]
                if not conflicted:
                    if not changed_paths(repo):  # nothing left to apply: it's already on the branch
                        git(repo, "cherry-pick", "--skip")
                        picks.append({"sha": sha, "subject": subj, "status": "skipped",
                                      "note": "already applied"})
                        say(f"  {ok('–')} {sha[:7]} {subj[:60]} {paint('(already on the branch, skipped)', 'dim')}")
                        continue
                    git(repo, "cherry-pick", "--abort", check=False)
                    note = " ".join((r.stderr or r.stdout).split())[:200]
                    picks.append({"sha": sha, "subject": subj, "status": "failed", "note": note})
                    say(f"  {bad()} {sha[:7]} {subj[:60]}: {note}")
                    break
                say(f"  {warn()} {sha[:7]} {subj[:60]}: conflicts in {', '.join(conflicted)}")
                resolved_all, note = self.resolve_conflicts(sha, subj, conflicted)
                if not resolved_all:
                    git(repo, "cherry-pick", "--abort", check=False)
                    picks.append({"sha": sha, "subject": subj, "status": "failed", "note": note})
                    say(f"  {bad()} {sha[:7]} not applied: {note}. Stopping, since later commits may depend on it.")
                    break
                git(repo, "add", "--", *conflicted)
                subprocess.run(["git", "-c", "core.editor=true", "cherry-pick", "--continue"], cwd=repo,
                               capture_output=True, text=True, check=True, env={**os.environ, "GIT_EDITOR": "true"})
                picks.append({"sha": sha, "subject": subj, "status": "resolved", "note": note})
                resolved += conflicted
                say(f"  {ok()} {sha[:7]} applied after resolving the conflicts")
            after_picks = git(repo, "rev-parse", "HEAD").strip()
            self.heading(3, "Checks after the picks")
            now = run_checks(repo, checks, self.run, "picked", self.cfg["check_timeout_s"])
            regressions = [n for n, r in now.items() if baseline.get(n, {}).get("ok") and not r["ok"]]
            rounds, verdict = [], None
            if regressions or resolved:
                why = []
                if resolved:
                    why.append(f"conflicts were resolved in {', '.join(sorted(set(resolved)))}")
                if regressions:
                    why.append(f"checks now fail: {', '.join(regressions)}")
                plan = {"summary": f"Cherry-picked {len(commits)} commit(s) onto {target}; " + "; ".join(why)
                        + ". Check the resolutions keep both sides' intent and fix anything the picks broke.",
                        "tasks": []}
                rounds, verdict, now = self.rounds(plan, [], base, checks, baseline, shortcut_base=after_picks,
                                                   now=now, code_step=4, review_step=4)
            else:
                self.heading(4, "Review: not needed (every commit applied cleanly and checks pass)")
        except KeyboardInterrupt:
            clear_status()
            subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo, capture_output=True)
            say(f"stopped: {leave_branch(repo, branch, start_branch, base)}")
            raise SystemExit(130)
        except Exception as e:
            clear_status()
            subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo, capture_output=True)
            say(f"stopped by an error ({type(e).__name__}: {e}): {leave_branch(repo, branch, start_branch, base)}")
            raise

        done = all(pk["status"] != "failed" for pk in picks) and len(picks) == len(commits) \
            and (verdict is None or verdict["done"]) \
            and not [n for n, r in now.items() if baseline.get(n, {}).get("ok") and not r["ok"]]
        report = render_pick_report(self, picks, commits, rounds, verdict, checks, baseline, now,
                                    branch, base, start_branch, target, done)
        path = self.run.dir / "report.md"
        path.write_text(report)
        if not changed_paths(repo):
            git(repo, "checkout", "-q", start_branch)
        print(report)
        finish(self, "pick", done, branch, target, path)
        return 0 if done else 2

    def code(self, plan_only: bool = False) -> int:
        repo = self.repo
        if changed_paths(repo) and not plan_only:
            die(dirty_tree(repo))
        if not plan_only:
            require_identity(repo)
        checks = self.checks_with_index(detect_checks(repo, self.rc))
        self.heading(2, "Checking the starting point")
        baseline = run_checks(repo, checks, self.run, "baseline", self.cfg["check_timeout_s"])
        self.surface_before = self.index.surface()
        files = self.index.files
        if getattr(self, "plan_file", None):
            self.heading(3, "Using the plan you gave (no reading or planning)")
            plan = {**self.plan_file, "summary": self.plan_file["summary"] or "Plan from --plan-file."}
            self.run.save("plan.json", plan)
        elif getattr(self, "checks_define_done", False) and failures_only_mechanical(baseline, files):
            skeleton = skeleton_tasks(baseline, repo, files, "T", self.cfg["max_tasks"])
            self.heading(3, "Planning from the check errors (no model needed)")
            plan = {"summary": f"Fix the problems the checks report, one task per file ({len(skeleton)} file(s)); "
                               "built mechanically from the check output.", "tasks": skeleton}
            self.run.save("plan.json", plan)
        else:
            plan = self.plan(checks, baseline)
        print_plan(plan)
        if plan_only or not plan["tasks"]:
            path = self.run.dir / "plan.md"
            path.write_text(f"# Plan: {self.request}\n\n{plan['summary']}\n\n" + "\n".join(
                f"## {t['id']} {t['action']} `{t['file']}`\n\n{t['instructions']}\n\nDone when: {t['done_when']}\n"
                for t in plan["tasks"]))
            finish(self, "plan", bool(plan["tasks"]), None, None, path)
            return 0 if plan["tasks"] or plan_only else 1

        base = git(repo, "rev-parse", "HEAD").strip()
        start_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if start_branch == "HEAD":  # detached
            start_branch = base
        branch = f"buckets/{slug(self.request)}-{datetime.now().strftime('%m%d-%H%M')}"
        git(repo, "checkout", "-q", "-b", branch)
        say(f"  working on a new branch: {paint(branch, 'cyan')}")

        try:
            rounds, verdict, now = self.rounds(plan, plan["tasks"], base, checks, baseline)
        except KeyboardInterrupt:
            clear_status()
            say(f"stopped: {leave_branch(repo, branch, start_branch, base)}")
            raise SystemExit(130)
        except Exception as e:
            clear_status()
            say(f"stopped by an error ({type(e).__name__}: {e}): {leave_branch(repo, branch, start_branch, base)}")
            say(f"details are in {self.run.dir}")
            raise

        report = render_report(self, plan, rounds, verdict, checks, baseline, now, branch, base, start_branch)
        path = self.run.dir / "report.md"
        path.write_text(report)
        if not changed_paths(repo):
            git(repo, "checkout", "-q", start_branch)
        print(report)
        finish(self, "code", bool(verdict and verdict["done"]), branch, start_branch, path)
        return 0 if verdict and verdict["done"] else 2


ACTION_NOTES = {
    "create": "\nThis file doesn't exist yet. Create it with your write tool; don't try to open it first.\n",
    "delete": "\nDelete this file.\n",
    "edit": "",
}


def stuck(failing: set, failing_before: set) -> bool:
    """A round made no progress: something still fails, and nothing that failed before was fixed."""
    return bool(failing) and failing >= failing_before


def checks_settle_job(results: list, baseline: dict, now: dict, shortcuts: list, api: dict | None) -> bool:
    """For jobs defined by the checks: every task finished, something was failing at the start,
    everything passes now, and nothing suspicious was added. Then a model review adds nothing."""
    return (bool(results) and all(r["status"] == "done" for r in results)
            and any(not r["ok"] for r in baseline.values())
            and bool(now) and all(r["ok"] for r in now.values())
            and not shortcuts and not (api and api.get("untouched")))


CI_WORDS = re.compile(r"\b(ci|pipeline|github actions|workflow|checks? (?:are|is) (?:red|failing)|red (?:build|checks?)|"
                      r"build (?:is )?(?:red|failing|broken))\b", re.I)


def trim_ci_log(log: str, limit: int = 12000) -> str:
    """`gh run view --log-failed` output without the job/step/timestamp columns, ANSI codes and runner
    noise, keeping the end of each failed step (where the errors are)."""
    steps: dict[str, list[str]] = {}
    for raw in log.splitlines():
        cols = raw.split("\t", 2)
        step = " / ".join(cols[:2]) if len(cols) == 3 else ""
        line = re.sub(r"^\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?", "", cols[-1])
        line = strip_ansi(line).rstrip()
        if not line or line.startswith(("##[group]", "##[endgroup]")):
            continue
        steps.setdefault(step, []).append(line)
    per = max(40, (limit // 120) // max(1, len(steps)))
    out = []
    for step, lines in steps.items():
        out.append(f"--- {step or 'log'} ---")
        out += (["…"] if len(lines) > per else []) + lines[-per:]
    text = "\n".join(out)
    return text[-limit:]


LOCALE_DIRS = ("localization", "locales", "locale", "i18n", "lang", "translations")
BASE_LOCALES = ("en_us.json", "en-US.json", "en.json", "en_US.json", "en-us.json")
UI_STRING = re.compile(r"""\b(?:text|title|placeholder|label|message|header|subtitle)=\{?\s*(?:"([^"{}]{2,120})"|'([^'{}]{2,120})')|"""
                       r"""\btranslate\(\s*(?:"([^"]{2,120})"|'([^']{2,120})')|(?<![=\-])>\s*([A-Z][A-Za-z ,.'!?&:-]{2,100}[a-z.!?])\s*</""")


def locale_files(repo: Path, files) -> tuple[str, list[str]] | None:
    """The base locale file and its siblings, for repos whose strings live in flat JSON maps keyed by
    the source text (`"REMEMBER ME": "Remember me"`)."""
    for f in files:
        parts = f.split("/")
        if len(parts) >= 2 and parts[-2] in LOCALE_DIRS and parts[-1] in BASE_LOCALES:
            folder = "/".join(parts[:-1])
            others = sorted(g for g in files if g.startswith(folder + "/") and g.endswith(".json") and g != f
                            and g.count("/") == f.count("/"))
            return f, others
    return None


def i18n_gaps(repo: Path, diff: str, files) -> list[str]:
    """User-facing strings a change adds with no entry in the base locale, and keys it adds to the base
    locale that the other locales don't have. Only for repos keyed by the uppercased source text."""
    found = locale_files(repo, files)
    if not found:
        return []
    base, others = found
    try:
        keys = json.loads((repo / base).read_text())
    except (OSError, ValueError):
        return []
    real = [k for k in keys if not k.startswith("#")]
    if not real or sum(k == k.upper() for k in real) < 0.9 * len(real):
        return []
    out, file = [], ""
    # Strings on removed lines moved with their code (a move or share); only new ones count.
    gone = " ".join(re.sub(r"\s+", " ", l[1:]) for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else ""
            continue
        if not line.startswith("+") or not file.endswith((".tsx", ".jsx", ".ts", ".js")) or TEST_PATH.search(file):
            continue
        for m in UI_STRING.finditer(line[1:]):
            if m.group(5) and not file.endswith((".tsx", ".jsx")):
                continue
            text = next(g for g in m.groups() if g).strip()
            if text in gone:  # also when a formatter joined `<Text>`, the text and `</Text>` onto one line
                continue
            if " " not in text and not text[:1].isupper() or re.search(r"https?:|[/_]|\.(png|jpg|svg)$", text):
                continue
            gap = f"`{file}` shows \"{text}\" but `{base}` has no \"{text.upper()}\" entry"
            if text.upper() not in keys and gap not in out:
                out.append(gap)
    chunk = next((c for c in diff.split("diff --git ") if c.split("\n", 1)[0].endswith(f" b/{base}")), "")
    key = re.compile(r'"((?:[^"\\]|\\.)+)"\s*:')
    plus = {k for l in chunk.splitlines() if l.startswith("+") and not l.startswith("+++") for k in key.findall(l)}
    minus = {k for l in chunk.splitlines() if l.startswith("-") and not l.startswith("---") for k in key.findall(l)}
    added = plus - minus
    for other in others:
        try:
            theirs = json.loads((repo / other).read_text())
        except (OSError, ValueError):
            continue
        missing = sorted(k for k in added if k not in theirs and not k.startswith("#"))
        if missing:
            out.append(f"`{other}` lacks {len(missing)} key(s) added to `{base}`: " + ", ".join(f'"{k}"' for k in missing[:6]))
    return out[:15]


TEST_PATH = re.compile(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py|[^/]+\.(test|spec)\.[jt]sx?|__tests__/[^/]+\.[jt]sx?)$")
TEST_START = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(|^\s*(?:it|test)(?:\.\w+)?\(\s*['\"`]([^'\"`]+)")
ASSERTION = re.compile(r"\bself\.assert\w+|^\s*assert\b|\bexpect\(|\.assert_\w+\(|\bcall_count\b")
CALL_ASSERTION = re.compile(r"\.assert_(?:called\w*|any_call|not_called|has_calls)\(|\bcall_count\b|"
                            r"\.toHaveBeenCalled\w*\(|\.toBeCalled\w*\(|\.toHaveBeenNthCalledWith\(")
MOCKED = re.compile(r"""patch(?:\.object)?\(\s*(?:[\w.]+\s*,\s*)?['"]([\w.]+)['"]|jest\.spyOn\(\s*\w+\s*,\s*['"](\w+)['"]|"""
                    r"""jest\.mock\(\s*['"]([^'"]+)['"]""")
DEFINED = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)|^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)|"
                     r"^\s*(?:export\s+)?const\s+(\w+)\s*=")


def weak_tests(diff: str) -> list[str]:
    """New tests that can pass whatever the code does: ones whose only assertions check that mocks
    were called, ones without assertions, and ones that mock the very function this change edits.
    Mechanical, so the reviewers get it as evidence rather than a verdict."""
    tests: list[tuple[str, str, list[str]]] = []
    edited: set[str] = set()
    file = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            file = line[6:] if line.startswith("+++ b/") else ""
            continue
        is_test = bool(TEST_PATH.search(file))
        if line.startswith("@@"):
            head = DEFINED.search(line.split("@@")[-1]) if not is_test else None
            if head:
                edited.add(next(g for g in head.groups() if g))
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:]
        if not is_test:
            m = DEFINED.search(text)
            if m:
                edited.add(next(g for g in m.groups() if g))
            continue
        m = TEST_START.search(text)
        if m:
            tests.append((file, m.group(1) or m.group(2), []))
        elif tests and tests[-1][0] == file:
            tests[-1][2].append(text)
    found = []
    for path, name, body in tests:
        asserts = [b for b in body if ASSERTION.search(b)]
        calls = [b for b in asserts if CALL_ASSERTION.search(b)]
        mocked = {next(g for g in m.groups() if g).split(".")[-1] for b in body for m in MOCKED.finditer(b)}
        where = f"`{name}` (`{path}`)"
        if not asserts:
            found.append(f"{where} has no assertions")
        elif len(calls) == len(asserts):
            found.append(f"{where}: all {len(asserts)} assertion(s) only check that mocks were called, not results")
        for n in sorted(mocked & edited):
            found.append(f"{where} mocks `{n}`, which this change edits, so it doesn't run the new code")
    return found


def impact_text(api: dict | None) -> str:
    """API changes for the reviewer: what changed shape, what went, what's new, and the callers left alone."""
    if not api or not any(api.values()):
        return ""
    out = ["", "API changes (from the code index, comparing exports before and after):"]
    for c in api["changed"]:
        out.append(f"- changed `{c['name']}` in `{c['file']}`\n    before: {c['before']}\n    after:  {c['after']}")
    out += [f"- removed {r}" for r in api["removed"]]
    out += [f"- added {a}" for a in api["added"][:20]]
    if api["untouched"]:
        out.append("Callers of changed exports that this work didn't update:")
        out += [f"- {u}" for u in api["untouched"][:30]]
    return "\n".join(out) + "\n"


LINE_NUMBER = re.compile(r"^\s*\d+\| ?")


def plain_code(block: str) -> str:
    """Code as the planner wrote it, minus line numbers copied from the excerpts ("  12| x = 1")."""
    lines = block.split("\n")
    numbered = [l for l in lines if l.strip()]
    if numbered and all(LINE_NUMBER.match(l) for l in numbered):
        return "\n".join(LINE_NUMBER.sub("", l) for l in lines)
    return block


def reindent(code: str, like: str) -> list[str]:
    """New code indented at least as deep as the line it goes next to (a method written flush left)."""
    lines = code.split("\n")
    while lines and not lines[0].strip():  # blank (or whitespace-only) lines at the edges: spacing is ours
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    first = next((l for l in lines if l.strip()), "")
    have, want = len(first) - len(first.lstrip()), len(like) - len(like.lstrip())
    return [(" " * (want - have) + l if l.strip() else l) for l in lines] if want > have else lines


DECL_LINE = re.compile(r"\s*(?:export\s+)?(?:async\s+)?(?:def|class|function)\s+([\w$]+)")
CLOSER = re.compile(r"[\]\)\}][\]\)\};,]*")


def append_into(lines: list[str], span: tuple[int, int], code: str) -> list[str]:
    """New code at the end of a block: before the closing bracket of a dict, list or braced class,
    or after the last line of a Python class; indented like the block's body, a comma added after
    the last entry, a blank line between members."""
    lo, hi = span
    body = [k for k in range(lo + 1, hi) if lines[k].strip()]
    header = next((k for k in range(lo, hi) if not lines[k].lstrip().startswith("@")), lo)
    first = next((k for k in body if k > header), None)
    like = lines[first] if first is not None else " " * 4 + lines[header]
    new = reindent(code, like)
    # a bracket-closed block (dict, list, braced class); a Python class (header ends with ":") never is,
    # even when its last method ends with a lone ")"
    closed = CLOSER.fullmatch(lines[hi - 1].strip()) is not None and not lines[header].rstrip().endswith(":")
    member = re.match(r"\s*(class|def|async def|function|export|public|private|protected|static|\w+\s*\()",
                      new[0] if new else "") is not None
    if closed:
        last = max((k for k in body if k < hi - 1), default=None)
        if last is not None and not member and not lines[last].rstrip().endswith((",", "{", "[", "(")):
            lines[last] = lines[last].rstrip() + ","
        return lines[:hi - 1] + ([""] if member and last is not None else []) + new + lines[hi - 1:]
    return lines[:hi] + [""] + new + lines[hi:]


def apply_edits(text: str, edits: list[dict], decls_for=None) -> str:
    """The planner's edits, in order. "at" is a declaration name (resolved with `decls_for` on the
    text as it is now) or lines copied from the code, matched once ignoring indentation."""
    for i, e in enumerate(edits, 1):
        lines, at, mode = text.split("\n"), plain_code(e["at"]).strip("\n"), e["mode"]
        if "\n" not in at:  # "at" written as the file's path ("src/a.tsx", "src/a.tsx:Box.open"): not a place in it
            at = re.sub(r"^\s*[\w@.\-]+(?:/[\w@.\-]+)+\.\w+(?::|\s+|$)", "", at, count=1)
        name = at.strip().strip("`").removesuffix("()")
        span = None
        if name and "\n" not in name and decls_for and re.fullmatch(r"[\w.$]+", name):
            d = find_decl(decls_for(text), name)
            # the name must really be on those lines (an index made before earlier edits can be stale)
            if d and any(name.split(".")[-1] in l for l in lines[d["line"] - 1:d["end"]]):
                span = (d["line"] - 1, d["end"])
        by_name = span is not None
        if mode == "append":
            if span is None:
                raise ValueError(f"edit {i}: append needs the name of a class, dict or list, not `{at[:60]}`")
            text = "\n".join(append_into(lines, span, plain_code(e["code"])))
            continue
        if span is None and re.fullmatch(r"\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*", at):  # a bare line number or range
            m = re.fullmatch(r"\s*(\d+)(?:\s*[-–]\s*(\d+))?\s*", at)
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            if not 1 <= a <= b <= len(lines):
                raise ValueError(f"edit {i}: lines {a}-{b} aren't in the file ({len(lines)} lines)")
            span = (a - 1, b)
        if span is None:
            by_name = False
            want = [l.strip() for l in at.split("\n")]
            if not any(want):
                raise ValueError(f"edit {i} says where only with an empty \"at\"")
            hits = [k for k in range(len(lines) - len(want) + 1)
                    if [l.strip() for l in lines[k:k + len(want)]] == want]
            # Lines that start with a declaration (def test_op_log(self): …) mean that declaration:
            # "after" it is after its last line, even when only its first line was quoted, and lines
            # "quoted" from memory that don't match still find it. A replace takes the whole
            # declaration only when the quote covered most of it (else it could drop a body).
            head = DECL_LINE.match(next(l for l in at.split("\n") if l.strip()))
            d = find_decl(decls_for(text), head.group(1)) if head and decls_for else None
            if d:
                inside = len(hits) == 1 and d["line"] - 1 <= hits[0] < d["end"]
                whole = len(want) >= 0.8 * (d["end"] - d["line"] + 1)
                if (mode in ("after", "before") and (not hits or inside)) or (mode == "replace" and not hits and whole):
                    span, by_name, hits = (d["line"] - 1, d["end"]), True, [d["line"] - 1]
            if span is None and len(hits) != 1:
                raise ValueError(f"edit {i}: `{at[:60]}` " + (f"appears {len(hits)} times" if hits
                                                              else "isn't a name or lines in the file"))
            if span is None:
                span = (hits[0], hits[0] + len(want))
        lo, hi = span
        new = reindent(plain_code(e["code"]), lines[lo]) if e["code"].strip() else []
        gap = 0  # next to a declaration: the same spacing that follows it (or precedes it, at the end)
        if by_name and new and mode != "replace":
            while gap < 2 and hi + gap < len(lines) and not lines[hi + gap].strip() and hi + gap + 1 < len(lines):
                gap += 1
            while not gap and lo - 1 >= 0 and not lines[lo - 1].strip():
                gap = 1 + (lo - 2 >= 0 and not lines[lo - 2].strip())
            gap = max(gap, 1) if not lines[lo][:1].isspace() else 1  # members: one blank line
        if mode == "replace":
            lines[lo:hi] = new
        elif mode == "after":
            lines[hi:hi] = [""] * gap + new
        else:
            lines[lo:lo] = new + [""] * gap
        text = "\n".join(lines)
    return text


def coder_prompt(request: str, task: dict, focus: str = "") -> str:
    hint = ("\nOpen these first for context: " + ", ".join(f"`{p}`" for p in task["read_first"]) + "\n"
            if task["read_first"] else "")
    hint = ACTION_NOTES[task["action"]] + (f"\n{focus}\n" if focus else "") + hint
    return CODER_PROMPT.format(request=request, read_hint=hint, id=task["id"], action=task["action"],
                               file=task["file"], instructions=task["instructions"],
                               done_when=task["done_when"])


def leave_branch(repo: Path, branch: str, start_branch: str, base: str) -> str:
    """After a run stops early: drop the branch if it holds nothing, else say where the work is."""
    if changed_paths(repo):
        return f"uncommitted changes are left on {branch}; you're still on that branch"
    git(repo, "checkout", "-q", start_branch)
    if git(repo, "rev-parse", branch).strip() == base:
        git(repo, "branch", "-q", "-D", branch)
        return f"nothing was committed, so {branch} was deleted; back on {start_branch}"
    return f"work so far is committed on {branch}; back on {start_branch}"


def print_plan(plan: dict) -> None:
    say(f"  {paint('Plan:', 'bold')} {plan['summary'] or '(no summary)'}")
    for t in plan["tasks"]:
        say(f"    {paint(t['id'], 'cyan')} {t['action']:<6} {t['file']}")


def render_pick_report(b, picks, commits, rounds, verdict, checks, baseline, now, branch, base, start_branch,
                       target, done) -> str:
    lines = [
        f"# buckets: cherry-pick onto {target}",
        "",
        f"- **Result:** {'done' if done else 'not done'}",
        f"- **Repo:** `{b.repo}`",
        f"- **Branch:** `{branch}` (from `{target}` at `{base[:10]}`)",
        f"- **Time:** {int((time.time() - _T0) / 60)} min",
        f"- **Mode:** {b.mode}" + (f" ({b.cfg['claude']['model']}, ${b.cost:.2f})" if b.mode == "claude" else ""),
        f"- **Run folder:** `{b.run.dir}`",
        "",
        "## Commits",
        "",
        "| Commit | Subject | Result | Note |",
        "|---|---|---|---|",
    ]
    seen = {pk["sha"] for pk in picks}
    for pk in picks:
        note = " ".join(pk["note"].split()).replace("|", "\\|")[:160]
        lines.append(f"| `{pk['sha'][:10]}` | {pk['subject'][:70]} | {pk['status']} | {note} |")
    for sha, subj in commits:
        if sha not in seen:
            lines.append(f"| `{sha[:10]}` | {subj[:70]} | not attempted | an earlier commit failed |")
    if verdict:
        lines += ["", "## Review", "", verdict["summary"]]
        for i, results in enumerate(rounds, 1):
            for r in results:
                lines.append(f"- round {i}: {r['id']} {r['action']} `{r['file']}`: {r['status']}")
        if verdict["issues"] and not done:
            lines += ["", "### Open issues", ""] + [f"- {i}" for i in verdict["issues"]]
    lines += ["", "## Checks", "", "| Check | Before | After |", "|---|---|---|"]
    for name in checks:
        before = "pass" if baseline.get(name, {}).get("ok") else "FAIL"
        after = "pass" if now.get(name, {}).get("ok") else "FAIL"
        lines.append(f"| {name} | {before} | {after} |")
    lines += [
        "", "## Next steps", "", "```bash",
        f"git -C {b.repo} log --oneline {base[:10]}..{branch}",
        f"git -C {b.repo} checkout {target} && git -C {b.repo} merge --ff-only {branch}",
        f"git -C {b.repo} branch -D {branch}   # to throw it away",
        "```", "",
    ]
    return "\n".join(lines)


def render_report(b: Buckets, plan, rounds, verdict, checks, baseline, now, branch, base, start_branch) -> str:
    done = bool(verdict and verdict["done"])
    lines = [
        f"# buckets: {b.request.splitlines()[0][:100]}",
        "",
        f"- **Result:** {'done' if done else 'not done'}",
        f"- **Repo:** `{b.repo}`",
        f"- **Branch:** `{branch}` (from `{start_branch}` at `{base[:10]}`)",
        f"- **Rounds:** {len(rounds)} of {b.cfg['max_rounds']}",
        f"- **Time:** {int((time.time() - _T0) / 60)} min",
        f"- **Mode:** {b.mode}" + (f" ({b.cfg['claude']['model']}, ${b.cost:.2f})" if b.mode == "claude" else ""),
        f"- **Run folder:** `{b.run.dir}`",
    ] + ([f"- **Escalations:** " + "; ".join(b.escalations)] if getattr(b, "escalations", None) else []) + [
        "",
        "## Summary",
        "",
        (verdict or {}).get("summary") or plan["summary"],
        "",
        "## Tasks",
        "",
        "| Round | Task | File | Status | Note |",
        "|---|---|---|---|---|",
    ]
    for i, results in enumerate(rounds, 1):
        for r in results:
            note = r["note"].replace("|", "\\|")[:160]
            lines.append(f"| {i} | {r['id']} {r['action']} | `{r['file']}` | {r['status']} | {note} |")
    lines += ["", "## Checks", "", "| Check | Before | After |", "|---|---|---|"]
    for name in checks:
        before = "pass" if baseline.get(name, {}).get("ok") else "FAIL"
        after = "pass" if now.get(name, {}).get("ok") else "FAIL"
        lines.append(f"| {name} | {before} | {after} |")
    if verdict and verdict.get("api") and any(verdict["api"].values()):
        lines += ["", "## API changes", "", impact_text(verdict["api"]).strip().split("\n", 1)[1]]
    if verdict and verdict.get("shortcuts"):
        lines += ["", "## Shortcuts in the diff", ""] + [f"- {h}" for h in verdict["shortcuts"]]
    if verdict and verdict["issues"] and not done:
        lines += ["", "## Open issues", ""] + [f"- {i}" for i in verdict["issues"]]
    lines += [
        "", "## Next steps", "", "```bash",
        f"git -C {b.repo} diff {start_branch}...{branch}",
        f"git -C {b.repo} merge {branch}",
        f"git -C {b.repo} branch -D {branch}   # to throw it away",
        "```", "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- doctor


def machine_label(role: dict) -> str:
    """What a role's machine is called in messages: its "label", else "this machine" for
    localhost, else the host name."""
    if role.get("label"):
        return role["label"]
    host = urllib.parse.urlparse(role.get("url", "")).hostname or ""
    return "this machine" if host in ("localhost", "127.0.0.1", "::1", "") else host


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def planner_on_worker(offload: dict) -> bool:
    """offload.planner: "worker" (or the older "windows") means the thinker plans."""
    return offload.get("planner") in ("worker", "windows")


def unload(model_cfg: dict) -> None:
    """Ask Ollama to drop a model from memory now (it reloads in ~20–30 s next time). A llama.cpp
    server holds one model for as long as it runs, so there's nothing to drop."""
    if model_cfg.get("api") == "llamacpp":
        return
    try:
        body = json.dumps({"model": model_cfg["model"], "keep_alive": 0}).encode()
        req = urllib.request.Request(model_cfg["url"].rstrip("/") + "/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError, ValueError):
        pass


def server_models(url: str) -> set[str] | None:
    """The models a server offers: Ollama's list, or a llama.cpp server's (its --alias); None when
    nothing answers."""
    names = None
    for path, read in (("/api/tags", lambda d: {m["name"] for m in d["models"]}),
                       ("/v1/models", lambda d: {m["id"] for m in d["data"]})):
        try:
            with urllib.request.urlopen(url.rstrip("/") + path, timeout=5) as r:
                names = read(json.load(r))
                break
        except urllib.error.HTTPError:
            continue  # not this kind of server; try the other
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
            return None
    if names is None:
        return None
    return names | {n.removesuffix(":latest") for n in names}


def doctor(cfg: dict, repo: Path | None) -> int:
    all_good = True

    def line(good: bool, text: str):
        nonlocal all_good
        all_good &= good
        print(f"  {ok('ok ') if good else bad('BAD')}  {text}")

    print(f"buckets {VERSION}\n  config: {CONFIG_PATH}\n  runs:   {STATE_DIR / 'runs'}\n")
    t = cfg.get("thinker")
    if t:
        models = server_models(t["url"])
        there = models is not None and t["model"] in models
        wanted = planner_on_worker(cfg.get("offload", {}))
        line(there or not wanted, f"thinker model {t['model']} on {t['url']}"
             + ("" if there else " (not pulled yet; optional)" if not wanted else " (needed: offload.planner is worker)"))
    for role in ("planner", "coder"):
        c = cfg[role]
        models = server_models(c["url"])
        line(models is not None, f"{role} server {c['url']}" + (" (llama.cpp)" if c.get("api") == "llamacpp" else ""))
        if models is not None:
            line(c["model"] in models, f"{role} model {c['model']}")
    cl = shutil.which(cfg["claude"]["bin"])
    line(bool(cl), f"claude CLI at {cl or cfg['claude']['bin']} (only needed for --claude)")
    if cl:
        code, out, _ = run_proc([cl, "auth", "status"], Path.home(), 30)
        try:
            logged_in = bool(json.loads(out).get("loggedIn"))
        except (json.JSONDecodeError, AttributeError):
            logged_in = False
        line(logged_in, "claude CLI logged in" + ("" if logged_in else f": run `{cl} auth login`"))
    oc = shutil.which(cfg["opencode_bin"])
    line(bool(oc), f"opencode at {oc or cfg['opencode_bin']}")
    if oc:
        agent = cfg["coder"]["opencode_agent"]
        for _ in range(3):  # the first call after opencode's service starts can come back empty
            code, out, _ = run_proc([oc, "debug", "agents"], Path.home(), 60)
            if '"id"' in out:
                break
            time.sleep(2)
        line(f'"id": "{agent}"' in out, f"opencode agent {agent}")
        reader = cfg["research"]["opencode_agent"]
        line(f'"id": "{reader}"' in out, f"opencode agent {reader} (for research reading)")
        code, out, _ = run_proc([oc, "models"], Path.home(), 60)
        line(cfg["coder"]["opencode_model"] in out, f"opencode model {cfg['coder']['opencode_model']}")
    if repo:
        rc = {**path_rules(cfg, repo), **repo_config(repo)}
        checks = detect_checks(repo, rc)
        src = (".buckets.json" if repo_config(repo).get("checks") is not None
               else "your config's repos rules" if rc.get("checks") is not None else "detected")
        print(f"\n  repo {repo}\n  checks ({src}):")
        for n, cmd in checks.items():
            print(f"    {n}: {cmd}")
        if not checks:
            print('    none: add {"checks": {"test": "..."}} to .buckets.json')
        fixers = detect_fixers(repo, repo_config(repo))
        print("  auto-fixers: " + (", ".join(f.split()[0].split("/")[-1] for f in fixers) if fixers else "none found"))
        index = CodeIndex(repo, list_files(repo, []))
        has_py = any(f.endswith(".py") for f in index.files)
        langs = [x for x, on in (("TypeScript/JavaScript", index.ts), ("Python", has_py)) if on]
        print(f"  code index: {', '.join(langs) if langs else 'not available (no TypeScript package or Python files)'}"
              + (f"; plus the `{INDEX_CHECK}` check" if index.ts else ""))
    print("\n" + (ok("all good") if all_good else bad("fix the BAD lines above")))
    return 0 if all_good else 1


# ---------------------------------------------------------------- main


def finish(b, kind: str, done: bool | None, branch: str | None, merge_into: str | None, report: Path) -> None:
    """The closing lines: result, time, cost, where the report is, what to do next.
    Also saves what `buckets last`, `buckets merge` and `buckets discard` need."""
    spent = f" · Claude {money(b.cost)}" if b.cost else ""
    print(file=sys.stderr)
    if kind == "info":
        say(f"{ok()} {paint('Answer above', 'bold', 'green')} · {elapsed()}{spent}")
    elif kind == "plan":
        say(f"{ok()} {paint('Plan ready', 'bold', 'green')} (nothing was changed) · {elapsed()}{spent}")
    elif done:
        say(f"{ok()} {paint('Done', 'bold', 'green')} in {elapsed()}{spent} · on branch {paint(branch, 'cyan')}")
    else:
        say(f"{warn()} {paint('Finished, but not done', 'bold', 'yellow')} in {elapsed()}{spent}"
            + (f" · work so far is on {paint(branch, 'cyan')}" if branch else "") + " · see the open issues in the report")
    say(f"  report: {report}")
    if branch:
        say(f"  next: {paint('buckets merge', 'bold')} to bring it into {merge_into}, or {paint('buckets discard', 'bold')}"
            f"; look first with {paint(f'git diff {merge_into}...{branch}', 'dim')}")
        why = review_reasons(b, done, branch, merge_into)
        if why:
            say(f"  {paint('suggestion:', 'bold', 'yellow')} a Claude review is worth it before merging ({why}): "
                f"{paint('buckets review', 'bold')} (about $0.20–0.50; it reads the code around the change too)")
    meta = {"repo": str(b.repo), "kind": kind, "request": b.request, "done": done, "branch": branch,
            "merge_into": merge_into, "report": str(report), "finished": datetime.now().isoformat(timespec="seconds"),
            "minutes": round((time.time() - _T0) / 60, 1), "claude_usd": round(b.cost, 3)}
    (b.run.dir / "meta.json").write_text(json.dumps(meta, indent=2))
    remember(b, kind, done, branch, merge_into, report)


def remember(b, kind: str, done, branch, merge_into, report: Path) -> None:
    """The run in the repo's journal, and a sentence on each file it involved that the map can't
    describe yet. Never fails the run."""
    try:
        mem = b.memory
        if not mem:
            return
        changed = [f for f in git(b.repo, "diff", "--name-only", f"{merge_into}...{branch}", check=False).split()
                   if f] if branch and merge_into else []
        text = report.read_text(errors="replace") if report.exists() else ""
        m = re.search(r"^## Summary\s*\n(.+?)(?=^## |\Z)", text, re.S | re.M)
        summary = m.group(1) if m else re.sub(r"^#.*$", "", text, flags=re.M)[:600]
        outcome = {"info": "answered", "plan": "planned"}.get(kind) or ("done" if done else "not done")
        mem.note(kind, outcome + (f" on {branch}" if branch else ""), b.request, changed, summary)
        mem.refresh(b.index.files, b.index.outline()) if changed else None
        mem.learn(b, changed + [t for t in getattr(b, "involved", []) if t not in changed])
    except (ModelError, OSError, ValueError, RuntimeError) as e:
        detail(f"project memory not updated: {e}")


def review_reasons(b, done, branch: str, merge_into: str) -> str:
    """Why a local run's change deserves a second look by a stronger model, or "": the signs that
    went with the mistakes local planning made on a 2,000-file app (an invented file, a filter slot
    that clashed with another, several files that had to fit together)."""
    if getattr(b, "mode", "") == "claude" or not claude_ready(b.cfg):
        return ""
    added = git(b.repo, "diff", "--name-only", "--diff-filter=A", f"{merge_into}...{branch}", check=False).split()
    files = git(b.repo, "diff", "--name-only", f"{merge_into}...{branch}", check=False).split()
    reasons = []
    if not done:
        reasons.append("it isn't done")
    if added:
        reasons.append(f"it adds {len(added)} new file{'s' if len(added) != 1 else ''}")
    if len(files) >= 3:
        reasons.append(f"{len(files)} files that have to fit together")
    if getattr(b, "rounds_used", 1) > 1 and done:
        reasons.append("it took more than one round")
    return ", ".join(reasons)


def runs_newest_first(repo: Path | None = None, kinds: tuple = ("info", "plan", "code", "pick")):
    metas = sorted((STATE_DIR / "runs").glob("*/meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for m in metas:
        try:
            d = json.loads(m.read_text())
        except json.JSONDecodeError:
            continue
        if d.get("kind") in kinds and (repo is None or d.get("repo") == str(repo)):
            yield d


def branch_exists(repo: Path, branch: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo,
                          capture_output=True).returncode == 0


def cmd_last(repo: Path | None) -> int:
    d = next(runs_newest_first(repo), None) or next(runs_newest_first(None), None)
    if not d:
        die("no finished runs yet")
    print(Path(d["report"]).read_text())
    status_word = {True: ok("done"), False: warn("not done"), None: "answered"}[d.get("done")]
    say(f"{status_word} · {d['kind']} · {d['finished']} · {d['repo']}")
    if d.get("branch") and branch_exists(Path(d["repo"]), d["branch"]):
        say(f"  next: {paint('buckets merge', 'bold')} or {paint('buckets discard', 'bold')}")
    return 0


def last_branch(repo: Path) -> dict:
    """The newest run in this repo whose branch still exists."""
    d = next((d for d in runs_newest_first(repo, ("code", "pick")) if d.get("branch")
              and branch_exists(repo, d["branch"])), None)
    if not d:
        die("no buckets branch to act on in this repo")
    return d


def merge_run(repo: Path) -> str:
    """Fast-forward the latest run's branch into the branch it started from and delete it."""
    d = next((d for d in runs_newest_first(repo, ("code", "pick")) if d.get("branch")
              and branch_exists(repo, d["branch"])), None)
    if not d:
        raise RuntimeError("no buckets branch to merge in this repo")
    branch, into = d["branch"], d["merge_into"]
    if changed_paths(repo):
        raise RuntimeError(dirty_tree(repo))
    note = "" if d.get("done") else " (that run finished but wasn't done; merged anyway because you asked)"
    if git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() != into:
        git(repo, "checkout", "-q", into)
    r = subprocess.run(["git", "merge", "--ff-only", branch], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{into} has moved since the run, so it can't fast-forward; merge it yourself: "
                           f"git merge {branch}")
    git(repo, "branch", "-q", "-d", branch)
    return f"merged {branch} into {into} and deleted the branch (nothing pushed){note}"


CLAUDE_REVIEW_PROMPT = """A smaller local model made the change below for this request. Review it the way a
careful senior engineer on this codebase would. You're in the repository root with read-only tools
(Read, Grep, Glob): read the code around the change, not just the diff. Check that it fits what is
already there: existing helpers and patterns it should have reused, keys, ids, slots or names it
collides with, every place that should have changed (both apps, both variants, translations, types),
and behaviour it changes by accident. The checks (types, lint, tests) already pass or fail on their
own; look for what they can't see.

Reply in markdown: a first line "Verdict: looks good" or "Verdict: needs changes", then each problem
with `path:line`, what's wrong and how to fix it, then anything worth testing by hand. Be concise;
don't restate the diff.

Request:
{request}

The change ({branch} against {base}):
{diff}"""


def cmd_review(cfg: dict, repo: Path) -> int:
    """`buckets review`: Claude reviews the latest run's branch, reading the code around the change."""
    d = next((d for d in runs_newest_first(repo, ("code", "pick")) if d.get("branch")
              and branch_exists(repo, d["branch"])), None)
    if not d:
        die("no buckets branch to review in this repo")
    if not claude_ready(cfg):
        die("Claude isn't set up here (run `claude auth login`)")
    base, branch = d["merge_into"], d["branch"]
    diff = git(repo, "diff", f"{base}...{branch}")
    if not diff.strip():
        die(f"{branch} has no changes against {base}")
    budget = cfg["diff_budget_chars"]
    diff = diff if len(diff) <= budget else diff[:budget] + f"\n… (cut off; {len(diff) - budget} more characters)"
    rc = {**path_rules(cfg, repo), **repo_config(repo)}
    related = [Path(os.path.expanduser(str(p))) for p in (rc.get("related") or {}).values()]
    run = Run(repo)
    say(f"{paint('buckets', 'bold')} {VERSION} · reviewing {paint(branch, 'cyan')} with Claude {cfg['claude']['model']}")
    print(f"Reviewing run: {d['request'][:100]} · branch: {branch}")
    worktree = Path(tempfile.mkdtemp(prefix="buckets-review-")) / "tree"
    git(repo, "worktree", "add", "-q", "--detach", str(worktree), branch)  # read the branch, not your checkout
    try:
        text, cost = claude_call(cfg, run, "review", CLAUDE_REVIEW_PROMPT.format(
            request=d["request"], branch=branch, base=base, diff=diff), worktree,
            add_dirs=[p for p in related if p.is_dir()])
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree), check=False)
    report = run.save("review.md", f"# Claude review of {branch}\n\n{text}\n")
    print(text)
    say(f"{ok()} reviewed · Claude {money(cost)} · {report}")
    return 0


def cmd_merge(repo: Path) -> int:
    try:
        say(f"{ok()} {merge_run(repo)}")
        return 0
    except RuntimeError as e:
        say(f"{warn()} {e}")
        return 1


def cmd_discard(repo: Path, yes: bool) -> int:
    d = last_branch(repo)
    branch, into = d["branch"], d["merge_into"]
    count = git(repo, "rev-list", "--count", f"{into}..{branch}").strip()
    if not yes and not ask_yes(f"Delete {branch} and its {count} commit(s)?"):
        say("kept it")
        return 1
    if git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch:
        if changed_paths(repo):
            die("you're on that branch with uncommitted changes; commit or stash them first")
        git(repo, "checkout", "-q", into)
    git(repo, "branch", "-q", "-D", branch)
    say(f"{ok()} deleted {branch}")
    return 0


# ---------------------------------------------------------------- setup


SAFE_TOOLS = {"read": "allow", "glob": "allow", "grep": "allow", "list": "allow", "lsp": "allow",
              "bash": "deny", "execute": "deny", "webfetch": "deny", "websearch": "deny", "task": "deny",
              "external_directory": "deny", "question": "deny", "skill": "deny", "doom_loop": "deny"}
OPENCODE_AGENTS = {
    "buckets-coder": {
        "description": "Single-task coder driven by the buckets orchestrator. Edits files only; never runs commands.",
        "mode": "primary", "temperature": 0.2, "steps": 40,
        "permission": {**SAFE_TOOLS, "edit": "allow", "todowrite": "allow"}},
    "buckets-reader": {
        "description": "Read-only researcher driven by the buckets orchestrator. Reads and searches; never edits or runs anything.",
        "mode": "primary", "temperature": 0.2, "steps": 30,
        "permission": {**SAFE_TOOLS, "edit": "deny", "todowrite": "deny"}},
}
OPENCODE_CONFIG = Path.home() / ".config/opencode/opencode.json"


def find_claude() -> str | None:
    """The `claude` CLI, also when it's installed under a Node version nvm isn't using right now."""
    found = shutil.which("claude")
    if found:
        return found
    candidates = sorted(Path.home().glob(".nvm/versions/node/*/bin/claude"), reverse=True)
    return str(candidates[0]) if candidates else None


def install_opencode(cfg: dict, path: Path | None = None) -> dict:
    """Add (or update) what buckets needs in opencode's config: a provider per Ollama server with the
    models it uses, and the two locked-down agents. Everything else in the file is left alone.
    Returns {url: provider id} and sets each role's opencode_model."""
    path = path or OPENCODE_CONFIG  # looked up at call time, so tests (and callers) can redirect it
    oc = json.loads(path.read_text()) if path.exists() else {"$schema": "https://opencode.ai/config.json"}
    if path.exists():
        shutil.copy(path, path.with_suffix(".json.bak"))
    providers = oc.setdefault("provider", {})
    ids: dict[str, str] = {}
    for role in ("thinker", "coder", "reader", "planner"):
        url = cfg[role]["url"].rstrip("/")
        if url not in ids:
            existing = next((pid for pid, pv in providers.items()
                             if (pv.get("options") or {}).get("baseURL", "").rstrip("/") == url + "/v1"), None)
            pid = existing or "buckets_" + slug(machine_label(cfg[role]), 20).replace("-", "_")
            providers.setdefault(pid, {"npm": "@ai-sdk/openai-compatible", "name": f"Ollama ({machine_label(cfg[role])})",
                                       "options": {"baseURL": url + "/v1", "headerTimeout": 600000}, "models": {}})
            ids[url] = pid
        model = cfg[role]["model"]
        providers[ids[url]].setdefault("models", {}).setdefault(model, {
            "name": f"{model} ({machine_label(cfg[role])})", "tool_call": True,
            "limit": {"context": cfg[role].get("num_ctx", 65536), "output": 16384}})
    for role in ("thinker", "coder"):
        cfg[role]["opencode_model"] = f"{ids[cfg[role]['url'].rstrip('/')]}/{cfg[role]['model']}"
    agents = oc.setdefault("agent", {})
    for name, spec in OPENCODE_AGENTS.items():
        agents[name] = {**agents.get(name, {}), **spec, "model": cfg["coder"]["opencode_model"]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(oc, indent=2) + "\n")
    return ids


def cmd_setup(yes: bool) -> int:
    """Describe your machines and models; writes ~/.config/buckets/config.json and the opencode bits."""
    cfg = load_config()
    asking = interactive() and not yes

    def ask(question: str, default: str) -> str:
        if not asking:
            return default
        answer = input(f"  {question} [{default}]: ").strip()
        return answer or default

    print(paint("buckets setup", "bold") + " · Enter keeps the [default]\n", file=sys.stderr)
    print("  The worker is the machine that does most of the work (planning, coding). It can be this\n"
          "  machine (http://localhost:11434) or another one on your network running Ollama.\n", file=sys.stderr)
    worker_url = ask("Worker's Ollama URL", cfg["thinker"]["url"]).rstrip("/")
    there = server_models(worker_url)
    if there is None:
        say(f"  {warn()} can't reach {worker_url} right now; continuing, but check Ollama is running there")
    else:
        say(f"  models there: {', '.join(sorted(m for m in there if ':' in m)) or '(none)'}")
    same = cfg["thinker"]["url"].rstrip("/") == worker_url
    worker_label = ask("A name for the worker in messages",
                       machine_label(cfg["thinker"] if same else {"url": worker_url}))
    thinker = ask("Model that plans and reviews (a reasoning model)", cfg["thinker"]["model"])
    coder = ask("Model that writes the code", thinker if cfg["coder"]["model"] == cfg["thinker"]["model"] else cfg["coder"]["model"])
    print("\n  The advisor is an optional stronger (usually slower) model for very hard jobs and for runs\n"
          "  that stop making progress. Type 'none' to skip it.\n", file=sys.stderr)
    advisor_url = ask("Advisor's Ollama URL", cfg["planner"]["url"]).rstrip("/")
    for role, model, url, label in (("thinker", thinker, worker_url, worker_label),
                                    ("coder", coder, worker_url, worker_label),
                                    ("reader", thinker, worker_url, worker_label)):
        cfg[role].update({"url": url, "model": model, "label": label})
    if advisor_url.lower() == "none":
        cfg["planner"].update({k: cfg["thinker"][k] for k in ("url", "model", "label")})
        cfg["offload"].update({"escalate_very_hard": False, "escalate_when_stuck": False})
    else:
        advisor = ask("Advisor model", cfg["planner"]["model"])
        cfg["planner"].update({"url": advisor_url, "model": advisor,
                               "label": ask("A name for the advisor's machine", machine_label(
                                   cfg["planner"] if cfg["planner"]["url"].rstrip("/") == advisor_url else {"url": advisor_url}))})
    cfg["offload"]["planner"] = "worker"
    claude = find_claude()
    if claude:
        cfg["claude"]["bin"] = claude
    ids = install_opencode(cfg)
    keep = {k: cfg[k] for k in ("planner", "thinker", "coder", "reader", "offload", "claude", "repos")}
    old = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if CONFIG_PATH.exists():
        shutil.copy(CONFIG_PATH, CONFIG_PATH.with_suffix(".json.bak"))
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({**old, **keep}, indent=2) + "\n")
    say(f"{ok()} wrote {CONFIG_PATH} and updated {OPENCODE_CONFIG} (providers: {', '.join(sorted(set(ids.values())))})")
    for role in ("thinker", "coder", "planner"):
        models = server_models(cfg[role]["url"])
        if models is not None and cfg[role]["model"] not in models:
            say(f"  {warn()} {cfg[role]['model']} isn't on {machine_label(cfg[role])} yet: run "
                f"{paint('ollama pull ' + cfg[role]['model'], 'bold')} there")
    print(file=sys.stderr)
    return doctor(cfg, None)


# ---------------------------------------------------------------- git and GitHub assistant


GIT_OPS = {
    # op: (what it does, changes something locally, reaches outside this machine)
    "branch": ("create a branch and switch to it: {name, from?}", True, False),
    "switch": ("switch to a branch: {name}", True, False),
    "commit": ("commit the current changes: {message, paths?}", True, False),
    "squash": ("squash the current branch's commits since `onto` into one: {message, onto?}", True, False),
    "rebase": ("rebase the current branch onto `onto` (stops cleanly on conflicts): {onto?}", True, False),
    "sync": ("fetch and fast-forward a branch to its upstream: {branch?}", True, False),
    "push": ("push a branch and set its upstream: {branch?}", True, True),
    "pr_create": ("open a pull request: {title, body, base?, head?, draft?}", True, True),
    "pr_from_run": ("open a PR from the latest buckets run, squashed onto the remote base: {base?, draft?}", True, True),
    "pr_list": ("list pull requests: {state?}", False, False),
    "pr_status": ("a PR's state, reviews and CI: {number?}", False, False),
    "pr_failures": ("failing CI jobs of a PR with the end of their logs: {number?}", False, False),
    "pr_comment": ("comment on a PR: {number, body}", True, True),
    "ci_rerun": ("re-run a PR's failed CI jobs (for flaky or infrastructure failures): {number?}", True, True),
    "pr_update": ("change a PR's title or body: {number, title?, body?}", True, True),
    "pr_merge": ("merge a PR: {number, method: squash|merge|rebase}", True, True),
    "branch_delete": ("delete a branch: {name, remote?, force?}", True, False),
    "prune_merged": ("delete local branches already merged into a base: {base?}", True, False),
    "run_merge": ("bring the latest buckets run's branch into the branch it started from (fast-forward) and "
                  "delete it, like `buckets merge`: {}", True, False),
    "init": ("make this folder a git repository: {branch?}", True, False),
    "gitignore": ("add entries to .gitignore: {entries: [..]}", True, False),
    "repo_create": ("create the GitHub repository for this folder under the folder's GitHub account and push it "
                    "(needs a commit first): {name?: defaults to the folder name, visibility?: private|public "
                    "(default private), description?}", True, True),
    "status": ("the working tree: changed and untracked files, the branch and how far ahead or behind its upstream it is: {}", False, False),
    "log": ("show the last {count} commits (default 10): {count?}", False, False),
    "branches": ("list local branches with upstream and ahead/behind counts: {}", False, False),
    "remotes": ("list configured remotes and their URLs: {}", False, False),
    "tags": ("list the newest tags with their dates: {count?}", False, False),
}

GIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "ops": {"type": "array", "items": {
            "type": "object",
            "properties": {"op": {"type": "string", "enum": list(GIT_OPS)}, "args": {"type": "object"}},
            "required": ["op", "args"]}},
    },
    "required": ["summary", "ops"],
}

GIT_PROMPT = """Turn this request into git/GitHub steps. Use only these operations, in order:

{ops}

Every operation's result is shown to the user, so questions ("what's the CI status?", "why
does CI fail?", "show me the logs") are answered by running the matching read-only operation
(pr_status, pr_failures, pr_list). Fill names from the repository state below (branches, PR
numbers). Don't ask questions: use sensible defaults for anything the request leaves out (repo_create's
name is the folder name; the GitHub account is picked from the folder automatically). If the
request can't be done with these operations, return no ops and say why in "summary". For commit
messages and PR titles use the repo's style (see recent commits), e.g. "feat(core): …".

Request:
{request}

Repository state:
{state}"""

PR_TEXT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "body": {"type": "string"}, "branch": {"type": "string"}},
    "required": ["title", "body", "branch"],
}

PR_TEXT_PROMPT = """Write a pull request for this change: a title in the repo's commit style (see recent
commits), a short markdown body (## Summary with 2-4 bullets, then ## Tests saying which checks
pass), and a branch name like "feat/short-name" or "fix/short-name".

The request that produced it:
{request}

Recent commits:
{log}

Changed files:
{stat}

Diff (start):
{diff}"""


class GitAssistant:
    """Plain-language git and GitHub chores: the model picks typed operations, buckets runs them
    with git and gh (using the folder's GitHub account), asking once before anything that changes
    or publishes."""

    def __init__(self, b):
        self.b, self.repo, self.cfg = b, b.repo, b.cfg
        self.remote = self.cfg.get("git", {}).get("remote", "origin")
        env = {k: os.path.expanduser(str(v)) for k, v in (b.rc.get("env") or {}).items()}
        self.env = {**os.environ, **env}

    # -- running git and gh with the folder's account

    def run(self, *cmd: str, check: bool = True, input: str | None = None) -> str:
        r = subprocess.run(list(cmd), cwd=self.repo, env=self.env, capture_output=True, text=True,
                           errors="replace", input=input)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])}…: {(r.stderr or r.stdout).strip()[:400]}")
        return r.stdout

    def git(self, *args: str, check: bool = True, input: str | None = None) -> str:
        return self.run("git", *args, check=check, input=input)

    def gh(self, *args: str, check: bool = True) -> str:
        return self.run(self.cfg.get("gh_bin", "gh"), *args, check=check)

    def current(self) -> str:
        # symbolic-ref also works on a branch with no commits yet; a detached HEAD falls back to "HEAD"
        name = self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
        return name or self.git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def has_commits(self) -> bool:
        return bool(self.git("rev-parse", "--verify", "--quiet", "HEAD", check=False).strip())

    def account(self) -> str:
        """The GitHub login gh uses for this folder, or "" when gh is missing or logged out."""
        if not hasattr(self, "_login"):
            try:
                self._login = self.gh("api", "user", "--jq", ".login", check=False).strip()
            except OSError:
                self._login = ""
        return self._login

    def base(self) -> str:
        """The remote's default branch (main, master, develop…)."""
        ref = self.git("symbolic-ref", "--quiet", f"refs/remotes/{self.remote}/HEAD", check=False).strip()
        if ref:
            return ref.rsplit("/", 1)[-1]
        for name in ("main", "master", "develop"):
            if self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False).strip():
                return name
        return self.current()

    def pr_number(self, args: dict) -> str:
        if args.get("number"):
            return str(args["number"]).lstrip("#")
        out = self.gh("pr", "view", "--json", "number", check=False)
        try:
            return str(json.loads(out)["number"])
        except (json.JSONDecodeError, KeyError):
            raise RuntimeError("no PR number given and the current branch has no PR")

    # -- what the model sees

    def is_repo(self) -> bool:
        return subprocess.run(["git", "rev-parse", "--git-dir"], cwd=self.repo, capture_output=True).returncode == 0

    def state(self) -> str:
        login = self.account()
        github = (f"GitHub account for this folder: {login}" if login
                  else "GitHub: gh isn't available or not logged in for this folder")
        if not self.is_repo():
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in self.repo.iterdir())
            return (f"This folder ({self.repo.name}) is NOT a git repository yet; start with `init`.\n"
                    f"Its top level: {', '.join(entries[:40])}\n{github}")
        log = self.git("log", "--oneline", "-10", check=False).strip()
        parts = [f"Current branch: {self.current()} · default branch: {self.base()} · remote: {self.remote}",
                 "Status:\n" + "\n".join(self.git("status", "-sb").splitlines()[:15]),
                 "Local branches (name | upstream | ahead/behind | last commit):\n" + "\n".join(
                     self.git("for-each-ref", "--sort=-committerdate", "refs/heads", "--count=30",
                              "--format=%(refname:short) | %(upstream:short) | %(upstream:track) | "
                              "%(committerdate:relative) | %(subject)").splitlines()),
                 "Recent commits:\n" + (log or "(none yet: commit before repo_create)"), github]
        if not self.git("remote", "get-url", self.remote, check=False).strip():
            parts.append(f"Open PRs: (no {self.remote} remote yet; repo_create adds it)")
            return "\n\n".join(parts)
        prs = self.gh("pr", "list", "--json", "number,title,headRefName,baseRefName,isDraft", "--limit", "20",
                      check=False)
        try:
            listed = json.loads(prs)
            parts.append("Open PRs:\n" + ("\n".join(f"#{p['number']} {p['title']} ({p['headRefName']} → "
                                                     f"{p['baseRefName']}{', draft' if p['isDraft'] else ''})"
                                                     for p in listed) or "(none)"))
        except json.JSONDecodeError:
            parts.append("Open PRs: (gh isn't available or not logged in for this folder)")
        recent = ProjectMemory(self.repo).recent(budget=400) if (self.repo / MEMORY_DIR).is_dir() else ""
        if recent:
            parts.append("Recent buckets work here (\"it\" or \"that\" may refer to it):\n" + recent)
        run = next(runs_newest_first(self.repo, ("code", "pick")), None)
        if run and run.get("branch") and branch_exists(self.repo, run["branch"]):
            parts.append(f"Latest buckets run: {run['request'][:120]} · branch {run['branch']} · "
                         f"{'done' if run.get('done') else 'not done'}")
        return "\n\n".join(parts)

    # -- the flow

    def handle(self, request: str, yes: bool) -> int:
        b = self.b
        b.heading(2, "Working out the git steps")
        earlier = pending_git(self.repo)
        if earlier and earlier.get("ops") and not earlier.get("ran") and is_affirmation(request):
            # "yes go ahead" right after a plan that wasn't answered in time: that's the answer.
            request, data, yes = earlier["request"], validate_git_ops(earlier), True
            say(f"  going ahead with the steps for: {request.splitlines()[0][:100]}")
        else:
            if earlier:  # "name it buckets" after a request that didn't run: plan both together
                request = f"{earlier['request']}\nFollow-up: {request}"
                detail(f"following up on: {earlier['request'][:100]}")
            ops_help = "\n".join(f"- {name}: {what}" for name, (what, _, _) in GIT_OPS.items())
            try:
                state = self.state()
            except (RuntimeError, OSError) as e:
                say(f"  {bad()} couldn't read the repository: {str(e)[:400]}")
                return 2
            data = ask_json(b.chores, b.run, "git", SYSTEM,
                            GIT_PROMPT.format(ops=ops_help, request=request, state=state),
                            GIT_SCHEMA, validate_git_ops, think=False, max_tokens=2000, timeout=b.timeout)
            data["ops"] = self.fill_in(data["ops"])
        return self.carry_out(request, data, yes)

    def target(self, step: dict) -> str:
        """Where a publishing step lands, spelled out so the account and name can be checked."""
        if step["op"] != "repo_create":
            return ""
        login = self.account() or "?"
        name = step["args"].get("name") or self.repo.name
        return paint(f"  → github.com/{login}/{name} ({step['args'].get('visibility') or 'private'})", "cyan")

    def fill_in(self, ops: list) -> list:
        """Steps the model tends to leave out: `init` first in a plain folder, and a first commit before
        publishing a repository that has none."""
        names = [s["op"] for s in ops]
        if not self.is_repo() and "init" not in names and any(GIT_OPS[n][1] for n in names):
            ops.insert(0, {"op": "init", "args": {}})
        no_commits = not self.has_commits()
        if "repo_create" in names and no_commits:
            at = next(i for i, s in enumerate(ops) if s["op"] == "repo_create")
            if not any(s["op"] == "commit" for s in ops[:at]):
                ops.insert(at, {"op": "commit", "args": {"message": "chore: initial commit"}})
        # Drop paths from every commit step when the repo has no commits yet
        if no_commits:
            for step in ops:
                if step["op"] == "commit" and "paths" in step.get("args", {}):
                    del step["args"]["paths"]
        return ops

    def record(self, request: str, data: dict, ran: bool, done: bool) -> None:
        """Remember the request so a follow-up ("name it X", "yes go ahead") can build on it."""
        meta = {"repo": str(self.repo), "kind": "git", "request": request, "summary": data["summary"],
                "ops": data["ops"], "ran": ran, "done": done, "finished": datetime.now().isoformat(timespec="seconds")}
        (self.b.run.dir / "meta.json").write_text(json.dumps(meta, indent=2))

    def carry_out(self, request: str, data: dict, yes: bool) -> int:
        ops = data["ops"]
        if not ops:
            say(f"  {warn()} {data['summary']}")
            self.record(request, data, ran=False, done=False)
            return 1
        say(f"  {paint(data['summary'], 'bold')}")
        for i, step in enumerate(ops, 1):
            _, changes, outward = GIT_OPS[step["op"]]
            mark = paint("⇡ ", "magenta") if outward else ("• " if changes else "  ")
            say(f"    {mark}{i}. {describe_op(step)}" + self.target(step))
        changing = [s for s in ops if GIT_OPS[s["op"]][1]]
        if changing and not yes:
            outward = any(GIT_OPS[s["op"]][2] for s in ops)
            later = f'to run these steps, answer with buckets "go ahead" within {PENDING_GIT_MINUTES} minutes'
            if not interactive():
                say(f"  {warn()} not running steps that change things without -y; {later}")
                self.record(request, data, ran=False, done=False)
                return 2
            answer = ask_line("Go ahead?" + (" (⇡ = pushes or publishes to GitHub)" if outward else "")
                              + " [y/N, or say what to change]", timeout=300)
            if answer and not is_affirmation(answer) and answer.lower() not in NEGATIONS:
                # "call it buckets-cli", "make it public": plan again with that as a follow-up
                self.record(request, data, ran=False, done=False)
                return self.handle(answer, yes)
            if not answer or answer.lower() in NEGATIONS:
                say(f"  nothing done; {later}")
                self.record(request, data, ran=False, done=False)
                return 1
        b = self.b
        b.heading(3, "Doing it")
        for i, step in enumerate(ops, 1):
            try:
                result = getattr(self, f"op_{step['op']}")(step["args"], yes)
            except (RuntimeError, OSError) as e:
                say(f"  {bad()} {i}. {describe_op(step)}: {str(e)[:400]}")
                say("  stopped here; the steps before it are done")
                self.record(request, data, ran=True, done=False)
                return 2
            say(f"  {ok()} {i}. {describe_op(step)}" + (f"\n{indent(result)}" if result else ""))
        self.record(request, data, ran=True, done=True)
        if changing and (self.repo / MEMORY_DIR).is_dir():
            ProjectMemory(self.repo).note("git", "done", request.splitlines()[0], [],
                                          "; ".join(describe_op(s) for s in ops)[:400])
        return 0

    # -- operations (each returns a short report, or "")

    def op_branch(self, a, yes):
        self.git("switch", "-c", a["name"], *([a["from"]] if a.get("from") else []))
        return ""
    def op_branches(self, a, yes):
        return self.git("branch", "-vv")

    def op_remotes(self, a, yes):
        return self.git("remote", "-v")

    def op_tags(self, a, yes):
        count = a.get("count", 10)
        fmt = "%(refname:short) %(creatordate:short)"
        result = self.git("tag", "--sort=-creatordate", f"--format={fmt}")
        if result:
            lines = result.splitlines()
            return '\n'.join(lines[:count])
        return result

    def op_switch(self, a, yes):
        self.git("switch", a["name"])
        return ""

    def op_commit(self, a, yes):
        paths = a.get("paths") or []
        self.git("add", "-A", "--", *paths) if paths else self.git("add", "-A")
        if not self.git("diff", "--cached", "--name-only").strip():
            raise RuntimeError("nothing to commit")
        self.git("commit", "-q", "-m", self.message(a["message"]))
        return self.git("log", "--oneline", "-1").strip()

    def op_squash(self, a, yes):
        if changed_paths(self.repo):
            raise RuntimeError("commit or stash your changes first")
        onto = a.get("onto") or f"{self.remote}/{self.base()}"
        self.git("fetch", "-q", self.remote, check=False)
        start = self.git("merge-base", "HEAD", onto).strip()
        count = self.git("rev-list", "--count", f"{start}..HEAD").strip()
        if count in ("0", "1"):
            return f"nothing to squash ({count} commit since {onto})"
        self.git("reset", "--soft", start)
        self.git("commit", "-q", "-m", self.message(a["message"]))
        return f"{count} commits → {self.git('log', '--oneline', '-1').strip()}"

    def op_rebase(self, a, yes):
        if changed_paths(self.repo):
            raise RuntimeError("commit or stash your changes first")
        self.git("fetch", "-q", self.remote, check=False)
        onto = a.get("onto") or f"{self.remote}/{self.base()}"
        r = subprocess.run(["git", "rebase", onto], cwd=self.repo, env=self.env, capture_output=True, text=True)
        if r.returncode != 0:
            conflicted = self.git("diff", "--name-only", "--diff-filter=U", check=False).split()
            self.git("rebase", "--abort", check=False)
            raise RuntimeError(f"conflicts in {', '.join(conflicted) or '?'}; rebase aborted, nothing changed")
        return f"onto {onto}"

    def op_sync(self, a, yes):
        branch = a.get("branch") or self.current()
        self.git("fetch", "-q", self.remote)
        upstream = self.git("rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}", check=False).strip() \
            or f"{self.remote}/{branch}"
        if branch == self.current():
            self.git("merge", "--ff-only", upstream)
        else:
            self.git("fetch", self.remote, f"{branch}:{branch}")
        return f"{branch} = {upstream}"

    def op_push(self, a, yes):
        branch = a.get("branch") or self.current()
        self.git("push", "-q", "-u", self.remote, branch)
        return ""

    def op_status(self, a, yes):
        return self.git("status", "-sb")

    def op_log(self, a, yes):
        count = a.get("count", 10)
        return self.git("log", "--oneline", "-n", str(count))

    def op_pr_create(self, a, yes):
        head = a.get("head") or self.current()
        args = ["pr", "create", "--base", a.get("base") or self.base(), "--head", head,
                "--title", a["title"], "--body", a.get("body") or ""]
        return self.gh(*args, *(["--draft"] if a.get("draft") else [])).strip()

    def op_pr_list(self, a, yes):
        out = self.gh("pr", "list", "--state", a.get("state") or "open", "--json",
                      "number,title,headRefName,isDraft,url", "--limit", "30")
        prs = json.loads(out)
        return "\n".join(f"#{p['number']} {p['title']} ({p['headRefName']}{', draft' if p['isDraft'] else ''}) {p['url']}"
                         for p in prs) or "(none)"

    def op_pr_status(self, a, yes):
        n = self.pr_number(a)
        d = json.loads(self.gh("pr", "view", n, "--json",
                               "number,title,state,isDraft,mergeable,reviewDecision,statusCheckRollup,url"))
        checks = d.get("statusCheckRollup") or []
        state = lambda c: (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        failing = [c.get("name") or c.get("context") for c in checks if state(c) in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT")]
        passing = sum(state(c) in ("SUCCESS", "NEUTRAL", "SKIPPED") for c in checks)
        pending = len(checks) - passing - len(failing)
        return (f"#{d['number']} {d['title']} · {d['state'].lower()}{' (draft)' if d.get('isDraft') else ''} · "
                f"review: {(d.get('reviewDecision') or 'none').lower()} · mergeable: {(d.get('mergeable') or '?').lower()}\n"
                f"CI: {passing} passed, {len(failing)} failed, {pending} pending"
                + (f"\nfailing: {', '.join(failing)}" if failing else "") + f"\n{d['url']}")

    def op_pr_failures(self, a, yes):
        n = self.pr_number(a)
        checks = json.loads(self.gh("pr", "checks", n, "--json", "name,state,link", check=False) or "[]")
        failed = [c for c in checks if c.get("state", "").upper() in ("FAILURE", "ERROR")]
        if not failed:
            return "no failing checks"
        out, files, logs = [], list_files(self.repo, []), {}
        for c in failed[:6]:
            m = re.search(r"/runs/(\d+)/job/(\d+)", c.get("link", ""))
            if m and m.group(1) not in logs:
                logs[m.group(1)] = self.gh("run", "view", m.group(1), "--log-failed", check=False)
            job = [l for l in (logs.get(m.group(1), "") if m else "").splitlines() if l.startswith(c["name"] + "\t")]
            parsed = diagnostics_text("\n".join(job), self.repo, files)
            out.append(f"--- {c['name']}\n" + (parsed + "\n" if parsed else "") + error_excerpt(job))
        return "\n\n".join(out)

    def op_ci_rerun(self, a, yes):
        n = self.pr_number(a)
        checks = json.loads(self.gh("pr", "checks", n, "--json", "name,state,link", check=False) or "[]")
        runs = sorted({m.group(1) for c in checks if c.get("state", "").upper() in ("FAILURE", "ERROR", "CANCELLED")
                       for m in [re.search(r"/runs/(\d+)", c.get("link", ""))] if m})
        if not runs:
            return f"#{n} has no failed jobs"
        for run in runs:
            self.gh("run", "rerun", run, "--failed")
        return f"#{n}: re-running the failed jobs of run(s) {', '.join(runs)}"

    def op_pr_comment(self, a, yes):
        self.gh("pr", "comment", self.pr_number(a), "--body", a["body"])
        return ""

    def op_pr_update(self, a, yes):
        args = [x for k in ("title", "body") if a.get(k) for x in (f"--{k}", a[k])]
        self.gh("pr", "edit", self.pr_number(a), *args)
        return ""

    def op_pr_merge(self, a, yes):
        method = a.get("method") or "squash"
        if method not in ("squash", "merge", "rebase"):
            raise RuntimeError(f"unknown merge method {method!r}")
        self.gh("pr", "merge", self.pr_number(a), f"--{method}")
        return ""

    def op_branch_delete(self, a, yes):
        name = a["name"]
        if name in (self.base(), "main", "master", "develop") or name == self.current():
            raise RuntimeError(f"won't delete {name} (the base or current branch)")
        r = subprocess.run(["git", "branch", "-d", name], cwd=self.repo, capture_output=True, text=True)
        if r.returncode != 0:
            if not (a.get("force") and yes):
                raise RuntimeError(f"{name} isn't merged; ask with force and -y to delete it anyway")
            self.git("branch", "-D", name)
        if a.get("remote"):
            self.git("push", "-q", self.remote, "--delete", name)
        return ""

    def op_run_merge(self, a, yes):
        return merge_run(self.repo)

    def op_init(self, a, yes):
        if self.is_repo():
            return "already a git repository"
        if self.repo.resolve() in (Path.home().resolve(), Path("/")):
            raise RuntimeError(f"won't make {self.repo} a git repository; cd into the project folder first")
        self.git("init", "-q", "-b", a.get("branch") or "main")
        return f"{self.repo} is a git repository (branch {a.get('branch') or 'main'})"

    def op_gitignore(self, a, yes):
        path = self.repo / ".gitignore"
        have = path.read_text().splitlines() if path.exists() else []
        new = list(dict.fromkeys(e.strip() for e in a.get("entries") or [] if e.strip() and e.strip() not in have))
        if new:
            path.write_text("\n".join(have + new).strip() + "\n")
        return ("added " + ", ".join(new)) if new else "nothing new"

    def op_repo_create(self, a, yes):
        visibility = a.get("visibility") or "private"
        if visibility not in ("private", "public"):
            raise RuntimeError(f"visibility must be private or public, not {visibility!r}")
        if not self.git("rev-parse", "--verify", "--quiet", "HEAD", check=False).strip():
            raise RuntimeError("commit something first (a repository needs at least one commit to push)")
        args = ["repo", "create", a.get("name") or self.repo.name, f"--{visibility}", "--source", ".",
                "--remote", self.remote, "--push"]
        if a.get("description"):
            args += ["--description", a["description"]]
        return self.gh(*args).strip()

    def op_prune_merged(self, a, yes):
        base = a.get("base") or self.base()
        keep = {base, "main", "master", "develop", self.current()}
        merged = [l.strip().lstrip("* ") for l in self.git("branch", "--merged", base).splitlines()]
        gone = [m for m in merged if m and m not in keep]
        for name in gone:
            self.git("branch", "-d", name)
        return ("deleted " + ", ".join(gone)) if gone else "nothing merged to delete"

    def op_pr_from_run(self, a, yes):
        b = self.b
        d = last_branch(self.repo)
        run_branch, into = d["branch"], a.get("base") or d["merge_into"]
        if changed_paths(self.repo):
            raise RuntimeError("commit or stash your changes first")
        self.git("fetch", "-q", self.remote, check=False)
        remote_base = f"{self.remote}/{into}"
        if not self.git("rev-parse", "--verify", "--quiet", remote_base, check=False).strip():
            raise RuntimeError(f"{remote_base} doesn't exist; push {into} first")
        start = self.git("merge-base", into, run_branch).strip()
        diff = self.git("diff", "--binary", f"{start}..{run_branch}")
        text = self.pr_text(d["request"], f"{start}..{run_branch}", diff)
        name = unique_branch(self.repo, slug_branch(text["branch"]))
        back = self.current()
        self.git("switch", "-q", "-c", name, remote_base)
        r = subprocess.run(["git", "apply", "--3way", "--index"], cwd=self.repo, input=diff, text=True,
                           capture_output=True)
        if r.returncode != 0:
            self.git("reset", "-q", "--hard", remote_base)
            self.git("switch", "-q", back)
            self.git("branch", "-q", "-D", name)
            ahead = self.git("rev-list", "--count", f"{remote_base}..{into}", check=False).strip()
            raise RuntimeError(f"the run's changes don't apply on {remote_base} alone (your local {into} has "
                               f"{ahead} unpushed commit(s) they build on); push {into} first, or open a PR "
                               f"for those too")
        self.git("commit", "-q", "-m", self.message(f"{text['title']}\n\n{text['body']}"))
        checks = detect_checks(self.repo, b.rc)
        results = run_checks(self.repo, checks, b.run, "pr", self.cfg["check_timeout_s"])
        failing = [n for n, res in results.items() if not res["ok"]]
        if failing:
            self.git("switch", "-q", back)
            raise RuntimeError(f"checks fail on the clean branch {name} ({', '.join(failing)}); not pushed")
        self.git("push", "-q", "-u", self.remote, name)
        body = text["body"] + "\n\nChecks: " + ", ".join(f"{n} ✓" for n in results) if results else text["body"]
        url = self.gh("pr", "create", "--base", into, "--head", name, "--title", text["title"], "--body", body,
                      *(["--draft"] if a.get("draft") else [])).strip()
        self.git("switch", "-q", back)
        return f"{name} → {url}"

    # -- helpers

    def message(self, text: str) -> str:
        trailer = self.cfg.get("git", {}).get("commit_trailer") or ""
        return text + (f"\n\n{trailer}" if trailer else "")

    def pr_text(self, request: str, rng: str, diff: str) -> dict:
        try:
            return ask_json(self.b.chores, self.b.run, "pr-text", SYSTEM, PR_TEXT_PROMPT.format(
                request=request, log=self.git("log", "--oneline", "-8").strip(),
                stat=self.git("diff", "--stat", rng).strip(), diff=diff[:12000]),
                PR_TEXT_SCHEMA, lambda d: {k: str(d[k]).strip() for k in ("title", "body", "branch")},
                think=False, max_tokens=1500, timeout=self.b.timeout)
        except ModelError:
            first = request.strip().splitlines()[0][:72]
            return {"title": first, "body": f"## Summary\n{request.strip()}", "branch": "buckets/" + slug(first)}


def error_excerpt(job_lines: list[str], limit: int = 12) -> str:
    """The lines of a CI job log that say what went wrong: GitHub's ##[error] lines and lines with
    error/failure words, each with the line before it; without timestamps and job/step prefixes."""
    clean = [re.sub(r"^[^\t]*\t[^\t]*\t(\ufeff)?\S+Z ", "", strip_ansi(l)) for l in job_lines]
    hits = [i for i, l in enumerate(clean)
            if "##[error]" in l or re.search(r"\b(error|failed|failure|fail|exception)\b", l, re.I)]
    keep: list[int] = []
    for i in hits:
        for j in (i - 1, i):
            if 0 <= j < len(clean) and j not in keep:
                keep.append(j)
    shown = [clean[i][:200] for i in sorted(keep)][-limit:]
    return "\n".join(shown) if shown else "\n".join(c[:200] for c in clean[-8:])


PENDING_GIT_MINUTES = 15
NEGATIONS = {"n", "no", "nope", "stop", "cancel", "abort"}
AFFIRMATIONS = {"y", "yes", "yep", "yeah", "ok", "okay", "sure", "go", "ahead", "do", "it", "proceed", "please",
                "run", "them", "confirm", "confirmed", "sounds", "good", "fine", "lgtm"}


def is_affirmation(text: str) -> bool:
    """A bare "yes go ahead" (and nothing more: "yes but make it private" is a new request)."""
    words = re.findall(r"[a-z]+", text.lower())
    return bool(words) and len(words) <= 6 and all(w in AFFIRMATIONS for w in words)


def pending_git(repo: Path) -> dict | None:
    """This folder's latest git request if it didn't finish in the last few minutes (no steps, not
    confirmed, or a step failed), so a follow-up can build on it."""
    d = next(runs_newest_first(repo, ("git",)), None)
    if not d or d.get("done"):
        return None
    try:
        age = datetime.now() - datetime.fromisoformat(d["finished"])
    except (KeyError, ValueError):
        return None
    return d if age.total_seconds() < PENDING_GIT_MINUTES * 60 else None


def validate_git_ops(d: dict) -> dict:
    ops = []
    for step in d.get("ops") or []:
        if step.get("op") not in GIT_OPS:
            raise ValueError(f"unknown operation {step.get('op')!r}")
        ops.append({"op": step["op"], "args": dict(step.get("args") or {})})
    return {"summary": str(d.get("summary", "")), "ops": ops}


def describe_op(step: dict) -> str:
    args = ", ".join(f"{k}={str(v)[:60]!r}" for k, v in step["args"].items() if k != "body")
    return f"{step['op']}({args})"


def indent(text: str, by: str = "      ") -> str:
    return "\n".join(by + l for l in text.splitlines())


def slug_branch(name: str) -> str:
    parts = [slug(p, 40) for p in name.strip().split("/") if p.strip()]
    return "/".join(p for p in parts if p) or "buckets/change"


def unique_branch(repo: Path, name: str) -> str:
    candidate, n = name, 2
    while branch_exists(repo, candidate):
        candidate, n = f"{name}-{n}", n + 1
    return candidate


def claude_ready(cfg: dict) -> bool:
    cl = shutil.which(cfg["claude"]["bin"])
    if not cl:
        return False
    _, out, _ = run_proc([cl, "auth", "status"], Path.home(), 30)
    try:
        return bool(json.loads(out).get("loggedIn"))
    except (json.JSONDecodeError, AttributeError):
        return False


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def ask_line(question: str, timeout: int = 30) -> str | None:
    """A line typed on the terminal; None when there's no terminal or no answer in time."""
    if not interactive():
        return None
    import select
    clear_status()
    print(f"{paint(question, 'bold')} ", end="", file=sys.stderr, flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        print(f"(no answer in {timeout // 60} min)" if timeout >= 120 else f"(no answer in {timeout}s)",
              file=sys.stderr)
        return None
    return sys.stdin.readline().strip()


def ask_yes(question: str, timeout: int = 30) -> bool:
    """y/N on the terminal; no terminal or no answer in time means no."""
    return (ask_line(f"{question} [y/N]", timeout) or "").lower() in ("y", "yes")


def countdown(text: str, seconds: int) -> None:
    """Show what's about to happen and give a moment to Ctrl-C."""
    if seconds <= 0 or not interactive():
        say(text)
        return
    for left in range(seconds, 0, -1):
        status(f"{text} · starting in {left}s (Ctrl-C to stop)")
        time.sleep(1)
    say(text)


def choose_mode(cfg: dict, difficulty: str) -> tuple[str, str]:
    """Auto mode: light jobs stay local; heavier ones go to Claude, Opus only when allowed."""
    if difficulty == "light":
        return "local", ""
    if not claude_ready(cfg):
        return "local", "(Claude isn't set up, so staying local; run `claude auth login` to use it)"
    if difficulty == "very_hard":
        policy = cfg.get("opus_policy", "ask")
        if policy == "allow" or (policy == "ask" and ask_yes("This looks very hard. Use Claude Opus (costs more)?")):
            cfg["claude"]["model"] = "opus"
    return "claude", ""


def git_toplevel(path: str) -> Path | None:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True,
                             text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return None
    return Path(top)


def find_repo(path: str) -> Path:
    top = git_toplevel(path)
    if top is None:
        die(f"{Path(path).resolve()} is not inside a git repository; cd into one or pass -C <repo>")
    return top


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="buckets", description=__doc__.split("\n\n")[0])
    ap.add_argument("request", nargs="*", help="what you want, in plain words")
    ap.add_argument("-f", "--file", help="read the request from a file")
    ap.add_argument("-C", "--repo", default=".", help="repository (default: current directory)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--ask", action="store_true", help="info mode: report, change nothing")
    mode.add_argument("--do", action="store_true", help="code mode: plan, code, check, review")
    mode.add_argument("--doctor", action="store_true", help="check servers, models and repo checks")
    mode.add_argument("--pick", nargs="+", metavar="COMMIT", help="cherry-pick commits or ranges (a..b)")
    ap.add_argument("--onto", help="with --pick: the branch to pick onto (default: the current branch)")
    ap.add_argument("-y", "--yes", action="store_true", help="start without the 5-second pause")
    ap.add_argument("-v", "--verbose", action="store_true", help="also show token counts and timings")
    ap.add_argument("--reading", choices=["auto", "direct", "research"],
                    help="local mode: read picked files directly, or have the planner ask the helper model "
                         "fetch questions in rounds (auto: research for heavy jobs)")
    ap.add_argument("--plan-only", action="store_true", help="code mode: stop after printing the plan")
    ap.add_argument("--rounds", type=int, help="max plan/code/review rounds (default from config)")
    ap.add_argument("--no-think", action="store_true", help="planner skips thinking (faster, weaker)")
    ap.add_argument("--planner-model", help="use another Ollama model as the planner for this run")
    where = ap.add_mutually_exclusive_group()
    where.add_argument("--claude", action="store_true",
                       help="Claude plans and reviews; the local coder still writes the code")
    where.add_argument("--local", action="store_true", help="everything local and free")
    ap.add_argument("--model", choices=["haiku", "sonnet", "opus"],
                    help="Claude model for --claude (default from config: sonnet)")
    ap.add_argument("--plan-file", metavar="JSON",
                    help="run a ready-made plan (e.g. written by Claude): {\"summary\": …, \"tasks\": [{\"file\", "
                         "\"action\", \"instructions\", \"done_when\"}]}; skips reading and planning")
    ap.add_argument("--like", action="append", metavar="REF",
                    help="an earlier change to copy: a commit, a range (a..b) or a file; repeatable")
    ap.add_argument("--version", action="version", version=f"buckets {VERSION}")
    args = ap.parse_args(argv)

    cfg = load_config()
    if args.rounds:
        cfg["max_rounds"] = args.rounds
    if args.no_think:
        cfg["planner"]["think"] = False
    if args.model:
        cfg["claude"]["model"] = args.model
    cfg["mode_override"] = "claude" if (args.claude or args.model) else "local" if args.local else None
    if args.planner_model:
        cfg["planner"]["model"] = args.planner_model

    if args.doctor:
        return doctor(cfg, git_toplevel(args.repo))

    global VERBOSE, _LOG
    VERBOSE = args.verbose
    words = [w.lower() for w in args.request]
    if words == ["setup"] and not args.file:
        return cmd_setup(args.yes)
    if words == ["pr"] and not args.file:
        args.request = ["open a pull request for the latest buckets run"]
    if words == ["review"] and not args.file:
        here = git_toplevel(args.repo)
        if here is None:
            die("run this inside the repo")
        return cmd_review(cfg, here)
    if words in (["last"], ["merge"], ["discard"]) and not args.file:
        here = git_toplevel(args.repo)
        if words == ["last"]:
            return cmd_last(here)
        if here is None:
            die("run this inside the repo")
        return cmd_merge(here) if words == ["merge"] else cmd_discard(here, args.yes)

    request = Path(args.file).read_text() if args.file else " ".join(args.request)
    if not request.strip() and args.plan_file:
        try:
            request = json.loads(Path(args.plan_file).read_text()).get("summary") or f"plan from {args.plan_file}"
        except (OSError, ValueError):
            request = f"plan from {args.plan_file}"
    if not request.strip() and args.pick:
        request = f"cherry-pick {' '.join(args.pick)} onto {args.onto or 'the current branch'}"
    if not request.strip():
        if interactive():
            return chat_loop(sys.argv[1:] if argv is None else list(argv))
        ap.error("say what you want, e.g. buckets \"explain how auth works\"")
    repo = git_toplevel(args.repo)
    if repo is None:
        # Not a git repository yet: only git/GitHub chores make sense here ("make this a git repo…").
        repo = Path(args.repo).resolve()
        if not repo.is_dir():
            die(f"{repo} isn't a folder")
        if repo in (Path.home().resolve(), Path("/")):
            die(f"{repo} is your {'home folder' if repo != Path('/') else 'root folder'}, not a project; "
                f"cd into the project first (e.g. cd ~/Coding/ME/my-project)")
        b = Buckets(cfg, repo, request.strip(), like=args.like)
        _LOG = b.run.dir / "run.log"
        say(f"{paint('buckets', 'bold')} {VERSION} · {repo.name} (not a git repository yet) · "
            f"{paint(request.strip()[:100], 'bold')}")
        try:
            # A follow-up to an unfinished git request here ("name it buckets", "yes go ahead") is git too.
            if not pending_git(repo) and b.route()["intent"] != "git":
                die(f"{repo} isn't a git repository; ask to make it one first "
                    f"(e.g. buckets \"make this a git repo\"), or cd into a repo")
            b.total = 3
            b.heading(1, "Understanding your request")
            say("  git/GitHub chores → local (free), with your confirmation before anything changes")
            return GitAssistant(b).handle(b.request, args.yes)
        except ModelError as e:
            die(str(e))
        except KeyboardInterrupt:
            clear_status()
            print("\nstopped", file=sys.stderr)
            return 130
    b = Buckets(cfg, repo, request.strip(), like=args.like)
    _LOG = b.run.dir / "run.log"
    say(f"{paint('buckets', 'bold')} {VERSION} · {repo.name} · {paint(request.strip()[:100], 'bold')}")
    detail(f"run folder: {b.run.dir}")
    code = 1
    try:
        code = run_request(b, cfg, args, repo)
        return code
    finally:
        if cfg.get("offload", {}).get("unload_mac"):
            unload(cfg["planner"])
        if time.time() - _T0 > 60 and cfg.get("notify", True):
            notify("buckets", f"{'Done' if code == 0 else 'Finished, not done' if code == 2 else 'Stopped'}"
                              f" · {repo.name} · {elapsed()}")


CHAT_COMMANDS = {"merge": "merge", "last": "last", "discard": "discard", "pr": "pr", "setup": "setup", "review": "review",
                 "doctor": "--doctor", "--doctor": "--doctor"}


def chat_loop(argv: list[str]) -> int:
    """`buckets` on its own: ask, answer, ask again, in this folder. Each line is one request (the same
    as `buckets "<line>"` with the same options), so follow-ups like "name it buckets" or "yes go
    ahead" build on the request before them."""
    global _T0
    try:
        import readline  # noqa: F401  (arrow keys and history for input())
    except ImportError:
        pass
    here = Path(git_toplevel(".") or Path.cwd())
    where = here.name + ("" if git_toplevel(".") else " (not a git repository yet)")
    say(f"{paint('buckets', 'bold')} {VERSION} · {where} · what do you need? "
        f"{paint('(exit or Ctrl-D to leave, Ctrl-C stops the current request)', 'dim')}")
    while True:
        try:
            line = input("> ").strip()  # no colors: readline would miscount the prompt's width
        except EOFError:
            print(file=sys.stderr)
            return 0
        except KeyboardInterrupt:
            print(file=sys.stderr)
            continue
        if not line:
            continue
        if line.lower() in ("exit", "quit", "bye", "q", ":q"):
            return 0
        # Typed like the shell command (buckets merge, buckets "fix x"): the same thing here.
        line = re.sub(r"^buckets\s+", "", line).strip()
        if len(line) > 1 and line[0] == line[-1] and line[0] in "\"'":
            line = line[1:-1].strip()
        words = [line.lower()] if line.lower() in CHAT_COMMANDS else ["--", line]
        _T0 = time.time()
        try:
            main(argv + [CHAT_COMMANDS.get(line.lower(), w) for w in words])
        except SystemExit:  # die() ends the request, not the chat
            pass
        except KeyboardInterrupt:
            clear_status()
            print("\nstopped", file=sys.stderr)
        print(file=sys.stderr)


def run_request(b, cfg: dict, args, repo: Path) -> int:
    try:
        intent = ("info" if args.ask else "pick" if args.pick
                  else "code" if (args.do or args.plan_only or args.plan_file) else None)
        if args.plan_file:
            try:
                b.plan_file = validate_plan({"summary": "", **json.loads(Path(args.plan_file).read_text())},
                                            repo, b.cfg["max_tasks"] * 2)
            except (OSError, ValueError, KeyError, TypeError) as e:
                die(f"--plan-file {args.plan_file}: {e}")
        if intent is None and is_affirmation(b.request) and pending_git(repo):
            intent = "git"
        route = b.route() if intent is None or b.mode == "auto" else None
        intent = intent or route["intent"]
        if intent == "git":
            b.total = 3
            b.heading(1, "Understanding your request")
            say("  git/GitHub chores → local (free), with your confirmation before anything changes")
            return GitAssistant(b).handle(b.request, args.yes)
        plan_only = args.plan_only or bool(route and route["plan_only"] and intent == "code")
        if route:
            b.like += [e for e in valid_examples(repo, route["examples"]) if e not in b.like]
        if route:
            b.difficulty = route["difficulty"]
            b.checks_define_done = route["checks_define_done"] and intent == "code"
        if args.reading:
            cfg["reading"] = args.reading
        why = ""
        if b.mode == "auto":
            b.mode, why = choose_mode(cfg, route["difficulty"])
        offload = cfg.get("offload", {})
        if b.mode != "claude" and not args.planner_model:
            # Planning and review need a reasoning model: the Windows thinker, with the Mac's 27B as the
            # advisor for very hard jobs and when Windows is down. The coder model planned and reviewed
            # badly even on light jobs (field notes, run C), so it never plans.
            wants_windows = planner_on_worker(offload) or (b.difficulty == "light" and offload.get("light_jobs"))
            very_hard = b.difficulty == "very_hard" and offload.get("escalate_very_hard") and b.mac_up
            if b.thinker_up and wants_windows and not very_hard:
                b.plan_on_windows(thinker=True)
            elif very_hard and wants_windows:
                why = f"(very hard, so the advisor on {b.home} plans)"
        refs = args.pick or (route["commits"] if route else [])
        onto = args.onto or (route["onto"] if route else "")

        b.total = {"info": 2 if b.mode == "claude" else 3, "pick": 4}.get(intent, 3 if plan_only else 5)
        b.heading(1, "Understanding your request")
        kind = {"info": "question", "code": "code change", "pick": "cherry-pick"}[intent]
        head = kind
        if route:
            head += f" · {route['difficulty'].replace('_', ' ')} ({route['reason'].strip().rstrip('.')[:90]})"
        where = (f"Claude {cfg['claude']['model']}" if b.mode == "claude"
                 else f"local (free), all on {b.worker} ({b.planner['model']})" if getattr(b, "on_windows", False)
                 else "local (free)")
        parts = [head + " → " + where + (f" {why}" if why else "")]
        if intent == "pick":
            parts.append(f"commits: {' '.join(refs) or '?'}" + (f" onto {onto}" if onto else ""))
        if b.like:
            parts.append("example: " + ", ".join(b.like))
        if plan_only:
            parts.append("plan only")
        if getattr(b, "checks_define_done", False):
            parts.append("done when the checks pass")
        if b.mode != "claude" and intent != "pick":
            parts.append(f"reading: {b.reading()}")
        countdown("  " + " · ".join(parts), 0 if args.yes else 5)
        if (b.mode != "claude" and intent == "code" and route and route["difficulty"] != "light"
                and claude_ready(cfg)):
            say(f"  {paint('suggestion:', 'bold', 'yellow')} {route['difficulty'].replace('_', ' ')} changes went much "
                f"better with Claude planning in tests (local planning struggled with changes that must fit existing "
                f"patterns across files); add {paint('--claude', 'bold')} to use it (about $0.50–1.50)")

        if intent == "info":
            b.answer()
            return 0
        if intent == "pick":
            if not refs:
                die('say which commits to cherry-pick, e.g. buckets "cherry-pick a1b2c3d onto release/1.3"')
            return b.pick(refs, onto)
        return b.code(plan_only=plan_only)
    except ModelError as e:
        die(str(e))
    except KeyboardInterrupt:
        clear_status()
        print("\nstopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
