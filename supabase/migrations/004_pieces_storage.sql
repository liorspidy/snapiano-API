-- Private bucket for the straightened line images of a piece. The worker (service role) writes;
-- each user can only read inside their own folder pieces/<user_id>/...
insert into storage.buckets (id, name, public) values ('pieces', 'pieces', false)
  on conflict (id) do nothing;

create policy "read own piece images, admin reads all" on storage.objects
  for select to authenticated
  using (bucket_id = 'pieces' and ((storage.foldername(name))[1] = auth.uid()::text or public.is_admin()));
