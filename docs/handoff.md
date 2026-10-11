# Handoff (updated 2026-10-11 night)

Read this first. Details: [../README.md](../README.md), [field-notes.md](field-notes.md) (every
measurement; 16–19 are the latest), [roadmap.md](roadmap.md), and the research in
[research/2026-10-11-local-agentic-coding.md](research/2026-10-11-local-agentic-coding.md).

## What it is

`~/Coding/ME/buckets/buckets.py` (single file, Python 3.11+, no dependencies), linked as
`~/.local/bin/buckets`, plus `ts_index.cjs` (TypeScript index) and `test_buckets.py` (160 tests, ~30 s,
sandboxed, never touch real servers). Public repo `lvlrSajjad/buckets`, pushed with the personal
account (the repo's credential helper). Use it as a chat: `cd <repo> && buckets`. Commands: `merge`,
`discard`, `last`, `pr`, `review` (Claude reviews the latest branch), `setup`, `--doctor`.

## Machines and the current split (default since 2026-10-11)

| Role | Where | Model, speed |
|---|---|---|
| Reader and coder: routes, picks files, chooses parts, answers the planner's `ask` look-ups, codes | Windows (RTX 3070 8 GB, 32 GB RAM), llama.cpp server `192.168.1.111:8080`, starts at login (`C:\llama\start-buckets-server.ps1`) | Qwen3.6-35B-A3B (Ollama's GGUF `sha256-d372…`, experts in RAM, 4,096-token chunks): ~625 tok/s reading, ~40 writing (MTP) |
| Planner and reviewer (plans with look-up tools) | the Mac (M2 Pro 32 GB), Ollama `localhost:11434`, unloaded after each run | the same Qwen3.6-35B-A3B (copied from Windows): ~475 tok/s reading, ~48 writing (Ollama's MTP is on) |
| Paid fallback | `claude` CLI (Team login) | Sonnet: `--claude`, the escalation ladder's last step, `buckets review` |
| Installed, unused | Mac: `qwen3.6-planner` (27B); Windows Ollama: `qwen3:4b-instruct-2507`, `qwen3-coder:30b` | |

Config: `~/.config/buckets/config.json` (backups `config.json.bak-*`). `planner` = the Mac's 35B,
`offload.planner` = `advisor`, `offload.light_jobs` = false; `reader`/`thinker`/`coder` = Windows with
`"api": "llamacpp"`. opencode has a `llamacpp_windows` provider. Repo rules: `~/Coding/ME/*` local-only;
`~/work/mobile-app` has explicit checks (tsc, eslint errors-only, jest in `packages/shared`)
and `"related": {"backend-api": "~/work/backend-api"}`, mode automatic. the work repo
itself was left exactly as found (no branches, no `.buckets/`).

## The plan, in order (what to do next)

Done (field notes 20–24): MTP and caching on Windows; double review (a lone objection while checks pass is
weighed by the other reviewer); mechanical moves (`ts_moves.cjs`: extract, compare, share, share_hook,
unify) with dry-run suggestions, guards and live-verified results (big screen split 5m 14s; pair-1,
pair-2, pair-3 and pair-4 merges in 4m 43s–5m 38s); the AGENTS.md rule digest;
the locale-entry check; red CI as check output; the variant inventory for questions.

Next: **docs/roadmap.md, "Backlog, ranked"**: 0 (public-repo decision), then tier 1 (follow-ups on the
unmerged branch, `undo`, round rollback, diff-checked reviews, time limit, memory outside the repo), then
the refactoring toolbox (move to util, change a signature everywhere, N-way duplicates, components, renames),
then `measure`, the batch loop and the regression suite. Older infrastructure items (prompt cache layout,
parallel lanes, two candidates, adaptive placement, Windows network) are tier 5 there.

## How to test changes (gotchas learned the hard way)

- **This repo is public.** Work-repo specifics (screen and function names, file sizes, its AGENTS.md
  rules, findings like debug logs or missing locale keys) go in `docs/private/` (git-ignored; full versions
  of the field notes, roadmap and handoff are there). The public docs say the same things generically.

- **Live runs on the work repo:** start with `python3 <scratchpad>/capped.py 1200 buckets --local -y "<request>"`
  (a 20-min cap whose Ctrl-C reaches buckets: background shells ignore SIGINT otherwise); afterwards delete the
  `buckets/…` branch and the untracked `.buckets/` it creates, so the repo is as found. Verify a branch in a
  separate worktree with `node_modules` symlinked (`tsc` 10 s, eslint, jest 1,545 tests ~8 s).
- Don't edit `ts_moves.cjs` or `ts_index.cjs` during a live run (buckets calls them as it goes); `buckets.py`
  edits are safe once a run has started.

- **buckets runs from this working copy.** Never `git stash` buckets.py before a live run: the run uses
  the stashed-away (old) code. Commit first, then run.
- A/B: the same request twice, each on its own branch from the same `main`; a second config via
  `BUCKETS_CONFIG=/path/config.json buckets …`; compare per-step times in each run's `run.log` (the
  second run's "tests before" is cached).
- Verify every branch yourself before merging: `git diff main...<branch>` and the tests in a worktree
  (`git worktree add <scratch> <branch>`), never by switching the owner's checkout.
- macOS `zsh`: quote `"${B}:file"` (zsh reads `$B:t` as a modifier).
- Tests must not reach real servers: tests that plan should mock `chat_turn`/`ask_json` or set
  `"lookups": False`.

## Where local stops and Claude starts (field notes 17–19)

Local is reliable for questions, finding things, and changes in places it can find (2–5 min on a
2,000-file repo). It isn't for changes that must fit patterns across files, or judgment calls; Claude
planning did those in 2–8 min for $0.50–1.20, and `buckets review` caught a design mistake no check
could see for $0.28. Reviews are the weak spot locally: a false "undefined variable" turned a 3-minute
success into a broken result (fixed: whole changed functions in the review, loop check ignores quoted
code); mocked tests pass broken code.
