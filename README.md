# Universal Test Agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 的通用 E2E 测试执行 + 数据验证 agent。

给它一个业务场景（自然语言），它自己走完整个流程，遇到障碍自己解决，最后验证数据库里的数据对不对。**任意 scenario，不限于某一种固定流程。**

## 它能做什么

- **执行任意业务流程** — 购买、注册、退款、权限检查、搜索... 任何你能用自然语言描述的 scenario
- **自动处理障碍** — 没登录就登录，有弹窗就关掉，加载慢就等
- **收集证据** — 关键步骤自动截图、记录页面状态
- **数据验证** — 流程走完后查数据库，确认数据落库正确
- **结构化报告** — 每步 pass/fail + 每项数据验证 pass/fail
- **测试记忆** — 记住过去的测试结果，避免重复踩坑
- **多 LLM 支持** — OpenAI / Gemini / DeepSeek / Groq，一行切换
- **多项目通用** — 一套代码，每个项目一个 YAML 配置

## 架构

```
┌─────────────────────────────────────────────────┐
│             Test Agent (单 Agent)                │
│           ReAct 模式 ↔ Analysis 模式             │
│      (Orchestrator + StuckDetector 控制切换)      │
├──────────┬──────────┬──────────┬────────────────┤
│Playwright│ DB MCP   │  Code    │    Memory      │
│  MCP     │ (MySQL)  │  Tools   │   (JSONL)      │
│ 操作页面  │ 只读验证  │ 读代码   │ 历史测试记录    │
│ 截图/日志 │ 查数据    │ 搜索    │ 注入 prompt    │
├──────────┴──────────┴──────────┴────────────────┤
│  LLM Provider (OpenAI / Gemini / DeepSeek /...) │
├─────────────────────────────────────────────────┤
│             Project Profile (YAML)               │
│     每个项目独立配置环境、认证、LLM、边界、工具     │
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
# Gemini（有免费额度）
export GEMINI_API_KEY=你的key

# 或 OpenAI
export OPENAI_API_KEY=你的key
```

### 3. 创建 Project Profile

```bash
cp profiles/example_project.yaml profiles/my_project.yaml
```

编辑关键字段（完整参考见 [Profile 完整参考](#profile-完整参考)）：

```yaml
project:
  name: "My E-commerce"
model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"
environment:
  base_url: "https://staging.myapp.com"
code:
  root_dir: "/home/user/my-project"
```

### 4. 验证 Profile

```bash
python -m universal_debug_agent validate-profile profiles/my_project.yaml
```

### 5. 运行测试

```bash
python -m universal_debug_agent test \
  -p profiles/my_project.yaml \
  -s "购买产品 A：加入购物车，checkout，填写地址，付款，确认订单成功"
```

---

## Workflow

```
            你写的                      Agent 自动完成
    ┌───────────────────┐     ┌─────────────────────────────────────┐
    │  1. Profile YAML  │     │  3. 拆解 scenario 为步骤              │
    │  (一次性，per项目)  │     │  4. 用 Playwright 逐步执行页面操作    │
    │                   │     │  5. 遇到障碍自己解决（登录/弹窗/等待） │
    │  2. --scenario    │────▶│  6. 关键步骤截图                      │
    │  (每次测试一句话)   │     │  7. 查 DB 验证数据正确性              │
    │                   │     │  8. 输出结构化报告 (pass/fail)        │
    └───────────────────┘     │  9. 保存到 Memory                    │
                              └─────────────────────────────────────┘
```

### 详细执行流程

```
输入: -s "购买产品 A，验证订单数据"
        │
        ▼
1. 加载 Profile
   ├── 读取项目配置 (环境/认证/边界)
   ├── 创建 LLM (Gemini/OpenAI/...)
   ├── 加载 Memory (历史测试记录)
   └── 启动 MCP servers (Playwright + DB)
        │
        ▼
2. ReAct 执行循环
   ├── Think: "第一步应该打开商品页"
   ├── Act:   Playwright → page.goto('/products')
   ├── Observe: "页面加载了，看到产品列表"
   ├── Think: "找到产品 A，点击加入购物车"
   ├── Act:   Playwright → page.click('.add-to-cart')
   ├── Observe: "购物车数量变为 1"
   ├── ...继续直到流程走完...
   │
   │   遇到障碍时:
   │   ├── 需要登录 → 自动用 profile 里的测试账号登录
   │   ├── 有弹窗 → 自动关闭
   │   └── 页面加载慢 → 等待
   │
   └── 每步记录: step_number, action, status, screenshot
        │
        ▼
3. 数据验证（流程完成或证据足够时触发）
   ├── SELECT * FROM orders WHERE ... → 验证订单存在
   ├── SELECT * FROM order_items WHERE ... → 验证商品正确
   ├── SELECT stock FROM products WHERE ... → 验证库存扣减
   └── SELECT status FROM payments WHERE ... → 验证支付状态
   注: 如果流程在中途 blocked，可能只输出执行证据、截图和下一步排查建议，数据验证会为空。
        │
        ▼
4. 卡住检测 (StuckDetector，全程监控)
   ├── 连续 3 次相同操作 → 卡住
   ├── 最近 5 次结果完全相同 → 卡住
   └── 超过配置的预算比例仍无报告 → 卡住
   → 卡住时自动切换 Analysis 模式
     ├── 停止调用工具
     ├── 汇总已执行的步骤和证据
     └── 输出分析报告
        │
        ▼
5. 输出报告
   ├── 终端: 彩色表格 (Steps + Verifications)
   ├── JSON: 完整结构化报告
   └── Memory: 追加写入 JSONL (供下次参考)
```

---

## 使用场景

`--scenario` 接受**任意自然语言**，不限于某一种固定流程：

### 购买流程

```bash
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "购买 iPhone 16：搜索产品，加入购物车，checkout，
      如果没登录先登录，填写收货地址，信用卡支付，完成付款。
      验证：orders 表有新订单，order_items 包含 iPhone 16，
      库存减少 1，支付状态为 completed"
```

### 注册流程

```bash
python -m universal_debug_agent test \
  -p profiles/my_app.yaml \
  -s "新用户注册：打开注册页，填写邮箱密码，提交，
      验证跳转到 dashboard。验证 users 表有新记录，状态为 active"
```

### 退款流程

```bash
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "用 admin 登录，找到订单 #1234，点击退款，确认退款成功。
      验证 refunds 表有新记录，订单状态变为 refunded，用户余额增加"
```

### 权限测试

```bash
python -m universal_debug_agent test \
  -p profiles/admin.yaml \
  -s "用普通用户登录，尝试访问 /admin/users，
      应该看到 403 或跳转到首页。验证没有越权数据返回"
```

### 搜索功能

```bash
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "搜索 'iPhone'，验证结果列表包含 iPhone 16，
      点击第一个结果，验证跳转到正确的商品详情页，价格和 DB 一致"
```

### 异常路径

```bash
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "不登录直接访问 /checkout，应该跳转到登录页。
      提交空的地址表单，应该显示验证错误"
```

### 多项目并行

不同终端窗口跑不同项目，互不干扰：

```bash
# 终端 1 — 电商前端（Gemini）
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "购买流程"

# 终端 2 — 后台管理（DeepSeek）
python -m universal_debug_agent test \
  -p profiles/admin.yaml \
  -s "用户管理流程"
```

---

## 输出示例

### 终端输出

```
Loading profile: profiles/ecommerce.yaml
Project: My E-commerce
Model: gemini / gemini-2.0-flash
Memory: ./memory/my_e-commerce.jsonl (3 past records)
MCP servers: playwright, database

┌─ Test Scenario ─────────────────────────────────────────┐
│ 购买产品 A：加入购物车，checkout，填写地址，付款          │
└─────────────────────────────────────────────────────────┘

... (agent 执行中，verbose 模式下会显示每步日志) ...

┌─ 购买产品 A 的完整流程 ─────────────────────┐
│                   PASS                       │
└──────────────────────────────────────────────┘

        Steps Executed
┌───┬───────────────┬────────┬──────────────────────┐
│ # │ Action        │ Status │ Notes                │
├───┼───────────────┼────────┼──────────────────────┤
│ 1 │ 打开商品页     │ pass   │ 页面加载成功           │
│ 2 │ 搜索 iPhone   │ pass   │ 找到 3 个结果          │
│ 3 │ 加入购物车     │ pass   │ 购物车数量+1           │
│ 4 │ 进入 checkout │ pass   │ 检测到未登录，自动登录  │
│ 5 │ 填写地址      │ pass   │ 自动填写测试地址        │
│ 6 │ 完成支付      │ pass   │ 跳转到成功页           │
└───┴───────────────┴────────┴──────────────────────┘

     Data Verifications
┌──────────────┬───────────┬───────────┬────────┐
│ Check        │ Expected  │ Actual    │ Status │
├──────────────┼───────────┼───────────┼────────┤
│ 订单已创建    │ >= 1 row  │ 1 row     │ pass   │
│ 产品匹配     │ iPhone 16 │ iPhone 16 │ pass   │
│ 库存扣减     │ stock=99  │ stock=99  │ pass   │
│ 支付状态     │ completed │ completed │ pass   │
└──────────────┴───────────┴───────────┴────────┘

Memory updated
```

### JSON 报告

```json
{
  "scenario_summary": "购买产品 A 的完整流程",
  "overall_status": "pass",
  "steps_executed": [
    {"step_number": 1, "action": "打开商品页", "status": "pass", "actual_result": "页面加载成功"},
    {"step_number": 2, "action": "搜索 iPhone", "status": "pass", "actual_result": "找到 3 个结果"},
    {"step_number": 3, "action": "加入购物车", "status": "pass", "actual_result": "购物车数量+1"},
    {"step_number": 4, "action": "进入 checkout", "status": "pass", "notes": "检测到未登录，自动走登录流程"},
    {"step_number": 5, "action": "填写地址", "status": "pass"},
    {"step_number": 6, "action": "完成支付", "status": "pass", "actual_result": "跳转到成功页"}
  ],
  "data_verifications": [
    {"check_name": "订单已创建", "query": "SELECT COUNT(*) FROM orders WHERE user_id=1 AND created_at > '2026-03-29'", "expected": ">= 1 row", "actual": "1 row", "status": "pass", "severity": "high"},
    {"check_name": "order_items 正确", "query": "SELECT product_id, quantity FROM order_items WHERE order_id=456", "expected": "iPhone 16, qty=1", "actual": "iPhone 16, qty=1", "status": "pass", "severity": "high"},
    {"check_name": "库存扣减", "query": "SELECT stock FROM products WHERE id='iphone16'", "expected": "stock=99", "actual": "stock=99", "status": "pass", "severity": "medium"},
    {"check_name": "支付状态", "query": "SELECT status FROM payments WHERE order_id=456", "expected": "completed", "actual": "completed", "status": "pass", "severity": "high"}
  ],
  "evidence": [
    {"type": "screenshot", "source": "/checkout/success", "description": "付款成功页面"},
    {"type": "screenshot", "source": "/orders/456", "description": "订单详情页"}
  ],
  "issues_found": [],
  "next_steps": [],
  "metadata": {
    "timestamp": "2026-03-29T10:30:00",
    "agent_version": "0.1.0",
    "profile_name": "My E-commerce",
    "total_steps": 14,
    "mode_switches": 0
  }
}
```

---

## CLI 参考

```bash
# 执行测试场景
python -m universal_debug_agent test [OPTIONS]

Options:
  -p, --profile TEXT     Project Profile YAML 路径（必填）
  -s, --scenario TEXT    测试场景描述（任意自然语言）
  -o, --output TEXT      报告输出文件路径（默认输出到终端）
  --max-steps INT        覆盖 profile 中的 max_steps
  -v, --verbose          显示详细日志

# 验证 profile
python -m universal_debug_agent validate-profile <path>
```

---

## LLM 切换

Profile 的 `model` 字段，一行切换：

```yaml
# Gemini（免费额度）
model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"

# OpenAI
model:
  provider: "openai"
  model_name: "gpt-5.4-nano"

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

对应的环境变量会自动查找（`GEMINI_API_KEY`、`OPENAI_API_KEY` 等），也可以通过 `api_key_env` 显式指定。

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
  base_url: null                              # 自定义端点

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
  branch: "main"                              # 信息标注
  entry_dirs:                                 # agent 优先查看的目录
    - "src/pages"
    - "src/api"
  config_files:
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
    args: ["@playwright/mcp@latest"]
    cwd: "./artifacts/playwright"
    client_session_timeout_seconds: 15
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
      DB_ALIASES_ENV: "DB_ALIASES"

# --- 边界 ---
boundaries:
  readonly: true                              # V1 强制只读
  forbidden_actions:                          # SQL 黑名单
    - "DELETE FROM"
    - "DROP TABLE"
    - "INSERT INTO"
    - "UPDATE"
  max_steps: 40                               # 最大工具步骤数
  max_turns: 20                               # 最大 LLM turns
  stuck_budget_ratio: 0.85                    # 超过该比例仍无报告则收口
  allowed_domains:                            # Playwright 可访问的域名
    - "staging.example.com"
```

---

## 项目结构

```
src/universal_debug_agent/
├── main.py              # CLI 入口 (typer)
├── config.py            # YAML Profile 加载
├── schemas/
│   ├── profile.py       # ProjectProfile + ModelConfig + MemoryConfig
│   └── report.py        # ScenarioReport + ScenarioStep + DataVerification
├── agents/
│   ├── brain.py         # Agent Brain (ReAct / Analysis 双模式)
│   └── prompts.py       # System prompts (含数据验证指令)
├── orchestrator/
│   ├── state_machine.py # StuckDetector + InvestigationOrchestrator
│   ├── hooks.py         # RunHooks (tool 监控 + 卡住检测)
│   └── input_filters.py # MCP 输出过滤：Playwright snapshot 提取 interactive 元素，历史轮次保留语义压缩版本，非 snapshot 输出字符截断兜底
├── models/
│   └── factory.py       # LLM 工厂 (OpenAI/Gemini/DeepSeek/...)
├── memory/
│   └── store.py         # JSONL 记忆存储 (load/save/prompt 注入)
├── observability/
│   ├── llm_usage.py     # LLM token usage / per-run usage summary
│   └── trace_recorder.py # 执行轨迹落盘 (trace.md / trace.jsonl)
├── mcp/
│   └── factory.py       # MCP server 工厂 (Playwright + DB)
├── tools/
│   ├── auth_tools.py    # 测试账号解析与 get_test_account tool
│   ├── code_tools.py    # 本地代码读取 (read_file, grep_code, list_dir)
│   └── report_tool.py   # 结构化报告提交
└── generators/          # [V2] Playwright 脚本生成 + Contract 固化
```

---

## Token 控制

Playwright 每次 browser 操作都会返回完整的页面 ARIA 树（可达 13,000+ chars / ~3,400 tokens）。随着步骤增加，上下文会线性膨胀，容易触发 TPM（每分钟 token）限额。

`MCPToolOutputFilter` 分两层处理：

1. **Snapshot 语义过滤**（默认开启）：提取 ARIA 树中的 interactive 元素（button、link、textbox、checkbox、radio 等）、状态标记（`[active]`、`[checked]`）、内联文本节点，丢弃纯容器节点和 `[unchanged]` 回引。当前轮和历史轮次都使用过滤后的版本，通常可将单个 snapshot 从 ~13k chars 压缩到 ~2-3k chars。
2. **字符截断兜底**：非 snapshot 类输出（DB 查询结果、代码文件等）仍按原有字符数上限截断。

关闭 snapshot 过滤（回退到纯字符截断）：

```python
# src/universal_debug_agent/orchestrator/state_machine.py
_RUN_CONFIG = RunConfig(
    call_model_input_filter=MCPToolOutputFilter(snapshot_filter=False),
)
```

---

## V1 边界

| 做 | 不做 |
|---|---|
| 执行任意业务流程（Playwright） | 自动改代码 |
| 数据验证（DB 只读查询） | 写数据库 |
| 自动处理登录/弹窗等障碍 | 自动提 PR |
| 读本地代码辅助理解 | 解 CAPTCHA / 2FA |
| 结构化 pass/fail 报告 | 无限制浏览 |
| JSONL 测试记忆 | |
| 多 LLM provider 切换 | |

## Roadmap

- **V2**: Contract 固化（LLM 第一次探索生成验证 contract，后续确定性复用）+ Playwright 脚本导出
- **V3**: Memory RAG + 智能测试策略选择

详见 [docs/PLAN_V1.md](docs/PLAN_V1.md) | [docs/PLAN_V2.md](docs/PLAN_V2.md) | [docs/PLAN_V3.md](docs/PLAN_V3.md)。

## License

MIT
