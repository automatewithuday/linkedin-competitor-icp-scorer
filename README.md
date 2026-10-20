# LinkedIn Competitor ICP Scorer

**Turn a competitor's LinkedIn post engagers into a list qualified against your buyers.**

Define your ICP through a guided conversation. The workflow saves the source data, checks each company first, then checks whether the person is the right buyer at that company. Every decision includes a reason.

```text
Guided ICP interview — one question at a time
                     |
Review 10 companies, refine, review the next 10
          Two approved rounds lock the rules
                     |
Competitor post or personal profile → collect engagers
                     |
        Save complete evidence in SQLite
                     |
 Resolve current employers → enrich each company once
                     |
       Company fit → person fit at that company
                     |
       Qualified / not qualified / needs review
                     |
        All leads CSV + qualified-only CSV
```

## Get started

You need Python 3.11+, [uv](https://docs.astral.sh/uv/), Claude Code and your own [Apify](https://apify.com) token. GetLeads is optional for missing evidence. Scoring in your agent session needs no separate model API key.

```bash
git clone https://github.com/automatewithuday/linkedin-competitor-icp-scorer.git
cd linkedin-competitor-icp-scorer
claude
```

Inside Claude Code:

1. `/setup` — configure keys locally and answer the ICP questions. Confirm which criteria are mandatory, preferred or excluded. Resume whenever needed.
2. `/icp-prompt-builder` — test your rules on batches of 10 companies. Two consecutive human-approved rounds lock an ICP. A correction resets the streak.
3. `/first-run` — provide a competitor post or personal profile. Review the summary and CSVs.

Company pages are enrichment inputs, not engagement-harvest targets. A post link collects only that post.

## What you get

- **Company and person decisions separately**, with the evidence and missing facts that explain each one.
- **`all_leads.csv`** with every engager, including rejected and review-held people.
- **`qualified_leads.csv`** with current, fully qualified and unsuppressed people only.
- **Reusable SQLite evidence.** Repeat engagers and colleagues share saved company enrichment. Changing an ICP requires reassessment, not a fresh scrape.

One installation supports one business with multiple named ICPs. Direct buyers and agency partners can have different criteria. Missing preferred data does not block qualification; missing mandatory evidence does.

Signal definitions are tailored during onboarding. Any existing dated evidence is saved; signals influence priority by default. Active hiring/funding research is a later module.

Email discovery, Jev Activity Intel through TypeSafe, drafts and campaign integrations are preserved as legacy tools and are outside this core release. Core exports cannot be passed into those legacy integrations.

[Commands and data model](docs/OPERATIONS.md) · [Example ICP](examples/client-profile.example.yaml) · [Example export](examples/all_leads.core.sample.csv) · [Live sample validation](docs/VALIDATION.md)
