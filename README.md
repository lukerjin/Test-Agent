# Universal Test Agent

基于 [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) 的通用 E2E 测试执行 + 数据验证 agent。

给它一个业务场景（自然语言），它自己走完整个流程，遇到障碍自己解决，最后验证数据库里的数据对不对。

## 它能做什么

- **执行业务流程** — 打开页面、加购物车、checkout、填地址、付款
- **自动处理障碍** — 没登录就登录，有弹窗就关掉，加载慢就等
- **收集证据** — 关键步骤自动截图、记录页面状态
- **数据验证** — 流程走完后查数据库，确认 orders/order_items/payments 等表数据正确
- **结构化报告** — 每步 pass/fail + 每项数据验证 pass/fail
- **测试记忆** — 记住过去的测试结果，下次更快
- **多 LLM 支持** — OpenAI / Gemini / DeepSeek / Groq 等

## 架构

```
┌─────────────────────────────────────────────────┐
│           Test Agent (单 Agent)                  │
│         ReAct 模式 ↔ Analysis 模式               │
│      (Orchestrator + StuckDetector 控制切换)      │
├──────────┬──────────┬──────────┬────────────────┤
│Playwright│ DB MCP   │  Code    │    Memory      │
│  MCP     │ (MySQL)  │  Tools   │   (JSONL)      │
│ 操作页面  │ 只读验证  │ 读代码   │ 历史测试记录    │
│ 截图     │ 查数据    │          │                │
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
# Gemini（有免费额度）
export GEMINI_API_KEY=你的key

# 或 OpenAI
export OPENAI_API_KEY=你的key
```

### 3. 创建 Project Profile

```bash
cp profiles/example_project.yaml profiles/my_project.yaml
```

编辑关键字段（完整参考见下方）：

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

### 4. 运行测试

```bash
python -m universal_debug_agent test \
  -p profiles/my_project.yaml \
  -s "购买产品 A：加入购物车，checkout，填写地址，付款，确认订单成功"
```

---

## 使用教程

### 场景 1: 购买流程测试

```bash
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "购买产品 iPhone 16：搜索产品，加入购物车，进入 checkout，
      如果没登录先登录，填写收货地址，选信用卡支付，完成付款。
      验证：orders 表有新订单，order_items 包含 iPhone 16，
      库存减少 1，支付状态为 completed"
```

Agent 会：
1. 打开网站，搜索 "iPhone 16"
2. 点击加入购物车
3. 进入 checkout → 检测到需要登录 → 自动用测试账号登录
4. 填写地址表单
5. 选择支付方式，完成付款
6. 看到成功页面，截图
7. 查 DB 验证: orders, order_items, inventory, payments

### 场景 2: 注册流程测试

```bash
python -m universal_debug_agent test \
  -p profiles/my_app.yaml \
  -s "新用户注册：打开注册页，填写邮箱密码，提交，验证跳转到 dashboard。
      验证 users 表有新记录，email 正确，状态为 active"
```

### 场景 3: 权限测试

```bash
python -m universal_debug_agent test \
  -p profiles/admin.yaml \
  -s "用普通用户登录，尝试访问 /admin/users 页面，
      应该看到 403 或跳转到首页。验证没有越权访问"
```

### 场景 4: 多项目并行

```bash
# 终端 1
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml -s "购买流程"

# 终端 2
python -m universal_debug_agent test \
  -p profiles/admin.yaml -s "用户管理流程"
```

---

## 输出示例

终端会打印彩色摘要表格：

```
┌─ 购买产品 A 的完整流程 ─────────────────────┐
│                   PASS                       │
└──────────────────────────────────────────────┘

        Steps Executed
┌───┬─────────────┬────────┬──────────────────┐
│ # │ Action      │ Status │ Notes            │
├───┼─────────────┼────────┼──────────────────┤
│ 1 │ 打开商品页   │ pass   │ 页面加载成功       │
│ 2 │ 加入购物车   │ pass   │ 购物车数量+1       │
│ 3 │ 登录        │ pass   │ 检测到未登录，自动  │
│ 4 │ 填写地址    │ pass   │ 自动填写测试地址    │
│ 5 │ 完成支付    │ pass   │ 跳转到成功页       │
└───┴─────────────┴────────┴──────────────────┘

     Data Verifications
┌──────────────┬──────────┬──────────┬────────┐
│ Check        │ Expected │ Actual   │ Status │
├──────────────┼──────────┼──────────┼────────┤
│ 订单已创建    │ 1 row    │ 1 row    │ pass   │
│ 产品匹配     │ iPhone16 │ iPhone16 │ pass   │
│ 库存扣减     │ stock=99 │ stock=99 │ pass   │
│ 支付状态     │completed │completed │ pass   │
└──────────────┴──────────┴──────────┴────────┘
```

JSON 报告：

```json
{
  "scenario_summary": "购买产品 A 的完整流程",
  "overall_status": "pass",
  "steps_executed": [
    {"step_number": 1, "action": "打开商品页", "status": "pass", "actual_result": "页面加载成功"},
    {"step_number": 2, "action": "加入购物车", "status": "pass", "actual_result": "购物车数量+1"},
    {"step_number": 3, "action": "登录", "status": "pass", "notes": "检测到未登录，自动走登录流程"},
    {"step_number": 4, "action": "填写地址", "status": "pass"},
    {"step_number": 5, "action": "完成支付", "status": "pass", "actual_result": "跳转到成功页"}
  ],
  "data_verifications": [
    {"check_name": "订单已创建", "query": "SELECT ...", "expected": "1 row", "actual": "1 row", "status": "pass"},
    {"check_name": "order_items 正确", "expected": "iPhone 16, qty=1", "actual": "iPhone 16, qty=1", "status": "pass"},
    {"check_name": "库存扣减", "expected": "stock=99", "actual": "stock=99", "status": "pass"},
    {"check_name": "支付状态", "expected": "completed", "actual": "completed", "status": "pass"}
  ],
  "issues_found": [],
  "next_steps": []
}
```

---

## CLI 参考

```bash
# 执行测试场景
python -m universal_debug_agent test [OPTIONS]

Options:
  -p, --profile TEXT     Project Profile YAML 路径（必填）
  -s, --scenario TEXT    测试场景描述（自然语言）
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
  model_name: "gpt-4o"

# DeepSeek
model:
  provider: "deepseek"
  model_name: "deepseek-chat"

# 任意 OpenAI 兼容 API
model:
  provider: "custom"
  model_name: "your-model"
  base_url: "https://your-api.example.com/v1"
  api_key_env: "YOUR_API_KEY"
```

---

## 工作原理

```
输入: "购买产品 A，验证订单数据"
        │
        ▼
1. 加载 Profile → 创建 LLM → 加载 Memory → 创建 MCP
        │
        ▼
2. Agent 拆解场景为步骤，逐步执行:
   ├── Playwright: 打开页面、搜索、点击、填表、提交
   ├── 遇到障碍自动处理（登录、弹窗、加载等待）
   ├── 关键步骤截图
   └── 每步记录 pass/fail
        │
        ▼
3. 业务流程完成后，数据验证:
   ├── DB MCP: SELECT 查询各相关表
   ├── 逐项比对 expected vs actual
   └── 记录每项 pass/fail
        │
        ▼
4. 如果中途卡住 (StuckDetector):
   └── 切换 Analysis 模式，分析已执行的步骤，输出报告
        │
        ▼
5. 输出 TestReport (终端表格 + JSON)
6. 保存到 Memory
```

---

## V1 边界

| 做 | 不做 |
|---|---|
| 执行业务流程（Playwright） | 自动改代码 |
| 数据验证（DB 只读查询） | 写数据库 |
| 自动处理登录/弹窗等障碍 | 自动提 PR |
| 读本地代码辅助理解 | 解 CAPTCHA / 2FA |
| 结构化 pass/fail 报告 | 无限制浏览 |
| JSONL 测试记忆 | |
| 多 LLM provider 切换 | |

## Roadmap

- **V2**: 测试操作序列 → Playwright 测试脚本生成（`.spec.ts`），CI 可跑
- **V3**: Memory RAG + 智能测试策略选择

详见 [docs/PLAN_V1.md](docs/PLAN_V1.md)。

## License

MIT
