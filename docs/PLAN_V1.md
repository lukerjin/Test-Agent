# Universal Debug Agent - V1 Implementation Plan


## Context

需要构建一个基于 OpenAI Agents SDK (`openai-agents`) 的通用 debug/investigation agent，用于多个不同项目的问题复现、排查、证据收集和根因分析。V1 聚焦只读调查能力，不做自动修复。

## 设计决策

- **方案 B: 单 Agent + Orchestrator 动态 prompt 注入**
- 只有一个 Agent (Brain)，默认 ReAct 模式
- Python 代码 (StuckDetector) 通过 RunHooks 监控 tool call 历史
- 检测到卡住时，Orchestrator 中断当前 run，**追加分析 prompt** 后重新启动 Agent
- 分析 prompt 指令 Agent 停止调工具，基于已收集证据做 CoT 推理并输出报告
- 不需要 handoff、不需要第二个 Agent
- **代码访问**: 本地文件系统直接读取（`@function_tool`），不走 Code MCP
- **MCP 只保留两个**: Playwright（看页面）+ DB MCP（MySQL 查询）
- **V1 新增**: UI/DB 交叉验证（prompt 指令 + ConsistencyCheck evidence）
- **V2 规划**: 操作序列 → Playwright 测试脚本生成

## 目录结构

```
Test-Agent/
├── pyproject.toml                  # Python 项目配置 + 依赖
├── .gitignore
├── README.md
├── src/
│   └── universal_debug_agent/
│       ├── __init__.py
│       ├── main.py                 # CLI 入口
│       ├── config.py               # 加载 Project Profile
│       ├── schemas/
│       │   ├── __init__.py
│       │   ├── profile.py          # Project Profile Pydantic schema
│       │   └── report.py           # Investigation Report schema
│       ├── agents/
│       │   ├── __init__.py
│       │   ├── brain.py            # 单 Agent Brain (ReAct + 动态分析模式)
│       │   └── prompts.py          # Agent system prompts (含分析 fallback prompt)
│       ├── orchestrator/
│       │   ├── __init__.py
│       │   ├── state_machine.py    # ReAct ↔ Analysis 模式切换
│       │   └── hooks.py            # RunHooks 实现 (监控卡住检测)
│       ├── mcp/
│       │   ├── __init__.py
│       │   └── factory.py          # MCP server 工厂 (根据 profile 创建)
│       ├── tools/
│       │   ├── __init__.py
│       │   ├── code_tools.py       # 本地代码读取 tools (read_file, grep_code, list_dir)
│       │   └── report_tool.py      # 生成结构化报告的 function tool
│       └── generators/             # [V2] 脚本生成器
│           ├── __init__.py
│           └── script_generator.py # [V2] Playwright 操作序列 → .spec.ts
├── profiles/
│   └── example_project.yaml        # 示例 Project Profile
└── tests/
    ├── __init__.py
    ├── test_config.py
    ├── test_state_machine.py
    └── test_schemas.py
```

## 核心模块设计

### 1. Project Profile Schema (`src/universal_debug_agent/schemas/profile.py`)

使用 Pydantic v2 定义，YAML 格式存储：

```yaml
# profiles/example_project.yaml
project:
  name: "My Web App"
  description: "E-commerce platform"

environment:
  type: "web"                    # web | api | mobile
  base_url: "https://staging.example.com"
  start_command: "npm run dev"   # 可选，本地启动
  health_check_url: "/api/health"

auth:
  method: "form"                 # form | token | cookie | none
  login_url: "/login"
  test_accounts:
    - role: "admin"
      username_env: "TEST_ADMIN_USER"    # 从环境变量读取
      password_env: "TEST_ADMIN_PASS"
    - role: "user"
      username_env: "TEST_USER_USER"
      password_env: "TEST_USER_PASS"

code:
  root_dir: "/path/to/project"       # 本地 repo 路径，Agent 直接读文件系统
  branch: "main"                      # 当前 branch（信息标注，Agent 不切换）
  entry_dirs:                         # Agent 优先查看的目录
    - "src/pages"
    - "src/api"
    - "src/components"
  config_files:
    - ".env.staging"
    - "src/config/features.ts"

mcp_servers:
  playwright:
    enabled: true
    command: "npx"
    args: ["@anthropic-ai/mcp-playwright"]
  database:
    enabled: true
    command: "node"
    args: ["./mcp-servers/db-mcp/index.js"]
    env:
      DB_TYPE: "mysql"
      DB_HOST_ENV: "DB_HOST"          # 从环境变量读取
      DB_PORT_ENV: "DB_PORT"
      DB_USER_ENV: "DB_USER"
      DB_PASSWORD_ENV: "DB_PASSWORD"
      DB_NAME_ENV: "DB_NAME"

boundaries:
  readonly: true
  forbidden_actions:
    - "DELETE FROM"
    - "DROP TABLE"
    - "INSERT INTO"
    - "UPDATE"
  max_steps: 30
  allowed_domains:
    - "staging.example.com"
```

### 2. Agent Brain (`src/universal_debug_agent/agents/brain.py`)

单 Agent 设计，Orchestrator 根据状态动态切换 prompt：

```python
from agents import Agent, ModelSettings

def create_brain_agent(profile, mcp_servers, report_tool, mode="react"):
    """根据 mode 创建不同 prompt 的同一个 Agent"""
    if mode == "react":
        instructions = build_react_prompt(profile)
    else:  # mode == "analysis"
        instructions = build_analysis_prompt(profile)

    return Agent(
        name="DebugBrain",
        instructions=instructions,
        mcp_servers=mcp_servers,
        tools=[report_tool],
        output_type=InvestigationReport if mode == "analysis" else None,
        model="gpt-4o",
        model_settings=ModelSettings(temperature=0.2 if mode == "react" else 0.7),
    )
```

**ReAct 模式 prompt** 包含：
- 项目背景（从 profile 注入）
- ReAct 工作流指令（观察→行动→工具→观察→决策）
- 证据收集规范
- 边界限制（从 profile.boundaries 注入）

**Analysis 模式 prompt** 包含：
- "你已经收集了以下证据：{evidence_summary}"
- "停止调用工具，基于已有证据做深度分析"
- CoT 推理指令：列出所有可能的 root cause，逐一评估
- Self-Consistency：生成 3 个独立假设，比较一致性
- 强制输出 InvestigationReport 结构

### 3. 状态机 (`src/universal_debug_agent/orchestrator/state_machine.py`)

控制 ReAct ↔ Analysis 模式切换：

```python
class InvestigationState(Enum):
    REACT = "react"           # 正常 ReAct 循环
    ANALYZING = "analyzing"   # CoT 分析模式 (同一 Agent, 不同 prompt)
    DONE = "done"

class StuckDetector:
    """检测 agent 是否卡住（确定性 Python 逻辑，不依赖 LLM）"""

    def __init__(self, max_steps: int):
        self.tool_history: list[ToolCall] = []
        self.max_steps = max_steps

    def record(self, tool_name: str, tool_args: str, result_hash: str): ...

    def is_stuck(self) -> bool:
        # 规则 1: 连续 3 次完全相同的 tool call (name + args)
        # 规则 2: 最近 5 次 tool call 结果 hash 全部相同（无新信息）
        # 规则 3: 已用步数 > max_steps * 0.7 且无 evidence 产出
        ...

class InvestigationOrchestrator:
    """主编排器 — 单 Agent，双模式"""

    async def run(self, issue: str) -> InvestigationReport:
        # Phase 1: ReAct
        react_agent = create_brain_agent(profile, mcp_servers, report_tool, mode="react")
        try:
            result = await Runner.run(react_agent, issue, hooks=self.hooks)
            return result  # 正常完成
        except SwitchToAnalysisMode as e:
            # Phase 2: Analysis — 用收集到的 evidence 重建 prompt
            analysis_agent = create_brain_agent(profile, mcp_servers, report_tool, mode="analysis")
            analysis_input = build_analysis_input(issue, e.evidence)
            result = await Runner.run(analysis_agent, analysis_input)
            return result
```

### 4. RunHooks 实现 (`src/universal_debug_agent/orchestrator/hooks.py`)

利用 OpenAI Agents SDK 的 `RunHooks` 接口监控执行：

```python
from agents import RunHooks, RunContextWrapper, Tool, Agent

class InvestigationHooks(RunHooks):
    def __init__(self, stuck_detector: StuckDetector, evidence_collector: EvidenceCollector):
        self.stuck_detector = stuck_detector
        self.evidence_collector = evidence_collector

    async def on_tool_start(self, context, agent, tool):
        self.stuck_detector.record(tool.name, str(tool.args), None)

    async def on_tool_end(self, context, agent, tool, result):
        result_hash = hashlib.md5(str(result).encode()).hexdigest()
        self.stuck_detector.update_last_result(result_hash)
        self.evidence_collector.collect(tool.name, tool.args, result)

        if self.stuck_detector.is_stuck():
            raise SwitchToAnalysisMode(
                evidence=self.evidence_collector.get_all(),
                reason=self.stuck_detector.stuck_reason(),
            )
```

### 6. 本地代码 Tools (`src/universal_debug_agent/tools/code_tools.py`)

不走 MCP，直接用 `@function_tool` 注册本地文件操作：

```python
from agents import function_tool
import os

@function_tool
def read_file(path: str, start_line: int = 1, end_line: int = 100) -> str:
    """读取本地代码文件，限制在 profile.code.root_dir 内"""
    # 安全检查：path 必须在 root_dir 下，防止路径穿越
    # 限制单次最多读 200 行
    ...

@function_tool
def grep_code(pattern: str, directory: str = "", file_glob: str = "*.py") -> str:
    """在代码目录中搜索关键字/正则"""
    # 底层用 subprocess 调 grep -rn，限制在 root_dir 内
    # 限制返回前 50 条匹配
    ...

@function_tool
def list_directory(path: str = "") -> str:
    """列出目录内容"""
    # 限制在 root_dir 内
    ...
```

这些 tool 在 `create_brain_agent()` 时通过 `tools=[read_file, grep_code, list_directory, report_tool]` 注入。

### 7. MCP Factory (`src/universal_debug_agent/mcp/factory.py`)

根据 Project Profile 动态创建 MCP server 连接：

```python
from agents.mcp import MCPServerStdio

def create_mcp_servers(profile):
    servers = []
    for name, config in profile.mcp_servers.items():
        if config.enabled:
            env = resolve_env_vars(config.env)
            servers.append(MCPServerStdio(
                name=name,
                command=config.command,
                args=config.args,
                env=env,
            ))
    return servers
```

### 8. Investigation Report Schema (`src/universal_debug_agent/schemas/report.py`)

```python
class InvestigationReport(BaseModel):
    issue_summary: str
    steps_to_reproduce: list[str]
    evidence: list[Evidence]          # screenshots, logs, DB queries, code refs
    root_cause_hypotheses: list[Hypothesis]  # ranked by confidence
    classification: IssueClassification      # frontend / data / environment / config
    next_steps: list[str]
    metadata: ReportMetadata          # timestamp, agent version, profile used
```

Agent 通过 `output_type=InvestigationReport` 强制结构化输出。

### 9. CLI 入口 (`src/universal_debug_agent/main.py`)

```python
# 使用方式:
# python -m universal_debug_agent --profile profiles/my_project.yaml --issue "用户无法登录"
# python -m universal_debug_agent --profile profiles/my_project.yaml --issue-url "https://github.com/org/repo/issues/123"
```

参数：
- `--profile`: Project Profile YAML 路径（必填）
- `--issue`: 问题描述文本
- `--issue-url`: GitHub issue URL（自动抓取描述）
- `--output`: 报告输出路径（默认 stdout JSON）
- `--max-steps`: 覆盖 profile 中的 max_steps
- `--verbose`: 详细日志

### 10. 依赖 (`pyproject.toml`)

```toml
[project]
name = "universal-debug-agent"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "openai-agents>=0.0.7",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "rich>=13.0",        # 终端美化输出
    "typer>=0.9",        # CLI 框架
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]
```

## 执行流程

```
1. CLI 解析参数 → 加载 Project Profile
2. MCP Factory 根据 profile 创建 MCP servers
3. InvestigationOrchestrator 启动
4. Phase 1 — ReAct 模式:
   ├── 创建 Brain Agent (mode="react")
   ├── Runner.run(brain_agent, issue_description, hooks=investigation_hooks)
   ├── ReAct 循环: 观察 → 调工具 → 收集证据
   ├── RunHooks 每步记录 tool call + evidence
   └── StuckDetector 检查:
       ├── 未卡住 → 继续 ReAct → 正常输出报告
       └── 卡住 → raise SwitchToAnalysisMode
5. Phase 2 — Analysis 模式 (仅在卡住时触发):
   ├── 用已收集的 evidence 重建 Agent (mode="analysis")
   ├── Runner.run(analysis_agent, evidence_summary)
   ├── Agent 做 CoT 推理，不再调工具
   └── 强制输出 InvestigationReport
6. 输出 InvestigationReport (JSON/Rich terminal)
```

## 实现步骤

### Step 1: 项目脚手架
- 创建 `pyproject.toml`、`.gitignore`、目录结构
- 所有 `__init__.py` 文件

### Step 2: Schema 定义
- `schemas/profile.py` — Project Profile Pydantic model
- `schemas/report.py` — InvestigationReport Pydantic model
- `config.py` — YAML 加载 + 校验

### Step 3: MCP Factory
- `mcp/factory.py` — 根据 profile 创建 MCPServerStdio 实例

### Step 4: Agent Prompts
- `agents/prompts.py` — Brain prompt (ReAct) + Analysis fallback prompt (CoT/SC)
- 动态注入 profile 信息的模板函数
- ReAct prompt 包含 **UI/DB 交叉验证指令**（见下方 V1 新增能力）

### Step 5: Agent 定义
- `agents/brain.py` — 单 Agent，支持 react/analysis 双模式
- `tools/report_tool.py` — 结构化报告输出 tool

### Step 6: 状态机 + Hooks
- `orchestrator/state_machine.py` — 状态枚举 + StuckDetector + Orchestrator
- `orchestrator/hooks.py` — RunHooks 实现

### Step 7: CLI 入口
- `main.py` — Typer CLI + 完整执行流程

### Step 8: 示例 Profile + 测试
- `profiles/example_project.yaml`
- 单元测试: schema 校验、状态机逻辑、config 加载

## V1 新增能力: UI/DB 一致性交叉验证

Agent 同时接入 Playwright MCP 和 DB MCP，在 ReAct 循环中自动做交叉验证。

### 实现方式

1. **ReAct prompt 指令** — 在 `agents/prompts.py` 的 ReAct prompt 中加入：
   ```
   ## 交叉验证规则
   当你从 UI 上获取到关键业务状态（订单状态、用户权限、feature flag、余额等）时，
   你必须同时通过 DB MCP 查询对应数据做交叉验证。

   交叉验证步骤：
   1. 记录 UI 显示值（截图 + 文字）
   2. 构造对应 SQL 查询（只读 SELECT）
   3. 比较 UI 值和 DB 值
   4. 如果不一致 → 记录为 "data_inconsistency" evidence，标记严重程度
   ```

2. **Evidence 类型扩展** — `schemas/report.py` 中增加：
   ```python
   class ConsistencyCheck(BaseModel):
       ui_source: str          # "页面 /orders/123, 订单状态区域"
       ui_value: str           # "已发货"
       db_query: str           # "SELECT status FROM orders WHERE id = 123"
       db_value: str           # "pending"
       consistent: bool        # False
       severity: str           # "high" | "medium" | "low"
   ```

3. **Report 分类** — 当检测到 inconsistency 时，自动将 `classification` 倾向 "data" 类问题。

### 不需要额外代码模块
交叉验证通过 prompt 指令 + evidence schema 实现，不需要新的 Python 模块。Agent 已经有 Playwright 和 DB 两个 MCP，只需要 prompt 告诉它"看到 UI 数据要查 DB 对比"。

---

## V2 Roadmap: 探索性测试 → 确定性脚本生成

### 目标
LLM 先做一次探索性调查（V1），然后把成功的操作路径导出为 Playwright 测试脚本，后续 CI 可以直接跑，不再需要 LLM。

### 架构

```
V1 Agent 调查
  └── tool_call_history (Playwright 操作序列)
        │
        ▼
  ScriptGenerator (V2 新模块)
        │
        ▼
  output: test_xxx.spec.ts (Playwright 测试脚本)
        │
        ▼
  CI / 手动执行: npx playwright test
```

### 核心模块: `src/universal_debug_agent/generators/script_generator.py`

```python
class ScriptGenerator:
    """将 Agent 的 Playwright tool call 历史转为 .spec.ts 测试脚本"""

    def generate(self, tool_history: list[ToolCall], report: InvestigationReport) -> str:
        """
        输入:
        - tool_history: Agent 实际执行的 Playwright MCP 调用序列
          例: navigate("https://..."), click("#login"), fill("#email", "test@..."), screenshot()
        - report: 调查报告（用于生成测试断言）

        输出:
        - Playwright 测试脚本字符串 (.spec.ts)

        转换逻辑:
        1. MCP tool calls → Playwright API 调用映射
           navigate(url) → await page.goto(url)
           click(selector) → await page.click(selector)
           fill(selector, value) → await page.fill(selector, value)
           screenshot() → await expect(page).toHaveScreenshot()
        2. 从 report.evidence 中的 ConsistencyCheck 生成断言
           expect(await page.textContent('.status')).toBe('已发货')
        3. 包装成完整的 test('issue描述', async ({ page }) => { ... })
        """
```

### V2 实现步骤（不在当前 V1 范围内）
1. 在 `orchestrator/hooks.py` 的 `EvidenceCollector` 中记录完整 Playwright tool call 序列
2. 新增 `generators/` 目录 + `script_generator.py`
3. CLI 增加 `--generate-script` 参数
4. 生成的 `.spec.ts` 文件输出到指定目录

### V2 依赖
- V1 的 tool call history 记录能力（hooks 已有）
- Playwright MCP tool name → Playwright API 的映射表
- 模板引擎（Jinja2 或简单字符串模板）

---

## 验证方式

1. **Schema 测试**: `pytest tests/` — 验证 profile 加载、report 序列化
2. **状态机测试**: 验证 StuckDetector 在各种场景下正确触发
3. **集成冒烟测试**: 用 example profile 启动 agent（无真实 MCP），验证启动流程不报错
4. **手动端到端**: 配置真实 Playwright MCP + DB MCP，给一个真实 issue 跑一遍

## 关键文件清单

| 文件 | 职责 |
|------|------|
| `pyproject.toml` | 依赖 + 项目元数据 |
| `src/universal_debug_agent/main.py` | CLI 入口 |
| `src/universal_debug_agent/config.py` | Profile 加载 |
| `src/universal_debug_agent/schemas/profile.py` | Profile schema |
| `src/universal_debug_agent/schemas/report.py` | Report schema |
| `src/universal_debug_agent/agents/brain.py` | 单 Agent (react/analysis 双模式) |
| `src/universal_debug_agent/agents/prompts.py` | Prompt 模板 (ReAct + Analysis fallback) |
| `src/universal_debug_agent/orchestrator/state_machine.py` | 模式切换 |
| `src/universal_debug_agent/orchestrator/hooks.py` | RunHooks |
| `src/universal_debug_agent/mcp/factory.py` | MCP 工厂 (Playwright + DB) |
| `src/universal_debug_agent/tools/code_tools.py` | 本地代码读取 tools |
| `src/universal_debug_agent/tools/report_tool.py` | 报告工具 |
| `profiles/example_project.yaml` | 示例配置 |
