# Small qualification run

Read CLAUDE.md and docs/OPERATIONS.md. Select the user's named ICP (ask only if several exist). A LinkedIn post URL means --post; a personal profile URL means --profile. Company pages are enrichment inputs, not engagement targets.

Preview the requested harvest/profile/company caps and use authorized limits. If the ICP is not locked, use --sample for at most 10 engagers or --skip-harvest for paid data already saved. Do not fabricate calibration approval. Run.py stores raw data before normalization and qualifies companies before their people. Company enrichment is enabled by default; --getleads is optional gap-filling and needs its own preview/authorization.

Use the assessment packet to judge company criteria first, then people at companies that qualify in the same segment. Use in-session subagents, copy score_context and cite observation IDs/quotes. Repeat --skip-harvest with --scores; completed provider requests are reused. Unknown submissions require provider review before explicit retry.

Return all_leads.csv, qualified_leads.csv and a readable summary of every decision, including missing evidence. An uncalibrated ICP can produce provisional results but never qualified-only leads. No active signal research, email discovery, Jev checks, drafts or campaign imports occur in this core release.
