# Railway Deploy

## GitHub

Push only source files. Do not upload `.env`, `.venv`, `messages.db`, `storage`, `logs`, or `exports`.

## Railway variables

Set these variables in Railway:

```env
BOT_TOKEN=your_bot_token
OWNER_ID=your_telegram_id
SUPER_ADMIN_IDS=your_telegram_id
ALLOWED_USER_IDS=
DATA_DIR=/data
MAX_DOWNLOAD_MB=50
MAX_EXPORT_MESSAGES=5000
MEDIA_RETENTION_DAYS=7
MEDIA_CLEANUP_ENABLED=true
MEDIA_USER_LIMIT_MB=500
MEDIA_QUOTA_GRACE_HOURS=24
DEBUG_EXPORT=false
```

## Storage

For persistent SQLite and media storage, add a Railway Volume and mount it to:

```text
/data
```

Then keep `DATA_DIR=/data`.

## Serverless

For this bot, keep Serverless disabled. The bot uses long polling and should run continuously.
