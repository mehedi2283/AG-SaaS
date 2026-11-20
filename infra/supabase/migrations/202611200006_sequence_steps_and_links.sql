-- 7. Each sequence step has its own subject (Instantly custom_subject_{n}); steps per campaign are set
--    by sequence_count. Step 1 is subject + body / personalization_1.
alter table public.campaigns
  add column sequence_count int not null default 1 check (sequence_count between 1 and 5);
alter table public.generated_emails
  add column subject_2 text,
  add column subject_3 text,
  add column subject_4 text,
  add column subject_5 text;

grant insert (sequence_count), update (sequence_count) on public.campaigns to authenticated;
grant update (subject_2, subject_3, subject_4, subject_5) on public.generated_emails to authenticated;
grant insert (subject_2, subject_3, subject_4, subject_5), update (subject_2, subject_3, subject_4, subject_5)
  on public.generated_emails to gazi_worker;

-- 8. One link test for both backstops. Also catches bare domains with a path ("calendly.com/sam"),
--    which mail clients turn into links.
--    shortcut: any <img> is allowed here because the signature is encrypted and the DB cannot compare it;
--    app/llm/guards.py must accept only an <img> whose src is the tenant's signature URL.
create or replace function app.has_link(p_html text) returns boolean
language sql immutable
set search_path = ''
as $$
  select app.strip_img_tags(p_html) ~* '(<a[[:space:]>]|https?://|www\.|\m[a-z0-9-]+\.[a-z]{2,}/)'
$$;
grant execute on function app.has_link(text) to authenticated, gazi_worker;

alter table public.generated_emails drop constraint follow_ups_have_no_links;
alter table public.generated_emails add constraint follow_ups_have_no_links check (
  not app.has_link(concat_ws(' ', personalization_2, personalization_3, personalization_4, personalization_5,
                             subject_2, subject_3, subject_4, subject_5))
);

alter table public.replies drop constraint links_only_when_interested_or_question;
alter table public.replies add constraint links_only_when_interested_or_question check (
  coalesce(intent in ('interested', 'question'), false) or not app.has_link(concat_ws(' ', draft_body, final_body))
);
