# Chris Brain API — Reference

Auto-generated from FastAPI OpenAPI spec at `/openapi.json`. Version: **2.1.0**.

Base URL: `http://127.0.0.1:8791`. All authenticated routes require `Authorization: Bearer <token>` from `~/.openclaw/credentials/.personal_webhook_secret`. Most read routes also accept `x-agent: <actor>` for action_audit attribution (M7-WS8).

Total routes: **111**.

## How to regenerate this file

```bash
SECRET=$(cat ~/.openclaw/credentials/.personal_webhook_secret)
curl -sf -H "Authorization: Bearer $SECRET" http://127.0.0.1:8791/openapi.json > /tmp/brain_openapi.json
python3 sdk/scripts/api_md_from_openapi.py > brain/API.md   # reuses this generator
```

## Routes by tag

### `admin`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/admin/restart` | Admin Restart |

### `atoms`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/atoms` | List Atoms |
| `GET` | `/brain/atoms/stats` | Atoms Stats |
| `GET` | `/brain/atoms/{atom_id}` | Get Atom Detail |
| `GET` | `/brain/review` | Brain Review |
| `POST` | `/brain/review/{chroma_id}` | Brain Review Grade |

### `audit`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/audit` | Audit List |
| `GET` | `/brain/audit/stats` | Audit Stats Endpoint |
| `POST` | `/brain/audit/{event_id}/review` | Audit Review |

### `autonomy`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/accuracy` | Brain Accuracy |
| `GET` | `/brain/autonomy` | Autonomy List |
| `GET` | `/brain/autonomy/{kind}` | Autonomy Get |
| `POST` | `/brain/autonomy/{kind}` | Autonomy Set |
| `GET` | `/brain/autopilot` | Get Autopilot |
| `POST` | `/brain/autopilot` | Set Autopilot |
| `GET` | `/brain/breakers` | Breakers List |
| `POST` | `/brain/breakers/{kind}/reset` | Breakers Reset |
| `GET` | `/brain/denylist` | Get Denylist |
| `POST` | `/brain/denylist/add` | Add Denylist Entry |
| `POST` | `/brain/denylist/remove` | Remove Denylist Entry |
| `GET` | `/brain/focus` | Get Focus |
| `POST` | `/brain/focus` | Add Focus |
| `DELETE` | `/brain/focus/{focus_id}` | Delete Focus |
| `GET` | `/brain/goals` | List Goals |
| `POST` | `/brain/goals` | Create Goal |
| `GET` | `/brain/goals/{goal_id}` | Get Goal |
| `POST` | `/brain/goals/{goal_id}/complete` | Complete Goal Route |
| `POST` | `/brain/goals/{goal_id}/decompose` | Decompose Goal Endpoint |
| `POST` | `/brain/index/rebuild` | Rebuild Canonical Index |
| `POST` | `/brain/ingest` | Brain Ingest |
| `GET` | `/brain/outcomes` | Brain Outcomes |
| `GET` | `/brain/policy/preview` | Autonomy Preview |
| `GET` | `/brain/procedures` | List Procedures |
| `GET` | `/brain/quiet-hours` | Get Quiet Hours |
| `POST` | `/brain/quiet-hours` | Set Quiet Hours |
| `GET` | `/brain/tasks` | List Tasks |
| `POST` | `/brain/tasks` | Create Task |
| `POST` | `/brain/tasks/dispatch` | Dispatch Ready Tasks |
| `POST` | `/brain/tasks/process` | Process Pending Tasks |
| `GET` | `/brain/tasks/{task_id}` | Get Task |
| `POST` | `/brain/tasks/{task_id}/approve` | Approve Task |
| `POST` | `/brain/tasks/{task_id}/complete` | Complete Task Route |
| `POST` | `/brain/tasks/{task_id}/reject` | Reject Task |
| `POST` | `/brain/tasks/{task_id}/start` | Start Task |
| `GET` | `/brain/trace/{note_id}` | Trace Provenance |
| `GET` | `/brain/triggers` | List Triggers Endpoint |
| `POST` | `/brain/triggers` | Create Trigger Endpoint |
| `DELETE` | `/brain/triggers/{trigger_id}` | Delete Trigger Endpoint |
| `PATCH` | `/brain/triggers/{trigger_id}` | Update Trigger Endpoint |

### `brain`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/changes` | Knowledge Changes |
| `GET` | `/brain/code/find` | Code Find |
| `GET` | `/brain/evolution` | Preference Evolution |
| `GET` | `/brain/lessons` | Get Lessons |
| `GET` | `/brain/schema-versions` | Get Schema Versions |
| `GET` | `/brain/search-quality` | Search Quality |
| `POST` | `/brain/self-heal/signal` | Emit Heal Signal |
| `GET` | `/brain/self-heal/status` | Self Heal Status |
| `GET` | `/brain/session/{session_id}/context` | Get Session Context |
| `POST` | `/brain/session/{session_id}/context` | Set Session Context |
| `GET` | `/brain/skills` | Discover Skills |
| `GET` | `/brain/timetravel` | Timetravel |
| `GET` | `/brain/todos` | Get Todos |
| `POST` | `/brain/todos` | Sync Todos |
| `GET` | `/brain/usage` | Brain Usage |

### `capture`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/capture/{source_type}` | Capture Generic |
| `POST` | `/health/ingest` | Capture Health |
| `POST` | `/location/ingest` | Capture Location |

### `coordination`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/brain/messages` | Send Agent Message |
| `GET` | `/brain/messages/{agent}` | Get Agent Messages |
| `POST` | `/brain/messages/{agent}/dismiss_all` | Dismiss All Messages |
| `POST` | `/brain/messages/{msg_id}/ack` | Ack Agent Message |
| `GET` | `/brain/session/{session_id}/active_agents` | Session Active Agents |

### `decide`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/brain/decide` | Brain Decide |
| `GET` | `/brain/insights` | Brain Insights |
| `GET` | `/brain/proactive` | Brain Proactive |
| `POST` | `/brain/proactive/{insight_id}/dismiss` | Dismiss Proactive |
| `POST` | `/brain/reason` | Brain Reason |
| `POST` | `/chris/think` | Chris Think |

### `eval`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/eval-proposals` | List Eval Proposals |
| `POST` | `/brain/eval-proposals` | Create Eval Proposal |
| `GET` | `/brain/eval-proposals/stats` | Eval Proposal Stats |
| `POST` | `/brain/eval-proposals/{proposal_id}/approve` | Approve Eval Proposal |
| `POST` | `/brain/eval-proposals/{proposal_id}/reject` | Reject Eval Proposal |

### `facts`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/facts` | Facts Query |
| `POST` | `/brain/facts` | Facts Store |
| `GET` | `/brain/facts/entity/{entity_name}` | Facts By Entity |
| `GET` | `/brain/facts/stats` | Facts Stats |

### `graph`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/graph/nodes` | Graph Nodes Endpoint |
| `GET` | `/brain/graph/stats` | Graph Stats Endpoint |

### `jobs`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/jobs` | List Jobs |
| `POST` | `/jobs/{job}` | Trigger Job |
| `GET` | `/jobs/{job}/history` | Job History |

### `learn`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/learn` | Learn Route |

### `liveness`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/health` | Brain Health |
| `GET` | `/healthz` | Healthz |

### `mcp`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/tools` | Brain Tools |

### `memory`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/memory` | List Memory |
| `POST` | `/memory` | Create Memory |
| `POST` | `/memory/batch` | Create Memory Batch |
| `GET` | `/memory/contradictions` | List Contradictions |
| `POST` | `/memory/contradictions/{contra_id}/resolve` | Resolve Contradiction |
| `POST` | `/memory/contradictions/{contra_id}/vote` | Vote On Contradiction |
| `GET` | `/memory/contradictions/{contra_id}/votes` | Get Contradiction Votes |
| `GET` | `/memory/export` | Export Memory |
| `DELETE` | `/memory/{mem_id}` | Delete Memory |
| `GET` | `/memory/{mem_id}` | Get Memory |
| `PATCH` | `/memory/{mem_id}` | Patch Memory |

### `metrics`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/eval-history` | Brain Eval History |
| `GET` | `/collections` | Collections |
| `GET` | `/metrics` | Metrics |

### `observability`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/brain/slos` | Get Slos |
| `POST` | `/brain/slos/check` | Trigger Slos Check |

### `profile`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/profile` | Profile |
| `GET` | `/profile/section/{name}` | Profile Section |

### `recall`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/boot-context/flush` | Boot Ctx Flush |
| `GET` | `/boot-context/{agent}` | Boot Ctx |
| `POST` | `/brain/reason/multihop` | Brain Reason Multihop |
| `POST` | `/brain/reason/multihop/{thread_id}/resume` | Brain Reason Multihop Resume |
| `GET` | `/recall` | Recall |
| `POST` | `/recall/feedback` | Search Feedback |
| `GET` | `/recall/v2` | Recall V2 |

### `synthesis`

| Method | Path | Summary |
|---|---|---|
| `GET` | `/synthesis/daily` | Synthesis Daily |
| `GET` | `/synthesis/monthly` | Synthesis Monthly |
| `GET` | `/synthesis/weekly` | Synthesis Weekly |

### `web`

| Method | Path | Summary |
|---|---|---|
| `POST` | `/web/search` | Web Search |
| `POST` | `/web/search/outcome` | Web Search Outcome |

