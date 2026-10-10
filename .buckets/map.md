# Project map

Written by buckets: what each file is and how they connect. Kept in step with the files by content hash.

- .buckets.json (1 line)
- .gitignore (1 line)
- README.md (417 lines): Empty the pool with many small buckets.
- buckets.py (5345 lines): buckets: empty the pool with many small buckets. Declares Buckets, GitAssistant, ProjectMemory, CodeIndex, Ticker, Run, ThinkingCut, ModelError, …. Used by test_buckets.py.
- docs/field-notes.md (343 lines): A record of building and using `buckets`: what broke, what we measured, and how each real run went, including the failures.
- docs/handoff.md (67 lines): Where buckets stands after the first build session, for whoever (or whichever session) picks it up next.
- docs/roadmap.md (93 lines): What's left, roughly in the order I'd do it, with why and how big.
- test_buckets.py (1838 lines): Tests for the parts of buckets that don't need a model: run with `python3 -m unittest -v`. Declares GitAssistantTests, FasterRuns, Escalation, TargetedReading, CodeIndexTests, Mechanical, ProjectMemoryTests, Offload, …. Imports buckets.py.
- ts_index.cjs (423 lines): A syntax-only index of a TypeScript/JavaScript repository, for buckets.
