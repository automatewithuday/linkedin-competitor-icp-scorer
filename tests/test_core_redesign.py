"""Offline acceptance tests for the company-first qualification core."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import run as pipeline
from src import common, company_stage, core, coredb, getleads, harvest, ledger, profiles, qualify, profile_stage


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'ledger.sqlite'
        self.enterContext(patch.dict(os.environ,{'LEDGER_DB':str(self.path),'APIFY_TOKEN':'offline','CORE_RUN_SLUG':'test'}))
        self.out=io.StringIO();self.enterContext(contextlib.redirect_stdout(self.out))
        self.con=coredb.connect();self.addCleanup(self.con.close)
        self.doc={'schema_version':2,'name':'buyers','business':{'website':'https://seller.example','one_liner':'Sales software'},
            'segments':[{'id':'core','name':'Core buyers','company':[
                {'id':'industry','field':'industry','operator':'equals','value':'Software','mode':'mandatory'},
                {'id':'size','field':'headcount','operator':'range','value':[50,2000],'mode':'preferred'}],
                'person':[{'id':'title','field':'title','operator':'contains','value':'Sales','mode':'mandatory'}], 'signals':[]}]}
        self.profile_path=self.root/'profile'/'client-profile.yaml'
        self.v=profiles.write_profile(self.con,self.doc,self.profile_path)
        self.rid=coredb.new_request(self.con,'fixture','test','fixture',status='complete')
        self.raw_id=coredb.raw(self.con,self.rid,'fixture',{'source':'fictional test data'})

    def facts(self,n=1,**kw):
        return {'name':f'Company {n}','linkedin_url':f'https://www.linkedin.com/company/co{n}',
                'domain':f'co{n}.example','industry':'Software','description':'B2B software',
                'headcount':100,'identity_status':'confirmed',**kw}

    def company(self,n=1,**kw):
        cid=coredb.company(self.con,f'Company {n}',f'https://www.linkedin.com/company/co{n}',str(n))
        coredb.observe(self.con,'company',cid,self.facts(n,**kw),self.raw_id)
        return cid

    def person(self,n=1,co=1,title='VP Sales',**kw):
        e={'id':f'ACoAA{n}','name':f'Person {n}','linkedinUrl':f'https://www.linkedin.com/in/person{n}',
           'location':'United States','position':title,'company':f'Company {co}','posts_touched':['post1'],
           'comments':[],'reaction_types':['LIKE'],'profile_evidence':{'source':'linkedin_profile','current_roles':[
               {'title':title,'company':f'Company {co}','company_id':str(co),
                'company_linkedin_url':f'https://www.linkedin.com/company/co{co}','start_date':'2026','end_date':'Present'}]},**kw}
        return coredb.ingest_person(self.con,'run',e,self.raw_id)

    def version(self):return qualify.version(self.con,self.v['id'])

    def run_core(self,submitted=None):
        return qualify.run_assessments(self.con,self.v['id'],'run',submitted)

    def export(self):return qualify.export(self.con,self.v['id'],'run',self.root/'output')

    def calibration(self,batch=0,approve=True,corrections=None,reviewer='human'):
        cases=[]
        for i in range(10):
            n=100+batch*10+i;co=self.company(n,industry='Consumer' if i%3==1 else 'Software')
            cases.append({'company_id':co,'category':['likely_match','likely_non_match','edge_case'][i%3]})
        rid=profiles.start_round(self.con,self.v['id'],cases)
        packet=profiles.round_packet(self.con,rid)
        results=[{'company_id':c['company_id'],'score_context':c['score_context'],
                  'decision':'not_qualified' if c['evidence']['facts']['industry']=='Consumer' else 'qualified',
                  'confidence':1.0,'reason':'Fictional structured industry condition evaluated.','criterion_results':[]} for c in packet['cases']]
        profiles.submit_round(self.con,rid,results)
        profiles.review_round(self.con,rid,approve,corrections or [],reviewer,'Fixture human feedback: approved' if approve else 'Fixture corrections')
        return rid

    def lock_fixture(self):
        # Simulated human feedback for fictional fixtures only; no real ICP is approved.
        self.calibration(0);self.calibration(1)

    def test_intake_resumes_and_requires_explicit_criterion_modes(self):
        profiles.save_answer(self.con,'new','website','https://seller.example')
        self.assertEqual(profiles.next_question(self.con,'new')[0],'one_liner')
        other=coredb.connect(self.path);self.addCleanup(other.close)
        profiles.save_answer(other,'new','one_liner','Software')
        self.assertEqual(profiles.next_question(self.con,'new')[0],'best_customers')
        profiles.save_answer(other,'new','segments','Core, Partners')
        prefix='segment:Core:'
        profiles.save_answer(other,'new',prefix+'headcount','50-2000')
        with self.assertRaises(ValueError):profiles.save_answer(other,'new',prefix+'headcount:mode','')
        profiles.save_answer(other,'new',prefix+'headcount:mode','preferred')
        answers=json.loads(other.execute("SELECT answers FROM intake_sessions WHERE profile='new'").fetchone()[0])
        doc=profiles.document_from_answers('new',answers)
        self.assertEqual(len(doc['segments']),2)
        self.assertEqual(doc['segments'][0]['company'][0]['mode'],'preferred')
        self.assertEqual(doc['segments'][0]['company'][0]['value'],[50,2000])

    def test_old_style_icp_yaml_is_rejected_and_draft_icp_cannot_harvest(self):
        with self.assertRaisesRegex(ValueError,'schema_version 2'):
            profiles.register(self.con,{'titles':['CEO'],'company_size':'50-2000'},self.profile_path)
        self.assertEqual(self.version()['state'],'draft')
        with patch.object(core.subprocess,'run') as provider:
            with self.assertRaisesRegex(ValueError,'not calibrated'):
                core.main(['--slug','x','--icp',str(self.profile_path),'--post','https://linkedin.com/posts/x'])
        provider.assert_not_called()

    def test_calibration_needs_two_human_rounds_and_corrections_reset_streak(self):
        self.calibration(0)
        self.assertEqual(self.version()['approval_streak'],1)
        self.calibration(1,corrections=['Wrong reasoning'])
        self.assertEqual(self.version()['approval_streak'],0)
        self.calibration(2);self.calibration(3)
        self.assertEqual(self.version()['state'],'locked')
        round_id=self.con.execute('SELECT MAX(id) FROM calibration_rounds').fetchone()[0]
        with self.assertRaisesRegex(ValueError,'unreviewed'):profiles.review_round(self.con,round_id,True,[],'human','Fixture approved')

    def test_agent_cannot_approve_calibration(self):
        with self.assertRaisesRegex(ValueError,'agents cannot self-approve'):self.calibration(0,reviewer='agent')
        self.assertEqual(self.version()['state'],'calibrating')
        self.assertEqual(self.version()['approval_streak'],0)

    def test_calibration_rejects_repeated_companies_and_stale_evidence(self):
        rid=self.calibration(0)
        cases=json.loads(self.con.execute('SELECT cases FROM calibration_rounds WHERE id=?',(rid,)).fetchone()[0])
        with self.assertRaisesRegex(ValueError,'new batch'):profiles.start_round(self.con,self.v['id'],cases)
        # New round, but company data changes after scoring, before human review.
        cases=[{'company_id':self.company(200+i),'category':['likely_match','likely_non_match','edge_case'][i%3]} for i in range(10)]
        rid=profiles.start_round(self.con,self.v['id'],cases);packet=profiles.round_packet(self.con,rid)
        results=[{'company_id':c['company_id'],'score_context':c['score_context'],'decision':'qualified','confidence':1,
                  'reason':'Fictional match.','criterion_results':[]} for c in packet['cases']]
        profiles.submit_round(self.con,rid,results)
        coredb.observe(self.con,'company',cases[0]['company_id'],self.facts(200,industry='Consumer'),self.raw_id)
        with self.assertRaisesRegex(ValueError,'Evidence changed'):profiles.review_round(self.con,rid,True,[],'human','Fixture approved')

    def test_rules_or_prompt_change_creates_draft_without_deleting_evidence(self):
        self.lock_fixture();co=self.company();before=self.con.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0]
        changed=copy.deepcopy(self.doc);changed['segments'][0]['company'][0]['value']='Manufacturing'
        v=profiles.register(self.con,changed,self.profile_path)
        self.assertNotEqual(v['id'],self.v['id']);self.assertEqual(v['state'],'draft')
        self.assertEqual(coredb.state(self.con,'company',co)['facts']['industry'],'Software')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],before)
        updated=profiles.register(self.con,changed,self.profile_path,'A newly tuned prompt')
        self.assertNotEqual(updated['id'],v['id']);self.assertEqual(updated['approval_streak'],0)

    def test_missing_preferences_do_not_block_but_missing_requirements_do(self):
        co=self.company(headcount=None);self.person();self.run_core()
        companies=self.con.execute("SELECT * FROM current_assessments WHERE entity_type='company'").fetchall()
        self.assertEqual(companies[0]['decision'],'qualified')
        coredb.observe(self.con,'company',co,self.facts(industry=None),self.raw_id);self.run_core()
        self.assertEqual(self.con.execute("SELECT decision FROM current_assessments WHERE entity_type='company'").fetchone()[0],'needs_review')

    def test_company_before_person_and_no_cross_segment_qualification(self):
        doc=copy.deepcopy(self.doc)
        doc['segments'].append({'id':'partner','company':[{'id':'agency','field':'industry','operator':'equals','value':'Agency','mode':'mandatory'}],
                               'person':[{'id':'founder','field':'title','operator':'contains','value':'Founder','mode':'mandatory'}], 'signals':[]})
        self.v=profiles.register(self.con,doc,self.profile_path)
        self.company(industry='Software');self.person(title='Founder');self.run_core()
        rows,qualified=self.export()
        self.assertEqual(rows[0]['qualification_status'],'not_qualified');self.assertEqual(qualified,[])
        pairs=json.loads(rows[0]['employment_decisions'])
        self.assertEqual({p['segment'] for p in pairs},{'core','partner'})
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM assessments WHERE entity_type='person' AND segment='partner'").fetchone()[0],0)

    def test_qualified_view_is_joined_and_stale_scores_disappear(self):
        self.lock_fixture();co=self.company();self.person();self.run_core()
        rows,yes=self.export();self.assertEqual(len(yes),1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],1)
        coredb.observe(self.con,'company',co,self.facts(industry='Consumer'),self.raw_id)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],0)
        self.run_core();self.assertEqual(self.export()[0][0]['qualification_status'],'not_qualified')
        all_rows,fields=common.read_csv(self.root/'output'/'all_leads.csv')
        self.assertTrue(all_rows[0]['company_reason']);self.assertTrue(all_rows[0]['score_context'])
        self.assertIn('evidence_refs',fields)
        self.assertEqual(common.read_csv(self.root/'output'/'qualified_leads.csv')[0],[])

    def test_draft_icp_exports_are_provisional_and_never_qualified_only(self):
        self.company();self.person();self.run_core();rows,yes=self.export()
        self.assertEqual(rows[0]['company_decision'],'qualified');self.assertEqual(rows[0]['person_decision'],'qualified')
        self.assertEqual(rows[0]['qualification_status'],'needs_review');self.assertEqual(yes,[])

    def test_two_people_share_one_company_assessment_and_enrichment(self):
        self.company(headcount=None,industry=None);self.person(1);self.person(2)
        response=[{'id':'1','name':'Company 1','linkedinUrl':'https://www.linkedin.com/company/co1','employeeCount':100,'industry':'Software','website':'co1.example','description':'Software'}]
        with patch.object(company_stage.harvest,'post_run',return_value={'id':'r1','defaultDatasetId':'d1','status':'SUCCEEDED'}) as post, \
             patch.object(company_stage.harvest,'get_items',return_value=response):
            company_stage.enrich_companies(self.con,self.version(),'run')
            company_stage.enrich_companies(self.con,self.version(),'run')
        self.assertEqual(post.call_count,1)
        self.run_core()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM current_assessments WHERE entity_type='company'").fetchone()[0],1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM current_assessments WHERE entity_type='person'").fetchone()[0],2)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM apify_records").fetchone()[0],1)

    def test_company_search_requires_independent_identity_evidence(self):
        co=coredb.company(self.con,'Same Name',unresolved_key='a')
        coredb.observe(self.con,'company',co,{'name':'Same Name','identity_status':'unresolved'},self.raw_id)
        records=[{'name':'Same Name','id':'700','linkedinUrl':'https://www.linkedin.com/company/samename','industry':'Software'}]
        self.assertFalse(company_stage.attach(self.con,co,records,self.raw_id))
        self.assertEqual(coredb.state(self.con,'company',co)['facts']['identity_status'],'unresolved')
        a=coredb.company(self.con,'Same Name',unresolved_key='other')
        self.assertNotEqual(co,a)

    def test_employer_change_cannot_transfer_previous_company_facts(self):
        self.lock_fixture();old=self.company(1);self.person();self.run_core()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],1)
        self.person(co=2)
        emps=qualify.relationships(self.con,'run');self.assertEqual(len(emps),1)
        self.assertNotEqual(emps[0]['company_id'],old)
        self.assertNotIn('industry',coredb.state(self.con,'company',emps[0]['company_id'])['facts'])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],0)

    def test_unknown_paid_outcomes_and_cross_run_claims_do_not_resubmit(self):
        co=self.company(industry=None);self.person()
        with patch.object(company_stage.harvest,'post_run',side_effect=RuntimeError('timeout')) as post:
            with self.assertRaisesRegex(RuntimeError,'requires review'):company_stage.enrich_companies(self.con,self.version(),'run')
            company_stage.enrich_companies(self.con,self.version(),'run')
        self.assertEqual(post.call_count,1)
        other=coredb.connect(self.path);self.addCleanup(other.close)
        old,start=company_stage.claim(other,'other','https://www.linkedin.com/company/co1',{'companies':['x']},10,1,0.02)
        self.assertFalse(start);self.assertEqual(old['status'],'unknown')
        self.assertEqual(coredb.state(self.con,'company',co)['facts']['identity_status'],'confirmed')

    def test_budget_cap_survives_rerun(self):
        for i in (1,2):self.company(i,industry=None);self.person(i,co=i)
        with patch.object(company_stage.harvest,'post_run',return_value={'id':'run','defaultDatasetId':'data','status':'SUCCEEDED'}) as post,patch.object(company_stage.harvest,'get_items',return_value=[]):
            company_stage.enrich_companies(self.con,self.version(),'run',max_lookups=1)
            company_stage.enrich_companies(self.con,self.version(),'run',max_lookups=1)
        self.assertEqual(post.call_count,1)

    def test_raw_survives_normalization_failure_and_restart(self):
        malformed={'ok':True,'results':'bad shape','provider_extra':{'work_email':'private@example.com','cellphone':'fictional'}}
        with patch.object(getleads,'request',return_value=malformed):
            with self.assertRaises(getleads.ProviderError):getleads.enrich_batch(['https://www.linkedin.com/in/person1'],'offline')
        saved=self.con.execute("SELECT data FROM getleads_records WHERE record_key='response'").fetchone()[0]
        self.assertEqual(json.loads(saved),malformed)
        with self.assertRaises(sqlite3.IntegrityError):self.con.execute('UPDATE raw_records SET data=?',('{}',))
        self.con.rollback()
        other=coredb.connect(self.path);self.addCleanup(other.close)
        self.assertEqual(json.loads(other.execute("SELECT data FROM getleads_records WHERE record_key='response'").fetchone()[0]),malformed)

    def test_harvest_raw_is_saved_and_completed_request_replays_without_post(self):
        actor={'id':'ACoAA1','name':'Person 1','extra':{'all':'retained'}}
        items=[{'type':'reaction','actor':actor,'postId':'p'}]
        with patch.object(harvest,'post_run',return_value={'id':'r','defaultDatasetId':'d','status':'SUCCEEDED'}) as post,patch.object(harvest,'get_items',return_value=items):
            first,_=harvest.run_actor('actor',{'targetUrls':['example']},'offline',0.1)
            second,_=harvest.run_actor('actor',{'targetUrls':['example']},'offline',0.1)
        self.assertEqual(first,second);self.assertEqual(post.call_count,1)
        self.assertEqual(json.loads(self.con.execute("SELECT data FROM apify_records WHERE record_key='dataset'").fetchone()[0]),items)

    def test_required_signals_hold_and_soft_signals_do_not(self):
        segment=copy.deepcopy(self.doc['segments'][0]);segment['signals']=[{'id':'hire','kind':'hiring_sdr','entity':'company',
            'mandatory':False,'max_age_days':30,'evidence_required':'Dated SDR vacancy URL'}]
        missing,_,_=qualify.signals_for(segment,'company',{})
        self.assertEqual(missing,[])
        segment['signals'][0]['mandatory']=True
        self.assertEqual(qualify.signals_for(segment,'company',{})[0],['hire'])
        date=datetime.now(timezone.utc).date().isoformat()
        facts={'signals':[{'kind':'hiring_sdr','event_date':date,'source_url':'https://co.example/jobs/1'}]}
        self.assertEqual(qualify.signals_for(segment,'company',facts)[0],[])
        self.assertTrue(qualify.signals_for(segment,'company',facts)[2])
        facts['signals'][0]['event_date']=None
        self.assertEqual(qualify.signals_for(segment,'company',facts)[0],['hire'])

    def test_semantic_judgments_are_bound_to_evidence_and_low_confidence_holds(self):
        criterion={'id':'fit','field':'description','operator':'semantic','mode':'mandatory','value':'B2B','description':'B2B software'}
        judgments=[{'criterion_id':'fit','outcome':'match','confidence':0.9,'reason':'The description says B2B software.',
                   'evidence_quote':'B2B software','evidence_refs':[1]}]
        result=qualify.evaluate([criterion],{'description':'B2B software'},[1],judgments)
        self.assertEqual(result['decision'],'qualified')
        judgments[0]['confidence']=0.5
        self.assertEqual(qualify.evaluate([criterion],{'description':'B2B software'},[1],judgments)['decision'],'needs_review')
        judgments[0]['evidence_refs']=[999]
        with self.assertRaisesRegex(ValueError,'supplied observations'):qualify.evaluate([criterion],{'description':'B2B software'},[1],judgments)
        judgments[0].update(evidence_refs=[1],evidence_quote='Invented')
        with self.assertRaisesRegex(ValueError,'supporting quote'):qualify.evaluate([criterion],{'description':'B2B software'},[1],judgments)

    def test_migration_backup_keeps_lookup_history_and_labels_incomplete_evidence(self):
        old=self.root/'old.sqlite';legacy=ledger.connect(old)
        legacy.execute('INSERT INTO lookups VALUES (?,?,?,?,?)',('ACoAAold','resolved','https://linkedin.com/in/old','paid','then'))
        legacy.commit();legacy.close()
        migrated=coredb.connect(old);self.addCleanup(migrated.close)
        self.assertTrue(Path(str(old)+'.before-core-v2.sqlite').exists())
        self.assertEqual(migrated.execute('SELECT run_id FROM lookups').fetchone()[0],'paid')
        base=self.root/'saved';base.mkdir()
        common.write_json(base/'engagers.json',[{'id':'ACoAAold','name':'Old','linkedinUrl':'https://linkedin.com/in/old','position':'CEO'}])
        common.write_json(base/'posts.json',[{'postId':'p','content':'post'}])
        coredb.import_saved(migrated,base);n=migrated.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0]
        coredb.import_saved(migrated,base)
        self.assertEqual(migrated.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],n)
        self.assertEqual(migrated.execute("SELECT COUNT(*) FROM raw_records WHERE completeness='full'").fetchone()[0],0)
        self.assertEqual(migrated.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_contact_alias_merge_keeps_core_relationships(self):
        self.company();a=self.person(1)
        e={'id':'public','name':'Also person 1','linkedinUrl':'https://www.linkedin.com/in/alias','company':'Company 1',
           'profile_evidence':{'current_roles':[{'company':'Company 1','company_id':'1','title':'VP Sales'}]}}
        b=coredb.ingest_person(self.con,'run',e,self.raw_id);self.assertNotEqual(a,b)
        # Discover both aliases refer to the same person.
        merged,_=ledger.contact_for(self.con,['id:ACoAA1','url:https://www.linkedin.com/in/alias'])
        self.con.commit();self.assertEqual(merged,a)
        self.assertEqual(self.con.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM core_run_contacts').fetchone()[0],1)

    def test_suppression_and_campaign_history_remove_eligible_view_rows(self):
        self.lock_fixture();self.company();self.person();self.run_core()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],1)
        ledger.record_campaign(self.con,[('https://www.linkedin.com/in/person1',None)],'campaign','imported')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],0)
        self.assertEqual(self.export()[1],[])

    def test_preflight_company_dry_run_never_calls_provider(self):
        self.company(industry=None);self.person()
        with patch.object(company_stage.harvest,'post_run') as post,patch.object(getleads,'request') as gl:
            company_stage.enrich_companies(self.con,self.version(),'run',dry_run=True)
            company_stage.getleads_gaps(self.con,self.version(),'run',dry_run=True)
        post.assert_not_called();gl.assert_not_called()

    def test_semantic_company_then_person_packets_and_bound_results(self):
        doc=copy.deepcopy(self.doc)
        doc['segments'][0]['company']=[{'id':'b2b','field':'description','operator':'semantic','value':'B2B','mode':'mandatory'}]
        doc['segments'][0]['person']=[{'id':'buyer','field':'title','operator':'semantic','value':'Sales leadership','mode':'mandatory'}]
        self.v=profiles.register(self.con,doc,self.profile_path)
        self.company();self.person();packets,pending=self.run_core()
        self.assertEqual([p['entity_type'] for p in pending],['company'])
        def judgment(p,criterion,quote):
            return {k:p[k] for k in ('entity_type','entity_id','employment_id','segment','score_context')} | {
                'decision':'qualified','confidence':0.9,'reason':'Explicit fictional evidence supports the criterion.',
                'criterion_results':[{'criterion_id':criterion,'outcome':'match','confidence':0.9,
                    'reason':'Explicit supporting statement.','evidence_quote':quote,'evidence_refs':[p['observation_ids'][0]]}]}
        cj=judgment(pending[0],'b2b','B2B software')
        packets,pending=self.run_core([cj])
        self.assertEqual([p['entity_type'] for p in pending],['person'])
        pj=judgment(pending[0],'buyer','VP Sales')
        packets,pending=self.run_core([pj]);self.assertEqual(pending,[])
        self.assertEqual(self.export()[0][0]['person_decision'],'qualified')
        self.assertEqual(self.export()[1],[]) # draft, not human-calibrated
        cj['score_context']='stale'
        with self.assertRaisesRegex(ValueError,'Stale or unbound'):self.run_core([cj])

    def test_invalid_batch_rolls_back_all_assessment_writes(self):
        self.company(1);self.company(2);self.person(1);self.person(2,co=2)
        packets,_=self.run_core();count=self.con.execute('SELECT COUNT(*) FROM assessments').fetchone()[0]
        companies=[p for p in packets if p['entity_type']=='company'];submitted=[]
        for p in companies:
            submitted.append({k:p[k] for k in ('entity_type','entity_id','employment_id','segment','score_context')} | {
                'decision':'qualified','confidence':1,'reason':'Fixture revised explanation.','criterion_results':[]})
        submitted[-1]['score_context']='invalid'
        with self.assertRaises(ValueError):self.run_core(submitted)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM assessments').fetchone()[0],count)

    def test_profile_resolution_saves_full_response_and_reuses_it(self):
        self.company();self.person(linkedinUrl='',profile_evidence={})
        items=[{'id':'ACoAA1','name':'Person 1','publicIdentifier':'person1','location':'United States',
                'headline':'VP Sales','unknown_provider_field':'retained','currentPosition':[
                    {'position':'VP Sales','companyName':'Company 1','companyId':'1','companyLinkedinUrl':'https://www.linkedin.com/company/co1'}]}]
        with patch.object(harvest,'post_run',return_value={'id':'profile-run','defaultDatasetId':'profile-data','status':'SUCCEEDED'}) as post,patch.object(harvest,'get_items',return_value=items):
            profile_stage.enrich_profiles(self.con,'run')
            profile_stage.enrich_profiles(self.con,'run')
        self.assertEqual(post.call_count,1)
        cid=self.con.execute('SELECT contact_id FROM core_run_contacts').fetchone()[0]
        self.assertEqual(coredb.state(self.con,'person',cid)['facts']['current_roles'][0]['title'],'VP Sales')
        self.assertIn('unknown_provider_field',self.con.execute("SELECT data FROM apify_records WHERE record_key='dataset'").fetchone()[0])
        self.assertEqual(self.con.execute('SELECT run_id FROM lookups').fetchone()[0],'profile-run')
        self.assertEqual(self.con.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_sample_zero_comments_does_not_submit_an_unlimited_comment_actor(self):
        with patch('sys.argv',['harvest.py','--post','https://www.linkedin.com/posts/example-post/',
                              '--slug','sample','--output-dir',str(self.root),'--max-reactions','10','--max-comments','0']), \
             patch.object(harvest,'run_actor',return_value=([],{'usageTotalUsd':0})) as run:
            harvest.main()
        self.assertEqual(run.call_count,1)
        self.assertEqual(run.call_args.args[0],harvest.POST_ACTORS['reaction'])
        self.assertLessEqual(run.call_args.args[3],1.0)

    def test_core_entrypoint_offline_export_and_summary(self):
        self.lock_fixture();self.company()
        base=self.root/'run';base.mkdir()
        person={'id':'ACoAA1','name':'Offline Person','linkedinUrl':'https://www.linkedin.com/in/person1',
                'position':'VP Sales','company':'Company 1','location':'US','profile_evidence':{'current_roles':[
                    {'title':'VP Sales','company':'Company 1','company_id':'1','company_linkedin_url':'https://www.linkedin.com/company/co1'}]}}
        common.write_json(base/'engagers.json',[person]);common.write_json(base/'posts.json',[])
        with patch.object(harvest,'post_run') as post,patch.object(getleads,'request') as gl:
            core.main(['--slug','run','--icp',str(self.profile_path),'--output-dir',str(self.root),
                       '--skip-harvest','--no-resolve','--no-enrich'])
        post.assert_not_called();gl.assert_not_called()
        self.assertEqual(len(common.read_csv(base/'qualified_leads.csv')[0]),1)
        self.assertEqual(json.loads((base/'core-run.json').read_text())['schema_version'],2)
        from src import summary
        self.assertIn('Eligible: 1','\n'.join(summary.summarize(common.read_csv(base/'all_leads.csv')[0])))

    def test_signal_import_is_idempotent_and_expiry_removes_view_results(self):
        self.lock_fixture();co=self.company();self.person();self.run_core()
        item={'entity_type':'company','entity_id':co,'kind':'hiring_sdr','source_url':'https://co1.example/jobs','event_date':None,'detail':'No event date supplied.'}
        coredb.import_signals(self.con,[item]);before=self.con.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0]
        coredb.import_signals(self.con,[item])
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM signals').fetchone()[0],1)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM raw_records').fetchone()[0],before)
        self.run_core();self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],1)
        self.con.execute("UPDATE assessments SET expires_at='2000-01-01T00:00:00+00:00'");self.con.commit()
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM qualified_leads').fetchone()[0],0)

    def test_same_company_url_cannot_merge_different_native_ids(self):
        self.company()
        with self.assertRaisesRegex(ValueError,'different LinkedIn identity'):
            coredb.company(self.con,'Another company','https://www.linkedin.com/company/co1','999')
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM core_companies').fetchone()[0],1)

    def test_human_message_is_required_not_just_reviewer_label(self):
        with self.assertRaisesRegex(ValueError,'human feedback'):
            profiles.review_round(self.con,1,True,[],'human')

    def test_optional_signals_rank_without_becoming_required_and_priority_expires(self):
        doc=copy.deepcopy(self.doc)
        doc['segments'][0]['signals']=[{'id':'hiring','kind':'hiring_sdr','entity':'company','mandatory':False,
            'max_age_days':30,'evidence_required':'Dated SDR job listing'}]
        self.v=profiles.register(self.con,doc,self.profile_path)
        self.lock_fixture();co=self.company();self.person()
        today=datetime.now(timezone.utc).date()
        coredb.import_signals(self.con,[{'entity_type':'company','entity_id':co,'kind':'hiring_sdr',
            'source_url':'https://co1.example/jobs/1','event_date':today.isoformat()}])
        self.run_core();before=self.export()[0][0]
        self.assertEqual(before['qualification_status'],'qualified');self.assertEqual(before['priority_score'],2)
        original=qualify.signals_for
        with patch.object(qualify,'signals_for',side_effect=lambda segment,kind,facts:original(segment,kind,facts,today=today+timedelta(days=31))):
            after=self.export()[0][0]
        self.assertEqual(after['qualification_status'],'qualified');self.assertEqual(after['priority_score'],1)

    def test_validated_name_search_resolution_survives_sparse_reingestion(self):
        person={'id':'ACoAA9','name':'Founder','linkedinUrl':'https://www.linkedin.com/in/founder',
                'company':'Unique Corp','profile_evidence':{'current_roles':[
                    {'company':'Unique Corp','title':'VP Sales','company_domain':'unique.example'}]}}
        cid=coredb.ingest_person(self.con,'run',person,self.raw_id)
        provisional=qualify.relationships(self.con,'run')[0]['company_id']
        items=[{'id':'900','name':'Unique Corp','linkedinUrl':'https://www.linkedin.com/company/uniquecorp',
                'website':'https://unique.example','industry':'Software','description':'B2B software'}]
        self.assertTrue(company_stage.attach(self.con,provisional,items,self.raw_id))
        resolved=qualify.relationships(self.con,'run')[0]['company_id']
        self.assertNotEqual(provisional,resolved)
        coredb.ingest_person(self.con,'run',person,self.raw_id)
        self.assertEqual(qualify.relationships(self.con,'run')[0]['company_id'],resolved)
        self.assertEqual(coredb.state(self.con,'company',resolved)['facts']['industry'],'Software')
        self.assertEqual(self.con.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_replaying_an_old_harvest_does_not_erase_newer_employment_evidence(self):
        base=self.root/'history';base.mkdir()
        old={'id':'ACoAAold','name':'Person','linkedinUrl':'https://www.linkedin.com/in/old','company':'Old Co',
             'profile_evidence':{'current_roles':[{'company':'Old Co','company_id':'1000','title':'VP Sales'}]}}
        common.write_json(base/'engagers.json',[old]);coredb.import_saved(self.con,base)
        newer={**old,'company':'New Co','profile_evidence':{'current_roles':[{'company':'New Co','company_id':'2000','title':'CEO'}]}}
        cid=coredb.ingest_person(self.con,'history',newer,self.raw_id)
        coredb.import_saved(self.con,base)
        self.assertEqual(coredb.state(self.con,'person',cid)['facts']['current_roles'][0]['company'],'New Co')
        self.assertEqual(qualify.relationships(self.con,'history')[0]['role'],coredb.encode(newer['profile_evidence']['current_roles'][0]))

    def test_qualify_cli_dump_works_on_a_saved_run_and_refuses_unknown_slugs(self):
        self.company()
        base=self.root/'cli';base.mkdir()
        person={'id':'ACoAA1','name':'CLI Person','linkedinUrl':'https://www.linkedin.com/in/person1','position':'VP Sales',
                'company':'Company 1','profile_evidence':{'current_roles':[{'title':'VP Sales','company':'Company 1','company_id':'1'}]}}
        common.write_json(base/'engagers.json',[person]);common.write_json(base/'posts.json',[])
        qualify.main(['--slug','cli','--icp',str(self.profile_path),'--output-dir',str(self.root),'--dump'])
        packet=json.loads((base/'assessment-packet.json').read_text())
        self.assertEqual(packet['icp']['name'],'buyers');self.assertIn('"packets"',self.out.getvalue())
        self.assertEqual(len(common.read_csv(base/'all_leads.csv')[0]),1)
        with self.assertRaisesRegex(ValueError,'No saved run'):
            qualify.main(['--slug','missing','--icp',str(self.profile_path),'--output-dir',str(self.root),'--dump'])
        with self.assertRaisesRegex(ValueError,'not found'):
            qualify.main(['--slug','cli','--icp',str(self.root/'nope.yaml'),'--output-dir',str(self.root)])

    def test_round_packet_shows_decisions_after_scoring(self):
        rid=self.calibration(0)
        packet=profiles.round_packet(self.con,rid)
        self.assertEqual(len(packet['results']),10);self.assertEqual(packet['status'],'approved')
        text=profiles.round_review(packet)
        self.assertIn(packet['prompt'].splitlines()[0],text)
        self.assertIn('not_qualified (confidence 1.0)',text);self.assertIn('Fictional structured industry',text)

    def test_short_calibration_batch_is_reported_as_a_shortage(self):
        cases=[{'company_id':self.company(300+i),'category':c} for i,c in enumerate(('likely_match','likely_non_match','edge_case'))]
        with self.assertRaisesRegex(ValueError,'exactly 10 distinct companies.*3 supplied'):profiles.start_round(self.con,self.v['id'],cases)
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM calibration_rounds').fetchone()[0],0)

    def test_abandoned_round_frees_its_companies(self):
        cases=[{'company_id':self.company(400+i),'category':['likely_match','likely_non_match','edge_case'][i%3]} for i in range(10)]
        rid=profiles.start_round(self.con,self.v['id'],cases)
        profiles.main(['round','--round',str(rid),'--abandon','evidence changed'])
        again=profiles.start_round(self.con,self.v['id'],cases)
        self.assertNotEqual(again,rid)
        self.assertIn('abandoned',self.out.getvalue())

    def test_optional_skip_answers_are_stored_empty(self):
        answers={'website':'https://seller.example','one_liner':'Sales software','best_customers':'Acme','segments':'Core',
                 'segment:Core:titles':'VP Sales','segment:Core:titles:mode':'mandatory','segment:Core:industries':'Software',
                 'segment:Core:industries:mode':'mandatory','offer':'skip','lead_magnet':'none','tone':'-','constraints':'Skip'}
        doc=profiles.document_from_answers('x',answers)
        self.assertEqual((doc['offer'],doc['tone'],doc['constraints']),({'cta':'','lead_magnet':''},'',''))
        self.assertEqual(len(doc['segments'][0]['company']),1)

    def test_dry_run_previews_harvest_caps_without_calls(self):
        with patch.object(core.subprocess,'run') as provider,patch.object(harvest,'post_run') as post:
            core.main(['--slug','preview','--icp',str(self.profile_path),'--output-dir',str(self.root),
                       '--post','https://www.linkedin.com/posts/x','--sample','--dry-run'])
        provider.assert_not_called();post.assert_not_called()
        text=self.out.getvalue()
        self.assertIn('hard cap $0.10',text);self.assertIn('up to 10 reactions and 0 comments',text);self.assertIn('No calls made',text)
        self.assertIn('submission cap 10; total hard cap $0.25',text)

    def test_ledger_mark_and_suppress_report_what_was_recorded(self):
        csv=self.root/'leads.csv'
        common.write_csv(csv,[{'linkedin_url':'https://www.linkedin.com/in/person1'},{'linkedin_url':''}],['linkedin_url'])
        ledger.main(['--db',str(self.path),'mark','--campaign','c1','--status','imported','--csv',str(csv)])
        self.assertIn('Marked 1 of 2',self.out.getvalue())
        self.assertEqual(self.con.execute('SELECT COUNT(*) FROM campaign_contacts').fetchone()[0],1)
        with self.assertRaisesRegex(ValueError,'--url or --csv'):ledger.main(['--db',str(self.path),'mark','--campaign','c1','--status','failed'])
        with self.assertRaisesRegex(ValueError,'needs'):ledger.main(['--db',str(self.path),'suppress','--reason','x'])
        ledger.main(['--db',str(self.path),'stats'])
        self.assertIn('qualified leads (current view)',self.out.getvalue())

if __name__=='__main__':unittest.main()
