-- Collector environments and the measurements their agents push.
--
-- An environment is one `lfc_` key. The key itself is never stored: only its
-- SHA-256 and a short display prefix, so a database leak cannot be replayed
-- against the ingest endpoint. Agents authenticate with the key alone, so the
-- ingest edge function runs with verify_jwt = false and the service role; the
-- browser can read its own rows but never key_hash, and never writes either
-- table directly.

create table if not exists public.collector_environments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 120),
  key_hash text not null unique check (char_length(key_hash) = 64),
  key_prefix text not null check (char_length(key_prefix) <= 16),
  status text not null default 'active' check (status in ('active', 'revoked')),
  last_seen_at timestamptz,
  last_hostname text check (char_length(last_hostname) <= 255),
  measurement_count bigint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, name)
);

create index if not exists collector_environments_user_idx
  on public.collector_environments (user_id);

-- Measurements are append-only. The unique key makes ingest idempotent: an
-- agent that retries a spooled batch after a lost response re-sends the same
-- (environment, host, collected_at) rows and they are counted as duplicates
-- rather than stored twice.
create table if not exists public.collector_measurements (
  id bigint generated always as identity primary key,
  environment_id uuid not null references public.collector_environments(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  hostname text not null check (char_length(hostname) between 1 and 255),
  schema_version text not null check (char_length(schema_version) <= 64),
  collected_at timestamptz not null,
  received_at timestamptz not null default now(),
  host jsonb not null default '{}'::jsonb,
  metrics jsonb not null default '{}'::jsonb,
  unique (environment_id, hostname, collected_at)
);

create index if not exists collector_measurements_env_idx
  on public.collector_measurements (environment_id, collected_at desc);

create index if not exists collector_measurements_user_idx
  on public.collector_measurements (user_id, received_at desc);

alter table public.collector_environments enable row level security;
alter table public.collector_measurements enable row level security;

drop policy if exists "Users read own collector environments" on public.collector_environments;
create policy "Users read own collector environments"
  on public.collector_environments for select to authenticated
  using (auth.uid() = user_id);

drop policy if exists "Users read own collector measurements" on public.collector_measurements;
create policy "Users read own collector measurements"
  on public.collector_measurements for select to authenticated
  using (auth.uid() = user_id);

-- Column-level grant: key_hash is not selectable by clients.
revoke all on public.collector_environments from anon, authenticated;
grant select (id, user_id, name, key_prefix, status, last_seen_at, last_hostname,
              measurement_count, created_at, updated_at)
  on public.collector_environments to authenticated;

revoke all on public.collector_measurements from anon, authenticated;
grant select on public.collector_measurements to authenticated;

-- Mint an environment and return its key once. The caller is the only one who
-- ever sees the plaintext; nothing stores it. gen_random_uuid() is used rather
-- than pgcrypto's gen_random_bytes so this needs no extension: two UUIDv4
-- values carry well over 200 bits of entropy.
create or replace function public.collector_environment_create(p_name text)
returns table (id uuid, name text, key text, key_prefix text, created_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_key text;
begin
  if auth.uid() is null then
    raise exception 'not authenticated';
  end if;

  v_key := 'lfc_'
    || replace(gen_random_uuid()::text, '-', '')
    || replace(gen_random_uuid()::text, '-', '');

  insert into public.collector_environments (user_id, name, key_hash, key_prefix)
  values (
    auth.uid(),
    p_name,
    encode(sha256(convert_to(v_key, 'utf8')), 'hex'),
    substring(v_key from 1 for 12)
  )
  returning
    collector_environments.id,
    collector_environments.name,
    v_key,
    collector_environments.key_prefix,
    collector_environments.created_at
  into id, name, key, key_prefix, created_at;

  return next;
end;
$$;

-- Called by the ingest function after a batch lands. The counter is bumped in
-- SQL so concurrent agents on one environment cannot lose increments, and
-- last_seen_at only ever moves forward.
create or replace function public.collector_environment_touch(
  p_id uuid,
  p_last_seen timestamptz,
  p_hostname text,
  p_accepted integer
)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.collector_environments
     set last_seen_at = greatest(coalesce(last_seen_at, p_last_seen), p_last_seen),
         last_hostname = p_hostname,
         measurement_count = measurement_count + greatest(coalesce(p_accepted, 0), 0)
   where id = p_id;
$$;

revoke all on function public.collector_environment_touch(uuid, timestamptz, text, integer)
  from public, anon, authenticated;
grant execute on function public.collector_environment_touch(uuid, timestamptz, text, integer)
  to service_role;

create or replace function public.collector_environment_revoke(p_id uuid)
returns void
language sql
security definer
set search_path = ''
as $$
  update public.collector_environments
     set status = 'revoked'
   where id = p_id
     and user_id = auth.uid();
$$;

revoke all on function public.collector_environment_create(text) from public, anon;
revoke all on function public.collector_environment_revoke(uuid) from public, anon;
grant execute on function public.collector_environment_create(text) to authenticated;
grant execute on function public.collector_environment_revoke(uuid) to authenticated;

create or replace function public.touch_collector_environments()
returns trigger language plpgsql set search_path = '' as $$
begin new.updated_at = now(); return new; end;
$$;

drop trigger if exists collector_environments_touch on public.collector_environments;
create trigger collector_environments_touch
  before update on public.collector_environments
  for each row execute function public.touch_collector_environments();
