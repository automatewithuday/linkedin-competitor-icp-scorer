# LinkedIn Competitor ICP Scorer

**Turn the people engaging with a competitor's LinkedIn posts into an outreach shortlist scored against your ideal customer profile.**

Give Claude Code a competitor founder's profile or a specific post. It collects the people who reacted or commented, checks their fit against the buyers you want, and returns a ranked CSV with a summary in chat.

## How it works

1. **Define your buyers.** Share your website, target roles, industries, company size and countries.
2. **Paste a LinkedIn link.** Use a personal profile or a specific post. Company pages are not supported.
3. **Review your shortlist.** See who matches, what they said, and who is worth researching for outreach.

## Get started with Claude Code

You need [Claude Code](https://claude.com/claude-code), Python 3.11+, [uv](https://docs.astral.sh/uv/) and an [Apify](https://apify.com) API token. Apify charges apply to data collection; the first-run workflow shows the estimated spend before running. Scoring in your Claude Code session needs no separate OpenAI API key.

Clone the repo and open Claude Code:

```bash
git clone https://github.com/automatewithuday/linkedin-competitor-icp-scorer.git
cd linkedin-competitor-icp-scorer
claude
```

Then run these commands **inside Claude Code**, one at a time:

```text
/setup
```

This installs dependencies, helps you add your API token locally to `.env`, and defines your ideal customer profile.

```text
/first-run
```

Paste a competitor founder's LinkedIn profile or a post link when asked. Claude starts with a small sample, scores the engagers and shows the results in chat.

Accepted links include `linkedin.com/in/...`, `linkedin.com/posts/...`, `linkedin.com/feed/update/...` and `lnkd.in` links to posts.

## What you get

- **People and profile links**, with their title, employer and location when available.
- **An ICP fit score from 1–5**, with scores of 4–5 meeting the default qualification threshold.
- **Comment-based intent**, with the supporting comment and post link. Reactions alone count as low intent.
- **A ranked CSV** at `output/<run>/ranked_engagers.csv`, plus a readable summary in chat.

Fit is an estimate based on available evidence. Review shortlisted people before outreach. For better results, choose profiles your buyers actually follow; some competitor audiences contain mostly peers and other vendors.

[See an example CSV](examples/ranked_engagers.sample.csv).

## Optional next steps

Draft connection notes and DMs, check recent LinkedIn activity, find verified work emails, or preview an import into HeyReach or Smartlead. These integrations may need additional API keys and credits. Campaign imports require a separate `--execute` command.

For repeat runs, the local database remembers people, reuses completed lookups and unchanged scores, and checks recorded campaign history and your do-not-contact list.

## More details

- [Commands, integrations, costs and upgrade instructions](docs/OPERATIONS.md)
- [Outreach playbooks](playbooks/copy-templates.md)

Prefer running commands directly in your terminal? The [operations guide](docs/OPERATIONS.md) covers that workflow too.
