# Set up the qualification core

Read CLAUDE.md and skills/icp-onboarding/SKILL.md.

1. Run `uv sync`. No paid/provider calls occur during setup. APIFY_TOKEN is needed for collection; GETLEADS_API_KEY is optional gap-filling. Create .env from .env.example if needed. Check key names only; never print values or request keys in chat.
2. Use the local icp-onboarding skill. Ask for the website or business description, then one question at a time. Save progress with `uv run python -m src.profiles answer`. An older icp.yaml cannot be loaded; if one exists, propose its contents as inferred answers and confirm each. Confirm mandatory/preferred/excluded modes and alternative segments explicitly. Do not ask the user to re-enter facts already confirmed in this session.
3. Show the completed profile and any inferred proposals. Write the draft locally with `uv run python -m src.profiles draft`. Signals default to priority, not filtering. Do not infer SDR-team size from employees.
4. Ask about optional suppressions and record known customers/teammates/opt-outs with `uv run src/ledger.py suppress`.
5. Continue to /icp-prompt-builder. Explain that two consecutive human-reviewed rounds of 10 companies are needed before bulk qualification, and that the companies come from a bounded `--sample` collection when none are saved yet.
