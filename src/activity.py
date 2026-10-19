"""LinkedIn activity intel: wraps vendored activity_intel.py and joins its verdicts into the ranked CSV."""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
try:
    from .common import atomic_text, fit, linkedin_url, read_csv, write_csv
    from .activity_intel import username
except ImportError:
    from common import atomic_text, fit, linkedin_url, read_csv, write_csv
    from activity_intel import username

FIELDS = ['linkedin_outreach_ready', 'outreach_ready_probability', 'outreach_reason', 'activity_status',
          'activity_level', 'activity_confidence', 'days_since_last_activity', 'last_activity_type',
          'most_recent_activity_url', 'data_quality_warning', 'activity_miss_reason']
TOOL = Path(__file__).with_name('activity_intel.py')


def eligible(row, min_fit):
    """-> (normalized public URL or '', miss reason or '')."""
    if fit(row) < min_fit:
        return '', 'below_min_fit'
    url = linkedin_url(row.get('linkedin_url'))
    return (url, '') if url else ('', 'no_public_url')


def join(rows, results, min_fit):
    by_user = {username(r.get('linkedin_url')): r for r in results}
    for row in rows:
        url, why = eligible(row, min_fit)
        hit = by_user.get(username(url)) if url else None
        verdict = dict.fromkeys(FIELDS, '')
        if hit is None:
            verdict['activity_miss_reason'] = why or 'not_in_result'
        elif not hit.get('activity_status'):
            verdict['activity_miss_reason'] = 'no_verdict'  # fetch or TypeSafe failed; see upstream csv `error`
        else:  # upstream writes Python bools as True/False; this repo uses true/false
            verdict.update({k: hit[k].lower() if hit[k] in ('True', 'False') else hit[k] for k in FIELDS if k in hit})
        row.update(verdict)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--output', required=True)
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    ap.add_argument('--config', default='activity.toml')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--dry-run', action='store_true', help='print profile count + Apify estimate and hard cap; spend nothing')
    ap.add_argument('--from-raw', help='re-judge a cached run without Apify: <output stem>/raw/<run>')
    args = ap.parse_args()
    rows, fields = read_csv(args.input)
    work = Path(args.output).with_suffix('')  # output/<slug>/activity/
    urls = list(dict.fromkeys(u for u, _ in (eligible(r, args.min_fit) for r in rows) if u))
    print(f'Rows: {len(rows)}; eligible profiles: {len(urls)}', file=sys.stderr)
    if urls:  # upstream exits 1 on an empty list; nothing eligible is a normal outcome here
        atomic_text(work / 'input.txt', '\n'.join(urls) + '\n')  # .txt so the *.csv glob only sees upstream output
        cmd = [sys.executable, str(TOOL), str(work / 'input.txt'), '--config', args.config, '--out', str(work)]
        cmd += ['--limit', str(args.limit)] if args.limit else []
        cmd += ['--dry-run'] if args.dry_run else []
        cmd += ['--from-raw', args.from_raw] if args.from_raw else []
        env = os.environ | {'APIFY_TOKEN': os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY') or ''}
        if subprocess.run(cmd, env=env).returncode != 0:
            raise SystemExit('activity_intel failed')
    if args.dry_run:
        return
    results = read_csv(max(work.glob('*.csv'), key=lambda p: p.name))[0] if urls else []
    join(rows, results, args.min_fit)
    write_csv(args.output, rows, fields + [x for x in FIELDS if x not in fields])
    print(f'Rows: {len(rows)}; checked: {len(results)}; '
          f'outreach_ready: {sum(r["linkedin_outreach_ready"] == "true" for r in rows)}')


if __name__ == '__main__':
    main()
