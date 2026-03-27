# Universal Debug Agent - V3 Implementation Plan

## V3 目标：Memory 升级 + 智能调查策略

V1 用 JSONL 做全量 prompt 注入，V2 加了脚本生成。V3 解决两个问题：
1. **Memory 规模化** — 当历史调查超过几百条，JSONL 全量注入撑不住，需要 RAG
2. **智能调查策略** — Agent 根据历史模式自动选择调查路径，而不是每次从零开始

---

## 架构演进

```
V1 Memory (JSONL)           V3 Memory (Hybrid)
┌──────────────┐            ┌───────────────────────────┐
│  全量读取     │     →     │  少量记录: JSONL 全量注入   │
│  全量注入     │            │  大量记录: RAG 语义检索     │
│  prompt       │            │  + metadata filter        │
└──────────────┘            └───────────────────────────┘

V1 调查策略                  V3 调查策略
┌──────────────┐            ┌───────────────────────────┐
│  固定 ReAct   │     →     │  StrategySelector         │
│  卡住→分析    │            │  根据 issue + memory      │
│              │            │  选择最优调查路径          │
└──────────────┘            └───────────────────────────┘
```

---

## Part 1: Memory RAG 升级

### 1.1 混合存储架构

```python
class HybridMemoryStore:
    """混合记忆存储 — 小数据走 JSONL，大数据走 RAG"""

    JSONL_THRESHOLD = 100  # 低于此数量用 JSONL 全量注入

    def __init__(self, jsonl_path: str, vector_store: VectorStore | None = None):
        self.jsonl_store = MemoryStore(jsonl_path)  # 复用 V1
        self.vector_store = vector_store

    def build_prompt_context(self, query: str, max_entries: int = 20) -> str:
        records = self.jsonl_store.load()

        if len(records) <= self.JSONL_THRESHOLD or self.vector_store is None:
            # 小数据: V1 行为，全量注入
            return self.jsonl_store.build_prompt_context(max_entries)
        else:
            # 大数据: RAG 语义检索
            return self._rag_search(query, max_entries)

    def _rag_search(self, query: str, max_entries: int) -> str:
        """用 query (issue 描述) 做语义搜索，返回最相关的历史调查"""
        results = self.vector_store.search(
            query=query,
            n_results=max_entries,
        )
        return self._format_results(results)
```

### 1.2 向量存储抽象

不绑定具体向量数据库，定义接口：

```python
from abc import ABC, abstractmethod


class VectorStore(ABC):
    """向量存储抽象接口"""

    @abstractmethod
    def add(self, record: MemoryRecord) -> None: ...

    @abstractmethod
    def search(self, query: str, n_results: int = 10,
               filters: dict | None = None) -> list[MemoryRecord]: ...

    @abstractmethod
    def count(self) -> int: ...
```

### 1.3 ChromaDB 实现（默认）

```python
class ChromaVectorStore(VectorStore):
    """基于 ChromaDB 的本地向量存储（无需外部服务）"""

    def __init__(self, collection_name: str, persist_dir: str):
        import chromadb
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(collection_name)

    def add(self, record: MemoryRecord) -> None:
        self.collection.add(
            documents=[record.model_dump_json()],
            metadatas=[{
                "classification": record.classification,
                "timestamp": record.timestamp,
                "has_dead_ends": len(record.dead_ends) > 0,
            }],
            ids=[f"{record.timestamp}_{hash(record.issue)}"],
        )

    def search(self, query: str, n_results: int = 10,
               filters: dict | None = None) -> list[MemoryRecord]:
        where = filters or {}
        results = self.collection.query(
            query_texts=[query],
            n_results=n_results,
            where=where if where else None,
        )
        return [
            MemoryRecord.model_validate_json(doc)
            for doc in results["documents"][0]
        ]
```

### 1.4 JSONL → RAG 迁移脚本

```python
def migrate_jsonl_to_vector(jsonl_path: str, vector_store: VectorStore) -> int:
    """一键迁移: 读取 JSONL 的所有记录，写入向量存储"""
    store = MemoryStore(jsonl_path)
    records = store.load()
    for record in records:
        vector_store.add(record)
    return len(records)
```

**关键点**：V1 的 JSONL 记录结构零改动，直接当 RAG document。`classification`、`timestamp` 等字段变成 metadata filter。

### 1.5 Profile 扩展

```yaml
memory:
  enabled: true
  path: "./memory/{project_name}.jsonl"       # JSONL 始终写入（备份 + 小数据源）
  max_entries_in_prompt: 20
  rag:
    enabled: false                             # 默认关闭，数据量大时开启
    provider: "chroma"                         # chroma | (未来: pinecone, qdrant)
    persist_dir: "./memory/vector/{project_name}"
    auto_migrate: true                         # 启动时自动从 JSONL 同步到向量库
```

---

## Part 2: 智能调查策略

### 2.1 StrategySelector

根据 issue 描述 + memory 历史，选择最优的初始调查策略：

```python
class InvestigationStrategy(Enum):
    FULL_STACK = "full_stack"       # 默认: UI + DB + Code 全查
    UI_FIRST = "ui_first"           # 先看页面，再查代码
    DATA_FIRST = "data_first"       # 先查 DB，再看页面
    CODE_FIRST = "code_first"       # 先读代码，再验证
    REGRESSION = "regression"       # 回归: 基于历史类似问题快速验证


class StrategySelector:
    """根据 issue + memory 选择调查策略"""

    def select(self, issue: str, memory_records: list[MemoryRecord]) -> InvestigationStrategy:
        # 1. 如果历史中有高度相似的 issue → REGRESSION
        similar = self._find_similar(issue, memory_records)
        if similar and similar[0].confidence > 0.8:
            return InvestigationStrategy.REGRESSION

        # 2. 如果历史中同类 issue 主要是 data 问题 → DATA_FIRST
        classifications = [r.classification for r in memory_records[-10:]]
        if classifications.count("data") > len(classifications) * 0.5:
            return InvestigationStrategy.DATA_FIRST

        # 3. 关键词匹配
        issue_lower = issue.lower()
        if any(kw in issue_lower for kw in ["显示", "页面", "ui", "样式", "空白"]):
            return InvestigationStrategy.UI_FIRST
        if any(kw in issue_lower for kw in ["数据", "状态", "权限", "缺失"]):
            return InvestigationStrategy.DATA_FIRST
        if any(kw in issue_lower for kw in ["报错", "error", "exception", "500"]):
            return InvestigationStrategy.CODE_FIRST

        return InvestigationStrategy.FULL_STACK
```

### 2.2 策略注入 Prompt

不同策略生成不同的 ReAct prompt 前缀：

```python
STRATEGY_PROMPTS = {
    InvestigationStrategy.FULL_STACK: """
        Investigate systematically: check the UI, query the database, and read the code.
    """,
    InvestigationStrategy.UI_FIRST: """
        Start by opening the page and observing the UI behavior.
        Take screenshots first, then investigate code and data if needed.
    """,
    InvestigationStrategy.DATA_FIRST: """
        Start by querying the database to verify data state.
        Compare with expected values, then check UI and code if needed.
    """,
    InvestigationStrategy.CODE_FIRST: """
        Start by reading the relevant code (error handlers, API routes).
        Look for the error pattern, then verify with UI and data.
    """,
    InvestigationStrategy.REGRESSION: """
        A similar issue was found before. Start by checking if the same root cause applies.
        Verify the previous fix is still in place, then look for new factors.
        Previous finding: {previous_finding}
    """,
}
```

### 2.3 Orchestrator 集成

```python
class InvestigationOrchestrator:
    async def run(self, issue: str) -> InvestigationReport:
        # V3: 选择调查策略
        strategy = self.strategy_selector.select(issue, self.memory_records)
        logger.info(f"Selected strategy: {strategy.value}")

        # 策略影响 prompt
        react_agent = create_brain_agent(
            ...
            strategy=strategy,
            memory_context=self.memory_context,
        )
        ...
```

---

## Part 3: 其他 V3 增强

### 3.1 Memory 质量管理

```python
class MemoryQualityManager:
    """管理 memory 质量，避免噪音积累"""

    def should_save(self, report: InvestigationReport) -> bool:
        """只保存有价值的调查结果"""
        # 跳过: 无法复现的 issue
        if report.classification == IssueClassification.UNKNOWN and not report.root_cause_hypotheses:
            return False
        # 跳过: 置信度太低的结果
        if report.root_cause_hypotheses and report.root_cause_hypotheses[0].confidence < 0.3:
            return False
        return True

    def deduplicate(self, records: list[MemoryRecord]) -> list[MemoryRecord]:
        """去重: 同一个 root cause 不需要重复记忆"""
        seen_causes: set[str] = set()
        unique: list[MemoryRecord] = []
        for record in records:
            key = record.root_cause.lower().strip()
            if key and key in seen_causes:
                continue
            seen_causes.add(key)
            unique.append(record)
        return unique
```

### 3.2 Memory CLI 工具

```bash
# 查看项目 memory
python -m universal_debug_agent memory list -p profiles/my_project.yaml

# 搜索 memory
python -m universal_debug_agent memory search -p profiles/my_project.yaml -q "订单状态"

# 导出为 CSV（给人看）
python -m universal_debug_agent memory export -p profiles/my_project.yaml -o memory.csv

# 迁移到 RAG
python -m universal_debug_agent memory migrate -p profiles/my_project.yaml

# 清理低质量记录
python -m universal_debug_agent memory clean -p profiles/my_project.yaml
```

### 3.3 GitHub Issue 自动拉取

V1 deferred 的 `--issue-url` 功能：

```python
async def fetch_github_issue(url: str) -> str:
    """从 GitHub issue URL 拉取标题 + body 作为 issue 描述"""
    # 解析 owner/repo/issue_number
    # 调用 GitHub API (或 gh CLI)
    # 返回格式化的 issue 描述
```

---

## 实现步骤

### Phase 1: Memory RAG（核心）
1. `memory/vector_store.py` — VectorStore 抽象接口
2. `memory/chroma_store.py` — ChromaDB 实现
3. `memory/hybrid_store.py` — 混合存储（JSONL + RAG 自动切换）
4. `memory/migrate.py` — JSONL → RAG 迁移工具
5. Profile 扩展 `memory.rag` 配置
6. 测试: 写入、搜索、迁移、自动切换

### Phase 2: 智能调查策略
7. `orchestrator/strategy.py` — StrategySelector
8. 策略 prompt 模板
9. Orchestrator 集成
10. 测试: 策略选择逻辑

### Phase 3: Memory 管理工具
11. `memory/quality.py` — MemoryQualityManager
12. CLI `memory` 子命令组
13. GitHub issue 拉取

---

## V3 新增依赖

```toml
[project.optional-dependencies]
rag = [
    "chromadb>=0.4",          # 本地向量数据库
]
```

RAG 作为可选依赖，不装也能用（fallback 到 JSONL）。

---

## V3 新增文件

| 文件 | 职责 |
|------|------|
| `memory/vector_store.py` | VectorStore 抽象接口 |
| `memory/chroma_store.py` | ChromaDB 实现 |
| `memory/hybrid_store.py` | 混合存储（自动切换） |
| `memory/migrate.py` | JSONL → RAG 迁移 |
| `memory/quality.py` | Memory 质量管理 |
| `orchestrator/strategy.py` | 调查策略选择器 |
| `tests/test_vector_store.py` | RAG 测试 |
| `tests/test_strategy.py` | 策略选择测试 |

---

## 版本对比

| 能力 | V1 | V2 | V3 |
|------|----|----|-----|
| 调查 + 报告 | Y | Y | Y |
| 多 LLM 支持 | Y | Y | Y |
| JSONL Memory | Y | Y | Y |
| 脚本生成 | - | Y | Y |
| RAG Memory | - | - | Y |
| 智能调查策略 | - | - | Y |
| Memory 管理 CLI | - | - | Y |
| GitHub Issue 拉取 | - | - | Y |
