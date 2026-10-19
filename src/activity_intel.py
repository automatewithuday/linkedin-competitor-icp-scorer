# Vendored unchanged from https://github.com/automatewithuday/LinkedIn-Profile-Activity-Intel (MIT)
# commit 3e3e178, plus ONE local patch: ensure_ascii=False in the three username(json.dumps(query)) calls, so
# vanity slugs with non-ASCII characters (emoji) attribute correctly. Upstream that fix before re-vendoring.
"""LinkedIn profile activity check: HarvestAPI (Apify) evidence -> code-computed facts -> TypeSafe judgments.

uv run --env-file .env activity_intel.py leads.csv [--dry-run] [--limit N] [--from-raw out/raw/<run>]
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote

ACTORS = {
    "posts": "harvestapi/linkedin-profile-posts",
    "comments": "harvestapi/linkedin-profile-comments",
    "reactions": "harvestapi/linkedin-profile-reactions",
}
# Fixed on purpose: output field names (active_30d, ..._count_30d) and the TypeSafe question text in
# config.toml all say 30/90 days. Making these configurable would let code and model silently disagree.
WINDOWS = {"recent": 7, "active": 30, "stale": 90}
DAY_MS = 86_400_000
FIELDS = [
    "success", "status_code", "linkedin_url", "username",
    "linkedin_outreach_ready", "outreach_ready_probability", "outreach_reason",
    "activity_status", "activity_level", "activity_confidence",
    "days_since_last_activity", "last_activity_type", "most_recent_activity_url",
    "data_quality_warning", "samples_at_cap", "rejected_evidence_count",
    "active_7d", "active_30d", "active_90d",
    "recent_activity_evidence_at", "most_recent_exact_activity_at",
    "most_recent_observed_authored_post_at", "most_recent_observed_authored_comment_at",
    "post_evidence_count", "repost_evidence_count", "comment_evidence_count", "reaction_evidence_count",
    "recent_post_evidence_count_30d", "recent_repost_evidence_count_30d", "recent_comment_evidence_count_30d",
    "recent_reaction_evidence_count_7d", "recent_reaction_evidence_count_30d",
    "recent_reaction_evidence_count_90d",
    "most_recent_post_url", "most_recent_comment_url",
    "typesafe_status_confidence", "status_matches_date_rule",
    "error", "analyzed_at",
]
_IN = re.compile(r"linkedin\.com/in/([^/?#\s\"']+)", re.I)


def username(url):
    m = _IN.search(url or "")
    return unquote(m.group(1)).lower() if m else None


def load_urls(path):
    """CSV with a linkedin_url column, or any text file with one URL per line. Deduped, order kept."""
    text = Path(path).read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(text.splitlines()))
    if rows and "linkedin_url" in rows[0]:
        lines = [r["linkedin_url"] for r in rows]
    else:
        lines = text.splitlines()
    users = dict.fromkeys(u for u in map(username, lines) if u)
    return [f"https://www.linkedin.com/in/{u}" for u in users]


# ---------- evidence (pure) ----------

def attribute(kind, items, usernames):
    """Group one actor's items by the queried profile. Returns ({username: [items]}, unattributed_count)."""
    out = {u: [] for u in usernames}
    missed = 0
    for it in items:
        if it.get("query"):  # the actor's echo of its input names the owner; never hand their item to someone else
            cands = [username(json.dumps(it["query"], ensure_ascii=False))]
        else:  # fallbacks for items without a query echo
            cands = [
                username((it.get("actor") or {}).get("linkedinUrl")) if kind != "posts" else None,
                ((it.get("repostedBy") or {}).get("publicIdentifier") or "").lower() or None,
                ((it.get("author") or {}).get("publicIdentifier") or "").lower() or None if kind == "posts" else None,
            ]
        owner = next((c for c in cands if c in out), None)
        if owner:
            out[owner].append(it)
        else:
            missed += 1
    return out, missed


def _ts(kind, it, now_ms=math.inf):
    """Evidence timestamp in ms, or None when missing, malformed or in the future (>1h: clock-skew tolerance).
    For reactions this is the post's date: a lower bound on when the reaction happened.
    (Reaction items do carry createdAt, but live data shows it is a millisecond-exact copy of post.postedAt.)"""
    try:
        if kind == "comments":
            t = it.get("createdAtTimestamp")
            if t is None and it.get("createdAt"):
                t = datetime.fromisoformat(str(it["createdAt"]).replace("Z", "+00:00")).timestamp() * 1000
        else:
            src = it.get("post") or {} if kind == "reactions" else it
            t = (src.get("postedAt") or {}).get("timestamp")
        t = float(t)
    except (TypeError, ValueError):
        return None
    return t if t <= now_ms + 3_600_000 else None


def _url(kind, it):
    return (it.get("post") or {}).get("linkedinUrl") if kind == "reactions" else it.get("linkedinUrl")


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds") if ms else None


def build_evidence(posts, comments, reactions, now, windows=WINDOWS, caps=None):
    """-> (facts dict with LinkedPulse-style fields, evidence list for TypeSafe). caps = {kind: max items fetched}."""
    now_ms = now.timestamp() * 1000
    fetched = {"posts": len(posts), "comments": len(comments), "reactions": len(reactions)}
    # a repost is the person's own action (live data: postedAt == repostedAt) but not an authored post
    posts, reposts = [p for p in posts if not p.get("repostedBy")], [p for p in posts if p.get("repostedBy")]
    dated, rejected = {}, 0
    for k, items in (("posts", posts), ("reposts", reposts), ("comments", comments), ("reactions", reactions)):
        stamped = [(_ts(k, it, now_ms), it) for it in items]
        rejected += sum(t is None for t, _ in stamped)
        dated[k] = sorted((p for p in stamped if p[0] is not None), key=lambda p: -p[0])
    age = lambda ms: max(0.0, (now_ms - ms) / DAY_MS)  # unrounded for TypeSafe; display rounding happens below
    shown = lambda ms, up: (math.ceil(age(ms) * 10) if up else round(age(ms) * 10)) / 10  # bounds round up, never down
    within = lambda ms, days: ms is not None and now_ms - ms <= days * DAY_MS  # exact, no day rounding
    latest = {k: (v[0][0] if v else None) for k, v in dated.items()}
    newest = max((t for t in latest.values() if t), default=None)
    newest_kind = next((k for k in dated if newest and latest[k] == newest), None)
    exact = max((latest[k] for k in ("posts", "reposts", "comments") if latest[k]), default=None)
    r7, r30, r90 = windows["recent"], windows["active"], windows["stale"]

    facts = {
        "days_since_last_activity": shown(newest, newest_kind == "reactions") if newest else None,  # reaction: upper bound
        "last_activity_type": newest_kind[:-1] if newest_kind else None,
        "most_recent_activity_url": _url(newest_kind, dated[newest_kind][0][1]) if newest_kind else None,
        "samples_at_cap": ",".join(k for k in ACTORS if caps and fetched[k] >= caps[k]),
        "rejected_evidence_count": rejected,
        "recent_post_evidence_count_30d": sum(within(t, r30) for t, _ in dated["posts"]),
        "recent_repost_evidence_count_30d": sum(within(t, r30) for t, _ in dated["reposts"]),
        "recent_comment_evidence_count_30d": sum(within(t, r30) for t, _ in dated["comments"]),
        "active_7d": within(newest, r7),
        "active_30d": within(newest, r30),
        "active_90d": within(newest, r90),
        "recent_activity_evidence_at": _iso(newest),
        "most_recent_exact_activity_at": _iso(exact),  # newest post/repost/comment: exactly dated, unlike reactions
        "most_recent_observed_authored_post_at": _iso(latest["posts"]),
        "most_recent_observed_authored_comment_at": _iso(latest["comments"]),
        "post_evidence_count": len(dated["posts"]),
        "repost_evidence_count": len(dated["reposts"]),
        "comment_evidence_count": len(dated["comments"]),
        "reaction_evidence_count": len(dated["reactions"]),
        "most_recent_post_url": dated["posts"][0][1].get("linkedinUrl") if dated["posts"] else None,
        "most_recent_comment_url": dated["comments"][0][1].get("linkedinUrl") if dated["comments"] else None,
    }
    for d, name in ((r7, "7d"), (r30, "30d"), (r90, "90d")):
        facts[f"recent_reaction_evidence_count_{name}"] = sum(within(t, d) for t, _ in dated["reactions"])
    facts["date_rule_status"] = ("no_activity_observed" if newest is None else "active" if facts["active_30d"]
                                 else "stale" if facts["active_90d"] else "inactive")

    evidence = [{"type": "post", "age_days": age(t)} for t, _ in dated["posts"]]
    evidence += [{"type": "repost", "age_days": age(t)} for t, _ in dated["reposts"]]
    evidence += [{"type": "comment", "age_days": age(t)} for t, _ in dated["comments"]]
    evidence += [{"type": "reaction", "age_days_at_most": age(t)} for t, _ in dated["reactions"]]
    return facts, evidence


# ---------- fetch ----------

def estimate_cost(n, cfg):
    f, p = cfg["fetch"], cfg["prices_usd_per_1000"]
    per = {"posts": f["max_posts"] * p["posts"], "comments": f["max_comments"] * p["comments"],
           "reactions": f["max_reactions"] * p["reactions"]}
    return {k: n * v / 1000 for k, v in per.items()}


def run_cap(n, kind, cfg):
    """Hard max_total_charge_usd for one actor run over n profiles: estimate + 25% + $0.05 headroom."""
    return Decimal(str(round(estimate_cost(n, cfg)[kind] * 1.25 + 0.05, 2)))


def chunks(urls, cfg):
    cs = cfg["fetch"]["chunk_size"]
    return [urls[i:i + cs] for i in range(0, len(urls), cs)]


def fetch(client, urls, cfg, raw_dir, now):
    """Run the 3 actors per chunk of profiles. Returns ({kind: [items]}, runs).
    runs = [{kind, profiles, run_id, status, charged_events, usage_usd, charges_final, error, ...}] — one per actor run;
    error set = those profiles lack that source. Charge fields are observed at run end and lag; charges_final is always False."""
    f = cfg["fetch"]
    items, runs = {k: [] for k in ACTORS}, []

    def run(kind, chunk, retry=True):
        inp = ({"targetUrls": chunk, "maxPosts": f["max_posts"], "includeReposts": f["include_reposts"],
                "includeQuotePosts": f["include_quote_posts"]} if kind == "posts"
               else {"profiles": chunk, "maxItems": f[f"max_{kind}"]})
        r = client.actor(ACTORS[kind]).call(run_input=inp, max_total_charge_usd=run_cap(len(chunk), kind, cfg), logger=None)
        if r.status != "SUCCEEDED":  # call() returns FAILED / TIMED-OUT / ABORTED runs too, with empty or partial datasets
            return [{"kind": kind, "profiles": chunk, "run_id": r.id, "status": r.status,
                     "error": f"run {r.id} {r.status}: {r.status_message}"}], []
        items = client.dataset(r.default_dataset_id).list_items().items  # the scrape itself; everything below is best-effort
        entry = {"kind": kind, "profiles": chunk, "run_id": r.id, "status": r.status, "error": None,
                 "charges_final": False, "charges_observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:  # charge counters lag the run end (often by minutes): recorded as observed, never final; Console has the truth
            final = client.run(r.id).get()
            entry |= {"charged_events": final.charged_event_counts, "usage_usd": final.usage_total_usd}
        except Exception as e:
            entry |= {"charged_events": None, "usage_usd": None, "charges_error": f"{type(e).__name__}: {e}"}
        # A SUCCEEDED run can still silently skip profiles (live: upstream "Too many queued requests (code_22)" gave 0 posts
        # for 3-4 of 10 profiles). The only trace is the actor log, so scan it per profile.
        # ponytail: HarvestAPI log-format heuristic; replace if the actor ever reports per-profile errors in the dataset.
        try:
            log = client.run(r.id).log().get() or ""
        except Exception as e:  # without the log, a profile with zero items cannot be told apart from a skipped one
            entry["log_error"] = f"{type(e).__name__}: {e}"
            got = {username(json.dumps(it.get("query"), ensure_ascii=False)) for it in items}
            empty = [p for p in chunk if username(p) not in got]
            unverified = [{"kind": kind, "profiles": empty, "run_id": r.id, "status": r.status,
                           "error": f"collection unverified: no {kind} returned and the run log was unavailable ({entry['log_error']})"}]
            return [entry] + (unverified if empty else []), items
        skipped = {u: msg for target, msg in re.findall(r'Error scraping item#\d+ (\{.*?\}): "?(.*?)"?$', log, re.M)
                   if (u := username(target))}
        if not skipped:
            return [entry], items
        urls_ = [p for p in chunk if username(p) in skipped]
        items = [it for it in items if username(json.dumps(it.get("query"), ensure_ascii=False)) not in skipped]  # keep only complete profiles
        why = f"actor errors for {len(urls_)} profiles: {'; '.join(sorted(set(skipped.values())))}"
        if retry:  # skipped profiles returned nothing, so were not charged: one retry costs what the first try should have
            print(f"[fetch] {kind}: actor skipped {len(urls_)} profiles, retrying once", file=sys.stderr)
            try:
                more_entries, more_items = run(kind, urls_, retry=False)
                return [entry] + more_entries, items + more_items
            except Exception as e:  # a failed retry must not undo the first run's results
                why = f"{why}; retry failed: {type(e).__name__}: {e}"
        return [entry, {"kind": kind, "profiles": urls_, "run_id": r.id, "status": r.status, "error": why}], items

    with ThreadPoolExecutor(3) as ex:
        for chunk in chunks(urls, cfg):
            futs = {k: ex.submit(run, k, chunk) for k in ACTORS}
            for k, fut in futs.items():
                try:
                    entries, got = fut.result()
                except Exception as e:  # one actor failing must not lose the other two
                    entries, got = [{"kind": k, "profiles": chunk, "error": f"{type(e).__name__}: {e}"}], []
                runs += entries
                items[k] += got
                for e in entries:
                    if e["error"]:
                        print(f"[fetch] {k} failed for {len(e['profiles'])} profiles: {e['error']}", file=sys.stderr)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for k, v in items.items():
        (raw_dir / f"{k}.json").write_text(json.dumps(v, default=str))
    manifest = {"profiles": urls, "collected_at": now.isoformat(timespec="seconds"), "config": cfg, "runs": runs}
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str))
    return items, runs


def load_raw(raw, urls):
    """Cached actor output for --from-raw. Profiles missing from the cache's manifest come back as failed runs."""
    items = {k: json.loads((raw / f"{k}.json").read_text()) for k in ACTORS}
    if (raw / "manifest.json").exists():
        m = json.loads((raw / "manifest.json").read_text())
        collected = set(map(username, m["profiles"]))
        missing = [u for u in urls if username(u) not in collected]
        runs = m["runs"] + [{"kind": k, "profiles": missing, "error": "not collected in this raw cache"}
                            for k in ACTORS if missing]
    else:  # cache from before manifests existed: coverage cannot be checked
        print("[from-raw] no manifest.json: cannot verify which profiles this cache covers", file=sys.stderr)
        old = json.loads((raw / "errors.json").read_text()) if (raw / "errors.json").exists() else {}
        runs = [{"kind": k, "profiles": urls, "error": e} for k, e in old.items()]
    return items, runs


# ---------- judge (TypeSafe) ----------

def build_questions(cfg):
    from typesafe_sdk import Choice, Noul, Score
    qs = {}
    for qid, q in cfg["questions"].items():
        if q["type"] == "choice":
            qs[qid] = Choice(instructions=q["instructions"], criteria=q["criteria"])
        elif q["type"] == "score":
            qs[qid] = Score(instructions=q["instructions"], criteria=q["criteria"])
        else:
            qs[qid] = Noul(instructions=q["instructions"])
    return qs


def judge(ts, questions, facts, evidence, cfg, sources_unavailable=()):
    # counts only: the date-rule status and active_* flags would hand Jev the answer
    summary = {k: v for k, v in facts.items() if "_count" in k and k != "rejected_evidence_count"}
    if sources_unavailable:
        summary["sources_unavailable"] = list(sources_unavailable)
    r = ts.system_one(state={"summary": summary, "evidence": evidence}, questions=questions,
                      model=cfg["judge"]["model"])
    def label(qid):  # score is a float position on the levels; nearest level's label
        labels = cfg["questions"][qid]["labels"]
        return labels[min(len(labels) - 1, max(0, round(r.scores[qid].score)))]

    p = r.nouls["linkedin_outreach_ready"].noul
    status = r.choices["activity_status"]
    return {
        "activity_status": status.choice,
        "typesafe_status_confidence": round(status.confidence, 3),
        "activity_level": label("activity_level"),
        "activity_confidence": label("activity_confidence"),
        "outreach_ready_probability": round(p, 3),
        "linkedin_outreach_ready": p >= cfg["judge"]["outreach_ready_threshold"],
        "status_matches_date_rule": status.choice == facts["date_rule_status"],
    }


# ---------- assemble ----------

def explain(row, now):
    """Plain-English reason for the outreach verdict, assembled from facts + Jev answers (TypeSafe cannot write text)."""
    n, kind = row["days_since_last_activity"], row["last_activity_type"]
    if n is None:
        return "No public posts, comments or reactions were observed, so there is no sign this person uses LinkedIn."
    when = "today" if n < 1 else f"{n:.0f}d ago"
    last = (f"a reaction on a post from {when} (so the reaction is at most {math.ceil(n) or 1}d old)" if kind == "reaction"
            else f"a {kind} {when}")
    posts, reposts, comments, reacts = (row["recent_post_evidence_count_30d"], row["recent_repost_evidence_count_30d"],
                                        row["recent_comment_evidence_count_30d"], row["recent_reaction_evidence_count_30d"])
    parts = [f"{'Worth' if row['linkedin_outreach_ready'] else 'Not worth'} reaching out on LinkedIn now "
             f"(probability {row['outreach_ready_probability']:.2f}).",
             f"Status {row['activity_status']}: most recent evidence is {last}.",
             f"Last 30 days in sample: {posts} own posts, {reposts} reposts, {comments} comments, {reacts} reactions."]
    if posts:
        parts.append("Publishes their own posts.")
    elif reposts or comments or reacts:
        own = row["most_recent_observed_authored_post_at"]
        ago = f"last own post {(now - datetime.fromisoformat(own)).days}d ago" if own else "no own posts observed"
        parts.append(f"Engages with others' content rather than posting ({ago}).")
    parts.append(f"Activity level {row['activity_level']}; evidence confidence {row['activity_confidence']}"
                 + (" (reaction dates are lower bounds)." if kind == "reaction" else "."))
    return " ".join(parts)


def analyze(url, grouped, runs, now, cfg, ts, questions):
    u = username(url)
    row = dict.fromkeys(FIELDS) | {"linkedin_url": url, "username": u, "analyzed_at": now.isoformat(timespec="seconds"),
                                   "success": True, "status_code": 200, "data_quality_warning": False}
    try:
        return _analyze(row, u, grouped, runs, now, cfg, ts, questions)
    except Exception as e:  # one bad profile must never abort the batch
        return row | {"success": False, "status_code": 500, "data_quality_warning": True,
                      "error": f"{type(e).__name__}: {e}"}


def _analyze(row, u, grouped, runs, now, cfg, ts, questions):
    failed = {r["kind"]: r["error"] for r in runs if r.get("error") and u in map(username, r["profiles"])}
    caps = {k: cfg["fetch"][f"max_{k}"] for k in ACTORS}
    facts, evidence = build_evidence(*(grouped[k].get(u, []) for k in ACTORS), now, caps=caps)
    row |= {k: v for k, v in facts.items() if k in FIELDS}
    row["data_quality_warning"] = bool(failed) or facts["rejected_evidence_count"] > 0
    if failed:
        row |= {"success": len(failed) < len(ACTORS), "status_code": 502,
                "error": "; ".join(f"{k}: {v}" for k, v in failed.items())}
    if not evidence and failed:  # a failed fetch is "unknown", not "no activity"
        return row
    if not evidence:  # nothing to judge
        row |= {"activity_status": "no_activity_observed", "activity_level": "none",
                "activity_confidence": "low", "linkedin_outreach_ready": False}
        return row | {"outreach_reason": explain(row, now)}
    try:
        row |= judge(ts, questions, facts, evidence, cfg, sources_unavailable=sorted(failed))
    except Exception as e:
        return row | {"success": False, "status_code": 502, "data_quality_warning": True,
                      "error": "; ".join(filter(None, [row["error"], f"typesafe: {type(e).__name__}: {e}"]))}
    if facts["most_recent_exact_activity_at"] is None and row["activity_confidence"] == "high":
        row["activity_confidence"] = "medium"  # reaction-only evidence has no exact dates: documented ceiling
    return row | {"outreach_reason": explain(row, now)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="CSV with linkedin_url column, or TXT with one URL per line")
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--out", default="out")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true", help="print profile count + Apify cost estimate and hard cap, then exit")
    ap.add_argument("--from-raw", help="re-judge cached actor output from out/raw/<run> without calling Apify")
    a = ap.parse_args()

    cfg = tomllib.loads(Path(a.config).read_text())
    urls = load_urls(a.input)[:a.limit]
    if not urls:
        sys.exit("no LinkedIn /in/ URLs found in input")
    if not a.from_raw:
        est = estimate_cost(len(urls), cfg)
        cap = sum(run_cap(len(c), k, cfg) for c in chunks(urls, cfg) for k in ACTORS)
        print(f"{len(urls)} profiles | est. Apify ${sum(est.values()):.2f} "
              f"({', '.join(f'{k} ${v:.2f}' for k, v in est.items())}) | hard cap ${cap:.2f} (sum of per-run spend caps; "
              f"a one-off retry of profiles the actor skipped can add at most the same again)",
              file=sys.stderr)
    if a.dry_run:
        return
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY not set (put it in .env and run with: uv run --env-file .env ...)")

    now = datetime.now(timezone.utc)
    run_id = now.strftime("%Y%m%d-%H%M%S")
    out = Path(a.out)
    if a.from_raw:
        items, runs = load_raw(Path(a.from_raw), urls)
    else:
        token = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
        if not token:
            sys.exit("APIFY_TOKEN not set (put it in .env and run with: uv run --env-file .env ...)")
        from apify_client import ApifyClient
        items, runs = fetch(ApifyClient(token), urls, cfg, out / "raw" / run_id, now)

    users = [username(u) for u in urls]
    grouped = {}
    for k in ACTORS:
        grouped[k], missed = attribute(k, items[k], users)
        if missed:
            print(f"[attribute] {missed}/{len(items[k])} {k} items matched no input profile", file=sys.stderr)

    from typesafe_sdk import TypeSafeClient
    with TypeSafeClient() as ts, ThreadPoolExecutor(cfg["judge"]["workers"]) as ex:
        questions = build_questions(cfg)
        rows = list(ex.map(lambda url: analyze(url, grouped, runs, now, cfg, ts, questions), urls))

    out.mkdir(parents=True, exist_ok=True)
    (out / f"{run_id}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    with open(out / f"{run_id}.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by[r["activity_status"]] = by.get(r["activity_status"], 0) + 1
    flagged = sum(r["data_quality_warning"] for r in rows)
    print(f"wrote {out / run_id}.jsonl + .csv | {by} | {flagged} rows flagged for review", file=sys.stderr)


if __name__ == "__main__":
    main()
