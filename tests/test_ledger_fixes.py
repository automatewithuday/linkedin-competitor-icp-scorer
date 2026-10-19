"""Regression coverage for evidence provenance, delayed exports and import history."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run as pipeline
from src import common, icp, ledger, push, push_heyreach, rank, resolve, summary


class LedgerFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {'LEDGER_DB': str(self.root / 'ledger.sqlite'),
                                                 'APIFY_TOKEN': 'offline-dummy',
                                                 'SMARTLEAD_API_KEY': 'offline-dummy',
                                                 'HEYREACH_API_KEY': 'offline-dummy'}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.con = ledger.connect()
        self.addCleanup(self.con.close)

    def person(self, **changes):
        return {'id': 'ACoAA1', 'name': 'Jane', 'position': 'VP Sales',
                'linkedinUrl': 'https://www.linkedin.com/in/jane', 'location': 'United States', **changes}

    def seed(self, slug, people):
        base = self.root / slug
        base.mkdir()
        common.write_json(base / 'engagers.json', people)
        common.write_json(base / 'recipient_icp.json', {'titles': ['VP Sales'], 'geos': ['United States']})
        common.write_json(base / 'posts.json', [])
        common.write_csv(base / 'ranked_engagers.csv', [{'actor_id': e['id'], 'icp_fit': 4} for e in people], ['actor_id', 'icp_fit'])
        return base

    def submit(self, base, results):
        es = json.loads((base / 'engagers.json').read_text())
        recipient = json.loads((base / 'recipient_icp.json').read_text())
        path = base / 'scores.json'
        common.write_json(path, {'results': [{'qualification_reason': 'Role evidence assessed against the ICP.',
                                                        'intent_reason': 'Intent assessed from supplied engagement evidence.', **r, 'score_context': ledger.score_context(es[r['i']], recipient, rank.SYSTEM)}
                                            for r in results]})
        return path

    def rank(self, base, scores=None, dump=False):
        args = ['rank.py', '--slug', base.name, '--output-dir', str(self.root)]
        args += ['--dump'] if dump else ['--scores', str(scores)]
        out = io.StringIO()
        with patch('sys.argv', args), contextlib.redirect_stdout(out):
            rank.main()
        return out.getvalue()

    def scored_rows(self):
        base = self.seed('scored', [self.person()])
        ledger.ingest(self.con, base)
        self.rank(base, self.submit(base, [{'i': 0, 'icp_fit': 4, 'intent': None}]))
        ledger.gate(self.con, base)
        return base, common.read_csv(base / 'ranked_engagers.csv')[0]

    def test_partial_failure_retry_requires_current_judgment_and_completes_safely(self):
        es = [self.person(linkedinUrl='', location=''),
              self.person(id='ACoAA2', name='Bob', linkedinUrl='', location='')]
        base = self.seed('retry', es)
        yaml = self.root / 'icp.yaml'
        yaml.write_text('titles: [VP Sales]\ngeos: [United States]\ndisqualifiers: [Outside the United States]\n')
        icp.derive_from_yaml(str(yaml), base.name, base)
        scores = self.submit(base, [{'i': i, 'icp_fit': 4, 'intent': None} for i in range(2)])
        modules = {'src/icp.py': icp, 'src/ledger.py': ledger, 'src/rank.py': rank,
                   'src/resolve.py': resolve, 'src/summary.py': summary}
        def execute(label, cmd):
            with patch('sys.argv', cmd[1:]):
                modules[cmd[1]].main()
        args = ['run.py', '--slug', base.name, '--output-dir', str(self.root), '--skip-harvest',
                '--icp', str(yaml), '--scores', str(scores), '--resolve-batch', '1']
        def invoke():
            with patch('sys.argv', args):
                pipeline.main()
        a = {'id': 'ACoAA1', 'publicIdentifier': 'jane', 'location': 'India'}
        b = {'id': 'ACoAA2', 'publicIdentifier': 'bob', 'location': 'United States'}
        with patch.object(pipeline, 'run_step', side_effect=execute), patch.object(resolve, 'scrape', side_effect=[
                ({'ACoAA1': a}, True, 'one'), RuntimeError('network'), ({'ACoAA2': b}, True, 'two')]) as scrape:
            with self.assertRaises(SystemExit) as first:
                invoke()
            self.assertEqual(first.exception.code, 3)
            with self.assertRaisesRegex(SystemExit, 'Stale or unbound'):
                invoke()
            self.assertEqual(scrape.call_count, 2)
            self.assertIn('0. Jane', self.rank(base, dump=True))
            self.submit(base, [{'i': 0, 'icp_fit': 1, 'intent': None}])
            with self.assertRaisesRegex(SystemExit, 'need judgments'):
                invoke()
            self.assertIn('1. Bob', self.rank(base, dump=True))
            self.assertNotIn('0. Jane', self.rank(base, dump=True))
            self.submit(base, [{'i': 1, 'icp_fit': 4, 'intent': None}])
            invoke()
            self.assertEqual(scrape.call_count, 3)
        rows = common.read_csv(base / 'ranked_engagers.csv')[0]
        jane = next(r for r in rows if r['name'] == 'Jane')
        self.assertEqual((jane['location'], jane['icp_fit']), ('India', '1'))
        self.assertEqual([l['firstName'] for l in push_heyreach.select_leads(rows)[0]], ['Bob'])

    def test_bound_submission_rejects_changed_identity_evidence_icp_and_rubric(self):
        for change in ('identity', 'evidence', 'icp', 'rubric', 'missing'):
            with self.subTest(change=change):
                base = self.seed(change, [self.person()])
                path = self.submit(base, [{'i': 0, 'icp_fit': 4, 'intent': None}])
                if change in ('identity', 'evidence'):
                    e = self.person(**({'id': 'ACoAA2'} if change == 'identity' else {'location': 'India'}))
                    common.write_json(base / 'engagers.json', [e])
                elif change == 'icp':
                    common.write_json(base / 'recipient_icp.json', {'titles': ['Student']})
                elif change == 'missing':
                    common.write_json(path, {'results': [{'i': 0, 'icp_fit': 4}]})
                with patch.object(rank, 'SYSTEM', rank.SYSTEM + ('changed' if change == 'rubric' else '')):
                    with self.assertRaisesRegex(SystemExit, 'Stale or unbound'):
                        self.rank(base, path)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM scores').fetchone()[0], 0)

    def test_dump_exposes_context_and_old_file_cannot_hide_pending_rows(self):
        base = self.seed('dump', [self.person()])
        path = self.submit(base, [{'i': 0, 'icp_fit': 4, 'intent': None}])
        self.rank(base, path)
        common.write_json(base / 'engagers.json', [self.person(location='India')])
        with self.assertRaises(SystemExit):
            self.rank(base, path)
        dump = self.rank(base, dump=True)
        self.assertIn('0. Jane', dump)
        self.assertIn('score_context: ', dump)

    def test_new_harvest_preserves_known_profile_and_reuses_lookup_without_spend(self):
        raw = self.person(linkedinUrl='', location='')
        first = self.seed('first', [raw])
        ledger.ingest(self.con, first)
        hit = {'id': 'ACoAA1', 'publicIdentifier': 'jane', 'location': 'India', 'currentPosition': [{'companyName': 'Acme'}]}
        with patch.object(resolve, 'scrape', return_value=({'ACoAA1': hit}, True, 'lookup')):
            resolve.enrich_harvest(first, 3)
        second = self.seed('second', [raw])
        ledger.ingest(self.con, second)
        with patch.object(resolve, 'scrape') as scrape:
            resolve.enrich_harvest(second, 3)
        scrape.assert_not_called()
        e = json.loads((second / 'engagers.json').read_text())[0]
        self.assertEqual((e['linkedinUrl'], e['location'], e['company']), ('https://www.linkedin.com/in/jane', 'India', 'Acme'))
        third = self.seed('third', [self.person(location='Canada', company='Beta')])
        ledger.ingest(self.con, third)
        self.assertEqual(ledger.cached_lookup(self.con, 'ACoAA1')['location'], 'Canada')
        self.assertEqual(ledger.cached_lookup(self.con, 'ACoAA1')['company'], 'Beta')

    def test_legacy_blank_snapshots_do_not_hide_nonblank_lookup_fields(self):
        e = self.person(location='India', company='Acme')
        ledger.record_lookup(self.con, e['id'], 'resolved', e['linkedinUrl'], 'old', e)
        cid = ledger.find_contact(self.con, ledger.aliases_of(e))
        ledger.snapshot(self.con, cid, self.person(location='', company=''), 'harvest')
        self.con.commit()
        cached = ledger.cached_lookup(self.con, e['id'])
        self.assertEqual((cached['location'], cached['company']), ('India', 'Acme'))

    def test_delayed_csv_is_revalidated_by_both_import_commands(self):
        base, rows = self.scored_rows()
        self.assertEqual(ledger.filter_export_rows(self.con, rows), (rows, 0))
        later = self.seed('later', [self.person(location='India')])
        ledger.ingest(self.con, later)
        self.assertEqual(ledger.filter_export_rows(self.con, rows), ([], 1))
        row = {**rows[0], 'email': 'jane@acme.com', 'email_status': 'VERIFIED', 'sendable': 'true', 'email_checked_at': ledger.now()}
        source = base / 'enriched.csv'
        common.write_csv(source, [row], list(row))
        for module, args in ((push, ['--campaign-id', '1']), (push_heyreach, ['--list-id', '1'])):
            with self.subTest(module=module.__name__), patch.object(module, 'post_json') as post, patch('sys.argv', [
                    'push.py', '--input', str(source), '--execute', *args]):
                module.main()
                post.assert_not_called()

    def test_export_provenance_rejects_missing_metadata_corrected_score_and_rubric(self):
        base, rows = self.scored_rows()
        for key in ledger.EXPORT_FIELDS:
            invalid = {k: v for k, v in rows[0].items() if k != key}
            self.assertEqual(ledger.filter_export_rows(self.con, [invalid]), ([], 1))
        self.assertEqual(ledger.filter_export_rows(self.con, [{**rows[0], 'icp_fit': '5'}]), ([], 1))
        with patch.object(rank, 'SYSTEM', rank.SYSTEM + 'new'):
            self.assertEqual(ledger.filter_export_rows(self.con, rows), ([], 1))
        self.rank(base, self.submit(base, [{'i': 0, 'icp_fit': 1, 'intent': None}]))
        self.assertEqual(ledger.filter_export_rows(self.con, rows), ([], 1))

    def test_valid_export_previews_still_include_leads(self):
        base, rows = self.scored_rows()
        row = {**rows[0], 'email': 'jane@acme.com', 'email_status': 'VERIFIED', 'sendable': 'true', 'email_checked_at': ledger.now()}
        source = base / 'enriched.csv'
        common.write_csv(source, [row], list(row))
        for module, args, suffix in ((push, ['--campaign-id', '1'], '.smartlead.json'),
                                     (push_heyreach, ['--list-id', '1'], '.heyreach.json')):
            with patch('sys.argv', ['push.py', '--input', str(source), *args]), patch.object(module, 'post_json') as post:
                module.main()
                post.assert_not_called()
            self.assertEqual(json.loads(Path(str(source) + suffix).read_text())['eligible'], 1)

    def test_changed_run_icp_and_legacy_cached_scores_require_review(self):
        base, rows = self.scored_rows()
        common.write_json(base / 'recipient_icp.json', {'titles': ['Student']})
        self.assertEqual(ledger.filter_export_rows(self.con, rows, base), ([], 1))
        self.con.execute('UPDATE scores SET rubric_hash=?', (ledger.digest(rank.SYSTEM),))
        self.con.commit()
        recipient = {'titles': ['VP Sales'], 'geos': ['United States']}
        self.assertEqual(ledger.fresh_scores(self.con, [self.person()], recipient, rank.SYSTEM), {})

    def test_live_execute_keeps_internal_provenance_out_of_provider_payload(self):
        base, rows = self.scored_rows()
        source = base / 'ranked_engagers.csv'
        response = {'addedLeadsCount': 1, 'updatedLeadsCount': 0, 'failedLeadsCount': 0}
        with patch.object(push_heyreach, 'post_json', return_value=response) as post, patch('sys.argv', [
                'push_heyreach.py', '--input', str(source), '--list-id', '1', '--execute']):
            push_heyreach.main()
        self.assertEqual(post.call_count, 1)
        self.assertNotIn('ledger_', json.dumps(post.call_args.kwargs['json']))
        self.assertIsNotNone(ledger.blocked(self.con, self.person()['linkedinUrl']))

    def test_evidence_change_between_batches_stops_before_second_provider_request(self):
        es = [self.person(), self.person(id='ACoAA2', name='Bob', linkedinUrl='https://www.linkedin.com/in/bob')]
        base = self.seed('batches', es)
        ledger.ingest(self.con, base)
        self.rank(base, self.submit(base, [{'i': i, 'icp_fit': 4, 'intent': None} for i in range(2)]))
        ledger.gate(self.con, base)
        def first_request(*args, **kwargs):
            later = self.seed('changed-between-requests', [{**es[1], 'location': 'India'}])
            ledger.ingest(self.con, later)
            return {'addedLeadsCount': 1, 'updatedLeadsCount': 0, 'failedLeadsCount': 0}
        with patch.object(push_heyreach, 'BATCH', 1), patch.object(push_heyreach, 'post_json', side_effect=first_request) as post, \
                patch('sys.argv', ['push_heyreach.py', '--input', str(base / 'ranked_engagers.csv'), '--list-id', '1', '--execute']):
            with self.assertRaisesRegex(SystemExit, 'eligibility changed'):
                push_heyreach.main()
        self.assertEqual(post.call_count, 1)
        self.assertIsNotNone(ledger.blocked(self.con, es[0]['linkedinUrl']))
        self.assertIsNone(ledger.blocked(self.con, es[1]['linkedinUrl']))

    def test_opaque_import_is_recorded_and_blocks_vanity_alias_after_resolving(self):
        url = 'https://www.linkedin.com/in/ACoAA1'
        receipt = self.root / 'opaque.json'
        leads = [{'profileUrl': url}]
        common.write_json(receipt, {'batches': [{'offset': 0, 'requested': 1, 'status': 'confirmed', 'failedLeadsCount': 0}]})
        self.assertEqual(ledger.record_receipt(self.con, leads, receipt, 'heyreach:list:1', 'profileUrl', 'emailAddress'), 1)
        self.assertEqual(ledger.filter_blocked(self.con, leads, 'profileUrl', 'emailAddress'), ([], 1))
        ledger.contact_for(self.con, ledger.aliases_of(self.person()))
        self.assertEqual(ledger.blocked(self.con, self.person()['linkedinUrl']), 'in_campaign:heyreach:list:1')
        self.assertEqual(ledger.aliases_of(url='https://linkedin.com.evil.com/in/ACoAA1'), [])

    def test_mixed_and_failed_imports_have_distinct_recoverable_states(self):
        leads = [{'profileUrl': 'https://www.linkedin.com/in/jane'}, {'profileUrl': 'https://www.linkedin.com/in/bob'}]
        receipt = self.root / 'mixed.json'
        with patch.object(push_heyreach, 'post_json', return_value={'addedLeadsCount': 1, 'updatedLeadsCount': 0, 'failedLeadsCount': 1}):
            push_heyreach.push_batches(leads, {'listId': 1}, 'dummy', receipt)
        self.assertEqual(ledger.record_receipt(self.con, leads, receipt, 'heyreach:list:1', 'profileUrl', 'emailAddress'), 0)
        self.assertEqual([r[0] for r in self.con.execute('SELECT status FROM campaign_contacts')], ['needs_reconciliation'] * 2)
        ledger.record_campaign(self.con, [(leads[0]['profileUrl'], None)], 'heyreach:list:1', 'imported')
        ledger.record_campaign(self.con, [(leads[1]['profileUrl'], None)], 'heyreach:list:1', 'failed', resolve=True)
        self.assertIsNotNone(ledger.blocked(self.con, leads[0]['profileUrl']))
        self.assertIsNone(ledger.blocked(self.con, leads[1]['profileUrl']))
        common.write_json(receipt, {'batches': [{'offset': 0, 'requested': 1, 'status': 'confirmed', 'failedLeadsCount': 1}]})
        self.assertEqual(ledger.record_receipt(self.con, leads[1:], receipt, 'heyreach:list:2', 'profileUrl', 'emailAddress'), 0)
        self.assertIsNone(ledger.blocked(self.con, leads[1]['profileUrl']))

    def test_smartlead_skips_are_reconciled_and_email_only_history_can_be_resolved(self):
        receipt = self.root / 'email.json'
        leads = [{'email': 'jane@example.com'}]
        common.write_json(receipt, {'batches': [{'offset': 0, 'requested': 1, 'status': 'confirmed', 'skipped': 1}]})
        self.assertEqual(ledger.record_receipt(self.con, leads, receipt, 'smartlead:1', 'linkedin_profile', 'email'), 0)
        self.assertIsNotNone(ledger.blocked(self.con, email='JANE@example.com'))
        ledger.record_campaign(self.con, [(None, 'jane@example.com')], 'smartlead:1', 'failed', resolve=True)
        self.assertIsNone(ledger.blocked(self.con, email='jane@example.com'))

    def test_alias_merge_preserves_strongest_campaign_status_and_both_receipts(self):
        for index, status in enumerate(('imported', 'contacted', 'replied', 'excluded', 'needs_reconciliation')):
            with self.subTest(status=status):
                old, new = f'https://www.linkedin.com/in/old{index}', f'https://www.linkedin.com/in/new{index}'
                opaque = f'ACoAA{index}'
                ledger.contact_for(self.con, ledger.aliases_of(url=old, actor_id=opaque))
                ledger.record_campaign(self.con, [(old, None)], 'heyreach:list:1', 'unconfirmed', 'first.json')
                ledger.record_campaign(self.con, [(new, None)], 'heyreach:list:1', status, 'second.json')
                ledger.contact_for(self.con, ledger.aliases_of(url=new, actor_id=opaque))
                self.con.commit()
                cid = ledger.find_contact(self.con, ledger.aliases_of(url=new))
                row = self.con.execute('SELECT status, receipt FROM campaign_contacts WHERE contact_id=?', (cid,)).fetchone()
                self.assertEqual(row['status'], status)
                self.assertEqual(set(json.loads(row['receipt'])), {'first.json', 'second.json'})
                self.assertIsNotNone(ledger.blocked(self.con, new))

    def test_alias_merge_cannot_treat_an_unrelated_failed_record_as_reconciliation(self):
        old, new = 'https://www.linkedin.com/in/old', 'https://www.linkedin.com/in/new'
        ledger.contact_for(self.con, ledger.aliases_of(url=old, actor_id='ACoAA1'))
        ledger.record_campaign(self.con, [(old, None)], 'heyreach:list:1', 'needs_reconciliation')
        ledger.record_campaign(self.con, [(new, None)], 'heyreach:list:1', 'failed')
        ledger.contact_for(self.con, ledger.aliases_of(url=new, actor_id='ACoAA1'))
        self.assertIsNotNone(ledger.blocked(self.con, new))


if __name__ == '__main__':
    unittest.main()
