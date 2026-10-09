from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_ENVS = {"local", "test"}
KNOWN_ENVS = {"local", "test", "staging", "production"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    allow_local_synthetic_auth: bool = False
    intake_webhook_key: str = ""
    database_url: str = ""

    @property
    def synthetic_auth_active(self) -> bool:
        return self.allow_local_synthetic_auth and self.app_env in LOCAL_ENVS

    def validate_startup(self) -> None:
        """Fail closed: refuse to start in unsafe configurations (docs/SPEC.md, scenario 19)."""
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required (PostgreSQL)")
        if self.app_env not in KNOWN_ENVS:
            raise RuntimeError(f"Unknown APP_ENV {self.app_env!r}")
        if self.allow_local_synthetic_auth and self.app_env not in LOCAL_ENVS:
            raise RuntimeError(
                "Local synthetic auth must not be enabled outside local/test"
            )
