-- Run once in your Supabase SQL editor. No public API access to lead data.
create table if not exists public.warm_lead_snapshots (
  run_id text not null,
  lead_key text not null,
  linkedin_url text,
  data jsonb not null,
  first_stored_at timestamptz not null default now(),
  primary key (run_id, lead_key)
);
create index if not exists warm_lead_identity_idx
  on public.warm_lead_snapshots (lead_key, first_stored_at desc);
alter table public.warm_lead_snapshots enable row level security;
revoke all on public.warm_lead_snapshots from anon, authenticated;
grant select, insert, update on public.warm_lead_snapshots to service_role;
