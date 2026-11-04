"""Environment-backed configuration. Never commit real secrets.

Every deployment-specific value (website addresses, secrets, PayFast keys)
is read from environment variables or from a local `.env` file. On Render,
set them under the service's "Environment" tab. See `.env.example` for the
full list.

Local development
-----------------
With APP_ENV unset (or "development") a few harmless localhost defaults are
filled in so the app runs without any setup: the local frontend/backend
addresses and PayFast SANDBOX. Secrets are never defaulted.

Production (APP_ENV=production)
-------------------------------
Nothing is guessed. If a required value is missing or unsafe (for example
a URL that points at localhost), the app refuses to start and the Render
log lists exactly which variables to fix.
"""

from typing import List
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# PayFast endpoints. Which pair is used follows PAYFAST_SANDBOX, unless you
# override them with PAYFAST_PROCESS_URL / PAYFAST_ITN_VALIDATE_URL.
_PAYFAST_URLS = {
    True: (  # sandbox
        "https://sandbox.payfast.co.za/eng/process",
        "https://sandbox.payfast.co.za/eng/query/validate",
    ),
    False: (  # live
        "https://www.payfast.co.za/eng/process",
        "https://www.payfast.co.za/eng/query/validate",
    ),
}

# Used ONLY outside production, so local development works with no setup.
_DEV_FRONTEND_URL = "http://localhost:5173"
_DEV_API_URL = "http://localhost:8000"
_DEV_CORS = "http://localhost:5173,http://localhost:3000"


def _origin(url: str) -> str:
    """'https://site.com/some/path' -> 'https://site.com' ('' if invalid)."""
    parts = urlsplit(url)
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # "development" or "production"
    app_env: str = "development"

    # Website addresses (env: FRONTEND_BASE_URL, API_BASE_URL)
    frontend_base_url: str = ""
    api_base_url: str = ""

    # Websites allowed to call this API, comma-separated (env: CORS_ORIGINS_CSV).
    # The frontend address above is always allowed as well.
    cors_origins_csv: str = ""

    # Firebase
    firebase_project_id: str = ""
    firebase_service_account_json: str = ""
    google_application_credentials: str = ""
    # Firebase Storage bucket for vehicle photos, e.g. "your-project.appspot.com"
    # (shown in Firebase console -> Storage). Photos stay private.
    firebase_storage_bucket: str = ""

    # PayFast
    payfast_merchant_id: str = ""
    payfast_merchant_key: str = ""
    payfast_passphrase: str = ""
    # True = sandbox (testing). Set PAYFAST_SANDBOX=false on live only.
    payfast_sandbox: bool = True
    # Optional overrides; normally left empty and chosen by payfast_sandbox.
    payfast_process_url: str = ""
    payfast_itn_validate_url: str = ""

    # Pricing and billing
    subscription_monthly_amount: str = "19.99"
    trial_days: int = 7
    subscription_grace_days: int = 7

    # Secret used to hash dates of birth (env: DOB_HMAC_SECRET).
    # Keep it fixed: changing it stops every saved profile from verifying.
    dob_hmac_secret: str = ""

    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"production", "prod"}

    @model_validator(mode="after")
    def _fill_derived_values(self):
        """Apply local-only defaults, tidy URLs, and pick PayFast endpoints."""
        if not self.is_production:
            self.frontend_base_url = self.frontend_base_url or _DEV_FRONTEND_URL
            self.api_base_url = self.api_base_url or _DEV_API_URL
            self.cors_origins_csv = self.cors_origins_csv or _DEV_CORS

        # No trailing slash: code builds f"{base}/subscription/return" etc.
        self.frontend_base_url = self.frontend_base_url.strip().rstrip("/")
        self.api_base_url = self.api_base_url.strip().rstrip("/")

        process_url, validate_url = _PAYFAST_URLS[self.payfast_sandbox]
        self.payfast_process_url = self.payfast_process_url.strip() or process_url
        self.payfast_itn_validate_url = (
            self.payfast_itn_validate_url.strip() or validate_url
        )
        return self

    @property
    def cors_origins(self) -> List[str]:
        """Clean origins for FastAPI CORS (frontend address always included)."""
        origins = [
            item.strip().rstrip("/")
            for item in self.cors_origins_csv.split(",")
            if item.strip()
        ]
        frontend = _origin(self.frontend_base_url)
        if frontend and frontend not in origins:
            origins.append(frontend)
        return origins

    def missing_payfast_settings(self) -> List[str]:
        """Names of PayFast variables that are still empty (for a clear 503)."""
        required = {
            "PAYFAST_MERCHANT_ID": self.payfast_merchant_id,
            "PAYFAST_MERCHANT_KEY": self.payfast_merchant_key,
            "PAYFAST_PASSPHRASE": self.payfast_passphrase,
        }
        return [name for name, value in required.items() if not value.strip()]

    # ------------------------------------------------------------------
    def startup_problems(self) -> List[str]:
        """Everything that must be fixed before the API can safely start."""
        problems: List[str] = []

        if not self.dob_hmac_secret:
            problems.append("DOB_HMAC_SECRET is not set.")

        for name, value in (
            ("FRONTEND_BASE_URL", self.frontend_base_url),
            ("API_BASE_URL", self.api_base_url),
        ):
            if not value:
                problems.append(f"{name} is not set.")
            elif not _origin(value):
                problems.append(f"{name} must start with http:// or https://.")
            elif self.is_production and (
                not value.startswith("https://") or "localhost" in value
            ):
                problems.append(f"{name} must be a public https:// address in production.")

        if not self.cors_origins_csv.strip():
            problems.append("CORS_ORIGINS_CSV is not set.")
        if "*" in self.cors_origins_csv:
            problems.append(
                "CORS_ORIGINS_CSV must list exact sites, not '*' "
                "(credentials are enabled)."
            )

        return problems

    def validate_runtime(self) -> None:
        problems = self.startup_problems()
        if problems:
            raise RuntimeError(
                "SafeLink configuration is incomplete:\n  - "
                + "\n  - ".join(problems)
            )


settings = Settings()
settings.validate_runtime()