# Gazi design system

Subject: correspondence between an exporter and overseas buyers. The core object is a *conversation*,
not a metric. Home screen = the reply desk, not a dashboard of big numbers.

Color
- Desk   #F5F5F8  cool grey app background
- Paper  #FFFFFF  panes
- Ink    #201D29  text (violet-leaning, tied to brand)
- Ink-2  #5D5A69  secondary text
- Rule   #E4E3EA  1px dividers
- Violet #6A2CC9  the only action colour (Gazi #A234FD deepened for AA text contrast)
- Intent colours carry meaning only: interested #17734A, question #2451A6, out of office #93600A,
  unsubscribe #B42318, others ink-2.

Type
- UI: Schibsted Grotesk (newsroom grotesk: firm, a little editorial, not Inter/Geist).
- Messages: Source Serif 4. Everything a *person wrote* (the buyer's reply, the draft we'll send)
  is set as a letter in serif; everything the *system* says is in the grotesk. That split is the idea.
- Scale: 12 / 13 / 14 (base) / 16 (letter) / 20 / 26. Tabular figures for counts.

Layout
  ┌────┬───────────────┬─────────────────────────────────┐
  │nav │ reply list    │ thread: our email (collapsed)    │
  │    │ name, intent  │ their reply  (serif letter)      │
  │    │ snippet, time │ draft reply  (serif, editable)   │
  │    │               │ [intent ▾] link note   [Send]    │
  └────┴───────────────┴─────────────────────────────────┘
  Columns separated by rules, no floating cards, no shadows. Left aligned throughout.
  Campaigns = a plain table. Campaign page = one sentence of numbers + the sequence (a real sequence,
  so step numbers are earned). Leads = import + duplicate review.

Principles
1. People's words in serif, system words in sans.
2. Colour means something (intent or action) or it isn't there.
3. The rules are visible: when intent changes, the UI says whether a link will be added and why.
4. One bold thing: the letter treatment. Everything else stays quiet.

Review vs generic defaults
- Draft 0 had a KPI card row on the home screen (4 big numbers + sparkline). That's the SaaS-card kit;
  cut. Numbers now live in one sentence on the campaign page.
- Serif + warm cream is the classic tell; kept the serif but only for message bodies, on cool grey/white,
  never as display type.
- No ALL-CAPS labels, no mono data labels, no "→" on buttons, no middle-dot meta strings.

Build notes (from the screenshot review)
- Schibsted Grotesk's tabular figures also make punctuation tabular, so `tabular-nums` is applied only
  to number cells, times and counts, never to body text.
- Drafts auto-size to their content, and are measured again once fonts load.
- Prototype: `docs/design/prototype.html` (open it in a browser; the screens are Replies, Campaigns,
  Campaign, Leads and Settings).

## v2: the production frontend ("very clean and elegant")

Changes from the prototype, decided before building:
- One white surface instead of grey desk + white panes. The side rail is the only tinted area (#F8F7FA).
  Panes are separated by hairlines (#ECEBF0), never boxed; no shadows except on the open menu.
- Base size 15px (was 14) with more air: 48px page gutter, 32px between sections, row height ~64px.
- Weights limited to 400 / 500 / 600. Titles are 28px/600 grotesk, tight tracking.
- The one bold element stays the letter: people's words in Source Serif 4. It also appears on the
  sign-in page as a buyer's reply, so the product's idea is the first thing anyone sees.
- Empty screens are written as invitations ("Replies from buyers will appear here") with one action.
- Lists are rows, not cards. Settings is a single column of rows, each with one clear action.

Review against generic defaults: white + violet is common for SaaS, so the identity has to come from the
letter treatment and the restraint, not colour. Cut from the first sketch: a coloured header band on
settings, icon badges beside every provider, and a stat strip on the replies page. They added noise.

## Loading and toasts (2026-11-21)
- Loading is shown as skeletons shaped like the screen (`components/Skeleton.tsx`): hairline rows, letter lines beside
  the message rule. Rule-coloured bars that breathe, no shimmer; they fade in after 0.15s so fast loads don't flash.
  `app/(app)/loading.tsx` shows one the moment a menu item is clicked.
- Toasts sit bottom-left in line with the content. A dot carries the meaning (green done, red needs attention);
  `toast(message, "error")` stays 7s, can be dismissed and uses role="alert".
