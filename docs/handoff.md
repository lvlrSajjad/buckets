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
`~/Coding/ET/ecotrak-mobile` has explicit checks (tsc, eslint errors-only, jest in `packages/shared`)
and `"related": {"ecotrak-api": "~/Coding/ET/web_api/ecotrak-api"}`, mode automatic. ecotrak-mobile
itself was left exactly as found (no branches, no `.buckets/`).

## The plan, in order (what to do next)

1. ~~Windows server: MTP and caching~~ **done** (field notes 20): writes 35 → ~40 tok/s, drafts 97%
   accepted, multi-turn prompts reuse the cache.
2. ~~Double review + evidence rules~~ **done** (field notes 21): both machines review at once; a lone
   definite-error claim while checks pass is confirmed by the other reviewer; weak new tests are listed
   mechanically for the reviewers. Still to see live: a disagreement (the confirm step).
2b. ~~Big-file refactor: mechanical extract~~ **done** (field notes 22): `ts_moves.cjs` (extract / compare /
   share), task actions `move` and `share`, a dry-run suggestion in the planner's facts, a guard that swaps a
   retyping plan for the move, redundant edit tasks next to a move dropped. The questionnaire split: 20 min
   and nothing (retyping) → **5m 14s, correct**.
2c. **Merge ecotrak / ecotrak-techs variants** (field notes 23): `share` moves identical top-level declarations
   into the named shared file and identical statements inside both components into a shared hook in the same
   file (re-deriving `useStores()`-style values, props typed from the component's props type, unused names and
   imports cleaned). Live: rfp-details 5m 38s, inventory-details 4m 52s, both verified. create-invoice: running
   live; by hand 10 inner statements shared, its number "helpers" are module `let`s the hook assigns (rightly
   kept). Next: near copies (the model makes them identical from the small diff, then `share`), JSX sections
   → components, whole-screen merges behind a `variant` prop.
2d. **Owner's job list** (docs/roadmap.md, "Where it has to help"). Done tonight: AGENTS.md rule digest in every
   plan and review; locale-entry check for reviews (10 real gaps found in 60 ecotrak-mobile commits); red CI
   (failed GitHub Actions log as check output). Next: PR conflicts on PR branches, dependency batches,
   graphify communities as facts, Sonar issues as check output, a backend contract check.
3. **Prompt layout for cache reuse:** byte-stable front (system, tool schema in fixed order, guidance,
   map, facts), the step's instructions last; check `prompt_eval_cached_count`. Ollama: constant
   `num_ctx`, a longer `keep_alive` during a run.
4. **Parallel coding lanes:** independent tasks coded on both machines when edits fall back to coders
   (needs an opencode provider for the Mac's Ollama 35B).
5. **Two candidates + tests** for hard tasks (both machines, tests pick, only "both failed" goes to
   Claude); research cites 63.5% → 71.2% (SWE-TRACE).
6. **Language servers and ast-grep:** `multilspy` (tsserver, basedpyright) for references/rename;
   ast-grep for codemods; tree-sitter outlines (TS outline is cached per run today).
7. **Adaptive placement:** if the Mac is under memory pressure (IDE, browsers), plan on Windows.
8. **Windows network** (owner, when convenient): Windows → Mac is ~27 KB/s on every non-SMB port (SMB
   30–40 MB/s). Research's top suspects: antivirus HTTP/web scanning on all ports, or Killer/SmartByte
   prioritization. Diagnostics in the research file, section 8.

## How to test changes (gotchas learned the hard way)

- **Live runs on ecotrak-mobile:** start with `python3 <scratchpad>/capped.py 1200 buckets --local -y "<request>"`
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
