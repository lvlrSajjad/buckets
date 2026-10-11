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

### 17. A 2,000-file React Native monorepo (the work repo), local vs Claude (2026-10-10)

the work repo: 2,123 files (1,400 TypeScript) in `apps/` and `packages/`, with the backend
(`backend-api`, NestJS, 3,182 files) next to it. Checks set in the config's `repos` rule: `tsc` (10 s),
eslint errors only (34 s), jest (1,545 tests, 6 s), all in `packages/shared`. Every change ran on a
throwaway `buckets/…` branch; `main` was never touched.

| Test | Planner | Time | Result |
|---|---|---|---|
| "Does order details have a created date / created at field?" | local | 1m 55s → **1m 45s** | wrong files, "can't confirm" → **correct** (`details-screen.ts:108 a date field`) after the request-words search |
| Hide 2 rows, vague ("the order details") | local | 13m 17s → 11m 12s → **4m 55s** | detour via an invented file → half-done while the review said done → **clean 24-line diff** |
| Hide 2 rows, files named | local | 5m 01s | correct (older code, without the coder focus fixes) |
| 10 `react-hooks/exhaustive-deps` warnings (one module) | local | 15 min cap | 10 → 1 warnings, but **changed behaviour** (an on-mount fetch now re-runs on date changes; an effect resets on data loads). Not mergeable |
| "Show a date field as a row in order details, both apps" | local | 20 min cap | not done: invented a third hook file, type and import errors, the Mac advisor too slow to help |
| Same | **Claude** (Sonnet plans and reviews) | **1m 59s, $0.51** | done; reused `utils/details-dates.ts` (next to `formatWorkOrderCompletedDate`), all 3 edits applied with no coder |
| "Pick a search param missing from the filters and add it" | **Claude** | 8m 11s, $1.19 | works end to end (Project), **but slot 32 collides with another filter** (`filter-sp-name-filter.ts`); every check passed |
| Same, backend readable (`related`) | **Claude** | 5m 21s, $1.07 | correct: slot 33, reuses the projects dropdown, typed. The plan never mentions the backend: the better result was most likely run-to-run variation |
| `buckets review` of the slot-32 branch | **Claude** | **36 s, $0.28** | **caught the collision** with both locations, plus a type mismatch, a possible permission gate and the missing locales (from AGENTS.md), and a manual test plan |

What the first runs got wrong, and the fix each led to:
- File picking chose the API service and the store, never the screens: **request words → paths
  and code** (`order details` → `details-screen/`, `created at` → `a date field`), ~1 s.
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

### 22. Big-file refactors by moving code, not retyping it (2026-10-11 night)

The owner's request on the work repo: split `big-screen.tsx` (3,687 lines; ~1,450
lines of state, refs, effects, a save queue, validation, submit, pagination and attachment handlers
inside one component) by moving the logic into a `useBigScreen` hook, keeping the render
helpers and JSX in the screen.

| Run | What happened | Time | Result |
|---|---|---|---|
| Baseline (retyping) | plan in 4m 13s (a good inventory, but missing `a save function`, inventing an import path for a local helper, a circular import); the Windows coder retyped the hook for 15 min until the connection dropped | 20 min cap | **nothing written** |
| `ts_moves.cjs extract` by hand | TypeScript's scoping picks the statements, the parameters (`navigation`), the 56 returned names, the 12 private helpers that must come along (5 exported back) and the imports; the lines move byte for byte | **2.8 s, no model** | `tsc`, eslint, all 1,545 jest tests pass; eslint warnings identical (12 → 4 + 8, just split) |
| buckets with `move` (runs 1–5) | the local planner ignored the new rule in a 26k prompt (1), then chose `move` but put its fields in the text (2, crashed), then the guard switched a retyping plan to the move (3); false "shortcut" alarms for `any`s that only moved (36, then 16, then 7: per-file counting, Prettier dropping `;`, Prettier joining lines, the tool's `export`, the generated `navigation: any`), and a redundant "update the screen" task that spent 9 min changing nothing (4) | | each fixed in turn |
| **buckets with `move` (run 6)** | planner chose `move` itself (the dry-run suggestion in its facts), the redundant task dropped, move 3 s, checks pass, both reviews "done" | **5m 14s** | **correct**: verified in a worktree (`tsc`, eslint, 1,545 tests) |

Same idea for the drifted app-a / app-b screens: `compare` pairs declarations (pair-1:
`an input component`, `a modal component`, `FULL`, `a module-level `let`` identical; pair-3's hook shares ten
number helpers, the screen is 72% alike); `share` moved pair-1' four into `pair-1-shared.tsx`
(245 duplicate lines gone) in 2.3 s with every check passing.

Lessons: the model is good at deciding what goes where and bad at copying 1,500 lines; give it a move
and it needs only to choose it. A long prompt's rule isn't enough for the local planner: a dry run in
its facts plus a guard is. Checks that compare lines must expect code to move between files and be
reformatted on the way.

### 23. Merging the drifted app-a / app-b variants (2026-10-11 night)

Ladder step 1, pair-1 (1,563 + 718 lines): "merge the duplicated code of the two variants into
`pair-1-shared.tsx`, behaviour unchanged". Ten live runs; each failure was fixed before the next:

| Runs | What went wrong | Fix |
|---|---|---|
| 1–4 | the planner added edits to both screens next to the share (they'd redo it, or spend minutes changing nothing); the "every named file has a task" check put dropped tasks back; one bogus `move` with no destination crashed the plan | edits next to a move/share go even with exact code; files a move/share covers count as planned; a task that can't run is dropped, not fatal |
| 5–6 | both reviewers wanted locale entries for strings that only moved (once because the formatter joined `<Text>` onto one line) | moved strings aren't new gaps; reviews are told moved code is judged as a move, its old problems out of scope |
| 7 | only `FULL` moved: the new module-state rule refused `a module-level `let`` even though everything assigning it moves too | module state moves when all its assigners move |
| 8 | the reviews disagreed (first time): Windows wanted the hook in the named file and worried about the shared `let` | the hook goes into the named shared file, imports merged into one line; shared module state is stated in the note |
| 9 | the guard swapped in the share but kept the redundant edits | the swap re-runs the drop |
| **10** | | **5m 38s, both reviews done; verified: 342 duplicate lines out of the screens, a 170-line shared file, `tsc`, eslint, 1,545 tests pass** |

What the share does now: identical top-level declarations (a style constant, a module-level `let`, an input
component and a modal component) into the shared file; identical statements inside both components into
`usePair1Shared` in the same file (only those both variants can move with the same
statement: 4 of 16; values from `useStores()` and store destructures are re-derived inside the hook, not
passed); destructured names the move left unused, and their imports, removed. Planning fell from 3–6 min
to 2m 54s once long files a move covers were shown as outlines instead of read.

pair-2, ladder step 2, live: run 1's planner wrote the shared file into a task's edits (retyping), which the guard didn't swap because the task had edits; fixed (a task writing the move's target is swapped either way). **Run 2: 4m 52s, both reviews done**; verified: 142 duplicate lines out of the screens into a 72-line shared file, `tsc`, eslint, 1,545 tests pass. The three module-level `let`s the screens assign stay.

pair-3, ladder step 3 (the pair Claude struggles with; the hook is only 52% alike), live: run 1's
reviews disagreed: Windows objected to the shared module refs, a "redundant" `useStores()` and a "hook
ordering risk" ("while valid"); none a bug, but any issue won. Fixed: when every check passes and one
reviewer objects, the other weighs each issue (style and hypothetical risks don't count, nor claims the
checks rule out). **Run 2: 5m 05s, both reviews done**; verified: 117 duplicate lines out of the two hooks into
a 52-line shared file (10 identical statements in a shared hook; its ten identical number "helpers" are
module `let`s the hook assigns, rightly kept), `tsc`, eslint, 1,545 tests pass. What's left in this pair is
near copies and real differences: the model's part (next).

Survey of every variant pair's small near copies (`compare`, diff ≤ 4 lines): most are **real differences**
that must stay apart (filter heights 88 vs 138 and 100 vs 150; `isInternalTech &&` on a permission check;
`create(true)` vs `create()`; extra branches for copied records); some are
**incidental** (`Linking.openURL(url ?? "")` vs `(url)`, `record?.id ?? -1`, `useCallback` vs a plain
function, optional chaining). Telling them apart is the model's part; `unify` then copies the chosen
version exactly. Found on the way: leftover debug logging in one variant (details in the private notes).

Questions for the owner: a module-level `let` is now one variable for both variants (each app ships one,
so nothing changes per app); and the 12 pair-1 statements that differ slightly are where the model's help
is needed next (make them identical, then share).

### 24. Near copies: the model picks, buckets copies (`unify`), then shares (2026-10-11 early morning)

pair-4, "merge … where a near copy differs only incidentally, use the safer version and share
it; keep real differences". The local planner judged the two near copies right: `a handler` takes
app B's version (`Linking.openURL(url ?? "")`, null-safe); `another handler` differs only in quote
style. It wrote them as edits; they were dropped as "redoing the share" (run 1), so now an edit that makes
a near copy match the other variant becomes a `unify` (copied exactly) and runs before the share (run 2:
both unified in 2 s each, then shared into `usePair4Shared`).

Run 2 then showed a real gap: the hook call landed after `if (!record) return` (lint's rules-of-hooks
caught it). Round 2's model moved the call up, which also moved a destructure of `record` above the null
check (both reviewers caught the crash); round 3 broke `tsc`. Not done after 3 rounds. Fix in the tool: a
hook call goes before an early return only when what moves past it just declares functions and its inputs
are set by then; values set after the return are never re-derived inside the hook; otherwise the share
refuses the hook with the reason ("it would need a value, set after the early return at line 256") and
does the safe part (the unifies and `FULL`), with every check passing.

Runs 3–4: the planner chose `unify` itself but with broken paths (now filled from the other variant), the share ran before the unifies (now always after), review follow-ups weren't filled (now are). **Run 4: 4m 43s, both reviews done**; verified: `a handler` unified to the null-safe version, `another handler` to one quote style, `FULL` shared; the two unified functions stay in each screen (the hook would need a value, set after the early return); `tsc`, eslint, 1,545 tests pass.

Lessons: the local model is good at the judgment (incidental vs real) and poor at repairing a structural
mistake across rounds (each fix introduced the next problem); the tool has to refuse what it can't do
safely rather than leave it to follow-up rounds.

### 25. Tier 1: follow-ups, undo, round rollback, clean stops (2026-10-11)

Live in a scratch clone of buckets:
- **Time limit:** `--time-limit 0.5` stopped a run at 0:44 (mid-planning), exit 130, back on `main`, no branch left.
- **Follow-ups:** run 1 added the `tags` operation (3m 44s, unmerged); run 2, "now make op_tags default to 5
  instead of 10", built on run 1's branch. The first try decided after planning, so the planner read `main`,
  didn't see `op_tags` and added it again (duplicates); now the switch happens before the checks and the
  reading. Run 2 again: **3m 24s, done; the diff on top of run 1 is exactly 10 → 5 and its test.**
- **Merge and undo:** `merge` brought in both runs and deleted run 1's branch; `undo` moved `main` back to the
  exact commit before and kept the work as `…-undone`.
- Round rollback and diff-checked reviews are covered by unit tests; memory outside the repo is set for the
  work repo (`"memory": "outside"`), so its runs stop creating `.buckets/`.

### 26. Move to a util, and copies that drifted (2026-10-11)

A real case in the work repo: two small time-formatting helpers in a screen folder's helper file, which
nothing imported, while two screens each kept their own copy of one of them. Request: move them into the
shared date utils and update everything that uses them, copies included, behaviour unchanged.

| Run | What happened | Result |
|---|---|---|
| 1 | `relocate` dropped (the planner left out the target); the reviewers' hand edits broke `tsc` | stopped |
| 2 | relocate 3 s; `dedupe` rightly refused (the copies differ); round 3's coder deleted the copies by hand and **both reviewers said done**, but the copies guarded empty input and the moved version didn't: an empty value would now show `NaNh NaNm 0s` | **wrong**; caught in verification, never merged |
| 3–5 | the differences shown to the reviewers; a removed differing copy flagged as a behaviour change; dedupe aimed at the wrong file; an emptied helper file the coder couldn't delete; stale evidence once the shared version was fixed; the confirm step losing its answer when its thinking ran out | each fixed |
| **6** | relocate (3 s), dedupe shows the difference, the planner adds the guard to the shared version, **dedupe replaces both copies mechanically** (now equivalent), emptied helper deleted | **7m 09s, both reviews done; verified**: the new version behaves like both original copies on 12 inputs, `tsc`, eslint, all tests pass, 66 lines out |

What made it work: **running the code instead of reading it.** For small pure functions, `dedupe` compiles
both versions and runs them on sample inputs from their parameter types (empty and odd strings, edge
numbers, null/undefined): equivalent copies are replaced even when written differently, and otherwise the
first differing input is shown (`("") gives "NaNh NaNm 0s" … "0h 0m 0s"`). That turned a review argument
into a fact, and it is checked again at review time against the code as it is then.

Lessons: two confident reviewers can both miss a behaviour change that the checks don't cover; a
mechanical behaviour comparison catches it. Evidence must be recomputed when the code it describes changes.

### 27. A parameter added everywhere (`signature`, 2026-10-11)

In the work repo, a widely used message helper (37 calls in 19 files): "add a duration parameter (default
2000), use it instead of the fixed value, and pass 3000 from every call".

| Run | What happened | Fix |
|---|---|---|
| 1, 3 | the planner chose `signature` itself; the values came JSX-style (`{3000}`), then labelled (`default: 2000`), then as `number = 2000`, each breaking every call | values unwrapped and unlabelled; values and types that don't parse are refused before anything is written |
| 2 | the planner hand-edited the first 8 of 19 files instead | a plan that hand-edits 3+ call-site files is asked once more for the single signature task |
| 4 | right, but the edit helper had also collapsed 90 of the files' own blank lines | blank lines tidied only where a removal left extra ones |
| **7** | signature task (6 s for 37 calls), lint flags the parameter as unused, round 2 makes the one-line body change | **8m 57s, both reviews done; verified: all 37 calls pass 3000, `tsc`, eslint over the app, all tests pass** |

Two smaller cases (a prop added to a component used in 4 places; a prop passed only from one screen's two
uses) were planned as plain edits, which was right: the tool is for changes that repeat across many call
sites, and plain edits did those in 3m 47s and 7m 03s.

### 28. Part of a screen into a component (`component`, 2026-10-11)

On the work repo's 3,687-line screen, every render helper tried mechanically: **12 of 13 become components
with `tsc`, eslint and all tests passing** (props: the helper's parameters plus the screen values it uses,
2 to 18 of them; context values re-derived inside; module state the screen assigns passed as a prop; types
the props mention imported). The 13th is refused: a private helper that would move along reads module state
the screen keeps.

Live, "turn the reference image part of the screen into a component in its own file": run 1 picked the wrong
helper (words in the file path matched; the real helper's name is misspelled in the code); run 2's reviewers
flagged the generated `navigation: any` (Prettier had split the signature, so the generated-line exemption
missed it); run 3's reviewers objected to the inherited `any` and to a moved interface's new home. **Run 4:
4m 38s, both reviews done; verified: 8 uses render the new component, `tsc`, eslint, all tests pass.**

Also found: an unattended stop had put a half-done task in the work repo's stash list. Stops now commit it on
buckets' own branch instead; the stash was dropped, so the repo is as found.

### 29. A regression suite, and opencode vs a native coder (2026-10-11)

`regress.py` replays real requests on a clone it owns and checks the result (files, text, line counts, where
changes land, tsc/eslint/jest, an answer's names). Its first run stopped itself: a coder session had edited the
source repo instead of the clone. The cause took two tries to find. opencode takes its folder from `$PWD`, not
from its working directory (checked: started in folder A with `PWD=B`, it writes into B), and the suite ran
buckets from another folder. In normal use buckets runs inside the repo, so `$PWD` was right by luck. Fixed by
setting `PWD` (and a data folder per repo path) for every opencode session.

A/B on a fixed two-task plan (hide two rows in both variants of a screen; the planner's exact edits removed so
the coder does both), two runs each:

| Coder | Passed | Run time (median) | Coder time (median) | Model calls |
|---|---|---|---|---|
| opencode | 2/2 | 2.5 min | 76 s | 2 sessions |
| native loop (read/search/edit/write) | 2/2 | 2.2 min | 63 s | 11–16 short turns |

Close: the native loop is a little faster on a small task and just as right. Not enough to switch the
default; it needs the larger cases (several tasks, files to search) and more runs.

### 30. Checks on the other machine: measured, and not worth it here (2026-10-11)

The buckets agent (`tools/windows-agent.cjs`) runs on the Windows machine: `buckets win` runs commands there,
and buckets can keep a copy of a repo there (git bundles; the first copy of a 2,000-file app repo took 12.5 min,
mostly installing dependencies) and run checks in it, compared with the Mac's ("shadow") or instead of them
("split"). The numbers on this pair of machines, same commit:

| Check | Mac (M2 Pro) | Windows (the 35B's experts in its RAM) |
|---|---|---|
| tsc | 10 s | 24.5 s |
| eslint | 35 s | 124 s |
| jest (1,545 tests) | 8 s, all pass | 123 s, 2 fail (tests that read files by path) |

And the model paid for it: llama-server's resident memory went from 23.3 GB to 21.1 GB while the checks ran (Windows
trimmed the model to make room), so the next requests read those parts back from disk. With the model's experts
in RAM there's about 3 GB and little CPU left on Windows. Checks stay on the Mac (`agent.checks` is off); shadow and
split are kept for a machine with room to spare. The remote commands are the useful part here.

### 31. Two ideas from how a brain works, measured: one kept, one parked (2026-10-11)

Roadmap tier 6 came from picturing buckets as a brain. Before building, the run history (152 runs) was checked
for where each idea would actually bite.

**Reflexes (item 30), kept.** The idea was mechanical fixers before a model round. The history said otherwise:
only ~23 rounds failed their checks after coding, and almost none on something a fixer clears (44 "cannot find
name" errors were undefined variables in one run, not missing imports; `no-unused-vars` isn't auto-fixable). What
did repeat: **the planner's own exact edits, pasted mechanically, leaving a .tsx file that doesn't parse** (three
rounds that day, e.g. a stray `{` after a JSX opening tag). Python files already had a parse check there; TS/JS
didn't. Now `ts_moves.cjs syntax` (parse only, ~0.15 s) guards planned edits (they go to the coder instead), the
native coder's edit tool, and any coder session (one more try with the exact lines). On that real plan, replayed:

| Case (2 runs each) | Before | After |
|---|---|---|
| the planner's plan with the stray `{` | 2/2, 7.2 min: both reviews found it, a second round fixed it | 2/2, **3.8 min**, one round |
| the same plan without it | 2/2, 2.5 min | 2/2, 1.7 min (mostly the first run loading the model) |

The full work-repo suite on it: the same 5 cases pass and the same 4 fail as before (none from this; the reflex
didn't fire in any of them, as expected for something this rare).

**Surprises only (item 29), parked** on branch `park/predictive-review`. The planner's review left out files that
went in exactly as it wrote them (commits marked, checks naming none of them), and when everything went in that
way with every check green, only the second reviewer looked. It worked as built and saved little: the prompt
shrank by ~1,000 tokens (~2 s of reading), and the full skip rightly didn't fire because other callers were
deliberately left on the old signature. A review's time isn't reading, it's thinking:

| | Calls (all runs) | Thought until the budget cut it off |
|---|---|---|
| Planner reviews (Mac) | 95 | 42 (44%) |
| Second reviews (Windows) | 43 | 29 (67%) |
| Plans | 120 | 26 (22%) |

A cut-off review thinks ~60–80 s, then needs another call to answer (10–20 s). That is the bigger lever (roadmap
29b). Item 29 makes more sense in a tree of many machines, where each level has to compress what it passes up.

### 32. The night's toolbox: fold, refs, chunks, and routes read from the code (2026-10-12)

- **Fold** (identical variant files → one file in the parent folder, importers repointed by TypeScript's
  file-rename edits). On the work repo the inventory listed 6 "identical" pairs; 5 were per-app config files
  (bundler, linter, env, a `.d.ts`) that tools find by location and nothing imports. Folding those would have
  broken the builds, so fold refuses a pair nothing imports. One real pair is left (a navigator, folded in a
  34 s dry run, type-checked). Byte-identical copies can still be different code: when their relative imports
  point at their own siblings, the moved texts differ, and fold refuses.
- **Module state into refs** (the owner's call: `useRef`, not module variables). On four real screens a dry run
  converted 1–16 variables each and left exported ones and ones shared by two components, each with the reason.
  Shares and moves now do this first, so a merge shares refs in the hook instead of one module variable that
  both variants write. The report says what changes: a ref starts again on each mount.
- **A JSX chunk by lines → a component**: the chunk becomes a typed render helper in memory, then the existing
  component op runs. A `key` on its root moves to the new element.
- **Contract from the code**: the backend's exported swagger was 2.5 months stale. Reading the NestJS
  controllers and DTOs instead took 7 s, and the results went from 292 fitting calls of 337 to 310. "Missing
  required" findings dropped from 15 to 0: the swagger had marked named query params required. Most of what's
  left looks real (routes since removed or renamed). Two were matcher limits, both fixed: words compared with
  case, and an unresolved part that couldn't match a literal route.
- **The coder A/B, both halves** (frozen code from before tonight's fixes; one run each): opencode 8/12, native
  10/12. Most of the gap is the planner's run-to-run variation in cases where no coder ran at all. On the tasks both
  coders did, the results and times matched: hiding two fields took 94 s vs 90 s, a new prop 42 s vs 39 s. Native
  failed one case that opencode passed: its planner added an unasked-for file, and the round tidying it up broke
  lint. The native coder is now the default (nothing outside buckets, no `$PWD` class of bug), with opencode for
  the last try when a native try changes nothing.
- **The opencode half, read closely**: 8 of 12. Two of the failures were buckets bugs, now fixed. The planner
  dropped the new parameter's default and the value every call should pass, then three rounds hand-edited 37
  calls in 18 min (the request's words fill those fields now). And one plan overflowed the planner's context.

## Lessons so far

- Run it, don't argue about it: comparing two versions of a small function on sample inputs settles
  "does this change behaviour?" where two reviewers disagreed or both missed it (field notes 26).

- Moving code is a job for a tool, not a model: a 1,450-line split failed after 20 min of retyping and took 3 s
  as a mechanical move (field notes 22). The model decides what goes where; buckets moves it.

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
