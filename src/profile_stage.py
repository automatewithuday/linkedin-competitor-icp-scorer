"""Resolve missing person URLs/current employment before company qualification."""
import json
import os

from . import company_stage, coredb, harvest, ledger, resolve
from .common import linkedin_url


def enrich_profiles(con,slug,max_lookups=10,max_usd=0.25,dry_run=False,retry_unknown=False):
    contacts=con.execute('SELECT contact_id FROM core_run_contacts WHERE run_slug=?',(slug,)).fetchall();planned=0
    for row in contacts:
        cid=row['contact_id'];person=coredb.state(con,'person',cid);facts=person['facts']
        if facts.get('linkedin_url') and facts.get('current_roles'):continue
        aliases=[r[0] for r in con.execute('SELECT alias FROM aliases WHERE contact_id=?',(cid,))]
        if ledger.status_for(con,aliases)!='eligible':continue
        opaque=next((a[3:] for a in aliases if a.startswith('id:ACoAA')),'')
        public=facts.get('linkedin_url');identity=opaque or public
        if not identity:continue
        hit=ledger.cached_lookup(con,opaque) if opaque else None
        if hit and hit['status']=='resolved' and hit.get('profile_evidence'):
            rid=coredb.new_request(con,'legacy','profile_cache',identity,status='complete')
            raw_id=coredb.raw(con,rid,identity,hit,'normalized_only')
            e={'id':opaque,'name':facts.get('name'),'linkedinUrl':hit['public_url'],
               'position':facts.get('headline'),'location':hit.get('location') or facts.get('location'),
               'company':hit.get('company'),'profile_evidence':hit['profile_evidence'],'comments':facts.get('comments',[])}
            coredb.ingest_person(con,slug,e,raw_id);continue
        payload={'profileIds':[opaque]} if opaque else {'urls':[public]}
        payload['profileScraperMode']=resolve.MODE
        if dry_run:planned+=1;continue
        request,start=company_stage.claim(con,slug,identity,payload,max_lookups,max_usd,0.02,retry_unknown,'profile',resolve.ACTOR.replace('/','~'))
        if not request:continue
        if request['status'] in ('complete','not_found'):
            record=con.execute("SELECT id,data FROM raw_records WHERE request_id=? AND record_key='dataset'",(request['id'],)).fetchone()
            result=(json.loads(record['data']),record['id']) if record else None
        elif request['status']=='unknown' and not start:continue
        else:
            token=os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY')
            if not token:raise ValueError('APIFY_TOKEN is required for a new profile request')
            result=company_stage.fetch(con,request,token,0.02,start)
        if not result:continue
        items,raw_id=result
        matched=[]
        for item in items:
            if not isinstance(item,dict):continue
            url,location,company=harvest.profile_fields(item)
            source_id=item.get('id') or (item.get('originalQuery') or {}).get('profileId')
            if linkedin_url(url) and ((opaque and source_id==opaque) or (public and linkedin_url(public)==linkedin_url(url))):
                matched.append((item,url,location,company))
        if len(matched)!=1:continue
        item,url,location,company=matched[0]
        e={'id':opaque or item.get('id') or public,'name':item.get('name') or facts.get('name'),'linkedinUrl':url,
           'position':item.get('headline') or facts.get('headline'),'location':location or facts.get('location'),
           'company':company,'profile_evidence':harvest.profile_evidence(item),'comments':facts.get('comments',[]),
           'reaction_types':facts.get('reaction_types',[])}
        coredb.ingest_person(con,slug,e,raw_id)
        if opaque:
            remote=con.execute('SELECT remote_run_id FROM provider_requests WHERE id=?',(request['id'],)).fetchone()[0]
            ledger.record_lookup(con,opaque,'resolved',linkedin_url(url),remote,e)
        con.commit()
    if dry_run:print(f'Profile lookup candidates: {planned}; submission cap {max_lookups}; total hard cap ${max_usd:.2f}. No calls made.')
