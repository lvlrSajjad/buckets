#!/usr/bin/env python3
"""Replay saved review calls with other settings (roadmap 29b/29c): each review prompt a past run sent is
sent again under each arm, on the machine that answered it (planner reviews on the planner's, second reviews
on the thinker's), and the verdicts, thinking cut-offs and times are compared. The machines run side by side,
each working through its own queue.

    python3 tools/replay_reviews.py pick -n 24 > picks.json        # a stratified sample of saved reviews
    python3 tools/replay_reviews.py run picks.json arms.json out/  # every arm on every pick, results.jsonl
    python3 tools/replay_reviews.py report out/results.jsonl

arms.json: {"base": {}, "precise": {"think_sampling": {...}}, "budget1500": {"think_budget": 1500}, ...}; each
arm's settings go over the role's config (planner or thinker) for that call.
"""
import json
import random
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import buckets  # noqa: E402

RUNS = Path.home() / ".local/state/buckets-regress/state/runs"
NOTES = "\n\nYour notes from thinking about this so far"


def saved_reviews(runs: Path = RUNS):
    for f in sorted(runs.glob("*/*review-*.json")):
        m = re.match(r"\d+-(second-review|review)-(\d+)(-[a-z-]+)?\.json$", f.name)
        if not m or (m.group(3) or "").endswith(("retry", "short", "answer", "escalated")):
            continue
        d = json.loads(f.read_text())
        body = d["request"]
        user = body["messages"][1]["content"]
        cut = NOTES in user
        try:
            v = buckets.extract_json(d["content"])
            verdict = {"done": bool(v["done"]), "issues": [str(i) for i in v.get("issues") or []],
                       "tasks": len(v.get("tasks") or [])}
        except Exception:
            continue
        try:
            light = json.loads(json.loads((f.parent / "02-route.json").read_text())["content"]).get("difficulty") == "light"
        except Exception:
            light = False
        yield {"file": str(f), "run": f.parent.name, "light": light, "role": "thinker" if m.group(1) == "second-review" else "planner",
               "system": body["messages"][0]["content"], "user": user.split(NOTES)[0], "cut": cut,
               "think": True if cut else bool(body["think"]), "schema": body.get("format"),
               "request": (f.parent / "01-request.md").read_text()[:200] if (f.parent / "01-request.md").exists() else "",
               "verdict": verdict}


def pick(n: int, seed: int = 7):
    """n reviews, half from each role, and within a role half that were cut off and half that weren't,
    each half split between approvals and objections where it can be."""
    rnd, out = random.Random(seed), []
    every = [r for r in saved_reviews() if r["think"]]
    for role in ("planner", "thinker"):
        for cut in (True, False):
            group = [r for r in every if r["role"] == role and r["cut"] == cut]
            want = n // 4
            ok = [r for r in group if r["verdict"]["done"]]
            no = [r for r in group if not r["verdict"]["done"]]
            rnd.shuffle(ok), rnd.shuffle(no)
            take = no[: want // 2] + ok[: want - min(len(no), want // 2)]
            out += take[:want]
    return out


class Sink:
    """What buckets.chat needs of a Run: it saves files, and tells us whether thinking was cut."""

    def __init__(self, folder: Path):
        self.dir, self.cut, self.talked, self.saved = folder, False, False, {}
        folder.mkdir(parents=True, exist_ok=True)

    def save(self, name, content):
        if name.endswith("-thinking-cut.txt"):
            self.cut = True
        self.saved[name] = content
        p = self.dir / name
        p.write_text(content if isinstance(content, str) else json.dumps(content, indent=1))
        return p


def one(cfg: dict, arm: str, settings: dict, r: dict, i: int, out: Path, budgets: dict) -> dict:
    # the budget a run gives a review: the planner's for both reviewers (the round waits for both), less for light jobs
    c = {**cfg, "think_budget": budgets["light" if r.get("light") else "planner"],
         **{k: v for k, v in settings.items() if k != "think"}}
    sink = Sink(out / arm / f"{i:02d}")
    start, err, verdict = time.time(), "", None
    try:
        reply = buckets.chat(c, sink, "review", r["system"], r["user"], schema=r["schema"],
                             think=settings.get("think", r["think"]), max_tokens=16000, timeout=1800, quiet=True)
        v = buckets.extract_json(reply)
        verdict = {"done": bool(v["done"]), "issues": [str(x) for x in v.get("issues") or []],
                   "tasks": len(v.get("tasks") or [])}
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:300]
    stats = {}
    for name, val in sink.saved.items():
        if name.endswith(".json") and isinstance(val, dict):
            stats.setdefault("calls", []).append({k: val["stats"].get(k) for k in
                                                  ("eval_count", "eval_duration", "prompt_eval_duration", "done_reason")})
            stats.setdefault("thinking_chars", 0)
            stats["thinking_chars"] += len(val.get("thinking") or "")
    return {"i": i, "arm": arm, "role": r["role"], "file": r["file"], "orig_cut": r["cut"], "orig": r["verdict"],
            "cut": sink.cut, "secs": round(time.time() - start, 1), "verdict": verdict, "error": err, **stats}


def run(picks_path: str, arms_path: str, out_dir: str):
    picks, arms = json.loads(Path(picks_path).read_text()), json.loads(Path(arms_path).read_text())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cfg = buckets.load_config() if hasattr(buckets, "load_config") else json.loads(buckets.CONFIG_PATH.read_text())
    budgets = {"planner": cfg["planner"].get("think_budget", 0), "light": cfg.get("light_think_budget") or 1500}
    results, lock = out / "results.jsonl", threading.Lock()
    done = {(d["arm"], d["i"]) for d in map(json.loads, results.read_text().splitlines())} if results.exists() else set()

    def lane(role):
        # arm by arm, so a stop part-way still leaves whole arms to compare
        for arm, settings in arms.items():
            for i, r in enumerate(picks):
                if r["role"] != role or (arm, i) in done:
                    continue
                res = one(cfg[role], arm, settings, r, i, out, budgets)
                with lock, results.open("a") as f:
                    f.write(json.dumps(res) + "\n")
                    print(f"{arm:>12} {role:>7} #{i:02d} cut={res['cut']!s:5} {res['secs']:6.1f}s "
                          f"done={res['verdict'] and res['verdict']['done']} (was {r['verdict']['done']}) {res['error']}",
                          flush=True)

    lanes = [threading.Thread(target=lane, args=(role,)) for role in ("planner", "thinker")]
    for t in lanes:
        t.start()
    for t in lanes:
        t.join()


def report(results_path: str):
    rows = [json.loads(line) for line in Path(results_path).read_text().splitlines()]
    arms = list(dict.fromkeys(r["arm"] for r in rows))
    print(f"{'arm':>12} {'role':>7} {'n':>3} {'cut':>5} {'mean s':>7} {'think ch':>9} {'same verdict':>13} {'errors':>6}")
    for arm in arms:
        for role in ("planner", "thinker", "all"):
            rs = [r for r in rows if r["arm"] == arm and (role == "all" or r["role"] == role)]
            if not rs:
                continue
            ok = [r for r in rs if r["verdict"]]
            same = sum(r["verdict"]["done"] == r["orig"]["done"] for r in ok)
            print(f"{arm:>12} {role:>7} {len(rs):>3} {sum(r['cut'] for r in rs) / len(rs):>5.0%} "
                  f"{sum(r['secs'] for r in rs) / len(rs):>7.1f} {sum(r.get('thinking_chars', 0) for r in rs) // len(rs):>9} "
                  f"{same:>5}/{len(ok):<7} {sum(bool(r['error']) for r in rs):>6}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "pick":
        n = int(sys.argv[sys.argv.index("-n") + 1]) if "-n" in sys.argv else 24
        print(json.dumps(pick(n), indent=1))
    elif cmd == "run":
        run(*sys.argv[2:5])
    elif cmd == "report":
        report(sys.argv[2])
