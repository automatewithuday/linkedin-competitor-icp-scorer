"""Orchestrator: harvest -> (enrich) -> icp -> rank

Usage:
  python run.py --profile <linkedin-url> --slug <slug> --domain <domain> [options]
  python run.py --profile <linkedin-url> --slug <slug> --icp icp.yaml [options]
"""
import argparse
import os
import re
from dotenv import load_dotenv
from src.common import linkedin_url, llm_provider
import subprocess
import sys
from pathlib import Path


def run_step(label: str, cmd: list[str]) -> None:
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        sys.exit(f"Step failed: {label} (exit {result.returncode})")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Full pipeline: harvest LinkedIn engagers, derive ICP, rank as warm buyers"
    )
    ap.add_argument("--profile", required=True, help="LinkedIn profile URL to harvest")
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
    ap.add_argument("--supabase", action="store_true", help="Also store lead snapshots in Supabase")
    ap.add_argument("--campaign-id", type=int, help="Generate a Smartlead import preview")
    args = ap.parse_args()
    load_dotenv()
    if not linkedin_url(args.profile):
        ap.error("--profile must be a public LinkedIn /in/ URL")
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', args.slug):
        ap.error("Slug must contain only letters, digits, underscores and hyphens")
    if min(args.max_posts, args.max_reactions, args.max_comments) < 0 or args.max_lookups < 1:
        ap.error("Invalid scrape or lookup limits")
    if args.campaign_id is not None and (args.campaign_id < 1 or not args.find_emails):
        ap.error("--campaign-id must be positive and requires --find-emails")
    required = []
    if not llm_provider():
        sys.exit('No LLM for scoring: set OPENAI_API_KEY in .env, or install the `claude` CLI and log in '
                 'to your Claude subscription (LLM_PROVIDER=claude). See README.')
    if not (os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY')):
        required.append('APIFY_TOKEN')
    if args.domain:
        required.append('FIRECRAWL_API_KEY')
    if args.find_emails:
        required.append('PROSPEO_API_KEY')
    if args.supabase:
        required.append('SUPABASE_URL')
        if not (os.getenv('SUPABASE_SECRET_KEY') or os.getenv('SUPABASE_SERVICE_ROLE_KEY')):
            required.append('SUPABASE_SECRET_KEY')
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        ap.error('Missing configuration: ' + ', '.join(missing))
    if args.icp and not Path(args.icp).is_file():
        ap.error('ICP file does not exist')

    py = sys.executable
    out = args.output_dir
    slug = args.slug

    # Step 1: Harvest
    harvest_cmd = [
        py, "src/harvest.py",
        "--profile", args.profile,
        "--slug", slug,
        "--max-posts", str(args.max_posts),
        "--max-reactions", str(args.max_reactions),
        "--max-comments", str(args.max_comments),
        "--posted-limit", args.posted_limit,
        "--output-dir", out,
    ]
    run_step("Harvest posts + engagers", harvest_cmd)

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
    ]
    run_step("Score + rank engagers", rank_cmd)

    out_csv = Path(out) / slug / "ranked_engagers.csv"
    final_csv = out_csv
    if args.find_emails:
        final_csv = out_csv.with_name("enriched.csv")
        run_step("Prospeo verified emails", [py, "src/email_enrich.py", "--input", str(out_csv),
                 "--output", str(final_csv), "--min-fit", str(args.min_fit), "--max-lookups", str(args.max_lookups)])
    if args.supabase:
        run_step("Supabase snapshots", [py, "src/supabase_export.py", "--input", str(final_csv), "--run-id", slug])
    if args.campaign_id:
        run_step("Smartlead preview", [py, "src/push.py", "--input", str(final_csv),
                 "--campaign-id", str(args.campaign_id), "--min-fit", str(args.min_fit)])
    print(f"\n{'='*60}")
    print("PIPELINE COMPLETE")
    print(f"  Slug       : {slug}")
    print(f"  Ranked CSV : {out_csv.resolve()}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
