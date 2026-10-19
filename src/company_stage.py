"""Bounded company enrichment with durable reservations and identity validation."""
import json
import os
import re
import time
from datetime import datetime, timezone

from . import coredb, enrich, getleads, harvest, ledger, qualify
from .evidence import company_url, domain


def number_range(value):
    if type(value) is int:return value
    if isinstance(value,dict):
        lo=value.get('start',value.get('min'));hi=value.get('end',value.get('max'))
        if type(lo) is int and type(hi) is int:return {'min':lo,'max':hi}
        if type(lo) is int and hi is None:return {'min':lo,'max':10**12}
    if isinstance(value,str):
        digits=re.findall(r'\d+',value.replace(',',''))
        if len(digits)==2:return {'min':int(digits[0]),'max':int(digits[1])}
        if len(digits)==1 and '+' in value:return {'min':int(digits[0]),'max':10**12}
        if len(digits)==1 and value.strip().isdigit():return int(digits[0])
    return None


def normalize(item,url=''):
    norm=enrich.extract_company(item)
    return {k:v for k,v in {'name':norm['name'],'linkedin_url':company_url(item.get('linkedinUrl') or item.get('companyUrl') or url),
        'linkedin_id':str(item.get('id') or item.get('companyId') or ''),
        'domain':domain(item.get('website') or item.get('websiteUrl') or ''),
        'headcount':number_range(item.get('employeeCountRange')) or number_range(norm['size']),
        'headcount_source':'declared_band' if item.get('employeeCountRange') else 'reported_employee_count','industry':norm['industry'], 'location':norm['geo'],
        'description':str(item.get('description') or item.get('about') or '')[:6000], 'identity_status':'confirmed',
        'source':'apify_company'}.items() if v is not None and v!=''}


def recent(request,days=30):
    if not request or not request['completed_at']:return False
    return 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(request['completed_at'])).total_seconds()<days*86400


def claim(con,slug,identity,payload,max_lookups,max_usd,cap,retry_unknown=False,purpose='company',actor=enrich.ACTOR):
    with con:
        con.execute('BEGIN IMMEDIATE')
        con.execute('INSERT OR IGNORE INTO core_runs(slug,created_at) VALUES (?,?)',(slug,ledger.now()))
        old=con.execute("SELECT * FROM provider_requests WHERE provider='apify' AND purpose=? AND identity=? ORDER BY id DESC LIMIT 1",(purpose,identity)).fetchone()
        if old and old['status'] in ('pending','running','unknown') and not retry_unknown:return dict(old),False
        if old and old['status'] in ('complete','not_found') and recent(old):return dict(old),False
        used=con.execute("SELECT COUNT(*),COALESCE(SUM(charge_cap),0) FROM provider_requests WHERE provider='apify' AND purpose=? AND run_slug=?",(purpose,slug)).fetchone()
        if used[0]>=max_lookups or used[1]+cap>max_usd+1e-9:return None,False
        rid=con.execute('INSERT INTO provider_requests(provider,purpose,identity,run_slug,request_json,status,submitted_at,charge_cap) '
            "VALUES ('apify',?,?,?,?,'pending',?,?)",(purpose,identity,slug,coredb.encode({'actor':actor,'input':payload}),ledger.now(),cap)).lastrowid
    return dict(con.execute('SELECT * FROM provider_requests WHERE id=?',(rid,)).fetchone()),True


def fetch(con,request,token,cap,start):
    rid=request['id'];remote=request['remote_run_id']
    if not start and not remote:return None # unknown POST outcome: never blindly resubmit
    try:
        if start:
            metadata=json.loads(request['request_json'])
            run=harvest.post_run(metadata['actor'],metadata['input'],token,cap)
            remote=run['id'];dataset=run['defaultDatasetId']
            coredb.finish_request(con,rid,'running',remote,dataset)
        else:run=harvest.get_run(remote,token)
        deadline=time.monotonic()+1800
        while run['status'] not in ('SUCCEEDED','FAILED','ABORTED','TIMED-OUT'):
            if time.monotonic()>deadline:raise TimeoutError()
            time.sleep(8);run=harvest.get_run(remote,token)
        items=harvest.get_items(run['defaultDatasetId'],token)
        # Even zero results are saved as a completed, reusable miss.
        raw_id=coredb.raw(con,rid,'dataset',items)
        if run['status']!='SUCCEEDED' or any(isinstance(i,dict) and i.get('error') for i in items):
            coredb.finish_request(con,rid,'unknown',remote,run['defaultDatasetId']);return None
        coredb.finish_request(con,rid,'complete' if items else 'not_found',remote,run['defaultDatasetId'])
        return items,raw_id
    except Exception as exc:
        coredb.finish_request(con,rid,'unknown',remote)
        raise RuntimeError('Company lookup outcome requires review; saved requests will not be silently resubmitted') from None


def company_url_for(con,co):
    r=con.execute("SELECT alias FROM company_aliases WHERE company_id=? AND alias LIKE 'url:%' LIMIT 1",(co,)).fetchone()
    return r[0][4:] if r else ''


def attach(con,co,items,raw_id):
    current=coredb.state(con,'company',co);url=company_url_for(con,co)
    candidates=[]
    for item in items:
        if not isinstance(item,dict):continue
        facts=normalize(item,url if len(items)==1 else '')
        returned=company_url(item.get('linkedinUrl') or item.get('companyUrl'))
        old_ids={r[0][3:] for r in con.execute("SELECT alias FROM company_aliases WHERE company_id=? AND alias LIKE 'id:%'",(co,))}
        native=facts.get('linkedin_id')
        if native and old_ids and native not in old_ids:continue
        if url and returned and returned!=url and (not native or native not in old_ids):continue
        if not url:
            # Name-search results require independent corroboration, not a same-name assumption.
            known_domain=(current or {}).get('facts',{}).get('domain')
            if not known_domain or facts.get('domain')!=known_domain or not returned:continue
        candidates.append(facts)
    if len(candidates)!=1:return False
    facts=candidates[0]
    resolved=coredb.company(con,facts.get('name'),facts.get('linkedin_url'),facts.get('linkedin_id'),str(co))
    if resolved!=co:
        con.execute("UPDATE company_aliases SET company_id=? WHERE company_id=? AND alias LIKE 'unresolved:%'",(resolved,co))
        # Relink employment to an already known company; never copy conflicting provisional facts.
        for emp in con.execute('SELECT * FROM employments WHERE company_id=? AND active=1',(co,)).fetchall():
            con.execute('UPDATE employments SET company_id=?,hash=? WHERE id=?',(resolved,coredb.fingerprint([resolved,json.loads(emp['role'])]),emp['id']))
            coredb.observe(con,'employment',emp['id'],{'company_id':resolved,'role':json.loads(emp['role']),'active':True},raw_id)
    saved=[dict(r) for r in con.execute('SELECT kind,source_url,event_date,detail FROM signals WHERE entity_type=? AND entity_id=?',('company',resolved))]
    if saved:
        facts['signals']=saved
        old=coredb.state(con,'company',resolved)
        if old: facts['retained_observation_ids']=old['facts'].get('retained_observation_ids') or [old['id']]
    coredb.observe(con,'company',resolved,facts,raw_id)
    con.execute("UPDATE core_companies SET identity_status='confirmed',name=? WHERE id=?",(facts.get('name') or 'Unknown',resolved));con.commit()
    return True


def enrich_companies(con,v,slug,max_lookups=10,max_usd=0.25,dry_run=False,retry_unknown=False):
    co_ids={e['company_id'] for e in qualify.relationships(con,slug) if not qualify.early_excluded(con,v,e)}
    token=os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY');planned=0
    for co in sorted(co_ids):
        current=coredb.state(con,'company',co);url=company_url_for(con,co)
        if not current:continue
        facts=current['facts']
        # Only fetch for missing configured criteria or unresolved company identity.
        needed={c['field'] for seg in v['doc']['segments'] for c in seg['company'] if c['mode']!='preferred'}
        raw_date=con.execute('SELECT collected_at FROM raw_records WHERE id=?',(current['raw_id'],)).fetchone()[0]
        fresh=(datetime.now(timezone.utc)-datetime.fromisoformat(raw_date)).total_seconds()<30*86400
        if fresh and facts.get('identity_status')=='confirmed' and all(facts.get(k) not in (None,'',[]) for k in needed):continue
        payload={'companies':[url]} if url else {'searches':[facts.get('name') or '']}
        identity=url or 'unresolved:'+str(co);cap=0.02
        if dry_run:
            planned+=1;continue
        request,start=claim(con,slug,identity,payload,max_lookups,max_usd,cap,retry_unknown)
        if not request:continue
        if request['status'] in ('complete','not_found'):
            record=con.execute('SELECT * FROM raw_records WHERE request_id=? AND record_key=? ORDER BY id DESC LIMIT 1',(request['id'],'dataset')).fetchone()
            if record:attach(con,co,json.loads(record['data']),record['id'])
            continue
        if request['status']=='unknown' and not start:continue
        if not token:raise ValueError('APIFY_TOKEN is required for a new company request')
        result=fetch(con,request,token,cap,start)
        if result:attach(con,co,*result)
    if dry_run:print(f'Company lookup candidates: {planned}; submission cap {max_lookups}; total hard cap ${max_usd:.2f}. No calls made.')


def getleads_gaps(con,v,slug,max_lookups=100,dry_run=False):
    """Use the existing person-URL endpoint, only for required gaps; no email-discovery stage."""
    candidates={}
    for emp in qualify.relationships(con,slug):
        if qualify.early_excluded(con,v,emp):continue
        co=coredb.state(con,'company',emp['company_id']);person=coredb.state(con,'person',emp['contact_id'])
        fields={c['field'] for seg in v['doc']['segments'] for c in seg['company'] if c['mode']!='preferred'}
        if co and all(co['facts'].get(k) not in (None,'',[]) for k in fields):continue
        url=person['facts'].get('linkedin_url')
        if url:candidates.setdefault(emp['company_id'],(emp,url)) # one lookup per shared company gap
    if dry_run:print(f'GetLeads gap candidates: {len(candidates)}; shared slug cap {max_lookups}. No calls made.');return
    key=os.getenv('GETLEADS_API_KEY')
    for co,(emp,url) in candidates.items():
        hit=getleads.cached(con,url)
        if not getleads.fresh(hit,30):
            if not key:raise ValueError('GETLEADS_API_KEY is required for new gap lookups')
            remaining,_=getleads.allowance(key)
            if remaining is not None and remaining<1:break
            attempts,reused,held=getleads.reserve(con,[url],slug,max_lookups,30,False)
            if held:continue
            if attempts:
                hits,error=getleads.fetch_reserved(con,attempts,key)
                if error:raise ValueError(str(error))
                hit=hits.get(url)
            else:hit=reused.get(url)
        if not hit or hit['status']!='matched':continue
        data=json.loads(hit['data']);current=coredb.state(con,'company',co)
        aliases={r[0] for r in con.execute('SELECT alias FROM company_aliases WHERE company_id=?',(co,))}
        exact=(data.get('company_linkedin_url') and 'url:'+company_url(data['company_linkedin_url']) in aliases) or \
              (data.get('company_id') and 'id:'+str(data['company_id']) in aliases) or \
              (current['facts'].get('domain') and domain(data.get('company_domain'))==current['facts']['domain'])
        rid=coredb.new_request(con,'getleads','normalized_cache_replay',url,status='complete')
        parent=con.execute("SELECT r.* FROM getleads_records r WHERE record_key=? AND completeness='full' ORDER BY id DESC LIMIT 1",(url,)).fetchone()
        source=None
        if parent:
            original=json.loads(parent['data'])
            if isinstance(original.get('data'),dict) and getleads.normalize(original['data'])==data:source=parent['id']
        raw_id=coredb.raw(con,rid,url,data,'normalized_only',source)
        coredb.observe(con,'person_provider',emp['contact_id'],data,raw_id)
        if data.get('work_email'):
            con.execute('INSERT OR IGNORE INTO discovered_emails(contact_id,email,raw_id) VALUES (?,?,?)',(emp['contact_id'],data['work_email'],raw_id));con.commit()
        if not exact:continue # full raw data is retained even when no firmographics may attach
        facts=dict(current['facts'])
        for target,source in (('industry','company_industry'),('description','company_description'),('domain','company_domain')):
            if not facts.get(target) and data.get(source):facts[target]=data[source]
        if facts.get('headcount') is None:facts['headcount']=number_range(data.get('company_size'))
        facts['getleads_alignment']='matched_identity'
        if facts==current['facts']:continue
        facts['retained_observation_ids']=current['facts'].get('retained_observation_ids') or [current['id']]
        coredb.observe(con,'company',co,facts,raw_id)
