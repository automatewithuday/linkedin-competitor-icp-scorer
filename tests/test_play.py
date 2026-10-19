import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ['LEDGER_DB'] = ':memory:'  # tests never touch data/ledger.sqlite; ledger tests use their own file

from src import activity, common, ledger, email_enrich, harvest, icp, push, rank, resolve, summary, supabase_export


def verified_row(**changes):
    row = {'name': 'Jane Doe', 'linkedin_url': 'https://linkedin.com/in/jane',
           'icp_fit': '4', 'email': 'Jane@example.com', 'email_status': 'VERIFIED',
           'sendable': 'true', 'email_checked_at': datetime.now(timezone.utc).isoformat()}
    row.update(changes)
    return row


def explained(result):
    return {'qualification_reason': 'The stated role and employer evidence were compared with the ICP.',
            'intent_reason': 'Intent reflects the supplied comment or reaction evidence.', **result}


def bind_scores(base, path):
    engagers = json.loads((base / 'engagers.json').read_text())
    recipient = json.loads((base / 'recipient_icp.json').read_text())
    data = json.loads(path.read_text())
    data['results'] = [explained(result) for result in data['results']]
    for result in data['results']:
        result['score_context'] = ledger.score_context(engagers[result['i']], recipient, rank.SYSTEM)
    path.write_text(json.dumps(data))


class IdentityTests(unittest.TestCase):
    def test_normalization(self):
        self.assertEqual(common.linkedin_url('https://uk.linkedin.com/in/Jane/?trk=x'),
                         'https://www.linkedin.com/in/jane')
        for url in ['https://notlinkedin.com/in/jane', 'https://linkedin.com.evil.com/in/jane',
                    'https://linkedin.com/company/acme', 'https://linkedin.com/in/ACoAA123']:
            self.assertEqual(common.linkedin_url(url), '')

    def test_csv_unicode_and_empty_output(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'nested' / 'test.csv'
            common.write_csv(path, [{'name': 'Zoë, 李'}], ['name'])
            self.assertEqual(common.read_csv(path)[0][0]['name'], 'Zoë, 李')
            common.write_csv(path, [], ['name'])
            self.assertEqual(common.read_csv(path), ([], ['name']))


class EmailContractTests(unittest.TestCase):
    def test_discovered_work_email_never_becomes_sendable(self):
        hit = {'status': 'matched', 'checked_at': ledger.now(),
               'data': json.dumps({'work_email': 'jane@example.com', 'email_status': 'VERIFIED'})}
        result = email_enrich.email_result(hit)
        self.assertEqual((result['email'], result['email_status'], result['sendable']),
                         ('jane@example.com', 'UNVERIFIED', 'false'))
        self.assertEqual(result['email_provider'], 'getleads')
        self.assertEqual(result['miss_reason'], 'verification_required')

    def test_misses_and_invalid_addresses_stay_unsendable(self):
        for status, data in [('not_found', {}), ('matched', {'work_email': 'ja***@example.com'}),
                             ('matched', {'work_email': 'broken'}), ('matched', {})]:
            result = email_enrich.email_result({'status': status, 'data': json.dumps(data), 'checked_at': ledger.now()})
            self.assertEqual((result['email'], result['email_status'], result['sendable']), ('', 'UNAVAILABLE', 'false'))


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


class ActivityTests(unittest.TestCase):
    @patch('src.activity.subprocess.run')
    def test_eligibility_join_and_miss_reasons(self, run):
        def fake(cmd, **kwargs):
            self.assertEqual(Path(cmd[2]).read_text().split(), ['https://www.linkedin.com/in/jane'])
            out = Path(cmd[cmd.index('--out') + 1])
            common.write_csv(out / '20260101-000000.csv',
                             [{'linkedin_url': 'https://www.linkedin.com/in/jane', 'activity_status': 'active',
                               'linkedin_outreach_ready': 'True', 'outreach_ready_probability': '0.9'}],
                             ['linkedin_url', 'activity_status', 'linkedin_outreach_ready', 'outreach_ready_probability'])
            return SimpleNamespace(returncode=0)
        run.side_effect = fake
        rows = [{'name': 'Jane', 'linkedin_url': 'https://linkedin.com/in/Jane/', 'icp_fit': '5'},
                {'name': 'Opaque', 'linkedin_url': 'https://linkedin.com/in/ACoAA123', 'icp_fit': '5'},
                {'name': 'Low', 'linkedin_url': 'https://linkedin.com/in/low', 'icp_fit': '2'}]
        with tempfile.TemporaryDirectory() as d:
            source, output = str(Path(d) / 'ranked.csv'), str(Path(d) / 'activity.csv')
            common.write_csv(source, rows, ['name', 'linkedin_url', 'icp_fit'])
            with patch('sys.argv', ['activity.py', '--input', source, '--output', output]):
                activity.main()
            got, fields = common.read_csv(output)
        self.assertEqual(fields, ['name', 'linkedin_url', 'icp_fit'] + activity.FIELDS)
        self.assertEqual(got[0]['linkedin_outreach_ready'], 'true')
        self.assertEqual(got[0]['activity_miss_reason'], '')
        self.assertEqual(got[1]['activity_miss_reason'], 'no_public_url')
        self.assertEqual(got[2]['activity_miss_reason'], 'below_min_fit')

    def test_emoji_slug_attributes_actor_items(self):
        from src import activity_intel
        url = 'https://www.linkedin.com/in/cody-smith-🇨🇦-49b'
        grouped, missed = activity_intel.attribute('comments', [{'query': {'profile': url}, 'commentary': 'x'}], [activity_intel.username(url)])
        self.assertEqual((missed, len(grouped[activity_intel.username(url)])), (0, 1))

    @patch('src.activity.subprocess.run')
    def test_nothing_eligible_skips_tool(self, run):
        with tempfile.TemporaryDirectory() as d:
            source, output = str(Path(d) / 'ranked.csv'), str(Path(d) / 'activity.csv')
            common.write_csv(source, [{'name': 'Low', 'linkedin_url': 'https://linkedin.com/in/low', 'icp_fit': '1'}],
                             ['name', 'linkedin_url', 'icp_fit'])
            with patch('sys.argv', ['activity.py', '--input', source, '--output', output]):
                activity.main()
            self.assertEqual(common.read_csv(output)[0][0]['activity_miss_reason'], 'below_min_fit')
        run.assert_not_called()


class ScorerAndSummaryTests(unittest.TestCase):
    def test_align_accepts_in_session_scores_and_rejects_gaps(self):
        good = {'results': [explained({'i': 1, 'icp_fit': 4, 'intent': None}), explained({'i': 0, 'icp_fit': 2, 'intent': 'high'})]}
        self.assertEqual([r['icp_fit'] for r in rank.align(good, 2)], [2, 4])
        for bad in [{'results': [{'i': 0, 'icp_fit': 4}]},                       # missing row 1
                    {'results': [{'i': 0, 'icp_fit': 6}, {'i': 1, 'icp_fit': 1}]},  # fit out of range
                    {'results': [{'i': 0, 'icp_fit': 3, 'intent': 'hot'}, {'i': 1, 'icp_fit': 1}]}]:
            with self.assertRaises(ValueError):
                rank.align(bad, 2)

    @patch('src.rank.score_batch')
    def test_rank_scores_file_skips_openai(self, score_batch):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            (base / 'engagers.json').write_text(json.dumps([
                {'id': 'A', 'name': 'Jane', 'position': 'CRO', 'engagement_count': 2, 'comments': ['need help'], 'linkedinUrl': 'https://linkedin.com/in/jane'},
                {'id': 'B', 'name': 'Bob', 'position': 'Student', 'engagement_count': 1, 'reaction_types': ['LIKE']}]))
            (base / 'recipient_icp.json').write_text('{}')
            scores = Path(d) / 'scores.json'
            scores.write_text(json.dumps({'results': [{'i': 0, 'icp_fit': 5, 'intent': 'high', 'clean_title': 'CRO'},
                                                      {'i': 1, 'icp_fit': 1, 'intent': None}]}))
            bind_scores(base, scores)
            with patch.dict('os.environ', {'OPENAI_API_KEY': ''}), \
                 patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', d, '--scores', str(scores)]):
                rank.main()
            rows, cols = common.read_csv(base / 'ranked_engagers.csv')
        score_batch.assert_not_called()
        self.assertEqual(cols[cols.index('company_guess') + 1], 'location')
        self.assertEqual([(r['name'], r['icp_fit'], r['intent']) for r in rows], [('Jane', '5', 'high'), ('Bob', '1', 'low')])

    def test_summary_names_and_breakdowns(self):
        rows = [{'name': 'Jane', 'clean_title': 'CRO', 'company_guess': 'Acme', 'icp_fit': '5', 'intent': 'high', 'rank_score': '9',
                 'linkedin_url': 'https://linkedin.com/in/jane', 'activity_status': 'active', 'linkedin_outreach_ready': 'true',
                 'outreach_ready_probability': '0.9', 'last_activity_type': 'post', 'data_quality_warning': 'false', 'activity_miss_reason': ''},
                {'name': 'Bob', 'clean_title': 'Student', 'company_guess': '', 'icp_fit': '1', 'intent': 'low', 'rank_score': '0.3',
                 'linkedin_url': 'https://linkedin.com/in/ACoAA1', 'activity_status': '', 'linkedin_outreach_ready': '',
                 'outreach_ready_probability': '', 'last_activity_type': '', 'data_quality_warning': '', 'activity_miss_reason': 'below_min_fit'}]
        text = '\n'.join(summary.summarize(rows, top=1))
        self.assertIn('Qualified (fit>=4): 1; with public profile URL: 1; high-intent comments: 1', text)
        self.assertIn("checked 1, outreach-ready 1", text)
        self.assertIn('Jane', text)
        self.assertNotIn('  2 Bob', text)  # top=1 hides the second row

    def test_post_url_resolution_and_tagging(self):
        self.assertEqual(harvest.resolve_post('https://www.linkedin.com/posts/a_b-1-c/?utm_source=x'), 'https://www.linkedin.com/posts/a_b-1-c/')
        self.assertEqual(harvest.resolve_post('linkedin.com/feed/update/urn:li:activity:1/'), 'https://www.linkedin.com/feed/update/urn:li:activity:1/')
        self.assertEqual(harvest.resolve_post('https://www.linkedin.com/in/someone'), '')
        self.assertEqual(harvest.resolve_post('https://evil.com/posts/x'), '')
        with patch('src.harvest.requests.head') as head:
            head.return_value = SimpleNamespace(url='https://www.linkedin.com/posts/a_b-1-c/?trk=share')
            self.assertEqual(harvest.resolve_post('https://lnkd.in/p/abc'), 'https://www.linkedin.com/posts/a_b-1-c/')
        items = harvest.tag_post_items([{'actor': {'id': 'A', 'name': 'J'}, 'reactionType': 'LIKE', 'postId': 'p1'}], 'reaction') + \
                harvest.tag_post_items([{'actor': {'id': 'A', 'name': 'J'}, 'commentary': 'hi', 'postId': 'p1'}], 'comment')
        eng = harvest.build_engagers(items)
        self.assertEqual((len(eng), eng[0]['reaction_types'], eng[0]['comments'], eng[0]['engagement_count']), (1, ['LIKE'], ['hi'], 1))


class OpaqueIdTests(unittest.TestCase):
    def test_harvest_main_mode_profile_fields(self):
        main = {'id': 'ACoAA1', 'name': 'Adam', 'linkedinUrl': 'https://www.linkedin.com/in/ACoAA1',
                'publicIdentifier': 'adamj', 'headline': 'VP Sales @ Acme',
                'location': {'linkedinText': 'Austin, Texas, United States'},
                'currentPosition': [{'position': 'VP Sales', 'companyName': 'Acme'}]}
        short = {'id': 'ACoAA2', 'name': 'Bo', 'linkedinUrl': 'https://www.linkedin.com/in/ACoAA2', 'position': 'CRO'}
        eng = {e['id']: e for e in harvest.build_engagers(harvest.tag_post_items(
            [{'actor': main, 'postId': 'p'}, {'actor': short, 'postId': 'p'}], 'reaction'))}
        self.assertEqual((eng['ACoAA1']['linkedinUrl'], eng['ACoAA1']['location'], eng['ACoAA1']['company'],
                          eng['ACoAA1']['position']),
                         ('https://www.linkedin.com/in/adamj', 'Austin, Texas, United States', 'Acme', 'VP Sales @ Acme'))
        self.assertEqual((eng['ACoAA2']['linkedinUrl'], eng['ACoAA2']['position'], eng['ACoAA2']['location']),
                         ('https://www.linkedin.com/in/ACoAA2', 'CRO', ''))
        line = rank.engager_lines([eng['ACoAA1']])[0]
        self.assertIn('location: "Austin, Texas, United States"', line)
        self.assertIn('company: "Acme"', line)



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


class DraftTests(unittest.TestCase):
    def test_template_mapping_is_strict(self):
        from src import draft
        self.assertIsNone(draft.pick_template(2, 'high', 'we need this badly'))
        self.assertEqual(draft.pick_template(3, 'high', 'anything'), 'Integration-Partner')
        self.assertEqual(draft.pick_template(5, 'high', 'Great post!'), 'Pain-Pivot')
        self.assertEqual(draft.pick_template(4, 'medium', ''), 'Pain-Pivot')
        self.assertEqual(draft.pick_template(4, 'high', 'Half the list is enrichment. None solve decay.'), 'Assumed-Knowledge')

    def test_topic_falls_back_to_url_slug(self):
        from src import draft
        posts = {'1': {'content': '', 'url': 'https://www.linkedin.com/posts/x_28-apis-that-turn-any-agent-into-a-gtm-engine-activity-7505-X4vU'}}
        self.assertEqual(draft.post_topic(['1'], posts), '28 apis that turn any agent into a gtm engine')


class HeyReachTests(unittest.TestCase):
    def rows(self):
        return [{'name': 'Ann Lee', 'linkedin_url': 'https://www.linkedin.com/in/ann', 'icp_fit': '4', 'clean_title': 'CMO',
                 'company_guess': 'Acme', 'intent': 'high', 'email': 'a@x.io', 'email_status': 'VERIFIED'},
                {'name': 'Dup', 'linkedin_url': 'https://www.linkedin.com/in/ann/', 'icp_fit': '5'},
                {'name': 'Low', 'linkedin_url': 'https://www.linkedin.com/in/low', 'icp_fit': '2'},
                {'name': 'NoUrl', 'linkedin_url': '', 'icp_fit': '5'},
                {'name': 'Urn Id', 'linkedin_url': 'https://www.linkedin.com/in/ACoAAC2AeykBzJtlA4f9aIQymmFs7b3UoQ7B20M', 'icp_fit': '4'}]

    def test_select_merges_drafts(self):
        from src import push_heyreach
        drafts = {'https://www.linkedin.com/in/ann': {'template': 'Pain-Pivot', 'connection_note': 'hi', 'dm': 'yo'}}
        leads, excluded = push_heyreach.select_leads(self.rows(), 4, drafts)
        self.assertEqual((len(leads), excluded), (2, 3))
        self.assertEqual(leads[0]['emailAddress'], 'a@x.io')
        names = {f['name']: f['value'] for f in leads[0]['customUserFields']}
        self.assertEqual(names['dm'], 'yo')
        self.assertEqual(names['icp_fit'], '4')

    @patch('src.push_heyreach.post_json')
    def test_push_confirms_counts_and_batches(self, post):
        from src import push_heyreach
        post.return_value = {'addedLeadsCount': 100, 'updatedLeadsCount': 0, 'failedLeadsCount': 0}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'r.json'
            ok = [{'profileUrl': f'https://www.linkedin.com/in/u{i}'} for i in range(100)]
            receipt = push_heyreach.push_batches(ok, {'listId': 7}, 'k', path)
            self.assertTrue(receipt['complete'])
            self.assertEqual(post.call_args.kwargs['json']['listId'], 7)
            post.return_value = {'addedLeadsCount': 1}
            with self.assertRaises(RuntimeError):
                push_heyreach.push_batches(ok[:2], {'campaignId': 1, 'linkedInAccountId': 2}, 'k', path)
            self.assertEqual(json.loads(path.read_text())['batches'][0]['status'], 'unconfirmed')


class NewUserRegressionTests(unittest.TestCase):
    @patch('src.activity.subprocess.run', return_value=SimpleNamespace(returncode=0))
    def test_activity_dry_run_leaves_run_folder_untouched(self, run):
        with tempfile.TemporaryDirectory() as d:
            src_, out = str(Path(d) / 'ranked.csv'), str(Path(d) / 'activity.csv')
            common.write_csv(src_, [{'name': 'J', 'linkedin_url': 'https://linkedin.com/in/jane', 'icp_fit': '5'}],
                             ['name', 'linkedin_url', 'icp_fit'])
            with patch('sys.argv', ['activity.py', '--input', src_, '--output', out, '--dry-run']):
                activity.main()
            self.assertFalse((Path(d) / 'activity').exists())
        self.assertIn('--dry-run', run.call_args.args[0])

    def test_scoring_prompt_sentences_are_terminated(self):
        self.assertNotIn('use it Be honest', rank.SYSTEM)


class LockedVersionTests(unittest.TestCase):
    def test_in_session_guard(self):
        with patch.dict('os.environ', {'CLAUDECODE': '1', 'LLM_PROVIDER': ''}):
            self.assertIn('--scores', common.in_session_guard())
        with patch.dict('os.environ', {'CLAUDECODE': '1', 'LLM_PROVIDER': 'claude'}):
            self.assertIsNone(common.in_session_guard())
        with patch.dict('os.environ', {'CLAUDECODE': '', 'LLM_PROVIDER': ''}):
            self.assertIsNone(common.in_session_guard())

    @patch('src.rank.score_batch')
    def test_rank_refuses_silent_llm_inside_claude_code(self, score_batch):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            (base / 'engagers.json').write_text('[{"id": "A", "name": "J"}]'); (base / 'recipient_icp.json').write_text('{}')
            with patch.dict('os.environ', {'CLAUDECODE': '1', 'LLM_PROVIDER': '', 'OPENAI_API_KEY': 'k'}), \
                 patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', d]), self.assertRaises(SystemExit):
                rank.main()
        score_batch.assert_not_called()

    def test_qualified_rows_always_rank_first(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            (base / 'engagers.json').write_text(json.dumps([
                {'id': 'A', 'name': 'Commenter', 'engagement_count': 3, 'comments': ['we need this now']},
                {'id': 'B', 'name': 'Buyer', 'engagement_count': 1, 'reaction_types': ['LIKE']}]))
            (base / 'recipient_icp.json').write_text('{}')
            scores = Path(d) / 'scores.json'
            scores.write_text(json.dumps({'results': [{'i': 0, 'icp_fit': 2, 'intent': 'high'}, {'i': 1, 'icp_fit': 4, 'intent': None}]}))
            bind_scores(base, scores)
            with patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', d, '--scores', str(scores)]):
                rank.main()
            rows, _ = common.read_csv(base / 'ranked_engagers.csv')
        self.assertEqual([r['name'] for r in rows], ['Buyer', 'Commenter'])  # 2*(1+ln3)*1.0 > 4*0.3, still second
        text = '\n'.join(summary.summarize(rows))
        self.assertIn('Strong comments below the fit bar', text)
        self.assertLess(text.index('Buyer'), text.index('  2 Commenter'))


def make_run(d, engagers, fits):
    base = Path(d) / 's'; base.mkdir()
    (base / 'engagers.json').write_text(json.dumps(engagers))
    common.write_csv(base / 'ranked_engagers.csv', [{'actor_id': k, 'icp_fit': str(v)} for k, v in fits.items()],
                     ['actor_id', 'icp_fit'])
    return base


def opaque(pid, name):
    return {'id': pid, 'name': name, 'linkedinUrl': f'https://www.linkedin.com/in/{pid}'}


def profile(pid, slug, where='London', employer='Acme'):
    return {'id': pid, 'publicIdentifier': slug, 'location': {'linkedinText': where},
            'currentPosition': [{'companyName': employer}]}


@patch.dict('os.environ', {'APIFY_TOKEN': 'test-dummy'})  # no-spend suite must not depend on a real token (review #7)
class ReviewFindingTests(unittest.TestCase):
    @patch('src.resolve.scrape')
    def test_lookup_statuses_cache_and_run_log(self, scrape):
        from src import resolve
        scrape.return_value = ({'ACoAA5': profile('ACoAA5', 'Hot-One')}, True, 'run1')
        with tempfile.TemporaryDirectory() as d:
            eng = [opaque('ACoAA5', 'Hot'), opaque('ACoAA2', 'Cold'),
                   {'id': 'ACoAA9', 'name': 'Public', 'linkedinUrl': 'https://www.linkedin.com/in/jane'}, opaque('ACoAA4', 'Gone')]
            base = make_run(d, eng, {'ACoAA5': 5, 'ACoAA2': 2, 'ACoAA9': 5, 'ACoAA4': 4})
            self.assertEqual(resolve.enrich_harvest(base, 3), ([0], []))
            self.assertEqual(scrape.call_args.args[0], ['ACoAA5', 'ACoAA4'])  # fit 2 and public never sent
            out = json.loads((base / 'engagers.json').read_text())
            self.assertEqual((out[0]['linkedinUrl'], out[0]['location'], out[0]['company'], out[0]['resolve_status']),
                             ('https://www.linkedin.com/in/hot-one', 'London', 'Acme', 'resolved'))
            self.assertEqual(out[3]['resolve_status'], 'not_found')
            self.assertEqual(json.loads((base / 'rescore.json').read_text())['indices'], [0])
            self.assertEqual(json.loads((base / 'resolve_runs.json').read_text())[0]['run_id'], 'run1')
            scrape.reset_mock()
            self.assertEqual(resolve.enrich_harvest(base, 3), ([], []))  # resolved and missed: nothing paid twice
            scrape.assert_not_called()

    @patch('src.resolve.scrape')
    def test_misses_cached_even_when_nothing_found_and_refreshable(self, scrape):  # review #3
        from src import resolve
        scrape.return_value = ({}, True, 'r')
        with tempfile.TemporaryDirectory() as d:
            base = make_run(d, [opaque('ACoAA1', 'A')], {'ACoAA1': 4})
            resolve.enrich_harvest(base, 3)
            self.assertEqual(json.loads((base / 'engagers.json').read_text())[0]['resolve_status'], 'not_found')
            scrape.reset_mock(); resolve.enrich_harvest(base, 3); scrape.assert_not_called()
            resolve.enrich_harvest(base, 3, refresh_misses=True); self.assertEqual(scrape.call_count, 1)
            eng = json.loads((base / 'engagers.json').read_text())
            eng[0]['resolve_checked_at'] = '2020-01-01T00:00:00+00:00'  # older than MISS_DAYS: expires
            (base / 'engagers.json').write_text(json.dumps(eng))
            scrape.reset_mock(); resolve.enrich_harvest(base, 3); self.assertEqual(scrape.call_count, 1)

    @patch('src.resolve.scrape')
    def test_later_batch_failure_keeps_earlier_paid_batches(self, scrape):  # review #2
        from src import resolve
        ids = [f'ACoAA{n:02d}' for n in range(11)]
        scrape.side_effect = [({i: profile(i, f'p{i}') for i in ids[:10]}, True, 'r1'), RuntimeError('network')]
        with tempfile.TemporaryDirectory() as d:
            base = make_run(d, [opaque(i, i) for i in ids], {i: 4 for i in ids})
            changed, failed = resolve.enrich_harvest(base, 3, batch_size=10)
            self.assertEqual((len(changed), failed), (10, [ids[10]]))
            self.assertEqual(sum(e.get('resolve_status') == 'resolved'
                                 for e in json.loads((base / 'engagers.json').read_text())), 10)
            scrape.side_effect = None
            scrape.return_value = ({ids[10]: profile(ids[10], 'last')}, True, 'r2')
            resolve.enrich_harvest(base, 3, batch_size=10)
            self.assertEqual(scrape.call_args.args[0], [ids[10]])  # only the unresolved one is retried

    @patch('src.resolve.scrape')
    def test_free_plan_error_item_is_a_failure_and_exits_3(self, scrape):
        from src import resolve
        scrape.return_value = (*resolve.parse([{'error': 'Free users are limited to 10 items per run.'}], 'SUCCEEDED'), 'r')
        with tempfile.TemporaryDirectory() as d:
            base = make_run(d, [opaque('ACoAA1', 'A')], {'ACoAA1': 4})
            with patch('sys.argv', ['resolve.py', '--slug', 's', '--output-dir', d]), self.assertRaises(SystemExit) as ex:
                resolve.main()
            self.assertEqual(ex.exception.code, resolve.FAILED_EXIT)
            self.assertNotIn('resolve_status', json.loads((base / 'engagers.json').read_text())[0])  # not a cached miss
            scrape.reset_mock()
            with patch('sys.argv', ['resolve.py', '--slug', 's', '--output-dir', d, '--dry-run']):
                resolve.main()
            scrape.assert_not_called()

    @patch('src.resolve.scrape')
    def test_pending_rescore_indices_survive_a_partial_pass(self, scrape):
        from src import resolve
        scrape.return_value = ({'ACoAA2': profile('ACoAA2', 'b')}, True, 'r')
        with tempfile.TemporaryDirectory() as d:
            base = make_run(d, [opaque('ACoAA1', 'A'), opaque('ACoAA2', 'B')], {'ACoAA1': 4, 'ACoAA2': 4})
            scores = Path(d) / 'scores.json'; scores.write_text('{}')
            os.utime(scores, (1, 1))  # scored before the earlier pass
            (base / 'rescore.json').write_text(json.dumps({'indices': [0]}))
            resolve.enrich_harvest(base, 3, scores=str(scores))
            self.assertEqual(json.loads((base / 'rescore.json').read_text())['indices'], [0, 1])

    def test_failed_lookup_stops_pipeline_before_downstream(self):  # review #1
        import run as orchestrator
        calls = []

        def fake(cmd, check=False):
            calls.append(' '.join(cmd))
            return SimpleNamespace(returncode=3 if 'src/resolve.py' in cmd else 0)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            for f in ('posts.json', 'engagers.json'):
                (base / f).write_text('[]')
            icp_path, scores = Path(d) / 'icp.yaml', Path(d) / 'scores.json'
            icp_path.write_text('titles: []'); scores.write_text('{"results": []}')
            argv = ['run.py', '--slug', 's', '--output-dir', d, '--skip-harvest', '--icp', str(icp_path),
                    '--scores', str(scores), '--activity', '--find-emails']
            with patch.dict('os.environ', {'TYPESAFE_API_KEY': 'x', 'GETLEADS_API_KEY': 'x'}), \
                 patch('run.subprocess.run', side_effect=fake), patch('sys.argv', argv), self.assertRaises(SystemExit):
                orchestrator.main()
        self.assertEqual(sum('src/resolve.py' in c for c in calls), 1)
        self.assertIn('--slug', next(c for c in calls if 'src/resolve.py' in c))
        self.assertFalse(any(x in c for c in calls for x in ('activity.py', 'email_enrich.py', 'summary.py')))

    def test_later_comment_drives_intent_and_keeps_its_post(self):  # review #4
        e = {'id': 'A', 'name': 'J', 'position': 'VP Sales', 'engagement_count': 2,
             'comments': ['Great post', 'We are evaluating vendors this week. Can we book a demo?'],
             'comment_post_urls': ['https://www.linkedin.com/posts/one', 'https://www.linkedin.com/posts/two']}
        line = rank.engager_lines([e])[0]
        self.assertIn('c2: "We are evaluating vendors', line)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            (base / 'engagers.json').write_text(json.dumps([e])); (base / 'recipient_icp.json').write_text('{}')
            scores = Path(d) / 'scores.json'
            scores.write_text(json.dumps({'results': [{'i': 0, 'icp_fit': 4, 'intent': 'high', 'evidence_comment': 2}]}))
            bind_scores(base, scores)
            with patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', d, '--scores', str(scores)]):
                rank.main()
            row = common.read_csv(base / 'ranked_engagers.csv')[0][0]
        self.assertTrue(row['comment_excerpt'].startswith('We are evaluating vendors'))
        self.assertEqual(row['comment_post_url'], 'https://www.linkedin.com/posts/two')

    def test_profile_employer_survives_a_null_headline_guess(self):  # review #5
        e = {'id': 'A', 'name': 'J', 'position': 'VP Sales', 'company': 'Acme', 'engagement_count': 1}
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            (base / 'engagers.json').write_text(json.dumps([e])); (base / 'recipient_icp.json').write_text('{}')
            scores = Path(d) / 'scores.json'
            scores.write_text(json.dumps({'results': [{'i': 0, 'icp_fit': 4, 'intent': None, 'company_guess': None}]}))
            bind_scores(base, scores)
            with patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', d, '--scores', str(scores)]):
                rank.main()
            row = common.read_csv(base / 'ranked_engagers.csv')[0][0]
        self.assertEqual((row['company_guess'], row['company_source']), ('Acme', 'profile'))

    def test_draft_with_zero_qualified_writes_empty_file_and_succeeds(self):  # review #6
        from src import draft
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            (base / 'recipient_icp.json').write_text('{}')
            (base / 'drafts.csv').write_text('stale\n')
            common.write_csv(base / 'ranked_engagers.csv', [{'rank': '1', 'name': 'Low', 'icp_fit': '2', 'intent': 'low'}],
                             ['rank', 'name', 'icp_fit', 'intent'])
            with patch.object(draft, 'llm_json') as llm, patch('sys.argv', ['draft.py', '--slug', 's', '--output-dir', d]):
                draft.main()
            rows, fields = common.read_csv(base / 'drafts.csv')
        llm.assert_not_called()
        self.assertEqual((rows, fields[:2]), ([], ['rank', 'name']))


class LedgerTests(unittest.TestCase):
    """The ledger rules from the design: one person across runs, stale-on-change scores, no repeat outreach."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'ledger.sqlite')
        self.env = patch.dict('os.environ', {'LEDGER_DB': self.db, 'APIFY_TOKEN': 'test-dummy'})
        self.env.start()

    def tearDown(self):
        self.env.stop(); self.tmp.cleanup()

    def run_dir(self, slug, engagers, icp=None):
        base = Path(self.tmp.name) / slug; base.mkdir()
        (base / 'engagers.json').write_text(json.dumps(engagers)); (base / 'recipient_icp.json').write_text(json.dumps(icp or {'titles': ['CRO']}))
        return base

    def test_one_person_across_runs_and_aliases(self):
        con = ledger.connect()
        ledger.ingest(con, self.run_dir('a', [{'id': 'ACoAA1', 'name': 'J', 'posts_touched': ['p1'], 'comments': ['hi'], 'comment_post_ids': ['p1']}]))
        ledger.ingest(con, self.run_dir('b', [{'id': 'x', 'name': 'J', 'linkedinUrl': 'https://www.linkedin.com/in/jane', 'posts_touched': ['p2']}]))
        self.assertEqual(con.execute('SELECT COUNT(*) FROM contacts').fetchone()[0], 2)  # nothing links them yet
        new, seen = ledger.ingest(con, self.run_dir('c', [{'id': 'ACoAA1', 'linkedinUrl': 'https://www.linkedin.com/in/jane', 'posts_touched': ['p1']}]))
        self.assertEqual((new, seen, con.execute('SELECT COUNT(*) FROM contacts').fetchone()[0]), (0, 1, 1))  # merged
        ledger.ingest(con, Path(self.tmp.name) / 'a')  # re-ingest: engagements are never inserted twice
        self.assertEqual(con.execute('SELECT COUNT(*) FROM engagements').fetchone()[0], 3)

    def test_location_change_makes_score_stale_until_rescored(self):
        con = ledger.connect()
        e = {'id': 'ACoAA1', 'name': 'J', 'position': 'CRO', 'location': ''}
        icp = {'titles': ['CRO']}
        ledger.record_scores(con, [e], [explained({'icp_fit': 4, 'intent': None})], icp, 'rubric', 'in-session', 'a')
        self.assertEqual(list(ledger.fresh_scores(con, [e], icp, 'rubric')), [0])
        moved = {**e, 'location': 'Mumbai, India'}
        self.assertEqual(ledger.fresh_scores(con, [moved], icp, 'rubric'), {})                    # evidence changed
        self.assertEqual(ledger.fresh_scores(con, [e], {'titles': ['VP Sales']}, 'rubric'), {})   # other ICP
        self.assertEqual(ledger.fresh_scores(con, [e], icp, 'new rubric'), {})                    # rubric changed
        icp_id = ledger.icp_version(con, icp)
        self.assertEqual(ledger.status_for(con, ['id:ACoAA1'], icp_id, ledger.rubric_hash('rubric'),
                                           ledger.evidence_hash(moved)), 'stale_score')
        ledger.record_scores(con, [moved], [explained({'icp_fit': 2, 'intent': None})], icp, 'rubric', 'in-session', 'a')
        self.assertEqual(ledger.status_for(con, ['id:ACoAA1'], icp_id, ledger.rubric_hash('rubric'),
                                           ledger.evidence_hash(moved)), 'eligible')
        self.assertEqual(ledger.fresh_scores(con, [moved], icp, 'rubric')[0]['icp_fit'], 2)

    def test_scores_belong_to_contact_and_icp(self):
        con = ledger.connect()
        e = {'id': 'ACoAA1', 'position': 'CRO'}
        ledger.record_scores(con, [e], [explained({'icp_fit': 5})], {'client': 'A'}, 'r', 's', 'x')
        ledger.record_scores(con, [e], [explained({'icp_fit': 1})], {'client': 'B'}, 'r', 's', 'x')
        self.assertEqual((ledger.fresh_scores(con, [e], {'client': 'A'}, 'r')[0]['icp_fit'],
                          ledger.fresh_scores(con, [e], {'client': 'B'}, 'r')[0]['icp_fit']), (5, 1))

    def test_rank_reuses_fresh_scores_and_dump_hides_them(self):
        engs = [{'id': 'ACoAA1', 'name': 'Seen', 'position': 'CRO'}, {'id': 'ACoAA2', 'name': 'New', 'position': 'CTO'}]
        base = self.run_dir('s', engs)
        ledger.record_scores(ledger.connect(), engs[:1], [explained({'icp_fit': 5, 'intent': None, 'clean_title': 'CRO'})],
                             {'titles': ['CRO']}, rank.SYSTEM, 'in-session', 'old')
        out = io.StringIO()
        with patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', self.tmp.name, '--dump']), patch('sys.stdout', out):
            rank.main()
        self.assertIn('1. New', out.getvalue()); self.assertNotIn('0. Seen', out.getvalue())
        scores = Path(self.tmp.name) / 'scores.json'
        scores.write_text(json.dumps({'results': [{'i': 1, 'icp_fit': 2, 'intent': None}]}))  # only the pending row
        bind_scores(base, scores)
        with patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', self.tmp.name, '--scores', str(scores)]):
            rank.main()
        rows = {r['name']: r['icp_fit'] for r in common.read_csv(base / 'ranked_engagers.csv')[0]}
        self.assertEqual(rows, {'Seen': '5', 'New': '2'})

    @patch('src.resolve.scrape')
    def test_lookups_are_shared_across_runs(self, scrape):
        from src import resolve
        scrape.return_value = ({'ACoAA1': profile('ACoAA1', 'jane', 'Austin, Texas', 'Acme')}, True, 'r1')
        for slug in ('first', 'second'):
            base = self.run_dir(slug, [opaque('ACoAA1', 'Jane')])
            common.write_csv(base / 'ranked_engagers.csv', [{'actor_id': 'ACoAA1', 'icp_fit': '4'}], ['actor_id', 'icp_fit'])
            changed, failed = resolve.enrich_harvest(base, 3)
            self.assertEqual((changed, failed), ([0], []))  # second run: evidence still goes back to scoring
        self.assertEqual(scrape.call_count, 1)  # but it is paid for once
        e = json.loads((Path(self.tmp.name) / 'second' / 'engagers.json').read_text())[0]
        self.assertEqual((e['linkedinUrl'], e['location'], e['company']), ('https://www.linkedin.com/in/jane', 'Austin, Texas', 'Acme'))

    def test_campaign_history_and_suppression_block_repeat_outreach(self):
        con = ledger.connect()
        leads = [{'linkedin_profile': 'https://www.linkedin.com/in/jane', 'email': 'jane@acme.com'},
                 {'linkedin_profile': 'https://www.linkedin.com/in/bob', 'email': 'bob@beta.io'}]
        receipt = Path(self.tmp.name) / 'r.json'
        receipt.write_text(json.dumps({'batches': [{'offset': 0, 'requested': 1, 'status': 'confirmed'},
                                                   {'offset': 1, 'requested': 1, 'status': 'unconfirmed'}]}))
        self.assertEqual(ledger.record_receipt(con, leads, receipt, 'smartlead:1', 'linkedin_profile', 'email'), 1)
        self.assertEqual(ledger.blocked(con, 'https://linkedin.com/in/Jane/'), 'in_campaign:smartlead:1')
        self.assertIsNone(ledger.blocked(con, 'https://www.linkedin.com/in/bob'))  # unconfirmed: reconcile, not block
        con.execute("INSERT INTO suppressions VALUES ('domain:beta.io', 'customer', 'now')")
        self.assertEqual(ledger.blocked(con, None, 'BOB@beta.io'), 'suppressed:customer')
        kept, dropped = ledger.filter_blocked(con, leads, 'linkedin_profile', 'email')
        self.assertEqual((kept, dropped), ([], 2))

    def test_gate_stamps_rows_and_downstream_respects_it(self):
        engs = [{'id': 'ACoAA1', 'name': 'Jane', 'linkedinUrl': 'https://www.linkedin.com/in/jane', 'position': 'CRO'},
                {'id': 'ACoAA2', 'name': 'Bob', 'linkedinUrl': 'https://www.linkedin.com/in/bob', 'position': 'CRO'}]
        base = self.run_dir('g', engs)
        con = ledger.connect()
        icp = json.loads((base / 'recipient_icp.json').read_text())
        ledger.record_scores(con, engs, [explained({'icp_fit': 5}), explained({'icp_fit': 5})], icp, rank.SYSTEM, 'in-session', 'g')
        con.execute("INSERT INTO suppressions VALUES ('url:https://www.linkedin.com/in/bob', 'teammate', 'now')"); con.commit()
        common.write_csv(base / 'ranked_engagers.csv',
                         [{'name': e['name'], 'icp_fit': '5', 'actor_id': e['id'], 'linkedin_url': e['linkedinUrl']} for e in engs],
                         ['name', 'icp_fit', 'actor_id', 'linkedin_url'])
        self.assertEqual(ledger.gate(con, base), {'eligible': 1, 'suppressed': 1})
        rows = {r['name']: r for r in common.read_csv(base / 'ranked_engagers.csv')[0]}
        self.assertEqual(rows['Bob']['ledger_status'], 'suppressed:teammate')
        self.assertEqual(activity.eligible(rows['Bob'], 4), ('', 'ledger_suppressed'))
        self.assertEqual(activity.eligible(rows['Jane'], 4)[1], '')
        self.assertEqual(len(push.select_leads([{**verified_row(), **rows['Bob']}])[0]), 0)
        self.assertIn('Ledger gate', '\n'.join(summary.summarize(list(rows.values()))))
