"""Current employment, employer alignment and durable three-way ICP decisions."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src import activity, common, draft, email_enrich, evidence, getleads, harvest, ledger, push, push_heyreach, rank, resolve, summary


class ProfileReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(patch.dict(os.environ, {'LEDGER_DB': str(self.root / 'ledger.sqlite')}))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.con = ledger.connect()
        self.addCleanup(self.con.close)

    def person(self, **kw):
        return dict(id='ACoAA1', name='Jane', linkedinUrl='https://www.linkedin.com/in/jane',
                    company='Current Corp', location='United States', position='Building AI',
                    comments=[], **kw)

    def actor(self):
        return {'id': 'ACoAA1', 'name': 'Jane', 'headline': 'Building AI', 'location': 'United States',
                'publicIdentifier': 'jane', 'about': 'B2B software for revenue teams.',
                'currentPosition': [{'position': 'Global Head of Growth', 'companyName': 'Current Corp',
                    'companyLinkedinUrl': 'https://www.linkedin.com/company/current-corp/', 'companyId': 123,
                    'startDate': {'text':'Aug 2026','year':2026}, 'endDate': {'text':'Present'}, 'employmentType': 'Full-time',
                    'description': 'Leads revenue and sales.', 'location': 'Germany'}]}

    def score(self, review=True, fit=5):
        return {'i': 0, 'icp_fit': fit, 'needs_review': review, 'intent': None,
                'qualification_reason': 'Current leadership fits; required team size needs confirmation.' if review else 'Observed role and country fit.',
                'intent_reason': 'Reaction only.', 'clean_title': 'Stale title'}

    def seed(self, e=None):
        base = self.root / 'run'; base.mkdir(exist_ok=True)
        common.write_json(base / 'engagers.json', [e or self.person()])
        common.write_json(base / 'recipient_icp.json', {'titles': ['Head of Growth'], 'geos': ['United States']})
        ledger.ingest(self.con, base)
        return base

    def rank(self, base, result=None, threshold=4):
        args = ['rank.py', '--legacy', '--slug', base.name, '--output-dir', str(self.root), '--min-fit', str(threshold)]
        if result is not None:
            es=json.loads((base/'engagers.json').read_text()); icp=json.loads((base/'recipient_icp.json').read_text())
            common.write_json(base/'scores.json', {'results': [{**result, 'score_context': ledger.score_context(es[0], icp, rank.SYSTEM)}]})
            args+=['--scores',str(base/'scores.json')]
        with patch('sys.argv',args), patch.object(rank,'llm_json') as llm:
            rank.main()
        llm.assert_not_called()
        ledger.gate(self.con,base)
        return common.read_csv(base/'ranked_engagers.csv')[0][0]

    def test_harvest_preserves_current_role_dates_identity_and_description(self):
        items=[{'type':'reaction','actor':self.actor(),'postId':'p','reactionType':'LIKE'}]
        e=harvest.build_engagers(items)[0]
        role=e['profile_evidence']['current_roles'][0]
        self.assertEqual((role['title'],role['company_id'],role['start_date']),('Global Head of Growth','123','Aug 2026'))
        self.assertEqual(e['location'],'United States') # job location is Germany
        e['icp_enrichment']={'title':'Assistant Manager','company':'Current Corp','person_country':'Germany','company_size':'501 to 1000'}
        line=rank.engager_lines([e])[0]
        self.assertIn('Global Head of Growth',line)
        self.assertNotIn('Assistant Manager',line)
        self.assertNotIn('person_country',line)
        base=self.seed(e)
        row=self.rank(base,self.score(review=False))
        self.assertEqual(row['clean_title'],'Global Head of Growth')
        self.assertEqual(row['location'],'United States')

    def test_past_experience_is_not_mistaken_for_current_employment(self):
        actor={'experience':[{'companyName':'Old Corp','position':'CEO','endDate':'2023'}]}
        self.assertEqual(harvest.profile_fields(actor)[2],'')
        actor['experience'].append({'companyName':'New Corp','position':'Founder','endDate':'Present'})
        self.assertEqual(harvest.profile_fields(actor)[2],'New Corp')
        self.assertEqual(evidence.company_url('https://www.linkedin.com/search/results/all/?keywords=corp'),'')

    def test_conflicting_and_ambiguous_company_facts_are_not_scoring_evidence(self):
        e=self.person()
        e['icp_enrichment']={'company':'Old Corp','company_size':'1 to 10','company_industry':'Unrelated industry'}
        allowed,status=evidence.scoring_enrichment(e)
        self.assertEqual(status,'conflict')
        self.assertNotIn('company_size',allowed)
        self.assertNotIn('Unrelated industry',rank.engager_lines([e])[0])
        e['company']='A/B';e['icp_enrichment']['company']='A/B'
        self.assertEqual(evidence.scoring_enrichment(e)[1],'unconfirmed')
        e['company']='Current Corp';e['icp_enrichment']['company']='Current Corp'
        self.assertEqual(evidence.scoring_enrichment(e)[1],'matched_name_only')
        e['profile_evidence']=evidence.profile_evidence(self.actor())
        e['icp_enrichment']['company_linkedin_url']='https://www.linkedin.com/company/other/'
        self.assertEqual(evidence.scoring_enrichment(e)[1],'conflict')
        e['icp_enrichment']['company_linkedin_url']='https://www.linkedin.com/company/current-corp/'
        self.assertEqual(evidence.scoring_enrichment(e)[1],'matched_identity')

    def test_review_boolean_is_required_in_both_scoring_paths(self):
        for value in (None, 'false', 0):
            with self.assertRaisesRegex(ValueError,'needs_review'):
                rank.align({'results':[{**self.score(),'needs_review':value}]},1)
        with patch.object(rank,'llm_json',return_value=({'results':[self.score()]},0,0)):
            self.assertTrue(rank.score_batch('ICP',[self.person()])[0][0]['needs_review'])

    def test_review_is_cached_proven_and_not_cleared_by_threshold_or_csv_edit(self):
        base=self.seed()
        row=self.rank(base,self.score())
        self.assertEqual((row['qualification_status'],row['ledger_status']),('needs_review','needs_review'))
        cached=self.rank(base,threshold=1)
        self.assertEqual(cached['qualification_status'],'needs_review')
        forged={**cached,'needs_review':'false','qualification_status':'qualified','ledger_status':'eligible'}
        self.assertEqual(ledger.filter_export_rows(self.con,[forged],base)[0],[])
        self.assertIn("'needs_review': 1",'\n'.join(summary.summarize([row])))
        resolved=self.rank(base,self.score(review=False))
        self.assertEqual(resolved['qualification_status'],'qualified')
        self.assertEqual(ledger.filter_export_rows(self.con,[row],base)[0],[])

    def test_review_blocks_downstream_work_even_at_high_fit(self):
        base=self.seed();row=self.rank(base,self.score())
        row.update(email='jane@example.com',email_status='VERIFIED',sendable='true',email_checked_at=ledger.now())
        self.assertEqual(activity.eligible(row,1)[1],'qualification_hold')
        self.assertEqual(push.select_leads([row],min_fit=1)[0],[])
        self.assertEqual(push_heyreach.select_leads([row],min_fit=1)[0],[])
        with patch.object(getleads,'request') as request:
            result,_=email_enrich.discover([dict(row)],self.con,'run',base,min_fit=1)
        request.assert_not_called()
        self.assertEqual(result[0]['miss_reason'],'qualification_hold')
        with patch('sys.argv',['draft.py','--slug','run','--output-dir',str(self.root)]), patch.object(draft,'llm_json') as llm:
            draft.main()
        llm.assert_not_called()
        self.assertEqual(common.read_csv(base/'drafts.csv')[0],[])

    def test_profile_changes_invalidate_scores_and_survive_bare_harvests(self):
        e=self.person(profile_evidence=evidence.profile_evidence(self.actor()))
        base=self.seed(e);self.rank(base,self.score(review=False))
        cid=ledger.find_contact(self.con,ledger.aliases_of(e))
        common.write_json(base/'engagers.json',[self.person()]);ledger.ingest(self.con,base)
        restored=json.loads((base/'engagers.json').read_text())[0]
        self.assertEqual(restored['profile_evidence'],e['profile_evidence'])
        changed=json.loads(json.dumps(restored));changed['profile_evidence']['current_roles'][0]['title']='Analyst'
        self.assertNotEqual(ledger.evidence_hash(changed),ledger.evidence_hash(restored))
        self.assertEqual(ledger.fresh_scores(self.con,[changed],{'titles':['Head of Growth'],'geos':['United States']},rank.SYSTEM),{})
        # Changing employers cannot resurrect an old role through a later sparse harvest.
        newer={**self.person(),'company':'New Employer'}
        common.write_json(base/'engagers.json',[newer]);ledger.ingest(self.con,base)
        self.assertNotIn('profile_evidence',ledger.profile_state(self.con,cid))
        common.write_json(base/'engagers.json',[{'id':'ACoAA1'}]);ledger.ingest(self.con,base)
        self.assertNotIn('profile_evidence',json.loads((base/'engagers.json').read_text())[0])

    def test_profile_lookup_preserves_role_and_cached_replay(self):
        e={**self.person(),'linkedinUrl':''}
        base=self.seed(e)
        common.write_csv(base/'ranked_engagers.csv',[{'actor_id':'ACoAA1','icp_fit':4}],['actor_id','icp_fit'])
        with patch.object(resolve,'scrape',return_value=({'ACoAA1':self.actor()},True,'paid-run')), patch.dict(os.environ,{'APIFY_TOKEN':'offline'}):
            resolve.enrich_harvest(base,3)
        cached=ledger.cached_lookup(self.con,'ACoAA1')
        self.assertEqual(cached['profile_evidence']['current_roles'][0]['title'],'Global Head of Growth')

    def test_alias_merge_preserves_review_and_structured_profile(self):
        e=self.person(profile_evidence=evidence.profile_evidence(self.actor()))
        opaque={**e,'linkedinUrl':''}
        cid,_=ledger.contact_for(self.con,ledger.aliases_of(opaque))
        ledger.snapshot(self.con,cid,opaque,'lookup')
        icp={'titles':['Head of Growth']}
        ledger.record_scores(self.con,[opaque],[self.score(review=False)],icp,rank.SYSTEM,'test','old')
        public={**e,'id':'public-only'}
        ledger.record_scores(self.con,[public],[self.score(review=True)],icp,rank.SYSTEM,'test','new')
        merged,_=ledger.contact_for(self.con,ledger.aliases_of(e));self.con.commit()
        cached=ledger.fresh_scores(self.con,[e],icp,rank.SYSTEM)[0]
        self.assertTrue(cached['needs_review'])
        self.assertEqual(ledger.profile_state(self.con,merged)['profile_evidence'],e['profile_evidence'])

    def test_old_decision_migration_requires_rescore_and_keeps_paid_history(self):
        base=self.seed();self.rank(base,self.score())
        self.con.execute('UPDATE scores SET needs_review=NULL');self.con.commit()
        people=json.loads((base/'engagers.json').read_text());icp=json.loads((base/'recipient_icp.json').read_text())
        self.assertEqual(ledger.fresh_scores(self.con,people,icp,rank.SYSTEM),{})
        self.assertEqual(ledger.gate(self.con,base),{'stale_score':1})

    def test_high_fit_company_conflict_forces_review_and_keeps_raw_audit_facts(self):
        e=self.person(icp_enrichment={'company':'Old Corp','company_size':'51 to 200'})
        base=self.seed(e)
        common.write_json(base/'recipient_icp.json',{'titles':['Head of Growth'],'size_range':'50-2000'})
        row=self.rank(base,self.score(review=False))
        self.assertEqual(row['qualification_status'],'needs_review')
        self.assertEqual(row['getleads_company'],'Old Corp')
        self.assertEqual(row['getleads_company_alignment'],'conflict')

if __name__=='__main__':
    unittest.main()
