-- LOCAL DEVELOPMENT ONLY. Runs after the migrations on `supabase start` / `supabase db reset`.
-- The API's login role: member of `authenticated` and `gazi_worker`, never superuser or BYPASSRLS.
-- The password below is for the local Docker database only; production creates this role with a secret.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'gazi_api') then
    create role gazi_api login noinherit password 'gazi-local-dev';
  end if;
end
$$;
grant authenticated, gazi_worker to gazi_api;
