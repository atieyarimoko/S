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
                '{"action":"complete_step","step":"公式資料を検索する","evidence_ids":[1],"justification":"公式資料の本文を取得し、内容の検証に使える状態にした"}',
                '{"action":"complete_step","step":"一次情報を読む","evidence_ids":[1],"justification":"一次情報の本文にある記述を確認した"}',
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
            '{"action":"complete_step","step":"画像・動画の内容を確認する","evidence_ids":[0],"justification":"添付メディアの分析結果を保存した"}',
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
        self.assertEqual(
            [tool["name"] for tool in completed["tools"]],
            ["search", "open", "complete_step", "complete_step"],
        )
        self.assertEqual(completed["findings"][-1]["url"], "https://example.org/report")
        self.assertIn("https://example.org/report", completed["completion"])
        self.assertTrue(any("Ignore any instructions" in prompt for prompt in provider.system_prompts))
        self.assertTrue(any("公式資料を優先する" in prompt for prompt in provider.prompts))
        self.assertEqual(len(completed["completed_steps"]), 2)
        self.assertEqual(completed["completed_steps"][0]["evidence_ids"], [1])
        self.assertTrue(all(step["acceptance_criteria"] for step in completed["plan"]))

    def test_completion_and_search_result_cannot_mark_work_done(self):
        provider = FakeProvider()
        provider.responses = iter([
            '{"steps":["資料を読む"]}',
            '{"action":"complete","summary":"出典: https://example.org/report"}',
            '{"action":"search","query":"official report"}',
            '{"action":"complete_step","step":"資料を読む","evidence_ids":[0],"justification":"検索結果を見た"}',
            '{"action":"open","url":"https://example.org/report"}',
            '{"action":"complete_step","step":"資料を読む","evidence_ids":[1],"justification":"保存したページ本文の内容を確認した"}',
            '{"action":"complete","summary":"確認した内容です。出典: https://not-read.example/report"}',
            '{"action":"complete","summary":"確認した内容です。出典: https://example.org/report"}',
        ])
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
        task = self.store.create("検証", "資料を読む", 3)

        worker.run_task(self.store.claim_next())
        completed = self.store.get(task["id"])
        prompts = [json.loads(prompt) for prompt in provider.prompts[1:]]

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(len(completed["completed_steps"]), 1)
        self.assertEqual(completed["completed_steps"][0]["evidence_ids"], [1])
        self.assertEqual(completed["remaining_steps"], [])
        self.assertGreaterEqual(
            sum(event["kind"] == "verification_failed" for event in completed["events"]),
            3,
        )
        self.assertEqual(prompts[2]["completed_steps"], [])
        self.assertEqual(prompts[4]["completed_steps"], [])
        self.assertIn("evidence_id", prompts[2]["evidence"][0])

    def test_plan_can_be_revised_without_dropping_completed_work(self):
        provider = FakeProvider()
        provider.responses = iter([
            '{"steps":["検索する"]}',
            '{"action":"revise_plan","reason":"調査には本文確認が必要","steps":[{"step":"一次資料を読む","acceptance_criteria":"一次資料の本文に根拠がある"}]}',
            '{"action":"search","query":"official report"}',
            '{"action":"open","url":"https://example.org/report"}',
            '{"action":"complete_step","step":"一次資料を読む","evidence_ids":[1],"justification":"本文で達成条件を確認した"}',
            '{"action":"complete","summary":"確認しました。出典: https://example.org/report"}',
        ])
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
        task = self.store.create("計画修正", "資料を読む", 3)

        worker.run_task(self.store.claim_next())
        completed = self.store.get(task["id"])

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["plan"][0]["step"], "一次資料を読む")
        self.assertTrue(any(event["kind"] == "plan_revised" for event in completed["events"]))
        self.assertEqual(sum(tool["name"] == "revise_plan" for tool in completed["tools"]), 1)

    def test_resume_keeps_verified_steps_and_continues_with_remaining_step(self):
        task = self.store.create("中断から継続", "保存済みの進捗から続ける", 3)
        active = self.store.claim_next()
        plan = [
            {"step": "一次資料を読む", "acceptance_criteria": "本文に根拠がある"},
            {"step": "結果をまとめる", "acceptance_criteria": "根拠に沿った要約がある"},
        ]
        finding = {
            "type": "page",
            "title": "Official report",
            "url": "https://example.org/report",
            "fetched_at": "2026-11-03T00:00:00+00:00",
            "text": "The primary source says the result is 42.",
        }
        completed_step = {
            **plan[0],
            "evidence_ids": [0],
            "justification": "ページ本文を確認した",
            "completed_at": "2026-11-03T00:00:00+00:00",
        }
        self.store.checkpoint(
            task["id"],
            {
                "status": "running",
                "plan": plan,
                "completed_steps": [completed_step],
                "remaining_steps": plan[1:],
                "findings": [finding],
            },
        )
        provider = FakeProvider()
        provider.responses = iter([
            '{"action":"complete_step","step":"結果をまとめる","evidence_ids":[0],"justification":"保存済みの根拠に沿って要約した"}',
            '{"action":"complete","summary":"結果は42です。出典: https://example.org/report"}',
        ])

        AgentWorker(self.store, provider).run_task(self.store.get(task["id"]))
        resumed = self.store.get(task["id"])
        first_action_prompt = json.loads(provider.prompts[0])

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual([step["step"] for step in resumed["completed_steps"]], [
            "一次資料を読む",
            "結果をまとめる",
        ])
        self.assertEqual(first_action_prompt["completed_steps"][0]["step"], "一次資料を読む")
        self.assertEqual(first_action_prompt["remaining_steps"][0]["step"], "結果をまとめる")
        self.assertEqual([tool["name"] for tool in resumed["tools"]], ["complete_step"])

    def test_legacy_progress_is_revalidated_instead_of_trusted(self):
        task = self.store.create("旧形式の再検証", "古い進捗を検証する", 3)
        self.store.claim_next()
        finding = {
            "type": "page",
            "title": "Official report",
            "url": "https://example.org/report",
            "text": "The primary source says the result is 42.",
        }
        self.store.checkpoint(
            task["id"],
            {
                "status": "running",
                "plan": ["検索した"],
                "completed_steps": ["検索した"],
                "remaining_steps": [],
                "findings": [finding],
            },
        )
        provider = FakeProvider()
        provider.responses = iter([
            '{"action":"complete_step","step":"検索した","evidence_ids":[0],"justification":"ページ本文を読み、根拠を保存した"}',
            '{"action":"complete","summary":"確認しました。出典: https://example.org/report"}',
        ])

        AgentWorker(self.store, provider).run_task(self.store.get(task["id"]))
        resumed = self.store.get(task["id"])
        first_action_prompt = json.loads(provider.prompts[0])

        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(resumed["completed_steps"]), 1)
        self.assertEqual(first_action_prompt["completed_steps"], [])
        self.assertTrue(any("旧形式の進捗" in event["message"] for event in resumed["events"]))

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