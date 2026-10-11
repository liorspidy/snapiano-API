// Deletes the signed-in user: data rows (delete_my_data), their files, then the login itself.
// Deploy: supabase functions deploy delete-account   (SUPABASE_URL / SERVICE_ROLE_KEY are provided by Supabase)
import { createClient } from 'jsr:@supabase/supabase-js@2';

const BUCKETS = ['scans', 'pieces', 'feedback'];

Deno.serve(async (req) => {
  const auth = req.headers.get('Authorization') ?? '';
  const url = Deno.env.get('SUPABASE_URL')!;
  const asUser = createClient(url, Deno.env.get('SUPABASE_ANON_KEY')!, { global: { headers: { Authorization: auth } } });
  const { data: { user } } = await asUser.auth.getUser();
  if (!user) return new Response('unauthorized', { status: 401 });

  const admin = createClient(url, Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!);
  const { error } = await asUser.rpc('delete_my_data');
  if (error) return new Response(error.message, { status: 500 });

  for (const bucket of BUCKETS) {
    const stack = [user.id];
    while (stack.length) { // pieces/<user>/<piece>/lineNN.png is nested one level
      const dir = stack.pop()!;
      const { data } = await admin.storage.from(bucket).list(dir, { limit: 1000 });
      const files: string[] = [];
      for (const f of data ?? []) (f.id ? files.push(`${dir}/${f.name}`) : stack.push(`${dir}/${f.name}`));
      if (files.length) await admin.storage.from(bucket).remove(files);
    }
  }
  const { error: delErr } = await admin.auth.admin.deleteUser(user.id);
  if (delErr) return new Response(delErr.message, { status: 500 });
  return new Response('ok');
});
