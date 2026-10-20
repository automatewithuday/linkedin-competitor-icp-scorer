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


def main(argv=None):
    load_dotenv()
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--slug',required=True);ap.add_argument('--icp');ap.add_argument('--domain')
    source=ap.add_mutually_exclusive_group();source.add_argument('--post');source.add_argument('--profile');source.add_argument('--skip-harvest',action='store_true')
    ap.add_argument('--output-dir',default='./output');ap.add_argument('--scores');ap.add_argument('--dump',action='store_true')
    ap.add_argument('--sample',action='store_true',help='Collect at most 10 engagers/companies for provisional calibration evidence')
    ap.add_argument('--dry-run',action='store_true',help='Preview requests without provider or model calls')
    ap.add_argument('--no-resolve',action='store_true');ap.add_argument('--profile-max-lookups',type=int,default=10)
    ap.add_argument('--profile-max-usd',type=float,default=0.25)
    ap.add_argument('--no-enrich',action='store_true');ap.add_argument('--enrich',action='store_true',help='Company enrichment is already enabled by default')
    ap.add_argument('--company-max-lookups',type=int,default=10);ap.add_argument('--company-max-usd',type=float,default=0.25)
    ap.add_argument('--retry-unknown',action='store_true',help='Only after checking the provider; a retry may incur charges')
    ap.add_argument('--getleads',action='store_true');ap.add_argument('--max-lookups','--getleads-max-lookups',type=int,default=100)
    ap.add_argument('--harvest-max-usd',type=float,default=1.0)
    ap.add_argument('--max-posts',type=int,default=3);ap.add_argument('--max-reactions',type=int,default=20);ap.add_argument('--max-comments',type=int,default=10)
    ap.add_argument('--posted-limit',default='month');ap.add_argument('--migrate',action='store_true');ap.add_argument('--import-signals')
    for flag in ('find-emails','activity','draft','supabase'):
        ap.add_argument('--'+flag,action='store_true',help='Outside the qualification-core release')
    ap.add_argument('--campaign-id');ap.add_argument('--heyreach-list-id');ap.add_argument('--min-fit')
    a=ap.parse_args(argv);profiles.slug(a.slug)
    if any((a.find_emails,a.activity,a.draft,a.supabase,a.campaign_id,a.heyreach_list_id,a.min_fit)):
        ap.error('This release qualifies and exports only. Outreach integrations and numeric fit gates are not core inputs.')
    if a.domain:ap.error('Website inference is an onboarding proposal. Run setup_icp.py, confirm criteria and calibrate the ICP first.')
    if min(a.company_max_lookups,a.max_lookups,a.max_posts,a.max_reactions,a.max_comments,a.profile_max_lookups)<0 or min(a.company_max_usd,a.profile_max_usd,a.harvest_max_usd)<0:
        ap.error('Lookup counts, harvest limits and budgets must be nonnegative')
    base=Path(a.output_dir)/a.slug;con=coredb.connect()
    os.environ['CORE_RUN_SLUG']=a.slug
    if a.import_signals:coredb.import_signals(con,json.loads(Path(a.import_signals).read_text()))
    if a.migrate:
        coredb.import_saved(con,base)
        print('Saved evidence imported offline. Original files and legacy history retained.');return
    if not a.icp:ap.error('--icp is required')
    v=profiles.load(con,a.icp);v=qualify.version(con,v['id'])
    if not a.skip_harvest and (a.post or a.profile):
        if v['state']!='locked' and not a.sample:
            ap.error('ICP is not calibrated. Use --sample for a provisional batch, or --skip-harvest to reuse saved evidence.')
        if not a.dry_run:
            command=[sys.executable,'-m','src.harvest','--slug',a.slug,'--output-dir',a.output_dir,
                     '--post' if a.post else '--profile',a.post or a.profile,'--max-usd',str(min(0.1,a.harvest_max_usd) if a.sample else a.harvest_max_usd),
                     '--max-posts',str(1 if a.sample else a.max_posts),
                     '--max-reactions',str(min(10,a.max_reactions) if a.sample else a.max_reactions),
                     '--max-comments',str(0 if a.sample else a.max_comments),'--posted-limit',a.posted_limit]
            if a.retry_unknown: command.append('--retry-unknown')
            subprocess.run(command,check=True,env=os.environ|{'CORE_RUN_SLUG':a.slug})
    if not (base/'engagers.json').exists():
        if a.dry_run:print('Harvest would run; no saved people to preview yet. No calls made.');return
        ap.error('Supply --post/--profile, or reuse a saved run with --skip-harvest')
    coredb.import_saved(con,base)
    con.execute('UPDATE core_runs SET profile_version=? WHERE slug=?',(v['id'],a.slug));con.commit()
    if not a.no_resolve:
        profile_stage.enrich_profiles(con,a.slug,min(10,a.profile_max_lookups) if a.sample else a.profile_max_lookups,
                                     a.profile_max_usd,a.dry_run,a.retry_unknown)
    if not a.no_enrich:
        company_stage.enrich_companies(con,v,a.slug,min(10,a.company_max_lookups) if a.sample else a.company_max_lookups,
                                      a.company_max_usd,a.dry_run,a.retry_unknown)
    if a.getleads:company_stage.getleads_gaps(con,v,a.slug,a.max_lookups,a.dry_run)
    if a.dry_run:return
    submitted=json.loads(Path(a.scores).read_text())['results'] if a.scores else []
    packets,pending=qualify.run_assessments(con,v['id'],a.slug,submitted)
    write_json(base/'core-run.json',{'schema_version':2,'profile_version':v['id'],'icp_path':str(Path(a.icp).resolve()),'state':v['state']})
    write_json(base/'assessment-packet.json',{'schema_version':2,'profile_version':v['id'],'prompt':v['prompt'],'icp':v['doc'],'packets':pending})
    if a.dump:print(json.dumps({'prompt':v['prompt'],'icp':v['doc'],'packets':packets},indent=2))
    qualify.export(con,v['id'],a.slug,base)
    if v['state']!='locked':print('Provisional only: this ICP has not completed two human-approved calibration rounds.')
    if pending:print(f'{len(pending)} semantic assessments pending. Review assessment-packet.json; use a subagent in-session, not a separate model API.')

if __name__=='__main__':main()
