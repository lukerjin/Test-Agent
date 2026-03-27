# Universal Debug Agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 的通用 debug / investigation agent。

用于多个不同项目的问题复现、调试排查、证据收集和根因分析。不绑定任何特定 repo，通过 Project Profile 适配不同项目。

## 它能做什么

- 复现问题 — 通过 Playwright 操作页面，按步骤重现 bug
- 调试排查 — 读取本地代码，定位路由、组件、接口调用、feature flag
- 收集证据 — 截图、console/network 日志、DB 查询结果、代码片段
- 交叉验证 — UI 显示值与数据库实际值的一致性比对
- 根因分析 — 判断问题属于前端 / 数据 / 环境 / 配置哪一类
- 输出报告 — 结构化的调查报告，含 root cause 假设和下一步建议

## 架构

```
┌─────────────────────────────────────────────┐
│           Agent Brain (单 Agent)             │
│         ReAct 模式 ↔ Analysis 模式           │
│    (Orchestrator + StuckDetector 控制切换)    │
├─────────────────────────────────────────────┤
│  Playwright MCP  │  DB MCP (MySQL)  │ Code  │
│   看页面/截图     │   只读查询        │ Tools │
│   抓 console     │   查状态/权限     │ 读本地 │
│   操作 UI        │   查配置/数据     │ 文件   │
├─────────────────────────────────────────────┤
│              Project Profile (YAML)          │
│     每个项目独立配置环境、认证、边界、工具      │
└─────────────────────────────────────────────┘
```

## 快速开始

### 安装

```bash
pip install -e .
```

### 配置 Project Profile

为你的项目创建一个 YAML 配置文件（参考 `profiles/example_project.yaml`）：

```yaml
project:
  name: "My Web App"
  description: "E-commerce platform"

environment:
  type: "web"
  base_url: "https://staging.example.com"

auth:
  method: "form"
  login_url: "/login"
  test_accounts:
    - role: "admin"
      username_env: "TEST_ADMIN_USER"
      password_env: "TEST_ADMIN_PASS"

code:
  root_dir: "/path/to/your/project"
  entry_dirs: ["src/pages", "src/api"]

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
      DB_HOST_ENV: "DB_HOST"
      DB_USER_ENV: "DB_USER"
      DB_PASSWORD_ENV: "DB_PASSWORD"
      DB_NAME_ENV: "DB_NAME"

boundaries:
  readonly: true
  max_steps: 30
```

### 运行

```bash
# 文本描述问题
python -m universal_debug_agent --profile profiles/my_project.yaml --issue "用户登录后跳转到空白页"

# 指定 GitHub issue
python -m universal_debug_agent --profile profiles/my_project.yaml --issue-url "https://github.com/org/repo/issues/123"

# 输出报告到文件
python -m universal_debug_agent --profile profiles/my_project.yaml --issue "订单状态显示不正确" --output report.json
```

### 多项目并行

不同终端窗口跑不同项目，互不干扰：

```bash
# 终端 1
python -m universal_debug_agent --profile profiles/project_a.yaml --issue "..."

# 终端 2
python -m universal_debug_agent --profile profiles/project_b.yaml --issue "..."
```

## 工作流程

```
1. 加载 Project Profile → 创建 MCP 连接
2. ReAct 模式启动:
   ├── 观察 → 调工具 → 收集证据 → 再观察
   ├── Playwright: 打开页面、操作、截图、抓日志
   ├── DB MCP: 查询用户/订单/权限/配置状态
   ├── Code Tools: 读取相关代码、搜索关键字
   └── UI/DB 交叉验证: 发现数据不一致自动标记
3. 如果卡住 (StuckDetector 检测):
   └── 自动切换到 Analysis 模式
       ├── 汇总已收集证据
       ├── CoT 推理 + 多假设比较
       └── 输出结构化报告
4. 输出 InvestigationReport (JSON)
```

## 输出示例

```json
{
  "issue_summary": "用户登录后订单列表页显示'已发货'，但数据库中状态为 pending",
  "steps_to_reproduce": [
    "使用 test_user 账号登录",
    "进入 /orders/123",
    "观察订单状态显示区域"
  ],
  "evidence": [
    { "type": "screenshot", "source": "/orders/123", "description": "页面显示'已发货'" },
    { "type": "db_query", "query": "SELECT status FROM orders WHERE id=123", "result": "pending" },
    { "type": "consistency_check", "ui_value": "已发货", "db_value": "pending", "consistent": false }
  ],
  "root_cause_hypotheses": [
    { "hypothesis": "前端状态映射表 status_map 中 pending 被错误映射为'已发货'", "confidence": 0.7 },
    { "hypothesis": "缓存未失效，显示的是旧状态", "confidence": 0.2 }
  ],
  "classification": "frontend",
  "next_steps": [
    "检查 src/utils/statusMap.ts 中的状态映射逻辑",
    "确认是否有 Redis 缓存层影响状态展示"
  ]
}
```

## 项目结构

```
src/universal_debug_agent/
├── main.py              # CLI 入口
├── config.py            # Profile 加载
├── schemas/
│   ├── profile.py       # Project Profile schema
│   └── report.py        # InvestigationReport schema
├── agents/
│   ├── brain.py         # Agent Brain (ReAct + Analysis 双模式)
│   └── prompts.py       # System prompts
├── orchestrator/
│   ├── state_machine.py # StuckDetector + 模式切换
│   └── hooks.py         # RunHooks 监控
├── mcp/
│   └── factory.py       # MCP server 工厂
└── tools/
    ├── code_tools.py    # 本地文件读取 (read_file, grep_code, list_dir)
    └── report_tool.py   # 报告生成
```

## V1 边界

**做的**:
- 读取 Project Profile，适配不同项目
- Playwright MCP 操作页面、收集 UI 证据
- DB MCP 查询 MySQL（只读）
- 本地代码读取和搜索
- UI/DB 交叉验证
- 卡住检测 + 自动切换分析模式
- 结构化调查报告输出

**不做的**:
- 自动改代码
- 自动提交 PR
- 写数据库
- 无限制浏览整个 repo

## V2 Roadmap

- 探索性操作 → Playwright 测试脚本生成（`.spec.ts`）
- Agent 的操作路径可直接导出为 CI 可跑的确定性测试

详见 [docs/PLAN_V1.md](docs/PLAN_V1.md)。

## License

MIT
