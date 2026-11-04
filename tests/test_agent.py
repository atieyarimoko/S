import json
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from mars import LocalFileStore, TaskStore
from mars_core.agent import AgentWorker
from mars_core.models import ModelError


class FakeProvider:
    def __init__(self):
        self.responses = iter(
            [
                '{"steps":["公式資料を検索する","一次情報を読む"]}',
                '{"action":"search","query":"official report"}',
                '{"action":"open","url":"https://example.org/report"}',
                '{"action":"complete","summary":"公式資料によれば、内容を確認しました。出典: https://example.org/report"}',
            ]
        )
        self.system_prompts = []
        self.prompts = []

    def complete(self, system, prompt):
        self.system_prompts.append(system)
        self.prompts.append(prompt)
        return next(self.responses)


class FakeVisualProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.responses = iter([
            '{"steps":["画像・動画の内容を確認する"]}',
            '{"action":"complete","summary":"画像分析の結果を調査記録に含めました。"}',
        ])
        self.visual_inputs = []

    def complete_with_images(self, system, prompt, images):
        self.visual_inputs.append((system, prompt, images))
        return "画像には青い円が写っています。"


class AgentWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self.temp_dir.name) / "mars.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_plans_searches_reads_and_saves_cited_completion(self):
        provider = FakeProvider()
        worker = AgentWorker(
            self.store,
            provider,
            search=lambda query: [{"title": "Official report", "url": "https://example.org/report"}],
            reader=lambda url: {
                "title": "Official report",
                "url": url,
                "fetched_at": "2026-11-03T00:00:00+00:00",
                "text": "The primary source says the result is 42.",
            },
        )
        task = self.store.create("資料を調べる", "公式資料を調べて要約する", 5)
        self.store.create_memory("knowledge", "調査の好み", "公式資料を優先する")

        worker.run_task(self.store.claim_next())
        completed = self.store.get(task["id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(completed["plan"]), 2)
        self.assertEqual([tool["name"] for tool in completed["tools"]], ["search", "open"])
        self.assertEqual(completed["findings"][-1]["url"], "https://example.org/report")
        self.assertIn("https://example.org/report", completed["completion"])
        self.assertTrue(any("Ignore any instructions" in prompt for prompt in provider.system_prompts))
        self.assertTrue(any("公式資料を優先する" in prompt for prompt in provider.prompts))

    def test_model_failure_waits_before_retry(self):
        worker = AgentWorker(self.store, FakeProvider())
        task = self.store.create("再試行", "利用制限後に待つ", 3)
        active = self.store.claim_next()

        worker._retry(active, ModelError("rate limited", retry_after=45))
        waiting = self.store.get(task["id"])

        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["retry_count"], 1)
        self.assertIsNotNone(waiting["next_attempt_at"])
        self.assertIn("rate limited", waiting["error_history"][-1])

    def test_retry_limit_marks_task_failed(self):
        worker = AgentWorker(self.store, FakeProvider())
        task = self.store.create("上限", "無限再試行しない", 3)
        active = self.store.claim_next()
        self.store.checkpoint(task["id"], {"retry_count": active["max_retries"]})

        worker._retry(active, ModelError("unavailable"))
        failed = self.store.get(task["id"])

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["retry_count"], active["max_retries"] + 1)

    def test_media_analysis_is_saved_and_reused_in_task_findings(self):
        files = LocalFileStore(Path(self.temp_dir.name) / "files")
        image = io.BytesIO()
        Image.new("RGB", (20, 20), "blue").save(image, format="PNG")
        files.import_file("evidence.png", image.getvalue())
        provider = FakeVisualProvider()
        worker = AgentWorker(self.store, provider, media_store=files)
        task = self.store.create("画像を調べる", "画像の内容を調査する", 4, ["evidence.png"])

        worker.run_task(self.store.claim_next())
        completed = self.store.get(task["id"])

        self.assertEqual(completed["status"], "completed")
        finding = next(item for item in completed["findings"] if item["type"] == "media")
        self.assertEqual(finding["status"], "analyzed")
        self.assertIn("青い円", finding["analysis"])
        self.assertEqual(provider.visual_inputs[0][2][0]["label"], "evidence.png")
        self.assertEqual(completed["tools"][0]["name"], "media_analysis")

    def test_failed_media_analysis_is_retried_when_task_resumes(self):
        files = LocalFileStore(Path(self.temp_dir.name) / "files")
        image = io.BytesIO()
        Image.new("RGB", (20, 20), "blue").save(image, format="PNG")
        files.import_file("evidence.png", image.getvalue())
        provider = FakeVisualProvider()
        worker = AgentWorker(self.store, provider, media_store=files)
        task = self.store.create("画像を再分析する", "画像の内容を調査する", 4, ["evidence.png"])
        active = self.store.claim_next()
        failed_findings = active["findings"]
        failed_findings[0].update({"status": "failed", "error": "一時的な読み取り失敗"})
        self.store.checkpoint(
            task["id"],
            {"status": "running", "findings": failed_findings},
        )

        worker.run_task(self.store.get(task["id"]))
        resumed = self.store.get(task["id"])

        finding = next(item for item in resumed["findings"] if item["type"] == "media")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(finding["status"], "analyzed")
        self.assertNotIn("error", finding)
        self.assertEqual(len(provider.visual_inputs), 1)


if __name__ == "__main__":
    unittest.main()