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
    from .evidence import profile_evidence
except ImportError:
    from common import location_text
    from evidence import profile_evidence

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
    jobs = profile_evidence(actor).get("current_roles") or []
    company = jobs[0].get("company", "") if jobs else ""
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
        evidence = profile_evidence(actor)
        e = engagers.get(aid)
        if e is None:
            e = {
                "id": aid,
                "name": actor.get("name"),
                "linkedinUrl": cand,
                "position": position,
                "location": location,
                "company": company,
                "profile_evidence": evidence,
                "pictureUrl": actor.get("pictureUrl"),
                "engagement_count": 0,
                "posts_touched": set(),
                "reaction_types": [],
                "comments": [],
                "comment_post_ids": [],  # parallel to comments
            }
            engagers[aid] = e

        # Prefer a real vanity URL over the ACoAA reaction fallback.
        if cand and (not is_vanity_url(e["linkedinUrl"])) and is_vanity_url(cand):
            e["linkedinUrl"] = cand
        for k, v in (("position", position), ("location", location), ("company", company)):
            if not e.get(k) and v:
                e[k] = v
        if evidence and (not e.get("profile_evidence") or
                         (evidence.get("current_roles") and not e["profile_evidence"].get("current_roles"))):
            e["profile_evidence"] = evidence
            if company:
                e["company"] = company
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
                e["comment_post_ids"].append(post_id or "")

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


def claim_harvest(con, coredb, actor, payload, slug, charge_cap):
    identity=coredb.fingerprint([slug,actor,payload])
    with con:
        con.execute('BEGIN IMMEDIATE')
        old=con.execute("SELECT * FROM provider_requests WHERE provider='apify' AND purpose='harvest' AND identity=? ORDER BY id DESC LIMIT 1",(identity,)).fetchone()
        retry=os.getenv('CORE_RETRY_UNKNOWN')=='1'
        if old and not (retry and old['status'] in ('pending','unknown')):
            return dict(old),False
        cap=charge_cap or 0
        budget=float(os.getenv('CORE_HARVEST_BUDGET') or cap or 1)
        used=con.execute("SELECT COALESCE(SUM(charge_cap),0) FROM provider_requests WHERE purpose='harvest' AND run_slug=?",(slug,)).fetchone()[0]
        if used+cap>budget+1e-9:
            raise RuntimeError('Harvest budget already reserved; review previous requests before increasing the cap')
        con.execute('INSERT OR IGNORE INTO core_runs(slug,created_at) VALUES (?,?)',(slug,coredb.ledger.now()))
        rid=con.execute('INSERT INTO provider_requests(provider,purpose,identity,run_slug,request_json,status,submitted_at,charge_cap) '
                        "VALUES ('apify','harvest',?,?,?,'pending',?,?)",(identity,slug,coredb.encode({'actor':actor,'input':payload}),coredb.ledger.now(),cap)).lastrowid
    return dict(con.execute('SELECT * FROM provider_requests WHERE id=?',(rid,)).fetchone()),True


def run_actor(actor: str, payload: dict, token: str, max_usd: float | None = None) -> tuple[list, dict]:
    """Start an actor run, poll to completion, return (dataset items, final run record). Exits on failure."""
    # Persist the request before POST, remote IDs before polling, and full data before parsing it.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import coredb
    con = coredb.connect()
    slug = os.getenv('CORE_RUN_SLUG') or 'standalone'
    try:
        request, start = claim_harvest(con, coredb, actor, payload, slug, max_usd)
    except Exception:
        con.close()
        raise
    old = request if not start else None
    if old and old['status'] == 'complete':
        record = con.execute("SELECT data FROM raw_records WHERE request_id=? AND record_key='dataset' ORDER BY id DESC LIMIT 1", (old['id'],)).fetchone()
        if record:
            con.close()
            return json.loads(record[0]), {'id': old['remote_run_id'], 'defaultDatasetId': old['dataset_id'], 'usageTotalUsd': 0}
    if old and old['status'] in ('pending', 'unknown') and not old['remote_run_id']:
        con.close()
        raise RuntimeError('Harvest submission outcome is unknown. Check Apify before starting another paid run.')
    rid = request['id']
    try:
        if old and old['remote_run_id']:
            run = get_run(old['remote_run_id'], token)
        else:
            run = post_run(actor, payload, token, max_usd)
        run_id, ds_id = run['id'], run['defaultDatasetId']
        coredb.finish_request(con, rid, 'running', run_id, ds_id)
        print(f'  run_id={run_id} dataset={ds_id}')
        deadline = time.monotonic() + 1800
        while run['status'] not in ('SUCCEEDED', 'FAILED', 'ABORTED', 'TIMED-OUT'):
            if time.monotonic() > deadline:
                raise TimeoutError()
            time.sleep(8)
            run = get_run(run_id, token)
            print(f"  status={run['status']}")
        items = get_items(ds_id, token)
        coredb.raw(con, rid, 'dataset', items)
        for i, item in enumerate(items):
            coredb.raw(con, rid, i, item)
        coredb.finish_request(con, rid, 'complete' if run['status'] == 'SUCCEEDED' else 'error', run_id, ds_id)
        if run['status'] != 'SUCCEEDED':
            raise RuntimeError('Harvest did not succeed. Partial data is saved; reconcile the provider run before retrying.')
        return items, run
    except Exception:
        row = con.execute('SELECT status FROM provider_requests WHERE id=?', (rid,)).fetchone()
        if row and row[0] != 'error':
            coredb.finish_request(con, rid, 'unknown')
        raise RuntimeError('Harvest failed or outcome is unknown; stored request/run IDs must be checked before retrying.') from None
    finally:
        con.close()


def tag_post_items(items: list, kind: str) -> list:
    """Post-level actors omit `type`; build_engagers keys on it."""
    return [{**it, "type": kind} for it in items]


def main() -> None:
    ap = argparse.ArgumentParser(description="Harvest LinkedIn post engagers (one profile's recent posts, or one post)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--profile", help="LinkedIn profile URL: harvest its recent posts")
    src.add_argument("--post", help="LinkedIn post URL (lnkd.in links accepted): harvest only this post")
    ap.add_argument("--slug", required=True, help="Slug for output directory")
    ap.add_argument("--retry-unknown", action="store_true", help="Only after checking Apify; may incur another charge")
    ap.add_argument("--max-usd", type=float, default=1.0, help="Total hard cap for collection actor runs")
    ap.add_argument("--max-posts", type=int, default=0, help="Max posts to fetch (0 = all; profile mode only)")
    ap.add_argument("--max-reactions", type=int, default=100)
    ap.add_argument("--max-comments", type=int, default=50)
    ap.add_argument("--posted-limit", default="month", help="24h | week | month")
    ap.add_argument("--output-dir", default="./output", help="Base output directory")
    args = ap.parse_args()

    if args.max_usd <= 0:
        ap.error("--max-usd must be positive")
    token = load_token()
    os.environ["CORE_RUN_SLUG"] = args.slug
    os.environ["CORE_HARVEST_BUDGET"] = str(args.max_usd)
    os.environ["CORE_RETRY_UNKNOWN"] = "1" if args.retry_unknown else "0"
    out_dir = Path(args.output_dir) / args.slug
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.post:
        post = resolve_post(args.post)
        if not post:
            sys.exit("--post must be a LinkedIn post URL (linkedin.com/posts/... or /feed/update/...) or an lnkd.in link to one")
        items, cost = [], 0.0
        for kind, actor in POST_ACTORS.items():
            n = args.max_reactions if kind == "reaction" else args.max_comments
            if n <= 0:
                continue
            # main mode: actor becomes a full profile (vanity URL, location, current company), +$0.002/engager
            payload = {"posts": [post], "maxItems": n, "profileScraperMode": "main"}
            cap = min(round(n * (POST_ITEM_USD + POST_PROFILE_USD) * 1.25 + 0.05, 2),
                      args.max_usd * n / max(1, args.max_reactions + args.max_comments))
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
            "scrapeReactions": args.max_reactions > 0,
            "maxReactions": args.max_reactions,
            "scrapeComments": args.max_comments > 0,
            "maxComments": args.max_comments,
            "commentsPostedLimit": args.posted_limit,
            "includeReposts": True,
            "includeQuotePosts": True,
        }
        print(f"Run: {ACTOR} on {args.profile}")
        print(f"  maxPosts={args.max_posts} maxReactions={args.max_reactions} "
              f"maxComments={args.max_comments} postedLimit={args.posted_limit}")
        items, run = run_actor(ACTOR, payload, token, args.max_usd)
        cost = run.get("usageTotalUsd")
        posts = build_posts(items)
        source = args.profile

    engagers = build_engagers(items)
    urls = {p['postId']: p['url'] for p in posts if p.get('url')}
    for e in engagers:
        e['source_profile'] = source
        e['post_urls'] = [urls[pid] for pid in e['posts_touched'] if pid in urls]
        e['comment_post_urls'] = [urls.get(pid, '') for pid in e.get('comment_post_ids', [])]
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
