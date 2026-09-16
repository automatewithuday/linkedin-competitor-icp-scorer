# Operating this play

Read README.md for current commands. Providers: Apify, OpenAI, Prospeo, Smartlead; optional Supabase.

Use the user's ICP and target profiles. Never invent an ICP from unrelated prior projects. Keep API credentials in .env and never print them. Proceed with authorized work without repeated permission prompts.

Run `run.py` for a new harvest, or individual stage commands for retries. Review CSV scoring and Smartlead preview before an actual import. `push.py --execute` imports into a campaign and may initiate sending if that campaign is active; only execute when the user has authorized that campaign import. Keep all provider suppression checks enabled.

For recurring sweeps choose a dated slug for each target. Reuse a run ID only for retries. Do not mark contacts as sent merely because they were harvested or exported. Supabase stores observations; Smartlead receipts track import outcomes. Do not infer success from a zero HTTP error count.

Validation: `python -m unittest discover -s tests -v`.
