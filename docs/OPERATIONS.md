# Qualification core: operations

## Setup and onboarding

```bash
uv sync
cp .env.example .env
uv run setup_icp.py --profile my-buyers
```

Fill keys in your editor; never put them into chat or committed files. Only APIFY_TOKEN is required for collection. GETLEADS_API_KEY enables optional gap-filling. Scoring is performed by the agent in-session; no separate model API is called by the core.

The interview saves each answer in SQLite. Run the same command after interruption. Claude Code's `/setup` uses the same state:

```bash
uv run python -m src.profiles next-question --profile my-buyers
uv run python -m src.profiles answer --profile my-buyers --key one_liner --value 'Sales software for B2B teams'
uv run python -m src.profiles draft --profile my-buyers
```

`draft` requires the interview to be complete. Each targeting condition needs an explicit mandatory/preferred/excluded mode. Optional outreach questions may be left empty. The agent may assemble richer conditions from confirmed answers with `setup_icp.py --profile my-buyers --document <yaml>`; this still saves a draft, not an approval. A website proposal must be confirmed, not automatically activated.

Existing configuration is an interview seed:

```bash
uv run setup_icp.py --profile my-buyers --seed icp.yaml
```

Profiles live at `profiles/<name>/client-profile.yaml`, with `icp-prompt.txt` and immutable version directories. One installation supports one business and multiple named ICPs. Data stays local and ignored by Git. [Example profile](../examples/client-profile.example.yaml).

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

The agent uses a subagent to score the packet in-session and shows the full prompt plus all 10 decisions and reasons. Calibration result JSON is `{"results": [{"company_id": 1, "score_context": "<copied>", "decision": "qualified", "confidence": 0.9, "reason": "...", "criterion_results": [...]}]}`. Company criteria use the semantic format below; structured conditions are checked by code. Do not fabricate company data or human approval.

Human review must be explicit. Agents may record actual user feedback with `round --round 1 --feedback feedback.json`, containing `reviewer: "human"`, `approved: true/false`, `corrections: []` and `human_message` containing the user's actual words. Silence is not approval. Corrections reset the streak. Two consecutive approved rounds with no corrections lock the version. Companies cannot repeat within that version's rounds. Rule/prompt changes create a draft version with no inherited approval. Feedback against stale evidence cannot lock a prompt. Use `round --round 1 --abandon 'Evidence changed'` to close an unfinished round.

If fewer than 10 suitable companies are available, keep the ICP provisional and report the shortage. Saved evidence can be used first; new sample collection needs authorized spending. Tests with fictional human feedback do not calibrate a real ICP.

## Collect, qualify and export

```bash
uv run run.py --slug example-2026-10-20 \
  --icp profiles/my-buyers/client-profile.yaml \
  --post 'https://www.linkedin.com/posts/.../' --sample --dry-run
```

A post link collects only that post. Use `--profile` for a personal profile's recent posts; company pages are not engagement targets. `--sample` allows provisional collection before calibration: at most 10 reactions and no comments on one post. Without `--sample`, a new harvest requires a locked ICP.

Remove `--dry-run` for an authorized collection. Raw responses are persisted before parsing. Missing person URLs/current roles are resolved before companies. Company enrichment uses current employment IDs/URLs first and bounded name search only when needed; name-only matches never transfer account facts. Shared companies are enriched once. Complete search responses are reused. `--getleads` optionally fills required gaps through person LinkedIn URLs; company facts attach only with corroborated company identity/domain. Incidental emails remain separately stored and unverified.

Default durable limits:

| Stage | Limit |
|---|---|
| Engagement harvest | $1 combined hard cap; sample cap $0.10 |
| Person/profile resolution | 10 lookups, $0.25 reserved hard cap total |
| Company enrichment/search | 10 lookups, $0.25 reserved hard cap total |
| GetLeads, when requested | 100 submitted identities per slug, shared with legacy submission history |

Override with `--harvest-max-usd`, `--profile-max-lookups`, `--profile-max-usd`, `--company-max-lookups`, `--company-max-usd`, and `--max-lookups`. Profile/company requests each reserve at most $0.02 within the total cap; actual provider charges and returned coverage may differ. `--dry-run` makes no provider/model calls. Unknown submissions require checking the provider before `--retry-unknown`; remote run IDs allow resuming a known run without paying for a new POST. Misses and completed provider responses are reused for 30 days. Failed/unknown submissions count toward persistent limits.

For a strict offline replay, disable enrichment explicitly:

```bash
uv run run.py --slug saved-run --icp profiles/my-buyers/client-profile.yaml \
  --skip-harvest --no-resolve --no-enrich --dump
```

`--skip-harvest` alone can still fill missing evidence; it does not disable all paid stages.

### In-session assessment protocol

`output/<slug>/assessment-packet.json` contains pending entities, criteria, observations, dependencies and score_context. Judge companies first; people are emitted only after their company qualifies in that segment. Submit partial, current judgments, rerun, then judge newly eligible people:

```bash
uv run run.py --slug saved-run --icp profiles/my-buyers/client-profile.yaml \
  --skip-harvest --no-resolve --no-enrich --scores scores.json
```

A result in `{"results": [...]}` has this shape:

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

Use only IDs/quotes actually supplied. Person results use the person entity ID and employment_id from the packet. Criterion IDs, entity IDs, employment, segment and score_context must all agree. Semantic outcomes are `match`, `no_match` or `unknown`. A confidence below 0.6 requires review. Code checks structured conditions and rejects verdicts that contradict mandatory evidence. Preferred criterion matches and fresh optional signals each add one point to priority_score, ordering leads within their decision group. Numeric scores are not qualification gates. No separate model API is invoked.

`src/rank.py --icp <profile> --slug <slug> --dump` is an equivalent scoring entrypoint. For a completed core run it can find the profile from core-run.json. `src/enrich.py --icp <profile> --slug <slug> --dry-run` previews the company stage.

### Outputs

- `all_leads.csv`: one selected company/employment/segment per engager, with company/person/overall decisions, separate reasons, eligibility, profile version, context and observation references. `employment_decisions` retains other current role/segment assessments. An eligible match takes precedence when several roles qualify.
- `qualified_leads.csv`: only current, locked-ICP, fully qualified and unsuppressed people. Uncalibrated results never appear here.
- `assessment-packet.json`: pending semantic work; `core-run.json`: profile/version receipt.
- Chat summary: all decision counts, eligible count, names and missing evidence. `src/summary.py --input <csv>` also supports core exports.

Known campaign imports, suppressions and opt-outs still block eligibility. Rejections and review holds are retained. CSV files are snapshots: re-export after evidence, rule or prompt changes. Legacy integrations refuse schema_version=2, so these exports cannot trigger sending through old campaign commands.

## SQLite and migration

Default database: `data/ledger.sqlite`; override with `LEDGER_DB`. The first core upgrade creates `ledger.sqlite.before-core-v2.sqlite` using SQLite backup. Original tables/files remain intact.

```bash
uv run run.py --slug saved-run --migrate --output-dir ./output
```

This imports available raw reactions/comments, normalized engager/post files and prior provider caches offline. Full raw files are marked `full`; normalized-only legacy records are explicitly marked `normalized_only`. Missing historical raw responses are not recreated or re-purchased. Imported records retain original metadata when present; migration timestamps do not establish event dates.

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

## Validation and archived integrations

```bash
uv run --extra legacy python -m unittest discover -s tests -v
```

The optional `legacy` dependency group supports the retained integrations and their regression tests. Default installation uses only requests, PyYAML and python-dotenv. Active research, email discovery/verification, Jev Activity Intel through TypeSafe, drafts, Supabase and campaign imports are outside this core release. The historical commands remain documented in [legacy operations](LEGACY_OPERATIONS.md); run.py/rank.py/enrich.py/setup_icp.py require explicit `--legacy` for that archived path. Never use it to bypass core calibration or review holds.
