
"""Environment-backed configuration. Never commit real secrets."""

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"

    # Local development and deployed SafeLink frontend.
    # Render environment variables can override this default.
    cors_origins_csv: str = (
        "http://localhost:5173,"
        "http://localhost:3000,"
        "https://safelinktag.netlify.app"
    )

    firebase_project_id: str = ""
    firebase_service_account_json: str = ""
    google_application_credentials: str = ""

    payfast_merchant_id: str = ""
    payfast_merchant_key: str = ""
    payfast_passphrase: str = ""
    payfast_sandbox: bool = True

    payfast_itn_validate_url: str = (
        "https://sandbox.payfast.co.za/eng/query/validate"
    )

    payfast_process_url: str = (
        "https://sandbox.payfast.co.za/eng/process"
    )

    frontend_base_url: str = "http://localhost:5173"
    api_base_url: str = "http://localhost:8000"

    subscription_monthly_amount: str = "19.99"
    trial_days: int = 7
    subscription_grace_days: int = 7

    # Secret used to hash dates of birth.
    dob_hmac_secret: str = ""

    @property
    def cors_origins(self) -> List[str]:
        """Return clean frontend origins for FastAPI CORS."""
        return [
            origin.strip().rstrip("/")
            for origin in self.cors_origins_csv.split(",")
            if origin.strip()
        ]


settings = Settings()

# Safe startup validation.
if not settings.dob_hmac_secret:
    raise RuntimeError("DOB_HMAC_SECRET is not configured.")
