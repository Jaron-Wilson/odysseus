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

- **PR A**: data model, rules, API, UI, tests. Nothing runs.
- **PR B (`cost-tracking`)**: server-side usage and cost per model call
  (`usage_events`, `src/usage.py`, `src/budgets.py`).
- **PR C (this one, `company-execution`)**: the executor. Agents run.

## PR C: agents run (src/company_executor.py, src/company_tools.py)

### The executor loop

Started with the app after the task scheduler (`app.py`), stopped first on
shutdown. Every 3 seconds, or at once when a route queues a wake-up:

1. **Timer heartbeats.** An agent (idle, active or error, in an active
   company) whose heartbeat is enabled gets a `timer` wake-up once
   `interval_sec` has passed since `last_heartbeat_at` (or since it was
   hired), unless one is already waiting.
2. **Claiming.** Pending wake-ups are grouped per agent and ALL of an
   agent's pending rows become one run (status `consumed`, `run_id` set).
   The run's `invocation_source` is the most important reason: approval,
   assignment, mention, on demand, timer. Paused, pending and terminated
   agents' wake-ups are dropped; a paused company's wait.
3. **Concurrency.** At most `company_max_concurrent_runs` runs at once
   (default 2, Company settings > "Agents running at once"), one per agent.
4. **Foreground gate.** A run on a LOCAL model (loopback, private, tailnet
   CGNAT, a bare LAN host name) waits for Odysseus to be quiet before it
   starts (`src/interactive_gate.py`, as scheduled tasks do), and between
   agent rounds waits again if the user came back, each wait capped at
   `COMPANY_GATE_MAX_WAIT_S` (60 s) so a busy open page can't hold an agent
   forever. A run the person asked for (Wake now, an approval decision)
   starts at once. Cloud models are not gated. `COMPANY_FOREGROUND_GATE=always|never` overrides. Company runs are
   flagged `background` in `src/agent_runs.py`, so they don't count as the
   user's activity (one agent doesn't block the next) and a restart doesn't
   carry them on as chats.
5. **Notifications** to the owner only: a plan / strategy / hire waiting for
   approval, a budget warning (once a month per agent or company) or stop,
   and a failed run. A web push to the owner's browsers, and a toast on an
   open Company page.

At startup, runs left `queued` or `running` are marked `interrupted` (a new
run status), their issue locks are released and running agents go idle.

### A run

1. Budgets are checked for the agent and the company (`budgets.check`);
   spend is copied onto their `spent_cents`. Hard stop: the agent is paused
   with `pause_reason`, its wake-ups are dropped, a `budget_override`
   approval opens, and nothing runs. Local and plan models cost $0 here, so
   they never stop a run.
2. The model spec (the approved plan's `chosen_model`, else the agent's) is
   resolved with `_resolve_model`. Failure fails the run; the agent shows
   `error` until the next good run.
3. **Workspace.** Answer to the open question: one workspace chat per agent
   (`agent.session_id`, filed in the "Company" folder), and a **fresh thread
   per run** under it (`run.session_id`). Why: a run's context stays small
   and bounded, which matters on 8-32K local models, so nothing has to be
   compacted; the durable memory is the issues and their comments, which
   every run reads back; each run's transcript opens on its own (Runs >
   Open as chat) and shows as a thread of the agent's chat; and runs never
   interleave in one history.
4. **Prompt.** System: the agent's instructions, its place in the org
   (company description, goals, manager, reports and their capabilities)
   and a short "how to work" protocol. User (saved in the thread): why it
   woke (assignment, the mention's comment, approval decision and note,
   heartbeat), any budget notice, the focus issue(s) with recent comments,
   and its open issues.
5. **Loop.** `stream_agent_loop` on the resolved model, as an `agent_runs`
   run in the thread, inside `usage_scope(source="company", company_id,
   company_agent_id, company_run_id)` so every model call is attributed.
   Tools: the agent's `enabled_tools` (plus the Company tools) when set, else
   the usual retrieval plus the Company tools; `disabled_tools` plus
   `ask_user` (nobody is there to answer). An approved plan is pinned with
   `approved_plan`; its `max_steps` caps rounds (default 20,
   `company_agent_max_rounds`). Between rounds: the run's own `max_cost_usd`,
   the agent's and company's budgets, and the foreground gate.
6. **Finish.** `finish_run` (status, summary, usage JSON with tokens, cost,
   rounds, tool calls) releases held issues; the summary is posted as a
   comment on the run's issue (or the one it checked out) unless the agent
   already commented there; the agent goes back to idle (or error); spend
   is copied onto the rows; activity `run.*` is logged.
7. **Stop.** `POST /api/company/runs/{id}/stop` stops a queued run's wait or
   a running agent loop (status `cancelled`, "Stopped by you").

### Company tools

Registered like other tools (`src/tool_schemas.py`, `src/agent_tools`
TOOL_TAGS, `TOOL_SECTIONS` for fenced-block models, `src/tool_execution.py`;
the mutating ones in `_PLAN_MODE_KNOWN_MUTATORS`). They act through
`src/company.py` as `Actor("agent", agent_id, run_id)` and only exist inside
a Company run (a context variable set by the executor; ordinary chats have
them disabled). Every lookup is limited to the agent's own company.

| Tool | What |
|---|---|
| `company_list_issues` | mine / team / unassigned / all, open by default |
| `company_get_issue` | details, parent, sub-issues, recent comments |
| `company_checkout_issue` | atomic checkout for this run |
| `company_update_issue` | validated transition (in_progress goes through checkout), priority, optional comment; only its own, its reports' or ones it created |
| `company_comment` | @mentions wake other agents |
| `company_create_issue` | managers only; assign to self or a report, optional parent |
| `company_request_approval` | plan / strategy (payload plan, issue), or hire_agent (managers; goes through `create_agent`, so the company's hire-approval setting applies) |
| `company_release_issue` | give up the checkout |

Checkout now requires a real, live (`queued` or `running`) run of that
agent; any other `run_id` is a 422 (unknown) or 409 (finished or another
agent's).

### Approvals

- **Plan / strategy.** Approve opens a dialog with a model picker listing
  every model from `GET /api/models` (local, OpenRouter, ChatGPT plan,
  anything configured), defaulting to the model last chosen for that
  agent's approved plans (`GET /approvals/{id}/defaults`), else the agent's
  model; plus max steps and max cost. Stored as `payload.chosen_model`,
  `max_steps`, `max_cost_usd`. The agent is woken and runs the pinned plan on
  that model. Reject and "ask for changes" wake it with the note.
- **Hire.** Approving activates the pending agent and wakes the manager.
- **Budget override.** Approve asks for the new monthly limit (0 = none),
  applies it to the scope that ran out and resumes and wakes the agent.

### Coding agents

Per agent: "Coding agent" (off by default, or OpenCode / Claude Code /
Antigravity) and a working folder (absolute, never inside the Odysseus
install). Off adds `claude_code` to the run's disabled tools; on, the run's
chat allows only that engine (`src/chat_prefs.py`) and the prompt names the
folder. A coding agent's plan approval appears in the run's chat (Runs >
Open as chat), not yet in the Company Approvals inbox. The CLI's own token
costs are not recorded in `usage_events`, so they don't count toward budgets.

### UI

Runs view (live list, a log per run polled while it runs, Stop, Open as
chat), running dots on agents, budget bars on agent cards, pause reason and
resume, Wake now, heartbeat and coding-agent settings in the agent form, the
approve-with-model dialog, plans shown in Approvals, and "Agents running at
once" in company settings. The page polls `GET /companies/{cid}/live` every
3 seconds (a passive path for the interactive gate).

### Follow-ups

- Surface coding-agent plan approvals in the Company inbox.
- Stream the run log over SSE instead of polling.
- Stale lock window per agent.

## Open questions

- (Answered in PR C: a fresh thread per run under one workspace chat per agent.)
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
