"""Readable summary of a core export (all_leads.csv or qualified_leads.csv): counts, then every decision with its reasons."""
import argparse
from collections import Counter

try:
    from .common import read_csv
except ImportError:
    from common import read_csv

ORDER = ('qualified', 'needs_review', 'not_qualified')


def summarize(rows, top=20):
    lines = [f'Rows: {len(rows)}']
    if not rows:
        return lines
    if str(rows[0].get('schema_version', '')) != '2':
        raise ValueError('Not a qualification-core export (schema_version 2 expected)')
    counts = Counter(r.get('qualification_status') or 'needs_review' for r in rows)
    lines.append('ICP decisions: ' + ', '.join(f'{k}={counts.get(k, 0)}' for k in ORDER))
    lines.append(f"Eligible: {sum(r.get('eligibility_status') == 'eligible' for r in rows)}")
    for row in rows[:top]:
        who = f"{row.get('name') or '?'}" + (f" — {row['current_title']}" if row.get('current_title') else '') \
              + (f" at {row['company_name']}" if row.get('company_name') else '')
        lines.append(f"{who}: {row.get('qualification_status')} — {row.get('qualification_reason')}")
        lines.append(f"  Company: {row.get('company_decision')} — {row.get('company_reason')}")
        lines.append(f"  Person: {row.get('person_decision')} — {row.get('person_reason')}")
        if row.get('eligibility_status') not in ('eligible', 'hold', '', None):
            lines.append(f"  Eligibility: {row['eligibility_status']}")
    if len(rows) > top:
        lines.append(f'... {len(rows) - top} more rows in the CSV')
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--top', type=int, default=20)
    args = ap.parse_args(argv)
    rows, _ = read_csv(args.input)
    print(f"=== SUMMARY: {args.input} ===")
    print("\n".join(summarize(rows, args.top)))


if __name__ == '__main__':
    try:
        from .common import cli
    except ImportError:
        from common import cli
    cli(main)
