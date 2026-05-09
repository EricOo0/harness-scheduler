# Symphony Self

This repository contains a Python implementation of the
[OpenAI Symphony service specification](https://github.com/openai/symphony/blob/main/SPEC.md).

## Scope

Implemented core pieces:

- `WORKFLOW.md` discovery, YAML front matter parsing, strict prompt rendering, defaults, validation, and mtime reload.
- Lark Base task reader for candidates, terminal-state sweep, state transitions, Agent-suggested next-state decisions, AI collaboration document creation, and document/comment context loading.
- Legacy Linear-compatible issue reader remains available behind `tracker.kind: linear`.
- Per-issue workspace management with sanitized identifiers, root containment checks, and lifecycle hooks.
- Single-authority in-memory orchestrator with polling, dispatch, reconciliation, stall detection, retries, and runtime snapshots.
- Local Codex app-server JSON-RPC subprocess client using `bash -lc <codex.command>` from the per-issue workspace.
- Structured JSON logs and an optional loopback HTTP status/control surface.
- Optional `linear_graphql` dynamic tool for app-server sessions when `tracker.kind: linear` is configured.

Not implemented as first-class orchestrator behavior:

- Tracker writes such as comments, state transitions, or PR links. Per the spec, those belong in the workflow prompt and agent tooling.
- Durable retry/session recovery after process restart.
- Remote SSH worker extension.

## Safety Posture

This implementation is intended for trusted local automation. Hooks are trusted shell scripts from `WORKFLOW.md`.
The configured Codex command runs only with `cwd` set to the issue workspace, and the workspace path must remain
under `workspace.root`.

Implementation-defined policy choices:

- Command execution approvals are auto-approved for the session with `acceptForSession`.
- File-change approvals are auto-approved for the session with `acceptForSession`.
- `item/tool/requestUserInput` is treated as a hard failure for the active turn; the service does not wait indefinitely.
- Unsupported server requests or dynamic tools receive structured failure responses instead of stalling.
- `codex.approval_policy`, `codex.thread_sandbox`, and `codex.turn_sandbox_policy` are passed through to the app-server protocol when configured.
- Runtime logs, Base status timestamps, and AI interaction document records are written in Beijing time (`UTC+08:00`) for user-facing consistency.
- AI interaction document sections should explicitly mark ownership, for example `（用户填写）`, `（Agent 填写）`, or `（调度器填写）`.
- The AI interaction document template is organized into four stable areas: background information, interaction area, solution, and interaction records. Template placeholders and sample rows should be removed once real information is written.
- The interaction area tracks pending user questions with explicit statuses such as `待回复`, `已确认`, `已否定`, `已部分确认`, `已废弃`, and `已转方案`.
- The interaction area also maintains `2.5 用户输入处理台账（Agent 填写）`, a single ledger for user-added document rows and document comments. The agent uses it to identify new feedback, mark handled inputs as done, and avoid reprocessing old comments.
- For Lark Base tasks, the scheduler passes Base fields and the AI interaction document link into the prompt; the agent must read the document body, inline comments, and whole-document comments at the start of each run. In `代码需修改`, the agent must use the `codebase` CLI to read MR details, review status, unresolved MR comments, and MR diff files, then register unresolved comments in the same ledger. MR comment reply/resolve is done through `codebase mr comment reply` and `codebase mr comment resolve`; the ledger remains the source of truth when comment operations fail.
- Lark Base task table must include `Agent 建议下一状态` as a single-select field. Allowed values are the exact state names `需求确认`, `方案评审`, and `代码评审`; the scheduler validates the value against the source state and clears it in the same Base update that moves the task state.
- This implementation does not add an external OS/container sandbox beyond the selected Codex policy and host OS controls.

Secrets are resolved only when config values explicitly reference `$VAR_NAME`. Tokens are validated for presence but not logged.

## Usage

Create a `WORKFLOW.md`:

```md
---
tracker:
  kind: lark_base
  base_token: <LARK_BASE_TOKEN>
  task_table_id: <TASK_TABLE_ID>
  subtask_table_id: <SUBTASK_TABLE_ID>
  doc_template_url: <DOC_TEMPLATE_URL>
  cli_command: <LARK_CLI_PATH>
  dispatch_states:
    - 待规划
    - 待方案设计
    - 方案需修改
    - 待实施
    - 代码需修改
  running_states:
    - 规划中
    - 方案设计中
    - 实施中
  handoff_states:
    - 需求确认
    - 方案评审
    - 代码评审
  terminal_states:
    - 已完成
    - 已取消
  start_state_by_dispatch:
    待规划: 规划中
    待方案设计: 方案设计中
    方案需修改: 方案设计中
    待实施: 实施中
    代码需修改: 实施中
  success_state_by_dispatch:
    待规划: 需求确认
    待方案设计: 方案评审
    方案需修改: 方案评审
    待实施: 代码评审
    代码需修改: 代码评审
  failure_state_by_dispatch:
    待规划: 已阻塞
    待方案设计: 已阻塞
    方案需修改: 已阻塞
    待实施: 已阻塞
    代码需修改: 已阻塞
polling:
  interval_ms: 30000
workspace:
  root: ./workspaces
codex:
  command: PATH=<NODE_BIN_DIR>:$PATH codex app-server -c shell_environment_policy.inherit=all -c 'mcp_servers={}' -c 'sandbox_mode="danger-full-access"' -c 'approval_policy="never"'
  approval_policy: never
  thread_sandbox: danger-full-access
server:
  port: 8765
auth_monitor:
  enabled: true
  interval_ms: 600000
  warning_before_ms: 3600000
  system_table_id: <SYSTEM_TABLE_ID>
  system_record_id: <SYSTEM_RECORD_ID>
---
你正在处理飞书 Base 任务 {{ issue.identifier }}。

Title: {{ issue.title }}
Description:
{{ issue.description }}
```

For Lark Base mode, the task table needs these fields by default: `标题`, `任务说明`, `状态`, `参考资料链接`, `AI 交互文档链接`, `PR 链接`, `Agent 交接说明`, `Agent 建议下一状态`, `最近错误`, and `阻塞原因`. `Agent 建议下一状态` should be a single-select field with options `需求确认`, `方案评审`, and `代码评审`.

Run:

```bash
python3 -m symphony.cli WORKFLOW.md
```

Optional dashboard/API:

```bash
python3 -m symphony.cli WORKFLOW.md --port 8765
```

Then open `http://127.0.0.1:8765/` or `GET /api/v1/state`.

API endpoints:

- `GET /api/v1/state`
- `GET /api/v1/<issue_identifier>`
- `POST /api/v1/refresh`

## Lark Auth Monitoring

常规业务提醒建议用 Base Workflow 实现，例如监听「任务」表状态进入 `方案评审`、`代码评审`、`已阻塞` 后发送飞书消息。

后台只负责 `lark-cli` 登录态到期预警：

- 定期执行 `lark-cli auth status`。
- 当访问令牌剩余时间低于 `auth_monitor.warning_before_ms` 时，写入「系统状态」表的 `登录态即将过期`。
- Base Workflow 可以监听「系统状态」表并提醒用户重新执行 `lark-cli auth login`。
- 如果登录态已经失效导致 Base 无法写入，后台只记录日志和状态页，不再尝试发送飞书消息。

## Verification

This repo uses Python `unittest` so it does not require pytest:

```bash
python3 -m unittest discover -s tests -v
```
