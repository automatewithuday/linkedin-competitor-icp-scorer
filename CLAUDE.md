# Operating this play

Read README.md for the overview and docs/OPERATIONS.md for current commands and caveats. Providers: Apify, Prospeo, Smartlead, HeyReach; optional OpenAI or the `claude` CLI (automatic scoring), Supabase and TypeSafe (for `--activity`).

Validation: `uv run python -m unittest discover -s tests -v`.

Scoring is your job: always pass `--scores`. Inside Claude Code run.py and rank.py refuse to score on their own unless the user set LLM_PROVIDER in .env. Flow: after a harvest run `uv run src/rank.py --dump --slug <slug>`, judge every engager with the rubric in `src/rank.py` SYSTEM (stingy: most engagers are 1-2; the user's own profile and the source author are 1), write `output/<slug>/scores.json` as `{"results": [{i, score_context, icp_fit, intent, evidence_comment, clean_title, company_guess}]}` (`evidence_comment` is the number of the comment `cN` your intent rests on, or null), then continue with `uv run run.py --slug <slug> --skip-harvest --icp icp.yaml --scores output/<slug>/scores.json ...`. Never require an OpenAI key.

A single post link (linkedin.com/posts/..., /feed/update/..., or lnkd.in/...) means `--post`: harvest only that post, never the author's last 10 posts. A personal profile link means `--profile`. Company pages are not supported; say so instead of guessing.

Every run ends with `src/summary.py` output (counts plus the top rows by name). Paste that summary into your reply; the user does not open CSVs.

Use the user's ICP and target profiles. Never invent an ICP from unrelated prior projects. Keep API credentials in .env and never print them. Proceed with authorized work without repeated permission prompts.

Run `run.py` for a new harvest, or individual stage commands for retries. Review CSV scoring and Smartlead preview before an actual import. `push.py --execute` imports into a campaign and may initiate sending if that campaign is active; only execute when the user has authorized that campaign import. Keep all provider suppression checks enabled.

For recurring sweeps choose a dated slug for each target. Reuse a run ID only for retries. Do not mark contacts as sent merely because they were harvested or exported. Supabase stores observations; Smartlead receipts track import outcomes. Do not infer success from a zero HTTP error count.

`run.py` looks up opaque `/in/ACoAA...` candidates (first-pass fit >= min_fit - 1) before final scoring (`src/resolve.py --slug`, $0.004 per profile; `--no-resolve` skips it). That is the only place profiles are fetched. If any lookup fails it exits 3 and the run stops before activity, emails or exports; successful results are kept, so rerunning retries only the failures. Completed misses are cached for 30 days (`--refresh-misses` retries them).

`--activity` (or `src/activity.py` standalone) spends Apify at about $0.04 per profile. Run `src/activity.py --dry-run` first and state the estimate and hard cap before a paid run. `activity.toml` is user-owned: propose changes, never edit it. The `/setup` and `/first-run` commands cover onboarding.

The Apify free plan caps the profile scraper at 10 ids per run; `--resolve-batch 10` paces lookups around it only when the user asks. Rerunning `run.py --skip-harvest` never pays for a profile already resolved or missed.

Two-pass scoring: after your first pass, run.py resolves opaque candidates at fit >= min_fit - 1 (default 3) and writes location and company into engagers.json. If any gained profile data it stops and lists their indices (also in `output/<slug>/rescore.json`): run `rank.py --dump` again, rescore just those rows with the new location/employer evidence (geo disqualifiers apply), save scores.json and rerun the same command. Nothing is paid twice. Ranking always puts rows at or above `--min-fit` first.

The SQLite ledger (`data/ledger.sqlite`, `src/ledger.py`) remembers people across runs. Never push or enrich a row whose `ledger_status` is not `eligible`. When the user names customers, teammates or people to avoid, add them with `ledger.py suppress`; when they report a reply or a manual contact, record it with `ledger.py mark`. Use `ledger.py stats` to report repeats across competitors.

Every supplied judgment must copy `score_context` exactly from `rank.py --dump`. It binds that judgment to the person's identity, evidence, ICP and rubric. If a partial lookup fails, run the dump and judge changed rows before retrying; never copy an old judgment onto a new context without reviewing the evidence. File timestamps do not certify scoring. Legacy unbound scores require a new dump and review; old CSVs require scoring and the ledger gate to be rerun.

Smartlead and HeyReach recheck current evidence, scoring provenance, suppressions and campaign history before each import batch. Mixed aggregate import results are `needs_reconciliation`, not imported: reconcile individual leads with the provider, then use `ledger.py mark --status imported` for successes or `--status failed` for confirmed failures. Failed leads can retry. Do not turn an uncertain outcome into a confirmed failure without checking the provider.
