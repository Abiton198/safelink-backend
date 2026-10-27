"""Environment-backed configuration. Never commit real secrets."""
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import List

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    cors_origins_csv: str = "http://localhost:5173,http://localhost:3000"
    firebase_project_id: str = ""
    firebase_service_account_json: str = ""  # Optional JSON string; prefer GOOGLE_APPLICATION_CREDENTIALS in production.
    google_application_credentials: str = ""
    payfast_merchant_id: str = ""
    payfast_merchant_key: str = ""
    payfast_passphrase: str = ""
    payfast_sandbox: bool = True
    payfast_itn_validate_url: str = "https://sandbox.payfast.co.za/eng/query/validate"
    payfast_process_url: str = "https://sandbox.payfast.co.za/eng/process"
    frontend_base_url: str = "http://localhost:5173"
    api_base_url: str = "http://localhost:8000"
    subscription_monthly_amount: str = "19.99"
    trial_days: int = 7
    subscription_grace_days: int = 7

    @property
    def cors_origins(self) -> List[str]:
        return [origin.strip() for origin in self.cors_origins_csv.split(",") if origin.strip()]

settings = Settings()
