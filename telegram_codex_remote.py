#!/usr/bin/env python3
"""Telegram bridge using Codex app-server remote-control protocol.

This keeps a Codex app-server process alive and drives a persistent thread via
JSON-RPC over stdio. It is closer to IDE/remote-control usage than repeatedly
spawning `codex exec resume --last`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from telegram_codex_bridge import (
    Attachment,
    BridgeError,
    StateStore,
    TelegramClient,
    env_bool,
    is_image_path,
    log,
    safe_filename,
)


DEFAULT_STATE_DIR = ".telegram-codex"
REQUEST_TIMEOUT_SECONDS = 90
TURN_TIMEOUT_SECONDS = 60 * 60
DEFAULT_SLOT = "default"
SEND_FILE_MARKER = "TELEGRAM_SEND_FILE:"
MAX_AUTO_FILES = 5
MAX_AUTO_FILE_BYTES = 45 * 1024 * 1024
THREAD_LIST_SOURCE_KINDS = ("cli", "vscode", "exec", "appServer")
THREAD_PREVIEW_LIMIT = 50
WORKDIR_SESSION_SCOPES = {"here", "cwd", "workdir", "current"}


def format_thread_time(value: Any) -> str:
    if value in {None, ""}:
        return "?"
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10**12:
            timestamp /= 1000
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    return str(value)


def compact_text(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def thread_status_type(status: Any) -> str:
    if isinstance(status, dict):
        return str(status.get("type") or "unknown")
    return "unknown"


def thread_list_params(limit: int, cwd: Path | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "limit": limit,
        "sortKey": "updated_at",
        "archived": False,
        "sourceKinds": list(THREAD_LIST_SOURCE_KINDS),
    }
    if cwd is not None:
        params["cwd"] = str(cwd)
    return params


def session_list_cwd(scope: str, workdir: Path) -> Path | None:
    return workdir if scope.strip().lower() in WORKDIR_SESSION_SCOPES else None


def normalize_slot(slot: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in slot.strip())
    cleaned = cleaned.strip(".-_")
    if not cleaned:
        raise BridgeError("Session slot name cannot be empty.")
    return cleaned[:80]


def main_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "Status"}, {"text": "Sessions"}],
            [{"text": "New Session"}, {"text": "Cancel"}],
            [{"text": "Where"}, {"text": "Help"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def approval_keyboard(local_id: int) -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": f"Approve {local_id}"}, {"text": f"Deny {local_id}"}],
            [{"text": "Status"}, {"text": "Cancel"}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def button_to_command(text: str) -> str | None:
    normalized = text.strip().lower()
    mapping = {
        "status": "/status",
        "sessions": "/sessions",
        "new session": "/new",
        "cancel": "/cancel",
        "where": "/where",
        "help": "/help",
    }
    if normalized in mapping:
        return mapping[normalized]
    approve = re.fullmatch(r"approve\s+(\d+)", normalized)
    if approve:
        return f"/approve {approve.group(1)}"
    deny = re.fullmatch(r"deny\s+(\d+)", normalized)
    if deny:
        return f"/deny {deny.group(1)}"
    return None


@dataclass
class RemoteBridgeConfig:
    token: str
    codex_bin: str
    app_server_url: str | None
    app_server_args: list[str]
    workdir: Path
    state_dir: Path
    attachments_dir: Path
    allowed_chat_id: int | None
    pair_code: str
    approval_policy: str
    sandbox: str
    model: str | None
    model_provider: str | None
    allow_absolute_send: bool


@dataclass
class RemoteCodexJob:
    chat_id: int
    message_id: int
    prompt: str
    image_paths: list[Path] = field(default_factory=list)
    force_new: bool = False


@dataclass
class PendingApproval:
    local_id: int
    request_id: int | str
    method: str
    chat_id: int
    summary: str


@dataclass
class CodexTurnResult:
    text: str
    files_to_send: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class CodexThreadInfo:
    thread_id: str
    source: str
    status: str
    updated: str
    cwd: str
    preview: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CodexThreadInfo":
        preview = payload.get("name") or payload.get("preview") or ""
        return cls(
            thread_id=str(payload.get("id") or payload.get("sessionId") or "(unknown id)"),
            source=str(payload.get("source") or "unknown"),
            status=thread_status_type(payload.get("status")),
            updated=format_thread_time(payload.get("updatedAt") or payload.get("updated_at")),
            cwd=str(payload.get("cwd") or "(unknown cwd)"),
            preview=compact_text(str(preview), THREAD_PREVIEW_LIMIT) or "(no preview)",
        )

    def to_lines(self) -> list[str]:
        return [
            f"- {self.thread_id} [{self.source}/{self.status}] updated={self.updated}",
            f"  cwd: {self.cwd}",
            f"  {self.preview}",
        ]


def format_recent_threads_summary(raw_threads: list[dict[str, Any]], cwd: Path | None = None) -> str:
    scope = " for this workdir" if cwd is not None else ""
    if not raw_threads:
        return f"No Codex threads found{scope}."

    lines = [f"Recent Codex threads{scope}:"]
    for thread in raw_threads:
        lines.extend(CodexThreadInfo.from_payload(thread).to_lines())
    return "\n".join(lines)


class WebSocketConnection:
    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            raise BridgeError("Only ws:// app-server URLs are supported by the built-in client.")
        if not parsed.hostname or not parsed.port:
            raise BridgeError("CODEX_APP_SERVER_URL must include host and port, e.g. ws://127.0.0.1:8765")
        self.url = url
        self.host = parsed.hostname
        self.port = parsed.port
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_http_response()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise BridgeError(f"WebSocket handshake failed: {response[:200]!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if f"sec-websocket-accept: {expected}".lower().encode("ascii") not in response.lower():
            raise BridgeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept")
        self.sock.settimeout(None)

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str | None:
        chunks: list[bytes] = []
        while True:
            opcode, payload, fin = self._recv_frame()
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x0}:
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8")

    def _read_http_response(self) -> bytes:
        assert self.sock
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 65536:
                raise BridgeError("WebSocket handshake response is too large")
        return bytes(data)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self.sock
        first = 0x80 | opcode
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        with self.lock:
            self.sock.sendall(header + mask + masked)

    def _recv_frame(self) -> tuple[int, bytes, bool]:
        assert self.sock
        header = self._recv_exact(2)
        first, second = header
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload, fin

    def _recv_exact(self, size: int) -> bytes:
        assert self.sock
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise BridgeError("WebSocket connection closed")
            data.extend(chunk)
        return bytes(data)


class AppServerClient:
    def __init__(
        self,
        codex_bin: str,
        app_server_url: str | None,
        app_server_args: list[str],
        workdir: Path,
        on_notification: Callable[[dict[str, Any]], None],
        on_request: Callable[[dict[str, Any]], None],
    ) -> None:
        self.codex_bin = codex_bin
        self.app_server_url = app_server_url
        self.app_server_args = app_server_args
        self.workdir = workdir
        self.on_notification = on_notification
        self.on_request = on_request
        self.process: subprocess.Popen[str] | None = None
        self.write_lock = threading.Lock()
        self.next_id = 1
        self.pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.pending_lock = threading.Lock()
        self.closed = threading.Event()
        self.ws: WebSocketConnection | None = None

    def start(self) -> None:
        if self.app_server_url:
            self.ws = WebSocketConnection(self.app_server_url)
            log(f"Connecting to Codex app-server: {self.app_server_url}")
            self.ws.connect()
            threading.Thread(target=self._read_websocket, name="codex-app-websocket", daemon=True).start()
            self._initialize()
            return
        if self.process and self.process.poll() is None:
            return
        cmd = [self.codex_bin, "app-server", "--listen", "stdio://", *self.app_server_args]
        log("Starting Codex app-server: " + shlex.join(cmd[:4]) + " ...")
        self.process = subprocess.Popen(
            cmd,
            cwd=self.workdir,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, name="codex-app-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="codex-app-stderr", daemon=True).start()
        self._initialize()

    def _initialize(self) -> None:
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram-codex",
                    "title": "Telegram Codex",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")

    def stop(self) -> None:
        self.closed.set()
        if self.ws:
            self.ws.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def request(
        self,
        method: str,
        params: Any,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        request_id = self._next_request_id()
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.pending[request_id] = response_queue
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise BridgeError(f"Timed out waiting for app-server response to {method}") from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

        if "error" in response:
            raise BridgeError(f"app-server {method} failed: {response['error']}")
        return response.get("result")

    def notify(self, method: str, params: Any | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def respond(self, request_id: int | str, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def respond_error(self, request_id: int | str, message: str) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": message},
            }
        )

    def _next_request_id(self) -> int:
        with self.pending_lock:
            request_id = self.next_id
            self.next_id += 1
            return request_id

    def _send(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":"))
        if self.ws:
            self.ws.send_text(payload)
            return
        if not self.process or not self.process.stdin:
            raise BridgeError("Codex app-server is not running")
        with self.write_lock:
            self.process.stdin.write(payload + "\n")
            self.process.stdin.flush()

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" in message:
            self.on_request(message)
        elif "id" in message:
            with self.pending_lock:
                response_queue = self.pending.get(message["id"])
            if response_queue:
                response_queue.put(message)
            else:
                log(f"Unexpected app-server response id: {message.get('id')}")
        elif "method" in message:
            self.on_notification(message)
        else:
            log(f"Unexpected app-server message: {message}")

    def _read_websocket(self) -> None:
        assert self.ws
        while not self.closed.is_set():
            try:
                text = self.ws.recv_text()
            except Exception as exc:  # noqa: BLE001
                log(f"Codex app-server websocket read failed: {exc}")
                break
            if text is None:
                break
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                log(f"Unparseable app-server websocket message: {text[:300]}")
                continue
            self._dispatch_message(message)
        self.closed.set()

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                log(f"Unparseable app-server stdout: {line[:300]}")
                continue
            self._dispatch_message(message)
        self.closed.set()

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            line = line.strip()
            if line:
                log(f"codex app-server: {line}")


class RemoteCodexRunner:
    def __init__(
        self,
        config: RemoteBridgeConfig,
        telegram: TelegramClient,
        state: StateStore,
    ) -> None:
        self.config = config
        self.telegram = telegram
        self.state = state
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.jobs: queue.Queue[RemoteCodexJob | None] = queue.Queue()
        self.client = AppServerClient(
            config.codex_bin,
            config.app_server_url,
            config.app_server_args,
            config.workdir,
            on_notification=self.events.put,
            on_request=self.handle_server_request,
        )
        self.active_slot = str(self.state.data.get("active_slot") or DEFAULT_SLOT)
        self.thread_id: str | None = self._slot_thread_id(self.active_slot)
        self.force_new_next = False
        self.active_turn_id: str | None = None
        self.active_chat_id: int | None = None
        self.pending_approvals: dict[int, PendingApproval] = {}
        self.pending_lock = threading.Lock()
        self.next_approval_id = 1
        self.client.start()
        threading.Thread(target=self._worker, name="codex-remote-worker", daemon=True).start()

    def enqueue(self, job: RemoteCodexJob) -> int:
        pending = self.jobs.qsize()
        self.jobs.put(job)
        return pending + 1

    def start_new_next(self, slot: str | None = None) -> str:
        if slot:
            self.set_active_slot(slot)
        self.thread_id = None
        self.force_new_next = True
        self._set_slot_thread_id(self.active_slot, None)
        self.state.save()
        return self.active_slot

    def set_active_slot(self, slot: str) -> None:
        slot = normalize_slot(slot)
        self.active_slot = slot
        self.state.data["active_slot"] = slot
        self.thread_id = self._slot_thread_id(slot)
        self.force_new_next = False
        self.state.save()

    def bind_thread(self, thread_id: str, slot: str | None = None) -> str:
        if slot:
            self.set_active_slot(slot)
        self.thread_id = thread_id
        self._set_slot_thread_id(self.active_slot, thread_id)
        self.force_new_next = False
        self._save_thread_id()
        return self.active_slot

    def slot_summary(self) -> str:
        slots = self._slots()
        if not slots:
            return "No saved Telegram session slots yet."
        lines = ["Telegram session slots:"]
        for slot in sorted(slots):
            marker = "*" if slot == self.active_slot else "-"
            thread_id = slots[slot] or "(new on next prompt)"
            lines.append(f"{marker} {slot}: {thread_id}")
        return "\n".join(lines)

    def recent_threads_summary(self, limit: int = 10, cwd: Path | None = None) -> str:
        try:
            result = self.client.request("thread/list", thread_list_params(limit, cwd), timeout=15)
        except Exception as exc:  # noqa: BLE001
            return f"Could not list Codex threads: {exc}"
        return format_recent_threads_summary(result.get("data") or [], cwd)

    def _slots(self) -> dict[str, str | None]:
        slots = self.state.data.get("session_slots")
        if not isinstance(slots, dict):
            slots = {}
            legacy_thread_id = self.state.data.get("codex_thread_id")
            if legacy_thread_id:
                slots[DEFAULT_SLOT] = legacy_thread_id
            self.state.data["session_slots"] = slots
        return slots

    def _slot_thread_id(self, slot: str) -> str | None:
        value = self._slots().get(slot)
        return str(value) if value else None

    def _set_slot_thread_id(self, slot: str, thread_id: str | None) -> None:
        slots = self._slots()
        if thread_id:
            slots[slot] = thread_id
        else:
            slots[slot] = None

    def interrupt(self) -> None:
        if self.thread_id and self.active_turn_id:
            self.client.request(
                "turn/interrupt",
                {"threadId": self.thread_id, "turnId": self.active_turn_id},
                timeout=15,
            )

    def approve(self, local_id: int, accepted: bool) -> bool:
        with self.pending_lock:
            approval = self.pending_approvals.pop(local_id, None)
        if not approval:
            return False
        result = self._approval_result(approval.method, accepted)
        self.client.respond(approval.request_id, result)
        self.telegram.send_message(
            approval.chat_id,
            f"Approval #{local_id} {'accepted' if accepted else 'declined'}.",
        )
        return True

    def _approval_result(self, method: str, accepted: bool) -> dict[str, Any]:
        if method == "item/commandExecution/requestApproval":
            return {"decision": "accept" if accepted else "decline"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "accept" if accepted else "decline"}
        if method == "execCommandApproval":
            return {"decision": "approved" if accepted else "denied"}
        if method == "applyPatchApproval":
            return {"decision": "approved" if accepted else "denied"}
        return {"decision": "accept" if accepted else "decline"}

    def handle_server_request(self, request: dict[str, Any]) -> None:
        method = request.get("method", "")
        request_id = request["id"]
        params = request.get("params") or {}
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "execCommandApproval",
            "applyPatchApproval",
        }:
            self._queue_approval(request_id, method, params)
            return
        if method == "item/tool/requestUserInput":
            self.client.respond(request_id, {"answers": {}})
            return
        if method == "mcpServer/elicitation/request":
            self.client.respond(request_id, {"action": "decline", "content": None, "_meta": None})
            return
        if method == "item/tool/call":
            self.client.respond(
                request_id,
                {
                    "contentItems": [
                        {"type": "inputText", "text": "Dynamic tool calls are not handled by Telegram bridge."}
                    ],
                    "success": False,
                },
            )
            return
        self.client.respond_error(request_id, f"Unsupported app-server request: {method}")

    def _queue_approval(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        chat_id = self.active_chat_id
        if chat_id is None:
            self.client.respond(request_id, self._approval_result(method, accepted=False))
            return

        summary = self._approval_summary(method, params)
        with self.pending_lock:
            local_id = self.next_approval_id
            self.next_approval_id += 1
            self.pending_approvals[local_id] = PendingApproval(
                local_id=local_id,
                request_id=request_id,
                method=method,
                chat_id=chat_id,
                summary=summary,
            )
        self.telegram.send_message(
            chat_id,
            "\n".join(
                [
                    f"Codex approval required #{local_id}",
                    summary,
                    "",
                    f"/approve {local_id}",
                    f"/deny {local_id}",
                ]
            ),
            reply_markup=approval_keyboard(local_id),
        )

    def _approval_summary(self, method: str, params: dict[str, Any]) -> str:
        if "command" in params and params.get("command"):
            command = params["command"]
            if isinstance(command, list):
                command = shlex.join(str(part) for part in command)
            lines = [f"command: {command}"]
            if params.get("cwd"):
                lines.append(f"cwd: {params['cwd']}")
            if params.get("reason"):
                lines.append(f"reason: {params['reason']}")
            return "\n".join(lines)
        if params.get("grantRoot"):
            return f"file write access: {params['grantRoot']}"
        if params.get("reason"):
            return str(params["reason"])
        return method

    def _worker(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                result = self.run_turn(job)
                self.telegram.send_message(job.chat_id, result.text, job.message_id, reply_markup=main_keyboard())
                self.send_marked_files(job.chat_id, result.files_to_send)
            except Exception as exc:  # noqa: BLE001
                log(f"Codex remote job failed: {exc}")
                self.telegram.send_message(
                    job.chat_id,
                    f"Codex remote run failed:\n{exc}",
                    job.message_id,
                    reply_markup=main_keyboard(),
                )
            finally:
                self.active_turn_id = None
                self.active_chat_id = None
                self.jobs.task_done()

    def ensure_thread(self, force_new: bool = False) -> str:
        self.thread_id = self._slot_thread_id(self.active_slot)
        if not force_new and not self.force_new_next and self.thread_id:
            try:
                result = self.client.request("thread/resume", self._thread_resume_params(self.thread_id))
                self.thread_id = result["thread"]["id"]
                self._save_thread_id()
                return self.thread_id
            except BridgeError as exc:
                log(f"Could not resume Codex thread {self.thread_id}: {exc}; starting a new thread")

        result = self.client.request("thread/start", self._thread_start_params())
        self.thread_id = result["thread"]["id"]
        self.force_new_next = False
        self._save_thread_id()
        return self.thread_id

    def _save_thread_id(self) -> None:
        if self.thread_id:
            self._set_slot_thread_id(self.active_slot, self.thread_id)
            self.state.data["codex_thread_id"] = self.thread_id
            self.state.data["active_slot"] = self.active_slot
            self.state.save()

    def _thread_start_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(self.config.workdir),
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
            "experimentalRawEvents": False,
            "persistExtendedHistory": True,
        }
        if self.config.model:
            params["model"] = self.config.model
        if self.config.model_provider:
            params["modelProvider"] = self.config.model_provider
        return params

    def _thread_resume_params(self, thread_id: str) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(self.config.workdir),
            "approvalPolicy": self.config.approval_policy,
            "sandbox": self.config.sandbox,
            "persistExtendedHistory": True,
        }
        if self.config.model:
            params["model"] = self.config.model
        if self.config.model_provider:
            params["modelProvider"] = self.config.model_provider
        return params

    def run_turn(self, job: RemoteCodexJob) -> CodexTurnResult:
        thread_id = self.ensure_thread(force_new=job.force_new)
        input_items = build_user_input(job.prompt, job.image_paths)
        self.active_chat_id = job.chat_id
        self.telegram.send_chat_action(job.chat_id)
        result = self.client.request(
            "turn/start",
            {"threadId": thread_id, "input": input_items},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        turn_id = result["turn"]["id"]
        self.active_turn_id = turn_id
        text = self._collect_turn_response(thread_id, turn_id)
        cleaned_text, files_to_send = extract_send_file_markers(
            text,
            workdir=self.config.workdir,
            allow_absolute=self.config.allow_absolute_send,
        )
        return CodexTurnResult(text=cleaned_text, files_to_send=files_to_send)

    def send_marked_files(self, chat_id: int, paths: list[Path]) -> None:
        sent = 0
        for path in paths:
            if sent >= MAX_AUTO_FILES:
                self.telegram.send_message(chat_id, f"Skipped remaining files after {MAX_AUTO_FILES} automatic sends.")
                return
            if not path.is_file():
                self.telegram.send_message(chat_id, f"Marked file not found: {path}")
                continue
            if path.stat().st_size > MAX_AUTO_FILE_BYTES:
                self.telegram.send_message(chat_id, f"Marked file is too large to auto-send: {path}")
                continue
            self.telegram.send_file(chat_id, path)
            sent += 1

    def _collect_turn_response(self, thread_id: str, turn_id: str) -> str:
        chunks: list[str] = []
        deadline = time.time() + TURN_TIMEOUT_SECONDS
        while time.time() < deadline:
            try:
                event = self.events.get(timeout=1)
            except queue.Empty:
                continue
            method = event.get("method")
            params = event.get("params") or {}
            if params.get("threadId") != thread_id:
                continue
            if method == "item/agentMessage/delta" and params.get("turnId") == turn_id:
                chunks.append(params.get("delta", ""))
            elif method == "error" and params.get("turnId") == turn_id:
                error = params.get("error") or {}
                chunks.append(f"\nCodex error: {error}")
            elif method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                turn = params.get("turn") or {}
                status = turn.get("status")
                text = "".join(chunks).strip()
                if status == "failed":
                    return text or f"Codex turn failed: {turn.get('error')}"
                if status == "interrupted":
                    return text or "Codex turn was interrupted."
                return text or "Codex turn completed without a final message."
        raise BridgeError("Timed out waiting for Codex turn to complete")


def build_user_input(prompt: str, image_paths: list[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if prompt.strip():
        items.append({"type": "text", "text": prompt, "text_elements": []})
    for path in image_paths:
        items.append({"type": "localImage", "path": str(path)})
    return items


def extract_send_file_markers(text: str, workdir: Path, allow_absolute: bool) -> tuple[str, list[Path]]:
    kept_lines: list[str] = []
    paths: list[Path] = []
    for line in text.splitlines():
        if not line.strip().startswith(SEND_FILE_MARKER):
            kept_lines.append(line)
            continue
        raw_path = line.split(SEND_FILE_MARKER, 1)[1].strip().strip("\"'")
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = workdir / path
        path = path.resolve()
        if allow_absolute or path.is_relative_to(workdir):
            paths.append(path)
        else:
            kept_lines.append(f"Skipped unsafe file path outside CODEX_WORKDIR: {path}")
    return "\n".join(kept_lines).strip() or "Done.", paths


class TelegramRemoteCodexBridge:
    def __init__(self, config: RemoteBridgeConfig) -> None:
        self.config = config
        self.telegram = TelegramClient(config.token)
        self.state = StateStore(config.state_dir / "remote-state.json")
        self.runner = RemoteCodexRunner(config, self.telegram, self.state)

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

    def run(self) -> None:
        if self.config.allowed_chat_id is not None:
            log(f"Allowed chat id: {self.config.allowed_chat_id}")
        elif self.state.paired_chat_id is None:
            log(f"Send this to your bot from the authorized Telegram chat: /pair {self.config.pair_code}")
        else:
            log(f"Paired chat id: {self.state.paired_chat_id}")
        log(f"Workdir: {self.config.workdir}")
        log("Remote bridge polling Telegram. Press Ctrl-C to stop.")
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

    def send_main(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        self.telegram.send_message(chat_id, text, reply_to, reply_markup=main_keyboard())

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
        button_command = button_to_command(text)
        if button_command:
            text = button_command
        if text.startswith("/") and self.handle_command(chat_id, int(message["message_id"]), text):
            return

        attachments = self.download_attachments(message)
        prompt = self.build_prompt(message, attachments)
        if not prompt.strip() and not attachments:
            self.telegram.send_message(chat_id, "Send text, a photo, or a document to forward it to Codex.")
            return
        image_paths = [item.path for item in attachments if is_image_path(item.path, item.mime_type)]
        queue_position = self.runner.enqueue(
            RemoteCodexJob(
                chat_id=chat_id,
                message_id=int(message["message_id"]),
                prompt=prompt,
                image_paths=image_paths,
                force_new=self.runner.force_new_next,
            )
        )
        if queue_position > 1:
            self.telegram.send_message(chat_id, f"Queued for Codex. Queue position: {queue_position}.")
        else:
            self.telegram.send_message(
                chat_id,
                f"Received. Running Codex in slot `{self.runner.active_slot}`.",
                int(message["message_id"]),
                reply_markup=main_keyboard(),
            )

    def handle_command(self, chat_id: int, message_id: int, text: str) -> bool:
        command, _, arg = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            self.send_main(chat_id, self.help_text(), message_id)
            return True
        if command == "/status":
            self.send_main(chat_id, self.status_text(), message_id)
            return True
        if command == "/sessions":
            cwd = session_list_cwd(arg, self.config.workdir)
            self.send_main(
                chat_id,
                self.runner.slot_summary() + "\n\n" + self.runner.recent_threads_summary(cwd=cwd),
                message_id,
            )
            return True
        if command == "/new":
            slot = arg.strip().split()[0] if arg.strip() else None
            active_slot = self.runner.start_new_next(slot)
            self.send_main(
                chat_id,
                f"Next prompt will start a fresh Codex thread in slot `{active_slot}`.",
                message_id,
            )
            return True
        if command == "/resume":
            if arg.strip():
                self.handle_use(chat_id, message_id, arg)
                return True
            self.runner.force_new_next = False
            self.send_main(
                chat_id,
                f"Next prompt will use slot `{self.runner.active_slot}`.",
                message_id,
            )
            return True
        if command == "/use":
            self.handle_use(chat_id, message_id, arg)
            return True
        if command == "/bind":
            self.handle_bind(chat_id, message_id, arg)
            return True
        if command in {"/cancel", "/stop"}:
            try:
                self.runner.interrupt()
                self.send_main(
                    chat_id,
                    "Cancel requested for the active Codex turn.",
                    message_id,
                )
            except Exception as exc:  # noqa: BLE001
                self.send_main(chat_id, f"Cancel failed: {exc}", message_id)
            return True
        if command == "/approve":
            self.handle_approval(chat_id, message_id, arg, accepted=True)
            return True
        if command in {"/deny", "/decline"}:
            self.handle_approval(chat_id, message_id, arg, accepted=False)
            return True
        if command == "/where":
            self.send_main(
                chat_id,
                f"workdir: {self.config.workdir}\nattachments: {self.config.attachments_dir}",
                message_id,
            )
            return True
        if command == "/sendfile":
            self.handle_sendfile(chat_id, message_id, arg)
            return True
        return False

    def handle_use(self, chat_id: int, message_id: int, arg: str) -> None:
        slot = arg.strip().split()[0] if arg.strip() else ""
        if not slot:
            self.send_main(chat_id, "Usage: /use <slot>", message_id)
            return
        normalized = normalize_slot(slot)
        if normalized not in self.runner._slots():
            self.send_main(
                chat_id,
                f"No saved slot `{normalized}`. Use /new {normalized} to create it, or /bind <thread_id> {normalized} to bind an existing Codex thread.",
                message_id,
            )
            return
        self.runner.set_active_slot(normalized)
        thread_id = self.runner.thread_id or "(new on next prompt)"
        self.send_main(
            chat_id,
            f"Using slot `{normalized}`: {thread_id}",
            message_id,
        )

    def handle_bind(self, chat_id: int, message_id: int, arg: str) -> None:
        parts = arg.strip().split()
        if not parts:
            self.send_main(chat_id, "Usage: /bind <thread_id> [slot]", message_id)
            return
        thread_id = parts[0]
        slot = parts[1] if len(parts) > 1 else None
        active_slot = self.runner.bind_thread(thread_id, slot)
        self.send_main(
            chat_id,
            f"Bound slot `{active_slot}` to Codex thread {thread_id}.",
            message_id,
        )

    def handle_approval(self, chat_id: int, message_id: int, arg: str, accepted: bool) -> None:
        try:
            local_id = int(arg.strip())
        except ValueError:
            self.send_main(
                chat_id,
                "Usage: /approve <id> or /deny <id>",
                message_id,
            )
            return
        if not self.runner.approve(local_id, accepted):
            self.send_main(chat_id, f"No pending approval #{local_id}.", message_id)

    def handle_sendfile(self, chat_id: int, message_id: int, arg: str) -> None:
        if not arg.strip():
            self.send_main(chat_id, "Usage: /sendfile <path> [caption]", message_id)
            return
        try:
            parts = shlex.split(arg)
        except ValueError as exc:
            self.send_main(
                chat_id,
                f"Could not parse /sendfile arguments: {exc}",
                message_id,
            )
            return
        path = Path(parts[0]).expanduser()
        if not path.is_absolute():
            path = self.config.workdir / path
        path = path.resolve()
        if not self.config.allow_absolute_send and not path.is_relative_to(self.config.workdir):
            self.send_main(
                chat_id,
                "Refusing to send files outside CODEX_WORKDIR. Set TELEGRAM_ALLOW_ABSOLUTE_SEND=1 to allow it.",
                message_id,
            )
            return
        if not path.is_file():
            self.send_main(chat_id, f"File not found: {path}", message_id)
            return
        self.telegram.send_file(chat_id, path, " ".join(parts[1:]))

    def status_text(self) -> str:
        pending = sorted(self.runner.pending_approvals)
        return "\n".join(
            [
                "Telegram Codex remote bridge is running.",
                f"active_slot: {self.runner.active_slot}",
                f"thread_id: {self.runner.thread_id or '(none yet)'}",
                f"workdir: {self.config.workdir}",
                f"approval_policy: {self.config.approval_policy}",
                f"sandbox: {self.config.sandbox}",
                f"queued jobs: {self.runner.jobs.qsize()}",
                f"pending approvals: {pending or 'none'}",
            ]
        )

    def help_text(self) -> str:
        return "\n".join(
            [
                "Commands:",
                "/help - show this help",
                "/status - bridge status",
                "/sessions [here] - list Telegram slots and recent Codex threads",
                "/new [slot] - start a fresh Codex thread in the current or named slot",
                "/use <slot> - switch Telegram to a saved session slot",
                "/bind <thread_id> [slot] - bind Telegram to an existing Codex thread",
                "/resume [slot] - use the current or named saved slot",
                "/cancel - interrupt the active Codex turn",
                "/approve <id> - approve a Codex command/file request",
                "/deny <id> - decline a Codex command/file request",
                "/where - show work and attachment directories",
                "/sendfile <path> [caption] - send a local image/PDF/file back to Telegram",
                "",
                "Text is sent with turn/start. Photos are sent as localImage inputs. PDFs and other documents are downloaded and passed as local file paths in the text prompt.",
            ]
        )

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

        non_images = [item for item in attachments if not is_image_path(item.path, item.mime_type)]
        if non_images:
            lines = ["Documents downloaded from Telegram:"]
            for item in non_images:
                label = item.original_name or item.path.name
                lines.append(f"- {item.kind}: {item.path} ({label})")
            lines.append("Use these local file paths directly when they are relevant.")
            parts.append("\n".join(lines))

        if attachments and not text:
            parts.append("Please inspect or use the attached file(s) as context for the next step.")

        parts.append(
            "Telegram bridge capability:\n"
            f"- To send a local file back to Telegram, put one line in your final answer as `{SEND_FILE_MARKER} path/to/file`.\n"
            "- The bridge will upload marked files automatically; do not use the marker unless the user asked to receive that file."
        )
        return "\n\n".join(parts)


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


def build_config(args: argparse.Namespace) -> RemoteBridgeConfig:
    token = args.token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise BridgeError("TELEGRAM_BOT_TOKEN is required.")

    workdir = Path(args.workdir or os.getenv("CODEX_WORKDIR") or os.getcwd()).expanduser().resolve()
    state_dir = Path(os.getenv("TELEGRAM_CODEX_STATE_DIR", workdir / DEFAULT_STATE_DIR)).expanduser().resolve()
    attachments_dir = Path(
        os.getenv("TELEGRAM_ATTACHMENTS_DIR", state_dir / "attachments")
    ).expanduser().resolve()

    raw_chat = args.allowed_chat_id or os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
    allowed_chat = int(raw_chat) if raw_chat else None
    pair_code = os.getenv("TELEGRAM_PAIR_CODE") or secrets.token_hex(3)

    return RemoteBridgeConfig(
        token=token,
        codex_bin=find_codex_bin(),
        app_server_url=os.getenv("CODEX_APP_SERVER_URL"),
        app_server_args=shlex.split(os.getenv("CODEX_APP_SERVER_ARGS", "")),
        workdir=workdir,
        state_dir=state_dir,
        attachments_dir=attachments_dir,
        allowed_chat_id=allowed_chat,
        pair_code=pair_code,
        approval_policy=os.getenv("CODEX_APPROVAL_POLICY", "on-request"),
        sandbox=os.getenv("CODEX_SANDBOX", "workspace-write"),
        model=os.getenv("CODEX_MODEL"),
        model_provider=os.getenv("CODEX_MODEL_PROVIDER"),
        allow_absolute_send=env_bool("TELEGRAM_ALLOW_ABSOLUTE_SEND"),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remote-control Codex app-server from Telegram.")
    parser.add_argument("--token", help="Telegram bot token. Defaults to TELEGRAM_BOT_TOKEN.")
    parser.add_argument("--allowed-chat-id", help="Only accept this Telegram chat id.")
    parser.add_argument("--workdir", help="Codex working directory. Defaults to current directory.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        config = build_config(parse_args(argv or sys.argv[1:]))
        TelegramRemoteCodexBridge(config).run()
    except KeyboardInterrupt:
        log("Stopped.")
        return 0
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
