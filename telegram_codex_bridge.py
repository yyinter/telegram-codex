#!/usr/bin/env python3
"""Telegram remote-control bridge for Codex CLI.

This is intentionally dependency-free: it uses Telegram's HTTPS Bot API
directly and shells out to the official Codex CLI.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TELEGRAM_MAX_MESSAGE = 3900
DEFAULT_STATE_DIR = ".telegram-codex"
IMAGE_MIME_PREFIX = "image/"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


class BridgeError(RuntimeError):
    pass


def log(message: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), message, flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def safe_filename(name: str, fallback: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in name)
    cleaned = cleaned.strip(" .")
    return cleaned or fallback


def is_image_path(path: Path, mime_type: str | None = None) -> bool:
    if mime_type and mime_type.startswith(IMAGE_MIME_PREFIX):
        return True
    guessed = mimetypes.guess_type(path.name)[0]
    if guessed and guessed.startswith(IMAGE_MIME_PREFIX):
        return True
    return path.suffix.lower() in IMAGE_SUFFIXES


def split_message(text: str, limit: int = TELEGRAM_MAX_MESSAGE) -> list[str]:
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    chunks.append(remaining)
    return chunks


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.api_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"

    def call(self, method: str, data: dict[str, Any] | None = None) -> Any:
        payload = urllib.parse.urlencode(data or {}).encode("utf-8")
        req = urllib.request.Request(f"{self.api_url}/{method}", data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=70) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            raise BridgeError(f"Telegram {method} failed: {body}")
        return body.get("result")

    def call_multipart(
        self,
        method: str,
        fields: dict[str, Any],
        file_field: str,
        file_path: Path,
    ) -> Any:
        boundary = "----telegram-codex-" + secrets.token_hex(12)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body = bytearray()

        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(file_path.read_bytes())
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = urllib.request.Request(
            f"{self.api_url}/{method}",
            data=bytes(body),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not result.get("ok"):
            raise BridgeError(f"Telegram {method} failed: {result}")
        return result.get("result")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        data: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            data["offset"] = offset
        return self.call("getUpdates", data) or []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        for index, chunk in enumerate(split_message(text)):
            data: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk or " ",
                "disable_web_page_preview": "true",
            }
            if reply_to and index == 0:
                data["reply_to_message_id"] = reply_to
                data["allow_sending_without_reply"] = "true"
            if reply_markup and index == 0:
                data["reply_markup"] = json.dumps(reply_markup)
            self.call("sendMessage", data)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id, "action": action})
        except Exception as exc:  # noqa: BLE001
            log(f"sendChatAction failed: {exc}")

    def send_file(self, chat_id: int, path: Path, caption: str = "") -> None:
        if is_image_path(path):
            self.call_multipart("sendPhoto", {"chat_id": chat_id, "caption": caption}, "photo", path)
        else:
            self.call_multipart(
                "sendDocument",
                {"chat_id": chat_id, "caption": caption},
                "document",
                path,
            )

    def get_file_path(self, file_id: str) -> str:
        result = self.call("getFile", {"file_id": file_id})
        return result["file_path"]

    def download_file(self, file_id: str, destination: Path) -> Path:
        file_path = self.get_file_path(file_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{self.file_url}/{file_path}"
        with urllib.request.urlopen(url, timeout=120) as response:
            destination.write_bytes(response.read())
        return destination


@dataclass
class Attachment:
    kind: str
    path: Path
    mime_type: str | None = None
    original_name: str | None = None


@dataclass
class CodexJob:
    chat_id: int
    message_id: int
    prompt: str
    image_paths: list[Path] = field(default_factory=list)
    force_new: bool = False


@dataclass
class BridgeConfig:
    token: str
    codex_bin: str
    codex_args: list[str]
    workdir: Path
    state_dir: Path
    attachments_dir: Path
    allowed_chat_id: int | None
    pair_code: str
    resume_by_default: bool
    allow_absolute_send: bool


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def offset(self) -> int | None:
        value = self.data.get("offset")
        return int(value) if value is not None else None

    @offset.setter
    def offset(self, value: int | None) -> None:
        self.data["offset"] = value
        self.save()

    @property
    def paired_chat_id(self) -> int | None:
        value = self.data.get("paired_chat_id")
        return int(value) if value is not None else None

    @paired_chat_id.setter
    def paired_chat_id(self, value: int | None) -> None:
        self.data["paired_chat_id"] = value
        self.save()


class CodexRunner:
    def __init__(self, config: BridgeConfig, telegram: TelegramClient) -> None:
        self.config = config
        self.telegram = telegram
        self.jobs: queue.Queue[CodexJob | None] = queue.Queue()
        self.thread = threading.Thread(target=self._worker, name="codex-worker", daemon=True)
        self.current_process: subprocess.Popen[str] | None = None
        self.current_lock = threading.Lock()
        self.use_resume = config.resume_by_default
        self.thread.start()

    def enqueue(self, job: CodexJob) -> int:
        pending = self.jobs.qsize()
        self.jobs.put(job)
        return pending + 1

    def stop(self) -> None:
        with self.current_lock:
            process = self.current_process
        if process and process.poll() is None:
            process.terminate()
            log("Terminated running Codex process")

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self._run_job(job)
            except Exception as exc:  # noqa: BLE001
                log(f"Codex job failed: {exc}")
                self.telegram.send_message(job.chat_id, f"Codex run failed:\n{exc}", job.message_id)
            finally:
                self.jobs.task_done()

    def _build_command(self, job: CodexJob, output_file: Path) -> list[str]:
        image_args: list[str] = []
        for image in job.image_paths:
            image_args.extend(["-i", str(image)])

        common = [*self.config.codex_args, *image_args, "-o", str(output_file)]
        if job.force_new or not self.use_resume:
            return [self.config.codex_bin, "exec", *common, "-"]
        return [self.config.codex_bin, "exec", "resume", "--last", *common, "-"]

    def _run_once(self, job: CodexJob, resume: bool) -> tuple[int, str, str, str]:
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as output:
            output_path = Path(output.name)
        cmd_job = CodexJob(
            chat_id=job.chat_id,
            message_id=job.message_id,
            prompt=job.prompt,
            image_paths=job.image_paths,
            force_new=not resume,
        )
        cmd = self._build_command(cmd_job, output_path)
        log("Running: " + shlex.join(cmd[:4]) + " ...")
        process = subprocess.Popen(
            cmd,
            cwd=self.config.workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        with self.current_lock:
            self.current_process = process
        try:
            stdout, stderr = process.communicate(job.prompt)
        finally:
            with self.current_lock:
                self.current_process = None
        last_message = ""
        if output_path.exists():
            last_message = output_path.read_text(encoding="utf-8", errors="replace").strip()
            output_path.unlink(missing_ok=True)
        return process.returncode, stdout, stderr, last_message

    def _run_job(self, job: CodexJob) -> None:
        self.telegram.send_chat_action(job.chat_id)
        resume = not job.force_new and self.use_resume
        returncode, stdout, stderr, last_message = self._run_once(job, resume=resume)

        if resume and returncode != 0 and "No session" in (stdout + stderr):
            self.telegram.send_message(job.chat_id, "No previous Codex session found; starting a new one.")
            returncode, stdout, stderr, last_message = self._run_once(job, resume=False)

        if returncode < 0:
            response = "Codex run was cancelled."
        elif returncode != 0:
            response = (last_message or stdout or stderr or "Codex exited without output.").strip()
            response = f"Codex exited with code {returncode}.\n\n{response}"
        else:
            response = (last_message or stdout or "Codex completed without a final message.").strip()
            self.use_resume = True

        self.telegram.send_message(job.chat_id, response, job.message_id)


class TelegramCodexBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.telegram = TelegramClient(config.token)
        self.state = StateStore(config.state_dir / "state.json")
        self.runner = CodexRunner(config, self.telegram)

    def is_authorized(self, chat_id: int) -> bool:
        if self.config.allowed_chat_id is not None:
            return chat_id == self.config.allowed_chat_id
        paired = self.state.paired_chat_id
        return paired is not None and chat_id == paired

    def maybe_pair(self, message: dict[str, Any]) -> bool:
        chat_id = int(message["chat"]["id"])
        text = (message.get("text") or "").strip()
        if self.config.allowed_chat_id is not None:
            return False
        if self.state.paired_chat_id is not None:
            return False
        if text == f"/pair {self.config.pair_code}":
            self.state.paired_chat_id = chat_id
            self.telegram.send_message(chat_id, "Paired. Send /help for commands.")
            log(f"Paired Telegram chat {chat_id}")
            return True
        self.telegram.send_message(chat_id, "Bridge is not paired. Send /pair <code> from the bridge terminal.")
        return True

    def handle_update(self, update: dict[str, Any]) -> None:
        self.state.offset = int(update["update_id"]) + 1
        message = update.get("message")
        if not message:
            return
        chat_id = int(message["chat"]["id"])
        if self.maybe_pair(message):
            return
        if not self.is_authorized(chat_id):
            log(f"Ignored unauthorized chat {chat_id}")
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/"):
            if self.handle_command(chat_id, int(message["message_id"]), text):
                return

        attachments = self.download_attachments(message)
        prompt = self.build_prompt(message, attachments)
        if not prompt.strip():
            self.telegram.send_message(chat_id, "Send text, a photo, or a document to forward it to Codex.")
            return
        image_paths = [item.path for item in attachments if is_image_path(item.path, item.mime_type)]
        queue_position = self.runner.enqueue(
            CodexJob(
                chat_id=chat_id,
                message_id=int(message["message_id"]),
                prompt=prompt,
                image_paths=image_paths,
            )
        )
        if queue_position > 1:
            self.telegram.send_message(chat_id, f"Queued for Codex. Queue position: {queue_position}.")

    def handle_command(self, chat_id: int, message_id: int, text: str) -> bool:
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0].lower()

        if command in {"/start", "/help"}:
            self.telegram.send_message(chat_id, self.help_text(), message_id)
            return True
        if command == "/status":
            paired = self.config.allowed_chat_id or self.state.paired_chat_id
            mode = "resume --last" if self.runner.use_resume else "new session"
            self.telegram.send_message(
                chat_id,
                "\n".join(
                    [
                        "Telegram Codex bridge is running.",
                        f"chat_id: {paired}",
                        f"workdir: {self.config.workdir}",
                        f"attachments: {self.config.attachments_dir}",
                        f"codex mode: {mode}",
                        f"queued jobs: {self.runner.jobs.qsize()}",
                    ]
                ),
                message_id,
            )
            return True
        if command == "/new":
            self.runner.use_resume = False
            self.telegram.send_message(chat_id, "Next prompt will start a new Codex session.", message_id)
            return True
        if command == "/resume":
            self.runner.use_resume = True
            self.telegram.send_message(chat_id, "Next prompt will resume the latest Codex session.", message_id)
            return True
        if command in {"/cancel", "/stop"}:
            self.runner.stop()
            self.telegram.send_message(chat_id, "Cancel requested for the current Codex run.", message_id)
            return True
        if command == "/where":
            self.telegram.send_message(
                chat_id,
                f"workdir: {self.config.workdir}\nattachments: {self.config.attachments_dir}",
                message_id,
            )
            return True
        if command == "/sendfile":
            self.handle_sendfile(chat_id, message_id, arg)
            return True
        return False

    def handle_sendfile(self, chat_id: int, message_id: int, arg: str) -> None:
        if not arg.strip():
            self.telegram.send_message(chat_id, "Usage: /sendfile <path> [caption]", message_id)
            return
        try:
            parts = shlex.split(arg)
        except ValueError as exc:
            self.telegram.send_message(chat_id, f"Could not parse /sendfile arguments: {exc}", message_id)
            return
        if not parts:
            self.telegram.send_message(chat_id, "Usage: /sendfile <path> [caption]", message_id)
            return
        path = Path(parts[0]).expanduser()
        if not path.is_absolute():
            path = self.config.workdir / path
        path = path.resolve()
        if not self.config.allow_absolute_send and not path.is_relative_to(self.config.workdir):
            self.telegram.send_message(
                chat_id,
                "Refusing to send files outside CODEX_WORKDIR. Set TELEGRAM_ALLOW_ABSOLUTE_SEND=1 to allow it.",
                message_id,
            )
            return
        if not path.is_file():
            self.telegram.send_message(chat_id, f"File not found: {path}", message_id)
            return
        caption = " ".join(parts[1:])
        self.telegram.send_file(chat_id, path, caption)

    def download_attachments(self, message: dict[str, Any]) -> list[Attachment]:
        attachments: list[Attachment] = []
        message_id = int(message["message_id"])
        timestamp = int(time.time())
        base_dir = self.config.attachments_dir / f"{timestamp}-{message_id}"

        if "photo" in message:
            photo = max(message["photo"], key=lambda item: item.get("file_size", 0))
            path = base_dir / "photo.jpg"
            self.telegram.download_file(photo["file_id"], path)
            attachments.append(Attachment(kind="photo", path=path, mime_type="image/jpeg"))

        if "document" in message:
            document = message["document"]
            name = safe_filename(document.get("file_name", ""), f"document-{message_id}")
            path = base_dir / name
            self.telegram.download_file(document["file_id"], path)
            attachments.append(
                Attachment(
                    kind="document",
                    path=path,
                    mime_type=document.get("mime_type"),
                    original_name=document.get("file_name"),
                )
            )

        return attachments

    def build_prompt(self, message: dict[str, Any], attachments: list[Attachment]) -> str:
        parts: list[str] = []
        reply = message.get("reply_to_message")
        if reply:
            reply_text = reply.get("text") or reply.get("caption")
            if reply_text:
                parts.append("Telegram reply context:\n" + reply_text.strip())

        text = (message.get("text") or message.get("caption") or "").strip()
        if text:
            parts.append("Telegram user message:\n" + text)

        if attachments:
            lines = ["Attachments downloaded from Telegram:"]
            for item in attachments:
                label = item.original_name or item.path.name
                lines.append(f"- {item.kind}: {item.path} ({label})")
            lines.append("Use these local file paths directly when they are relevant.")
            parts.append("\n".join(lines))

        if attachments and not text:
            parts.append("Please inspect or use the attached file(s) as context for the next step.")

        return "\n\n".join(parts)

    def help_text(self) -> str:
        return "\n".join(
            [
                "Commands:",
                "/help - show this help",
                "/status - bridge status",
                "/new - start a fresh Codex session on the next prompt",
                "/resume - resume the latest Codex session on the next prompt",
                "/cancel - terminate the current Codex run",
                "/where - show work and attachment directories",
                "/sendfile <path> [caption] - send a local image/PDF/file back to Telegram",
                "",
                "Plain text is forwarded to Codex. Photos are attached with codex -i; PDFs and other documents are downloaded and passed as local paths.",
            ]
        )

    def run(self) -> None:
        if self.config.allowed_chat_id is not None:
            log(f"Allowed chat id: {self.config.allowed_chat_id}")
        elif self.state.paired_chat_id is None:
            log(f"Send this to your bot from the authorized Telegram chat: /pair {self.config.pair_code}")
        else:
            log(f"Paired chat id: {self.state.paired_chat_id}")

        log(f"Workdir: {self.config.workdir}")
        log("Bridge polling Telegram. Press Ctrl-C to stop.")
        while True:
            try:
                updates = self.telegram.get_updates(self.state.offset)
                for update in updates:
                    self.handle_update(update)
            except KeyboardInterrupt:
                raise
            except urllib.error.URLError as exc:
                log(f"Telegram network error: {exc}; retrying in 5s")
                time.sleep(5)
            except Exception as exc:  # noqa: BLE001
                log(f"Bridge error: {exc}; retrying in 5s")
                time.sleep(5)


def find_codex_bin() -> str:
    env_value = os.getenv("CODEX_BIN")
    if env_value:
        return env_value
    user_npm = Path.home() / ".local" / "npm" / "bin" / "codex"
    if user_npm.exists():
        return str(user_npm)
    found = shutil.which("codex")
    if found:
        return found
    local = Path(__file__).resolve().parent.parent / "node_modules" / ".bin" / "codex"
    if local.exists():
        return str(local)
    raise BridgeError("Could not find codex. Set CODEX_BIN=/path/to/codex.")


def build_config(args: argparse.Namespace) -> BridgeConfig:
    token = args.token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise BridgeError("TELEGRAM_BOT_TOKEN is required.")

    workdir = Path(args.workdir or os.getenv("CODEX_WORKDIR") or os.getcwd()).expanduser().resolve()
    state_dir = Path(os.getenv("TELEGRAM_CODEX_STATE_DIR", workdir / DEFAULT_STATE_DIR)).expanduser().resolve()
    attachments_dir = Path(
        os.getenv("TELEGRAM_ATTACHMENTS_DIR", state_dir / "attachments")
    ).expanduser().resolve()

    allowed_chat: int | None = None
    raw_chat = args.allowed_chat_id or os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
    if raw_chat:
        allowed_chat = int(raw_chat)

    codex_args = shlex.split(os.getenv("CODEX_ARGS", "--skip-git-repo-check --full-auto"))
    pair_code = os.getenv("TELEGRAM_PAIR_CODE") or secrets.token_hex(3)
    default_mode = os.getenv("TELEGRAM_CODEX_DEFAULT", "resume").strip().lower()

    return BridgeConfig(
        token=token,
        codex_bin=find_codex_bin(),
        codex_args=codex_args,
        workdir=workdir,
        state_dir=state_dir,
        attachments_dir=attachments_dir,
        allowed_chat_id=allowed_chat,
        pair_code=pair_code,
        resume_by_default=default_mode != "new",
        allow_absolute_send=env_bool("TELEGRAM_ALLOW_ABSOLUTE_SEND"),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remote-control Codex CLI from Telegram.")
    parser.add_argument("--token", help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--allowed-chat-id", help="Only accept this Telegram chat id.")
    parser.add_argument("--workdir", help="Codex working directory. Defaults to current directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        config = build_config(parse_args(argv or sys.argv[1:]))
        TelegramCodexBridge(config).run()
    except KeyboardInterrupt:
        log("Stopped.")
        return 0
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
