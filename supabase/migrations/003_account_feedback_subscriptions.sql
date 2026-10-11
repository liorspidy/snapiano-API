-- Language, feedback / bug reports, store subscriptions, and deleting your own account.
-- Run after 001 and 002.

alter table public.profiles
  add column locale text not null default 'he' check (locale in ('he', 'en', 'fr', 'ar', 'ru'));

-- ---------- feedback: bug reports and feature ideas ----------
create table public.feedback (
  id               uuid primary key default gen_random_uuid(),
  user_id          uuid references auth.users on delete set null,
  kind             text not null check (kind in ('bug', 'idea', 'other')),
  message          text not null check (length(message) between 3 and 4000),
  screenshot_path  text,                 -- in the private 'feedback' bucket, under the user's folder
  app_version      text,
  device           text,                 -- e.g. "Pixel 7, Android 15" (only if the user agreed)
  locale           text,
  status           text not null default 'new' check (status in ('new', 'in_progress', 'done', 'wont_do')),
  admin_note       text,
  created_at       timestamptz not null default now()
);
create index feedback_status_idx on public.feedback (status, created_at desc);
alter table public.feedback enable row level security;

create policy "send own feedback" on public.feedback
  for insert to authenticated with check (user_id = auth.uid() and status = 'new' and admin_note is null);
create policy "read own feedback or admin" on public.feedback
  for select to authenticated using (user_id = auth.uid() or public.is_admin());
create policy "admin updates feedback" on public.feedback
  for update to authenticated using (public.is_admin()) with check (public.is_admin());

-- ---------- store subscriptions (phase 2: written only by the payment webhook) ----------
create table public.subscriptions (
  user_id             uuid primary key references auth.users on delete cascade,
  store               text not null check (store in ('play_store', 'app_store')),
  product_id          text not null,
  status              text not null check (status in ('active', 'in_grace', 'cancelled', 'expired')),
  will_renew          boolean not null default true,
  current_period_end  timestamptz not null,
  updated_at          timestamptz not null default now()
);
alter table public.subscriptions enable row level security;
create policy "read own subscription or admin" on public.subscriptions
  for select to authenticated using (user_id = auth.uid() or public.is_admin());
-- No insert/update policies: only the server (service role) writes here, from the store webhook,
-- and it also sets profiles.plan_id / plan_until / plan_source = 'store'.

-- ---------- account screen ----------
create function public.my_account() returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'email', p.email, 'display_name', p.display_name, 'locale', p.locale,
    'created_at', p.created_at, 'entitlements', my_entitlements(),
    'plan_source', p.plan_source,
    'subscription', (select to_jsonb(s) - 'user_id' from subscriptions s where s.user_id = p.id))
  from profiles p where p.id = auth.uid();
$$;

-- Users may change their own language (protect_profile_fields still blocks plan/role changes).

-- Deleting an account (required by Google Play): this removes the user's data rows; the app then
-- calls the 'delete-account' Edge Function, which deletes the login itself with the admin API
-- (auth.admin.deleteUser) and the user's files in storage.
create function public.delete_my_data() returns void
language plpgsql security definer set search_path = public as $$
declare uid uuid := auth.uid();
begin
  if uid is null then raise exception 'not_authenticated'; end if;
  delete from scans where user_id = uid;
  delete from pieces where user_id = uid;
  delete from code_redemptions where user_id = uid;
  update feedback set user_id = null, device = null where user_id = uid;  -- keep the report, drop who sent it
end $$;

-- ---------- admin inbox ----------
create function public.admin_feedback(p_status text default null)
returns table (id uuid, kind text, message text, status text, email text, app_version text,
               device text, locale text, has_screenshot boolean, created_at timestamptz)
language plpgsql stable security definer set search_path = public as $$
begin
  if not is_admin() then raise exception 'not_admin'; end if;
  return query
    select f.id, f.kind, f.message, f.status, p.email, f.app_version, f.device, f.locale,
           f.screenshot_path is not null, f.created_at
    from feedback f left join profiles p on p.id = f.user_id
    where p_status is null or f.status = p_status
    order by f.created_at desc;
end $$;

insert into storage.buckets (id, name, public) values ('feedback', 'feedback', false)
on conflict (id) do nothing;
create policy "upload own feedback screenshots" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'feedback' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "admin reads feedback screenshots" on storage.objects
  for select to authenticated
  using (bucket_id = 'feedback' and ((storage.foldername(name))[1] = auth.uid()::text or public.is_admin()));
