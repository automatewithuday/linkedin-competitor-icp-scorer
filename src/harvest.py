"""Harvest LinkedIn profile posts + deduped engagers via Apify.

Runs harvestapi/linkedin-profile-posts (reactions+comments ON) for one profile,
parses the flat dataset by `type`, and emits:
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

from dotenv import load_dotenv

load_dotenv()

ACTOR = "harvestapi~linkedin-profile-posts"


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

        e = engagers.get(aid)
        if e is None:
            e = {
                "id": aid,
                "name": actor.get("name"),
                "linkedinUrl": actor.get("linkedinUrl"),
                "position": actor.get("position"),
                "pictureUrl": actor.get("pictureUrl"),
                "engagement_count": 0,
                "posts_touched": set(),
                "reaction_types": [],
                "comments": [],
            }
            engagers[aid] = e

        # Prefer a real vanity URL over the ACoAA reaction fallback.
        cand = actor.get("linkedinUrl")
        if cand and (not is_vanity_url(e["linkedinUrl"])) and is_vanity_url(cand):
            e["linkedinUrl"] = cand
        if not e.get("position") and actor.get("position"):
            e["position"] = actor.get("position")
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest LinkedIn profile posts + deduped engagers")
    ap.add_argument("--profile", required=True, help="LinkedIn profile URL")
    ap.add_argument("--slug", required=True, help="Slug for output directory")
    ap.add_argument("--max-posts", type=int, default=0, help="Max posts to fetch (0 = all)")
    ap.add_argument("--max-reactions", type=int, default=100)
    ap.add_argument("--max-comments", type=int, default=50)
    ap.add_argument("--posted-limit", default="month", help="24h | week | month")
    ap.add_argument("--output-dir", default="./output", help="Base output directory")
    args = ap.parse_args()

    token = load_token()
    out_dir = Path(args.output_dir) / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)

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
    run = post_run(ACTOR, payload, token)
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

    cost = run.get("usageTotalUsd")
    if st != "SUCCEEDED":
        sys.exit(f"Run did not succeed: {st} (usageTotalUsd=${cost})")

    items = get_items(ds_id, token)
    posts = build_posts(items)
    engagers = build_engagers(items)
    urls = {p['postId']: p['url'] for p in posts if p.get('url')}
    for e in engagers:
        e['source_profile'] = args.profile
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
