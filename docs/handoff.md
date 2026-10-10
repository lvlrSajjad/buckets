# Handoff (2026-10-10, evening)

Where buckets stands after the second build day, for whoever (or whichever session) picks it up
next. Details live in [../README.md](../README.md), [field-notes.md](field-notes.md) (every
measurement and failure; 16–17 are today's) and [roadmap.md](roadmap.md).

## What it is

`~/Coding/ME/buckets/buckets.py` (single file, Python 3.11+, no dependencies), linked as
`~/.local/bin/buckets`, plus `ts_index.cjs` (the TypeScript code index) and `test_buckets.py`
(155 tests, all passing in ~30 s; sandboxed, hardware-free). A public GitHub repo:
`lvlrSajjad/buckets` (pushed with the personal account, via the repo's credential helper).

Use it as a chat: `cd <repo> && buckets`, then one request per line. Commands in the chat or the
shell: `merge`, `discard`, `last`, `pr`, `review`, `setup`, `--doctor`.

## How the owner's machines are set up

| Role | Where | Model |
|---|---|---|
| Worker: routes, reads, plans, reviews, codes | Windows laptop (RTX 3070 8 GB, 32 GB RAM), **llama.cpp server** at `192.168.1.111:8080` | `qwen3.6:35b-a3b` (Ollama's GGUF, experts in RAM, 4,096-token chunks): ~650 tok/s reading, ~35 tok/s writing |
| Advisor (escalations only) | the Mac, Ollama at `localhost:11434` | `qwen3.6-planner` (27B, 64k context): ~85 tok/s reading, ~9 tok/s writing |
| Claude (`--claude`, the ladder's last step, `buckets review`) | `claude` CLI, Team login | Sonnet by default |
| Unused, installed | Windows Ollama | `qwen3:4b-instruct-2507` (fast but too inaccurate for chores; field notes 16) |

The Windows server starts at login (a shortcut to `C:\llama\start-buckets-server.ps1` in the
Startup folder; the script stops Ollama's copy of the 35B first: both can't fit in 32 GB). Config:
`~/.config/buckets/config.json` (`reader`, `thinker`, `coder` have `"api": "llamacpp"` and the
8080 URL; backups `*.bak-*`); opencode has a `llamacpp_windows` provider. Going back to Ollama is
those two settings per role.

Repo rules in the config: `~/Coding/ME/*` local-only; each `~/Coding/<org>/*` sets `GH_CONFIG_DIR`;
`~/Coding/ET/ecotrak-mobile` has explicit checks (tsc, eslint errors only, jest, all in
`packages/shared`), `"related": {"ecotrak-api": …}` and, for the tests, `"mode": "local"` (the
owner may want it back on auto so heavy jobs go to Claude).

## What makes it fast (and why)

Reading is the cost on local hardware, so buckets reads as little as possible, mechanically:
- the project map (`.buckets/map.md`, one line per file) instead of the repo's outline;
- the request's own words searched as paths and code (`work order details` → `work-order-details/`);
- named code found by name; long files through their outline; long components as the stretches
  around the request's terms; agent docs cut to the sections that match;
- the planner's exact edits applied with no coder; coders shown the lines they change;
- check results cached by file tree; thinking capped (1,500 tokens on light jobs) and cut when it
  loops.

## Where local stops and Claude starts (field notes 17)

Local is reliable for questions, finding things and changes in places it can find (2–5 min on a
2,000-file repo, free). It is not reliable for changes that must fit existing patterns across files,
or for judgment calls (hook dependencies). Claude planning did those in 2–8 min for $0.50–1.20, and
`buckets review` ($0.20–0.50) caught a design mistake no check could see. buckets now says so: a
suggestion to add `--claude` for heavy local jobs, and to run `buckets review` when a local result
adds files, spans 3+ files, needed rounds, or isn't done.

## Open threads

1. **ecotrak-mobile throwaway branches** from the tests (8 `buckets/…` branches): delete them, or
   keep the Project filter (`buckets/look-at-the-current-work-order-s-1010-1832`, slot 33) if the
   owner wants it. `.buckets/` there is ignored through `.git/info/exclude`.
2. **castellan PRs** #1–#3 (from the first day): CI re-runs were triggered; check their state.
3. The roadmap's "Next (from the big-repo tests)": edit anchors, more matches, a reading loop on
   the llama.cpp prompt cache, speculative decoding with the model's MTP layers, an MCP tool for
   Claude Code.
