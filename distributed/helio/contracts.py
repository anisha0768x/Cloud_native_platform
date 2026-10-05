from typing import Literal
from pydantic import BaseModel, Field, field_validator

METRICS = {
    "request_rate",
    "latency_p95_ms",
    "error_percent",
    "success_percent",
    "cpu_percent",
    "memory_mb",
    "ready_replicas",
}


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=256)
    code: str = Field(default="", max_length=6)


class NewUser(Credentials):
    name: str = Field(min_length=2, max_length=80)
    password: str = Field(min_length=12, max_length=256)
    role: Literal["viewer", "operator", "admin"] = "viewer"
    setup_token: str = Field(default="", max_length=200)

    @field_validator("setup_token")
    @classmethod
    def clean_setup_token(cls, value):
        value = value.strip()
        # Users may copy the whole labelled line from the launcher or configuration.
        # Only known display labels are removed; the secret must still match exactly.
        for label in (
            "Server setup token:",
            "First-administrator setup token (keep private):",
            "SETUP_TOKEN=",
        ):
            if value.startswith(label):
                return value[len(label) :].strip()
        return value

    @field_validator("email")
    @classmethod
    def email_format(cls, value):
        value = value.lower().strip()
        if "@" not in value or "." not in value.split("@")[-1] or any(c.isspace() for c in value):
            raise ValueError("Enter a valid email address")
        return value


class UserUpdate(BaseModel):
    role: Literal["viewer", "operator", "admin"]
    active: bool = True


class Registration(BaseModel):
    name: str = Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9-]+$")
    environment: Literal["local", "development", "staging", "production"] = "development"
    kind: Literal["api", "worker", "cron"] = "api"
    namespace: str = Field(default="platform", min_length=2, max_length=63)
    owner_team: str = Field(default="operations", min_length=2, max_length=64)


class Metric(BaseModel):
    service_id: str
    metric_name: str
    value: float = Field(ge=0, le=1e12, allow_inf_nan=False)
    occurred_at: str | None = None

    @field_validator("metric_name")
    @classmethod
    def known(cls, value):
        if value not in METRICS:
            raise ValueError("Unsupported metric")
        return value


class LogInput(BaseModel):
    service_id: str
    level: Literal["INFO", "WARN", "ERROR"] = "INFO"
    message: str = Field(min_length=1, max_length=10000)
    trace_id: str | None = Field(default=None, max_length=128)


class IncidentUpdate(BaseModel):
    status: Literal["open", "acknowledged", "investigating", "resolved"]
    owner_id: str | None = None
    note: str = Field(min_length=3, max_length=2000)


class Scale(BaseModel):
    deployment: str = "checkout-api"
    replicas: int = Field(ge=1, le=8, strict=True)


class Load(BaseModel):
    total: int = Field(default=100, ge=10, le=500, strict=True)
    concurrency: int = Field(default=8, ge=1, le=24, strict=True)
    work_ms: int = Field(default=30, ge=1, le=300, strict=True)


class Autoscale(BaseModel):
    enabled: bool = False
    min_replicas: int = Field(default=1, ge=1, le=8)
    max_replicas: int = Field(default=4, ge=1, le=8)
    target_rps: float = Field(default=5, ge=0.1, le=1000, allow_inf_nan=False)
    cooldown_seconds: int = Field(default=30, ge=5, le=600)
    consecutive_samples: int = Field(default=3, ge=2, le=10)
    self_heal: bool = True


class Thresholds(BaseModel):
    latency_ms: float = Field(ge=1, le=30000, allow_inf_nan=False)
    error_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    consecutive_windows: int = Field(ge=1, le=10)
    window_seconds: int = Field(ge=10, le=300)


class Rates(BaseModel):
    worker_hour: float = Field(ge=0, le=1000, allow_inf_nan=False)
    storage_gb_month: float = Field(ge=0, le=1000, allow_inf_nan=False)
    database_gb_month: float = Field(ge=0, le=1000, allow_inf_nan=False)
    budget: float = Field(gt=0, le=1e9, allow_inf_nan=False)
    currency: Literal["USD", "INR"] = "USD"
