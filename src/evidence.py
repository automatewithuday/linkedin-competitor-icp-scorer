"""Bounded LinkedIn evidence: current roles only, and canonical company identifiers."""
from urllib.parse import urlsplit


def text(value, limit=2000):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else ''


def company_url(value):
    p = urlsplit(value if isinstance(value, str) and '://' in value else '')
    parts = p.path.strip('/').split('/')
    if (p.hostname == 'linkedin.com' or (p.hostname or '').endswith('.linkedin.com')) and len(parts) == 2 and parts[0] == 'company':
        return 'https://www.linkedin.com/company/' + parts[1].lower()
    return ''


def domain(value):
    if not isinstance(value, str):
        return ''
    p = urlsplit(value if '://' in (value or '') else 'https://' + (value or ''))
    return (p.hostname or '').lower().removeprefix('www.')


def date_text(value):
    return text(value.get('text')) if isinstance(value, dict) else text(value)


def profile_evidence(actor):
    """Current positions only; job location is never substituted for the person's location."""
    jobs = actor.get('currentPosition')
    if not isinstance(jobs, list):
        jobs = [j for j in (actor.get('experience') or []) if isinstance(j, dict) and
                date_text(j.get('endDate')).lower() == 'present']
    roles = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        end = date_text(job.get('endDate'))
        if end and end.lower() != 'present':
            continue
        role = {k: text(job.get(source), 1000 if k == 'description' else 250) for k, source in (
            ('title', 'position'), ('company', 'companyName'), ('company_id', 'companyId'),
            ('start_date', 'startDate'), ('end_date', 'endDate'), ('employment_type', 'employmentType'),
            ('job_location', 'location'), ('description', 'description'))}
        role['start_date'] = date_text(job.get('startDate'))
        role['end_date'] = end
        if isinstance(job.get('companyId'), int):
            role['company_id'] = str(job['companyId'])
        role['company_linkedin_url'] = company_url(job.get('companyLinkedinUrl'))
        role['company_domain'] = domain(job.get('companyWebsite'))
        role = {k: v for k, v in role.items() if v}
        if role and role not in roles:
            roles.append(role)
        if len(roles) == 5:
            break
    about = text(actor.get('about'))
    return {'source': 'linkedin_profile', 'current_roles': roles, 'about': about} if roles or about else {}
