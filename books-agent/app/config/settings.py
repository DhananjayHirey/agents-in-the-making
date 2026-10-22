from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    agent_port: int = 8001
    internal_auth_secret: str
    backend_graphql_url: str = "http://localhost:4000/graphql"
    # The backend publishes the agent's tools here (GET), plus confirm-card previews (POST /{tool}/preview).
    backend_tools_url: str = "http://localhost:4000/internal/agent-tools"
    tool_refresh_seconds: float = 30.0
    agent_database_url: str
    redis_url: str = "redis://localhost:6379/0"

    llm_backend: str = "bedrock"  # "bedrock" | "gateway"
    aws_region: str = "us-east-1"
    aws_profile: str | None = None  # a named profile in ~/.aws/credentials; None = default chain
    bedrock_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    gateway_model: str = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"

    # Turn budget and context window
    turn_timeout_seconds: float = 90.0
    heartbeat_seconds: float = 15.0
    max_tool_turns: int = 8
    history_exchanges_in_prompt: int = 6
    pending_action_ttl_hours: int = 24
    user_cache_seconds: int = 300


settings = Settings()  # type: ignore[call-arg]
