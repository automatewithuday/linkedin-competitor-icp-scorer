"""Local SQLite ledger: one record per person across runs and competitors, plus the do-not-contact rules.

Records (data/ledger.sqlite, or $LEDGER_DB):
  contacts + aliases     one person; aliases link the opaque LinkedIn id (id:ACoAA...) and the public URL (url:...)
  engagements            comments / post touches per post, never inserted twice
  profile_snapshots      location / company / headline evidence over time
  lookups                profile-scraper results, shared across runs (resolved / not_found + date)
  provider_enrichments   GetLeads responses by URL; enrichment_attempts is the durable submission cap
  campaign_contacts      imports and outreach status per campaign, recorded by hand with `mark`
  suppressions           customers, teammates, opt-outs: by URL, email or domain

The qualification core (src/coredb.py) adds the evidence and assessment tables on top of these. A person whose
status here is not `eligible` (suppressed, or already in a campaign) never reaches qualified_leads.

CLI: suppress | mark | stats   (see --help on each)
"""
import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .common import linkedin_url, read_csv
except ImportError:
    from common import linkedin_url, read_csv

DEFAULT_DB = 'data/ledger.sqlite'
BLOCKING = ('imported', 'contacted', 'replied', 'excluded', 'needs_reconciliation')
STATUS_PRIORITY = {'failed': 0, 'unconfirmed': 1, 'needs_reconciliation': 2,
                   'imported': 3, 'contacted': 4, 'replied': 5, 'excluded': 6}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS contacts (id INTEGER PRIMARY KEY, name TEXT, evidence_hash TEXT,
    first_slug TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS aliases (alias TEXT PRIMARY KEY, contact_id INTEGER NOT NULL REFERENCES contacts(id));
CREATE TABLE IF NOT EXISTS engagements (contact_id INTEGER NOT NULL, post_id TEXT NOT NULL, kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '', post_url TEXT, slug TEXT, seen_at TEXT,
    UNIQUE (contact_id, post_id, kind, detail));
CREATE TABLE IF NOT EXISTS profile_snapshots (id INTEGER PRIMARY KEY, contact_id INTEGER NOT NULL, headline TEXT,
    location TEXT, company TEXT, source TEXT, hash TEXT, taken_at TEXT);
CREATE TABLE IF NOT EXISTS lookups (opaque_id TEXT PRIMARY KEY, status TEXT NOT NULL, public_url TEXT,
    run_id TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS provider_enrichments (provider TEXT NOT NULL, identity TEXT NOT NULL,
    status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', checked_at TEXT NOT NULL,
    PRIMARY KEY (provider, identity));
CREATE TABLE IF NOT EXISTS enrichment_attempts (id INTEGER PRIMARY KEY, provider TEXT NOT NULL,
    slug TEXT NOT NULL, identity TEXT NOT NULL, status TEXT NOT NULL, submitted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_contacts (contact_id INTEGER NOT NULL, campaign TEXT NOT NULL, status TEXT NOT NULL,
    email TEXT, receipt TEXT, updated_at TEXT, UNIQUE (contact_id, campaign));
CREATE TABLE IF NOT EXISTS suppressions (key TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
'''
# Databases created before the qualification core also hold `icp_versions` and `scores` from the retired
# headline scorer. They are left untouched; nothing reads them any more.


def connect(path=None):
    path = str(path or os.getenv('LEDGER_DB') or DEFAULT_DB)
    if path != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    with con:
        columns = {r['name'] for r in con.execute('PRAGMA table_info(profile_snapshots)')}
        for column in ('profile_json', 'enrichment_json'):
            if column not in columns:
                con.execute(f"ALTER TABLE profile_snapshots ADD COLUMN {column} TEXT NOT NULL DEFAULT '{{}}'")
    return con


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def evidence_hash(e):
    """What a person is judged on. Any change here makes earlier judgments of this person stale."""
    evidence = {k: e.get(k) or '' for k in ('position', 'location', 'company')} | {'comments': e.get('comments') or []}
    enrichment = {k: v for k, v in (e.get('icp_enrichment') or {}).items() if k != 'checked_at'}
    if enrichment:
        evidence['icp_enrichment'] = enrichment
    if e.get('profile_evidence'):
        evidence['profile_evidence'] = e['profile_evidence']
    return digest(evidence)


def aliases_of(e=None, url=None, actor_id=None):
    e = e or {}
    out = []
    aid = actor_id or e.get('id') or e.get('actor_id')
    if aid and str(aid).startswith('ACoAA'):
        out.append('id:' + aid)
    raw = url or e.get('linkedinUrl') or e.get('linkedin_url') or ''
    public = linkedin_url(raw)
    if public:
        out.append('url:' + public)
    elif raw:
        parsed = urlsplit(raw if '://' in raw else 'https://' + raw)
        parts = parsed.path.strip('/').split('/')
        if (parsed.hostname == 'linkedin.com' or (parsed.hostname or '').endswith('.linkedin.com')) and \
                len(parts) == 2 and parts[0] == 'in' and parts[1].startswith('ACoAA'):
            out.append('id:' + parts[1])
    return list(dict.fromkeys(out))


def find_contact(con, aliases):
    if not aliases:
        return None
    row = con.execute(f'SELECT MIN(contact_id) FROM aliases WHERE alias IN ({",".join("?" * len(aliases))})',
                      aliases).fetchone()
    return row[0]


def merge(con, keep, drop):
    """Two records turned out to be one person (an id seen alone in one run, a URL alone in another)."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_run_contacts'").fetchone():
        try:
            from .coredb import merge_contacts
        except ImportError:
            from src.coredb import merge_contacts
        merge_contacts(con, keep, drop)
    for r in con.execute('SELECT * FROM campaign_contacts WHERE contact_id=?', (drop,)).fetchall():
        save_campaign(con, keep, r['campaign'], r['status'], r['email'], r['receipt'], r['updated_at'])
    con.execute('DELETE FROM campaign_contacts WHERE contact_id=?', (drop,))
    for table in ('aliases', 'engagements', 'profile_snapshots'):
        con.execute(f'UPDATE OR IGNORE {table} SET contact_id=? WHERE contact_id=?', (keep, drop))
        con.execute(f'DELETE FROM {table} WHERE contact_id=?', (drop,))
    newer = con.execute('SELECT evidence_hash, updated_at FROM contacts WHERE id=?', (drop,)).fetchone()
    current = con.execute('SELECT updated_at FROM contacts WHERE id=?', (keep,)).fetchone()
    if (newer['updated_at'] or '') > (current['updated_at'] or ''):
        con.execute('UPDATE contacts SET evidence_hash=?, updated_at=? WHERE id=?', (*newer, keep))
    con.execute('DELETE FROM contacts WHERE id=?', (drop,))


def contact_for(con, aliases, name=None, slug=None):
    """-> (contact_id, created). Creates, extends or merges so every alias points at one contact."""
    if not aliases:
        return None, False
    found = sorted({r[0] for r in con.execute(
        f'SELECT contact_id FROM aliases WHERE alias IN ({",".join("?" * len(aliases))})', aliases)})
    created = not found
    if created:
        cid = con.execute('INSERT INTO contacts (name, first_slug, created_at, updated_at) VALUES (?,?,?,?)',
                          (name, slug, now(), now())).lastrowid
    else:
        cid = found[0]
        for other in found[1:]:
            merge(con, cid, other)
    for a in aliases:
        con.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (a, cid))
    if name:
        con.execute('UPDATE contacts SET name=COALESCE(name, ?), updated_at=? WHERE id=?', (name, now(), cid))
    return cid, created


def snapshot(con, cid, e, source):
    ev = {'headline': e.get('position') or '', 'location': e.get('location') or '', 'company': e.get('company') or ''}
    enrichment = e.get('icp_enrichment') or {}
    profile = e.get('profile_evidence') or {}
    if not any(ev.values()) and not enrichment and not profile:
        return
    h = digest(ev | {'icp_enrichment': enrichment, 'profile_evidence': profile})
    last = con.execute('SELECT hash FROM profile_snapshots WHERE contact_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
    if not last or last[0] != h:
        con.execute('INSERT INTO profile_snapshots (contact_id, headline, location, company, source, hash, taken_at, enrichment_json, profile_json) '
                    'VALUES (?,?,?,?,?,?,?,?,?)', (cid, *ev.values(), source, h, now(), json.dumps(enrichment), json.dumps(profile)))


def touch_evidence(con, cid, e):
    con.execute('UPDATE contacts SET evidence_hash=?, updated_at=? WHERE id=?', (evidence_hash(e), now(), cid))


def profile_state(con, cid):
    """Missing harvest fields do not erase earlier evidence; latest nonblank value wins per field."""
    state = {}
    for field, column in (('position', 'headline'), ('location', 'location'), ('company', 'company')):
        row = con.execute(f"SELECT {column} FROM profile_snapshots WHERE contact_id=? "
                          f"AND {column} IS NOT NULL AND TRIM({column}) != '' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
        state[field] = row[0] if row else ''
    row = con.execute("SELECT enrichment_json FROM profile_snapshots WHERE contact_id=? "
                      "AND enrichment_json != '{}' ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    if row:
        state['icp_enrichment'] = json.loads(row[0])
    row = con.execute("SELECT profile_json FROM profile_snapshots WHERE contact_id=? "
                      "AND profile_json != '{}' AND company=? ORDER BY id DESC LIMIT 1", (cid, state.get('company', ''))).fetchone()
    if row:
        state['profile_evidence'] = json.loads(row[0])
    return state


# ---------- lookups (shared across runs) ----------

def cached_lookup(con, opaque_id):
    row = con.execute('SELECT * FROM lookups WHERE opaque_id=?', (opaque_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    if row['status'] == 'resolved':
        cid = find_contact(con, ['id:' + opaque_id])
        state = profile_state(con, cid) if cid else {}
        out |= {key: state.get(key, '') for key in ('location', 'company', 'profile_evidence')}
    return out


def record_lookup(con, opaque_id, status, public_url=None, run_id=None, e=None):
    con.execute('INSERT OR REPLACE INTO lookups VALUES (?,?,?,?,?)', (opaque_id, status, public_url, run_id, now()))
    if e is not None and status == 'resolved':
        cid, _ = contact_for(con, aliases_of(e), e.get('name'))
        snapshot(con, cid, e, 'lookup')
        touch_evidence(con, cid, e)


# ---------- eligibility ----------

def suppression_for(con, aliases=(), email=None):
    keys = list(aliases)
    if email and '@' in email:
        keys += ['email:' + email.strip().lower(), 'domain:' + email.strip().lower().rsplit('@', 1)[1]]
    if not keys:
        return None
    row = con.execute(f'SELECT reason FROM suppressions WHERE key IN ({",".join("?" * len(keys))})', keys).fetchone()
    return row[0] if row else None


def status_for(con, aliases, email=None):
    """'eligible', 'suppressed:<reason>' or 'in_campaign:<campaign>' for the person behind these aliases."""
    aliases = list(aliases) + (['email:' + email.strip().lower()] if email else [])
    cid = find_contact(con, aliases)
    all_aliases = list(aliases) + ([r[0] for r in con.execute('SELECT alias FROM aliases WHERE contact_id=?', (cid,))]
                                   if cid else [])
    reason = suppression_for(con, all_aliases, email)
    if reason:
        return f'suppressed:{reason}'
    if cid:
        c = con.execute(f'SELECT campaign FROM campaign_contacts WHERE contact_id=? AND status IN '
                        f'({",".join("?" * len(BLOCKING))}) ORDER BY updated_at LIMIT 1', (cid, *BLOCKING)).fetchone()
        if c:
            return f'in_campaign:{c[0]}'
    return 'eligible'


def save_campaign(con, cid, campaign, status, email=None, receipt=None, updated_at=None, resolve=False):
    previous = con.execute('SELECT * FROM campaign_contacts WHERE contact_id=? AND campaign=?', (cid, campaign)).fetchone()
    if previous:
        # A later explicit failed result can resolve uncertainty, but cannot undo a confirmed import or reply.
        if STATUS_PRIORITY.get(previous['status'], 0) > STATUS_PRIORITY.get(status, 0) and not (
                resolve and status == 'failed' and previous['status'] in ('needs_reconciliation', 'unconfirmed')):
            status = previous['status']
        email = email or previous['email']
        receipts = []
        for value in (previous['receipt'], receipt):
            if value:
                try:
                    parsed = json.loads(value)
                except (ValueError, TypeError):
                    parsed = value
                receipts.extend(parsed if isinstance(parsed, list) else [value])
        receipts = list(dict.fromkeys(receipts))
        receipt = receipts[0] if len(receipts) == 1 else json.dumps(receipts) if receipts else None
    con.execute('INSERT OR REPLACE INTO campaign_contacts VALUES (?,?,?,?,?,?)',
                (cid, campaign, status, email, receipt, updated_at or now()))


def record_campaign(con, leads, campaign, status, receipt=None, resolve=False):
    """leads: [(linkedin_url, email)]. -> number of people actually recorded."""
    recorded = 0
    for url, email in leads:
        aliases = aliases_of(url=url) + (['email:' + email.strip().lower()] if email else [])
        cid, _ = contact_for(con, aliases)
        if cid is not None:
            save_campaign(con, cid, campaign, status, email, receipt, resolve=resolve)
            recorded += 1
    con.commit()
    return recorded


def stats(con):
    one = lambda q, *a: con.execute(q, a).fetchone()[0]
    has = lambda t: con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (t,)).fetchone()
    out = {
        'contacts': one('SELECT COUNT(*) FROM contacts'),
        'seen in 2+ runs': one('SELECT COUNT(*) FROM (SELECT contact_id FROM engagements GROUP BY contact_id '
                               'HAVING COUNT(DISTINCT slug) > 1)'),
        'engagements': one('SELECT COUNT(*) FROM engagements'),
        'lookups': dict(con.execute('SELECT status, COUNT(*) FROM lookups GROUP BY status').fetchall()),
        'campaign contacts': dict(con.execute('SELECT status, COUNT(*) FROM campaign_contacts GROUP BY status').fetchall()),
        'suppressions': one('SELECT COUNT(*) FROM suppressions'),
    }
    if has('assessments'):
        out |= {'companies': one('SELECT COUNT(*) FROM core_companies'),
                'assessments': one('SELECT COUNT(*) FROM assessments'),
                'qualified leads (current view)': one('SELECT COUNT(*) FROM qualified_leads')}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', help=f'SQLite path (default $LEDGER_DB or {DEFAULT_DB})')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('suppress', help='never contact these people (customers, teammates, opt-outs)')
    p.add_argument('--url'); p.add_argument('--email'); p.add_argument('--domain')
    p.add_argument('--csv', help='CSV with linkedin_url and/or email columns')
    p.add_argument('--reason', required=True)
    p = sub.add_parser('mark', help='record outreach status by hand (e.g. replied) for one URL or the people in a CSV')
    p.add_argument('--campaign', required=True)
    p.add_argument('--status', required=True, choices=['contacted', 'replied', 'excluded', 'imported', 'failed'])
    p.add_argument('--url'); p.add_argument('--csv')
    sub.add_parser('stats', help='counts: contacts, repeats, lookups, assessments, campaign statuses')
    a = ap.parse_args(argv)
    con = connect(a.db)
    if a.cmd == 'suppress':
        if not (a.url or a.email or a.domain or a.csv):
            raise ValueError('suppress needs --url, --email, --domain or --csv')
        keys = []
        if a.url:
            keys += aliases_of(url=a.url)
        keys += ['email:' + a.email.lower()] if a.email else []
        keys += ['domain:' + a.domain.lower().lstrip('@')] if a.domain else []
        for r in (read_csv(a.csv)[0] if a.csv else []):
            keys += ['url:' + linkedin_url(r.get('linkedin_url'))] if linkedin_url(r.get('linkedin_url')) else []
            keys += ['email:' + r['email'].strip().lower()] if '@' in (r.get('email') or '') else []
        if not keys:
            raise ValueError('Nothing to suppress: no valid LinkedIn URL, email or domain found')
        con.executemany('INSERT OR REPLACE INTO suppressions VALUES (?,?,?)', [(k, a.reason, now()) for k in keys])
        con.commit()
        print(f'Suppressed {len(keys)} keys ({a.reason})')
    elif a.cmd == 'mark':
        if not (a.url or a.csv):
            raise ValueError('mark needs --url or --csv')
        rows = read_csv(a.csv)[0] if a.csv else [{'linkedin_url': a.url}]
        leads = [(r.get('linkedin_url'), r.get('email')) for r in rows]
        recorded = record_campaign(con, leads, a.campaign, a.status, resolve=True)
        print(f'Marked {recorded} of {len(leads)} as {a.status} in {a.campaign}'
              + ('' if recorded == len(leads) else ' (rows without a valid LinkedIn URL or email were skipped)'))
    else:
        print(json.dumps(stats(con), indent=1))


if __name__ == '__main__':
    try:
        from .common import cli
    except ImportError:
        from common import cli
    cli(main)
