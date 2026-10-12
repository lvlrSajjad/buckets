# Handoff (updated 2026-10-12 ~06:30)

Read this first. Details: [../README.md](../README.md), [field-notes.md](field-notes.md) (every
measurement; 20–28 are the latest), [roadmap.md](roadmap.md), and the research in
[research/2026-10-11-local-agentic-coding.md](research/2026-10-11-local-agentic-coding.md).

## What it is

`~/Coding/ME/buckets/buckets.py` (single file, Python 3.11+, no dependencies), linked as
`~/.local/bin/buckets`, plus `ts_index.cjs` (TypeScript index) and `test_buckets.py` (~215 tests, ~45 s,
sandboxed, never touch real servers). Public repo `lvlrSajjad/buckets`, pushed with the personal
account (the repo's credential helper). Use it as a chat: `cd <repo> && buckets`. Commands: `merge`,
`discard`, `undo` (take back the last merge), `last`, `pr`, `review` (Claude reviews the latest branch),
`setup`, `--doctor`; flags `--local`, `-y`, `--time-limit MIN`. Mechanical moves live in `ts_moves.cjs`
(extract, compare, share, share_hook, unify, relocate, dedupe, same_behaviour, signature, component).

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

Done (field notes 20–28, roadmap "Backlog, ranked"):
- MTP and caching on Windows; double review (a lone objection while checks pass is weighed by the other
  reviewer; the confirm step re-asks without thinking when its budget runs out).
- Tier 1 (all): follow-ups build on the last unmerged run's branch (switch before planning); `undo`; a
  follow-up round that breaks passing checks is undone; reviews hear which planned tasks didn't run;
  `--time-limit`, Ctrl-C from background shells, SIGTERM; unattended stops commit half-done work on buckets'
  own branch (never the repo's stash list); `"memory": "outside"` (set for the work repo).
- Tier 2: `move`/extract to a hook, `share`/`share_hook`/`unify` for variants, `relocate` (to a util, every
  importer repointed, emptied source deleted), `dedupe` (copies replaced when they behave the same, run on
  sample inputs in a sandbox; differences shown, rechecked at review time), `signature` (a parameter or prop
  added everywhere), `component` (a render helper becomes a component). Each has a dry-run suggestion in the
  facts, guards against retyping, field-filling for the planner's quirks, and a live-verified run.

Done since (2026-10-11 afternoon):
- **The regression suite** (`regress.py`, cases in `regress/` and `docs/private/regress/`): each case runs on a
  clone under `~/.local/state/buckets-regress/clones/` (pinned commit, the repo's rules and node_modules
  links), never the repo itself; the suite stops if the repo's HEAD, branches, stashes or status change.
  `python3 regress.py --list`, `regress.py <names> -n 3`, `--set coder.engine=native`, `--summary`.
- **opencode fix:** opencode finds a project by its first commit and reopens the folder it first saw, so in
  a clone or second worktree it edited the original. Sessions now get `XDG_DATA_HOME` per repo path.
- **Native coder** (`coder.engine = "native"`, opt-in): buckets' own read/search/edit/write loop.
- **`buckets live`** (port 8765): `live.html` animates `events.jsonl` live, replays finished runs.

Done the night of 2026-10-11 (owner's go-ahead for 8 hours, both laptops; ~22:40–06:45):
- One run per machine (`--wait` queues); `buckets win` (agent on Windows, `tools/windows-agent.cjs`, v3,
  self-updating; `C:\buckets`, port 8766, token in `~/.config/buckets/agent.token`); Windows checks measured
  2.5–15× slower and push the model out of RAM (field notes 30), so `agent.checks = "overflow"` (only while the
  Mac is busy, tsc+eslint). The work repo's copy on Windows is kept (owner's wish).
- `rename` (item 11), `find_copies` + dedupe aliases (item 9: identical-once-renamed, non-trivial copies only),
  `buckets measure` (13), `buckets each` (14), coverage facts for test requests (17), `buckets bisect` (18),
  `--update BRANCH|PR` (19), red CI on a PR (20), `buckets contract` (23, `api_contract.cjs`), leftover-debug
  shortcut, "Decided without asking" in reports, repeated look-ups end planning, look-ups shortened to fit
  the planner's context (a plan hit 70k of 64k).
- Regression suite: `setup` commands per case; private cases `rename`, `update-conflict` added (not run yet);
  merge-inventory/rfp/proposal and signature expectations corrected (they checked lines, not what moved).

Done after compacting (2026-10-12, 00:00–01:00; field notes 32):
- `fold` (identical variant files → one file; refuses files nothing imports, `.d.ts`, copies reading their own
  siblings, and any quoted path in the repo still resolving to a copy), `to_ref` (module `let`s → `useRef` in the
  component; moves and shares do it first for the state they meet; `"module_state": "share"` keeps the old way),
  `component` by `lines` (+ `jsx_outline` facts), `buckets contract` reads NestJS routes from the backend's code
  (`nest_routes.cjs`, cached per backend state; falls back to the exported file), signature fields filled from the
  request (+ "instead of the fixed N"). A Sonnet review of all of it found 12 issues; all fixed (commit fa58d5c).
- Private cases added: `fold-navigator`, `chunk-component`; `merge-create-invoice` now expects no module `let` in
  the shared file.

Done 01:00–04:30 (field notes 32):
- **The native coder is the default** (A/B: the same results and times as opencode on every task both did);
  opencode takes a coder's last try when a native try changed nothing (`"coder": {"fallback": "opencode"}`).
- Live runs of the new tools on the current code: chunk → component, fold, rename, branch update with a conflict,
  signature all pass. Fixes from them: unify keeps copies that swap a value; every module `let` a merge meets
  becomes a ref (bare `let x` too, and ones only one variant has); "no changes needed" is done; request-spelled
  signature values win over the planner's; tasks on folders dropped; a half-done coder is blocked, not done; refs
  typed `any` like their untyped variable aren't counted as shortcuts.
- JSX chunks: identical copies in the same component become the same element.
- The work repo was left as found after every run (clean `main`, 5 stashes, no `buckets/*` branches).

Next (in order):
1. **Baseline 13/16** (suite 20261012-045022; diffs next to the logs). Failing: `merge-proposal` (item 12),
   `merge-rfp` and `component` (round 1 right and green; a review objection, to the plan's wording and to imports
   the move copied as they were, started a round 2 that broke the checks). Reviews that object to what a mechanical
   move kept as it was are 29b's territory (the other session). Also in rfp: a second task tried to create the
   shared file the share had just made ("already exists"); worth a look.
2. `merge-proposal` stops at the early-return limit (item 12): needs the owner's call (the clean fixes change call
   sites: late values passed as arguments, or the null guard moved).
3. Dependabot batches need `gh auth login` for the work repo on the Mac.
4. UI checks through simframe can start as a repo check command (roadmap 15b): needs a saved flow and Metro.
5. The other session owns 29b (review thinking budget) and tier 6.
6. Tier 7 (new projects from scratch, roadmap 38–46), after the above: start with the setup step (38) and page
   checks (39). Stacks: Ignite on Expo, Vite + React + shadcn, NestJS, the web app in Electron.

Known weak spots: the local planner varies run to run (guards cover it); reviewers are the weakest link
(verify every branch yourself); each new tool needed 3–7 live attempts before a clean run.

## How to test changes (gotchas learned the hard way)

- **This repo is public.** Work-repo specifics (screen and function names, file sizes, its AGENTS.md
  rules, findings like debug logs or missing locale keys) go in `docs/private/` (git-ignored; full versions
  of the field notes, roadmap and handoff are there). The public docs say the same things generically.

- **Live runs on the work repo:** `buckets --local -y --time-limit 20 "<request>"` from the repo (run in the
  background, watch the log). Afterwards: back on `main`, delete every `buckets/…` branch, check `git status` is
  clean and `git stash list` still has its 5 entries (memory is outside the repo now, so no `.buckets/`). Verify a branch in a
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
