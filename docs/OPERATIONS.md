# Operations reference

The full command reference and every operational caveat. Start with the [README](../README.md) if you are new; come here when you are running the pipeline for real.

Adapted from [LeadGrowGTM/linkedin-warm-leads](https://github.com/LeadGrowGTM/linkedin-warm-leads), MIT licensed. Original license retained. Upstream baseline: `c7ae920823c6c2e048e6a218a6f6bb4031d1833d`. This version uses direct provider APIs; it does not require Deepline.

## Setup

Python 3.11+ is required. Run commands from the repository root.

```bash
uv sync
cp .env.example .env
uv run setup_icp.py
```

Without uv: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.lock.txt` (exported from `uv.lock`), then replace `uv run` with `python` below. In Claude Code, `/setup` and `/first-run` cover this.

Fill in `.env` locally. Do not paste keys into chat or commit them. You need Apify for harvesting and one LLM for ranking (an OpenAI key, or your Claude subscription via the `claude` CLI; see below), GetLeads for optional ICP enrichment and work-email discovery, and Smartlead only for actual imports. Firecrawl is optional for `--domain` automatic ICP derivation and for the website step in `setup_icp.py`; without a key a plain page fetch is used, which is fine for most marketing sites but misses JavaScript-rendered content. Edit `icp.example.yaml` or use the intake to define your own buyers; the example is not a recommended ICP for your business.

## Scoring LLM

ICP scoring needs a judge. Three options, no code changes:

- **In-session (Claude Code, no key):** after the harvest run `uv run src/rank.py --dump --slug <slug>`, judge every numbered engager with the rubric in `src/rank.py` (`SYSTEM`), write `output/<slug>/scores.json` as `{"results": [{"i": 0, "score_context": "copy from dump", "icp_fit": 1-5, "qualification_reason": "ICP evidence, mismatch or missing facts", "intent_reason": "comment evidence or reaction-only limitation", "intent": "high"|"medium"|"low"|null, "evidence_comment": 2|null, "clean_title": "...", "company_guess": "..."|null}, ...]}` with one object per engager. The dump shows up to 900 characters of each person's comments as `c1`, `c2`, ...; `evidence_comment` names the one the intent rests on, and that comment becomes `comment_excerpt`, with the post it was left on in `comment_post_url`, and continue with `uv run run.py --slug <slug> --skip-harvest --icp icp.yaml --scores output/<slug>/scores.json ...`. The same validation applies to every scorer.

- **OpenAI API key** (automatic): set `OPENAI_API_KEY` in `.env`. Default model `gpt-4.1-mini`, override with `OPENAI_MODEL`. Pay per token; a 60-engager run costs well under a cent.
- **Claude subscription via the CLI** (automatic, no API key; picked automatically when `claude` is on PATH and no OpenAI key is set, which is always true inside Claude Code, so pass `--scores` there): install [Claude Code](https://claude.com/claude-code), run `claude` once to log in, and leave `OPENAI_API_KEY` empty. Scoring shells out to `claude -p` and is billed against your subscription's usage limits instead of dollars. Default model `sonnet`, override with `CLAUDE_MODEL` (`haiku`, `opus`). Each batch of 25 engagers is one CLI call. Output is not temperature-pinned, so scores can differ by one point between reruns.

If both are available, `LLM_PROVIDER=openai` or `LLM_PROVIDER=claude` picks one; otherwise OpenAI wins when its key is set.

The scoring prompt was tuned on live runs against outbound-vendor profiles: matching titles without a named employer cap at 3, and people who sell the same category as you (agencies, consultants, tool founders) cap at 2 even when their title matches the ICP. Adjust `SYSTEM` in `src/rank.py` if your ICP does target consultants or founders.

## Test only Apify and scoring

For an initial test, configure `APIFY_TOKEN` plus one scoring LLM (see "Scoring LLM") in your local `.env`, then run `uv run setup_icp.py` to define your buyers.

```bash
uv run run.py \
  --profile https://www.linkedin.com/in/YOUR-TARGET \
  --slug scraping-test-01 \
  --icp icp.yaml \
  --max-posts 3 --max-reactions 20 --max-comments 10 \
  --posted-limit month
```

This produces `output/scraping-test-01/ranked_engagers.csv` (opaque candidates are looked up before final scoring at $0.004 each; `--no-resolve` skips it), without email enrichment, campaign imports, or database writes. Compare sampled engagers and comments with the source posts, then manually review high, borderline and low ICP scores. The current qualification threshold is `icp_fit >= 4`; missing company size and geography still require verification.

After changing your ICP, rescore the saved scrape without running Apify again:

```bash
uv run src/icp.py --slug scraping-test-01 --icp icp.yaml
uv run src/rank.py --slug scraping-test-01
```

## Run the play

```bash
uv run run.py \
  --profile https://www.linkedin.com/in/COMPETITOR-FOUNDER \
  --slug competitor-2026-09-16 \
  --icp icp.yaml \
  --max-posts 10 \
  --find-emails --min-fit 4 --max-lookups 100
```

Outputs in `output/competitor-2026-09-16/`:

- `posts.json`, `engagers.json`: source evidence and deduplicated engagers.
- `recipient_icp.json`: criteria used for scoring.
- `scores.json`, `rescore.json`: in-session judgments, and the rows to rescore after the profile pass.
- `ranked_engagers.csv`: everyone, including low-fit leads, qualified first, with source profile and post references.
- `resolve_runs.json`: every profile-lookup actor run (run id, ids sent, complete or error), for reconciling Apify charges.
- `activity.csv` and `activity/`: with `--activity`, LinkedIn activity verdicts plus the full per-profile output and raw cache.
- `enriched.csv`: all rows with optional discovered work emails, explicit UNVERIFIED status and sendable=false; miss_reason explains skips, misses or the need for verification.
- `data/ledger.sqlite`: shared GetLeads profile/email cache and durable submission history; successful and missed lookups are reusable for 30 days. Legacy provider cache files are preserved but not read.

`--find-emails` is optional. Default harvesting caps posts at 10, reactions at 100/post and comments at 50/post, over the last month. Use `--posted-limit week` or `24h`. `--max-posts 0` removes the post cap and may increase spend. Email discovery requires fit >=4 by default. --max-lookups caps total GetLeads submissions per slug (100 by default), shared with optional ICP enrichment and including retries. Cached results do not add submissions. Caps limit requests, not currency; check your provider plans.

The rank score combines ICP fit, number of distinct posts engaged with, and comment intent. Reactions alone remain low intent. Engagement is a prioritization signal, not proof of purchase intent. Headline-only scoring cannot establish missing company size, geography, or industry. Review the ranked output before outreach. ICP exclusions and triggers are included in the prompt; this is not a substitute for a CRM suppression list.

GetLeads email discovery matches a public LinkedIn URL only, for qualified, eligible rows. It reuses the ICP-enrichment response when available, avoiding a second paid lookup. Found work emails are UNVERIFIED with sendable=false and miss_reason=verification_required because the enrichment endpoint returns no verification metadata. No-match/no-email results are normal cached misses. Pending or unknown submissions stop the stage until provider review; successful batches survive later failures. Old caches that discarded email fields require one new lookup if email discovery is requested. No email verifier is bundled.

Preview optional email discovery without spending credits:

```bash
uv run src/email_enrich.py --input output/RUN_NAME/ranked_engagers.csv --output output/RUN_NAME/enriched.csv --dry-run
```

Run the same command without `--dry-run` to discover addresses. Use `--slug` only when the input folder does not match the pipeline run name. `--max-lookups` is the shared per-slug cap, and `--retry-unknown` is an explicit retry after provider review. Finding an email does not mark it verified or sendable.

## Smartlead

Create or choose a campaign in Smartlead. Preview an import locally:

```bash
uv run src/push.py --input output/competitor-2026-09-16/enriched.csv \
  --campaign-id 123
```

Review the generated `.smartlead.json` file. Then import explicitly:

```bash
uv run src/push.py --input output/competitor-2026-09-16/enriched.csv \
  --campaign-id 123 --execute
```

Importing into an active campaign can start sending. This code does not create sequences or activate campaigns. It requires fit >=4, `sendable=true`, `email_status=VERIFIED`, and verification within 30 days. It excludes `suppressed=true` rows and deduplicates email addresses. Smartlead's global block list, unsubscribe, bounce and cross-campaign duplicate checks remain enabled. Optional flags: `--min-fit`, `--max-email-age-days`, `--receipt`.

Imports use batches of at most 400 and save confirmed added/skipped counts, IDs and skip details. A timeout, malformed response or inconsistent counts stop the import with an unconfirmed receipt. Reconcile that batch in Smartlead before retrying; writes are not automatically replayed and exactly-once delivery is not guaranteed. A receipt covers one invocation; copy it before retrying if you need to retain all attempts. Local suppression is supported through the CSV column; inbound reply/unsubscribe webhooks are not implemented.

You can pass `--campaign-id 123` to `run.py` with `--find-emails` to generate the preview at the end. Newly discovered GetLeads emails are excluded until separately verified; expect zero eligible emails from discovery alone. To import later, use a separately verified CSV with email_status=VERIFIED, sendable=true and the actual verification time in email_checked_at. Preserve qualification reasons and ledger provenance columns. The orchestrator never executes the campaign import.

## HeyReach (LinkedIn outreach)

```bash
uv run src/push_heyreach.py --input output/<slug>/ranked_engagers.csv --list-id <id>
uv run src/push_heyreach.py --input output/<slug>/ranked_engagers.csv --list-id <id> --execute
```

Preview is the default and writes `<input>.heyreach.json`. `--list-id` adds leads to a HeyReach lead list, which never sends; attach the list to a campaign in HeyReach. `--campaign-id <id> --linkedin-account-id <seat>` adds straight into a running campaign and can start sending. Requires `HEYREACH_API_KEY` for `--execute` only. Eligible rows need fit >= `--min-fit` (default 4) and a LinkedIn URL; member-ID URLs (`/in/ACoAA...`) are accepted since LinkedIn resolves them. Email is optional and attached only when `email_status=VERIFIED`. If `drafts.csv` exists in the same folder, `template`, `connection_note` and `dm` are attached as custom fields alongside `clean_title`, `icp_fit`, `intent`, `rank_score`, `comment_excerpt`, `source_profile` and `post_urls`, so they can be used as personalisation variables in sequences. Batches are 100 (the HeyReach cap); a batch whose response counts do not add up is written to the receipt as unconfirmed and stops the run. Custom-field shape follows the public API as documented by third parties (`customUserFields: [{name, value}]`); verify on the first live push.

## Optional Supabase storage

1. Run [`sql/001_warm_leads.sql`](../sql/001_warm_leads.sql) in your project's SQL editor.
2. Set `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (or legacy `SUPABASE_SERVICE_ROLE_KEY`) in `.env`.
3. Add `--supabase` to the pipeline command, or export an existing CSV:

```bash
uv run src/supabase_export.py \
  --input output/competitor-2026-09-16/enriched.csv \
  --run-id competitor-2026-09-16
```

Rows are upserted into `warm_lead_snapshots` on `(run_id, lead_key)`. Retrying the same run does not create duplicate snapshots. Use a new dated run ID for each new sweep to retain scoring history. A normalized LinkedIn URL, falling back to the actor ID, identifies each lead. The entire CSV row is preserved in JSONB. Supabase never replaces the CSV.

RLS is enabled and public roles have no access. Use the secret key only server-side. Export is chunked, not transactional across batches: after a failure, rerun with the same run ID. Identity can change if an opaque actor later gains a public URL; automatic identity reconciliation is not implemented. Snapshots are observations, not a CRM lifecycle table.

## Ranking

`ranked_engagers.csv` is ordered qualified-first: every row at or above `--min-fit` comes before every row below it, and `rank_score` (fit x engagement x intent) orders within each group. A strong comment from someone outside the ICP therefore never takes an outreach slot from a buyer. The summary lists those commenters separately as referral or introduction leads.

Inside Claude Code (`CLAUDECODE=1`), `run.py` and `rank.py` refuse to score automatically unless `LLM_PROVIDER` is set in `.env`; the agent scores in-session and passes `--scores`. Outside Claude Code, OpenAI or the `claude` CLI is used automatically as before.

## Rerun individual stages

```bash
uv run src/rank.py --slug competitor-2026-09-16
uv run src/draft.py --slug competitor-2026-09-16 --min-fit 4   # drafts.csv; one LLM call per 10 rows
uv run src/email_enrich.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/enriched.csv --min-fit 4 --max-lookups 100
```

Re-running `run.py` starts another paid harvest. Reuse individual stages when recovering. Do not run concurrent jobs against the same slug/output files. No recurring schedule is installed by this project.

The optional upstream `--enrich` company-scraping stage is retained, but its company output is not yet joined into ranking; it is not needed for GetLeads email discovery.

## Validation

```bash
uv run python -m unittest discover -s tests -v
```

Tests use mocked provider responses and do not spend API credits or send emails. Live setup requires your keys, ICP, target profiles and campaign ID.

API references: [GetLeads enrichment](https://www.getleads.io/docs/#2-from-linkedin-profile-url), [Smartlead import](https://api.smartlead.ai/api-reference/campaigns/add-leads), [Supabase API security](https://supabase.com/docs/guides/api/securing-your-api). The original generated PDF setup guide was removed because it described the previous provider stack; this README is the current guide.

## Single-post harvest

Give `--post <url>` instead of `--profile` to harvest one specific post only. Full `linkedin.com/posts/...` and `/feed/update/...` URLs and `lnkd.in` short links all work; `--max-posts` is ignored. The post actors run in full-profile mode, so nearly every engager carries a vanity URL, location and company (about $0.004 per engager instead of $0.002; an occasional reactor still comes back opaque, and resolve picks it up if it qualifies). Comments are not date-filtered in post mode, so an older post keeps all its comments. `--skip-harvest` reuses `posts.json` and `engagers.json` already in the slug folder, which is how an in-session-scored run continues after the harvest without paying Apify twice.

```bash
uv run src/harvest.py --post https://lnkd.in/p/XXXX --slug post-2026-10-19 --max-reactions 100 --max-comments 50
```

Every run ends with `src/summary.py`: counts, fit distribution, resolve, activity and email breakdowns, and the top rows by name. Run it on any CSV with `uv run src/summary.py --input output/<slug>/enriched.csv`.

## LinkedIn activity check (optional)

`--activity` answers one question per qualified lead: does this person actually use LinkedIn right now, and how? Activity Intel fetches recent public posts, comments and reactions (HarvestAPI actors on Apify), computes dates and counts in plain code, and uses **Jev through [TypeSafe](https://typesafe.ai)** for typed judgments. The configured model is `jev-latest` by default (`activity.toml`). Plain-language explanations combine the sampled evidence with those verdicts. No LinkedIn login. Cost is about $0.04 per profile checked (Apify) plus a negligible TypeSafe charge. Set `TYPESAFE_API_KEY` in `.env`.

```bash
uv run run.py --profile https://www.linkedin.com/in/COMPETITOR-FOUNDER --slug competitor-2026-09-16 \
  --icp icp.yaml --activity --find-emails
```

Only rows with `icp_fit >= --min-fit` and a public `/in/` URL are checked. Rows whose opaque `/in/ACoAA...` id was not resolved (see "Opaque profile IDs") get `activity_miss_reason=no_public_url`. Everyone in a harvest engaged within the harvest window by construction, so read the result as a posting-habit and recency signal for prioritisation and channel choice, not as proof of inactivity.

The stage preserves qualification and intent explanations and appends activity fields: `linkedin_outreach_ready`, `outreach_ready_probability` (sort by this), `activity_reason`, `outreach_reason`, `activity_model`, `activity_status` (`active` / `stale` / `inactive` / `no_activity_observed` / `not_checked` / `unknown`), `activity_level`, `activity_confidence`, `days_since_last_activity`, `last_activity_type`, `most_recent_activity_url`, `data_quality_warning`, `activity_miss_reason` (`below_min_fit`, `no_public_url`, `not_in_result`, `no_verdict`). Email enrichment, Supabase and Smartlead read the augmented CSV unchanged; nothing is gated on the verdict. Rows with `data_quality_warning=true` deserve a look before outreach.

`activity_reason` is the readable explanation for every row, including skipped or failed checks; `outreach_reason` mirrors it for compatibility. `activity_model` names the configured model only when a readiness probability was returned. Skipped rows have `activity_status=not_checked`; a failed collection or judgment has `activity_status=unknown` and a data-quality warning. Neither means inactive. Rejected ICP rows explain why they were not checked; suppressed or previously imported people explain the ledger block. Limits and missing public profile URLs are reported explicitly too.

Standalone, always dry-run first; it prints the eligible count, the Apify estimate and the hard spend cap without spending anything:

```bash
uv run src/activity.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/activity.csv --dry-run
uv run src/activity.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/activity.csv --limit 20
```

The full ~40-column result, a JSONL copy and the raw actor output land in `output/<slug>/activity/`. To re-judge after changing `activity.toml` without paying Apify again, add `--from-raw output/<slug>/activity/raw/<run>`. Profile lookups write public URLs back into the harvest, so `ranked_engagers.csv` already carries them. After a rescore, use `--from-raw` to re-judge and re-join activity without recollecting it through Apify.

`activity.toml` holds the fetch caps, the outreach-ready threshold and the exact TypeSafe question wording. It is user-owned. `src/activity_intel.py` is vendored unchanged from [LinkedIn-Profile-Activity-Intel](https://github.com/automatewithuday/LinkedIn-Profile-Activity-Intel) (MIT, commit `7a3aabd`); that README explains the evidence rules, including why reactions only give a lower bound on the date.

## Opaque profile IDs

Reaction items, and every engager from a `--profile` harvest, often carry only LinkedIn's opaque profile ID (`/in/ACoAA...`) instead of a public URL. That hides location and employer from the scorer, and GetLeads, the activity check and Smartlead cannot use the row. Two mechanisms close the gap:

- `--post` harvests ask the post actors for `profileScraperMode: "main"`, so nearly every engager arrives with a vanity URL, `location` and current `company` (+$0.002 per engager on top of $0.002 per reaction/comment; the spend cap includes it). `--profile` harvests cannot do this: that actor has no profile mode.
- **Profile lookups before final scoring** (default): after the first scoring pass, `run.py` runs `src/resolve.py --slug <slug>` over opaque engagers at `icp_fit >= --min-fit - 1` (3 by default): `harvestapi/linkedin-profile-scraper`, $0.004 per profile, hard cap = estimate x 1.25 + $0.05 per run. Results go into `engagers.json` (public URL, `location`, `company`, `resolve_status`). Automatic scorers then rescore on their own. With `--scores` the run stops, lists the indices that gained profile data (also in `rescore.json`), and continues only once `scores.json` has been updated. On a real 151-engager run this cost $0.12 and moved 10 of 53 candidates (4 up, 6 down on geography).

This is the only place profiles are fetched, so every new piece of evidence goes back through scoring before any row reaches activity, drafts, emails or exports:

- **Saved per batch.** Each actor run's results are written before the next run starts; `resolve_runs.json` logs every run id.
- **Failures stop the run.** A provider error, network error or free-plan refusal leaves those rows untouched and the command exits 3, so `run.py` stops before any downstream stage. Rerunning retries only what failed. Pass `--no-resolve` to continue without them.
- **Nothing paid twice.** Resolved profiles are never looked up again. A completed lookup that finds nothing is cached as `not_found` for 30 days; `resolve.py --refresh-misses` retries sooner.
- **Free plan.** HarvestAPI allows 10 ids per run on the free Apify plan. Pass `--resolve-batch 10` (or `resolve.py --batch-size 10`), upgrade the plan, or raise `--min-fit`.

Standalone, dry-run first (prints the lookup count, the estimate and the cap; spends nothing):

```bash
uv run src/resolve.py --slug competitor-2026-09-16 --dry-run
uv run src/resolve.py --slug competitor-2026-09-16 --batch-size 10   # then rescore the indices in rescore.json
```

## Ledger (SQLite)

### Reasons in every export

Scorers must return `qualification_reason` and `intent_reason` for every person. The first explains the observed ICP match, mismatch and important missing evidence; the second explains comment-based intent or the limitations of reaction-only engagement. Empty or missing explanations are rejected for both in-session and automatic scoring. `qualification_threshold` records the selected `--min-fit`; `qualification_status` is `qualified` at or above it, otherwise `rejected`; changing the threshold recalculates the status without discarding the evidence explanation.

The ledger persists both explanations and includes them in score provenance. Existing SQLite databases gain the two explanation columns automatically without losing lookup or campaign history. Scores without explanations require rescoring. Run `rank.py --dump`, review the current evidence, and include the two reasons and the new `score_context` in every submitted result; do not manufacture explanations for old scores. Regenerate the gate and downstream CSVs after rescoring.

`eligibility_reason` explains whether ledger checks passed or why suppression, campaign history or stale scoring blocks outreach stages. ICP fit, ledger eligibility and activity readiness are separate decisions; review their reasons together. [The sample CSV](../examples/ranked_engagers.sample.csv) illustrates all three with [fictional buyer criteria and cases](../examples/README.md).

`src/ledger.py` keeps a local SQLite file, `data/ledger.sqlite` (override with `LEDGER_DB`; git-ignored). CSVs remain the per-run output; the ledger is the memory across runs, competitors and campaigns. It uses Python's built-in `sqlite3`, so there is nothing to install or host.

| Record | Purpose |
|---|---|
| `contacts`, `aliases` | One person across runs. Aliases link the opaque id (`id:ACoAA...`) and the public URL (`url:...`); two records that turn out to be one person are merged. |
| `engagements` | Each comment and post touch, tied to its post. Re-ingesting a run never inserts the same engagement twice. |
| `profile_snapshots`, `lookups` | Headline, location and employer over time; every profile lookup (resolved or not found, with date), shared by all runs. |
| `icp_versions`, `scores` | A score belongs to a contact **and** an ICP version, and stores the evidence hash and rubric hash it was made from. The same person can qualify for one client and fail another. |
| `campaign_contacts`, `suppressions` | Imports per campaign, outreach status, and do-not-contact entries by URL, email or domain. |

**Rules**

- **Fresh or stale.** A score is fresh only while the person's evidence (headline, location, employer, comments), the ICP version and the scoring rubric (`rank.SYSTEM`) all match. Location changes, so the score is stale, so the person waits for rescoring, and the new qualification decides export eligibility.
- **Rescore only when necessary.** `rank.py --dump` lists only people without a fresh score; `rank.py` fills the rest from the ledger, so `scores.json` may contain just the new rows. Automatic scorers skip them too.
- **Pay once.** Profile lookups made in any run are reused by every later run, and still go back through scoring as new evidence.
- **Gate before anything costs or sends.** After final scoring, `run.py` stamps `ledger_status` on every row: `eligible`, `stale_score`, `suppressed:<reason>` or `in_campaign:<campaign>`. Activity, drafts, GetLeads email discovery, Smartlead and HeyReach only take `eligible` rows. The push stages also check email and domain suppressions.
- **No repeat outreach.** Every all-success batch of a `--execute` push is recorded as `imported`; that person is then out of every later push, in any campaign. Opaque `/in/ACoAA...` URLs share the same identity history as public URLs. A HeyReach batch with only failures is `failed` and can retry. Mixed success/failure counts, or Smartlead skipped counts without confirmed individual identities, become `needs_reconciliation` and block automatic retries until checked in the provider. Unconfirmed batches remain `unconfirmed` and retain their existing retry policy: reconcile them in the provider first.

**Scoring provenance and upgrades.** Copy `score_context` from each row of `rank.py --dump` into its judgment in `scores.json`. This fingerprint binds the result to that person's identity, headline, location, employer, comments, optional GetLeads evidence, ICP and rubric. Missing or outdated contexts are rejected before any supplied score is stored. After a partial lookup failure, dump and judge changed rows before retrying. Updating a file timestamp never makes a score fresh. Scoring-rubric changes require new judgments; purchased lookup results remain reusable. SQLite upgrades automatically add enrichment caches, submission history and a snapshot evidence column without removing prior scores or campaign records.

**Delayed imports.** The gate now attaches `ledger_icp_hash`, `ledger_evidence_hash`, `ledger_rubric_hash` and `ledger_score_hash` to CSV rows. Both import commands validate these against the current ledger before previewing and before each request, including evidence changes, score corrections and the current rubric. When `recipient_icp.json` is next to the CSV, its ICP must match too. CSVs from earlier releases lack this proof and are excluded; rerun scoring and the gate, then regenerate any email/activity CSV. A raw harvest with missing profile fields inherits the latest nonblank profile evidence rather than deleting a known employer or location.

**Resolve mixed import outcomes.** Inspect the provider's per-person results first. Mark confirmed successes as imported and confirmed failures as failed; failed rows are available after regenerating the gate and preview. For example:

```bash
uv run src/ledger.py mark --campaign heyreach:list:123 --status imported --url https://www.linkedin.com/in/successful-lead
uv run src/ledger.py mark --campaign heyreach:list:123 --status failed --url https://www.linkedin.com/in/failed-lead
```

Identity merges preserve the strongest campaign status and all receipt references. A routine failed or unconfirmed update cannot undo a confirmed import, contact, reply or exclusion.

**Commands**

```bash
uv run src/ledger.py stats                                          # people, repeats, lookups, stale scores, campaigns
uv run src/ledger.py suppress --csv customers.csv --reason customer # linkedin_url and/or email columns
uv run src/ledger.py suppress --domain mycompany.com --reason teammate
uv run src/ledger.py mark --campaign smartlead:123 --status replied --url https://www.linkedin.com/in/jane
uv run src/ledger.py ingest --slug <slug>    # run.py does this after every harvest; safe to repeat
uv run src/ledger.py gate --slug <slug>      # run.py does this after final scoring
```

The ledger only knows what this tool recorded: replies, unsubscribes and sends inside Smartlead or HeyReach are not synced back (record them with `mark`), and Smartlead's own duplicate, block-list and unsubscribe checks stay on. Supabase remains an optional snapshot export; for a shared multi-client service the same model could live there.

## Optional GetLeads ICP evidence

Use your own GetLeads account to fill missing buyer data while keeping Apify for engagement collection and opaque-ID resolution. Add `GETLEADS_API_KEY` to `.env`. No additional package, MCP server or GetLeads scraping subscription is required by the code; available enrichment depends on your account. See [GetLeads API documentation](https://www.getleads.io/docs/).

After a first scoring pass, preview the selective lookups:

```bash
uv run src/getleads.py --slug RUN_NAME --dry-run --max-lookups 50
```

Then resume the saved harvest with enrichment enabled (automatic scorer shown; add `--scores output/RUN_NAME/scores.json` for in-session judgments):

```bash
uv run run.py --slug RUN_NAME --skip-harvest --icp icp.yaml --getleads --getleads-max-lookups 50
```

- **Selection:** first-pass fit at least `--min-fit - 1` (default 3), a public LinkedIn URL, no recorded suppression/campaign block, and missing role/employer evidence or a missing location, industry or size criterion required by the ICP. Clear rejects and hidden IDs are skipped. The existing Apify resolver runs first unless `--no-resolve` is set. Fresh partial matches and misses are reused rather than billed again for the same gaps.
- **API:** batches of up to 25 known URLs go to `POST /api/v1/enrich/from-linkedin`, with strict returned-URL checks. No name search, database-wide search or wallet-funded post scrape. Each submitted URL costs one plan credit even on a miss; unlimited accounts use fair-use rows. The free `GET /api/v1/usage/fair-use` check runs before submission and stops the stage if the planned allowance is unavailable. No automatic top-ups. Dry-run makes no API calls; its estimate is a credit count, not a dollar quote.
- **Cap and cache:** default 100 submitted identities per slug across ICP enrichment and email discovery, including failed/unknown attempts and retries. `--max-lookups` (alias `--getleads-max-lookups`) changes that shared total cap; a plain rerun never resets it. Matched and not-found responses are cached in SQLite for 30 days across runs. The standalone command accepts `--cache-days` to change the freshness window. Cached evidence remains visible with its original lookup date while awaiting a refresh; a lookup date is not a live verification date.
- **Failure recovery:** each batch is claimed in SQLite before submission, preventing another run from repeating the same pending lookup. Completed batches survive later failures. A network timeout, interrupted request, malformed result or ambiguous identity stops exports and holds the request as pending/unknown. Check GetLeads before explicitly retrying with `uv run src/getleads.py --slug RUN_NAME --retry-unknown --max-lookups 100`; a retry may cost again and still counts against the slug cap. Authentication, credit and rate-limit errors stop the stage; fix the cause and rerun. Reaching the configured cap leaves remaining rows marked `lookup_limit_reached`; missing evidence stays unknown.
- **Final scoring:** GetLeads role, employer, person location, industry, size band, domain and company description appear in `rank.py --dump` and the scorer prompt. Company HQ is never substituted for person location. Conflicting employers remain separately sourced; size bands are not exact headcounts. New evidence invalidates prior scores and delayed export proofs. In-session runs stop for new judgments: dump, judge the changed indices, copy their new contexts, and resume. Automatic scoring rescores changed evidence. Neither an enrichment miss nor an API error means the person fails the ICP.
- **Output:** `ranked_engagers.csv` adds `getleads_status`, lookup date and separate `getleads_*` evidence columns, alongside the existing qualification/rejection explanations. The summary reports lookup statuses. Apify employer/location remain preferred in the main columns; GetLeads can fill blanks, with `company_source`/`location_source` identifying it. See the [fictional enrichment example](../examples/getleads-enrichment.sample.csv).

Optional `--find-emails` uses the same GetLeads responses and cache; found addresses are UNVERIFIED with sendable=false. Phones are discarded, and emails never enter ICP scoring evidence. Activity Intel still uses Apify + Jev via TypeSafe. Runs without either `--getleads` or `--find-emails` make no GetLeads requests. Each repository user supplies their own keys.
