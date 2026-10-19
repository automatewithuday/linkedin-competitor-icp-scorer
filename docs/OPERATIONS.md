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

Fill in `.env` locally. Do not paste keys into chat or commit them. You need Apify for harvesting and one LLM for ranking (an OpenAI key, or your Claude subscription via the `claude` CLI; see below), Prospeo for email enrichment, and Smartlead only for actual imports. Firecrawl is optional for `--domain` automatic ICP derivation and for the website step in `setup_icp.py`; without a key a plain page fetch is used, which is fine for most marketing sites but misses JavaScript-rendered content. Edit `icp.example.yaml` or use the intake to define your own buyers; the example is not a recommended ICP for your business.

## Scoring LLM

ICP scoring needs a judge. Three options, no code changes:

- **In-session (Claude Code, no key):** after the harvest run `uv run src/rank.py --dump --slug <slug>`, judge every numbered engager with the rubric in `src/rank.py` (`SYSTEM`), write `output/<slug>/scores.json` as `{"results": [{"i": 0, "icp_fit": 1-5, "intent": "high"|"medium"|"low"|null, "clean_title": "...", "company_guess": "..."|null}, ...]}` with one object per engager, and continue with `uv run run.py --slug <slug> --skip-harvest --icp icp.yaml --scores output/<slug>/scores.json ...`. The same validation applies to every scorer.

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

This produces `output/scraping-test-01/ranked_engagers.csv` and `resolved.csv` (the resolve step runs by default at $0.004 per qualified opaque id; `--no-resolve` skips it), without email enrichment, campaign imports, or database writes. Compare sampled engagers and comments with the source posts, then manually review high, borderline and low ICP scores. The current qualification threshold is `icp_fit >= 4`; missing company size and geography still require verification.

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
- `resolved.csv`: the ranked rows with hidden profile ids resolved (`resolve_status`, `location`).
- `activity.csv` and `activity/`: with `--activity`, LinkedIn activity verdicts plus the full per-profile output and raw cache.
- `enriched.csv`: all rows with email verification fields and a `miss_reason` for every skipped/unresolved row.
- `enriched.prospeo-cache.json`: checkpointed enrichment results, reusable for 30 days.

`--find-emails` is optional. Default harvesting caps posts at 10, reactions at 100/post and comments at 50/post, over the last month. Use `--posted-limit week` or `24h`. `--max-posts 0` removes the post cap and may increase spend. Lookups are capped at 100 and fit >=4 by default. Cached lookups do not consume that cap. Caps limit requests, not currency; check your provider plans.

The rank score combines ICP fit, number of distinct posts engaged with, and comment intent. Reactions alone remain low intent. Engagement is a prioritization signal, not proof of purchase intent. Headline-only scoring cannot establish missing company size, geography, or industry. Review the ranked output before outreach. ICP exclusions and triggers are included in the prompt; this is not a substitute for a CRM suppression list.

Prospeo matches a public LinkedIn URL first, or a full name plus known `domain`/`company_website`. It never enriches from `company_guess` alone. Only revealed, syntactically valid addresses with `VERIFIED` status become sendable. Errors fail the command after saving the CSV; rerun the enrichment step to reuse successful checkpoints. A `NO_MATCH` is a normal miss, not a pipeline error.

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

The optional upstream `--enrich` company-scraping stage is retained, but its company output is not yet joined into ranking; it is not needed for Prospeo email enrichment.

## Validation

```bash
uv run python -m unittest discover -s tests -v
```

Tests use mocked provider responses and do not spend API credits or send emails. Live setup requires your keys, ICP, target profiles and campaign ID.

API references: [Prospeo Enrich Person](https://prospeo.io/api-docs/enrich-person), [Smartlead import](https://api.smartlead.ai/api-reference/campaigns/add-leads), [Supabase API security](https://supabase.com/docs/guides/api/securing-your-api). The original generated PDF setup guide was removed because it described the previous provider stack; this README is the current guide.

## Single-post harvest

Give `--post <url>` instead of `--profile` to harvest one specific post only. Full `linkedin.com/posts/...` and `/feed/update/...` URLs and `lnkd.in` short links all work; `--max-posts` is ignored. The post actors run in full-profile mode, so nearly every engager carries a vanity URL, location and company (about $0.004 per engager instead of $0.002; an occasional reactor still comes back opaque, and resolve picks it up if it qualifies). Comments are not date-filtered in post mode, so an older post keeps all its comments. `--skip-harvest` reuses `posts.json` and `engagers.json` already in the slug folder, which is how an in-session-scored run continues after the harvest without paying Apify twice.

```bash
uv run src/harvest.py --post https://lnkd.in/p/XXXX --slug post-2026-10-19 --max-reactions 100 --max-comments 50
```

Every run ends with `src/summary.py`: counts, fit distribution, resolve, activity and email breakdowns, and the top rows by name. Run it on any CSV with `uv run src/summary.py --input output/<slug>/enriched.csv`.

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
uv run src/activity.py --input output/competitor-2026-09-16/resolved.csv \
  --output output/competitor-2026-09-16/activity.csv --dry-run
uv run src/activity.py --input output/competitor-2026-09-16/resolved.csv \
  --output output/competitor-2026-09-16/activity.csv --limit 20
```

The full ~40-column result, a JSONL copy and the raw actor output land in `output/<slug>/activity/`. To re-judge after changing `activity.toml` without paying Apify again, add `--from-raw output/<slug>/activity/raw/<run>`. Use `resolved.csv` as the input, not `ranked_engagers.csv`: the latter still holds the opaque ids, so resolved leads would be skipped. After a rescore, rerun resolve (free for rows already resolved) and re-join activity with `--from-raw`.

`activity.toml` holds the fetch caps, the outreach-ready threshold and the exact TypeSafe question wording. It is user-owned. `src/activity_intel.py` is vendored unchanged from [LinkedIn-Profile-Activity-Intel](https://github.com/automatewithuday/LinkedIn-Profile-Activity-Intel) (MIT, commit `7a3aabd`); that README explains the evidence rules, including why reactions only give a lower bound on the date.

## Opaque profile IDs

Reaction items, and every engager from a `--profile` harvest, often carry only LinkedIn's opaque profile ID (`/in/ACoAA...`) instead of a public URL. Prospeo, the activity check and Smartlead cannot use those, so in a typical run most qualified rows were silently skipped. Two fixes:

On the Apify free plan HarvestAPI's profile scraper accepts at most 10 ids per run and returns an error item for larger runs (the rows get `resolve_status=error`). `resolve.py --batch-size 10` (or `run.py --resolve-batch 10`) splits the ids into runs of 10. That paces around a provider limit, so it is off unless you pass it.

- `--post` harvests ask the post actors for `profileScraperMode: "main"`, so nearly every engager arrives with a vanity URL, `location` and current `company` (+$0.002 per engager on top of $0.002 per reaction/comment; the spend cap includes it). Location and company also reach the scorer (`rank.py --dump`). `--profile` harvests cannot do this: that actor has no profile mode.
- Two-pass scoring (default): after the first scoring pass, `run.py` resolves opaque engagers at `icp_fit >= --min-fit - 1` (3 by default) with `src/resolve.py --slug <slug>`, reusing anything already in `resolved.csv`, and writes their public URL, `location` and `company` into `engagers.json`. With an automatic scorer it then rescores everything on its own. With `--scores` it stops, lists the indices that gained profile data (also in `output/<slug>/rescore.json`), and asks for those rows to be rescored before continuing; the next run sends nothing already resolved. This is what lets geo and employer rules apply to profile harvests: on a real 151-engager run it cost $0.12 and moved 10 of 53 candidates (4 up, 6 down on geography).
- After ranking, `run.py` also runs `src/resolve.py` by default as a safety net: one `harvestapi/linkedin-profile-scraper` run over the qualified (`icp_fit >= --min-fit`) rows that still have an opaque ID, at $0.004 per profile (hard cap = estimate x 1.25 + $0.05). It writes `resolved.csv` with the vanity `linkedin_url`, `location` when empty, and `resolve_status`: `resolved`, `not_found` (sent, nothing returned), `skipped` (below min fit, already public, or no ID), `error` (run failed or the provider returned an error item; the other rows carry on). Pass `--no-resolve` to skip it.

On a free Apify plan HarvestAPI limits this actor to 10 items per run: a larger run scrapes nothing, returns one error item, and every sent row becomes `error`. Pass `--resolve-batch 10` (or `resolve.py --batch-size 10`), upgrade the plan, or lower the count (for example `--min-fit 5`). Rerunning reuses rows already marked `resolved` in the existing `resolved.csv`, so a rescore never pays for the same profile twice.

Standalone, dry-run first (prints the ID count, the estimate and the cap; spends nothing):

```bash
uv run src/resolve.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/resolved.csv --min-fit 4 --dry-run
```
