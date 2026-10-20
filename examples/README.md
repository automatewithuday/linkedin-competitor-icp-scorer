# Examples

[All leads](all_leads.core.sample.csv) · [Qualified only](qualified_leads.core.sample.csv) · [Profile template](client-profile.example.yaml)

All example people, companies, observations and human feedback are fictional. The CSVs were generated through the tested core engine without provider calls. They illustrate a structured example ICP requiring Software industry and a Sales role, with employee count preferred. One lead qualifies, one fails company fit, one fails person fit, and one needs company evidence. Company/person reasons, priority, version and evidence references are included; the columns are documented in [docs/SCHEMAS.md](../docs/SCHEMAS.md). The profile template separately illustrates semantic criteria and alternative buyer/partner segments; adapt it through onboarding rather than treating it as your approved ICP.

Two-round human feedback was simulated only in a temporary software-test database. It does not approve a real ICP. Use a separate LEDGER_DB for demonstrations.

There is no offline sample harvest: calibration needs 10 real companies with saved evidence, which come from a bounded `--sample` collection (about $0.10 plus enrichment) on a post of your choice.
