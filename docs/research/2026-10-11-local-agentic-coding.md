# Local Agentic Coding on an RTX 3070 Laptop + M2 Pro 32 GB: Models, Speed, Caching, Tooling and a Windows Network Fix (October 2026)

Keep Qwen3.6-35B-A3B as the main model on both machines. It is still the strongest open-weight agentic coder that fits these laptops; NVIDIA's own harness puts it at 70.1% on SWE-bench Verified against 51.6% for Nemotron 3.5 Lightning and 57.4% for Gemma 4 26B-A4B.\[1\] The biggest free speedups are setup changes, not a model swap: switch to the MTP GGUF with `--spec-type draft-mtp`, make every prompt byte-stable at the front, and fix whatever is shaping Windows' non-SMB TCP traffic.

## TL;DR

- **Model:** keep Qwen3.6-35B-A3B (SWE-bench Verified 73.4% on Qwen's own model card, 70.12% in NVIDIA's independent harness; Aider Polyglot 62.2% at Q8 in an independent run). None of Qwen3-Coder-30B-A3B (51.6–51.9%), Nemotron 3.5 Lightning (51.56%), gpt-oss-20b (52.44%), Gemma 4 26B-A4B (57.40%) or GLM-4.7-Flash (59.2%, vendor figure) beats it at this size. Devstral Small 2 (68.0%) is dense, so it will be slow on both machines.
- **Speed:** llama.cpp has supported Qwen3.6's built-in MTP heads since PR #22673 (merged 2026-05-16). Use an MTP GGUF with `--spec-type draft-mtp --spec-draft-n-max 2` and `-np 1`. Expect +20–30% writing speed with experts on CPU, not the ~2× seen on full-GPU setups. Ollama already turns MTP on for MTP-bearing Qwen3.6 builds (`draft_num_predict`, default 3–4). MLX decodes Qwen3.6 about 1.5× faster than llama.cpp/Ollama on an M2 Pro, but prefills about 40% slower and `mlx_lm.server` has documented memory-blowup crashes. Keep GGUF on the Mac for planning and review.
- **Windows network:** "SMB fast, every other TCP port at ~27 KB/s" almost certainly points to host software that intercepts or shapes non-SMB TCP: AV HTTP/web scanning (ESET scans HTTP "on all ports"), or Intel Killer Prioritization Engine / Dell SmartByte. It is unlikely to be the TCP stack, because SMB uses the same stack at full speed. Test with iperf3, then switch off AV web protection, then stop the Killer/SmartByte services.

---

## 1. Models for these exact machines

**Recommendation:** stay on Qwen3.6-35B-A3B Q4_K_M on both boxes, using the **MTP** GGUF variant. Keep Qwen3-Coder-30B-A3B only as a non-thinking fallback. Skip Gemma 4 26B-A4B and Nemotron 3.5 Lightning for the coding/editing role. Do not run dense models (Devstral Small 2, Qwen3.6-27B) on the Windows box.

### Benchmark comparison

| Model | Arch / active | SWE-bench Verified | Aider Polyglot | Tool-use scores | GGUF / MLX | Q4 weights (approx.) |
|---|---|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (Apr 16, 2026) | Hybrid Gated DeltaNet + attention MoE, 3B active | 73.4 (Qwen card, internal scaffold); 70.12 (NVIDIA harness) | 62.2% pass_rate_2 (Q8, diff, LLMKube); 78.7% (little-coder harness, not stock aider) | Terminal-Bench 2.0 51.5 (Qwen); Terminal-Bench 2.1 44.38, τ³-bench Banking 10.52, PinchBench 88.07 (NVIDIA) | GGUF incl. MTP variants (ggml-org, unsloth); MLX 4/8-bit (mlx-community) | ~22 GB (UD-Q4_K_M 22.1 GB; Ollama `qwen3.6:35b` 22 GB)\[1\]\[2\]\[3\]\[4\]\[5\]\[6\] |
| Qwen3-Coder-30B-A3B-Instruct | MoE 30.5B, 3.3B active, 48 layers, 4 KV heads | 51.6 (OpenHands 100 turns, Qwen); 51.9±0.2 (Nebius) | — | BFCL v3 63.4; τ²-bench Airline 42.0 / Retail 25.4 (InCoder-32B paper) | GGUF (Unsloth with tool-call template fixes); MLX | ~18–19 GB\[7\]\[8\]\[9\]\[10\]\[11\] |
| Nemotron 3.5 Lightning 30B-A3B (Aug 11, 2026) | Mamba-2 + MoE + few attention layers, MTP | 51.56 (NVIDIA) | — | Terminal-Bench 2.1 24.58; τ³ Banking 9.28; IFBench 71.88 | GGUF (ggml-org Q4_K_M, Unsloth UD) | ~20 GB RAM at 4-bit, 33 GB at 8-bit (Unsloth)\[1\]\[12\] |
| Gemma 4 26B-A4B | MoE 25.2B, 3.8B active | 57.40 (NVIDIA harness); 17.4 shown in Qwen's card (disputed) | — | SWE-bench Lite 38.67% (mini-swe-agent, FP8); tool-call recall 100% in T1-Bench | GGUF, MLX, Ollama `gemma4` | 14–18 GB at Q4\[1\]\[5\]\[13\]\[14\] |
| gpt-oss-20b | MoE, native MXFP4 | 52.44 (NVIDIA harness) | — | Terminal-Bench 2.1 15.17; T1-Bench tool-call recall 70.44 | GGUF (MXFP4), MLX | ~13 GB\[1\]\[15\] |
| Devstral Small 2 (Dec 2025) | **Dense 24B** | 68.0 (Mistral card); 65.8 (Ollama page); 56.4 (independent harness, vdf.ai) | — | Terminal-Bench 2: 22.5 | GGUF, Ollama 15 GB | ~13–15 GB\[16\]\[17\]\[18\] |
| GLM-4.7-Flash | MoE 30B-A3B | 59.2 (vendor) | — | τ²-Bench 79.5 (vendor) | GGUF (Docker `ai/glm-4.7-flash` UD-Q4_K_XL) | ~18–19 GB\[19\] |
| Qwen3.6-27B | Dense 27B | 77.2 (Qwen) | — | — | GGUF/MLX | ~15–22 GB\[6\] |

How to read these numbers:

- **Treat vendor SWE-bench numbers as non-comparable.** Qwen's 73.4% used an "internal agent scaffold (bash + file-edit tools); temp=1.0, top_p=0.95, 200K context window" (Qwen3.6-35B-A3B model card).\[5\]
- **The only like-for-like table is NVIDIA's** (Nemotron 3.5 Lightning BF16 card, Aug 11, 2026; "measured by NVIDIA under a consistent harness"). It ranks Qwen3.6-35B-A3B first on every coding/agentic row except τ³-Banking, where Gemma 4 leads (14.02 vs 10.52) and all models score badly.\[1\]
- **Gemma 4 26B-A4B is not a good fit for an agent loop that relies on JSON tool calls.** In ai-muninn's run (Apr 2026), it "cannot reliably produce OpenAI-style JSON tool calls when there are 10+ tools registered", and NVFP4 quantization "broke the tool calling tokens entirely".\[13\] The 17.4% figure in Qwen's card is an outlier; NVIDIA measured 57.4%.\[1\]\[5\]
- **Qwen3-Coder-30B-A3B's weak point is tool formatting in small sizes.** Nebius (2025) had to post-process its tool calls for OpenHands because it "struggled, causing tool formatting errors". Unsloth later shipped template fixes for llama.cpp `--jinja`.\[8\]\[9\]
- **Nemotron 3.5 Lightning is NVIDIA's "execution" model** for high-volume sub-agent steps.\[12\] It scores highest on instruction following (IFBench 71.88 vs Qwen's 63.71) but trails Qwen3.6 by about 19 points on SWE-bench Verified in NVIDIA's own table.\[1\] It might work as a cheap formatter or validator, but not as the coder.
- **GLM-4.5-Air is out of scope.** Z.AI's GLM-4.5 developer docs state "GLM-4.5-Air adopts a more streamlined design with 106B total parameters and 12B active parameters", so even 4-bit weights exceed 32 GB.
- **Newer releases:** Qwen3.8-27B (dense) appears in Ollama's library and on benchlm, but being dense it has the same Windows-speed problem as Qwen3.6-27B.\[20\]\[21\] The Nemotron card dates show nothing newer in the 30B-A3B MoE class that beats Qwen3.6 on coding as of October 2026.

### Memory footprint: weights + KV at 32k and 64k

These KV figures are my own calculations from published architecture configs. They were not measured in this research.

- **Qwen3.6-35B-A3B:** only about 1 in 4 layers is full attention; the rest are Gated DeltaNet with fixed-size recurrent state.
  - The Qwen3.6-35B-A3B model card's hidden layout is "10 × (3 × (Gated DeltaNet → MoE) → 1 × (Gated Attention → MoE))" with "16 for Q and 2 for KV" heads and "Head Dimension: 256"; 10 attention layers × 2 KV heads × 256 head-dim at f16 is about 20 KB/token.
  - 32k ≈ 0.6 GB; 64k ≈ 1.3 GB, plus tens of MB of recurrent state.
  - This is why Q4_K_M (~22 GB) + 64k fits on a 32 GB Mac.
- **Qwen3-Coder-30B-A3B:** 48 layers × 4 KV heads × 128 × 2 (K,V) × 2 bytes ≈ 96 KB/token.\[10\] 32k ≈ 3 GB; 64k ≈ 6 GB. Use `-ctk q8_0 -ctv q8_0` to halve that.
- **Devstral Small 2:** 163,840 B/token (vdf.ai calculation: 40 layers × 8 KV heads × 128).\[17\] 32k ≈ 5.4 GB; 64k ≈ 10.7 GB.
- **gpt-oss-20b:** half its layers are 128-token sliding-window, so KV is roughly 24 KB/token on the full-attention layers. 32k ≈ 0.75 GB.
- **Nemotron 3.5 Lightning:** only a few attention layers, so KV is small and Mamba state is fixed. Long context is cheap, but see Section 3 about recurrent-state caching.

### Fit and expected speed

- **Mac (32 GB unified, M2 Pro, 200 GB/s unified memory bandwidth per Apple's Jan 17, 2023 M2 Pro launch announcement):**
  - Every MoE above fits at Q4 with 64k context. Qwen3.6 at ~22 GB + ~1.3 GB KV is the tightest.
  - Measured on an M2 Pro 32 GB Mac mini (Rapid-MLX benchmark, Aug 11, 2026), Qwen3.6-35B-A3B decode: llama.cpp 38.3 tok/s, Ollama 39.0, mlx-lm 61.7.\[22\] gpt-oss-20b: llama.cpp 42.6, Ollama 32.1, MLX 46.7.\[22\]
  - Your 48 tok/s in Ollama is above that Ollama baseline. That suggests Ollama's MTP drafting is already active (see Section 2).
  - Dense 24–27B models are bandwidth-bound. 200 GB/s ÷ ~14 GB ≈ 14 tok/s is a theoretical ceiling, so Devstral Small 2 would write about 3× slower than Qwen3.6.
- **Windows (8 GB VRAM + 32 GB RAM):** all 30B-class MoEs need `--n-cpu-moe`. Qwen3.6, Qwen3-Coder, Nemotron and GLM-4.7-Flash should all land in the same 25–40 tok/s band as your current 35 tok/s, because they share ~3B active parameters. gpt-oss-20b (~13 GB) needs fewer experts offloaded and should be somewhat faster. A dense 24B with half its layers in DDR would likely drop below 10 tok/s. These are estimates from active-parameter and bandwidth arithmetic, not measured numbers.

---

## 2. Faster writing: MTP, draft models, Ollama

**Recommendation:** on Windows, download `ggml-org/Qwen3.6-35B-A3B-MTP-GGUF` (or `unsloth/Qwen3.6-35B-A3B-MTP-GGUF`), build llama.cpp from master (any commit after 2026-05-16), and add `--spec-type draft-mtp --spec-draft-n-max 2 -np 1`. Benchmark n-max 1/2/3 on your own coding prompts. Expect roughly 35 → 42–45 tok/s, not 70. Leave Ollama's MTP on with its model default, and don't bother with separate draft models.

### llama.cpp MTP support

- **The key PR is llama.cpp #22673, "llama + spec: MTP Support"** by am17an. It was opened May 4, 2026 and merged to master May 16, 2026 (Ian Paterson's write-up). The author tested it on Qwen3.6 27B and 35B-A3B and reports "a steady-state acceptance of around 75% with 3 draft tokens, which is more than >2x speed-up over baseline".\[23\]\[24\]
- **How it loads:** the MTP head is a separate model loaded from the same GGUF, with its own context and KV cache.\[24\]
- **Known costs:** "Prompt processing (PP) speed typically takes a negative hit when MTP is enabled mainly due to Device-To-Host (D2H) embedding transfers", and parallel decoding with MTP is "not fully optimized yet".\[24\]
- **Flags:**
  - `--spec-type draft-mtp --spec-draft-n-max N`. The flag was renamed from `--spec-type mtp`, and old and new binaries each reject the other name (Pedro Alonso, Aug 2026).\[25\]
  - `llama-bench` does not accept spec flags (issue #22947), so measure with `llama-server`.\[26\]
  - The current speculative docs list `draft-simple | draft-eagle3 | draft-dflash | draft-dspark | draft-mtp | ngram-*`.\[27\]
  - Issue #25187 confirms that the `qwen35moe.cpp` graph carries the MTP head (`nextn.*` tensors).\[28\]
- **You need an MTP-bearing GGUF.** Ordinary quants drop the `mtp.*`/`nextn` tensors. aprender issue #4245 notes the unsloth plain GGUF has 0 MTP tensors, while Ollama's registry blob has 15.\[29\]

### Measured speedups

| Setup | Baseline → MTP | Source |
|---|---|---|
| Qwen3.6-27B Q8, DGX Spark | ~7 tok/s → 16–22 tok/s, aggregate acceptance 0.72 (n=3), 0.83 (n=2) | PR #22673 body\[24\] |
| Qwen3.6-27B Q6_K, RTX 3090 | 22.4 → 42.5 tok/s (~1.9×) | PR comment, via mer.vin (May 2026)\[30\] |
| **Qwen3.6-35B-A3B, 6 GB VRAM + 64 GB RAM (experts on CPU)** | **22.9 → 29.4 tok/s (+28%) at n-max 2** | community bench in PR thread, via mer.vin\[30\] |
| Qwen3.6-35B-A3B UD-Q4_K_XL, RTX 3090 (full GPU) | +22.7% at n-max 2, acceptance 78.4% (141.9 tok/s) | thc1006 repo, re-measured 2026-08-26\[31\] |
| Qwen3.6-35B-A3B Q4XL, Tesla K40 | ~22 → 23.5 tok/s | PR #22673 comment (theIvanR)\[24\] |
| Qwen3.6-27B, M2 Ultra Metal | 1.35× on code at temp 0, 1.08× prose, **0.91× creative**; n-max 20 → 0.68× | llama.cpp issue #23052\[32\] |

The row closest to your Windows box is the 6 GB VRAM + RAM-offload MoE result (+28%). Your result will likely be similar, with three cautions:

- Acceptance is highest on code and low-temperature output. Run the coder at temp ≤0.6 if quality allows.
- Prefill gets slower with MTP.\[24\] Your 650 tok/s reading speed will drop somewhat, so measure whether long-read turns lose more than writing gains.
- MTP with `-np > 1` is unoptimized. Some forks refuse it outright (ik_llama.cpp PR #2369: "MTP speculative decoding is single-slot").\[33\]

### Does speculative decoding help MoE with experts on CPU?

Yes, but by less. Verifying k drafted tokens activates the union of their experts, so more expert weights stream from DDR per verify step. The llama.cpp DFlash PR #22105 explicitly flags this "expert-activation effect for MoE targets".\[31\] thc1006's first study (April 2026) found that classic draft models and n-gram methods gave **no net speedup** on Ampere + A3B MoE.\[31\] Only MTP, which is cheap and highly accepted, came out clearly positive in the August re-measurement.\[31\]

### Separate draft models

- There is no Qwen3.6 small model. The vocab-matched drafter people use is Qwen3.5-0.8B (`-hfd unsloth/Qwen3.5-0.8B-GGUF:Q8_0`, from PR #22673's comparison runs). On the dense 27B it reached 0.68 aggregate acceptance, similar to MTP overall, but it swung wildly by task (0.43 on explanations, 0.99 on factual QA).\[24\]
- For the Qwen3-30B-A3B generation, MoE-Inference-Bench (arXiv 2508.17467) found Qwen3-1.7B the best drafter, about 20% better than an 8B drafter and 25–35% better than 0.6B.\[34\]
- On your 8 GB card, a second model also takes VRAM from the experts. **Use MTP, not a draft model.**

### Ollama

- **`draft_num_predict`** is the number of tokens drafted per step. Current `qwen3.6:27b`/`qwen3.6:35b` Modelfiles ship with it set (3 on qwen3.6:27b) and with `repeat_penalty 1`, which helps speculation (computingforgeeks Ollama cheat sheet, 2026). Setting it to 0 disables drafting.\[21\]
- **Ollama enables MTP automatically when the GGUF has `mtp.*` tensors.** In v0.34.4 it passes `--spec-type draft-mtp --spec-draft-n-max 4 --spec-draft-backend-sampling` to its llama-server runner (aprender issue #4245, citing Ollama's source).\[29\] MTP support arrived in v0.32.6 (Aug 5, 2026, according to a secondary guide).\[35\]
- **Higher values are not better.** An Open WebUI user found `draft_num_predict: 6` slower than off (discussion #27479, Jul 25, 2026). The parameter does nothing on GGUFs without MTP tensors.\[36\]
- **Action:** run `ollama show qwen3.6:35b --modelfile` to confirm `draft_num_predict`. A/B test 2 vs 3 vs 0 with `"options": {"draft_num_predict": N}` on a real planning prompt.

---

## 3. Prompt caching

**Recommendation:**

- **Structure every request as `[frozen system prompt] [frozen tool schema, deterministically ordered] [repo map / file context, append-only] [conversation history, append-only, reasoning preserved] [new task last]`.**\[37\]
- Pin one slot per role (`id_slot`) and use `-np 2` at most.
- On Qwen3.6, `--cache-reuse` does not apply, so rely on **exact prefix matching plus context checkpoints**.

### llama.cpp server: what each knob does

Flag defaults below are from the server README and Particula's source read (Jul 30, 2026).

- **`--cache-prompt`** (default on) and the request field `cache_prompt: true` (default true): reuse the longest common prefix between the new prompt and the slot's existing KV, and process only the remainder.\[38\]\[39\]
- **`--cache-reuse N` / `n_cache_reuse`** (default 0): "min chunk size to attempt reusing from the cache via KV shifting".\[40\] This lets a matching chunk be reused even after a mismatch earlier in the prompt.
  - **It cannot work on Qwen3.6.** Shifting or partially erasing recurrent state is impossible. llama.cpp's source notes that Mamba-style state "can't have a state partially erased", and SWA/recurrent caches lose token information.\[41\]
  - On qwen35moe, any change at token t forces reprocessing from the nearest checkpoint before t.
- **Context checkpoints** are how hybrid models resume. Relevant flags: `-ctxcp/--ctx-checkpoints` (default 32 per slot), `-cms/--checkpoint-min-step` (default 8192; it replaced `--checkpoint-every-n-tokens`, which was removed by PR #22929 on 2026-05-25), and `-cram/--cache-ram` (default 8192 MiB host cache; `-1` = unlimited).\[41\]
  - If no checkpoint clears the threshold, the server logs `forcing full prompt re-processing due to lack of cache data`, but only at `-lv 4` (trace).\[41\]
  - When that happens it erases every checkpoint. One logged event in issue #22746 discarded 15 × 149.6 MiB ≈ 2.19 GiB.\[41\]
  - **Run with `-lv 4` once** and grep for it.
- **`-sps/--slot-prompt-similarity`** (default 0.10): how much prefix must match for a request to be routed to an existing slot.\[41\] Explicit `id_slot` in the request is more reliable.
- **`--slot-save-path DIR`** plus `POST /slots/{id}?action=save` / `?action=restore` with `{"filename": "plan.bin"}`: persist a warmed slot to disk. Saves are hundreds of MB for long contexts.\[40\]\[42\]\[43\] Use this to restore a "repo context" slot after restarts or between tasks.
- **`n_keep`**: only matters when the context overflows. It sets how many prompt tokens survive the shift (`-1` = all).\[40\] It does not control caching.
- **`-np N`**: splits `-c` into N slots (`n_ctx_slot = ctx/N`; a 131072/2 setup logs 65536 per slot).\[41\] Each slot keeps its own KV and checkpoints, so 2 slots double the KV/checkpoint memory. For Qwen3.6 the KV cost is only ~1.3 GB per 64k slot, but MTP is effectively single-slot.

### The traps that silently kill reuse

- **Reasoning stripping.** Chat templates that drop `<think>` from earlier turns change the prefix every turn. A maintainer called this "normal behavior for chat templates that do not keep the reasoning contents of previous turns".\[41\] Use `--chat-template-kwargs '{"preserve_thinking": true}'` for Qwen3.6; PR #22673's benchmark commands use exactly this.\[24\]
- **Tool lists regenerated in a different order**, MCP restarts, and timestamps or session IDs in the system prompt:\[44\]
  - Zoo-Code #1891 and hermes-webui #8148: a per-session ID line cost 15 s on the first answer of every chat.\[45\]
  - Hermes-agent PR #24778 moved to a "byte-static" system prompt for the same reason.\[46\]
- **A request rejected for exceeding context** can trigger LRU slot reassignment and wipe checkpoints (issue #22746).\[41\] Enforce a token budget in the orchestrator before sending.
- **Debugging:** `--log-prompts-dir /tmp/p` writes each prompt to disk. Diff consecutive dumps to find what changed.\[41\]

### Prompt layout for planner, reviewer and coder

1. A system prompt that is byte-identical across the planner, reviewer and coder roles. Put role differences in the first user message, not in the system prompt.
2. Tools as a sorted, fixed JSON schema.
3. Repo map and pinned files, appended only. Never re-render the whole map when one file changes; add a "file X changed" delta at the end.
4. The task and diff last.

For follow-ups, append and never rewrite. When you must truncate, drop from the tail of history, not the middle.\[37\]

### Ollama

- **Prefix caching is implicit.** If the model is still loaded, the shared prefix is reused.\[47\]
- **`keep_alive` defaults to 5 minutes;** unloading evicts the cache. Set `keep_alive: -1` or `OLLAMA_KEEP_ALIVE=-1`.\[37\]
- **`num_ctx` must stay constant.** A request whose `num_ctx` differs from the loaded value reloads the model (ollama issue #6176). Set it once in a Modelfile or with `OLLAMA_CONTEXT_LENGTH`, not per request. Note that the OpenAI-compatible `/v1` endpoint can't set `num_ctx`.\[48\]
- **Ollama 0.34 runs a llama-server runner,** so the same hybrid-checkpoint behaviour applies. One OpenClaw report shows `LLAMA_ARG_CACHE_RAM=24576` being honoured by Ollama's daemon, and cached prefix dropping from 25.4k to 14.3k tokens when the client changed its system-prompt digest (openclaw issue #150081, Ollama 0.34.0).\[49\]
- **Pitfalls:** the same as llama.cpp, plus per-request option changes that force reloads. Check `prompt_eval_count` in responses to confirm reuse.

---

## 4. Mac: MLX vs llama.cpp/Ollama

**Recommendation:** don't move the planner/reviewer to MLX yet. MLX writes about 1.5× faster on Qwen3.6, but the Mac's roles are prefill-heavy (reading plans, diffs and files). MLX prefills about 40% slower and `mlx_lm.server` still has unbounded-cache OOM crashes on 32 GB machines. If you want more Mac speed, try **llama.cpp Metal + MTP GGUF** or keep Ollama with MTP. Re-evaluate MLX when `mlx_lm.server` gets a KV cap.

### Benchmarks (M2 Pro 32 GB Mac mini, Qwen3.6-35B-A3B Q4)

From Rapid-MLX, Aug 11–12, 2026. Rapid-MLX builds an MLX server and discloses this; raw JSON is published.\[22\]

| Metric | llama.cpp (b10330) | Ollama 0.32.7 | mlx-lm 0.31.3 / rapid-mlx |
|---|---|---|---|
| Decode, single stream | 38.3 tok/s (llama-bench 39.1) | 39.0 | 61.7 raw mlx-lm / 58.5 server\[22\] |
| Prefill pp512 | **460 tok/s** | — | 277 tok/s\[22\] |
| Cold 1K-token TTFT | **2.3 s** | 2.6 s | 3.4 s\[22\] |
| 8-stream aggregate decode | 48.8 | 27.2 | 82.9\[22\] |

- **On M1 Max with Q8 the result flips:** llama.cpp decoded 34 vs MLX 26.2 tok/s (dev.to/sleepyquant).\[50\] famstack (2026) explains part of this: on M1/M2, converting bf16 to fp16 closes most of the gap.\[51\] Results depend on chip generation and quant.
- **Your current numbers** (475 tok/s reading, 48 tok/s writing in Ollama) already beat that article's Ollama decode, likely thanks to MTP. Realistic MLX gain: 48 → ~60 tok/s writing, at the cost of 475 → under 300 tok/s reading.

### Quantizations

- mlx-community publishes `Qwen3.6-35B-A3B-4bit` and `-8bit`; unsloth publishes MLX 4-bit (UD) builds.
- At 6-bit (~27 GB), weights plus macOS overhead leave almost no room on 32 GB. Treat 4-bit (≈19.5 GB peak in one secondary benchmark) as the only practical MLX option. DWQ variants exist for Qwen-family models in mlx-community but were not verified for 3.6 in this research.

### Stability (why your MLX build crashed)

- **mlx-lm issue #883:** `mlx_lm.server` caused a **macOS kernel panic** (IOGPUMemory) from unbounded KV growth in an agentic session at about 58k tokens. The server has no `--max-kv-size` (feature request #615).\[52\]
- **mlx-lm issue #1390** (mlx-lm 0.31.3): the prompt cache grew to 23–26 GB, then Metal "Insufficient Memory" abort.\[53\]
- **LM Studio mlx-engine issue #314:** Qwen3.6-35B-A3B 4-bit MLX "consumes significantly more memory than the GGUF version" and crashed on a Mac mini M4 **32 GB** in Claude Code, while the GGUF was fine.\[54\]
- **mlx-lm issue #1292:** Qwen3.6 **MTP** MLX variants return 1–9-token completions when the system prompt is reused with a new user message.\[55\] That is exactly the cache pattern an orchestrator produces.
- Partial mitigations: `--prompt-cache-size`/`--prompt-cache-bytes` (helped only slightly in Kulman's M5 Pro tests)\[56\] and capping context in the orchestrator.

### Tool calling and caching in mlx-lm server

- It exposes OpenAI-compatible `/v1/chat/completions` with tool calls, but only if the model's `tokenizer_config.json` declares a parser. For Qwen3-Coder, community 4-bit builds needed `"tool_parser_type": "qwen3_coder"` added by hand.\[57\] Verify this for any Qwen3.6 MLX build.
- It has an in-memory prompt cache (the source of the OOMs above). `--prompt-cache-file` is not supported in server mode (issue #1178).\[58\]

### LM Studio

- Its MLX engine posted the fastest burst decode in the Rapid-MLX tests. But it **re-paid the full prefill on every long request** to the same conversation (~4 s TTFT where others were warm), which is fatal for agent loops.\[22\]
- Its llama.cpp engine matches plain llama.cpp: 41.7 vs 41.4 effective tok/s in famstack's test, versus 26.0 for Ollama on the same engine.\[51\]

---

## 5. Agent architecture: what the evidence says

**Recommendation:**

- Keep the planner/editor split, but have the planner produce a concrete per-file change list rather than code.
- Make the coder emit **search/replace blocks** (aider "diff" style) with a lint/type-check gate that rejects bad edits.
- Show files in **~100-line windows**, and keep only the **last ~5 full observations**.
- Always run tests and give the coder **one retry with test output**; that roughly doubles the solve rate.
- Spend Sonnet escalations on cases where the retry still fails.

### Planner/editor split (Aider architect mode)

- **Sept 26, 2024 (aider):** o1-preview as architect with o1-mini or DeepSeek as editor scored **85.0%** ("whole" format) vs **79.7%** for o1-preview alone (diff), on the 133-exercise edit benchmark.\[59\]
- **Jan 24, 2025 (aider):** R1 architect + Sonnet editor set **64.0%** on Polyglot vs o1 alone at 61.7%, at 14× lower cost. But "o1 paired with Sonnet didn't produce better results than just using o1 alone". The gain is model-dependent.\[60\]
- **Most relevant to small local models (aider issue #2401):** Qwen2.5-Coder-32B produced well-formed diffs in **92%** of cases on its own and **100%** in architect mode with itself as editor. The split's main benefit for small models is **edit-format reliability**, not reasoning.\[61\]
- Your Mac-plans / Windows-edits design is sound, provided the architect's output is unambiguous.

### Edit formats

- **Aider Polyglot requires strict SEARCH/REPLACE blocks;** small models "frequently fail due to format violations rather than logic errors" (vibecoderjournal benchmark notes).\[62\]
- **Qwen3-32B (aider, May 8, 2025):** 41.3% with diff format and 94.2% correct-format rate.\[63\] Even good mid-size models miss about 6% of edits, so build an auto-repair/retry loop.
- **Aider's default for unknown models is "whole" file,** "the easiest format for an LLM to use", but it costs far more output tokens.\[64\] At 35–45 tok/s, rewriting a 400-line TSX file is about 3 minutes per edit. Use whole-file only for files under ~150 lines or after two failed diff attempts.
- **Unified diffs** were not measured for these models in sources found here. Line-numbered `edit start:end` with a linter gate is the SWE-agent approach (below).

### SWE-agent ACI findings (NeurIPS 2024)

- The tailored interface raised GPT-4 Turbo from **11.0% (raw shell) to 18.0%** on SWE-bench Lite.\[65\]
- **File viewer window:** 100 lines = 18.0%; 30 lines = 14.3%; full file = 12.7%.\[65\]\[66\]
- The **edit command with linter** (rejects syntactically invalid edits and shows the error) is worth +4.7 points: 15.0% with edit vs 10.3% without.\[65\]\[67\]
- Searches return **at most 50 results** and reject overly broad queries. The final config kept only the **last 5 observations** in full.\[65\]

### Retries, self-consistency and test-time scaling

- **LLMKube, Qwen3.6-35B-A3B Q8 on Aider Polyglot:** pass_rate_1 = **34.7%** → pass_rate_2 = **62.2%** after one retry with test feedback.\[3\]
- **little-coder harness:** 158 first-attempt passes + 19 on retry out of 225.\[2\]
- **SWE-TRACE (arXiv 2604.14820):** a Qwen3-30B-A3B agent goes from **63.5% → 71.2%** on SWE-bench Verified when heuristic-guided test-time scaling selects among candidate trajectories.\[68\] That is direct evidence that sampling several candidates and picking with a verifier pays off at this model size, if you can afford the tokens.
- **Practical version for you:** on hard tasks, have Windows generate 2 candidates at different temperatures, run tests on both, and send only the failing-both cases to Sonnet.
- **Fine-tuned small agents:** SWE-Gym-32B 20.6% (OpenHands); SWE-Lego-32B 52.6%; Nebius's SWE-rebench-trained Qwen3-30B-A3B 50.3%.\[8\]\[68\] Scaffold plus training data matter as much as base size.

### Read loops and context budget

From the SWE-agent ablations and the caching analysis in Section 3:

- Locate first with grep, ast-grep or LSP references; cap search results at 50.
- Open only the windows you need.
- Summarize stale observations rather than keeping them verbatim.
- Since summaries invalidate the KV cache from that point, do it rarely and in batches, for example every N turns, not every turn.\[37\]

No source here gives a validated "N reads before editing" number. Enforcing "must have viewed the target lines in this session before editing them" is a cheap, sensible guard.

---

## 6. Mechanical code tooling from Python

**Recommendation:**

- Use **ast-grep** (`ast-grep-py`) for structural search and codemods, and **tree-sitter** for outlines and repo maps.
- Keep **one long-lived LSP process per language per repo**: typescript-language-server (tsserver) for TS/TSX, basedpyright for Python. Drive them through **multilspy** or **lsp-client**, and use them only for references and rename.
- Start the LSPs once per session and reuse them. Project indexing is the expensive part.

### LSP from Python

- **multilspy (Microsoft; came out of the NeurIPS 2023 "monitor-guided decoding" work)** handles JSON-RPC, server download and launch, and exposes `request_definition`, `request_references`, `request_document_symbols`, `request_hover` and `request_completions`, in both sync (`SyncLanguageServer`) and async forms. It supports `"typescript"`, `"javascript"` and `"python"` (via jedi-language-server).\[69\]\[70\]
  ```python
  from multilspy import SyncLanguageServer
  from multilspy.multilspy_config import MultilspyConfig
  from multilspy.multilspy_logger import MultilspyLogger
  lsp = SyncLanguageServer.create(MultilspyConfig.from_dict({"code_language": "typescript"}),
                                  MultilspyLogger(), "/abs/repo")
  with lsp.start_server():
      refs = lsp.request_references("src/api/user.service.ts", 41, 10)
  ```
- **lsp-client (PyPI)** is a newer async client "inspired by multilspy". It uses lsprotocol types, ships Pyright/Pyrefly clients and containerized servers, and has `request_references` and `request_definition_locations`.\[71\] It fits better if you want pyright-grade Python analysis instead of jedi.
- **For Python, use basedpyright** (`pip install basedpyright` provides `basedpyright-langserver --stdio`). In codex-lsp-bridge's config, `.venv` and similar large folders are excluded to keep the server responsive.\[72\] Do the same with `exclude` in `pyrightconfig.json`.
- **For rename,** send `textDocument/prepareRename` then `textDocument/rename`, and apply the returned `WorkspaceEdit` yourself. LSPs return edits; they do not write files.
- **Reliability notes:**
  - Do not count references until the server has finished its initial project load. tsserver can return partial results while still loading a large monorepo.
  - Keep one server per (language, workspace root), as codex-lsp-bridge does, to avoid cross-repo contamination.\[72\]
  - Restart the server after `package.json`/`tsconfig` changes.

### tree-sitter and ast-grep

The API details below are from general knowledge of these libraries, not re-verified in this pass.

- **py-tree-sitter:** `pip install tree-sitter tree-sitter-typescript tree-sitter-python`. Load `tree_sitter_typescript.language_tsx()`. Use queries for `function_declaration`, `class_declaration`, `method_definition`, `lexical_declaration` with arrow functions, and `export_statement` to build a cached per-file outline: path, symbol, line range and signature. This is how aider-style repo maps work. Parsing is milliseconds per file, so recompute only changed files, keyed on mtime or hash.
- **Call graphs:** tree-sitter only resolves calls by name. Use it to propose edges and LSP references to confirm the ones that matter.
- **ast-grep-py:** `from ast_grep_py import SgRoot; root = SgRoot(src, "tsx").root(); root.find_all(pattern="useEffect($FN, [])")`. For codemods, use the CLI with YAML rules (`ast-grep scan -r rule.yml --update-all`) or `node.replace(...)` + `commit_edits` in Python. Use it for mechanical refactors such as renaming props across components or swapping API calls, so the LLM never hand-edits those.

---

## 7. Catching weak and over-mocked tests

**Recommendation:**

- Add a "changed-files mutation gate" for every coder change: **StrykerJS** (`coverageAnalysis: "perTest"`, `incremental: true`, mutate globs built from `git diff`) and **mutmut 3** for Python.
- Give each a time budget of a few minutes, and fail the task if new or changed lines have surviving mutants.
- Add a mock-ratio lint, and require at least one property-based test (fast-check/Hypothesis) for pure functions the agent writes.

### Mutation testing

- **Why mutation testing and not coverage:** coverage shows a line ran, not that a test would notice it being wrong. One project's issue (glion #737, citing a replication study, arXiv 2607.22880) calls LLM-written tests "boundary-blind" and anchored to what the model expects rather than what the code does.\[73\]
- **StrykerJS config:**
  ```json
  { "testRunner": "vitest", "coverageAnalysis": "perTest", "incremental": true,
    "reporters": ["clear-text","progress","json"],
    "mutate": ["src/**/*.ts","!src/**/index.ts"], "thresholds": {"high":85,"low":70,"break":0} }
  ```
  - `--incremental` stores results in `reports/stryker-incremental.json` and re-tests only changed mutants. It was introduced in beta in June 2022 (Nico Jansen, Stryker maintainer).\[74\]\[75\]
  - `perTest` coverage runs only the tests that cover each mutant.\[75\]
  - Jest, Mocha, Vitest and Karma runners exist.\[76\]
- **`--since` is disputed.** Several 2026 guides use `npx stryker run --since main` for StrykerJS. PandaStack's guide states "StrykerJS does not have a `since` option — its levers are incremental mode … plus a `mutate` glob you build from the diff yourself" (`since` is a Stryker.NET feature).\[77\]\[78\] The safe, portable approach:
  ```bash
  MUTATE=$(git diff --name-only main...HEAD -- 'src/**/*.ts' 'src/**/*.tsx' | grep -v '\.test\.' | paste -sd, -)
  npx stryker run --incremental --mutate "$MUTATE" --concurrency 4
  ```
  Reported PR-scale runtimes with incremental + perTest are 1–5 minutes (qaskills guide, 2026).\[75\] Treat that as indicative.
- **Python, mutmut:** re-tests only mutants in functions whose source changed (PandaStack). Run `mutmut run "pkg.changed_module*"`, then `mutmut results`.\[77\]\[79\] cosmic-ray is the heavier, distributed alternative. For a single laptop, mutmut's per-function incrementality is the better fit.

### Cheap heuristics that need no mutation run

These are my own design suggestions, not sourced.

- **Mock ratio:** count `jest.mock`/`vi.mock`/`jest.spyOn`/`unittest.mock.patch`/`MagicMock` per test file, and also assertions that target mocks (`toHaveBeenCalled*`, `assert_called*`) vs assertions on real return values. Flag tests where the module under test is itself mocked. That check is easy with ast-grep (`jest.mock($PATH)` where `$PATH` resolves to the file under test).
- **Assertion-free or trivially true tests:** use ast-grep rules for tests with no `expect`/`assert`, `expect(true)`, and snapshot-only tests on new logic.
- **Diff coverage:** require changed lines to be covered (`vitest --coverage` / `coverage.py` + `diff-cover`). This is necessary but not sufficient; mutation is the real check.
- **Property-based tests:** fast-check (`fc.assert(fc.property(...))`) and Hypothesis (`@given`) find edge cases that example tests miss. Ask the coder for one property per pure function and keep `numRuns`/`max_examples` low (e.g., 100) for local speed.

---

## 8. Windows → Mac TCP at ~27 KB/s while SMB runs at 30–40 MB/s

**Recommendation:** SMB/445 being fast proves the Wi-Fi link, the Windows TCP stack, MTU and the routing to the Mac are basically fine. Something is treating **non-SMB TCP flows** differently. In order:

1. iperf3 on a fresh port, both directions.
2. Disable third-party AV **web/HTTP protection and firewall**.
3. Stop **Killer/SmartByte** prioritization.
4. Check `Get-NetTransportFilter` and `Get-NetQosPolicy`.
5. Verify the route.
6. Capture with pktmon and Wireshark.

### What the numbers say

This is my own arithmetic, not from a source. 27 KB/s is about one 1460-byte segment every ~54 ms. Windows' default delayed-ACK timeout is 40 ms with an ACK every 2 segments (Microsoft Networking Blog, "TCP Templates for Windows Server 2019", shows `DelayedAckTimeout(ms): 40, DelayedAckFrequency: 2` in `Get-NetTCPSetting` output). A flat rate this low looks like one segment per ACK timeout, which is what happens when something holds back segments or ACKs (a local proxy, a per-flow shaper, or Nagle + delayed ACK with tiny writes). It is not a bandwidth limit. Mac → Windows at ~5 MB/s, also far below SMB, fits a Windows-side component inspecting both directions better than a Mac-side cause.

### Ranked likely causes

1. **AV HTTP/web scanning through a WFP proxy (top suspect if any third-party AV is installed).**
   - ESET's official docs (Endpoint Security 10.1) say: "HTTP traffic is always monitored on all ports for all applications." ESET's macOS docs predefine 8080 as an HTTP port.\[80\]\[81\]
   - Your 8080, 8090 and 11434 traffic is all HTTP (web servers, Ollama API). SMB is not HTTP, so it is not proxied.
   - ESET KB2654 links its WFP filtering to slow HTTP transfers and suggests `TcpAckFrequency=1` as a workaround.\[82\] That KB is old, but it shows the ACK-timing mechanism.
   - Avast Web Shield/Web Guard "monitors all network activities". One Avast thread showed its firewall alone cutting LAN SMB from about 120 to 30–40 MB/s.\[83\]\[84\]
   - Microsoft's own SMB troubleshooting says to test with anti-malware network filter drivers unloaded.\[85\]
2. **Intel Killer Prioritization Engine / Dell SmartByte.**
   - A Microsoft Q&A moderator answer (2023-06-01) calls Killer Prioritization Engine "a known issue as the reason for slow upload speed".\[86\]\[87\]
   - Intel Community reports it re-enables itself after reboots and driver updates. A BleepingComputer case found the throttling "isolated to the LAN side".\[88\]\[89\]
   - These engines classify user-mode apps (python, node, ollama), while SMB is sent by the kernel (System process). That plausibly explains the SMB exemption, though no vendor documents it.
3. **A leftover per-port TCP template.** `New-NetTransportFilter` can bind a TCP setting "to a port number or destination IP address range" (Microsoft Learn, Get-NetTCPSetting, updated 2025-05-14).\[90\] It is rare, but it is the one stack-level mechanism that is per-port.
4. **App-level small writes (Nagle + delayed ACK).** This fits the math, but it is unlikely to produce one identical rate across three unrelated servers. iperf3 will clear or confirm it.
5. **Routing via Tailscale or vEthernet.** Tailscale issue #8524 shows its vNIC (reported as 100 Gbps, so given a low metric) capturing LAN traffic, but only while connected.\[91\]\[92\] Hyper-V **external** switches bound to Wi-Fi are known to wreck host throughput (ElevenForum/TenForums: disable packet coalescing and LSO on the bridged vEthernet, or remove the switch).\[93\]\[94\]\[95\]\[96\] The **Default Switch** is NAT/ICS and shouldn't carry LAN traffic. If routing were wrong, SMB would be slow too, so this is low probability. Verify anyway.
6. **Wi-Fi driver options** (packet coalescing, throughput booster) and **autotuning or congestion provider:** these would hurt SMB too, so they are unlikely. CUBIC has been the Windows client default since Windows 10 updates, per Microsoft's Networking Blog (Daniel Havey, Feb 14, 2019: "If you are running a client just get the latest Windows 10 update and you will have Cubic by default").
7. **macOS firewall or stealth mode:** these allow or block incoming connections and do not throttle (Apple support docs).\[97\]\[98\] Rule out third-party Mac filters such as Little Snitch, LuLu or a Mac AV.

### Step-by-step diagnostics

```powershell
# 0) Baseline, app-neutral (Mac: brew install iperf3; iperf3 -s)
iperf3 -c <MAC_IP> -t 10            # Win -> Mac
iperf3 -c <MAC_IP> -t 10 -R         # Mac -> Win
iperf3 -c <MAC_IP> -t 10 -w 1M -P 4 # bigger window / parallel
iperf3 -c <MAC_IP> -p 445 ...       # (after stopping SMB on Mac? no - instead try -p 80 and -p 5201)
```
If iperf3 also runs at ~27 KB/s, the cause is host or network, not your apps. If iperf3 is fast but your servers are slow, look at the app's socket writes.

```powershell
# 1) Interceptors and shapers
fltmc                                   # filter drivers (AV)
netsh wfp show filters                  # writes filters.xml; look for third-party callouts
Get-Service *Killer*,*SmartByte*,*Rivet* | ft Name,Status,StartType
Get-NetQosPolicy
# Temporarily: disable AV "Web access protection"/"Web Shield"/HTTP scanning AND AV firewall; retest.
# Stop-Service "SmartByte Network Service" / Killer Network Service, or turn off Prioritization Engine; retest.

# 2) TCP stack / per-port templates
netsh int tcp show global
Get-NetTCPSetting | Select SettingName,AutoTuningLevelLocal,CongestionProvider,DelayedAckTimeoutMs
Get-NetTransportFilter                  # anything besides the default Automatic filter is suspicious
netsh int tcp set global autotuninglevel=normal

# 3) Path selection
Find-NetRoute -RemoteIPAddress <MAC_IP>
route print
Get-NetIPInterface | sort InterfaceMetric | ft ifIndex,InterfaceAlias,AddressFamily,InterfaceMetric
# Test with the Hyper-V Default Switch adapter disabled and Tailscale fully quit.
# Also compare IPv4 vs IPv6 (curl -4 / -6) and wired vs Wi-Fi.

# 4) Capture
pktmon filter remove
pktmon filter add -i <MAC_IP> -p 8080
pktmon start -c --comp nics
#   ...run the slow transfer...
pktmon counters
pktmon counters --type drop --drop-reason
pktmon stop
pktmon etl2pcap PktMon.etl --out slow.pcapng
pktmon list -a -i       # shows filter/WFP components in the path
```

In Wireshark, check four things:

- Segments leaving roughly every 40–200 ms with an idle wire between them points to a local shaper or proxy.
- Retransmissions or dup-ACKs point to the path.
- A tiny advertised window or zero-window from the Mac points to the Mac receiver or app.
- Compare the first SYN's window scale on both sides.

On the Mac, run:

```bash
sudo tcpdump -i en0 host <WIN_IP> and port 8080 -w mac.pcap
/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
/usr/libexec/ApplicationFirewall/socketfilterfw --getstealthmode
sudo pfctl -s rules
```

**Most likely fix:** exclude the Mac's IP or your ports from AV web scanning (or turn off HTTP scanning for local networks), and permanently disable Killer/SmartByte prioritization. Re-check after driver updates, because Killer re-enables itself.\[89\]

---

## Caveats

- **Vendor benchmarks are not comparable across harnesses.** Qwen's 73.4% and Mistral's 68.0% come from their own scaffolds. NVIDIA's table is the only consistent cross-model comparison, and NVIDIA has an interest in Nemotron. Aider Polyglot numbers for Qwen3.6 come from community runs (62.2% stock aider at Q8; 78.7% with a custom harness), not the official leaderboard.\[2\]\[3\]
- **KV-cache sizes are my own calculations** from architecture configs (Qwen3.6 attention-layer count and head dims are from its Hugging Face model card: 10 Gated Attention layers out of 40, 2 KV heads, head dimension 256), not measurements. Verify with llama-server's startup log (`llama_kv_cache: size = ...`).
- **The MTP speedup for your exact setup is extrapolated** from a 6 GB VRAM + RAM-offload report (+28%) and full-GPU 3090 results (+23%). Results are prompt- and temperature-dependent and can go negative on high-entropy text (issue #23052).
- **Some Ollama details come from secondary sources:** the v0.32.6 MTP release date and the v0.34.4 auto-MTP flags come from a third-party guide and a GitHub issue that reads Ollama's source, not Ollama release notes.
- **The MLX vs llama.cpp benchmark on an M2 Pro is from Rapid-MLX,** which sells an MLX server. It publishes raw data, and its llama.cpp numbers agree with llama-bench.\[22\] Other users found llama.cpp faster on M1 Max at Q8.
- **The Windows networking diagnosis is a ranked hypothesis.** The SMB-exemption explanation for AV/Killer is inferred from how these products work, not stated by the vendors, and much of the Killer/SmartByte evidence comes from forums and Q&A sites.
- **Some tooling details were not re-verified in this research:** tree-sitter and ast-grep API snippets, LSP startup-time behaviour, and the StrykerJS `--since` flag, where sources conflict.

## Sources

1. [nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16 · Hugging Face](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16)
2. [little-coder/docs/benchmark-qwen3.6-35b-a3b.md at main · itayinbarr/little-coder](https://github.com/itayinbarr/little-coder/blob/main/docs/benchmark-qwen3.6-35b-a3b.md)
3. [62.2% on Aider Polyglot from a MacBook Pro. Then the other model we tried scored 4%. Here's what actually happened, with a working cost loop attached. - LLMKube Blog](https://llmkube.com/blog/m5-max-aider-polyglot-and-finops)
4. [Qwen3.6 35B A3B - VRAM, Specs & Benchmarks](https://apxml.com/models/qwen36-35b-a3b)
5. [Qwen/Qwen3.6-35B-A3B · Hugging Face](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)
6. [Qwen 3.6 Complete Guide: 27B Dense, 35B-A3B MoE, and Which to Use](https://insiderllm.com/guides/qwen-3-6-local-ai-guide/)
7. [Qwen/Qwen3-Coder-30B-A3B-Instruct · SWE-bench Verified Repro](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct/discussions/30)
8. [OpenHands trajectories with Qwen3-Coder-480B-A35B-Instruct](https://nebius.com/blog/posts/openhands-trajectories-with-qwen3-coder-480b)
9. [Qwen3-Coder: How to Run Locally](https://unsloth.ai/docs/models/tutorials/qwen3-coder-how-to-run-locally)
10. [Qwen/Qwen3-Coder-30B-A3B-Instruct · Hugging Face](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct)
11. [InCoder-32B: Code Foundation Model for Industrial Scenarios](https://arxiv.org/pdf/2603.16790)
12. [NVIDIA Nemotron 3.5 Lightning: How To Run Locally](https://unsloth.ai/docs/models/nemotron-3.5)
13. [\[Benchmark\] SWE-bench Lite 38.67% with a 26B Local Model — 0.33% from Claude 3.5 Sonnet Scaffolds](https://ai-muninn.com/en/blog/swe-bench-lite-gemma4-26b-38-percent)
14. [Gemma 4 by Google: Specs, Benchmarks, Model Sizes, and How to Run It Locally (2026 Guide)](https://aurigait.com/blog/gemma-4-features-benchmarks-guide/)
15. [T1-Bench: Benchmarking Multi-Scenario Agents in Real-World Domains](https://arxiv.org/pdf/2606.11070)
16. [mistralai/Devstral-Small-2-24B-Instruct-2512 · Hugging Face](https://huggingface.co/mistralai/Devstral-Small-2-24B-Instruct-2512)
17. [Best Local LLM for Coding in 2026: 10 Models Compared](https://vdf.ai/blog/best-local-llm-for-coding/)
18. [devstral-small-2](https://ollama.com/library/devstral-small-2)
19. [glm 4.7 flash](https://hub.docker.com/r/ai/glm-4.7-flash)
20. [Gemma 4 26B A4B vs Qwen3.8-27B: Benchmarks & Cost](https://benchlm.ai/compare/gemma-4-26b-a4b-vs-qwen3-8-27b)
21. [Ollama Models Cheat Sheet 2026](https://computingforgeeks.com/ollama-models-cheat-sheet/)
22. [MLX vs Ollama vs LM Studio: Rapid-MLX Speed Benchmark](https://rapidmlx.com/blog/rapid-mlx-vs-ollama-benchmark)
23. [DFlash vs MTP: Speculative Decoding on a 3090 Ti (Qwen)](https://ianlpaterson.com/blog/3090-ti-qwen-speedup-dflash-mtp/)
24. [llama + spec: MTP Support](https://github.com/ggml-org/llama.cpp/pull/22673)
25. [Speculative Decoding Nearly Tripled Qwen on One 4090 - Pedro Alonso](https://www.pedroalonso.net/blog/qwen-mtp-speculative-decoding-4090/)
26. [Feature Request: Add MTP / speculative decoding support to llama-bench for PR #22673 · Issue #22947 · ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp/issues/22947)
27. [llama.cpp/docs/speculative.md at master · ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/docs/speculative.md)
28. [Research: FR-Spec-style draft-vocab trimming for native MTP speculative decoding · Issue #25187 · ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp/issues/25187)
29. [qwen35 MTP speculative decoding with GDN state rollback · Issue #4245 · paiml/aprender](https://github.com/paiml/aprender/issues/4245)
30. [Run Qwen 3.6 MTP in llama.cpp: Faster Local Inference With Built-In Speculative Decoding - Mervin Praison](https://mer.vin/2026/05/run-qwen-3-6-mtp-in-llama-cpp-faster-local-inference-with-built-in-speculative-decoding/)
31. [GitHub - thc1006/qwen3.6-speculative-decoding-rtx3090: llama.cpp speculative decoding measured on one RTX 3090, Qwen3.6-35B-A3B UD-Q4\_K\_XL, commit 3737e4137. Published figures are re-derived from the committed data by a checker that fails on drift, a coverage probe reports how many of them it actually covers, and ERRATA.md lists this study's own retracted claims. · GitHub](https://github.com/thc1006/qwen3.6-speculative-decoding-rtx3090)
32. [Feature Request: per-request speculative decoding toggle in llama-server · Issue #23052 · ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp/issues/23052)
33. [qwen4exp: MTP (NextN) self-speculative decoding support by jcr211 · Pull Request #2369 · ikawrakow/ik\_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp/pull/2369)
34. [MoE-Inference-Bench: Performance Evaluation of Mixture of Expert Large Language and Vision Models](https://arxiv.org/pdf/2508.17467)
35. [Speculative Decoding Setup: 13 Steps, 90 Min \[2026\]](https://tech-insider.org/speculative-decoding-setup-2026/)
36. [Enable Ollama MTP in Open WebUI with advanced parameter: draft\_num\_predict · open-webui/open-webui · Discussion #27479](https://github.com/open-webui/open-webui/discussions/27479)
37. [Stop Re-Computing Your System Prompt: Prefix Caching for Local Agent Loops](https://www.smfclearinghouse.com/blog/prefix-caching-agent-loops-local-llm-inference/)
38. [llama-server(1) — llama.cpp-tools — Debian unstable — Debian Manpages](https://manpages.debian.org/unstable/llama.cpp-tools/llama-server.1.en.html)
39. [How to cache system prompt? · ggml-org/llama.cpp · Discussion #8947](https://github.com/ggml-org/llama.cpp/discussions/8947)
40. [llama.cpp/tools/server/README.md at master · ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)
41. [Full Prompt Re-Processing: llama.cpp Cache Fixes That Work](https://particula.tech/blog/prompt-reprocessing-swa-hybrid-models-kv-cache)
42. [Tutorial: Persistent KV cache per session with llama-server hooks · ggml-org/llama.cpp · Discussion #20572](https://github.com/ggml-org/llama.cpp/discussions/20572)
43. [I have a 950k prompt, how do I not reprocess it every time? · ggml-org/llama.cpp · Discussion #18244](https://github.com/ggml-org/llama.cpp/discussions/18244)
44. [\[ENHANCEMENT\] Avoid needless prompt-cache invalidation so local models (llama.cpp) can reuse their KV cache · Issue #1891 · Zoo-Code-Org/Zoo-Code](https://github.com/Zoo-Code-Org/Zoo-Code/issues/1891)
45. [bug(cache): per-session \`Session ID\` in the ephemeral system prompt defeats prefix caching for every new chat · Issue #8148 · nesquena/hermes-webui](https://github.com/nesquena/hermes-webui/issues/8148)
46. [fix(cache): kill long-lived prefix layout — system prompt is now byte-static within a session by teknium1 · Pull Request #24778 · NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/pull/24778)
47. [Prompt Caching — Ollama in Action: Building Safe, Private AI with LLMs, Function Calling and Agents](https://leanpub.com/read/ollama/prompt-caching)
48. [System Prompts can not work on the first round. · Issue #6176 · ollama/ollama](https://github.com/ollama/ollama/issues/6176)
49. [\[Bug\]: Possible prompt-cache regression on sessions\_yield/announce: Ollama cached prefix drops from \~25.4k to \~14.3k tokens · Issue #150081 · openclaw/openclaw](https://github.com/openclaw/openclaw/issues/150081)
50. [MLX vs llama.cpp on M1 Max with 35B Q8 — The Honest Benchmark - DEV Community](https://dev.to/sleepyquant/mlx-vs-llamacpp-on-m1-max-with-35b-q8-the-honest-benchmark-3496)
51. [GGUF vs MLX on Apple Silicon: Same Engine, 37% Slower // famstack.dev](https://famstack.dev/guides/mlx-vs-gguf-part-2-isolating-variables/)
52. [mlx\_lm.server causes macOS kernel panic (IOGPUMemory crash) due to unbounded memory growth · Issue #883 · ml-explore/mlx-lm](https://github.com/ml-explore/mlx-lm/issues/883)
53. [\`mlx\_lm.server\` aborts with Metal OOM after prompt cache grows to \~23-26 GB · Issue #1390 · ml-explore/mlx-lm](https://github.com/ml-explore/mlx-lm/issues/1390)
54. [Multi-round inference leads to insufficient memory crash · Issue #314 · lmstudio-ai/mlx-engine](https://github.com/lmstudio-ai/mlx-engine/issues/314)
55. [mlx\_lm.server returns 1-token completions on Qwen3.6 MTP variants when system prompt is reused with a different user prompt · Issue #1292 · ml-explore/mlx-lm](https://github.com/ml-explore/mlx-lm/issues/1292)
56. [Running a Local LLM Coding Server on MacBook Pro M5 Pro 48 GB - Igor Kulman](https://blog.kulman.sk/running-local-llm-coding-server/)
57. [How My Local Coding Agent Crashed My Mac, and What I Learned About MLX Memory Management](https://medium.com/@michael.hannecke/how-my-local-coding-agent-crashed-my-mac-and-what-i-learned-about-mlx-memory-management-e0cbad01553c)
58. [Support \`--prompt-cache-file\` with mlx\_lm.server · Issue #1178 · ml-explore/mlx-lm](https://github.com/ml-explore/mlx-lm/issues/1178)
59. [Separating code reasoning and editing](https://aider.chat/2024/09/26/architect.html)
60. [R1+Sonnet set SOTA on aider’s polyglot benchmark](https://aider.chat/2025/01/24/r1-sonnet.html)
61. [Architect mode benefit on code format · Issue #2401 · Aider-AI/aider](https://github.com/Aider-AI/aider/issues/2401)
62. [Aider Polyglot Benchmark](https://vibecoderjournal.com/benchmarks/aider-polyglot/)
63. [Qwen3 benchmark results](https://aider.chat/2025/05/08/qwen3.html)
64. [Code editing leaderboard](https://aider.chat/docs/leaderboards/edit.html)
65. [Foundations: SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering · Issue #1902 · jjakimoto/research-issues](https://github.com/jjakimoto/research-issues/issues/1902)
66. [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering](https://hyper.ai/en/papers/2405.15793)
67. [Paper Summary: SWE-agent Agent-Computer Interfaces Enable Automated Software Engineering?](https://blog.astrobot.tech/paper-summary-swe-agent-agent-computer-interfaces-enable-automated-software-engineering)
68. [SWE-TRACE: Optimizing Long-Horizon SWE Agents Through Rubric Process Reward Models and Heuristic Test-Time Scaling](https://arxiv.org/pdf/2604.14820)
69. [GitHub - microsoft/multilspy: multilspy is a lsp client library in Python intended to be used to build applications around language servers. · GitHub](https://github.com/microsoft/multilspy)
70. [multilspy/README.md at main · microsoft/multilspy](https://github.com/microsoft/multilspy/blob/main/README.md)
71. [lsp-client · PyPI](https://pypi.org/project/lsp-client/0.2.3/)
72. [lsp mcp](https://glama.ai/mcp/servers/@CesarPetrescu/lsp-mcp)
73. [test: add StrykerJS mutation testing, changed-files on PR and weekly full run · Issue #737 · rethinkhealth/glion](https://github.com/rethinkhealth/glion/issues/737)
74. [Nico Jansen on X: "👽 We've started on StrykerJS support for \`--incremental\` mode. With this mode active, Stryker will try to only run mutation testing for the changed files since the previous run! We've published a beta release and are looking for feedback 🙏 See https://t.co/mT4Z90ZWTx" / X](https://x.com/_nicojs/status/1542055462610391040)
75. [Mutation Testing -- Stryker, Code Quality, and Killing Mutants](https://qaskills.sh/blog/mutation-testing-stryker-guide)
76. [1.2.11 • stryker-mutation • testland • Registry • Tessl](https://tessl.io/registry/testland/stryker-mutation/1.2.11)
77. [Mutation Testing at Scale in Ephemeral MicroVMs · PandaStack](https://www.pandastack.ai/blog/mutation-testing-at-scale-ephemeral-microvms/)
78. [Mutation Testing With Stryker: Complete Guide 2026](https://qaskills.sh/blog/mutation-testing-stryker-guide-2026)
79. [How to Handle Mutation Testing](https://oneuptime.com/blog/post/2026-01-24-mutation-testing/view)
80. [Web Access protection](https://help.eset.com/ees_mac/10/en-US/web_access_protection.html)
81. [HTTP(S) traffic scanning | ESET Endpoint Security](https://help.eset.com/ees/10.1/en-US/idh_config_epfw_scan_http.html)
82. [\[KB2654\] Slow download speeds while using the HTTP protocol with ESET Windows products](https://support.eset.com/en/kb2654-slow-download-speeds-while-using-the-http-protocol-with-eset-windows-products)
83. [Troubleshooting connection speed issues with Web Guard in Avast Antivirus](https://support.avast.com/en-ww/article/troubleshoot-antivirus-web-shield/)
84. [Slow Lan Network speed : Paid Avast Internet Security - Avast Free Antivirus / Premium Security - Avast Community](https://community.avast.com/t/slow-lan-network-speed-paid-avast-internet-security/744281)
85. [Slow SMB files transfer speed - Windows Server](https://learn.microsoft.com/en-us/troubleshoot/windows-server/networking/slow-smb-file-transfer)
86. [Very slow upload speeds with PC using Windows 11 - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/4137425/very-slow-upload-speeds-with-pc-using-windows-11)
87. [Very slow upload speeds with PC using Windows 11 - Microsoft Q&A](https://learn.microsoft.com/en-us/answers/questions/4137425/very-slow-upload-speeds-with-pc-using-windows-11?page=7)
88. [Slow Upload Speed All Of A Sudden - Page 3 - Networking](https://www.bleepingcomputer.com/forums/t/782698/slow-upload-speed-all-of-a-sudden/page-3)
89. [Re: Killer Intelligence Center Slow Upload Speed - Page 2 - Intel Community](https://community.intel.com/t5/Ethernet-Products/Killer-Intelligence-Center-Slow-Upload-Speed/m-p/1566407)
90. [Get-NetTCPSetting (NetTCPIP)](https://learn.microsoft.com/en-us/powershell/module/nettcpip/get-nettcpsetting?view=windowsserver2025-ps)
91. [All network traffic slowed when Tailscale client is connected · Issue #8524 · tailscale/tailscale](https://github.com/tailscale/tailscale/issues/8524)
92. [Windows Kills SMB Speeds When Using Tailscale](https://news.ycombinator.com/item?id=42132131)
93. [Very slow up/slow download speeds if Hyper-V external switch enabled](https://www.elevenforum.com/t/very-slow-up-slow-download-speeds-if-hyper-v-external-switch-enabled.902/)
94. [Very slow up/download speeds with HyperV external switch setup - Windows 10 Help Forums](https://www.tenforums.com/virtualization/183576-very-slow-up-download-speeds-hyperv-external-switch-setup.html)
95. [Solved: Hyper-V External Virtual Switch Bridge Mode - Slow download speeds on 8265AC - Intel Community](https://community.intel.com/t5/Wireless/Hyper-V-External-Virtual-Switch-Bridge-Mode-Slow-download-speeds/td-p/1222149)
96. [Slow download speed after creating External Virtual Switch - Windows 10 Help Forums](https://www.tenforums.com/virtualization/96869-slow-download-speed-after-creating-external-virtual-switch.html)
97. [Block connections to your Mac with a firewall - Apple Support](https://support.apple.com/guide/mac-help/block-connections-to-your-mac-with-a-firewall-mh34041/13.0/mac/13.0)
98. [macOS Firewall Explained: What It Misses (2026)](https://www.netmute.com/blog/macos-firewall-explained)
