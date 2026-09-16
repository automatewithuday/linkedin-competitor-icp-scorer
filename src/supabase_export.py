"""Store per-run lead snapshots in Supabase while retaining the CSV artifact."""
import argparse
import hashlib
import os
import re
from urllib.parse import urlsplit

from dotenv import load_dotenv
try:
    from .common import linkedin_url, post_json, read_csv, write_json
except ImportError:
    from common import linkedin_url, post_json, read_csv, write_json


def snapshot_rows(rows, run_id):
    records = {}
    for row in rows:
        identity = linkedin_url(row.get('linkedin_url')) or row.get('actor_id', '').strip()
        if not identity:
            raise ValueError('Supabase export requires a public LinkedIn URL or actor_id for every row')
        key = hashlib.sha256(identity.encode()).hexdigest()
        records[key] = {'run_id': run_id, 'lead_key': key,
                        'linkedin_url': linkedin_url(row.get('linkedin_url')) or None,
                        'data': row}
    return list(records.values())


def export(rows, run_id, url, key, table='warm_lead_snapshots'):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.query or p.fragment or p.path not in ('', '/'):
        raise ValueError('SUPABASE_URL must be an HTTPS project origin')
    if not re.fullmatch(r'[a-z][a-z0-9_]*', table):
        raise ValueError('Invalid Supabase table name')
    records = snapshot_rows(rows, run_id)
    headers = {'apikey': key, 'Content-Type': 'application/json',
               'Prefer': 'resolution=merge-duplicates,return=minimal'}
    # Legacy service-role JWTs require Authorization; new secret keys use apikey.
    if not key.startswith('sb_secret_'):
        headers['Authorization'] = 'Bearer ' + key
    for start in range(0, len(records), 500):
        post_json(url.rstrip('/') + '/rest/v1/' + table, retry=True,
                  params={'on_conflict': 'run_id,lead_key'}, headers=headers,
                  json=records[start:start + 500])
    return len(records)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--run-id', required=True, help='Reuse to retry; choose a new value for a new observation run')
    ap.add_argument('--table', default='warm_lead_snapshots')
    args = ap.parse_args()
    url, key = os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SECRET_KEY') or os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        ap.error('Set SUPABASE_URL and SUPABASE_SECRET_KEY (or SUPABASE_SERVICE_ROLE_KEY)')
    rows, _ = read_csv(args.input)
    try:
        count = export(rows, args.run_id, url, key, args.table)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from None
    write_json(args.input + '.supabase.json', {'run_id': args.run_id, 'upserted': count, 'table': args.table})
    print(f'Supabase: {count} snapshots upserted for {args.run_id}')


if __name__ == '__main__':
    main()
