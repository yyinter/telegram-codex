from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from telegram_codex_bridge import (  # noqa: E402
    Attachment,
    BridgeConfig,
    CodexJob,
    CodexRunner,
    TelegramCodexBridge,
    is_image_path,
    split_message,
)


class DummyTelegram:
    def __init__(self) -> None:
        self.messages = []
        self.files = []

    def send_message(self, chat_id, text, reply_to=None):
        self.messages.append((chat_id, text, reply_to))

    def send_file(self, chat_id, path, caption=""):
        self.files.append((chat_id, Path(path), caption))


class BridgeTests(unittest.TestCase):
    def config(self) -> BridgeConfig:
        root = ROOT
        return BridgeConfig(
            token="token",
            codex_bin="/bin/echo",
            codex_args=["--skip-git-repo-check", "--full-auto"],
            workdir=root,
            state_dir=root / ".tmp-test-state",
            attachments_dir=root / ".tmp-test-state" / "attachments",
            allowed_chat_id=123,
            pair_code="abc123",
            resume_by_default=True,
            allow_absolute_send=False,
        )

    def test_split_message_chunks_long_text(self):
        text = "a" * 5000
        chunks = split_message(text, limit=1000)
        self.assertEqual(5, len(chunks))
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))

    def test_image_detection_uses_mime_and_suffix(self):
        self.assertTrue(is_image_path(Path("photo.jpg")))
        self.assertTrue(is_image_path(Path("upload.bin"), "image/png"))
        self.assertFalse(is_image_path(Path("report.pdf"), "application/pdf"))

    def test_build_prompt_includes_reply_text_and_attachments(self):
        bridge = TelegramCodexBridge.__new__(TelegramCodexBridge)
        bridge.config = self.config()
        message = {
            "text": "please inspect",
            "reply_to_message": {"text": "previous answer"},
        }
        attachments = [
            Attachment(
                kind="document",
                path=ROOT / "report.pdf",
                mime_type="application/pdf",
                original_name="report.pdf",
            )
        ]
        prompt = TelegramCodexBridge.build_prompt(bridge, message, attachments)
        self.assertIn("Telegram reply context", prompt)
        self.assertIn("previous answer", prompt)
        self.assertIn("Telegram user message", prompt)
        self.assertIn("report.pdf", prompt)

    def test_codex_resume_command_uses_resume_last(self):
        runner = CodexRunner.__new__(CodexRunner)
        runner.config = self.config()
        runner.use_resume = True
        command = CodexRunner._build_command(
            runner,
            CodexJob(chat_id=1, message_id=2, prompt="hi", image_paths=[ROOT / "image.png"]),
            ROOT / "last.txt",
        )
        self.assertEqual(["/bin/echo", "exec", "resume", "--last"], command[:4])
        self.assertIn("-i", command)
        self.assertEqual("-", command[-1])

    def test_sendfile_refuses_paths_outside_workdir(self):
        bridge = TelegramCodexBridge.__new__(TelegramCodexBridge)
        bridge.config = self.config()
        bridge.telegram = DummyTelegram()
        TelegramCodexBridge.handle_sendfile(bridge, 123, 10, "/etc/passwd")
        self.assertFalse(bridge.telegram.files)
        self.assertIn("Refusing", bridge.telegram.messages[0][1])


if __name__ == "__main__":
    unittest.main()
