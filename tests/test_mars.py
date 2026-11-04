import base64
import io
import tempfile
import zipfile
import json
import os
import stat
import sqlite3
import threading
import unittest
from openpyxl import Workbook
from http.cookiejar import CookieJar
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from PIL import Image

from mars import LocalFileStore, MarsHandler, TaskStore


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "mars.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_persists_task_and_event(self):
        task = self.store.create("調査", "公開情報を調べる", 4)
        loaded = self.store.get(task["id"])

        self.assertEqual(loaded["status"], "pending")
        self.assertEqual(loaded["priority"], 4)
        self.assertEqual(loaded["goal"], "公開情報を調べる")
        self.assertEqual(loaded["events"][0]["kind"], "created")

    def test_list_orders_by_priority(self):
        lower = self.store.create("通常", "通常の目的", 2)
        higher = self.store.create("優先", "優先する目的", 5)

        self.assertEqual([task["id"] for task in self.store.list()], [higher["id"], lower["id"]])

    def test_pause_and_resume(self):
        task = self.store.create("再開", "再開できること", 3)
        self.store.transition(task["id"], "planning")
        self.store.transition(task["id"], "running")
        self.store.checkpoint(task["id"], {"retry_count": 2})

        paused = self.store.transition(task["id"], "paused")
        resumed = self.store.transition(task["id"], "pending")

        self.assertEqual(paused["status"], "paused")
        self.assertEqual(resumed["status"], "pending")
        self.assertEqual(resumed["retry_count"], 0)
        self.assertEqual(len(resumed["events"]), 5)

    def test_resuming_failed_task_retries_failed_media_analysis(self):
        task = self.store.create("メディア再試行", "失敗後に再実行", 3, ["image.png"])
        active = self.store.claim_next()
        self.store.checkpoint(
            task["id"],
            {
                "status": "failed",
                "findings": [{"type": "media", "path": "image.png", "status": "failed", "error": "temporary"}],
            },
        )

        resumed = self.store.transition(active["id"], "pending")

        self.assertEqual(resumed["findings"], [{"type": "media", "path": "image.png", "status": "pending"}])

    def test_recover_interrupted_work(self):
        task = self.store.create("中断", "復旧すること", 3)
        self.store.transition(task["id"], "planning")
        self.store.transition(task["id"], "running")

        recovered_count = self.store.recover_interrupted()
        recovered = self.store.get(task["id"])

        self.assertEqual(recovered_count, 1)
        self.assertEqual(recovered["status"], "paused")
        self.assertIn("再起動", recovered["next_action"])
        self.assertEqual(recovered["events"][0]["kind"], "recovered")

    def test_invalid_transition_is_rejected(self):
        task = self.store.create("完了", "完了後は変更不可", 3)
        self.store.transition(task["id"], "planning")
        self.store.transition(task["id"], "completed")

        with self.assertRaises(ValueError):
            self.store.transition(task["id"], "pending")

    def test_memory_can_be_searched_updated_and_deleted(self):
        memory = self.store.create_memory("long_term", "好み", "日本語で回答")

        self.assertEqual(self.store.list_memories(query="日本語")[0]["id"], memory["id"])
        updated = self.store.update_memory(memory["id"], "knowledge", "回答方針", "簡潔に回答")
        self.assertEqual(updated["category"], "knowledge")
        self.assertEqual(updated["title"], "回答方針")
        self.assertTrue(self.store.delete_memory(memory["id"]))
        self.assertIsNone(self.store.get_memory(memory["id"]))

    def test_saving_an_exact_duplicate_memory_reuses_existing_entry(self):
        memory = self.store.create_memory("knowledge", "検索方針", "一次資料を優先")
        duplicate = self.store.create_memory("knowledge", "検索方針", "一次資料を優先")

        self.assertEqual(duplicate["id"], memory["id"])
        self.assertEqual(len(self.store.list_memories(category="knowledge")), 1)

    def test_updating_memory_to_an_exact_duplicate_merges_the_entries(self):
        original = self.store.create_memory("knowledge", "検索方針", "一次資料を優先")
        edited = self.store.create_memory("long_term", "好み", "日本語で回答")

        result = self.store.update_memory(edited["id"], "knowledge", "検索方針", "一次資料を優先")

        self.assertEqual(result["id"], original["id"])
        self.assertIsNone(self.store.get_memory(edited["id"]))
        self.assertEqual(len(self.store.list_memories(category="knowledge")), 1)

    def test_backup_contains_saved_tasks_and_memories(self):
        task = self.store.create("バックアップ", "保存対象を確認", 3)
        memory = self.store.create_memory("project", "Mars", "個人用アシスタント")
        backup = sqlite3.connect(":memory:")
        try:
            backup.deserialize(self.store.backup())
            self.assertIsNotNone(backup.execute("SELECT id FROM tasks WHERE id = ?", (task["id"],)).fetchone())
            self.assertIsNotNone(backup.execute("SELECT id FROM memories WHERE id = ?", (memory["id"],)).fetchone())
        finally:
            backup.close()

    def test_restore_replaces_database_contents(self):
        task = self.store.create("復元対象", "バックアップ", 3)
        self.store.transition(task["id"], "planning")
        self.store.transition(task["id"], "completed")
        memory = self.store.create_memory("knowledge", "復元対象", "元の内容")
        snapshot = self.store.backup()
        self.store.update_memory(memory["id"], "knowledge", "変更", "復元後は消える")

        self.store.restore(snapshot)

        self.assertEqual(self.store.get(task["id"])["status"], "completed")
        self.assertEqual(self.store.get_memory(memory["id"])["content"], "元の内容")

    def test_restore_refuses_unsettled_tasks_and_invalid_backup(self):
        task = self.store.create("未完了", "置き換え防止", 3)
        safety_path = Path(self.temp_dir.name) / "backups" / "before.sqlite3"

        self.assertFalse(self.store.restore_if_idle(b"invalid", safety_path))
        self.assertFalse(safety_path.exists())
        self.assertEqual(self.store.get(task["id"])["status"], "pending")
        with self.assertRaises(ValueError):
            self.store.restore(b"SQLite format 3\x00invalid")


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        store = TaskStore(Path(self.temp_dir.name) / "mars.sqlite3")
        handler = type(
            "TestMarsHandler",
            (MarsHandler,),
            {
                "store": store,
                "files": LocalFileStore(Path(self.temp_dir.name) / "files"),
                "data_root": Path(self.temp_dir.name) / "data",
                "provider": None,
                "sessions": {},
                "login_attempts": {},
            },
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp_dir.cleanup()

    def test_create_and_list_task_over_http(self):
        body = json.dumps({"title": "HTTPテスト", "goal": "APIで保存", "priority": 4}).encode()
        request = Request(self.base_url + "/api/tasks", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            created = json.load(response)["task"]
            self.assertEqual(response.status, 201)

        with urlopen(self.base_url + "/api/tasks") as response:
            tasks = json.load(response)["tasks"]
        self.assertEqual(tasks[0]["id"], created["id"])

    def test_invalid_priority_is_rejected(self):
        body = json.dumps({"title": "不正", "goal": "拒否", "priority": 9}).encode()
        request = Request(self.base_url + "/api/tasks", data=body, headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

    def test_non_string_task_fields_are_rejected(self):
        body = json.dumps({"title": None, "goal": "拒否"}).encode()
        request = Request(self.base_url + "/api/tasks", data=body, headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.load(error.exception)["error"], "タスク名は1〜120文字で入力してください")
        error.exception.close()

    def test_memory_api_requires_confirmation_for_deletion(self):
        body = json.dumps({"category": "knowledge", "title": "HTTP記憶", "content": "APIで保存"}).encode()
        request = Request(self.base_url + "/api/memories", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            memory = json.load(response)["memory"]
            self.assertEqual(response.status, 201)

        request = Request(self.base_url + "/api/memories?q=API", headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["memories"][0]["id"], memory["id"])

        request = Request(
            self.base_url + f"/api/memories/{memory['id']}",
            data=json.dumps({"confirm": False}).encode(),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        request = Request(
            self.base_url + f"/api/memories/{memory['id']}",
            data=json.dumps({"confirm": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with urlopen(request) as response:
            self.assertTrue(json.load(response)["ok"])

    def test_backup_endpoint_returns_valid_sqlite_database(self):
        self.server.RequestHandlerClass.store.create("backup", "HTTP backup", 2)
        with urlopen(self.base_url + "/api/backup") as response:
            self.assertEqual(response.headers.get_content_type(), "application/vnd.sqlite3")
            data = response.read()
        backup = sqlite3.connect(":memory:")
        try:
            backup.deserialize(data)
            self.assertEqual(backup.execute("SELECT title FROM tasks").fetchone()[0], "backup")
        finally:
            backup.close()

    def test_full_backup_contains_files_and_restores_database_and_file_tree(self):
        handler = self.server.RequestHandlerClass
        store = handler.store
        task = store.create("完全バックアップ", "DBとファイルを保存", 3)
        store.transition(task["id"], "planning")
        store.transition(task["id"], "completed")
        memory = store.create_memory("project", "完全バックアップ", "元の記憶")
        handler.files.save("folder/report.txt", "元のファイル")
        with urlopen(self.base_url + "/api/backup/full") as response:
            self.assertEqual(response.headers.get_content_type(), "application/zip")
            archive_data = response.read()
        with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
            self.assertIn("mars.sqlite3", archive.namelist())
            self.assertIn("files/folder/report.txt", archive.namelist())

        handler.files.save("folder/report.txt", "変更後")
        handler.files.save("new.txt", "復元時に消える")
        store.update_memory(memory["id"], "project", "変更", "変更後")
        request = Request(
            self.base_url + "/api/backup/restore",
            data=archive_data,
            headers={"Content-Type": "application/zip", "X-Mars-Confirm": "replace-database"},
            method="POST",
        )
        with urlopen(request) as response:
            restored = json.load(response)

        self.assertEqual(store.get_memory(memory["id"])["content"], "元の記憶")
        self.assertEqual(handler.files.read("folder/report.txt"), "元のファイル")
        with self.assertRaises(FileNotFoundError):
            handler.files.read("new.txt")
        self.assertTrue(restored["safety_backup"].endswith(".zip"))
        safety_backup = Path(self.temp_dir.name) / "data" / "backups" / restored["safety_backup"]
        self.assertTrue(safety_backup.is_file())
        with zipfile.ZipFile(safety_backup) as archive:
            self.assertEqual(archive.read("files/folder/report.txt"), "変更後".encode())
            self.assertIn("files/new.txt", archive.namelist())

    def test_full_backup_rejects_traversal_without_changing_existing_data(self):
        handler = self.server.RequestHandlerClass
        handler.files.save("preserve.txt", "still here")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mars.sqlite3", handler.store.backup())
            archive.writestr("files/", b"")
            archive.writestr("files/../outside.txt", "unsafe")
        request = Request(
            self.base_url + "/api/backup/restore",
            data=buffer.getvalue(),
            headers={"Content-Type": "application/zip", "X-Mars-Confirm": "replace-database"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(handler.files.read("preserve.txt"), "still here")
        self.assertFalse((Path(self.temp_dir.name) / "outside.txt").exists())

    def test_full_backup_restore_refuses_unsettled_tasks(self):
        handler = self.server.RequestHandlerClass
        handler.files.save("preserve.txt", "still here")
        with urlopen(self.base_url + "/api/backup/full") as response:
            archive_data = response.read()
        handler.store.create("未完了", "復元を止める", 3)
        request = Request(
            self.base_url + "/api/backup/restore",
            data=archive_data,
            headers={"Content-Type": "application/zip", "X-Mars-Confirm": "replace-database"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        self.assertEqual(handler.files.read("preserve.txt"), "still here")

    def test_restore_endpoint_requires_confirmation_and_preserves_safety_backup(self):
        store = self.server.RequestHandlerClass.store
        task = store.create("復元対象", "保存済み", 3)
        store.transition(task["id"], "planning")
        store.transition(task["id"], "completed")
        original_backup = store.backup()
        memory = store.create_memory("project", "退避", "復元で消える")

        request = Request(
            self.base_url + "/api/backup/restore",
            data=original_backup,
            headers={"Content-Type": "application/vnd.sqlite3"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        request = Request(
            self.base_url + "/api/backup/restore",
            data=original_backup,
            headers={
                "Content-Type": "application/vnd.sqlite3",
                "X-Mars-Confirm": "replace-database",
            },
            method="POST",
        )
        with urlopen(request) as response:
            result = json.load(response)
        self.assertTrue(result["safety_backup"].startswith("before-restore-"))
        self.assertIsNone(store.get_memory(memory["id"]))
        backup_dir = Path(self.temp_dir.name) / "data" / "backups"
        safety_backup = backup_dir / result["safety_backup"]
        self.assertTrue(safety_backup.is_file())
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(safety_backup.stat().st_mode), 0o600)

    def test_restore_endpoint_refuses_unsettled_work(self):
        store = self.server.RequestHandlerClass.store
        store.create("未完了", "復元を止める", 3)
        request = Request(
            self.base_url + "/api/backup/restore",
            data=store.backup(),
            headers={
                "Content-Type": "application/vnd.sqlite3",
                "X-Mars-Confirm": "replace-database",
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 409)
        self.assertIn("未完了タスク", json.load(error.exception)["error"])
        error.exception.close()

    def test_chat_requires_a_configured_provider(self):
        body = json.dumps({"message": "こんにちは"}).encode()
        request = Request(self.base_url + "/api/chat", data=body, headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 503)
        payload = json.load(error.exception)
        self.assertIn("未設定", payload["error"])
        self.assertNotIn("message_saved", payload)
        error.exception.close()

    def test_chat_sends_attached_image_to_multimodal_provider(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (24, 16), "red").save(image_buffer, format="PNG")

        class VisualProvider:
            def __init__(self):
                self.images = None

            def complete_with_images(self, system, prompt, images):
                self.images = images
                return "赤い画像を確認しました。"

        handler = self.server.RequestHandlerClass
        provider = VisualProvider()
        handler.provider = provider
        handler.files.import_file("picture.png", image_buffer.getvalue())
        payload = json.dumps({
            "message": "この画像を説明して",
            "media": ["picture.png"],
        }).encode()
        request = Request(self.base_url + "/api/chat", data=payload, headers={"Content-Type": "application/json"})

        with urlopen(request) as response:
            answer = json.load(response)["message"]["content"]

        self.assertIn("赤い", answer)
        self.assertEqual(provider.images[0]["label"], "picture.png")
        self.assertEqual(provider.images[0]["mime_type"], "image/png")
        self.assertIn("picture.png", handler.store.chat_history()[-2]["content"])

    def test_media_import_is_validated_private_and_listed_for_tasks(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (12, 12), "green").save(image_buffer, format="PNG")
        payload = json.dumps({
            "action": "import",
            "path": "media/green.png",
            "content_base64": base64.b64encode(image_buffer.getvalue()).decode("ascii"),
        }).encode()
        request = Request(self.base_url + "/api/files", data=payload, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            imported = json.load(response)["file"]

        with urlopen(self.base_url + "/api/files/media") as response:
            media = json.load(response)["files"]

        self.assertEqual(imported["path"], "media/green.png")
        self.assertEqual(media[0]["media_type"], "image")
        self.assertEqual(media[0]["path"], "media/green.png")
        with urlopen(self.base_url + "/api/backup/full") as response:
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                self.assertEqual(archive.read("files/media/green.png"), image_buffer.getvalue())
        if os.name == "posix":
            stored = self.server.RequestHandlerClass.files.root / "media" / "green.png"
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)

    def test_create_task_persists_selected_media_for_analysis(self):
        image_buffer = io.BytesIO()
        Image.new("RGB", (10, 10), "blue").save(image_buffer, format="PNG")
        self.server.RequestHandlerClass.files.import_file("input.png", image_buffer.getvalue())
        payload = json.dumps({
            "title": "画像を分析",
            "goal": "画像の内容を確認",
            "media": ["input.png"],
        }).encode()
        request = Request(self.base_url + "/api/tasks", data=payload, headers={"Content-Type": "application/json"})

        with urlopen(request) as response:
            task = json.load(response)["task"]

        self.assertEqual(task["findings"], [{"type": "media", "path": "input.png", "status": "pending"}])

    def test_file_api_saves_lists_reads_downloads_and_confirms_deletion(self):
        body = json.dumps({"action": "save", "path": "notes/test.md", "content": "Mars note"}).encode()
        request = Request(self.base_url + "/api/files", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["file"]["path"], "notes/test.md")

        with urlopen(self.base_url + "/api/files?path=notes") as response:
            self.assertEqual(json.load(response)["entries"][0]["name"], "test.md")
        query = urlencode({"path": "notes/test.md"})
        with urlopen(self.base_url + f"/api/files/content?{query}") as response:
            self.assertEqual(json.load(response)["content"], "Mars note")
        with urlopen(self.base_url + f"/api/files/download?{query}") as response:
            self.assertEqual(response.read(), b"Mars note")

        request = Request(
            self.base_url + "/api/files/notes/test.md",
            data=json.dumps({"confirm": True}).encode(),
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        with urlopen(request) as response:
            self.assertTrue(json.load(response)["ok"])

    def test_file_api_accepts_large_text_and_rejects_control_characters(self):
        body = json.dumps({"action": "save", "path": "large.txt", "content": "x" * 100000}).encode()
        request = Request(self.base_url + "/api/files", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
        query = urlencode({"path": "large.txt"})
        with urlopen(self.base_url + f"/api/files/download?{query}") as response:
            self.assertEqual(len(response.read()), 100000)

    def test_file_import_creates_without_overwriting_existing_data(self):
        body = json.dumps({"action": "import", "path": "imports/note.md", "content": "imported"}).encode()
        request = Request(self.base_url + "/api/files", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
        if os.name == "posix":
            files = self.server.RequestHandlerClass.files
            self.assertEqual(stat.S_IMODE(files.root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((files.root / "imports").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((files.root / "imports" / "note.md").stat().st_mode), 0o600)
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.server.RequestHandlerClass.files.read("imports/note.md"), "imported")

    def test_xlsx_import_extracts_content_and_downloads_original(self):
        workbook = Workbook()
        workbook.active.title = "Summary"
        workbook.active.append(["Mars", "Readable"])
        buffer = io.BytesIO()
        workbook.save(buffer)
        original = buffer.getvalue()
        payload = json.dumps({
            "action": "import",
            "path": "reports/summary.xlsx",
            "content_base64": base64.b64encode(original).decode("ascii"),
        }).encode()
        request = Request(self.base_url + "/api/files", data=payload, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
        self.assertIn("Mars", self.server.RequestHandlerClass.files.read("reports/summary.xlsx"))
        with urlopen(self.base_url + "/api/files?path=reports") as response:
            entry = json.load(response)["entries"][0]
        self.assertTrue(entry["readable"])
        self.assertFalse(entry["supported"])
        with urlopen(self.base_url + "/api/files/search?q=Readable") as response:
            self.assertEqual(json.load(response)["files"][0]["path"], "reports/summary.xlsx")
        with urlopen(self.base_url + f"/api/files/download?{urlencode({'path': entry['path']})}") as response:
            self.assertEqual(response.read(), original)
            self.assertEqual(response.headers.get_content_type(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    def test_file_api_body_limit_allows_json_escaped_text_within_file_size_limit(self):
        body = json.dumps({"action": "import", "path": "controls.txt", "content": "\x00" * 400000}).encode()
        request = Request(self.base_url + "/api/files", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 201)
        self.assertEqual(len(self.server.RequestHandlerClass.files.read("controls.txt")), 400000)

    def test_file_store_rejects_traversal_and_unsupported_types(self):
        files = self.server.RequestHandlerClass.files
        with self.assertRaises(ValueError):
            files.save("../outside.txt", "not allowed")
        with self.assertRaises(ValueError):
            files.save("script.py", "not supported")
        with self.assertRaises(ValueError):
            files.save("bad\r\nheader.txt", "not allowed")

    def test_created_folder_is_private(self):
        files = self.server.RequestHandlerClass.files
        folder = files.create_folder("private/nested")
        self.assertEqual(folder, "private/nested")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE((files.root / "private").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((files.root / folder).stat().st_mode), 0o700)

    def test_password_auth_protects_api_and_rejects_cross_origin_actions(self):
        handler = self.server.RequestHandlerClass
        handler.auth_password = "test-password-long"
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        request = Request(self.base_url + "/api/auth", headers={"Host": "attacker.example"})
        with self.assertRaises(HTTPError) as error:
            opener.open(request)
        self.assertEqual(error.exception.code, 421)
        error.exception.close()

        with self.assertRaises(HTTPError) as error:
            opener.open(self.base_url + "/api/tasks")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        request = Request(
            self.base_url + "/api/auth/login",
            data=json.dumps({"password": "wrong"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            opener.open(request)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        request = Request(
            self.base_url + "/api/auth/login",
            data=json.dumps({"password": "test-password-long"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request) as response:
            self.assertTrue(json.load(response)["authenticated"])

        request = Request(
            self.base_url + "/api/tasks",
            headers={"Origin": "http://attacker.example"},
        )
        with self.assertRaises(HTTPError) as error:
            opener.open(request)
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

        request = Request(
            self.base_url + "/api/auth/logout",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request) as response:
            self.assertFalse(json.load(response)["authenticated"])
        with self.assertRaises(HTTPError) as error:
            opener.open(self.base_url + "/api/tasks")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_chat_uses_related_memories_and_persists_response(self):
        memory = self.server.RequestHandlerClass.store.create_memory(
            "long_term", "回答の言語", "日本語で回答してほしい"
        )
        self.server.RequestHandlerClass.files.save("chat-note.md", "重要な資料の内容")

        class FakeProvider:
            def __init__(self):
                self.prompt = ""

            def complete(self, system, prompt):
                self.prompt = prompt
                return "こんにちは。"

        provider = FakeProvider()
        self.server.RequestHandlerClass.provider = provider
        body = json.dumps({"message": "日本語で話して", "files": ["chat-note.md"]}).encode()
        request = Request(self.base_url + "/api/chat", data=body, headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["message"]["content"], "こんにちは。")

        self.assertIn(memory["content"], provider.prompt)
        self.assertIn("重要な資料の内容", provider.prompt)
        with urlopen(self.base_url + "/api/chat") as response:
            messages = json.load(response)["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])

    def test_chat_rejects_too_many_or_unavailable_attachments_without_saving_message(self):
        self.server.RequestHandlerClass.provider = type(
            "FakeProvider",
            (),
            {"complete": lambda self, system, prompt: "回答"},
        )()
        for files in (["one.md", "two.md", "three.md", "four.md"], ["missing.md"]):
            body = json.dumps({"message": "添付を確認", "files": files}).encode()
            request = Request(self.base_url + "/api/chat", data=body, headers={"Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        self.assertEqual(self.server.RequestHandlerClass.store.chat_history(), [])


if __name__ == "__main__":
    unittest.main()