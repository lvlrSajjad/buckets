# Field notes: running coding agents on two laptops

A record of building and using `buckets`: what broke, what we measured, and how each
real run went, including the failures. Newest results go at the bottom of each section.

## Hardware and models

| Role | Machine | Model | Context | Memory used |
|---|---|---|---|---|
| Planner | MacBook Pro M2 Pro, 32 GB | `qwen3.6:27b-q4_K_M` (GGUF), as `qwen3.6-planner` | 64k | 20.7 GB |
| Coder | Windows gaming laptop, 32 GB RAM, 8 GB GPU | `qwen3-coder:30b` (MoE, ~3B active) | 64k, `q8_0` KV cache | 22.4 GB (6.4 GB on GPU) |
| Optional planner | Claude via the `claude` CLI | Sonnet by default | n/a | n/a |

Both run Ollama on the same home network; the coder is driven through opencode 2.0.26.

## Setup problems and fixes (2026-10-09)

| Symptom | Cause | Fix |
|---|---|---|
| `Error: Model exo-free has been deprecated` | opencode v2 config used `agents` (should be `agent`), no top-level `model`, a nonexistent `@opencode/ollama` package; opencode fell back to its default free model | Rewrote the config: `@ai-sdk/openai-compatible` providers with explicit models, `provider/model` names, a top-level `model` |
| `request (5370 tokens) exceeds the available context size (4096 tokens)` | Ollama's default context is 4k; opencode's system prompt alone is ~5.4k tokens | Windows: Ollama Settings → Context length. Mac: a model variant with `PARAMETER num_ctx 65536` |
| 64k setting on Windows had no effect | The Ollama app's own context slider overrides `OLLAMA_CONTEXT_LENGTH` | Set the slider, fully quit Ollama from the tray, restart |
| Windows at 64k too big for 8 GB GPU | KV cache ~96 KB/token at fp16 | `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`: 64k cost the same memory as 32k did before |
| `OpenAI Chat stream ended without finish_reason` on the Mac planner | Ollama 0.40.x MLX engine panics in speculative decoding on the M2 Pro (`Maximum threads per threadgroup is 896 but requested 960`; also `cache: offset … exceeds … stored keys`). Reproduced 3/3 on a 2.6k-token prompt; `draft_num_predict` didn't help | Switched to the GGUF build (`qwen3.6:27b-q4_K_M`, llama.cpp engine): 3/3 clean on the same prompt |
| `opencode run --agent plan` used the Windows model | `opencode run` ignores an agent's configured model | Always pass `-m provider/model` |
| `opencode run --format json` hung for 10 minutes | Bug in opencode 2.0.26 | Use plain output; judge results from `git diff` |
| The coder used an `execute` (run code) tool on a trivial prompt | opencode v2 has an `execute` tool the permission list didn't cover | `buckets-coder` agent denies `execute`, `bash`, web, `task`, `question`, `skill` |
| A coder session hung right after editing the opencode config | The background service reloads on config changes | Don't edit opencode config or restart its service mid-run |
| `claude: command not found` | Claude Code installed under nvm Node 20, default Node is 22 | buckets config uses the absolute path to `claude` |

## Measured speeds

| Step | Speed |
|---|---|
| Mac planner reading new text (prefill) | ~83–87 tokens/s |
| Mac planner reading cached text | ~2,000 tokens/s |
| Mac planner writing / thinking | ~9 tokens/s |
| opencode session overhead (5.8k-token system prompt) | ~80 s on the Mac, ~40 s on Windows |
| One-file edit by the Windows coder | 47–78 s |
| KV cache, Qwen 3.6 27B | 64 KB/token (only 16 of 64 layers use attention) |

## Runs

### 1. Info question, local mode with the 14B stand-in (castellan)

*"Explain how a permission check flows from the NestJS guard down to the core evaluator."*
Planner: `qwen3:14b` (the 27B MLX build was crashing).

- 12 min 46 s: classify 9 s, pick 11 files 21 s, 3 survey batches (~40k tokens) 10 min, answer 2 min.
- Steps 1–4 of the answer were right. Step 5 was invented: it said the guard calls
  `Authz.enforce` after `Ability.can`; it doesn't. No `path:line` citations despite the prompt.
- Lesson: info answers from small models need checking before you act on them.

### 2. `canAll` feature, local mode (castellan)

*"Add `canAll(action, subjects)` to `Ability` … add unit tests."* Planner: Qwen 3.6 27B GGUF, thinking on.

- 17 min total: plan 11 min (16k tokens in, 4.2k thinking/answer tokens out), coder 78 s + 47 s,
  checks 7 s, review 2 min 11 s. Done in 1 round.
- The thinking plan was excellent (exact code, the file's real fixtures, line numbers) but a
  buckets JSON-parsing bug threw it away: a code fence inside an instruction string was taken
  for a fenced reply. The no-thinking retry (52 s) wrote a vaguer plan. Fixed since.
- Result correct and mergeable: loop-based `canAll`, 3 tests with a real denial case.

### 3. `canAll` feature, Claude mode (castellan)

Same request, `--claude` (Sonnet plans and reviews, Windows codes).

- 2 min 56 s total: plan 16 s ($0.17), coder 74 s + 56 s, checks 7 s, review 9 s ($0.11).
- Claude usage $0.29 (API-price estimate; on a Team plan it counts toward usage limits).
- Result slightly tidier: `subjects.every(...)` with a one-line doc in the file's style, 2 tests
  using the file's existing "closed work order" rule. Merged into castellan.

| | Local | Claude mode |
|---|---|---|
| Total time | 17 min | 3 min |
| Claude cost | $0 | $0.29 |
| Coder time | ~2 min | ~2 min |
| Quality | correct | correct, a bit tidier |

### 4. Stress test, Claude mode with Sonnet (castellan, 2026-10-09)

Three scenarios harder than `canAll`, run one after another. The tsc and bug scenarios start
from throwaway branches with a planted problem committed under a neutral message.

| Scenario | Setup | Result | Time | Claude cost | Rounds |
|---|---|---|---|---|---|
| **Tests** for untested `subject.ts` and `lint.ts` | none, from `main` | **Done.** 175 lines in 2 new test files, good coverage (`modelName` override, anonymous classes, tagging, lint shadowing) | 7 min 50 s | $0.64 | 2 |
| **tsc fix** after renaming `SubjectType` → `SubjectKind` in `subject.ts` | 3 core files fail typecheck; nestjs/typeorm also use the old name but compile against core's built `dist`, so `tsc` doesn't flag them | **Failed in planning:** `error_max_structured_output_retries` after 23 turns. No plan, nothing changed | 2 min 54 s | $0.52 | 0 |
| **Bug:** `$lte` made to behave like `<` | Core's 60 tests still pass; 2 TypeORM parity tests fail | **Done.** Root cause named exactly in the plan, one-character fix, plus 2 boundary tests | 2 min 46 s | $0.47 | 1 |

What happened:
- **Tests:** the first attempt at T1 made no change: the coder tried to open the not-yet-existing
  test file and gave up; the retry worked. The round-1 review then caught a misleading test: it
  claimed to check that a warning includes the deny rule's reason, but `lintRules` never uses
  the reason, so the assertion passed trivially. Round 2 fixed it. This is the "weak tests that
  pass by accident" risk, and the review loop caught it.
- **tsc:** Claude explored for 23 turns but couldn't fit its answer to the plan's JSON schema;
  the CLI gave up after its retries. Likely cause: a rename across up to 7 files with exact code
  per task makes a very long structured answer. buckets had no fallback for this error.
- **Bug:** the failing tests and the cause were in different packages; the plan traced it
  through the parity test to `compare()` in core and explained why SQL (`LessThanOrEqual`)
  disagreed with the in-memory check.

Fixes queued from this run: retry without the JSON schema (parse JSON from the text) when
structured output fails; ask Claude to keep mechanical instructions short; tell the coder when
a file doesn't exist yet so it creates it instead of trying to read it.

### 5. tsc scenario re-run after the fixes (castellan, Claude mode with Sonnet)

Same planted rename, after adding the plain-JSON fallback and asking for old → new replacements
instead of pasted files.

- **Plan in 37 s ($0.15), 9 turns.** It caught the hidden catch: 7 tasks, including the 4 files in
  `packages/typeorm` and `packages/nestjs` that `tsc` never flagged (they compile against core's
  built `dist`).
- Coder: 7 one-file sessions, 20–126 s each, 6 min 27 s in all. Typecheck, lint and tests pass.
- Review in 14 s ($0.12): "all 7 files correctly renamed; no alias".
- **But the run ended "not done"**: the shortcut detector flagged `request: any` in
  `authz.service.ts`. That `any` was already there; the rename edited the same line. A false
  positive. Fixed: shortcuts now count per file and pattern against removed lines, so only
  shortcuts a change *adds* are flagged.
- Total 7 min 32 s, $0.27.

Updated stress table: tests ✓ (2 rounds), tsc ✓ on the second try (after two buckets fixes),
bug ✓ (1 round).

### 6. Router on six prompts (local, free)

| Prompt | Routed as | Time |
|---|---|---|
| "add tests for lint.ts" | code, light → local | 20 s |
| "why do the parity tests in typeorm fail…? find the root cause" | question, heavy | 15 s |
| "cherry pick 4f2a1c9 and a1b2c3d onto release/1.3" | pick; both commits and the target extracted | 14 s |
| "how does scope resolution work?" | question, light | 16 s |
| "add a Prisma adapter like the TypeORM one in packages/typeorm, just show me the plan first" | code, heavy, plan only, example `packages/typeorm` | 15 s |
| "redesign the condition engine so … share one compiled representation" | code, very hard (would offer Opus) | 18 s |

All reasonable. "Why… find the root cause" became a question, not a fix; say "fix" to get a
change. The folder example (`packages/typeorm`) led to adding folder support to `--like`.

Also learned: git identity comes from a per-folder include under `~/Coding/ME`, so throwaway repos
elsewhere have no identity and `git cherry-pick` refuses to commit. buckets now checks for an
identity before changing anything.

### 7. Cherry-pick with a real conflict, local mode (castellan)

Setup: a throwaway branch from before `canAll` was merged, with two commits: `canAny` added in
exactly the spot where `main` now has `canAll` (a guaranteed conflict), and an unrelated doc
comment in `lint.ts`. Request, in plain words: *"cherry pick 5f58e84 and 9bbaf79 onto main"*.

- Router (15 s): cherry-pick, light; both commits and `main` extracted; local because the repo
  is under `~/Coding/ME`.
- `5f58e84` conflicted in `ability.ts`. Conflict plan: 7.6k tokens in, 2.9k out (thinking), 7 min.
  Coder: one session, 84 s. No markers left; `cherry-pick --continue`.
- `9bbaf79` applied cleanly (plain git, no model).
- Checks passed; review (2 min) confirmed the resolution. **Done, 11 min, $0.**
- The resolution was exactly right: kept `main`'s `canAll` and put `canAny` after it. The commit
  kept its message plus `(cherry picked from commit …)`.
- Found while watching: a review round with no tasks re-ran all checks for nothing (fixed), and
  fixing that briefly made the review see pre-pick check results (caught on re-read, fixed).

### 8. Parallel reading: Mac and Windows share the survey (castellan, local mode)

Same question as run 1 (*"Explain how a permission check flows from the NestJS guard down to the
core evaluator"*), now with the 27B planner and survey batches shared through a queue.

| | Run 1: 14B, Mac only | Run 8: 27B, Mac + Windows |
|---|---|---|
| Reading 3 batches (~36k tokens) | ~10 min | **4 min 30 s** |
| Windows | — | 2 batches (17.5k + 7.6k tokens) in 117 s + 54 s |
| Mac | all 3 | 1 batch (11.2k tokens) in 270 s: the slowest piece |
| Answer | 2 min | 8 min 46 s (27B, thinking, 4.4k tokens out) |
| Total | 12 min 46 s | 14 min 5 s |
| Accuracy | step 5 invented | all named functions real and in the right order; covers both modes (store-backed vs external grants); one slip (`AuthzService.check` doesn't exist) |

- Windows read about 3× faster than the Mac (~150 vs ~40–85 tokens/s on these batches). The
  Mac's one batch set the pace; giving Windows a larger share would cut reading further.
- No line numbers in the answer: the survey notes didn't carry `path:line`, so the answer had
  nothing to cite. The survey prompt now asks for locations.
- The 27B answer is slower to write than the 14B's but much more accurate. Thinking is where the
  time goes now, not reading.

### 9. Question-and-answer reading, first design (stopped early)

The idea: the Mac planner asks fetch questions, a read-only Windows opencode session answers each
from the code, and the script attaches the real lines for every `path:line` cited. Tested on the
planted `$lte` bug, local mode, plan only.

- The Mac didn't ask research questions. Its first round was 5 requests of the form "show the
  full code of <file>", for the test, the SQL scoper, the condition engine, `ability.ts` and
  `find-options.ts`.
- Windows answered each by pasting the file: 116–205 s per answer, and every answer hit the
  8,000-character cap, so the files arrived **cut off**.
- That's direct reading done slowly and lossily through a middleman. Stopped after 3 errands
  (10 min) since the lesson was clear.

Redesign:
- **"Show me" goes to the script.** The planner lists files or line ranges under `read`; the
  script serves the exact text from disk, at once, with no model in between.
- **"Find me" goes to Windows.** Searches and scans only (where is X used, which tests cover Y,
  scan these files for a likely cause). It may span several files but answers with at most 12
  short located leads (`path:line`: one sentence) and a 3-sentence summary, under ~3,000
  characters, with no pasted code.
- **The script attaches the real lines** behind every lead; the planner judges from those and
  treats the helper's sentences as hints.

### 10. Mechanical hands (built and unit-tested; first live measurement tonight)

Built so the slow planner reads answers instead of reasoning toward them:

- **Code index** (TypeScript via the repo's own `typescript`, Python via `ast`): outline with line
  ranges; `uses` across packages; **`imports` check**. On the tsc scenario the imports check found
  all 7 broken files mechanically in ~0.3 s, including the 4 NestJS/TypeORM files `tsc` couldn't
  see; nothing on `main`.
- **API impact:** exports compared before and after each round; callers of changed exports that
  weren't updated must be handled by the reviewer.
- **Exact lookups for the planner:** `read` (files/ranges from disk), `uses` (code index), `grep`
  (`git grep`); Windows only for fuzzy scans, answering in short located leads.
- **Parsed check output:** tsc, ESLint, Vitest/Jest, pytest, ruff, mypy, go → file → line → message
  with the source line. Verified on today's real tsc and Vitest logs.
- **Auto-fixers** (the repo's ESLint/Prettier/ruff/gofmt) on changed files only, before checks.
- **Coverage map** from Istanbul or coverage.py reports (dormant until a repo has a coverage check).
- **Facts before planning:** code around each failing check, definitions and uses of names in the
  request, closest tests. For the `$lte` bug it gathers the failing parity test (scope.test.ts:81-99),
  `scopeQuery` and `can()`; it does not reach `conditions.ts` by itself, which the planner still has
  to find.
- **Thinking budget:** stream thinking to 2,500 tokens (~5 min on the Mac), then ask for the answer
  without thinking, passing the notes. Tested against a fake Ollama server.
- **Review skip** when the router says the checks define the job and every check passes with no
  regressions, shortcuts or untouched callers.

### 11. The Windows "thinker": Qwen 3.6 35B-A3B (first measurement)

Pulled on the Windows laptop: GGUF, llama.cpp engine, 35.5B parameters (mixture of experts, ~3B
active), supports tools and thinking. One test: `conditions.ts` (4,001 tokens) plus a question
about `compare()` and `$lte`, thinking on, 64k context.

| | Mac 27B dense | Windows 35B-A3B |
|---|---|---|
| Reading | ~85 tokens/s | **112 tokens/s** |
| Thinking/writing | ~9 tokens/s | **37.5 tokens/s (~4×)** |
| Memory | 21 GB on the work machine | 22.7 GB on the idle laptop (4.5 GB on GPU) |
| First load | — | 55 s (Ollama swapped out the coder) |

The answer was correct (on `main`, `$lte` is `a <= b`). Its thinking budget is set to 6,000 tokens
(~2.7 min), more than the Mac's 2,500, since it thinks 4× faster. Plan quality against the 27B is
the open question for the comparison run.

### 12. Windows-only comparison (castellan, local mode, Mac planner never used)

Five runs back to back. A and B plan the planted `$lte` bug (plan only); C–E do a full run of
"add unit tests for `lint.ts` in a new `packages/core/test/lint.test.ts`".

| Run | Plans | Codes | Time | Result |
|---|---|---|---|---|
| A, direct reading | thinker | — | 8 min 10 s | ✓ correct root cause: `$lte` as `a < b` in `compare()`, `conditions.ts` |
| B, research | thinker | — | 11 min 35 s | ✗ blamed `resolveScope` in `ability.ts`; never looked at `conditions.ts` |
| C | Qwen3 Coder | Qwen3 Coder | 23 min 42 s | ✗ not done after 3 rounds |
| D | thinker | Qwen3 Coder | 21 min 53 s | ✗ not done after 3 rounds |
| E | thinker | thinker | **6 min 49 s** | ✓ done in round 1, all 4 checks pass |

- **A:** the outline made `conditions.ts` one of the 8 files picked (the old direct run had missed
  it). Thinking hit the 6,000-token budget at 7 min 21 s; the answer came 50 s later.
- **B:** round 1 asked for sensible reads, uses, greps and 2 Windows questions (1–2 min each), then
  round 2 said "enough" without checking how `can()` evaluates conditions. Research rounds run
  without thinking, so "enough" is a shallow call.
- **C:** the coder model as reviewer claimed the repo uses Jest (it uses Vitest); the coder then
  "fixed" working Vitest imports to Jest, and round 3 found tests broken by Jest imports. A wrong
  reviewer does real damage. Also picked too many files (5 survey parts, 7.5 min) for a small job.
- **D:** accurate reviews every round, but one call, `rule()` on line 73 (needs an argument),
  stayed broken: the coder reported it fixed twice and wasn't. The type checker had already named
  the exact line.
- **E:** 4 files read, plan in 4 min (no model swap), one coder session (103 s), checks and a 35 s
  review. Good tests: 8 cases over every branch; most assertions only check that one warning
  appears, not its text.

Changes made from this: planning and review never use the coder model; light jobs go to the
thinker (else the Mac). Promoted on the roadmap: plan skeletons from parsed check errors (D's
one-line fix), thinking plus hypotheses in research rounds (B), the thinker as default coder after
a few more confirming runs (E vs D).

### 13. The Mac advisor on the `$lte` bug (plan only, direct reading)

Same request as run A, with the Mac's 27B planning (`offload.planner: "advisor"`); file picking is a
chore, so the Windows thinker still did it.

| | Run A: Windows thinker | Run 13: Mac advisor (27B) |
|---|---|---|
| Time | 8 min 10 s | 15 min 1 s (thinking cut at its 2,500-token budget at 13 min) |
| Files picked | included `conditions.ts` | missed `conditions.ts` |
| Diagnosis | ✓ read the `<` in `compare()` | ✓ reasoned from the data: only rows with `priority === 5 === maxPriority` disagree, so `$lte` must act like `<` |
| Task | `conditions.ts`, exact change | `conditions.ts` (found through the outline), instructions hedged ("find where `$lte` is evaluated") |

The 27B is the stronger reasoner (it got there without the file) and about 2× slower on the work
machine. That fits its role: escalation for very hard or stuck jobs, not the default. Its memory was
released when the run ended (`unload_mac`). File picking varies between runs: the same Windows model
picked `conditions.ts` in run A but not here.

### 14. Confirmation runs with the new defaults (Windows thinker plans and codes)

| Run | Job | Time | Result |
|---|---|---|---|
| G | full fix of the planted `$lte` bug (heavy → research reading) | 18 min 50 s | ✗ failed at planning |
| H | "fix the type errors" after the `SubjectType` → `SubjectKind` rename | **10 min 54 s** | ✓ done, round 1 |
| I | add `canAny(action, subjects)` with tests | **6 min 39 s** | ✓ done, round 1 |

- **G:** research ran 3 rounds of exact lookups (no Windows questions needed), read the wrong part
  of `conditions.ts`, and blamed `resolveScope` again. After the thinking budget cut, the answer
  kept reasoning *inside the JSON* ("but then why isn't it working? Let me re-examine…") for 63,000
  characters until the 16,000-token output limit. Research reading is now 0/2 on this bug, direct
  2/2. Fixes: direct reading is the default; after a thinking cut the answer is told "no more
  reasoning, under 300 words per task" with an 8,000-token cap; a too-long answer gets one short
  retry instead of stopping the run.
- **H:** planned from the check errors with **no model, in 10 seconds** (7 tasks). The `imports` check
  put the 4 NestJS/TypeORM files that `tsc` can't see into the plan. All 4 checks passed after one
  round of coding. Compare: Sonnet took 37 s and $0.15 to plan the same job in run 5.
- **I:** the cleanest result so far: `canAny` matches `canAll`'s style exactly, and the tests include
  a real "false" case with two different denial reasons.

The escalation rules (advisor for unsure research, repeated coder failures, reviewer/check
disagreement, sensitive files; ladder to Claude where allowed; at most 2 per run) were built after
these runs and are unit-tested; none has fired in a live run yet.

### 15. Git assistant, first live use (castellan)

- "What's the CI status of PR 1 and PR 2?" → routed to the git assistant, two `pr_status` steps, 27 s,
  using the personal GitHub account via the folder rule. Showed 5 failing jobs on each.
- "Why does CI fail on PR 2?" was first routed as a *code question* (read 21 files for 9 min); then,
  routed correctly, the model refused because no operation could "display" logs. Two prompt fixes
  (CI/PR questions are git; every result is shown to the user) → `pr_failures` worked.
- The failures were infrastructure: the runners couldn't pull `postgres:17-alpine` from Docker Hub.
  Raw log tails were noise; now only `##[error]`/error lines with one line of context are shown.
- "Re-run their failed CI jobs" → 3 `ci_rerun` steps (marked ⇡), 30 s.

### 16. Speed day (2026-10-10): read less, then read faster

Same kind of job three times (add a git operation plus its test, in buckets itself, all on Windows):

| Run | Time | Rounds | Notes |
|---|---|---|---|
| fix #2, whole-file reading | 23+ min, stopped | 2+ | wrong function edited; the coder re-read 4,400 lines |
| fix #2, outline + exact parts | 6 min 15 s | 1 | |
| fix #3, + planned edits | 9 min 48 s | 2 | an edit anchor missed by one character; the test task was dropped |
| `log` op, + append edits, named-file check, project map | **4 min 37 s** | 1 | both edits applied with no coder; 27 s of it was the model loading after boot |

Where reading time went before: the worker reads prompts at ~200 tokens/s, so a 13k-token file
picker cost 66 s and a 45k-token whole-file survey 5 min. Thinking was capped at 1,500 tokens for
light jobs (~40 s); it hit the cap every time.

A 4B (`qwen3:4b-instruct-2507`, 2.5 GB, fully on the RTX 3070) replayed today's real chore prompts:
~3,000 tokens/s reading (14× the 35B), but routing agreed 13/17, git steps 1/7 (it invented steps),
file picking in castellan 25–70% overlap. It can't stay loaded beside the 35B (4 GB + 4.5 GB > 8 GB);
the 35B reloads in ~30 s after each swap, and giving the 35B fewer card layers so both fit cut its
writing from 38 to 12 tokens/s. Kept installed, unused.

llama.cpp's server with the 35B's experts in RAM (`--n-cpu-moe 41`) and 4,096-token chunks read the
`llama-bench` 8k prompt at 1,067 tokens/s (Ollama 197); 2,048-token chunks 674–752; 512 (the
default) 233–271. `--n-cpu-moe 28` overflowed the card: 65 tokens/s. On a real buckets plan prompt
(8.4k tokens) the server reads ~650 tokens/s and writes ~35; a one-file opencode edit took ~20 s.
Plain `-ngl 8` (whole layers on the card, like a naive split) wrote at 12.8 tokens/s, so Ollama was
already smarter than that.

### 17. A 2,000-file React Native monorepo (ecotrak-mobile), local vs Claude (2026-10-10)

ecotrak-mobile: 2,123 files (1,400 TypeScript) in `apps/` and `packages/`, with the backend
(`ecotrak-api`, NestJS, 3,182 files) next to it. Checks set in the config's `repos` rule: `tsc` (10 s),
eslint errors only (34 s), jest (1,545 tests, 6 s), all in `packages/shared`. Every change ran on a
throwaway `buckets/…` branch; `main` was never touched.

| Test | Planner | Time | Result |
|---|---|---|---|
| "Does work order details have a created date / created at field?" | local | 1m 55s → **1m 45s** | wrong files, "can't confirm" → **correct** (`work-order-details.ts:108 createdAt`) after the request-words search |
| Hide 2 rows, vague ("the work order details") | local | 13m 17s → 11m 12s → **4m 55s** | detour via an invented file → half-done while the review said done → **clean 24-line diff** |
| Hide 2 rows, files named | local | 5m 01s | correct (older code, without the coder focus fixes) |
| 10 `react-hooks/exhaustive-deps` warnings (`time-track`) | local | 15 min cap | 10 → 1 warnings, but **changed behaviour** (an on-mount fetch now re-runs on date changes; an effect resets on data loads). Not mergeable |
| "Show createdAt as a row in work order details, both apps" | local | 20 min cap | not done: invented a third hook file, type and import errors, the Mac advisor too slow to help |
| Same | **Claude** (Sonnet plans and reviews) | **1m 59s, $0.51** | done; reused `utils/details-dates.ts` (next to `formatWorkOrderCompletedDate`), all 3 edits applied with no coder |
| "Pick a search param missing from the filters and add it" | **Claude** | 8m 11s, $1.19 | works end to end (Project), **but slot 32 collides with the team filter** (`work-order-sp-name-filter.ts`); every check passed |
| Same, backend readable (`related`) | **Claude** | 5m 21s, $1.07 | correct: slot 33, reuses the projects dropdown, typed. The plan never mentions the backend: the better result was most likely run-to-run variation |
| `buckets review` of the slot-32 branch | **Claude** | **36 s, $0.28** | **caught the collision** with both locations, plus a type mismatch, a possible permission gate and the missing locales (from AGENTS.md), and a manual test plan |

What the first runs got wrong, and the fix each led to:
- File picking chose the API service and the store, never the screens: **request words → paths
  and code** (`work order details` → `work-order-details/`, `created at` → `createdAt`), ~1 s.
- A 1,500-line screen is one function, so "the part it needs" was its first 150 lines: **the
  stretches around the request's rarer terms** instead.
- The planner's edits named the file's path as their anchor; a coder searched 17 times (6.5 min)
  and removed half a row: **coders get the lines their quoted text is on**; path anchors dropped.
- The review said done with "Tracking Code" still in the file: **the review gets where the
  request's phrases still appear**, found mechanically.
- The auto-detected `lint` check was `eslint --fix && prettier` in each workspace (it would rewrite
  files): **checks that write aren't detected**. `--doctor` ignored config `repos` rules.
- Prettier on a never-formatted 1,800-line file would reformat all of it; keeping only "nearby"
  changes misaligned and **lost code** in a throwaway copy, so it was dropped: **formatters run only
  on files that were formatted at the start**, and leftover blank lines are removed mechanically.
- An "API change" on every run: an interface and a const share the name `HomeHeader`.
- AGENTS.md is 70k characters and buckets read its first 6k: **long agent docs and matching skill
  docs are cut to the sections that mention the request**.

Speeds: the worker reads ~650 tokens/s on llama.cpp (see 16); the request-words search costs
~1 s in one repo and ~8 s with the 3,182-file backend as a related repo.

### 18. The Mac as a second 35B, and who should plan (2026-10-10 night)

The Windows laptop's 35B file (21.7 GB, Ollama's GGUF) was copied to the Mac and registered in its
Ollama with the same settings (`RENDERER`/`PARSER qwen3.5`, `draft_num_predict 2`: Ollama already
uses the model's multi-token-prediction layers, which llama.cpp leaves unused).

| | Reads | Writes | Notes |
|---|---|---|---|
| Mac M2 Pro, 35B-A3B (Ollama, Metal) | ~475 tok/s | **~48 tok/s** | 100% on the GPU even at 64k context; loads in ~10 s |
| Windows, 35B-A3B (llama.cpp, experts in RAM) | ~650 tok/s | ~35 tok/s | |
| Mac, 27B dense (the old advisor) | ~85 tok/s | ~9 tok/s | |

The copy itself: laptop → Mac ran at ~27 KB/s on every port tried (8090, 11434, 8080) while ping
was 3–8 ms, Mac → laptop ~5 MB/s, and the Mac's internet ~2 MB/s. Not the Mac's VPN (V2Box), not
MTU 1280 → 1500, not Large Send Offload; SMB (port 445) with a local sharing user ran at
30–40 MB/s. Something on Windows slows its other outgoing ports; buckets' heavy traffic is
Mac → laptop (prompts), so it's barely affected, but it's worth finding.

A/B, the same request on buckets (add a read-only `tags` git operation with `{count?}`):

| | A: Windows plans and reviews | B: the Mac plans and reviews (Windows reads and codes) |
|---|---|---|
| Time | 4m 32s, 1 round | 4m 41s, 2 rounds (the "tests before" cached) |
| Result | **wrong and dangerous**: `git tag … 10` creates a tag named "10"; the review said done | **correct**: lists, then keeps the newest `count`; tests default and custom count |
| Planning (think + answer) | 58 s + 35 s | 48 s + 23 s |
| Choosing parts | 16 s on Windows | 32 s on the Mac (now moved to the reader) |

Same model on both, so one sample doesn't make the Mac "smarter"; what it shows is that two
independent reviews of one change disagree, and the disagreement is where the bug was. Both tests
only mocked `git`, which is how A's bug passed. Made the default: the Mac plans and reviews,
Windows routes, reads, chooses parts and codes; the Mac's model is unloaded after each run.

### 19. Look-ups while planning, and a review that broke correct code (2026-10-11)

Planning became a conversation with tools: `read` and `search` answered here, exactly; `ask` sent to
Windows, which reads the named files and answers with `path:line` (real lines attached). The same
request three times on buckets ("make `buckets review` say which run it's reviewing, add a test"):

| Attempt | Setup | Time | Result |
|---|---|---|---|
| 1 | old single-shot planner (look-ups not loaded) | 9m 44s, 3 rounds | **not done**: edited `Buckets.review()` instead of `cmd_review`; the test never passed |
| 2 | look-ups (4 reads, 1 ask; Windows answered in 12 s) | 14m 30s, 2 rounds | round 1 **correct** at 3m 07s; then the review claimed `d` was undefined (it was set above the hunk), round 2 rewrote it to read an empty run folder and mocked the test to pass. **Wrong** |
| 3 | + review sees whole changed functions, + loop check ignores quoted code | **4m 58s, 1 round** | **correct** (`d["request"][:100]`), real test; merged |

Two causes in attempt 2: the loop detector cut the review's thinking at 400 tokens because it quoted
the same line of code twice, and the diff's three context lines didn't show where `d` was set.
Attempt 3's planner didn't need a look-up (the first read already had `cmd_review`), so the
look-ups' own effect is shown by attempt 2's round 1, not attempt 3. A double review with an
"evidence or it didn't happen" rule for "undefined"-style claims is the next guard.

### 20. MTP and caching on the Windows server (2026-10-11)

Start script now has `--spec-type draft-mtp --spec-draft-n-max 2` and no `--no-reasoning-preserve`
(llama.cpp b11541). Same saved plan prompt (8.4k tokens, nonce in the system prompt, thinking off):

| | reads | writes | MTP drafts accepted |
|---|---|---|---|
| before (field notes 16, 18) | ~650 tok/s | ~35 tok/s | — |
| MTP, 2 drafts | ~625 tok/s | **39–42 tok/s** | 96–97% |

Writing is ~15% faster, not the ~2x the acceptance rate suggests: with the experts in RAM, checking
3 tokens per step pulls in more experts per step. MTP can't be turned off per request (the server
ignores `speculative.*` in the body), so the "before" is the earlier measurement.

Caching across a tool conversation works: turn 2 (turn 1 + the tool call + its result) reused
3,377 of 3,438 tokens and read 61 new ones; echoing the reasoning back made no difference (3,498
cached, 31 new). So the planner's look-up rounds on Windows reread almost nothing.

### 21. Double review, on the request one review got wrong (2026-10-11)

The `tags` request from field notes 18 again (a scratch clone of buckets with `op_tags` removed;
the Mac plans, both machines review each round at once, plus the mechanical weak-test list):

| | Time | Rounds | Result |
|---|---|---|---|
| A, 18 (Windows plans and reviews alone) | 4m 32s | 1 | **wrong**: `git tag … 10` creates a tag named "10"; review said done |
| B, 18 (the Mac alone) | 4m 41s | 2 | correct |
| **Double review** | 6m 14s | 2 | **correct**: lists, keeps the newest `count` (checked on a repo with 12 tags; no tag created); tests check the returned values |

- Round 1: both reviewers said not done for the same reason: `test_op_tags` only asserted that the
  mocked `git` was called, which is what the weak-test list flagged ("all 1 assertion(s) only check that
  mocks were called"). Round 2 added value assertions; both said done.
- Cost of the second look: the two reviews ran at once (27 s and 22 s of answer after 1,500 tokens of
  thinking each); the round took ~15–20 s longer than the Mac alone would have.
- Every edit, including round 2's, was applied straight from the plan (0 s of coder time).
- They never disagreed here, so the confirm step didn't run; the unit tests cover it (a false
  "undefined" claim is dismissed by the reviewer who said done, a confirmed one wins).

## Lessons so far

- A wrong review is worse than none: one false "undefined variable" turned a 3-minute success into a
  broken result. Reviews need the whole changed functions, and claims checks can't confirm need a second look.
- Two looks at the same change catch more than one: the review that caught a dangerous bug was the
  second, independent one (field notes 18). Mocked tests only check which arguments were passed.
- On a big repo, time follows how much code a change touches, not the repo's size, once the
  request's own words are searched mechanically (a vague request: 13 min → 5 min, the same as
  naming the files).
- Local planning is reliable for finding, answering, and changes in places it can find; it is not
  for changes that must fit existing patterns across files, or judgment calls like hook
  dependencies. Claude planning did those in 2–8 min for $0.50–1.20, with the local side still
  reading, editing and checking.
- Checks can't see design mistakes (a reused filter slot passed types, lint and tests). A Claude
  review that reads the code around the change caught it for $0.28.
- The coder is the cheap part either way; mode only changes who plans and reviews.
- The Mac planner's time goes to reading new code (~85 tokens/s) and thinking (~9 tokens/s).
  Name the files in the request, and use `--plan-only` before long runs.
- Thinking produced a much more concrete plan than no thinking, and concrete plans are what
  let the 30B coder succeed.
- Keep deterministic checks in charge: tests, `tsc` and lint decide "done", not the reviewer.
