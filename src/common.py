"""Shared validation, atomic artifacts and redacted HTTP errors."""
import csv
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


def linkedin_url(value):
    value = (value or '').strip()
    if not value:
        return ''
    p = urlsplit(value if '://' in value else 'https://' + value)
    if p.hostname != 'linkedin.com' and not (p.hostname or '').endswith('.linkedin.com'):
        return ''
    parts = p.path.strip('/').split('/')
    if len(parts) != 2 or parts[0] != 'in' or not parts[1] or parts[1].startswith('ACoAA'):
        return ''
    return 'https://www.linkedin.com/in/' + parts[1].lower()


def location_text(value):
    """HarvestAPI location: a plain string, or {linkedinText, parsed: {text}} on full profiles."""
    if isinstance(value, dict):
        return value.get('linkedinText') or (value.get('parsed') or {}).get('text') or ''
    return value or ''


def valid_email(value):
    return bool(re.fullmatch(r'[^\s@*]+@[^\s@*]+\.[^\s@*]+', value or ''))


def fit(row):
    try:
        return int(row.get('icp_fit', 0))
    except (ValueError, TypeError):
        return 0


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def read_csv(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError('CSV must contain a header')
        return list(reader), reader.fieldnames


def write_csv(path, rows, fields):
    import io
    out = io.StringIO(newline='')
    w = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
    w.writeheader()
    w.writerows(rows)
    atomic_text(path, out.getvalue())


def post_json(url, *, retry=False, accepted_errors=(), **kwargs):
    # Never print exception strings: Smartlead authenticates in its query string.
    attempts = 4 if retry else 1
    for i in range(attempts):
        try:
            r = requests.post(url, timeout=45, **kwargs)
        except requests.RequestException:
            raise RuntimeError('Network failure; remote outcome may be unknown') from None
        if r.status_code == 429 and i + 1 < attempts:
            try:
                delay = min(60, max(1, float(r.headers.get('Retry-After', 2 ** i))))
            except ValueError:
                delay = 2 ** i
            time.sleep(delay)
            continue
        if not 200 <= r.status_code < 300:
            try:
                code = r.json().get('error_code')
            except (ValueError, AttributeError):
                code = None
            if code in accepted_errors:
                return {'error': True, 'error_code': code}
            raise RuntimeError(f'Provider HTTP {r.status_code}')
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            raise RuntimeError('Provider returned invalid JSON') from None


def llm_provider():
    """'openai' (API key) or 'claude' (local `claude` CLI, billed to a Claude subscription).
    LLM_PROVIDER wins; otherwise OpenAI if a key is set, else the claude CLI if installed."""
    import shutil
    p = (os.getenv('LLM_PROVIDER') or '').strip().lower()
    if p in ('openai', 'claude'):
        return p
    if os.getenv('OPENAI_API_KEY'):
        return 'openai'
    if shutil.which('claude'):
        return 'claude'
    return None


def in_session_guard():
    """Inside Claude Code (CLAUDECODE=1) the agent scores in-session with full context. Automatic scoring there
    must be chosen explicitly via LLM_PROVIDER; otherwise llm_provider() would silently pick the claude CLI.
    Returns the refusal message, or None when automatic scoring is fine."""
    if os.getenv('CLAUDECODE') and not (os.getenv('LLM_PROVIDER') or '').strip():
        return ('Inside Claude Code the agent scores in-session: run `src/rank.py --dump --slug <slug>`, judge every '
                'engager, write output/<slug>/scores.json and pass --scores. To let the pipeline score on its own '
                'instead, set LLM_PROVIDER=openai or LLM_PROVIDER=claude in .env.')
    return None


def llm_json(system, user, model=None):
    """Ask the configured LLM for a JSON object. Returns (parsed_dict, input_tokens, output_tokens)."""
    provider = llm_provider()
    if provider == 'openai':
        return _openai_json(system, user, model or os.getenv('OPENAI_MODEL', 'gpt-4.1-mini'))
    if provider == 'claude':
        m = model or os.getenv('CLAUDE_MODEL', 'sonnet')
        try:
            return _claude_cli_json(system, user, m)
        except json.JSONDecodeError:
            # ponytail: CLI has no JSON mode; free text with stray quotes breaks parsing. One retry.
            return _claude_cli_json(system, user + '\n\nYour last reply was not valid JSON. '
                                    'Return ONLY a JSON object; escape or avoid double quotes inside strings.', m)
    raise RuntimeError('No LLM configured: set OPENAI_API_KEY, or install the `claude` CLI '
                       'and log in to your Claude subscription (see README).')


def _openai_json(system, user, model):
    from openai import OpenAI
    resp = OpenAI().chat.completions.create(
        model=model, temperature=0, response_format={'type': 'json_object'},
        messages=[{'role': 'system', 'content': system}, {'role': 'user', 'content': user}])
    return json.loads(resp.choices[0].message.content), resp.usage.prompt_tokens, resp.usage.completion_tokens


def _claude_cli_json(system, user, model):
    import subprocess
    proc = subprocess.run(
        ['claude', '-p', '--model', model, '--output-format', 'json',
         '--tools', '', '--system-prompt', system],
        input=user, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f'claude CLI failed (exit {proc.returncode}): {proc.stderr.strip()[:500]}')
    out = json.loads(proc.stdout)
    if out.get('is_error'):
        raise RuntimeError(f'claude CLI error: {str(out.get("result"))[:500]}')
    text = (out.get('result') or '').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    u = out.get('usage') or {}
    in_tok = u.get('input_tokens', 0) + u.get('cache_read_input_tokens', 0) + u.get('cache_creation_input_tokens', 0)
    return json.loads(text), in_tok, u.get('output_tokens', 0)


def ledger_ok(row):
    """False when the ledger gate marked this row suppressed, already in a campaign, or scored on stale evidence.
    Rows from CSVs that never went through the gate count as eligible."""
    return (row.get('ledger_status') or 'eligible') == 'eligible'


def qualification_ok(row, min_fit=4):
    """Explicit review/rejection holds cannot be bypassed by lowering a numeric threshold."""
    return (fit(row) >= min_fit and str(row.get('needs_review', '')).lower() not in ('true', '1') and
            (row.get('qualification_status') or 'qualified') == 'qualified')
