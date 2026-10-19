# Sample outreach list

[Open the sample CSV](ranked_engagers.sample.csv). All people, employers, comments, scores, emails and activity verdicts are fictional illustrations; no live Jev or data-provider calls were used to create it.

Example buyer criteria: in-house Sales and Revenue Operations leaders, in the US, UK, Canada or Germany, at plausible B2B accounts. Target headcount is 50–500; qualified examples include fictional B2B software and 51–200 employee account evidence; missing required evidence is held for review. The fit threshold is 4. This example excludes consulting providers and locations outside the target countries.

Read these columns together:

| Column | What it explains |
|---|---|
| `qualification_threshold`, `qualification_status`, `qualification_reason` | The required fit score, whether the person qualifies, fails or needs review, and which evidence, mismatch or missing facts explain their score. |
| `intent_reason`, `comment_excerpt`, `comment_post_url` | Why the comment signals high, medium or low intent; reactions alone provide no comment-based buying signal. |
| `ledger_status`, `eligibility_reason` | Whether recorded campaign history, suppression or stale scoring blocks the next outreach stages. Passing ledger checks does not override the ICP threshold. |
| `activity_status`, `activity_reason` | Why LinkedIn looks suitable now, looks less promising, was not checked, or could not be assessed. |
| `activity_model`, `outreach_ready_probability`, `most_recent_activity_url` | The configured Jev model, readiness estimate and supporting activity link when a judgment exists. |

Jane is a strong fit with a specific need and recent activity. Morgan meets the ICP threshold but has weak recent activity evidence. Sam has a relevant problem but fails this ICP because they sell consulting services. Riley needs review because employer, industry and size evidence are missing. Nina is outside the target countries. Taylor has no usable public profile URL; Quinn's activity judgment failed. Neither an unchecked profile nor a failed check establishes inactivity.

The sample illustrates `--activity` and GetLeads `--find-emails` output together. Discovered work emails are UNVERIFIED and sendable=false; a separate verification step is required before email imports. Actual runs write reasons for every scored person, including rejected people. Activity is checked only for eligible people above the fit threshold with a usable public URL. Activity readiness is advisory and does not automatically block imports. The original `outreach_reason` column remains as an alias of `activity_reason`.

The sample shows selected human-facing columns. Real exports also include internal identity and ledger provenance fields.

[GetLeads enrichment example](getleads-enrichment.sample.csv) shows the optional ICP evidence step: Jane passes with a compatible company-size band, Morgan fails on company size, and Riley needs review with missing employer evidence after a no-match lookup. These people, provider results and explanations are fictional; the file was generated through the CSV exporter without API calls. This separate example targets US Sales leaders at software companies with 50–500 employees. `getleads_*` fields identify database evidence; no email or activity checks are included.
