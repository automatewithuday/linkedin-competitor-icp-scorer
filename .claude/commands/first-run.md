# Small qualification run

Read CLAUDE.md and docs/OPERATIONS.md. Select the user's named ICP (ask only if several exist). A LinkedIn post URL means --post; a personal profile URL means --profile. Company pages are enrichment inputs, not engagement targets.

Run `uv run run.py ... --dry-run` first and state the previewed harvest, profile and company caps before an authorized live collection. If the ICP is not locked, use --sample for at most 10 engagers or --skip-harvest for paid data already saved. Do not fabricate calibration approval. run.py stores raw data before normalization and qualifies companies before their people. Company enrichment is enabled by default; --getleads is optional gap-filling and needs its own preview/authorization.

Use the assessment packet to judge company criteria first, then people at companies that qualify in the same segment. Use in-session subagents, copy score_context and cite observation IDs/quotes. Submit with `uv run python -m src.qualify --slug <slug> --icp <profile> --scores <json>` and repeat until nothing is pending; completed provider requests are reused. Unknown submissions require provider review before explicit retry.

Finish with `uv run src/summary.py --input output/<slug>/all_leads.csv` and paste it. An uncalibrated ICP produces provisional results but never qualified-only leads. The tool stops at the CSVs; outreach happens elsewhere.
