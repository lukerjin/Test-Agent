# Universal Debug Agent - V2 Implementation Plan

## V2 目标：探索性测试 → 确定性脚本生成

V1 的 agent 做了一次探索性调查，收集了证据和操作路径。V2 把这个操作路径**导出为 Playwright 测试脚本**，后续 CI 可以直接跑，不再需要 LLM。

**核心价值**：LLM 探索一次，生成永久可用的确定性测试。

---

## 架构

```
V1 Agent 调查
  └── tool_call_history (Playwright 操作序列)
        │
        ▼
  PlaywrightRecorder (V2 hooks 扩展)
        │  记录: navigate, click, fill, screenshot...
        ▼
  ScriptGenerator
        │  MCP tool calls → Playwright API 映射
        │  + report.evidence → 断言生成
        ▼
  output: tests/test_issue_1234.spec.ts
        │
        ▼
  CI: npx playwright test
```

---

## 新增模块

### 1. PlaywrightRecorder (`src/universal_debug_agent/orchestrator/recorder.py`)

扩展现有 hooks，专门记录 Playwright MCP 的 tool call 序列：

```python
from dataclasses import dataclass, field


@dataclass
class PlaywrightAction:
    """一次 Playwright MCP 操作"""
    action: str          # "navigate" | "click" | "fill" | "screenshot" | ...
    selector: str = ""   # CSS selector
    value: str = ""      # fill value, URL, etc.
    timestamp: float = 0.0


class PlaywrightRecorder:
    """从 tool call history 中提取 Playwright 操作序列"""

    # MCP tool name → action type 映射
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
        """从 tool call 中提取 Playwright 操作。非 Playwright 的忽略。"""
        action_type = self.TOOL_MAP.get(tool_name)
        if not action_type:
            return

        self.actions.append(PlaywrightAction(
            action=action_type,
            selector=tool_args.get("selector", ""),
            value=tool_args.get("value", tool_args.get("url", "")),
        ))

    def get_actions(self) -> list[PlaywrightAction]:
        return list(self.actions)

    def has_actions(self) -> bool:
        return len(self.actions) > 0
```

### 2. ScriptGenerator (`src/universal_debug_agent/generators/script_generator.py`)

将 Playwright 操作序列 + 调查报告转为 `.spec.ts` 测试脚本：

```python
class ScriptGenerator:
    """将 Agent 的 Playwright 操作历史转为 .spec.ts 测试脚本"""

    # MCP action → Playwright API 映射
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

    def generate(
        self,
        actions: list[PlaywrightAction],
        report: InvestigationReport,
        test_name: str = "",
    ) -> str:
        """生成完整的 Playwright 测试脚本"""

        name = test_name or report.issue_summary[:60]
        lines: list[str] = []

        # Import
        lines.append("import { test, expect } from '@playwright/test';")
        lines.append("")

        # Test block
        lines.append(f"test('{self._escape(name)}', async ({{ page }}) => {{")

        # Actions
        for action in actions:
            template = self.ACTION_MAP.get(action.action)
            if template:
                code = template.format(
                    selector=self._escape(action.selector),
                    value=self._escape(action.value),
                )
                lines.append(f"  {code}")

        # Assertions from consistency checks
        for check in report.consistency_checks:
            if not check.consistent:
                lines.append(f"  // Cross-validation: {check.ui_source}")
                lines.append(f"  // Expected DB value: {check.db_value}")
                # 生成断言（基于 UI source 推断 selector）
                lines.append(f"  // TODO: Add assertion for {check.ui_source}")

        lines.append("});")
        lines.append("")

        return "\n".join(lines)

    def _escape(self, s: str) -> str:
        return s.replace("'", "\\'").replace("\n", "\\n")
```

### 3. Profile 扩展

```yaml
# profiles/example_project.yaml 新增
script_generation:
  enabled: true
  output_dir: "./generated-tests"       # 生成的 .spec.ts 输出目录
  include_comments: true                 # 在脚本中加注释说明每步的目的
  include_todo_assertions: true          # 为 consistency_check 生成 TODO 断言
```

```python
# schemas/profile.py 新增
class ScriptGenerationConfig(BaseModel):
    enabled: bool = False
    output_dir: str = "./generated-tests"
    include_comments: bool = True
    include_todo_assertions: bool = True
```

### 4. CLI 扩展

```bash
# 调查 + 生成测试脚本
python -m universal_debug_agent investigate \
  -p profiles/my_project.yaml \
  -i "订单状态不一致" \
  --generate-script

# 仅从已有报告生成脚本（不重新调查）
python -m universal_debug_agent generate-script \
  --report report.json \
  --output tests/test_order_status.spec.ts
```

### 5. Hooks 集成

在现有 `InvestigationHooks.on_tool_start` 中增加 recorder 调用：

```python
class InvestigationHooks(RunHooks):
    def __init__(self, stuck_detector, evidence_collector, playwright_recorder=None):
        ...
        self.playwright_recorder = playwright_recorder

    async def on_tool_start(self, context, agent, tool):
        ...
        if self.playwright_recorder:
            self.playwright_recorder.record(tool_name, tool_args_dict)
```

---

## 生成的脚本示例

**输入**：Agent 对 "订单状态显示不一致" 的调查操作序列

**输出** `tests/test_order_status.spec.ts`：
```typescript
import { test, expect } from '@playwright/test';

test('订单状态显示不一致', async ({ page }) => {
  // Step 1: 登录
  await page.goto('https://staging.example.com/login');
  await page.fill('#email', 'admin@example.com');
  await page.fill('#password', 'test123');
  await page.click('button[type="submit"]');

  // Step 2: 导航到订单详情
  await page.goto('https://staging.example.com/orders/1234');

  // Step 3: 验证
  await expect(page).toHaveScreenshot();

  // Cross-validation: /orders/1234 订单状态区域
  // Expected DB value: pending
  // TODO: Add assertion for order status display
});
```

---

## 实现步骤

### Step 1: PlaywrightRecorder
- `orchestrator/recorder.py` — 操作序列记录器
- 集成到 `InvestigationHooks`
- 测试: 验证 MCP tool call 正确映射为 PlaywrightAction

### Step 2: ScriptGenerator
- `generators/script_generator.py` — 脚本生成器
- 模板系统（简单字符串格式化，不需要 Jinja2）
- 测试: 给定 actions + report，验证生成的 .spec.ts 语法正确

### Step 3: Profile + CLI 扩展
- `ScriptGenerationConfig` 加入 profile schema
- CLI 增加 `--generate-script` flag
- CLI 增加 `generate-script` 子命令

### Step 4: 端到端集成
- orchestrator 完成调查后，如果 `script_generation.enabled`，自动调用 generator
- 输出文件到 `output_dir`
- 文件名基于 issue summary 或 timestamp

### Step 5: 测试
- 单元测试: recorder, generator
- 集成测试: 从 mock tool call 历史生成 .spec.ts
- 语法验证: 用 TypeScript compiler 验证生成的脚本

---

## V2 依赖

| 依赖 | 说明 |
|------|------|
| V1 hooks 体系 | PlaywrightRecorder 集成到现有 RunHooks |
| V1 report schema | ConsistencyCheck 用于生成断言 |
| Playwright MCP tool name 规范 | 需要确定 MCP tool name 和参数格式 |

## V2 新增文件

| 文件 | 职责 |
|------|------|
| `orchestrator/recorder.py` | Playwright 操作序列记录器 |
| `generators/script_generator.py` | .spec.ts 脚本生成器 |
| `schemas/profile.py` (扩展) | ScriptGenerationConfig |
| `tests/test_recorder.py` | recorder 测试 |
| `tests/test_generator.py` | generator 测试 |
