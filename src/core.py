"""Qualification core: collect -> preserve -> qualify companies -> qualify people -> export."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import company_stage, coredb, profiles, qualify, profile_stage
from .common import write_json

SAMPLE = {'max_usd': 0.1, 'max_posts': 1, 'max_reactions': 10, 'max_comments': 0, 'lookups': 10}


def harvest_plan(a):
    """The harvest command run.py would launch, and a one-line description of its limits."""
    sample = a.sample
    limits = {'max_usd': min(SAMPLE['max_usd'], a.harvest_max_usd) if sample else a.harvest_max_usd,
              'max_posts': SAMPLE['max_posts'] if sample else a.max_posts,
              'max_reactions': min(SAMPLE['max_reactions'], a.max_reactions) if sample else a.max_reactions,
              'max_comments': SAMPLE['max_comments'] if sample else a.max_comments}
    command = [sys.executable, '-m', 'src.harvest', '--slug', a.slug, '--output-dir', a.output_dir,
               '--post' if a.post else '--profile', a.post or a.profile, '--max-usd', str(limits['max_usd']),
               '--max-posts', str(limits['max_posts']), '--max-reactions', str(limits['max_reactions']),
               '--max-comments', str(limits['max_comments']), '--posted-limit', a.posted_limit]
    if a.retry_unknown:
        command.append('--retry-unknown')
    kind = 'one post' if a.post else f"recent posts (up to {limits['max_posts']})"
    text = (f"Harvest: {kind} from {a.post or a.profile}; up to {limits['max_reactions']} reactions and "
            f"{limits['max_comments']} comments per post; hard cap ${limits['max_usd']:.2f}"
            + (' (sample limits)' if sample else ''))
    return command, text


def main(argv=None):
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--slug', required=True); ap.add_argument('--icp', help='profiles/<name>/client-profile.yaml')
    source = ap.add_mutually_exclusive_group(); source.add_argument('--post'); source.add_argument('--profile'); source.add_argument('--skip-harvest', action='store_true')
    ap.add_argument('--output-dir', default='./output'); ap.add_argument('--scores'); ap.add_argument('--dump', action='store_true')
    ap.add_argument('--sample', action='store_true', help='Collect at most 10 engagers/companies for provisional calibration evidence')
    ap.add_argument('--dry-run', action='store_true', help='Preview requests and caps without provider or model calls')
    ap.add_argument('--no-resolve', action='store_true'); ap.add_argument('--profile-max-lookups', type=int, default=10)
    ap.add_argument('--profile-max-usd', type=float, default=0.25)
    ap.add_argument('--no-enrich', action='store_true')
    ap.add_argument('--company-max-lookups', type=int, default=10); ap.add_argument('--company-max-usd', type=float, default=0.25)
    ap.add_argument('--retry-unknown', action='store_true', help='Only after checking the provider; a retry may incur charges')
    ap.add_argument('--getleads', action='store_true', help='Fill required company gaps with GetLeads plan credits')
    ap.add_argument('--max-lookups', '--getleads-max-lookups', type=int, default=100)
    ap.add_argument('--harvest-max-usd', type=float, default=1.0)
    ap.add_argument('--max-posts', type=int, default=3); ap.add_argument('--max-reactions', type=int, default=20); ap.add_argument('--max-comments', type=int, default=10)
    ap.add_argument('--posted-limit', default='month'); ap.add_argument('--migrate', action='store_true', help='Import saved output/<slug> files into SQLite offline')
    ap.add_argument('--import-signals', help='JSON list of dated signal evidence to attach')
    a = ap.parse_args(argv); profiles.slug(a.slug)
    if min(a.company_max_lookups, a.max_lookups, a.max_posts, a.max_reactions, a.max_comments, a.profile_max_lookups) < 0 or \
            min(a.company_max_usd, a.profile_max_usd, a.harvest_max_usd) < 0:
        ap.error('Lookup counts, harvest limits and budgets must be nonnegative')
    base = Path(a.output_dir) / a.slug; con = coredb.connect()
    os.environ['CORE_RUN_SLUG'] = a.slug
    if a.import_signals:
        path = Path(a.import_signals)
        if not path.is_file():
            raise ValueError(f'Signals file not found: {path}')
        coredb.import_signals(con, json.loads(path.read_text()))
    if a.migrate:
        coredb.import_saved(con, base)
        print('Saved evidence imported offline. Original files retained.'); return
    if not a.icp: ap.error('--icp is required')
    if not Path(a.icp).is_file():
        raise ValueError(f'ICP profile not found: {a.icp}. Run the interview first (see /setup).')
    v = profiles.load(con, a.icp); v = qualify.version(con, v['id'])
    if not a.skip_harvest and (a.post or a.profile):
        if v['state'] != 'locked' and not a.sample:
            raise ValueError('ICP is not calibrated. Use --sample for a provisional batch, or --skip-harvest to reuse saved evidence.')
        command, text = harvest_plan(a)
        print(text + ('. No calls made.' if a.dry_run else ''))
        if not a.dry_run:
            subprocess.run(command, check=True, env=os.environ | {'CORE_RUN_SLUG': a.slug})
    if not (base / 'engagers.json').exists():
        if a.dry_run:
            print(f"Profile lookups: submission cap {min(SAMPLE['lookups'], a.profile_max_lookups) if a.sample else a.profile_max_lookups}; "
                  f"total hard cap ${a.profile_max_usd:.2f}. Company lookups: submission cap "
                  f"{min(SAMPLE['lookups'], a.company_max_lookups) if a.sample else a.company_max_lookups}; total hard cap ${a.company_max_usd:.2f}. "
                  'Candidates are counted once engagers are saved. No calls made.'); return
        raise ValueError(f'No saved engagers for slug {a.slug!r} under {base.parent}. Supply --post/--profile to collect, '
                         'or --skip-harvest with an existing slug.')
    coredb.import_saved(con, base)
    con.execute('UPDATE core_runs SET profile_version=? WHERE slug=?', (v['id'], a.slug)); con.commit()
    if not a.no_resolve:
        profile_stage.enrich_profiles(con, a.slug, min(SAMPLE['lookups'], a.profile_max_lookups) if a.sample else a.profile_max_lookups,
                                      a.profile_max_usd, a.dry_run, a.retry_unknown)
    if not a.no_enrich:
        company_stage.enrich_companies(con, v, a.slug, min(SAMPLE['lookups'], a.company_max_lookups) if a.sample else a.company_max_lookups,
                                       a.company_max_usd, a.dry_run, a.retry_unknown)
    if a.getleads: company_stage.getleads_gaps(con, v, a.slug, a.max_lookups, a.dry_run)
    if a.dry_run: return
    if a.scores and not Path(a.scores).is_file():
        raise ValueError(f'Scores file not found: {a.scores}')
    submitted = json.loads(Path(a.scores).read_text())['results'] if a.scores else []
    packets, pending = qualify.run_assessments(con, v['id'], a.slug, submitted)
    write_json(base / 'core-run.json', {'schema_version': 2, 'profile_version': v['id'], 'icp_path': str(Path(a.icp).resolve()), 'state': v['state']})
    write_json(base / 'assessment-packet.json', {'schema_version': 2, 'profile_version': v['id'], 'prompt': v['prompt'], 'icp': v['doc'], 'packets': pending})
    if a.dump: print(json.dumps({'prompt': v['prompt'], 'icp': v['doc'], 'packets': packets}, indent=2))
    qualify.export(con, v['id'], a.slug, base)
    if v['state'] != 'locked': print('Provisional only: this ICP has not completed two human-approved calibration rounds.')
    if pending: print(f'{len(pending)} semantic assessments pending. Review assessment-packet.json; use a subagent in-session, not a separate model API.')


if __name__ == '__main__':
    from .common import cli
    cli(main)
