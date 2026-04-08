"""Project Profile schema — defines per-project configuration."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectInfo(BaseModel):
    name: str
    description: str = ""


class EnvironmentConfig(BaseModel):
    type: str = Field(default="web", pattern=r"^(web|api|mobile)$")
    base_url: str = ""
    start_command: str | None = None
    health_check_url: str | None = None


class TestAccount(BaseModel):
    role: str
    username_env: str
    password_env: str


class AuthConfig(BaseModel):
    method: str = Field(default="none", pattern=r"^(form|token|cookie|none)$")
    login_url: str = ""
    test_accounts: list[TestAccount] = Field(default_factory=list)


class CodeConfig(BaseModel):
    root_dir: str
    branch: str = "main"
    entry_dirs: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)


class MCPServerConfig(BaseModel):
    enabled: bool = True
    role: str | None = None  # "database" | "browser" | None — used to route servers to sub-agents
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    client_session_timeout_seconds: float | None = None
    cache_tools_list: bool = True
    allowed_tools: list[str] | None = None   # whitelist — only expose these tools
    blocked_tools: list[str] | None = None   # blacklist — hide these tools


class ModelConfig(BaseModel):
    """LLM provider configuration — supports OpenAI, Gemini, and any OpenAI-compatible API."""

    provider: str = "openai"  # openai | gemini | deepseek | groq | together | openrouter
    model_name: str | None = None  # None = use provider default
    api_key_env: str | None = None  # Env var name for API key (e.g. "GEMINI_API_KEY")
    base_url: str | None = None  # Custom endpoint URL (overrides provider default)


class MemoryConfig(BaseModel):
    """Investigation memory — stores past findings per project."""

    enabled: bool = False
    path: str = "./memory/{project_name}.jsonl"  # supports {project_name} placeholder
    max_entries_in_prompt: int = 20  # how many past records to inject into prompt


class FilterConfig(BaseModel):
    """Token-budget-aware filtering parameters for MCP tool outputs."""

    recent_turns: int = 1
    default_max_chars: int = 4_000
    default_preview_chars: int = 800
    aggressive_max_chars: int = 1_500
    aggressive_preview_chars: int = 300
    evidence_preview_chars: int = 1500
    old_turn_summary: bool = True


class BoundariesConfig(BaseModel):
    readonly: bool = True
    forbidden_actions: list[str] = Field(
        default_factory=lambda: ["DELETE FROM", "DROP TABLE", "INSERT INTO", "UPDATE"]
    )
    max_steps: int = 40
    max_turns: int = 20
    stuck_budget_ratio: float = Field(default=0.85, ge=0.5, le=1.0)
    allowed_domains: list[str] = Field(default_factory=list)
    filter: FilterConfig = Field(default_factory=FilterConfig)


class DBCheck(BaseModel):
    """A structured DB verification check."""

    table: str
    find_by: str = ""
    verify: str = ""
    hint: str = ""


# A db_check can be either a plain string or a structured DBCheck
DBCheckItem = str | DBCheck


class ScenarioConfig(BaseModel):
    """A test scenario with optional DB verification hints."""

    description: str
    db_checks: list[DBCheckItem] = Field(default_factory=list)


class ProjectProfile(BaseModel):
    """Root configuration model for a project."""

    project: ProjectInfo
    environment: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    code: CodeConfig
    model: ModelConfig = Field(default_factory=ModelConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    boundaries: BoundariesConfig = Field(default_factory=BoundariesConfig)
    scenarios: dict[str, str | ScenarioConfig] = Field(default_factory=dict)
