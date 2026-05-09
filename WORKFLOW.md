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
agent:
  stale_running_recovery_ms: 1800000
  max_retry_attempts: 1
codex:
  command: PATH=<NODE_BIN_DIR>:$PATH codex app-server -c shell_environment_policy.inherit=all -c 'mcp_servers={}' -c 'sandbox_mode="danger-full-access"' -c 'approval_policy="never"'
  approval_policy: never
  thread_sandbox: danger-full-access
  read_timeout_ms: 60000
server:
  port: 8765
auth_monitor:
  enabled: true
  interval_ms: 600000
  warning_before_ms: 3600000
  system_table_id: <SYSTEM_TABLE_ID>
  system_record_id: <SYSTEM_RECORD_ID>
---
你正在处理飞书多维表格 Base 中的一条任务。Symphony 只会给你任务字段和 AI 交互文档链接；AI 交互文档正文、划词评论、全文评论必须由你在本轮开始后自行读取。

任务记录 ID：{{ issue.id }}
任务标题：{{ issue.title }}
调度来源状态：{{ issue.dispatch_state }}
运行状态：{{ issue.state }}

任务索引信息：
{{ issue.description }}

## 不可违反的边界

- 只使用 `<LARK_CLI_PATH>` 读写飞书 Base、云文档和评论，不要依赖 PATH 中的 `lark-cli`。
- 禁止使用任何 MCP 飞书工具，例如 `mcp__feishu__*`。
- 不要直接修改任务主表「状态」；状态由 Symphony 调度器从当前待处理态推进到运行态、交接态或阻塞态。
- 你的启动目录是 Symphony 临时 workspace。规划和方案阶段不得在这里创建需求产物、方案文件、测试文件或代码文件；实施阶段也必须先切到 AI 交互文档指定的目标仓库后再改代码。
- 所有写入 AI 交互文档、Base 字段、PR/MR 描述和最终回复的时间统一使用北京时间，格式为 `YYYY-MM-DD HH:mm:ss UTC+08:00`。不要写 UTC、CST 或本机时区缩写。
- AI 交互文档小节标题必须标注责任方：`（用户填写）`、`（Agent 填写）` 或 `（调度器填写）`。用户原始输入和 Agent 整理结论不要混在同一个小节。
- 如果信息不足，先把缺口写清楚并回填，不要猜测需求、仓库、分支、验证命令、Meego 单或 PR/MR 链接。
- 任务主表「Agent 建议下一状态」是单选字段。只允许写精确状态名：`需求确认`、`方案评审`、`代码评审`；不要写解释文本、多个状态或其它值。
- 对 Go 项目或 Go 单测，禁止本地运行任何形式的 `go test` 或 `go build`，也不要运行会间接触发它们的脚本或 make target。Go 单测必须走 Remote CI Test。

## 本轮固定执行顺序

1. 从任务索引信息中提取「AI 交互文档链接」。如果没有链接，先回填任务主表「Agent 交接说明」说明缺失，停止推进本轮。
2. 读取 AI 交互文档正文、划词评论和全文评论。正文或评论读取失败时，不要继续设计或实施；把失败原因写入「4.2 轮次记录（Agent 填写）」和任务主表「Agent 交接说明」。
3. 维护「2.5 用户输入处理台账（Agent 填写）」：登记新正文输入和新评论，跳过已处理输入，只把 `待处理`、`需澄清` 或用户明确重开的输入作为本轮 active input。
4. 基于 `调度来源状态` 判断本轮阶段，只执行该状态允许的动作。`运行状态` 是 Symphony 锁定任务后的运行态，不用于判断需求、方案或代码阶段。
5. 更新 AI 交互文档固定章节。固定章节用 `replace_range --selection-by-title` 更新原章节；历史记录只插入「4.2 轮次记录（Agent 填写）」。
6. 需要同步 Base 字段或任务子表时，使用 `base +record-upsert` 明确写回。
7. 结束前在最终回复中列出：本轮结论、已写回内容、验证结果或阻塞原因、用户下一步应该把任务状态改成什么。

## 上下文读取命令

读取 AI 交互文档正文：

```bash
<LARK_CLI_PATH> docs +fetch --as user \
  --doc '<AI 交互文档链接>' \
  --format json
```

读取评论：先从 AI 交互文档链接中提取 `/docx/<token>` 的 token，再分别读取划词评论和全文评论。
如果返回分页标记或 `has_more`，必须继续翻页直到读完；不能只处理第一页。

```bash
<LARK_CLI_PATH> drive file.comments list --as user \
  --params '{"file_token":"<docx token>","file_type":"docx","is_whole":false,"page_size":100}'

<LARK_CLI_PATH> drive file.comments list --as user \
  --params '{"file_token":"<docx token>","file_type":"docx","is_whole":true,"page_size":100}'
```

## AI 交互文档写回契约

AI 交互文档分为四个稳定区域：背景信息、交互区、方案、交互记录。固定章节只能更新原章节，禁止在文档末尾新增同名、近似同名或带“补充/追加”后缀的章节。

固定章节包括：

- `## 1. 背景信息`
- `### 1.1 任务摘要（调度器填写）`
- `### 1.2 原始需求（用户填写）`
- `### 1.3 参考资料（用户填写）`
- `### 1.4 执行配置（用户填写）`
- `## 2. 交互区`
- `### 2.1 当前待用户处理（Agent 提出 / 用户填写）`
- `### 2.2 用户补充说明（用户填写）`
- `### 2.3 Agent 本轮判断（Agent 填写）`
- `### 2.4 评审反馈（用户填写）`
- `### 2.5 用户输入处理台账（Agent 填写）`
- `## 3. 方案`
- `### 3.1 目标与非目标（Agent 填写）`
- `### 3.2 方案设计（Agent 填写）`
- `### 3.3 子任务拆解（Agent 填写）`
- `### 3.4 风险与验证（Agent 填写）`
- `### 3.5 PR / MR 与验证结果（Agent 填写）`
- `## 4. 交互记录`
- `### 4.1 状态流转记录（调度器填写）`
- `### 4.2 轮次记录（Agent 填写）`

写回规则：

- 更新固定章节时，优先使用 `docs +update --mode replace_range --selection-by-title '<章节标题>'`。
- 如果表格替换不稳定，才允许在原章节标题下用局部 `replace_range` 或 `insert_after` 修正内容，但仍然不能创建新的顶级或二级章节。
- 「2.2 用户补充说明（用户填写）」必须维护为用户追加表格，列为：`输入 ID`、`时间`、`类型`、`内容`、`关联问题/方案`、`状态`。用户新增信息时追加新行；不要直接覆盖历史行。
- 「2.4 评审反馈（用户填写）」必须维护为用户追加表格，列为：`输入 ID`、`时间`、`反馈对象`、`反馈内容`、`状态`。方案或代码评审意见都先进入这张表或评论，再由台账登记处理。
- 「2.5 用户输入处理台账（Agent 填写）」是正文输入和评论输入的统一处理状态表，列为：`输入 ID`、`来源`、`来源标识`、`发现时间`、`摘要`、`状态`、`处理结果`、`处理轮次`。
- 「4.1 状态流转记录（调度器填写）」是状态变化的固定位置，由调度器维护同一张表；不要在其它位置重复维护状态流转表。
- 「4.2 轮次记录（Agent 填写）」是唯一允许持续插入 Agent 历史记录的区域。每轮都用 `insert_after --selection-by-title '### 4.2 轮次记录（Agent 填写）'` 插入精简轮次记录，不要使用 `append`。
- 没有内容的小节写「无」。
- 正式写入真实信息后，必须清除模板占位、示例行和假数据，例如 `待填充`、`示例：...`、`第 N 轮`、`YYYY-MM-DD HH:mm:ss UTC+08:00`、空白示例表格行。

固定章节替换示例：

```bash
<LARK_CLI_PATH> docs +update --as user \
  --doc '<AI 交互文档链接>' \
  --mode replace_range \
  --selection-by-title '### 3.2 方案设计（Agent 填写）' \
  --markdown '### 3.2 方案设计（Agent 填写）\n\n...'
```

轮次记录插入示例：

```bash
<LARK_CLI_PATH> docs +update --as user \
  --doc '<AI 交互文档链接>' \
  --mode insert_after \
  --selection-by-title '### 4.2 轮次记录（Agent 填写）' \
  --markdown '#### 第 1 轮（Agent 填写，2026-05-06 18:30:00 UTC+08:00）\n\n...'
```

轮次记录只保留影响决策的信息：

```md
#### 第 1 轮（Agent 填写，2026-05-06 18:30:00 UTC+08:00）

| 字段 | 内容 |
| --- | --- |
| 本轮状态 | 待方案设计 |
| 新增输入 | 用户补充了仓库路径，但缺少验证命令 |
| 本轮判断 | 信息不足，不能进入方案评审 |
| 已更新内容 | 更新当前待用户处理、Agent 本轮判断、任务主表 Agent 建议下一状态 |
| 建议下一状态 | 需求确认 |
| 下一步 | 用户补充验证命令后把任务状态改为待方案设计 |
```

交互区维护规则：

- 「2.1 当前待用户处理」的问题表状态只能使用：`待回复`、`已确认`、`已否定`、`已部分确认`、`已废弃`、`已转方案`。
- 每轮读取用户正文和评论后，先维护「2.5 用户输入处理台账」，再维护已有问题状态，最后决定是否新增问题。
- 用户明确同意的问题改为 `已确认`；明确反对的问题改为 `已否定`；只回答一部分的问题改为 `已部分确认` 并拆出新的待回复问题；已经吸收到方案的问题改为 `已转方案`。
- 用户已经回复后，不允许继续停留在 `待回复`。
- 「2.3 Agent 本轮判断」必须写明：本轮状态、信息是否足够进入下一阶段、判断依据、建议下一状态、用户下一步。

用户输入处理台账规则：

- 「2.5 用户输入处理台账」状态只能使用：`待处理`、`已采纳`、`已否定`、`已转问题`、`已转方案`、`已转实施`、`已废弃`、`需澄清`。
- 正文输入来源包括「2.2 用户补充说明」和「2.4 评审反馈」。如果用户在正文表格新增 `输入 ID`，台账用该 ID 登记；如果用户没有填写 ID，Agent 为本轮新行分配稳定 ID，例如 `U-001` 或 `R-001`，并回填到原表格。
- 评论输入以 `comment_id` 作为唯一去重标识。划词评论来源写 `划词评论`，全文评论来源写 `全文评论`；来源标识写 `comment_id=<id>`。没有 `comment_id` 时，使用 `来源 + 创建时间 + 内容摘要` 作为弱标识，并在处理结果里说明。
- 已处于 `已采纳`、`已否定`、`已转问题`、`已转方案`、`已转实施`、`已废弃` 的输入，后续轮次不得再次作为新增输入处理；除非用户新增一条正文输入或评论明确要求重新打开。
- 处理完成后，必须把 `待处理` 改成明确结果，不能长期停留。信息不足时改为 `已转问题` 或 `需澄清`，并同步更新「2.1 当前待用户处理」。
- 用户修改历史正文行不视为可靠新增输入；需要用户追加一行修正说明。Agent 发现历史行疑似被改动时，必须把风险写入「2.3 Agent 本轮判断」，并要求用户追加修正行。
- 处理评论后，可以尽力使用 lark-cli 回复评论或标记评论已解决，说明处理结论和落点；如果当前 lark-cli 不支持、权限不足或调用失败，只记录到「2.5 用户输入处理台账」和「4.2 轮次记录」，不要因此阻塞主流程。

## 状态动作

| 调度来源状态 | 本轮目标 | 允许动作 | 必须写回 | 成功目标状态 |
| --- | --- | --- | --- | --- |
| 待规划 | 初始化需求确认 | 检查文档结构，整理原始需求，提出必须补充的信息 | 背景信息、交互区、用户输入处理台账、轮次记录 | 需求确认 |
| 待方案设计 | 形成可评审方案，或判断信息不足 | 信息足够时写方案；信息不足时只提问和建议回需求确认 | 交互区、用户输入处理台账、方案区或当前待用户处理、任务子表、Agent 建议下一状态、轮次记录 | 方案评审或需求确认 |
| 方案需修改 | 按反馈修正方案，或判断反馈不清 | 反馈清楚时修订方案；反馈不清时只提问和建议回需求确认 | 交互区、用户输入处理台账、方案区或当前待用户处理、任务子表、Agent 建议下一状态、轮次记录 | 方案评审或需求确认 |
| 待实施 | 按已确认方案实现 | 切到目标仓库，TDD 改代码，验证，创建/更新 MR/PR | 用户输入处理台账、PR/MR 与验证结果、轮次记录、任务子表、任务主表 PR 链接 | 代码评审 |
| 代码需修改 | 按代码评审反馈修正 | 读取评审反馈和评论，TDD 修代码，验证，更新 MR/PR | 用户输入处理台账、PR/MR 与验证结果、轮次记录、任务子表、任务主表 PR 链接 | 代码评审 |

## 各阶段要求

### 待规划

- Symphony 通常已经创建或初始化 AI 交互文档；你只需要检查固定章节是否完整并补齐必要占位。
- 不进入完整方案设计，不创建子任务，不实施代码。
- 必须明确列出用户需要补充的信息，尤其是执行配置：仓库地址、本地仓库路径、基准分支、工作分支、目标目录、禁止修改范围、验证命令。
- 信息缺失时，更新「2.1 当前待用户处理」和「2.3 Agent 本轮判断」；不要把问题只写在最终回复里。

### 待方案设计 / 方案需修改

- 先确认原始需求、补充说明、评论、评审反馈和「2.5 用户输入处理台账」中的 active input 是否足够支撑方案。
- 信息足够时，方案必须写清楚目标、非目标、改动范围、风险、验证方式和回滚或降级思路。
- 信息足够时，子任务拆解必须同步维护任务子表，字段至少包括：所属任务、子任务标题、子任务说明、类型、状态、验证方式。
- 信息不足或反馈不清时，不写完整方案，不创建确定性子任务；更新「2.1 当前待用户处理」和「2.3 Agent 本轮判断」，任务主表写入「Agent 建议下一状态」=`需求确认`，任务主表「Agent 交接说明」写明用户需要补充什么。
- 信息足够进入评审时，任务主表「Agent 建议下一状态」可以写 `方案评审` 或保持为空，调度器默认会进入方案评审。
- 调度器只接受方案阶段建议进入 `需求确认` 或 `方案评审`；不要建议其它状态。

### 待实施 / 代码需修改

- 实施前必须从 AI 交互文档「执行配置（用户填写）」读取本地仓库路径。路径为空、目录不存在或不是目标工程时，不要改代码；写回阻塞原因。
- 切到目标仓库后，先执行 `pwd` 和 `git rev-parse --show-toplevel` 确认位置，再创建或切换工作分支。
- 严格遵守执行配置中的仓库地址、基准分支、工作分支、目标目录、禁止修改范围和验证命令。
- 写任何生产代码前，必须加载并遵循 `superpowers:test-driven-development` skill：先写一个能暴露目标行为的失败测试，确认失败原因正确，再写最小实现使测试通过，最后只在绿色状态下清理。
- 如果本轮是 Go 代码变更，TDD 的 RED/GREEN 验证不能使用本地 `go test` 或 `go build`；必须加载并使用 `remote-ci-test` skill，通过远端 CI 跑包级、单函数或 testify suite case 测试。
- 创建或更新 GitLab PR/MR 前，必须从任务说明、AI 交互文档正文和评论中查找 Meego 单号或链接。
- 找到 Meego 单时，PR/MR 必须关联该 Meego 单，并在「PR / MR 与验证结果（Agent 填写）」记录关联关系。
- 没有 Meego 单时，PR/MR 标题必须追加 `--no-meego`。
- 必须创建或更新 GitLab MR/PR，并把有效链接写回任务主表「PR 链接」。没有 PR/MR 链接时，Symphony 会阻止任务进入「代码评审」。
- 修改代码后必须记录文件列表、验证命令和验证结果；验证不能执行时说明原因。

代码评审反馈读取规则：

- 调度来源状态为 `代码需修改` 时，除 AI 交互文档正文、划词评论和全文评论外，必须从任务主表「PR 链接」或 AI 交互文档「3.5 PR / MR 与验证结果（Agent 填写）」提取 Codebase MR 链接。
- 解析出 repo 和 MR number 后，直接使用 `codebase` CLI 读取评审信息。先执行 `codebase auth status` 检查登录态；未登录或登录无效时，不要猜测修复，必须阻塞并写清认证失败原因。
- 读取范围必须覆盖 MR 详情、review 状态、unresolved MR 评论和 MR diff 文件。使用：
  - `codebase mr view -R <repo> -N <mr> --verbose`
  - `codebase mr status -R <repo> -N <mr>`
  - `codebase mr comment list -R <repo> -N <mr> --unresolved`
  - `codebase mr diff -R <repo> -N <mr> --name-only`
- 只把 unresolved MR 评论作为本轮 active input。已 resolved / closed / outdated 的 MR 评论不得驱动本轮修复；如需核对历史评论，可用 `codebase mr comment list -R <repo> -N <mr> --resolved`，但只能用于确认已闭环状态。
- MR 评论必须登记到「2.5 用户输入处理台账（Agent 填写）」，来源写 `Codebase MR 评论`，来源标识至少包含 repo、MR number 和 thread id / comment id。优先使用 `repo + mr number + thread id` 去重。
- 已登记且状态为 `已采纳`、`已否定`、`已转问题`、`已转方案`、`已转实施`、`已废弃`、`已解决` 的 MR 评论，后续轮次不得重复处理；除非用户或 reviewer 新增 unresolved 评论明确要求重新打开。
- 处理 MR 评论后，必须使用 `codebase mr comment reply -R <repo> -N <mr> --thread-id <thread-id> --body '<处理结论>'` 回复处理结论，说明是否已修、修改文件、验证结果或不采纳原因。
- 修复并回复后，如果确认该线程已闭环，必须尽力使用 `codebase mr comment resolve -R <repo> --id <thread-id>` 标记已解决；如果权限不足或调用失败，不阻塞主流程，但必须写入「2.5 用户输入处理台账」的处理结果和「4.2 轮次记录（Agent 填写）」。
- 如果无法读取 MR 评论，但 AI 交互文档已有足够明确的代码修改意见，可以继续按 AI 文档修复，并记录 MR 评论读取失败原因；如果 AI 文档也没有足够明确的修改意见，不得猜测修复，必须阻塞并写清失败原因。
- 如果调度来源状态为 `代码需修改` 但没有可解析的 PR/MR 链接，必须阻塞，要求用户补充 PR/MR 链接。

Go 单测远端 CI 命令格式：

```bash
python3 <REMOTE_CI_TEST_SCRIPT> <package-or-test-file> [test-name]
```

示例：

```bash
python3 <REMOTE_CI_TEST_SCRIPT> ./application/handler/workspace
python3 <REMOTE_CI_TEST_SCRIPT> ./application/handler/workspace TestListWorkspaceObjs
python3 <REMOTE_CI_TEST_SCRIPT> ./application/handler/workspace TestWorkspaceServiceTestSuite/TestListWorkspaceObjs
```

记录验证结果时，必须写明远端 CI 命令退出码，以及输出中的 `result_dir` / `status_file` 路径。

## Base 写回命令

更新任务主表字段：

```bash
<LARK_CLI_PATH> base +record-upsert --as user \
  --base-token <LARK_BASE_TOKEN> \
  --table-id <TASK_TABLE_ID> \
  --record-id {{ issue.id }} \
  --json '{"PR 链接":"https://...","Agent 交接说明":"本轮完成...","Agent 建议下一状态":"需求确认"}'
```

新增或更新任务子表记录：

```bash
<LARK_CLI_PATH> base +record-upsert --as user \
  --base-token <LARK_BASE_TOKEN> \
  --table-id <SUBTASK_TABLE_ID> \
  --json '{"子任务标题":"...","所属任务":[{"id":"{{ issue.id }}"}],"子任务说明":"...","类型":"代码","状态":"待执行","验证方式":"..."}'
```

## 完成标准

本轮结束前必须满足：

- 已读取 AI 交互文档正文、划词评论和全文评论，或已写回读取失败原因。
- 已维护「2.5 用户输入处理台账」：新正文输入和新评论已登记，已处理输入不再作为本轮新增输入，active input 有明确处理结果或下一步。
- 已按调度来源状态完成允许范围内的动作，没有跨阶段实施。
- 如果调度来源状态是 `代码需修改`，已读取 Codebase MR 详情、review 状态、MR 评论和 MR diff 文件，或已写清不能读取的原因及是否因此阻塞。
- 已更新必要的固定章节，并把本轮历史记录插入「4.2 轮次记录（Agent 填写）」。
- 已同步必要的 Base 主表字段和任务子表。
- 没有新增重复章节、追加章节或 Symphony 临时 workspace 产物。
- 如果修改代码，已写明文件、TDD 执行情况、验证命令、验证结果和 PR/MR 链接。
- 如果是 Go 代码变更，验证记录中不得出现本地 `go test` 或 `go build`；必须记录 Remote CI Test 命令、退出码、`result_dir` 和 `status_file`。
- 如果阻塞，已写清楚根因、缺失信息和用户下一步动作。
