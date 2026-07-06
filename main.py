import asyncio
import html
import json
import logging
import mimetypes
import re
import shutil
import secrets
import time
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BusinessConnection,
    BusinessMessagesDeleted,
    CallbackQuery,
    Chat,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import (
    ALLOWED_USER_IDS,
    BOT_TOKEN,
    DATA_DIR,
    MEDIA_CLEANUP_ENABLED,
    MEDIA_QUOTA_GRACE_HOURS,
    MEDIA_RETENTION_DAYS,
    MEDIA_USER_LIMIT_MB,
    MAX_DOWNLOAD_MB,
    MAX_EXPORT_MESSAGES,
    SUPER_ADMIN_IDS,
)
from database import (
    ensure_trial_subscription,
    export_dialog_data,
    find_dialog_for_owner,
    get_dialogs_for_owner,
    get_business_media,
    get_business_message,
    get_connection_owner,
    get_media_cleanup_delete_candidates,
    get_media_cleanup_warning_candidates,
    get_active_media_with_owners,
    get_owner_connections,
    get_message,
    get_stats,
    get_stats_for_owner,
    get_user_subscription,
    init_db,
    is_connected_owner,
    mark_media_cleaned,
    mark_media_cleanup_warned,
    mark_media_quota_warned,
    resolve_deleted_business_messages,
    save_business_connection,
    save_business_media,
    save_business_message,
    save_business_message_version,
    save_message,
    save_message_version,
    update_business_media_caption,
    update_business_message_content,
    update_message_text,
)


PRIVATE_CHAT = ChatType.PRIVATE
GROUP_CHATS = {ChatType.GROUP, ChatType.SUPERGROUP}
IGNORED_GROUP_COMMANDS = {
    "/start",
    "/stats",
    "/help",
    "/debug",
    "/ping",
    "/dialogs",
    "/export",
    "/export_help",
    "/translation",
    "/translate_settings",
}
MAX_DELETED_TEXT_LENGTH = 3500
MAX_MEDIA_CAPTION_LENGTH = 900
MAX_DOWNLOAD_BYTES = MAX_DOWNLOAD_MB * 1024 * 1024
MAX_EXPORT_FILE_BYTES = 45 * 1024 * 1024
STORAGE_DIR = DATA_DIR / "storage"
PHOTO_DIR = STORAGE_DIR / "photos"
VIDEO_DIR = STORAGE_DIR / "videos"
FILES_DIR = STORAGE_DIR / "files"
DEBUG_DIR = STORAGE_DIR / "debug"
EXPORTS_DIR = DATA_DIR / "exports"
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]
EXPORT_FORMATS = {"html", "json", "txt"}
PENDING_EXPORTS: dict[str, dict[str, Any]] = {}
RATE_LIMIT_EVENTS: dict[tuple[str, int], deque[float]] = defaultdict(deque)
RATE_LIMIT_BLOCKED_UNTIL: dict[tuple[str, int], float] = {}
BOT_SIGNATURE = "Бот @MajorSpyBot"
CONNECT_URL = "tg://settings/edit"
BOT_SHORT_DESCRIPTION = "Сохраняет сообщения, медиа и помогает быстро переводить переписки."
BOT_DESCRIPTION = (
    "Major Spy помогает вести Telegram Business-переписки: сохраняет сообщения и медиа, "
    "показывает удалённые и изменённые сообщения, делает экспорт чатов и умеет переводить "
    "или исправлять раскладку прямо через ответ на сообщение."
)
TRIAL_DAYS = 7
PRO_TARIFFS = {
    "week": ("Неделя", 99),
    "month": ("1 месяц", 249),
    "3months": ("3 месяца", 599),
    "6months": ("6 месяцев", 990),
    "year": ("Год", 1690),
}
LAYOUT_EN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>?"
LAYOUT_RU = "ёйцукенгшщзхъфывапролджэячсмитьбю.ЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,"
LAYOUT_TRANSLATION = str.maketrans(LAYOUT_EN, LAYOUT_RU)
TRANSLATE_FALLBACK = {
    "hello": "привет",
    "hi": "привет",
    "thanks": "спасибо",
    "thank you": "спасибо",
    "yes": "да",
    "no": "нет",
    "ok": "ок",
    "good": "хорошо",
    "bad": "плохо",
    "please": "пожалуйста",
}


def setup_logging() -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
        ],
    )


def _ensure_storage_dirs() -> None:
    for directory in (PHOTO_DIR, VIDEO_DIR, FILES_DIR, DEBUG_DIR, EXPORTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _chat_display(chat: Chat) -> str:
    full_name = " ".join(
        item for item in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if item
    )
    return chat.title or full_name or chat.username or str(chat.id)


def _message_chat_title(message: Message) -> str:
    return message.chat.title or _chat_display(message.chat)


def _user_id(message: Message) -> int | None:
    return message.from_user.id if message.from_user else None


def _username(message: Message) -> str | None:
    return message.from_user.username if message.from_user else None


def _full_name(message: Message) -> str | None:
    if not message.from_user:
        return None

    return message.from_user.full_name or None


def _format_user(full_name: str | None, username: str | None) -> str:
    name = full_name or "Unknown"
    return f"{name} (@{username})" if username else name


def _message_created_at(message: Message) -> str | None:
    return message.date.isoformat() if message.date else None


def _truncate_text(text: str | None, limit: int = MAX_DELETED_TEXT_LENGTH) -> str:
    value = text or "[нет текста]"
    if len(value) <= limit:
        return value

    return value[:limit] + "...[обрезано]"


def _is_allowed_user(user_id: int | None) -> bool:
    if user_id is None:
        return False

    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def _check_rate_limit(
    scope: str,
    user_id: int | None,
    *,
    limit: int = 5,
    window_seconds: int = 60,
    block_seconds: int = 60,
) -> tuple[bool, int]:
    if user_id is None:
        return True, 0

    now = time.monotonic()
    key = (scope, user_id)
    blocked_until = RATE_LIMIT_BLOCKED_UNTIL.get(key, 0)
    if blocked_until > now:
        return False, max(1, int(blocked_until - now))

    events = RATE_LIMIT_EVENTS[key]
    while events and now - events[0] > window_seconds:
        events.popleft()

    events.append(now)
    if len(events) > limit:
        RATE_LIMIT_BLOCKED_UNTIL[key] = now + block_seconds
        events.clear()
        logging.warning("RATE_LIMIT blocked scope=%s user_id=%s seconds=%s", scope, user_id, block_seconds)
        return False, block_seconds

    return True, 0


def _is_ignored_group_command(text: str | None) -> bool:
    if not text:
        return False

    command = text.split(maxsplit=1)[0].split("@", maxsplit=1)[0].lower()
    return command in IGNORED_GROUP_COMMANDS


def _business_connection_id(message: Message) -> str | None:
    return message.business_connection_id


def _media_label(media_type: str) -> str:
    return {
        "photo": "фото",
        "video": "видео",
        "document": "документ",
        "animation": "анимация",
        "audio": "аудио",
        "voice": "голосовое",
        "video_note": "кружок",
        "unavailable": "недоступное медиа",
    }.get(media_type, media_type)


def _is_media_message(message: Message) -> bool:
    return any(
        (
            message.photo,
            message.video,
            message.document,
            message.animation,
            message.audio,
            message.voice,
            message.video_note,
            message.paid_media,
        )
    )


def _is_empty_reply_placeholder(message: Message) -> bool:
    return not any(
        (
            message.text,
            message.caption,
            message.photo,
            message.video,
            message.document,
            message.animation,
            message.audio,
            message.voice,
            message.video_note,
            message.paid_media,
        )
    )


def _is_unavailable_media(message: Message) -> bool:
    return bool(message.paid_media)


def _storage_filename(
    *,
    business_connection_id: str,
    chat_id: int,
    message_id: int,
    media_type: str,
    file_unique_id: str | None,
    original_name: str | None,
    mime_type: str | None,
) -> str:
    safe_connection = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in business_connection_id)
    safe_unique = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (file_unique_id or "file"))
    extension = Path(original_name or "").suffix
    if not extension and mime_type:
        extension = mimetypes.guess_extension(mime_type) or ""
    if not extension and media_type == "photo":
        extension = ".jpg"
    if not extension and media_type in {"video", "video_note"}:
        extension = ".mp4"
    if not extension and media_type == "voice":
        extension = ".ogg"
    if not extension and media_type == "audio":
        extension = ".mp3"

    return f"{safe_connection}_{chat_id}_{message_id}_{safe_unique}{extension}"


def _media_local_path(
    *,
    business_connection_id: str,
    message: Message,
    metadata: dict[str, Any],
) -> Path | None:
    if metadata["media_type"] == "unavailable" or not metadata.get("file_id"):
        return None

    filename = _storage_filename(
        business_connection_id=business_connection_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        media_type=metadata["media_type"],
        file_unique_id=metadata.get("file_unique_id"),
        original_name=metadata.get("file_name"),
        mime_type=metadata.get("mime_type"),
    )
    return metadata["download_dir"] / filename


def _extract_media_metadata(message: Message) -> dict[str, Any] | None:
    if message.photo:
        photo = message.photo[-1]
        return {
            "media_type": "photo",
            "file_id": photo.file_id,
            "file_unique_id": photo.file_unique_id,
            "file_name": None,
            "mime_type": "image/jpeg",
            "file_size": photo.file_size,
            "duration": None,
            "download_dir": PHOTO_DIR,
        }

    media_map = [
        ("video", message.video, VIDEO_DIR),
        ("document", message.document, FILES_DIR),
        ("animation", message.animation, FILES_DIR),
        ("audio", message.audio, FILES_DIR),
        ("voice", message.voice, FILES_DIR),
        ("video_note", message.video_note, FILES_DIR),
    ]
    for media_type, media, download_dir in media_map:
        if not media:
            continue

        return {
            "media_type": media_type,
            "file_id": getattr(media, "file_id", None),
            "file_unique_id": getattr(media, "file_unique_id", None),
            "file_name": getattr(media, "file_name", None),
            "mime_type": getattr(media, "mime_type", None),
            "file_size": getattr(media, "file_size", None),
            "duration": getattr(media, "duration", None),
            "download_dir": download_dir,
        }

    if message.paid_media or message.has_protected_content:
        return {
            "media_type": "unavailable",
            "file_id": None,
            "file_unique_id": None,
            "file_name": None,
            "mime_type": None,
            "file_size": None,
            "duration": None,
            "download_dir": FILES_DIR,
        }

    return None


def _business_message_payload(message: Message, text: str | None, caption: str | None = None) -> dict[str, Any]:
    business_connection_id = _business_connection_id(message)
    if not business_connection_id:
        raise ValueError("business_connection_id is required")

    return {
        "business_connection_id": business_connection_id,
        "chat_id": message.chat.id,
        "chat_title": message.chat.title,
        "message_id": message.message_id,
        "user_id": _user_id(message),
        "username": _username(message),
        "full_name": _full_name(message),
        "text": text,
        "caption": caption,
        "created_at": _message_created_at(message),
    }


def _write_business_debug_update(message: Message, business_connection_id: str) -> None:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "business_connection_id": business_connection_id,
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "from_user_id": _user_id(message),
        "from_username": _username(message),
        "content_type": str(getattr(message, "content_type", "")),
        "has_text": bool(message.text),
        "has_caption": bool(message.caption),
        "has_photo": bool(message.photo),
        "has_video": bool(message.video),
        "has_document": bool(message.document),
        "has_animation": bool(message.animation),
        "has_audio": bool(message.audio),
        "has_voice": bool(message.voice),
        "has_video_note": bool(message.video_note),
        "has_paid_media": bool(message.paid_media),
        "has_protected_content": bool(message.has_protected_content),
        "raw": json.loads(message.model_dump_json(exclude_none=True)),
    }
    with (DEBUG_DIR / "business_updates.jsonl").open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _media_payload(
    *,
    message: Message,
    metadata: dict[str, Any],
    local_path: str | None,
    is_unavailable: bool = False,
) -> dict[str, Any]:
    business_connection_id = _business_connection_id(message)
    if not business_connection_id:
        raise ValueError("business_connection_id is required")

    return {
        "business_connection_id": business_connection_id,
        "chat_id": message.chat.id,
        "chat_title": message.chat.title,
        "message_id": message.message_id,
        "user_id": _user_id(message),
        "username": _username(message),
        "full_name": _full_name(message),
        "media_type": metadata["media_type"],
        "file_id": metadata.get("file_id"),
        "file_unique_id": metadata.get("file_unique_id"),
        "file_name": metadata.get("file_name"),
        "mime_type": metadata.get("mime_type"),
        "file_size": metadata.get("file_size"),
        "duration": metadata.get("duration"),
        "caption": message.caption,
        "local_path": local_path,
        "created_at": _message_created_at(message),
        "is_unavailable": is_unavailable,
    }


def _active_connection_owner_id(business_connection_id: str) -> int | None:
    owner = get_connection_owner(business_connection_id)
    if not owner:
        logging.warning("Connection owner not found business_connection_id=%s", business_connection_id)
        return None
    if owner.get("is_enabled") != 1:
        logging.warning("Connection owner notification skipped disabled business_connection_id=%s", business_connection_id)
        return None

    return int(owner["owner_user_id"])


async def safe_notify_connection_owner(bot: Bot, business_connection_id: str, text: str) -> None:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return

    try:
        await bot.send_message(chat_id=owner_user_id, text=text, parse_mode=ParseMode.HTML)
        logging.info("Connection owner notification sent connection=%s owner=%s", business_connection_id, owner_user_id)
    except Exception as exc:
        logging.warning(
            "Failed to notify connection owner connection=%s owner=%s: %s",
            business_connection_id,
            owner_user_id,
            exc,
        )


async def _wait_for_local_file(local_path: str | None, attempts: int = 10, delay: float = 0.2) -> bool:
    if not local_path:
        return False

    path = Path(local_path)
    for _ in range(attempts):
        if path.exists():
            return True
        await asyncio.sleep(delay)
    return path.exists()


async def safe_notify_connection_owner_photo(
    bot: Bot,
    business_connection_id: str,
    local_path: str | None,
    caption: str | None = None,
) -> None:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return

    if not await _wait_for_local_file(local_path):
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Медиа было удалено, но файл не найден в storage.",
        )
        return

    try:
        await bot.send_photo(
            chat_id=owner_user_id,
            photo=FSInputFile(local_path),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        logging.info("Connection owner photo sent connection=%s owner=%s", business_connection_id, owner_user_id)
    except Exception as exc:
        logging.warning(
            "Failed to send photo to connection owner connection=%s owner=%s: %s",
            business_connection_id,
            owner_user_id,
            exc,
        )


async def safe_notify_connection_owner_video(
    bot: Bot,
    business_connection_id: str,
    local_path: str | None,
    caption: str | None = None,
) -> None:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return

    if not await _wait_for_local_file(local_path):
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Медиа было удалено, но файл не найден в storage.",
        )
        return

    try:
        await bot.send_video(
            chat_id=owner_user_id,
            video=FSInputFile(local_path),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        logging.info("Connection owner video sent connection=%s owner=%s", business_connection_id, owner_user_id)
    except Exception as exc:
        logging.warning(
            "Failed to send video to connection owner connection=%s owner=%s: %s",
            business_connection_id,
            owner_user_id,
            exc,
        )


async def safe_notify_connection_owner_video_note(
    bot: Bot,
    business_connection_id: str,
    local_path: str | None,
    caption: str | None = None,
) -> None:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return

    if not await _wait_for_local_file(local_path):
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Кружок был удалён, но файл не найден в storage.",
        )
        return

    try:
        if caption:
            await bot.send_message(chat_id=owner_user_id, text=caption, parse_mode=ParseMode.HTML)
        await bot.send_video_note(chat_id=owner_user_id, video_note=FSInputFile(local_path))
        logging.info("Connection owner video note sent connection=%s owner=%s", business_connection_id, owner_user_id)
    except Exception as exc:
        logging.warning(
            "Failed to send video note to connection owner connection=%s owner=%s: %s",
            business_connection_id,
            owner_user_id,
            exc,
        )


async def safe_notify_connection_owner_voice(
    bot: Bot,
    business_connection_id: str,
    local_path: str | None,
    caption: str | None = None,
) -> None:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return

    if not await _wait_for_local_file(local_path):
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Голосовое было удалено, но файл не найден в storage.",
        )
        return

    try:
        await bot.send_voice(
            chat_id=owner_user_id,
            voice=FSInputFile(local_path),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        logging.info("Connection owner voice sent connection=%s owner=%s", business_connection_id, owner_user_id)
    except Exception as exc:
        logging.warning(
            "Failed to send voice to connection owner connection=%s owner=%s: %s",
            business_connection_id,
            owner_user_id,
            exc,
        )


async def safe_notify_connection_owner_audio(
    bot: Bot,
    business_connection_id: str,
    local_path: str | None,
    caption: str | None = None,
) -> None:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return

    if not await _wait_for_local_file(local_path):
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Аудио было удалено, но файл не найден в storage.",
        )
        return

    try:
        await bot.send_audio(
            chat_id=owner_user_id,
            audio=FSInputFile(local_path),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        logging.info("Connection owner audio sent connection=%s owner=%s", business_connection_id, owner_user_id)
    except Exception as exc:
        logging.warning(
            "Failed to send audio to connection owner connection=%s owner=%s: %s",
            business_connection_id,
            owner_user_id,
            exc,
        )


async def safe_notify_connection_owner_document(
    bot: Bot,
    business_connection_id: str,
    local_path: str | None,
    caption: str | None = None,
    missing_text: str = "Файл экспорта не найден локально.",
) -> None:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return

    if not await _wait_for_local_file(local_path):
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            missing_text,
        )
        return

    try:
        await bot.send_document(
            chat_id=owner_user_id,
            document=FSInputFile(local_path),
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        logging.info("Connection owner document sent connection=%s owner=%s", business_connection_id, owner_user_id)
    except Exception as exc:
        logging.warning(
            "Failed to send document to connection owner connection=%s owner=%s: %s",
            business_connection_id,
            owner_user_id,
            exc,
        )


async def safe_copy_business_message_to_owner(
    bot: Bot,
    business_connection_id: str,
    message: Message,
) -> bool:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None:
        return False

    try:
        await bot.copy_message(
            chat_id=owner_user_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
        logging.info(
            "Business media copied to owner connection=%s owner=%s chat_id=%s message_id=%s",
            business_connection_id,
            owner_user_id,
            message.chat.id,
            message.message_id,
        )
        return True
    except Exception as exc:
        logging.warning(
            "Failed to copy business media to owner connection=%s owner=%s chat_id=%s message_id=%s: %s",
            business_connection_id,
            owner_user_id,
            message.chat.id,
            message.message_id,
            exc,
        )
        return False


async def _download_business_media_if_needed(
    *,
    bot: Bot,
    business_connection_id: str,
    message: Message,
    metadata: dict[str, Any],
) -> str | None:
    media_type = metadata["media_type"]
    if media_type == "unavailable":
        return None

    file_id = metadata.get("file_id")
    if not file_id:
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Получено медиа, но Telegram не отдал файл для сохранения.",
        )
        return None

    file_size = metadata.get("file_size")
    if file_size and file_size > MAX_DOWNLOAD_BYTES:
        logging.info(
            "Business media metadata saved without download: connection=%s chat_id=%s message_id=%s file_size=%s",
            business_connection_id,
            message.chat.id,
            message.message_id,
            file_size,
        )
        return None

    local_path = _media_local_path(
        business_connection_id=business_connection_id,
        message=message,
        metadata=metadata,
    )
    if local_path is None:
        return None

    try:
        await bot.download(file_id, destination=local_path)
    except Exception as exc:
        logging.warning(
            "Business media download failed connection=%s chat_id=%s message_id=%s: %s",
            business_connection_id,
            message.chat.id,
            message.message_id,
            exc,
        )
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Получено медиа, но Telegram не разрешил скачать файл.",
        )
        return None

    return str(local_path)


def _format_deleted_text(event: BusinessMessagesDeleted, row: dict[str, Any]) -> str:
    message = row.get("message")
    if not message:
        return (
            "🗑 <b>Сообщение удалено</b>\n\n"
            "Удаление зафиксировано, но содержимое не найдено в базе. "
            "Возможно, бот не успел сохранить сообщение.\n\n"
            "Бот @MajorSpyBot"
        )

    deleted_text = row.get("deleted_text") or "[нет текста]"
    is_empty_placeholder = (
        not message.get("user_id")
        and not message.get("username")
        and not message.get("full_name")
        and not message.get("text")
        and not message.get("caption")
    )
    if is_empty_placeholder:
        return (
            "🗑 <b>Исчезающее сообщение удалено</b>\n\n"
            "Telegram прислал событие удаления, но не передал боту текст или файл. "
            "Такое сообщение нельзя восстановить без того, чтобы Telegram сначала отдал его содержимое.\n\n"
            "Бот @MajorSpyBot"
        )

    return (
        "🗑 <b>Сообщение удалено</b>\n\n"
        f"Пользователь: {escape(_format_user(message.get('full_name'), message.get('username')))}\n"
        "\n"
        "Удалённый текст:\n"
        f"{escape(_truncate_text(deleted_text))}\n\n"
        "Бот @MajorSpyBot"
    )


def _format_deleted_media(event: BusinessMessagesDeleted, media: dict[str, Any]) -> str:
    caption = media.get("caption") or "[нет подписи]"
    return (
        "🗑 <b>Медиа удалено</b>\n\n"
        f"Тип: {escape(_media_label(str(media.get('media_type'))))}\n"
        f"Пользователь: {escape(_format_user(media.get('full_name'), media.get('username')))}\n"
        "\n"
        "Подпись:\n"
        f"{escape(_truncate_text(caption, MAX_MEDIA_CAPTION_LENGTH))}\n\n"
        "<b>Бот @MajorSpyBot</b>"
    )


def _format_saved_media_caption(message: Message, media_type: str) -> str:
    caption = message.caption or "[нет подписи]"
    return (
        "📥 <b>Медиа сохранено</b>\n\n"
        f"<b>Тип:</b> {escape(_media_label(media_type))}\n"
        f"<b>Пользователь:</b> {escape(_format_user(_full_name(message), _username(message)))}\n\n"
        "<b>Подпись:</b>\n"
        f"{escape(_truncate_text(caption, MAX_MEDIA_CAPTION_LENGTH))}\n\n"
        "<b>Бот @MajorSpyBot</b>"
    )


def _format_media_debug_notice(message: Message, metadata: dict[str, Any], reason: str) -> str:
    has_file = "есть" if metadata.get("file_id") else "нет"
    file_size = metadata.get("file_size")
    size_text = f"{file_size} байт" if file_size else "не указан"
    protected = "да" if message.has_protected_content else "нет"
    paid = "да" if message.paid_media else "нет"
    return (
        "⚠️ <b>Медиа не сохранено файлом</b>\n\n"
        f"Причина: {escape(reason)}\n"
        f"Тип: {escape(_media_label(str(metadata.get('media_type'))))}\n"
        f"Файл от Telegram: {has_file}\n"
        f"Размер: {escape(size_text)}\n"
        f"Защищённое: {protected}\n"
        f"Платное/скрытое: {paid}\n"
        f"Пользователь: {escape(_format_user(_full_name(message), _username(message)))}\n\n"
        "Бот @MajorSpyBot"
    )


async def _save_and_notify_business_media(
    *,
    bot: Bot,
    business_connection_id: str,
    message: Message,
    source: str = "message",
) -> bool:
    metadata = _extract_media_metadata(message) or {
        "media_type": "unavailable",
        "file_id": None,
        "file_unique_id": None,
        "file_name": None,
        "mime_type": None,
        "file_size": None,
        "duration": None,
        "download_dir": FILES_DIR,
    }
    unavailable = metadata["media_type"] == "unavailable"
    expected_path = None
    file_size = metadata.get("file_size")
    if not unavailable and metadata.get("file_id") and not (file_size and file_size > MAX_DOWNLOAD_BYTES):
        expected_path = _media_local_path(
            business_connection_id=business_connection_id,
            message=message,
            metadata=metadata,
        )
    local_path = str(expected_path) if expected_path else None

    save_business_media(
        **_media_payload(
            message=message,
            metadata=metadata,
            local_path=local_path,
            is_unavailable=unavailable,
        )
    )

    if unavailable:
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            _format_media_debug_notice(
                message,
                metadata,
                "Telegram не отдал файл для сохранения.",
            ),
        )
        await safe_copy_business_message_to_owner(bot, business_connection_id, message)
    else:
        downloaded_path = await _download_business_media_if_needed(
            bot=bot,
            business_connection_id=business_connection_id,
            message=message,
            metadata=metadata,
        )
        if downloaded_path:
            local_path = downloaded_path
            saved_caption = _format_saved_media_caption(message, metadata["media_type"])
            if source == "reply":
                saved_caption = "📥 Медиа сохранено из ответа\n\n" + saved_caption.split("\n\n", 1)[1]

            if metadata["media_type"] == "photo":
                await safe_notify_connection_owner_photo(
                    bot,
                    business_connection_id,
                    local_path,
                    caption=saved_caption,
                )
            elif metadata["media_type"] == "video":
                await safe_notify_connection_owner_video(
                    bot,
                    business_connection_id,
                    local_path,
                    caption=saved_caption,
                )
            elif metadata["media_type"] == "video_note":
                await safe_notify_connection_owner_video_note(
                    bot,
                    business_connection_id,
                    local_path,
                    caption=saved_caption,
                )
            elif metadata["media_type"] == "voice":
                await safe_notify_connection_owner_voice(
                    bot,
                    business_connection_id,
                    local_path,
                    caption=saved_caption,
                )
            elif metadata["media_type"] == "audio":
                await safe_notify_connection_owner_audio(
                    bot,
                    business_connection_id,
                    local_path,
                    caption=saved_caption,
                )
            else:
                await safe_notify_connection_owner_document(
                    bot,
                    business_connection_id,
                    local_path,
                    caption=saved_caption,
                    missing_text="Медиа сохранено, но файл не найден в storage.",
                )
        else:
            copied = await safe_copy_business_message_to_owner(bot, business_connection_id, message)
            if copied:
                await safe_notify_connection_owner(
                    bot,
                    business_connection_id,
                    "Медиа не удалось скачать файлом, но бот сразу скопировал его сюда.",
                )
            else:
                await safe_notify_connection_owner(
                    bot,
                    business_connection_id,
                    _format_media_debug_notice(
                        message,
                        metadata,
                        "Telegram не разрешил скачать или скопировать файл.",
                    ),
                )

    save_business_media(
        **_media_payload(
            message=message,
            metadata=metadata,
            local_path=local_path,
            is_unavailable=unavailable,
        )
    )
    logging.info(
        "BUSINESS_MEDIA saved source=%s connection=%s chat_id=%s message_id=%s type=%s local_path=%s",
        source,
        business_connection_id,
        message.chat.id,
        message.message_id,
        metadata["media_type"],
        local_path,
    )
    return True


def _format_edit_notice(
    *,
    title: str,
    business_connection_id: str,
    message: Message,
    old_value: str | None,
    new_value: str | None,
) -> str:
    return (
        "🔄 <b>Сообщение изменено</b>\n\n"
        f"Пользователь: {escape(_format_user(_full_name(message), _username(message)))}\n"
        "\n"
        "<b>Было:</b>\n"
        f"{escape(_truncate_text(old_value))}\n\n"
        "<b>Стало:</b>\n"
        f"{escape(_truncate_text(new_value))}\n\n"
        "Бот @MajorSpyBot"
    )


def _safe_filename_part(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    cleaned = cleaned.strip("._")
    return cleaned or "unknown"


def _format_export_timestamp(value: str | None) -> str:
    if not value:
        return "unknown"

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _plain_value(value: Any, fallback: str = "[нет данных]") -> str:
    if value is None:
        return fallback
    text = str(value)
    return text if text else fallback


def _chat_export_title(dialog: dict[str, Any]) -> str:
    return (
        dialog.get("chat_title")
        or dialog.get("full_name")
        or (f"@{dialog['username']}" if dialog.get("username") else None)
        or str(dialog.get("chat_id") or "unknown")
    )


def _export_chat_dir_name(chat_id: Any) -> str:
    return "chat_" + _safe_filename_part(chat_id)


def _clean_export_file_name(value: str | None) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value or "")
    cleaned = re.sub(r"[\x00-\x1f]+", "_", cleaned)
    cleaned = cleaned.strip(" ._")
    return cleaned or "document"


def _media_export_relative_path(media: dict[str, Any]) -> str | None:
    media_type = str(media.get("media_type") or "")
    message_id = _safe_filename_part(media.get("message_id"))
    if media_type == "photo":
        return f"photos/photo_{message_id}.jpg"
    if media_type == "video":
        return f"video_files/video_{message_id}.mp4"
    if media_type == "document":
        original_name = _clean_export_file_name(media.get("file_name"))
        return f"files/file_{message_id}_{original_name}"
    return None


def _build_export_indexes(data: dict[str, Any]) -> dict[str, Any]:
    dialog = data["dialog"]
    messages_by_key = {(row["chat_id"], row["message_id"]): row for row in data["messages"]}
    media_by_key: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    versions_by_key: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    deleted_by_key: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)

    for row in data["media"]:
        media_by_key[(row["chat_id"], row["message_id"])].append(row)
    for row in data["versions"]:
        versions_by_key[(row["chat_id"], row["message_id"])].append(row)
    for row in data["deleted"]:
        deleted_by_key[(row["chat_id"], row["message_id"])].append(row)

    keys = set(messages_by_key) | set(media_by_key) | set(versions_by_key) | set(deleted_by_key)

    def sort_key(key: tuple[int, int]) -> tuple[str, int]:
        message = messages_by_key.get(key)
        media = media_by_key.get(key, [])
        deleted = deleted_by_key.get(key, [])
        timestamp = (
            (message or {}).get("created_at")
            or (media[0].get("created_at") if media else None)
            or (deleted[0].get("deleted_at") if deleted else None)
            or ""
        )
        return timestamp, key[1]

    return {
        "dialog": dialog,
        "messages_by_key": messages_by_key,
        "media_by_key": media_by_key,
        "versions_by_key": versions_by_key,
        "deleted_by_key": deleted_by_key,
        "keys": sorted(keys, key=sort_key),
    }


def _message_author(source: dict[str, Any] | None) -> str:
    if not source:
        return "Unknown"
    return _format_user(source.get("full_name"), source.get("username"))


def _build_export_json(
    *,
    owner_user_id: int,
    data: dict[str, Any],
    exported_at: str,
    media_manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    dialog = data["dialog"]
    return {
        "owner_user_id": owner_user_id,
        "business_connection_id": dialog.get("business_connection_id"),
        "chat_id": dialog.get("chat_id"),
        "chat_title": _chat_export_title(dialog),
        "exported_at": exported_at,
        "total_messages": data.get("total_messages", 0),
        "exported_messages": len(_build_export_indexes(data)["keys"]),
        "truncated": bool(data.get("truncated")),
        "limit": data.get("limit"),
        "messages": data["messages"],
        "media": media_manifest,
        "edits": data["versions"],
        "deletions": data["deleted"],
    }


def _build_export_css() -> str:
    return """
:root {
  color-scheme: light;
  --bg: #e7edf3;
  --panel: #ffffff;
  --text: #17212b;
  --muted: #6d7883;
  --accent: #2aabee;
  --deleted: #fff1f0;
  --edited: #fff7df;
  font-family: Arial, Helvetica, sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
}
.page {
  width: min(920px, calc(100% - 24px));
  margin: 0 auto;
  padding: 24px 0 48px;
}
.header {
  background: var(--panel);
  border-radius: 8px;
  padding: 18px 20px;
  margin-bottom: 14px;
  border: 1px solid rgba(23, 33, 43, 0.08);
}
h1 {
  margin: 0 0 8px;
  font-size: 22px;
  line-height: 1.25;
}
.meta {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.5;
}
.message {
  background: var(--panel);
  border: 1px solid rgba(23, 33, 43, 0.08);
  border-radius: 8px;
  padding: 12px 14px;
  margin: 10px 0;
}
.message.deleted { background: var(--deleted); }
.topline {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: baseline;
  margin-bottom: 8px;
}
.author {
  color: var(--accent);
  font-weight: 700;
}
.time, .message-id, .user-id {
  color: var(--muted);
  font-size: 12px;
}
.text, .caption, .deleted-text, .edit-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.45;
}
.caption {
  margin-top: 8px;
  color: #26313b;
}
.media {
  margin-top: 10px;
}
.photo {
  display: block;
  max-width: min(420px, 100%);
  max-height: 560px;
  border-radius: 6px;
}
video {
  display: block;
  max-width: min(520px, 100%);
  border-radius: 6px;
  background: #000;
}
.file-link {
  color: var(--accent);
  font-weight: 700;
  text-decoration: none;
}
.missing {
  color: #b42318;
  font-weight: 700;
}
.badge {
  display: inline-block;
  color: var(--muted);
  font-size: 12px;
  margin-top: 8px;
}
.edit {
  margin-top: 10px;
  padding: 8px 10px;
  background: var(--edited);
  border-radius: 6px;
}
.edit-title, .deleted-title {
  font-weight: 700;
  margin-bottom: 5px;
}
""".strip()


def _build_messages_html(
    *,
    owner_user_id: int,
    data: dict[str, Any],
    exported_at: str,
    media_manifest: list[dict[str, Any]],
) -> str:
    indexes = _build_export_indexes(data)
    dialog = indexes["dialog"]
    messages_by_key = indexes["messages_by_key"]
    media_by_key = indexes["media_by_key"]
    versions_by_key = indexes["versions_by_key"]
    deleted_by_key = indexes["deleted_by_key"]
    media_by_id = {item["media_id"]: item for item in media_manifest if item.get("media_id") is not None}
    title = html.escape(_chat_export_title(dialog))
    chat_id = html.escape(str(dialog.get("chat_id") or "unknown"))
    business_connection = html.escape(str(dialog.get("business_connection_id") or "unknown"))
    exported_at_html = html.escape(_format_export_timestamp(exported_at))
    truncated_note = ""
    if data.get("truncated"):
        truncated_note = (
            f"<div>Экспорт ограничен последними {html.escape(str(data.get('limit')))} сообщениями.</div>"
        )

    chunks = [
        "<!doctype html>",
        '<html lang="ru">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{title}</title>",
        '<link rel="stylesheet" href="style.css">',
        "</head>",
        "<body>",
        '<main class="page">',
        '<section class="header">',
        f"<h1>{title}</h1>",
        '<div class="meta">',
        f"<div>chat_id: {chat_id}</div>",
        f"<div>Business connection: {business_connection}</div>",
        f"<div>Владелец: {html.escape(str(owner_user_id))}</div>",
        f"<div>Дата экспорта: {exported_at_html} UTC</div>",
        f"<div>Сообщений: {html.escape(str(data.get('total_messages', 0)))}</div>",
        truncated_note,
        "</div>",
        "</section>",
    ]

    for key in indexes["keys"]:
        message = messages_by_key.get(key)
        media_items = media_by_key.get(key, [])
        versions = versions_by_key.get(key, [])
        deleted = deleted_by_key.get(key, [])
        source = message or (media_items[0] if media_items else None) or (deleted[0] if deleted else {})
        timestamp = (
            (message or {}).get("created_at")
            or (media_items[0].get("created_at") if media_items else None)
            or (deleted[0].get("deleted_at") if deleted else None)
        )
        is_deleted = bool(deleted) or bool((message or {}).get("is_deleted"))
        css_class = "message deleted" if is_deleted else "message"
        chunks.extend(
            [
                f'<article class="{css_class}">',
                '<div class="topline">',
                f'<span class="author">{html.escape(_message_author(source))}</span>',
                f'<span class="time">{html.escape(_format_export_timestamp(timestamp))}</span>',
                f'<span class="message-id">Message ID: {html.escape(str(key[1]))}</span>',
            ]
        )
        if source and source.get("user_id") is not None:
            chunks.append(f'<span class="user-id">User ID: {html.escape(str(source.get("user_id")))}</span>')
        chunks.extend(["</div>"])

        if message and message.get("text") is not None:
            chunks.append(f'<div class="text">{html.escape(_plain_value(message.get("text"), "[нет текста]"))}</div>')
        if message and message.get("caption") is not None and not media_items:
            chunks.append(
                f'<div class="caption">{html.escape(_plain_value(message.get("caption"), "[нет подписи]"))}</div>'
            )

        for media in media_items:
            manifest_item = media_by_id.get(media.get("id"))
            media_type = str(media.get("media_type") or "")
            caption = media.get("caption")
            rel_path = manifest_item.get("relative_path") if manifest_item else None
            copied = bool(manifest_item and manifest_item.get("copied"))
            chunks.append('<div class="media">')
            if rel_path and copied and media_type == "photo":
                safe_rel = html.escape(rel_path, quote=True)
                chunks.append(f'<a href="{safe_rel}" target="_blank"><img src="{safe_rel}" class="photo" alt=""></a>')
            elif rel_path and copied and media_type == "video":
                safe_rel = html.escape(rel_path, quote=True)
                chunks.append(f'<video controls src="{safe_rel}"></video>')
            elif rel_path and copied and media_type == "document":
                safe_rel = html.escape(rel_path, quote=True)
                safe_name = html.escape(_clean_export_file_name(media.get("file_name")))
                chunks.append(f'<a class="file-link" href="{safe_rel}" target="_blank">{safe_name}</a>')
            else:
                chunks.append('<span class="missing">[файл не найден локально]</span>')

            if caption is not None:
                chunks.append(f'<div class="caption">{html.escape(_plain_value(caption, "[нет подписи]"))}</div>')
            chunks.append(f'<span class="badge">{html.escape(_media_label(media_type))}</span>')
            chunks.append("</div>")

        for version in versions:
            chunks.extend(
                [
                    '<div class="edit">',
                    '<div class="edit-title">[ИЗМЕНЕНО]</div>',
                    '<div class="edit-text"><b>Было:</b><br>'
                    + html.escape(_plain_value(version.get("old_text"), "[нет текста]"))
                    + "</div>",
                    '<div class="edit-text"><b>Стало:</b><br>'
                    + html.escape(_plain_value(version.get("new_text"), "[нет текста]"))
                    + "</div>",
                    "</div>",
                ]
            )

        for deleted_item in deleted:
            chunks.extend(
                [
                    '<div class="deleted-block">',
                    '<div class="deleted-title">[УДАЛЕНО]</div>',
                    f'<div class="time">Удалено: {html.escape(_format_export_timestamp(deleted_item.get("deleted_at")))}</div>',
                    '<div class="deleted-text">'
                    + html.escape(_plain_value(deleted_item.get("deleted_text"), "[нет текста]"))
                    + "</div>",
                    "</div>",
                ]
            )

        chunks.append("</article>")

    chunks.extend(["</main>", "</body>", "</html>"])
    return "\n".join(item for item in chunks if item)


def _copy_export_media(data: dict[str, Any], chat_dir: Path) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for media in data["media"]:
        relative_path = _media_export_relative_path(media)
        source_path = Path(media["local_path"]) if media.get("local_path") else None
        copied = False
        destination_path: Path | None = None
        if relative_path:
            destination_path = chat_dir / relative_path
            destination_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path and source_path.exists() and destination_path:
            shutil.copy2(source_path, destination_path)
            copied = True

        item = dict(media)
        item["relative_path"] = relative_path
        item["copied"] = copied
        item["missing"] = not copied
        item["media_id"] = media.get("id")
        manifest.append(item)
    return manifest


def _write_zip_from_dir(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_dir():
                archive.write(file_path, str(file_path.relative_to(source_dir)).replace("\\", "/") + "/")
                continue
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(source_dir))


def _build_export_text(owner_user_id: int, data: dict[str, Any]) -> str:
    indexes = _build_export_indexes(data)
    dialog = indexes["dialog"]
    lines = [
        "Экспорт диалога",
        "",
        f"Владелец: {owner_user_id}",
        f"Business connection: {dialog.get('business_connection_id')}",
        f"Название чата/пользователь: {_chat_export_title(dialog)}",
        f"chat_id: {dialog.get('chat_id')}",
        f"Дата экспорта: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"Количество сообщений: {data.get('total_messages', 0)}",
        f"Количество экспортированных сообщений: {len(keys)}",
        f"Количество изменений: {len(data['versions'])}",
        f"Количество удалений: {len(data['deleted'])}",
    ]
    if data.get("truncated"):
        lines.append(f"Экспорт ограничен последними {data.get('limit')} сообщениями.")
    lines.extend(["", "Сообщения:", ""])

    for key in indexes["keys"]:
        message = indexes["messages_by_key"].get(key)
        media_items = indexes["media_by_key"].get(key, [])
        versions = indexes["versions_by_key"].get(key, [])
        deleted = indexes["deleted_by_key"].get(key, [])
        source = message or (media_items[0] if media_items else None) or (deleted[0] if deleted else {})
        timestamp = (
            (message or {}).get("created_at")
            or (media_items[0].get("created_at") if media_items else None)
            or (deleted[0].get("deleted_at") if deleted else None)
        )
        message_type = "text"
        if media_items:
            message_type = "/".join(_plain_value(item.get("media_type"), "media") for item in media_items)
        elif message and message.get("caption") and not message.get("text"):
            message_type = "caption"

        lines.append(f"[{_format_export_timestamp(timestamp)}]")
        lines.append(
            "Пользователь: "
            + _format_user(source.get("full_name") if source else None, source.get("username") if source else None)
        )
        if source and source.get("user_id") is not None:
            lines.append(f"User ID: {source.get('user_id')}")
        lines.append(f"Message ID: {key[1]}")
        lines.append(f"Тип: {message_type}")

        if message and message.get("text") is not None:
            lines.extend(["Текст:", _plain_value(message.get("text"), "[нет текста]")])
        if message and message.get("caption") is not None and not media_items:
            lines.extend(["Caption:", _plain_value(message.get("caption"), "[нет подписи]")])

        for media in media_items:
            lines.append(f"Тип: {_plain_value(media.get('media_type'), 'media')}")
            lines.extend(["Caption:", _plain_value(media.get("caption"), "[нет подписи]")])
            local_path = media.get("local_path")
            if local_path:
                exists_suffix = "" if Path(local_path).exists() else " [файл отсутствует локально]"
                lines.append(f"Файл: {local_path}{exists_suffix}")
            else:
                lines.append("Файл: [не сохранён]")
            if media.get("file_name"):
                lines.append(f"Имя файла: {media.get('file_name')}")
            if media.get("mime_type"):
                lines.append(f"MIME: {media.get('mime_type')}")

        for version in versions:
            lines.append("[ИЗМЕНЕНО]")
            lines.extend(["Было:", _plain_value(version.get("old_text"), "[нет текста]")])
            lines.extend(["Стало:", _plain_value(version.get("new_text"), "[нет текста]")])

        for deleted_item in deleted:
            lines.append("[УДАЛЕНО]")
            lines.append(f"Удалено: {_format_export_timestamp(deleted_item.get('deleted_at'))}")
            lines.extend(["Удалённый текст:", _plain_value(deleted_item.get("deleted_text"), "[нет текста]")])
            if deleted_item.get("media_id") is not None:
                lines.append(f"Media ID: {deleted_item.get('media_id')}")

        lines.append("")

    return "\n".join(lines)


def _export_base_name(owner_user_id: int, data: dict[str, Any]) -> str:
    dialog = data["dialog"]
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return (
        "export_"
        + _safe_filename_part(owner_user_id)
        + "_"
        + _safe_filename_part(dialog.get("chat_id"))
        + "_"
        + _safe_filename_part(date_part)
    )


def create_export_file(owner_user_id: int, data: dict[str, Any], export_format: str = "html") -> Path:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    export_format = export_format.lower().strip()
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format: {export_format}")

    dialog = data["dialog"]
    base_name = _export_base_name(owner_user_id, data)
    exported_at = datetime.now(timezone.utc).isoformat()

    if export_format == "json":
        json_path = EXPORTS_DIR / f"{base_name}.json"
        json_path.write_text(
            json.dumps(
                _build_export_json(
                    owner_user_id=owner_user_id,
                    data=data,
                    exported_at=exported_at,
                    media_manifest=[],
                ),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logging.info("EXPORT json created path=%s", json_path)
        return json_path

    if export_format == "txt":
        txt_path = EXPORTS_DIR / f"{base_name}.txt"
        txt_path.write_text(_build_export_text(owner_user_id, data), encoding="utf-8")
        logging.info("EXPORT txt created path=%s", txt_path)
        return txt_path

    chat_dir = EXPORTS_DIR / base_name / _export_chat_dir_name(dialog.get("chat_id"))
    if chat_dir.parent.exists():
        shutil.rmtree(chat_dir.parent)
    for directory_name in ("photos", "video_files", "files"):
        (chat_dir / directory_name).mkdir(parents=True, exist_ok=True)

    media_manifest = _copy_export_media(data, chat_dir)
    (chat_dir / "style.css").write_text(_build_export_css(), encoding="utf-8")
    (chat_dir / "messages.html").write_text(
        _build_messages_html(
            owner_user_id=owner_user_id,
            data=data,
            exported_at=exported_at,
            media_manifest=media_manifest,
        ),
        encoding="utf-8",
    )
    (chat_dir / "messages.json").write_text(
        json.dumps(
            _build_export_json(
                owner_user_id=owner_user_id,
                data=data,
                exported_at=exported_at,
                media_manifest=media_manifest,
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    zip_path = EXPORTS_DIR / f"{base_name}.zip"
    if zip_path.exists():
        zip_path.unlink()
    _write_zip_from_dir(chat_dir.parent, zip_path)
    shutil.rmtree(chat_dir.parent)
    logging.info("EXPORT zip created path=%s", zip_path)
    return zip_path


def _export_format_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="HTML", callback_data=f"export:{token}:html"),
                InlineKeyboardButton(text="JSON", callback_data=f"export:{token}:json"),
                InlineKeyboardButton(text="TXT", callback_data=f"export:{token}:txt"),
            ]
        ]
    )


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _subscription_status(owner_user_id: int | None) -> tuple[bool, str]:
    if owner_user_id is None:
        return False, "подписка не найдена"
    if owner_user_id in SUPER_ADMIN_IDS:
        return True, "super admin"

    subscription = get_user_subscription(owner_user_id)
    if not subscription:
        return False, "trial ещё не активирован"

    now = datetime.now(timezone.utc)
    paid_until = _parse_iso_datetime(subscription.get("paid_until"))
    if paid_until and paid_until > now:
        days_left = max(1, (paid_until - now).days + 1)
        return True, f"Major Pro активен, осталось {days_left} дн."

    trial_ends_at = _parse_iso_datetime(subscription.get("trial_ends_at"))
    if trial_ends_at and trial_ends_at > now:
        seconds_left = int((trial_ends_at - now).total_seconds())
        days_left = max(1, (seconds_left + 86399) // 86400)
        return True, f"пробный период, осталось {days_left} дн."

    return False, "пробный период закончился"


async def _ensure_subscription_access(bot: Bot, business_connection_id: str) -> bool:
    owner_user_id = _active_connection_owner_id(business_connection_id)
    active, status = _subscription_status(owner_user_id)
    if active:
        return True

    allowed, _ = _check_rate_limit(
        "subscription_notice",
        owner_user_id,
        limit=1,
        window_seconds=3600,
        block_seconds=3600,
    )
    if allowed and owner_user_id is not None:
        await bot.send_message(
            chat_id=owner_user_id,
            text=(
                "👑 <b>Нужен Major Pro</b>\n\n"
                f"Статус: {escape(status)}.\n"
                "Чтобы бот продолжил сохранять сообщения, продлите подписку."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_pro_keyboard(),
        )
    return False


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Функции", callback_data="menu:features"),
                InlineKeyboardButton(text="Перевод", callback_data="menu:translation"),
            ],
            [
                InlineKeyboardButton(text="Экспорт", callback_data="menu:export"),
                InlineKeyboardButton(text="Статистика", callback_data="menu:stats"),
            ],
        ]
    )


def _start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подключить", url=CONNECT_URL),
                InlineKeyboardButton(text="🔵 Major Pro", callback_data="menu:pro"),
            ],
            [
                InlineKeyboardButton(text="Инструкция", callback_data="menu:instruction"),
                InlineKeyboardButton(text="Демонстрация работы", callback_data="menu:demo"),
            ],
        ]
    )


def _start_text(user_id: int | None, status: str) -> str:
    _, subscription_text = _subscription_status(user_id)
    return (
        "👋 <b>Добро пожаловать в Major Spy</b>\n\n"
        "Это помощник для Telegram Business. Он сохраняет переписки, медиа, удаления и изменения, "
        "а ещё помогает быстро переводить сообщения и исправлять раскладку прямо в чате.\n\n"
        "<b>Что умеет бот:</b>\n"
        "• сохраняет сообщения, фото, видео и файлы\n"
        "• присылает удалённые сообщения\n"
        "• показывает изменения текста\n"
        "• экспортирует диалоги в HTML / JSON / TXT\n"
        "• переводит и исправляет раскладку через reply-команды\n\n"
        "Нажмите <b>Подключить</b>, чтобы открыть настройки Telegram Business и добавить бота.\n\n"
        f"<b>Ваш ID:</b> {user_id}\n"
        f"<b>Статус:</b> {escape(status)}\n"
        f"<b>Подписка:</b> {escape(subscription_text)}"
    )


def _features_text() -> str:
    return (
        "<b>Функции Major Spy</b>\n\n"
        "• Сохранение Business-сообщений\n"
        "• Уведомления об удалённых сообщениях\n"
        "• Уведомления об изменениях текста\n"
        "• Сохранение фото, видео и файлов\n"
        "• Экспорт конкретного диалога\n"
        "• Reply-команды для текста собеседника"
    )


def _translation_text() -> str:
    return (
        "<b>Настройки перевода</b>\n\n"
        "Доступные функции:\n\n"
        "1. Авто-транскрипция (исправление раскладки)\n"
        "   • ghbdtn → привет\n"
        "   • Статус: выключена\n\n"
        "2. Авто-перевод (английский → русский)\n"
        "   • hello → привет\n"
        "   • Статус: выключен\n\n"
        "<b>Как пользоваться:</b>\n"
        "• ответьте <code>.transcript</code> на сообщение собеседника\n"
        "• ответьте <code>.translate</code> на английское сообщение собеседника\n\n"
        "Бот изменит ваше командное сообщение и покажет результат прямо в чате."
    )


def _instruction_text() -> str:
    return (
        "<b>Как подключить Major Spy</b>\n\n"
        "1. Нажмите кнопку <b>Подключить</b>.\n"
        "2. В настройках Telegram откройте раздел Telegram Business.\n"
        "3. Добавьте <b>@MajorSpyBot</b> как business-бота.\n"
        "4. Выдайте доступ к сообщениям.\n"
        "5. Вернитесь сюда и отправьте /start.\n\n"
        "После подключения бот начнёт сохранять новые сообщения и медиа."
    )


def _demo_text() -> str:
    return (
        "<b>Демонстрация работы</b>\n\n"
        "<b>Удаление:</b>\n"
        "собеседник удаляет сообщение → бот присылает сохранённый текст или медиа.\n\n"
        "<b>Изменение:</b>\n"
        "собеседник меняет сообщение → бот показывает <b>Было</b> и <b>Стало</b>.\n\n"
        "<b>Перевод:</b>\n"
        "ответьте <code>.translate</code> на английское сообщение → команда заменится переводом прямо в чате.\n\n"
        "<b>Раскладка:</b>\n"
        "ответьте <code>.transcript</code> на текст вроде <code>ghbdtn</code> → получите <code>привет</code>."
    )


def _pro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"{title} — {price} ₽", callback_data=f"pro:plan:{code}")
            ]
            for code, (title, price) in PRO_TARIFFS.items()
        ]
        + [[InlineKeyboardButton(text="Назад", callback_data="menu:instruction")]]
    )


def _pro_text(owner_user_id: int | None) -> str:
    _, status = _subscription_status(owner_user_id)
    return (
        "👑 <b>Major Pro</b>\n\n"
        "Первые <b>7 дней бесплатно</b> после подключения Business-бота.\n\n"
        "<b>Что входит:</b>\n"
        "• сохранение удалённых сообщений\n"
        "• сохранение фото, видео, кружков, голосовых и аудио\n"
        "• уведомления об изменениях\n"
        "• экспорт HTML / JSON / TXT\n"
        "• перевод и исправление раскладки\n"
        "• хранение медиа 3 дня с предупреждением за 24 часа\n\n"
        "<b>Тарифы:</b>\n"
        "• Неделя — 99 ₽\n"
        "• 1 месяц — 249 ₽\n"
        "• 3 месяца — 599 ₽\n"
        "• 6 месяцев — 990 ₽\n"
        "• Год — 1 690 ₽\n\n"
        f"<b>Ваш статус:</b> {escape(status)}"
    )


def _export_help_text() -> str:
    return (
        "<b>Экспорт чата</b>\n\n"
        "1. Откройте список диалогов: /dialogs\n"
        "2. Скопируйте chat_id, username или user_id\n"
        "3. Запустите экспорт: <code>/export chat_id</code>\n"
        "4. Выберите формат: HTML / JSON / TXT"
    )


def _export_format_caption(export_format: str) -> str:
    if export_format == "html":
        return "HTML-экспорт диалога из локальной SQLite."
    if export_format == "json":
        return "JSON-экспорт диалога из локальной SQLite."
    return "TXT-экспорт диалога из локальной SQLite."


def _parse_export_query(message: Message) -> str | None:
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip():
        return None
    return parts[1].strip()


def fix_keyboard_layout(text: str) -> str:
    return text.translate(LAYOUT_TRANSLATION)


def _extract_dot_command_text(message: Message, command: str) -> str | None:
    text = message.text or ""
    value = text[len(command):].strip()
    if value:
        return value

    reply = message.reply_to_message
    if reply:
        return (reply.text or reply.caption or "").strip() or None
    return None


def _translate_en_to_ru_sync(text: str) -> str:
    normalized = text.strip().lower()
    if normalized in TRANSLATE_FALLBACK:
        return TRANSLATE_FALLBACK[normalized]

    query = urllib.parse.urlencode({"q": text, "langpair": "en|ru"})
    request = urllib.request.Request(
        f"https://api.mymemory.translated.net/get?{query}",
        headers={"User-Agent": "MajorSpyBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))

    translated = (
        payload.get("responseData", {}).get("translatedText")
        if isinstance(payload, dict)
        else None
    )
    if not translated:
        raise RuntimeError("empty translation response")
    return str(translated)


async def translate_en_to_ru(text: str) -> str:
    return await asyncio.to_thread(_translate_en_to_ru_sync, text)


def _dot_command_name(text: str) -> str | None:
    if not text.strip():
        return None

    command = text.split(maxsplit=1)[0].lower()
    if command in {".transcript", ".translate"}:
        return command
    return None


def _reply_source_text(message: Message) -> str | None:
    reply = message.reply_to_message
    if not reply:
        return None
    return (reply.text or reply.caption or "").strip() or None


async def _run_dot_text_command(message: Message, command: str) -> str | None:
    source = _extract_dot_command_text(message, command)
    if not source:
        return None

    if command == ".transcript":
        return fix_keyboard_layout(source)

    return await translate_en_to_ru(source)


def _format_inline_dot_result(message: Message, command: str, result: str | None) -> str:
    source = _reply_source_text(message) or _extract_dot_command_text(message, command) or ""
    title = "Исправление раскладки" if command == ".transcript" else "Перевод"
    return (
        f"<b>{title}</b>\n\n"
        f"<b>Собеседник:</b>\n"
        f"{escape(_truncate_text(source, 1200))}\n\n"
        f"<b>Результат:</b>\n"
        f"{escape(_truncate_text(result or '[нет результата]', 1200))}"
    )


async def _handle_business_dot_command(message: Message, bot: Bot, business_connection_id: str) -> bool:
    text = (message.text or "").strip()
    command = _dot_command_name(text)
    if not command:
        return False

    owner_user_id = _active_connection_owner_id(business_connection_id)
    if owner_user_id is None or _user_id(message) != owner_user_id:
        return False

    if not _reply_source_text(message):
        await safe_notify_connection_owner(
            bot,
            business_connection_id,
            "Ответьте командой <code>.transcript</code> или <code>.translate</code> на сообщение собеседника.",
        )
        return True

    try:
        result = await _run_dot_text_command(message, command)
    except Exception:
        logging.exception("Business dot command failed command=%s", command)
        await safe_notify_connection_owner(bot, business_connection_id, "Не получилось обработать текст сейчас.")
        return True

    await safe_notify_connection_owner(bot, business_connection_id, _format_inline_dot_result(message, command, result))
    return True


async def handle_translation_settings(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    await message.answer(_translation_text(), parse_mode=ParseMode.HTML, reply_markup=_main_menu_keyboard())


async def handle_private_text(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    text = (message.text or "").strip()
    command = _dot_command_name(text)
    if command == ".transcript":
        if not _reply_source_text(message) and not _extract_dot_command_text(message, command):
            await message.answer("Ответьте .transcript на сообщение или напишите текст после команды.")
            return

        result = await _run_dot_text_command(message, command)
        await message.answer(result or "[нет результата]")
        return

    if command == ".translate":
        if not _reply_source_text(message) and not _extract_dot_command_text(message, command):
            await message.answer("Ответьте .translate на английское сообщение или напишите текст после команды.")
            return

        try:
            translated = await _run_dot_text_command(message, command)
        except Exception:
            logging.exception("Manual translate failed")
            await message.answer("Не получилось перевести сейчас. Попробуйте позже.")
            return

        await message.answer(translated or "[нет результата]")


async def handle_start(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    user_id = _user_id(message)
    allowed, retry_after = _check_rate_limit("start", user_id)
    if not allowed:
        await message.answer(f"Слишком много запросов. Подождите {retry_after} сек.")
        return

    status = "не подключён"
    if user_id is not None and is_connected_owner(user_id):
        status = "подключён как владелец"
        if not get_user_subscription(user_id):
            ensure_trial_subscription(user_id, TRIAL_DAYS)
    if user_id in SUPER_ADMIN_IDS:
        status = "super admin"

    await message.answer(
        _start_text(user_id, status),
        parse_mode=ParseMode.HTML,
        reply_markup=_start_keyboard(),
    )


async def handle_help(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    user_id = _user_id(message)
    if user_id in SUPER_ADMIN_IDS or (user_id is not None and is_connected_owner(user_id)):
        await message.answer(
            _features_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_main_menu_keyboard(),
        )


async def handle_menu_callback(callback: CallbackQuery, bot: Bot) -> None:
    data = callback.data or ""
    if not data.startswith("menu:"):
        return

    user_id = callback.from_user.id if callback.from_user else None
    allowed, retry_after = _check_rate_limit("callback", user_id)
    if not allowed:
        await callback.answer(f"Слишком много нажатий. Подождите {retry_after} сек.", show_alert=True)
        return

    if data == "menu:features":
        text = _features_text()
    elif data == "menu:translation":
        text = _translation_text()
    elif data == "menu:export":
        text = _export_help_text()
    elif data == "menu:instruction":
        text = _instruction_text()
    elif data == "menu:demo":
        text = _demo_text()
    elif data == "menu:pro":
        text = _pro_text(user_id)
    elif data == "menu:stats":
        if user_id in SUPER_ADMIN_IDS:
            stats = get_stats()
        elif user_id is not None and is_connected_owner(user_id):
            stats = get_stats_for_owner(user_id)
        else:
            await callback.answer("Нет доступа.", show_alert=True)
            return
        text = (
            "<b>Статистика</b>\n\n"
            f"Подключений: {stats['business_connections']}\n"
            f"Сохранено сообщений: {stats['business_messages']}\n"
            f"Сохранено медиа: {stats['business_media']}\n"
            f"Удалений: {stats['business_deleted_messages']}\n"
            f"Изменений: {stats['business_versions']}\n"
            f"Уникальных чатов: {stats['business_unique_chats']}"
        )
    else:
        await callback.answer()
        return

    if data == "menu:pro":
        reply_markup = _pro_keyboard()
    elif data in {"menu:instruction", "menu:demo"}:
        reply_markup = _start_keyboard()
    else:
        reply_markup = _main_menu_keyboard()
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except Exception:
            logging.warning("Menu callback edit skipped data=%s user_id=%s", data, user_id, exc_info=True)
    await callback.answer()


async def handle_pro_plan_callback(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "pro" or parts[1] != "plan":
        await callback.answer()
        return

    code = parts[2]
    tariff = PRO_TARIFFS.get(code)
    if not tariff:
        await callback.answer("Тариф не найден.", show_alert=True)
        return

    title, price = tariff
    text = (
        "👑 <b>Major Pro</b>\n\n"
        f"Вы выбрали: <b>{escape(title)}</b>\n"
        f"Стоимость: <b>{price} ₽</b>\n\n"
        "Автооплата ещё не подключена. Сейчас этот экран нужен для выбора тарифа; "
        "дальше можно подключить Telegram Payments или ЮKassa и активировать подписку автоматически."
    )
    if callback.message:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=_pro_keyboard())
    await callback.answer()


async def handle_quota_delete_now(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    if user_id is None:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    allowed, retry_after = _check_rate_limit("quota_delete", user_id, limit=3, window_seconds=60, block_seconds=60)
    if not allowed:
        await callback.answer(f"Слишком много нажатий. Подождите {retry_after} сек.", show_alert=True)
        return

    deleted_count = await cleanup_media_quota_files(owner_filter=user_id, force=True)
    if deleted_count:
        text = (
            "✅ <b>Очистка выполнена</b>\n\n"
            f"Удалено файлов: {deleted_count}.\n"
            f"Бот остановился, когда размер медиа стал ниже лимита {MEDIA_USER_LIMIT_MB} МБ."
        )
    else:
        text = (
            "✅ <b>Очистка не требуется</b>\n\n"
            f"Сейчас размер медиа не превышает лимит {MEDIA_USER_LIMIT_MB} МБ."
        )

    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            logging.exception("Failed to edit quota cleanup callback message")
    await callback.answer("Готово")


async def handle_ping(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    user_id = _user_id(message)
    lines = [f"user_id: {user_id}"]
    connected_owner = user_id is not None and is_connected_owner(user_id)
    super_admin = user_id in SUPER_ADMIN_IDS
    if connected_owner:
        lines.append("connected owner ok")
    if super_admin:
        lines.append("super admin ok")
    if not connected_owner and not super_admin:
        lines.append("not connected")
    await message.answer("\n".join(lines))


async def handle_debug(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    user_id = _user_id(message)
    if user_id not in SUPER_ADMIN_IDS:
        logging.info("DEBUG denied user_id=%s", user_id)
        return

    stats = get_stats()
    await message.answer(
        "Debug:\n"
        f"SUPER_ADMIN_IDS: {escape(str(SUPER_ADMIN_IDS))}\n"
        f"ALLOWED_USER_IDS: {escape(str(sorted(ALLOWED_USER_IDS)))}\n"
        f"MAX_DOWNLOAD_MB: {MAX_DOWNLOAD_MB}\n"
        f"business_connections: {stats['business_connections']}\n"
        f"business_messages: {stats['business_messages']}\n"
        f"business_media: {stats['business_media']}\n"
        f"deleted_events: {stats['business_deleted_messages']}\n"
        f"edited_events: {stats['business_versions']}"
    )


async def handle_stats(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    user_id = _user_id(message)
    if user_id in SUPER_ADMIN_IDS:
        stats = get_stats()
    elif user_id is not None and is_connected_owner(user_id):
        stats = get_stats_for_owner(user_id)
    else:
        logging.info("STATS denied user_id=%s", user_id)
        await message.answer("Нет доступа.")
        return

    await message.answer(
        "Статистика:\n"
        f"Подключений: {stats['business_connections']}\n"
        f"Сохранено сообщений: {stats['business_messages']}\n"
        f"Сохранено медиа: {stats['business_media']}\n"
        f"Удалений: {stats['business_deleted_messages']}\n"
        f"Изменений: {stats['business_versions']}\n"
        f"Уникальных чатов: {stats['business_unique_chats']}\n"
        f"Уникальных пользователей: {stats['business_unique_users']}"
    )


async def handle_export_help(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    await message.answer(
        "/dialogs — список диалогов\n"
        "/export <chat_id или username или user_id> — выбрать формат и экспортировать диалог\n"
        "/translation — настройки перевода\n"
        ".transcript <текст> — исправить раскладку\n"
        ".translate <text> — перевести английский текст"
    )


async def handle_dialogs(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    user_id = _user_id(message)
    if user_id is None or not is_connected_owner(user_id):
        logging.info("DIALOGS denied user_id=%s", user_id)
        await message.answer("Нет доступа.")
        return

    dialogs = get_dialogs_for_owner(user_id)
    if not dialogs:
        await message.answer("Диалоги не найдены. Бот показывает только сообщения, которые уже есть в SQLite.")
        return

    lines = ["chat_id | username | full_name | messages_count | last_message_at"]
    for dialog in dialogs[:80]:
        username = f"@{dialog['username']}" if dialog.get("username") else "-"
        full_name = dialog.get("full_name") or dialog.get("chat_title") or "-"
        lines.append(
            f"{dialog.get('chat_id')} | {username} | {full_name} | "
            f"{dialog.get('messages_count', 0)} | {_format_export_timestamp(dialog.get('last_message_at'))}"
        )
    if len(dialogs) > 80:
        lines.append(f"...и ещё {len(dialogs) - 80}")

    await message.answer("\n".join(lines))


async def handle_export(message: Message, bot: Bot) -> None:
    if message.chat.type != PRIVATE_CHAT:
        return

    user_id = _user_id(message)
    allowed, retry_after = _check_rate_limit("export", user_id, limit=3, window_seconds=60, block_seconds=90)
    if not allowed:
        await message.answer(f"Слишком много экспортов. Подождите {retry_after} сек.")
        return

    query = _parse_export_query(message)
    logging.info("EXPORT requested owner_id=%s", user_id)
    if user_id is None:
        logging.info("EXPORT allowed=False")
        logging.info("EXPORT denied reason=missing_user_id")
        return
    if not query:
        logging.info("EXPORT allowed=False")
        logging.info("EXPORT denied reason=missing_query")
        await message.answer("Использование: /export <chat_id или username или user_id>")
        return

    connected_owner = is_connected_owner(user_id)
    super_admin = user_id in SUPER_ADMIN_IDS
    if connected_owner:
        dialog = find_dialog_for_owner(user_id, query)
        if not dialog:
            logging.info("EXPORT allowed=False")
            logging.info("EXPORT denied reason=dialog_not_found owner_id=%s", user_id)
            await message.answer("Диалог не найден среди ваших подключений.")
            return

        data = export_dialog_data(dialog["business_connection_id"], query, MAX_EXPORT_MESSAGES)
        if not data:
            logging.info("EXPORT allowed=False")
            logging.info("EXPORT denied reason=dialog_export_empty owner_id=%s", user_id)
            await message.answer("Диалог не найден среди ваших подключений.")
            return

        logging.info("EXPORT allowed=True")
        token = secrets.token_urlsafe(8)
        PENDING_EXPORTS[token] = {
            "owner_user_id": user_id,
            "business_connection_id": dialog["business_connection_id"],
            "query": query,
            "created_at": datetime.now(timezone.utc),
        }
        await message.answer(
            "Выберите формат экспорта:",
            reply_markup=_export_format_keyboard(token),
        )
        return

    if super_admin:
        stats = get_stats()
        logging.info("EXPORT allowed=False")
        logging.info("EXPORT denied reason=super_admin_export_disabled owner_id=%s", user_id)
        await message.answer(
            "SUPER_ADMIN: экспорт диалогов доступен только владельцу конкретного business_connection.\n"
            "Диагностика без текстов:\n"
            f"business_connections: {stats['business_connections']}\n"
            f"business_messages: {stats['business_messages']}\n"
            f"business_media: {stats['business_media']}\n"
            f"deleted_events: {stats['business_deleted_messages']}\n"
            f"edited_events: {stats['business_versions']}"
        )
        return

    logging.info("EXPORT allowed=False")
    logging.info("EXPORT denied reason=not_connected_owner owner_id=%s", user_id)
    await message.answer("Нет доступа.")


async def handle_export_format(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id if callback.from_user else None
    allowed, retry_after = _check_rate_limit("callback", user_id)
    if not allowed:
        await callback.answer(f"Слишком много нажатий. Подождите {retry_after} сек.", show_alert=True)
        return

    data_raw = callback.data or ""
    parts = data_raw.split(":")
    if len(parts) != 3 or parts[0] != "export":
        await callback.answer()
        return

    token = parts[1]
    export_format = parts[2].lower()
    pending = PENDING_EXPORTS.get(token)
    if not pending or export_format not in EXPORT_FORMATS:
        await callback.answer("Экспорт устарел. Запустите /export ещё раз.", show_alert=True)
        return

    if user_id != pending["owner_user_id"]:
        await callback.answer("Это не ваш экспорт.", show_alert=True)
        return

    export_data = export_dialog_data(
        pending["business_connection_id"],
        pending["query"],
        MAX_EXPORT_MESSAGES,
    )
    if not export_data:
        PENDING_EXPORTS.pop(token, None)
        await callback.answer("Диалог не найден.", show_alert=True)
        return

    path = create_export_file(user_id, export_data, export_format)
    if path.stat().st_size > MAX_EXPORT_FILE_BYTES:
        await bot.send_message(
            chat_id=user_id,
            text=f"Файл экспорта больше 45 MB и не отправлен. Локальный путь: {path}",
        )
        await callback.answer()
        return

    await bot.send_document(
        chat_id=user_id,
        document=FSInputFile(str(path)),
        caption=_export_format_caption(export_format),
    )
    PENDING_EXPORTS.pop(token, None)
    if callback.message:
        await callback.message.edit_text(f"Экспорт отправлен в формате {export_format.upper()}.")
    await callback.answer()


async def handle_group_message(message: Message) -> None:
    if message.chat.type not in GROUP_CHATS:
        return

    if not message.from_user or message.from_user.is_bot:
        return

    if not _is_allowed_user(message.from_user.id):
        return

    if not message.text or _is_ignored_group_command(message.text):
        return

    save_message(
        chat_id=message.chat.id,
        chat_title=message.chat.title,
        message_id=message.message_id,
        user_id=_user_id(message),
        username=_username(message),
        full_name=_full_name(message),
        text=message.text,
        created_at=_message_created_at(message),
    )


async def handle_edited_message(message: Message, bot: Bot) -> None:
    if message.chat.type not in GROUP_CHATS:
        return

    if not message.from_user or message.from_user.is_bot:
        return

    if not _is_allowed_user(message.from_user.id) or not message.text:
        return

    old_message = get_message(message.chat.id, message.message_id)
    if not old_message:
        save_message(
            chat_id=message.chat.id,
            chat_title=message.chat.title,
            message_id=message.message_id,
            user_id=_user_id(message),
            username=_username(message),
            full_name=_full_name(message),
            text=message.text,
            created_at=_message_created_at(message),
        )
        return

    old_text = old_message.get("text")
    if old_text == message.text:
        return

    save_message_version(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=_user_id(message),
        old_text=old_text,
        new_text=message.text,
        edited_at=_message_created_at(message),
    )
    update_message_text(message.chat.id, message.message_id, message.text)
    logging.info(
        "GROUP_EDITED_MESSAGE saved chat_id=%s message_id=%s user_id=%s",
        message.chat.id,
        message.message_id,
        _user_id(message),
    )


# Business handlers are read-only toward the source chat.
# They must never answer, reply, or send anything to the business conversation.
async def handle_business_connection(event: BusinessConnection, bot: Bot) -> None:
    try:
        user = event.user
        if not user:
            logging.warning("BUSINESS_CONNECTION skipped missing owner user business_connection_id=%s", event.id)
            return
        existing_subscription = get_user_subscription(user.id)
        save_business_connection(
            business_connection_id=event.id,
            owner_user_id=user.id,
            owner_username=user.username,
            owner_full_name=user.full_name if user.full_name else None,
            is_enabled=event.is_enabled,
            can_reply=getattr(event, "can_reply", None),
            rights=str(getattr(event, "rights", "")) if getattr(event, "rights", None) is not None else None,
        )
        logging.info(
            "BUSINESS_CONNECTION updated business_connection_id=%s is_enabled=%s",
            event.id,
            event.is_enabled,
        )
        subscription = ensure_trial_subscription(user.id, TRIAL_DAYS)
        if event.is_enabled and not existing_subscription and user.id not in SUPER_ADMIN_IDS:
            trial_ends_at = _parse_iso_datetime(subscription.get("trial_ends_at"))
            trial_until = trial_ends_at.strftime("%d.%m.%Y %H:%M") if trial_ends_at else "через 7 дней"
            await bot.send_message(
                chat_id=user.id,
                text=(
                    "✅ <b>Business подключён</b>\n\n"
                    f"Запущен пробный период Major Pro на <b>{TRIAL_DAYS} дней</b>.\n"
                    f"Активен до: <b>{escape(trial_until)}</b>\n\n"
                    "Пока trial активен, бот сохраняет удаления, изменения, медиа и экспорт чатов."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=_pro_keyboard(),
            )
    except Exception:
        logging.exception("Unexpected error in business_connection handler")


async def handle_business_message(message: Message, bot: Bot) -> None:
    try:
        business_connection_id = _business_connection_id(message)
        if not business_connection_id:
            logging.warning("BUSINESS_MESSAGE skipped missing business_connection_id")
            return
        _write_business_debug_update(message, business_connection_id)

        if not await _ensure_subscription_access(bot, business_connection_id):
            return

        if message.from_user and message.from_user.is_bot:
            return
        if message.sender_business_bot:
            return

        user_id = _user_id(message)
        if not _is_allowed_user(user_id):
            return

        has_text = bool(message.text)
        has_media = _is_media_message(message)
        reply_media = message.reply_to_message if message.reply_to_message and _is_media_message(message.reply_to_message) else None
        reply_placeholder = (
            message.reply_to_message
            if message.reply_to_message and not reply_media and _is_empty_reply_placeholder(message.reply_to_message)
            else None
        )
        saved = False

        if has_text and await _handle_business_dot_command(message, bot, business_connection_id):
            logging.info(
                "BUSINESS_DOT_COMMAND handled business_connection_id=%s chat_id=%s message_id=%s user_id=%s",
                business_connection_id,
                message.chat.id,
                message.message_id,
                user_id,
            )
            return

        if has_text or message.caption:
            save_business_message(**_business_message_payload(message, message.text, message.caption))
            saved = True

        if has_media:
            await _save_and_notify_business_media(
                bot=bot,
                business_connection_id=business_connection_id,
                message=message,
                source="message",
            )
            saved = True

        if reply_media:
            await _save_and_notify_business_media(
                bot=bot,
                business_connection_id=business_connection_id,
                message=reply_media,
                source="reply",
            )
            saved = True

        if reply_placeholder:
            await safe_notify_connection_owner(
                bot,
                business_connection_id,
                "⚠️ <b>Ответ на исчезающее медиа пойман</b>\n\n"
                "Но Telegram не передал боту файл внутри reply_to_message. "
                "В апдейте есть только пустая заглушка сообщения, без photo/video/document.\n\n"
                f"Пользователь: {escape(_format_user(_full_name(message), _username(message)))}\n"
                f"Reply Message ID: {reply_placeholder.message_id}\n\n"
                "Бот @MajorSpyBot",
            )

        logging.info(
            "BUSINESS_MESSAGE received business_connection_id=%s chat_id=%s message_id=%s content_type=%s saved=%s has_text=%s has_media=%s has_reply_media=%s has_reply_placeholder=%s photo=%s video=%s document=%s audio=%s voice=%s video_note=%s paid_media=%s protected=%s",
            business_connection_id,
            message.chat.id,
            message.message_id,
            getattr(message, "content_type", ""),
            saved,
            has_text,
            has_media,
            bool(reply_media),
            bool(reply_placeholder),
            bool(message.photo),
            bool(message.video),
            bool(message.document),
            bool(message.audio),
            bool(message.voice),
            bool(message.video_note),
            bool(message.paid_media),
            bool(message.has_protected_content),
        )
    except Exception:
        logging.exception("Unexpected error in business_message handler")


async def handle_edited_business_message(message: Message, bot: Bot) -> None:
    try:
        business_connection_id = _business_connection_id(message)
        if not business_connection_id:
            logging.warning("EDITED_BUSINESS_MESSAGE skipped missing business_connection_id")
            return

        if not await _ensure_subscription_access(bot, business_connection_id):
            return

        if message.from_user and message.from_user.is_bot:
            return
        if message.sender_business_bot:
            return

        user_id = _user_id(message)
        if not _is_allowed_user(user_id):
            return

        old_message = get_business_message(business_connection_id, message.chat.id, message.message_id)
        old_media = get_business_media(business_connection_id, message.chat.id, message.message_id)
        old_found = bool(old_message or old_media)
        changed = False

        if not old_found:
            save_business_message(**_business_message_payload(message, message.text, message.caption))
            await safe_notify_connection_owner(
                bot,
                business_connection_id,
                "⚠️ <b>Сообщение изменено, но старая версия не найдена</b>\n\n"
                f"Business connection: {escape(business_connection_id)}\n"
                f"Чат: {escape(_message_chat_title(message))}\n"
                f"Message ID: {message.message_id}",
            )
            logging.info(
                "EDITED_BUSINESS_MESSAGE received business_connection_id=%s chat_id=%s message_id=%s old_found=False changed=True",
                business_connection_id,
                message.chat.id,
                message.message_id,
            )
            return

        old_text = old_message.get("text") if old_message else None
        old_caption = old_message.get("caption") if old_message else None
        new_text = message.text
        new_caption = message.caption

        if old_text != new_text and (old_text is not None or new_text is not None):
            save_business_message_version(
                business_connection_id=business_connection_id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                user_id=user_id,
                old_text=old_text,
                new_text=new_text,
                edited_at=_message_created_at(message),
            )
            update_business_message_content(
                business_connection_id,
                message.chat.id,
                message.message_id,
                text=new_text,
                caption=old_caption,
            )
            changed = True
            await safe_notify_connection_owner(
                bot,
                business_connection_id,
                _format_edit_notice(
                    title="Business-сообщение изменено",
                    business_connection_id=business_connection_id,
                    message=message,
                    old_value=old_text,
                    new_value=new_text,
                ),
            )

        media_caption_old = old_media[0].get("caption") if old_media else old_caption
        if media_caption_old != new_caption and (media_caption_old is not None or new_caption is not None):
            save_business_message_version(
                business_connection_id=business_connection_id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                user_id=user_id,
                old_text=media_caption_old,
                new_text=new_caption,
                edited_at=_message_created_at(message),
            )
            update_business_media_caption(business_connection_id, message.chat.id, message.message_id, new_caption)
            update_business_message_content(
                business_connection_id,
                message.chat.id,
                message.message_id,
                text=new_text if new_text is not None else old_text,
                caption=new_caption,
            )
            changed = True
            await safe_notify_connection_owner(
                bot,
                business_connection_id,
                _format_edit_notice(
                    title="Подпись business-медиа изменена",
                    business_connection_id=business_connection_id,
                    message=message,
                    old_value=media_caption_old,
                    new_value=new_caption,
                ),
            )

        logging.info(
            "EDITED_BUSINESS_MESSAGE received business_connection_id=%s chat_id=%s message_id=%s old_found=%s changed=%s",
            business_connection_id,
            message.chat.id,
            message.message_id,
            old_found,
            changed,
        )
    except Exception:
        logging.exception("Unexpected error in edited_business_message handler")


async def handle_deleted_business_messages(event: BusinessMessagesDeleted, bot: Bot) -> None:
    try:
        business_connection_id = event.business_connection_id
        if not await _ensure_subscription_access(bot, business_connection_id):
            return

        message_ids = list(event.message_ids)
        rows = resolve_deleted_business_messages(
            business_connection_id=business_connection_id,
            chat_id=event.chat.id,
            message_ids=message_ids,
        )
        for _ in range(3):
            if not any(not row["found_any"] for row in rows):
                break
            await asyncio.sleep(0.35)
            rows = resolve_deleted_business_messages(
                business_connection_id=business_connection_id,
                chat_id=event.chat.id,
                message_ids=message_ids,
            )

        found_text = sum(1 for row in rows if row["found_text"])
        found_media = sum(len(row["media"]) for row in rows)
        missing = sum(1 for row in rows if not row["found_any"])
        logging.info(
            "DELETED_BUSINESS_MESSAGES received business_connection_id=%s chat_id=%s message_ids=%s found_text=%s found_media=%s missing=%s",
            business_connection_id,
            event.chat.id,
            message_ids,
            found_text,
            found_media,
            missing,
        )

        for row in rows:
            if row["found_text"] or not row["found_media"]:
                await safe_notify_connection_owner(bot, business_connection_id, _format_deleted_text(event, row))

            for media in row["media"]:
                caption = _format_deleted_media(event, media)
                if media.get("media_type") == "photo":
                    await safe_notify_connection_owner_photo(
                        bot,
                        business_connection_id,
                        media.get("local_path"),
                        caption=caption,
                    )
                elif media.get("media_type") == "video":
                    await safe_notify_connection_owner_video(
                        bot,
                        business_connection_id,
                        media.get("local_path"),
                        caption=caption,
                    )
                elif media.get("media_type") == "video_note":
                    await safe_notify_connection_owner_video_note(
                        bot,
                        business_connection_id,
                        media.get("local_path"),
                        caption=caption,
                    )
                elif media.get("media_type") == "voice":
                    await safe_notify_connection_owner_voice(
                        bot,
                        business_connection_id,
                        media.get("local_path"),
                        caption=caption,
                    )
                elif media.get("media_type") == "audio":
                    await safe_notify_connection_owner_audio(
                        bot,
                        business_connection_id,
                        media.get("local_path"),
                        caption=caption,
                    )
                else:
                    await safe_notify_connection_owner_document(
                        bot,
                        business_connection_id,
                        media.get("local_path"),
                        caption=caption,
                        missing_text="Медиа было удалено, но файл не найден в storage.",
                    )
    except Exception:
        logging.exception("Unexpected error in deleted_business_messages handler")


async def handle_unexpected_error(event: ErrorEvent) -> bool:
    logging.error("Unexpected dispatcher error: %s", event.exception, exc_info=event.exception)
    return True


def _group_media_by_owner(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        owner_user_id = row.get("owner_user_id")
        if owner_user_id is None:
            continue
        grouped[int(owner_user_id)].append(row)
    return grouped


def _media_disk_size(row: dict[str, Any]) -> int:
    local_path = row.get("local_path")
    if local_path:
        path = Path(local_path)
        if path.exists() and path.is_file():
            try:
                return path.stat().st_size
            except OSError:
                pass
    return int(row.get("file_size") or 0)


def _quota_delete_plan(items: list[dict[str, Any]], limit_bytes: int) -> list[dict[str, Any]]:
    total = sum(_media_disk_size(item) for item in items)
    if total <= limit_bytes:
        return []

    plan: list[dict[str, Any]] = []
    remaining = total
    for item in sorted(items, key=lambda row: (_media_disk_size(row), row.get("created_at") or "", row.get("id") or 0)):
        plan.append(item)
        remaining -= _media_disk_size(item)
        if remaining <= limit_bytes:
            break
    return plan


async def warn_media_quota(bot: Bot) -> None:
    limit_bytes = MEDIA_USER_LIMIT_MB * 1024 * 1024
    delete_after = (datetime.now(timezone.utc) + timedelta(hours=MEDIA_QUOTA_GRACE_HOURS)).isoformat()
    warned_ids: list[int] = []

    for owner_user_id, items in _group_media_by_owner(get_active_media_with_owners()).items():
        plan = [item for item in _quota_delete_plan(items, limit_bytes) if not item.get("quota_warned_at")]
        if not plan:
            continue

        total_mb = sum(_media_disk_size(item) for item in items) / 1024 / 1024
        planned_mb = sum(_media_disk_size(item) for item in plan) / 1024 / 1024
        try:
            await bot.send_message(
                chat_id=owner_user_id,
                text=(
                    "⚠️ <b>Превышен лимит медиа</b>\n\n"
                    f"Ваши сохранённые файлы занимают примерно {total_mb:.1f} МБ.\n"
                    f"Лимит: {MEDIA_USER_LIMIT_MB} МБ.\n\n"
                    f"Через {MEDIA_QUOTA_GRACE_HOURS} ч бот удалит самые маленькие файлы на {planned_mb:.1f} МБ "
                    "и остановится, как только размер снова станет ниже лимита.\n\n"
                    "Если что-то важно, сохраните эти файлы в Избранное."
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Удалить сейчас", callback_data="quota:delete_now")]
                    ]
                ),
            )
            warned_ids.extend(int(item["id"]) for item in plan if item.get("id") is not None)
        except Exception:
            logging.exception("Failed to send media quota warning owner=%s", owner_user_id)

    mark_media_quota_warned(warned_ids, delete_after)


async def cleanup_media_quota_files(owner_filter: int | None = None, force: bool = False) -> int:
    limit_bytes = MEDIA_USER_LIMIT_MB * 1024 * 1024
    now = datetime.now(timezone.utc)
    cleaned_ids: list[int] = []

    for owner_user_id, items in _group_media_by_owner(get_active_media_with_owners()).items():
        if owner_filter is not None and owner_user_id != owner_filter:
            continue

        total = sum(_media_disk_size(item) for item in items)
        if total <= limit_bytes:
            continue

        if force:
            candidates = _quota_delete_plan(items, limit_bytes)
        else:
            candidates = sorted(items, key=lambda row: (_media_disk_size(row), row.get("created_at") or "", row.get("id") or 0))

        for item in candidates:
            if total <= limit_bytes:
                break

            if not force:
                delete_after = item.get("quota_delete_after")
                try:
                    delete_after_dt = datetime.fromisoformat(delete_after) if delete_after else None
                except ValueError:
                    delete_after_dt = None
                if not delete_after_dt or delete_after_dt.astimezone(timezone.utc) > now:
                    continue

            media_id = item.get("id")
            local_path = item.get("local_path")
            if media_id is None or not local_path:
                continue

            size = _media_disk_size(item)
            try:
                path = Path(local_path)
                if path.exists() and path.is_file():
                    path.unlink()
                cleaned_ids.append(int(media_id))
                total -= size
            except Exception:
                logging.exception("Failed to cleanup quota media file owner=%s path=%s", owner_user_id, local_path)

    mark_media_cleaned(cleaned_ids)
    if cleaned_ids:
        logging.info("MEDIA_QUOTA_CLEANUP deleted_files=%s limit_mb=%s", len(cleaned_ids), MEDIA_USER_LIMIT_MB)
    return len(cleaned_ids)


async def warn_media_cleanup(bot: Bot) -> None:
    rows = get_media_cleanup_warning_candidates(MEDIA_RETENTION_DAYS)
    if not rows:
        return

    warned_ids: list[int] = []
    for owner_user_id, items in _group_media_by_owner(rows).items():
        count = len(items)
        try:
            await bot.send_message(
                chat_id=owner_user_id,
                text=(
                    "⚠️ <b>Скоро очистка медиа</b>\n\n"
                    f"Через 24 часа бот удалит из storage файлы старше {MEDIA_RETENTION_DAYS} дней.\n"
                    f"К удалению готовится файлов: {count}.\n\n"
                    "Если что-то важно, сохраните нужные фото, видео, кружки или голосовые в Избранное."
                ),
                parse_mode=ParseMode.HTML,
            )
            warned_ids.extend(int(item["id"]) for item in items if item.get("id") is not None)
        except Exception:
            logging.exception("Failed to send media cleanup warning owner=%s", owner_user_id)

    mark_media_cleanup_warned(warned_ids)


async def cleanup_old_media_files() -> None:
    rows = get_media_cleanup_delete_candidates(MEDIA_RETENTION_DAYS)
    if not rows:
        return

    cleaned_ids: list[int] = []
    for row in rows:
        media_id = row.get("id")
        local_path = row.get("local_path")
        if media_id is None or not local_path:
            continue

        try:
            path = Path(local_path)
            if path.exists() and path.is_file():
                path.unlink()
            cleaned_ids.append(int(media_id))
        except Exception:
            logging.exception("Failed to cleanup media file path=%s", local_path)

    mark_media_cleaned(cleaned_ids)
    if cleaned_ids:
        logging.info("MEDIA_CLEANUP deleted_files=%s retention_days=%s", len(cleaned_ids), MEDIA_RETENTION_DAYS)


async def media_cleanup_loop(bot: Bot) -> None:
    if not MEDIA_CLEANUP_ENABLED:
        logging.info("MEDIA_CLEANUP disabled")
        return

    while True:
        try:
            await warn_media_quota(bot)
            await cleanup_media_quota_files()
            await warn_media_cleanup(bot)
            await cleanup_old_media_files()
        except Exception:
            logging.exception("Unexpected media cleanup loop error")
        await asyncio.sleep(6 * 60 * 60)


async def setup_bot_profile(bot: Bot) -> None:
    try:
        await bot.set_my_short_description(short_description=BOT_SHORT_DESCRIPTION)
        await bot.set_my_description(description=BOT_DESCRIPTION)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Главное меню"),
                BotCommand(command="help", description="Функции бота"),
                BotCommand(command="dialogs", description="Список сохранённых диалогов"),
                BotCommand(command="export", description="Экспорт диалога"),
                BotCommand(command="translation", description="Команды перевода"),
                BotCommand(command="stats", description="Статистика"),
            ]
        )
        logging.info("Bot profile updated")
    except Exception:
        logging.exception("Failed to update bot profile")


async def main() -> None:
    setup_logging()
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN is not set. Add BOT_TOKEN to .env before starting polling.")
        return

    if not SUPER_ADMIN_IDS:
        logging.warning("SUPER_ADMIN_IDS is empty. /debug super admin access is disabled.")

    _ensure_storage_dirs()
    init_db()

    bot = Bot(token=BOT_TOKEN)
    await setup_bot_profile(bot)
    asyncio.create_task(media_cleanup_loop(bot))
    dispatcher = Dispatcher()

    dispatcher.message.register(handle_start, Command("start"))
    dispatcher.message.register(handle_help, Command("help"))
    dispatcher.message.register(handle_ping, Command("ping"))
    dispatcher.message.register(handle_debug, Command("debug"))
    dispatcher.message.register(handle_stats, Command("stats"))
    dispatcher.message.register(handle_dialogs, Command("dialogs"))
    dispatcher.message.register(handle_export, Command("export"))
    dispatcher.message.register(handle_export_help, Command("export_help"))
    dispatcher.message.register(handle_translation_settings, Command("translation", "translate_settings"))
    dispatcher.callback_query.register(handle_export_format, F.data.startswith("export:"))
    dispatcher.callback_query.register(handle_menu_callback, F.data.startswith("menu:"))
    dispatcher.callback_query.register(handle_pro_plan_callback, F.data.startswith("pro:plan:"))
    dispatcher.callback_query.register(handle_quota_delete_now, F.data == "quota:delete_now")
    dispatcher.message.register(handle_private_text, F.chat.type == PRIVATE_CHAT)
    dispatcher.message.register(handle_group_message, F.chat.type.in_(GROUP_CHATS))
    dispatcher.edited_message.register(handle_edited_message, F.chat.type.in_(GROUP_CHATS))
    dispatcher.business_connection.register(handle_business_connection)
    dispatcher.business_message.register(handle_business_message)
    dispatcher.edited_business_message.register(handle_edited_business_message)
    dispatcher.deleted_business_messages.register(handle_deleted_business_messages)
    dispatcher.errors.register(handle_unexpected_error)

    logging.info("Bot started")
    logging.info("Allowed updates: %s", ALLOWED_UPDATES)
    logging.info("SUPER_ADMIN_IDS count: %s", len(SUPER_ADMIN_IDS))
    logging.info("MAX_DOWNLOAD_MB: %s", MAX_DOWNLOAD_MB)
    logging.info("MEDIA_RETENTION_DAYS: %s", MEDIA_RETENTION_DAYS)
    logging.info("MEDIA_CLEANUP_ENABLED: %s", MEDIA_CLEANUP_ENABLED)
    logging.info("MEDIA_USER_LIMIT_MB: %s", MEDIA_USER_LIMIT_MB)
    logging.info("MEDIA_QUOTA_GRACE_HOURS: %s", MEDIA_QUOTA_GRACE_HOURS)
    await dispatcher.start_polling(bot, allowed_updates=ALLOWED_UPDATES)


if __name__ == "__main__":
    asyncio.run(main())
