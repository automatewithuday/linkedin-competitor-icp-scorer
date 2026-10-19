"""Harvest LinkedIn post engagers via Apify.

--profile URL: runs harvestapi/linkedin-profile-posts (reactions+comments ON) over the profile's recent posts.
--post URL:    runs harvestapi/linkedin-post-reactions + linkedin-post-comments on that ONE post (lnkd.in links resolved),
               profileScraperMode=main so engagers carry vanity URL, location and company (profile mode stays opaque: see resolve.py).
Both parse the flat dataset by `type` and emit:
  - <output-dir>/<slug>/posts.json     : the profile's posts
  - <output-dir>/<slug>/engagers.json  : unique engagers keyed by actor.id
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from dotenv import load_dotenv
try:
    from .common import location_text
except ImportError:
    from common import location_text

load_dotenv()

ACTOR = "harvestapi~linkedin-profile-posts"
POST_ACTORS = {"reaction": "harvestapi~linkedin-post-reactions", "comment": "harvestapi~linkedin-post-comments"}
POST_ITEM_USD = 0.002  # HarvestAPI pay-per-event price for both post actors; used only for the spend cap
POST_PROFILE_USD = 0.002  # extra "main-profile" event per engager with profileScraperMode=main


def resolve_post(url: str) -> str:
    """Canonical LinkedIn post URL (query/tracking stripped), following lnkd.in short links. '' if not a post."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    if urlsplit(url).hostname == "lnkd.in":
        try:
            url = requests.head(url, allow_redirects=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"}).url
        except requests.RequestException:
            return ""
    p = urlsplit(url)
    host = p.hostname or ""
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    if not (p.path.startswith("/posts/") or p.path.startswith("/feed/update/")):
        return ""
    return urlunsplit(("https", "www.linkedin.com", p.path, "", ""))


def load_token() -> str:
    tok = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_KEY") or ""
    if not tok:
        sys.exit("No Apify token found. Set APIFY_TOKEN in your .env file.")
    return tok


def post_run(actor: str, payload: dict, token: str, max_usd: float | None = None) -> dict:
    url = f"https://api.apify.com/v2/acts/{actor}/runs?token={token}"
    if max_usd is not None:
        url += f"&maxTotalChargeUsd={max_usd:.2f}"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    return json.loads(urllib.request.urlopen(req, timeout=45).read())["data"]


def get_run(run_id: str, token: str) -> dict:
    url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={token}"
    return json.loads(urllib.request.urlopen(url, timeout=45).read())["data"]


def get_items(dataset_id: str, token: str) -> list:
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}&format=json"
    return json.loads(urllib.request.urlopen(url, timeout=45).read())


def is_vanity_url(url: str) -> bool:
    """True if url is a real /in/ vanity URL (not an opaque ACoAA fsd_profile fallback)."""
    if not url or "/in/" not in url:
        return False
    return "/in/ACoAA" not in url


def profile_fields(actor: dict) -> tuple:
    """(url, location, company) from a post actor's `actor`; main mode carries publicIdentifier/location/currentPosition."""
    url = actor.get("linkedinUrl")
    if not is_vanity_url(url) and actor.get("publicIdentifier"):
        url = "https://www.linkedin.com/in/" + actor["publicIdentifier"]
    jobs = actor.get("currentPosition") or actor.get("experience") or []
    company = (jobs[0].get("companyName") or "") if isinstance(jobs, list) and jobs and isinstance(jobs[0], dict) else ""
    return url, location_text(actor.get("location")), company


def build_engagers(items: list) -> list:
    """Dedupe reaction+comment items by actor.id into engager objects."""
    engagers: dict[str, dict] = {}
    for it in items:
        t = it.get("type")
        if t not in ("reaction", "comment"):
            continue
        actor = it.get("actor") or {}
        aid = actor.get("id")
        if not aid:
            continue

        cand, location, company = profile_fields(actor)
        position = actor.get("position") or actor.get("headline")
        e = engagers.get(aid)
        if e is None:
            e = {
                "id": aid,
                "name": actor.get("name"),
                "linkedinUrl": cand,
                "position": position,
                "location": location,
                "company": company,
                "pictureUrl": actor.get("pictureUrl"),
                "engagement_count": 0,
                "posts_touched": set(),
                "reaction_types": [],
                "comments": [],
            }
            engagers[aid] = e

        # Prefer a real vanity URL over the ACoAA reaction fallback.
        if cand and (not is_vanity_url(e["linkedinUrl"])) and is_vanity_url(cand):
            e["linkedinUrl"] = cand
        for k, v in (("position", position), ("location", location), ("company", company)):
            if not e.get(k) and v:
                e[k] = v
        if not e.get("pictureUrl") and actor.get("pictureUrl"):
            e["pictureUrl"] = actor.get("pictureUrl")

        post_id = it.get("postId")
        if post_id:
            e["posts_touched"].add(post_id)

        if t == "reaction":
            rt = it.get("reactionType")
            if rt:
                e["reaction_types"].append(rt)
        elif t == "comment":
            text = it.get("commentary")
            if text:
                e["comments"].append(text)

    out = []
    for e in engagers.values():
        e["engagement_count"] = len(e["posts_touched"])
        e["posts_touched"] = sorted(e["posts_touched"])
        out.append(e)
    out.sort(key=lambda x: x["engagement_count"], reverse=True)
    return out


def build_posts(items: list) -> list:
    posts = []
    for it in items:
        if it.get("type") != "post":
            continue
        posts.append({
            "postId": it.get("id"),
            "url": it.get("linkedinUrl"),
            "content": it.get("content"),
            "engagement": it.get("engagement"),
        })
    return posts


def run_actor(actor: str, payload: dict, token: str, max_usd: float | None = None) -> tuple[list, dict]:
    """Start an actor run, poll to completion, return (dataset items, final run record). Exits on failure."""
    run = post_run(actor, payload, token, max_usd)
    run_id, ds_id = run["id"], run["defaultDatasetId"]
    print(f"  run_id={run_id} dataset={ds_id}")
    deadline = time.monotonic() + 1800
    while True:
        if time.monotonic() > deadline:
            sys.exit(f"Polling deadline exceeded; check Apify run {run_id} before starting another paid run")
        time.sleep(8)
        run = get_run(run_id, token)
        st = run["status"]
        print(f"  status={st}")
        if st in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    if st != "SUCCEEDED":
        sys.exit(f"Run did not succeed: {st} (usageTotalUsd=${run.get('usageTotalUsd')})")
    return get_items(ds_id, token), run


def tag_post_items(items: list, kind: str) -> list:
    """Post-level actors omit `type`; build_engagers keys on it."""
    return [{**it, "type": kind} for it in items]


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest LinkedIn post engagers (one profile's recent posts, or one post)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--profile", help="LinkedIn profile URL: harvest its recent posts")
    src.add_argument("--post", help="LinkedIn post URL (lnkd.in links accepted): harvest only this post")
    ap.add_argument("--slug", required=True, help="Slug for output directory")
    ap.add_argument("--max-posts", type=int, default=0, help="Max posts to fetch (0 = all; profile mode only)")
    ap.add_argument("--max-reactions", type=int, default=100)
    ap.add_argument("--max-comments", type=int, default=50)
    ap.add_argument("--posted-limit", default="month", help="24h | week | month")
    ap.add_argument("--output-dir", default="./output", help="Base output directory")
    args = ap.parse_args()

    token = load_token()
    out_dir = Path(args.output_dir) / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.post:
        post = resolve_post(args.post)
        if not post:
            sys.exit("--post must be a LinkedIn post URL (linkedin.com/posts/... or /feed/update/...) or an lnkd.in link to one")
        items, cost = [], 0.0
        for kind, actor in POST_ACTORS.items():
            n = args.max_reactions if kind == "reaction" else args.max_comments
            # main mode: actor becomes a full profile (vanity URL, location, current company), +$0.002/engager
            payload = {"posts": [post], "maxItems": n, "profileScraperMode": "main"}
            cap = round(n * (POST_ITEM_USD + POST_PROFILE_USD) * 1.25 + 0.05, 2)
            print(f"Run: {actor} on {post}")
            print(f"  maxItems={n} spend cap=${cap:.2f}")
            got, run = run_actor(actor, payload, token, cap)
            items += tag_post_items(got, kind)
            cost += float(run.get("usageTotalUsd") or 0)
        post_ids = sorted({it.get("postId") for it in items if it.get("postId")}) or [post]
        posts = [{"postId": pid, "url": post, "content": None, "engagement": None} for pid in post_ids]
        source = post
    else:
        payload = {
            "targetUrls": [args.profile],
            "maxPosts": args.max_posts,
            "postedLimit": args.posted_limit,
            "scrapeReactions": True,
            "maxReactions": args.max_reactions,
            "scrapeComments": True,
            "maxComments": args.max_comments,
            "commentsPostedLimit": args.posted_limit,
            "includeReposts": True,
            "includeQuotePosts": True,
        }
        print(f"Run: {ACTOR} on {args.profile}")
        print(f"  maxPosts={args.max_posts} maxReactions={args.max_reactions} "
              f"maxComments={args.max_comments} postedLimit={args.posted_limit}")
        items, run = run_actor(ACTOR, payload, token)
        cost = run.get("usageTotalUsd")
        posts = build_posts(items)
        source = args.profile

    engagers = build_engagers(items)
    urls = {p['postId']: p['url'] for p in posts if p.get('url')}
    for e in engagers:
        e['source_profile'] = source
        e['post_urls'] = [urls[pid] for pid in e['posts_touched'] if pid in urls]
    with_comments = sum(1 for e in engagers if e["comments"])

    (out_dir / "posts.json").write_text(
        json.dumps(posts, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "engagers.json").write_text(
        json.dumps(engagers, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== SUCCEEDED | usageTotalUsd=${cost} ===")
    print(f"  dataset items     : {len(items)}")
    print(f"  posts             : {len(posts)}")
    print(f"  unique engagers   : {len(engagers)}")
    print(f"  engagers w/comment: {with_comments}")
    print(f"  -> {out_dir / 'posts.json'}")
    print(f"  -> {out_dir / 'engagers.json'}")


if __name__ == "__main__":
    main()
