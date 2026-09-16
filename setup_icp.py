"""Interactive ICP intake — asks ~8 questions, writes icp.yaml.

If an LLM is configured (OpenAI key or claude CLI) and the user agrees, calls it once to
expand raw answers into a tight summary and clean the lists. Without a key
the raw answers are written as-is.

Output file must be compatible with src/icp.py --icp: requires keys
titles, industries, company_size, geos, summary.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()


def ask(prompt: str, default: str = "") -> str:
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    val = input(prompt).strip()
    return val if val else default


def ask_list(prompt: str) -> list[str]:
    raw = ask(prompt)
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def expand_with_llm(answers: dict) -> dict:
    """Call the configured LLM to clean lists and generate a one-sentence summary."""
    from src.common import llm_json

    user_msg = (
        "Given this ICP intake, return a JSON object with these fields:\n"
        "  titles: list of job title strings (deduplicated, properly capitalised)\n"
        "  industries: list of industry strings\n"
        "  industries_out: list of excluded industry strings\n"
        "  company_size: string like '50-500 employees'\n"
        "  geos: list of country/region strings\n"
        "  triggers: list of buying trigger strings\n"
        "  disqualifiers: list of hard-disqualifier strings\n"
        "  summary: ONE sentence — who you sell to and the pain you solve\n\n"
        f"Raw answers:\n{json.dumps(answers, indent=2)}\n\n"
        "Return STRICT JSON only."
    )
    data, _, _ = llm_json("You are a B2B GTM analyst. Clean and structure an ICP intake.", user_msg)
    return data


def build_yaml(raw: dict) -> dict:
    return {
        "titles": raw.get("titles") or [],
        "industries": raw.get("industries") or [],
        "industries_out": raw.get("industries_out") or [],
        "company_size": raw.get("company_size") or "",
        "geos": raw.get("geos") or [],
        "triggers": raw.get("triggers") or [],
        "disqualifiers": raw.get("disqualifiers") or [],
        "summary": raw.get("summary") or "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Interactive ICP intake — writes icp.yaml for use with src/icp.py --icp"
    )
    ap.add_argument("--output", default="icp.yaml", help="Output path (default: icp.yaml)")
    args = ap.parse_args()

    out_path = Path(args.output)

    print("\n=== ICP Intake ===")
    print("Answer each question. Separate multiple items with commas.\n")

    answers: dict = {}

    answers["what_you_sell"] = ask("1. What do you sell? (one line)")
    answers["titles"] = ask_list("2. Job titles you sell to (e.g. VP Sales, Head of RevOps)")
    answers["industries_in"] = ask_list("3. Industries to target (e.g. B2B SaaS, Fintech)")
    answers["industries_out"] = ask_list("4. Industries to exclude (or press Enter to skip)")
    answers["company_size"] = ask("5. Company size range (e.g. 50-500 employees)")
    answers["geos"] = ask_list("6. Geographies / countries (e.g. United States, Canada)")
    answers["triggers"] = ask_list(
        "7. Buying triggers or pains (e.g. raised Series A, new VP hired)"
    )
    answers["disqualifiers"] = ask_list(
        "8. Hard disqualifiers (e.g. competitors, existing customers)"
    )

    # Optional LLM expansion
    from src.common import llm_provider
    provider = llm_provider()
    if provider:
        use_llm = ask(f"\nLLM available ({provider}). Use it to clean and summarise? (y/n)", "y")
        if use_llm.lower().startswith("y"):
            print(f"  Calling {provider} ...")
            try:
                expanded = expand_with_llm(answers)
                icp = build_yaml(expanded)
            except Exception as exc:
                print(f"  LLM call failed ({exc}). Writing raw answers instead.")
                icp = build_yaml({
                    "titles": answers["titles"],
                    "industries": answers["industries_in"],
                    "industries_out": answers["industries_out"],
                    "company_size": answers["company_size"],
                    "geos": answers["geos"],
                    "triggers": answers["triggers"],
                    "disqualifiers": answers["disqualifiers"],
                    "summary": answers["what_you_sell"],
                })
        else:
            icp = build_yaml({
                "titles": answers["titles"],
                "industries": answers["industries_in"],
                "industries_out": answers["industries_out"],
                "company_size": answers["company_size"],
                "geos": answers["geos"],
                "triggers": answers["triggers"],
                "disqualifiers": answers["disqualifiers"],
                "summary": answers["what_you_sell"],
            })
    else:
        icp = build_yaml({
            "titles": answers["titles"],
            "industries": answers["industries_in"],
            "industries_out": answers["industries_out"],
            "company_size": answers["company_size"],
            "geos": answers["geos"],
            "triggers": answers["triggers"],
            "disqualifiers": answers["disqualifiers"],
            "summary": answers["what_you_sell"],
        })

    # Confirm overwrite if file already exists
    if out_path.exists():
        overwrite = ask(f"\n{out_path} already exists. Overwrite? (y/n)", "n")
        if not overwrite.lower().startswith("y"):
            print("Aborted — file not written.")
            sys.exit(0)

    out_path.write_text(yaml.dump(icp, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"\n  -> {out_path.resolve()}")


if __name__ == "__main__":
    main()
