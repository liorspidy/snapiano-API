-- Roles, plans, quotas and access codes.
-- Everything a regular user is limited by is enforced here, in the database,
-- so changing the app can't bypass it. Admin can do everything.

-- ---------- tables ----------

create table public.plans (
  id               text primary key,             -- 'free', 'pro'
  name             text not null,
  scans_per_month  int,                          -- null = unlimited
  max_pieces       int,                          -- null = unlimited
  features         jsonb not null default '{}'::jsonb,
  updated_at       timestamptz not null default now()
);

insert into public.plans (id, name, scans_per_month, max_pieces, features) values
  ('free', 'חינם', 3, 5,
   '{"playback":true,"keyboard":true,"listen_mic":false,"midi":false,"wait_mode":false,"loop":false}'),
  ('pro', 'פרו', null, null,
   '{"playback":true,"keyboard":true,"listen_mic":true,"midi":true,"wait_mode":true,"loop":true}');

create table public.profiles (
  id              uuid primary key references auth.users on delete cascade,
  email           text,
  display_name    text,
  role            text not null default 'user' check (role in ('user', 'admin')),
  plan_id         text not null default 'free' references public.plans,
  plan_until      timestamptz,                   -- null = no expiry; after it the user is back on free
  plan_source     text not null default 'default' check (plan_source in ('default', 'admin', 'code', 'store')),
  scans_reset_at  timestamptz,                   -- admin "reset this month's counter"
  created_at      timestamptz not null default now()
);

create table public.pieces (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references auth.users on delete cascade,
  title             text not null default '',
  composer          text not null default '',
  notes_json        jsonb,                       -- output of score_to_json.to_app_json
  musicxml_path     text,
  progress_measure  int not null default 0,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);
create index pieces_user_idx on public.pieces (user_id);

create table public.scans (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references auth.users on delete cascade,
  piece_id          uuid references public.pieces on delete set null,
  status            text not null default 'queued' check (status in ('queued', 'processing', 'done', 'failed')),
  image_path        text not null,
  measures          int,
  flagged_measures  int[],
  error             text,
  created_at        timestamptz not null default now()
);
create index scans_user_month_idx on public.scans (user_id, created_at);

create table public.access_codes (
  code           text primary key,               -- stored upper-case, e.g. PIANO-7KQ2
  plan_id        text not null references public.plans,
  duration_days  int,                            -- null = no expiry
  max_uses       int not null default 1,
  used_count     int not null default 0,
  expires_at     timestamptz,
  note           text,
  created_by     uuid references auth.users,
  created_at     timestamptz not null default now()
);

create table public.code_redemptions (
  code         text not null references public.access_codes on delete cascade,
  user_id      uuid not null references auth.users on delete cascade,
  redeemed_at  timestamptz not null default now(),
  primary key (code, user_id)
);

-- ---------- helpers ----------

create function public.is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from profiles where id = auth.uid() and role = 'admin');
$$;

create function public.plan_for(uid uuid) returns text
language sql stable security definer set search_path = public as $$
  select case when plan_until is null or plan_until > now() then plan_id else 'free' end
  from profiles where id = uid;
$$;

create function public.scans_used_this_month(uid uuid) returns int
language sql stable security definer set search_path = public as $$
  select count(*)::int from scans s
  where s.user_id = uid
    and s.status <> 'failed'                     -- failed scans don't count against the quota
    and s.created_at >= greatest(date_trunc('month', now()),
                                 coalesce((select scans_reset_at from profiles where id = uid), '-infinity'));
$$;

revoke execute on function public.plan_for(uuid) from public, anon, authenticated;
revoke execute on function public.scans_used_this_month(uuid) from public, anon, authenticated;

-- New sign-ups get a profile automatically.
create function public.handle_new_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into profiles (id, email, display_name)
  values (new.id, new.email, coalesce(new.raw_user_meta_data ->> 'full_name', new.email));
  return new;
end $$;

create trigger on_auth_user_created
  after insert on auth.users for each row execute function public.handle_new_user();

-- A user may edit their own name, but never their role, plan or counters.
-- Trusted code (security definer functions, the server's service role, the SQL editor) is allowed.
create function public.protect_profile_fields() returns trigger
language plpgsql set search_path = public as $$
begin
  if current_user in ('authenticated', 'anon') and not public.is_admin() then
    if new.id is distinct from old.id
       or new.role is distinct from old.role
       or new.plan_id is distinct from old.plan_id
       or new.plan_until is distinct from old.plan_until
       or new.plan_source is distinct from old.plan_source
       or new.scans_reset_at is distinct from old.scans_reset_at
       or new.email is distinct from old.email then
      raise exception 'only_admin_can_change_plan';
    end if;
  end if;
  return new;
end $$;

create trigger profiles_protect_fields
  before update on public.profiles for each row execute function public.protect_profile_fields();

-- ---------- what the app calls ----------

-- One call on app start tells the app what this user may do.
create function public.my_entitlements() returns jsonb
language sql stable security definer set search_path = public as $$
  select case when p.role = 'admin' then jsonb_build_object(
           'role', 'admin', 'plan', 'admin', 'plan_until', null,
           'scans_per_month', null, 'max_pieces', null,
           'scans_used', scans_used_this_month(p.id),
           'features', (select jsonb_object_agg(k, true)
                        from (select distinct jsonb_object_keys(features) as k from plans) keys))
         else jsonb_build_object(
           'role', p.role, 'plan', pl.id, 'plan_until', p.plan_until,
           'scans_per_month', pl.scans_per_month, 'max_pieces', pl.max_pieces,
           'scans_used', scans_used_this_month(p.id),
           'features', pl.features)
         end
  from profiles p join plans pl on pl.id = plan_for(p.id)
  where p.id = auth.uid();
$$;

-- The only way to create a scan. The OMR server processes only scans created here.
create function public.start_scan(p_image_path text) returns uuid
language plpgsql security definer set search_path = public as $$
declare
  uid uuid := auth.uid();
  pl plans%rowtype;
  new_id uuid;
begin
  if uid is null then raise exception 'not_authenticated'; end if;
  if split_part(p_image_path, '/', 1) <> uid::text then raise exception 'bad_image_path'; end if;

  perform 1 from profiles where id = uid for update;   -- serialize this user's quota checks
  if not is_admin() then
    select * into pl from plans where id = plan_for(uid);
    if pl.scans_per_month is not null and scans_used_this_month(uid) >= pl.scans_per_month then
      raise exception 'scan_quota_exceeded';
    end if;
    if pl.max_pieces is not null and (select count(*) from pieces where user_id = uid) >= pl.max_pieces then
      raise exception 'piece_limit_reached';
    end if;
  end if;

  insert into scans (user_id, image_path) values (uid, p_image_path) returning id into new_id;
  return new_id;
end $$;

create function public.redeem_code(p_code text) returns jsonb
language plpgsql security definer set search_path = public as $$
declare
  uid uuid := auth.uid();
  c access_codes%rowtype;
begin
  if uid is null then raise exception 'not_authenticated'; end if;
  select * into c from access_codes where code = upper(trim(p_code)) for update;
  if not found then raise exception 'code_not_found'; end if;
  if c.expires_at is not null and c.expires_at < now() then raise exception 'code_expired'; end if;
  if c.used_count >= c.max_uses then raise exception 'code_used_up'; end if;
  if exists (select 1 from code_redemptions where code = c.code and user_id = uid) then
    raise exception 'code_already_redeemed';
  end if;

  update profiles set
    plan_id = c.plan_id,
    plan_until = case when c.duration_days is null then null
                      else greatest(now(), coalesce(plan_until, now())) + make_interval(days => c.duration_days) end,
    plan_source = 'code'
  where id = uid;
  insert into code_redemptions (code, user_id) values (c.code, uid);
  update access_codes set used_count = used_count + 1 where code = c.code;
  return my_entitlements();
end $$;

-- Admin screen: every user with this month's usage.
create function public.admin_users()
returns table (id uuid, email text, display_name text, role text, plan_id text,
               plan_until timestamptz, scans_this_month int, created_at timestamptz)
language plpgsql stable security definer set search_path = public as $$
begin
  if not is_admin() then raise exception 'not_admin'; end if;
  return query
    select p.id, p.email, p.display_name, p.role, p.plan_id, p.plan_until,
           scans_used_this_month(p.id), p.created_at
    from profiles p order by p.created_at desc;
end $$;

-- ---------- row level security ----------

alter table public.plans            enable row level security;
alter table public.profiles         enable row level security;
alter table public.pieces           enable row level security;
alter table public.scans            enable row level security;
alter table public.access_codes     enable row level security;
alter table public.code_redemptions enable row level security;

create policy "plans readable by signed-in users" on public.plans
  for select to authenticated using (true);
create policy "plans editable by admin" on public.plans
  for all to authenticated using (public.is_admin()) with check (public.is_admin());

create policy "read own profile or admin" on public.profiles
  for select to authenticated using (id = auth.uid() or public.is_admin());
create policy "update own profile or admin" on public.profiles
  for update to authenticated using (id = auth.uid() or public.is_admin())
  with check (id = auth.uid() or public.is_admin());

-- Pieces are created by the OMR server (service role); users read, rename, fix and delete their own.
create policy "read own pieces or admin" on public.pieces
  for select to authenticated using (user_id = auth.uid() or public.is_admin());
create policy "update own pieces" on public.pieces
  for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "delete own pieces" on public.pieces
  for delete to authenticated using (user_id = auth.uid());

create policy "read own scans or admin" on public.scans
  for select to authenticated using (user_id = auth.uid() or public.is_admin());

create policy "codes managed by admin" on public.access_codes
  for all to authenticated using (public.is_admin()) with check (public.is_admin());

create policy "read own redemptions or admin" on public.code_redemptions
  for select to authenticated using (user_id = auth.uid() or public.is_admin());

-- ---------- make yourself admin (run once in the SQL editor, after you sign up) ----------
-- update public.profiles set role = 'admin' where email = 'YOUR_EMAIL';
