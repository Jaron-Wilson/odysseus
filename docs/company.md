# Company: an organization of AI agents, native to Odysseus

Design notes for a Paperclip-style "company" inside Odysseus: goals, an org
chart of agents, issues they work on, approvals, budgets and an audit trail.

Written 2026-10-10. Everything under "What already exists" was read out of
this checkout; "What this PR builds" is in the code as of PR A; "What to
build next" is a proposal.

---

## Why

Jaron self-hosts [Paperclip](https://github.com/paperclipai/paperclip). It is a
good model of how to run a team of agents, but its agents run through
adapters for paid CLIs (Claude Code, Codex, Cursor and the like). The
instance has one empty company, so nothing needs importing.

Odysseus already reaches every model he uses: local Ollama and vLLM boxes,
OpenAI-compatible APIs, Anthropic, ChatGPT plan. So the same organization,
built natively, lets each agent run on **any Odysseus model**, a 7B on the
3090 box for routine work and a frontier model for the CEO.

---

## Concept mapping

| Paperclip | Odysseus Company | Notes |
|---|---|---|
| Company (goal, budget, issue prefix) | `company_companies` | One owner can run several companies. Issue prefix unique per owner. |
| Goal hierarchy | `company_goals` (`parent_id`, `level`) | Levels company / team / agent / task. |
| Project | `company_projects` | Optional goal, lead agent, target date, color. |
| Agent + adapter config | `company_agents` | The adapter is replaced by an Odysseus **model spec** `model@Endpoint Name`, resolved by `src/ai_interaction.py::_resolve_model`. |
| Strict reporting tree | `reports_to` | One CEO at the root, no loops. "Make CEO" swaps the top. |
| Issue (status, priority, assignee, parent) | `company_issues` | Identifier `PREFIX-n`, validated transitions, sub-issues. |
| Atomic checkout (409 on conflict) | `checkout_run_id`, `execution_locked_at` | One conditional `UPDATE`; same run may re-check-out; stale lock takeover. |
| Issue comments, @mentions | `company_issue_comments.mentions` | Mentions parsed on save and queued as wake-ups. |
| Labels, blocking relations | `company_labels`, `company_issue_labels`, `company_issue_relations` | |
| Heartbeat runs | `company_runs` | Written by the executor (PR C). |
| Wakeup requests | `company_wakeups` | Queue of assignment / mention / on-demand / approval wake-ups. |
| Approvals (hire, strategy) | `company_approvals` | hire_agent, strategy, plan, budget_override. |
| Activity log | `company_activity` | Every mutation. |
| Cost events, budget policies | Budget fields now; cost rows in PR B | See "Seams". |
| Board (the human) | The signed-in owner | Owner-scoped like every other Odysseus table. |

What deliberately does not carry over: adapters, agent API keys and JWTs
(agents run in-process, not as external CLIs calling back over HTTP),
company memberships (Odysseus is one owner per company), plugins, chat
channels, routines (Odysseus already has scheduled tasks).

---

## What already exists

| Need | Odysseus piece | State |
|---|---|---|
| Run a model with a system prompt and tools | `src/agent_loop.py`, `src/llm_core.py` | Works for chats and scheduled agent tasks |
| Resolve "model@Endpoint" for an owner | `src/ai_interaction.py::_resolve_model` | Works; probes the endpoint, owner-scoped |
| List every model an owner can pick | `GET /api/models` (`routes/model_routes.py::fetch_models`) | Works |
| Timers | `ScheduledTask` + the task scheduler | Works; one row per job |
| A workspace for a long-running job | Chat sessions, and side threads (`src/chat_threads.py`) | Works |
| Owner scoping | `src/auth_helpers.py` (`require_user`, `owner_filter`) | Works |
| Token accounting | Per-session totals only | Server-side cost tracking is PR B |

---

## What this PR builds (PR A: data model, API, UI)

### Tables

All in `core/database.py`. New tables only, so `init_db`'s `create_all` adds
them; no migration needed. Every table has its own indexed `owner` column.

- **company_companies**: name, description, status (active / paused /
  archived), `issue_prefix` (unique per owner, fixed once there are issues),
  `issue_counter`, `monthly_budget_cents`, `spent_cents`, `budget_month`,
  `require_approval_for_new_agents`.
- **company_goals**: `parent_id`, level, status (planned / active / achieved /
  cancelled), `owner_agent_id`.
- **company_projects**: `goal_id`, name, description, status (backlog /
  planned / in_progress / completed / cancelled), `lead_agent_id`,
  `target_date`, color.
- **company_agents**: name, role (ceo, cto, cmo, cfo, security, engineer,
  designer, pm, qa, devops, researcher, general), title, capabilities,
  `reports_to`, status (active / paused / idle / running / error /
  pending_approval / terminated), `model`, `instructions` (system prompt),
  `enabled_tools`, `disabled_tools`, `heartbeat` JSON (`enabled`,
  `interval_sec`, `wake_on_assignment`, `wake_on_mention`, `wake_on_demand`),
  budget fields, `last_heartbeat_at`, `session_id` (an Odysseus chat used as
  the agent's workspace).
- **company_issues**: `identifier` (`PREFIX-n`) and `number`, title,
  description, status, priority (critical / high / medium / low),
  `parent_id`, `project_id`, `goal_id`, `assignee_agent_id` or
  `assignee_user`, `created_by_type/id`, `checkout_run_id`,
  `execution_locked_at`, started / completed / cancelled timestamps.
- **company_labels**, **company_issue_labels**, **company_issue_relations**
  (type `blocks`).
- **company_issue_comments**: body, author type / id, `run_id`, `mentions`
  (agent ids).
- **company_activity**: actor type / id, action, entity type / id, agent,
  run, details JSON.
- **company_runs**: agent, issue, invocation source (timer / assignment /
  mention / on_demand / approval), status (queued / running / succeeded /
  failed / cancelled / timed_out), model used, session id, error, result
  summary, usage JSON, timestamps.
- **company_wakeups**: agent, source, issue / comment / approval, status
  (pending / consumed / dropped), the run that consumed it.
- **company_approvals**: type, title, requested by, status (pending /
  revision_requested / approved / rejected / cancelled), payload JSON (a
  hire carries the chosen model), decision note, decided by / at.

### Rules (src/company.py)

The rules live in a service module, not the routes, so the executor calls
the same functions and gets the same validation and activity log.

- **Org tree.** A CEO has no manager and there is at most one live CEO.
  Everyone else must report to a live agent in the same company. Changing a
  manager walks up the chain and refuses a loop. The CEO can't be demoted or
  terminated while it has reports; "Make CEO" puts another agent on top and
  the old CEO reports to it. Terminating a manager hands its reports to its
  own manager and unassigns its open issues.
- **Model spec.** Saved as given (`model@Endpoint Name`) after
  `_resolve_model(spec, owner)` succeeds. If the endpoint is unreachable at
  that moment, an exact match in that endpoint's cached model list is
  accepted, so a local box being down doesn't block editing.
- **Issue transitions.**

  | From | To |
  |---|---|
  | backlog | todo, in_progress, blocked, cancelled |
  | todo | backlog, in_progress, blocked, cancelled |
  | in_progress | todo, in_review, done, blocked, cancelled |
  | in_review | in_progress, todo, done, blocked, cancelled |
  | blocked | backlog, todo, in_progress, cancelled |
  | done | todo (reopen) |
  | cancelled | backlog, todo (reopen) |

  Anything else is a 422. Moving to in_progress stamps `started_at` once;
  done stamps `completed_at` (cleared on reopen); leaving in_progress drops
  any checkout lock. A person may move an issue to in_progress by hand (a
  board override); agents go through checkout.
- **Identifiers.** The counter is incremented in SQL (`issue_counter =
  issue_counter + 1`) in the same transaction as the insert, with a unique
  index on `(company_id, number)`, so parallel creates never collide.
- **Atomic checkout.** `POST /issues/{id}/checkout {agent_id, run_id,
  expected_statuses?}` is one conditional `UPDATE ... WHERE` the status is
  expected, the issue is unassigned or assigned to that agent, no person is
  assigned, and the lock is free. Free means no holder, the same run, a lock
  older than `STALE_LOCK_SECONDS` (30 minutes), or a holder run that has
  finished. Exactly one of several racing runs changes the row; the rest get
  409. A takeover is recorded in the activity details. Release is by the
  holding run, or forced by the person.
- **Mentions.** `@Name` (spaces allowed, any case) or `@name-with-hyphens`.
  The longest name wins; an `@` inside a word (an email) or a name running on
  into more letters doesn't count. Each mention queues a wake-up if the
  agent's policy allows it.
- **Wake-ups.** Assignment, mention, "Wake now" and an approval decision on
  an agent's request each add a `company_wakeups` row, unless the agent is
  paused, pending or terminated, or its heartbeat policy says no.
- **Approvals.** Deciding records the decision. The one effect applied now:
  a pending hire (an agent created by another agent while the company
  requires approval) becomes idle when approved and terminated when
  rejected. A hire made by the person directly needs no approval.
- **Activity.** Every create, update, delete, transition, checkout, release,
  comment and decision writes a `company_activity` row with the actor and
  the changes.

### API (routes/company_routes.py, all under `/api/company`)

| Area | Endpoints |
|---|---|
| Companies | `GET/POST /companies`, `GET/PATCH/DELETE /companies/{cid}`, `GET /companies/{cid}/dashboard` |
| Agents | `GET/POST /companies/{cid}/agents`, `GET /companies/{cid}/org`, `GET/PATCH/DELETE /agents/{aid}`, `POST /agents/{aid}/make-ceo`, `POST /agents/{aid}/wake` |
| Goals, projects, labels | `GET/POST /companies/{cid}/goals`, `PATCH/DELETE /goals/{gid}`, same for `projects`; `GET/POST /companies/{cid}/labels`, `DELETE /labels/{lid}` |
| Issues | `GET/POST /companies/{cid}/issues` (filters: status, assignee_agent_id, assignee_user, project_id, goal_id, priority, parent_id, label_id, q; sorted by priority, then most recently updated), `GET/PATCH/DELETE /issues/{iid}`, `POST /issues/{iid}/checkout`, `POST /issues/{iid}/release`, `POST /issues/{iid}/relations`, `DELETE /relations/{rid}` |
| Comments | `GET/POST /issues/{iid}/comments`, `DELETE /comments/{cmid}` |
| Feed and runs | `GET /companies/{cid}/activity`, `GET /companies/{cid}/runs`, `GET /runs/{rid}`, `GET /companies/{cid}/wakeups` |
| Approvals | `GET/POST /companies/{cid}/approvals`, `GET /approvals/{apid}`, `POST /approvals/{apid}/decide`, `POST /approvals/{apid}/cancel` |

Every route goes through `require_user`; every query is filtered by owner.
Another account's ids answer 404, and links (manager, assignee, project,
parent) must stay inside one company.

### UI (static/js/company.js, static/css/company.css)

Build > Company in the sidebar, `#company`, or a tab in Workspace. A company
switcher with create and settings, then:

- **Dashboard**: agents, open issues, approvals waiting, budget; agents and
  issues by status; recent activity.
- **Org chart**: the tree from the CEO down (an indented list on a phone).
  Hire or edit an agent: role, manager, a model picker listing every model
  from `GET /api/models` as `model@Endpoint`, instructions, capabilities,
  tools, heartbeat policy, budget, status; Make CEO, Wake now.
- **Issues**: a board with a column per status. Drag a card, or use its Move
  menu, which only offers allowed moves (disallowed columns dim while
  dragging). Filters for search, assignee, project and priority. A card
  opens its details: status, priority, assignee (an agent or me), project,
  goal, parent, labels, description, sub-issues, blocking links, and
  comments with @mention autocomplete.
- **Goals** (nested list), **Projects** (cards), **Activity** (the feed),
  **Approvals** (waiting or all; approve, ask for changes, reject, with a
  note).

### Company in chat (src/company_chat.py, src/agent_tools/company_chat_tools.py)

Any chat can mention the Company. Typing `@` in the composer (or an issue
prefix like `JAR`) opens a popup with companies, agents and issues
(`static/js/companyMentions.js`, fed by `GET /api/company/mentions`).

| Syntax | Means |
|---|---|
| `@Acme Labs`, `@acme-labs`, `@JAR` | a company (name, slug or issue prefix) |
| `@Ada`, `@CEO` | an agent, by name or role (the longest name wins) |
| `@JAR/CEO` | that company's CEO, when several companies have one |
| `JAR-1`, `@JAR-1` | an issue |

Sent messages show mentions as chips that open the Company page at the item
(in Workspace, its Company tab). A name that fits several things is settled
by the company the rest of the message names, else shown as ambiguous and
the model is told the candidates. "Discuss in chat" on an issue opens a new
chat with it mentioned.

For each mention the server adds a short, fresh snapshot to that turn's user
message only, inside `<company_context>`: an issue's status, priority,
assignee, description and last three comments; an agent's role, model,
status, open issues and last run; a company's goals, agents by status, open
issues by status and approvals waiting. Owner-scoped, capped at 5000 chars.

Chat tools act as the user (`Actor("user", owner)`) through the same
functions as the page: read `company_overview`, `company_issue_list`,
`company_issue_get`, `company_agent_status`; write `company_issue_create`,
`company_issue_update` (status, priority, assignee), `company_issue_comment`,
`company_agent_wake`, `company_approval_decide`. Write tools are blocked in
plan mode. The tools are hidden for an owner with no company and pulled in
when a message mentions the Company; a plain chat turn asking to change a
mentioned item is promoted to agent mode.

---

## Lifecycle

1. Create a company. Hire a CEO and pick its model.
2. Set goals; add projects under them.
3. Hire the rest of the org under the CEO, each on whatever model suits the
   role.
4. Open issues and assign them, or (with PR C) let the CEO break goals down
   and delegate.
5. Agents wake on a timer, an assignment, a mention or a click; each wake is
   a run that checks out one issue, works it in its own Odysseus session,
   comments, and moves it along.
6. Hires (when gated), strategies, plans and budget overrides come to the
   Approvals inbox.
7. Spend is tracked per run and rolled up per agent and company; a budget
   that runs out pauses the agent.

---

## Phases

- **PR A (this one)**: data model, rules, API, UI, tests. Nothing runs.
- **PR B (`cost-tracking`, in parallel)**: server-side usage and cost per
  model call (a `usage_events`-style table and a pricing module).
- **PR C**: the executor. Heartbeat scheduling, running an agent on its
  model with its instructions and tools, checkout and release around each
  run, comments and status changes as the agent, acting on approvals,
  budget enforcement.

## Seams for PR B and PR C

- **Service functions.** PR C calls `src/company.py` directly with
  `Actor("agent", agent_id, run_id)`. Comments, transitions and checkouts
  made that way are attributed to the agent and the run in the activity log.
- **Runs.** `create_run(db, owner, agent_id, source, issue_id=, model=)` and
  `finish_run(db, owner, run_id, status, error=, result_summary=, usage=)`
  exist; `finish_run` also releases any issue the run held. PR C sets
  `running`, `started_at`, `session_id` and `last_heartbeat_at`.
- **Checkout.** Today any `run_id` string is accepted when no matching
  `company_runs` row exists (so the API can be exercised before the
  executor). PR C should require a real, live run of that agent.
- **Wake-ups.** `company_wakeups` with `status = pending` is the queue. PR C
  consumes rows (set `consumed`, `run_id`, `consumed_at`), coalesces several
  for one agent into one run, and adds its own `timer` wake-ups from each
  agent's heartbeat policy.
- **Approvals.** `decide_approval` records the decision and wakes the
  requesting agent (`source = approval`). Applying a strategy or plan is PR C.
- **Budgets.** `monthly_budget_cents`, `spent_cents` and `budget_month` on
  companies and agents. Serializers already report `spent_cents` as 0 when
  `budget_month` is not the current month. PR B's usage rows should carry
  the run id (and so the agent and company); PR C adds each run's cost to
  the agent and company in the run's month and pauses an agent (status
  `paused`, activity `agent.budget_paused`) when it reaches its limit or the
  company's. `run.usage` holds the per-run summary for the UI.
- **Sessions.** `agent.session_id` and `run.session_id` point at Odysseus
  chats, so a run's transcript opens like any other chat (or a side thread
  of the agent's workspace chat).

## Open questions

- Should each run get a fresh side thread under the agent's workspace chat,
  or one long session per agent (and how to compact it)?
- Tool access: are `enabled_tools` / `disabled_tools` enough, or do agents
  need per-company sandboxes (working folder, allowed hosts)?
- Should the CEO be allowed to create issues and hire without approval when
  the company doesn't require it, or should strategy approval come first
  as in Paperclip?
- Cross-company work and shared agents: out of scope, or a later
  membership table?
- Stale lock window: 30 minutes fits local models; should it follow each
  agent's expected run length instead?
- Should a person be able to check out an issue for themselves (a human
  assignee working in the same board), beyond assigning it to "me"?
