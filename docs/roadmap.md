# buckets roadmap

What's left, roughly in the order I'd do it, with why and how big. Measured results and the history
behind these are in [field-notes.md](field-notes.md).

## Done (2026-10-09)

- Plan skeletons from parsed check errors: check-only jobs are planned with no model; follow-up
  tasks always carry the exact errors and current code for their file (would have fixed run D).
- Research rounds think (1,500-token budget) and keep hypotheses; "enough" with an open hypothesis
  gets one more round (would have caught run B).
- Review rule for test assertions that only count results.
- Router: "fix …" requests are code changes even when they also ask why.
- Local docs for the installed version (`docs: "pkg"` / `"pkg#Symbol"`; npm packages and Python
  modules) and git history for line ranges (`history: "path:a-b"`), both exact and instant.
- `--plan-file plan.json`: run a plan written elsewhere (e.g. by Claude), skipping reading and planning.
- Windows-first defaults with the Mac as an advisor (very hard jobs, no-progress rounds, worker down).
- Shareable: neutral built-in defaults, machine labels in messages, `buckets setup` (writes the config,
  installs opencode providers and agents, with backups), a setup guide and model table in the README.
- A hardware-free end-to-end test (fake Ollama and fake opencode: plan → code → checks → review →
  merge in ~2 s). Tests are sandboxed so they can't touch real configs.

## Done (2026-10-10)

- Chat mode (`buckets` on its own), git follow-ups ("name it X", "yes go ahead"), defaults instead
  of refusals for `repo_create`, and fixes from the first live publish (unborn HEAD crash, `.gitignore`
  requests routed as code, never `git init` the home folder).
- **Speed** (the worker reads prompts at ~200 tokens/s, so prompt size is most of the time): long
  files read through their outline; light jobs take the files the request names; tasks carry a
  `where` and the coder is shown that code; the planner's exact `edits` are applied without a
  coder session; no "pick the parts" call when the facts already show the named code; light jobs
  think for 1,500 tokens at most, and thinking that repeats itself is cut early; checks aren't
  re-run on a file tree already checked. A small Python change went from 20+ minutes (unfinished)
  to 6 minutes before the last three of these.
- Coders and look-ups run with `opencode run --standalone`, so Ctrl-C stops their edits.

## Git and GitHub assistant (added 2026-10-09)

- **`pr_fix`: fix red PRs with real code failures:** check out the PR branch, feed the parsed CI
  failures in as check output, run the code loop, push the commits to the PR. (Infrastructure
  failures are already handled: `ci_rerun`.)
- **Rebase with model-resolved conflicts**, reusing the cherry-pick conflict resolution (today a
  conflicting rebase is aborted cleanly).
- **Two-step requests:** let the assistant look first, then decide ("re-run only the jobs that
  failed for infrastructure reasons"); today the steps are planned up front.
- **Live-test** branch/squash/push/`pr_from_run` on a real repo (only read-only steps and CI re-runs
  have run live so far).

## Next

- **Confirm the thinker as coder** on 2–3 more jobs (a real bug fix, a small feature); run E was the
  only full run with it. Windows-only, so it can run while you work.
- **The advisor's value:** the Mac test (27B plans the `$lte` bug) decides whether escalation should
  go to the Mac or straight to Claude.

## Mechanical hands

| Item | Why | Size |
|---|---|---|
| **Mechanical renames** through the TypeScript language service (`rope` for Python) | A rename across packages with no planner or coder at all | Large: needs a full type-checked program per repo |
| **`git bisect run <check>`** when a check passed at a known commit | Pins a regression to the commit that caused it, mechanically | Medium: needs a "good" commit from the request or history |
| **Affected tests only** during rounds (Vitest `related`, Jest `--findRelatedTests`), the full suite at the end | Faster rounds on big repos | Medium |
| **Python index parity**: `uses`, `importers` and the broken-imports check (the outline exists), plus a syntax check right after each coder edit (planned edits already get one) | The same impact checks for Python repos; a broken edit caught in a second instead of at the next check run | Medium |
| **Dependency inventory** (package tree, `pnpm audit --json` / `osv-scanner`, optionally a CycloneDX SBOM) for dependency, vulnerability or license requests | Exact lists instead of reading lockfiles | Medium |
| **Coverage-driven jobs**: add a coverage check where missing (castellan needs `@vitest/coverage-v8`) | The coverage map is built but dormant until a repo produces a report | Small per repo |

## Speed and usability

- **Time hints from history:** "planning usually takes 4–8 min here", from past runs' `run.log`s.
- **Thinking budget tuning:** compare plan quality at 1,500 / 2,500 / 4,000 (advisor) and 3,000 / 6,000 /
  10,000 (thinker) on the same tasks.

## Claude as layer 0 (work repos)

- **A `/buckets` Claude Code skill:** explore with a cheaper subagent, write a plan file, run
  `buckets --plan-file` in the background, review the result.
- **Measure the saving:** the stress scenarios with plain Claude Code vs Claude mode ($0.27–0.64 per
  scenario so far). Costs about $1–3 of Claude usage; needs your go-ahead.

## Docs and the web

- **Fetch a named documentation URL** (opt-in per repo) through the read-only agent, passed as quoted,
  untrusted data.
- **Web search** (opt-in per repo): Claude's in Claude mode; check which backend opencode's search tool
  needs. Off by default: queries leave the network, and web text is untrusted input to models that
  write code.

## Upkeep

- **Version control for buckets itself:** `~/Coding/ME/buckets` isn't a git repo yet (needed before
  sharing it by clone).
- **Re-check upstream bugs on updates:** `opencode run --format json` hang; `opencode run --agent`
  ignoring the agent's model; Ollama's MLX speculative-decoding crash on the M2 Pro.
