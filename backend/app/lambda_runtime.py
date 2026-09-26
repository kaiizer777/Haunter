"""Shared AWS Lambda runtime detection."""

from __future__ import annotations

import os
from typing import Optional


def resolve_lambda_function_name(configured: Optional[str] = None) -> Optional[str]:
    if isinstance(configured, str):
        normalized = configured.strip()
        if normalized:
            return normalized
    from app.config import settings

    setting_value = settings.aws_lambda_function_name
    if isinstance(setting_value, str):
        normalized = setting_value.strip()
        if normalized:
            return normalized
    environment_value = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip()
    return environment_value or None


def is_lambda_runtime() -> bool:
    return resolve_lambda_function_name() is not None
