# LifeLoop AI
**Proactive, permission-based caregiving. It asks before it alarms.**

Built as a self-hosted caregiving assistant using the Model Context Protocol (MCP), Streamable HTTP, and a permission-based workflow.
A caregiver (Charitha in the demo) asks "How was Maya's day?" and gets a timeline, a plain-language summary, the evidence behind it, and what remains unknown. The agent asks the elder first, and nothing is "sent" or "ordered" without explicit approval.

> **Demo notice:** household events are **simulated**. No Ring hardware or real sensors are connected. No messages, orders or emergency calls are ever made. Not medical advice.

## Quick start (Python 3.9+, no dependencies)
```bash
python server.py            # then open http://127.0.0.1:8000
python -m unittest discover -s tests -v    # end-to-end MCP tests
python reset_demo.py        # fresh demo data (stop the server first)
```
Optional Amazon Bedrock summaries:
```bash
pip install boto3
export AWS_REGION=us-east-1
export BEDROCK_MODEL_ID=<a model id you have access to>   # e.g. an Anthropic or Amazon Nova model enabled in your account
python server.py
```
Without `BEDROCK_MODEL_ID` (or if the call fails) the summary uses a local fallback, and the UI says so ("Summary: local fallback"). It never claims Bedrock when Bedrock wasn't used.

## How MCP is used
- `static/index.html` is an MCP **client**: it POSTs `initialize` -> `notifications/initialized` -> `tools/list` -> `tools/call` to `http://127.0.0.1:8000/mcp`, with `Mcp-Session-Id` and `MCP-Protocol-Version` headers. Open "Live MCP traffic" in the dashboard to see real JSON-RPC messages.
- `lifeloop/mcp_server.py` is the MCP **server** (Streamable HTTP, single `/mcp` endpoint, JSON responses, session ids, Origin validation, `ping`, `tools/list`, `tools/call`, structured tool results, `isError` for tool failures).
- It is a small standard-library implementation of the protocol, not the official SDK, so it runs with zero installs. It can be driven by any MCP client that supports Streamable HTTP, e.g. `npx @modelcontextprotocol/inspector` -> URL `http://127.0.0.1:8000/mcp`.
- The dashboard only talks to the backend through MCP (the only extra route is `/api/health`).

## Dashboard (v1.1)
Sidebar app with five pages: **Overview** (today at a glance, latest check-in, small next steps, evidence-based daily summary, recent records), **Daily timeline** (source-labeled records + reminders and medication reports), **Review & approve** (pending proposals with a live count), **Consent & settings** (resident/caregiver names, check-in times, quiet hours, authorized access, notification preference, consent switch) and **Evidence & audit** (audit log, live MCP traffic, tools discovered via `tools/list`). A demo **SOS** button is always in the top bar.
Default demo people: caregiver **Charitha**, resident **Maya** (both editable in settings). If you ran an older build, the demo database is rebuilt automatically on first start.

## Tools (13)
`get_daily_summary`, `get_timeline`, `check_inactivity`, `record_checkin`, `log_medication`, `schedule_reminder`, `acknowledge_reminder`, `suggest_meal`, `draft_family_alert` (kinds: inactivity, missed_checkin, daily_update, sos), `approve_alert`, `get_settings`, `update_settings`, `get_audit_log`.

## Trust and safety design
| Rule | Where |
|---|---|
| A missing record is a question, not a conclusion | `check_inactivity` wording; "Ask the elder first" |
| Food is only suggested after the elder says they want it | `suggest_meal(elder_wants_food=true)` required |
| Nothing is sent or ordered without approval; approvals are idempotent | `approve_alert`, duplicate-pending checks |
| Approved actions use a demo adapter and say so | "No message was sent / No order was placed" |
| SOS is a draft only and never claims emergency services were contacted | `draft_family_alert(kind="sos")` |
| Medication reminders require caregiver-verified instructions; no dosing advice | `schedule_reminder` |
| Reminder acknowledgement and medication logs are reports, not verification | `acknowledge_reminder`, `log_medication` |
| Only contacts who consented receive alert drafts; quiet hours hold non-urgent alerts | settings, `draft_family_alert` |
| Every tool call and outcome is audit-logged | `get_audit_log` |

## Layout
`lifeloop/db.py` SQLite + simulated feed - `lifeloop/tools.py` MCP tools - `lifeloop/summarizer.py` Bedrock + fallback - `lifeloop/mcp_server.py` HTTP/MCP server + simulated scheduler - `static/index.html` dashboard - `tests/test_e2e.py`.

## Limits
Simulated event feed and demo clock (15:00) so the lunch-gap scenario reproduces at any time; simulated delivery adapters; single elder; no authentication (local demo only, binds to 127.0.0.1).

License: MIT.
