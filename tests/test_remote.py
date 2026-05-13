from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram_codex_remote import (  # noqa: E402
    RemoteCodexRunner,
    build_user_input,
    format_recent_threads_summary,
    normalize_slot,
    session_list_cwd,
    thread_list_params,
)


class RemoteBridgeTests(unittest.TestCase):
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
        runner = RemoteCodexRunner.__new__(RemoteCodexRunner)

        class State:
            data = {}

            def save(self):
                pass

        runner.state = State()
        self.assertIsNone(RemoteCodexRunner._slot_thread_id(runner, "default"))
        RemoteCodexRunner._set_slot_thread_id(runner, "debug", "thread-1")
        self.assertEqual("thread-1", RemoteCodexRunner._slot_thread_id(runner, "debug"))

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
