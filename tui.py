#!/usr/bin/env python3
"""ncm-lite TUI —— 网易云音乐无损流媒体终端播放器。"""
from __future__ import annotations

import asyncio
import bisect
import random
import sys
from typing import Any

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, Static

import ncm
from player import MpvPlayer


def fmt_time(seconds: float) -> str:
    if seconds <= 0:
        return "00:00"
    total = int(seconds)
    m, s = divmod(total, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class PlaylistItem(ListItem):
    def __init__(self, playlist: dict[str, Any]) -> None:
        self.playlist = playlist
        name = playlist.get("name") or "未命名歌单"
        count = playlist.get("trackCount") or 0
        tag = "♥ " if not playlist.get("subscribed") else "☆ "
        super().__init__(Label(f"{tag}{name} ({count})", markup=False))


class TrackItem(ListItem):
    def __init__(self, track: dict[str, Any], index: int, playing: bool = False) -> None:
        self.track = track
        self.track_index = index
        dur = fmt_time((track.get("duration_ms") or 0) / 1000.0)
        mark = "▶ " if playing else f"{index + 1:>3}. "
        title = f"{mark}{track['name']} - {track['artists']} [{dur}]"
        super().__init__(Label(title, markup=False))
        if playing:
            self.add_class("playing-track")


class LyricLine(Static):
    def __init__(self, sec: float, orig: str, trans: str, line_idx: int) -> None:
        self.sec = sec
        self.line_idx = line_idx
        content = orig if not trans else f"{orig}\n{trans}"
        super().__init__(content, markup=False)
        self.add_class("lyric-line")


class SeekBar(Static):
    """可点击跳转进度的文本进度条。"""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", markup=False, **kwargs)
        self.duration: float = 0.0
        self.position: float = 0.0

    def update_progress(self, position: float, duration: float) -> None:
        self.position = max(0.0, position)
        self.duration = max(0.0, duration)
        width = max(16, (self.size.width or 40) - 2)
        ratio = min(1.0, self.position / self.duration) if self.duration > 0 else 0.0
        filled = int(round(width * ratio))
        knob = "●" if width > 2 else ""
        if filled >= width:
            bar = "━" * (width - 1) + knob
        elif filled <= 0:
            bar = knob + "─" * (width - 1)
        else:
            bar = "━" * filled + knob + "─" * max(0, width - filled - 1)
        self.update(bar)

    def on_click(self, event: events.Click) -> None:
        if self.duration <= 0 or self.size.width <= 0:
            return
        ratio = max(0.0, min(1.0, event.x / max(1, self.size.width - 1)))
        target = ratio * self.duration
        app = self.app
        if isinstance(app, NcmTuiApp):
            app.seek_to(target)


class NcmTuiApp(App):
    TITLE = "ncm-lite"
    CSS = """
    Screen {
        background: #0f111a;
        color: #e4e7f0;
    }

    #top-bar {
        height: 3;
        background: #171a26;
        border-bottom: solid #2a3045;
        padding: 0 2;
        content-align: left middle;
        layout: horizontal;
    }

    #brand {
        width: 1fr;
        content-align: left middle;
        text-style: bold;
        color: #ff5c7c;
    }

    #user-badge {
        width: auto;
        content-align: right middle;
        color: #9aa5c4;
    }

    #main-area {
        height: 1fr;
    }

    #left-pane {
        width: 26%;
        min-width: 22;
        border-right: solid #262c40;
        background: #131622;
    }

    #mid-pane {
        width: 38%;
        min-width: 28;
        border-right: solid #262c40;
        background: #10131e;
    }

    #right-pane {
        width: 36%;
        min-width: 28;
        padding: 1 2;
        background: #0f111a;
    }

    .pane-header {
        height: 2;
        background: #181c2b;
        color: #c7d0ea;
        text-style: bold;
        padding: 0 1;
        content-align: left middle;
        border-bottom: solid #23293d;
    }

    #filter-input {
        height: 3;
        border: none;
        border-bottom: solid #262c40;
        background: #141824;
        padding: 0 1;
    }

    ListView {
        background: transparent;
        height: 1fr;
    }

    ListItem {
        padding: 0 1;
        height: auto;
    }

    ListItem.--highlight {
        background: #252c44;
        color: #ffffff;
    }

    .playing-track {
        color: #ff6b88;
        text-style: bold;
    }

    #cover-box {
        height: auto;
        min-height: 14;
        align: center middle;
        margin-bottom: 1;
    }

    #cover-art {
        width: auto;
        height: auto;
        content-align: center middle;
    }

    #now-title {
        text-style: bold;
        color: #ffffff;
        content-align: center middle;
        text-align: center;
        margin-top: 0;
    }

    #now-meta {
        color: #98a4c7;
        text-align: center;
        margin-bottom: 0;
    }

    #now-quality {
        color: #66d9a8;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #lyric-scroll {
        height: 1fr;
        border-top: solid #23293d;
        padding: 1 0;
    }

    .lyric-line {
        padding: 0 1;
        margin-bottom: 1;
        color: #7782a6;
        text-align: center;
    }

    .lyric-line:hover {
        color: #c5cde8;
    }

    .lyric-active {
        color: #ff6b88;
        text-style: bold;
        background: #1b1f30;
    }

    #player-bar {
        height: 5;
        background: #161926;
        border-top: solid #2a3045;
        padding: 0 2;
    }

    #progress-row {
        height: 2;
        align: left middle;
    }

    #time-cur, #time-dur {
        width: 8;
        color: #b4bedb;
        content-align: center middle;
    }

    #seek-bar {
        width: 1fr;
        color: #ff5c7c;
        content-align: center middle;
    }

    #controls-row {
        height: 3;
        align: left middle;
    }

    #controls-row Button {
        min-width: 6;
        height: 3;
        margin-right: 1;
        background: #23283b;
        border: none;
        color: #e4e7f0;
    }

    #controls-row Button:hover {
        background: #323954;
    }

    #btn-play {
        background: #ff5c7c;
        color: #ffffff;
        text-style: bold;
    }

    #status-label {
        width: 1fr;
        content-align: right middle;
        color: #9aa5c4;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_pause", "播放/暂停"),
        Binding("p", "toggle_pause", "暂停", show=False),
        Binding("n", "next_track", "下一首"),
        Binding("b", "prev_track", "上一首"),
        Binding("left", "seek_back", "后退5s"),
        Binding("right", "seek_fwd", "快进5s"),
        Binding("comma", "seek_back_15", "后退15s", show=False),
        Binding("full_stop", "seek_fwd_15", "快进15s", show=False),
        Binding("s", "toggle_shuffle", "随机/顺序"),
        Binding("x", "shuffle_current_playlist", "随机播当前歌单"),
        Binding("slash", "focus_filter", "搜索"),
        Binding("escape", "clear_filter", "清除搜索", show=False),
        Binding("minus", "vol_down", "音量-", key_display="-"),
        Binding("equals_sign", "vol_up", "音量+", key_display="="),
        Binding("plus", "vol_up", "音量+", show=False),
        Binding("r", "reload_playlists", "刷新歌单"),
        Binding("q", "quit_app", "退出"),
    ]

    def __init__(self, level: str = "lossless", cookie: str = "") -> None:
        super().__init__()
        self.level = level
        self.cookie = cookie
        self.user_info: dict[str, Any] = {}
        self.playlists: list[dict[str, Any]] = []
        self.viewing_tracks: list[dict[str, Any]] = []
        self.filtered_tracks: list[dict[str, Any]] = []

        self.play_queue: list[dict[str, Any]] = []
        self.play_queue_name: str = ""
        self.current_index: int | None = None
        self.current_track: dict[str, Any] | None = None
        self.shuffle_mode: bool = True
        self.shuffle_bag: list[int] = []
        self.history: list[int] = []

        self.lyrics: list[tuple[float, str, str]] = []
        self.lyric_times: list[float] = []
        self.active_lyric_idx: int = -1

        self._load_seq: int = 0
        self._consecutive_failures: int = 0
        self._cover_cache: dict[str, str] = {}
        self._player: MpvPlayer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tick_timer = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="top-bar"):
            yield Static("♪ ncm-lite · 无损流媒体播放器", id="brand")
            yield Static("校验账号中...", id="user-badge")

        with Horizontal(id="main-area"):
            with Vertical(id="left-pane"):
                yield Static("我的歌单", classes="pane-header")
                yield ListView(id="playlist-list")

            with Vertical(id="mid-pane"):
                yield Static("歌曲列表", id="track-header", classes="pane-header")
                yield Input(placeholder="按 / 搜索歌名、歌手、专辑...", id="filter-input")
                yield ListView(id="track-list")

            with Vertical(id="right-pane"):
                with Vertical(id="cover-box"):
                    yield Static("♪ 暂无播放", id="cover-art")
                yield Static("未播放歌曲", id="now-title")
                yield Static("从左侧选择歌单，按 Enter 或 x 开始播放", id="now-meta")
                yield Static(f"目标音质: {self.level.upper()}", id="now-quality")
                yield VerticalScroll(id="lyric-scroll")

        with Vertical(id="player-bar"):
            with Horizontal(id="progress-row"):
                yield Static("00:00", id="time-cur")
                yield SeekBar(id="seek-bar")
                yield Static("00:00", id="time-dur")
            with Horizontal(id="controls-row"):
                yield Button("⏮", id="btn-prev")
                yield Button("▶", id="btn-play")
                yield Button("⏭", id="btn-next")
                yield Button("🔀 随机", id="btn-mode")
                yield Button("↺ -5s", id="btn-back")
                yield Button("+5s ↻", id="btn-fwd")
                yield Static("音量 100% · 待机", id="status-label")
        yield Footer()

    async def on_mount(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._player = MpvPlayer(on_track_end=self._on_mpv_track_end)
        self._tick_timer = self.set_interval(0.25, self._tick_player_ui)
        asyncio.create_task(self._bootstrap())

    async def on_unmount(self) -> None:
        if self._tick_timer:
            self._tick_timer.stop()
            self._tick_timer = None
        if self._player:
            self._player.close()
            self._player = None

    def _on_mpv_track_end(self, reason: str) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._handle_track_end_on_loop, reason)

    def _handle_track_end_on_loop(self, reason: str) -> None:
        if reason == "eof":
            self._consecutive_failures = 0
            asyncio.create_task(self._play_next(auto=True))
        elif reason == "error":
            self.notify("音频流读取失败，尝试下一首", severity="warning")
            asyncio.create_task(self._play_next(auto=True))

    async def _bootstrap(self) -> None:
        try:
            me = await asyncio.to_thread(ncm.whoami, self.cookie)
            prof = me.get("profile") or {}
            uid = prof.get("userId")
            if me.get("code") != 200 or not uid:
                self.query_one("#user-badge", Static).update("cookie 已失效，请先执行 login")
                self.notify("cookie 无效或已过期，请先运行 ncm.py login", severity="error")
                return
            self.user_info = prof
            vip = "黑胶VIP" if prof.get("vipType") else "普通"
            self.query_one("#user-badge", Static).update(f"{prof.get('nickname', '')} · {vip}")

            playlists = await asyncio.to_thread(ncm.user_playlists, self.cookie, uid)
            self.playlists = playlists
            lv = self.query_one("#playlist-list", ListView)
            await lv.clear()
            for pl in playlists:
                await lv.append(PlaylistItem(pl))
            if playlists:
                lv.index = 0
                await self._load_playlist(playlists[0])
        except Exception as e:  # noqa: BLE001
            self.notify(f"初始化失败: {e}", severity="error")

    async def _load_playlist(self, playlist: dict[str, Any]) -> None:
        name = playlist.get("name") or "歌单"
        self.query_one("#track-header", Static).update(f"加载中: {name}...")
        try:
            tracks = await asyncio.to_thread(ncm.playlist_tracks, self.cookie, playlist["id"])
        except Exception as e:  # noqa: BLE001
            self.notify(f"加载歌单失败: {e}", severity="error")
            return
        self.viewing_tracks = tracks
        self.query_one("#filter-input", Input).value = ""
        await self._render_track_list(tracks, name)

    async def _render_track_list(self, tracks: list[dict[str, Any]], header_title: str | None = None) -> None:
        self.filtered_tracks = tracks
        if header_title is not None:
            self.query_one("#track-header", Static).update(f"{header_title} ({len(tracks)}首)")
        lv = self.query_one("#track-list", ListView)
        await lv.clear()
        playing_id = self.current_track["id"] if self.current_track else None
        for idx, tr in enumerate(tracks):
            await lv.append(TrackItem(tr, idx, playing=(tr["id"] == playing_id)))

    @on(ListView.Selected, "#playlist-list")
    async def on_playlist_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, PlaylistItem):
            await self._load_playlist(event.item.playlist)

    @on(ListView.Selected, "#track-list")
    async def on_track_selected(self, event: ListView.Selected) -> None:
        if not isinstance(event.item, TrackItem):
            return
        selected = event.item.track
        # 将当前浏览歌单设为播放队列
        self.play_queue = list(self.viewing_tracks)
        pl_lv = self.query_one("#playlist-list", ListView)
        if pl_lv.highlighted_child and isinstance(pl_lv.highlighted_child, PlaylistItem):
            self.play_queue_name = pl_lv.highlighted_child.playlist.get("name") or ""
        try:
            q_idx = next(i for i, t in enumerate(self.play_queue) if t["id"] == selected["id"])
        except StopIteration:
            q_idx = 0
        self.shuffle_bag.clear()
        if self.current_index is not None:
            self.history.append(self.current_index)
        await self._play_index(q_idx)

    @on(Input.Changed, "#filter-input")
    async def on_filter_changed(self, event: Input.Changed) -> None:
        kw = event.value.strip().lower()
        if not kw:
            await self._render_track_list(self.viewing_tracks)
            return
        matched = [
            t
            for t in self.viewing_tracks
            if kw in t["name"].lower() or kw in t["artists"].lower() or kw in t["album"].lower()
        ]
        await self._render_track_list(matched)

    @on(Button.Pressed)
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-play":
            self.action_toggle_pause()
        elif bid == "btn-next":
            await self.action_next_track()
        elif bid == "btn-prev":
            await self.action_prev_track()
        elif bid == "btn-mode":
            self.action_toggle_shuffle()
        elif bid == "btn-back":
            self.action_seek_back()
        elif bid == "btn-fwd":
            self.action_seek_fwd()

    def _pick_next_shuffle_index(self) -> int | None:
        n = len(self.play_queue)
        if n == 0:
            return None
        if n == 1:
            return 0
        if not self.shuffle_bag:
            self.shuffle_bag = [i for i in range(n) if i != self.current_index]
            random.shuffle(self.shuffle_bag)
        return self.shuffle_bag.pop()

    async def _play_next(self, auto: bool = False) -> None:
        if not self.play_queue:
            if self.viewing_tracks:
                self.play_queue = list(self.viewing_tracks)
                self.shuffle_bag.clear()
            else:
                return
        if self.current_index is not None:
            self.history.append(self.current_index)
            if len(self.history) > 200:
                self.history.pop(0)
        if self.shuffle_mode:
            nxt = self._pick_next_shuffle_index()
        else:
            nxt = 0 if self.current_index is None else (self.current_index + 1) % len(self.play_queue)
        if nxt is not None:
            await self._play_index(nxt, auto=auto)

    async def _play_index(self, idx: int, auto: bool = False) -> None:
        if not (0 <= idx < len(self.play_queue)) or not self._player:
            return
        self._load_seq += 1
        seq = self._load_seq
        track = self.play_queue[idx]
        self.current_index = idx
        self.current_track = track

        self.query_one("#now-title", Static).update(track["name"])
        self.query_one("#now-meta", Static).update(f"{track['artists']} · {track['album']}")
        self.query_one("#now-quality", Static).update(f"正在解析 {self.level.upper()} 直链...")
        self._refresh_playing_marker()

        try:
            info = await asyncio.to_thread(ncm.stream_url, self.cookie, track["id"], self.level)
        except Exception as e:  # noqa: BLE001
            if seq != self._load_seq:
                return
            self.notify(f"获取直链出错: {e}", severity="error")
            await self._skip_on_failure(track)
            return

        if seq != self._load_seq:
            return

        raw_url = info.get("url")
        if not raw_url:
            self.notify(f"跳过无版权/不可播曲目: {track['name']}", severity="warning")
            await self._skip_on_failure(track)
            return

        playable_url = await asyncio.to_thread(ncm.probe_playable_url, raw_url)
        if seq != self._load_seq:
            return

        ok = self._player.play(playable_url)
        if not ok:
            self.notify(f"播放器加载失败: {track['name']}", severity="warning")
            await self._skip_on_failure(track)
            return

        self._consecutive_failures = 0
        lv = (info.get("level") or self.level).upper()
        fmt = (info.get("type") or "flac").upper()
        br = int((info.get("br") or 0) / 1000)
        size_mb = round((info.get("size") or 0) / 1048576, 1)
        self.query_one("#now-quality", Static).update(f"{lv} · {fmt} · {br}kbps · {size_mb}MB")

        # 并行加载封面与歌词
        asyncio.create_task(self._load_cover_and_lyrics(track, seq))

    async def _skip_on_failure(self, track: dict[str, Any]) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= min(8, max(1, len(self.play_queue))):
            self.query_one("#now-quality", Static).update("连续多首不可播，已停止自动跳过")
            self._consecutive_failures = 0
            return
        await asyncio.sleep(0.3)
        await self._play_next(auto=True)

    async def _load_cover_and_lyrics(self, track: dict[str, Any], seq: int) -> None:
        pic_url = track.get("pic_url") or ""
        cover_widget = self.query_one("#cover-art", Static)
        if pic_url:
            if pic_url in self._cover_cache:
                ansi = self._cover_cache[pic_url]
            else:
                cover_box = self.query_one("#cover-box")
                w = max(24, min(42, (cover_box.size.width or 36) - 4))
                h = max(12, min(20, w // 2))
                try:
                    ansi = await asyncio.to_thread(ncm.fetch_cover_ansi, pic_url, w, h)
                except Exception:  # noqa: BLE001
                    ansi = ""
                if ansi:
                    if len(self._cover_cache) > 40:
                        self._cover_cache.clear()
                    self._cover_cache[pic_url] = ansi
            if seq != self._load_seq:
                return
            if ansi:
                cover_widget.update(Text.from_ansi(ansi))
            else:
                cover_widget.update("♪ 封面加载失败")
        else:
            cover_widget.update("♪ 无专辑封面")

        try:
            lyrics = await asyncio.to_thread(ncm.song_lyric, self.cookie, track["id"])
        except Exception:  # noqa: BLE001
            lyrics = []
        if seq != self._load_seq:
            return
        await self._render_lyrics(lyrics)

    async def _render_lyrics(self, lyrics: list[tuple[float, str, str]]) -> None:
        self.lyrics = lyrics
        self.lyric_times = [row[0] for row in lyrics]
        self.active_lyric_idx = -1
        scroll = self.query_one("#lyric-scroll", VerticalScroll)
        await scroll.remove_children()
        if not lyrics:
            await scroll.mount(Static("纯音乐 / 暂无歌词", classes="lyric-line"))
            return
        widgets = [LyricLine(sec, orig, trans, i) for i, (sec, orig, trans) in enumerate(lyrics)]
        await scroll.mount_all(widgets)

    @on(events.Click)
    def on_any_click(self, event: events.Click) -> None:
        if isinstance(event.widget, LyricLine):
            self.seek_to(event.widget.sec)

    def _refresh_playing_marker(self) -> None:
        lv = self.query_one("#track-list", ListView)
        playing_id = self.current_track["id"] if self.current_track else None
        for item in lv.children:
            if isinstance(item, TrackItem):
                is_cur = item.track["id"] == playing_id
                item.set_class(is_cur, "playing-track")
                lbl = item.query_one(Label)
                dur = fmt_time((item.track.get("duration_ms") or 0) / 1000.0)
                mark = "▶ " if is_cur else f"{item.track_index + 1:>3}. "
                lbl.update(f"{mark}{item.track['name']} - {item.track['artists']} [{dur}]")

    def _tick_player_ui(self) -> None:
        if not self._player or not self.is_mounted:
            return
        try:
            time_cur = self.query_one("#time-cur", Static)
            time_dur = self.query_one("#time-dur", Static)
            seek_bar = self.query_one(SeekBar)
            play_btn = self.query_one("#btn-play", Button)
            status_lbl = self.query_one("#status-label", Static)
        except Exception:  # noqa: BLE001
            return
        st = self._player.get_state()
        dur = st.duration
        if dur <= 0 and self.current_track:
            dur = (self.current_track.get("duration_ms") or 0) / 1000.0
        pos = st.time_pos

        time_cur.update(fmt_time(pos))
        time_dur.update(fmt_time(dur))
        seek_bar.update_progress(pos, dur)

        play_btn.label = "▶" if (st.paused or not st.playing) else "⏸"

        state_txt = "缓冲中..." if st.buffering else ("已暂停" if st.paused else ("播放中" if st.playing else "待机"))
        status_lbl.update(f"音量 {int(st.volume)}% · {state_txt}")

        if self.lyric_times and st.playing:
            idx = bisect.bisect_right(self.lyric_times, pos + 0.15) - 1
            if idx != self.active_lyric_idx:
                scroll = self.query_one("#lyric-scroll", VerticalScroll)
                lines = list(scroll.query(LyricLine))
                if 0 <= self.active_lyric_idx < len(lines):
                    lines[self.active_lyric_idx].remove_class("lyric-active")
                self.active_lyric_idx = idx
                if 0 <= idx < len(lines):
                    target = lines[idx]
                    target.add_class("lyric-active")
                    scroll.scroll_to_widget(target, animate=False, force=True, center=True)

    def seek_to(self, seconds: float) -> None:
        if self._player:
            self._player.seek_to(seconds)

    def action_toggle_pause(self) -> None:
        if not self._player:
            return
        if not self.current_track and self.viewing_tracks:
            asyncio.create_task(self.action_shuffle_current_playlist())
            return
        self._player.toggle_pause()

    async def action_next_track(self) -> None:
        await self._play_next(auto=False)

    async def action_prev_track(self) -> None:
        if not self.play_queue:
            return
        if self.history:
            prev = self.history.pop()
        elif self.current_index is not None:
            prev = (self.current_index - 1) % len(self.play_queue)
        else:
            prev = 0
        await self._play_index(prev)

    def action_seek_back(self) -> None:
        if self._player:
            self._player.seek_relative(-5.0)

    def action_seek_fwd(self) -> None:
        if self._player:
            self._player.seek_relative(5.0)

    def action_seek_back_15(self) -> None:
        if self._player:
            self._player.seek_relative(-15.0)

    def action_seek_fwd_15(self) -> None:
        if self._player:
            self._player.seek_relative(15.0)

    def action_toggle_shuffle(self) -> None:
        self.shuffle_mode = not self.shuffle_mode
        self.shuffle_bag.clear()
        btn = self.query_one("#btn-mode", Button)
        btn.label = "🔀 随机" if self.shuffle_mode else "🔁 顺序"
        self.notify("已切换为随机播放" if self.shuffle_mode else "已切换为列表顺序循环")

    async def action_shuffle_current_playlist(self) -> None:
        if not self.viewing_tracks:
            return
        self.play_queue = list(self.viewing_tracks)
        self.shuffle_mode = True
        self.shuffle_bag.clear()
        self.query_one("#btn-mode", Button).label = "🔀 随机"
        idx = random.randrange(len(self.play_queue))
        await self._play_index(idx)

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_clear_filter(self) -> None:
        inp = self.query_one("#filter-input", Input)
        if inp.value:
            inp.value = ""
        self.query_one("#track-list", ListView).focus()

    def action_vol_down(self) -> None:
        if self._player:
            self._player.add_volume(-5.0)

    def action_vol_up(self) -> None:
        if self._player:
            self._player.add_volume(5.0)

    async def action_reload_playlists(self) -> None:
        await self._bootstrap()
        self.notify("歌单已刷新")

    async def action_quit_app(self) -> None:
        if self._tick_timer:
            self._tick_timer.stop()
            self._tick_timer = None
        if self._player:
            self._player.close()
            self._player = None
        self.exit()


def run_tui(level: str = "lossless") -> int:
    cookie = ncm.load_cookie()
    app = NcmTuiApp(level=level, cookie=cookie)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(run_tui())
