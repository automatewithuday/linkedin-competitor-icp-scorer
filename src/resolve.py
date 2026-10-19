"""Resolve qualified opaque /in/ACoAA... engagers to public vanity URLs via harvestapi/linkedin-profile-scraper.

Profile-mode harvests (and short-mode reactions) only carry LinkedIn's opaque profile id, which email enrichment,
the activity check and Smartlead cannot use. One actor run over the qualified ids, $0.004 per profile.
Adds `resolve_status`: resolved | not_found | skipped | error. Never fails the pipeline on a provider error.

--slug mode (before final scoring): resolves opaque engagers whose first-pass fit is >= --min-fit (run.py uses
min_fit - 1) and writes the public URL, location and company into engagers.json, so the final scoring can apply
geo and employer rules. Engagers that changed are listed in rescore.json for a second scoring pass.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
try:
    from .common import fit, linkedin_url, location_text, read_csv, write_csv, write_json
    from .harvest import profile_fields
except ImportError:
    from common import fit, linkedin_url, location_text, read_csv, write_csv, write_json
    from harvest import profile_fields

ACTOR = 'harvestapi/linkedin-profile-scraper'
MODE = 'Profile details no email ($4 per 1k)'
PROFILE_USD = 0.004


def opaque_id(row):
    """ACoAA id of a row without a public URL, else ''."""
    if linkedin_url(row.get('linkedin_url')):
        return ''
    for v in (row.get('actor_id') or '', (row.get('linkedin_url') or '').rstrip('/').rsplit('/', 1)[-1]):
        if v.startswith('ACoAA'):
            return v
    return ''


def cap(n):
    return round(n * PROFILE_USD * 1.25 + 0.05, 2)


def parse(items, status):
    """-> ({id: profile item}, complete). A SUCCEEDED run can still hold only {"error": ...} items
    (e.g. "Free users are limited to 10 items per run"); then missing ids are errors, not not_found."""
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


def scrape(ids, token, batch_size=0):
    """Actor run(s) over ids: one run, or one per batch of batch_size (HarvestAPI allows 10 items per run on the
    Apify free plan; batching is the user's call, so it is off unless --batch-size is given). Raises on API failure."""
    from apify_client import ApifyClient
    client = ApifyClient(token)
    found, ok = {}, True
    chunks = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)] if batch_size else [ids]
    for n, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            print(f'Resolve batch {n}/{len(chunks)}: {len(chunk)} ids', file=sys.stderr)
        run = client.actor(ACTOR).call(run_input={'profileIds': chunk, 'profileScraperMode': MODE},
                                       max_total_charge_usd=cap(len(chunk)), logger=None)
        got, good = parse(client.dataset(run.default_dataset_id).list_items().items, run.status)
        found |= got
        ok = ok and good
    return found, ok


def enrich_harvest(base, min_fit, batch_size=0, dry_run=False):
    """Pre-scoring pass over output/<slug>/: resolve opaque engagers with first-pass fit >= min_fit (fits read from
    ranked_engagers.csv by actor_id) and write url/location/company into engagers.json in place. Engager order is
    untouched, so scores.json indices stay valid. Returns the indices that changed (also written to rescore.json)."""
    base = Path(base)
    engagers = json.loads((base / 'engagers.json').read_text(encoding='utf-8'))
    fits = {r.get('actor_id'): fit(r) for r in read_csv(base / 'ranked_engagers.csv')[0]}
    changed = []
    if (base / 'resolved.csv').exists():  # profiles a post-rank resolve already paid for
        paid = {r['actor_id']: r for r in read_csv(base / 'resolved.csv')[0] if r.get('resolve_status') == 'resolved'}
        for i, e in enumerate(engagers):
            hit = paid.get(e.get('id'))
            if hit and not linkedin_url(e.get('linkedinUrl')) and linkedin_url(hit.get('linkedin_url')):
                e['linkedinUrl'], e['resolve_status'] = linkedin_url(hit['linkedin_url']), 'resolved'
                e['location'] = e.get('location') or hit.get('location', '')
                changed.append(i)
        if changed:
            print(f'Reused {len(changed)} profiles already resolved in resolved.csv', file=sys.stderr)
    todo = [i for i, e in enumerate(engagers)
            if fits.get(e.get('id'), 0) >= min_fit and not linkedin_url(e.get('linkedinUrl'))
            and (e.get('id') or '').startswith('ACoAA') and e.get('resolve_status') != 'not_found']
    ids = list(dict.fromkeys(engagers[i]['id'] for i in todo))
    print(f'Pre-scoring candidates (first-pass fit>={min_fit}, opaque): {len(ids)}; estimate ${len(ids) * PROFILE_USD:.3f}; '
          f'hard cap ${cap(len(ids)):.2f}' if ids else f'Pre-scoring candidates (first-pass fit>={min_fit}): none to resolve',
          file=sys.stderr)
    found, ok = {}, False
    if ids and not dry_run:
        token = os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY')
        try:
            if not token:
                raise RuntimeError('APIFY_TOKEN is not set')
            found, ok = scrape(ids, token, batch_size)
        except Exception as exc:  # the post-rank resolve retries qualified rows; scoring carries on without location
            print(f'Pre-scoring resolve failed ({type(exc).__name__}); continuing without it', file=sys.stderr)
    if dry_run or not (changed or found):
        return []
    for i in todo:
        e, hit = engagers[i], found.get(engagers[i]['id'])
        url, location, company = profile_fields(hit) if hit else ('', '', '')
        if linkedin_url(url):
            e['linkedinUrl'] = linkedin_url(url)
            e['location'] = e.get('location') or location
            e['company'] = e.get('company') or company
            e['resolve_status'] = 'resolved'
            changed.append(i)
        elif ok:  # complete run, id absent: do not pay for it again on the next run
            e['resolve_status'] = 'not_found'
    write_json(base / 'engagers.json', engagers)
    if changed:
        write_json(base / 'rescore.json', {
            'indices': changed, 'names': [engagers[i].get('name') for i in changed],
            'why': 'location/company arrived after the first scoring pass; rescore these indices'})
    print(f'Pre-scoring resolve: sent {len(ids)}; gained profile data: {len(changed)}', file=sys.stderr)
    return changed


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', help='ranked CSV (post-rank mode)')
    ap.add_argument('--output', help='resolved CSV to write (post-rank mode)')
    ap.add_argument('--slug', help='pre-scoring mode: enrich output/<slug>/engagers.json instead of a CSV')
    ap.add_argument('--output-dir', default='./output')
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    ap.add_argument('--dry-run', action='store_true', help='print id count, estimate and hard cap; spend nothing')
    ap.add_argument('--batch-size', type=int, default=0,
                    help='ids per actor run (0 = one run). The Apify free plan allows 10 per run on this actor')
    args = ap.parse_args()
    if args.slug:
        enrich_harvest(Path(args.output_dir) / args.slug, args.min_fit, args.batch_size, args.dry_run)
        return
    if not (args.input and args.output):
        ap.error('pass --slug (pre-scoring) or both --input and --output (post-rank)')
    rows, fields = read_csv(args.input)
    prior = {}  # actor_id -> earlier resolved row; a rerun after rescoring must not pay twice
    if os.path.exists(args.output):
        prior = {r['actor_id']: r for r in read_csv(args.output)[0]
                 if r.get('resolve_status') == 'resolved' and r.get('actor_id')}
    for row in rows:
        hit = prior.get(row.get('actor_id'))
        if hit and opaque_id(row):
            row['linkedin_url'], row['resolve_status'] = hit['linkedin_url'], 'resolved'
            row['location'] = row.get('location') or hit.get('location', '')
    if any(r.get('resolve_status') == 'resolved' for r in rows if prior.get(r.get('actor_id'))):
        print(f'Reusing {sum(r.get("resolve_status") == "resolved" for r in rows)} earlier resolved rows from {args.output}',
              file=sys.stderr)
    ids = list(dict.fromkeys(i for i in (opaque_id(r) for r in rows if fit(r) >= args.min_fit) if i))
    print(f'Rows: {len(rows)}; opaque qualified ids (fit>={args.min_fit}): {len(ids)}; '
          f'estimate ${len(ids) * PROFILE_USD:.3f}; hard cap ${cap(len(ids)):.2f}' if ids else
          f'Rows: {len(rows)}; nothing to resolve (fit>={args.min_fit})', file=sys.stderr)
    if args.dry_run:
        return

    found, ok, failed = {}, True, False
    if ids:
        token = os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY')
        try:
            if not token:
                raise RuntimeError('APIFY_TOKEN is not set')
            found, ok = scrape(ids, token, args.batch_size)
        except Exception as exc:  # keep the other rows moving; the error column says what happened
            print(f'Resolve run failed ({type(exc).__name__}); marking {len(ids)} rows error', file=sys.stderr)
            failed = True
    sent = set(ids)
    for row in rows:
        rid = opaque_id(row) if fit(row) >= args.min_fit else ''
        hit = found.get(rid)
        url = linkedin_url((hit or {}).get('linkedinUrl')) or \
            linkedin_url('linkedin.com/in/' + ((hit or {}).get('publicIdentifier') or ''))
        if rid not in sent:
            if row.get('resolve_status') != 'resolved':  # keep the earlier outcome on a rerun
                row['resolve_status'] = 'skipped'
        elif url:
            row['linkedin_url'] = url
            row['location'] = row.get('location') or location_text(hit.get('location'))
            row['resolve_status'] = 'resolved'
        else:
            # ponytail: a non-SUCCEEDED run (e.g. cap hit) can't distinguish "absent" from "not reached"
            row['resolve_status'] = 'error' if failed or not ok else 'not_found'
    write_csv(args.output, rows, fields + [c for c in ('location', 'resolve_status')
                                           if c not in fields and any(r.get(c) for r in rows)])
    n = Counter(r['resolve_status'] for r in rows)
    print(f'Rows: {len(rows)}; sent: {len(ids)}; resolved: {n["resolved"]}; '
          f'not_found: {n["not_found"]}; error: {n["error"]}')


if __name__ == '__main__':
    main()
