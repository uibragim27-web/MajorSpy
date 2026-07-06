# Telegram Business / Secretary Bot MVP

Python MVP для контроля business-сообщений через Telegram Business / Secretary Bot API. Бот сохраняет сообщения, медиа, изменения и удаления, но не отвечает собеседникам и не пишет в business-чаты.

Business-уведомления отправляются только владельцу конкретного `business_connection_id`.

## Возможности

- `business_message`: сохраняет `text`, `caption`, `photo`, `video`, `document`, `animation`, `voice`, `video_note`.
- `edited_business_message`: отслеживает изменение текста и подписи.
- `deleted_business_messages`: фиксирует удаления и показывает владельцам сохранённый текст или заранее скачанное фото/видео.
- `/dialogs` и `/export`: показывают доступные владельцу диалоги и экспортируют конкретный диалог только из локальной SQLite.
- Обычные фото скачиваются в `storage/photos/`.
- Обычные видео скачиваются в `storage/videos/`.
- Большие файлы не скачиваются, если размер больше `MAX_DOWNLOAD_MB`.
- Одноразовые, исчезающие, protected или недоступные media не обходятся и не сохраняются как файл.

## Безопасность

Бот не использует автоответы в business-чатах. В handlers `business_message`, `edited_business_message`, `deleted_business_messages` нельзя отправлять сообщения в исходный чат.

Super admins для `/debug` и общей диагностики задаются так:

```env
SUPER_ADMIN_IDS=123456789,987654321
```

Пустые значения игнорируются, дубликаты удаляются. Если `SUPER_ADMIN_IDS` пустой, временно читается старый `OWNER_ID`, но только как список super admins, не как получатели business-событий.

## Установка

```powershell
cd C:\Users\1\Downloads\pudch1
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Настройка

Скопируйте пример:

```powershell
copy .env.example .env
```

Заполните `.env`:

```env
BOT_TOKEN=токен_от_BotFather
SUPER_ADMIN_IDS=123456789,987654321
ALLOWED_USER_IDS=
MAX_DOWNLOAD_MB=20
MAX_EXPORT_MESSAGES=5000
DEBUG_EXPORT=false
```

`ALLOWED_USER_IDS` можно оставить пустым. Если заполнить, бот будет сохранять business-события только от этих пользователей.
`MAX_EXPORT_MESSAGES` ограничивает экспорт последними N сообщениями диалога. `DEBUG_EXPORT` оставлен только для совместимости конфигурации; полнотекстовый экспорт через super admin запрещён.

## Запуск

```powershell
.\.venv\Scripts\activate
python main.py
```

При запуске создаются:

- `messages.db`
- `storage/photos/`
- `storage/videos/`
- `storage/files/`
- `exports/`

## Подключение Secretary Mode

1. Создайте бота через `@BotFather`.
2. Вставьте `BOT_TOKEN` в `.env`.
3. Запустите `python main.py`.
4. В Telegram откройте `Settings`.
5. Перейдите в `Chat Automation`.
6. Подключите своего бота.
7. Проверьте тестовый business-чат.

## Команды

Команды работают только в личном чате. В группах и business-чатах бот молчит.

- `/ping` — показывает `user_id` и статус `connected owner ok`, `super admin ok` или `not connected`.
- `/debug` — только для `SUPER_ADMIN_IDS`, показывает конфиг и счётчики без текстов сообщений.
- `/stats` — для connected owner показывает статистику только его `business_connection_id`, для super admin общие счётчики без текстов сообщений.
- `/dialogs` — только для connected owner, показывает `chat_id | username | full_name | messages_count | last_message_at` без текстов сообщений.
- `/export <chat_id или username или user_id>` — только в личке; для connected owner экспортирует конкретный диалог только из его `business_connection_id`.
- `/export_help` — показывает краткую инструкцию по `/dialogs` и `/export`.
- `/start` — показывает Telegram ID и статус подключения.

Business-события изолированы по `business_connection_id`: один подключивший пользователь не получает события другого подключившего пользователя.

### Экспорт диалога

Примеры:

```text
/export 7250056867
/export @ivan
/export 123456789
```

Экспорт создаёт переносимый ZIP-архив:

```text
exports/export_<owner_id>_<chat_id>_<date>.zip
```

Структура архива:

```text
export_<owner_id>_<chat_id>_<date>.zip
└── chat_<chat_id>/
    ├── messages.html
    ├── messages.json
    ├── photos/
    ├── video_files/
    ├── files/
    └── style.css
```

`messages.html` похож на Telegram export: заголовок диалога, дата экспорта, сообщения по времени, текст, удаления, изменения, фото, видео и документы. Фото и видео открываются из папок внутри архива по относительным ссылкам. `messages.json` содержит `chat_id`, `exported_at`, `messages`, `media`, `edits`, `deletions`.

Если фото, видео или документ есть в `storage/`, файл копируется внутрь ZIP:

- `photo` → `chat_<chat_id>/photos/photo_<message_id>.jpg`
- `video` → `chat_<chat_id>/video_files/video_<message_id>.mp4`
- `document` → `chat_<chat_id>/files/file_<message_id>_<clean_name>`

Если локального файла нет, экспорт не падает, а в HTML пишет `[файл не найден локально]`.

Как открыть экспорт:

1. Скачать ZIP.
2. Распаковать архив.
3. Открыть `chat_<chat_id>/messages.html` в браузере.
4. Фото и видео будут открываться из папок внутри распакованного архива.

На телефоне: скачайте ZIP из Telegram, распакуйте его приложением “Файлы” или любым архиватором и откройте `messages.html` в браузере.
На компьютере: распакуйте ZIP штатным архиватором Windows/macOS/Linux и откройте `messages.html` двойным кликом.

Ограничения экспорта:

- Бот экспортирует только строки, которые уже есть в SQLite. Он не вызывает Telegram API для получения старой истории и не может выгрузить сообщения, которые не видел раньше.
- Connected owner видит только собственные подключения и собственные `business_connection_id`.
- `SUPER_ADMIN_IDS` получают только диагностику без чужих текстов. Полнотекстовый экспорт доступен только connected owner конкретного `business_connection_id`.
- Если в диалоге больше `MAX_EXPORT_MESSAGES`, экспортируются последние `MAX_EXPORT_MESSAGES` сообщений.
- Если ZIP больше 45 MB, бот не отправляет файл в Telegram и пишет владельцу локальный путь.

Чтобы проверить, что данные владельцев не смешиваются: создайте два business-подключения, отправьте сообщения в разные диалоги, выполните `/dialogs` и `/export` от каждого владельца. Владелец A не должен видеть `chat_id` владельца B, а попытка `/export <chat_id B>` должна вернуть “Диалог не найден среди ваших подключений”. То же проверяет `python self_check.py`.

## Проверка

Локальная самопроверка:

```powershell
python -m py_compile main.py database.py config.py self_check.py
python self_check.py
```

Успешный результат:

```text
SELF CHECK OK
```

## Тестирование редактирования

1. Напишите текст в business-чат.
2. Отредактируйте текст.
3. Владелец должен получить уведомление со старой и новой версией.
4. Для фото/видео измените подпись.
5. Владелец должен получить уведомление об изменении подписи.

## Тестирование удаления

1. Напишите текст в business-чат.
2. Удалите сообщение.
3. Если бот успел сохранить сообщение, владелец увидит удалённый текст.
4. Отправьте обычное фото или видео.
5. Удалите его.
6. Если файл был заранее скачан в `storage/`, владелец получит уведомление и сам файл.

Удалённый текст или файл доступны только если бот получил и сохранил сообщение до удаления.

## Ограничения

- Бот работает только через Telegram Bot API и Business / Secretary Bot подключение.
- Userbot, вход в аккаунт и обход ограничений не используются.
- Одноразовые, disappearing, self-destruct, protected и недоступные media не сохраняются.
- Если Telegram не дал скачать файл, бот логирует событие и уведомляет владельца конкретного подключения.
- Если `SUPER_ADMIN_IDS` пустой или владельцу подключения нельзя написать, polling не должен падать.
- ИИ, CRM, веб-панель, оплата и автоответы не добавлены.
