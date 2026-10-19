"""Offline provider-contract, billing, evidence and pipeline regression tests."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import run as pipeline
from src import common, getleads, icp, ledger, rank, summary


class GetLeadsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {'LEDGER_DB': str(self.root / 'ledger.sqlite'),
                                                 'GETLEADS_API_KEY': 'offline-secret', 'APIFY_TOKEN': 'offline',
                                                 'LLM_PROVIDER': 'openai', 'OPENAI_API_KEY': 'offline'}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.con = ledger.connect()
        self.addCleanup(self.con.close)

    def person(self, n=1, **changes):
        return {'id': f'ACoAA{n}', 'name': f'Person {n}', 'position': 'VP Sales at Acme',
                'company': 'Acme', 'location': 'United States',
                'linkedinUrl': f'https://www.linkedin.com/in/person{n}', **changes}

    def seed(self, slug='run', people=None, recipient=None, fits=None):
        people = people or [self.person()]
        base = self.root / slug
        base.mkdir()
        common.write_json(base / 'engagers.json', people)
        common.write_json(base / 'posts.json', [])
        common.write_json(base / 'recipient_icp.json', recipient or {
            'titles': ['VP Sales'], 'industries': ['Software Development'], 'geos': ['United States'], 'size_range': '50-500'})
        common.write_csv(base / 'ranked_engagers.csv', [{'actor_id': e['id'], 'icp_fit': f}
            for e, f in zip(people, fits or [4] * len(people))], ['actor_id', 'icp_fit'])
        ledger.ingest(self.con, base)
        return base

    def data(self, **changes):
        return {'current_title': 'VP Sales', 'current_company_name': 'Acme',
                'current_company_industry': 'Software Development', 'employee_count_range': '51 to 200',
                'current_company_website': 'acme.example', 'contact_country': 'United States',
                'work_email': 'discard@acme.example', 'cellphone': 'discard-phone', **changes}

    def normalized(self, **changes):
        return getleads.normalize(self.data(**changes))

    def invoke(self, base, **kwargs):
        return getleads.enrich_harvest(base, **kwargs)

    def score(self, base, fit=4):
        people = json.loads((base / 'engagers.json').read_text())
        recipient = json.loads((base / 'recipient_icp.json').read_text())
        scores = base / 'scores.json'
        common.write_json(scores, {'results': [
            {'i': i, 'icp_fit': fit, 'intent': None, 'clean_title': 'VP Sales',
             'qualification_reason': 'Sales buyer evidence assessed; unknown criteria require review.',
             'intent_reason': 'No comment intent.', 'score_context': ledger.score_context(e, recipient, rank.SYSTEM)}
            for i, e in enumerate(people)]})
        with patch('sys.argv', ['rank.py', '--slug', base.name, '--output-dir', str(self.root), '--scores', str(scores)]):
            rank.main()
        return scores

    def test_batch_contract_correlates_reordered_results_and_discards_email(self):
        urls = [self.person(i)['linkedinUrl'] for i in (1, 2)]
        payload = {'ok': True, 'results': [
            {'linkedinUrl': urls[1], 'success': False, 'data': None},
            {'linkedinUrl': urls[0], 'success': True, 'data': self.data(linkedin_url=urls[0])}]}
        with patch.object(getleads, 'request', return_value=payload) as req:
            result = getleads.enrich_batch(urls, 'offline')
        self.assertEqual(result[urls[1]], ('not_found', {}))
        self.assertEqual(result[urls[0]][1]['company_size'], '51 to 200')
        self.assertNotIn('discard', json.dumps(result))
        self.assertEqual(req.call_args.kwargs['json']['items'], [{'linkedin_url': u} for u in urls])

    def test_ambiguous_or_wrong_person_response_never_attaches_evidence(self):
        url = self.person()['linkedinUrl']
        good = {'linkedinUrl': url, 'success': True, 'data': self.data(linkedin_url=url)}
        cases = [[], [good, good], [{**good, 'linkedinUrl': self.person(2)['linkedinUrl']}],
                 [{**good, 'data': self.data(linkedin_url=self.person(2)['linkedinUrl'])}],
                 [{**good, 'success': False}], [{**good, 'data': self.data()}]]
        for results in cases:
            with self.subTest(results=results), patch.object(getleads, 'request', return_value={'results': results}):
                with self.assertRaises(getleads.ProviderError) as err:
                    getleads.enrich_batch([url], 'offline')
                self.assertTrue(err.exception.unknown)

    def test_http_errors_are_redacted_and_never_retry_a_submission(self):
        with patch.object(requests, 'request', side_effect=requests.Timeout('offline-secret')) as req:
            with self.assertRaises(getleads.ProviderError) as err:
                getleads.request('POST', '/enrich/from-linkedin', 'offline-secret')
        self.assertTrue(err.exception.unknown)
        self.assertNotIn('offline-secret', str(err.exception))
        self.assertEqual(req.call_count, 1)
        for status in (401, 402, 429, 500):
            with self.subTest(status=status), patch.object(requests, 'request', return_value=Mock(status_code=status)):
                with self.assertRaises(getleads.ProviderError) as err:
                    getleads.request('POST', '/enrich/from-linkedin', 'offline-secret')
                self.assertEqual(err.exception.unknown, status >= 500)

    def test_account_allowance_distinguishes_credits_and_fair_use(self):
        cases = [({'applies': False, 'credits_remaining': 17}, 17),
                 ({'applies': True, 'daily': {'remaining': 8}, 'monthly': {'remaining': 500}}, 8),
                 ({'applies': True, 'exempt': True}, None)]
        for response, expected in cases:
            with patch.object(getleads, 'request', return_value=response):
                self.assertEqual(getleads.allowance('offline')[0], expected)
        with patch.object(getleads, 'request', return_value={'applies': False}):
            with self.assertRaises(getleads.ProviderError):
                getleads.allowance('offline')

    def test_selection_skips_clear_rejects_hidden_ids_and_recorded_blocks(self):
        people = [self.person(i) for i in range(1, 6)]
        people[2]['linkedinUrl'] = 'https://www.linkedin.com/in/ACoAA3'
        base = self.seed(people=people, fits=[3, 2, 4, 4, 4])
        self.con.execute('INSERT INTO suppressions VALUES (?,?,?)', ('url:' + people[3]['linkedinUrl'], 'customer', ledger.now()))
        self.con.commit()
        ledger.record_campaign(self.con, [(people[4]['linkedinUrl'], None)], 'heyreach:1', 'imported')
        with patch.object(getleads, 'allowance', return_value=(100, 'credits')), \
                patch.object(getleads, 'enrich_batch', return_value={people[0]['linkedinUrl']: ('matched', self.normalized())}) as req:
            self.assertEqual(self.invoke(base), ([0], False))
        self.assertEqual(req.call_args.args[0], [people[0]['linkedinUrl']])
        updated = json.loads((base / 'engagers.json').read_text())
        self.assertEqual([e['getleads_status'] for e in updated],
                         ['matched', 'below_min_fit', 'missing_public_url', 'suppressed:customer', 'in_campaign:heyreach:1'])

    def test_dry_run_and_complete_evidence_spend_nothing(self):
        base = self.seed()
        original = (base / 'engagers.json').read_bytes()
        with patch.object(getleads, 'request') as req:
            self.assertEqual(self.invoke(base, dry_run=True), ([], False))
        req.assert_not_called()
        self.assertEqual((base / 'engagers.json').read_bytes(), original)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM enrichment_attempts').fetchone()[0], 0)
        complete = self.seed('complete', recipient={'titles': ['VP Sales']})
        with patch.object(getleads, 'request') as req:
            self.invoke(complete)
        req.assert_not_called()

    def test_matches_and_misses_reuse_across_runs_and_preserve_new_scraped_employer(self):
        people = [self.person(), self.person(2)]
        first = self.seed(people=people)
        with patch.object(getleads, 'allowance', return_value=(10, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                people[0]['linkedinUrl']: ('matched', self.normalized()), people[1]['linkedinUrl']: ('not_found', {})}) as req:
            self.invoke(first)
            second = self.seed('second', people=[self.person(company='New Employer'), self.person(2)])
            self.invoke(second)
            self.assertEqual(req.call_count, 1)
        loaded = json.loads((second / 'engagers.json').read_text())
        self.assertEqual(loaded[0]['company'], 'New Employer')
        self.assertEqual(loaded[0]['icp_enrichment']['company'], 'Acme')
        self.assertEqual(loaded[1]['getleads_status'], 'not_found')
        self.assertIn('GetLeads database evidence', rank.engager_lines(loaded)[0])
        self.assertIn('conflicts', rank.SYSTEM)

    def test_new_evidence_invalidates_old_exports_and_appears_in_final_csv(self):
        base = self.seed()
        scores = self.score(base)
        ledger.gate(self.con, base)
        old_rows, _ = common.read_csv(base / 'ranked_engagers.csv')
        with patch.object(getleads, 'allowance', return_value=(10, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                self.person()['linkedinUrl']: ('matched', self.normalized())}):
            self.invoke(base)
        self.assertEqual(ledger.filter_export_rows(self.con, old_rows), ([], 1))
        with patch('sys.argv', ['rank.py', '--slug', base.name, '--output-dir', str(self.root), '--scores', str(scores)]):
            with self.assertRaisesRegex(SystemExit, 'Stale or unbound'):
                rank.main()
        self.score(base)
        ledger.gate(self.con, base)
        rows, _ = common.read_csv(base / 'ranked_engagers.csv')
        self.assertEqual(rows[0]['getleads_company_size'], '51 to 200')
        self.assertEqual(rows[0]['getleads_company_industry'], 'Software Development')
        self.assertIn('GetLeads ICP evidence', '\n'.join(summary.summarize(rows)))
        self.assertEqual(ledger.filter_export_rows(self.con, rows), (rows, 0))
        self.assertNotIn('discard', json.dumps(rows))

    def test_persistent_submission_cap_is_shared_by_retries_and_dedupes_urls(self):
        people = [self.person(), self.person(2), self.person(3, linkedinUrl=self.person()['linkedinUrl'])]
        base = self.seed(people=people)
        with patch.object(getleads, 'allowance', return_value=(100, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                people[0]['linkedinUrl']: ('matched', self.normalized())}) as req:
            self.invoke(base, max_lookups=1)
            self.invoke(base, max_lookups=1)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM enrichment_attempts').fetchone()[0], 1)
        loaded = json.loads((base / 'engagers.json').read_text())
        self.assertEqual(loaded[1]['getleads_status'], 'lookup_limit_reached')
        self.assertEqual(loaded[2]['icp_enrichment']['company'], 'Acme')

    def test_partial_failure_retains_paid_results_and_holds_unknown_retries(self):
        people = [self.person(), self.person(2)]
        base = self.seed(people=people)
        with patch.object(getleads, 'BATCH_SIZE', 1), patch.object(getleads, 'allowance', return_value=(100, 'credits')), \
                patch.object(getleads, 'enrich_batch', side_effect=[{people[0]['linkedinUrl']: ('matched', self.normalized())},
                    getleads.ProviderError('unknown', unknown=True)]) as req:
            self.assertEqual(self.invoke(base), ([0], True))
            self.assertEqual(self.invoke(base), ([], True))
        self.assertEqual(req.call_count, 2)
        loaded = json.loads((base / 'engagers.json').read_text())
        self.assertEqual(loaded[0]['icp_enrichment']['company'], 'Acme')
        with patch.object(getleads, 'allowance', return_value=(100, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                people[1]['linkedinUrl']: ('not_found', {})}) as req:
            self.assertEqual(self.invoke(base, retry_unknown=True), ([], False))
            self.assertEqual(req.call_count, 1)

    def test_insufficient_allowance_submits_nothing(self):
        base = self.seed()
        with patch.object(getleads, 'allowance', return_value=(0, 'credits')), patch.object(getleads, 'enrich_batch') as req:
            with self.assertRaises(getleads.ProviderError):
                self.invoke(base)
        req.assert_not_called()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM enrichment_attempts').fetchone()[0], 0)

    def test_atomic_reservation_blocks_another_run_from_repeating_pending_submissions(self):
        url = self.person()['linkedinUrl']
        first, _, _ = getleads.reserve(self.con, [url], 'one', 1, 30, False)
        self.assertEqual(len(first), 1)
        with ledger.connect() as other:
            attempts, reused, held = getleads.reserve(other, [url], 'two', 100, 30, False)
        self.assertEqual((attempts, reused, held), ({}, {}, [url]))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM enrichment_attempts').fetchone()[0], 1)

    def test_completed_miss_expires_and_timestamp_alone_does_not_change_score_evidence(self):
        base = self.seed()
        url = self.person()['linkedinUrl']
        expired = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.con.execute('INSERT INTO provider_enrichments VALUES (?,?,?,?,?)', ('getleads', url, 'not_found', '{}', expired))
        self.con.commit()
        with patch.object(getleads, 'allowance', return_value=(10, 'credits')), patch.object(getleads, 'enrich_batch', return_value={
                url: ('matched', self.normalized())}) as req:
            self.invoke(base)
        self.assertEqual(req.call_count, 1)
        e = json.loads((base / 'engagers.json').read_text())[0]
        before = ledger.evidence_hash(e)
        e['icp_enrichment']['checked_at'] = 'later'
        self.assertEqual(before, ledger.evidence_hash(e))
        e['icp_enrichment']['company_size'] = '10001+'
        self.assertNotEqual(before, ledger.evidence_hash(e))

    def test_pipeline_requires_rescore_then_reuses_paid_enrichment_without_new_calls(self):
        base = self.seed()
        manual = self.root / 'icp.yaml'
        manual.write_text('titles: [VP Sales]\nindustries: [Software Development]\ngeos: [United States]\ncompany_size: {min: 50, max: 500}\n')
        icp.derive_from_yaml(str(manual), base.name, base)
        scores = self.score(base)
        modules = {'src/ledger.py': ledger, 'src/icp.py': icp, 'src/rank.py': rank,
                   'src/getleads.py': getleads, 'src/summary.py': summary}
        def execute(label, cmd):
            with patch('sys.argv', cmd[1:]):
                modules[cmd[1]].main()
        args = ['run.py', '--slug', base.name, '--output-dir', str(self.root), '--skip-harvest',
                '--no-resolve', '--icp', str(manual), '--scores', str(scores), '--getleads']
        with patch.object(pipeline, 'run_step', side_effect=execute), patch.object(getleads, 'allowance', return_value=(10, 'credits')), \
                patch.object(getleads, 'enrich_batch', return_value={self.person()['linkedinUrl']: ('matched', self.normalized())}) as req:
            with patch('sys.argv', args), self.assertRaisesRegex(SystemExit, 'need judgments'):
                pipeline.main()
            self.score(base)
            with patch('sys.argv', args):
                pipeline.main()
        self.assertEqual(req.call_count, 1)
        self.assertEqual(common.read_csv(base / 'ranked_engagers.csv')[0][0]['ledger_status'], 'eligible')


if __name__ == '__main__':
    unittest.main()
