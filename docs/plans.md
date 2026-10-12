# Plans

Plans are Markdown files you keep in Odysseus: Organize > Plans (or `#plans`,
a tab in Workspace). Each plan is one file, `DATA_DIR/plans/<owner>/<title>.md`,
so the folder can be opened, backed up or edited with any other tool. A
deleted plan moves to the owner's `.trash` folder.

- Editor: `static/js/plans.js`, `static/js/plansText.js`, `static/css/plans.css`
- Store: `src/plans.py` (names checked against a strict pattern, every path
  confirmed to stay in the owner's folder, atomic writes)
- API: `routes/plans_routes.py`

```
GET    /api/plans?q=            the owner's plans, newest first; q searches titles and text
POST   /api/plans               create {"title": "...", "content"?: "..."}
GET    /api/plans/{name}        one plan with its text
PUT    /api/plans/{name}        save {"content": "...", "base_version"?: "..."}
POST   /api/plans/{name}/rename {"title": "..."}
DELETE /api/plans/{name}        moves it to the owner's .trash folder
```

A save made from an older copy (another tab, or the chat card below) gets a
409 instead of overwriting.

## Native plans: the chat model writes its plan here

Asked for: "i want it so that the model never uses anything external to make
the plans, only using the native markdown planner, so instead of sending it
out it writes the md file in the chat so that i can approve or deny it in the
chat."

A chat model makes a plan with one tool, `propose_plan`, and nothing else.

```propose_plan
{"title": "Add a dark mode", "plan": "## Goal\n...\n## Steps\n- [ ] ...\n## Files\n...\n## Risks\n...\n## On approval\nOpenCode in /home/me/site",
 "engine": "opencode", "cwd": "/home/me/site", "revises": "<plan_id>"?}
```

1. **Propose.** The plan is saved through `src/plans.py` as an ordinary Plans
   document (so it is in the Plans list, marked "Waiting for you"), and
   recorded in `DATA_DIR/native_plans.json` (`src/native_plans.py`) with the
   chat it belongs to, status `pending`, and the engine and folder a coding
   run would use. The turn ends there, like `ask_user`.
2. **The card.** The chat shows the plan's Markdown in a card with Approve,
   Deny (with an optional note) and Edit (`static/js/nativePlanCard.js`).
   Edit changes the same `.md` file, right in the card; "Open in Plans" opens
   it in the editor, and a save there redraws the card. The card asks the
   server for the plan's state each time it is drawn, so a reload, another tab
   or another layout (Classic, Studio, Workspace, phone) shows the same answer.
3. **Approve.** The status becomes `approved` with a timestamp, and the text
   approved (the file as it is at that moment, with your edits) is kept. The
   chat starts again by itself with "The user approved plan <title>; carry it
   out" and the approved Markdown as the spec. If coding is needed, the model
   calls `claude_code` with `action: "execute"` and the `plan_id`: a fresh run
   handed the approved text, with no planning phase of its own.
4. **Deny.** The status becomes `denied` with your note. The chat starts again
   with the denial and the note; the model either proposes a new version
   (`revises`, kept as "<title> (v2)", the earlier one stays) or stops.

Only one plan per chat waits at a time: a new one marks the older pending one
`superseded`.

```
GET  /api/native_plans?chat=<id>      a chat's plans, oldest first
GET  /api/native_plans/{id}           one plan: current text, status, answer
POST /api/native_plans/{id}/approve   approve and start the chat on it
POST /api/native_plans/{id}/deny      {"note"?: "..."}: deny and tell the chat
```

### What is enforced, and what is only asked

Enforced in code:

- `claude_code` has no `plan` action for a model. It is gone from the schema
  and the tool description, and a call with it (or with no action, which used
  to mean plan) is refused with a pointer to `propose_plan`. A read-only check
  sent as a plan still runs as `ask`.
- `claude_code` `execute` from a model needs an approved native plan for the
  same chat (`plan_id`), runs each approval once, and must use the folder the
  plan names. A run that never really ran (bad model name, stall, timeout,
  crash before output) gives the approval back. The only other execute that
  still runs is a coding agent's plan the user approved through its own
  Approve link before this change (`src/claude_code_approvals.py`).
- While a plan in the chat waits for an answer, delegation tools are refused:
  `claude_code` (except its pure reads `list`, `status`, `agents`, `attach`),
  `chat_with_model`, `ask_teacher`, `pipeline`, `send_to_session`,
  `create_session`, `spawn_subagent` and `branch_thread`.
- Only an authenticated request from the plan owner's page reaches approve and
  deny. Another account gets a 404.

Asked in the system prompt (`native_plans.RULE`): write plans only with
`propose_plan`, never with another model, a sub-agent, an MCP tool or a coding
agent. Each turn also says where the chat's newest plan stands (waiting, or
approved and not yet run, with its `plan_id`).

The coding agent's own plan phase (`action: "plan"`) is still in
`claude_code_tool.py` for code that acts on the user's own request and passes
`user_initiated` in its context. The model's tool path
(`src/tool_execution.py`) never passes it, and nothing in the app does today.

### Plan mode

Plan mode keeps `propose_plan` (it is in `PLAN_MODE_READONLY_TOOLS`), and its
directive tells the model to finish by calling it.

### Company plans

A Company agent asks for approval with `company_request_approval` type `plan`
as before, and its plan is also saved as a Plans document (source "company",
marked in the list). It is answered in Company > Approvals, not in a chat;
the decision is copied onto the Plans entry. Plans a person writes for an
agent in Company > Approvals are not copied.
