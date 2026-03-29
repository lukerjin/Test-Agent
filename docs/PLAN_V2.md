# Universal Test Agent - V2 Implementation Plan

## V2 目标：探索 → 固化（Contract Solidification）

V1 是纯 NL：LLM 自由执行 scenario，自由决定验证什么。
V2 加两个能力：

1. **Contract 固化** — LLM 第一次跑出的验证项自动保存为 contract，后续复用
2. **Playwright 脚本生成** — LLM 的操作序列导出为 `.spec.ts`，CI 直接跑

**核心价值**：第一次 LLM 探索，之后确定性复用。不需要人从零写 contract。

---

## Part 1: Contract Solidification

### 问题

V1 同一个 scenario 跑两次，验证项可能不同：
- 第一次查了 orders + order_items + payments
- 第二次只查了 orders 就说 pass 了

不满足 "same reliable verification passes"。

### 解决方案：两阶段模式

```
第一次跑 (explore 模式):
  NL scenario → NL execution → NL verification → report
                                    │
                                    ▼
                            --save-contract
                                    │
                                    ▼
                    scenarios/buy_product_a.yaml
                    (LLM 生成，人可以改)

之后跑 (contract 模式):
  scenario.yaml → NL execution → CODE verification → report
                                (固定 SQL + expected)
```

### 1.1 Contract Schema

```yaml
# scenarios/buy_product_a.yaml
scenario:
  name: "购买产品 A"
  description: "加入购物车，checkout，填写地址，付款"

  # LLM 自动生成，人可以审核/修改
  required_verifications:
    - name: "订单已创建"
      query: "SELECT COUNT(*) as cnt FROM orders WHERE user_id = {test_user_id} AND created_at > '{test_start_time}'"
      expect: "cnt >= 1"
      severity: "high"

    - name: "order_items 包含产品 A"
      query: "SELECT product_id, quantity FROM order_items WHERE order_id = {last_order_id}"
      expect: "product_id = 'A' AND quantity = 1"
      severity: "high"

    - name: "支付状态正确"
      query: "SELECT status FROM payments WHERE order_id = {last_order_id}"
      expect: "status = 'completed'"
      severity: "high"

    - name: "库存已扣减"
      query: "SELECT stock FROM products WHERE id = 'A'"
      expect: "stock = {original_stock} - 1"
      severity: "medium"

  # 必须产出的截图
  required_screenshots:
    - at: "checkout_success"
      description: "付款成功页面"
    - at: "order_detail"
      description: "订单详情页"

  # 运行时变量（由 agent 在执行过程中填充）
  variables:
    test_user_id: null      # agent 登录后从 DB 查到
    test_start_time: null   # 测试开始时间戳
    last_order_id: null     # agent 从成功页面或 DB 获取
    original_stock: null    # agent 执行前查一次库存
```

### 1.2 Pydantic Schema

```python
# src/universal_debug_agent/schemas/contract.py

class VerificationItem(BaseModel):
    name: str
    query: str                  # SQL with {variable} placeholders
    expect: str                 # 期望条件表达式
    severity: str = "high"

class ScreenshotRequirement(BaseModel):
    at: str                     # 时间点标识
    description: str = ""

class ScenarioContract(BaseModel):
    name: str
    description: str = ""
    required_verifications: list[VerificationItem] = []
    required_screenshots: list[ScreenshotRequirement] = []
    variables: dict[str, str | None] = {}   # runtime 填充
```

### 1.3 Contract 自动生成

Agent 跑完 explore 模式后，从 `ScenarioReport.data_verifications` 自动提取：

```python
# src/universal_debug_agent/contract/generator.py

class ContractGenerator:
    def generate(self, report: ScenarioReport) -> ScenarioContract:
        """从 LLM 跑出的报告自动生成 contract"""
        verifications = []
        for v in report.data_verifications:
            verifications.append(VerificationItem(
                name=v.check_name,
                query=v.query,
                expect=v.expected,
                severity=v.severity,
            ))

        screenshots = []
        for e in report.evidence:
            if e.type == EvidenceType.SCREENSHOT:
                screenshots.append(ScreenshotRequirement(
                    at=e.source,
                    description=e.description,
                ))

        return ScenarioContract(
            name=report.scenario_summary,
            verifications=verifications,
            screenshots=screenshots,
        )
```

### 1.4 Contract 执行器

```python
# src/universal_debug_agent/contract/executor.py

class ContractExecutor:
    """跑完 NL execution 后，强制执行 contract 里的验证"""

    async def verify(self, contract: ScenarioContract, db_tool, variables: dict) -> list[DataVerification]:
        results = []
        for item in contract.required_verifications:
            # 替换变量
            query = item.query.format(**variables)
            # 执行 SQL（通过 DB MCP）
            actual = await db_tool.execute(query)
            # 判断是否满足 expect
            passed = self._evaluate(actual, item.expect, variables)
            results.append(DataVerification(
                check_name=item.name,
                query=query,
                expected=item.expect,
                actual=str(actual),
                status=StepStatus.PASS if passed else StepStatus.FAIL,
                severity=item.severity,
            ))
        return results
```

### 1.5 CLI 扩展

```bash
# 探索模式：跑一次，保存 contract
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "购买产品 A" \
  --save-contract scenarios/buy_product_a.yaml

# Contract 模式：用固化的验证跑
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  --contract scenarios/buy_product_a.yaml

# 也可以同时传 scenario 覆盖 description
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  --contract scenarios/buy_product_a.yaml \
  -s "这次用 admin 账号购买产品 A"
```

### 1.6 Orchestrator 集成

```python
class InvestigationOrchestrator:
    async def run(self, scenario: str, contract: ScenarioContract | None = None) -> ScenarioReport:
        # Phase 1: NL execution（和 V1 一样）
        report = await self._run_react(scenario)

        # Phase 2: 如果有 contract，强制跑固定验证
        if contract:
            contract_results = await self.contract_executor.verify(
                contract, self.db_tool, self.runtime_variables
            )
            # 替换 LLM 自己跑的验证，用 contract 的
            report.data_verifications = contract_results
            # 根据 contract 结果重新计算 overall_status
            report.overall_status = self._compute_status(report)

        return report
```

---

## Part 2: Playwright 脚本生成

### 架构

```
V1 Agent 执行 scenario
  └── tool_call_history (Playwright 操作序列)
        │
        ▼
  PlaywrightRecorder (hooks 扩展)
        │
        ▼
  ScriptGenerator
        │  MCP tool calls → Playwright API
        │  + contract.verifications → 注释/TODO
        ▼
  output: tests/test_buy_product_a.spec.ts
        │
        ▼
  CI: npx playwright test
```

### 2.1 PlaywrightRecorder (`orchestrator/recorder.py`)

```python
@dataclass
class PlaywrightAction:
    action: str          # "navigate" | "click" | "fill" | ...
    selector: str = ""
    value: str = ""
    timestamp: float = 0.0

class PlaywrightRecorder:
    TOOL_MAP = {
        "browser_navigate": "navigate",
        "browser_click": "click",
        "browser_fill": "fill",
        "browser_screenshot": "screenshot",
        "browser_select": "select",
        "browser_hover": "hover",
        "browser_wait": "wait",
        "browser_press_key": "press_key",
    }

    def __init__(self):
        self.actions: list[PlaywrightAction] = []

    def record(self, tool_name: str, tool_args: dict) -> None:
        action_type = self.TOOL_MAP.get(tool_name)
        if action_type:
            self.actions.append(PlaywrightAction(
                action=action_type,
                selector=tool_args.get("selector", ""),
                value=tool_args.get("value", tool_args.get("url", "")),
            ))
```

### 2.2 ScriptGenerator (`generators/script_generator.py`)

```python
class ScriptGenerator:
    ACTION_MAP = {
        "navigate":   "await page.goto('{value}');",
        "click":      "await page.click('{selector}');",
        "fill":       "await page.fill('{selector}', '{value}');",
        "screenshot": "await expect(page).toHaveScreenshot();",
        "select":     "await page.selectOption('{selector}', '{value}');",
        "hover":      "await page.hover('{selector}');",
        "wait":       "await page.waitForSelector('{selector}');",
        "press_key":  "await page.keyboard.press('{value}');",
    }

    def generate(self, actions: list[PlaywrightAction], report: ScenarioReport) -> str:
        lines = ["import { test, expect } from '@playwright/test';", ""]
        lines.append(f"test('{self._escape(report.scenario_summary)}', async ({{ page }}) => {{")

        for action in actions:
            template = self.ACTION_MAP.get(action.action)
            if template:
                code = template.format(
                    selector=self._escape(action.selector),
                    value=self._escape(action.value),
                )
                lines.append(f"  {code}")

        # DB verification 作为注释提醒
        for v in report.data_verifications:
            lines.append(f"  // DB Check: {v.check_name}")
            lines.append(f"  // Query: {v.query}")
            lines.append(f"  // Expected: {v.expected}")

        lines.append("});")
        return "\n".join(lines)
```

### 2.3 生成的脚本示例

```typescript
import { test, expect } from '@playwright/test';

test('购买产品 A', async ({ page }) => {
  await page.goto('https://staging.example.com/products');
  await page.click('[data-product="A"] .add-to-cart');
  await page.click('.cart-icon');
  await page.click('.checkout-btn');
  await page.fill('#email', 'test@example.com');
  await page.fill('#password', 'test123');
  await page.click('button[type="submit"]');
  await page.fill('#address', '123 Test St');
  await page.click('.pay-btn');
  await expect(page).toHaveScreenshot();

  // DB Check: 订单已创建
  // Query: SELECT COUNT(*) FROM orders WHERE ...
  // Expected: >= 1 row
  // DB Check: 支付状态正确
  // Query: SELECT status FROM payments WHERE ...
  // Expected: status = 'completed'
});
```

---

## Part 2.5: CLI 完整扩展

```bash
# 探索 + 保存 contract
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "购买产品 A" \
  --save-contract scenarios/buy_product_a.yaml

# 用 contract 跑（验证确定性）
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  --contract scenarios/buy_product_a.yaml

# 探索 + 生成 Playwright 脚本
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "购买产品 A" \
  --generate-script tests/test_buy_a.spec.ts

# 三合一：跑 + 保存 contract + 生成脚本
python -m universal_debug_agent test \
  -p profiles/ecommerce.yaml \
  -s "购买产品 A" \
  --save-contract scenarios/buy_product_a.yaml \
  --generate-script tests/test_buy_a.spec.ts

# 验证 contract 文件
python -m universal_debug_agent validate-contract scenarios/buy_product_a.yaml
```

---

## 实现步骤

### Phase 1: Contract Solidification（核心）
1. `schemas/contract.py` — ScenarioContract Pydantic model
2. `contract/generator.py` — 从 ScenarioReport 自动生成 contract
3. `contract/executor.py` — 执行 contract 里的固定验证
4. `contract/loader.py` — 加载 YAML contract 文件
5. Orchestrator 集成 — contract 模式 vs explore 模式
6. CLI: `--save-contract` + `--contract`
7. 测试

### Phase 2: Playwright 脚本生成
8. `orchestrator/recorder.py` — PlaywrightRecorder
9. `generators/script_generator.py` — ScriptGenerator
10. Hooks 集成 — recorder 接入 on_tool_start
11. CLI: `--generate-script`
12. 测试

---

## V2 新增文件

| 文件 | 职责 |
|------|------|
| `schemas/contract.py` | ScenarioContract schema |
| `contract/__init__.py` | |
| `contract/generator.py` | Report → Contract 自动生成 |
| `contract/executor.py` | 执行固定验证项 |
| `contract/loader.py` | 加载 YAML contract |
| `orchestrator/recorder.py` | Playwright 操作录制 |
| `generators/script_generator.py` | .spec.ts 生成 |
| `tests/test_contract.py` | contract 测试 |
| `tests/test_recorder.py` | recorder 测试 |
| `tests/test_generator.py` | generator 测试 |

## V2 新增依赖

无。全用现有依赖（pydantic, pyyaml）。

---

## 版本对比

| 能力 | V1 | V2 |
|------|----|----|
| NL scenario 执行 | Y | Y |
| NL 数据验证（LLM 决定查什么） | Y | Y |
| **Contract 固化（确定性验证）** | - | **Y** |
| **Contract 自动生成（从 LLM 报告）** | - | **Y** |
| **Playwright 脚本导出** | - | **Y** |
| 多 LLM 支持 | Y | Y |
| Memory | Y | Y |

### 关键区别

```
V1: 每次跑验证不一定一致（LLM 自由决定）
V2: 第一次探索，之后验证固定可重复
```
