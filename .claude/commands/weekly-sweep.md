# Weekly warm-lead sweep

Read README.md and targets.txt. For each nonblank, noncomment target URL run the documented pipeline with the user's ICP and a distinct target/date slug. Start with 10 posts and a maximum 100 qualified Prospeo lookups per target unless the user has specified other limits.

CSV is always produced. Add --supabase only when the user has configured the database and requested it. If a Smartlead campaign is supplied, generate a preview. Execute the import only when the user has authorized that campaign import; an active campaign may send emails.

Report per-target errors and artifact paths. Reuse completed Prospeo checkpoints when recovering an enrichment stage. Do not rerun paid harvests merely to retry a destination export. Do not mark harvested leads as contacted. Smartlead duplicate/block/unsubscribe checks must remain enabled. This command does not install a recurring scheduler.
