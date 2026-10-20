"""Shared validation, atomic artifacts and CLI error handling."""
import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


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


def cli(main, argv=None):
    """Run a command's main. Refusals raised as ValueError are printed as one line, not a traceback."""
    try:
        return main(argv)
    except ValueError as exc:
        print(f'Error: {exc}', file=sys.stderr)
        raise SystemExit(1) from None
