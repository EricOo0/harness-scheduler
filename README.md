# Harness Scheduler

Harness Scheduler 是一个本地 HTTP 调度服务。它使用 SQLite 管理任务元数据，使用每个任务独立的 `artifact.html` 作为用户和 Agent 共享的真实产物。

## Scope

- 本地任务管理，不依赖 Lark Base、Linear 或外部办公工具。
- 固定 5 阶段 Workflow：需求规划、方案设计、任务拆解、代码实施、经验沉淀。
- 每个任务一份 `.harness/tasks/{task_id}/artifact.html`。
- 评论、正文、评论状态和 runtime 都保存在 HTML 内。
- Scheduler 按状态自动捞取任务，不提供手动运行按钮。
- 当前 Agent 使用 mock adapter，不调用真实 Agent。

## Usage

启动服务：

```bash
python3 -m symphony.cli --host 127.0.0.1 --port 8765
```

兼容写法：

```bash
python3 -m symphony.cli harness --host 127.0.0.1 --port 8765
```

打开页面：

- `http://127.0.0.1:8765/tasks`
- `http://127.0.0.1:8765/workflow`
- `http://127.0.0.1:8765/skills`
- `http://127.0.0.1:8765/monitor`

指定数据目录：

```bash
python3 -m symphony.cli --data-dir /tmp/harness-data
```

## API

- `GET /api/tasks`
- `POST /api/tasks`
- `GET /api/tasks/{task_id}`
- `GET /api/tasks/{task_id}/artifact`
- `GET /tasks/{task_id}/artifact`
- `POST /api/tasks/{task_id}/artifact/save`
- `POST /api/tasks/{task_id}/artifact/reset-template`
- `POST /api/tasks/{task_id}/status`
- `POST /api/tasks/{task_id}/learning/export`
- `GET /api/tasks/{task_id}/runs`
- `GET /api/workflow`
- `PUT /api/workflow/stages/{stage}/prompt`
- `GET /api/skills`
- `POST /api/skills`
- `DELETE /api/skills/{skill_id}`
- `GET /api/runs`
- `GET /api/scheduler/health`

## Verification

```bash
python3 -m unittest discover -s tests -v
```
