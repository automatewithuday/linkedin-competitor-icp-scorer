# Set up the qualification core

Read CLAUDE.md and skills/icp-onboarding/SKILL.md.

1. Run uv sync. No paid/provider calls occur during setup. APIFY_TOKEN is needed for collection; GETLEADS_API_KEY is optional gap-filling. Create .env from .env.example if needed. Check key names only; never print values or request keys in chat.
2. Use the local icp-onboarding skill. Ask for the website or business description, then one question at a time. Save progress with src.profiles. If icp.yaml exists, use it only as a draft proposal. Confirm mandatory/preferred/excluded modes and alternative segments explicitly. Do not ask the user to re-enter facts already confirmed in this session.
3. Show the completed profile and any inferred proposals. Write the draft locally. Signals default to priority, not filtering. Do not infer SDR-team size from employees.
4. Ask about optional suppressions and record known customers/teammates/opt-outs in the existing ledger.
5. Continue to /icp-prompt-builder. Explain that two consecutive human-reviewed rounds are needed before bulk qualification. Existing evidence can be used first; a bounded --sample run may collect missing calibration examples when authorized.
