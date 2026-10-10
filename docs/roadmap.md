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

## Done (2026-10-10, big repo)

- Request words → paths and code (mechanical), stretches of long components around the request's
  terms, coder focus on quoted text, review evidence for "remove" requests, safe monorepo fixers,
  checks that write aren't detected, request-shaped AGENTS.md and skill docs, related repos
  (read-only, `--add-dir` for Claude), `buckets review` (Claude reads the change and the code around
  it), suggestions to use Claude (heavy local runs) and to have Claude review (risky local results).
  See field notes 17.

## Research (2026-10-11), summarized

From [research/2026-10-11-local-agentic-coding.md](research/2026-10-11-local-agentic-coding.md):
- **Model:** keep Qwen3.6-35B-A3B (NVIDIA's like-for-like table: 70.1% SWE-bench Verified vs 51–59% for
  Qwen3-Coder-30B-A3B, Nemotron 3.5 Lightning, gpt-oss-20b, Gemma 4 26B-A4B, GLM-4.7-Flash). Dense
  models (Devstral Small 2, Qwen3.6-27B) would be slow here.
- **MTP:** llama.cpp supports Qwen3.6's built-in MTP heads (`--spec-type draft-mtp --spec-draft-n-max
  2`, PR #22673, 2026-05-16); expect ~+20–30% writing with experts on CPU, slower prefill, single slot.
  **Measured (field notes 20): +15% writing (35 → ~40), 97% of drafts accepted, reading ~unchanged.**
  Ollama already uses MTP (`draft_num_predict`).
- **Caching:** Qwen3.6 is hybrid (Gated DeltaNet), so `--cache-reuse` can't work; only exact prefix
  matches and context checkpoints do. Keep prompts byte-stable at the front; keep reasoning in multi-turn
  history (don't use `--no-reasoning-preserve`); constant `num_ctx` on Ollama.
- **MLX on the Mac:** ~1.5× faster writing, ~40% slower reading, memory crashes on 32 GB: not yet.
- **Agent findings:** planner/editor split mainly buys edit-format reliability for small models;
  search/replace edits with a lint gate; ~100-line windows; one retry with test output roughly doubles
  solve rate (34.7% → 62.2% on Aider Polyglot for this model); candidates + a verifier pay off.
- **Tooling:** multilspy (tsserver, basedpyright) for references/rename, ast-grep for codemods,
  tree-sitter for outlines; mutation testing (StrykerJS incremental, mutmut) and a mock-ratio check
  for weak tests.
- **Windows network:** likely antivirus HTTP scanning on all ports or Killer/SmartByte shaping.

## Two brains (the Mac and Windows both run the 35B)

Where a run's time goes now (field notes 18, B): planning and review thinking ~45%, tests ~20%,
reading ~15%, coding 0–25% (only when edits fall back to a coder). Ideas, by what they buy:
- **Double review** (quality, ~no time): both review each round at once; any issue wins.
- **Overlap** (time): review reads the diff while the tests run; the planner starts thinking on the
  facts while the reader is still choosing parts.
- **Look-ups while thinking** (time, with a reading loop): the planner asks "read X, answer Y", the
  other machine answers, the planner keeps going.
- **Parallel coding lanes** (time, when coders run): independent tasks on both machines at once.
- **Two plans compared** (quality): agreement → go; disagreement → a third look or a Claude review.

## Next (from the big-repo tests)

- **Edit anchors:** the planner keeps naming the file's path as `at`; a worked example in the plan
  prompt, or falling back to the quoted lines, would let more edits apply with no coder.
- **"Show more matches":** when the first page of the request-words search isn't enough, let the
  planner ask for the next page of a phrase (lists are capped at 12 paths / 6 files).
- **A reading loop on llama.cpp:** think, read a part, keep thinking, with the server's prompt cache
  making each extra read cost only its new tokens (a session per job instead of fresh calls).
- **Prompt order for the cache:** stable parts first (guidance, map, facts), the step's own
  instructions last, so planning, review and follow-ups reuse the server's cached prefix.
- **Writing speed:** the 35B has multi-token-prediction layers (`nextn` tensors, unused today); if
  llama.cpp can use them for speculative decoding, writing could go well past 35 tokens/s.
- **buckets as an MCP tool for Claude Code:** Claude explores and plans, buckets runs the plan
  locally (`--plan-file` exists), returns a run id, Claude checks back and reviews.
- **The 186 accessibility lint warnings** in ecotrak-mobile are eslint-fixable with no model; the
  431 hook-dependency warnings need Claude planning, a folder at a time.
- **pick-dirs** still runs on vague requests (~10 s); the request-words search could choose the
  folders when it already found a clear place.

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
| **Mechanical moves: extract to hook / module / component** (next to build). The model only picks the line ranges and the parameters; the TypeScript index works out which moved names the rest still uses (the hook's return and the screen's destructuring) and the imports; buckets moves the lines byte for byte; `tsc`/lint/tests decide | A 3,687-line screen (`assessment-questionnaire-screen.tsx`, ~1,450 lines of logic) can't be split by retyping it: ~16k tokens of edit code, ~6 min of writing on the Mac, and one changed line breaks it. The model stitches, buckets moves | Medium-large: TS first (ts_index.cjs), Python (ast) after |
| **Merge drifted variants** (`screens/<x>/ecotrak` vs `ecotrak-techs`): a matcher pairs declarations by name (identical / near / different / one side only; prototype in `tools/variant-*.{cjs,py}`); identical ones move to shared files mechanically, the model only parameterizes near ones from their small diffs, one-sided ones stay; later, shared JSX sections to components | create-invoice defeats Claude: the hook is 52% similar, 684 one-sided lines, so a merge by retyping means holding and rewriting two 1,000–1,500-line files. Ladder: rfp-details (150 identical lines) → inventory-details (3 one-sided lines) → create-invoice (pull shared parts only) | Large; after mechanical moves |
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
