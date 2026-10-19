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
from urllib.parse import urlsplit

try:
    from .common import linkedin_url, read_csv, write_csv, write_json
except ImportError:
    from common import linkedin_url, read_csv, write_csv, write_json

DEFAULT_DB = 'data/ledger.sqlite'
BLOCKING = ('imported', 'contacted', 'replied', 'excluded', 'needs_reconciliation')
EXPORT_FIELDS = ['ledger_icp_hash', 'ledger_evidence_hash', 'ledger_rubric_hash', 'ledger_score_hash']
STATUS_PRIORITY = {'failed': 0, 'unconfirmed': 1, 'needs_reconciliation': 2,
                   'imported': 3, 'contacted': 4, 'replied': 5, 'excluded': 6}
SCORE_COLUMNS = ['contact_id', 'icp_id', 'icp_fit', 'intent', 'evidence_comment', 'clean_title',
                 'company_guess', 'evidence_hash', 'rubric_hash', 'scorer', 'slug', 'scored_at',
                 'qualification_reason', 'intent_reason']

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
CREATE TABLE IF NOT EXISTS icp_versions (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, icp TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS scores (contact_id INTEGER NOT NULL, icp_id INTEGER NOT NULL, icp_fit INTEGER,
    intent TEXT, evidence_comment INTEGER, clean_title TEXT, company_guess TEXT, evidence_hash TEXT,
    rubric_hash TEXT, scorer TEXT, slug TEXT, scored_at TEXT,
    qualification_reason TEXT NOT NULL DEFAULT '', intent_reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (contact_id, icp_id));
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
    columns = {r['name'] for r in con.execute('PRAGMA table_info(scores)')}
    with con:
        for column in ('qualification_reason', 'intent_reason'):
            if column not in columns:
                con.execute(f"ALTER TABLE scores ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        if 'enrichment_json' not in {r['name'] for r in con.execute('PRAGMA table_info(profile_snapshots)')}:
            con.execute("ALTER TABLE profile_snapshots ADD COLUMN enrichment_json TEXT NOT NULL DEFAULT '{}'")
    return con


def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def evidence_hash(e):
    """What a score is judged on. Any change here makes earlier scores of this person stale."""
    evidence = {k: e.get(k) or '' for k in ('position', 'location', 'company')} | {'comments': e.get('comments') or []}
    enrichment = {k: v for k, v in (e.get('icp_enrichment') or {}).items() if k != 'checked_at'}
    if enrichment:
        evidence['icp_enrichment'] = enrichment
    return digest(evidence)


def icp_hash(icp):
    return digest({k: v for k, v in (icp or {}).items() if k not in ('slug', 'sources')})


def rubric_hash(system):
    # Invalidate pre-provenance scores, including judgments incorrectly certified by earlier retries.
    return digest({'protocol': 2, 'system': system})


def score_context(e, icp, system):
    """Fingerprint copied from the dump into each submitted judgment; never inferred from the current file."""
    identity = e.get('id') or linkedin_url(e.get('linkedinUrl')) or ''
    return digest([identity, evidence_hash(e), icp_hash(icp), rubric_hash(system)])


def score_hash(score):
    return digest({k: score[k] for k in ('icp_fit', 'intent', 'evidence_comment', 'clean_title', 'company_guess',
                                       'qualification_reason', 'intent_reason')})


def has_reasons(score):
    return all(isinstance(score[k], str) and score[k].strip() for k in ('qualification_reason', 'intent_reason'))


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
    for r in con.execute('SELECT * FROM campaign_contacts WHERE contact_id=?', (drop,)).fetchall():
        save_campaign(con, keep, r['campaign'], r['status'], r['email'], r['receipt'], r['updated_at'])
    con.execute('DELETE FROM campaign_contacts WHERE contact_id=?', (drop,))
    for r in con.execute('SELECT * FROM scores WHERE contact_id=?', (drop,)).fetchall():
        current = con.execute('SELECT scored_at FROM scores WHERE contact_id=? AND icp_id=?', (keep, r['icp_id'])).fetchone()
        if not current or (r['scored_at'] or '') >= (current['scored_at'] or ''):
            con.execute(f'INSERT OR REPLACE INTO scores ({",".join(SCORE_COLUMNS)}) VALUES ({",".join("?" * len(SCORE_COLUMNS))})',
                        (keep, *(r[k] for k in SCORE_COLUMNS[1:])))
    con.execute('DELETE FROM scores WHERE contact_id=?', (drop,))
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
    if not any(ev.values()) and not enrichment:
        return
    h = digest(ev | {'icp_enrichment': enrichment})
    last = con.execute('SELECT hash FROM profile_snapshots WHERE contact_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
    if not last or last[0] != h:
        con.execute('INSERT INTO profile_snapshots (contact_id, headline, location, company, source, hash, taken_at, enrichment_json) '
                    'VALUES (?,?,?,?,?,?,?,?)', (cid, *ev.values(), source, h, now(), json.dumps(enrichment)))


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
    return state


def ingest(con, base):
    """Harvest -> contacts, aliases, engagements, snapshots. Idempotent. -> (contacts, new, seen before)."""
    base = Path(base)
    slug = base.name
    new = seen = 0
    engagers = json.loads((base / 'engagers.json').read_text(encoding='utf-8'))
    for e in engagers:
        cid, created = contact_for(con, aliases_of(e), e.get('name'), slug)
        if cid is None:
            continue
        new += created
        seen += (not created) and con.execute('SELECT first_slug FROM contacts WHERE id=?', (cid,)).fetchone()[0] != slug
        for key, value in profile_state(con, cid).items():
            e[key] = e.get(key) or value
        if (e.get('id') or '').startswith('ACoAA') and not linkedin_url(e.get('linkedinUrl')):
            cached = cached_lookup(con, e['id'])
            if cached and cached['status'] == 'resolved' and cached['public_url']:
                e['linkedinUrl'], e['resolve_status'] = cached['public_url'], 'resolved'
                cid, _ = contact_for(con, aliases_of(e), e.get('name'), slug)
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
    write_json(base / 'engagers.json', engagers)
    return new, seen


# ---------- lookups (shared across runs) ----------

def cached_lookup(con, opaque_id):
    row = con.execute('SELECT * FROM lookups WHERE opaque_id=?', (opaque_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    if row['status'] == 'resolved':
        cid = find_contact(con, ['id:' + opaque_id])
        state = profile_state(con, cid) if cid else {}
        out |= {key: state.get(key, '') for key in ('location', 'company')}
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
        con.execute(f'INSERT OR REPLACE INTO scores ({",".join(SCORE_COLUMNS)}) VALUES ({",".join("?" * len(SCORE_COLUMNS))})',
                    (cid, icp_id, r.get('icp_fit'), r.get('intent'), r.get('evidence_comment'), r.get('clean_title'),
                     r.get('company_guess'), evidence_hash(e), rh, scorer, slug, now(),
                     r.get('qualification_reason') or '', r.get('intent_reason') or ''))
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
        if s and s['evidence_hash'] == evidence_hash(e) and s['rubric_hash'] == rh and has_reasons(s):
            out[i] = {'i': i, 'icp_fit': s['icp_fit'], 'intent': s['intent'], 'evidence_comment': s['evidence_comment'],
                      'clean_title': s['clean_title'], 'company_guess': s['company_guess'],
                      'qualification_reason': s['qualification_reason'], 'intent_reason': s['intent_reason'],
                      'score_context': score_context(e, icp, system)}
    return out


def pending_scores(con, base):
    try:
        from .rank import SYSTEM
    except ImportError:
        from rank import SYSTEM
    base = Path(base)
    engagers = json.loads((base / 'engagers.json').read_text(encoding='utf-8'))
    icp = json.loads((base / 'recipient_icp.json').read_text(encoding='utf-8'))
    reused = fresh_scores(con, engagers, icp, SYSTEM)
    return [(i, e.get('name') or '?') for i, e in enumerate(engagers) if i not in reused]


# ---------- eligibility ----------

def suppression_for(con, aliases=(), email=None):
    keys = list(aliases)
    if email and '@' in email:
        keys += ['email:' + email.strip().lower(), 'domain:' + email.strip().lower().rsplit('@', 1)[1]]
    if not keys:
        return None
    row = con.execute(f'SELECT reason FROM suppressions WHERE key IN ({",".join("?" * len(keys))})', keys).fetchone()
    return row[0] if row else None


def status_for(con, aliases, icp_id=None, rh=None, ev=None, email=None):
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
        if icp_id is not None:
            s = con.execute('SELECT * FROM scores WHERE contact_id=? AND icp_id=?',
                            (cid, icp_id)).fetchone()
            current = con.execute('SELECT evidence_hash FROM contacts WHERE id=?', (cid,)).fetchone()[0]
            if not s or s['evidence_hash'] != ev or s['rubric_hash'] != rh or current != ev or not has_reasons(s):
                return 'stale_score'
    elif icp_id is not None:
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
        r['eligibility_reason'] = eligibility_reason(r['ledger_status'])
        cid = find_contact(con, aliases_of(e))
        score = con.execute('SELECT * FROM scores WHERE contact_id=? AND icp_id=?', (cid, icp_id)).fetchone() if cid else None
        r.update(dict(zip(EXPORT_FIELDS, [icp_hash(icp), evidence_hash(e), rh, score_hash(score) if score else ''])))
    write_csv(base / 'ranked_engagers.csv', rows, fields + [k for k in ['ledger_status', 'eligibility_reason'] + EXPORT_FIELDS if k not in fields])
    return Counter(r['ledger_status'].split(':')[0] for r in rows)


def eligibility_reason(status):
    if status == 'eligible':
        return 'Ledger checks passed: current score and no recorded suppression or campaign block. ICP fit and channel suitability still apply.'
    if status == 'stale_score':
        return 'Hold outreach: score or explanation is missing or does not match the current evidence, ICP or rubric; rescore first.'
    kind, _, detail = status.partition(':')
    if kind == 'suppressed':
        return f'Do not contact: on the do-not-contact list ({detail}).'
    if kind == 'in_campaign':
        return f'Hold outreach: campaign history already records this person in {detail}; resolve any uncertain import in the provider first.'
    return f'Hold outreach: ledger status {status}.'


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
    """leads: [(linkedin_url, email)] confirmed by the provider."""
    recorded = 0
    for url, email in leads:
        aliases = aliases_of(url=url) + (['email:' + email.strip().lower()] if email else [])
        cid, _ = contact_for(con, aliases)
        if cid is not None:
            save_campaign(con, cid, campaign, status, email, receipt, resolve=resolve)
            recorded += 1
    con.commit()
    return recorded


def record_receipt(con, leads, receipt_path, campaign, url_key, email_key):
    """All-success batches are imported; mixed aggregates need reconciliation; all-failed batches can retry.
    Unconfirmed batches retain their existing retry policy. Returns the number actually recorded as imported."""
    try:
        receipt = json.loads(Path(receipt_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return 0
    n = 0
    for b in receipt.get('batches', []):
        batch = leads[b['offset']:b['offset'] + b['requested']]
        status = 'unconfirmed'
        if b.get('status') == 'confirmed':
            failures = b.get('failedLeadsCount', b.get('skipped', 0))
            status = ('failed' if b.get('failedLeadsCount') == len(batch) else 'needs_reconciliation') if failures else 'imported'
        recorded = record_campaign(con, [(l.get(url_key), l.get(email_key)) for l in batch], campaign, status, str(receipt_path))
        n += recorded if status == 'imported' else 0
    return n


def filter_blocked(con, leads, url_key, email_key):
    """-> (kept leads, number dropped) using suppressions and campaign history."""
    kept = [l for l in leads if not blocked(con, l.get(url_key), l.get(email_key))]
    return kept, len(leads) - len(kept)


def filter_export_rows(con, rows, base=None):
    """Revalidate CSV provenance and current evidence immediately before preview/import."""
    try:
        from .rank import SYSTEM
    except ImportError:
        from rank import SYSTEM
    recipient = Path(base) / 'recipient_icp.json' if base else None
    expected_icp = icp_hash(json.loads(recipient.read_text(encoding='utf-8'))) if recipient and recipient.exists() else None
    kept = []
    for row in rows:
        aliases = aliases_of(row)
        version = con.execute('SELECT id FROM icp_versions WHERE hash=?', (row.get('ledger_icp_hash'),)).fetchone()
        if not version or not all(row.get(k) for k in EXPORT_FIELDS):
            continue
        if expected_icp is not None and row['ledger_icp_hash'] != expected_icp:
            continue
        status = status_for(con, aliases, version[0], rubric_hash(SYSTEM), row['ledger_evidence_hash'], row.get('email'))
        cid = find_contact(con, aliases)
        score = con.execute('SELECT * FROM scores WHERE contact_id=? AND icp_id=?', (cid, version[0])).fetchone() if cid else None
        if status == 'eligible' and row['ledger_rubric_hash'] == rubric_hash(SYSTEM) and score and \
                row['ledger_score_hash'] == score_hash(score) and str(row.get('icp_fit')) == str(score['icp_fit']):
            kept.append(row)
    return kept, len(rows) - len(kept)


def export_validator(con, rows, url_key, email_key, base=None):
    """Check original provenance for each batch without attaching internal metadata to provider payloads."""
    def key(url, email):
        aliases = aliases_of(url=url)
        return tuple(aliases) if aliases else ('email:' + (email or '').strip().lower(),)
    original = {}
    for row in rows:
        original.setdefault(key(row.get('linkedin_url'), row.get('email')), row)
    def validate(batch):
        selected = [original.get(key(lead.get(url_key), lead.get(email_key))) for lead in batch]
        if any(row is None for row in selected) or filter_export_rows(con, selected, base)[1]:
            raise RuntimeError('Lead eligibility changed before import; regenerate the ledger gate and preview before retrying')
    return validate


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
    p.add_argument('--status', required=True, choices=['contacted', 'replied', 'excluded', 'imported', 'failed'])
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
        if a.url:
            keys += aliases_of(url=a.url)
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
        record_campaign(con, leads, a.campaign, a.status, resolve=True)
        print(f'Marked {len(leads)} as {a.status} in {a.campaign}')
    else:
        print(json.dumps(stats(con), indent=1))


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # `from src.rank import SYSTEM` when run as a script
    main()
