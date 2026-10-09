# buckets

Empty the pool with many small buckets. `buckets` is a local orchestrator for two
machines: a **planner** model reads the repo, splits the job into one-file tasks and
reviews the result, and a **coder** model runs each task in its own opencode session.

The script owns the control flow, git and the checks. The models only make
judgments, and always answer in JSON that the script validates.

## Daily start (2 minutes)

First time? See "Set up on your own machines" below, then come back here.

1. **Worker machine** (the one running the models, if it isn't this one): turn it on and make
   sure Ollama is running there. Leave it plugged in.
2. **This machine:** make sure Ollama is running if the advisor lives here.
3. **Check everything is ready:**
   ```bash
   buckets --doctor
   ```
   Every line should say `ok`. If one says `BAD`, see "When something's wrong" below.
4. **Go to a repo and say what you want, in plain words:**
   ```bash
   cd ~/code/my-repo
   buckets "add tests for lint.ts"
   ```
   It prints what it understood (question, change or cherry-pick; local or Claude) and starts
   after 5 seconds. Questions get a report and change nothing. Changes need a clean working
   tree (commit or stash first) and happen on a new `buckets/…` branch.
5. **Do something else.** Local runs take 10–30 minutes; Claude-mode runs take a few. You'll get
   a desktop notification when it ends (macOS). Along the way it shows numbered steps, what the coder is
   doing right now ("editing ability.ts"), and a running clock on every wait.
6. **Review, then keep or throw away the work:**
   ```bash
   buckets last       # the latest report again
   buckets merge      # bring the branch into the branch you started from (fast-forward only)
   buckets discard    # delete the branch (asks first)
   ```

Writing good requests:
- Say **what** and **where**: "add `canAll` to `Ability` in `packages/core/src/ability.ts`" beats
  "add a canAll method". Naming files saves the planner most of its reading time.
- Point at an example when one exists: "like the TypeORM adapter".
- For anything big, ask for the plan first: `buckets --plan-only "…"`.
- Repos under `~/Coding/ME/` always run locally (free). Elsewhere, heavy jobs go to Claude
  automatically. Add `--claude` to force Claude, or `--local` to keep a job free.

### When something's wrong

| `--doctor` says | Do this |
|---|---|
| `BAD coder server` | The worker machine is asleep, off, or on another network. Wake it; check Ollama is running there. |
| `BAD planner server` | Start Ollama on the advisor's machine (or rerun `buckets setup` without an advisor). |
| `BAD … model …` | Pull it on the machine `--doctor` names: `ollama pull <model>`. |
| `BAD claude CLI logged in` | Run the `auth login` command it prints. Only needed for `--claude`. |
| `BAD opencode agent` | Run `buckets setup -y` (installs the agents), or run `--doctor` again if opencode's service was starting. |

If a run fails, its folder (printed at the start, under `~/.local/state/buckets/runs/`)
has the plan, every model call and every coder session's log.

## How it works

```
request ─▶ classify ─┬─ info ─▶ pick files ─▶ (survey in batches) ─▶ report
                     │
                     └─ code ─▶ baseline checks ─▶ pick files ─▶ (survey) ─▶ plan
                                   ▲                                         │
                                   │            one opencode session per task, one file each
                                   │            (commit after each; out-of-scope edits reverted)
                                   │                                         │
                                   └── follow-up tasks ◀── review ◀── checks + git diff
                                        (max 3 rounds)        │
                                                              └─ done ─▶ report
```

## Usage

Just say what you want. buckets reads the request and decides the rest:

```bash
buckets "how does scope resolution work?"                       # a question: report only
buckets "add tests for lint.ts"                                 # a change: plan, code, check, review
buckets "add a Prisma adapter like packages/typeorm, plan first" # example + plan only
buckets "cherry pick 5f58e84 and 9bbaf79 onto main"             # cherry-pick
```

Or run `buckets` on its own for a chat in the current folder: each line you type is one request
(with the same options), and you leave with `exit` or Ctrl-D. Ctrl-C stops the current request,
not the chat.

```
$ cd ~/Coding/ME/my-tool && buckets
buckets 0.1.0 · my-tool (not a git repository yet) · what do you need?
> make this a git repo, ignore __pycache__ and publish it on GitHub
  Initialize, ignore caches, commit and publish.
    • 1. init(branch='main')
    • 2. gitignore(entries="['__pycache__/']")
    • 3. commit(message='chore: initial commit')
    ⇡ 4. repo_create(visibility='private')  → github.com/<you>/my-tool (private)
Go ahead? (⇡ = pushes or publishes to GitHub) [y/N, or say what to change] make it public
  …the same steps with visibility='public'…
Go ahead? … y
```

Git/GitHub chores fill in defaults rather than asking: the repository name is the folder's name,
it's private unless you say otherwise, and the GitHub account is the one set up for the folder (it's
shown next to the step). At "Go ahead?" you can answer y, n, or say what to change, and it plans again.
If the question goes unanswered (5 minutes), a follow-up within 15 minutes builds on it:
`buckets "go ahead"` runs those steps, and `buckets "call it buckets-cli"` re-plans with the change.

Before starting it prints what it understood and waits 5 seconds (Ctrl-C to stop):

```
code change · heavy (needs changes across several modules) → Claude sonnet · example: packages/typeorm · plan only
```

What it picks up from your words:

| From the request | Effect |
|---|---|
| A question vs a change vs "cherry pick …" | report only / plan-code-check-review / cherry-pick flow |
| How hard it is: light, heavy, very hard | which models run it (see Modes) |
| "like commit 4f2a1c9", "like packages/typeorm" | that commit, file or folder is shown to the planner as the example to follow |
| "plan first", "just show me the plan" | stops after printing the plan |
| "onto release/1.3" | the target branch for a cherry-pick |

Other commands: `buckets last` (show the latest report), `buckets merge` (fast-forward the
latest run's branch into the branch it started from, then delete it), `buckets discard` (delete
it, after a y/N). `-v`/`--verbose` also shows token counts and timings; they're always in the
run folder's `run.log`.

Flags still work and override the guess: `--ask`, `--do`, `--plan-only`, `--pick C1 C2 --onto B`,
`--like REF`, `--local`, `--claude`, `--model haiku|sonnet|opus`, `-y` (skip the 5-second
pause), `-f request.md`, `-C <repo>`, `--doctor`.

Changes and cherry-picks need a clean working tree. They happen on a new `buckets/…` branch,
with a commit per finished task, and buckets switches back to your branch at the end. Nothing
touches your branch until you merge.

Exit codes: `0` done, `2` finished but not done (see the report), `1` error, `130` interrupted
(work so far stays committed on the branch).

### Cherry-picks

Clean picks are plain `git cherry-pick -x`, no model involved. When a commit conflicts, the
planner reads the commit's change and the files with conflict markers and writes how to resolve
each file; the coder applies it, buckets checks no markers remain and continues. After all picks
the checks run; if conflicts were resolved or checks broke, a review round checks the
resolutions and fixes what broke. Commits already on the branch are skipped. Merge commits,
binary-file conflicts and modify/delete conflicts are left for you, and the run stops at the
first commit it can't apply (later ones may depend on it).

## How the planner learns the code

Two strategies in local mode (`--reading direct|research`; automatic: research for heavy jobs):

- **direct**: the planner picks files from an outline of the repo (or, for light jobs, takes the
  files the request names, with no model call), the script reads them, and when they're too big for
  one prompt they're summarized in parts, on the worker first (the advisor's machine helps with 3+
  parts). Files of `targeted_min_lines` (400) or more are never read whole: the planner gets their
  outline (each class, function and method with its lines), names the parts it needs, and gets
  exactly those.
- **research**: rounds of back-and-forth. The planner asks for:
  - `read`: files or line ranges, which the script serves exactly from disk;
  - `uses`: where a symbol is imported and used, answered exactly by the code index;
  - `questions`: searches and scans for a read-only session on the worker, which answers with at most
    12 short located leads (`path:line`: one sentence). The script attaches the real lines behind
    every lead, so the planner judges from the code, not from the helper's description.
  Findings are condensed when they near the planner's context limit, and every prompt is
  size-checked before it's sent (Ollama would otherwise silently drop the start of it).

Before either, buckets gathers **facts mechanically**: the code around every failing check, the
definitions and uses of code names in your request, and the closest tests. The planner also gets
**`grep`** (exact, via `git grep`) alongside `read` and `uses`.

**Planned edits skip the coder:** when the planner has seen the exact code, each task carries
`edits` (lines to find, copied from the code shown, and what they become). buckets applies them
directly, checks that a Python file still parses, and commits; only edits that don't apply (or
don't parse) go to a coder session. When the facts gathered mechanically already show every part of
a long file that the request names, the planner isn't asked which parts to read.

**Checks aren't repeated on the same code:** results are cached by the commit's file tree for 12
hours (`~/.local/state/buckets/checks-cache.json`), and only for a clean working tree. A run that
starts where the last one ended (after `buckets merge`) reuses its final check results.

**The coder starts at its place:** each task names a `where` (`GitAssistant.fill_in`, or a line
range), and the coder's prompt carries that code exactly as it is when the task starts, so it doesn't
read a long file to find it. When the planner leaves `where` empty, buckets uses the places the
instructions name (code names, "lines 120-140"). Coders run with `opencode run --standalone`, so
stopping buckets stops their edits too (the shared background server kept editing after Ctrl-C).

On the worker, every 1,000 tokens sent costs about 5 seconds before the model writes anything, so
keeping prompts short is most of the speed: the same small Python change went from 20+ minutes to
about 2 minutes of reading and planning.

**Thinking budget:** light jobs plan and review with `light_think_budget` (1,500) thinking tokens,
and any thinking that starts repeating itself (the same long sentence three times, or the latest
stretch already written earlier) is cut there and the model is asked for its answer.
Otherwise the planner's thinking is streamed and cut at `planner.think_budget` tokens
(default 2,500, about 5 minutes on the M2 Pro); then it's asked for the answer without thinking,
with its notes so far. `0` turns the cap off.

**Review skip:** when the request is only to make the checks pass (fix type or lint errors, failing
tests), and every check passes afterwards with no regressions, shortcuts or untouched callers, the
run is done without a model review.

Claude mode doesn't need the reading strategies: Claude explores the repo itself with read-only
tools (the gathered facts are still passed to it).

## The code index

For TypeScript/JavaScript (using the repo's own `typescript` package, no build needed) and Python
(the built-in `ast` module), buckets indexes what each file declares and where:

- **Outline:** the planner sees functions, classes and exported types with line ranges instead
  of a bare file list, so it can ask for 17 lines instead of a 400-line file.
- **Uses:** every place a symbol is imported and used, following re-exports and resolving
  workspace packages to their sources, so a use in `packages/nestjs` of something from
  `packages/core` is found even when `tsc` only sees core's built `dist`.
- **`imports` check:** runs with the repo's own checks. It fails when an import or re-export names
  something the imported file doesn't export: a rename that missed a caller, in any package.
  Turn it off with `"index_check": false` in `.buckets.json`.
- **API impact:** exports are compared before and after each round. Callers of a changed export that
  the work didn't update are listed for the reviewer, who must update them, keep the old form
  working, or explain why it's safe.

**Other mechanical helpers:** check failures are parsed into file → line → message with the source
line (tsc, ESLint, Vitest/Jest, pytest, ruff, mypy, go); the repo's own auto-fixers (ESLint
`--fix`, Prettier, ruff, gofmt; override with `"fixers"` in `.buckets.json`) run on the changed
files before the checks; with a check named `coverage…` (or "coverage" in the request), uncovered
line ranges from Istanbul or coverage.py reports go to the planner.

For repos with their own mechanical gates, add them as checks. For example, ecotrak-api's client
impact check fails a run that would break a client screen:

```json
{ "checks": { "client-impact": "node scripts/client-impact.js --base origin/develop --client web=../ecotrak-web" } }
```

## Which machine does what

Two roles: the **worker** runs the models that do the work, and the optional **advisor** is a
stronger, slower model. In the example setup the worker is an otherwise idle Windows laptop and
the advisor is the Mac you work on, so by default the worker does all the model work and the
advisor is something it
escalates to:

| Work | Runs on |
|---|---|
| Reading your request, picking files, reading code, planning, review | worker: thinker (Qwen 3.6 35B-A3B, ~37 tokens/s thinking) |
| Coding, look-ups | worker: thinker (same model, so Ollama never swaps) |
| Checks, git, code index | this machine's CPU (fast, no model) |
| **Advisor:** planning and review when the worker is unavailable, when the job is rated **very hard**, or when a round makes **no progress** on the failing checks | Mac (Qwen 3.6 27B, thinking) |

### When the advisor (and then Claude) is asked

The worker does the routine thinking; the advisor is asked only when something mechanical says the
thinking is going wrong. Each escalation is one targeted call with compact input (notes, parsed
errors, the diff), not a takeover, except the "no progress" rule, which hands over planning and
review for the rest of the run.

| Signal | The advisor… | Setting (`offload.…`) |
|---|---|---|
| The worker is unreachable | plans and reviews | (always) |
| The job is rated very hard | plans and reviews | `escalate_very_hard` |
| A round fixes none of the failing checks | takes over planning and review | `escalate_when_stuck` |
| Research ends with no confirmed cause, or 2+ still open | writes the plan from the notes | `escalate_unsure_research` |
| The coder made no change on a task's first try, or the same error is still in a file a round later | rewrites those tasks with the exact change | `escalate_repeated_failure` |
| The reviewer says done but the checks disagree, or finds problems but plans no fixes | does the review instead | `escalate_review_disagreement` |
| The diff touches sensitive files (`*auth*`, `*token*`, `*payment*`, `*migration*`, … plus `"sensitive"` globs in `.buckets.json`) | gives a second-opinion review before "done" | `second_opinion` (a list of globs) |

**The ladder:** worker → advisor → Claude. Claude is the last step only where it's allowed: never
in repos (or runs) that are local-only, and only when the `claude` CLI is logged in
(`claude_ladder`). At most `max_escalations` (default 2) per run; the console and the report say
each time why.

When a run ends, the advisor's model is unloaded (`unload_mac`), giving its memory back. The report says
when and why a run escalated.

Settings (`"offload"` in the config): `planner: "windows" | "mac"`, `escalate_very_hard`,
`escalate_when_stuck`, `light_jobs`, `chores`, `survey`, `unload_mac`. To code with Qwen3 Coder
instead, set `coder.model` to `qwen3-coder:30b` and `coder.opencode_model` to
`ollama_windows/qwen3-coder:30b` (that combination did worse in run 12 of the field notes).

## Modes

| | local | Claude |
|---|---|---|
| Read the request | worker (free, ~15 s) | worker (free, ~15 s) |
| Read code, plan, answer | worker's thinker (advisor when escalated) | Claude (Sonnet by default) |
| Write code | worker's coder | worker's coder |
| Checks, git, retries | buckets | buckets |
| Review and follow-ups | worker's thinker (or advisor) | Claude |
| Cost | free | Claude usage for planning and review only |

**How the mode is chosen**, first match wins:

1. Flags: `--local`, `--claude`, `--model …`.
2. The repo's `.buckets.json`: `"mode": "local"`, `"claude"` or `"auto"`.
3. Folder rules in `~/.config/buckets/config.json` under `"repos"`. The default makes
   everything under `~/Coding/ME/` local-only, so open source work never spends Claude usage.
4. Otherwise `"auto"`: light jobs run locally; heavy ones use Claude Sonnet; very hard ones
   offer Claude Opus. `"opus_policy"` decides that: `"ask"` (default: a y/N prompt in the
   terminal, Sonnet in background runs), `"allow"` or `"never"`. If Claude isn't logged in,
   everything stays local.

Claude runs through the `claude` CLI with your Claude Code login (run `claude auth login`
once). Each call can only read the repo (Read, Grep, Glob), loads no MCP servers or skills,
and has a spending cap (`claude.budget_usd`, default $3 per call). The report shows the
run's Claude cost (an API-price estimate; on a subscription it counts toward usage limits).

## Where things go

- `~/.config/buckets/config.json`: machines, models, limits. Created on first run.
- `~/.local/state/buckets/runs/<repo>-<time>/`: one folder per run, with the report,
  the plan, every model call (including the model's thinking), every coder session's log
  and every check's output. Look here first when a run goes wrong.
- `.buckets.json` in a repo (optional):

```json
{
  "checks": { "typecheck": "pnpm -s run typecheck", "test": "pnpm -s run test" },
  "exclude": ["docs/generated/*", "fixtures/*"],
  "notes": "Anything the planner should know about this repo."
}
```

Without `checks`, they're detected from `package.json` scripts (typecheck, lint, test,
using the repo's package manager), `pyproject.toml` (ruff, mypy, pytest), `go.mod` or
`Cargo.toml`. `"checks": {}` turns them off. The planner also reads `AGENTS.md` or
`CLAUDE.md` and the start of the README.

## Set up on your own machines

**You need:** Python 3.11+, git, [Ollama](https://ollama.com) on each machine that runs models,
[opencode](https://opencode.ai) on this machine, Node.js (only for the TypeScript code index), and
optionally the `claude` CLI for Claude mode.

1. **Get buckets and put it on your PATH:**
   ```bash
   ln -s /path/to/buckets/buckets.py ~/.local/bin/buckets
   ```
2. **Prepare each Ollama machine:**
   - Pull the models you'll use (see the table below), e.g. `ollama pull qwen3.6:35b-a3b`.
   - Set the context length to **64k**: Ollama app → Settings → Context length (the app's slider
     overrides `OLLAMA_CONTEXT_LENGTH`). The default 4k is too small: opencode's own prompt is ~5.4k.
   - On a machine with a small GPU, halve the memory the context needs with
     `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`.
   - For a machine other than this one, let Ollama listen on the network
     (`OLLAMA_HOST=0.0.0.0`) and allow port 11434 through its firewall. Then fully quit and restart Ollama.
3. **Run the setup and answer its questions** (Enter keeps the suggestion):
   ```bash
   buckets setup
   ```
   It asks for the worker's Ollama URL and models, and an optional advisor; then it writes
   `~/.config/buckets/config.json`, adds the Ollama providers and the two locked-down agents to
   `~/.config/opencode/opencode.json` (backing both up first), and runs `buckets --doctor`.
   Run it again any time to change machines or models; `buckets setup -y` keeps the current answers.

**Picking models**, from what we measured (field notes):

| Your hardware | Thinker and coder | Advisor |
|---|---|---|
| One machine, 32 GB+ (Apple Silicon or a PC with a GPU) | `qwen3.6:35b-a3b` (mixture of experts, ~3B active: fast) | none, or Claude mode |
| Two machines: a work machine plus an idle one with 32 GB RAM and a small GPU | `qwen3.6:35b-a3b` on the idle one | `qwen3.6:27b-q4_K_M` on the work machine (slower, ~21 GB, a stronger reasoner) |
| Less than 32 GB | smaller models plan and review poorly; prefer Claude mode for planning and keep a local coder | — |

Avoid the MLX builds of Qwen 3.6 on M-series Macs with Ollama 0.40.x (they crashed for us; use the
GGUF tags such as `qwen3.6:27b-q4_K_M`), and don't let a coder-only model plan or review (it
invented a wrong test framework in our runs).

**Folder rules:** to keep some repos free (never Claude), add globs to `"repos"` in your config,
e.g. `{"~/code/oss/*": {"mode": "local"}}`.

### Example: the setup this was built on

- **Worker:** a Windows gaming laptop (32 GB RAM, 8 GB GPU) on the home network, running
  `qwen3.6:35b-a3b` for thinking, coding and reading (labelled "Windows" in messages).
- **Advisor:** an M2 Pro MacBook (32 GB) running `qwen3.6-planner`, which is `qwen3.6:27b-q4_K_M`
  with a 64k context baked in:
  `printf 'FROM qwen3.6:27b-q4_K_M\nPARAMETER num_ctx 65536\n' > Modelfile && ollama create qwen3.6-planner -f Modelfile`.
- Claude mode through the `claude` CLI for heavy work jobs.

## Why it works this way

- **Small, precise tasks.** Each coder session starts fresh and sees only its task, so
  the planner has to write self-contained instructions. That's what lets a 30B model
  do useful work.
- **The script decides, not the models.** Routing, retries, the round limit, git and the
  checks are plain code. A check that passed before and fails now always means "not
  done", whatever the reviewer says.
- **Reading is batched.** If the picked files don't fit one planner call
  (`file_budget_tokens`), they're surveyed in batches and the planner works from the notes.
  On the M2 Pro the planner reads about 70 tokens a second, so a full 64k prompt takes
  about 15 minutes. Keep requests focused.

## Known limits (opencode 2.0.26, Ollama 0.40.x)

- `opencode run --format json` hangs, so the script reads plain output and judges the
  work from `git diff`.
- `opencode run --agent X` ignores the agent's model, so the script always passes `-m`.
- Don't use the MLX build of the planner (`qwen3.6:27b`, `nvfp4`). On the M2 Pro, Ollama
  0.40.2's MLX engine crashes during speculative decoding
  (`Maximum threads per threadgroup is 896 but requested 960`). The GGUF build doesn't.
  Planner calls retry a crashed runner automatically.

## Tests

```bash
python3 -m unittest -v test_buckets
```
