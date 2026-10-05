# LinkedIn MCP Operations Playbook

Operating rules for running this server against a real LinkedIn account without
getting it restricted. Numbers below are the caps we chose; the mechanics are
what the server actually does.

## Read this first

LinkedIn's User Agreement (§8.2) prohibits automated access, scraping and
software that scrapes or copies data. Nothing in this playbook makes automated
use of LinkedIn contractually compliant — it manages *risk and volume*, which
is a different thing. Treat every cap here as a way to stay under detection
thresholds and keep the account alive, not as a compliance certificate. The
account owner carries the consequences of a restriction or ban.

LinkedIn publishes none of its real thresholds. Every number below is either
(a) our own chosen budget or (b) a community-observed approximation, marked as
such. They move without notice.

## What the server does and does not do for you

| | |
|---|---|
| Serializes tool calls | Yes — `SequentialToolExecutionMiddleware` plus a cross-process profile lease. You cannot accidentally run two scrapes in parallel. |
| Paces navigations | Partly — `_NAV_DELAY = 2.0s` between page navigations (`scraping/extractor.py`). |
| Backs off when blocked | Weakly — `_RATE_LIMIT_RETRY_DELAY = 5.0s`, one retry. |
| Tracks daily/weekly quotas | **No.** There is no counter, no ledger, no refusal at a cap. |
| Detects a throttle reliably | **No.** `[Rate limited]` in `section_errors` is a *guess* from an empty page shell (see the comment above `_RATE_LIMITED_MSG`). Absence of it is not proof you are fine. |
| Detects a session kill | Yes — a redirect to `/login`, `/authwall` or `/checkpoint` raises before extraction. |

**The quotas in this playbook are enforced by the caller, not by the server.**
Whatever orchestrates the launches has to keep the ledger.

## The unit that matters is the navigation, not the profile

LinkedIn counts page views. This server does one navigation per requested
section (`PERSON_SECTIONS` in `scraping/fields.py`), so:

```
page views = profiles × (1 + number of extra sections)
```

| `sections=` | Navigations per profile | Profiles inside a 1500-view/day budget |
|---|---|---|
| omitted (main only) | 1 | 1500 |
| `experience` | 2 | 750 |
| `experience,education` | 3 | 500 |
| `experience,education,posts` | 4 | 375 |

**Rule: the daily cap is 1500 profile page views, not 1500 `get_person_profile`
calls.** Decide the section set before the run and divide. `contact_info` is an
overlay on the main page but still counts as a navigation.

Do not "top up" a profile later with a second call for another section — that
re-navigates the main page too and burns two views to get one. Request every
section you need in one call.

---

## Workflow 1 — Profile scraper

**Tool:** `get_person_profile` (discovery via `search_people` /
`get_company_employees`).

| Rule | Value |
|---|---|
| Profile page views per launch | 200 |
| Profile page views per day | 1500 |
| Launches per day | ≤ 8 (1500 ÷ 200 = 7.5) |
| Gap between launches | ≥ 45 min, ideally 60 |
| Active window | one 8–10 h block matching the account's local working hours |
| Days per week | 5 (Mon–Fri) unless this account is genuinely weekend-active |

**Timing.** A navigation costs ~2 s of enforced delay plus 3–6 s of page load,
so ~5–8 s. A 200-view launch runs ~20–27 min. 1500 views is ~2.5–3.5 h of
continuous browsing — which is why it must be split across ≥ 7 launches with
real gaps rather than run as one block. A single unbroken multi-hour session is
itself a signal.

**Discovery cost.** `search_people` and `search_companies` count against
LinkedIn's *commercial use limit* — a separate monthly counter that resets on
the 1st and is much tighter than the view budget on a free account. Do discovery
in bulk once, persist the usernames, then scrape from the stored list. Never
re-run a search to re-derive a list you already have.

**Capture the URN while you are there.** `get_person_profile` returns the
profile URN in `references`. Store it. `send_message` takes `profile_urn` and
skips the Message-button lookup, so Workflow 4 costs no extra profile views.

**Reality check on 1500/day.** For a free account this is far above the
commercial-use ceiling (community estimate: profile views start getting
throttled well under 300/day, and search results get capped within a few
hundred). 1500/day is only survivable on Sales Navigator or a long-warmed
Premium account, and even then it is aggressive. If this account is new or
free-tier, ramp: 100/day week 1, 300/day week 2, 600/day week 3, then reassess.

---

## Workflow 2 — Post engagement scraper

**This workflow is not currently supported by the server.** Stating it plainly
so nothing is built on an assumption:

There is no tool that returns *who* liked or commented on a post. The closest
paths and what they actually give you:

| Path | Returns |
|---|---|
| `get_person_profile(sections="posts")` | The person's own recent activity (`/recent-activity/all/`) as raw text — post bodies, dates, and **aggregate** reaction/comment counts. No reactor or commenter identities. |
| `search_posts(keywords=...)` | Global content search, raw text, same aggregate counts. No permalinks per post. |
| `get_company_posts` | One company page's posts. Same shape. |
| `get_feed` | Your own home feed, with `references["feed"]` post permalinks (cap 50). |

Extracting reactor/commenter identities needs a new section that navigates a
post's reactions overlay — one section, one navigation, per the scraping rules
in `CLAUDE.md`. Until that exists, treat this workflow as blocked. File it as an
issue before building around it.

**If you proceed with the aggregate-counts version** (post text + counts, no
identities), budget it against the same 1500 view/day pool: one target profile
with `sections="posts"` is 2 navigations, and `max_scrolls` on the posts section
defaults to 10 — those scrolls are not navigations but they do extend the
session and load more content, so keep `max_scrolls` at the default unless a
specific target needs more.

---

## Workflow 3 — Inbox scraper

**Tools:** `get_inbox`, `search_conversations`, `get_conversation`.

| Rule | Value |
|---|---|
| Frequency | 1–2 runs/day |
| Cost | `get_inbox` is 1 navigation; each `get_conversation` opens one thread |
| Thread opens per run | ≤ 30, and only for threads that changed |

This is the cheapest and lowest-risk workflow — reading your own messages is
ordinary account use. Two rules keep it that way:

1. **Diff before you open.** `get_inbox` gives the thread list. Only call
   `get_conversation` on threads whose latest message you have not already
   stored. Re-reading the whole inbox every day turns a cheap workflow into
   hundreds of navigations.
2. **Prefer `search_conversations` over paging the inbox** when you are looking
   for something specific.

**Replies are not automatable yet.** `send_message` targets a *profile* and
opens LinkedIn's compose flow — it may create a separate DM rather than
replying in the thread you just read, even after `get_conversation`. Route
replies to a human until a thread-targeted send path exists. This is documented
in `send_message`'s own docstring; do not work around it.

---

## Workflow 4 — Message sender

**Tool:** `send_message(linkedin_username, message, confirm_send=True, profile_urn=...)`.

| Rule | Value |
|---|---|
| Messages per week | 80 |
| Messages per launch | 10 |
| Launches per day | ≤ 2 |
| Messages per day | ≤ 16, target 14 (80 over 5–6 days) |
| Gap between sends | ≥ 3 min, randomised 3–7 min |
| Gap between launches | ≥ 3 h |

**Never send 80 in a day and stop.** The weekly cap is a ceiling, not a
schedule. Burst sending is the single loudest automation signal on the platform
— spread across 5–6 days, inside working hours, in the recipient's plausible
timezone.

**Additional hard rules:**

- `confirm_send` must be explicitly `True`. Nothing sends without it; do not
  default it to `True` in a wrapper.
- **Pass `profile_urn`** captured during Workflow 1. It bypasses the
  Message-button lookup, which is both more reliable and one fewer thing to
  navigate.
- **1st-degree only** unless the account has InMail credits. Messaging a
  non-connection without InMail fails or burns a credit silently.
- **Vary the message.** Identical bodies across recipients are trivially
  clustered. If the template is fixed, the workflow is a spam filter's easiest
  catch.
- **No message to someone already in an open thread** — check `get_inbox`
  first. Duplicate outreach to a live conversation is the fastest route to a
  "this person is spamming me" report, and user reports outrank every rate
  limit here.

---

## The REST endpoint

`POST /v1/send-message`, registered only on the `streamable-http` transport, for
an application that owns its own campaign logic and does not want to speak
JSON-RPC. It dispatches through the same tool and the same serializing
middleware as an MCP call, so it is a translation layer and nothing more.

```
POST /v1/send-message
{"linkedin_username": "...", "profile_urn": "ACoAAB...",
 "message": "...", "confirm_send": true}
```

| Response | Meaning |
|---|---|
| `200` | Sent. |
| `400` | Malformed request, or `confirm_send` was false. Nothing was attempted. |
| `409` | Another client holds the browser. Nothing was sent. |
| `422` | The recipient is not messageable from their profile. |
| `429` | Rate-limited by LinkedIn. |
| `502` | LinkedIn did not complete the flow. |
| `503` | The session is not usable; run `--login` on the server host. |

**`retry_safe` is the field that matters.** Every failure carries it. It is
`false` in exactly one case — `send_unavailable`, the status reached *after* the
send button is clicked, where LinkedIn may have delivered the message without
confirming it. A caller that retries on that turns one ambiguous send into two
messages to the same person, which is the "this person is spamming me" report
this playbook exists to avoid. An unrecognised status is reported unsafe too,
on the same reasoning.

**The endpoint carries none of the rate rules above.** No pacing, no daily or
weekly ceiling, no gap between sends, no open-thread check. All of it belongs to
the caller. The endpoint will send as fast as it is asked to, which is exactly
the burst pattern that draws a restriction.

**It is unauthenticated**, like the MCP endpoint beside it. Loopback binding is
what makes that acceptable, and the caller is expected to be on the same host.

**Timeouts.** A send blocks for as long as the flow takes plus any queue wait
behind another tool call. Set the client timeout above the server's
`--tool-timeout` so the server's own error arrives instead of the client giving
up on a send still in flight — a client-side timeout on a send that then
succeeds is the other way to get a duplicate.

---

## Connection requests

Not in the workflow list, but `connect_with_person` exists and shares the same
account risk budget. If it ever gets used: LinkedIn's observed weekly invite
ceiling is **~100/week** (this one LinkedIn has stated publicly), and a
pending-invite backlog above ~500 with a low acceptance rate draws restrictions
on its own. Withdraw stale invites before sending new ones.

---

## Daily and weekly ledger

The orchestrator keeps this. Reset the daily row at local midnight of the
account's timezone, the weekly row on Monday.

| Counter | Cap | Scope |
|---|---|---|
| Profile page views (profiles × sections) | 1500 | day |
| Profile-scrape launches | 8 | day |
| Search calls (`search_people` / `search_companies` / `search_posts`) | 30 | day, and watch the monthly commercial-use limit |
| Messages sent | 16 | day |
| Messages sent | 80 | week |
| Message launches | 2 | day |
| Connection invites | 100 | week |
| Total active browsing time | 4 h | day |

That last row is the one people forget. Volume caps mean nothing if the account
is driving a browser for nine hours straight.

## Stop conditions

Halt every workflow and escalate to a human — do not retry, do not back off and
resume — on any of these:

1. **`AuthenticationError` / redirect to `/login`, `/authwall`, `/checkpoint`.**
   The session is gone or a security challenge is up. Re-login manually with
   `uv run -m linkedin_mcp_server --login`. Never retry automatically into a
   checkpoint.
2. **`RateLimitError`** from `error_handler.py` — honour the wait time it
   reports, then stop for the day regardless.
3. **`[Rate limited]` in `section_errors`** on two consecutive calls. One
   occurrence is ambiguous (it is a guess from an empty page, not an observed
   throttle); two in a row means stop.
4. **Empty results where results are expected** — a search returning nothing for
   a query that worked yesterday is a soft block far more often than a real
   change in the data.
5. **A message send returning `sent: false`** twice in a launch.

After any stop: pause the account for 24 h minimum, log in manually through a
browser and confirm the account is unrestricted before resuming, then resume at
half the previous volume for three days.

## Preflight checklist per launch

- [ ] Ledger read; today's and this week's caps have room for the whole launch
- [ ] Section set fixed, and `profiles × (1 + sections)` fits the remaining view budget
- [ ] Target list loaded from storage, not re-derived from a fresh search
- [ ] Launch starts inside the account's working-hours window
- [ ] ≥ 45 min since the previous scrape launch (≥ 3 h for a message launch)
- [ ] For messages: `confirm_send=True` is deliberate, bodies vary, recipients are 1st-degree and not in an open thread
- [ ] Ledger updated on completion, including partial launches that stopped early
