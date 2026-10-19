"""Orchestrator: harvest -> (enrich) -> icp -> rank -> profile lookups -> rescore -> (draft) -> (activity) -> (emails) -> (supabase) -> (smartlead preview) -> summary

Usage:
  uv run run.py --profile <linkedin-url> --slug <slug> --icp icp.yaml --scores output/<slug>/scores.json [options]
  uv run run.py --post <post-url>        --slug <slug> --icp icp.yaml --scores ... [options]   # one post only
  uv run run.py --slug <slug> --skip-harvest --icp icp.yaml --scores ...                        # reuse a paid harvest

Scoring: pass --scores (judgments made in-session; see src/rank.py --dump), or let the configured LLM score:
OPENAI_API_KEY, else the `claude` CLI when installed (LLM_PROVIDER forces one).
"""
import argparse
import json
import os
import re
from dotenv import load_dotenv
from src.common import in_session_guard, linkedin_url, llm_provider
import subprocess
import sys
from pathlib import Path


def run_step(label: str, cmd: list[str]) -> None:
    print(f"\n{'='*60}\nSTEP: {label}\n{'='*60}", flush=True)  # flush: keep headers ahead of child output when piped
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        sys.exit(f"Step failed: {label} (exit {result.returncode})")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Full pipeline: harvest LinkedIn engagers, derive ICP, rank as warm buyers"
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--profile", help="LinkedIn profile URL: harvest its recent posts")
    src.add_argument("--post", help="LinkedIn post URL (lnkd.in accepted): harvest only this one post")
    ap.add_argument("--skip-harvest", action="store_true",
                    help="Reuse output/<slug>/posts.json + engagers.json from an earlier paid harvest")
    ap.add_argument("--scores", metavar="PATH",
                    help="JSON judgments for rank.py (made in-session, no LLM call). Without it the configured LLM scores: OPENAI_API_KEY, or the claude CLI when installed")
    ap.add_argument("--slug", required=True, help="Short identifier for this run (output folder name)")
    ap.add_argument("--output-dir", default="./output", help="Base output directory")

    icp_mode = ap.add_mutually_exclusive_group(required=True)
    icp_mode.add_argument("--domain", help="Auto ICP: scrape this domain and infer ICP via LLM")
    icp_mode.add_argument("--icp", metavar="PATH", help="Manual ICP: path to your icp.yaml")

    ap.add_argument("--enrich", action="store_true",
                    help="Optional: enrich top engager companies (costs extra Apify credits)")
    ap.add_argument("--max-posts", type=int, default=10)
    ap.add_argument("--max-reactions", type=int, default=100)
    ap.add_argument("--max-comments", type=int, default=50)
    ap.add_argument("--posted-limit", choices=["24h", "week", "month"], default="month")
    ap.add_argument("--find-emails", action="store_true", help="Enrich qualified leads with Prospeo")
    ap.add_argument("--min-fit", type=int, choices=range(1, 6), default=4)
    ap.add_argument("--max-lookups", type=int, default=100)
    ap.add_argument("--draft", action="store_true",
                    help="Draft connection note + DM per qualified row using playbooks/copy-templates.md")
    ap.add_argument("--supabase", action="store_true", help="Also store lead snapshots in Supabase")
    ap.add_argument("--campaign-id", type=int, help="Generate a Smartlead import preview")
    ap.add_argument("--heyreach-list-id", type=int, help="Generate a HeyReach list push preview (LinkedIn outreach)")
    ap.add_argument("--resolve-batch", type=int, default=0, metavar="N",
                    help="ids per profile-scraper run for resolve (0 = one run; Apify free plan allows 10)")
    ap.add_argument("--no-resolve", action="store_true",
                    help="Skip looking up opaque /in/ACoAA... candidates (fit >= min-fit - 1) before final scoring "
                         "(default: on; Apify, $0.004 per profile, results cached)")
    ap.add_argument("--activity", action="store_true",
                    help="Check LinkedIn activity of qualified leads (Apify, ~$0.04/profile; needs TYPESAFE_API_KEY)")
    args = ap.parse_args()
    load_dotenv()
    if args.skip_harvest:
        if args.profile or args.post:
            ap.error("--skip-harvest reuses the saved harvest; drop --profile/--post")
    elif args.profile:
        if not linkedin_url(args.profile):
            ap.error("--profile must be a public LinkedIn /in/ URL")
    elif args.post:
        if not re.search(r'(linkedin\.com/(posts/|feed/update/)|lnkd\.in/)', args.post):
            ap.error("--post must be a LinkedIn post URL or an lnkd.in link")
    else:
        ap.error("one of --profile, --post or --skip-harvest is required")
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.slug):
        ap.error("Slug must contain only letters, digits, underscores and hyphens")
    if min(args.max_posts, args.max_reactions, args.max_comments) < 0 or args.max_lookups < 1:
        ap.error("Invalid scrape or lookup limits")
    if args.campaign_id is not None and (args.campaign_id < 1 or not args.find_emails):
        ap.error("--campaign-id must be positive and requires --find-emails")
    required = []
    if not args.scores and in_session_guard():
        sys.exit(in_session_guard())
    if not args.scores and not llm_provider():
        sys.exit('No LLM for scoring: pass --scores (judgments made in-session), set OPENAI_API_KEY in .env, '
                 'or install the `claude` CLI and log in to your Claude subscription (LLM_PROVIDER=claude).')
    if not (args.skip_harvest and args.no_resolve) and not (os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY')):
        required.append('APIFY_TOKEN')
    if args.find_emails:
        required.append('PROSPEO_API_KEY')
    if args.supabase:
        required.append('SUPABASE_URL')
        if not (os.getenv('SUPABASE_SECRET_KEY') or os.getenv('SUPABASE_SERVICE_ROLE_KEY')):
            required.append('SUPABASE_SECRET_KEY')
    if args.activity:
        required.append('TYPESAFE_API_KEY')
        if not Path('activity.toml').is_file():
            ap.error('activity.toml does not exist')
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        ap.error('Missing configuration: ' + ', '.join(missing))
    if args.icp and not Path(args.icp).is_file():
        ap.error('ICP file does not exist')
    if args.scores and not Path(args.scores).is_file():
        ap.error('Scores file does not exist (run `src/rank.py --dump --slug <slug>` and score it first)')
    if args.skip_harvest:
        for name in ("posts.json", "engagers.json"):
            if not (Path(args.output_dir) / args.slug / name).is_file():
                ap.error(f'--skip-harvest needs output/{args.slug}/{name}')

    py = sys.executable
    out = args.output_dir
    slug = args.slug

    # Step 1: Harvest (one post, or a profile's recent posts)
    if not args.skip_harvest:
        harvest_cmd = [
            py, "src/harvest.py",
            "--post" if args.post else "--profile", args.post or args.profile,
            "--slug", slug,
            "--max-posts", str(args.max_posts),
            "--max-reactions", str(args.max_reactions),
            "--max-comments", str(args.max_comments),
            "--posted-limit", args.posted_limit,
            "--output-dir", out,
        ]
        run_step("Harvest engagers" + (" (single post)" if args.post else " (profile posts)"), harvest_cmd)

    # Step 2 (optional): Enrich companies
    if args.enrich:
        enrich_cmd = [
            py, "src/enrich.py",
            "--slug", slug,
            "--from-engagers",
            "--output-dir", out,
        ]
        run_step("Enrich engager companies", enrich_cmd)

    # Step 3: ICP
    icp_cmd = [
        py, "src/icp.py",
        "--slug", slug,
        "--output-dir", out,
    ]
    if args.domain:
        icp_cmd += ["--domain", args.domain]
    else:
        icp_cmd += ["--icp", args.icp]
    run_step("Derive/load ICP", icp_cmd)

    # Step 4: Rank
    rank_cmd = [
        py, "src/rank.py",
        "--slug", slug,
        "--output-dir", out,
        "--min-fit", str(args.min_fit),
    ]
    if args.scores:
        rank_cmd += ["--scores", args.scores]
    run_step("Score + rank engagers" + (" (in-session judgments)" if args.scores else f" ({llm_provider()})"), rank_cmd)

    # Step 4b: two-pass scoring. Profile harvests carry no location/company for reactors; resolve the borderline-and-up
    # candidates (fit >= min_fit - 1) so the final scoring can apply geo and employer rules.
    batch = ["--batch-size", str(args.resolve_batch)] if args.resolve_batch else []
    if not args.no_resolve:
        rescore = Path(out) / slug / "rescore.json"
        before = rescore.stat().st_mtime if rescore.exists() else None
        # The only profile fetch. A failed lookup exits non-zero and stops the run here, so no row reaches
        # activity/email/exports scored without its profile evidence; paid results are kept for the rerun.
        run_step("Profile lookups before final scoring", [py, "src/resolve.py", "--slug", slug, "--output-dir", out,
                 "--min-fit", str(max(1, args.min_fit - 1))] + batch + (["--scores", args.scores] if args.scores else []))
        fresh = rescore.exists() and rescore.stat().st_mtime != before
        if args.scores and rescore.exists() and rescore.stat().st_mtime > Path(args.scores).stat().st_mtime:
            todo = json.loads(rescore.read_text(encoding="utf-8"))
            sys.exit(f"\n{len(todo['indices'])} candidates gained location/company after scoring: "
                     f"{', '.join(n or '?' for n in todo['names'])}.\n"
                     f"Rescore engager indices {todo['indices']} with `uv run src/rank.py --dump --slug {slug}`, "
                     f"update {args.scores}, then rerun this command (already-resolved profiles are not paid again).")
        if fresh and not args.scores:
            run_step("Rescore with profile data", rank_cmd)

    out_csv = Path(out) / slug / "ranked_engagers.csv"
    final_csv = out_csv
    if args.draft:
        run_step("Draft outreach copy", [py, "src/draft.py", "--slug", slug, "--output-dir", out,
                 "--min-fit", str(max(3, args.min_fit))])
    if args.activity:
        activity_csv = out_csv.with_name("activity.csv")
        run_step("LinkedIn activity intel", [py, "src/activity.py", "--input", str(final_csv),
                 "--output", str(activity_csv), "--min-fit", str(args.min_fit)])
        final_csv = activity_csv
    if args.find_emails:
        enriched = out_csv.with_name("enriched.csv")
        run_step("Prospeo verified emails", [py, "src/email_enrich.py", "--input", str(final_csv),
                 "--output", str(enriched), "--min-fit", str(args.min_fit), "--max-lookups", str(args.max_lookups)])
        final_csv = enriched
    if args.supabase:
        run_step("Supabase snapshots", [py, "src/supabase_export.py", "--input", str(final_csv), "--run-id", slug])
    if args.campaign_id:
        run_step("Smartlead preview", [py, "src/push.py", "--input", str(final_csv),
                 "--campaign-id", str(args.campaign_id), "--min-fit", str(args.min_fit)])
    if args.heyreach_list_id:
        run_step("HeyReach preview", [py, "src/push_heyreach.py", "--input", str(final_csv),
                 "--list-id", str(args.heyreach_list_id), "--min-fit", str(args.min_fit)])
    run_step("Summary", [py, "src/summary.py", "--input", str(final_csv), "--min-fit", str(args.min_fit)])
    print(f"\n{'='*60}")
    print("PIPELINE COMPLETE")
    print(f"  Slug       : {slug}")
    print(f"  Ranked CSV : {out_csv.resolve()}")
    print(f"  Final CSV  : {final_csv.resolve()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
