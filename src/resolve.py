"""Resolve opaque /in/ACoAA... engagers to public profiles via harvestapi/linkedin-profile-scraper, BEFORE final scoring.

Profile-mode harvests (and short-mode reactions) only carry LinkedIn's opaque profile id, which email enrichment,
the activity check and Smartlead cannot use, and which hides location and employer from the scorer.

For engagers whose first-pass fit is >= --min-fit (run.py uses min_fit - 1), this writes the public URL, location and
company into output/<slug>/engagers.json and lists the changed engagers in rescore.json, so they are scored again
with that evidence. This is the only place profiles are fetched: every new piece of evidence goes back to scoring.

Paid results are saved after every actor run. A completed lookup that finds nothing is cached as `not_found` for
MISS_DAYS (--refresh-misses retries them sooner). Lookups that fail (provider error, free-plan limit, network) are
left untouched and listed; the command then exits 3 so the pipeline stops instead of exporting rows scored without
their profile evidence. Rerunning sends only what is still unresolved. $0.004 per profile.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
try:
    from .common import fit, linkedin_url, read_csv, write_json
    from .harvest import profile_fields, profile_evidence
    from . import ledger
except ImportError:
    from common import fit, linkedin_url, read_csv, write_json
    from harvest import profile_fields, profile_evidence
    import ledger

ACTOR = 'harvestapi/linkedin-profile-scraper'
MODE = 'Profile details no email ($4 per 1k)'
PROFILE_USD = 0.004
MISS_DAYS = 30  # profiles change; a miss is not permanent
FAILED_EXIT = 3


def cap(n):
    return round(n * PROFILE_USD * 1.25 + 0.05, 2)


def parse(items, status):
    """-> ({id: profile item}, complete). A SUCCEEDED run can still hold only {"error": ...} items
    (e.g. "Free users are limited to 10 items per run"); then missing ids are failures, not not_found."""
    found, errors = {}, []
    for it in items:
        key = it.get('id') or (it.get('originalQuery') or {}).get('profileId')
        if key and (it.get('linkedinUrl') or it.get('publicIdentifier')):
            found[key] = it
        elif it.get('error'):
            errors.append(str(it['error']))
    for msg in dict.fromkeys(errors):
        print(f'Provider error item: {msg[:200]}', file=sys.stderr)
    return found, status == 'SUCCEEDED' and not errors


def scrape(ids, token):
    """One actor run over ids -> (found, complete, run_id). Raises on API failure."""
    from apify_client import ApifyClient
    client = ApifyClient(token)
    run = client.actor(ACTOR).call(run_input={'profileIds': ids, 'profileScraperMode': MODE},
                                   max_total_charge_usd=cap(len(ids)), logger=None)
    found, ok = parse(client.dataset(run.default_dataset_id).list_items().items, run.status)
    return found, ok, run.id


def fresh_miss(e, now, refresh):
    if refresh or e.get('resolve_status') != 'not_found':
        return False
    try:
        return now - datetime.fromisoformat(e.get('resolve_checked_at') or '') < timedelta(days=MISS_DAYS)
    except ValueError:
        return False


def enrich_harvest(base, min_fit, batch_size=0, dry_run=False, refresh_misses=False, scores=None):
    """-> (changed engager indices, failed ids). See module doc."""
    base, now = Path(base), datetime.now(timezone.utc)
    engagers = json.loads((base / 'engagers.json').read_text(encoding='utf-8'))
    fits = {r.get('actor_id'): fit(r) for r in read_csv(base / 'ranked_engagers.csv')[0]}
    changed = []
    legacy = base / 'resolved.csv'  # results paid for by the pre-1.1 post-rank resolve
    if legacy.exists():
        paid = {r['actor_id']: r for r in read_csv(legacy)[0] if r.get('resolve_status') == 'resolved'}
        for i, e in enumerate(engagers):
            hit = paid.get(e.get('id'))
            if hit and not linkedin_url(e.get('linkedinUrl')) and linkedin_url(hit.get('linkedin_url')):
                e['linkedinUrl'], e['resolve_status'] = linkedin_url(hit['linkedin_url']), 'resolved'
                e['location'] = e.get('location') or hit.get('location', '')
                changed.append(i)

    con = ledger.connect()  # lookups other runs already paid for: same rules, no new spend
    shared = 0
    for i, e in enumerate(engagers):
        if fits.get(e.get('id'), 0) < min_fit or linkedin_url(e.get('linkedinUrl')) or not (e.get('id') or '').startswith('ACoAA'):
            continue
        hit = ledger.cached_lookup(con, e['id'])
        if hit and hit['status'] == 'resolved' and linkedin_url(hit['public_url']):
            e['linkedinUrl'], e['resolve_status'] = linkedin_url(hit['public_url']), 'resolved'
            e['location'] = e.get('location') or hit.get('location') or ''
            e['company'] = e.get('company') or hit.get('company') or ''
            if (hit.get('profile_evidence') and not e.get('profile_evidence') and
                    (not e.get('company') or e['company'] == hit.get('company'))):
                e['profile_evidence'] = hit['profile_evidence']
            changed.append(i); shared += 1
        elif hit and hit['status'] == 'not_found' and e.get('resolve_status') != 'not_found':
            e['resolve_status'], e['resolve_checked_at'] = 'not_found', hit['checked_at']
    if shared:
        print(f'Reused {shared} profile lookups from earlier runs (ledger)', file=sys.stderr)
    todo = [i for i, e in enumerate(engagers)
            if fits.get(e.get('id'), 0) >= min_fit and not linkedin_url(e.get('linkedinUrl'))
            and (e.get('id') or '').startswith('ACoAA') and not fresh_miss(e, now, refresh_misses)]
    ids = list(dict.fromkeys(engagers[i]['id'] for i in todo))
    print(f'Profile lookups (first-pass fit>={min_fit}, opaque): {len(ids)}; estimate ${len(ids) * PROFILE_USD:.3f}; '
          f'hard cap ${cap(len(ids)):.2f}' if ids else f'Profile lookups (first-pass fit>={min_fit}): none needed',
          file=sys.stderr)
    if dry_run:
        return [], []

    by_id = {}
    for i in todo:
        by_id.setdefault(engagers[i]['id'], []).append(i)
    failed, runs = [], []
    token = os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY')
    chunks = [ids[k:k + batch_size] for k in range(0, len(ids), batch_size)] if batch_size else ([ids] if ids else [])
    for n, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f'Lookup batch {n}/{len(chunks)}: {len(chunk)} ids', file=sys.stderr)
        try:
            if not token:
                raise RuntimeError('APIFY_TOKEN is not set')
            found, ok, run_id = scrape(chunk, token)
        except Exception as exc:  # keep earlier batches; retry only this one on the next run
            print(f'Lookup batch {n} failed ({type(exc).__name__}: {str(exc)[:120]})', file=sys.stderr)
            failed += chunk
            runs.append({'ids': chunk, 'error': f'{type(exc).__name__}', 'at': now.isoformat(timespec='seconds')})
            continue
        runs.append({'run_id': run_id, 'ids': chunk, 'complete': ok, 'at': now.isoformat(timespec='seconds')})
        for pid in chunk:
            url, location, company = profile_fields(found[pid]) if pid in found else ('', '', '')
            for i in by_id[pid]:
                e = engagers[i]
                if linkedin_url(url):
                    e['linkedinUrl'] = linkedin_url(url)
                    e['location'] = e.get('location') or location
                    e['company'] = company or e.get('company') or ''
                    evidence = profile_evidence(found[pid])
                    if evidence:
                        e['profile_evidence'] = evidence
                    e['resolve_status'] = 'resolved'
                    changed.append(i)
                    ledger.record_lookup(con, pid, 'resolved', e['linkedinUrl'], run_id, e)
                elif ok:  # complete run, id absent: a real miss, cached so it is not paid for again
                    e['resolve_status'], e['resolve_checked_at'] = 'not_found', now.isoformat(timespec='seconds')
                    ledger.record_lookup(con, pid, 'not_found', None, run_id)
                elif pid not in failed:
                    failed.append(pid)
        write_json(base / 'engagers.json', engagers)  # persist every paid batch before the next one runs
        con.commit()

    if runs:
        log = base / 'resolve_runs.json'
        write_json(log, (json.loads(log.read_text(encoding='utf-8')) if log.exists() else []) + runs)
    if changed:
        write_json(base / 'engagers.json', engagers)
        rescore = base / 'rescore.json'
        pending = []  # keep indices still waiting from an earlier pass, or a partial failure would drop them
        if rescore.exists():
            pending = json.loads(rescore.read_text(encoding='utf-8')).get('indices', [])
            if (base / 'recipient_icp.json').exists():
                stale = {i for i, _ in ledger.pending_scores(con, base)}
                pending = [i for i in pending if i in stale]
        idx = list(dict.fromkeys(pending + changed))
        write_json(rescore, {'indices': idx, 'names': [engagers[i].get('name') for i in idx],
                             'why': 'location/company arrived after scoring; rescore these indices'})
    print(f'Profile lookups: sent {len(ids) - len(failed)}/{len(ids)}; gained profile data: {len(changed)}; '
          f'failed: {len(failed)}', file=sys.stderr)
    return changed, failed


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--slug', required=True)
    ap.add_argument('--output-dir', default='./output')
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=3,
                    help='first-pass fit at or above which opaque engagers are looked up (run.py: min_fit - 1)')
    ap.add_argument('--scores', help='in-session scores.json; keeps earlier rescore indices that are still pending')
    ap.add_argument('--dry-run', action='store_true', help='print lookup count, estimate and hard cap; spend nothing')
    ap.add_argument('--batch-size', type=int, default=0,
                    help='ids per actor run (0 = one run). The Apify free plan allows 10 per run on this actor')
    ap.add_argument('--refresh-misses', action='store_true', help=f'retry cached not_found lookups younger than {MISS_DAYS} days')
    args = ap.parse_args()
    _, failed = enrich_harvest(Path(args.output_dir) / args.slug, args.min_fit, args.batch_size, args.dry_run,
                               args.refresh_misses, args.scores)
    if failed:
        print(f'{len(failed)} profile lookups failed; successful results are saved. Rerun the same command to retry only '
              f'those (with --scores: run rank.py --dump and judge changed rows first; free-plan users: '
              f'--batch-size 10 / run.py --resolve-batch 10), or continue without them with '
              f'run.py --no-resolve.', file=sys.stderr)
        sys.exit(FAILED_EXIT)


if __name__ == '__main__':
    main()
