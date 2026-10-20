# Schemas

Reference for the exports this tool writes, the assessment packet the agent judges, and the provider payloads behind them.

## Exports (`schema_version` 2)

`all_leads.csv` and `qualified_leads.csv` share one header. One row per engager, choosing the best current employment/segment pair; the others stay in `employment_decisions`.

| Column | Meaning |
|---|---|
| `schema_version` | Always `2`. |
| `name`, `linkedin_url` | The person; `linkedin_url` is the public `/in/` URL when known, else empty. |
| `company_id`, `company_name`, `company_linkedin_url` | The current employer this row was judged at. |
| `employment_id`, `current_title` | The employment relationship and the title it carries. |
| `segment` | The ICP segment whose company and person criteria both applied. |
| `company_decision`, `company_reason` | qualified / not_qualified / needs_review for the company, with the explanation. |
| `person_decision`, `person_reason` | The same for the person at that company. People are only judged after their company qualifies. |
| `qualification_status`, `qualification_reason` | Overall decision. A draft (unlocked) ICP turns `qualified` into `needs_review`. |
| `eligibility_status` | `eligible` (in the `qualified_leads` view), `hold`, `suppressed:<reason>` or `in_campaign:<campaign>`. |
| `priority_score` | Count of matched preferred criteria plus fresh optional signals; orders rows within a decision group, never a gate. |
| `profile_version`, `assessment_id`, `score_context` | Provenance: which ICP version and assessment produced the row, and the evidence fingerprint it was bound to. |
| `evidence_refs` | JSON list of observation IDs the decision cites. |
| `employment_decisions` | JSON list of every company/segment pair assessed for this person. |

Rows are ordered qualified, then needs_review, then not_qualified, by priority within each group.

## Assessment packet (`output/<slug>/assessment-packet.json`)

`{"schema_version": 2, "profile_version": N, "prompt": "...", "icp": {...}, "packets": [...]}`. Each packet is one pending entity:

| Field | Meaning |
|---|---|
| `entity_type`, `entity_id` | `company` or `person` and its ID. |
| `company_id`, `employment_id`, `segment` | The company (and for people, the employment) and segment being judged. |
| `score_context` | Fingerprint of ICP version, prompt, entity and evidence. Copy it exactly into the judgment. |
| `facts` | The current observed facts for the entity. For people: `title`, `employment`, `current_roles` (this employment only), `location`, `headline`, `about`, `comments`. |
| `observation_ids` | The only IDs a judgment may cite in `evidence_refs`. |
| `criteria`, `signals` | The segment's criteria for this entity type and its signal definitions. |
| `dependencies` | Entity hashes the assessment depends on; any change makes it stale. |

A judgment is `{entity_type, entity_id, employment_id, segment, score_context, decision, confidence, reason, criterion_results}`, with each criterion result `{criterion_id, outcome (match/no_match/unknown), confidence, reason, evidence_quote, evidence_refs}`. See [operations](OPERATIONS.md#in-session-assessment-protocol).

## Calibration packet

`start-round` and `round --round N` print `{round_id, status, prompt, icp, cases, results}`. Each case is `{company_id, category, score_context, evidence}`; `results` is empty until scored, then holds the submitted `{company_id, score_context, decision, confidence, reason, criterion_results}` list. The round review block prints the same decisions as text.

## Profile (`profiles/<name>/client-profile.yaml`)

`schema_version: 2`, `name`, `business {website, description, one_liner, best_customers}`, `segments[]`, `offer {cta, lead_magnet}`, `tone`, `constraints`. Each segment has `id`, `name`, `company[]`, `person[]`, `signals[]`. Criteria: `{id, field, operator (equals/one_of/contains/range/semantic), value, mode (mandatory/preferred/excluded), description}`. Signals: `{id, kind, entity, description, evidence_required, max_age_days, mandatory}`. [Example](../examples/client-profile.example.yaml).

## Apify actors

### Posts and engagers: `harvestapi~linkedin-profile-posts` (`--profile`)

Input `{"targetUrls": [profile], "maxPosts", "postedLimit": "24h|week|month", "scrapeReactions", "maxReactions", "scrapeComments", "maxComments", "commentsPostedLimit", "includeReposts": true, "includeQuotePosts": true}`. Output is a flat dataset keyed by `type`: `post`, `reaction` (with `reactionType`, `actor{}`) and `comment` (with `commentary`, `actor{}`). In this mode `actor{}` carries only `id` (opaque `ACoAA...`), `name`, `linkedinUrl`, `position`, `pictureUrl`; the profile stage resolves the rest. About $0.00175 per item.

### One post: `harvestapi~linkedin-post-reactions` and `harvestapi~linkedin-post-comments` (`--post`)

Input `{"posts": [url], "maxItems": n, "profileScraperMode": "main"}`. In `main` mode `actor{}` is a full profile: `id`, `name`, `linkedinUrl` (vanity), `publicIdentifier`, `headline`, `location`, `currentPosition[]`, `experience[]`, `about`, and more. `post-reaction` / `post-comment` events cost $0.002 each, plus $0.002 per engager for the main profile.

### Profile resolution: `harvestapi~linkedin-profile-scraper`

Used by the profile stage for people without a public URL or current roles. Input `{"profileIds": ["ACoAA..."]}` or `{"urls": [...]}` with `"profileScraperMode": "Profile details no email ($4 per 1k)"`. One item per resolved profile: `id`, `originalQuery`, `publicIdentifier`, `linkedinUrl`, `headline`, `location {linkedinText, parsed}`, `currentPosition[] {position, companyName, companyLinkedinUrl, companyId, location, startDate, endDate, description}`, `experience[]`, `about`. $0.004 per profile. The Apify free plan limits this actor to 10 items per run and returns one `{"error": ...}` item above that; such a run is recorded as `unknown`, never as a miss.

### Company enrichment: `harvestapi~linkedin-company`

Input `{"companies": [company url]}` or `{"searches": [name]}`. Output company objects with `name`, `linkedinUrl`, `id`, `website`, `employeeCount` / `employeeCountRange`, `industries`, `locations[]`, `description`. The company stage keeps `name`, `linkedin_url`, `linkedin_id`, `domain`, `headcount` (exact or `{min, max}` band), `industry`, `location`, `description`. A search result attaches only when its domain corroborates the saved company.

### Current roles

`evidence.profile_evidence` keeps up to five current positions (`currentPosition[]`, or `experience[]` entries ending in `Present`): `title`, `company`, `company_id`, `company_linkedin_url`, `company_domain`, `start_date`, `end_date`, `employment_type`, `job_location`, `description`, plus `about`. Job location never replaces the person's location.

## GetLeads (`--getleads`)

[REST enrichment](https://www.getleads.io/docs/#2-from-linkedin-profile-url): bearer authentication from GETLEADS_API_KEY, `POST https://app.getleads.io/api/v1/enrich/from-linkedin`, body `{"items":[{"linkedin_url":"..."}],"limit_per_item":1}`. At most 25 URLs per request. Results are correlated by the echoed `linkedinUrl` and must return a matching `data.linkedin_url`. A completed miss is `success:false,data:null`. Missing, duplicated or conflicting identities hold the batch as unknown. Allowance is checked first through `GET /usage/fair-use`.

Allowlisted `data` fields: `current_title`, `current_company_name`/`co_name`, `current_company_website`/`org_domain`/`co_website`, `current_company_industry`/`main_industry`, `employee_count_range` (a band), `co_description`, `current_location`, `contact_country`/`location_country`, `current_company_linkedin_url`/`co_linkedin_url`, `current_company_id`. Company facts attach to the saved company only when the company URL, ID or domain corroborates it. `work_email` is stored separately as unverified and is never scoring evidence. The raw response is kept in `getleads_records`; the cache (`provider_enrichments`) and submission cap (`enrichment_attempts`) live in the ledger tables.
