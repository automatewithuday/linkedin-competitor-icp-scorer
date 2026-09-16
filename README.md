# LinkedIn Competitor ICP Scorer

**Turn the people engaging with your competitors' LinkedIn posts into a ranked list of warm leads that match your ideal customer.**

When a competitor's founder posts on LinkedIn, the people who like and comment are telling you something: they care about the problem you solve. This tool collects those people, scores each one against your ideal customer profile (ICP) with an LLM, and gives you a spreadsheet sorted by who is most worth reaching out to. Optionally it finds their work email and stages them for a cold email campaign.

## How it works

```
 Competitor's LinkedIn profile
            │
            ▼
 1. Harvest      Pull their recent posts + everyone who reacted or commented   (Apify)
            │
            ▼
 2. Load ICP     Read who you sell to from icp.yaml                             (you)
            │
            ▼
 3. Score        Rate every engager 1–5 on ICP fit + read comments for intent   (OpenAI or Claude)
            │
            ▼
 4. Rank         Sort by fit × how often they engaged × buying intent           ──▶  ranked_engagers.csv
            │
            ▼  (optional)
 5. Draft        Connection note + DM per qualified lead, one playbook template each ──▶  drafts.csv
 6. Enrich       Find verified work emails                                      (Prospeo)
 7. Push         Stage a Smartlead campaign import; you approve before it sends (Smartlead)
```

Steps 1 to 4 are the core. Steps 5 and 6 are opt-in flags.

## What you get

A CSV with one row per engager, best leads first. The columns that matter:

| column | meaning |
|---|---|
| `icp_fit` | 1 to 5. How well their headline matches your ICP. 4 and 5 are qualified. |
| `intent` | `high`, `medium`, or `low`. From their comment if they left one; reactions alone are always `low`. |
| `engagement_count` | How many of the competitor's posts they touched. Repeat engagers rank higher. |
| `rank_score` | The sort key. Fit, engagement, and intent combined. |
| `comment_excerpt` | What they said, so your first message can reference it. |
| `linkedin_url`, `company_guess`, `clean_title` | Who they are. |

See [examples/ranked_engagers.sample.csv](examples/ranked_engagers.sample.csv) for a full sample.

## Quickstart

You need Python 3.11+, an [Apify](https://apify.com) token, and one LLM for scoring: either an OpenAI API key, or a Claude subscription through the [Claude Code](https://claude.com/claude-code) CLI (no API key needed, just run `claude` once to log in).

```bash
git clone https://github.com/automatewithuday/linkedin-competitor-icp-scorer.git
cd linkedin-competitor-icp-scorer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add APIFY_TOKEN, and OPENAI_API_KEY unless you use Claude
python setup_icp.py         # give it your website, confirm the proposed ICP -> writes icp.yaml
```

Then run it against one competitor, small and cheap:

```bash
python run.py \
  --profile https://www.linkedin.com/in/COMPETITOR-FOUNDER \
  --slug first-test \
  --icp icp.yaml \
  --max-posts 3 --max-reactions 20 --max-comments 10
```

Open `output/first-test/ranked_engagers.csv`. That run costs well under a dollar of Apify credit and finishes in a few minutes.

## Defining your ICP (the part most people get stuck on)

`setup_icp.py` does not expect you to know your ICP up front. Give it your website and it proposes one (titles, industries, size, countries, customers named on the site) that you confirm or correct in nine short questions. It then expands every job title into the synonyms that actually show up in LinkedIn headlines, because "VP Sales" alone would miss "Head of Sales". Works without Firecrawl; a plain page fetch is used when no key is set. If you have no website, answer from scratch.

Two things it will push you on: name one to three real best customers, and give headcount as numbers, not "SMB". Buying triggers are recorded but never used to reject anyone.

## What to expect

Honest numbers from testing this on three outbound-agency founders' profiles, 3 posts each: 136 engagers scored, 2 qualified. The scoring was right. The audiences were wrong.

**The target profile matters more than anything else.** People who engage with a vendor's posts are mostly other vendors, consultants, and peers. To find buyers, pick profiles your buyers actually follow: a product founder in your category, a well-known voice with your buyer's job title, an analyst. The scorer is deliberately stingy and will tell you quickly whether a profile's audience is worth mining.

Once you find a good profile, scale up: `--max-posts 10` with default caps gives roughly four times the sample.

## Going further

- **Get emails and push to a campaign**: add `--find-emails` and `--campaign-id`. Nothing sends without an explicit `--execute` on a separate command. Details in [docs/OPERATIONS.md](docs/OPERATIONS.md).
- **Tune the scoring**: your ICP lives in `icp.yaml` (titles, industries, exclusions, disqualifiers, triggers). Rescore a saved harvest without paying Apify again:
  ```bash
  python src/icp.py --slug first-test --icp icp.yaml
  python src/rank.py --slug first-test
  ```
- **Write the outreach**: add `--draft` (or run `python src/draft.py --slug first-test`) to get `drafts.csv` with a LinkedIn connection note and a DM for every lead at fit 4+. Each row is written strictly inside one of the three openers in [playbooks/copy-templates.md](playbooks/copy-templates.md), picked by the fit/intent/comment table there. `--min-fit 3` also drafts the Integration-Partner note for adjacent vendors. Nothing is sent; read and edit before use. The playbooks folder also has a positioning play and a battle-card prompt for writing by hand.
- **Run weekly**: list profiles in `targets.txt` and use the `/weekly-sweep` command in Claude Code.
- **Store history**: optional Supabase export keeps every run's scores.

## Costs

| what | cost |
|---|---|
| Apify harvest | about $0.002 per item. 3 posts with small caps is under $0.20; 10 posts at default caps is $2 to $3. |
| Scoring with OpenAI | under one cent per 100 engagers on `gpt-4.1-mini`. |
| Scoring with Claude subscription | no dollars; counts against your plan's usage limits. |
| Prospeo emails | per lookup, capped at 100 per run by default. |

## Repo map

```
run.py            one command that runs the whole pipeline
setup_icp.py      interactive ICP intake -> icp.yaml
src/harvest.py    Apify: posts + engagers
src/icp.py        load icp.yaml (or infer an ICP from a website with --domain)
src/rank.py       LLM scoring + ranking; the scoring prompt lives here
src/email_enrich.py   Prospeo
src/push.py       Smartlead preview / import
src/common.py     shared helpers, including the OpenAI-or-Claude switch
docs/OPERATIONS.md    full command reference and every caveat
docs/SCHEMAS.md   Apify actor input/output shapes
playbooks/        outreach copy and positioning
tests/            mocked, no API spend: python -m unittest discover -s tests
```

## Credits

Adapted from [LeadGrowGTM/linkedin-warm-leads](https://github.com/LeadGrowGTM/linkedin-warm-leads), MIT licensed. This version calls provider APIs directly and does not require Deepline.
