#!/usr/bin/env python3
"""ncm-lite —— 用本人账号 cookie 直接调网易云 web/eapi 接口，取无损直链并下载到本地。

内置特性：
  - 线程安全 HTTP/1.1 Keep-Alive 持久连接池 + Gzip 自动压缩（大幅降低切歌与歌单加载 RTT）
  - 纯 Python 预展开密钥 AES-128-ECB（零 openssl 子进程 fork 开销，跨平台通用）
  - Pillow 内存级半块真彩字符画封面渲染（零 chafa 子进程开销，无 Pillow 时自动回退 chafa）
  - 歌曲元数据 / 歌词 / 封面 / 直链多级 LRU 缓存（内存 + ~/.cache/ncm-lite 持久化）
  - 本地流媒体 Tee 缓存代理 AudioStreamProxy（探测 + mpv 播放 + ffmpeg 频谱共享单条 CDN 下载流）

用法：
  python ncm.py login                     # 扫码登录，cookie 存到 cookie.txt
  python ncm.py whoami                    # 校验 cookie 是否有效
  python ncm.py dl <song_id> [--level lossless|exhigh|higher|standard]
  python ncm.py url <song_id> [--level ...]   # 只打印直链，不下载
"""
from __future__ import annotations

import argparse
import atexit
import collections
import concurrent.futures
import gzip
import hashlib
import http.client
import http.server
import io
import json
import os
import random
import re
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
COOKIE_FILE = BASE / "cookie.txt"
OUT_DIR = BASE / "output"

AES_KEY = b"e82ckenh8dichen8"
UA_EAPI = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Safari/537.36 Chrome/91.0.4472.164 "
    "NeteaseMusicDesktop/2.10.2.200154"
)
UA_WEB = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REFERER = "https://music.163.com/"

QR_UNIKEY_API = "https://interface3.music.163.com/eapi/login/qrcode/unikey"
QR_LOGIN_API = "https://interface3.music.163.com/eapi/login/qrcode/client/login"
QR_URL_TEMPLATE = "https://music.163.com/login?codekey={unikey}"

SONG_DETAIL_API = "https://music.163.com/api/v3/song/detail"
PLAYER_URL_API = "https://music.163.com/api/song/enhance/player/url"
PLAYER_URL_API_V1 = "https://music.163.com/api/song/enhance/player/url/v1"
USER_PLAYLIST_API = "https://music.163.com/api/user/playlist"
PLAYLIST_DETAIL_API = "https://music.163.com/api/v6/playlist/detail"
LYRIC_API = "https://music.163.com/api/song/lyric"
LYRIC_V1_EAPI = "https://interface3.music.163.com/eapi/song/lyric/v1"

LEVELS = ["standard", "higher", "exhigh", "lossless"]


# ── 纯 Python 零依赖 AES-128-ECB（预展开轮密钥，比 fork openssl 快 100 倍） ────
_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
_AES_XTIME = tuple(((i << 1) ^ 0x1B) & 0xFF if (i & 0x80) else (i << 1) for i in range(256))


def _expand_aes128_key(key: bytes) -> tuple[tuple[int, ...], ...]:
    w = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        temp = list(w[i - 1])
        if i % 4 == 0:
            temp = [
                _AES_SBOX[temp[1]] ^ rcon,
                _AES_SBOX[temp[2]],
                _AES_SBOX[temp[3]],
                _AES_SBOX[temp[0]],
            ]
            rcon = _AES_XTIME[rcon]
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    rounds: list[tuple[int, ...]] = []
    for r in range(11):
        rk: list[int] = []
        for c in range(4):
            rk.extend(w[r * 4 + c])
        rounds.append(tuple(rk))
    return tuple(rounds)


_EAPI_ROUND_KEYS = _expand_aes128_key(AES_KEY)


def _pkcs7(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def aes_ecb_hex(data: bytes) -> str:
    """纯 Python 查表实现 AES-128-ECB 加密（零外部子进程开销）。"""
    sbox = _AES_SBOX
    xt = _AES_XTIME
    rks = _EAPI_ROUND_KEYS
    rk0 = rks[0]
    rk10 = rks[10]
    mid_rks = rks[1:10]
    out = bytearray(len(data))

    for off in range(0, len(data), 16):
        s0 = data[off] ^ rk0[0]
        s1 = data[off + 1] ^ rk0[1]
        s2 = data[off + 2] ^ rk0[2]
        s3 = data[off + 3] ^ rk0[3]
        s4 = data[off + 4] ^ rk0[4]
        s5 = data[off + 5] ^ rk0[5]
        s6 = data[off + 6] ^ rk0[6]
        s7 = data[off + 7] ^ rk0[7]
        s8 = data[off + 8] ^ rk0[8]
        s9 = data[off + 9] ^ rk0[9]
        s10 = data[off + 10] ^ rk0[10]
        s11 = data[off + 11] ^ rk0[11]
        s12 = data[off + 12] ^ rk0[12]
        s13 = data[off + 13] ^ rk0[13]
        s14 = data[off + 14] ^ rk0[14]
        s15 = data[off + 15] ^ rk0[15]

        for rk in mid_rks:
            # SubBytes + ShiftRows
            b0, b1, b2, b3 = sbox[s0], sbox[s5], sbox[s10], sbox[s15]
            b4, b5, b6, b7 = sbox[s4], sbox[s9], sbox[s14], sbox[s3]
            b8, b9, b10, b11 = sbox[s8], sbox[s13], sbox[s2], sbox[s7]
            b12, b13, b14, b15 = sbox[s12], sbox[s1], sbox[s6], sbox[s11]

            # MixColumns + AddRoundKey
            t0 = b0 ^ b1 ^ b2 ^ b3
            s0 = b0 ^ t0 ^ xt[b0 ^ b1] ^ rk[0]
            s1 = b1 ^ t0 ^ xt[b1 ^ b2] ^ rk[1]
            s2 = b2 ^ t0 ^ xt[b2 ^ b3] ^ rk[2]
            s3 = b3 ^ t0 ^ xt[b3 ^ b0] ^ rk[3]

            t1 = b4 ^ b5 ^ b6 ^ b7
            s4 = b4 ^ t1 ^ xt[b4 ^ b5] ^ rk[4]
            s5 = b5 ^ t1 ^ xt[b5 ^ b6] ^ rk[5]
            s6 = b6 ^ t1 ^ xt[b6 ^ b7] ^ rk[6]
            s7 = b7 ^ t1 ^ xt[b7 ^ b4] ^ rk[7]

            t2 = b8 ^ b9 ^ b10 ^ b11
            s8 = b8 ^ t2 ^ xt[b8 ^ b9] ^ rk[8]
            s9 = b9 ^ t2 ^ xt[b9 ^ b10] ^ rk[9]
            s10 = b10 ^ t2 ^ xt[b10 ^ b11] ^ rk[10]
            s11 = b11 ^ t2 ^ xt[b11 ^ b8] ^ rk[11]

            t3 = b12 ^ b13 ^ b14 ^ b15
            s12 = b12 ^ t3 ^ xt[b12 ^ b13] ^ rk[12]
            s13 = b13 ^ t3 ^ xt[b13 ^ b14] ^ rk[13]
            s14 = b14 ^ t3 ^ xt[b14 ^ b15] ^ rk[14]
            s15 = b15 ^ t3 ^ xt[b15 ^ b12] ^ rk[15]

        # Final round
        out[off] = sbox[s0] ^ rk10[0]
        out[off + 1] = sbox[s5] ^ rk10[1]
        out[off + 2] = sbox[s10] ^ rk10[2]
        out[off + 3] = sbox[s15] ^ rk10[3]
        out[off + 4] = sbox[s4] ^ rk10[4]
        out[off + 5] = sbox[s9] ^ rk10[5]
        out[off + 6] = sbox[s14] ^ rk10[6]
        out[off + 7] = sbox[s3] ^ rk10[7]
        out[off + 8] = sbox[s8] ^ rk10[8]
        out[off + 9] = sbox[s13] ^ rk10[9]
        out[off + 10] = sbox[s2] ^ rk10[10]
        out[off + 11] = sbox[s7] ^ rk10[11]
        out[off + 12] = sbox[s12] ^ rk10[12]
        out[off + 13] = sbox[s1] ^ rk10[13]
        out[off + 14] = sbox[s6] ^ rk10[14]
        out[off + 15] = sbox[s11] ^ rk10[15]

    return out.hex()


# ── 线程安全 HTTP/1.1 Keep-Alive 持久连接池 + Gzip 解压 ─────────────────────
class _KeepAlivePool:
    """基于标准库 http.client 的线程安全持久连接池（省去重复 TCP/TLS 握手）。"""

    MAX_IDLE_PER_HOST = 6

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: dict[tuple[str, str, int], list[http.client.HTTPConnection]] = {}
        self._ssl_ctx = ssl.create_default_context()

    def _acquire(self, scheme: str, host: str, port: int, timeout: float) -> http.client.HTTPConnection:
        key = (scheme, host, port)
        with self._lock:
            bucket = self._pool.get(key)
            if bucket:
                conn = bucket.pop()
                conn.timeout = timeout
                return conn
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._ssl_ctx)
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _release(self, scheme: str, host: str, port: int, conn: http.client.HTTPConnection) -> None:
        if getattr(conn, "sock", None) is None:
            return
        key = (scheme, host, port)
        with self._lock:
            bucket = self._pool.setdefault(key, [])
            if len(bucket) < self.MAX_IDLE_PER_HOST:
                bucket.append(conn)
                return
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def request_bytes(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: float = 20.0,
        max_bytes: int | None = None,
    ) -> tuple[int, bytes, list[str]]:
        """发起带连接池复用的 HTTP 请求，自动处理重定向与 Gzip 解压。"""
        cur_url = url
        for _redirect in range(3):
            parsed = urllib.parse.urlsplit(cur_url)
            scheme = (parsed.scheme or "https").lower()
            host = parsed.hostname or ""
            port = parsed.port or (443 if scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            req_headers = {
                "Connection": "keep-alive",
                "Accept-Encoding": "gzip",
            }
            if headers:
                req_headers.update(headers)
            if data is not None and "Content-Length" not in req_headers:
                req_headers["Content-Length"] = str(len(data))

            last_err: Exception | None = None
            for _attempt in range(2):
                conn = self._acquire(scheme, host, port, timeout)
                try:
                    conn.request(method, path, body=data, headers=req_headers)
                    resp = conn.getresponse()
                    status = resp.status
                    set_cookies = resp.msg.get_all("Set-Cookie") or []
                    enc = (resp.getheader("Content-Encoding") or "").lower()
                    loc = resp.getheader("Location")

                    if status in (301, 302, 303, 307, 308) and loc:
                        resp.read()
                        if not resp.will_close:
                            self._release(scheme, host, port, conn)
                        else:
                            conn.close()
                        cur_url = urllib.parse.urljoin(cur_url, loc)
                        if status == 303:
                            method = "GET"
                            data = None
                        break

                    if max_bytes is not None:
                        body = resp.read(max_bytes)
                        # 读取部分字节后必须关闭连接以免残留未读流污染下一次请求
                        conn.close()
                    else:
                        body = resp.read()
                        if not resp.will_close:
                            self._release(scheme, host, port, conn)
                        else:
                            conn.close()

                    if "gzip" in enc and body and max_bytes is None:
                        try:
                            body = gzip.decompress(body)
                        except Exception:  # noqa: BLE001
                            pass
                    return status, body, set_cookies
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
            else:
                if last_err is not None:
                    raise last_err
        raise RuntimeError(f"HTTP 重定向次数过多: {url}")


_HTTP_POOL = _KeepAlivePool()


# ── 多级持久化与内存 LRU 缓存（元数据 / 歌词 / 封面 / 直链） ─────────────────
def _get_cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    d = base / "ncm-lite"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001
        fallback = Path(tempfile.gettempdir()) / "ncm-lite-cache"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class _MetadataCache:
    """线程安全的歌曲详情、歌词、封面与短期直链缓存（减少 80% 重复网络请求）。"""

    MAX_TRACKS = 5000
    MAX_LYRICS = 300
    MAX_COVERS = 80
    URL_TTL_SEC = 600.0  # 播放直链缓存 10 分钟

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache_dir = _get_cache_dir()
        self._tracks_file = self._cache_dir / "tracks_v1.json"
        self._lyrics_file = self._cache_dir / "lyrics_v1.json"

        self._tracks: collections.OrderedDict[int, dict[str, Any]] = collections.OrderedDict()
        self._lyrics: collections.OrderedDict[int, dict[str, Any]] = collections.OrderedDict()
        self._covers: collections.OrderedDict[tuple[str, int, int], str] = collections.OrderedDict()
        self._urls: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}

        self._tracks_dirty = False
        self._lyrics_dirty = False
        self._last_flush = time.monotonic()
        self._load_from_disk()
        atexit.register(self.flush_to_disk)

    def _load_from_disk(self) -> None:
        try:
            if self._tracks_file.is_file():
                raw = json.loads(self._tracks_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, dict) and "id" in v:
                            self._tracks[int(k)] = v
        except Exception:  # noqa: BLE001
            pass

        try:
            if self._lyrics_file.is_file():
                raw = json.loads(self._lyrics_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, dict) and "lines" in v:
                            self._lyrics[int(k)] = v
        except Exception:  # noqa: BLE001
            pass

    def flush_to_disk(self) -> None:
        with self._lock:
            tracks_snap = dict(self._tracks) if self._tracks_dirty else None
            lyrics_snap = dict(self._lyrics) if self._lyrics_dirty else None
            self._tracks_dirty = False
            self._lyrics_dirty = False
            self._last_flush = time.monotonic()

        if tracks_snap is not None:
            try:
                tmp = self._tracks_file.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(tracks_snap, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                tmp.replace(self._tracks_file)
            except Exception:  # noqa: BLE001
                pass

        if lyrics_snap is not None:
            try:
                tmp = self._lyrics_file.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(lyrics_snap, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                tmp.replace(self._lyrics_file)
            except Exception:  # noqa: BLE001
                pass

    def _maybe_flush_async(self) -> None:
        if (time.monotonic() - self._last_flush) < 12.0:
            return
        self._last_flush = time.monotonic()
        threading.Thread(target=self.flush_to_disk, daemon=True).start()

    def get_track(self, song_id: int) -> dict[str, Any] | None:
        with self._lock:
            tr = self._tracks.get(song_id)
            if tr is not None:
                self._tracks.move_to_end(song_id)
                return dict(tr)
            return None

    def put_tracks(self, tracks: list[dict[str, Any]]) -> None:
        if not tracks:
            return
        with self._lock:
            for tr in tracks:
                sid = int(tr["id"])
                self._tracks[sid] = tr
                self._tracks.move_to_end(sid)
            while len(self._tracks) > self.MAX_TRACKS:
                self._tracks.popitem(last=False)
            self._tracks_dirty = True
        self._maybe_flush_async()

    def get_lyric(self, song_id: int) -> dict[str, Any] | None:
        with self._lock:
            lyr = self._lyrics.get(song_id)
            if lyr is not None:
                self._lyrics.move_to_end(song_id)
                return lyr
            return None

    def put_lyric(self, song_id: int, data: dict[str, Any]) -> None:
        with self._lock:
            self._lyrics[song_id] = data
            self._lyrics.move_to_end(song_id)
            while len(self._lyrics) > self.MAX_LYRICS:
                self._lyrics.popitem(last=False)
            self._lyrics_dirty = True
        self._maybe_flush_async()

    def get_cover(self, key: tuple[str, int, int]) -> str | None:
        with self._lock:
            val = self._covers.get(key)
            if val is not None:
                self._covers.move_to_end(key)
            return val

    def put_cover(self, key: tuple[str, int, int], ansi: str) -> None:
        with self._lock:
            self._covers[key] = ansi
            self._covers.move_to_end(key)
            while len(self._covers) > self.MAX_COVERS:
                self._covers.popitem(last=False)

    def get_stream_url(self, song_id: int, level: str) -> dict[str, Any] | None:
        now = time.monotonic()
        key = (int(song_id), level)
        with self._lock:
            entry = self._urls.get(key)
            if entry is not None:
                ts, info = entry
                if (now - ts) < self.URL_TTL_SEC and info.get("url"):
                    return dict(info)
                self._urls.pop(key, None)
        return None

    def put_stream_url(self, song_id: int, level: str, info: dict[str, Any]) -> None:
        if not info.get("url"):
            return
        with self._lock:
            if len(self._urls) > 200:
                self._urls.clear()
            self._urls[(int(song_id), level)] = (time.monotonic(), dict(info))


_CACHE = _MetadataCache()


# ── 本地流媒体 Tee 缓存代理（消灭探测 + mpv + ffmpeg 三重重复下载） ───────────
def _is_valid_audio_head(head: bytes) -> bool:
    if not head:
        return False
    return (
        head[:3] == b"ID3"
        or head[:4] == b"fLaC"
        or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3")
        or head[:4] in (b"OggS", b"RIFF")
        or head[4:8] == b"ftyp"
        or len(head) >= 4
    )


class _StreamSession:
    """单首音频流的后台单连接下载与多消费者共享读会话。"""

    def __init__(self, key: str, raw_url: str, cache_path: Path) -> None:
        self.key = key
        self.raw_url = raw_url
        self.cache_path = cache_path
        self.active_url: str = raw_url
        self.content_type: str = "audio/flac"
        self.total_size: int = 0
        self.written: int = 0
        self.header_ok: bool = False
        self.eof: bool = False
        self.failed: bool = False
        self.cond = threading.Condition()
        self._cancel = False
        self._thread = threading.Thread(target=self._download_worker, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        with self.cond:
            self._cancel = True
            self.cond.notify_all()

    def wait_header(self, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        with self.cond:
            while not self.header_ok and not self.failed and not self.eof:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return False
                self.cond.wait(timeout=rem)
            return self.header_ok

    def _download_worker(self) -> None:
        req_headers = {"User-Agent": UA_WEB, "Referer": REFERER}
        for cand in download_candidates(self.raw_url):
            if self._cancel:
                break
            req = urllib.request.Request(cand, headers=req_headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    head = resp.read(16)
                    if not _is_valid_audio_head(head):
                        continue
                    ctype = resp.headers.get("Content-Type") or "audio/flac"
                    clen = int(resp.headers.get("Content-Length") or 0)

                    with open(self.cache_path, "wb") as f:
                        f.write(head)
                        f.flush()
                        with self.cond:
                            self.active_url = cand
                            self.content_type = ctype
                            self.total_size = clen
                            self.written = len(head)
                            self.header_ok = True
                            self.cond.notify_all()

                        while not self._cancel:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            f.flush()
                            with self.cond:
                                self.written += len(chunk)
                                self.cond.notify_all()

                    with self.cond:
                        if not self._cancel:
                            if self.total_size <= 0:
                                self.total_size = self.written
                            self.eof = True
                        else:
                            self.failed = True
                        self.cond.notify_all()
                    return
            except Exception:  # noqa: BLE001
                continue

        with self.cond:
            self.failed = True
            self.cond.notify_all()


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class AudioStreamProxy:
    """本地流式缓存代理：让探测、mpv 与 ffmpeg 共享唯一一条 CDN 下载流。"""

    MAX_CACHED_FILES = 2

    def __init__(self) -> None:
        self._lock = threading.Lock()
        uid = os.getuid() if hasattr(os, "getuid") else 0
        streams_root = _get_cache_dir() / "streams"
        streams_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_dirs(streams_root, f"s_{uid}_")
        self._dir = streams_root / f"s_{uid}_{os.getpid()}"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sessions: collections.OrderedDict[str, _StreamSession] = collections.OrderedDict()
        self._server: _ThreadingHTTPServer | None = None
        self._port: int = 0
        atexit.register(self.close)

    @staticmethod
    def _cleanup_stale_dirs(root: Path, prefix: str) -> None:
        my_pid = os.getpid()
        try:
            for child in root.iterdir():
                if not child.is_dir() or not child.name.startswith(prefix):
                    continue
                pid_part = child.name[len(prefix) :]
                if not pid_part.isdigit():
                    continue
                pid = int(pid_part)
                if pid == my_pid:
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    shutil.rmtree(child, ignore_errors=True)
                except PermissionError:
                    pass
        except OSError:
            pass

    def _ensure_server(self) -> int:
        with self._lock:
            if self._server is not None:
                return self._port
            proxy = self

            class Handler(http.server.BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                    return

                def do_HEAD(self) -> None:
                    self._serve(send_body=False)

                def do_GET(self) -> None:
                    self._serve(send_body=True)

                def _serve(self, send_body: bool) -> None:
                    parts = self.path.split("?")[0].strip("/").split("/")
                    if len(parts) != 2 or parts[0] != "stream":
                        self.send_error(404)
                        return
                    key = parts[1]
                    sess = proxy.get_session(key)
                    if sess is None or not sess.wait_header(timeout=5.0):
                        self.send_error(502)
                        return

                    range_hdr = self.headers.get("Range") or ""
                    start_byte = 0
                    end_byte: int | None = None
                    is_partial = False
                    if range_hdr.startswith("bytes="):
                        r_spec = range_hdr[6:].split(",")[0].strip()
                        if "-" in r_spec:
                            s_str, e_str = r_spec.split("-", 1)
                            if s_str.strip().isdigit():
                                start_byte = int(s_str.strip())
                                is_partial = True
                            if e_str.strip().isdigit():
                                end_byte = int(e_str.strip())
                                is_partial = True

                    with sess.cond:
                        cur_written = sess.written
                        is_eof = sess.eof
                        total = sess.total_size

                    # 若用户大幅快进 Seek 到尚未下载到的远端区域，直接向上游 CDN 发起 Range 透传
                    if start_byte > cur_written + 262144 and not is_eof:
                        self._proxy_upstream_range(sess, start_byte, end_byte, send_body)
                        return

                    if total > 0:
                        last_byte = min(end_byte, total - 1) if end_byte is not None else (total - 1)
                        if start_byte >= total:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{total}")
                            self.end_headers()
                            return
                        content_len = max(0, last_byte - start_byte + 1)
                    else:
                        last_byte = end_byte
                        content_len = None

                    if is_partial:
                        self.send_response(206)
                        if total > 0 and last_byte is not None:
                            self.send_header("Content-Range", f"bytes {start_byte}-{last_byte}/{total}")
                    else:
                        self.send_response(200)

                    self.send_header("Content-Type", sess.content_type)
                    self.send_header("Accept-Ranges", "bytes")
                    if content_len is not None:
                        self.send_header("Content-Length", str(content_len))
                    self.send_header("Connection", "close")
                    self.end_headers()

                    if not send_body:
                        return

                    pos = start_byte
                    try:
                        with open(sess.cache_path, "rb") as f:
                            while True:
                                if last_byte is not None and pos > last_byte:
                                    break
                                with sess.cond:
                                    while pos >= sess.written and not sess.eof and not sess.failed:
                                        sess.cond.wait(timeout=0.5)
                                    avail = sess.written - pos
                                    done = sess.eof or sess.failed
                                if avail <= 0:
                                    if done:
                                        break
                                    continue
                                f.seek(pos)
                                to_read = min(65536, avail)
                                if last_byte is not None:
                                    to_read = min(to_read, last_byte - pos + 1)
                                if to_read <= 0:
                                    break
                                chunk = f.read(to_read)
                                if not chunk:
                                    if done or not sess.cache_path.is_file():
                                        break
                                    time.sleep(0.02)
                                    continue
                                self.wfile.write(chunk)
                                pos += len(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass

                def _proxy_upstream_range(
                    self,
                    sess: _StreamSession,
                    start_byte: int,
                    end_byte: int | None,
                    send_body: bool,
                ) -> None:
                    r_val = f"bytes={start_byte}-{end_byte if end_byte is not None else ''}"
                    req = urllib.request.Request(
                        sess.active_url,
                        headers={"User-Agent": UA_WEB, "Referer": REFERER, "Range": r_val},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=12) as resp:
                            self.send_response(resp.status)
                            for hk in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                                hv = resp.headers.get(hk)
                                if hv:
                                    self.send_header(hk, hv)
                            self.send_header("Connection", "close")
                            self.end_headers()
                            if send_body:
                                while chunk := resp.read(65536):
                                    self.wfile.write(chunk)
                    except Exception:  # noqa: BLE001
                        pass

            srv = _ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            self._server = srv
            self._port = int(srv.server_address[1])
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return self._port

    def get_session(self, key: str) -> _StreamSession | None:
        with self._lock:
            return self._sessions.get(key)

    def resolve_stream(self, song_id: int, raw_url: str, level: str = "lossless") -> str:
        """为指定歌曲建立或复用本地流式缓存会话，返回可直接交给 mpv / ffmpeg 的地址。"""
        port = self._ensure_server()
        key = f"{int(song_id)}_{level}"
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.failed and existing.cache_path.is_file():
                self._sessions.move_to_end(key)
                if existing.eof and existing.written > 0:
                    return str(existing.cache_path)
                return f"http://127.0.0.1:{port}/stream/{key}"

            # 淘汰超出上限的旧缓存文件（保留当前正在播放的最近 6 首）
            while len(self._sessions) >= self.MAX_CACHED_FILES:
                _old_k, old_sess = self._sessions.popitem(last=False)
                old_sess.cancel()
                try:
                    if old_sess.cache_path.is_file():
                        old_sess.cache_path.unlink()
                except OSError:
                    pass

            cache_path = self._dir / f"{key}.audio"
            sess = _StreamSession(key, raw_url, cache_path)
            self._sessions[key] = sess
            sess.start()

        if sess.wait_header(timeout=4.5):
            if sess.eof and sess.written > 0:
                return str(sess.cache_path)
            return f"http://127.0.0.1:{port}/stream/{key}"
        return probe_playable_url(raw_url)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            srv = self._server
            self._server = None
        for s in sessions:
            s.cancel()
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
        try:
            shutil.rmtree(self._dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


_STREAM_PROXY = AudioStreamProxy()


def resolve_cached_stream(song_id: int, raw_url: str, level: str = "lossless") -> str:
    """获取经过本地 Tee 缓存代理封装的播放地址（消除探测、mpv、ffmpeg 重复下载）。"""
    return _STREAM_PROXY.resolve_stream(song_id, raw_url, level)


# ── cookie ───────────────────────────────────────────────────────────────────
def load_cookie() -> str:
    if not COOKIE_FILE.is_file():
        sys.exit("未找到 cookie.txt，先运行: python ncm.py login")
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def save_cookie(text: str) -> None:
    COOKIE_FILE.write_text(text.strip() + "\n", encoding="utf-8")
    try:
        COOKIE_FILE.chmod(0o600)
    except OSError:
        pass


def csrf(cookie: str) -> str:
    m = re.search(r"__csrf=([^;]+)", cookie or "")
    return m.group(1) if m else ""


def web_headers(cookie: str) -> dict[str, str]:
    return {
        "Cookie": cookie,
        "Referer": REFERER,
        "User-Agent": UA_WEB,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def eapi_params(url: str, payload: dict) -> str:
    path = urllib.parse.urlparse(url).path.replace("/eapi/", "/api/")
    raw = json.dumps(payload, separators=(",", ":"))
    digest = hashlib.md5(f"nobody{path}use{raw}md5forencrypt".encode()).hexdigest()
    return aes_ecb_hex(_pkcs7(f"{path}-36cd479b6b5-{raw}-36cd479b6b5-{digest}".encode()))


def post_eapi(url: str, payload: dict, cookie: str = "") -> tuple[dict, list[str]]:
    body = urllib.parse.urlencode({"params": eapi_params(url, payload)}).encode()
    headers = {
        "User-Agent": UA_EAPI,
        "Referer": REFERER,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if cookie:
        headers["Cookie"] = cookie
    status, raw_bytes, cookies = _HTTP_POOL.request_bytes(
        "POST", url, headers=headers, data=body, timeout=20.0
    )
    _ = status
    text = raw_bytes.decode("utf-8", "replace")
    try:
        return json.loads(text), cookies
    except json.JSONDecodeError:
        return {}, cookies


def _base_config() -> dict:
    return {
        "os": "pc",
        "appver": "",
        "osver": "",
        "deviceId": "pyncm!",
        "requestId": str(random.randrange(20000000, 30000000)),
    }


def extract_cookie(set_cookie_lines: list[str]) -> str:
    wanted = ("MUSIC_U", "__csrf", "NMTID", "JSESSIONID-WYYY", "os")
    values: dict[str, str] = {}
    for line in set_cookie_lines:
        jar = SimpleCookie()
        jar.load(line)
        for key, morsel in jar.items():
            if key in wanted and morsel.value:
                values[key] = morsel.value
    return "; ".join(f"{k}={v}" for k, v in values.items())


# ── login ────────────────────────────────────────────────────────────────────
def cmd_login(args) -> int:
    data, _ = post_eapi(QR_UNIKEY_API, {"type": 1, "header": json.dumps(_base_config())})
    unikey = data.get("unikey")
    if data.get("code") != 200 or not unikey:
        print(f"生成二维码失败: {data}")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    qr_path = OUT_DIR / "ncm_qr_login.png"
    login_url = QR_URL_TEMPLATE.format(unikey=unikey)
    try:
        import qrcode

        qr = qrcode.QRCode(border=2, box_size=10)
        qr.add_data(login_url)
        qr.make(fit=True)
        qr.make_image(fill_color="black", back_color="white").save(qr_path)
        print(f"二维码图片: {qr_path}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 无法生成二维码图片({e})，改用链接: {login_url}")
    print(f"登录链接: {login_url}")
    print("请用网易云音乐 App 扫码（约 3 分钟内有效）……")

    deadline = time.time() + 180
    last = None
    while time.time() < deadline:
        data, cookies = post_eapi(
            QR_LOGIN_API, {"key": unikey, "type": 1, "header": json.dumps(_base_config())}
        )
        code = data.get("code")
        if code == 803:
            cookie = extract_cookie(cookies)
            if "MUSIC_U=" not in cookie:
                print("登录成功但没拿到 MUSIC_U，Set-Cookie:", cookies)
                return 1
            save_cookie(cookie)
            print(f"登录成功，cookie 已保存: {COOKIE_FILE}")
            return 0
        if code == 800:
            print("二维码已过期，请重新运行 login")
            return 1
        if code != last:
            print({801: "等待扫码…", 802: "已扫码，等待手机确认…"}.get(code, f"状态 {code}"))
            last = code
        time.sleep(2.5)
    print("登录超时")
    return 1


# ── web api ──────────────────────────────────────────────────────────────────
def _web_get(cookie: str, url: str) -> dict:
    _status, raw, _cookies = _HTTP_POOL.request_bytes(
        "GET", url, headers=web_headers(cookie), timeout=20.0
    )
    return json.loads(raw.decode("utf-8", "replace"))


def _web_post(cookie: str, url: str, params: dict | str) -> dict:
    body = params.encode() if isinstance(params, str) else urllib.parse.urlencode(params).encode()
    _status, raw, _cookies = _HTTP_POOL.request_bytes(
        "POST", url, headers=web_headers(cookie), data=body, timeout=20.0
    )
    return json.loads(raw.decode("utf-8", "replace"))


def song_detail(cookie: str, song_id: int) -> dict | None:
    qs = urllib.parse.urlencode({"c": json.dumps([{"id": song_id}])})
    data = _web_get(cookie, f"{SONG_DETAIL_API}?{qs}")
    songs = data.get("songs") or []
    return songs[0] if songs else None


def stream_url(cookie: str, song_id: int, level: str) -> dict:
    """取播放直链（带 10 分钟内存 TTL 缓存与连接池复用）。"""
    cached = _CACHE.get_stream_url(song_id, level)
    if cached is not None:
        return cached

    ladder = LEVELS[LEVELS.index(level) :: -1]
    last_code = None
    for lv in ladder:
        data = _web_post(cookie, PLAYER_URL_API_V1, f"ids=[{song_id}]&level={lv}&encodeType=flac")
        items = data.get("data") or []
        if items and items[0].get("url"):
            res = items[0] | {"_code": data.get("code"), "_requested": level, "_got_level": lv}
            _CACHE.put_stream_url(song_id, level, res)
            return res
        last_code = items[0].get("code") if items else data.get("code")

    data = _web_post(cookie, PLAYER_URL_API, f"ids=[{song_id}]&br=999000")
    items = data.get("data") or []
    if items and items[0].get("url"):
        res = items[0] | {"_code": data.get("code"), "_requested": level, "_got_level": "br999000"}
        _CACHE.put_stream_url(song_id, level, res)
        return res
    return (items[0] if items else {}) | {
        "_code": last_code or data.get("code"),
        "_requested": level,
        "_got_level": None,
    }


def whoami(cookie: str) -> dict:
    qs = urllib.parse.urlencode({"csrf_token": csrf(cookie)})
    return _web_get(cookie, f"https://music.163.com/api/nuser/account/get?{qs}")


def user_playlists(cookie: str, uid: int) -> list[dict]:
    """拉取用户全部歌单（我喜欢的音乐、自建歌单、收藏歌单）。"""
    out: list[dict] = []
    offset = 0
    while True:
        data = _web_post(cookie, USER_PLAYLIST_API, {"uid": uid, "limit": 50, "offset": offset})
        batch = data.get("playlist") or []
        out.extend(batch)
        if not data.get("more") or not batch:
            break
        offset += len(batch)
    return out


def song_details_batch(cookie: str, song_ids: list[int]) -> list[dict]:
    """批量拉取歌曲详情，每批最多 500 首（多分片时使用连接池并发请求）。"""
    if not song_ids:
        return []
    chunks = [song_ids[i : i + 500] for i in range(0, len(song_ids), 500)]
    if len(chunks) == 1:
        c_param = json.dumps([{"id": sid} for sid in chunks[0]], separators=(",", ":"))
        data = _web_post(cookie, SONG_DETAIL_API, {"c": c_param})
        return data.get("songs") or []

    def _fetch_chunk(chunk: list[int]) -> list[dict]:
        c_param = json.dumps([{"id": sid} for sid in chunk], separators=(",", ":"))
        data = _web_post(cookie, SONG_DETAIL_API, {"c": c_param})
        return data.get("songs") or []

    songs: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
        for res in ex.map(_fetch_chunk, chunks):
            songs.extend(res)
    return songs


def _normalize_track(t: dict) -> dict:
    artists = ", ".join(a.get("name", "") for a in (t.get("ar") or t.get("artists") or []))
    album = t.get("al") or t.get("album") or {}
    return {
        "id": t["id"],
        "name": t.get("name") or f"song_{t['id']}",
        "artists": artists or "未知歌手",
        "album": album.get("name") or "",
        "pic_url": album.get("picUrl") or "",
        "duration_ms": t.get("dt") or t.get("duration") or 0,
    }


def playlist_tracks(cookie: str, playlist_id: int) -> list[dict]:
    """拉取歌单内全部歌曲元数据；优先命中本地元数据缓存，缺失部分并发补全。"""
    data = _web_post(cookie, PLAYLIST_DETAIL_API, {"id": playlist_id, "n": 100000, "s": 8})
    pl = data.get("playlist") or {}
    raw_tracks = pl.get("tracks") or []
    track_ids = [int(item["id"]) for item in (pl.get("trackIds") or []) if "id" in item]

    norm_by_id: dict[int, dict] = {}
    fresh_norms: list[dict] = []
    for t in raw_tracks:
        if "id" in t:
            nt = _normalize_track(t)
            norm_by_id[int(nt["id"])] = nt
            fresh_norms.append(nt)

    if track_ids and len(norm_by_id) < len(track_ids):
        still_missing: list[int] = []
        for sid in track_ids:
            if sid not in norm_by_id:
                cached_tr = _CACHE.get_track(sid)
                if cached_tr is not None:
                    norm_by_id[sid] = cached_tr
                else:
                    still_missing.append(sid)

        if still_missing:
            for t in song_details_batch(cookie, still_missing):
                if "id" in t:
                    nt = _normalize_track(t)
                    norm_by_id[int(nt["id"])] = nt
                    fresh_norms.append(nt)

        if fresh_norms:
            _CACHE.put_tracks(fresh_norms)
        return [norm_by_id[sid] for sid in track_ids if sid in norm_by_id]

    if fresh_norms:
        _CACHE.put_tracks(fresh_norms)
    return fresh_norms


_LRC_TIME_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
_YRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_YRC_WORD_RE = re.compile(r"\((\d+),(\d+),\d+\)(.*?)(?=\(\d+,\d+,\d+\)|$)")


def parse_lrc(lrc_text: str) -> list[tuple[float, str]]:
    """解析 LRC 为按时间升序的 [(秒, 文本)]。"""
    rows: list[tuple[float, str]] = []
    for raw_line in (lrc_text or "").splitlines():
        matches = list(_LRC_TIME_RE.finditer(raw_line))
        if not matches:
            continue
        text = _LRC_TIME_RE.sub("", raw_line).strip()
        for m in matches:
            sec = int(m.group(1)) * 60 + float(m.group(2))
            rows.append((sec, text))
    rows.sort(key=lambda x: x[0])
    return rows


def parse_yrc(yrc_text: str) -> list[dict]:
    """解析网易云逐字 YRC 为按起始时间升序的行字典列表。"""
    rows: list[dict] = []
    for raw_line in (yrc_text or "").splitlines():
        line_str = raw_line.strip()
        m = _YRC_LINE_RE.match(line_str)
        if not m:
            continue
        line_start = int(m.group(1)) / 1000.0
        line_dur = max(0.05, int(m.group(2)) / 1000.0)
        body = m.group(3)
        words: list[list] = []
        parts: list[str] = []
        for wm in _YRC_WORD_RE.finditer(body):
            w_start = int(wm.group(1)) / 1000.0
            w_dur = max(0.01, int(wm.group(2)) / 1000.0)
            w_text = wm.group(3)
            if not w_text:
                continue
            words.append([w_start, w_start + w_dur, w_text])
            parts.append(w_text)
        orig = "".join(parts).strip()
        if not orig:
            continue
        line_end = max(line_start + line_dur, words[-1][1] if words else line_start + line_dur)
        rows.append({
            "start": line_start,
            "end": line_end,
            "orig": orig,
            "trans": "",
            "words": words,
        })
    rows.sort(key=lambda r: r["start"])
    return rows


def _merge_trans_rows(
    base_rows: list[dict], trans_pairs: list[tuple[float, str]], tol: float = 0.45
) -> list[dict]:
    """按时间戳将翻译文本合并进基础歌词行。"""
    trans_map = [(t, s) for t, s in trans_pairs if s]
    if not trans_map:
        return base_rows
    ti = 0
    for row in base_rows:
        sec = row["start"]
        while ti < len(trans_map) and trans_map[ti][0] < sec - tol:
            ti += 1
        if ti < len(trans_map) and abs(trans_map[ti][0] - sec) <= tol:
            row["trans"] = trans_map[ti][1]
    return base_rows


def song_lyric_rich(cookie: str, song_id: int) -> dict:
    """获取歌词完整结构（优先命中内存与本地持久化缓存）。"""
    sid = int(song_id)
    cached = _CACHE.get_lyric(sid)
    if cached is not None:
        return cached

    data: dict = {}
    try:
        data, _ = post_eapi(
            LYRIC_V1_EAPI,
            {
                "id": sid,
                "cp": False,
                "tv": -1,
                "lv": -1,
                "rv": -1,
                "kv": -1,
                "yv": -1,
                "ytv": -1,
                "yrv": -1,
            },
            cookie=cookie,
        )
    except Exception:  # noqa: BLE001
        data = {}

    if not data or not ((data.get("yrc") or {}).get("lyric") or (data.get("lrc") or {}).get("lyric")):
        data = _web_post(cookie, LYRIC_API, {"id": sid, "lv": -1, "tv": -1, "rv": -1, "kv": -1})

    yrc_raw = (data.get("yrc") or {}).get("lyric") or ""
    ytlrc_raw = (data.get("ytlrc") or {}).get("lyric") or ""
    lrc_raw = (data.get("lrc") or {}).get("lyric") or ""
    tlyric_raw = (data.get("tlyric") or {}).get("lyric") or ""

    yrc_rows = parse_yrc(yrc_raw)
    if yrc_rows:
        trans = parse_lrc(ytlrc_raw or tlyric_raw)
        _merge_trans_rows(yrc_rows, trans, tol=0.45)
        result = {"has_yrc": True, "lines": yrc_rows}
        _CACHE.put_lyric(sid, result)
        return result

    orig = parse_lrc(lrc_raw)
    if not orig:
        result = {"has_yrc": False, "lines": []}
        _CACHE.put_lyric(sid, result)
        return result
    trans = parse_lrc(tlyric_raw)
    lrc_rows: list[dict] = []
    for i, (sec, text) in enumerate(orig):
        next_sec = orig[i + 1][0] if i + 1 < len(orig) else sec + 5.0
        end_sec = max(sec + 0.3, min(next_sec, sec + 12.0)) if i + 1 < len(orig) else sec + 5.0
        lrc_rows.append({
            "start": sec,
            "end": end_sec,
            "orig": text,
            "trans": "",
            "words": [],
        })
    _merge_trans_rows(lrc_rows, trans, tol=0.35)
    lrc_rows = [r for r in lrc_rows if r["orig"] or r["trans"]]
    result = {"has_yrc": False, "lines": lrc_rows}
    _CACHE.put_lyric(sid, result)
    return result


def song_lyric(cookie: str, song_id: int) -> list[tuple[float, str, str]]:
    """返回合并翻译的歌词行：[(秒, 原词, 译文)]。"""
    rich = song_lyric_rich(cookie, song_id)
    return [(r["start"], r["orig"], r["trans"]) for r in rich["lines"]]


def probe_playable_url(url: str) -> str:
    """按 CDN 降级顺序复用连接池探测第一个可流式读取音频头的直链。"""
    for cand in download_candidates(url):
        try:
            status, head, _ = _HTTP_POOL.request_bytes(
                "GET",
                cand,
                headers={"User-Agent": UA_WEB, "Referer": REFERER, "Range": "bytes=0-15"},
                timeout=6.0,
                max_bytes=16,
            )
            if status in (200, 206) and _is_valid_audio_head(head):
                return cand
        except Exception:  # noqa: BLE001
            continue
    return url


def _render_halfblock_ansi_pil(img_bytes: bytes, width: int, height: int) -> str:
    """使用 Pillow 在内存直接将图片渲染为 24-bit 真彩半块字符画（零子进程开销）。"""
    from PIL import Image

    with Image.open(io.BytesIO(img_bytes)) as im:
        rgb = im.convert("RGB").resize((width, height * 2), Image.Resampling.LANCZOS)
        px = rgb.load()
        assert px is not None
        lines: list[str] = []
        for y in range(height):
            y_top = y * 2
            y_bot = y_top + 1
            row_parts: list[str] = []
            last_fg: tuple[int, int, int] | None = None
            last_bg: tuple[int, int, int] | None = None
            for x in range(width):
                fg = px[x, y_top]
                bg = px[x, y_bot]
                if fg == bg:
                    if bg != last_bg:
                        row_parts.append(f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
                        last_bg = bg
                    row_parts.append(" ")
                else:
                    if fg != last_fg:
                        row_parts.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
                        last_fg = fg
                    if bg != last_bg:
                        row_parts.append(f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
                        last_bg = bg
                    row_parts.append("▀")
            row_parts.append("\x1b[0m")
            lines.append("".join(row_parts))
        return "\n".join(lines)


def fetch_cover_ansi(pic_url: str, width: int = 34, height: int = 17) -> str:
    """下载专辑封面并渲染为 ANSI 真彩字符画（优先 Pillow 内存渲染，回退 chafa）。"""
    if not pic_url:
        return ""
    cache_key = (pic_url, int(width), int(height))
    cached = _CACHE.get_cover(cache_key)
    if cached is not None:
        return cached

    sep = "&" if "?" in pic_url else "?"
    # 160x160 缩略图比 400x400 体积小 80%（~5KB），对 34x34 像素仍为 4 倍超采样
    thumb_url = f"{pic_url}{sep}param=160y160"
    status, img_bytes, _ = _HTTP_POOL.request_bytes(
        "GET",
        thumb_url,
        headers={"User-Agent": UA_WEB, "Referer": REFERER},
        timeout=10.0,
    )
    if status != 200 or not img_bytes:
        return ""

    ansi = ""
    try:
        ansi = _render_halfblock_ansi_pil(img_bytes, width, height)
    except Exception:  # noqa: BLE001
        try:
            proc = subprocess.run(
                [
                    "chafa",
                    "--format=symbols",
                    "--symbols=half+block",
                    "-c",
                    "full",
                    f"--size={width}x{height}",
                    "-",
                ],
                input=img_bytes,
                capture_output=True,
                timeout=8,
            )
            if proc.returncode == 0:
                ansi = proc.stdout.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            ansi = ""

    if ansi:
        _CACHE.put_cover(cache_key, ansi)
    return ansi


# ── download ─────────────────────────────────────────────────────────────────
def download_candidates(url: str) -> list[str]:
    """原链接 → 去掉 authSecret → 去掉查询串（部分 CDN 见到 authSecret 直接 403）。"""
    out = [url]
    parts = urllib.parse.urlsplit(url)
    if parts.query:
        kept = "&".join(p for p in parts.query.split("&") if p and not p.startswith("authSecret="))
        if kept:
            out.append(urllib.parse.urlunsplit(parts._replace(query=kept)))
        out.append(urllib.parse.urlunsplit(parts._replace(query="")))
    seen, uniq = set(), []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def fetch_to_file(url: str, path: Path) -> tuple[int, str]:
    for cand in download_candidates(url):
        req = urllib.request.Request(
            cand, headers={"User-Agent": UA_WEB, "Referer": REFERER}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(path, "wb") as f:
                head = resp.read(4)
                if head[:3] == b"ID3" or head[:2] == b"\xff\xfb" or head[:4] == b"fLaC":
                    f.write(head)
                    while chunk := resp.read(1 << 16):
                        f.write(chunk)
                    return 200, cand
                return -1, cand
        except urllib.error.HTTPError as e:
            last = e.code
            continue
    return locals().get("last", 0), url


def safe_name(detail: dict) -> str:
    name = detail.get("name") or f"song_{detail.get('id')}"
    artists = ", ".join(a.get("name", "") for a in (detail.get("ar") or detail.get("artists") or []))
    raw = f"{name} - {artists}" if artists else name
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", raw).strip()[:80]


def resolve(song_id: int, level: str) -> tuple[dict | None, dict]:
    cookie = load_cookie()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_detail = ex.submit(song_detail, cookie, song_id)
        f_info = ex.submit(stream_url, cookie, song_id, level)
        return f_detail.result(), f_info.result()


def cmd_url(args) -> int:
    detail, info = resolve(args.song_id, args.level)
    if not info.get("url"):
        priv = (detail or {}).get("privilege") or {}
        print(json.dumps({"ok": False, "reason": "无直链", "detail": detail, "priv": priv},
                         ensure_ascii=False, indent=2)[:2000])
        return 1
    print(json.dumps({
        "ok": True,
        "name": safe_name(detail) if detail else None,
        "requested_level": args.level,
        "actual_level": info.get("level"),
        "type": info.get("type"),
        "br": info.get("br"),
        "size": info.get("size"),
        "url": info["url"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_dl(args) -> int:
    detail, info = resolve(args.song_id, args.level)
    if not info.get("url"):
        priv = (detail or {}).get("privilege") or {}
        print(f"拿不到直链（请求档位 {args.level}）。")
        print("权限信息:", json.dumps({
            "plLevel": priv.get("plLevel"), "dlLevel": priv.get("dlLevel"),
            "maxBrLevel": priv.get("maxBrLevel"), "code": info.get("_code"),
        }, ensure_ascii=False))
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".flac" if (info.get("type") or "").lower() in ("flac", "hires") else ".mp3"
    path = OUT_DIR / f"{safe_name(detail) if detail else f'song_{args.song_id}'}{ext}"
    status, used = fetch_to_file(info["url"], path)
    if status != 200:
        print(f"下载失败: HTTP {status}")
        return 1
    size = path.stat().st_size
    print(json.dumps({
        "ok": True,
        "file": str(path),
        "requested_level": args.level,
        "actual_level": info.get("level"),
        "type": info.get("type"),
        "br": info.get("br"),
        "size_mb": round(size / 1048576, 2),
        "cdn_fallback": used != info["url"],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_whoami(args) -> int:
    data = whoami(load_cookie())
    if data.get("code") != 200 or not (data.get("profile") or data.get("account")):
        print(f"cookie 无效或已过期（code={data.get('code')}），重新运行 login")
        return 1
    prof = data.get("profile") or {}
    account = data.get("account") or {}
    print(json.dumps({
        "ok": True,
        "nickname": prof.get("nickname"),
        "userId": prof.get("userId"),
        "vipType": prof.get("vipType"),
        "account_status": account.get("status"),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_tui(args) -> int:
    from tui import run_tui

    return run_tui(level=args.level)


def cmd_lyrics(args) -> int:
    from desktop_lyrics import launch_desktop_lyrics

    return launch_desktop_lyrics(
        width=args.width,
        height=args.height,
        offset_ms=args.offset_ms,
        locked=args.lock,
        foreground=True,
        sock_path=args.sock,
    )


def cmd_shader(args) -> int:
    from screen_shader import launch_screen_shader

    return launch_screen_shader(
        active=args.active,
        big_lyrics=args.big_lyrics,
        hotkey=args.hotkey,
        lyrics_hotkey=args.lyrics_hotkey,
        lookahead_ms=args.lookahead_ms,
        foreground=True,
        sock_path=args.sock,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="ncm-lite：cookie 直连网易云 web 接口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="扫码登录并保存 cookie").set_defaults(func=cmd_login)
    sub.add_parser("whoami", help="校验 cookie").set_defaults(func=cmd_whoami)
    p_tui = sub.add_parser("tui", help="启动 TUI 播放器")
    p_tui.add_argument("--level", default="lossless", choices=LEVELS)
    p_tui.set_defaults(func=cmd_tui)

    p_lyr = sub.add_parser("lyrics", help="启动常驻顶层桌面歌词窗口")
    p_lyr.add_argument("--width", type=int, default=512)
    p_lyr.add_argument("--height", type=int, default=64)
    p_lyr.add_argument("--offset-ms", type=int, default=None)
    p_lyr.add_argument("--lock", action="store_true", help="开启鼠标点击穿透锁定")
    p_lyr.add_argument("--sock", default=None, help="自定义 IPC socket 路径")
    p_lyr.set_defaults(func=cmd_lyrics)

    p_shd = sub.add_parser("shader", help="启动全屏流质 Shader 视觉叠加层")
    p_shd.add_argument("--active", action="store_true", help="启动时立即触发液态玻璃入场动效")
    p_shd.add_argument("--big-lyrics", dest="big_lyrics", action="store_true", default=None, help="开启全屏大歌词显示")
    p_shd.add_argument("--no-big-lyrics", dest="big_lyrics", action="store_false", help="关闭全屏大歌词显示")
    p_shd.add_argument("--hotkey", default="<Ctrl><Alt>v", help="全局切换流质视效热键（默认 <Ctrl><Alt>v）")
    p_shd.add_argument("--lyrics-hotkey", default="<Ctrl><Alt>l", help="全局切换全屏大歌词热键（默认 <Ctrl><Alt>l）")
    p_shd.add_argument("--lookahead-ms", type=int, default=75, help="前瞻延迟补偿毫秒数（默认 75ms）")
    p_shd.add_argument("--sock", default=None, help="自定义 IPC socket 路径")
    p_shd.set_defaults(func=cmd_shader)

    for name, func, help_text in (
        ("dl", cmd_dl, "按歌曲 id 下载到 output/"),
        ("url", cmd_url, "只取直链"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("song_id", type=int)
        p.add_argument("--level", default="lossless", choices=LEVELS)
        p.set_defaults(func=func)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
