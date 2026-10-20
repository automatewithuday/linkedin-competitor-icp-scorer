"""Decision explanations survive scoring, caching, ledger checks and downstream CSVs."""
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import activity, common, draft, email_enrich, ledger, rank, summary


class DecisionReasonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {'LEDGER_DB': str(self.root / 'ledger.sqlite'),
                                                 'LLM_PROVIDER': 'openai', 'OPENAI_API_KEY': 'offline-dummy',
                                                 'GETLEADS_API_KEY': 'offline-dummy', 'CLAUDECODE': ''}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.con = ledger.connect()
        self.addCleanup(self.con.close)

    def person(self, **updates):
        return {'id': 'ACoAA1', 'name': 'Jane', 'position': 'VP Sales at Acme', 'company': 'Acme',
                'linkedinUrl': 'https://www.linkedin.com/in/jane', 'location': 'United States',
                'comments': ['We are evaluating tools to fix our outbound handoff.'], **updates}

    def result(self, **updates):
        return {'i': 0, 'icp_fit': 4, 'intent': 'high', 'evidence_comment': 1,
                'clean_title': 'VP Sales', 'company_guess': 'Acme',
                'needs_review': False, 'qualification_reason': 'Sales leadership at a named employer in a target country matches a plausible buyer; industry and headcount remain unverified.',
                'intent_reason': 'The comment states a current tool evaluation and a specific outbound problem.', **updates}

    def seed(self, people=None):
        base = self.root / 'run'; base.mkdir()
        common.write_json(base / 'engagers.json', people or [self.person()])
        common.write_json(base / 'recipient_icp.json', {'titles': ['VP Sales'], 'geos': ['United States']})
        return base

    def score_file(self, base, results):
        people = json.loads((base / 'engagers.json').read_text())
        icp = json.loads((base / 'recipient_icp.json').read_text())
        path = base / 'scores.json'
        common.write_json(path, {'results': [{**r, 'score_context': ledger.score_context(people[r['i']], icp, rank.SYSTEM)}
                                            for r in results]})
        return path

    def run_rank(self, base, scores=None, min_fit=4):
        args = ['rank.py', '--legacy', '--slug', base.name, '--output-dir', str(self.root), '--min-fit', str(min_fit)]
        if scores:
            args += ['--scores', str(scores)]
        with patch('sys.argv', args):
            rank.main()
        return common.read_csv(base / 'ranked_engagers.csv')[0]

    def test_automatic_and_in_session_scoring_export_accepted_and_rejected_reasons(self):
        es = [self.person(), self.person(id='ACoAA2', name='Sam', position='Growth Consultant', company='',
                                       linkedinUrl='https://www.linkedin.com/in/sam')]
        base = self.seed(es)
        rejected = self.result(i=1, icp_fit=2, clean_title='Growth Consultant', company_guess=None,
                               qualification_reason='Consulting provider rather than the in-house buyer specified in this ICP.')
        results = [self.result(), rejected]
        with patch.object(rank, 'llm_json', return_value=({'results': results}, 1, 1)) as llm:
            rows = self.run_rank(base)
        self.assertEqual(llm.call_count, 1)
        self.assertIn('qualification_reason', llm.call_args.args[0])
        self.assertEqual([r['qualification_status'] for r in rows], ['qualified', 'not_qualified'])
        self.assertEqual([r['qualification_reason'] for r in rows], [r['qualification_reason'] for r in results])
        with patch.object(rank, 'score_batch') as scorer:
            reused = self.run_rank(base, self.score_file(base, []))
        scorer.assert_not_called()
        self.assertEqual([(r['qualification_reason'], r['intent_reason']) for r in reused],
                         [(r['qualification_reason'], r['intent_reason']) for r in rows])
        # Explicit in-session correction is persisted, including the new explanation.
        corrected = {**rejected, 'qualification_reason': 'Observed consulting role is explicitly excluded by this ICP.'}
        rows = self.run_rank(base, self.score_file(base, [corrected]))
        self.assertEqual(rows[1]['qualification_reason'], corrected['qualification_reason'])

    def test_missing_blank_or_nontext_reasons_are_rejected_for_every_scorer(self):
        for field in ('qualification_reason', 'intent_reason'):
            for value in (None, '', '  ', 42):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                    rank.align({'results': [self.result(**{field: value})]}, 1)
        base = self.seed()
        bad = self.result(qualification_reason='')
        with patch.object(rank, 'llm_json', return_value=({'results': [bad]}, 0, 0)), self.assertRaises(ValueError):
            self.run_rank(base)
        with self.assertRaises(ValueError):
            self.run_rank(base, self.score_file(base, [bad]))
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM scores').fetchone()[0], 0)

    def test_threshold_change_reuses_reasons_and_recomputes_qualification_status(self):
        base = self.seed()
        original = self.run_rank(base, self.score_file(base, [self.result()]))[0]
        changed = self.run_rank(base, self.score_file(base, []), min_fit=5)[0]
        self.assertEqual((original['qualification_status'], changed['qualification_status']), ('qualified', 'not_qualified'))
        self.assertEqual((original['qualification_threshold'], changed['qualification_threshold']), ('4', '5'))
        self.assertEqual(original['qualification_reason'], changed['qualification_reason'])
        self.assertEqual(original['intent_reason'], changed['intent_reason'])

    def test_reaction_only_intent_explanation_cannot_claim_a_buying_signal(self):
        base = self.seed([self.person(comments=[], reaction_types=['LIKE'])])
        row = self.run_rank(base, self.score_file(base, [self.result(intent_reason='Claims a strong buying signal.')]))[0]
        self.assertEqual(row['intent'], 'low')
        self.assertEqual(row['intent_reason'], 'Reaction-only engagement; no comment evidence of buying intent.')

    def test_legacy_database_migration_preserves_history_and_requires_explanations(self):
        path = self.root / 'legacy.sqlite'
        old = sqlite3.connect(path)
        old.executescript(ledger.SCHEMA.replace("qualification_reason TEXT NOT NULL DEFAULT '', intent_reason TEXT NOT NULL DEFAULT '',", ''))
        e, icp = self.person(), {'titles': ['VP Sales']}
        old.execute('INSERT INTO contacts VALUES (1,?,?,?,?,?)', ('Jane', ledger.evidence_hash(e), 'old', 'then', 'then'))
        old.execute('INSERT INTO aliases VALUES (?,1)', ('id:ACoAA1',))
        old.execute('INSERT INTO icp_versions VALUES (1,?,?,?)', (ledger.icp_hash(icp), json.dumps(icp), 'then'))
        old.execute('INSERT INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (1, 1, 4, 'high', 1, 'VP Sales', 'Acme', ledger.evidence_hash(e), ledger.rubric_hash(rank.SYSTEM), 'in-session', 'old', 'then'))
        old.execute('INSERT INTO lookups VALUES (?,?,?,?,?)', ('ACoAA1', 'resolved', e['linkedinUrl'], 'paid-run', 'then'))
        old.execute('INSERT INTO campaign_contacts VALUES (?,?,?,?,?,?)', (1, 'heyreach:list:1', 'imported', None, 'receipt.json', 'then'))
        old.commit(); old.close()
        migrated = ledger.connect(path)
        self.addCleanup(migrated.close)
        self.assertEqual(migrated.execute('SELECT icp_fit, qualification_reason, intent_reason FROM scores').fetchone()[:], (4, '', ''))
        self.assertEqual(migrated.execute('SELECT run_id FROM lookups').fetchone()[0], 'paid-run')
        self.assertEqual(migrated.execute('SELECT status FROM campaign_contacts').fetchone()[0], 'imported')
        self.assertEqual(ledger.fresh_scores(migrated, [e], icp, rank.SYSTEM), {})
        ledger.record_scores(migrated, [e], [self.result()], icp, rank.SYSTEM, 'in-session', 'new')
        self.assertEqual(ledger.fresh_scores(migrated, [e], icp, rank.SYSTEM)[0]['qualification_reason'], self.result()['qualification_reason'])
        again = ledger.connect(path)
        again.close()  # migration is idempotent

    def test_identity_merge_keeps_explanations_with_the_selected_score(self):
        e = self.person(linkedinUrl='')
        icp = {'titles': ['VP Sales']}
        ledger.record_scores(self.con, [e], [self.result()], icp, rank.SYSTEM, 'in-session', 'first')
        newer = self.result(icp_fit=5, qualification_reason='Verified role evidence is an exact buyer-title match.')
        ledger.record_scores(self.con, [self.person(id='public-only')], [newer], icp, rank.SYSTEM, 'in-session', 'second')
        ledger.contact_for(self.con, ledger.aliases_of(self.person()))
        self.con.commit()
        cached = ledger.fresh_scores(self.con, [self.person()], icp, rank.SYSTEM)[0]
        self.assertEqual((cached['icp_fit'], cached['qualification_reason'], cached['intent_reason']),
                         (5, newer['qualification_reason'], newer['intent_reason']))

    def test_activity_explains_active_stale_skipped_unknown_and_uncertain_cases(self):
        def row(name, **kwargs):
            return {'name': name, 'icp_fit': '4', 'linkedin_url': f'https://www.linkedin.com/in/{name}', **kwargs}
        rows = [row('active'), row('stale'), row('not_qualified', icp_fit='2'), row('opaque', linkedin_url=''),
                row('failed'), row('limited'), row('suppressed', ledger_status='suppressed:customer',
                eligibility_reason='Do not contact: existing customer.'), row('partial'), row('empty')]
        hits = [{'linkedin_url': rows[0]['linkedin_url'], 'activity_status': 'active', 'outreach_reason': 'Dated post 2 days ago supports LinkedIn use.',
                 'linkedin_outreach_ready': 'True', 'outreach_ready_probability': '0.92'},
                {'linkedin_url': rows[1]['linkedin_url'], 'activity_status': 'stale', 'outreach_reason': 'Latest dated comment was 65 days ago.',
                 'linkedin_outreach_ready': 'False', 'outreach_ready_probability': '0.2'},
                {'linkedin_url': rows[4]['linkedin_url'], 'activity_status': '', 'error': 'Do not export raw provider secrets'},
                {'linkedin_url': rows[7]['linkedin_url'], 'activity_status': 'active', 'data_quality_warning': 'True',
                 'outreach_reason': 'A recent post was observed.', 'outreach_ready_probability': '0.7'},
                {'linkedin_url': rows[8]['linkedin_url'], 'activity_status': 'no_activity_observed', 'linkedin_outreach_ready': 'False'}]
        activity.join(rows, hits, 4, 'jev-latest')
        self.assertTrue(all(r['activity_reason'] and r['outreach_reason'] == r['activity_reason'] for r in rows))
        self.assertEqual(rows[0]['activity_model'], 'jev-latest')
        self.assertIn('65 days', rows[1]['activity_reason'])
        self.assertEqual(rows[2]['activity_status'], 'not_checked')
        self.assertIn('below the required 4', rows[2]['activity_reason'])
        self.assertIn('public LinkedIn profile', rows[3]['activity_reason'])
        self.assertEqual(rows[4]['activity_status'], 'unknown')
        self.assertEqual(rows[4]['data_quality_warning'], 'true')
        self.assertNotIn('secrets', rows[4]['activity_reason'])
        self.assertIn('limit', rows[5]['activity_reason'])
        self.assertIn('existing customer', rows[6]['activity_reason'])
        self.assertIn('missing or uncertain', rows[7]['activity_reason'])
        self.assertIn('not established', rows[8]['activity_reason'])
        self.assertEqual(rows[8]['activity_model'], '')

    def test_reasons_survive_activity_email_export_and_summary(self):
        base = self.seed()
        self.run_rank(base, self.score_file(base, [self.result()]))
        ledger.gate(self.con, base)
        config = self.root / 'activity-test.toml'
        config.write_text('[judge]\nmodel = "jev-test-model"\n')
        output = base / 'activity.csv'
        def fake(cmd, **kwargs):
            out = Path(cmd[cmd.index('--out') + 1])
            hit = {'linkedin_url': self.person()['linkedinUrl'], 'activity_status': 'active',
                   'linkedin_outreach_ready': 'True', 'outreach_ready_probability': '0.9',
                   'outreach_reason': 'A dated post 2 days ago supports LinkedIn outreach.'}
            common.write_csv(out / '20261020-000000.csv', [hit], list(hit))
            return SimpleNamespace(returncode=0)
        with patch('sys.argv', ['activity.py', '--input', str(base / 'ranked_engagers.csv'), '--output', str(output), '--config', str(config)]), \
                patch.object(activity.subprocess, 'run', side_effect=fake):
            activity.main()
        enriched = base / 'enriched.csv'
        self.con.execute('INSERT INTO provider_enrichments VALUES (?,?,?,?,?)',
                         ('getleads', self.person()['linkedinUrl'], 'not_found', '{}', ledger.now()))
        self.con.commit()
        with patch('sys.argv', ['email_enrich.py', '--input', str(output), '--output', str(enriched)]), \
                patch.object(email_enrich.getleads, 'request') as request:
            email_enrich.main()
        request.assert_not_called()
        row = common.read_csv(enriched)[0][0]
        self.assertEqual(row['qualification_reason'], self.result()['qualification_reason'])
        self.assertEqual(row['intent_reason'], self.result()['intent_reason'])
        self.assertEqual(row['activity_model'], 'jev-test-model')
        self.assertIn('2 days ago', row['activity_reason'])
        text = '\n'.join(summary.summarize([row]))
        self.assertIn(row['qualification_reason'], text)
        self.assertIn(row['activity_reason'], text)

    def test_eligibility_reasons_explain_suppression_history_and_stale_scores(self):
        for status, phrase in [('eligible', 'Ledger checks passed'), ('suppressed:customer', 'customer'),
                               ('in_campaign:heyreach:list:1', 'heyreach:list:1'), ('stale_score', 'rescore first')]:
            with self.subTest(status=status):
                self.assertIn(phrase, ledger.eligibility_reason(status))

    def test_draft_csv_keeps_the_qualification_and_intent_explanations(self):
        base = self.seed()
        self.run_rank(base, self.score_file(base, [self.result()]))
        ledger.gate(self.con, base)
        with patch('sys.argv', ['draft.py', '--slug', base.name, '--output-dir', str(self.root)]), \
                patch.object(draft, 'llm_json', return_value=({'results': [{'i': 0, 'connection_note': 'A sample note.', 'dm': 'A sample message.'}]}, 0, 0)):
            draft.main()
        row = common.read_csv(base / 'drafts.csv')[0][0]
        self.assertEqual(row['qualification_status'], 'qualified')
        self.assertEqual(row['qualification_reason'], self.result()['qualification_reason'])
        self.assertEqual(row['intent_reason'], self.result()['intent_reason'])
        self.assertTrue(row['eligibility_reason'])

    def test_sample_has_consistent_rows_and_reasons_for_every_decision(self):
        sample = Path(__file__).resolve().parents[1] / 'examples' / 'ranked_engagers.sample.csv'
        rows, fields = common.read_csv(sample)
        self.assertGreaterEqual(len(rows), 5)
        self.assertTrue({'qualified', 'not_qualified', 'needs_review'} <= {r['qualification_status'] for r in rows})
        self.assertTrue({'active', 'stale', 'not_checked', 'unknown'} <= {r['activity_status'] for r in rows})
        for r in rows:
            self.assertNotIn(None, r)
            self.assertFalse(any(value is None for value in r.values()))
            for field in ('qualification_reason', 'intent_reason', 'eligibility_reason', 'activity_reason'):
                self.assertTrue(r[field].strip(), (r['name'], field))
            self.assertEqual(r['qualification_status'] == 'qualified', int(r['icp_fit']) >= 4)
            self.assertEqual(r['activity_reason'], r['outreach_reason'])
            if int(r['icp_fit']) < 4:
                self.assertEqual(r['activity_status'], 'not_checked')
                self.assertEqual(r['activity_model'], '')
            if r['outreach_ready_probability']:
                self.assertEqual(r['activity_model'], 'jev-latest')
            if r['email']:
                self.assertEqual((r['email_provider'], r['email_status'], r['sendable']), ('getleads', 'UNVERIFIED', 'false'))
        self.assertIn('comment_post_url', fields)


if __name__ == '__main__':
    unittest.main()
