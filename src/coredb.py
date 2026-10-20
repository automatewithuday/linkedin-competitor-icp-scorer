"""SQLite evidence store for the qualification core, layered on the person ledger in ledger.py."""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import ledger
from .common import linkedin_url
from .evidence import company_url


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS business (id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, website TEXT);
CREATE TABLE IF NOT EXISTS icp_profiles (name TEXT PRIMARY KEY, current_version INTEGER, path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS core_icp_versions (
 id INTEGER PRIMARY KEY, profile TEXT NOT NULL REFERENCES icp_profiles(name), hash TEXT NOT NULL,
 prompt_hash TEXT NOT NULL, document TEXT NOT NULL, prompt TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'draft', approval_streak INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
 UNIQUE(profile,hash,prompt_hash));
CREATE TABLE IF NOT EXISTS intake_sessions (profile TEXT PRIMARY KEY, answers TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS core_runs (slug TEXT PRIMARY KEY, profile_version INTEGER REFERENCES core_icp_versions(id),
 status TEXT NOT NULL DEFAULT 'collecting', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_requests (
 id INTEGER PRIMARY KEY, provider TEXT NOT NULL, purpose TEXT NOT NULL, identity TEXT NOT NULL,
 run_slug TEXT REFERENCES core_runs(slug), request_json TEXT NOT NULL, status TEXT NOT NULL, remote_run_id TEXT, dataset_id TEXT,
 submitted_at TEXT NOT NULL, completed_at TEXT, charge_cap REAL NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS request_identity ON provider_requests(provider,purpose,identity,id);
CREATE TABLE IF NOT EXISTS raw_records (
 id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES provider_requests(id), record_key TEXT NOT NULL,
 data TEXT NOT NULL, hash TEXT NOT NULL, source_raw_id INTEGER REFERENCES raw_records(id), completeness TEXT NOT NULL DEFAULT 'full', collected_at TEXT NOT NULL,
 UNIQUE(request_id,record_key,hash));
CREATE TRIGGER IF NOT EXISTS immutable_raw_update BEFORE UPDATE ON raw_records
 BEGIN SELECT RAISE(ABORT, 'Raw records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_raw_delete BEFORE DELETE ON raw_records
 BEGIN SELECT RAISE(ABORT, 'Raw records are immutable'); END;
CREATE VIEW IF NOT EXISTS apify_records AS SELECT r.*,q.purpose,q.identity,q.remote_run_id,q.dataset_id,q.run_slug
 FROM raw_records r JOIN provider_requests q ON q.id=r.request_id WHERE q.provider='apify';
CREATE VIEW IF NOT EXISTS getleads_records AS SELECT r.*,q.purpose,q.identity,q.run_slug
 FROM raw_records r JOIN provider_requests q ON q.id=r.request_id WHERE q.provider='getleads';
CREATE TABLE IF NOT EXISTS core_companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL, identity_status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS company_aliases (alias TEXT PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES core_companies(id));
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
 raw_id INTEGER NOT NULL REFERENCES raw_records(id), data TEXT NOT NULL, hash TEXT NOT NULL,
 observed_at TEXT, collected_at TEXT NOT NULL, UNIQUE(entity_type,entity_id,raw_id,hash));
CREATE TABLE IF NOT EXISTS entity_states (entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
 observation_id INTEGER NOT NULL REFERENCES observations(id), hash TEXT NOT NULL, PRIMARY KEY(entity_type,entity_id));
CREATE TABLE IF NOT EXISTS employments (
 id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id), company_id INTEGER NOT NULL REFERENCES core_companies(id),
 role_key TEXT NOT NULL, role TEXT NOT NULL, hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 raw_id INTEGER NOT NULL REFERENCES raw_records(id), UNIQUE(contact_id,role_key));
CREATE TABLE IF NOT EXISTS core_run_contacts (run_slug TEXT NOT NULL REFERENCES core_runs(slug),
 contact_id INTEGER NOT NULL REFERENCES contacts(id), PRIMARY KEY(run_slug,contact_id));
CREATE TABLE IF NOT EXISTS core_posts (post_key TEXT PRIMARY KEY, data TEXT NOT NULL, raw_id INTEGER NOT NULL REFERENCES raw_records(id));
CREATE TABLE IF NOT EXISTS core_engagements (contact_id INTEGER NOT NULL REFERENCES contacts(id), post_key TEXT NOT NULL,
 kind TEXT NOT NULL, detail TEXT NOT NULL, raw_id INTEGER NOT NULL REFERENCES raw_records(id),
 UNIQUE(contact_id,post_key,kind,detail));
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
 kind TEXT NOT NULL, source_url TEXT NOT NULL, event_date TEXT, collected_at TEXT NOT NULL,
 raw_id INTEGER NOT NULL REFERENCES raw_records(id), detail TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS signal_identity ON signals(entity_type,entity_id,kind,source_url,COALESCE(event_date,''),detail);
CREATE TABLE IF NOT EXISTS discovered_emails (contact_id INTEGER NOT NULL REFERENCES contacts(id),
 email TEXT NOT NULL, raw_id INTEGER NOT NULL REFERENCES raw_records(id), status TEXT NOT NULL DEFAULT 'UNVERIFIED',
 UNIQUE(contact_id,email,raw_id));
CREATE TABLE IF NOT EXISTS calibration_rounds (id INTEGER PRIMARY KEY, profile_version INTEGER NOT NULL REFERENCES core_icp_versions(id),
 prompt_hash TEXT NOT NULL, cases TEXT NOT NULL, contexts TEXT NOT NULL, results TEXT, feedback TEXT,
 status TEXT NOT NULL DEFAULT 'pending', reviewer TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assessments (
 id INTEGER PRIMARY KEY, profile_version INTEGER NOT NULL REFERENCES core_icp_versions(id),
 entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, company_id INTEGER NOT NULL REFERENCES core_companies(id),
 employment_id INTEGER REFERENCES employments(id), company_assessment_id INTEGER REFERENCES assessments(id),
 segment TEXT NOT NULL, input_context TEXT NOT NULL, context TEXT NOT NULL, decision TEXT NOT NULL,
 confidence REAL NOT NULL, reason TEXT NOT NULL, criteria TEXT NOT NULL, evidence_refs TEXT NOT NULL,
 prompt_hash TEXT NOT NULL, expires_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assessment_dependencies (assessment_id INTEGER NOT NULL REFERENCES assessments(id),
 entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, hash TEXT NOT NULL,
 PRIMARY KEY(assessment_id,entity_type,entity_id));
CREATE VIEW IF NOT EXISTS current_assessments AS SELECT a.* FROM assessments a
 JOIN core_icp_versions v ON v.id=a.profile_version JOIN icp_profiles p ON p.current_version=v.id
 WHERE a.prompt_hash=v.prompt_hash AND (a.expires_at IS NULL OR julianday(a.expires_at)>julianday('now'))
 AND a.id=(SELECT MAX(x.id) FROM assessments x WHERE x.profile_version=a.profile_version AND x.entity_type=a.entity_type
 AND x.entity_id=a.entity_id AND x.segment=a.segment AND COALESCE(x.employment_id,0)=COALESCE(a.employment_id,0))
 AND NOT EXISTS (SELECT 1 FROM assessment_dependencies d LEFT JOIN entity_states s
 ON s.entity_type=d.entity_type AND s.entity_id=d.entity_id
 WHERE d.assessment_id=a.id AND (s.hash IS NULL OR s.hash != d.hash));
CREATE VIEW IF NOT EXISTS qualified_leads AS SELECT a.id AS assessment_id,a.profile_version,a.entity_id AS contact_id,
 a.company_id,a.employment_id,a.segment,a.reason AS person_reason,c.reason AS company_reason,
 a.context,a.confidence FROM current_assessments a JOIN current_assessments c ON c.id=a.company_assessment_id
 JOIN core_icp_versions v ON v.id=a.profile_version
 JOIN employments e ON e.id=a.employment_id AND e.active=1 AND e.contact_id=a.entity_id AND e.company_id=a.company_id
 WHERE a.entity_type='person' AND a.decision='qualified' AND c.decision='qualified' AND c.segment=a.segment
 AND c.profile_version=a.profile_version AND c.company_id=a.company_id AND v.state='locked'
 AND NOT EXISTS (SELECT 1 FROM campaign_contacts cc WHERE cc.contact_id=a.entity_id
 AND cc.status IN ('imported','contacted','replied','excluded','needs_reconciliation'))
 AND NOT EXISTS (SELECT 1 FROM suppressions x JOIN aliases z ON z.alias=x.key WHERE z.contact_id=a.entity_id)
 AND NOT EXISTS (SELECT 1 FROM suppressions x JOIN discovered_emails de ON de.contact_id=a.entity_id
 WHERE x.key='email:' || lower(de.email) OR x.key='domain:' || lower(substr(de.email,instr(de.email,'@')+1)))
 AND NOT EXISTS (SELECT 1 FROM suppressions x JOIN entity_states cs ON cs.entity_type='company' AND cs.entity_id=a.company_id
 JOIN observations o ON o.id=cs.observation_id WHERE x.key='domain:' || lower(json_extract(o.data,'$.domain')));
'''


def connect(path=None):
    path = str(path or os.getenv('LEDGER_DB') or ledger.DEFAULT_DB)
    if path != ':memory:' and Path(path).exists():
        original = sqlite3.connect(path)
        if original.execute('PRAGMA user_version').fetchone()[0] < 2:
            backup = Path(path + '.before-core-v2.sqlite')
            if not backup.exists():
                target = sqlite3.connect(str(backup))
                original.backup(target); target.close()
        original.close()
    con = ledger.connect(path)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=30000')
    con.executescript(SCHEMA)
    con.execute('PRAGMA user_version=2')
    con.commit()
    return con


def new_request(con, provider, purpose, identity, request=None, slug=None, status='pending'):
    # Store only caller-selected metadata, never headers, authentication or request URLs containing tokens.
    safe = {k: v for k, v in (request or {}).items() if k in ('actor','input','source_file','attempt_id')}
    if slug:
        con.execute('INSERT OR IGNORE INTO core_runs(slug,created_at) VALUES (?,?)',(slug,ledger.now()))
    rid = con.execute('INSERT INTO provider_requests(provider,purpose,identity,run_slug,request_json,status,submitted_at) '
                      'VALUES (?,?,?,?,?,?,?)',(provider,purpose,identity,slug,encode(safe),status,ledger.now())).lastrowid
    con.commit()
    return rid


def raw(con, request_id, key, data, completeness='full', source_raw_id=None):
    value = encode(data); h = fingerprint(data)
    con.execute('INSERT OR IGNORE INTO raw_records(request_id,record_key,data,hash,completeness,collected_at,source_raw_id) VALUES (?,?,?,?,?,?,?)',
                (request_id,str(key),value,h,completeness,ledger.now(),source_raw_id))
    con.commit() # raw is durable before any normalization is attempted
    return con.execute('SELECT id FROM raw_records WHERE request_id=? AND record_key=? AND hash=?',
                       (request_id,str(key),h)).fetchone()[0]


def finish_request(con, rid, status, remote=None, dataset=None):
    con.execute('UPDATE provider_requests SET status=?,remote_run_id=COALESCE(?,remote_run_id),'
                'dataset_id=COALESCE(?,dataset_id),completed_at=? WHERE id=?',
                (status,remote,dataset,ledger.now(),rid));con.commit()


def state(con, kind, entity):
    r=con.execute('SELECT o.* FROM entity_states s JOIN observations o ON o.id=s.observation_id '
                  'WHERE s.entity_type=? AND s.entity_id=?',(kind,entity)).fetchone()
    return dict(r) | {'facts':json.loads(r['data'])} if r else None


def observe(con, kind, entity, facts, raw_id, observed_at=None):
    h=fingerprint(facts)
    con.execute('INSERT OR IGNORE INTO observations(entity_type,entity_id,raw_id,data,hash,observed_at,collected_at) '
                'VALUES (?,?,?,?,?,?,?)',(kind,entity,raw_id,encode(facts),h,observed_at,ledger.now()))
    oid=con.execute('SELECT id FROM observations WHERE entity_type=? AND entity_id=? AND raw_id=? AND hash=?',
                    (kind,entity,raw_id,h)).fetchone()[0]
    con.execute('INSERT OR REPLACE INTO entity_states VALUES (?,?,?,?)',(kind,entity,oid,h))
    con.commit()
    return oid


def company(con, name, url='', native_id='', unresolved_key=''):
    url=company_url(url); native_id=str(native_id or '')
    if not url and native_id.isdigit(): url='https://www.linkedin.com/company/'+native_id
    aliases=([f'id:{native_id}'] if native_id else [])+(['url:'+url] if url else [])
    if not aliases:
        aliases=['unresolved:'+unresolved_key]
    found={r[0] for r in con.execute('SELECT company_id FROM company_aliases WHERE alias IN ('+
                                   ','.join('?' for _ in aliases)+')',aliases)}
    if len(found)>1:
        raise ValueError('Conflicting company identifiers; resolve identity before linking evidence')
    cid=next(iter(found)) if found else con.execute('INSERT INTO core_companies(name,identity_status) VALUES (?,?)',
                                                 (name or 'Unknown company','confirmed' if url or native_id else 'unresolved')).lastrowid
    # Different LinkedIn IDs must never be silently merged through a URL alias.
    old_ids={r[0] for r in con.execute("SELECT alias FROM company_aliases WHERE company_id=? AND alias LIKE 'id:%'",(cid,))}
    if native_id and old_ids and 'id:'+native_id not in old_ids:
        raise ValueError('Company URL is associated with a different LinkedIn identity')
    for alias in aliases:
        con.execute('INSERT OR IGNORE INTO company_aliases VALUES (?,?)',(alias,cid))
    con.commit()
    return cid


def ingest_person(con, slug, e, raw_id):
    cid,_=ledger.contact_for(con,ledger.aliases_of(e),e.get('name'),slug)
    if cid is None:
        return None
    con.execute('INSERT OR IGNORE INTO core_runs(slug,created_at) VALUES (?,?)',(slug,ledger.now()))
    con.execute('INSERT OR IGNORE INTO core_run_contacts VALUES (?,?)',(slug,cid))
    pe=e.get('profile_evidence') or {}
    previous=state(con,'person',cid)
    facts={'name':e.get('name') or '', 'linkedin_url':linkedin_url(e.get('linkedinUrl')),
           'location':e.get('location') or '', 'headline':e.get('position') or '', 'about':pe.get('about') or '',
           'current_roles':pe.get('current_roles') or [], 'comments':e.get('comments') or [],
           'reaction_types':e.get('reaction_types') or []}
    if previous:
        retained=False
        for key in ('name','linkedin_url','location','headline','about'):
            if not facts.get(key) and previous['facts'].get(key):
                facts[key]=previous['facts'][key];retained=True
        old_roles=previous['facts'].get('current_roles') or []
        if not facts['current_roles'] and old_roles and (not e.get('company') or e['company']==old_roles[0].get('company')):
            facts['current_roles']=old_roles;retained=True
        if retained:
            facts['retained_observation_ids']=previous['facts'].get('retained_observation_ids') or [previous['id']]
    saved_signals=[dict(r) for r in con.execute('SELECT kind,source_url,event_date,detail FROM signals WHERE entity_type=? AND entity_id=?',('person',cid))]
    if saved_signals: facts['signals']=saved_signals
    observe(con,'person',cid,facts,raw_id)
    roles=facts['current_roles']
    if not roles and e.get('company'):
        roles=[{'company':e['company'],'title':'','identity_unknown':True}]
    keys=[]
    for role in roles:
        key=fingerprint([role.get('company_id'),role.get('company_linkedin_url'),role.get('company'),role.get('title'),role.get('start_date')])
        co=company(con,role.get('company'),role.get('company_linkedin_url'),role.get('company_id'),f'{cid}:{key}')
        if not state(con,'company',co):
            observe(con,'company',co,{'name':role.get('company') or '', 'linkedin_url':role.get('company_linkedin_url') or '',
                                    'domain':role.get('company_domain') or '',
                                    'identity_status':con.execute('SELECT identity_status FROM core_companies WHERE id=?',(co,)).fetchone()[0]},raw_id)
        rh=fingerprint([co,role]);keys.append(key)
        con.execute('INSERT INTO employments(contact_id,company_id,role_key,role,hash,raw_id) VALUES (?,?,?,?,?,?) '
                    'ON CONFLICT(contact_id,role_key) DO UPDATE SET company_id=excluded.company_id,role=excluded.role,'
                    'hash=excluded.hash,active=1,raw_id=excluded.raw_id',(cid,co,key,encode(role),rh,raw_id))
        eid=con.execute('SELECT id FROM employments WHERE contact_id=? AND role_key=?',(cid,key)).fetchone()[0]
        observe(con,'employment',eid,{'company_id':co,'role':role,'active':True},raw_id)
    if keys:
        for old in con.execute('SELECT * FROM employments WHERE contact_id=? AND active=1',(cid,)).fetchall():
            if old['role_key'] not in keys:
                con.execute('UPDATE employments SET active=0 WHERE id=?',(old['id'],))
                observe(con,'employment',old['id'],{'company_id':old['company_id'],'role':json.loads(old['role']),'active':False},raw_id)
    for pid in e.get('posts_touched') or []:
        con.execute('INSERT OR IGNORE INTO core_engagements VALUES (?,?,?,?,?)',(cid,pid,'reaction',','.join(e.get('reaction_types') or []),raw_id))
    for i,comment in enumerate(e.get('comments') or []):
        pids=e.get('comment_post_ids') or []
        con.execute('INSERT OR IGNORE INTO core_engagements VALUES (?,?,?,?,?)',(cid,pids[i] if i<len(pids) else '', 'comment',comment,raw_id))
    con.commit()
    return cid


def import_saved(con, base):
    """Offline, idempotent import of output/<slug> files. Normalized files are labelled normalized_only, never full raw responses."""
    from . import harvest
    base=Path(base);slug=base.name
    by_actor={}
    for filename,kind in (('raw_reactions.json','reaction'),('raw_comments.json','comment')):
        path=base/filename
        if path.exists():
            identity=str(path.resolve());req=con.execute('SELECT id FROM provider_requests WHERE identity=?',(identity,)).fetchone()
            rid=req[0] if req else new_request(con,'apify','historical_'+kind,identity,{'source_file':identity},slug,'complete')
            for i,item in enumerate(json.loads(path.read_text())):
                raw_id=raw(con,rid,i,item)
                for e in harvest.build_engagers(harvest.tag_post_items([item],kind)):
                    by_actor[e['id']]=(raw_id,e)
    path=base/'engagers.json'
    if path.exists():
        identity=str(path.resolve());req=con.execute('SELECT id FROM provider_requests WHERE identity=?',(identity,)).fetchone()
        rid=req[0] if req else new_request(con,'legacy','normalized_people',identity,{'source_file':identity},slug,'complete')
        for e in json.loads(path.read_text()):
            if e.get('id') in by_actor:
                recovered = by_actor[e['id']][1]
                e = dict(e)
                for field in ('profile_evidence','position','location','company'):
                    if recovered.get(field): e[field] = recovered[field]
            raw_id=raw(con,rid,e.get('id'),e,'normalized_only',by_actor[e['id']][0] if e.get('id') in by_actor else None)
            imported=con.execute("SELECT 1 FROM observations WHERE entity_type='person' AND raw_id=?",(raw_id,)).fetchone()
            known=ledger.find_contact(con,ledger.aliases_of(e))
            if imported and known:
                # A retry must not overwrite a newer profile lookup or manual evidence with an old harvest.
                cid=known
                con.execute('INSERT OR IGNORE INTO core_runs(slug,created_at) VALUES (?,?)',(slug,ledger.now()))
                con.execute('INSERT OR IGNORE INTO core_run_contacts VALUES (?,?)',(slug,cid))
            else:
                cid=ingest_person(con,slug,e,raw_id)
            # Link the normalized snapshot to original records without labeling the snapshot as a raw response.
            if cid and e.get('id') in by_actor:
                con.execute('INSERT OR IGNORE INTO observations(entity_type,entity_id,raw_id,data,hash,collected_at) '
                            'VALUES (?,?,?,?,?,?)',('person',cid,by_actor[e['id']][0],encode(e),fingerprint(e),ledger.now()))
    path=base/'posts.json'
    if path.exists():
        for post in json.loads(path.read_text()):
            identity='post:'+slug+':'+fingerprint(post)
            existing=con.execute('SELECT id FROM provider_requests WHERE identity=?',(identity,)).fetchone()
            rid=existing[0] if existing else new_request(con,'legacy','normalized_post',identity,slug=slug,status='complete')
            raw_id=raw(con,rid,post.get('postId'),post,'normalized_only')
            con.execute('INSERT OR REPLACE INTO core_posts VALUES (?,?,?)',(post.get('postId') or fingerprint(post),encode(post),raw_id))
    # Preserve caches already paid for; label them normalized-only.
    for hit in con.execute('SELECT * FROM provider_enrichments').fetchall():
        identity='legacy-cache:'+hit['provider']+':'+hit['identity']+':'+hit['checked_at']
        if not con.execute('SELECT 1 FROM provider_requests WHERE identity=?',(identity,)).fetchone():
            rid=new_request(con,hit['provider'],'legacy_cache',identity,slug=slug,status=hit['status'])
            data=json.loads(hit['data']);raw_id=raw(con,rid,hit['identity'],data,'normalized_only')
            cid=ledger.find_contact(con,ledger.aliases_of(url=hit['identity']))
            if cid:
                observe(con,'person_provider',cid,data,raw_id)
                if data.get('work_email'):
                    con.execute('INSERT OR IGNORE INTO discovered_emails(contact_id,email,raw_id) VALUES (?,?,?)',(cid,data['work_email'],raw_id))
    con.commit()


def import_signals(con, rows):
    for item in rows:
        kind=item['entity_type'];entity=int(item['entity_id'])
        current=state(con,kind,entity)
        if kind not in ('company','person') or not current:
            raise ValueError('Signal must reference an existing company or person')
        if not str(item.get('source_url','')).startswith(('https://','http://')) or not item.get('kind'):
            raise ValueError('Signal needs kind and evidence source URL')
        date=item.get('event_date')
        if date:
            datetime.fromisoformat(date)
        if con.execute("SELECT 1 FROM signals WHERE entity_type=? AND entity_id=? AND kind=? AND source_url=? AND COALESCE(event_date,'')=? AND detail=?",
                       (kind,entity,item['kind'],item['source_url'],date or '',item.get('detail',''))).fetchone():continue
        rid=new_request(con,'manual','signal',fingerprint(item),status='complete')
        raw_id=raw(con,rid,'signal',item)
        con.execute('INSERT INTO signals(entity_type,entity_id,kind,source_url,event_date,collected_at,raw_id,detail) '
                    'VALUES (?,?,?,?,?,?,?,?)',(kind,entity,item['kind'],item['source_url'],date,ledger.now(),raw_id,item.get('detail','')))
        facts=current['facts'];facts['retained_observation_ids']=facts.get('retained_observation_ids') or [current['id']]
        facts['signals']=[dict(r) for r in con.execute('SELECT kind,source_url,event_date,detail FROM signals '
                                                                          'WHERE entity_type=? AND entity_id=?',(kind,entity))]
        observe(con,kind,entity,facts,raw_id)
    con.commit()


def merge_contacts(con,keep,drop):
    for row in con.execute('SELECT * FROM core_run_contacts WHERE contact_id=?',(drop,)).fetchall():
        con.execute('INSERT OR IGNORE INTO core_run_contacts VALUES (?,?)',(row['run_slug'],keep))
    con.execute('DELETE FROM core_run_contacts WHERE contact_id=?',(drop,))
    for emp in con.execute('SELECT * FROM employments WHERE contact_id=?',(drop,)).fetchall():
        duplicate=con.execute('SELECT id FROM employments WHERE contact_id=? AND role_key=?',(keep,emp['role_key'])).fetchone()
        if duplicate:
            con.execute('UPDATE employments SET contact_id=?,role_key=?,active=0 WHERE id=?',(keep,emp['role_key']+':merged:'+str(emp['id']),emp['id']))
        else:con.execute('UPDATE employments SET contact_id=? WHERE id=?',(keep,emp['id']))
    for table in ('core_engagements','discovered_emails'):
        con.execute(f'UPDATE OR IGNORE {table} SET contact_id=? WHERE contact_id=?',(keep,drop))
        con.execute(f'DELETE FROM {table} WHERE contact_id=?',(drop,))
    con.execute("DELETE FROM entity_states WHERE entity_type='person' AND entity_id=?",(drop,))
