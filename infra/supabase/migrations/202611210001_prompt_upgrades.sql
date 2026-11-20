-- Workers retire an older built-in prompt version when the code ships a new one (app/llm/prompts.py ensure_prompt).
-- Only is_active may change; the immutability trigger (0002) still blocks any other edit.
grant update (is_active) on public.prompt_templates to gazi_worker;
