"""Enrich engager companies via Apify LinkedIn company actor. Optional step.

Derives a company identifier from each engager's `position` headline
("Founder @Acme Corp" -> "Acme Corp"), then batches them through
harvestapi/linkedin-company to get {size, industry, geo, name, url}.

Output: <output-dir>/<slug>/companies.json
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ACTOR = "harvestapi~linkedin-company"


def load_token() -> str:
    tok = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_KEY") or ""
    if not tok:
        sys.exit("No Apify token found. Set APIFY_TOKEN in your .env file.")
    return tok


def post_run(actor: str, payload: dict, token: str) -> dict:
    url = f"https://api.apify.com/v2/acts/{actor}/runs?token={token}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(req).read())["data"]


def get_run(run_id: str, token: str) -> dict:
    url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}"
    return json.loads(urllib.request.urlopen(url).read())["data"]


def get_items(dataset_id: str, token: str) -> list:
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}&format=json"
    return json.loads(urllib.request.urlopen(url).read())


def company_from_position(position: str | None) -> str | None:
    """Extract company name from a headline like 'Founder @Acme Corp' or 'Head of Sales at Acme'."""
    if not position:
        return None
    m = re.search(r"@\s*([^|·\n]+)", position)
    if not m:
        m = re.search(r"\bat\s+([^|·\n]+)", position, flags=re.IGNORECASE)
    if not m:
        return None
    name = m.group(1).strip()
    name = re.split(r"\s+[|·]\s+", name)[0].strip()
    if re.search(r"\bfollowers?\b", name, flags=re.IGNORECASE):
        return None
    return name or None


def first(d: dict, *keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def extract_geo(locations) -> str | None:
    """Pick the HQ from a locations list."""
    if not isinstance(locations, list) or not locations:
        return None
    hq = next((l for l in locations if isinstance(l, dict) and l.get("headquarter")), None)
    loc = hq or (locations[0] if isinstance(locations[0], dict) else None)
    if not loc:
        return None
    parsed = loc.get("parsed") or {}
    if parsed.get("text"):
        return parsed["text"]
    parts = [loc.get("city"), loc.get("geographicArea"), loc.get("country")]
    return ", ".join(p for p in parts if p) or None


def extract_industry(item: dict) -> str | None:
    ind = first(item, "industries", "industry", "industryName")
    if isinstance(ind, list):
        names = [i.get("name") if isinstance(i, dict) else i for i in ind]
        return ", ".join(n for n in names if n) or None
    if isinstance(ind, dict):
        return ind.get("name")
    return ind


def extract_company(item: dict) -> dict:
    return {
        "name": first(item, "name", "companyName", "title"),
        "size": first(item, "employeeCount", "employeeCountRange",
                      "companySize", "staffCount", "size", "employees"),
        "industry": extract_industry(item),
        "geo": extract_geo(first(item, "locations", "location")),
        "url": first(item, "linkedinUrl", "url", "website", "companyUrl"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Enrich engager companies via LinkedIn company actor")
    ap.add_argument("--slug", required=True, help="Slug matching harvest output")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from-engagers", action="store_true",
                     help="Derive company names from engagers.json positions")
    src.add_argument("--names", help="Comma-separated company names (name search)")
    src.add_argument("--urls", help="Comma-separated LinkedIn company URLs")
    ap.add_argument("--max-engager-companies", type=int, default=3,
                    help="Cap distinct companies pulled from engagers (cost guard)")
    ap.add_argument("--output-dir", default="./output", help="Base output directory")
    args = ap.parse_args()

    token = load_token()
    out_dir = Path(args.output_dir) / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    use_urls = False
    if args.urls:
        identifiers = [u.strip() for u in args.urls.split(",") if u.strip()]
        use_urls = True
    elif args.names:
        identifiers = [n.strip() for n in args.names.split(",") if n.strip()]
    else:
        eng_path = out_dir / "engagers.json"
        if not eng_path.exists():
            sys.exit(f"No engagers file: {eng_path} (run harvest.py first, or pass --names/--urls)")
        engagers = json.loads(eng_path.read_text(encoding="utf-8"))
        seen, identifiers = set(), []
        for e in engagers:
            c = company_from_position(e.get("position"))
            if c and c.lower() not in seen:
                seen.add(c.lower())
                identifiers.append(c)
            if len(identifiers) >= args.max_engager_companies:
                break

    if not identifiers:
        sys.exit("No company identifiers to enrich.")

    payload = {"companies": identifiers} if use_urls else {"searches": identifiers}
    field = "companies (URLs)" if use_urls else "searches (names)"
    print(f"Run: {ACTOR} | input={field} | {len(identifiers)} item(s)")
    for i in identifiers:
        print(f"  - {i}")

    run = post_run(ACTOR, payload, token)
    run_id, ds_id = run["id"], run["defaultDatasetId"]
    print(f"  run_id={run_id} dataset={ds_id}")

    while True:
        time.sleep(8)
        run = get_run(run_id, token)
        st = run["status"]
        print(f"  status={st}")
        if st in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    cost = run.get("usageTotalUsd")
    if st != "SUCCEEDED":
        sys.exit(f"Run did not succeed: {st} (usageTotalUsd=${cost})")

    items = get_items(ds_id, token)
    companies: dict[str, dict] = {}
    for idx, item in enumerate(items):
        norm = extract_company(item)
        oq = item.get("originalQuery") or {}
        key = oq.get("search") or oq.get("url") or norm.get("name") or f"item_{idx}"
        companies[key] = norm

    (out_dir / "companies.json").write_text(
        json.dumps(companies, ensure_ascii=False, indent=2), encoding="utf-8")

    n = len(items)
    per = (cost / n) if (cost and n) else None
    print(f"\n=== SUCCEEDED | usageTotalUsd=${cost} ===")
    print(f"  companies enriched: {n}")
    print(f"  $/item            : {('$%.5f' % per) if per is not None else 'n/a'}")
    print(f"  -> {out_dir / 'companies.json'}")
    if items:
        print(f"  raw item[0] keys  : {', '.join(sorted(items[0].keys()))}")


if __name__ == "__main__":
    main()
