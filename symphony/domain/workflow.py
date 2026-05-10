from __future__ import annotations

from typing import Any

STAGES: dict[str, dict[str, Any]] = {
    "planning": {"title": "需求规划", "dispatch": ["待规划"], "running": "规划中", "review": "需求确认", "confirm": "待方案设计", "request_changes": None, "editable_sections": ["background", "interaction", "history"]},
    "solution": {"title": "方案设计", "dispatch": ["待方案设计", "方案需修改"], "running": "方案设计中", "review": "方案评审", "confirm": "待拆解", "request_changes": "方案需修改", "editable_sections": ["solution", "validation", "interaction", "history"]},
    "breakdown": {"title": "任务拆解", "dispatch": ["待拆解", "拆解需修改"], "running": "拆解中", "review": "拆解评审", "confirm": "待实施", "request_changes": "拆解需修改", "editable_sections": ["task-breakdown", "validation", "history"]},
    "implementation": {"title": "代码实施", "dispatch": ["待实施", "代码需修改"], "running": "实施中", "review": "代码评审", "confirm": "待沉淀", "request_changes": "代码需修改", "editable_sections": ["history", "validation", "interaction"]},
    "learning": {"title": "经验沉淀", "dispatch": ["待沉淀", "沉淀需修改"], "running": "沉淀中", "review": "沉淀确认", "confirm": "已完成", "request_changes": "沉淀需修改", "editable_sections": ["learning", "history"]},
}

STATUS_TO_STAGE: dict[str, str] = {}
for _stage, _spec in STAGES.items():
    for _status in [*_spec["dispatch"], _spec["running"], _spec["review"]]:
        STATUS_TO_STAGE[_status] = _stage
STATUS_TO_STAGE.update({"已完成": "done", "已阻塞": "blocked"})
STATUS_TO_STAGE.update({"已失败": "failed"})

WAITING_USER_STATUSES = {spec["review"] for spec in STAGES.values()}
DISPATCHABLE_STATUSES = {status for spec in STAGES.values() for status in spec["dispatch"]}
RUNNING_STATUSES = {spec["running"] for spec in STAGES.values()}
ALL_TASK_STATUSES = [status for spec in STAGES.values() for status in [*spec["dispatch"], spec["running"], spec["review"]]] + ["已完成", "已阻塞", "已失败"]

ALLOWED_NEXT_STATUSES: dict[str, list[str]] = {
    "planning": ["需求确认", "已阻塞"],
    "solution": ["方案评审", "需求确认", "已阻塞"],
    "breakdown": ["拆解评审", "方案需修改", "已阻塞"],
    "implementation": ["代码评审", "已阻塞"],
    "learning": ["沉淀确认", "已阻塞"],
}

INPUT_LEDGER_RULES = """质量门禁：用户输入处理台账
- 每轮开始后必须先处理 PendingComments 和用户新增输入，并在交互区或交互历史区维护「用户输入处理台账」。
- 台账至少包含：输入 ID、来源、摘要、状态、处理结果、处理轮次。
- 输入 ID 优先使用 comment.id；正文补充可生成 U-001/R-001。
- 来源可写：用户评论、用户补充、评审反馈、Codebase MR 评论。
- 状态只能使用：待处理、已采纳、已否定、已转问题、已转方案、已转拆解、已转实施、已废弃、需澄清。
- 已处于终态的输入后续不得重复处理，除非用户新增评论明确要求重新打开。
- 信息不足时，状态必须改为「需澄清」或「已转问题」，并同步在交互区提出具体问题。"""

SOLUTION_QUALITY_GATE = """质量门禁：方案评审完整性
- 反馈不清或信息不足时，只允许提问和写明缺口，不要硬写完整方案。
- 信息足够时，方案必须包含目标、非目标、改动范围、核心设计、风险、验证方式、回滚或降级思路。
- 所有 pending 评论都必须逐条给出采纳/不采纳/需澄清结论，并更新评论状态。"""

BREAKDOWN_QUALITY_GATE = """质量门禁：拆解完整性
- 只能拆解已确认或足够清晰的方案；方案未确认或信息不足时，不要创建确定性任务。
- 每个任务必须包含：任务说明、依赖、优先级、预估耗时、验收标准、验证方式、阻塞项。
- 如果拆解发现方案缺口，建议下一状态应回到「方案需修改」或「已阻塞」。"""

IMPLEMENTATION_QUALITY_GATE = """质量门禁：代码实施与修改闭环
- 如果当前状态是「待实施」：按已确认任务拆解首次实现，开始前确认仓库路径、目标分支、禁止修改范围和验证方式。
- 如果当前状态是「代码需修改」：必须先读取 Artifact 中代码评审相关评论和交互历史；如果存在 MR 链接，还必须使用 codebase CLI 读取 MR 详情、review 状态、unresolved 评论和 diff 文件。
- codebase CLI 建议命令：codebase mr view -R <repo> -N <mr> --verbose；codebase mr status -R <repo> -N <mr>；codebase mr comment list -R <repo> -N <mr> --unresolved；codebase mr diff -R <repo> -N <mr> --name-only。
- 只处理 unresolved 评论；resolved/closed/outdated 不得驱动本轮修改。
- 每条 MR 评论必须进入用户输入处理台账，来源写「Codebase MR 评论」。
- 修复后必须回复处理结论，说明修改文件、验证结果或不采纳原因；如果确认闭环，尽力 resolve 评论，失败不阻塞但要记录原因。
- 无法读取 MR 评论且 Artifact 也没有明确反馈时，不得猜测修复，必须阻塞。"""

TDD_QUALITY_GATE = """质量门禁：TDD 与验证
- 写生产代码前必须加载并遵循 superpowers:test-driven-development。
- RED：先写或修改一个能暴露目标行为的测试，并确认它因目标问题失败。
- GREEN：写最小实现让测试通过。
- REFACTOR：仅在绿色状态下清理代码，不能引入行为变化。
- 记录测试文件、生产代码文件、验证命令、退出码和结果。
- 如果无法补测试，必须说明原因，并用最小可替代验证方案补足。
- backend_core 或 Go 代码变更禁止本地 go test / go build，必须使用 remote-ci-test，并记录远端 CI 命令、退出码、result_dir、status_file。"""

DEFAULT_QUALITY_GATES: dict[str, list[str]] = {
    "planning": [INPUT_LEDGER_RULES],
    "solution": [INPUT_LEDGER_RULES, SOLUTION_QUALITY_GATE],
    "breakdown": [INPUT_LEDGER_RULES, BREAKDOWN_QUALITY_GATE],
    "implementation": [INPUT_LEDGER_RULES, IMPLEMENTATION_QUALITY_GATE, TDD_QUALITY_GATE],
    "learning": [],
}

DEFAULT_STAGE_PROMPTS = {
    "planning": "探索任务背景，补充背景区与交互区，提出需要用户确认的问题。信息不足时不要猜测，必须把缺口写清楚。\n\n" + "\n\n".join(DEFAULT_QUALITY_GATES["planning"]),
    "solution": "基于背景、交互区和评论生成可评审方案，重点更新方案区、验证与风险区、交互区和历史区。\n\n" + "\n\n".join(DEFAULT_QUALITY_GATES["solution"]),
    "breakdown": "把已确认方案拆解成可执行任务列表、依赖关系、预估耗时和验收标准。\n\n" + "\n\n".join(DEFAULT_QUALITY_GATES["breakdown"]),
    "implementation": "按任务拆解执行代码实施，记录实施摘要、验证结果和代码评审待处理项。\n\n" + "\n\n".join(DEFAULT_QUALITY_GATES["implementation"]),
    "learning": "总结本任务的可复用经验，生成 Markdown 草稿和导出预览。",
}

DEFAULT_PROMPT_SECTIONS: dict[str, dict[str, Any]] = {
    "planning": {
        "stage_goal": "把用户原始需求整理成可确认的任务背景，识别缺失信息，并建立后续方案设计所需的事实基础。",
        "required_reads": ["任务标题、原始需求、补充资料", "artifact.html 的背景区、交互区、交互历史区", "未处理评论和待确认项"],
        "allowed_actions": ["更新背景区", "更新交互区的待确认问题", "登记用户输入和评论处理结果", "写入交互历史"],
        "forbidden_actions": ["不要生成最终技术方案", "不要修改代码", "不要删除用户评论或用户原始输入", "不要修改 Artifact runtime/style/nav"],
        "output_requirements": ["背景区包含任务摘要、已知事实、缺失信息", "交互区包含清晰的待用户确认问题", "交互历史记录本轮判断", "维护用户输入处理台账", "建议下一状态为需求确认或已阻塞"],
    },
    "solution": {
        "stage_goal": "生成可评审的 HTML 技术方案，吸收已确认输入，处理方案相关评论，并明确风险与验证方式。",
        "required_reads": ["背景区", "交互区", "方案区现有内容", "验证与风险区", "未处理方案评论"],
        "allowed_actions": ["更新方案区", "更新验证与风险区", "补充必要的任务拆解草案", "标记已处理评论状态", "写入交互历史"],
        "forbidden_actions": ["不要执行代码修改", "不要跳过未处理的关键评论", "不要直接把任务置为完成", "不要修改 Artifact runtime/style/nav"],
        "output_requirements": ["方案区包含目标、非目标、架构、数据结构、接口或流程", "验证与风险区包含验证计划、风险和回滚策略", "交互区说明仍需用户确认的问题", "逐条记录评论/反馈的采纳、拒绝或澄清结论", "建议下一状态为方案评审、需求确认或已阻塞"],
    },
    "breakdown": {
        "stage_goal": "把已确认方案拆成可执行的任务清单，明确依赖、优先级、预估耗时和验收标准。",
        "required_reads": ["方案区", "任务拆解区", "验证与风险区", "拆解相关评论"],
        "allowed_actions": ["更新任务拆解区", "补充验证与风险区中的验收标准", "写入交互历史", "处理拆解相关评论"],
        "forbidden_actions": ["不要修改已确认方案的核心结论", "不要执行代码修改", "不要删除 7 个主 section", "不要修改 Artifact runtime/style/nav"],
        "output_requirements": ["任务拆解区包含可执行任务、依赖、优先级、估时、验收标准", "风险区补齐验证责任和阻塞项", "发现方案缺口时写明回退原因", "建议下一状态为拆解评审、方案需修改或已阻塞"],
    },
    "implementation": {
        "stage_goal": "根据已确认任务拆解执行实现和验证，并把结果、风险和代码评审待处理项写回 Artifact。",
        "required_reads": ["任务拆解区", "验证与风险区", "交互历史区", "代码评审相关评论", "任务配置中的仓库路径和目标分支"],
        "allowed_actions": ["在目标仓库实施代码修改", "记录验证结果", "更新验证与风险区", "更新交互历史区", "处理代码评审反馈"],
        "forbidden_actions": ["不要在仓库路径未配置时猜测目标仓库", "不要无验证就声称完成", "不要重写方案区核心结论", "不要修改 Artifact runtime/style/nav"],
        "output_requirements": ["交互历史记录本轮改动摘要、修改文件、验证命令、退出码和结果", "验证与风险区更新剩余风险", "代码需修改时记录 MR 评论处理和回复/resolve 结果", "Go 远端 CI 记录 result_dir/status_file", "建议下一状态为代码评审或已阻塞"],
    },
    "learning": {
        "stage_goal": "从任务全过程中提炼可复用经验，形成可导出的 Markdown 草稿。",
        "required_reads": ["背景区", "方案区", "任务拆解区", "验证与风险区", "交互历史区", "经验沉淀区"],
        "allowed_actions": ["更新经验沉淀区", "整理可复用模式和踩坑", "生成 Markdown 导出预览", "写入交互历史"],
        "forbidden_actions": ["不要再修改代码", "不要改变已确认方案和实施结果", "不要导出未经用户确认的敏感信息", "不要修改 Artifact runtime/style/nav"],
        "output_requirements": ["经验沉淀区包含总结、可复用模式、踩坑和后续建议", "Markdown 草稿可被导出", "建议下一状态为沉淀确认或已阻塞"],
    },
}
