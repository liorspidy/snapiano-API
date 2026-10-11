-- Admin actions on users: ban for a time (or forever), change role, delete the account.
-- A ban is enforced in the database (no scans, and the app signs the person out). It is not a login-level ban.

alter table public.profiles
  add column banned_until timestamptz,   -- null = not banned, 'infinity' = permanent
  add column ban_reason   text;

-- Users must not be able to lift their own ban.
create or replace function public.protect_profile_fields() returns trigger
language plpgsql set search_path = public as $$
begin
  if current_user in ('authenticated', 'anon') and not public.is_admin() then
    if new.id is distinct from old.id
       or new.role is distinct from old.role
       or new.plan_id is distinct from old.plan_id
       or new.plan_until is distinct from old.plan_until
       or new.plan_source is distinct from old.plan_source
       or new.scans_reset_at is distinct from old.scans_reset_at
       or new.banned_until is distinct from old.banned_until
       or new.ban_reason is distinct from old.ban_reason
       or new.email is distinct from old.email then
      raise exception 'only_admin_can_change_plan';
    end if;
  end if;
  return new;
end $$;

create or replace function public.my_entitlements() returns jsonb
language sql stable security definer set search_path = public as $$
  select case when p.role = 'admin' then jsonb_build_object(
           'role', 'admin', 'plan', 'admin', 'plan_until', null,
           'scans_per_month', null, 'max_pieces', null,
           'scans_used', scans_used_this_month(p.id),
           'banned_until', null,
           'features', (select jsonb_object_agg(k, true)
                        from (select distinct jsonb_object_keys(features) as k from plans) keys))
         else jsonb_build_object(
           'role', p.role, 'plan', pl.id, 'plan_until', p.plan_until,
           'scans_per_month', pl.scans_per_month, 'max_pieces', pl.max_pieces,
           'scans_used', scans_used_this_month(p.id),
           'banned_until', case when p.banned_until > now() then p.banned_until end,
           'features', pl.features)
         end
  from profiles p join plans pl on pl.id = plan_for(p.id)
  where p.id = auth.uid();
$$;

create or replace function public.start_scan(p_image_path text) returns uuid
language plpgsql security definer set search_path = public as $$
declare
  uid uuid := auth.uid();
  pl plans%rowtype;
  new_id uuid;
begin
  if uid is null then raise exception 'not_authenticated'; end if;
  if split_part(p_image_path, '/', 1) <> uid::text then raise exception 'bad_image_path'; end if;
  if exists (select 1 from profiles where id = uid and banned_until > now()) then raise exception 'account_banned'; end if;

  perform 1 from profiles where id = uid for update;
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

drop function public.admin_users();
create function public.admin_users()
returns table (id uuid, email text, display_name text, role text, plan_id text,
               plan_until timestamptz, scans_this_month int, created_at timestamptz,
               banned_until timestamptz, ban_reason text)
language plpgsql stable security definer set search_path = public as $$
begin
  if not is_admin() then raise exception 'not_admin'; end if;
  return query
    select p.id, p.email, p.display_name, p.role, p.plan_id, p.plan_until,
           scans_used_this_month(p.id), p.created_at, p.banned_until, p.ban_reason
    from profiles p order by p.created_at desc;
end $$;

create function public.admin_set_ban(p_user uuid, p_until timestamptz, p_reason text default null) returns void
language plpgsql security definer set search_path = public as $$
begin
  if not is_admin() then raise exception 'not_admin'; end if;
  if p_user = auth.uid() then raise exception 'cannot_target_self'; end if;
  update profiles set banned_until = p_until, ban_reason = case when p_until is null then null else p_reason end where id = p_user;
end $$;

create function public.admin_set_role(p_user uuid, p_role text) returns void
language plpgsql security definer set search_path = public as $$
begin
  if not is_admin() then raise exception 'not_admin'; end if;
  if p_user = auth.uid() then raise exception 'cannot_target_self'; end if;
  if p_role not in ('user', 'admin') then raise exception 'bad_role'; end if;
  update profiles set role = p_role where id = p_user;
end $$;

-- Removes the login; profiles, pieces, scans and redemptions go with it (on delete cascade).
-- The app deletes the person's files from storage first.
create function public.admin_delete_user(p_user uuid) returns void
language plpgsql security definer set search_path = public, auth as $$
begin
  if not is_admin() then raise exception 'not_admin'; end if;
  if p_user = auth.uid() then raise exception 'cannot_target_self'; end if;
  update feedback set user_id = null, device = null where user_id = p_user;
  delete from auth.users where id = p_user;
end $$;

revoke execute on function public.admin_set_ban(uuid, timestamptz, text) from public, anon;
revoke execute on function public.admin_set_role(uuid, text) from public, anon;
revoke execute on function public.admin_delete_user(uuid) from public, anon;
revoke execute on function public.admin_users() from public, anon;

create policy "admin deletes any scan image" on storage.objects
  for delete to authenticated
  using (bucket_id = 'scans' and public.is_admin());
