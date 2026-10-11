-- A user may delete the page images of their own pieces (folder pieces/<user_id>/...). Admins may delete any.
create policy "delete own piece images" on storage.objects
  for delete to authenticated
  using (bucket_id = 'pieces' and ((storage.foldername(name))[1] = auth.uid()::text or public.is_admin()));
