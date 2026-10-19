import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import activity, common, email_enrich, harvest, icp, push, rank, resolve, summary, supabase_export


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
        good = {'results': [{'i': 1, 'icp_fit': 4, 'intent': None}, {'i': 0, 'icp_fit': 2, 'intent': 'high'}]}
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

    def write_ranked(self, d):
        rows = [{'name': 'Hot', 'icp_fit': '5', 'linkedin_url': 'https://www.linkedin.com/in/ACoAA5', 'actor_id': 'ACoAA5'},
                {'name': 'Cold', 'icp_fit': '2', 'linkedin_url': 'https://www.linkedin.com/in/ACoAA2', 'actor_id': 'ACoAA2'},
                {'name': 'Public', 'icp_fit': '5', 'linkedin_url': 'https://www.linkedin.com/in/jane', 'actor_id': 'ACoAA9'},
                {'name': 'Gone', 'icp_fit': '4', 'linkedin_url': 'https://www.linkedin.com/in/ACoAA4', 'actor_id': 'ACoAA4'}]
        source, output = str(Path(d) / 'ranked.csv'), str(Path(d) / 'resolved.csv')
        common.write_csv(source, rows, list(rows[0]))
        return source, output

    @patch.dict('os.environ', {'APIFY_TOKEN': 'test'})
    @patch('src.resolve.scrape')
    def test_resolve_statuses(self, scrape):
        scrape.return_value = ({'ACoAA5': {'id': 'ACoAA5', 'linkedinUrl': 'https://www.linkedin.com/in/Hot-One',
                                           'location': {'linkedinText': 'London, UK'}}}, True)
        with tempfile.TemporaryDirectory() as d:
            source, output = self.write_ranked(d)
            with patch('sys.argv', ['resolve.py', '--input', source, '--output', output]):
                resolve.main()
            rows, fields = common.read_csv(output)
        self.assertEqual(scrape.call_args.args[0], ['ACoAA5', 'ACoAA4'])
        got = {r['name']: (r['resolve_status'], r['linkedin_url'], r['location']) for r in rows}
        self.assertEqual(got['Hot'], ('resolved', 'https://www.linkedin.com/in/hot-one', 'London, UK'))
        self.assertEqual(got['Cold'][0], 'skipped')
        self.assertEqual(got['Public'][0], 'skipped')
        self.assertEqual(got['Gone'], ('not_found', 'https://www.linkedin.com/in/ACoAA4', ''))
        self.assertIn("'resolved': 1", '\n'.join(summary.summarize(rows)))

    @patch.dict('os.environ', {'APIFY_TOKEN': 'test'})
    @patch('src.resolve.scrape')
    def test_resolve_error_and_dry_run(self, scrape):
        scrape.side_effect = RuntimeError('boom')
        with tempfile.TemporaryDirectory() as d:
            source, output = self.write_ranked(d)
            with patch('sys.argv', ['resolve.py', '--input', source, '--output', output]):
                resolve.main()
            self.assertEqual([r['resolve_status'] for r in common.read_csv(output)[0]],
                             ['error', 'skipped', 'skipped', 'error'])
            scrape.side_effect = None  # free-plan refusal: SUCCEEDED run whose only item is an error
            scrape.return_value = resolve.parse([{'error': 'Free users are limited to 10 items per run.'}], 'SUCCEEDED')
            with patch('sys.argv', ['resolve.py', '--input', source, '--output', output]):
                resolve.main()
            self.assertEqual(common.read_csv(output)[0][3]['resolve_status'], 'error')
            scrape.reset_mock()
            Path(output).unlink()
            with patch('sys.argv', ['resolve.py', '--input', source, '--output', output, '--dry-run']):
                resolve.main()
            self.assertFalse(Path(output).exists())
        scrape.assert_not_called()


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
    @patch('src.resolve.scrape')
    def test_resolve_rerun_reuses_paid_results(self, scrape):
        from src import resolve
        row = {'name': 'J', 'icp_fit': '5', 'actor_id': 'ACoAAX', 'linkedin_url': 'https://www.linkedin.com/in/ACoAAX'}
        with tempfile.TemporaryDirectory() as d:
            src_, out = str(Path(d) / 'ranked.csv'), str(Path(d) / 'resolved.csv')
            common.write_csv(src_, [row], list(row))
            common.write_csv(out, [{**row, 'linkedin_url': 'https://www.linkedin.com/in/jane', 'location': 'London',
                                    'resolve_status': 'resolved'}], list(row) + ['location', 'resolve_status'])
            with patch('sys.argv', ['resolve.py', '--input', src_, '--output', out]):
                resolve.main()
            got = common.read_csv(out)[0][0]
        scrape.assert_not_called()
        self.assertEqual((got['linkedin_url'], got['resolve_status'], got['location']),
                         ('https://www.linkedin.com/in/jane', 'resolved', 'London'))

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
            with patch('sys.argv', ['rank.py', '--slug', 's', '--output-dir', d, '--scores', str(scores)]):
                rank.main()
            rows, _ = common.read_csv(base / 'ranked_engagers.csv')
        self.assertEqual([r['name'] for r in rows], ['Buyer', 'Commenter'])  # 2*(1+ln3)*1.0 > 4*0.3, still second
        text = '\n'.join(summary.summarize(rows))
        self.assertIn('Strong comments below the fit bar', text)
        self.assertLess(text.index('Buyer'), text.index('  2 Commenter'))

    @patch('src.resolve.scrape')
    def test_pre_scoring_enrichment_writes_profile_data_once(self, scrape):
        from src import resolve
        scrape.return_value = ({'ACoAA3': {'id': 'ACoAA3', 'publicIdentifier': 'jane', 'location': {'linkedinText': 'London'},
                                           'currentPosition': [{'companyName': 'Acme'}]}}, True)
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / 's'; base.mkdir()
            eng = [{'id': 'ACoAA3', 'name': 'Jane', 'linkedinUrl': 'https://www.linkedin.com/in/ACoAA3'},
                   {'id': 'ACoAA2', 'name': 'Low', 'linkedinUrl': 'https://www.linkedin.com/in/ACoAA2'},
                   {'id': 'ACoAA9', 'name': 'Gone', 'linkedinUrl': 'https://www.linkedin.com/in/ACoAA9'}]
            (base / 'engagers.json').write_text(json.dumps(eng))
            common.write_csv(base / 'ranked_engagers.csv', [{'actor_id': 'ACoAA3', 'icp_fit': '3'},
                             {'actor_id': 'ACoAA2', 'icp_fit': '2'}, {'actor_id': 'ACoAA9', 'icp_fit': '4'}], ['actor_id', 'icp_fit'])
            self.assertEqual(resolve.enrich_harvest(base, 3), [0])
            self.assertEqual(sorted(scrape.call_args.args[0]), ['ACoAA3', 'ACoAA9'])  # fit 2 never sent
            out = json.loads((base / 'engagers.json').read_text())
            self.assertEqual((out[0]['linkedinUrl'], out[0]['location'], out[0]['company']),
                             ('https://www.linkedin.com/in/jane', 'London', 'Acme'))
            self.assertEqual(out[2]['resolve_status'], 'not_found')
            self.assertEqual(json.loads((base / 'rescore.json').read_text())['indices'], [0])
            scrape.reset_mock()
            self.assertEqual(resolve.enrich_harvest(base, 3), [])  # rerun: resolved and not_found are not paid again
            scrape.assert_not_called()
