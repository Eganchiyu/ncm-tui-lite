"""基于 mpv JSON IPC 的流媒体音频控制器。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable

import ncm


@dataclass
class PlayerState:
    playing: bool = False
    paused: bool = False
    buffering: bool = False
    time_pos: float = 0.0
    duration: float = 0.0
    volume: float = 100.0


class MpvPlayer:
    def __init__(
        self,
        on_track_end: Callable[[str], None] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self._on_track_end = on_track_end
        self._sock_path = os.path.join(tempfile.gettempdir(), f"ncm_mpv_{os.getpid()}_{id(self)}.sock")
        if os.path.exists(self._sock_path):
            os.unlink(self._sock_path)

        cmd = [
            "mpv",
            "--no-video",
            "--idle=yes",
            "--no-terminal",
            "--really-quiet",
            "--cache=yes",
            "--cache-secs=30",
            "--demuxer-max-bytes=64MiB",
            "--demuxer-readahead-secs=20",
            f"--user-agent={ncm.UA_WEB}",
            f"--referrer={ncm.REFERER}",
            f"--input-ipc-server={self._sock_path}",
        ]
        if extra_args:
            cmd.extend(extra_args)

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if os.path.exists(self._sock_path):
                break
            if self._proc.poll() is not None:
                raise RuntimeError("mpv 启动失败")
            time.sleep(0.03)
        if not os.path.exists(self._sock_path):
            self._proc.terminate()
            raise RuntimeError("等待 mpv IPC socket 超时")

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self._sock_path)
        self._reader_file = self._sock.makefile("r", encoding="utf-8", errors="replace")

        self._lock = threading.Lock()
        self._req_id = 0
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._closed = False
        self._state = PlayerState()
        self._current_entry_id: int | None = None

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

        # 订阅核心属性变化
        for oid, prop in (
            (1, "time-pos"),
            (2, "duration"),
            (3, "pause"),
            (4, "paused-for-cache"),
            (5, "volume"),
            (6, "idle-active"),
        ):
            self._command(["observe_property", oid, prop])

    def _read_loop(self) -> None:
        try:
            for line in self._reader_file:
                if self._closed:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                req_id = msg.get("request_id")
                if req_id is not None:
                    with self._lock:
                        slot = self._pending.get(req_id)
                        if slot is not None:
                            ev, box = slot
                            box.update(msg)
                            ev.set()
                    continue

                event = msg.get("event")
                if event == "property-change":
                    name = msg.get("name")
                    val = msg.get("data")
                    with self._lock:
                        if name == "time-pos" and isinstance(val, (int, float)):
                            self._state.time_pos = max(0.0, float(val))
                        elif name == "duration" and isinstance(val, (int, float)):
                            self._state.duration = max(0.0, float(val))
                        elif name == "pause" and isinstance(val, bool):
                            self._state.paused = val
                        elif name == "paused-for-cache" and isinstance(val, bool):
                            self._state.buffering = val
                        elif name == "volume" and isinstance(val, (int, float)):
                            self._state.volume = float(val)
                        elif name == "idle-active" and isinstance(val, bool):
                            self._state.playing = not val
                elif event == "end-file":
                    reason = msg.get("reason", "")
                    eid = msg.get("playlist_entry_id")
                    with self._lock:
                        is_current = self._current_entry_id is None or eid == self._current_entry_id
                        if reason in ("eof", "error"):
                            self._state.playing = False
                            self._state.buffering = False
                    if is_current and reason in ("eof", "error") and self._on_track_end:
                        try:
                            self._on_track_end(reason)
                        except Exception:  # noqa: BLE001
                            pass
        except Exception:  # noqa: BLE001
            pass

    def _command(self, cmd: list, timeout: float = 3.0) -> dict:
        if self._closed:
            return {}
        ev = threading.Event()
        box: dict = {}
        with self._lock:
            self._req_id += 1
            rid = self._req_id
            self._pending[rid] = (ev, box)
            payload = (json.dumps({"command": cmd, "request_id": rid}) + "\n").encode()
            try:
                self._sock.sendall(payload)
            except OSError:
                self._pending.pop(rid, None)
                return {}
        ev.wait(timeout=timeout)
        with self._lock:
            self._pending.pop(rid, None)
        return box

    def play(self, url: str) -> bool:
        with self._lock:
            self._state.time_pos = 0.0
            self._state.duration = 0.0
            self._state.buffering = True
            self._state.paused = False
        self._command(["set_property", "pause", False])
        resp = self._command(["loadfile", url, "replace"], timeout=5.0)
        if resp.get("error") == "success":
            eid = (resp.get("data") or {}).get("playlist_entry_id")
            with self._lock:
                self._current_entry_id = eid
                self._state.playing = True
            return True
        return False

    def set_pause(self, paused: bool) -> None:
        self._command(["set_property", "pause", paused])

    def toggle_pause(self) -> bool:
        with self._lock:
            target = not self._state.paused
        self.set_pause(target)
        return target

    def seek_relative(self, delta: float) -> None:
        self._command(["seek", delta, "relative"])

    def seek_to(self, seconds: float) -> None:
        self._command(["seek", max(0.0, seconds), "absolute"])

    def set_volume(self, vol: float) -> float:
        clamped = max(0.0, min(100.0, vol))
        self._command(["set_property", "volume", clamped])
        return clamped

    def add_volume(self, delta: float) -> float:
        with self._lock:
            cur = self._state.volume
        return self.set_volume(cur + delta)

    def stop(self) -> None:
        self._command(["stop"])
        with self._lock:
            self._state.playing = False
            self._state.buffering = False
            self._state.time_pos = 0.0

    def get_state(self) -> PlayerState:
        with self._lock:
            return PlayerState(
                playing=self._state.playing,
                paused=self._state.paused,
                buffering=self._state.buffering,
                time_pos=self._state.time_pos,
                duration=self._state.duration,
                volume=self._state.volume,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.sendall(b'{"command":["quit"]}\n')
        except OSError:
            pass
        try:
            self._reader_file.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass
        if self._proc.poll() is None:
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        if os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass
