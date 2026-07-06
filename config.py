import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
SUPER_ADMIN_IDS_RAW = os.getenv("SUPER_ADMIN_IDS", "").strip()
ALLOWED_USER_IDS_RAW = os.getenv("ALLOWED_USER_IDS", "").strip()
MAX_DOWNLOAD_MB_RAW = os.getenv("MAX_DOWNLOAD_MB", "50").strip()
MAX_EXPORT_MESSAGES_RAW = os.getenv("MAX_EXPORT_MESSAGES", "5000").strip()
MEDIA_RETENTION_DAYS_RAW = os.getenv("MEDIA_RETENTION_DAYS", "3").strip()
MEDIA_CLEANUP_ENABLED_RAW = os.getenv("MEDIA_CLEANUP_ENABLED", "true").strip()
MEDIA_USER_LIMIT_MB_RAW = os.getenv("MEDIA_USER_LIMIT_MB", "500").strip()
MEDIA_QUOTA_GRACE_HOURS_RAW = os.getenv("MEDIA_QUOTA_GRACE_HOURS", "24").strip()
DEBUG_EXPORT_RAW = os.getenv("DEBUG_EXPORT", "false").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", ".").strip() or ".")


def _parse_int_list(value: str, name: str) -> list[int]:
    if not value:
        return []

    result: list[int] = []
    seen: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue

        try:
            parsed = int(item)
        except ValueError as exc:
            raise ValueError(f"{name} must contain comma-separated integers") from exc

        if parsed in seen:
            continue

        seen.add(parsed)
        result.append(parsed)

    return result


def _parse_allowed_user_ids(value: str) -> set[int]:
    return set(_parse_int_list(value, "ALLOWED_USER_IDS"))


def _parse_positive_int(value: str, name: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc

    if result <= 0:
        raise ValueError(f"{name} must be positive")

    return result


def _parse_bool(value: str, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", ""}:
        return False

    raise ValueError(f"{name} must be a boolean")


SUPER_ADMIN_IDS = _parse_int_list(SUPER_ADMIN_IDS_RAW, "SUPER_ADMIN_IDS")
if not SUPER_ADMIN_IDS:
    SUPER_ADMIN_IDS = _parse_int_list(OWNER_ID_RAW, "OWNER_ID")
ALLOWED_USER_IDS = _parse_allowed_user_ids(ALLOWED_USER_IDS_RAW)
MAX_DOWNLOAD_MB = _parse_positive_int(MAX_DOWNLOAD_MB_RAW, "MAX_DOWNLOAD_MB")
MAX_EXPORT_MESSAGES = _parse_positive_int(MAX_EXPORT_MESSAGES_RAW, "MAX_EXPORT_MESSAGES")
MEDIA_RETENTION_DAYS = _parse_positive_int(MEDIA_RETENTION_DAYS_RAW, "MEDIA_RETENTION_DAYS")
MEDIA_CLEANUP_ENABLED = _parse_bool(MEDIA_CLEANUP_ENABLED_RAW, "MEDIA_CLEANUP_ENABLED")
MEDIA_USER_LIMIT_MB = _parse_positive_int(MEDIA_USER_LIMIT_MB_RAW, "MEDIA_USER_LIMIT_MB")
MEDIA_QUOTA_GRACE_HOURS = _parse_positive_int(MEDIA_QUOTA_GRACE_HOURS_RAW, "MEDIA_QUOTA_GRACE_HOURS")
DEBUG_EXPORT = _parse_bool(DEBUG_EXPORT_RAW, "DEBUG_EXPORT")
