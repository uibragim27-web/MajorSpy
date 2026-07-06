# Новый бот для удалённых сообщений

Это чистая копия `C:\Users\1\Downloads\pudch1`.

В копию не перенесены:

- старый `.env` со старым токеном;
- старая база `messages.db`;
- папка `.venv`;
- `__pycache__`;
- сохранённые медиа из `storage`;
- старые экспорты из `exports`.

## Что заполнить

Откройте `.env` и укажите:

```env
BOT_TOKEN=новый_токен_от_BotFather
SUPER_ADMIN_IDS=ваш_telegram_id
ALLOWED_USER_IDS=
MAX_DOWNLOAD_MB=20
MAX_EXPORT_MESSAGES=5000
DEBUG_EXPORT=false
```

`SUPER_ADMIN_IDS` - ваш Telegram ID. Его можно узнать, написав новому боту `/start` или `/ping` после запуска.

## Как создать нового бота

1. В Telegram откройте `@BotFather`.
2. Отправьте `/newbot`.
3. Задайте новое имя бота.
4. Задайте новый username, он должен заканчиваться на `bot`.
5. Скопируйте токен в `BOT_TOKEN` внутри `.env`.
6. Запустите `run.bat`.
7. В Telegram подключите этого бота в `Settings` -> `Chat Automation`.

После первого запуска новая база `messages.db`, папки `storage/photos`, `storage/videos`, `storage/files` и `exports` создадутся заново.
