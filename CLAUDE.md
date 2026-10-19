# Operating the qualification core

Read README.md and docs/OPERATIONS.md. Default flow: guided ICP -> human calibration -> saved evidence -> company fit -> person fit -> CSV. One business per installation; multiple named ICPs. No active signals, email discovery, Jev checks, drafts or campaign imports in the core release.

Apply local skills/icp-onboarding/SKILL.md and skills/icp-prompt-builder/SKILL.md. Ask one intake question per message. Read the provided website if available, identify proposals as inferred, and wait for corrections. Record every answer with `python -m src.profiles answer`; use `next-question` to resume. Do not turn an inferred preference into a mandatory criterion. Distinguish company location, markets, person location, employee headcount and department/team headcount. Optional outreach answers can be skipped.

Legacy icp.yaml is a draft seed. Do not silently make its conditions mandatory. Named profiles live in profiles/<name>/client-profile.yaml with icp-prompt.txt and version archives. Profiles and provider data stay local and ignored by Git.

Use subagents for calibration scoring in-session; no separate model API calls. Show the full prompt and all 10 decisions/reasons. Require explicit human feedback for every round. Never create human approval on your own. Two consecutive approved rounds without corrections lock that version; corrections reset the streak. A changed profile or prompt creates a draft version. If insufficient companies are available, report the shortage and retain provisional results. Do not fabricate cases or labels for a real ICP.

Run `python run.py --slug <slug> --icp profiles/<name>/client-profile.yaml --post <URL>` (or --profile). Until calibrated, use --sample for a provisional collection or --skip-harvest to reuse data. --dry-run makes no provider/model calls. Default caps: harvest $1 total; up to 10 profile resolutions/$0.25; up to 10 company lookups/$0.25. Sample collection is capped at $0.10 and 10 engagers. State the previewed spend before authorized live collection. Keep secrets in .env; never print credentials.

Company enrichment is connected to qualification. Prefer company IDs/URLs from current employment, enrich each unique company once, and hold ambiguous name searches for review. GetLeads is optional gap-filling through --getleads; no Prospeo dependency. Durable requests prevent duplicate submissions. Unknown outcomes require checking the provider before --retry-unknown. Do not retry an unknown POST automatically.

Scoring is evidence-bound: use `python -m src.qualify --slug <slug> --icp <profile> --dump`. The pending packet is output/<slug>/assessment-packet.json. Company judgments precede person judgments in the same segment. Semantic results need a confidence, reason, exact supporting quote and supplied observation IDs; copy score_context exactly. Never use comments or a numeric fit score to override mandatory criteria. Missing preferred evidence is nonblocking. Missing required evidence or confidence below 0.6 means needs_review.

Use qualified_leads, a joined SQLite view, for the current unsuppressed subset. Keep every raw response before normalizing. Never overwrite raw evidence or discard rejected people. Current employment cannot inherit another company's facts. A company fit in one segment cannot be combined with a person fit in another. Qualification explanations and export proofs must remain current after evidence/ICP/prompt changes.

Every run ends with counts and names/reasons. Paste the useful summary into chat; link final artifacts. Existing 14-person sample is for offline replay, not proof that the real ICP is calibrated. Do not make paid provider calls to validate implementation.

Validation: `uv run --extra legacy python -m unittest discover -s tests -v`. The core itself only needs PyYAML, python-dotenv and requests. Legacy run.py/rank.py/enrich.py/setup_icp.py paths require --legacy and optional dependencies. Never use legacy flags to evade core approval or export holds. Legacy integrations reject schema_version=2 CSVs.
