# Project instructions

- Implement the project in small, verifiable stages defined by `跨Harness多Agent团队编排系统实施计划.md`.
- Keep runtime state and downloaded tool caches under `.agent-hub/`; never commit credentials or session tokens.
- Prefer Python standard-library code for the orchestration core. Vendor SDK imports must remain isolated inside their adapters.
- Stage 0 probes must default to read-only behavior and must never print credential contents.
- Do not advance to a later stage when an earlier stage's exit conditions are unmet; record the gap in `SPIKE_REPORT.md`.
- Run the unit tests after every behavior change.
- Treat `PROJECT_PROGRESS.md` as the handoff/status index and update it together with the active stage report after each completed slice.
- Use `stageN: complete ...` only after every exit condition for that stage is evidenced; use `stageN: checkpoint ...` for incomplete work.
