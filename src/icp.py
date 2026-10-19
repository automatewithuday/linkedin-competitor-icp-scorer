"""Derive or load recipient ICP. Two mutually-exclusive modes:

  --domain <domain>   Auto mode: scrape the domain via Spider/Firecrawl and use
                      the configured LLM to infer who the company sells to.
  --icp <path.yaml>   Manual mode: load a user-written icp.yaml and write the
                      same recipient_icp.json shape, skipping the scrape.

Output: <output-dir>/<slug>/recipient_icp.json
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
try:
    from .common import llm_json
except ImportError:
    from common import llm_json

load_dotenv()

FIRECRAWL_API_KEY = os.environ.get("FIRECRAWL_API_KEY")

SYSTEM = (
    "You are a B2B go-to-market analyst. Given marketing copy scraped from a "
    "company's website, infer WHO THAT COMPANY SELLS TO -- their customers' ICP "
    "(ideal customer profile). You are NOT describing the company itself; you are "
    "describing the buyers it targets.\n"
    "Be concrete and salesperson-actionable:\n"
    "- titles: real buyer job titles a salesperson would target (e.g. 'VP Engineering', "
    "'Head of Security', 'CISO', 'Director of Robotics'), NOT vague labels like 'decision makers'.\n"
    "- size_range: company size of the buyer (e.g. '50-500', 'SMB', 'Mid-market', "
    "'Enterprise', or a mix).\n"
    "- industries: the verticals/industries the buyers operate in.\n"
    "- geos: geographies the company appears to sell into (e.g. 'US', 'North America', "
    "'Global', 'EMEA'). If unclear, use ['Global'].\n"
    "- summary: ONE sentence naming who they sell to.\n"
    "If the copy is thin, infer the most likely ICP from the product/positioning rather "
    "than returning empty arrays.\n"
    'Return STRICT JSON only: {"titles":[...],"size_range":"...","industries":[...],'
    '"geos":[...],"summary":"..."}'
)

LIMIT = 12000
SECONDARY_PATHS = ["/about", "/about-us", "/product", "/platform", "/solutions", "/company"]


def _spider_scrape(url: str) -> str:
    try:
        r = subprocess.run(
            ["spider", "-u", url, "--limit", "1", "scrape"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60, check=False,
        )
    except Exception:
        return ""
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            page = json.loads(line)
        except json.JSONDecodeError:
            continue
        return (page.get("markdown") or page.get("content") or "").strip()
    return ""


def _firecrawl_client():
    from firecrawl import Firecrawl
    if not FIRECRAWL_API_KEY:
        raise ValueError("FIRECRAWL_API_KEY not set — set it in .env or use --icp manual mode")
    return Firecrawl(api_key=FIRECRAWL_API_KEY)


def _firecrawl_scrape(fc, url: str, js: bool = False) -> str:
    kwargs = {"formats": ["markdown"], "only_main_content": True, "timeout": 30000}
    if js:
        kwargs["wait_for"] = 2500
        kwargs["timeout"] = 60000
    try:
        doc = fc.scrape(url, **kwargs)
    except Exception:
        return ""
    return (getattr(doc, "markdown", None) or "").strip()


def _plain_scrape(url: str) -> str:
    """No-key fallback: fetch the page and strip tags with the stdlib parser."""
    import requests
    from html.parser import HTMLParser

    class _Text(HTMLParser):
        def __init__(self):
            super().__init__(); self.parts = []; self.skip = 0
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "svg"):
                self.skip += 1
        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "svg") and self.skip:
                self.skip -= 1
        def handle_data(self, data):
            if not self.skip and data.strip():
                self.parts.append(data.strip())
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (icp-intake)"})
        r.raise_for_status()
    except Exception:
        return ""
    p = _Text(); p.feed(r.text)
    return "\n".join(p.parts)


def _scrape_one(url: str, fc) -> str:
    text = _spider_scrape(url)
    if len(text) >= 200:
        return text
    if fc is None:
        return _plain_scrape(url)
    text = _firecrawl_scrape(fc, url, js=False)
    if len(text) >= 200:
        return text
    return _firecrawl_scrape(fc, url, js=True)


def gather_text(domain: str) -> tuple[str, list[str]]:
    base = f"https://{domain}" if not domain.startswith("http") else domain
    fc = _firecrawl_client() if FIRECRAWL_API_KEY else None  # no key: plain HTTP fallback

    chunks: list[str] = []
    sources: list[str] = []

    home = _scrape_one(base, fc)
    if home:
        chunks.append(f"## Homepage ({base})\n{home}")
        sources.append(base)

    if sum(len(c) for c in chunks) < LIMIT:
        for path in SECONDARY_PATHS:
            url = base.rstrip("/") + path
            sec = _scrape_one(url, fc)
            if len(sec) >= 200:
                chunks.append(f"## {path} ({url})\n{sec}")
                sources.append(url)
                break

    text = "\n\n".join(chunks)[:LIMIT]
    return text, sources


def derive_from_domain(domain: str, slug: str, out_dir: Path) -> None:
    print(f"Scraping {domain} ...")
    text, sources = gather_text(domain)
    if not text:
        print(f"SCRAPE FAILED: no usable content for {domain}")
        sys.exit(2)
    print(f"  scraped {len(sources)} page(s), {len(text)} chars")

    user = (
        f"Company domain: {domain}\n\n"
        f"Scraped marketing copy:\n{text}\n\n"
        "Infer who this company SELLS TO and return the strict JSON."
    )
    raw, in_tok, out_tok = llm_json(SYSTEM, user)

    icp = {
        "domain": domain,
        "slug": slug,
        "titles": raw.get("titles") or [],
        "size_range": raw.get("size_range") or "",
        "industries": raw.get("industries") or [],
        "geos": raw.get("geos") or [],
        "summary": raw.get("summary") or "",
        "sources": sources,
    }

    out_path = out_dir / "recipient_icp.json"
    out_path.write_text(json.dumps(icp, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== DERIVED ICP ===")
    print(f"  summary    : {icp['summary']}")
    print(f"  titles     : {', '.join(icp['titles'])}")
    print(f"  size_range : {icp['size_range']}")
    print(f"  industries : {', '.join(icp['industries'])}")
    print(f"  geos       : {', '.join(icp['geos'])}")
    print(f"  tokens in/out: {in_tok}/{out_tok}")
    print(f"  -> {out_path}")


def derive_from_yaml(icp_path: str, slug: str, out_dir: Path) -> None:
    path = Path(icp_path)
    if not path.exists():
        sys.exit(f"ICP file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    icp = {
        "slug": slug,
        "titles": raw.get("titles") or [],
        "size_range": raw.get("company_size") or "",
        "industries_out": raw.get("industries_out") or [],
        "disqualifiers": raw.get("disqualifiers") or [],
        "buying_triggers": raw.get("buying_triggers") or raw.get("triggers") or [],
        "industries": raw.get("industries") or [],
        "geos": raw.get("geos") or [],
        "best_customers": raw.get("best_customers") or [],
        "summary": raw.get("summary") or "",
        "sources": [str(path)],
    }

    out_path = out_dir / "recipient_icp.json"
    out_path.write_text(json.dumps(icp, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=== LOADED ICP (manual) ===")
    print(f"  summary    : {icp['summary']}")
    print(f"  titles     : {', '.join(icp['titles'])}")
    print(f"  size_range : {icp['size_range']}")
    print(f"  industries : {', '.join(icp['industries'])}")
    print(f"  geos       : {', '.join(icp['geos'])}")
    print(f"  -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Derive or load recipient ICP")
    ap.add_argument("--slug", required=True, help="Slug matching harvest output")
    ap.add_argument("--output-dir", default="./output", help="Base output directory")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--domain", help="Auto mode: scrape domain and infer ICP via LLM")
    mode.add_argument("--icp", metavar="PATH", help="Manual mode: path to icp.yaml")
    args = ap.parse_args()

    out_dir = Path(args.output_dir) / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.domain:
        derive_from_domain(args.domain, args.slug, out_dir)
    else:
        derive_from_yaml(args.icp, args.slug, out_dir)


if __name__ == "__main__":
    main()
