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
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None


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


class BoundariesConfig(BaseModel):
    readonly: bool = True
    forbidden_actions: list[str] = Field(
        default_factory=lambda: ["DELETE FROM", "DROP TABLE", "INSERT INTO", "UPDATE"]
    )
    max_steps: int = 30
    allowed_domains: list[str] = Field(default_factory=list)


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
