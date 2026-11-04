import json
import tempfile
import unittest
from pathlib import Path

from mars import TaskStore
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


if __name__ == "__main__":
    unittest.main()