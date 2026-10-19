"""Preview or import verified, qualified leads into a Smartlead campaign."""
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
try:
    from .common import fit, ledger_ok, post_json, read_csv, valid_email, write_json
    from . import ledger
except ImportError:
    from common import fit, ledger_ok, post_json, read_csv, valid_email, write_json
    import ledger


def select_leads(rows, min_fit=4, max_age_days=30):
    leads, seen, excluded = [], set(), 0
    for row in rows:
        email = (row.get('email') or '').strip().lower()
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(row.get('email_checked_at', ''))).total_seconds()
            fresh = 0 <= age <= max_age_days * 86400
        except (ValueError, TypeError):
            fresh = False
        if (not valid_email(email) or email in seen or fit(row) < min_fit or not fresh
                or row.get('sendable', '').lower() != 'true'
                or row.get('email_status', '').upper() != 'VERIFIED'
                or row.get('suppressed', '').lower() == 'true' or not ledger_ok(row)):
            excluded += 1
            continue
        seen.add(email)
        first, _, last = row.get('name', '').partition(' ')
        leads.append({'email': email, 'first_name': first, 'last_name': last,
                      'company_name': row.get('company_guess', ''),
                      'linkedin_profile': row.get('linkedin_url', ''),
                      'custom_fields': {k: str(row.get(k, '')) for k in
                          ['clean_title', 'icp_fit', 'intent', 'rank_score', 'comment_excerpt', 'source_profile', 'post_urls']}})
    return leads, excluded


def push_batches(leads, campaign_id, key, receipt_path, before_batch=None):
    receipt = {'campaign_id': campaign_id, 'requested': len(leads), 'batches': [], 'complete': False}
    write_json(receipt_path, receipt)
    for start in range(0, len(leads), 400):
        batch = leads[start:start + 400]
        if before_batch:
            before_batch(batch)
        entry = {'offset': start, 'requested': len(batch), 'status': 'pending'}
        receipt['batches'].append(entry)
        write_json(receipt_path, receipt)
        try:
            response = post_json(f'https://server.smartlead.ai/api/v1/campaigns/{campaign_id}/leads',
                params={'api_key': key}, json={'lead_list': batch, 'settings': {
                    'ignore_global_block_list': False, 'ignore_unsubscribe_list': False,
                    'ignore_duplicate_leads_in_other_campaign': False,
                    'ignore_community_bounce_list': False, 'return_lead_ids': True}})
            if not isinstance(response, dict) or response.get('success') is not True:
                raise RuntimeError('Smartlead did not confirm success; reconcile campaign before retry')
            added, skipped = response.get('added_count'), response.get('skipped_count')
            if type(added) is not int or type(skipped) is not int or min(added, skipped) < 0 or added + skipped != len(batch):
                raise RuntimeError('Smartlead counts missing/inconsistent; reconcile campaign before retry')
            entry.update(status='confirmed', added=added, skipped=skipped,
                         lead_ids=response.get('lead_ids', []), skipped_leads=response.get('skipped_leads', []))
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--to', choices=['smartlead'], default='smartlead')
    ap.add_argument('--campaign-id', type=int, required=True)
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    ap.add_argument('--max-email-age-days', type=int, default=30)
    ap.add_argument('--execute', action='store_true', help='Import into campaign; may trigger sending if campaign is active')
    ap.add_argument('--receipt', help='JSON receipt path')
    args = ap.parse_args()
    if args.campaign_id <= 0 or args.max_email_age_days <= 0:
        ap.error('Campaign ID and email age must be positive')
    rows, _ = read_csv(args.input)
    con = ledger.connect()
    rows, dropped = ledger.filter_export_rows(con, rows, Path(args.input).parent)
    leads, excluded = select_leads(rows, args.min_fit, args.max_email_age_days)
    excluded += dropped
    path = args.receipt or args.input + '.smartlead.json'
    if not args.execute:
        write_json(path, {'preview': True, 'campaign_id': args.campaign_id, 'eligible': len(leads), 'excluded': excluded, 'leads': leads})
        print(f'Preview: {len(leads)} eligible; {excluded} excluded. Review {path}; use --execute to import.')
        return
    key = os.getenv('SMARTLEAD_API_KEY')
    if not key:
        ap.error('Set SMARTLEAD_API_KEY in .env')
    try:
        receipt = push_batches(leads, args.campaign_id, key, path,
                              ledger.export_validator(con, rows, 'linkedin_profile', 'email', Path(args.input).parent))
    except RuntimeError as exc:
        ledger.record_receipt(con, leads, path, f'smartlead:{args.campaign_id}', 'linkedin_profile', 'email')
        raise SystemExit(str(exc)) from None
    ledger.record_receipt(con, leads, path, f'smartlead:{args.campaign_id}', 'linkedin_profile', 'email')
    print(f'Added: {sum(b["added"] for b in receipt["batches"])}; skipped: {sum(b["skipped"] for b in receipt["batches"])}. Receipt: {path}')


if __name__ == '__main__':
    main()
