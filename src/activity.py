"""LinkedIn activity intel: wraps vendored activity_intel.py and joins its verdicts into the ranked CSV."""
import argparse
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from dotenv import load_dotenv
try:
    from .common import qualification_ok, atomic_text, fit, ledger_ok, linkedin_url, read_csv, write_csv
    from .activity_intel import username
except ImportError:
    from common import qualification_ok, atomic_text, fit, ledger_ok, linkedin_url, read_csv, write_csv
    from activity_intel import username

FIELDS = ['linkedin_outreach_ready', 'outreach_ready_probability', 'outreach_reason', 'activity_reason', 'activity_model', 'activity_status',
          'activity_level', 'activity_confidence', 'days_since_last_activity', 'last_activity_type',
          'most_recent_activity_url', 'data_quality_warning', 'activity_miss_reason']
TOOL = Path(__file__).with_name('activity_intel.py')


def eligible(row, min_fit):
    """-> (normalized public URL or '', miss reason or '')."""
    if fit(row) < min_fit:
        return '', 'below_min_fit'
    if not qualification_ok(row, min_fit):
        return '', 'qualification_hold'
    if not ledger_ok(row):
        return '', 'ledger_' + row['ledger_status'].split(':')[0]
    url = linkedin_url(row.get('linkedin_url'))
    return (url, '') if url else ('', 'no_public_url')


def miss_reason(why, row, min_fit):
    if why == 'below_min_fit':
        return f'Activity not checked: ICP fit {fit(row)} is below the required {min_fit}; review the qualification reason.'
    if why == 'qualification_hold':
        return 'Activity not checked: ICP decision is ' + row.get('qualification_status', 'needs_review') + '; resolve the qualification reason first.'
    if why.startswith('ledger_'):
        return 'Activity not checked: ' + (row.get('eligibility_reason') or f'ledger blocked outreach ({row.get("ledger_status")}).')
    if why == 'no_public_url':
        return 'Activity not checked: no usable public LinkedIn profile URL was available.'
    if why == 'no_verdict':
        return 'Activity is unknown: collection or the Jev judgment failed; no conclusion about inactivity can be drawn.'
    return 'Activity not assessed: no result was returned for this person, possibly because the check limit was reached.'


def fallback_reason(hit):
    if hit.get('activity_status') == 'no_activity_observed':
        return 'No public posts, comments or reactions were observed in the sample; recent LinkedIn use is not established.'
    ready = (hit.get('linkedin_outreach_ready') or '').lower()
    probability = hit.get('outreach_ready_probability')
    reason = f'Observed activity status: {hit.get("activity_status") or "unknown"}.'
    if probability not in (None, ''):
        reason += f' Jev LinkedIn outreach readiness: {ready or "unknown"} (probability {probability}).'
    if hit.get('days_since_last_activity') not in (None, ''):
        reason += f' Latest sampled activity: {hit.get("last_activity_type") or "unknown type"}, {hit["days_since_last_activity"]} days ago.'
    return reason


def join(rows, results, min_fit, model=None):
    by_user = {username(r.get('linkedin_url')): r for r in results}
    for row in rows:
        url, why = eligible(row, min_fit)
        hit = by_user.get(username(url)) if url else None
        verdict = dict.fromkeys(FIELDS, '')
        if hit is None:
            verdict['activity_miss_reason'] = why or 'not_in_result'
            verdict['activity_status'] = 'not_checked'
            verdict['activity_reason'] = miss_reason(verdict['activity_miss_reason'], row, min_fit)
        elif not hit.get('activity_status'):
            verdict['activity_miss_reason'] = 'no_verdict'  # fetch or TypeSafe failed; see upstream csv `error`
            verdict['activity_status'] = 'unknown'
            verdict['data_quality_warning'] = 'true'
            verdict['activity_reason'] = miss_reason('no_verdict', row, min_fit)
            for field in ('days_since_last_activity', 'last_activity_type', 'most_recent_activity_url'):
                verdict[field] = hit.get(field) or ''
        else:  # upstream writes Python bools as True/False; this repo uses true/false
            verdict.update({k: hit[k].lower() if hit[k] in ('True', 'False') else hit[k] for k in FIELDS if k in hit})
            verdict['activity_reason'] = hit.get('outreach_reason') or fallback_reason(hit)
            if verdict.get('data_quality_warning') == 'true':
                verdict['activity_reason'] += ' Some activity evidence is missing or uncertain; review the available evidence before outreach.'
            if hit.get('outreach_ready_probability') not in (None, ''):
                verdict['activity_model'] = model or ''
        verdict['outreach_reason'] = verdict['activity_reason']  # retain the original column for existing consumers
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
    model = None
    if urls:  # upstream exits 1 on an empty list; nothing eligible is a normal outcome here
        model = tomllib.loads(Path(args.config).read_text(encoding='utf-8'))['judge']['model']
        # .txt so the *.csv glob only sees upstream output; a dry run writes nothing into the run folder
        listing = Path(tempfile.mkdtemp()) / 'input.txt' if args.dry_run else work / 'input.txt'
        atomic_text(listing, '\n'.join(urls) + '\n')
        cmd = [sys.executable, str(TOOL), str(listing), '--config', args.config, '--out', str(work)]
        cmd += ['--limit', str(args.limit)] if args.limit else []
        cmd += ['--dry-run'] if args.dry_run else []
        cmd += ['--from-raw', args.from_raw] if args.from_raw else []
        env = os.environ | {'APIFY_TOKEN': os.getenv('APIFY_TOKEN') or os.getenv('APIFY_API_KEY') or ''}
        if subprocess.run(cmd, env=env).returncode != 0:
            raise SystemExit('activity_intel failed')
    if args.dry_run:
        return
    results = read_csv(max(work.glob('*.csv'), key=lambda p: p.name))[0] if urls else []
    join(rows, results, args.min_fit, model)
    write_csv(args.output, rows, fields + [x for x in FIELDS if x not in fields])
    print(f'Rows: {len(rows)}; checked: {len(results)}; '
          f'outreach_ready: {sum(r["linkedin_outreach_ready"] == "true" for r in rows)}')


if __name__ == '__main__':
    main()
