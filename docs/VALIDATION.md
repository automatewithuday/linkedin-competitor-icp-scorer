# Qualification core: live sample validation

Validated on 20 October 2026 with a fresh, bounded collection from one LinkedIn post. Customer targeting rules, lead identities, raw responses and credentials remain local and are excluded from this repository.

## Sample and results

- Collected 10 reactions with main-profile evidence; no comments.
- Linked nine employers through company LinkedIn identities and enriched each once. One employer remained unresolved.
- Reused four existing person records rather than creating new records for those repeat engagers.
- Used subagents in the agent session to assess companies first, then current buyer roles. No separate model API calls were made.
- Found five provisional company-and-person matches, three non-matches and two evidence holds under the proposed test rules.

The ICP was not human-calibrated. The official export therefore contained **seven `needs_review` rows and three `not_qualified` rows**, with **zero eligible leads**. The qualified-only CSV contained its header and no data rows. A separate local review file distinguished provisional matches from missing-evidence holds without approving outreach.

The first ten-company calibration batch was scored and saved for human review. No human approval was recorded; the approval streak remained zero. A second distinct batch and two consecutive human-approved rounds with zero corrections are still required before bulk qualification.

## Evidence and spending

Apify supplied records for all nine identified company pages. One record lacked an offering description. A single GetLeads lookup matched the person and company domain but also lacked that description, so the company assessment retained the evidence hold.

Observed charges were **$0.07645 on Apify** and **one GetLeads credit**. These are this sample's recorded charges, not a promised per-lead price. No email discovery, active signal research, activity checks, drafting or campaign delivery ran.

Complete provider responses and normalized observations were retained in the shared SQLite database. Export checks confirmed ten distinct person LinkedIn URLs, populated company/person/overall reasons, zero eligible rows and no foreign-key violations. Private CSVs preserve evidence references and assessment contexts.

This live sample exercises the workflow and calibration gate. It does not establish that the customer's ICP is calibrated or that a provisional match has buying intent.
