# Project map

Written by buckets: what each file is and how they connect. Kept in step with the files by content hash.

- .buckets.json (1 line)
- .gitignore (2 lines)
- README.md (544 lines): Empty the pool with many small buckets.
- buckets.py (5882 lines): buckets: empty the pool with many small buckets. Declares Buckets, GitAssistant, ProjectMemory, CodeIndex, Ticker, Run, ThinkingCut, ModelError, …. Used by test_buckets.py.
- docs/field-notes.md (454 lines): A record of building and using `buckets`: what broke, what we measured, and how each real run went, including the failures.
- docs/handoff.md (69 lines): Where buckets stands after the second build day, for whoever (or whichever session) picks it up next.
- docs/roadmap.md (133 lines): What's left, roughly in the order I'd do it, with why and how big.
- test_buckets.py (2121 lines): Tests for the parts of buckets that don't need a model: run with `python3 -m unittest -v`. Declares GitAssistantTests, FasterRuns, BigRepoTools, Escalation, TargetedReading, CodeIndexTests, Mechanical, ProjectMemoryTests, …. Imports buckets.py.
- ts_index.cjs (423 lines): A syntax-only index of a TypeScript/JavaScript repository, for buckets.
