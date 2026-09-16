"""Prospeo enrichment. LinkedIn identity first, checkpointed verified results."""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
try:
    from .common import fit, linkedin_url, post_json, read_csv, valid_email, write_csv, write_json
except ImportError:
    from common import fit, linkedin_url, post_json, read_csv, valid_email, write_csv, write_json

FIELDS = ['email', 'email_status', 'sendable', 'email_provider', 'email_checked_at', 'miss_reason']


def identity(row):
    url = linkedin_url(row.get('linkedin_url'))
    if url:
        return {'linkedin_url': url}
    # A guessed employer is not a reliable identity match. Require a known domain.
    domain = row.get('domain') or row.get('company_website')
    if row.get('name') and domain:
        return {'full_name': row['name'], 'company_website': domain}
    return None


def enrich_person(data, key):
    result = post_json('https://api.prospeo.io/enrich-person', retry=True, accepted_errors=('NO_MATCH',),
                       headers={'X-KEY': key, 'Content-Type': 'application/json'},
                       json={'data': data, 'only_verified_email': True, 'enrich_mobile': False})
    if not isinstance(result, dict):
        raise RuntimeError('Malformed Prospeo response')
    if result.get('error_code') == 'NO_MATCH':
        return {'email': '', 'email_status': 'UNAVAILABLE', 'sendable': 'false',
                'email_provider': 'prospeo', 'email_checked_at': datetime.now(timezone.utc).isoformat(),
                'miss_reason': 'no_verified_match'}
    if result.get('error'):
        # Keep provider codes, never raw payloads or auth material.
        raise RuntimeError('Prospeo rejected enrichment')
    person = result.get('person') or {}
    if not isinstance(person, dict):
        raise RuntimeError('Malformed Prospeo person')
    email_data = person.get('email') or {}
    if not isinstance(email_data, dict):
        raise RuntimeError('Malformed Prospeo email')
    email = email_data.get('email') or ''
    status = email_data.get('status') or 'UNAVAILABLE'
    if not isinstance(status, str) or not isinstance(email, str):
        raise RuntimeError('Malformed Prospeo email fields')
    status = status.upper()
    sendable = status == 'VERIFIED' and email_data.get('revealed') is True and valid_email(email)
    return {'email': email if valid_email(email) else '', 'email_status': status,
            'sendable': str(sendable).lower(), 'email_provider': 'prospeo',
            'email_checked_at': datetime.now(timezone.utc).isoformat(),
            'miss_reason': '' if sendable else 'no_verified_email'}


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    ap.add_argument('--max-lookups', type=int, default=100)
    ap.add_argument('--cache-days', type=int, default=30)
    args = ap.parse_args()
    if args.max_lookups < 1 or args.cache_days < 0:
        ap.error('Lookup limit must be positive; cache days must be nonnegative')
    key = os.getenv('PROSPEO_API_KEY')
    if not key:
        ap.error('Set PROSPEO_API_KEY in .env')
    rows, fields = read_csv(args.input)
    cache_path = Path(args.output).with_suffix('.prospeo-cache.json')
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    calls = failures = 0
    for row in rows:
        result = dict.fromkeys(FIELDS, '')
        result.update(sendable='false', email_provider='prospeo', email_status='SKIPPED')
        data = identity(row)
        if fit(row) < args.min_fit:
            result['miss_reason'] = 'below_min_fit'
        elif not data:
            result['miss_reason'] = 'missing_reliable_identity'
        else:
            cache_id = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            cached = cache.get(cache_id)
            fresh = False
            if cached:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(cached['email_checked_at'])
                fresh = 0 <= age.total_seconds() < args.cache_days * 86400
            if fresh:
                result = cached
            elif calls >= args.max_lookups:
                result['miss_reason'] = 'lookup_limit_reached'
            else:
                calls += 1
                try:
                    result = enrich_person(data, key)
                    cache[cache_id] = result
                    write_json(cache_path, cache)
                except RuntimeError as exc:
                    result.update(email_status='ERROR', miss_reason=str(exc))
                    failures += 1
        row.update(result)
    write_csv(args.output, rows, fields + [x for x in FIELDS if x not in fields])
    print(f'Rows: {len(rows)}; lookups: {calls}; verified: {sum(r["sendable"] == "true" for r in rows)}; errors: {failures}')
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
