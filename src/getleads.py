"""Selective ICP evidence enrichment. Apify collects engagement; GetLeads fills buyer-data gaps.

Uses plan credits, NOT the $0.012/engager scraping product. Optional email discovery shares this cache.
Public LinkedIn URLs only; no name matching. Results and submission attempts are shared in SQLite.
"""
import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from . import ledger
    from .common import fit, linkedin_url, read_csv, valid_email, write_json
except ImportError:
    import ledger
    from common import fit, linkedin_url, read_csv, valid_email, write_json

BASE_URL = 'https://app.getleads.io/api/v1'
BATCH_SIZE = 25
CACHE_DAYS = 30
PROVIDER = 'getleads'
FIELDS = ('title', 'company', 'company_domain', 'company_industry', 'company_size',
          'company_description', 'person_location', 'person_country', 'company_linkedin_url', 'company_id')


class ProviderError(RuntimeError):
    def __init__(self, message, unknown=False):
        super().__init__(message)
        self.unknown = unknown


def request(method, path, key, **kwargs):
    # No automatic POST retries: a timeout may have consumed credits. Never surface response/exception bodies.
    try:
        response = requests.request(method, BASE_URL + path, timeout=90,
                                    headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
                                    **kwargs)
    except requests.RequestException:
        raise ProviderError('GetLeads network failure; submission outcome may be unknown.', unknown=True) from None
    if not 200 <= response.status_code < 300:
        raise ProviderError(f'GetLeads HTTP {response.status_code}', unknown=response.status_code >= 500)
    try:
        data = response.json()
    except ValueError:
        raise ProviderError('GetLeads returned invalid JSON.', unknown=True) from None
    if not isinstance(data, dict) or data.get('ok') is not True:
        raise ProviderError('GetLeads returned an invalid response.', unknown=True)
    return data


def allowance(key):
    """Free account check; do not assume that having an account means unlimited enrichment."""
    data = request('GET', '/usage/fair-use', key)
    if data.get('applies') is False:
        remaining = data.get('credits_remaining')
        if type(remaining) is int and remaining >= 0:
            return remaining, 'plan credits'
    elif data.get('applies') is True:
        if data.get('exempt') is True:
            return None, 'account reports fair-use exemption'
        counts = [data[window].get('remaining') if isinstance(data.get(window), dict) else None
                  for window in ('daily', 'monthly')]
        if all(type(n) is int and n >= 0 for n in counts):
            return min(counts), 'remaining fair-use rows'
    raise ProviderError('Cannot establish GetLeads allowance; no enrichment submitted.')


def text_field(data, *keys):
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = ' '.join(str(value).split())
            if value and value not in ('—', '-'):
                return value[:2000]
    return ''


def normalize(data):
    """Allowlist ICP facts and work email; email is never scoring evidence or verified here."""
    facts = dict(zip(FIELDS, (
        text_field(data, 'current_title'),
        text_field(data, 'current_company_name', 'co_name'),
        text_field(data, 'current_company_website', 'org_domain', 'co_website'),
        text_field(data, 'current_company_industry', 'main_industry'),
        text_field(data, 'employee_count_range'),
        text_field(data, 'co_description'),
        text_field(data, 'current_location'),
        text_field(data, 'contact_country', 'location_country'),
        text_field(data, 'current_company_linkedin_url', 'co_linkedin_url'),
        text_field(data, 'current_company_id'),
    )))
    email = text_field(data, 'work_email')
    return facts | {'work_email': email if valid_email(email) else '', 'email_lookup_complete': True}


def enrich_batch(urls, key):
    response = request('POST', '/enrich/from-linkedin', key,
                       json={'items': [{'linkedin_url': u} for u in urls], 'limit_per_item': 1})
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from src import coredb
    with coredb.connect() as evidence_db:
        rid = coredb.new_request(evidence_db, 'getleads', 'from_linkedin', coredb.fingerprint(urls),
                                {'input': {'items': urls}}, os.getenv('CORE_RUN_SLUG'), status='complete')
        root_id = coredb.raw(evidence_db, rid, 'response', response)
        if isinstance(response.get('results'), list):
            for index, result in enumerate(response['results']):
                if isinstance(result, dict):
                    echoed = result.get('linkedin_url') or result.get('linkedinUrl')
                    coredb.raw(evidence_db, rid, linkedin_url(echoed) or str(index), result, source_raw_id=root_id)
    results = response.get('results')
    if not isinstance(results, list):
        raise ProviderError('GetLeads omitted batch results.', unknown=True)
    out = {}
    for result in results:
        if not isinstance(result, dict):
            raise ProviderError('GetLeads returned malformed result.', unknown=True)
        echoed = result.get('linkedinUrl') or result.get('linkedin_url')
        url = linkedin_url(echoed) if isinstance(echoed, str) else ''
        if url not in urls or url in out or type(result.get('success')) is not bool:
            raise ProviderError('GetLeads returned ambiguous result identities.', unknown=True)
        data = result.get('data')
        if result['success'] is False and data is None:
            out[url] = ('not_found', {})
        elif result['success'] is True and isinstance(data, dict):
            returned = linkedin_url(data['linkedin_url']) if isinstance(data.get('linkedin_url'), str) else ''
            if not returned or returned != url:
                raise ProviderError('GetLeads profile identity did not match the requested person.', unknown=True)
            out[url] = ('matched', normalize(data))
        else:
            raise ProviderError('GetLeads returned inconsistent match data.', unknown=True)
    if set(out) != set(urls):
        raise ProviderError('GetLeads omitted requested identities.', unknown=True)
    return out


def missing_evidence(e, icp):
    try:
        from .evidence import scoring_enrichment
    except ImportError:
        from evidence import scoring_enrichment
    enriched, _ = scoring_enrichment(e)
    roles = (e.get('profile_evidence') or {}).get('current_roles') or []
    title = roles[0].get('title') if roles else ''
    missing = []
    for field, present in (
        ('title', title or e.get('position') or enriched.get('title')),
        ('company', e.get('company') or enriched.get('company')),
        ('location', not icp.get('geos') or e.get('location') or enriched.get('person_location') or enriched.get('person_country')),
        ('industry', not (icp.get('industries') or icp.get('industries_out')) or enriched.get('company_industry')),
        ('company_size', not icp.get('size_range') or enriched.get('company_size')),
    ):
        if not present:
            missing.append(field)
    return missing


def cached(con, url):
    row = con.execute('SELECT * FROM provider_enrichments WHERE provider=? AND identity=?', (PROVIDER, url)).fetchone()
    return dict(row) if row else None


def fresh(hit, days, require_email=False):
    if not hit or hit['status'] not in ('matched', 'not_found'):
        return False
    if require_email and hit['status'] == 'matched' and not json.loads(hit['data']).get('email_lookup_complete'):
        return False  # older caches discarded email fields; they cannot certify that no email was found
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(hit['checked_at'])).total_seconds()
    return 0 <= age < days * 86400


def apply(e, data, checked_at):
    # Keep Apify evidence intact. Conflicting employers stay visible as separately sourced evidence.
    facts = {k: data.get(k) or '' for k in FIELDS}
    if any(facts.values()):
        e['icp_enrichment'] = {**facts, 'source': PROVIDER, 'checked_at': checked_at}


def reserve(con, urls, slug, max_lookups, cache_days, retry_unknown, require_email=False):
    """Atomically claim submissions so concurrent runs cannot both pay for the same URL."""
    attempts, reused, held = {}, {}, []
    with con:
        con.execute('BEGIN IMMEDIATE')
        used = con.execute('SELECT COUNT(*) FROM enrichment_attempts WHERE provider=? AND slug=?',
                           (PROVIDER, slug)).fetchone()[0]
        for url in urls:
            hit = cached(con, url)
            if fresh(hit, cache_days, require_email):
                reused[url] = hit
                continue
            if hit and hit['status'] in ('pending', 'unknown') and not retry_unknown:
                held.append(url)
                continue
            if used >= max_lookups:
                continue
            stamp = ledger.now()
            attempts[url] = con.execute('INSERT INTO enrichment_attempts '
                '(provider, slug, identity, status, submitted_at) VALUES (?,?,?,?,?)',
                (PROVIDER, slug, url, 'pending', stamp)).lastrowid
            con.execute('INSERT INTO provider_enrichments VALUES (?,?,?,?,?) '
                        'ON CONFLICT(provider,identity) DO UPDATE SET status=excluded.status',
                        (PROVIDER, url, 'pending', '{}', stamp))
            used += 1
    return attempts, reused, held


def fetch_reserved(con, attempts, key):
    """Complete a previously committed claim; used by both ICP enrichment and email discovery."""
    stamp = ledger.now()
    try:
        results = enrich_batch(list(attempts), key)
    except ProviderError as exc:
        status = 'unknown' if exc.unknown else 'error'
        for url, attempt_id in attempts.items():
            con.execute('UPDATE enrichment_attempts SET status=? WHERE id=?', (status, attempt_id))
            con.execute('UPDATE provider_enrichments SET status=? WHERE provider=? AND identity=?', (status, PROVIDER, url))
        con.commit()
        return {}, exc
    hits = {}
    for url, (status, data) in results.items():
        con.execute('INSERT OR REPLACE INTO provider_enrichments VALUES (?,?,?,?,?)',
                    (PROVIDER, url, status, json.dumps(data), stamp))
        con.execute('UPDATE enrichment_attempts SET status=? WHERE id=?', (status, attempts[url]))
        hits[url] = cached(con, url)
    con.commit()
    return hits, None


def enrich_harvest(base, min_fit=3, max_lookups=100, dry_run=False, cache_days=CACHE_DAYS, retry_unknown=False):
    base = Path(base)
    engagers = json.loads((base / 'engagers.json').read_text())
    icp = json.loads((base / 'recipient_icp.json').read_text())
    rows, _ = read_csv(base / 'ranked_engagers.csv')
    fits = {r.get('actor_id'): fit(r) for r in rows}
    todo, changed, held = {}, [], []
    with ledger.connect() as con:
        before = [ledger.evidence_hash(e) for e in engagers]
        used = con.execute('SELECT COUNT(*) FROM enrichment_attempts WHERE provider=? AND slug=?',
                           (PROVIDER, base.name)).fetchone()[0]
        for i, e in enumerate(engagers):
            url = linkedin_url(e.get('linkedinUrl'))
            hit = cached(con, url) if url else None
            # A later rejection does not erase the fact that this person was already looked up.
            e['getleads_status'] = hit['status'] if hit and hit['status'] in ('matched', 'not_found') else 'below_min_fit'
            if hit:
                e['getleads_checked_at'] = hit['checked_at']
            if fits.get(e.get('id'), 0) < min_fit:
                continue
            e['getleads_status'] = 'missing_public_url'
            if not url:
                continue
            status = ledger.status_for(con, ledger.aliases_of(e))
            e['getleads_status'] = status
            if status != 'eligible':
                continue
            if hit and hit['status'] == 'matched':
                apply(e, json.loads(hit['data']), hit['checked_at'])
            if fresh(hit, cache_days):
                e['getleads_status'] = hit['status']
                continue  # both misses and partial matches consume credits; reuse either for 30 days
            if not missing_evidence(e, icp) and not hit:
                e['getleads_status'] = 'not_needed'
                continue
            if hit and hit['status'] in ('pending', 'unknown') and not retry_unknown:
                e['getleads_status'] = hit['status']
                held.append(url)
                continue
            e['getleads_status'] = 'lookup_limit_reached'
            todo.setdefault(url, []).append(i)

        candidates = len(todo)
        urls = list(todo)[:max(0, max_lookups - used)]
        print(f'GetLeads: {candidates} identities need lookup; {len(urls)} planned; '
              f'{used}/{max_lookups} submissions already recorded for this slug. '
              f'At most {len(urls)} plan credits (misses included in budget), or included fair-use rows; no wallet scrape.')
        if held:
            print(f'GetLeads: {len(held)} pending/unknown submissions held; check the provider before --retry-unknown.')
        if dry_run:
            print('Dry run: no account/enrichment API calls or evidence changes. Account allowance checked on execution.')
            return [], bool(held)

        def checkpoint():
            for index, person in enumerate(engagers):
                evidence_changed = ledger.evidence_hash(person) != before[index]
                if evidence_changed or person.get('icp_enrichment'):
                    cid, _ = ledger.contact_for(con, ledger.aliases_of(person), person.get('name'), base.name)
                    ledger.snapshot(con, cid, person, PROVIDER)
                if evidence_changed:
                    if index not in changed:
                        changed.append(index)
                    ledger.touch_evidence(con, cid, person)
            con.commit()
            write_json(base / 'engagers.json', engagers)

        checkpoint()  # cache replay can itself invalidate earlier scores
        if not urls:
            return changed, bool(held)
        key = os.getenv('GETLEADS_API_KEY')
        if not key:
            raise ProviderError('Set GETLEADS_API_KEY in .env; no enrichment submitted.')
        remaining, kind = allowance(key)
        print(f'GetLeads allowance: {remaining if remaining is not None else "exempt"} {kind}.')
        if remaining is not None and remaining < len(urls):
            raise ProviderError('Insufficient GetLeads allowance for the planned batch; lower --max-lookups or wait for reset.')
        for start in range(0, len(urls), BATCH_SIZE):
            chunk = urls[start:start + BATCH_SIZE]
            # Commit BEFORE submitting. Process interruption cannot silently repeat a charged batch.
            attempt_ids, reused, concurrent = reserve(con, chunk, base.name, max_lookups, cache_days, retry_unknown)
            held.extend(concurrent)
            for url, hit in reused.items():
                for i in todo[url]:
                    engagers[i]['getleads_status'] = hit['status']
                    engagers[i]['getleads_checked_at'] = hit['checked_at']
                    if hit['status'] == 'matched':
                        apply(engagers[i], json.loads(hit['data']), hit['checked_at'])
            for url in concurrent:
                for i in todo[url]:
                    engagers[i]['getleads_status'] = 'pending_or_unknown'
            checkpoint()
            chunk = list(attempt_ids)
            if not chunk:
                continue
            hits, exc = fetch_reserved(con, attempt_ids, key)
            if exc:
                status = 'unknown' if exc.unknown else 'error'
                for url in chunk:
                    for i in todo[url]:
                        engagers[i]['getleads_status'] = status
                checkpoint()
                print(str(exc))
                return changed, True
            for url, hit in hits.items():
                for i in todo[url]:
                    engagers[i]['getleads_status'] = hit['status']
                    engagers[i]['getleads_checked_at'] = hit['checked_at']
                if hit['status'] == 'matched':
                    for i in todo[url]:
                        apply(engagers[i], json.loads(hit['data']), hit['checked_at'])
            checkpoint()
            print(f'GetLeads batch: {len(chunk)} submitted; {sum(h["status"] == "matched" for h in hits.values())} matched.')
        return changed, bool(held)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--slug', required=True)
    ap.add_argument('--output-dir', default='./output')
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=3)
    ap.add_argument('--max-lookups', type=int, default=100, help='Total submission cap per slug, including retries')
    ap.add_argument('--cache-days', type=int, default=CACHE_DAYS)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--retry-unknown', action='store_true', help='Explicitly retry unknown submissions after provider review; may cost again')
    args = ap.parse_args()
    if args.max_lookups < 1 or args.cache_days < 1:
        ap.error('Lookup cap and cache days must be positive')
    try:
        changed, failed = enrich_harvest(Path(args.output_dir) / args.slug, args.min_fit, args.max_lookups,
                                        args.dry_run, args.cache_days, args.retry_unknown)
    except ProviderError as exc:
        print(str(exc))
        raise SystemExit(3) from None
    if changed:
        print(f'ICP evidence changed for indices {changed}; rescore with rank.py --dump before exports.')
    if failed and not args.dry_run:
        raise SystemExit(3)


if __name__ == '__main__':
    main()
