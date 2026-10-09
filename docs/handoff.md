# Handoff (2026-10-10)

Where buckets stands after the first build session, for whoever (or whichever session) picks it up
next. Details live in [../README.md](../README.md), [field-notes.md](field-notes.md) (runs 1–14,
every measurement and failure) and [roadmap.md](roadmap.md).

## What it is

`~/Coding/ME/buckets/buckets.py` (single file, Python 3.11+, no dependencies), linked as
`~/.local/bin/buckets`. Plus `ts_index.cjs` (the TypeScript code index) and `test_buckets.py`
(110 tests, all passing; sandboxed, never touch real configs; includes a hardware-free end-to-end
test). **Not a git repo yet: the owner wants to do that with buckets itself**, e.g.
`cd ~/Coding/ME/buckets && buckets "make this a git repo, ignore __pycache__, and create a private GitHub repo for it"`.

## How the owner's machines are set up

| Role | Where | Model |
|---|---|---|
| Worker: plans, reviews, reads, codes | Windows laptop, `192.168.1.111:11434` (label "Windows") | `qwen3.6:35b-a3b` (MoE; ~112 tok/s read, ~37 tok/s think) |
| Advisor (escalations only) | the Mac, `localhost:11434` (label "the Mac") | `qwen3.6-planner` = `qwen3.6:27b-q4_K_M` with 64k context |
| Claude (last escalation step, `--claude` mode) | `claude` CLI, Team login | Sonnet by default |

Config: `~/.config/buckets/config.json` (written by `buckets setup`; backups `*.bak`). Folder rules:
`~/Coding/ME/*` is local-only (never Claude); each `~/Coding/{ME,PMX,OSS,ET,RZT}` folder sets
`GH_CONFIG_DIR` for its GitHub account; `gh_bin` is `/opt/homebrew/bin/gh` (the shell `gh` is a
function). opencode (`~/.config/opencode/opencode.json`) has providers `ollama_local` /
`ollama_windows` and the locked-down agents `buckets-coder` / `buckets-reader`.

Pitfalls already hit: Ollama's MLX build of Qwen 3.6 crashes on the M2 Pro (use GGUF tags);
`opencode run --format json` hangs; `opencode run --agent` ignores the agent's model (always pass
`-m`); don't edit the opencode config while a run is going; Ollama's app slider overrides
`OLLAMA_CONTEXT_LENGTH`.

## What it does (all in the README)

- Plain-language requests, routed to: a question (report), a code change (plan → code → checks →
  review, on a `buckets/…` branch), a cherry-pick (conflicts resolved by the models), or **git/GitHub
  chores** (typed operations: branch, commit, squash, rebase, sync, push, PRs incl. `pr_from_run`,
  CI status/failures/re-runs, comments, merges, branch cleanup, init/gitignore/repo_create; asks
  once before anything that changes or publishes).
- Windows-first, with escalation to the Mac advisor and then Claude (where allowed) on mechanical
  signals; at most 2 per run.
- Mechanical helpers: code index (outline, uses, importers, `imports` check, API impact), exact
  read/grep/docs/history lookups, parsed check errors, plan skeletons, auto-fixers, coverage map,
  facts before planning, thinking budgets, review skip for check-only jobs.
- `buckets setup` for coworkers; `buckets last|merge|discard|pr|--doctor`.

## Open threads

1. **castellan PRs** [#1](https://github.com/lvlrSajjad/castellan/pull/1),
   [#2](https://github.com/lvlrSajjad/castellan/pull/2), [#3](https://github.com/lvlrSajjad/castellan/pull/3):
   they went red only because GitHub's runners couldn't pull `postgres:17-alpine` from Docker Hub;
   buckets re-ran the failed jobs on 2026-10-09 ~23:59. Check with
   `buckets "what's the CI status of PRs 1, 2 and 3?"` in castellan. After #1 merges, the local
   `main` (2 unpushed `canAll` commits) should be reset: `git fetch && git reset --keep origin/main`.
2. **castellan branches** still local: `buckets/add-a-canany…`, `buckets/add-unit-tests-…-1627`,
   `buckets/two-parity-tests-…` (their content is in the PRs now; delete after merging, or
   `buckets "delete branches already merged"`), the PR branches, and `stress/{bug,tsc,pick-src}`
   (planted test scenarios; keep as fixtures).
3. **Escalation rules** (unsure research, repeated coder failure, reviewer/check disagreement,
   sensitive files, ladder to Claude) are unit-tested but haven't fired in a live run yet.
4. **The git assistant** has been used live for read-only status, failure logs and CI re-runs; branch,
   squash, push, PR creation and `pr_from_run` are tested against real git with a fake `gh` only.
5. Next ideas (roadmap): fix red PRs whose failures are real code problems (`pr_fix`: check out the
   PR branch, use the parsed CI failures as check output, run the code loop, push to the PR);
   rebase with model-resolved conflicts; mechanical renames; `git bisect`; affected-tests-only
   checks.
