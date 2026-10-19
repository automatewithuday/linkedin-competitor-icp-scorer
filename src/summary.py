"""Print a readable summary of a pipeline CSV: counts, breakdowns and the top rows by name. No API calls."""
import argparse
from collections import Counter

try:
    from .common import fit, linkedin_url, read_csv
except ImportError:
    from common import fit, linkedin_url, read_csv


def summarize(rows, top=20, min_fit=4):
    lines = []
    n = len(rows)
    lines.append(f"Rows: {n}")
    if not rows:
        return lines
    cols = set(rows[0])
    if 'icp_fit' in cols:
        dist = Counter(fit(r) for r in rows)
        lines.append("ICP fit: " + ", ".join(f"{k}={dist[k]}" for k in sorted(dist, reverse=True)))
        qual = [r for r in rows if fit(r) >= min_fit]
        public = sum(bool(linkedin_url(r.get('linkedin_url'))) for r in qual)
        lines.append(f"Qualified (fit>={min_fit}): {len(qual)}; with public profile URL: {public}; "
                     f"high-intent comments: {sum(r.get('intent') == 'high' for r in rows)}")
    if 'resolve_status' in cols:
        lines.append(f"Resolve (opaque ids -> public URLs): {dict(Counter(r['resolve_status'] for r in rows))}")
    if 'activity_status' in cols:
        checked = [r for r in rows if not r.get('activity_miss_reason')]
        miss = Counter(r['activity_miss_reason'] for r in rows if r.get('activity_miss_reason'))
        lines.append(f"Activity: checked {len(checked)}, outreach-ready {sum(r.get('linkedin_outreach_ready') == 'true' for r in checked)}, "
                     f"status {dict(Counter(r['activity_status'] for r in checked))}, flagged {sum(r.get('data_quality_warning') == 'true' for r in checked)}; "
                     f"not checked: {dict(miss)}")
    if 'email_status' in cols:
        lines.append(f"Email: sendable {sum(r.get('sendable') == 'true' for r in rows)}; "
                     f"status {dict(Counter(r.get('email_status') or '' for r in rows))}; "
                     f"miss reasons {dict(Counter(r['miss_reason'] for r in rows if r.get('miss_reason')))}")
    if 'rank' in cols:  # rank.py order: qualified first, then rank_score
        ranked = sorted(rows, key=lambda r: int(r.get('rank') or 10**9))
    elif 'rank_score' in cols:
        ranked = sorted(rows, key=lambda r: (fit(r) >= min_fit, float(r.get('rank_score') or 0)), reverse=True)
    else:
        ranked = rows
    if 'icp_fit' in cols and 'intent' in cols:
        strong = [r for r in rows if fit(r) < min_fit and r.get('intent') in ('high', 'medium') and r.get('comment_excerpt')]
        if strong:
            lines.append(f"Strong comments below the fit bar (referral/intro value, not outreach slots): "
                         + ", ".join(f"{r.get('name') or '?'} ({r.get('intent')})" for r in strong))
    show = ranked[:top]
    head = f"\nTop {len(show)} (qualified first, then rank score):" if 'icp_fit' in cols else f"\nFirst {len(show)} rows:"
    lines.append(head)
    hdr = f"{'#':>3} {'name':<24} {'title':<34} {'company':<18} {'fit':>3} {'intent':<6}"
    if 'activity_status' in cols:
        hdr += f" {'li-prob':>7} {'li-last':<9}"
    if 'email_status' in cols:
        hdr += f" {'email':<11}"
    lines.append(hdr)
    for i, r in enumerate(show, 1):
        line = (f"{i:>3} {(r.get('name') or '')[:24]:<24} {(r.get('clean_title') or '')[:34]:<34} "
                f"{(r.get('company_guess') or '')[:18]:<18} {r.get('icp_fit', ''):>3} {(r.get('intent') or '')[:6]:<6}")
        if 'activity_status' in cols:
            last = r.get('last_activity_type') or r.get('activity_miss_reason') or ''
            line += f" {(r.get('outreach_ready_probability') or '-'):>7} {last[:9]:<9}"
        if 'email_status' in cols:
            line += f" {(r.get('email_status') or '')[:11]:<11}"
        lines.append(line)
    if 'icp_fit' in cols and len(qual) > len([r for r in show if fit(r) >= min_fit]):
        rest = [r for r in ranked[top:] if fit(r) >= min_fit]
        lines.append(f"\n{len(rest)} more qualified not shown: " + ", ".join((r.get('name') or '?') for r in rest))
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input', required=True)
    ap.add_argument('--top', type=int, default=20)
    ap.add_argument('--min-fit', type=int, choices=range(1, 6), default=4)
    args = ap.parse_args()
    rows, _ = read_csv(args.input)
    print(f"=== SUMMARY: {args.input} ===")
    print("\n".join(summarize(rows, args.top, args.min_fit)))


if __name__ == '__main__':
    main()
