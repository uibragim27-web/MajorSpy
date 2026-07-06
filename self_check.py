from pathlib import Path
import asyncio
import json
import sqlite3
import tempfile
import zipfile

import config
import database
import main


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_owner_parsing() -> None:
    parsed = config._parse_int_list("123,456,123,, 789 ", "OWNER_ID")
    _assert(parsed == [123, 456, 789], "OWNER_ID parsing failed")


def check_database() -> None:
    old_db_path = database.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        database.DB_PATH = Path(tmp_dir) / "messages.db"
        database.init_db()

        database.save_business_connection(
            business_connection_id="bc1",
            owner_user_id=1,
            owner_username="owner",
            owner_full_name="Owner",
            is_enabled=True,
            can_reply=True,
        )
        database.save_business_message(
            business_connection_id="bc1",
            chat_id=10,
            chat_title="Chat",
            message_id=20,
            user_id=30,
            username="user",
            full_name="User",
            text="old",
            caption=None,
            created_at="2026-05-25T00:00:00+00:00",
        )
        message = database.get_business_message("bc1", 10, 20)
        _assert(message is not None, "business_message was not saved")
        _assert(message["text"] == "old", "business_message text mismatch")
        owner = database.get_connection_owner("bc1")
        _assert(owner is not None and owner["owner_user_id"] == 1, "business_connection owner mismatch")

        database.save_business_message_version(
            business_connection_id="bc1",
            chat_id=10,
            message_id=20,
            user_id=30,
            old_text="old",
            new_text="new",
        )
        database.update_business_message_content("bc1", 10, 20, text="new", caption=None)
        message = database.get_business_message("bc1", 10, 20)
        _assert(message["is_edited"] == 1, "business_message edit flag was not updated")

        database.save_business_media(
            business_connection_id="bc1",
            chat_id=10,
            chat_title="Chat",
            message_id=21,
            user_id=30,
            username="user",
            full_name="User",
            media_type="photo",
            file_id="file",
            file_unique_id="unique",
            file_name=None,
            mime_type="image/jpeg",
            file_size=100,
            duration=None,
            caption="caption",
            local_path="storage/photos/test.jpg",
        )
        media = database.get_business_media("bc1", 10, 21)
        _assert(len(media) == 1, "business_media was not saved")
        _assert(media[0]["caption"] == "caption", "business_media caption mismatch")

        deleted = database.resolve_deleted_business_messages(
            business_connection_id="bc1",
            chat_id=10,
            message_ids=[20, 21, 999],
        )
        _assert(deleted[0]["found_text"], "known text deletion was not resolved")
        _assert(deleted[1]["found_media"], "known media deletion was not resolved")
        _assert(not deleted[2]["found_any"], "unknown deletion should be missing")

        media = database.get_business_media("bc1", 10, 21)[0]
        _assert(media["is_deleted"] == 1, "business_media deleted flag was not updated")

        stats = database.get_stats()
        _assert(stats["business_messages"] >= 2, "business_messages stats failed")
        _assert(stats["business_media"] == 1, "business_media stats failed")
        _assert(stats["business_deleted_messages"] >= 3, "deleted stats failed")

    database.DB_PATH = old_db_path


def check_migrations() -> None:
    old_db_path = database.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        database.DB_PATH = Path(tmp_dir) / "old.db"
        with sqlite3.connect(database.DB_PATH) as conn:
            conn.execute(
                """
                CREATE TABLE business_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_connection_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    text TEXT,
                    UNIQUE(business_connection_id, chat_id, message_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE business_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_connection_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    media_type TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE business_deleted_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    business_connection_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL
                )
                """
            )
            conn.commit()

        database.init_db()
        with database.connect() as conn:
            connection_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(business_connections)").fetchall()
            }
            message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(business_messages)").fetchall()}
            media_cols = {row["name"] for row in conn.execute("PRAGMA table_info(business_media)").fetchall()}
            deleted_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(business_deleted_messages)").fetchall()
            }

        _assert("business_connection_id" in connection_cols, "business_connections migration missed id")
        _assert("owner_user_id" in connection_cols, "business_connections migration missed owner_user_id")
        _assert("caption" in message_cols, "business_messages migration missed caption")
        _assert("is_unavailable" in media_cols, "business_media migration missed is_unavailable")
        _assert("media_id" in deleted_cols, "business_deleted_messages migration missed media_id")

    database.DB_PATH = old_db_path


def check_safe_functions() -> None:
    for name in (
        "safe_notify_connection_owner",
        "safe_notify_connection_owner_photo",
        "safe_notify_connection_owner_video",
        "safe_notify_connection_owner_document",
    ):
        _assert(hasattr(main, name), name + " is missing")


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, *, chat_id: int, text: str, parse_mode: str | None = None) -> None:
        self.messages.append((chat_id, text))


def check_privacy_isolation() -> None:
    old_db_path = database.DB_PATH
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        database.DB_PATH = Path(tmp_dir) / "messages.db"
        database.init_db()
        database.save_business_connection(
            business_connection_id="conn_a",
            owner_user_id=111,
            owner_username="owner_a",
            owner_full_name="Owner A",
            is_enabled=True,
            can_reply=True,
        )
        database.save_business_connection(
            business_connection_id="conn_b",
            owner_user_id=222,
            owner_username="owner_b",
            owner_full_name="Owner B",
            is_enabled=True,
            can_reply=True,
        )
        database.save_business_message(
            business_connection_id="conn_a",
            chat_id=10,
            chat_title="Chat A",
            message_id=1,
            user_id=1001,
            username="user_a",
            full_name="User A",
            text="a",
        )
        database.save_business_message(
            business_connection_id="conn_b",
            chat_id=20,
            chat_title="Chat B",
            message_id=2,
            user_id=1002,
            username="user_b",
            full_name="User B",
            text="b",
        )

        owner_a = database.get_connection_owner("conn_a")
        owner_b = database.get_connection_owner("conn_b")
        _assert(owner_a is not None and owner_a["owner_user_id"] == 111, "conn_a owner must be 111")
        _assert(owner_b is not None and owner_b["owner_user_id"] == 222, "conn_b owner must be 222")

        bot = FakeBot()
        asyncio.run(main.safe_notify_connection_owner(bot, "conn_a", "event a"))
        asyncio.run(main.safe_notify_connection_owner(bot, "conn_b", "event b"))
        _assert(bot.messages == [(111, "event a"), (222, "event b")], "connection notifications leaked owners")
        _assert((222, "event a") not in bot.messages, "conn_a notification leaked to owner 222")
        _assert((111, "event b") not in bot.messages, "conn_b notification leaked to owner 111")

    database.DB_PATH = old_db_path


def check_export_privacy_and_file() -> None:
    old_db_path = database.DB_PATH
    old_exports_dir = main.EXPORTS_DIR
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        tmp_path = Path(tmp_dir)
        database.DB_PATH = tmp_path / "messages.db"
        main.EXPORTS_DIR = tmp_path / "exports"
        photo_path = tmp_path / "storage" / "photos" / "source.jpg"
        video_path = tmp_path / "storage" / "videos" / "source.mp4"
        photo_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.parent.mkdir(parents=True, exist_ok=True)
        photo_path.write_bytes(b"fake-jpg")
        video_path.write_bytes(b"fake-mp4")
        database.init_db()
        database.save_business_connection(
            business_connection_id="conn_a",
            owner_user_id=111,
            owner_username="owner_a",
            owner_full_name="Owner A",
            is_enabled=True,
            can_reply=True,
        )
        database.save_business_connection(
            business_connection_id="conn_b",
            owner_user_id=222,
            owner_username="owner_b",
            owner_full_name="Owner B",
            is_enabled=True,
            can_reply=True,
        )
        database.save_business_message(
            business_connection_id="conn_a",
            chat_id=10,
            chat_title='Dialog <A> & "unsafe"',
            message_id=1,
            user_id=1001,
            username="ivan<script>",
            full_name="Ivan <User>",
            text="hello <b>export</b> & check",
            caption=None,
            created_at="2026-05-25T21:54:18+00:00",
        )
        database.save_business_message(
            business_connection_id="conn_a",
            chat_id=10,
            chat_title='Dialog <A> & "unsafe"',
            message_id=2,
            user_id=1001,
            username="ivan",
            full_name="Ivan",
            text="old text",
            caption=None,
            created_at="2026-05-25T21:55:18+00:00",
        )
        database.save_business_message_version(
            business_connection_id="conn_a",
            chat_id=10,
            message_id=2,
            user_id=1001,
            old_text="old text",
            new_text="new text",
            edited_at="2026-05-25T21:56:18+00:00",
        )
        database.update_business_message_content("conn_a", 10, 2, text="new text", caption=None)
        database.save_business_media(
            business_connection_id="conn_a",
            chat_id=10,
            chat_title='Dialog <A> & "unsafe"',
            message_id=3,
            user_id=1001,
            username="ivan",
            full_name="Ivan",
            media_type="photo",
            file_id="file_a",
            file_unique_id="unique_a",
            file_name=None,
            mime_type="image/jpeg",
            file_size=123,
            duration=None,
            caption="photo <caption> &",
            local_path=str(photo_path),
            created_at="2026-05-25T21:57:18+00:00",
        )
        database.save_business_media(
            business_connection_id="conn_a",
            chat_id=10,
            chat_title='Dialog <A> & "unsafe"',
            message_id=4,
            user_id=1001,
            username="ivan",
            full_name="Ivan",
            media_type="video",
            file_id="file_v",
            file_unique_id="unique_v",
            file_name="clip.mp4",
            mime_type="video/mp4",
            file_size=456,
            duration=1,
            caption="video caption",
            local_path=str(video_path),
            created_at="2026-05-25T21:58:18+00:00",
        )
        database.save_business_media(
            business_connection_id="conn_a",
            chat_id=10,
            chat_title='Dialog <A> & "unsafe"',
            message_id=5,
            user_id=1001,
            username="ivan",
            full_name="Ivan",
            media_type="document",
            file_id="file_d",
            file_unique_id="unique_d",
            file_name='bad\\/:*?"<>|name.txt',
            mime_type="text/plain",
            file_size=10,
            duration=None,
            caption="missing document",
            local_path=str(tmp_path / "storage" / "files" / "missing.txt"),
            created_at="2026-05-25T21:59:18+00:00",
        )
        database.save_business_message(
            business_connection_id="conn_a",
            chat_id=10,
            chat_title=None,
            message_id=6,
            user_id=None,
            username=None,
            full_name=None,
            text=None,
            caption=None,
            created_at=None,
        )
        database.resolve_deleted_business_messages(
            business_connection_id="conn_a",
            chat_id=10,
            message_ids=[1],
        )
        database.save_business_message(
            business_connection_id="conn_b",
            chat_id=20,
            chat_title="Dialog B",
            message_id=1,
            user_id=2001,
            username="petr",
            full_name="Petr",
            text="secret b",
            caption=None,
            created_at="2026-05-25T22:00:00+00:00",
        )

        _assert(database.get_owner_connections(111), "owner A connection not found")
        _assert(database.find_dialog_for_owner(111, "10") is not None, "owner A dialog not found")
        _assert(database.find_dialog_for_owner(111, "@ivan") is not None, "owner A username lookup failed")
        _assert(database.find_dialog_for_owner(111, "20") is None, "owner A can see owner B dialog")
        _assert(database.find_dialog_for_owner(222, "10") is None, "owner B can see owner A dialog")
        _assert(database.export_dialog_data("conn_a", "20", 5000) is None, "conn_a exported dialog B")
        _assert(database.export_dialog_data("conn_b", "10", 5000) is None, "conn_b exported dialog A")

        dialogs = database.get_dialogs_for_owner(111)
        _assert(len(dialogs) == 1 and dialogs[0]["chat_id"] == 10, "dialogs list leaked or missed dialog")

        data = database.export_dialog_data("conn_a", "10", 5000)
        _assert(data is not None, "export data missing")
        export_path = main.create_export_file(111, data)
        _assert(export_path.parent.name == "exports", "export file was not created in exports/")
        _assert(export_path.suffix == ".zip" and export_path.exists(), "ZIP export was not created")

        with zipfile.ZipFile(export_path) as archive:
            names = set(archive.namelist())
            _assert("chat_10/messages.html" in names, "messages.html missing from ZIP")
            _assert("chat_10/messages.json" in names, "messages.json missing from ZIP")
            _assert("chat_10/style.css" in names, "style.css missing from ZIP")
            _assert("chat_10/photos/photo_3.jpg" in names, "photo was not copied to photos/")
            _assert("chat_10/video_files/video_4.mp4" in names, "video was not copied to video_files/")
            html_text = archive.read("chat_10/messages.html").decode("utf-8")
            json_text = archive.read("chat_10/messages.json").decode("utf-8")

        _assert('href="photos/photo_3.jpg"' in html_text, "photo relative link missing")
        _assert('src="photos/photo_3.jpg"' in html_text, "photo relative src missing")
        _assert('<video controls src="video_files/video_4.mp4"></video>' in html_text, "video relative src missing")
        _assert("[файл не найден локально]" in html_text, "missing media notice missing")
        _assert("hello &lt;b&gt;export&lt;/b&gt; &amp; check" in html_text, "message HTML escaping failed")
        _assert("Dialog &lt;A&gt; &amp; &quot;unsafe&quot;" in html_text, "chat title HTML escaping failed")
        _assert("Ivan &lt;User&gt; (@ivan&lt;script&gt;)" in html_text, "user HTML escaping failed")
        _assert("photo &lt;caption&gt; &amp;" in html_text, "caption HTML escaping failed")
        _assert("[УДАЛЕНО]" in html_text and "Удалено:" in html_text, "deleted message export missing")
        _assert("[ИЗМЕНЕНО]" in html_text and "old text" in html_text and "new text" in html_text, "edited export missing")
        export_json = json.loads(json_text)
        _assert(export_json["chat_id"] == 10, "messages.json chat_id mismatch")
        _assert("messages" in export_json and "media" in export_json, "messages.json missing data")
        _assert("edits" in export_json and "deletions" in export_json, "messages.json missing edit/delete data")

    database.DB_PATH = old_db_path
    main.EXPORTS_DIR = old_exports_dir


def main_check() -> None:
    check_owner_parsing()
    check_database()
    check_migrations()
    check_safe_functions()
    check_privacy_isolation()
    check_export_privacy_and_file()
    print("SELF CHECK OK")


if __name__ == "__main__":
    main_check()
