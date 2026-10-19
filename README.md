# LinkedIn warm leads — Prospeo + Smartlead + Supabase

Collect the engagers of a competitor founder's recent posts, or of one specific post, rank them against your ICP, optionally check who is actually active on LinkedIn, find verified work emails with Prospeo, and prepare a Smartlead campaign import. Every run ends with a readable summary with names, so you never have to open the CSV. CSV is always retained; Supabase is optional.

Adapted from [LeadGrowGTM/linkedin-warm-leads](https://github.com/LeadGrowGTM/linkedin-warm-leads), MIT licensed. Original license retained. Upstream baseline: `c7ae920823c6c2e048e6a218a6f6bb4031d1833d`. This version uses direct provider APIs; it does not require Deepline.

## Setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required. Run commands from the repository root.

```bash
uv sync
cp .env.example .env
uv run setup_icp.py
```

Without uv: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.lock.txt` (exported from `uv.lock`; `requirements.txt` is the unpinned list), then replace `uv run` with `python` in the commands below.

In Claude Code, `/setup` walks through install, keys, ICP and targets without spending credits, and `/first-run` does a small paid test harvest with a review loop.

Fill in `.env` locally. Do not paste keys into chat or commit them. You need Apify for harvesting, Prospeo for email enrichment, Smartlead only for actual imports, and TypeSafe only for the optional `--activity` check. Firecrawl is optional for `--domain` automatic ICP derivation.

**Scoring needs no key.** By default the engagers are judged in-session by Claude (or by you) and handed to the pipeline as a scores file; `/setup` asks which you want. Set `OPENAI_API_KEY` only if you prefer gpt-4.1-mini to do it automatically. Edit `icp.example.yaml` or use the intake to define your own buyers; the example is not a recommended ICP for your business.

## Test only Apify and scoring

For an initial test, configure only `APIFY_TOKEN` in your local `.env`, then run `uv run setup_icp.py` to define your buyers. In Claude Code, `/first-run` does all of the below.

```bash
# 1. harvest (paid): a profile's recent posts, or exactly one post
uv run src/harvest.py --profile https://www.linkedin.com/in/YOUR-TARGET --slug scraping-test-01 \
  --max-posts 3 --max-reactions 20 --max-comments 10 --posted-limit month
uv run src/harvest.py --post https://lnkd.in/p/XXXX --slug scraping-test-01 --max-reactions 100 --max-comments 50

# 2. score: print the numbered engager list, judge it (Claude in-session, or yourself), save the JSON
uv run src/icp.py --slug scraping-test-01 --icp icp.yaml
uv run src/rank.py --dump --slug scraping-test-01          # -> write output/scraping-test-01/scores.json

# 3. everything else, reusing the paid harvest
uv run run.py --slug scraping-test-01 --skip-harvest --icp icp.yaml --scores output/scraping-test-01/scores.json
```

With `OPENAI_API_KEY` set you can skip step 2 and run `uv run run.py --profile ... --slug scraping-test-01 --icp icp.yaml --max-posts 3 --max-reactions 20 --max-comments 10` in one go; gpt-4.1-mini scores the engagers.

`scores.json` is `{"results": [{"i": 0, "icp_fit": 1-5, "intent": "high"|"medium"|"low"|null, "clean_title": "...", "company_guess": "..."|null}, ...]}` with exactly one object per engager index from the dump. Both scorers are validated by the same rules.

This produces `output/scraping-test-01/ranked_engagers.csv` without email enrichment, campaign imports, or database writes, and prints a summary with names at the end. Compare sampled engagers and comments with the source posts, then manually review high, borderline and low ICP scores. The current qualification threshold is `icp_fit >= 4`; missing company size and geography still require verification.

After changing your ICP, rescore the saved scrape without running Apify again:

```bash
uv run src/icp.py --slug scraping-test-01 --icp icp.yaml
uv run src/rank.py --slug scraping-test-01 --scores output/scraping-test-01/scores.json
```

## Run the play

```bash
uv run run.py \
  --profile https://www.linkedin.com/in/COMPETITOR-FOUNDER \
  --slug competitor-2026-09-16 \
  --icp icp.yaml --scores output/competitor-2026-09-16/scores.json \
  --max-posts 10 \
  --find-emails --min-fit 4 --max-lookups 100
```

Give `--post <url>` instead of `--profile` to harvest one specific post only (full `linkedin.com/posts/...` or `/feed/update/...` URLs and `lnkd.in` short links all work; `--max-posts` is ignored). `--skip-harvest` reuses `posts.json` and `engagers.json` already in the slug folder, which is how a Claude-scored run continues after the harvest without paying Apify twice. The run always ends with `src/summary.py`: counts, fit distribution, activity and email breakdowns, and the top rows by name. Run it on any CSV yourself with `uv run src/summary.py --input output/<slug>/enriched.csv`.

Outputs in `output/competitor-2026-09-16/`:

- `posts.json`, `engagers.json`: source evidence and deduplicated engagers.
- `scores.json`: the ICP judgments when scored in-session (absent when OpenAI scored).
- `recipient_icp.json`: criteria used for scoring.
- `ranked_engagers.csv`: everyone, including low-fit leads, source profile and post references (plus `location` when the harvest carried it).
- `resolved.csv`: the ranked rows after opaque-ID resolution, with a `resolve_status` column; later stages read this file (see "Opaque profile IDs").
- `enriched.csv`: all rows with email verification fields and a `miss_reason` for every skipped/unresolved row.
- `enriched.prospeo-cache.json`: checkpointed enrichment results, reusable for 30 days.
- `activity.csv` and `activity/`: with `--activity`, the ranked rows plus LinkedIn activity verdicts, and the full per-profile output and raw cache.

`--find-emails` is optional. Default harvesting caps posts at 10, reactions at 100/post and comments at 50/post, over the last month. Use `--posted-limit week` or `24h`. `--max-posts 0` removes the post cap and may increase spend. Lookups are capped at 100 and fit >=4 by default. Cached lookups do not consume that cap. Caps limit requests, not currency; check your provider plans.

The rank score combines ICP fit, number of distinct posts engaged with, and comment intent. Reactions alone remain low intent. Engagement is a prioritization signal, not proof of purchase intent. Headline-only scoring cannot establish missing company size, geography, or industry. Review the ranked output before outreach. ICP exclusions and triggers are included in the prompt; this is not a substitute for a CRM suppression list.

Prospeo matches a public LinkedIn URL first, or a full name plus known `domain`/`company_website`. It never enriches from `company_guess` alone. Only revealed, syntactically valid addresses with `VERIFIED` status become sendable. Errors fail the command after saving the CSV; rerun the enrichment step to reuse successful checkpoints. A `NO_MATCH` is a normal miss, not a pipeline error.

## Opaque profile IDs

Reaction items, and every engager from a `--profile` harvest, often carry only LinkedIn's opaque profile ID (`/in/ACoAA...`) instead of a public URL. Prospeo, the activity check and Smartlead cannot use those, so in a typical run most qualified rows were silently skipped. Two fixes:

On the Apify free plan HarvestAPI's profile scraper accepts at most 10 ids per run and returns an error item for larger runs (the rows get `resolve_status=error`). `resolve.py --batch-size 10` (or `run.py --resolve-batch 10`) splits the ids into runs of 10. That paces around a provider limit, so it is off unless you pass it.

- `--post` harvests ask the post actors for `profileScraperMode: "main"`, so every engager arrives with a vanity URL, `location` and current `company` (+$0.002 per engager on top of $0.002 per reaction/comment; the spend cap includes it). Location and company also reach the scorer (`rank.py --dump`). `--profile` harvests cannot do this: that actor has no profile mode.
- After ranking, `run.py` runs `src/resolve.py` by default: one `harvestapi/linkedin-profile-scraper` run over the qualified (`icp_fit >= --min-fit`) rows that still have an opaque ID, at $0.004 per profile (hard cap = estimate x 1.25 + $0.05). It writes `resolved.csv` with the vanity `linkedin_url`, `location` when empty, and `resolve_status`: `resolved`, `not_found` (sent, nothing returned), `skipped` (below min fit, already public, or no ID), `error` (run failed or the provider returned an error item; the other rows carry on). Pass `--no-resolve` to skip it.

On a free Apify plan HarvestAPI limits this actor to 10 items per run: a larger run scrapes nothing, returns one error item, and every sent row becomes `error`. Upgrade the plan or lower the count (for example `--min-fit 5`); the stage does not split runs to get around the limit.

Standalone, dry-run first (prints the ID count, the estimate and the cap; spends nothing):

```bash
uv run src/resolve.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/resolved.csv --min-fit 4 --dry-run
```

## LinkedIn activity check (optional)

`--activity` answers one question per qualified lead: does this person actually use LinkedIn right now, and how? It fetches each profile's recent public posts, comments and reactions (HarvestAPI actors on Apify), computes dates and counts in plain code, and asks [TypeSafe](https://typesafe.ai) for typed judgments. No LinkedIn login. Cost is about $0.04 per profile checked (Apify) plus a negligible TypeSafe charge. Set `TYPESAFE_API_KEY` in `.env`.

```bash
uv run run.py --profile https://www.linkedin.com/in/COMPETITOR-FOUNDER --slug competitor-2026-09-16 \
  --icp icp.yaml --activity --find-emails
```

Only rows with `icp_fit >= --min-fit` and a public `/in/` URL are checked. Rows whose opaque `/in/ACoAA...` id was not resolved (see "Opaque profile IDs") get `activity_miss_reason=no_public_url`. Everyone in a harvest engaged within the harvest window by construction, so read the result as a posting-habit and recency signal for prioritisation and channel choice, not as proof of inactivity.

The stage appends these columns and changes nothing else: `linkedin_outreach_ready`, `outreach_ready_probability` (sort by this), `outreach_reason`, `activity_status` (`active` / `stale` / `inactive` / `no_activity_observed`), `activity_level`, `activity_confidence`, `days_since_last_activity`, `last_activity_type`, `most_recent_activity_url`, `data_quality_warning`, `activity_miss_reason` (`below_min_fit`, `no_public_url`, `not_in_result`, `no_verdict`). Email enrichment, Supabase and Smartlead read the augmented CSV unchanged; nothing is gated on the verdict. Rows with `data_quality_warning=true` deserve a look before outreach.

Standalone, always dry-run first; it prints the eligible count, the Apify estimate and the hard spend cap without spending anything:

```bash
uv run src/activity.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/activity.csv --dry-run
uv run src/activity.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/activity.csv --limit 20
```

The full ~40-column result, a JSONL copy and the raw actor output land in `output/<slug>/activity/`. To re-judge after changing `activity.toml` without paying Apify again, add `--from-raw output/<slug>/activity/raw/<run>`. If you rerun `src/rank.py`, `ranked_engagers.csv` is regenerated without the activity columns; re-join them the same way.

`activity.toml` holds the fetch caps, the outreach-ready threshold and the exact TypeSafe question wording. It is user-owned. `src/activity_intel.py` is vendored unchanged from [LinkedIn-Profile-Activity-Intel](https://github.com/automatewithuday/LinkedIn-Profile-Activity-Intel) (MIT, commit `3e3e178`); that README explains the evidence rules, including why reactions only give a lower bound on the date.

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

You can pass `--campaign-id 123` to `run.py` with `--find-emails` to generate the preview at the end. The orchestrator never executes the campaign import.

## Optional Supabase storage

1. Run [`sql/001_warm_leads.sql`](sql/001_warm_leads.sql) in your project's SQL editor.
2. Set `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (or legacy `SUPABASE_SERVICE_ROLE_KEY`) in `.env`.
3. Add `--supabase` to the pipeline command, or export an existing CSV:

```bash
uv run src/supabase_export.py \
  --input output/competitor-2026-09-16/enriched.csv \
  --run-id competitor-2026-09-16
```

Rows are upserted into `warm_lead_snapshots` on `(run_id, lead_key)`. Retrying the same run does not create duplicate snapshots. Use a new dated run ID for each new sweep to retain scoring history. A normalized LinkedIn URL, falling back to the actor ID, identifies each lead. The entire CSV row is preserved in JSONB. Supabase never replaces the CSV.

RLS is enabled and public roles have no access. Use the secret key only server-side. Export is chunked, not transactional across batches: after a failure, rerun with the same run ID. Identity can change if an opaque actor later gains a public URL; automatic identity reconciliation is not implemented. Snapshots are observations, not a CRM lifecycle table.

## Rerun individual stages

```bash
uv run src/rank.py --slug competitor-2026-09-16 --scores output/competitor-2026-09-16/scores.json   # or no --scores with OPENAI_API_KEY
uv run src/resolve.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/resolved.csv --min-fit 4   # $0.004 per qualified opaque profile
uv run src/email_enrich.py --input output/competitor-2026-09-16/resolved.csv \
  --output output/competitor-2026-09-16/enriched.csv --min-fit 4 --max-lookups 100
uv run src/activity.py --input output/competitor-2026-09-16/resolved.csv \
  --output output/competitor-2026-09-16/activity.csv --min-fit 4
```

Re-running `run.py` starts another paid harvest. Reuse individual stages when recovering. Do not run concurrent jobs against the same slug/output files. No recurring schedule is installed by this project.

The optional upstream `--enrich` company-scraping stage is retained, but its company output is not yet joined into ranking; it is not needed for Prospeo email enrichment.

## Validation

```bash
uv run python -m unittest discover -s tests -v
```

Tests use mocked provider responses and do not spend API credits or send emails. Live setup requires your keys, ICP, target profiles and campaign ID.

API references: [Prospeo Enrich Person](https://prospeo.io/api-docs/enrich-person), [Smartlead import](https://api.smartlead.ai/api-reference/campaigns/add-leads), [Supabase API security](https://supabase.com/docs/guides/api/securing-your-api). The original generated PDF setup guide was removed because it described the previous provider stack; this README is the current guide.
