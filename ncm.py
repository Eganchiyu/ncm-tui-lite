#!/usr/bin/env python3
"""ncm-lite —— 用本人账号 cookie 直接调网易云 web/eapi 接口，取无损直链并下载到本地。

只用标准库（AES 走系统 openssl），QR 渲染需要 venv 里的 qrcode+pillow。

用法：
  python ncm.py login                     # 扫码登录，cookie 存到 cookie.txt
  python ncm.py whoami                    # 校验 cookie 是否有效
  python ncm.py dl <song_id> [--level lossless|exhigh|higher|standard]
  python ncm.py url <song_id> [--level ...]   # 只打印直链，不下载

默认下载音质：lossless（无损 SQ）；不碰 hires / jymaster 那些更高档。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

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

LEVELS = ["standard", "higher", "exhigh", "lossless"]


# ── cookie ───────────────────────────────────────────────────────────────────
def load_cookie() -> str:
    if not COOKIE_FILE.is_file():
        sys.exit("未找到 cookie.txt，先运行: python ncm.py login")
    return COOKIE_FILE.read_text(encoding="utf-8").strip()


def save_cookie(text: str) -> None:
    COOKIE_FILE.write_text(text.strip() + "\n", encoding="utf-8")
    COOKIE_FILE.chmod(0o600)


def csrf(cookie: str) -> str:
    m = re.search(r"__csrf=([^;]+)", cookie or "")
    return m.group(1) if m else ""


def web_headers(cookie: str) -> dict:
    return {
        "Cookie": cookie,
        "Referer": REFERER,
        "User-Agent": UA_WEB,
        "Content-Type": "application/x-www-form-urlencoded",
    }


# ── eapi 加密（openssl AES-128-ECB） ─────────────────────────────────────────
def _pkcs7(data: bytes, block: int = 16) -> bytes:
    pad = block - len(data) % block
    return data + bytes([pad]) * pad


def aes_ecb_hex(data: bytes) -> str:
    proc = subprocess.run(
        ["openssl", "enc", "-aes-128-ecb", "-K", AES_KEY.hex(), "-nosalt", "-nopad"],
        input=data,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"openssl 加密失败: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.hex()


def eapi_params(url: str, payload: dict) -> str:
    path = urllib.parse.urlparse(url).path.replace("/eapi/", "/api/")
    raw = json.dumps(payload, separators=(",", ":"))
    digest = hashlib.md5(f"nobody{path}use{raw}md5forencrypt".encode()).hexdigest()
    return aes_ecb_hex(_pkcs7(f"{path}-36cd479b6b5-{raw}-36cd479b6b5-{digest}".encode()))


def post_eapi(url: str, payload: dict) -> tuple[dict, list[str]]:
    body = urllib.parse.urlencode({"params": eapi_params(url, payload)}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": UA_EAPI,
            "Referer": REFERER,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        text = resp.read().decode("utf-8", "replace")
        cookies = resp.headers.get_all("Set-Cookie") or []
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
def song_detail(cookie: str, song_id: int) -> dict | None:
    qs = urllib.parse.urlencode({"c": json.dumps([{"id": song_id}])})
    req = urllib.request.Request(f"{SONG_DETAIL_API}?{qs}", headers=web_headers(cookie))
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    songs = data.get("songs") or []
    return songs[0] if songs else None


def stream_url(cookie: str, song_id: int, level: str) -> dict:
    """取播放直链。

    走 v1 接口（旧接口不认 level 参数，会报“参数错误”）；
    v1 拿不到时按档位逐级降级，最后再试旧接口的 br 参数。
    """
    def _post(url: str, payload: str) -> dict:
        req = urllib.request.Request(
            url, data=payload.encode(), headers=web_headers(cookie), method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    ladder = LEVELS[LEVELS.index(level) :: -1]  # 从请求档位一路降级到 standard
    last_code = None
    for lv in ladder:
        data = _post(PLAYER_URL_API_V1, f"ids=[{song_id}]&level={lv}&encodeType=flac")
        items = data.get("data") or []
        if items and items[0].get("url"):
            return items[0] | {"_code": data.get("code"), "_requested": level, "_got_level": lv}
        last_code = items[0].get("code") if items else data.get("code")

    data = _post(PLAYER_URL_API, f"ids=[{song_id}]&br=999000")
    items = data.get("data") or []
    if items and items[0].get("url"):
        return items[0] | {"_code": data.get("code"), "_requested": level, "_got_level": "br999000"}
    return (items[0] if items else {}) | {
        "_code": last_code or data.get("code"),
        "_requested": level,
        "_got_level": None,
    }


def whoami(cookie: str) -> dict:
    qs = urllib.parse.urlencode({"csrf_token": csrf(cookie)})
    req = urllib.request.Request(
        f"https://music.163.com/api/nuser/account/get?{qs}", headers=web_headers(cookie)
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _web_post(cookie: str, url: str, params: dict) -> dict:
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=body, headers=web_headers(cookie), method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


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
    """批量拉取歌曲详情，每批最多 500 首。"""
    songs: list[dict] = []
    for i in range(0, len(song_ids), 500):
        chunk = song_ids[i : i + 500]
        c_param = json.dumps([{"id": sid} for sid in chunk])
        data = _web_post(cookie, SONG_DETAIL_API, {"c": c_param})
        songs.extend(data.get("songs") or [])
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
    """拉取歌单内全部歌曲元数据；tracks 不全时通过 trackIds 补全。"""
    data = _web_post(cookie, PLAYLIST_DETAIL_API, {"id": playlist_id, "n": 100000, "s": 8})
    pl = data.get("playlist") or {}
    tracks = pl.get("tracks") or []
    track_ids = [item["id"] for item in (pl.get("trackIds") or []) if "id" in item]
    if track_ids and len(tracks) < len(track_ids):
        by_id = {t["id"]: t for t in tracks if "id" in t}
        missing = [sid for sid in track_ids if sid not in by_id]
        for t in song_details_batch(cookie, missing):
            by_id[t["id"]] = t
        ordered = [by_id[sid] for sid in track_ids if sid in by_id]
        return [_normalize_track(t) for t in ordered]
    return [_normalize_track(t) for t in tracks]


_LRC_TIME_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


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


def song_lyric(cookie: str, song_id: int) -> list[tuple[float, str, str]]:
    """返回合并翻译的歌词行：[(秒, 原词, 译文)]。"""
    data = _web_post(cookie, LYRIC_API, {"id": song_id, "lv": -1, "tv": -1, "rv": -1, "kv": -1})
    orig = parse_lrc((data.get("lrc") or {}).get("lyric") or "")
    trans = parse_lrc((data.get("tlyric") or {}).get("lyric") or "")
    if not orig:
        return []
    trans_map: list[tuple[float, str]] = [(t, s) for t, s in trans if s]
    merged: list[tuple[float, str, str]] = []
    ti = 0
    for sec, text in orig:
        t_text = ""
        while ti < len(trans_map) and trans_map[ti][0] < sec - 0.35:
            ti += 1
        if ti < len(trans_map) and abs(trans_map[ti][0] - sec) <= 0.35:
            t_text = trans_map[ti][1]
        if text or t_text:
            merged.append((sec, text, t_text))
    return merged


def probe_playable_url(url: str) -> str:
    """按 CDN 降级顺序探测第一个可流式读取音频头的直链。"""
    for cand in download_candidates(url):
        req = urllib.request.Request(
            cand,
            headers={"User-Agent": UA_WEB, "Referer": REFERER, "Range": "bytes=0-15"},
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                head = resp.read(4)
                if head[:3] == b"ID3" or head[:2] == b"\xff\xfb" or head[:4] == b"fLaC" or head:
                    return cand
        except Exception:  # noqa: BLE001
            continue
    return url


def fetch_cover_ansi(pic_url: str, width: int = 34, height: int = 17) -> str:
    """下载专辑封面并通过 chafa 渲染为 ANSI 真彩字符画。"""
    if not pic_url:
        return ""
    sep = "&" if "?" in pic_url else "?"
    thumb_url = f"{pic_url}{sep}param=400y400"
    req = urllib.request.Request(thumb_url, headers={"User-Agent": UA_WEB, "Referer": REFERER})
    with urllib.request.urlopen(req, timeout=12) as resp:
        img_bytes = resp.read()
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
        timeout=10,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", "replace")


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
    detail = song_detail(cookie, song_id)
    info = stream_url(cookie, song_id, level)
    return detail, info


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


def main() -> int:
    ap = argparse.ArgumentParser(description="ncm-lite：cookie 直连网易云 web 接口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="扫码登录并保存 cookie").set_defaults(func=cmd_login)
    sub.add_parser("whoami", help="校验 cookie").set_defaults(func=cmd_whoami)
    p_tui = sub.add_parser("tui", help="启动 TUI 播放器")
    p_tui.add_argument("--level", default="lossless", choices=LEVELS)
    p_tui.set_defaults(func=cmd_tui)

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
