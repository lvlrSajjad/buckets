#!/usr/bin/env python3
"""buckets' regression suite: replays real requests and checks what they produced, so a change to buckets is
measured against the cases that already worked instead of hoped about.

Each case is a JSON file: a repo, the commit the case was verified on, the request (and any follow-ups), and
what the result must look like (files, text, line counts, which files may change, commands that must pass, or
what an answer must name). A case runs on a clone of its repo that the suite owns, never on the repo itself:
the repo is only read (cloned and fetched), and the suite stops if anything in it changed while a case ran.

    python3 regress.py --list
    python3 regress.py                       # every case, once
    python3 regress.py split merge-a -n 3    # these cases, three times each (pass rates)
    python3 regress.py --summary             # the latest suite against the one before

Cases come from regress/ (examples on this repo) and docs/private/regress/ (git-ignored: real work-repo
requests stay out of this public repo), or --cases DIR. A folder's _defaults.json fills in what its cases
leave out (the repo, the commit, the checks every case must pass). Results append to
~/.local/state/buckets-regress/results.jsonl, with each run's log next to it."""

from __future__ import annotations

import argparse
import fnmatch
import glob as globlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("BUCKETS_REGRESS_STATE", Path.home() / ".local/state/buckets-regress"))
CASE_DIRS = [HERE / "regress", HERE / "docs/private/regress"]
CONFIG = Path(os.environ.get("BUCKETS_CONFIG", Path.home() / ".config/buckets/config.json"))
MAIN_STATE = Path(os.environ.get("BUCKETS_STATE", Path.home() / ".local/state/buckets"))


class RepoChanged(Exception):
    """The case's own repo changed while the case ran: the suite stops."""


def git(repo: Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and p.returncode:
        raise RuntimeError(f"git {' '.join(args)} in {repo}: {p.stderr.strip()}")
    return p.stdout


def say(text: str = "") -> None:
    print(text, flush=True)


# ---------------------------------------------------------------- cases


def load_cases(dirs: list[Path]) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for d in dirs:
        base = json.loads((d / "_defaults.json").read_text()) if (d / "_defaults.json").exists() else {}
        for f in sorted(d.glob("[!_]*.json")) if d.is_dir() else []:
            c = {**base, **json.loads(f.read_text())}
            c["expect"] = {**base.get("expect", {}), **c.get("expect", {})}
            c.setdefault("name", f.stem)
            c["_file"] = str(f)
            problems = case_problems(c)
            if problems:
                raise SystemExit(f"{f}: {'; '.join(problems)}")
            cases[c["name"]] = c
    return cases


def case_problems(c: dict) -> list[str]:
    out = [f"missing {k}" for k in ("repo", "commit", "request") if not c.get(k)]
    known = {"done", "max_minutes", "exists", "absent", "matches", "max_lines", "changed_within", "commands", "answer"}
    out += [f"unknown expectation {k}" for k in c.get("expect", {}) if k not in known]
    return out


def repo_of(c: dict) -> Path:
    return Path(os.path.expanduser(c["repo"].replace("{buckets}", str(HERE)))).resolve()


# ---------------------------------------------------------------- the repo stays as found


def snapshot(repo: Path) -> dict:
    """What a case must never change in its repo: where HEAD is, every branch, the stash list, the work tree."""
    return {"head": git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() + " " + git(repo, "rev-parse", "HEAD").strip(),
            "branches": git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
            "stashes": git(repo, "stash", "list", check=False),
            "status": git(repo, "status", "--porcelain")}


def differences(before: dict, after: dict) -> list[str]:
    return [k for k in before if before[k] != after[k]]


# ---------------------------------------------------------------- the clone the suite owns


def slug(path: Path) -> str:
    return hashlib.sha1(str(path).encode()).hexdigest()[:8]


def prepare_clone(c: dict, src: Path) -> Path:
    """A clone of the case's repo at the case's commit, with nothing left from earlier cases. Kept between
    suites so its map is built once; named like the repo so run folders read the same."""
    clone = STATE / "clones" / f"{src.name}-{slug(src)}" / src.name
    if not (clone / ".git").exists():
        clone.parent.mkdir(parents=True, exist_ok=True)
        git(clone.parent, "clone", "-q", "--no-checkout", str(src), str(clone))
    else:
        git(clone, "fetch", "-q", "origin")
    for key in ("user.name", "user.email"):  # commits in the clone carry the repo's identity
        value = git(src, "config", key, check=False).strip()
        if value:
            git(clone, "config", key, value)
    base = c.get("branch", "main")
    reset_clone(clone, base, c["commit"])
    exclude = clone / ".git/info/exclude"
    exclude.parent.mkdir(exist_ok=True)
    listed = exclude.read_text().splitlines() if exclude.exists() else []
    for rel in c.get("links", []):  # big ignored folders (node_modules) come from the repo, not copies
        if f"/{rel}" not in listed:  # a symlink isn't a folder to git, so "node_modules/" doesn't ignore it
            exclude.write_text("\n".join(listed := listed + [f"/{rel}"]) + "\n")
        link, target = clone / rel, src / rel
        if not link.exists() and not link.is_symlink() and target.exists():
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
    return clone


def reset_clone(clone: Path, base: str, commit: str) -> None:
    for wt in re.findall(r"^worktree (.+)$", git(clone, "worktree", "list", "--porcelain"), re.M)[1:]:
        git(clone, "worktree", "remove", "--force", wt, check=False)
    git(clone, "reset", "-q", "--hard")
    git(clone, "checkout", "-q", "-B", base, commit)
    for b in git(clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/buckets").split():
        git(clone, "branch", "-q", "-D", b)
    git(clone, "clean", "-qfd")  # untracked files only; ignored ones (the links) stay
    git(clone, "stash", "clear")


def config_for(clone: Path, src: Path) -> Path:
    """The owner's config with the source repo's rules (checks, memory, related repos) also applied to the clone."""
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    rule: dict = {}
    for pattern, r in (cfg.get("repos") or {}).items():
        if fnmatch.fnmatch(str(src), os.path.expanduser(pattern)):
            rule.update(r)
    rule["memory"] = "outside"  # the clone gets no new files, so "clean after the run" means something
    cfg.setdefault("repos", {})[str(clone)] = rule
    path = STATE / "config.json"
    path.write_text(json.dumps(cfg, indent=2))
    return path


def memory_dir(state: Path, repo: Path) -> Path:
    return state / "memory" / re.sub(r"[^\w.-]+", "_", str(repo.resolve()).strip("/"))


def seed_memory(src: Path, clone: Path) -> None:
    """The repo's map from the owner's state (when it lives outside the repo), so the clone doesn't build it
    again. The journal stays behind: a case shouldn't know the owner's runs."""
    have, want = memory_dir(MAIN_STATE, src), memory_dir(STATE / "state", clone)
    if not (have / "map.json").exists():
        have = src / ".buckets"
    for name in ("map.json", "map.md"):
        if (have / name).exists() and not (want / name).exists():
            want.mkdir(parents=True, exist_ok=True)
            shutil.copy2(have / name, want / name)


# ---------------------------------------------------------------- running buckets


def run_buckets(c: dict, clone: Path, config: Path, request: str, log: Path) -> dict:
    """One request, the way the owner runs it unattended. The time limit stops buckets cleanly; the suite kills
    it only five minutes past that."""
    limit = float(c.get("time_limit", 20))
    cmd = list(c.get("buckets") or [sys.executable, str(HERE / "buckets.py")])
    cmd += c.get("args", ["--local", "-y"]) + ["--time-limit", f"{limit:g}", request]
    env = {**os.environ, "BUCKETS_CONFIG": str(config), "BUCKETS_STATE": str(STATE / "state"),
           "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"}
    started = time.time()
    with log.open("a") as out:
        out.write(f"\n$ buckets … {request!r}\n")
        out.flush()
        p = subprocess.Popen(cmd, cwd=clone, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = p.wait(timeout=limit * 60 + 300)
        except KeyboardInterrupt:  # the suite's Ctrl-C reaches buckets too, so it stops cleanly
            os.killpg(p.pid, signal.SIGINT)
            p.wait(timeout=120)
            raise
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGINT)
            try:
                code = p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                code = p.wait()
    text = log.read_text(errors="replace")
    reports = re.findall(r"^\s*report: (\S+)$", text, re.M)
    meta: dict = {}
    if reports and (Path(reports[-1]).parent / "meta.json").exists():
        meta = json.loads((Path(reports[-1]).parent / "meta.json").read_text())
        if meta.get("request") != request:  # a report from an earlier request in this log
            meta = {}
    return {"request": request, "exit": code, "minutes": round((time.time() - started) / 60, 1),
            "done": meta.get("done"), "branch": meta.get("branch"), "report": meta.get("report"),
            "stopped": not meta}


# ---------------------------------------------------------------- what the result must look like


def lines_in(path: Path) -> int:
    return len(path.read_text(errors="replace").splitlines())


def files_for(root: Path, pattern: str) -> list[Path]:
    return sorted(Path(p) for p in globlib.glob(str(root / pattern), recursive=True) if Path(p).is_file())


def verify(c: dict, clone: Path, steps: list[dict]) -> list[dict]:
    """Every expectation as {check, ok, detail}. Code cases are checked on the last request's branch, checked out
    in the clone; answers on the last report."""
    exp, out = c.get("expect", {}), []

    def check(name: str, ok: bool, detail: str = "") -> None:
        out.append({"check": name, "ok": bool(ok), "detail": detail})

    last = steps[-1]
    if "done" in exp:
        check("done", last["done"] == exp["done"], f"buckets said {last['done']}" + (" (stopped)" if last["stopped"] else ""))
    if "answer" in exp:
        a = exp["answer"]
        text = Path(last["report"]).read_text(errors="replace") if last.get("report") else ""
        found = [s for s in a.get("contains", []) if re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text)]
        need = a.get("min", len(a.get("contains", [])))
        missing = [s for s in a.get("contains", []) if s not in found]
        check("answer", len(found) >= need, f"names {len(found)} of {len(a.get('contains', []))}"
              + (f"; missing {', '.join(missing[:8])}" if missing else ""))
    code_checks = [k for k in exp if k not in ("done", "answer", "max_minutes") and exp[k]]
    if not code_checks:
        return out
    branch = last.get("branch")
    if not branch or not git(clone, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip():
        check("branch", False, "no branch to check")
        return out
    base = c.get("branch", "main")
    git(clone, "reset", "-q", "--hard")
    git(clone, "checkout", "-q", branch)
    try:
        changed = [f for f in git(clone, "diff", "--name-only", f"{base}...{branch}").split() if f]
        for rel in exp.get("exists", []):
            check(f"exists {rel}", (clone / rel).exists())
        for rel in exp.get("absent", []):
            check(f"absent {rel}", not (clone / rel).exists())
        for m in exp.get("matches", []):
            files = files_for(clone, m["file"])
            n = sum(len(re.findall(m["regex"], f.read_text(errors="replace"), re.M)) for f in files)
            lo, hi = m.get("min", 1), m.get("max")
            ok = n >= lo and (hi is None or n <= hi)
            check(f"{m['file']} ~ /{m['regex']}/", ok, f"{n} match(es), want {lo}" + ("+" if hi is None else f"..{hi}"))
        for rel, most in exp.get("max_lines", {}).items():
            n = lines_in(clone / rel) if (clone / rel).exists() else None
            check(f"lines {rel}", n is not None and n <= most, f"{n} line(s), want at most {most}")
        if "changed_within" in exp:
            stray = [f for f in changed if not any(fnmatch.fnmatch(f, g) for g in exp["changed_within"])]
            check("changed only where expected", not stray, ", ".join(stray[:6]) if stray else f"{len(changed)} file(s)")
        for cmd in exp.get("commands", []):
            started = time.time()
            p = subprocess.run(cmd, shell=True, cwd=clone, capture_output=True, text=True, timeout=1800)
            tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
            check(f"$ {cmd}", p.returncode == 0, f"exit {p.returncode} in {time.time() - started:.0f}s"
                  + (": " + " / ".join(tail) if p.returncode else ""))
    finally:
        git(clone, "reset", "-q", "--hard")
        git(clone, "checkout", "-q", base)
    return out


# ---------------------------------------------------------------- one case, the suite


def buckets_version() -> str:
    head = git(HERE, "rev-parse", "--short", "HEAD", check=False).strip() or "?"
    dirty = git(HERE, "status", "--porcelain", "--", "buckets.py", "ts_moves.cjs", "ts_index.cjs", check=False).strip()
    return head + ("+dirty" if dirty else "")


def run_case(c: dict, suite: str, rep: int, logs: Path) -> dict:
    src = repo_of(c)
    before = snapshot(src)
    clone = prepare_clone(c, src)
    seed_memory(src, clone)
    config = config_for(clone, src)
    log = logs / f"{c['name']}-{rep}.log"
    steps = []
    for request in [c["request"], *c.get("follow_ups", [])]:
        steps.append(run_buckets(c, clone, config, request, log))
        if steps[-1]["stopped"]:
            break
    leftovers = git(clone, "status", "--porcelain").strip()
    if steps[-1]["stopped"]:
        checks = [{"check": "finished", "ok": False, "detail": f"stopped (exit {steps[-1]['exit']})"}]
    else:
        checks = verify(c, clone, steps)
    if leftovers:
        checks.append({"check": "clean after the run", "ok": False, "detail": leftovers.splitlines()[0]})
    minutes = round(sum(s["minutes"] for s in steps), 1)
    slow = c.get("expect", {}).get("max_minutes")
    reset_clone(clone, c.get("branch", "main"), c["commit"])
    changed = differences(before, snapshot(src))
    result = {"suite": suite, "case": c["name"], "rep": rep, "ok": all(x["ok"] for x in checks) and bool(checks),
              "minutes": minutes, "slow": bool(slow and minutes > slow), "steps": steps, "checks": checks,
              "buckets": buckets_version(), "log": str(log), "finished": datetime.now().isoformat(timespec="seconds")}
    if changed:
        result["ok"] = False
        result["checks"].append({"check": "repo as found", "ok": False, "detail": f"changed: {', '.join(changed)}"})
        record(result)
        raise RepoChanged(f"{src} changed during {c['name']} ({', '.join(changed)}); stopping the suite")
    return result


def record(result: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "results.jsonl").open("a") as f:
        f.write(json.dumps(result) + "\n")


def results() -> list[dict]:
    path = STATE / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def median(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[len(xs) // 2] if len(xs) % 2 else (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2


def summary(rows: list[dict], suite: str | None = None) -> str:
    """The suite's pass rate and median time per case, beside the previous suite that ran the same case."""
    suites = list(dict.fromkeys(r["suite"] for r in rows))
    if not suites:
        return "no results yet"
    suite = suite or suites[-1]
    now = [r for r in rows if r["suite"] == suite]
    earlier = [s for s in suites if s < suite]
    out = [f"suite {suite} · buckets {now[0]['buckets']}", "",
           "| Case | Passed | Median time | Before | Failing checks |", "|---|---|---|---|---|"]
    for case in dict.fromkeys(r["case"] for r in now):
        mine = [r for r in now if r["case"] == case]
        prev_suite = next((s for s in reversed(earlier) if any(r["suite"] == s and r["case"] == case for r in rows)), None)
        prev = [r for r in rows if r["suite"] == prev_suite and r["case"] == case] if prev_suite else []
        passed = sum(r["ok"] for r in mine)
        before = f"{sum(r['ok'] for r in prev)}/{len(prev)}, {median([r['minutes'] for r in prev]):.1f} min" if prev else "–"
        failing = sorted({x["check"] for r in mine for x in r["checks"] if not x["ok"]})
        slow = " (slow)" if any(r.get("slow") for r in mine) else ""
        out.append(f"| {case} | {passed}/{len(mine)} | {median([r['minutes'] for r in mine]):.1f} min{slow} | {before} | "
                   f"{'; '.join(failing)[:120] or '–'} |")
    total = sum(r["ok"] for r in now)
    out += ["", f"{total}/{len(now)} passed"]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="cases to run (default: all); a name or a glob")
    ap.add_argument("-n", "--repeat", type=int, default=1, help="runs per case, for pass rates")
    ap.add_argument("--cases", action="append", type=Path, help="a folder of case files (instead of the defaults)")
    ap.add_argument("--list", action="store_true", help="list the cases and stop")
    ap.add_argument("--summary", action="store_true", help="show the latest suite's results and stop")
    args = ap.parse_args(argv)

    if args.summary:
        say(summary(results()))
        return 0
    cases = load_cases(args.cases or CASE_DIRS)
    if args.names:
        cases = {k: v for k, v in cases.items() if any(fnmatch.fnmatch(k, n) for n in args.names)}
    if args.list or not cases:
        for c in cases.values():
            say(f"{c['name']:<24} {repo_of(c).name:<20} {' '.join(c['request'].split())[:90]}")
        return 0 if cases else 1

    suite = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs = STATE / "logs" / suite
    logs.mkdir(parents=True, exist_ok=True)
    say(f"suite {suite}: {len(cases)} case(s) × {args.repeat} · buckets {buckets_version()} · logs in {logs}")
    try:
        for rep in range(1, args.repeat + 1):
            for c in cases.values():
                say(f"  {c['name']} #{rep} …")
                r = run_case(c, suite, rep, logs)
                record(r)
                bad = [f"{x['check']}: {x['detail']}" for x in r["checks"] if not x["ok"]]
                say(f"    {'✓ pass' if r['ok'] else '✗ fail'} in {r['minutes']} min" + ("".join(f"\n      {b}" for b in bad)))
    except RepoChanged as e:
        say(f"STOPPED: {e}")
        return 3
    except KeyboardInterrupt:
        say("stopped")
        return 130
    text = summary(results(), suite)
    (logs / "summary.md").write_text(text + "\n")
    say("\n" + text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
