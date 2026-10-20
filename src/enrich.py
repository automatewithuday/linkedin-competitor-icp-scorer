"""Company facts from Apify's LinkedIn company actor. The company stage calls this; `main` previews or runs it alone."""
import argparse
import sys
from pathlib import Path

ACTOR = "harvestapi~linkedin-company"


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


def main(argv=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import coredb, profiles, qualify, company_stage
    ap = argparse.ArgumentParser(description='Enrich linked current companies for the qualification core')
    ap.add_argument('--slug', required=True); ap.add_argument('--icp', required=True); ap.add_argument('--output-dir', default='./output')
    ap.add_argument('--dry-run', action='store_true'); ap.add_argument('--max-lookups', type=int, default=10); ap.add_argument('--max-usd', type=float, default=0.25)
    a = ap.parse_args(argv); con = coredb.connect(); v = profiles.load(con, a.icp)
    coredb.import_saved(con, Path(a.output_dir) / a.slug)
    company_stage.enrich_companies(con, qualify.version(con, v['id']), a.slug, a.max_lookups, a.max_usd, a.dry_run)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src.common import cli
    cli(main)
