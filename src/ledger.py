"""Local SQLite ledger: one record per person across runs and competitors, and the rules that keep exports honest.

Records (data/ledger.sqlite, or $LEDGER_DB):
  contacts + aliases     one person; aliases link the opaque LinkedIn id (id:ACoAA...) and the public URL (url:...)
  engagements            comments / post touches per post, never inserted twice
  profile_snapshots      location / company / headline evidence over time
  lookups                profile-scraper results, shared across runs (resolved / not_found + date)
  icp_versions + scores  a score belongs to a contact AND an ICP version, and records the evidence and scoring
                         rubric that produced it
  campaign_contacts      imports and outreach status per campaign
  suppressions           customers, teammates, opt-outs: by URL, email or domain

Rules:
  - A score is fresh only while its evidence hash, ICP version and rubric hash all match the current ones.
    Location changes -> evidence hash changes -> the score is stale -> the person waits for rescoring.
  - Stale scores are never reused; fresh ones are, so a person seen again with the same evidence is not rescored.
  - gate stamps every ranked row with ledger_status: eligible | stale_score | suppressed:<reason> |
    in_campaign:<campaign>. Activity, drafts, emails and campaign pushes only take `eligible` rows.

CLI: ingest | gate | suppress | mark | stats   (see --help on each)
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from .common import linkedin_url, read_csv, write_csv
except ImportError:
    from common import linkedin_url, read_csv, write_csv

DEFAULT_DB = 'data/ledger.sqlite'
BLOCKING = ('imported', 'contacted', 'replied', 'excluded')  # campaign statuses that stop a repeat push

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
CREATE TABLE IF NOT EXISTS icp_versions (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, icp TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS scores (contact_id INTEGER NOT NULL, icp_id INTEGER NOT NULL, icp_fit INTEGER,
    intent TEXT, evidence_comment INTEGER, clean_title TEXT, company_guess TEXT, evidence_hash TEXT,
    rubric_hash TEXT, scorer TEXT, slug TEXT, scored_at TEXT, PRIMARY KEY (contact_id, icp_id));
CREATE TABLE IF NOT EXISTS campaign_contacts (contact_id INTEGER NOT NULL, campaign TEXT NOT NULL, status TEXT NOT NULL,
    email TEXT, receipt TEXT, updated_at TEXT, UNIQUE (contact_id, campaign));
CREATE TABLE IF NOT EXISTS suppressions (key TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
'''


def connect(path=None):
    path = str(path or os.getenv('LEDGER_DB') or DEFAULT_DB)
    if path != ':memory:':
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def evidence_hash(e):
    """What a score is judged on. Any change here makes earlier scores of this person stale."""
    return digest({k: e.get(k) or '' for k in ('position', 'location', 'company')} | {'comments': e.get('comments') or []})


def icp_hash(icp):
    return digest({k: v for k, v in (icp or {}).items() if k not in ('slug', 'sources')})


def rubric_hash(system):
    return digest(system)


def aliases_of(e=None, url=None, actor_id=None):
    e = e or {}
    out = []
    aid = actor_id or e.get('id') or e.get('actor_id')
    if aid and str(aid).startswith('ACoAA'):
        out.append('id:' + aid)
    public = linkedin_url(url or e.get('linkedinUrl') or e.get('linkedin_url'))
    if public:
        out.append('url:' + public)
    return out


def find_contact(con, aliases):
    if not aliases:
        return None
    row = con.execute(f'SELECT MIN(contact_id) FROM aliases WHERE alias IN ({",".join("?" * len(aliases))})',
                      aliases).fetchone()
    return row[0]


def merge(con, keep, drop):
    """Two records turned out to be one person (an id seen alone in one run, a URL alone in another)."""
    for table in ('aliases', 'engagements', 'profile_snapshots', 'scores', 'campaign_contacts'):
        con.execute(f'UPDATE OR IGNORE {table} SET contact_id=? WHERE contact_id=?', (keep, drop))
        con.execute(f'DELETE FROM {table} WHERE contact_id=?', (drop,))
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
    if not any(ev.values()):
        return
    h = digest(ev)
    last = con.execute('SELECT hash FROM profile_snapshots WHERE contact_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
    if not last or last[0] != h:
        con.execute('INSERT INTO profile_snapshots (contact_id, headline, location, company, source, hash, taken_at) '
                    'VALUES (?,?,?,?,?,?,?)', (cid, *ev.values(), source, h, now()))


def touch_evidence(con, cid, e):
    con.execute('UPDATE contacts SET evidence_hash=?, updated_at=? WHERE id=?', (evidence_hash(e), now(), cid))


def ingest(con, base):
    """Harvest -> contacts, aliases, engagements, snapshots. Idempotent. -> (contacts, new, seen before)."""
    base = Path(base)
    slug = base.name
    new = seen = 0
    for e in json.loads((base / 'engagers.json').read_text(encoding='utf-8')):
        cid, created = contact_for(con, aliases_of(e), e.get('name'), slug)
        if cid is None:
            continue
        new += created
        seen += (not created) and con.execute('SELECT first_slug FROM contacts WHERE id=?', (cid,)).fetchone()[0] != slug
        snapshot(con, cid, e, 'harvest')
        touch_evidence(con, cid, e)
        if (e.get('id') or '').startswith('ACoAA') and e.get('resolve_status') in ('resolved', 'not_found'):
            # lookups paid for before this ledger existed (or in this run): never pay for them again
            con.execute('INSERT OR IGNORE INTO lookups VALUES (?,?,?,?,?)',
                        (e['id'], e['resolve_status'], linkedin_url(e.get('linkedinUrl')) or None, None,
                         e.get('resolve_checked_at') or now()))
        urls = dict(zip(e.get('posts_touched') or [], e.get('post_urls') or []))
        for pid in e.get('posts_touched') or []:
            con.execute('INSERT OR IGNORE INTO engagements VALUES (?,?,?,?,?,?,?)',
                        (cid, pid, 'engaged', '', urls.get(pid), slug, now()))
        for text, pid in zip(e.get('comments') or [], e.get('comment_post_ids') or [''] * len(e.get('comments') or [])):
            con.execute('INSERT OR IGNORE INTO engagements VALUES (?,?,?,?,?,?,?)',
                        (cid, pid or '', 'comment', text, urls.get(pid), slug, now()))
    con.commit()
    return new, seen


# ---------- lookups (shared across runs) ----------

def cached_lookup(con, opaque_id):
    row = con.execute('SELECT * FROM lookups WHERE opaque_id=?', (opaque_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    if row['status'] == 'resolved':
        snap = con.execute('SELECT s.* FROM profile_snapshots s JOIN aliases a ON a.contact_id=s.contact_id '
                           'WHERE a.alias=? ORDER BY s.id DESC LIMIT 1', ('id:' + opaque_id,)).fetchone()
        out |= {'location': snap['location'] if snap else '', 'company': snap['company'] if snap else ''}
    return out


def record_lookup(con, opaque_id, status, public_url=None, run_id=None, e=None):
    con.execute('INSERT OR REPLACE INTO lookups VALUES (?,?,?,?,?)', (opaque_id, status, public_url, run_id, now()))
    if e is not None and status == 'resolved':
        cid, _ = contact_for(con, aliases_of(e), e.get('name'))
        snapshot(con, cid, e, 'lookup')
        touch_evidence(con, cid, e)


# ---------- scores ----------

def icp_version(con, icp):
    h = icp_hash(icp)
    con.execute('INSERT OR IGNORE INTO icp_versions (hash, icp, created_at) VALUES (?,?,?)',
                (h, json.dumps(icp, sort_keys=True, ensure_ascii=False), now()))
    return con.execute('SELECT id FROM icp_versions WHERE hash=?', (h,)).fetchone()[0]


def record_scores(con, engagers, results, icp, system, scorer, slug):
    icp_id, rh = icp_version(con, icp), rubric_hash(system)
    for e, r in zip(engagers, results):
        cid, _ = contact_for(con, aliases_of(e), e.get('name'), slug)
        if cid is None:
            continue
        touch_evidence(con, cid, e)
        con.execute('INSERT OR REPLACE INTO scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (cid, icp_id, r.get('icp_fit'), r.get('intent'), r.get('evidence_comment'), r.get('clean_title'),
                     r.get('company_guess'), evidence_hash(e), rh, scorer, slug, now()))
    con.commit()


def fresh_scores(con, engagers, icp, system):
    """-> {engager index: result} for people whose stored score still matches evidence, ICP version and rubric."""
    row = con.execute('SELECT id FROM icp_versions WHERE hash=?', (icp_hash(icp),)).fetchone()
    if not row:
        return {}
    icp_id, rh, out = row[0], rubric_hash(system), {}
    for i, e in enumerate(engagers):
        cid = find_contact(con, aliases_of(e))
        s = cid and con.execute('SELECT * FROM scores WHERE contact_id=? AND icp_id=?', (cid, icp_id)).fetchone()
        if s and s['evidence_hash'] == evidence_hash(e) and s['rubric_hash'] == rh:
            out[i] = {'i': i, 'icp_fit': s['icp_fit'], 'intent': s['intent'], 'evidence_comment': s['evidence_comment'],
                      'clean_title': s['clean_title'], 'company_guess': s['company_guess']}
    return out


# ---------- eligibility ----------

def suppression_for(con, aliases=(), email=None):
    keys = [a for a in aliases if a.startswith('url:')]
    if email and '@' in email:
        keys += ['email:' + email.strip().lower(), 'domain:' + email.strip().lower().rsplit('@', 1)[1]]
    if not keys:
        return None
    row = con.execute(f'SELECT reason FROM suppressions WHERE key IN ({",".join("?" * len(keys))})', keys).fetchone()
    return row[0] if row else None


def status_for(con, aliases, icp_id=None, rh=None, ev=None, email=None):
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
        if icp_id is not None:
            s = con.execute('SELECT evidence_hash, rubric_hash FROM scores WHERE contact_id=? AND icp_id=?',
                            (cid, icp_id)).fetchone()
            if not s or s['evidence_hash'] != ev or s['rubric_hash'] != rh:
                return 'stale_score'
    return 'eligible'


def gate(con, base):
    """Stamp ledger_status on every row of ranked_engagers.csv. -> Counter of statuses."""
    from src.rank import SYSTEM  # lazy: rank imports this module
    base = Path(base)
    icp = json.loads((base / 'recipient_icp.json').read_text(encoding='utf-8'))
    engagers = {e.get('id'): e for e in json.loads((base / 'engagers.json').read_text(encoding='utf-8'))}
    row = con.execute('SELECT id FROM icp_versions WHERE hash=?', (icp_hash(icp),)).fetchone()
    icp_id, rh = (row[0] if row else -1), rubric_hash(SYSTEM)
    rows, fields = read_csv(base / 'ranked_engagers.csv')
    for r in rows:
        e = engagers.get(r.get('actor_id')) or {}
        r['ledger_status'] = status_for(con, aliases_of(url=r.get('linkedin_url'), actor_id=r.get('actor_id')),
                                        icp_id, rh, evidence_hash(e) if e else None, r.get('email'))
    write_csv(base / 'ranked_engagers.csv', rows, fields + ([] if 'ledger_status' in fields else ['ledger_status']))
    return Counter(r['ledger_status'].split(':')[0] for r in rows)


def record_campaign(con, leads, campaign, status, receipt=None):
    """leads: [(linkedin_url, email)] confirmed by the provider."""
    for url, email in leads:
        cid, _ = contact_for(con, aliases_of(url=url)) if linkedin_url(url) else (None, False)
        if cid is None and email:  # email-only lead: suppress by email so it is still never pushed twice
            con.execute('INSERT OR IGNORE INTO suppressions VALUES (?,?,?)',
                        ('email:' + email.lower(), f'in_campaign:{campaign}', now()))
            continue
        if cid is not None:
            con.execute('INSERT INTO campaign_contacts VALUES (?,?,?,?,?,?) ON CONFLICT (contact_id, campaign) '
                        'DO UPDATE SET status=excluded.status, email=COALESCE(excluded.email, email), '
                        'receipt=excluded.receipt, updated_at=excluded.updated_at',
                        (cid, campaign, status, email or None, receipt, now()))
    con.commit()


def record_receipt(con, leads, receipt_path, campaign, url_key, email_key):
    """Record every provider-confirmed batch of a push receipt as imported (unconfirmed batches as unconfirmed,
    which does not block a retry: reconcile those in the provider first). Works for partial runs too."""
    try:
        receipt = json.loads(Path(receipt_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    n = 0
    for b in receipt.get('batches', []):
        batch = leads[b['offset']:b['offset'] + b['requested']]
        status = 'imported' if b.get('status') == 'confirmed' else 'unconfirmed'
        record_campaign(con, [(l.get(url_key), l.get(email_key)) for l in batch], campaign, status, str(receipt_path))
        n += len(batch) if status == 'imported' else 0
    return n


def filter_blocked(con, leads, url_key, email_key):
    """-> (kept leads, number dropped) using suppressions and campaign history."""
    kept = [l for l in leads if not blocked(con, l.get(url_key), l.get(email_key))]
    return kept, len(leads) - len(kept)


def blocked(con, url=None, email=None):
    """Reason a push must skip this lead, or None. Used by the Smartlead and HeyReach stages."""
    s = status_for(con, aliases_of(url=url), email=email)
    return None if s == 'eligible' else s


def stats(con):
    one = lambda q, *a: con.execute(q, a).fetchone()[0]
    stale = one('SELECT COUNT(*) FROM scores s JOIN contacts c ON c.id=s.contact_id WHERE s.evidence_hash != c.evidence_hash')
    return {
        'contacts': one('SELECT COUNT(*) FROM contacts'),
        'seen in 2+ runs': one('SELECT COUNT(*) FROM (SELECT contact_id FROM engagements GROUP BY contact_id '
                               'HAVING COUNT(DISTINCT slug) > 1)'),
        'engagements': one('SELECT COUNT(*) FROM engagements'),
        'lookups': dict(con.execute('SELECT status, COUNT(*) FROM lookups GROUP BY status').fetchall()),
        'icp versions': one('SELECT COUNT(*) FROM icp_versions'),
        'scores': one('SELECT COUNT(*) FROM scores'), 'stale scores': stale,
        'campaign contacts': dict(con.execute('SELECT status, COUNT(*) FROM campaign_contacts GROUP BY status').fetchall()),
        'suppressions': one('SELECT COUNT(*) FROM suppressions'),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--db', help=f'SQLite path (default $LEDGER_DB or {DEFAULT_DB})')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('ingest', 'gate'):
        p = sub.add_parser(name, help='record a harvest' if name == 'ingest' else 'stamp ledger_status on ranked_engagers.csv')
        p.add_argument('--slug', required=True)
        p.add_argument('--output-dir', default='./output')
    p = sub.add_parser('suppress', help='never push these people (customers, teammates, opt-outs)')
    p.add_argument('--url'); p.add_argument('--email'); p.add_argument('--domain')
    p.add_argument('--csv', help='CSV with linkedin_url and/or email columns')
    p.add_argument('--reason', required=True)
    p = sub.add_parser('mark', help='record outreach status by hand (e.g. replied) for people in a CSV or one URL')
    p.add_argument('--campaign', required=True)
    p.add_argument('--status', required=True, choices=['contacted', 'replied', 'excluded', 'imported'])
    p.add_argument('--url'); p.add_argument('--csv')
    sub.add_parser('stats', help='counts, stale scores, campaign statuses')
    a = ap.parse_args()
    con = connect(a.db)
    if a.cmd == 'ingest':
        new, seen = ingest(con, Path(a.output_dir) / a.slug)
        print(f'Ledger: {new} new contacts; {seen} already seen in earlier runs')
    elif a.cmd == 'gate':
        c = gate(con, Path(a.output_dir) / a.slug)
        print(f'Ledger gate: {dict(c)}')
    elif a.cmd == 'suppress':
        keys = []
        if a.url and linkedin_url(a.url):
            keys.append('url:' + linkedin_url(a.url))
        keys += ['email:' + a.email.lower()] if a.email else []
        keys += ['domain:' + a.domain.lower().lstrip('@')] if a.domain else []
        for r in (read_csv(a.csv)[0] if a.csv else []):
            keys += ['url:' + linkedin_url(r.get('linkedin_url'))] if linkedin_url(r.get('linkedin_url')) else []
            keys += ['email:' + r['email'].strip().lower()] if '@' in (r.get('email') or '') else []
        con.executemany('INSERT OR REPLACE INTO suppressions VALUES (?,?,?)', [(k, a.reason, now()) for k in keys])
        con.commit()
        print(f'Suppressed {len(keys)} keys ({a.reason})')
    elif a.cmd == 'mark':
        rows = read_csv(a.csv)[0] if a.csv else [{'linkedin_url': a.url}]
        leads = [(r.get('linkedin_url'), r.get('email')) for r in rows]
        record_campaign(con, leads, a.campaign, a.status)
        print(f'Marked {len(leads)} as {a.status} in {a.campaign}')
    else:
        print(json.dumps(stats(con), indent=1))


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # `from src.rank import SYSTEM` when run as a script
    main()
