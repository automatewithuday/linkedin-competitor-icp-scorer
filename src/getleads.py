"""GetLeads evidence gap-filling for the company stage. Uses plan credits, not the paid scraping product.

Public LinkedIn URLs only; no name matching. Raw responses go to the evidence store before parsing. Results and
submission attempts are shared in SQLite, so a URL is never paid for twice within CACHE_DAYS.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from . import ledger
    from .common import linkedin_url, valid_email
except ImportError:
    import ledger
    from common import linkedin_url, valid_email

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
    """Complete a previously committed claim."""
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
