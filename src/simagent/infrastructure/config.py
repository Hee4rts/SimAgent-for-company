"""Explicit .env configuration for the optional local web application."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr, field_validator


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    serpapi_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    model: str = Field(default="deepseek-chat", min_length=1)
    base_url: AnyHttpUrl = "https://api.deepseek.com"
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8000, ge=1, le=65535)
    timeout_seconds: float = Field(default=180, gt=0)

    @field_validator("model", "host", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @property
    def configured(self) -> bool:
        return bool(self.api_key.get_secret_value().strip())

    @classmethod
    def from_env(cls, path: str | Path = ".env") -> WebSettings:
        from dotenv import dotenv_values

        # Read just the requested file; never traverse parents or rewrite os.environ.
        values = {**dotenv_values(path, interpolate=False), **os.environ}
        mapping = {
            "api_key": "DEEPSEEK_API_KEY",
            "serpapi_api_key": "SERPAPI_API_KEY",
            "model": "DEEPSEEK_MODEL",
            "base_url": "DEEPSEEK_BASE_URL",
            "host": "SIMAGENT_HOST",
            "port": "SIMAGENT_PORT",
            "timeout_seconds": "SIMAGENT_TIMEOUT_SECONDS",
        }
        return cls.model_validate({
            field: values[key] for field, key in mapping.items()
            if values.get(key) is not None
        })
