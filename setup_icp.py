"""ICP intake: website first, then a short interview with pre-filled answers.

Writes icp.yaml for `src/icp.py --icp` / `run.py --icp`.

Flow:
  1. Optional website. It is scraped (Firecrawl if configured, plain HTTP otherwise)
     and the LLM proposes an ICP you confirm or correct. This anchors the interview.
  2. Nine questions, each pre-filled from the proposal. Enter accepts the pre-fill.
  3. The LLM cleans the answers and expands job titles with common synonyms, because
     headline matching is all the scorer has and "VP Sales" alone misses "Head of Sales".
  4. You review the final ICP and it is written.

Without an LLM (no OpenAI key, no claude CLI) the raw answers are written as-is.
Hard filters (titles, industries, size, geos, disqualifiers) decide fit. Triggers are
soft: they are personalization context, never a reason to reject someone.
"""
import argparse
import json
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.common import llm_json, llm_provider

load_dotenv()

PROPOSE_SYSTEM = (
    "You are a B2B go-to-market analyst. From scraped website copy, infer WHO THIS COMPANY "
    "SELLS TO (not what the company is). Be concrete and salesperson-actionable. "
    "Return STRICT JSON only:\n"
    '{"what_you_sell":"one sentence","titles":["buyer job titles"],"industries":["..."],'
    '"company_size":"e.g. 50-500 employees","geos":["countries"],'
    '"best_customers":["customer names visible in logos/case studies, else empty"],'
    '"summary":"one sentence: who they sell to and the pain solved"}'
)

CLEAN_SYSTEM = (
    "You are a B2B GTM analyst cleaning an ICP intake for a LinkedIn-headline scorer. "
    "Return STRICT JSON with keys: titles, industries, industries_out, company_size, geos, "
    "triggers, disqualifiers, best_customers, summary.\n"
    "titles: the user's titles PLUS common synonyms and seniority variants that would appear in "
    "LinkedIn headlines (e.g. 'VP Sales' -> 'VP of Sales', 'Head of Sales', 'SVP Sales', "
    "'Chief Sales Officer'). Deduplicate, keep it under 25, do not add unrelated functions.\n"
    "industries/industries_out/geos/best_customers: cleaned, deduplicated, properly capitalised lists.\n"
    "company_size: normalise to 'MIN-MAX employees'.\n"
    "triggers: keep as given; they are soft signals.\n"
    "disqualifiers: keep as given; these are hard exclusions.\n"
    "summary: ONE sentence: who they sell to and the pain they solve."
)


def ask(prompt: str, default: str = "") -> str:
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    val = input(shown).strip()
    return val if val else default


def ask_list(prompt: str, default: list | None = None) -> list[str]:
    raw = ask(prompt, ", ".join(default or []))
    if raw.lower() in ("skip", "none", "-"):
        return []
    return [i.strip() for i in raw.replace(";", ",").split(",") if i.strip()]


def propose_from_website(url: str) -> dict:
    from src.icp import gather_text
    print(f"\nScraping {url} ...")
    text, sources = gather_text(url)
    if not text:
        print("  Could not read the site. Continuing without a proposal.")
        return {}
    print(f"  read {len(sources)} page(s). Asking the LLM for a first-pass ICP ...")
    data, _, _ = llm_json(PROPOSE_SYSTEM, f"Website: {url}\n\nScraped copy:\n{text}\n\nReturn the strict JSON.")
    return data


def show(icp: dict) -> None:
    print(yaml.dump(icp, allow_unicode=True, sort_keys=False))


def main() -> None:
    ap = argparse.ArgumentParser(description="ICP intake -> icp.yaml")
    ap.add_argument("--output", default="icp.yaml")
    ap.add_argument("--website", help="Skip the first prompt and scrape this site")
    args = ap.parse_args()
    out_path = Path(args.output)
    provider = llm_provider()

    print("\n=== ICP Intake ===")
    print("Enter accepts the value in [brackets]. Separate items with commas. Type 'skip' for none.\n")

    proposal: dict = {}
    site = args.website or ask("Website of the business you sell from (Enter to skip)")
    if site:
        if not provider:
            print("  No LLM configured (OpenAI key or claude CLI), so the site cannot be summarised. Skipping.")
        else:
            if "://" not in site:
                site = "https://" + site
            try:
                proposal = propose_from_website(site)
            except Exception as exc:
                print(f"  Proposal failed ({exc}). Continuing without it.")
    if proposal:
        print("\nBased on the website, here is a starting point. You will confirm each part next.\n")
        show(proposal)

    a: dict = {}
    a["what_you_sell"] = ask("1. What do you sell, in one sentence?", proposal.get("what_you_sell", ""))
    a["best_customers"] = ask_list(
        "2. Name your best 1-3 customers (companies). Anchors everything else", proposal.get("best_customers") or [])
    a["titles"] = ask_list(
        "3. Job titles that BUY this (above ~50 employees it is rarely the CEO)", proposal.get("titles") or [])
    a["company_size"] = ask(
        "4. Headcount range, numbers not labels (e.g. 50-500)", proposal.get("company_size", ""))
    a["industries"] = ask_list("5. Industries IN", proposal.get("industries") or [])
    a["industries_out"] = ask_list("6. Industries OUT (Enter or 'skip' for none)")
    a["geos"] = ask_list("7. Countries", proposal.get("geos") or [])
    a["triggers"] = ask_list(
        "8. Buying triggers (hiring SDRs, raised a round). Soft signals, never filters; 'skip' ok")
    a["disqualifiers"] = ask_list(
        "9. Hard disqualifiers: competitors, existing customers, partners; 'skip' ok")

    icp = {
        "titles": a["titles"], "industries": a["industries"], "industries_out": a["industries_out"],
        "company_size": a["company_size"], "geos": a["geos"], "triggers": a["triggers"],
        "disqualifiers": a["disqualifiers"], "best_customers": a["best_customers"],
        "summary": a["what_you_sell"],
    }

    if provider and ask(f"\nUse {provider} to clean this and add title synonyms? (y/n)", "y").lower().startswith("y"):
        try:
            cleaned, _, _ = llm_json(CLEAN_SYSTEM, json.dumps(icp, indent=2))
            icp = {k: cleaned.get(k) or icp[k] for k in icp}
        except Exception as exc:
            print(f"  Cleanup failed ({exc}). Keeping raw answers.")

    print("\n=== Final ICP ===")
    show(icp)
    titles = ask_list("Titles look right? Enter to accept, or retype the full list", icp["titles"])
    icp["titles"] = titles or icp["titles"]

    if out_path.exists() and not ask(f"{out_path} exists. Overwrite? (y/n)", "n").lower().startswith("y"):
        sys.exit("Aborted, file not written.")
    out_path.write_text(yaml.dump(icp, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"\n  -> {out_path.resolve()}")
    print("  Next: run.py --icp icp.yaml against a competitor profile with --max-posts 3 to sanity-check scoring.")


if __name__ == "__main__":
    main()
