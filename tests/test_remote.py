from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram_codex_remote import (  # noqa: E402
    BridgeError,
    RemoteCodexJob,
    RemoteCodexRunner,
    build_user_input,
    format_recent_threads_summary,
    normalize_slot,
    session_list_cwd,
    thread_list_params,
)


class RemoteBridgeTests(unittest.TestCase):
    def make_runner(self, data=None):
        runner = RemoteCodexRunner.__new__(RemoteCodexRunner)

        class State:
            def __init__(self, initial):
                self.data = initial or {}
                self.saved = 0

            def save(self):
                self.saved += 1

        runner.state = State(data)
        runner.config = SimpleNamespace(
            workdir=ROOT,
            approval_policy="on-request",
            sandbox="workspace-write",
            model=None,
            model_provider=None,
        )
        runner.state_lock = threading.RLock()
        runner.active_slot = runner.state.data.get("active_slot", "default")
        runner.thread_id = RemoteCodexRunner._slot_thread_id(runner, runner.active_slot)
        runner.force_new_next = False
        return runner

    def test_build_user_input_text_and_images(self):
        items = build_user_input("hello", [ROOT / "a.png", ROOT / "b.jpg"])
        self.assertEqual({"type": "text", "text": "hello", "text_elements": []}, items[0])
        self.assertEqual({"type": "localImage", "path": str(ROOT / "a.png")}, items[1])
        self.assertEqual({"type": "localImage", "path": str(ROOT / "b.jpg")}, items[2])

    def test_approval_result_uses_app_server_decision_names(self):
        runner = RemoteCodexRunner.__new__(RemoteCodexRunner)
        self.assertEqual(
            {"decision": "accept"},
            RemoteCodexRunner._approval_result(
                runner,
                "item/commandExecution/requestApproval",
                True,
            ),
        )
        self.assertEqual(
            {"decision": "denied"},
            RemoteCodexRunner._approval_result(runner, "execCommandApproval", False),
        )

    def test_approval_summary_formats_command_lists(self):
        runner = RemoteCodexRunner.__new__(RemoteCodexRunner)
        summary = RemoteCodexRunner._approval_summary(
            runner,
            "item/commandExecution/requestApproval",
            {"command": ["git", "status"], "cwd": str(ROOT), "reason": "test"},
        )
        self.assertIn("command: git status", summary)
        self.assertIn(f"cwd: {ROOT}", summary)
        self.assertIn("reason: test", summary)

    def test_normalize_slot_makes_stable_storage_key(self):
        self.assertEqual("debug-fa", normalize_slot(" debug fa "))
        self.assertEqual("review.pr-1", normalize_slot("review.pr-1"))

    def test_slot_thread_storage(self):
        runner = self.make_runner()
        self.assertIsNone(RemoteCodexRunner._slot_thread_id(runner, "default"))
        RemoteCodexRunner._set_slot_thread_id(runner, "debug", "thread-1")
        self.assertEqual("thread-1", RemoteCodexRunner._slot_thread_id(runner, "debug"))

    def test_job_snapshot_captures_slot_thread_and_consumes_new_flag(self):
        runner = self.make_runner(
            {
                "active_slot": "debug",
                "session_slots": {"debug": "thread-1"},
                "session_slot_cwds": {"debug": str(ROOT / "project")},
            }
        )
        runner.force_new_next = True

        job = runner.build_job_snapshot(123, 456, "hello", [ROOT / "a.png"])

        self.assertEqual("debug", job.slot)
        self.assertEqual("thread-1", job.thread_id)
        self.assertEqual((ROOT / "project").resolve(), job.cwd)
        self.assertTrue(job.force_new)
        self.assertFalse(runner.force_new_next)

    def test_ensure_thread_uses_job_snapshot_after_active_slot_changes(self):
        runner = self.make_runner(
            {
                "active_slot": "new",
                "session_slots": {"old": "old-thread", "new": "new-thread"},
                "session_slot_cwds": {"old": str(ROOT / "old"), "new": str(ROOT / "new")},
            }
        )

        class Client:
            def __init__(self):
                self.calls = []

            def request(self, method, params, timeout=90):
                self.calls.append((method, params))
                return {"thread": {"id": "old-thread"}}

        client = Client()
        runner.client = client
        job = RemoteCodexJob(
            chat_id=1,
            message_id=2,
            slot="old",
            thread_id="old-thread",
            cwd=(ROOT / "old").resolve(),
            prompt="hello",
        )

        thread_id = runner.ensure_thread(job)

        self.assertEqual("old-thread", thread_id)
        self.assertEqual(("thread/resume", runner._thread_resume_params("old-thread", (ROOT / "old").resolve())), client.calls[0])
        self.assertEqual("new", runner.active_slot)
        self.assertEqual("old-thread", runner._slot_thread_id("old"))

    def test_ensure_thread_resume_failure_does_not_start_new_thread(self):
        runner = self.make_runner()

        class Client:
            def __init__(self):
                self.calls = []

            def request(self, method, params, timeout=90):
                self.calls.append(method)
                raise BridgeError("missing")

        runner.client = Client()
        job = RemoteCodexJob(
            chat_id=1,
            message_id=2,
            slot="debug",
            thread_id="missing-thread",
            cwd=ROOT,
            prompt="hello",
        )

        with self.assertRaises(BridgeError):
            runner.ensure_thread(job)

        self.assertEqual(["thread/resume"], runner.client.calls)

    def test_bind_thread_validates_and_saves_thread_cwd(self):
        runner = self.make_runner({"active_slot": "default"})

        class Client:
            def request(self, method, params, timeout=90):
                return {
                    "data": [
                        {
                            "id": "thread-2",
                            "source": "cli",
                            "status": {"type": "notLoaded"},
                            "cwd": str(ROOT / "other"),
                            "preview": "other project",
                        }
                    ]
                }

        runner.client = Client()

        slot = runner.bind_thread("thread-2", "other")

        self.assertEqual("other", slot)
        self.assertEqual("thread-2", runner._slot_thread_id("other"))
        self.assertEqual((ROOT / "other").resolve(), runner._slot_cwd("other"))
        self.assertEqual("other", runner.active_slot)

    def test_recent_threads_lists_all_threads_by_default(self):
        runner = RemoteCodexRunner.__new__(RemoteCodexRunner)

        class Client:
            def __init__(self):
                self.params = None

            def request(self, method, params, timeout):
                self.params = params
                return {
                    "data": [
                        {
                            "id": "thread-1",
                            "preview": "hello from another folder",
                            "source": "cli",
                            "status": {"type": "notLoaded"},
                            "updatedAt": 1778607918,
                            "cwd": "/tmp/other-project",
                        }
                    ]
                }

        client = Client()
        runner.client = client

        summary = RemoteCodexRunner.recent_threads_summary(runner)
        self.assertNotIn("cwd", client.params)
        self.assertIn("Recent Codex threads:", summary)
        self.assertIn("cwd: /tmp/other-project", summary)

    def test_recent_threads_can_filter_to_workdir(self):
        runner = RemoteCodexRunner.__new__(RemoteCodexRunner)

        class Client:
            def __init__(self):
                self.params = None

            def request(self, method, params, timeout):
                self.params = params
                return {"data": []}

        client = Client()
        runner.client = client

        summary = RemoteCodexRunner.recent_threads_summary(runner, cwd=ROOT)
        self.assertEqual(str(ROOT), client.params["cwd"])
        self.assertEqual("No Codex threads found for this workdir.", summary)

    def test_thread_list_params_only_adds_cwd_for_scoped_sessions(self):
        self.assertNotIn("cwd", thread_list_params(10))
        self.assertEqual(str(ROOT), thread_list_params(10, ROOT)["cwd"])
        self.assertIsNone(session_list_cwd("all", ROOT))
        self.assertEqual(ROOT, session_list_cwd("here", ROOT))

    def test_format_recent_threads_summary_handles_missing_fields(self):
        summary = format_recent_threads_summary([{"sessionId": "thread-2", "preview": ""}])
        self.assertIn("thread-2", summary)
        self.assertIn("[unknown/unknown]", summary)
        self.assertIn("(no preview)", summary)


if __name__ == "__main__":
    unittest.main()
