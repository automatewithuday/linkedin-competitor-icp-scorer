"""Preview or push qualified leads into a HeyReach list (or running campaign) for LinkedIn outreach.

Default target is a lead LIST: adding to a list never sends anything; you attach the
list to a campaign in HeyReach. --campaign-id adds straight into a running campaign
(needs --linkedin-account-id, the sender seat) and can start sending.
If drafts.csv sits next to the input CSV, connection_note and dm are attached as
custom fields so they can be used as personalisation variables.
"""
import argparse
import os
import re
from pathlib import Path

from dotenv import load_dotenv
try:
    from .common import fit, post_json, read_csv, valid_email, write_json, linkedin_url
except ImportError:
    from common import fit, post_json, read_csv, valid_email, write_json, linkedin_url

BASE = 'https://api.heyreach.io/api/public'
BATCH = 100  # HeyReach cap per request
CUSTOM = ['clean_title', 'icp_fit', 'intent', 'rank_score', 'comment_excerpt', 'source_profile',
          'post_urls', 'template', 'connection_note', 'dm']


def load_drafts(input_path):
    p = Path(input_path).with_name('drafts.csv')
    if not p.exists():
        return {}
    rows, _ = read_csv(p)
    return {r.get('linkedin_url', ''): r for r in rows}


def select_leads(rows, min_fit=4, drafts=None):
    leads, seen, excluded = [], set(), 0
    for row in rows:
        raw = (row.get('linkedin_url') or '').strip()
        # Apify often returns member-ID URLs (/in/ACoAA...). LinkedIn resolves them, HeyReach accepts them.
        url = linkedin_url(raw) or (raw if re.fullmatch(r'https://www\.linkedin\.com/in/ACoAA[\w-]+/?', raw) else '')
        if not url or url in seen or fit(row) < min_fit or row.get('suppressed', '').lower() == 'true':
            excluded += 1
            continue
        seen.add(url)
        d = (drafts or {}).get(row.get('linkedin_url', ''), {})
        merged = {**row, **{k: d.get(k, '') for k in ('template', 'connection_note', 'dm')}}
        first, _, last = row.get('name', '').partition(' ')
        lead = {'firstName': first, 'lastName': last, 'profileUrl': url,
                'companyName': row.get('company_guess', ''), 'position': row.get('clean_title', ''),
                'customUserFields': [{'name': k, 'value': str(merged.get(k, ''))} for k in CUSTOM if merged.get(k)]}
        email = (row.get('email') or '').strip().lower()
        if valid_email(email) and row.get('email_status', '').upper() == 'VERIFIED':
            lead['emailAddress'] = email
        leads.append(lead)
    return leads, excluded


def push_batches(leads, target, key, receipt_path):
    """target: {'listId': n} or {'campaignId': n, 'linkedInAccountId': n}."""
    receipt = {**target, 'requested': len(leads), 'batches': [], 'complete': False}
    write_json(receipt_path, receipt)
    headers = {'X-API-KEY': key}
    for start in range(0, len(leads), BATCH):
        batch = leads[start:start + BATCH]
        entry = {'offset': start, 'requested': len(batch), 'status': 'pending'}
        receipt['batches'].append(entry)
        write_json(receipt_path, receipt)
        try:
            if 'listId' in target:
                resp = post_json(f'{BASE}/list/AddLeadsToListV2', headers=headers,
                                 json={'listId': target['listId'], 'leads': batch})
            else:
                resp = post_json(f'{BASE}/campaign/AddLeadsToCampaignV2', headers=headers,
                                 json={'campaignId': target['campaignId'], 'accountLeadPairs': [
                                     {'linkedInAccountId': target['linkedInAccountId'], 'lead': l} for l in batch]})
            counts = {k: resp.get(k) for k in ('addedLeadsCount', 'updatedLeadsCount', 'failedLeadsCount')} if isinstance(resp, dict) else {}
            if any(type(v) is not int or v < 0 for v in counts.values()) or sum(counts.values()) != len(batch):
                raise RuntimeError('HeyReach counts missing/inconsistent; reconcile in HeyReach before retry')
            entry.update(status='confirmed', **counts)
        except RuntimeError as exc:
            entry.update(status='unconfirmed', error=str(exc))
            write_json(receipt_path, receipt)
            raise
        write_json(receipt_path, receipt)
    receipt['complete'] = True
    write_json(receipt_path, receipt)
    return receipt


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--input', required=True, help='ranked_engagers.csv or enriched.csv')
    dest = ap.add_mutually_exclusive_group(required=True)
    dest.add_argument('--list-id', type=int, help='HeyReach lead list (safe: nothing sends)')
    dest.add_argument('--campaign-id', type=int, help='Running HeyReach campaign (may start sending)')
    ap.add_argument('--linkedin-account-id', type=int, help='Sender seat; required with --campaign-id')
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    ap.add_argument('--execute', action='store_true', help='Actually push; default is a preview file')
    ap.add_argument('--receipt', help='JSON receipt path')
    args = ap.parse_args()
    if args.campaign_id and not args.linkedin_account_id:
        ap.error('--campaign-id requires --linkedin-account-id (see HeyReach > LinkedIn accounts)')
    target = {'listId': args.list_id} if args.list_id else {'campaignId': args.campaign_id, 'linkedInAccountId': args.linkedin_account_id}
    if min(target.values()) <= 0:
        ap.error('IDs must be positive')
    rows, _ = read_csv(args.input)
    leads, excluded = select_leads(rows, args.min_fit, load_drafts(args.input))
    path = args.receipt or args.input + '.heyreach.json'
    if not args.execute:
        write_json(path, {'preview': True, **target, 'eligible': len(leads), 'excluded': excluded, 'leads': leads})
        print(f'Preview: {len(leads)} eligible; {excluded} excluded. Review {path}; use --execute to push.')
        return
    key = os.getenv('HEYREACH_API_KEY')
    if not key:
        ap.error('Set HEYREACH_API_KEY in .env')
    try:
        receipt = push_batches(leads, target, key, path)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None
    b = receipt['batches']
    print(f'Added: {sum(x["addedLeadsCount"] for x in b)}; updated: {sum(x["updatedLeadsCount"] for x in b)}; '
          f'failed: {sum(x["failedLeadsCount"] for x in b)}. Receipt: {path}')


if __name__ == '__main__':
    main()
