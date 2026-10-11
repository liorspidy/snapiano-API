-- Private bucket for page photos. Each user can only write and read inside a folder named by their user id.
insert into storage.buckets (id, name, public) values ('scans', 'scans', false)
on conflict (id) do nothing;

create policy "upload scans to own folder" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'scans' and (storage.foldername(name))[1] = auth.uid()::text);

create policy "read own scans, admin reads all" on storage.objects
  for select to authenticated
  using (bucket_id = 'scans' and ((storage.foldername(name))[1] = auth.uid()::text or public.is_admin()));

create policy "delete own scans" on storage.objects
  for delete to authenticated
  using (bucket_id = 'scans' and (storage.foldername(name))[1] = auth.uid()::text);
