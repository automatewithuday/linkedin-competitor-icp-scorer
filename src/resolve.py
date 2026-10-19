"""Resolve qualified opaque /in/ACoAA... engagers to public vanity URLs via harvestapi/linkedin-profile-scraper.

Profile-mode harvests (and short-mode reactions) only carry LinkedIn's opaque profile id, which email enrichment,
the activity check and Smartlead cannot use. One actor run over the qualified ids, $0.004 per profile.
Adds `resolve_status`: resolved | not_found | skipped | error. Never fails the pipeline on a provider error.
"""
import argparse
import os
import sys
from collections import Counter

from dotenv import load_dotenv
try:
    from .common import fit, linkedin_url, location_text, read_csv, write_csv
except ImportError:
    from common import fit, linkedin_url, location_text, read_csv, write_csv

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


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    ap.add_argument('--dry-run', action='store_true', help='print id count, estimate and hard cap; spend nothing')
    ap.add_argument('--batch-size', type=int, default=0,
                    help='ids per actor run (0 = one run). The Apify free plan allows 10 per run on this actor')
    args = ap.parse_args()
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
    if prior:
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
