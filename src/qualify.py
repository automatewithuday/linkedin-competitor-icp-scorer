"""Company-first, segment-specific qualification. No automatic model or paid calls."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import coredb, ledger, profiles
from .common import write_csv, write_json

DECISIONS=('qualified','not_qualified','needs_review')


def deterministic(criterion, value):
    if value is None or value=='' or value==[]:
        return 'unknown'
    op=criterion['operator'];want=criterion.get('value')
    if want is None or want=='' or want==[]:return 'unknown'
    if op=='range':
        if type(value) in (int,float):lo=hi=value
        elif isinstance(value,dict) and type(value.get('min')) in (int,float) and type(value.get('max')) in (int,float):
            lo,hi=value['min'],value['max']
        else:return 'unknown'
        if lo>=want[0] and hi<=want[1]:return 'match'
        if hi<want[0] or lo>want[1]:return 'no_match'
        return 'unknown' # overlapping provider bands cannot certify a hard boundary
    if op=='equals':return 'match' if str(value).casefold()==str(want).casefold() else 'no_match'
    if op=='one_of':
        actual=value if isinstance(value,list) else [value]
        expected=want if isinstance(want,list) else [want]
        return 'match' if {str(v).casefold() for v in actual}&{str(v).casefold() for v in expected} else 'no_match'
    if op=='contains':
        return 'match' if str(want).casefold() in str(value).casefold() else 'no_match'
    return 'unknown'


def evaluate(criteria,facts,observation_ids,judgments=None):
    judgments=judgments or [];by_id={j.get('criterion_id'):j for j in judgments}
    if len(by_id)!=len(judgments) or set(by_id)-{c['id'] for c in criteria}:
        raise ValueError('Duplicate or unknown criterion identity')
    details=[];failed=[];missing=[];preferred=0
    for c in criteria:
        value=facts.get(c['field']);outcome=deterministic(c,value);confidence=1.0;refs=[]
        reason='Observed value evaluated against '+str(c.get('description') or c['id'])
        if c['operator']=='semantic':
            j=by_id.get(c['id'])
            if j:
                outcome=j.get('outcome');confidence=j.get('confidence')
                if outcome not in ('match','no_match','unknown') or type(confidence) not in (int,float) or not 0<=confidence<=1 or not isinstance(j.get('reason'),str) or not j['reason'].strip():
                    raise ValueError('Semantic judgments require outcome, confidence and reason')
                refs=j.get('evidence_refs') or []
                if not isinstance(refs,list) or any(type(r) is not int or r not in observation_ids for r in refs):
                    raise ValueError('Evidence references must point to supplied observations')
                quote=j.get('evidence_quote') or ''
                if outcome!='unknown' and (not refs or not isinstance(quote,str) or not quote.strip() or
                    quote.casefold() not in coredb.encode(facts).casefold()):
                    raise ValueError('A semantic match/failure requires a supporting quote from the supplied facts')
                reason=j['reason']
                if confidence<0.6:outcome='unknown';reason='Low-confidence judgment: '+reason
            else:reason='Needs an evidence-bound semantic judgment for '+c['id']
        elif outcome!='unknown':refs=list(observation_ids)
        if outcome=='unknown':
            reason='Unknown or insufficient evidence: '+reason
            if c['mode']!='preferred':missing.append(c['id'])
        elif (c['mode']=='mandatory' and outcome=='no_match') or (c['mode']=='excluded' and outcome=='match'):
            failed.append(c['id'])
        if c['mode']=='preferred' and outcome=='match':preferred+=1
        details.append({'criterion_id':c['id'],'mode':c['mode'],'outcome':outcome,'confidence':confidence,
                        'reason':reason,'evidence_refs':refs})
    decision='not_qualified' if failed else 'needs_review' if missing else 'qualified'
    reason=('Observed exclusion or mandatory mismatch: '+', '.join(failed) if failed else
            'Required evidence missing or uncertain: '+', '.join(missing) if missing else
            'All mandatory criteria supported; no observed exclusion.')
    return {'decision':decision,'confidence':min([d['confidence'] for d in details if d['mode']!='preferred'] or [1.0]),
            'reason':reason,'criterion_results':details,'preferred_matches':preferred}


def signals_for(segment,kind,facts,today=None):
    today=today or datetime.now(timezone.utc).date();missing=[];details=[];expiry=None
    for definition in segment.get('signals',[]):
        if definition['entity']!=kind:continue
        matches=[]
        for signal in facts.get('signals',[]):
            if signal.get('kind')!=definition['kind'] or not signal.get('source_url') or not signal.get('event_date'):continue
            try:date=datetime.fromisoformat(signal['event_date'].replace('Z','+00:00')).date()
            except ValueError:continue
            if 0<=(today-date).days<=definition['max_age_days']:matches.append(date)
        details.append({'criterion_id':definition['id'],'mode':'mandatory' if definition['mandatory'] else 'preferred',
                        'outcome':'match' if matches else 'unknown','confidence':1.0,'reason':
                        'Dated supporting signal present.' if matches else 'No dated evidence within the configured freshness window.',
                        'evidence_refs':[]})
        if definition['mandatory']:
            if not matches:missing.append(definition['id'])
            else:
                end=datetime.combine(max(matches)+timedelta(days=definition['max_age_days']+1),datetime.min.time(),timezone.utc).isoformat()
                expiry=min(expiry,end) if expiry else end
    return missing,details,expiry


def version(con,vid):
    v=con.execute('SELECT * FROM core_icp_versions WHERE id=?',(vid,)).fetchone()
    if not v:raise ValueError('Unknown ICP version')
    return dict(v)|{'doc':json.loads(v['document'])}


def packet(con,v,kind,entity,seg,employment=None,company_assessment=None):
    evidence=coredb.state(con,kind,entity)
    if not evidence:return None
    dependencies=[(kind,entity,evidence['hash'])];facts=dict(evidence['facts']);refs=[evidence['id'], *facts.get('retained_observation_ids', [])]
    co=entity if kind=='company' else employment['company_id']
    if kind=='person':
        role=json.loads(employment['role']);facts['title']=role.get('title') or ''
        facts['employment']=role
        # Only this employment supplies the title; do not mix roles across companies.
        facts['current_roles']=[role]
        es=coredb.state(con,'employment',employment['id']);cs=coredb.state(con,'company',co)
        dependencies.extend([('employment',employment['id'],es['hash']),('company',co,cs['hash'])])
        refs.extend([es['id'],cs['id']])
    context=coredb.fingerprint([v['hash'],v['prompt_hash'],kind,entity,seg['id'],dependencies,
                              company_assessment['context'] if company_assessment else None])
    return {'entity_type':kind,'entity_id':entity,'company_id':co,'employment_id':employment['id'] if employment else None,
            'segment':seg['id'],'score_context':context,'facts':facts,'observation_ids':refs,
            'criteria':seg[kind],'signals':seg.get('signals',[]),'dependencies':dependencies,
            'company_assessment_id':company_assessment['id'] if company_assessment else None}


def latest(con,v,p):
    row=con.execute('SELECT * FROM current_assessments WHERE profile_version=? AND entity_type=? AND entity_id=? '
                    'AND segment=? AND COALESCE(employment_id,0)=? ORDER BY id DESC LIMIT 1',
                    (v['id'],p['entity_type'],p['entity_id'],p['segment'],p['employment_id'] or 0)).fetchone()
    return dict(row) if row and row['input_context']==p['score_context'] else None


def assess(con,v,p,seg,judgment=None):
    if judgment is not None:
        if judgment.get('score_context')!=p['score_context']:
            raise ValueError('Stale or unbound judgment; dump the current company/person evidence again')
        if judgment.get('entity_type')!=p['entity_type'] or judgment.get('entity_id')!=p['entity_id'] or judgment.get('segment')!=p['segment'] or judgment.get('employment_id')!=p['employment_id']:
            raise ValueError('Judgment does not match the entity/segment/employment')
        if not isinstance(judgment.get('reason'),str) or not judgment['reason'].strip():
            raise ValueError('Every submitted assessment needs a concise explanation')
        if judgment.get('decision') not in DECISIONS or type(judgment.get('confidence')) not in (int,float) or not 0<=judgment['confidence']<=1:
            raise ValueError('Every submitted assessment needs a decision and confidence')
    result=evaluate(seg[p['entity_type']],p['facts'],p['observation_ids'],(judgment or {}).get('criterion_results'))
    missing,signal_details,expiry=signals_for(seg,p['entity_type'],p['facts'])
    result['criterion_results']+=signal_details
    if missing and result['decision']!='not_qualified':
        result.update(decision='needs_review',reason='Missing required signal evidence: '+', '.join(missing))
    if p['entity_type']=='company' and p['facts'].get('identity_status')!='confirmed' and result['decision']!='not_qualified':
        result.update(decision='needs_review',reason='Current company identity is unresolved; do not use name-only firmographics.')
    if p['entity_type']=='person' and not p['facts'].get('linkedin_url') and result['decision']!='not_qualified':
        result.update(decision='needs_review',reason='Person public LinkedIn identity needs resolution.')
    if judgment:
        if judgment['confidence']<0.6 and result['decision']=='qualified':
            result['decision']='needs_review'
        if judgment['decision']!=result['decision']:
            raise ValueError('Submitted decision disagrees with criterion evidence; mandatory criteria cannot be bypassed')
        result['reason']=judgment['reason'];result['confidence']=min(result['confidence'],judgment['confidence'])
    proof=coredb.fingerprint([p['score_context'],result])
    old=latest(con,v,p)
    if old and old['context']==proof:return old
    aid=con.execute('INSERT INTO assessments(profile_version,entity_type,entity_id,company_id,employment_id,company_assessment_id,'
        'segment,input_context,context,decision,confidence,reason,criteria,evidence_refs,prompt_hash,expires_at,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (v['id'],p['entity_type'],p['entity_id'],p['company_id'],p['employment_id'],p['company_assessment_id'],p['segment'],
         p['score_context'],proof,result['decision'],result['confidence'],result['reason'],coredb.encode(result['criterion_results']),
         coredb.encode(p['observation_ids']),v['prompt_hash'],expiry,ledger.now())).lastrowid
    for kind,entity,h in p['dependencies']:
        con.execute('INSERT INTO assessment_dependencies VALUES (?,?,?,?)',(aid,kind,entity,h))
    return dict(con.execute('SELECT * FROM assessments WHERE id=?',(aid,)).fetchone())


def relationships(con,slug):
    return [dict(r) for r in con.execute('SELECT e.* FROM employments e JOIN core_run_contacts rc ON rc.contact_id=e.contact_id '
                                       'WHERE rc.run_slug=? AND e.active=1 ORDER BY e.id',(slug,))]


def early_excluded(con,v,employment):
    person=coredb.state(con,'person',employment['contact_id'])
    aliases=[r[0] for r in con.execute('SELECT alias FROM aliases WHERE contact_id=?',(employment['contact_id'],))]
    if ledger.status_for(con,aliases)!='eligible':return True
    facts=person['facts']|{'title':json.loads(employment['role']).get('title') or ''}
    # Only conclusive structured disqualifiers, never missing or semantic facts, bypass paid enrichment.
    return all(evaluate(s['person'],facts,[person['id']])['decision']=='not_qualified' for s in v['doc']['segments'])


def _run_assessments(con,vid,slug,submitted=None):
    v=version(con,vid);relations=relationships(con,slug);packets=[];pending=[]
    by_key={}
    for j in submitted or []:
        key=(j.get('entity_type'),j.get('entity_id'),j.get('segment'),j.get('employment_id'))
        if key in by_key:raise ValueError('Duplicate assessment judgment')
        by_key[key]=j
    used=set()
    for seg in v['doc']['segments']:
        companies={r['company_id'] for r in relations}
        company_results={}
        for co in sorted(companies):
            p=packet(con,v,'company',co,seg)
            if not p:continue
            key=('company',co,seg['id'],None);j=by_key.get(key)
            if j:used.add(key)
            old=latest(con,v,p)
            r=assess(con,v,p,seg,j) if j or not old else old
            company_results[co]=r;packets.append(p|{'current_assessment':r})
            if not j and any(c['operator']=='semantic' for c in seg['company']) and r['decision']=='needs_review':pending.append(p)
        for emp in relations:
            co=emp['company_id'];ca=company_results.get(co)
            if not ca or ca['decision']!='qualified':continue
            p=packet(con,v,'person',emp['contact_id'],seg,emp,ca)
            key=('person',emp['contact_id'],seg['id'],emp['id']);j=by_key.get(key)
            if j:used.add(key)
            old=latest(con,v,p)
            r=assess(con,v,p,seg,j) if j or not old else old
            packets.append(p|{'current_assessment':r})
            if not j and any(c['operator']=='semantic' for c in seg['person']) and r['decision']=='needs_review':pending.append(p)
    if set(by_key)-used:raise ValueError('Person judgments require a qualified company in the same segment, or reference unknown entities')
    return packets,pending


def run_assessments(con,vid,slug,submitted=None):
    with con:
        return _run_assessments(con,vid,slug,submitted)


FIELDS=['schema_version','name','linkedin_url','company_id','company_name','company_linkedin_url','employment_id','current_title',
        'segment','company_decision','company_reason','person_decision','person_reason','qualification_status','qualification_reason',
        'eligibility_status','priority_score','profile_version','assessment_id','score_context','evidence_refs','employment_decisions']


def export(con,vid,slug,base):
    v=version(con,vid);rows=[]
    eligible={r['assessment_id'] for r in con.execute('SELECT assessment_id FROM qualified_leads WHERE profile_version=?',(vid,))}
    grouped={}
    for employment in relationships(con,slug): grouped.setdefault(employment['contact_id'],[]).append(employment)
    for contact in con.execute('SELECT c.* FROM contacts c JOIN core_run_contacts r ON r.contact_id=c.id WHERE r.run_slug=?',(slug,)).fetchall():
        ps=coredb.state(con,'person',contact['id']);pairs=[]
        emps=grouped.get(contact['id'],[])
        for emp in emps:
            cs=coredb.state(con,'company',emp['company_id'])
            for seg in v['doc']['segments']:
                cp=packet(con,v,'company',emp['company_id'],seg);ca=latest(con,v,cp) if cp else None
                pa=None
                if ca and ca['decision']=='qualified':
                    pp=packet(con,v,'person',contact['id'],seg,emp,ca);pa=latest(con,v,pp)
                cd=ca['decision'] if ca else 'needs_review';pd=pa['decision'] if pa else 'needs_review'
                decision='not_qualified' if cd=='not_qualified' or pd=='not_qualified' else 'qualified' if cd==pd=='qualified' else 'needs_review'
                reason=pa['reason'] if pa else ca['reason'] if ca else 'Company evidence needs assessment.'
                if decision=='qualified' and v['state']!='locked':decision='needs_review';reason='ICP calibration is not human-approved for bulk qualification.'
                signal_ids={s['id'] for s in seg.get('signals',[])}
                priority=sum(c.get('mode')=='preferred' and c.get('outcome')=='match' and c.get('criterion_id') not in signal_ids
                             for assessed in (ca,pa) if assessed for c in json.loads(assessed['criteria']))
                for kind,facts in (('company',cs['facts']),('person',ps['facts'])):
                    _,fresh_signals,_=signals_for(seg,kind,facts)
                    priority+=sum(s['mode']=='preferred' and s['outcome']=='match' for s in fresh_signals)
                pairs.append({'priority_score':priority,'company_id':emp['company_id'],'company_name':cs['facts'].get('name',''),
                    'company_linkedin_url':cs['facts'].get('linkedin_url',''),'employment_id':emp['id'],
                    'current_title':json.loads(emp['role']).get('title',''),'segment':seg['id'],
                    'company_decision':cd,'company_reason':ca['reason'] if ca else 'Company evidence is incomplete or changed.',
                    'person_decision':pd,'person_reason':pa['reason'] if pa else 'Person assessment waits for company qualification.',
                    'qualification_status':decision,'qualification_reason':reason,
                    'assessment_id':pa['id'] if pa else '', 'score_context':pa['context'] if pa else ca['context'] if ca else '',
                    'evidence_refs':json.loads(pa['evidence_refs']) if pa else json.loads(ca['evidence_refs']) if ca else []})
        pairs.sort(key=lambda r:(0 if r.get('assessment_id') in eligible else 1,{'qualified':0,'needs_review':1,'not_qualified':2}[r['qualification_status']],-r.get('priority_score',0),r['company_id']))
        best=pairs[0] if pairs else {'qualification_status':'needs_review','qualification_reason':'Current employer identity/employment evidence is missing.',
                                   'company_decision':'needs_review','company_reason':'Current employer is unknown.',
                                   'person_decision':'needs_review','person_reason':'A current employment relationship is required.'}
        public=ps['facts'].get('linkedin_url','') if ps else ''
        aliases=[r[0] for r in con.execute('SELECT alias FROM aliases WHERE contact_id=?',(contact['id'],))]
        status=ledger.status_for(con,aliases)
        blocked=None if status=='eligible' else status
        if not blocked and best.get('company_id'):
            cs=coredb.state(con,'company',best['company_id']);domain=cs['facts'].get('domain')
            if domain and ledger.suppression_for(con,['domain:'+domain.lower()]):blocked='suppressed:company_domain'
        row={'schema_version':2,'name':contact['name'] or '', 'linkedin_url':public,**best,'profile_version':vid,
             'eligibility_status':blocked or ('eligible' if best.get('assessment_id') in eligible else 'hold'),
             'employment_decisions':coredb.encode(pairs)}
        row['evidence_refs']=coredb.encode(row.get('evidence_refs',[]));rows.append(row)
    rows.sort(key=lambda r:({'qualified':0,'needs_review':1,'not_qualified':2}[r['qualification_status']],-r.get('priority_score',0),r['name']))
    base=Path(base);write_csv(base/'all_leads.csv',rows,FIELDS)
    qualified=[r for r in rows if r.get('assessment_id') in eligible and r['eligibility_status']=='eligible' and r['qualification_status']=='qualified']
    write_csv(base/'qualified_leads.csv',qualified,FIELDS)
    counts={d:sum(r['qualification_status']==d for r in rows) for d in DECISIONS}
    print(f"Rows: {len(rows)}; qualified: {counts['qualified']}; not qualified: {counts['not_qualified']}; needs review: {counts['needs_review']}; eligible: {len(qualified)}")
    for r in rows:print(f"  {r['name']}: {r['qualification_status']} — {r['qualification_reason']}")
    return rows,qualified


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--slug',required=True);ap.add_argument('--output-dir',default='./output');ap.add_argument('--icp',required=True)
    ap.add_argument('--scores');ap.add_argument('--dump',action='store_true')
    a=ap.parse_args(argv);con=coredb.connect();v=profiles.load(con,a.icp);base=Path(a.output_dir)/a.slug
    if not con.execute('SELECT 1 FROM core_runs WHERE slug=?',(a.slug,)).fetchone():coredb.import_saved(con,base)
    submitted=json.loads(Path(a.scores).read_text())['results'] if a.scores else []
    # Validate all bound identities before writing any submitted result.
    packets,pending=run_assessments(con,v['id'],a.slug,submitted)
    write_json(base/'assessment-packet.json',{'schema_version':2,'prompt':v['prompt'],'icp':v['doc'],'profile_version':v['id'],'packets':pending})
    if a.dump:print(json.dumps({'prompt':v['prompt'],'icp':v['doc'],'packets':packets},indent=2))
    export(con,v['id'],a.slug,base)
    if pending:print(f'{len(pending)} assessments need evidence-bound semantic judgments; see assessment-packet.json')

if __name__=='__main__':main()
