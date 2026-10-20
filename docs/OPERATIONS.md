# Qualification core: operations

All commands run through `uv run`. Nothing here calls a model API; judgments are made in the agent session.

## Setup and onboarding

```bash
uv sync
cp .env.example .env
uv run python -m src.profiles intake --profile my-buyers
```

Fill keys in your editor; never put them into chat or committed files. Only APIFY_TOKEN is required for collection. GETLEADS_API_KEY enables optional gap-filling.

The interview saves each answer in SQLite, so an interrupted session resumes with the same command. Claude Code's `/setup` drives the same state one question at a time:

```bash
uv run python -m src.profiles next-question --profile my-buyers
uv run python -m src.profiles answer --profile my-buyers --key one_liner --value 'Sales software for B2B teams'
uv run python -m src.profiles draft --profile my-buyers
```

`draft` requires the interview to be complete. Each targeting condition needs an explicit mandatory/preferred/excluded mode. Optional outreach questions accept `skip` and are stored empty. The agent may assemble richer conditions from confirmed answers and register them with `uv run python -m src.profiles intake --profile my-buyers --document <yaml>`; this still saves a draft, not an approval. A website proposal must be confirmed, not automatically activated.

Profiles live at `profiles/<name>/client-profile.yaml`, with `icp-prompt.txt` and immutable version directories. One installation supports one business and multiple named ICPs. Data stays local and ignored by Git. [Example profile](../examples/client-profile.example.yaml). A pre-2.0 `icp.yaml` cannot be loaded; use it as a reminder while answering the interview.

## Criteria and signals

Each segment contains separate `company` and `person` condition lists. All mandatory conditions in that segment must match; an excluded match disqualifies; preferred conditions do not block. A lead must match both company and person conditions in the same segment. Several segments provide alternative qualification routes.

A condition has `id`, `field`, `operator`, `value`, `mode`, and an optional `description`. Operators are `equals`, `one_of`, `contains`, `range`, and `semantic`. Numeric ranges use `[minimum, maximum]`; a provider headcount band must be fully within the hard bounds, otherwise overlapping boundaries need review. Total headcount never establishes department/team count. Fields absent from evidence remain unknown.

Company facts use `name`, `linkedin_url`, `domain`, `industry`, `headcount`, `description` and `location`; custom evidence can include `served_markets` or `team_headcount`. Person facts include `location`, current-employment `title` and `employment`. Job location is not buyer location. `semantic` conditions require evidence-bound in-session judgments; arbitrary custom fields cannot be manufactured.

Signal definitions include `id`, `kind`, `entity` (company/person), `description`, `evidence_required`, `max_age_days` and `mandatory`. Signals default to priority. An explicitly mandatory signal without dated supporting evidence holds qualification. Active signal research is outside this release. Existing evidence can be imported as JSON records containing `entity_type`, `entity_id`, `kind`, `source_url`, `event_date` (ISO date or null) and `detail`:

```bash
uv run run.py --slug saved-run --icp profiles/my-buyers/client-profile.yaml \
  --skip-harvest --no-resolve --no-enrich --import-signals signals.json
```

Importing evidence does not independently verify the source. Missing/future/stale event dates do not establish a current signal; collection time never substitutes for an event date.

## Calibrate before bulk qualification

Use `/icp-prompt-builder`, which reads the local skill. Select exactly 10 distinct saved companies, with likely matches, likely non-matches and edge cases. A cases file is an array such as `[{"company_id": 1, "category": "likely_match"}, ...]`. Valid categories are `likely_match`, `likely_non_match`, `edge_case`.

```bash
uv run python -m src.profiles start-round \
  --icp profiles/my-buyers/client-profile.yaml --cases cases.json
uv run python -m src.profiles round --round 1 --scores calibration-scores.json
uv run python -m src.profiles round --round 1 --review
```

`start-round` prints the prompt, the profile, each company's observations and its `score_context`. The agent uses a subagent to score the packet in-session. After `--scores`, `round --round 1` prints the packet and a review block: the full prompt followed by every company's decision, confidence, reason and criterion results. Calibration result JSON is `{"results": [{"company_id": 1, "score_context": "<copied>", "decision": "qualified", "confidence": 0.9, "reason": "...", "criterion_results": [...]}]}`. Company criteria use the semantic format below; structured conditions are checked by code. Do not fabricate company data or human approval.

Human review must be explicit. `--review` shows the review block and asks the human at the terminal. Agents record actual user feedback with `round --round 1 --feedback feedback.json`, containing `reviewer: "human"`, `approved: true/false`, `corrections: []` and `human_message` with the user's actual words. Silence is not approval. Corrections reset the streak. Two consecutive approved rounds with no corrections lock the version. Companies cannot repeat within that version's reviewed rounds. Rule/prompt changes create a draft version with no inherited approval. Feedback against stale evidence cannot lock a prompt. `round --round 1 --abandon 'Evidence changed'` closes an unfinished round and frees its companies for a new one.

Fewer than 10 suitable companies is reported as a shortage and no round is opened: keep the ICP provisional, use saved evidence first, and collect more only with authorized spending. Tests with fictional human feedback do not calibrate a real ICP.

## Collect, qualify and export

```bash
uv run run.py --slug example-2026-10-20 \
  --icp profiles/my-buyers/client-profile.yaml \
  --post 'https://www.linkedin.com/posts/.../' --sample --dry-run
```

A post link collects only that post. Use `--profile` for a personal profile's recent posts; company pages are not engagement targets. `--sample` allows provisional collection before calibration: one post, at most 10 reactions and no comments, capped at $0.10. Without `--sample`, a new harvest requires a locked ICP.

`--dry-run` prints what would be spent and makes no calls: the harvest limits and hard cap, then the profile and company lookup caps (with candidate counts once engagers are saved). Remove `--dry-run` for an authorized collection. Raw responses are persisted before parsing. Missing person URLs/current roles are resolved before companies. Company enrichment uses current employment IDs/URLs first and bounded name search only when needed; name-only matches never transfer account facts. Shared companies are enriched once. Complete search responses are reused. `--getleads` optionally fills required gaps through person LinkedIn URLs; company facts attach only with corroborated company identity/domain. Incidental emails remain separately stored and unverified.

Default durable limits:

| Stage | Limit |
|---|---|
| Engagement harvest | $1 combined hard cap; sample cap $0.10 |
| Person/profile resolution | 10 lookups, $0.25 reserved hard cap total |
| Company enrichment/search | 10 lookups, $0.25 reserved hard cap total |
| GetLeads, when requested | 100 submitted identities per slug |

Override with `--harvest-max-usd`, `--profile-max-lookups`, `--profile-max-usd`, `--company-max-lookups`, `--company-max-usd`, and `--max-lookups`. Profile/company requests each reserve at most $0.02 within the total cap; actual provider charges and returned coverage may differ. Unknown submissions require checking the provider before `--retry-unknown`; remote run IDs allow resuming a known run without paying for a new POST. Misses and completed provider responses are reused for 30 days. Failed/unknown submissions count toward persistent limits.

For a strict offline replay, disable enrichment explicitly:

```bash
uv run run.py --slug saved-run --icp profiles/my-buyers/client-profile.yaml \
  --skip-harvest --no-resolve --no-enrich --dump
```

`--skip-harvest` alone can still fill missing evidence; it does not disable all paid stages.

### In-session assessment protocol

`output/<slug>/assessment-packet.json` contains pending entities, criteria, observations, dependencies and score_context. Judge companies first; people are emitted only after their company qualifies in that segment. Submit partial, current judgments, rerun, then judge newly eligible people:

```bash
uv run python -m src.qualify --slug saved-run --icp profiles/my-buyers/client-profile.yaml --dump
uv run python -m src.qualify --slug saved-run --icp profiles/my-buyers/client-profile.yaml --scores scores.json
```

`src.qualify` works on any saved slug without paid stages; the run.py form with `--skip-harvest --no-resolve --no-enrich` is equivalent. A result in `{"results": [...]}` has this shape:

```json
{
  "entity_type": "company",
  "entity_id": 1,
  "employment_id": null,
  "segment": "core",
  "score_context": "copied exactly from the packet",
  "decision": "qualified",
  "confidence": 0.9,
  "reason": "Relevant B2B software company with supported account criteria.",
  "criterion_results": [{
    "criterion_id": "core-industry",
    "outcome": "match",
    "confidence": 0.9,
    "reason": "The supplied description explicitly describes B2B software.",
    "evidence_quote": "B2B software",
    "evidence_refs": [1]
  }]
}
```

Use only IDs/quotes actually supplied. Person results use the person entity ID and employment_id from the packet. Criterion IDs, entity IDs, employment, segment and score_context must all agree. Semantic outcomes are `match`, `no_match` or `unknown`. A confidence below 0.6 requires review. Code checks structured conditions and rejects verdicts that contradict mandatory evidence. Preferred criterion matches and fresh optional signals each add one point to priority_score, ordering leads within their decision group. Numeric scores are not qualification gates.

`uv run src/enrich.py --icp <profile> --slug <slug> --dry-run` previews the company stage on its own.

### Outputs

- `all_leads.csv`: one selected company/employment/segment per engager, with company/person/overall decisions, separate reasons, eligibility, profile version, context and observation references. `employment_decisions` retains other current role/segment assessments. An eligible match takes precedence when several roles qualify. Columns are listed in [SCHEMAS.md](SCHEMAS.md).
- `qualified_leads.csv`: only current, locked-ICP, fully qualified and unsuppressed people. Uncalibrated results never appear here.
- `assessment-packet.json`: pending semantic work; `core-run.json`: profile/version receipt.
- Chat summary: `uv run src/summary.py --input output/<slug>/all_leads.csv` prints decision counts, the eligible count and every person's company/person reasons.

Known campaign history, suppressions and opt-outs still block eligibility. Rejections and review holds are retained. CSV files are snapshots: re-export after evidence, rule or prompt changes.

## Ledger: suppressions and outreach history

```bash
uv run src/ledger.py suppress --domain customer.example --reason customer
uv run src/ledger.py suppress --csv teammates.csv --reason teammate
uv run src/ledger.py mark --campaign q4-sequence --status contacted --csv sent.csv
uv run src/ledger.py mark --campaign q4-sequence --status replied --url https://www.linkedin.com/in/example
uv run src/ledger.py stats
```

`suppress` takes a URL, email, domain or a CSV with `linkedin_url`/`email` columns. `mark` records what was sent or answered outside this tool; statuses are contacted, replied, excluded, imported and failed, and a later `failed` never overrides a confirmed import or reply. Both commands print how many people were actually recorded. Anyone suppressed or in a campaign drops out of `qualified_leads` immediately.

## SQLite and migration

Default database: `data/ledger.sqlite`; override with `LEDGER_DB`. The first core upgrade creates `ledger.sqlite.before-core-v2.sqlite` using SQLite backup. Original tables remain intact.

```bash
uv run run.py --slug saved-run --migrate --output-dir ./output
```

This imports available raw reactions/comments, normalized engager/post files and prior provider caches offline. Full raw files are marked `full`; normalized-only records are explicitly marked `normalized_only`. Missing historical raw responses are not recreated or re-purchased. Imported records retain original metadata when present; migration timestamps do not establish event dates.

The connected layers are:

```text
provider_requests → raw_records (immutable)
                         |
          observations → people / companies / employment
                         |
          versioned ICPs → company/person assessments
                         |
              current_assessments → qualified_leads VIEW
```

`apify_records` and `getleads_records` are separate provider views over the same evidence store. Normalized records can reference their original raw source. Employment and assessment dependencies prevent old-employer facts or stale scores from qualifying a person. Raw data and assessments remain available after rejection, restart or rule changes. Signal event dates, collection dates, incidental emails and campaign history stay separate from fit evidence.

## Validation

```bash
uv run python -m unittest discover -s tests -v
```

The tests run offline with fictional fixtures and need only requests, PyYAML and python-dotenv. They exercise the software; they never calibrate a real ICP.
