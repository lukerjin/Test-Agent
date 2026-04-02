# Universal Test Agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 的通用 E2E 测试执行 + 数据验证 agent。

给它一个业务场景（自然语言），它自己走完整个流程，遇到障碍自己解决，最后验证数据库里的数据对不对。**任意 scenario，不限于某一种固定流程。**

## 它能做什么

- **执行任意业务流程** — 购买、注册、退款、权限检查、搜索... 任何你能用自然语言描述的 scenario
- **自动处理障碍** — 没登录就登录，有弹窗就关掉，加载慢就等
- **收集证据** — 关键步骤自动截图、记录页面状态
- **数据验证** — 流程走完后查数据库，确认数据落库正确；支持 scenario 级 `db_checks` 精确指定验证项
- **结构化报告** — 每步 pass/fail + 每项数据验证 pass/fail
- **测试记忆（RAG）** — 每次 run 结束自动提炼 lesson，下次相同场景自动注入，覆盖 scenario 步骤里的错误方式
- **多 LLM 支持** — OpenAI / Gemini / DeepSeek / Groq，一行切换
- **多项目通用** — 一套代码，每个项目一个 YAML 配置

## 架构

```
┌────────────────────────────────────────────────────────────┐
│            UI Agent (ReAct / Analysis) — 无 DB 访问          │
│           Orchestrator + StuckDetector 控制模式切换          │
├──────────┬──────────┬──────────┬──────────────────────────┤
│Playwright│  Code    │Memory    │  verify_in_db (tool)     │
│  MCP     │  Tools   │  RAG     │  ┌────────────────────┐  │
│ 操作页面  │ 读代码   │JSONL+tag │  │   DB Sub-Agent     │  │
│ 截图/快照 │ 搜索     │lesson注入│  │  独立 Runner.run() │  │
│ auto-snap│          │          │  │  DB MCP (只读)     │  │
│          │          │          │  │  Schema Hints      │  │
│          │          │          │  │  Network Log       │  │
│          │          │          │  │  Workflow Summary   │  │
│          │          │          │  │  db_checks (YAML)  │  │
│          │          │          │  └────────────────────┘  │
├──────────┴──────────┴──────────┴──────────────────────────┤
│        LLM Provider (OpenAI / Gemini / DeepSeek / ...)     │
├────────────────────────────────────────────────────────────┤
│                  Project Profile (YAML)                     │
│         每个项目独立配置环境、认证、LLM、边界、工具            │
└────────────────────────────────────────────────────────────┘
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
# OpenAI
export OPENAI_API_KEY=你的key

# 或 Gemini（有免费额度）
export GEMINI_API_KEY=你的key
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
  provider: "openai"
  model_name: "gpt-4o"
environment:
  base_url: "https://staging.myapp.com"
code:
  root_dir: "/home/user/my-project"
```

### 4. 验证 Profile

```bash
test-agent validate-profile profiles/my_project.yaml
```

### 5. 运行测试

```bash
# 直接传 scenario
test-agent test \
  -p profiles/my_project.yaml \
  -s "购买产品 A：加入购物车，checkout，填写地址，付款，确认订单成功"

# 使用 profile 里预定义的 scenario 名
test-agent test -p profiles/my_project.yaml -s checkout

# 列出所有可用 scenario
test-agent test -p profiles/my_project.yaml
```

---

## Workflow

```
            你写的                      Agent 自动完成
    ┌───────────────────┐     ┌─────────────────────────────────────┐
    │  1. Profile YAML  │     │  3. 加载 Memory — 检索相关历史 lesson  │
    │  (一次性，per项目)  │     │  4. 拆解 scenario 为步骤              │
    │                   │     │  5. 用 Playwright 逐步执行页面操作    │
    │  2. --scenario    │────▶│  6. 遇到障碍自己解决（登录/弹窗/等待） │
    │  (每次测试一句话)   │     │  7. 关键步骤截图                      │
    │                   │     │  8. 查 DB 验证数据正确性              │
    └───────────────────┘     │  9. 输出结构化报告 (pass/fail)        │
                              │  10. LessonWriter 提炼 lesson 写入记忆 │
                              └─────────────────────────────────────┘
```

### 详细执行流程

```
输入: -s "购买产品 A，验证订单数据"
        │
        ▼
1. 加载 Profile
   ├── 读取项目配置（环境/认证/边界）
   ├── 创建 LLM（Gemini/OpenAI/...）
   ├── 检索 Memory — 按 tags 匹配历史 lesson，注入 prompt（优先级高于 scenario 步骤）
   └── 启动 MCP servers（Playwright + DB）
        │
        ▼
2. ReAct 执行循环
   ├── Think: "第一步应该打开商品页"
   ├── Act:   browser_navigate → 商品页
   ├── Observe: snapshot（当前轮完整 ARIA tree，历史轮只保留 URL/title）
   ├── Think: "找到 Add to Cart 按钮 ref=e144"
   ├── Act:   browser_click ref=e144
   ├── Observe: click result 内含更新后的 snapshot
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
3. 数据验证（流程完成后触发）
   ├── UI agent 从成功页提取业务数据（order_id / total / email 等）存入 extracted_data
   ├── 调用 verify_in_db(extracted_data) 工具
   │   └── 启动独立 DB Sub-Agent（新 Runner.run，context 从零开始）
   │       ├── 自动注入 Workflow Summary（UI agent 做了什么）
   │       ├── 自动注入 Network Log（浏览器 mutation API 请求 + request body）
   │       ├── 注入 Schema Hints（从本地 cache 关键词匹配，最多 20 张表）
   │       ├── 注入 db_checks（如果 scenario 配置了验证清单）
   │       ├── 可选 grep_code 查找代码中的表关系（1-2 次）
   │       ├── 一次性批量执行 2-3 条 SELECT 验证
   │       └── 返回 DataVerification JSON 数组给 UI agent
   └── UI agent 将结果原样写入报告的 data_verifications（不修改）
        │
        ▼
4. 卡住检测（StuckDetector，全程监控）
   ├── 连续 3 次相同 tool call → 卡住
   ├── 最近 5 次结果完全相同 → 卡住
   └── 超过 stuck_budget_ratio 仍无报告 → 卡住
   → 自动切换 Analysis 模式：停止调用工具，汇总证据，输出分析报告
        │
        ▼
5. 输出报告
   ├── 终端: 彩色表格（Steps + Verifications）
   ├── JSON: 完整结构化报告
   ├── trace.md / trace.jsonl: 完整执行轨迹（用于调试）
   └── usage summary: LLM token 用量统计
        │
        ▼
6. LessonWriter（独立 LLM call）
   ├── 分析本次 run 的 steps / issues / next_steps
   ├── 生成 1 段 actionable lesson + tags（checkout, p-16227, bank-transfer...）
   └── 写入 Memory JSONL — 下次相同场景自动检索注入
```

---

## Memory 系统（RAG）

每次 run 结束后，`LessonWriter` agent 自动提炼一段 actionable lesson 并打上 tags 写入 JSONL。下次运行相同场景时，按 tag 匹配检索，将相关 lesson 注入 system prompt。

**Lesson 优先级高于 scenario 步骤**。如果 lesson 说"不要通过 modal 的 View Cart 按钮，直接 navigate 到 /cart"，agent 会跳过 scenario 里对应的步骤。

```yaml
# profile 配置
memory:
  enabled: true
  path: "./memory/{project_name}.jsonl"   # {project_name} 自动替换
  max_entries_in_prompt: 2                 # 每次注入的最大 lesson 数
```

---

## Token 控制

Playwright 产品页的完整 ARIA tree 可达 900+ 行 / ~55K chars。`MCPToolOutputFilter` 分两层处理：

| 轮次 | 处理方式 |
|------|---------|
| **当前轮（recent）** | 保留完整 interactive ARIA tree（button、link、textbox 等 + ref 节点），模型能看到所有可操作元素 |
| **历史轮（old turns）** | 丢弃 snapshot ARIA tree，只保留 `### Page`（URL + title），标注 `[snapshot omitted]` |

历史轮模型只需知道"之前在哪个页面做了什么操作"，不需要看当时页面的全部元素。此策略将 old snapshot 从 ~7,000 tokens/条 降到 ~50 tokens/条，大幅降低长 run 的 TPM 压力。

**DB Sub-Agent 独立 context**：DB 验证在独立的 `Runner.run()` 里执行，context 从零开始，不携带 UI 流程的任何历史，单次请求仅 ~3-5K tokens。

**Rate limit retry**：所有 LLM client 设置 `max_retries=5`，SDK 内部对 429 自动指数退避重试，不重启场景。

## DB Schema Cache + Schema Hints

DB Sub-Agent 第一次运行时会调用 `describe_table` 探索表结构，结果自动缓存到本地 JSON 文件：

```
memory/db_schema_{project_name}.json
```

后续运行时，`verify_in_db` 从 cache 中**关键词匹配**相关表（从 UI data + workflow summary + network log 提取关键词），最多注入 20 张表的 schema 到 DB agent prompt。DB agent 直接使用这些表名和列名写 SQL，无需额外 `describe_table` 调用。

Cache 按 `database.table` 为 key 存储，有新表时自动追加。

### 预抓取 Schema Cache

可以在首次运行前批量抓取所有表结构，避免 DB agent 浪费 turn 做 `describe_table`：

```bash
# 抓指定数据库的所有表
uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml -d inkstation

# 增量抓取（跳过已缓存的表）
uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml -d inkstation --skip-cached

# 抓所有数据库（自动跳过 mysql/sys/information_schema/performance_schema）
uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml

# 自定义输出路径
uv run python scripts/cache_db_schema.py -p profiles/my_project.yaml -o memory/my_cache.json
```

脚本连接 profile 中配置的 DB MCP server，优先使用 `describe_all_tables` 一次性获取整个数据库的全部表结构（1 次调用），如果 MCP server 不支持则自动回退到逐表 `describe_table`。结果写入 `memory/db_schema_{project_name}.json`（与 agent 运行时使用的 cache 文件相同）。多次运行会自动合并，不会覆盖已有缓存。

## Network Log 自动注入

当 UI agent 调用 `verify_in_db` 时，系统自动通过 Playwright MCP 获取浏览器的网络请求日志，过滤出业务 API 的 mutation 请求（POST/PUT/PATCH/DELETE），注入 DB agent 的 prompt。

这样 DB agent 可以看到实际的 API 请求体（如 `{"orders_ref": "116NZXM27", "payment_method_id": 5}`），直接知道正确的字段名和表结构映射，无需猜测。

- **自动过滤**：只保留 `allowed_domains` 内的请求，排除第三方（forter、Google Analytics 等）
- **零额外开销**：UI agent 不需要做任何事，`verify_in_db` 内部自动获取
- **传统 Form POST**：不走 fetch/XHR 的页面刷新不会被捕获（这类场景依赖 schema cache）

## Scenario 级 db_checks

Profile 的 scenario 可以配置自然语言的 DB 验证清单。DB agent 收到后直接按清单执行，不再自主探索，大幅减少 LLM 调用。

```yaml
scenarios:
  checkout:
    description: |
      "Test checkout flow..."
    db_checks:
      - "orders 表中存在该订单，order_total 正确"
      - "payment_method 为 Bank Transfer"
      - "orders_status_history 中有对应的状态记录"

  newsletter:
    description: |
      "Subscribe Shopping Cart Reminder Email"
    db_checks:
      - "customer_newsletter_subscriptions.abandoned_cart 的值是否为1"
```

没有 `db_checks` 的 scenario 仍走自主发现路线（workflow + network log + schema hints + code grep）。也支持旧格式（纯字符串 scenario），完全向后兼容。

## Auto-Snapshot

`browser_click` 和 `browser_navigate` 触发页面刷新后，snapshot 里的 element refs 会失效。系统通过 hooks 在这两个操作后**自动调用 `browser_snapshot`**，将新鲜的 snapshot 注入下一轮 context，替换过时的 refs。

同时，`MCPToolOutputFilter` 的 same-page boundary 逻辑确保**同一页面 URL 下的 snapshot 不会被历史轮截断**（如 fill_form 等无 URL 的操作被视为同一页面）。

---

## CLI 参考

```bash
# 执行测试场景
test-agent test [OPTIONS]

Options:
  -p, --profile TEXT     Project Profile YAML 路径（必填）
  -s, --scenario TEXT    测试场景描述或 profile 中的 scenario 名
  -o, --output TEXT      报告输出文件路径（默认输出到终端）
  --max-steps INT        覆盖 profile 中的 max_steps
  -v, --verbose          显示详细日志

# 验证 profile
test-agent validate-profile profiles/my_project.yaml
```

---

## LLM 切换

Profile 的 `model` 字段，一行切换：

```yaml
# OpenAI
model:
  provider: "openai"
  model_name: "gpt-4o"

# Gemini（有免费额度）
model:
  provider: "gemini"
  model_name: "gemini-2.0-flash"

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

对应的环境变量自动查找（`GEMINI_API_KEY`、`OPENAI_API_KEY` 等），也可通过 `api_key_env` 显式指定。

---

## Profile 完整参考

```yaml
project:
  name: "项目名"                              # 必填
  description: "项目简介"

# --- LLM ---
model:
  provider: "openai"                          # openai | gemini | deepseek | groq | custom
  model_name: "gpt-4o"
  api_key_env: "OPENAI_API_KEY"              # 不填则自动按 provider 查找
  base_url: null                              # 自定义端点（custom provider 用）

# --- 环境 ---
environment:
  type: "web"
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
  branch: "main"
  entry_dirs:
    - "src/pages"
    - "src/api"
  config_files:
    - ".env.staging"

# --- 记忆 ---
memory:
  enabled: true
  path: "./memory/{project_name}.jsonl"      # {project_name} 自动替换
  max_entries_in_prompt: 2                   # 注入 prompt 的最大 lesson 数

# --- MCP 服务 ---
mcp_servers:
  playwright:
    enabled: true
    command: "npx"
    args: ["@playwright/mcp@latest", "--browser", "chromium", "--timeout-action", "15000"]
    cwd: "./artifacts/playwright"            # snapshot/截图输出目录
    cache_tools_list: true
    client_session_timeout_seconds: 30       # 必须 > timeout-action / 1000
    allowed_tools:                           # 限制 agent 可用的 browser 工具
      - browser_navigate
      - browser_snapshot
      - browser_click
      - browser_type
      - browser_fill_form
      - browser_select_option
      - browser_press_key
      - browser_handle_dialog
      - browser_wait_for
      - browser_take_screenshot
  database:
    enabled: true
    role: database                              # 标记为 DB server，路由给 DB Sub-Agent
    command: "node"
    args: ["./mcp-servers/db-mcp/index.js"]
    env:
      DB_TYPE: "mysql"
      DB_HOST_ENV: "DB_HOST"
      DB_PORT_ENV: "DB_PORT"
      DB_USER_ENV: "DB_USER"
      DB_PASSWORD_ENV: "DB_PASSWORD"
# --- 边界 ---
boundaries:
  readonly: true
  forbidden_actions:                          # SQL 黑名单
    - "DELETE FROM"
    - "DROP TABLE"
    - "INSERT INTO"
    - "UPDATE"
  max_steps: 40
  max_turns: 20
  stuck_budget_ratio: 0.85                    # 超过该比例仍无报告则切 Analysis 模式
  allowed_domains:
    - "staging.example.com"

# --- 预定义 scenario（可选）---
scenarios:
  # 简单格式（纯描述）
  login: |
    Test login flow with valid and invalid credentials.

  # 结构化格式（带 DB 验证清单）
  checkout:
    description: |
      Add product to cart and complete checkout via Bank Transfer.
    db_checks:                                 # 可选，自然语言验证项
      - "orders 表中存在该订单，order_total 正确"
      - "payment_method 为 Bank Transfer"
```

---

## 项目结构

```
src/universal_debug_agent/
├── main.py                  # CLI 入口 (typer) — test / validate-profile
├── config.py                # YAML Profile 加载
├── schemas/
│   ├── profile.py           # ProjectProfile + ScenarioConfig + ModelConfig + BoundariesConfig 等
│   └── report.py            # ScenarioReport + ScenarioStep + DataVerification
├── agents/
│   ├── brain.py             # create_brain_agent（ReAct + Analysis 模式）
│   ├── db_agent.py          # DB Sub-Agent（verify_in_db 工具内部调用）
│   └── prompts.py           # System prompts（ReAct + Analysis 双模式）
├── orchestrator/
│   ├── state_machine.py     # InvestigationOrchestrator + StuckDetector
│   ├── hooks.py             # InvestigationHooks：tool 监控、auto-snapshot、卡住检测
│   └── input_filters.py     # MCPToolOutputFilter：
│                            #   current turn → 完整 interactive ARIA tree
│                            #   old turns    → snapshot 全删，只留 URL/title
│                            #   same-page boundary → 同 URL 下不截断 snapshot
├── models/
│   └── factory.py           # LLM 工厂（OpenAI/Gemini/DeepSeek/...）
├── memory/
│   ├── store.py             # JSONL 存储：tag 倒排索引 + 场景相似度检索
│   └── lesson.py            # LessonWriter：run 结束后提炼 lesson + tags
├── observability/
│   ├── llm_usage.py         # per-run token 用量统计（JSONL）
│   └── trace_recorder.py    # 执行轨迹落盘（trace.md + trace.jsonl）
├── mcp/
│   └── factory.py           # MCP server 工厂（Playwright + DB）
└── tools/
    ├── auth_tools.py        # get_test_account(role) — 从 profile 读测试账号
    ├── code_tools.py        # read_file / grep_code / list_directory（沙箱化）
    ├── db_tool.py           # verify_in_db — 触发 DB Sub-Agent；管理 schema cache；
    │                        #   自动获取 network log + workflow summary + schema hints
    └── report_tool.py       # submit_report — 结构化报告提交，退出 ReAct 循环
```

---

## V1 边界

| 做 | 不做 |
|---|---|
| 执行任意业务流程（Playwright） | 自动改代码 |
| 数据验证（DB 只读查询） | 写数据库 |
| Scenario 级 db_checks 精确验证 | 自动提 PR |
| Auto-snapshot + same-page boundary | 解 CAPTCHA / 2FA |
| Network log + workflow summary 自动注入 DB agent | 无限制浏览外部域名 |
| 自动处理登录/弹窗等障碍 | |
| 读本地代码辅助理解 | |
| 结构化 pass/fail 报告 | |
| JSONL 测试记忆 + RAG lesson 检索 | |
| 多 LLM provider 切换 | |

## Roadmap

- **V2**: Contract 固化 — LLM 第一次探索后生成验证 contract，后续确定性复用，无需每次 ReAct
- **V3**: 多 agent 并行执行 + 智能测试策略生成

详见 [docs/PLAN_V1.md](docs/PLAN_V1.md) | [docs/PLAN_V2.md](docs/PLAN_V2.md) | [docs/PLAN_V3.md](docs/PLAN_V3.md)。

## License

MIT
