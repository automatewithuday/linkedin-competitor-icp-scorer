"""Optional GetLeads work-email discovery for qualified leads. Addresses remain UNVERIFIED."""
import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
try:
    from . import getleads, ledger
    from .common import qualification_ok, fit, ledger_ok, linkedin_url, read_csv, valid_email, write_csv
except ImportError:
    import getleads
    import ledger
    from common import qualification_ok, fit, ledger_ok, linkedin_url, read_csv, valid_email, write_csv

FIELDS = ['email', 'email_status', 'sendable', 'email_provider', 'email_checked_at', 'miss_reason']


def email_result(hit):
    data = json.loads(hit['data']) if hit['status'] == 'matched' else {}
    email = data.get('work_email') or ''
    found = isinstance(email, str) and valid_email(email)
    return {'email': email if found else '', 'email_status': 'UNVERIFIED' if found else 'UNAVAILABLE',
            'sendable': 'false', 'email_provider': 'getleads', 'email_checked_at': hit['checked_at'],
            'miss_reason': 'verification_required' if found else 'no_work_email'}


def discover(rows, con, slug, base, min_fit=4, max_lookups=100, cache_days=30, dry_run=False, retry_unknown=False):
    todo, held = {}, []
    used = con.execute('SELECT COUNT(*) FROM enrichment_attempts WHERE provider=? AND slug=?',
                       (getleads.PROVIDER, slug)).fetchone()[0]
    for row in rows:
        row.update(dict.fromkeys(FIELDS, ''))
        row.update(sendable='false', email_provider='getleads', email_status='SKIPPED')
        url = linkedin_url(row.get('linkedin_url'))
        if fit(row) < min_fit:
            row['miss_reason'] = 'below_min_fit'
        elif not qualification_ok(row, min_fit):
            row['miss_reason'] = 'qualification_hold'
        elif not ledger_ok(row):
            row['miss_reason'] = 'ledger_' + row['ledger_status'].split(':')[0]
        elif not url:
            row['miss_reason'] = 'missing_public_url'
        elif ledger.status_for(con, ledger.aliases_of(url=url)) != 'eligible':
            row['miss_reason'] = 'ledger_blocked'
        elif any(k in row for k in ledger.EXPORT_FIELDS) and not ledger.filter_export_rows(con, [row], base)[0]:
            row['miss_reason'] = 'stale_export'
        else:
            hit = getleads.cached(con, url)
            if getleads.fresh(hit, cache_days, require_email=True):
                row.update(email_result(hit))
            elif hit and hit['status'] in ('pending', 'unknown') and not retry_unknown:
                row.update(email_status='UNKNOWN', miss_reason='submission_requires_review')
                held.append(url)
            else:
                row['miss_reason'] = 'lookup_limit_reached'
                todo.setdefault(url, []).append(row)
    urls = list(todo)[:max(0, max_lookups - used)]
    print(f'GetLeads email discovery: {len(urls)} planned submissions; {used}/{max_lookups} used for this slug '
          '(shared with ICP enrichment). Matches and misses use plan credits/fair-use; cached results cost nothing.')
    if dry_run:
        print('Dry run: no API calls or output file. Found emails would remain UNVERIFIED and sendable=false.')
        return rows, bool(held)
    if not urls:
        return rows, bool(held)
    key = os.getenv('GETLEADS_API_KEY')
    if not key:
        raise getleads.ProviderError('Set GETLEADS_API_KEY in .env; no email lookup submitted.')
    remaining, _ = getleads.allowance(key)
    if remaining is not None and remaining < len(urls):
        raise getleads.ProviderError('Insufficient GetLeads allowance; lower --max-lookups or wait for reset.')
    for start in range(0, len(urls), getleads.BATCH_SIZE):
        attempts, reused, concurrent = getleads.reserve(con, urls[start:start + getleads.BATCH_SIZE], slug,
            max_lookups, cache_days, retry_unknown, require_email=True)
        held.extend(concurrent)
        for url in concurrent:
            for row in todo[url]:
                row.update(email_status='UNKNOWN', miss_reason='submission_requires_review')
        for url, hit in reused.items():
            for row in todo[url]:
                row.update(email_result(hit))
        if not attempts:
            continue
        hits, error = getleads.fetch_reserved(con, attempts, key)
        if error:
            for url in attempts:
                for row in todo[url]:
                    row.update(email_status='UNKNOWN' if error.unknown else 'ERROR',
                               miss_reason='submission_requires_review' if error.unknown else 'provider_error')
            print(str(error))
            return rows, True
        for url, hit in hits.items():
            for row in todo[url]:
                row.update(email_result(hit))
    return rows, bool(held)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--slug', help='Submission-budget identity (default: input folder name)')
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    ap.add_argument('--max-lookups', type=int, default=100, help='Total GetLeads submission cap per slug, shared with ICP enrichment')
    ap.add_argument('--cache-days', type=int, default=30)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--retry-unknown', action='store_true', help='Retry after provider review; may consume credits again')
    args = ap.parse_args()
    if args.max_lookups < 1 or args.cache_days < 1:
        ap.error('Lookup cap and cache days must be positive')
    rows, fields = read_csv(args.input)
    base = Path(args.input).parent
    try:
        with ledger.connect() as con:
            rows, failed = discover(rows, con, args.slug or base.name, base, args.min_fit, args.max_lookups,
                                    args.cache_days, args.dry_run, args.retry_unknown)
    except getleads.ProviderError as exc:
        raise SystemExit(str(exc)) from None
    if args.dry_run:
        return
    write_csv(args.output, rows, fields + [x for x in FIELDS if x not in fields])
    print(f'Rows: {len(rows)}; work emails found: {sum(r["email_status"] == "UNVERIFIED" for r in rows)}; '
          'all discovered emails require separate verification before sending.')
    if failed:
        raise SystemExit(3)


if __name__ == '__main__':
    main()
