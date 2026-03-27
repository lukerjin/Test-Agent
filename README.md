# Universal Debug Agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 的通用 debug / investigation agent。

用于多个不同项目的问题复现、调试排查、证据收集和根因分析。不绑定任何特定 repo，通过 Project Profile 适配不同项目。

## 它能做什么

- **复现问题** — 通过 Playwright 操作页面，按步骤重现 bug
- **调试排查** — 读取本地代码，定位路由、组件、接口调用、feature flag
- **收集证据** — 截图、console/network 日志、DB 查询结果、代码片段
- **交叉验证** — UI 显示值与数据库实际值的一致性比对
- **根因分析** — 判断问题属于前端 / 数据 / 环境 / 配置哪一类
- **输出报告** — 结构化的调查报告，含 root cause 假设和下一步建议
- **调查记忆** — 记住过去的调查结果，避免重复走死胡同
- **多 LLM 支持** — OpenAI / Gemini / DeepSeek / Groq 等，一行配置切换

## 架构

```
┌─────────────────────────────────────────────────┐
│             Agent Brain (单 Agent)               │
│           ReAct 模式 ↔ Analysis 模式             │
│      (Orchestrator + StuckDetector 控制切换)      │
├──────────┬──────────┬──────────┬────────────────┤
│Playwright│ DB MCP   │  Code    │    Memory      │
│  MCP     │ (MySQL)  │  Tools   │   (JSONL)      │
│ 看页面    │ 只读查询  │ 读本地   │ 历史调查记录    │
│ 截图/日志 │ 查状态    │ 文件     │ 注入 prompt    │
├──────────┴──────────┴──────────┴────────────────┤
│  LLM Provider (OpenAI / Gemini / DeepSeek /...) │
├─────────────────────────────────────────────────┤
│            Project Profile (YAML)                │
│    每个项目独立配置环境、认证、LLM、边界、工具      │
└─────────────────────────────────────────────────┘
```

---

## 快速开始

### 1. 安装

```bash
cd Test-Agent
pip install -e .
```

### 2. 设置 API Key

```bash
# 二选一

# Gemini（有免费额度）
export GEMINI_API_KEY=你的key

# OpenAI
export OPENAI_API_KEY=你的key
```

### 3. 创建 Project Profile

```bash
cp profiles/example_project.yaml profiles/my_project.yaml
```

编辑 `profiles/my_project.yaml`（完整字段说明见下方「Profile 完整参考」）：

```yaml
project:
  name: "My Web App"
  description: "电商平台"

model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"

environment:
  type: "web"
  base_url: "https://staging.example.com"

code:
  root_dir: "/home/user/my-project"

boundaries:
  readonly: true
  max_steps: 30
```

### 4. 验证 Profile

```bash
python -m universal_debug_agent validate-profile profiles/my_project.yaml
```

输出：
```
Valid profile: My Web App
  Environment: web @ https://staging.example.com
  Model: gemini / gemini-2.0-flash
  Code root: /home/user/my-project
  MCP servers: none
  Max steps: 30
```

### 5. 运行调查

```bash
python -m universal_debug_agent investigate \
  -p profiles/my_project.yaml \
  -i "用户登录后订单页面显示空白"
```

---

## 使用教程

### 场景 1: 最简模式（只用代码分析）

不需要 Playwright 和 DB，agent 只读本地代码做分析。

**Profile:**
```yaml
project:
  name: "My API"
  description: "后端 API 服务"

model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"

code:
  root_dir: "/home/user/my-api"
  entry_dirs: ["src/routes", "src/services"]

boundaries:
  max_steps: 20
```

**运行:**
```bash
export GEMINI_API_KEY=你的key

python -m universal_debug_agent investigate \
  -p profiles/my_api.yaml \
  -i "POST /api/orders 返回 500，日志显示 TypeError: Cannot read property 'id' of undefined"
```

Agent 会读取 `src/routes` 和 `src/services` 下的代码，定位问题。

---

### 场景 2: 前端页面 + 数据库（完整模式）

**前置安装:**
```bash
# Playwright MCP
npm install -g @anthropic-ai/mcp-playwright

# DB MCP（你自己的 Node.js MCP server）
# 确保 ./mcp-servers/db-mcp/index.js 存在
```

**环境变量:**
```bash
export GEMINI_API_KEY=你的key
export TEST_ADMIN_USER=admin@example.com
export TEST_ADMIN_PASS=password123
export DB_HOST=127.0.0.1
export DB_PORT=3306
export DB_USER=readonly
export DB_PASSWORD=secret
export DB_NAME=my_database
```

**Profile:**
```yaml
project:
  name: "E-commerce App"
  description: "电商前端 + MySQL"

model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"

environment:
  type: "web"
  base_url: "https://staging.myapp.com"

auth:
  method: "form"
  login_url: "/login"
  test_accounts:
    - role: "admin"
      username_env: "TEST_ADMIN_USER"
      password_env: "TEST_ADMIN_PASS"

code:
  root_dir: "/home/user/ecommerce-frontend"
  entry_dirs: ["src/pages", "src/api", "src/components"]
  config_files: ["src/config/features.ts", ".env.staging"]

memory:
  enabled: true
  path: "./memory/{project_name}.jsonl"

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
      DB_HOST_ENV: "DB_HOST"
      DB_PORT_ENV: "DB_PORT"
      DB_USER_ENV: "DB_USER"
      DB_PASSWORD_ENV: "DB_PASSWORD"
      DB_NAME_ENV: "DB_NAME"

boundaries:
  readonly: true
  forbidden_actions: ["DELETE FROM", "DROP TABLE", "INSERT INTO", "UPDATE"]
  max_steps: 30
  allowed_domains: ["staging.myapp.com"]
```

**运行:**
```bash
python -m universal_debug_agent investigate \
  -p profiles/ecommerce.yaml \
  -i "管理员登录后，订单 #1234 状态显示'已发货'，但客户反馈实际未收到货"
```

Agent 会：
1. 用 Playwright 登录页面，导航到订单详情，截图
2. 用 DB MCP 查询 `SELECT status FROM orders WHERE id = 1234`
3. 对比 UI 和 DB 的值（交叉验证）
4. 读取代码中的状态映射逻辑
5. 输出报告

---

### 场景 3: 多项目并行

不同终端，不同 profile，互不干扰：

```bash
# 终端 1 — 电商项目（Gemini）
python -m universal_debug_agent investigate \
  -p profiles/ecommerce.yaml \
  -i "首页加载超过 5 秒"

# 终端 2 — 后台管理（DeepSeek）
python -m universal_debug_agent investigate \
  -p profiles/admin_panel.yaml \
  -i "用户列表页面报 403"
```

---

### 场景 4: 启用记忆（Memory）

让 agent 记住过去的调查，下次遇到类似问题更快定位。

**Profile 中开启:**
```yaml
memory:
  enabled: true
  path: "./memory/{project_name}.jsonl"
  max_entries_in_prompt: 20
```

**效果:**
- 第一次调查："订单状态不一致" → 发现是 `status_map.ts` 映射错误 → 保存到 memory
- 第二次调查："支付状态显示异常" → agent 看到历史记忆，优先检查状态映射相关代码

**Memory 文件内容（自动生成）:**
```jsonl
{"issue":"订单状态不一致","root_cause":"status_map.ts line 42 映射错误","classification":"frontend","key_findings":["pending 被映射成 shipped"],"dead_ends":[],"timestamp":"2026-03-27T10:30:00"}
```

**查看 / 管理 memory:**
```bash
# 查看
cat memory/e-commerce_app.jsonl | python -m json.tool --json-lines

# 清空（重新开始）
rm memory/e-commerce_app.jsonl
```

---

## CLI 参考

```bash
# 调查命令
python -m universal_debug_agent investigate [OPTIONS]

Options:
  -p, --profile TEXT     Project Profile YAML 路径（必填）
  -i, --issue TEXT       问题描述
  --issue-url TEXT       GitHub issue URL（V2）
  -o, --output TEXT      报告输出文件路径（默认输出到终端）
  --max-steps INT        覆盖 profile 中的 max_steps
  -v, --verbose          显示详细日志

# 验证 profile
python -m universal_debug_agent validate-profile <path>
```

---

## LLM 切换

在 profile 的 `model` 字段配置，一行切换：

```yaml
# Gemini（免费额度）
model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"

# OpenAI
model:
  provider: "openai"
  model_name: "gpt-4o"

# DeepSeek
model:
  provider: "deepseek"
  model_name: "deepseek-chat"

# Groq
model:
  provider: "groq"
  model_name: "llama-3.3-70b-versatile"

# 任意 OpenAI 兼容 API
model:
  provider: "custom"
  model_name: "your-model"
  base_url: "https://your-api.example.com/v1"
  api_key_env: "YOUR_API_KEY"
```

对应的环境变量会自动查找（`GEMINI_API_KEY`、`OPENAI_API_KEY` 等），也可以通过 `api_key_env` 指定。

---

## Profile 完整参考

```yaml
# ============================================================
# Project Profile — 每个项目一个文件
# ============================================================

project:
  name: "项目名"                              # 必填
  description: "项目简介"

# --- LLM ---
model:
  provider: "gemini"                          # openai | gemini | deepseek | groq | together | openrouter
  model_name: "gemini-2.0-flash"             # 不填则用 provider 默认
  api_key_env: "GEMINI_API_KEY"              # 不填则自动按 provider 查找
  base_url: null                              # 自定义端点（覆盖 provider 默认）

# --- 环境 ---
environment:
  type: "web"                                 # web | api | mobile
  base_url: "https://staging.example.com"
  start_command: "npm run dev"               # 可选，本地启动命令
  health_check_url: "/api/health"            # 可选

# --- 认证 ---
auth:
  method: "form"                              # form | token | cookie | none
  login_url: "/login"
  test_accounts:
    - role: "admin"
      username_env: "TEST_ADMIN_USER"        # 环境变量名（不是明文密码）
      password_env: "TEST_ADMIN_PASS"
    - role: "user"
      username_env: "TEST_USER_USER"
      password_env: "TEST_USER_PASS"

# --- 代码 ---
code:
  root_dir: "/absolute/path/to/project"      # 必填，本地 repo 绝对路径
  branch: "main"                              # 信息标注，agent 不切换分支
  entry_dirs:                                 # agent 优先查看的目录
    - "src/pages"
    - "src/api"
  config_files:                               # 关键配置文件
    - ".env.staging"
    - "src/config/features.ts"

# --- 记忆 ---
memory:
  enabled: true                               # 默认 false
  path: "./memory/{project_name}.jsonl"      # {project_name} 自动替换
  max_entries_in_prompt: 20                  # 注入 prompt 的最大历史条数

# --- MCP 服务 ---
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
      DB_HOST_ENV: "DB_HOST"                 # _ENV 后缀 = 从环境变量读取
      DB_PORT_ENV: "DB_PORT"
      DB_USER_ENV: "DB_USER"
      DB_PASSWORD_ENV: "DB_PASSWORD"
      DB_NAME_ENV: "DB_NAME"

# --- 边界 ---
boundaries:
  readonly: true                              # V1 强制只读
  forbidden_actions:                          # SQL 黑名单
    - "DELETE FROM"
    - "DROP TABLE"
    - "INSERT INTO"
    - "UPDATE"
  max_steps: 30                               # 最大调查步数
  allowed_domains:                            # Playwright 可访问的域名
    - "staging.example.com"
```

---

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
    { "type": "db_query", "source": "orders table", "content": "status=pending" },
    { "type": "consistency_check", "source": "cross-validation" }
  ],
  "consistency_checks": [
    {
      "ui_source": "/orders/123",
      "ui_value": "已发货",
      "db_query": "SELECT status FROM orders WHERE id=123",
      "db_value": "pending",
      "consistent": false,
      "severity": "high"
    }
  ],
  "root_cause_hypotheses": [
    { "hypothesis": "前端 status_map 中 pending 被错误映射为'已发货'", "confidence": 0.7 },
    { "hypothesis": "Redis 缓存未失效，显示旧状态", "confidence": 0.2 }
  ],
  "classification": "frontend",
  "next_steps": [
    "检查 src/utils/statusMap.ts 中的状态映射逻辑",
    "确认是否有 Redis 缓存层影响状态展示"
  ],
  "metadata": {
    "timestamp": "2026-03-27T10:30:00",
    "agent_version": "0.1.0",
    "profile_name": "E-commerce App",
    "total_steps": 12,
    "mode_switches": 0
  }
}
```

---

## 项目结构

```
src/universal_debug_agent/
├── main.py              # CLI 入口 (typer)
├── config.py            # YAML Profile 加载
├── schemas/
│   ├── profile.py       # ProjectProfile + ModelConfig + MemoryConfig
│   └── report.py        # InvestigationReport + ConsistencyCheck
├── agents/
│   ├── brain.py         # Agent Brain (ReAct / Analysis 双模式)
│   └── prompts.py       # System prompts (含交叉验证 + memory 注入)
├── orchestrator/
│   ├── state_machine.py # StuckDetector + InvestigationOrchestrator
│   └── hooks.py         # RunHooks (tool 监控 + 卡住检测)
├── models/
│   └── factory.py       # LLM 工厂 (OpenAI/Gemini/DeepSeek/...)
├── memory/
│   └── store.py         # JSONL 记忆存储 (load/save/prompt 注入)
├── mcp/
│   └── factory.py       # MCP server 工厂 (Playwright + DB)
├── tools/
│   ├── code_tools.py    # 本地代码读取 (read_file, grep_code, list_dir)
│   └── report_tool.py   # 结构化报告提交
└── generators/          # [V2] Playwright 脚本生成
```

---

## 工作原理

```
1. 加载 Profile → 创建 LLM → 加载 Memory → 创建 MCP 连接
2. ReAct 模式启动:
   ├── 历史 memory 注入 prompt（如有）
   ├── 观察 → 调工具 → 收集证据 → 再观察
   ├── Playwright: 打开页面、操作、截图、抓日志
   ├── DB MCP: 查询用户/订单/权限/配置状态
   ├── Code Tools: 读取相关代码、搜索关键字
   └── UI/DB 交叉验证: 发现数据不一致自动标记
3. 如果卡住 (StuckDetector 检测):
   ├── 连续 3 次相同 tool call
   ├── 最近 5 次结果完全相同
   └── 或超过 70% 步数无报告
   → 自动切换到 Analysis 模式
     ├── 汇总已收集证据 + 历史 memory
     ├── CoT 推理 + 多假设比较
     └── 输出结构化报告
4. 输出 InvestigationReport (JSON)
5. 保存本次调查到 Memory（如已启用）
```

---

## V1 边界

| 做 | 不做 |
|---|---|
| 读取 Project Profile，适配不同项目 | 自动改代码 |
| Playwright MCP 操作页面、收集 UI 证据 | 自动提交 PR |
| DB MCP 查询 MySQL（只读） | 写数据库 |
| 本地代码读取和搜索 | 无限制浏览整个 repo |
| UI/DB 交叉验证 | |
| 卡住检测 + 自动切换分析模式 | |
| 结构化调查报告输出 | |
| JSONL 调查记忆 | |
| 多 LLM provider 切换 | |

## Roadmap

- **V2**: 探索性操作 → Playwright 测试脚本生成（`.spec.ts`），可直接在 CI 跑
- **V3**: Memory 迁移到 RAG（当前 JSONL 结构零改动兼容）

详见 [docs/PLAN_V1.md](docs/PLAN_V1.md)。

## License

MIT
