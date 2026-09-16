import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import common, email_enrich, harvest, icp, push, rank, supabase_export


def verified_row(**changes):
    row = {'name': 'Jane Doe', 'linkedin_url': 'https://linkedin.com/in/jane',
           'icp_fit': '4', 'email': 'Jane@example.com', 'email_status': 'VERIFIED',
           'sendable': 'true', 'email_checked_at': datetime.now(timezone.utc).isoformat()}
    row.update(changes)
    return row


class IdentityTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(common.linkedin_url('https://uk.linkedin.com/in/Jane/?trk=x'),
                         'https://www.linkedin.com/in/jane')
        for url in ['https://notlinkedin.com/in/jane', 'https://linkedin.com.evil.com/in/jane',
                    'https://linkedin.com/company/acme', 'https://linkedin.com/in/ACoAA123']:
            self.assertEqual(common.linkedin_url(url), '')

    def test_no_guessed_company_match(self):
        self.assertIsNone(email_enrich.identity({'name': 'Jane', 'company_guess': 'Acme'}))
        self.assertEqual(email_enrich.identity({'name': 'Jane Doe', 'domain': 'example.com'}),
                         {'full_name': 'Jane Doe', 'company_website': 'example.com'})

    def test_csv_unicode_and_empty_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'nested' / 'test.csv'
            common.write_csv(path, [{'name': 'Zoë, 李'}], ['name'])
            self.assertEqual(common.read_csv(path)[0][0]['name'], 'Zoë, 李')
            common.write_csv(path, [], ['name'])
            self.assertEqual(common.read_csv(path), ([], ['name']))


class ProspeoTests(unittest.TestCase):
    @patch('src.email_enrich.post_json')
    def test_verified_contract(self, post):
        post.return_value = {'error': False, 'person': {'email': {
            'email': 'jane@example.com', 'status': 'VERIFIED', 'revealed': True}}}
        self.assertEqual(email_enrich.enrich_person({'linkedin_url': 'url'}, 'secret')['sendable'], 'true')
        self.assertTrue(post.call_args.kwargs['json']['only_verified_email'])
        self.assertFalse(post.call_args.kwargs['json']['enrich_mobile'])
        self.assertEqual(post.call_args.kwargs['headers']['X-KEY'], 'secret')

    @patch('src.email_enrich.post_json')
    def test_not_verified_or_masked(self, post):
        for status, email, revealed in [('CATCH_ALL', 'jane@example.com', True),
                                        ('VERIFIED', 'ja***@example.com', True),
                                        ('VERIFIED', 'jane@example.com', False)]:
            post.return_value = {'person': {'email': {'email': email, 'status': status, 'revealed': revealed}}}
            self.assertEqual(email_enrich.enrich_person({}, 'key')['sendable'], 'false')

    @patch('src.email_enrich.post_json')
    def test_no_match_is_an_explicit_miss(self, post):
        post.return_value = {'error': True, 'error_code': 'NO_MATCH'}
        result = email_enrich.enrich_person({}, 'key')
        self.assertEqual(result['miss_reason'], 'no_verified_match')
        self.assertEqual(result['sendable'], 'false')

    @patch('src.email_enrich.post_json')
    def test_malformed_nested_response_fails_explicitly(self, post):
        for response in [{'person': []}, {'person': 'wrong'}, {'person': {'email': 'wrong'}},
                         {'person': {'email': {'status': 123}}}]:
            if response == {'person': []}:
                continue  # Empty missing person is a normal unavailable result.
            post.return_value = response
            with self.assertRaises(RuntimeError):
                email_enrich.enrich_person({}, 'key')

    @patch.dict('os.environ', {'PROSPEO_API_KEY': 'test'})
    @patch('src.email_enrich.enrich_person')
    def test_rerun_cache_and_lookup_cap(self, enrich):
        enrich.return_value = {k: verified_row().get(k, '') for k in email_enrich.FIELDS}
        with tempfile.TemporaryDirectory() as d:
            source, output = str(Path(d) / 'in.csv'), str(Path(d) / 'out.csv')
            rows = [verified_row(), verified_row(linkedin_url='https://linkedin.com/in/other')]
            common.write_csv(source, rows, list(rows[0]))
            argv = ['email_enrich.py', '--input', source, '--output', output, '--max-lookups', '1']
            with patch('sys.argv', argv):
                email_enrich.main()
                self.assertEqual(enrich.call_count, 1)
                self.assertEqual(common.read_csv(output)[0][1]['miss_reason'], 'lookup_limit_reached')
                email_enrich.main()
                self.assertEqual(enrich.call_count, 2)


class SmartleadTests(unittest.TestCase):
    def test_gate_dedupe_suppression_and_age(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        rows = [verified_row(), verified_row(email='jane@EXAMPLE.com'), verified_row(icp_fit='2'),
                verified_row(email_status='CATCH_ALL'), verified_row(suppressed='true'),
                verified_row(email_checked_at=stale), verified_row(email_checked_at=''),
                verified_row(sendable='false'), verified_row(email='broken')]
        leads, excluded = push.select_leads(rows)
        self.assertEqual(len(leads), 1)
        self.assertEqual(excluded, 8)

    @patch('src.push.post_json')
    def test_batch_limit_counts_and_suppression_settings(self, post):
        post.side_effect = [{'success': True, 'added_count': 399, 'skipped_count': 1},
                            {'success': True, 'added_count': 1, 'skipped_count': 0}]
        with tempfile.TemporaryDirectory() as d:
            receipt = push.push_batches([{'email': f'{i}@example.com'} for i in range(401)],
                                        123, 'key', Path(d) / 'receipt.json')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(post.call_args_list[0].kwargs['json']['lead_list']), 400)
        settings = post.call_args.kwargs['json']['settings']
        self.assertFalse(settings['ignore_unsubscribe_list'])
        self.assertFalse(settings['ignore_global_block_list'])
        self.assertTrue(receipt['complete'])
        self.assertEqual(sum(b['added'] for b in receipt['batches']), 400)

    @patch('src.push.post_json')
    def test_partial_failure_persists_prior_success(self, post):
        post.side_effect = [{'success': True, 'added_count': 400, 'skipped_count': 0}, RuntimeError('timeout')]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'receipt.json'
            with self.assertRaises(RuntimeError):
                push.push_batches([{}] * 401, 1, 'key', path)
            receipt = json.loads(path.read_text())
            self.assertFalse(receipt['complete'])
            self.assertEqual(receipt['batches'][0]['status'], 'confirmed')
            self.assertEqual(receipt['batches'][1]['status'], 'unconfirmed')

    @patch('src.push.post_json')
    def test_missing_counts_never_report_success(self, post):
        post.return_value = {'success': True}
        with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError):
            push.push_batches([{}], 1, 'key', Path(d) / 'receipt.json')

    @patch('src.push.post_json')
    def test_preview_does_not_call_api(self, post):
        with tempfile.TemporaryDirectory() as d:
            source = str(Path(d) / 'in.csv')
            row = verified_row()
            common.write_csv(source, [row], list(row))
            with patch('sys.argv', ['push.py', '--input', source, '--campaign-id', '1']):
                push.main()
            self.assertTrue(json.loads(Path(source + '.smartlead.json').read_text())['preview'])
        post.assert_not_called()


class SupabaseTests(unittest.TestCase):
    def test_normalized_dedupe_and_history(self):
        rows = [verified_row(), verified_row(linkedin_url='https://www.linkedin.com/in/jane/?trk=x')]
        first = supabase_export.snapshot_rows(rows, 'run-a')
        second = supabase_export.snapshot_rows(rows, 'run-b')
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['lead_key'], second[0]['lead_key'])
        self.assertNotEqual(first[0]['run_id'], second[0]['run_id'])
        with self.assertRaises(ValueError):
            supabase_export.snapshot_rows([{'name': 'Jane'}], 'run')

    @patch('src.supabase_export.post_json')
    def test_upsert_contract(self, post):
        self.assertEqual(supabase_export.export([verified_row()], 'run', 'https://project.supabase.co', 'sb_secret_key'), 1)
        args = post.call_args.kwargs
        self.assertEqual(args['params']['on_conflict'], 'run_id,lead_key')
        self.assertNotIn('Authorization', args['headers'])
        self.assertIn('merge-duplicates', args['headers']['Prefer'])

    @patch('src.supabase_export.post_json')
    def test_export_batches(self, post):
        rows = [verified_row(linkedin_url=f'https://linkedin.com/in/p{i}') for i in range(501)]
        supabase_export.export(rows, 'run', 'https://project.supabase.co', 'legacy-jwt')
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs['headers']['Authorization'], 'Bearer legacy-jwt')


class HTTPTests(unittest.TestCase):
    @patch('src.common.time.sleep')
    @patch('src.common.requests.post')
    def test_429_retries_and_204(self, post, sleep):
        post.side_effect = [Mock(status_code=429, headers={'Retry-After': '2'}), Mock(status_code=204, content=b'')]
        self.assertIsNone(common.post_json('https://example.com', retry=True))
        sleep.assert_called_once_with(2)

    @patch('src.common.requests.post')
    def test_400_no_match(self, post):
        response = Mock(status_code=400)
        response.json.return_value = {'error': True, 'error_code': 'NO_MATCH'}
        post.return_value = response
        self.assertEqual(common.post_json('https://example.com', accepted_errors=('NO_MATCH',))['error_code'], 'NO_MATCH')

    @patch('src.common.requests.post')
    def test_write_timeout_not_replayed_and_secret_redacted(self, post):
        import requests
        post.side_effect = requests.Timeout('https://example.com?api_key=secret')
        with self.assertRaises(RuntimeError) as cm:
            common.post_json('https://example.com')
        self.assertNotIn('secret', str(cm.exception))
        self.assertEqual(post.call_count, 1)


class ScoringTests(unittest.TestCase):
    def client(self, results):
        return patch.object(rank, 'llm_json', return_value=({'results': results}, 1, 1))

    def test_bad_model_alignment_rejected(self):
        for results in [[], [{'i': 1, 'icp_fit': 4}], [{'i': 0, 'icp_fit': 4}] * 2,
                        [{'i': 0, 'icp_fit': 6}], [{'i': 0, 'icp_fit': 4, 'intent': 'urgent'}]]:
            with self.client(results), self.assertRaises(ValueError):
                rank.score_batch('ICP', [{}])

    def test_reactions_do_not_claim_buying_intent(self):
        self.assertEqual(rank.reaction_intent(['INTEREST', 'PRAISE']), 'low')

    def test_icp_exclusions_survive_loading(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'icp.yaml'
            path.write_text('titles: [CEO]\nindustries_out: [Agency]\ntriggers: [Hiring]\ndisqualifiers: [Student]\n')
            icp.derive_from_yaml(str(path), 'test', Path(d))
            data = json.loads((Path(d) / 'recipient_icp.json').read_text())
            prompt = rank.icp_block(data)
            for word in ['Agency', 'Hiring', 'Student']:
                self.assertIn(word, prompt)

    def test_harvest_counts_distinct_posts(self):
        items = [{'type': 'reaction', 'actor': {'id': 'a', 'linkedinUrl': 'https://linkedin.com/in/ACoAA1'}, 'postId': 'p'},
                 {'type': 'comment', 'actor': {'id': 'a', 'linkedinUrl': 'https://linkedin.com/in/jane'}, 'postId': 'p', 'commentary': 'hello'}]
        rows = harvest.build_engagers(items)
        self.assertEqual(rows[0]['engagement_count'], 1)
        self.assertEqual(rows[0]['linkedinUrl'], 'https://linkedin.com/in/jane')


if __name__ == '__main__':
    unittest.main()


class LlmProviderTests(unittest.TestCase):
    def test_provider_selection(self):
        with patch.dict('os.environ', {'LLM_PROVIDER': '', 'OPENAI_API_KEY': 'k'}):
            self.assertEqual(common.llm_provider(), 'openai')
        with patch.dict('os.environ', {'LLM_PROVIDER': 'claude', 'OPENAI_API_KEY': 'k'}):
            self.assertEqual(common.llm_provider(), 'claude')
        with patch.dict('os.environ', {'LLM_PROVIDER': '', 'OPENAI_API_KEY': ''}), \
                patch('shutil.which', return_value=None):
            self.assertIsNone(common.llm_provider())
            with self.assertRaises(RuntimeError):
                common.llm_json('s', 'u')
