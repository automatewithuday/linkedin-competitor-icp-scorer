"""GetLeads email discovery: shared billing/cache and explicit verification boundaries."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import common, email_enrich, getleads, ledger, push, push_heyreach, rank


class EmailDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {'LEDGER_DB': str(self.root / 'ledger.sqlite'),
                                                 'GETLEADS_API_KEY': 'offline'}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.con = ledger.connect()
        self.addCleanup(self.con.close)

    def row(self, slug='jane', **changes):
        return {'name': slug, 'linkedin_url': f'https://www.linkedin.com/in/{slug}',
                'icp_fit': '4', 'ledger_status': 'eligible', **changes}

    def data(self, email='jane@example.com'):
        return getleads.normalize({'work_email': email, 'current_title': 'VP Sales',
                                  'current_company_name': 'Example', 'employee_count_range': '51 to 200'})

    def cache(self, row=None, status='matched', data=None):
        row = row or self.row()
        self.con.execute('INSERT OR REPLACE INTO provider_enrichments VALUES (?,?,?,?,?)',
                         ('getleads', row['linkedin_url'], status, json.dumps(data if data is not None else self.data()), ledger.now()))
        self.con.commit()

    def discover(self, rows, slug='run', **kwargs):
        return email_enrich.discover(rows, self.con, slug, self.root, **kwargs)

    def test_email_uses_icp_cache_and_never_changes_scoring_evidence(self):
        self.cache()
        person = {'id': 'ACoAA1', 'position': 'VP Sales'}
        getleads.apply(person, self.data(), ledger.now())
        before = ledger.evidence_hash(person)
        with patch.object(getleads, 'request') as req:
            rows, failed = self.discover([self.row()])
        req.assert_not_called()
        self.assertFalse(failed)
        self.assertEqual((rows[0]['email'], rows[0]['email_status'], rows[0]['sendable']),
                         ('jane@example.com', 'UNVERIFIED', 'false'))
        self.assertEqual(before, ledger.evidence_hash(person))
        self.assertNotIn('work_email', person['icp_enrichment'])
        self.assertEqual(push.select_leads(rows), ([], 1))
        self.assertNotIn('emailAddress', push_heyreach.select_leads(rows)[0][0])

    def test_only_qualified_public_unblocked_identities_are_submitted(self):
        rows = [self.row(), self.row('low', icp_fit='2'), self.row('hidden', linkedin_url='https://linkedin.com/in/ACoAA1'),
                self.row('blocked'), self.row('missing', linkedin_url='', domain='example.com')]
        ledger.record_campaign(self.con, [(rows[3]['linkedin_url'], None)], 'heyreach:1', 'imported')
        with patch.object(getleads, 'allowance', return_value=(20, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                rows[0]['linkedin_url']: ('matched', self.data())}) as req:
            out, failed = self.discover(rows)
        self.assertFalse(failed)
        self.assertEqual(req.call_args.args[0], [rows[0]['linkedin_url']])
        self.assertEqual([r['miss_reason'] for r in out[1:]],
                         ['below_min_fit', 'missing_public_url', 'ledger_blocked', 'missing_public_url'])

    def test_miss_and_no_email_are_cached_across_runs(self):
        for status, data in [('not_found', {}), ('matched', self.data(''))]:
            self.cache(status=status, data=data)
            with patch.object(getleads, 'request') as req:
                for slug in ('one', 'two'):
                    rows, failed = self.discover([self.row()], slug=slug)
                    self.assertEqual((rows[0]['email_status'], rows[0]['sendable']), ('UNAVAILABLE', 'false'))
                    self.assertFalse(failed)
            req.assert_not_called()

    def test_submission_cap_does_not_reset_on_rerun(self):
        first, second = self.row(), self.row('bob')
        with patch.object(getleads, 'allowance', return_value=(20, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                first['linkedin_url']: ('matched', self.data())}) as req:
            self.discover([first.copy(), second.copy()], max_lookups=1)
            rows, _ = self.discover([first.copy(), second.copy()], max_lookups=1)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(rows[1]['miss_reason'], 'lookup_limit_reached')

    def test_unknown_submission_is_not_silently_retried(self):
        with patch.object(getleads, 'allowance', return_value=(20, 'credits')), patch.object(getleads, 'enrich_batch',
                side_effect=getleads.ProviderError('unknown', unknown=True)) as req:
            _, failed = self.discover([self.row()])
            rows, again = self.discover([self.row()])
        self.assertTrue(failed and again)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(rows[0]['email_status'], 'UNKNOWN')
        with patch.object(getleads, 'allowance', return_value=(20, 'credits')), patch.object(getleads, 'enrich_batch',
                return_value={self.row()['linkedin_url']: ('matched', self.data())}) as req:
            rows, failed = self.discover([self.row()], retry_unknown=True)
        self.assertFalse(failed)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(rows[0]['email_status'], 'UNVERIFIED')

    def test_pre_upgrade_stripped_cache_requires_one_new_lookup(self):
        self.cache(data={'title': 'VP Sales', 'company': 'Example'})
        with patch.object(getleads, 'allowance', return_value=(20, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                self.row()['linkedin_url']: ('matched', self.data())}) as req:
            self.discover([self.row()])
            self.discover([self.row()], slug='another')
        self.assertEqual(req.call_count, 1)

    def test_dry_run_spends_nothing_and_writes_no_output(self):
        source, output = self.root / 'in.csv', self.root / 'out.csv'
        row = self.row()
        common.write_csv(source, [row], list(row))
        with patch.object(getleads, 'request') as req, patch('sys.argv', [
                'email_enrich.py', '--input', str(source), '--output', str(output), '--dry-run']):
            email_enrich.main()
        req.assert_not_called()
        self.assertFalse(output.exists())
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM enrichment_attempts').fetchone()[0], 0)

    def test_stale_export_is_skipped_before_spend(self):
        rows = [self.row(ledger_evidence_hash='stale')]
        with patch.object(getleads, 'request') as req:
            rows, failed = self.discover(rows)
        req.assert_not_called()
        self.assertFalse(failed)
        self.assertEqual(rows[0]['miss_reason'], 'stale_export')

    def test_icp_and_email_share_one_submission_cap(self):
        url = self.row('already-enriched')['linkedin_url']
        getleads.reserve(self.con, [url], 'run', 1, 30, False)
        with patch.object(getleads, 'request') as req:
            rows, _ = self.discover([self.row()], max_lookups=1)
        req.assert_not_called()
        self.assertEqual(rows[0]['miss_reason'], 'lookup_limit_reached')

    def test_partial_batches_keep_completed_results_in_sqlite(self):
        rows = [self.row(), self.row('bob')]
        with patch.object(getleads, 'BATCH_SIZE', 1), patch.object(getleads, 'allowance', return_value=(20, 'credits')), \
                patch.object(getleads, 'enrich_batch', side_effect=[{rows[0]['linkedin_url']: ('matched', self.data())},
                    getleads.ProviderError('unknown', unknown=True)]):
            out, failed = self.discover(rows)
        self.assertTrue(failed)
        self.assertEqual(out[0]['email_status'], 'UNVERIFIED')
        self.assertEqual(getleads.cached(self.con, rows[0]['linkedin_url'])['status'], 'matched')
        self.assertEqual(getleads.cached(self.con, rows[1]['linkedin_url'])['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
