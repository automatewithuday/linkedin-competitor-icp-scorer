# Operations reference

The full command reference and every operational caveat. Start with the [README](../README.md) if you are new; come here when you are running the pipeline for real.

Adapted from [LeadGrowGTM/linkedin-warm-leads](https://github.com/LeadGrowGTM/linkedin-warm-leads), MIT licensed. Original license retained. Upstream baseline: `c7ae920823c6c2e048e6a218a6f6bb4031d1833d`. This version uses direct provider APIs; it does not require Deepline.

## Setup

Python 3.11+ is required. Run commands from the repository root.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python setup_icp.py
```

For the exact dependency versions tested on Python 3.12, install `requirements.lock.txt` instead of `requirements.txt`.

Fill in `.env` locally. Do not paste keys into chat or commit them. You need Apify for harvesting and one LLM for ranking (an OpenAI key, or your Claude subscription via the `claude` CLI; see below), Prospeo for email enrichment, and Smartlead only for actual imports. Firecrawl is optional for `--domain` automatic ICP derivation and for the website step in `setup_icp.py`; without a key a plain page fetch is used, which is fine for most marketing sites but misses JavaScript-rendered content. Edit `icp.example.yaml` or use the intake to define your own buyers; the example is not a recommended ICP for your business.

## Scoring LLM

ICP scoring needs one LLM. Two options, no code changes:

- **OpenAI API key**: set `OPENAI_API_KEY` in `.env`. Default model `gpt-4.1-mini`, override with `OPENAI_MODEL`. Pay per token; a 60-engager run costs well under a cent.
- **Claude subscription (no API key)**: install [Claude Code](https://claude.com/claude-code), run `claude` once to log in, and leave `OPENAI_API_KEY` empty. Scoring shells out to `claude -p` and is billed against your subscription's usage limits instead of dollars. Default model `sonnet`, override with `CLAUDE_MODEL` (`haiku`, `opus`). Each batch of 25 engagers is one CLI call. Output is not temperature-pinned, so scores can differ by one point between reruns.

If both are available, `LLM_PROVIDER=openai` or `LLM_PROVIDER=claude` picks one; otherwise OpenAI wins when its key is set.

The scoring prompt was tuned on live runs against outbound-vendor profiles: matching titles without a named employer cap at 3, and people who sell the same category as you (agencies, consultants, tool founders) cap at 2 even when their title matches the ICP. Adjust `SYSTEM` in `src/rank.py` if your ICP does target consultants or founders.

## Test only Apify and scoring

For an initial test, configure `APIFY_TOKEN` plus one scoring LLM (see "Scoring LLM") in your local `.env`, then run `python setup_icp.py` to define your buyers.

```bash
python run.py \
  --profile https://www.linkedin.com/in/YOUR-TARGET \
  --slug scraping-test-01 \
  --icp icp.yaml \
  --max-posts 3 --max-reactions 20 --max-comments 10 \
  --posted-limit month
```

This produces `output/scraping-test-01/ranked_engagers.csv` without email enrichment, campaign imports, or database writes. Compare sampled engagers and comments with the source posts, then manually review high, borderline and low ICP scores. The current qualification threshold is `icp_fit >= 4`; missing company size and geography still require verification.

After changing your ICP, rescore the saved scrape without running Apify again:

```bash
python src/icp.py --slug scraping-test-01 --icp icp.yaml
python src/rank.py --slug scraping-test-01
```

## Run the play

```bash
python run.py \
  --profile https://www.linkedin.com/in/COMPETITOR-FOUNDER \
  --slug competitor-2026-09-16 \
  --icp icp.yaml \
  --max-posts 10 \
  --find-emails --min-fit 4 --max-lookups 100
```

Outputs in `output/competitor-2026-09-16/`:

- `posts.json`, `engagers.json`: source evidence and deduplicated engagers.
- `recipient_icp.json`: criteria used for scoring.
- `ranked_engagers.csv`: everyone, including low-fit leads, source profile and post references.
- `enriched.csv`: all rows with email verification fields and a `miss_reason` for every skipped/unresolved row.
- `enriched.prospeo-cache.json`: checkpointed enrichment results, reusable for 30 days.

`--find-emails` is optional. Default harvesting caps posts at 10, reactions at 100/post and comments at 50/post, over the last month. Use `--posted-limit week` or `24h`. `--max-posts 0` removes the post cap and may increase spend. Lookups are capped at 100 and fit >=4 by default. Cached lookups do not consume that cap. Caps limit requests, not currency; check your provider plans.

The rank score combines ICP fit, number of distinct posts engaged with, and comment intent. Reactions alone remain low intent. Engagement is a prioritization signal, not proof of purchase intent. Headline-only scoring cannot establish missing company size, geography, or industry. Review the ranked output before outreach. ICP exclusions and triggers are included in the prompt; this is not a substitute for a CRM suppression list.

Prospeo matches a public LinkedIn URL first, or a full name plus known `domain`/`company_website`. It never enriches from `company_guess` alone. Only revealed, syntactically valid addresses with `VERIFIED` status become sendable. Errors fail the command after saving the CSV; rerun the enrichment step to reuse successful checkpoints. A `NO_MATCH` is a normal miss, not a pipeline error.

## Smartlead

Create or choose a campaign in Smartlead. Preview an import locally:

```bash
python src/push.py --input output/competitor-2026-09-16/enriched.csv \
  --campaign-id 123
```

Review the generated `.smartlead.json` file. Then import explicitly:

```bash
python src/push.py --input output/competitor-2026-09-16/enriched.csv \
  --campaign-id 123 --execute
```

Importing into an active campaign can start sending. This code does not create sequences or activate campaigns. It requires fit >=4, `sendable=true`, `email_status=VERIFIED`, and verification within 30 days. It excludes `suppressed=true` rows and deduplicates email addresses. Smartlead's global block list, unsubscribe, bounce and cross-campaign duplicate checks remain enabled. Optional flags: `--min-fit`, `--max-email-age-days`, `--receipt`.

Imports use batches of at most 400 and save confirmed added/skipped counts, IDs and skip details. A timeout, malformed response or inconsistent counts stop the import with an unconfirmed receipt. Reconcile that batch in Smartlead before retrying; writes are not automatically replayed and exactly-once delivery is not guaranteed. A receipt covers one invocation; copy it before retrying if you need to retain all attempts. Local suppression is supported through the CSV column; inbound reply/unsubscribe webhooks are not implemented.

You can pass `--campaign-id 123` to `run.py` with `--find-emails` to generate the preview at the end. The orchestrator never executes the campaign import.

## HeyReach (LinkedIn outreach)

```bash
python src/push_heyreach.py --input output/<slug>/ranked_engagers.csv --list-id <id>
python src/push_heyreach.py --input output/<slug>/ranked_engagers.csv --list-id <id> --execute
```

Preview is the default and writes `<input>.heyreach.json`. `--list-id` adds leads to a HeyReach lead list, which never sends; attach the list to a campaign in HeyReach. `--campaign-id <id> --linkedin-account-id <seat>` adds straight into a running campaign and can start sending. Requires `HEYREACH_API_KEY` for `--execute` only. Eligible rows need fit >= `--min-fit` (default 4) and a LinkedIn URL; member-ID URLs (`/in/ACoAA...`) are accepted since LinkedIn resolves them. Email is optional and attached only when `email_status=VERIFIED`. If `drafts.csv` exists in the same folder, `template`, `connection_note` and `dm` are attached as custom fields alongside `clean_title`, `icp_fit`, `intent`, `rank_score`, `comment_excerpt`, `source_profile` and `post_urls`, so they can be used as personalisation variables in sequences. Batches are 100 (the HeyReach cap); a batch whose response counts do not add up is written to the receipt as unconfirmed and stops the run. Custom-field shape follows the public API as documented by third parties (`customUserFields: [{name, value}]`); verify on the first live push.

## Optional Supabase storage

1. Run [`sql/001_warm_leads.sql`](../sql/001_warm_leads.sql) in your project's SQL editor.
2. Set `SUPABASE_URL` and `SUPABASE_SECRET_KEY` (or legacy `SUPABASE_SERVICE_ROLE_KEY`) in `.env`.
3. Add `--supabase` to the pipeline command, or export an existing CSV:

```bash
python src/supabase_export.py \
  --input output/competitor-2026-09-16/enriched.csv \
  --run-id competitor-2026-09-16
```

Rows are upserted into `warm_lead_snapshots` on `(run_id, lead_key)`. Retrying the same run does not create duplicate snapshots. Use a new dated run ID for each new sweep to retain scoring history. A normalized LinkedIn URL, falling back to the actor ID, identifies each lead. The entire CSV row is preserved in JSONB. Supabase never replaces the CSV.

RLS is enabled and public roles have no access. Use the secret key only server-side. Export is chunked, not transactional across batches: after a failure, rerun with the same run ID. Identity can change if an opaque actor later gains a public URL; automatic identity reconciliation is not implemented. Snapshots are observations, not a CRM lifecycle table.

## Rerun individual stages

```bash
python src/rank.py --slug competitor-2026-09-16
python src/draft.py --slug competitor-2026-09-16 --min-fit 4   # drafts.csv; one LLM call per 10 rows
python src/email_enrich.py --input output/competitor-2026-09-16/ranked_engagers.csv \
  --output output/competitor-2026-09-16/enriched.csv --min-fit 4 --max-lookups 100
```

Re-running `run.py` starts another paid harvest. Reuse individual stages when recovering. Do not run concurrent jobs against the same slug/output files. No recurring schedule is installed by this project.

The optional upstream `--enrich` company-scraping stage is retained, but its company output is not yet joined into ranking; it is not needed for Prospeo email enrichment.

## Validation

```bash
python -m unittest discover -s tests -v
```

Tests use mocked provider responses and do not spend API credits or send emails. Live setup requires your keys, ICP, target profiles and campaign ID.

API references: [Prospeo Enrich Person](https://prospeo.io/api-docs/enrich-person), [Smartlead import](https://api.smartlead.ai/api-reference/campaigns/add-leads), [Supabase API security](https://supabase.com/docs/guides/api/securing-your-api). The original generated PDF setup guide was removed because it described the previous provider stack; this README is the current guide.
