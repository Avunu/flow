<!-- Copyright (c) 2026, Shrihari Mahabal and contributors
For license information, please see license.txt-->

<div align="center">

# Flow

**AI agents for Frappe.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![CI](https://github.com/frappe/flow/actions/workflows/ci.yml/badge.svg)](https://github.com/frappe/flow/actions/workflows/ci.yml)

</div>

<img width="1470" height="923" alt="image" src="https://github.com/user-attachments/assets/59749f10-ccc7-49ce-aa30-9d0dc46c117a" />

---

Flow puts an AI assistant inside your Frappe site. It knows your DocTypes, respects your permissions, and can operate your site in plain English — creating records, sending emails, building DocTypes, creating agents and tools, and setting up RAG chatbots.

**Set up a support agent:**

> _"Create an agent that resolves customer support issues. It should use the Support Ticket doctype and these files as its knowledge: [attach files]"_

Flow sets up the RAG pipeline, embeds and indexes the files, and keeps the DocType source in sync — automatically.

**Put reporting on autopilot:**

> _"Every weekday at 9am, email me a summary of yesterday's new leads with their contact details and source."_

**Do anything a user can do:**

> _"Find all overdue sales orders from last month and mark them as closed."_

---

## Quick Access

You can open the global AI chat interface from anywhere in the Frappe Desk by pressing:

<kbd>Cmd</kbd> + <kbd>I</kbd> _(or <kbd>Ctrl</kbd> + <kbd>I</kbd> on Windows)_

---

## Framework

Flow also ships as a Python framework for building agents in code.

### DocTypes

| DocType                                         | Purpose                                                                   |
| ----------------------------------------------- | ------------------------------------------------------------------------- |
| `Flow Provider`                                 | Provider-level credentials and endpoint settings (including Cloudflare)   |
| `Flow Model`                                    | Model configuration — set a `model_id` like `anthropic/claude-sonnet-4-6` |
| `Flow Tool`                                     | Reusable tools, defined as a module path or inline script                 |
| `Flow Agent`                                    | Instructions, model, and tools — configurable from the Desk or in code    |
| `Flow Trigger`                                  | Run an agent on a DocType event or a schedule                             |
| `Flow Knowledge Base` / `Flow Knowledge Source` | Knowledge sources (files, URLs, DocTypes) for retrieval-augmented answers |
| `Flow Session` / `Flow Run`                     | Persisted conversations and execution history                             |

### Define a tool

```python
import frappe
from flow.lib import tool


@tool
def get_open_todos(allocated_to: str) -> list[dict]:
	"""List open ToDo items for a user."""
	return frappe.get_list(
		"ToDo",
		filters={"allocated_to": allocated_to, "status": "Open"},
		fields=["name", "description", "status"],
	)
```

### Run an agent

```python
from flow.lib import Agent

agent = Agent(
	model="anthropic/claude-sonnet-4-6",
	instructions="You help with Frappe tasks.",
	tools=[get_open_todos],
)

result = agent.run("List open ToDo items assigned to me")
print(result.output)
```

### Persist a conversation

```python
session = agent.new_session(title="Todo assistant")

session.chat("What tasks do I have open?")
session.chat("Close the ones due before last Friday.")
```

Every turn is saved as a `Flow Run` linked to the session — so conversation history, tool calls, and outputs are all in the database.

### Built-in tools

| Tool            | What it does                                                           |
| --------------- | ---------------------------------------------------------------------- |
| `find_doctypes` | Search for DocTypes by name or module                                  |
| `describe`      | Get the fields, filters, and available actions for a DocType or record |
| `read`          | Fetch records from any DocType                                         |
| `create`        | Create one or more records                                             |
| `update`        | Update fields across multiple records at once                          |
| `delete`        | Delete records                                                         |
| `run_action`    | Call a whitelisted method or workflow action on a record               |
| `execute`       | Run arbitrary Python in a sandboxed context                            |

Tool calls can require human confirmation before executing, and every run is persisted for audit.

---

## Installation

Install into an existing [bench](https://github.com/frappe/bench):

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app flow
bench --site site-name install-app flow
```

Then add a `Flow Provider` with your API key and a `Flow Model` with a model ID such as `anthropic/claude-sonnet-4-6`, and open the Desk Flow panel to start. For local providers like Ollama or LM Studio, set `Base URL` on the provider or model. For Cloudflare Workers AI, see [Cloudflare Workers AI](#cloudflare-workers-ai).

## Access

Using Flow needs the `Flow User` role, which the app creates on install and migrate. Flow Users read the enabled agents and models and keep their own sessions and runs; System Managers set Flow up. Sessions, runs and personal memories are private to their owner, so a System Manager sees another user's chats only when a trigger started them. A `Flow Agent` or `Flow Model` with roles under Access is only for users holding one of those roles; with none it is open to every Flow User, and User Permissions narrow both further. A trigger's Run As user (else its owner) must be able to read the trigger's agent and that agent's model, which the trigger checks when it is saved; System Managers always can. A share is the one way past the role: a System Manager can share a single record with a user who lacks the role.

Flow used to be open to every Desk User. `bench migrate` runs the `grant_flow_role` patch, which gives the role to every enabled System User so nobody loses access; remove it from the users who should not have it afterwards. Users whose roles come from role profiles get a `Flow User` role profile added, because Frappe drops roles outside a user's profiles when it saves them; a user holding roles their profiles do not grant gets a `Flow User (<user>)` profile for those roles too, so none is dropped. A user the patch cannot save is logged in the Error Log and skipped, and the Flow workspace is shown to Flow Users and System Managers only.

## Cloudflare Workers AI

Flow runs Cloudflare-hosted models (`@cf/...`, for example `@cf/zai-org/glm-5.3-flash`) and their embedding models directly. One Cloudflare account serves the whole site, so it is configured once, on a `Flow Provider` named `cloudflare`.

1. Create an API token in the Cloudflare dashboard (_My Profile > API Tokens > Create Token > Custom token_), limited to the one account, with exactly these permissions:

   | Permission                  | Needed for                                                    |
   | --------------------------- | ------------------------------------------------------------- |
   | Account > Workers AI > Read | Calling models and reading the model catalogue                |
   | Account > AI Gateway > Run  | Only if you set a Gateway ID and the gateway is authenticated |

   Never grant a Write permission: Flow does not need one. Note that AI Gateway Run lets a token use every gateway in the account.

2. Add a `Flow Provider` with Provider `cloudflare`, your **Account ID** (32 hexadecimal characters, on the account home page of the dashboard) and the token as **API Key**.
   - **Gateway ID** is optional. Calls work without it; set it to route requests through an [AI Gateway](https://developers.cloudflare.com/ai-gateway/) for logging and caching. The gateway stores request and response payloads by default; to stop that, put `{"extra_headers": {"cf-aig-collect-log-payload": "false"}}` in the provider's Extra Params (only `cf-aig-*` gateway controls are accepted as extra headers, and Cloudflare params are limited to request settings such as `temperature`, `max_tokens` and `reasoning_effort`: `headers` and `extra_body` are refused).
   - **Base URL** is an advanced override of `https://api.cloudflare.com/client/v4/accounts/<account>/ai/v1`. It must use https outside developer mode.
3. Press **Test Connection** (reads the model catalogue with your token, no inference is spent) and **Sync Models**.
4. Add a `Flow Model` with Provider `cloudflare` and a model such as `@cf/zai-org/glm-5.3-flash` (stored as `cloudflare/@cf/zai-org/glm-5.3-flash`). The endpoint and token come from the provider, so a Cloudflare model cannot carry its own. Embedding models such as `@cf/baai/bge-m3` work as the knowledge base's embedding model.

The model's **Context Window** is read from the Cloudflare catalogue when you add it or change its id, and you can type your own value. The form warns when the catalogue marks a model as deprecated or about to be, and shows the `reasoning_effort` values a model accepts (GLM 5.3 requires one of `max`, `high` or `low`; Flow rejects any other value in the model's Params).

Only Cloudflare-hosted models are supported on this provider. Other vendors keep using their own provider, key and base URL.

### Upgrading from the `openai/@cf/...` workaround

`bench migrate` runs the `migrate_cloudflare_models` patch. It moves models of the form `openai/@cf/...` that carry their own Cloudflare Base URL and API key onto the `cloudflare` provider (taking the account id from the old URL, clearing the per-model URL and key, and keeping the provider's token), and renames a provider row called `Cloudflare` to `cloudflare`. Model names and context windows are kept. A model whose Base URL is not a Cloudflare endpoint, that uses another Cloudflare account or gateway than the provider's, whose token differs from the provider's (a model's token never replaces the provider's) or that has its own extra headers is left as it was and logged by name; it keeps working until you edit its id. If your model's token is the working one, put it on the `cloudflare` provider first. After fixing the provider you can run the patch again with `bench --site <site> execute flow.patches.migrate_cloudflare_models.execute`.

## License

[GNU AGPLv3](license.txt)

Pieces ported from Flow in the Frappe framework are MIT-licensed by Frappe Technologies Pvt. Ltd.; see [NOTICE.md](NOTICE.md).
