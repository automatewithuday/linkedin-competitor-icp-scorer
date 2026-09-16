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
