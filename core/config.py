from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supabase_url: str
    supabase_secret_key: str = Field(
        validation_alias=AliasChoices("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY")
    )
    supabase_jwt_secret: str = ""
    app_session_secret: str
    app_session_ttl_seconds: int = Field(default=60 * 60 * 24 * 7, gt=0)
    database_url: str = ""
    db_dsn: str = ""
    admin_email: str = ""
    admin_password: str = ""
    webmaster_email: str = ""
    mail_from: str = ""
    smtp_host: str = ""
    smtp_port: int = Field(default=587, gt=0)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    llm_provider: str = "gemini"
    google_api_key: str = ""
    gemini_model: str = "gemma-4-31b-it"
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_mailto: str = "mailto:info@girospesa.it"
    fcm_enabled: bool = False
    fcm_project_id: str = ""
    fcm_client_email: str = ""
    fcm_private_key: str = ""

    ops_cron_secret: str = ""
    notification_delivery_workers: int = Field(default=8, ge=1, le=16)

    environment: str = "development"
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://localhost:8000"
    cors_extra_origins: str = ""

    @field_validator("app_session_secret")
    @classmethod
    def validate_app_session_secret(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("app_session_secret must not be empty")
        return normalized

    @field_validator("smtp_use_ssl")
    @classmethod
    def validate_smtp_ssl_mode(cls, value: bool, info) -> bool:
        if value and info.data.get("smtp_use_tls"):
            raise ValueError("smtp_use_ssl and smtp_use_tls cannot both be true")
        return value



settings = Settings()  # type: ignore[call-arg]
