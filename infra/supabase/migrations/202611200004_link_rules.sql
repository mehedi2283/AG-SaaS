-- Link rules (decided 2026-11-20), DB backstops for app/llm/guards.py:
--   * Sequence follow-ups (personalization_2..5): no clickable links (<a> tags, raw URLs, www.).
--     The signature image (<img ...>) is the only URL allowed, so <img> tags are stripped before checking.
--   * Replies: links (calendar link, attachment) only when intent is 'interested' or 'question'.
--     The signature image is allowed on every reply.

create or replace function app.strip_img_tags(p_html text) returns text
language sql immutable
set search_path = ''
as $$
  select regexp_replace(coalesce(p_html, ''), '<img\M[^>]*>', '', 'gi')
$$;
grant execute on function app.strip_img_tags(text) to authenticated, gazi_worker;

alter table public.generated_emails drop constraint follow_ups_have_no_links;
alter table public.generated_emails add constraint follow_ups_have_no_links check (
  app.strip_img_tags(concat_ws(' ', personalization_2, personalization_3, personalization_4, personalization_5))
    !~* '(<a[[:space:]>]|https?://|www\.)'
);

alter table public.replies drop constraint links_only_when_interested;
alter table public.replies add constraint links_only_when_interested_or_question check (
  intent in ('interested', 'question')
  or app.strip_img_tags(concat_ws(' ', draft_body, final_body)) !~* '(<a[[:space:]>]|https?://|www\.)'
);
