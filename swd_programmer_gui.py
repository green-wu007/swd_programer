#!/usr/bin/env python3
"""
SWD Multi-Channel Programmer GUI
Built with Flet (Flutter for Python)

Install:
    pip install flet pyserial

Run:
    python swd_programmer_gui.py
"""

import flet as ft
import threading
import time
import random
import os
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

# Optional: real COM port listing
try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

# ─────────────────────────────────────────────────────────────────────────────
#  Utilities
# ─────────────────────────────────────────────────────────────────────────────

def crc16_calc(data: bytes) -> str:
    """Standard CRC-16/CCITT-FALSE."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return f"{(crc ^ 0xFFFF):04X}"


def fake_crc(s: str) -> str:
    return crc16_calc((s + str(random.random())).encode())


def fake_mac() -> str:
    return ':'.join(f'{random.randint(0, 255):02X}' for _ in range(6))


def now_ts() -> str:
    return datetime.now().strftime('%H:%M:%S')


def _fmt_mtime(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime(
            '%Y-%m-%d %H:%M')
    except OSError:
        return '—'


# ─────────────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────────────

BASE_WIDTH = 1580          # 1480 + 200；與預設視窗寬一致（見 page.window.width）
MIN_SCALE  = 0.70
MAX_SCALE  = 2.00
FONT_BOOST = 1.45          # larger global font scaling

CHIPS = [
    '', '', '', '',
    '', '', '', '',
]

SWD_FREQS = ['1 MHz', '2 MHz', '4 MHz', '8 MHz', '16 MHz', '32 MHz']

# Channel card colours
CH_FG = {
    'idle':      '#666666',
    'detecting': '#EF9F27',
    'running':   '#378ADD',
    'done_ok':   '#1D9E75',
    'done_err':  '#E24B4A',
    'no_target': '#444444',
}
CH_LABEL = {
    'idle':      '待機',
    'detecting': '偵測中',
    'running':   '燒錄中',
    'done_ok':   '完成',
    'done_err':  'CRC 錯誤',
    'no_target': '無裝置',
}
BAR_CLR = {
    'idle':      '#444444',
    'detecting': '#EF9F27',
    'running':   '#378ADD',
    'done_ok':   '#1D9E75',
    'done_err':  '#E24B4A',
    'no_target': '#333333',
}
LEFT_CLR = {        # left accent border per state
    'idle':      '#444444',
    'detecting': '#EF9F27',
    'running':   '#4A9EDB',
    'done_ok':   '#1D9E75',
    'done_err':  '#E24B4A',
    'no_target': '#333333',
}

LOG_CLR = {
    'info':  ft.Colors.BLUE_300,
    'ok':    ft.Colors.GREEN_400,
    'warn':  ft.Colors.ORANGE_400,
    'err':   ft.Colors.RED_400,
    'muted': ft.Colors.GREY_500,
}


# ─────────────────────────────────────────────────────────────────────────────
#  App State
# ─────────────────────────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.files = [
            {'name': 'app_v1.2.3.hex',       'size': '298 KB',
             'date': '2026-03-14 10:22', 'crc': 'A3F1', 'active': True},
            {'name': 'bootloader_v3.hex',     'size': '48 KB',
             'date': '2026-03-14 09:55', 'crc': '2C8D', 'active': False},
            {'name': 'softdevice_s140.hex',   'size': '156 KB',
             'date': '2026-03-13 17:30', 'crc': 'E7B2', 'active': False},
        ]
        self.ch_count  = 4
        self.swd_freq  = '8 MHz'
        self.opts      = {'verify': True, 'reset': True,
                          'auto': False,  'autoId': False}
        self.running   = False
        self.connected = False
        self.com_port  = ''
        self.com_labels: dict[str, str] = {}
        self.logs      = []
        self.log_seq   = 0
        self.flash_ok_count = 0          # cumulative
        self.flash_err_count = 0         # cumulative
        self.flash_ok_session = 0        # per run
        self.flash_err_session = 0       # per run
        self.scale     = 1.0          # font scale factor
        self.log_height = 220
        self.sidebar_width = 248
        self.toolbar_height = 78
        self._init_channels()

    # ── helpers ──────────────────────────────────────────────────────────────

    def _init_channels(self):
        self.ch_state = [
            {
                'id':         i,
                'chip':       CHIPS[i],
                'mac':        fake_mac(),
                'status':     'idle',
                'pct':        0.0,
                'crc':        '',
                'crc_expect': '',
                'speed':      '—',
                'time':       '—',
            }
            for i in range(8)
        ]

    def active_file(self) -> Optional[dict]:
        return next((f for f in self.files if f['active']), None)

    def fs(self, base: int) -> int:
        """Return font size with global +8 boost."""
        return max(8, int(base + 8))


# ─────────────────────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────────────────────

def main(page: ft.Page):
    page.title          = "SWD Multi-Channel Programmer"
    # Flet 0.28+ 以 page.window 設定原生視窗（單設 window_width 可能無效）
    page.window.width = 1580
    page.window.height = 860
    page.window.min_width = 900
    page.window.min_height = 640
    page.bgcolor        = ft.Colors.SURFACE
    page.padding        = 0
    page.theme_mode     = ft.ThemeMode.DARK
    page.text_scale_factor = FONT_BOOST
    page.theme = ft.Theme(font_family="Sarasa Gothic TC")
    page.dark_theme = ft.Theme(font_family="Sarasa Gothic TC")

    state = AppState()

    # ── File picker ───────────────────────────────────────────────────────────
    def _on_file_pick(files):
        if not files:
            return
        for f in files:
            if not any(x['name'] == f.name for x in state.files):
                kb = max(1, (f.size or 0) // 1024)
                path_str = getattr(f, 'path', None) or ''
                pobj = Path(path_str) if path_str else None
                date_s = (
                    _fmt_mtime(pobj)
                    if pobj and pobj.is_file() else
                    datetime.now().strftime('%Y-%m-%d %H:%M'))
                entry = {
                    'name':   f.name,
                    'size':   f'{kb} KB',
                    'date':   date_s,
                    'crc':    fake_crc(f.name),
                    'active': False,
                }
                if pobj and pobj.is_file():
                    entry['path'] = str(pobj.resolve())
                state.files.append(entry)
                _add_log('SYS', 'muted', f'加入: {f.name}')
        if not any(f['active'] for f in state.files) and state.files:
            state.files[0]['active'] = True
        _rebuild_file_list()
        page.update()
        if state.opts['auto'] and not state.running:
            _auto_sequence()

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    async def _pick_files(_=None):
        picked = await file_picker.pick_files(
            allowed_extensions=['hex', 'bin'],
            allow_multiple=True,
        )
        _on_file_pick(picked)

    async def _exit_app():
        await page.window.close()

    def _on_drop_hover(e: ft.HoverEvent):
        if not r_drop_zone.current:
            return
        hovering = e.data == "true"
        r_drop_zone.current.border = ft.Border.all(
            1.5, ft.Colors.BLUE_400 if hovering else ft.Colors.OUTLINE_VARIANT
        )
        r_drop_zone.current.bgcolor = "#1A2633" if hovering else None
        page.update()

    # ── Refs ──────────────────────────────────────────────────────────────────
    r_log_list    = ft.Ref[ft.ListView]()
    r_file_col    = ft.Ref[ft.ReorderableListView]()
    r_ch_col      = ft.Ref[ft.Column]()
    r_status      = ft.Ref[ft.Text]()
    r_conn_status = ft.Ref[ft.Text]()
    r_conn_btn    = ft.Ref[ft.OutlinedButton]()
    r_com_dd      = ft.Ref[ft.Dropdown]()
    r_ch4_btn     = ft.Ref[ft.OutlinedButton]()
    r_ch8_btn     = ft.Ref[ft.OutlinedButton]()
    r_start_btn   = ft.Ref[ft.FilledButton]()
    r_log_cnt     = ft.Ref[ft.Text]()
    r_ok_cnt      = ft.Ref[ft.Text]()
    r_err_cnt     = ft.Ref[ft.Text]()
    r_ok_session  = ft.Ref[ft.Text]()
    r_err_session = ft.Ref[ft.Text]()
    r_sb_auto     = ft.Ref[ft.Text]()
    r_sb_autoid   = ft.Ref[ft.Text]()
    r_sb_prog     = ft.Ref[ft.Text]()
    r_sb_clk      = ft.Ref[ft.Text]()
    r_drop_zone   = ft.Ref[ft.Container]()
    r_path_field  = ft.Ref[ft.TextField]()
    r_log_panel   = ft.Ref[ft.Container]()
    r_sidebar     = ft.Ref[ft.Container]()
    r_toolbar     = ft.Ref[ft.Container]()
    serial_conn = {"port": None}

    # ── Font scale on resize ──────────────────────────────────────────────────
    def _on_resize(e):
        w = page.window.width if page.window.width is not None else BASE_WIDTH
        state.scale = max(MIN_SCALE, min(MAX_SCALE, w / BASE_WIDTH))
        page.text_scale_factor = state.scale * FONT_BOOST
        _rebuild_channel_grid()
        _rebuild_file_list()
        page.update()

    page.on_resize = _on_resize

    MIN_SIDEBAR_W = 180
    MAX_SIDEBAR_W = 520
    MIN_TOOLBAR_H = 56
    MAX_TOOLBAR_H = 220

    def _on_sidebar_split_drag(e: ft.DragUpdateEvent):
        gd = e.global_delta
        dx = float(gd.x) if gd else 0.0
        state.sidebar_width = int(max(
            MIN_SIDEBAR_W,
            min(MAX_SIDEBAR_W, state.sidebar_width + dx)))
        if r_sidebar.current:
            r_sidebar.current.width = state.sidebar_width
        page.update()

    def _on_toolbar_split_drag(e: ft.DragUpdateEvent):
        # 保留分隔線控制樣式，但不再改變 Toolbar 高度
        return

    # ── Logging ───────────────────────────────────────────────────────────────
    def _add_log(ch: str, kind: str, msg: str, mac: str = ''):
        state.log_seq += 1
        idx = state.log_seq
        color    = LOG_CLR.get(kind, ft.Colors.GREY_500)
        mac_ctrl = (
            ft.Text(f'[{mac}] ', size=state.fs(10),
                    color='#9B8FE8', no_wrap=True)
            if mac else ft.Text('', width=0)
        )
        row = ft.Row(
            controls=[
                ft.Text(now_ts(), size=state.fs(10), color=ft.Colors.GREY_600,
                        width=state.fs(68), no_wrap=True),
                ft.Text(f'#{idx}', size=state.fs(10), color=ft.Colors.GREY_500,
                        width=state.fs(46), no_wrap=True),
                ft.Text(ch,       size=state.fs(10), color=color,
                        weight=ft.FontWeight.BOLD,
                        width=state.fs(34), no_wrap=True),
                mac_ctrl,
                ft.Text(msg,      size=state.fs(10), color=color, expand=True),
                ft.Text(
                    f'本次 S:{state.flash_ok_session} F:{state.flash_err_session} | '
                    f'累積 S:{state.flash_ok_count} F:{state.flash_err_count}',
                    size=state.fs(9), color=ft.Colors.GREY_500, no_wrap=True),
            ],
            spacing=4,
        )
        state.logs.insert(0, {'idx': idx, 'ts': now_ts(), 'ch': ch, 'kind': kind,
                              'msg': msg, 'mac': mac})
        if r_log_list.current:
            ctrls = r_log_list.current.controls
            ctrls.insert(0, row)
            if len(ctrls) > 300:
                ctrls.pop()
            if r_log_cnt.current:
                r_log_cnt.current.value = f'{len(state.logs)} 筆'
            if r_ok_cnt.current:
                r_ok_cnt.current.value = f'累積成功 {state.flash_ok_count}'
            if r_err_cnt.current:
                r_err_cnt.current.value = f'累積失敗 {state.flash_err_count}'
            if r_ok_session.current:
                r_ok_session.current.value = f'本次成功 {state.flash_ok_session}'
            if r_err_session.current:
                r_err_session.current.value = f'本次失敗 {state.flash_err_session}'
            page.update()

    def _clear_log(_=None):
        state.logs.clear()
        if r_log_list.current:
            r_log_list.current.controls.clear()
            if r_log_cnt.current:
                r_log_cnt.current.value = '0 筆'
            page.update()

    def _reset_counters(_=None):
        state.flash_ok_count = 0
        state.flash_err_count = 0
        state.flash_ok_session = 0
        state.flash_err_session = 0
        if r_ok_cnt.current:
            r_ok_cnt.current.value = '累積成功 0'
        if r_err_cnt.current:
            r_err_cnt.current.value = '累積失敗 0'
        if r_ok_session.current:
            r_ok_session.current.value = '本次成功 0'
        if r_err_session.current:
            r_err_session.current.value = '本次失敗 0'
        _add_log('SYS', 'muted', '已重置燒錄成功/失敗計數')

    def _log_item_exportable(item: dict) -> bool:
        ch = item.get("ch", "")
        msg = item.get("msg", "")
        kind = item.get("kind", "")
        if ch == "SYS" and "▶ 燒錄:" in msg:
            return True
        if ch.startswith("CH") and kind == "ok" and "完成" in msg:
            return True
        if ch.startswith("CH") and kind == "err" and "CRC 錯誤" in msg:
            return True
        if ch == "SYS" and kind in ("ok", "err"):
            if "所有通道燒錄完成" in msg or "個通道 CRC 錯誤" in msg:
                return True
        return False

    def _build_export_log_text() -> str:
        rows = []
        for item in reversed(state.logs):
            if not _log_item_exportable(item):
                continue
            msg = item.get("msg", "")
            if "本次" in msg:
                continue
            mac_part = f' [{item["mac"]}]' if item.get("mac") else ""
            rows.append(
                f'#{item["idx"]} {item["ts"]} {item["ch"]}{mac_part} {msg}'
            )
        return "\n".join(rows) + ("\n" if rows else "")

    def _default_log_name() -> str:
        return datetime.now().strftime("SWD_%Y%m%d%H%M.log")

    def _write_log_file(path: Path, chunk: str) -> tuple[bool, str]:
        """Returns (is_append, status_message)."""
        exists = path.exists()
        mode = "a" if exists else "w"
        with path.open(mode, encoding="utf-8") as f:
            f.write(chunk)
        action = "附加" if exists else "新建"
        return exists, action

    def _save_log(_=None):
        chunk = _build_export_log_text()
        if not chunk.strip():
            _add_log('SYS', 'warn', '無符合條件的 Log 可儲存（僅匯出燒錄檔/完成/CRC 錯誤相關）')
            return
        path = Path(_default_log_name())
        try:
            _, action = _write_log_file(path, chunk)
            _add_log('SYS', 'ok', f'Log 已儲存（{action}）: {path.resolve()}')
        except Exception as ex:
            _add_log('SYS', 'err', f'Log 儲存失敗: {ex}')

    async def _save_log_as():
        chunk = _build_export_log_text()
        if not chunk.strip():
            _add_log('SYS', 'warn', '無符合條件的 Log 可另存')
            return
        target = await file_picker.save_file(
            dialog_title="另存 Log",
            file_name=_default_log_name(),
            allowed_extensions=["log"],
        )
        if not target:
            return
        try:
            out = Path(target)
            action = _write_log_file(out, chunk)[1]
            _add_log('SYS', 'ok', f'Log 已另存（{action}）: {out.resolve()}')
        except Exception as ex:
            _add_log('SYS', 'err', f'Log 另存失敗: {ex}')

    # ── COM port ──────────────────────────────────────────────────────────────
    def _list_ports() -> list[dict]:
        if HAS_SERIAL:
            ports = list(serial.tools.list_ports.comports())
            if ports:
                return [
                    {
                        "device": p.device,
                        "label": f'{p.device} - {getattr(p, "description", "") or "Unknown device"}',
                    }
                    for p in ports
                ]
        return [{"device": p, "label": p} for p in _fake_ports()]

    def _fake_ports() -> list[str]:
        if os.name == 'nt':
            return ['']
        return ['']

    def _sync_com_dropdown_surface():
        if not r_com_dd.current:
            return
        lbl = state.com_labels.get(state.com_port, state.com_port or '')
        r_com_dd.current.text = lbl

    def _apply_port_list(ports: list[dict], *, notify_scan: bool = False,
                         notify_hw_change: bool = False):
        state.com_labels = {p["device"]: p["label"] for p in ports}
        devices = [p["device"] for p in ports]
        prev = state.com_port

        if state.connected and prev and prev not in devices:
            state.connected = False
            _close_serial()
            if r_conn_status.current:
                r_conn_status.current.value = '● 未連線'
                r_conn_status.current.color = ft.Colors.RED_400
            if r_conn_btn.current:
                r_conn_btn.current.content = ft.Text('連線', size=19)
            if r_sb_prog.current:
                r_sb_prog.current.value = 'Programmer: nRF54LM20A (未連線)'
            _add_log('SYS', 'warn', f'已斷線：目前 COM ({prev}) 已自系統移除')

        if devices:
            state.com_port = prev if prev in devices else devices[0]
        else:
            state.com_port = ''

        if r_com_dd.current:
            r_com_dd.current.options = [
                ft.dropdown.Option(
                    key=p["device"],
                    text=p["label"],
                    content=ft.Text(p["label"], size=25, no_wrap=True),
                )
                for p in ports
            ]
            r_com_dd.current.value = state.com_port if state.com_port else None
            _sync_com_dropdown_surface()

        if notify_scan:
            _add_log('SYS', 'muted', f'掃描 COM port：{len(devices)} 個')
        if notify_hw_change:
            _add_log('SYS', 'info', '偵測到 COM 硬體清單變更，已更新連線狀態')
        page.update()

    def _refresh_ports(_=None):
        _apply_port_list(_list_ports(), notify_scan=True)

    def _on_com_change(e):
        state.com_port = e.control.value or ''
        _sync_com_dropdown_surface()
        _add_log('SYS', 'muted',
                 f'COM Port 切換為 {state.com_labels.get(state.com_port, state.com_port)}')
        page.update()

    def _port_watch_loop():
        last_sig: Optional[tuple] = None
        first = True
        while True:
            time.sleep(2.0)
            try:
                ports = _list_ports()
                sig = tuple((p["device"], p["label"]) for p in ports)
                if first:
                    last_sig = sig
                    first = False
                    continue
                if sig == last_sig:
                    continue
                last_sig = sig
                _apply_port_list(ports, notify_hw_change=True)
            except Exception:
                continue

    def _close_serial():
        sp = serial_conn["port"]
        if sp:
            try:
                sp.close()
            except Exception:
                pass
        serial_conn["port"] = None

    def _serial_send(payload: str):
        sp = serial_conn["port"]
        if not sp:
            return
        try:
            sp.write((payload + "\n").encode("utf-8"))
        except Exception as ex:
            _add_log('SYS', 'err', f'串口傳送失敗: {ex}')

    def _on_connect(_):
        if state.connected:
            state.connected = False
            _close_serial()
            if r_conn_status.current:
                r_conn_status.current.value = '● 未連線'
                r_conn_status.current.color = ft.Colors.RED_400
            if r_conn_btn.current:
                r_conn_btn.current.content = ft.Text('連線', size=19)
            if r_sb_prog.current:
                r_sb_prog.current.value = 'Programmer: nRF54LM20A (未連線)'
            _add_log('SYS', 'warn', f'已斷線 ({state.com_port})')
        else:
            if not state.com_port:
                _add_log('SYS', 'warn', '請先選擇 COM port')
                return
            if not HAS_SERIAL:
                _add_log('SYS', 'err', 'pyserial 未安裝，無法連線實體 COM port')
                return
            try:
                serial_conn["port"] = serial.Serial(
                    port=state.com_port, baudrate=115200, timeout=0.2
                )
            except Exception as ex:
                _add_log('SYS', 'err', f'連線失敗: {ex}')
                return
            state.connected = True
            if r_conn_status.current:
                r_conn_status.current.value = f'● {state.com_port}'
                r_conn_status.current.color = ft.Colors.GREEN_400
            if r_conn_btn.current:
                r_conn_btn.current.content = ft.Text('斷線', size=19)
            if r_sb_prog.current:
                r_sb_prog.current.value = f'Programmer: nRF54LM20A ({state.com_port})'
            _add_log('SYS', 'ok', f'已連線 Programmer ({state.com_port})')
            _serial_send("PING")
        page.update()

    # ── File list ─────────────────────────────────────────────────────────────
    def _select_file(i: int):
        for j, f in enumerate(state.files):
            f['active'] = (j == i)
        _rebuild_file_list()
        page.update()

    def _remove_selected(_=None):
        idx = next((i for i, f in enumerate(state.files) if f['active']), -1)
        if idx < 0:
            return
        name = state.files[idx]['name']
        state.files.pop(idx)
        if state.files:
            state.files[min(idx, len(state.files) - 1)]['active'] = True
        _add_log('SYS', 'warn', f'移除: {name}')
        _rebuild_file_list()
        page.update()

    def _build_file_item(f: dict, i: int) -> ft.Container:
        active      = f['active']
        bg          = '#1E3A5F' if active else '#242424'
        border_clr  = '#378ADD' if active else ft.Colors.OUTLINE_VARIANT
        crc_lbl_clr = ft.Colors.BLUE_200 if active else ft.Colors.OUTLINE
        crc_val_clr = '#5BB8FF' if active else '#185FA5'

        return ft.Container(
            key=f"file-{f['name']}-{f['date']}",
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text(
                                f['name'], size=state.fs(11),
                                weight=ft.FontWeight.W_500,
                                color=ft.Colors.BLUE_200 if active
                                else ft.Colors.ON_SURFACE,
                                expand=True, no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Container(
                                content=ft.Text(
                                    f['size'], size=state.fs(9),
                                    color=ft.Colors.BLUE_300 if active
                                    else ft.Colors.OUTLINE,
                                ),
                                bgcolor=ft.Colors.BLUE_900 if active
                                else '#242424',
                                border_radius=3,
                                padding=ft.Padding.symmetric(
                                    horizontal=4, vertical=1),
                            ),
                        ],
                        spacing=6,
                    ),
                    ft.Row(
                        controls=[
                            ft.Text('CRC16 ', size=state.fs(16),
                                    color=crc_lbl_clr),
                            ft.Text(f['crc'],  size=state.fs(33),
                                    weight=ft.FontWeight.W_500,
                                    color=crc_val_clr,
                                    font_family='monospace'),
                        ],
                        spacing=2,
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    ),
                    ft.Text(
                            (_fmt_mtime(Path(f['path']))
                             if f.get('path') else f['date']),
                            size=state.fs(10),
                            color=ft.Colors.BLUE_200 if active
                            else ft.Colors.OUTLINE),
                ],
                spacing=3,
                tight=True,
            ),
            padding=ft.Padding.symmetric(horizontal=8, vertical=8),
            border_radius=6,
            bgcolor=bg,
            border=ft.Border.all(0.5, border_clr),
            on_click=lambda _, idx=i: _select_file(idx),
            ink=True,
        )

    def _rebuild_file_list():
        if not r_file_col.current:
            return
        r_file_col.current.controls = [
            _build_file_item(f, i) for i, f in enumerate(state.files)
        ]

    def _import_paths_from_text(raw: str):
        parts = re.split(r'[\r\n;]+', raw.strip())
        for s in parts:
            s = s.strip().strip('"').strip("'")
            if not s:
                continue
            p = Path(s)
            if not p.is_file():
                _add_log('SYS', 'warn', f'路徑無效或非檔案: {s}')
                continue
            if p.suffix.lower() not in ('.hex', '.bin'):
                _add_log('SYS', 'warn', f'僅支援 .hex / .bin: {p.name}')
                continue
            if any(x['name'] == p.name for x in state.files):
                continue
            kb = max(1, p.stat().st_size // 1024)
            state.files.append({
                'name':   p.name,
                'size':   f'{kb} KB',
                'date':   _fmt_mtime(p),
                'crc':    fake_crc(p.name),
                'active': False,
                'path':   str(p.resolve()),
            })
            _add_log('SYS', 'muted', f'加入: {p.name}')
        if not any(f['active'] for f in state.files) and state.files:
            state.files[0]['active'] = True
        _rebuild_file_list()
        page.update()
        if state.opts['auto'] and not state.running:
            _auto_sequence()

    def _on_path_submit(e: ft.ControlEvent):
        raw = e.control.value or ''
        e.control.value = ''
        page.update()
        _import_paths_from_text(raw)

    # ── Channel grid ──────────────────────────────────────────────────────────
    def _build_ch_card(c: dict) -> ft.Container:
        status   = c['status']
        fg       = CH_FG.get(status, '#666')
        bar_clr  = BAR_CLR.get(status, '#444')
        left_clr = LEFT_CLR.get(status, '#444')
        label    = CH_LABEL.get(status, '待機')
        pct_int  = int(min(100, max(0, c['pct'])))

        # ── CRC display area ──
        if status == 'done_ok':
            crc_body = ft.Column(
                controls=[
                    ft.Text('CRC16', size=state.fs(18), color='#5DCAA5',
                            text_align=ft.TextAlign.CENTER),
                    ft.Text(c['crc'], size=state.fs(36),
                            weight=ft.FontWeight.W_500, color='#1D9E75',
                            font_family='monospace',
                            text_align=ft.TextAlign.CENTER),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=2, tight=True,
            )
        elif status == 'done_err':
            crc_body = ft.Column(
                controls=[
                    ft.Text('CRC16 — 不符！', size=state.fs(18),
                            color='#F09595',
                            text_align=ft.TextAlign.CENTER),
                    ft.Text(c['crc'], size=state.fs(28),
                            weight=ft.FontWeight.W_500, color='#E24B4A',
                            font_family='monospace',
                            text_align=ft.TextAlign.CENTER),
                    ft.Text(f'預期 {c["crc_expect"]}', size=state.fs(13),
                            color='#E24B4A', font_family='monospace',
                            text_align=ft.TextAlign.CENTER),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=2, tight=True,
            )
        elif status == 'detecting':
            crc_body = ft.Text('偵測晶片中…', size=state.fs(14),
                               color='#EF9F27',
                               text_align=ft.TextAlign.CENTER)
        elif status == 'no_target':
            crc_body = ft.Text('無目標裝置', size=state.fs(15),
                               color='#666666',
                               text_align=ft.TextAlign.CENTER)
        elif status == 'running':
            crc_body = ft.Text(f'{pct_int}%', size=state.fs(22),
                               color='#888888',
                               text_align=ft.TextAlign.CENTER)
        else:
            crc_body = ft.Text(
                '待燒錄' if c['chip'] else '—',
                size=state.fs(15), color='#555555',
                text_align=ft.TextAlign.CENTER,
            )

        mac_row = (
            ft.Text(f"MAC {c['mac']}", size=state.fs(12),
                    color='#6a6a8a', font_family='monospace')
            if c['mac'] and status not in ('idle', 'no_target')
            else ft.Container(height=0)
        )

        return ft.Container(
            content=ft.Column(
                controls=[
                    # Header row
                    ft.Row(
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Text(f'CH {c["id"]}',
                                            size=state.fs(15),
                                            weight=ft.FontWeight.W_500,
                                            color='#e8e8e8'),
                                    ft.Text(c['chip'] or '—',
                                            size=state.fs(12),
                                            color='#aaaaaa',
                                            font_family='monospace'),
                                ],
                                spacing=8,
                            ),
                            ft.Container(
                                content=ft.Text(label, size=state.fs(11),
                                               weight=ft.FontWeight.W_500,
                                               color=fg),
                                bgcolor=f'{fg}2A',
                                border=ft.Border.all(0.5, f'{fg}66'),
                                border_radius=3,
                                padding=ft.Padding.symmetric(
                                    horizontal=7, vertical=2),
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    # Progress
                    ft.ProgressBar(value=c['pct'] / 100,
                                   bgcolor='#333333', color=bar_clr,
                                   height=15, border_radius=3),
                    # Speed / time
                    ft.Text(
                        c['speed'] +
                        (f" · ⏱ {c['time']}" if c['time'] != '—' else ''),
                        size=state.fs(12), color='#888888',
                    ),
                    mac_row,
                    # CRC area (fills remaining space)
                    ft.Container(
                        content=crc_body,
                        expand=True,
                        alignment=ft.Alignment.CENTER,
                    ),
                ],
                spacing=4,
                expand=True,
            ),
            padding=ft.Padding.only(left=12, right=12, top=10, bottom=10),
            bgcolor='#1a1a1a',
            border=ft.Border.only(
                left=ft.BorderSide(3, left_clr),
                top=ft.BorderSide(0.5, '#2a2a2a'),
                right=ft.BorderSide(0.5, '#2a2a2a'),
                bottom=ft.BorderSide(0.5, '#2a2a2a'),
            ),
            border_radius=8,
            expand=True,
        )

    def _rebuild_channel_grid():
        if not r_ch_col.current:
            return
        n    = state.ch_count
        cols = 2 if n <= 4 else 4
        rows = []
        for row_i in range(0, n, cols):
            row_cards = []
            for col_i in range(cols):
                idx = row_i + col_i
                card = (
                    ft.Container(content=_build_ch_card(state.ch_state[idx]),
                                 expand=True)
                    if idx < n
                    else ft.Container(expand=True)
                )
                row_cards.append(card)
            rows.append(ft.Row(controls=row_cards, spacing=6, expand=True))
        r_ch_col.current.controls = rows

    # ── Flash / detect logic ──────────────────────────────────────────────────
    def _identify_all(_=None):
        if state.running:
            return
        _add_log('SYS', 'info', '識別所有目標晶片中…')
        for i in range(state.ch_count):
            state.ch_state[i].update({'status': 'detecting', 'pct': 0.0})
        _rebuild_channel_grid()
        page.update()

        def _task():
            time.sleep(0.9)
            for i in range(state.ch_count):
                if random.random() > 0.15:
                    state.ch_state[i]['status'] = 'idle'
                    state.ch_state[i]['mac']    = fake_mac()
                    _add_log(f'CH{i}', 'info',
                             f'FICR: {CHIPS[i]}  MAC={state.ch_state[i]["mac"]}',
                             state.ch_state[i]['mac'])
                else:
                    state.ch_state[i]['status'] = 'no_target'
                    _add_log(f'CH{i}', 'warn', '無法識別目標 (SWD 無回應)')
            _rebuild_channel_grid()
            _add_log('SYS', 'info', '識別完成')
            page.update()

        threading.Thread(target=_task, daemon=True).start()

    def _auto_sequence():
        if state.running:
            return
        _add_log('SYS', 'info', '自動燒錄：偵測目標中…')
        _set_status('偵測中…')
        for i in range(state.ch_count):
            state.ch_state[i].update({'status': 'detecting', 'pct': 0.0})
        _rebuild_channel_grid()
        page.update()

        def _task():
            time.sleep(1.2)
            found = 0
            for i in range(state.ch_count):
                if random.random() > 0.2:
                    state.ch_state[i]['status'] = 'idle'
                    state.ch_state[i]['mac']    = fake_mac()
                    found += 1
                    _add_log(f'CH{i}', 'info',
                             f'Target: {CHIPS[i]}',
                             state.ch_state[i]['mac'])
                else:
                    state.ch_state[i]['status'] = 'no_target'
                    _add_log(f'CH{i}', 'warn', '無目標裝置')
            _rebuild_channel_grid()
            _add_log('SYS', 'info', f'偵測完成：{found}/{state.ch_count} 有目標')
            page.update()
            if found > 0:
                time.sleep(0.3)
                _do_flash()

        threading.Thread(target=_task, daemon=True).start()

    def _start_flash(_=None):
        if state.running:
            _add_log('SYS', 'muted', '仍有通道燒錄中，維持目前燒錄流程')
            return
        if not state.connected:
            _add_log('SYS', 'warn', '請先連線 Programmer')
            return
        af = state.active_file()
        if not af:
            _add_log('SYS', 'warn', '請先選擇燒錄檔案')
            return

        if state.opts['autoId']:
            _add_log('SYS', 'info', '自動識別晶片（燒錄前）…')
            for i in range(state.ch_count):
                if state.ch_state[i]['status'] != 'no_target':
                    state.ch_state[i]['status'] = 'detecting'
            _rebuild_channel_grid()
            page.update()

            def _id_then_flash():
                time.sleep(0.7)
                for i in range(state.ch_count):
                    if state.ch_state[i]['status'] == 'detecting':
                        state.ch_state[i]['status'] = 'idle'
                        state.ch_state[i]['mac']    = fake_mac()
                        _add_log(f'CH{i}', 'info',
                                 f'識別: {state.ch_state[i]["chip"]}',
                                 state.ch_state[i]['mac'])
                _rebuild_channel_grid()
                page.update()
                _do_flash()

            threading.Thread(target=_id_then_flash, daemon=True).start()
        else:
            _do_flash()

    def _do_flash():
        af = state.active_file()
        if not af:
            return
        state.flash_ok_session = 0
        state.flash_err_session = 0
        state.running = True
        _set_start_button_enabled(False)
        _serial_send(f"FLASH_START,{af['name']},{af['crc']}")
        _set_status('燒錄中…')
        _add_log('SYS', 'info',
                 f'▶ 燒錄: {af["name"]}  CRC16={af["crc"]}')

        active = [c for c in state.ch_state[:state.ch_count]
                  if c['status'] != 'no_target']
        if not active:
            _add_log('SYS', 'warn', '無可用目標通道')
            state.running = False
            _set_start_button_enabled(True)
            return

        for c in active:
            chunk = '1KB' if c['chip'] == 'nRF52810' else '4KB'
            c.update({'status': 'running', 'pct': 0.0, 'crc': '',
                      'crc_expect': af['crc'], 'time': '—',
                      'speed': f'{chunk} chunk'})
            _add_log(f'CH{c["id"]}', 'info',
                     'ERASEALL + 預填 buffers', c['mac'])

        _rebuild_channel_grid()
        page.update()

        def _run():
            start_reenabled = False
            while state.running:
                time.sleep(0.09)
                all_done = True
                for c in active:
                    if c['status'] == 'running':
                        all_done = False
                        rate = 2.0 if c['chip'] == 'nRF52810' else 3.0
                        c['pct'] = min(100.0,
                                       c['pct'] + rate + random.random() * 0.8)
                        if c['pct'] >= 100.0:
                            c['pct']  = 100.0
                            ok        = random.random() > 0.12
                            c['time'] = f'{2.5 + random.random() * 1.2:.2f}s'
                            if ok:
                                c['status'] = 'done_ok'
                                c['crc']    = af['crc']
                                state.flash_ok_count += 1
                                state.flash_ok_session += 1
                                _add_log(f'CH{c["id"]}', 'ok',
                                         f'完成  CRC16={c["crc"]}  ⏱{c["time"]}',
                                         c['mac'])
                            else:
                                c['status'] = 'done_err'
                                c['crc']    = fake_crc(f'err{c["id"]}')
                                state.flash_err_count += 1
                                state.flash_err_session += 1
                                _add_log(f'CH{c["id"]}', 'err',
                                         f'CRC 錯誤！'
                                         f'Expected {af["crc"]} Got {c["crc"]}',
                                         c['mac'])
                            if not start_reenabled:
                                start_reenabled = True
                                _set_start_button_enabled(True)
                _rebuild_channel_grid()
                page.update()
                if all_done:
                    break

            state.running = False
            _set_start_button_enabled(True)
            _serial_send("FLASH_DONE")
            errs = sum(1 for c in active if c['status'] == 'done_err')
            _set_status(f'完成（{errs} 個錯誤）' if errs else '全部完成 ✓')
            _add_log('SYS', 'err' if errs else 'ok',
                     f'{errs} 個通道 CRC 錯誤' if errs
                     else '所有通道燒錄完成 ✓')
            page.update()

        threading.Thread(target=_run, daemon=True).start()

    def _stop_flash(_=None):
        state.running = False
        _set_start_button_enabled(True)
        _serial_send("FLASH_STOP")
        for i in range(state.ch_count):
            if state.ch_state[i]['status'] in ('running', 'detecting'):
                state.ch_state[i]['status'] = 'idle'
        _rebuild_channel_grid()
        _add_log('SYS', 'warn', '使用者中止燒錄')
        _set_status('已中止')
        page.update()

    def _set_status(txt: str):
        if r_status.current:
            r_status.current.value = txt
            page.update()

    def _set_start_button_enabled(enabled: bool):
        if r_start_btn.current:
            r_start_btn.current.disabled = not enabled
            r_start_btn.current.bgcolor = (
                ft.Colors.PRIMARY if enabled else ft.Colors.GREY_700
            )
            r_start_btn.current.color = (
                ft.Colors.ON_PRIMARY if enabled else ft.Colors.GREY_300
            )
            page.update()


    def _on_reorder_files(e):
        old_i = e.old_index
        new_i = e.new_index
        if new_i > old_i:
            new_i -= 1
        if old_i < 0 or old_i >= len(state.files):
            return
        item = state.files.pop(old_i)
        state.files.insert(max(0, min(new_i, len(state.files))), item)
        _rebuild_file_list()
        page.update()

    def _set_channels(n: int):
        state.ch_count = n
        _sync_channel_btn_state()
        _rebuild_channel_grid()
        page.update()

    def _sync_channel_btn_state():
        if r_ch4_btn.current:
            r_ch4_btn.current.style = ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE_700 if state.ch_count == 4 else None,
                color=ft.Colors.WHITE if state.ch_count == 4 else ft.Colors.OUTLINE,
                side=ft.BorderSide(1, ft.Colors.BLUE_400 if state.ch_count == 4 else ft.Colors.OUTLINE_VARIANT),
                text_style=ft.TextStyle(size=19, weight=ft.FontWeight.W_500),
                padding=ft.Padding.symmetric(horizontal=10, vertical=0),
            )
        if r_ch8_btn.current:
            r_ch8_btn.current.style = ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE_700 if state.ch_count == 8 else None,
                color=ft.Colors.WHITE if state.ch_count == 8 else ft.Colors.OUTLINE,
                side=ft.BorderSide(1, ft.Colors.BLUE_400 if state.ch_count == 8 else ft.Colors.OUTLINE_VARIANT),
                text_style=ft.TextStyle(size=19, weight=ft.FontWeight.W_500),
                padding=ft.Padding.symmetric(horizontal=10, vertical=0),
            )

    def _on_swd_change(e):
        state.swd_freq = e.control.value or state.swd_freq
        e.control.text = state.swd_freq
        _add_log('SYS', 'muted', f'SWD 頻率設定為 {state.swd_freq}')

    def _toggle_auto(e):
        state.opts['auto'] = e.control.value
        if r_sb_auto.current:
            r_sb_auto.current.value = '★ 自動燒錄' if state.opts['auto'] else ''
        _add_log('SYS',
                 'warn' if state.opts['auto'] else 'muted',
                 '自動燒錄 ' + ('啟用' if state.opts['auto'] else '關閉'))
        page.update()

    def _toggle_autoid(e):
        state.opts['autoId'] = e.control.value
        if r_sb_autoid.current:
            r_sb_autoid.current.value = '● 自動識別' if state.opts['autoId'] else ''
        _add_log('SYS',
                 'info' if state.opts['autoId'] else 'muted',
                 '自動識別晶片 ' + ('啟用' if state.opts['autoId'] else '關閉'))
        page.update()

    # ─────────────────────────────────────────────────────────────────────────
    #  Build UI
    # ─────────────────────────────────────────────────────────────────────────

    # ── MenuBar ───────────────────────────────────────────────────────────────
    menu_bar = ft.MenuBar(
        style=ft.MenuStyle(
            bgcolor=ft.Colors.SURFACE_CONTAINER,
            padding=ft.Padding.symmetric(horizontal=4, vertical=2),
        ),
        controls=[
            ft.SubmenuButton(
                content=ft.Text('檔案', size=18),
                controls=[
                    ft.MenuItemButton(
                        content=ft.Text('開啟燒錄檔案…', size=19),
                        trailing=ft.Text('Ctrl+O', size=18,
                                         color=ft.Colors.OUTLINE),
                        on_click=_pick_files,
                    ),
                    ft.Divider(height=1),
                    ft.MenuItemButton(
                        content=ft.Text('儲存 Log…', size=19),
                        trailing=ft.Text('Ctrl+S', size=18,
                                         color=ft.Colors.OUTLINE),
                        on_click=_save_log,
                    ),
                    ft.MenuItemButton(
                        content=ft.Text('另存 Log…', size=19),
                        trailing=ft.Text('Ctrl+Shift+S', size=18,
                                         color=ft.Colors.OUTLINE),
                        on_click=lambda _: page.run_task(_save_log_as),
                    ),
                    ft.Divider(height=1),
                    ft.MenuItemButton(
                        content=ft.Text('結束', size=19),
                        on_click=lambda _: page.run_task(_exit_app),
                    ),
                ],
            ),
            ft.SubmenuButton(
                content=ft.Text('裝置', size=18),
                controls=[
                    ft.MenuItemButton(
                        content=ft.Text('重新掃描 COM port', size=19),
                        on_click=_refresh_ports,
                    ),
                    ft.Divider(height=1),
                    ft.MenuItemButton(
                        content=ft.Text('識別所有目標晶片', size=19),
                        on_click=_identify_all,
                    ),
                    ft.MenuItemButton(
                        content=ft.Text('解除讀寫保護…', size=19),
                        on_click=lambda _: _add_log(
                            'SYS', 'warn', '解除讀寫保護（未實作）'),
                    ),
                ],
            ),
            ft.SubmenuButton(
                content=ft.Text('選項', size=18),
                controls=[
                    ft.MenuItemButton(
                        content=ft.Text('CRC16 驗證', size=19),
                        leading=ft.Checkbox(
                            value=state.opts['verify'],
                            on_change=lambda e: state.opts.update(
                                {'verify': e.control.value}),
                        ),
                    ),
                    ft.MenuItemButton(
                        content=ft.Text('燒錄後 Reset', size=19),
                        leading=ft.Checkbox(
                            value=state.opts['reset'],
                            on_change=lambda e: state.opts.update(
                                {'reset': e.control.value}),
                        ),
                    ),
                    ft.MenuItemButton(
                        content=ft.Text('自動識別晶片', size=19),
                        on_click=lambda _: _add_log(
                            'SYS', 'muted', '請使用工具列 toggle'),
                    ),
                    ft.MenuItemButton(
                        content=ft.Text('自動燒錄', size=19),
                        on_click=lambda _: _add_log(
                            'SYS', 'muted', '請使用工具列 toggle'),
                    ),
                    ft.Divider(height=1),
                    ft.MenuItemButton(
                        content=ft.Text('通道數 4 / 8', size=19),
                        on_click=lambda _: _set_channels(
                            8 if state.ch_count == 4 else 4),
                    ),
                    ft.MenuItemButton(
                        content=ft.Text('RAM loader 設定…', size=19),
                        on_click=lambda _: _add_log(
                            'SYS', 'muted', 'RAM loader 設定（未實作）'),
                    ),
                ],
            ),
            ft.SubmenuButton(
                content=ft.Text('說明', size=18),
                controls=[
                    ft.MenuItemButton(
                        content=ft.Text('關於…', size=19),
                        on_click=lambda _: _add_log(
                            'SYS', 'muted', 'SWD Programmer GUI v1.0 — Flet'),
                    ),
                ],
            ),
        ],
    )

    # ── Toolbar ───────────────────────────────────────────────────────────────
    com_ports = _list_ports()
    state.com_labels = {p["device"]: p["label"] for p in com_ports}
    if com_ports:
        state.com_port = com_ports[0]["device"]
    _com_surface = state.com_labels.get(state.com_port, state.com_port or '')

    com_dd = ft.Dropdown(
        ref=r_com_dd,
        options=[
            ft.dropdown.Option(
                key=p["device"],
                text=p["label"],
                content=ft.Text(p["label"], size=19, no_wrap=True),
            )
            for p in com_ports
        ],
        value=state.com_port if state.com_port else None,
        text=_com_surface,
        width=340,
        menu_width=520,
        text_size=19,
        height=36,
        content_padding=5,
        tooltip='COM Port（連接埠 + 裝置名稱）',
        on_select=_on_com_change,
    )

    conn_status_txt = ft.Text(
        ref=r_conn_status,
        value='● 未連線', size=19,
        color=ft.Colors.RED_400,
        weight=ft.FontWeight.W_500,
    )

    conn_btn = ft.OutlinedButton(
        ref=r_conn_btn,
        content=ft.Text("連線", size=19),
        on_click=_on_connect,
        height=36,
    )

    swd_freq_dd = ft.Dropdown(
        options=[
            ft.dropdown.Option(key=f, text=f, content=ft.Text(f, size=19))
            for f in SWD_FREQS
        ],
        value=state.swd_freq,
        text=state.swd_freq,
        width=140,
        menu_width=140,
        text_size=19,
        height=36,
        content_padding=5,
        tooltip="SWD 時脈頻率",
        on_select=_on_swd_change,
    )

    toolbar = ft.Container(
        ref=r_toolbar,
        height=state.toolbar_height,
        content=ft.Column(
            controls=[
                ft.Container(
                    content=ft.Row(
                        controls=[
                        # ─ COM port section ─────────────────────────────────
                        ft.Text('COM', size=18, color=ft.Colors.OUTLINE),
                        com_dd,
                        ft.IconButton(
                            icon=ft.Icons.REFRESH,
                            icon_size=22, tooltip='重新掃描 COM port',
                            on_click=_refresh_ports,
                            style=ft.ButtonStyle(
                                padding=ft.Padding.all(4)),
                        ),
                        conn_btn,
                        conn_status_txt,
                        ft.VerticalDivider(
                            width=1, color=ft.Colors.OUTLINE_VARIANT),

                        # ─ Flash ───────────────────────────────────────────
                        ft.FilledButton(
                            ref=r_start_btn,
                            content=ft.Text('▶ 開始燒錄'),
                            height=36,
                            on_click=_start_flash,
                            bgcolor=ft.Colors.PRIMARY,
                            color=ft.Colors.ON_PRIMARY,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(
                                    size=17, weight=ft.FontWeight.W_500),
                                padding=ft.Padding.symmetric(
                                    horizontal=14, vertical=0)),
                        ),
                        ft.OutlinedButton(
                            '■ 停止', height=36,
                            on_click=_stop_flash,
                            style=ft.ButtonStyle(
                                text_style=ft.TextStyle(
                                    size=17, weight=ft.FontWeight.W_500,
                                    color=ft.Colors.RED_400),
                                side=ft.BorderSide(0.5, ft.Colors.RED_400),
                                padding=ft.Padding.symmetric(
                                    horizontal=12, vertical=0)),
                        ),
                        ft.VerticalDivider(
                            width=1, color=ft.Colors.OUTLINE_VARIANT),

                        # ─ Toggles ─────────────────────────────────────────
                        ft.Switch(
                            label='自動燒錄', value=False,
                            on_change=_toggle_auto,
                            label_text_style=ft.TextStyle(size=17),
                            active_color=ft.Colors.GREEN_400,
                        ),
                        ft.Switch(
                            label='自動識別', value=False,
                            on_change=_toggle_autoid,
                            label_text_style=ft.TextStyle(size=17),
                            active_color=ft.Colors.BLUE_400,
                        ),
                        ft.VerticalDivider(
                            width=1, color=ft.Colors.OUTLINE_VARIANT),

                        # ─ Channel count ───────────────────────────────────
                        ft.Text('通道', size=18, color=ft.Colors.OUTLINE),
                        ft.OutlinedButton(
                            ref=r_ch4_btn,
                            content=ft.Text('4'),
                            height=32,
                            on_click=lambda _: _set_channels(4),
                        ),
                        ft.OutlinedButton(
                            ref=r_ch8_btn,
                            content=ft.Text('8'),
                            height=32,
                            on_click=lambda _: _set_channels(8),
                        ),
                        ft.VerticalDivider(
                            width=1, color=ft.Colors.OUTLINE_VARIANT),

                        # ─ SWD freq dropdown ───────────────────────────────
                        ft.Text('SWD', size=18, color=ft.Colors.OUTLINE),
                        swd_freq_dd,
                        ft.VerticalDivider(
                            width=1, color=ft.Colors.OUTLINE_VARIANT),
                        ],
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        scroll=ft.ScrollMode.AUTO,
                    ),
                    expand=True,
                ),
            ],
            spacing=0,
            tight=True,
        ),
        padding=ft.Padding.symmetric(horizontal=10, vertical=5),
        bgcolor='#242424',
        border=ft.Border.only(
            bottom=ft.BorderSide(0.5, ft.Colors.OUTLINE_VARIANT)),
    )
    
    # ── Sidebar ───────────────────────────────────────────────────────────────
    sidebar = ft.Container(
        ref=r_sidebar,
        content=ft.Column(
            controls=[
                # header
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Text('燒錄檔案', size=17,
                                    weight=ft.FontWeight.W_500,
                                    color=ft.Colors.OUTLINE),
                            ft.Row(
                                controls=[
                                    ft.OutlinedButton(
                                        '+ 加入', height=28,
                                        on_click=_pick_files,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=17),
                                            padding=ft.Padding.symmetric(
                                                horizontal=6, vertical=0)),
                                    ),
                                    ft.OutlinedButton(
                                        '− 移除', height=28,
                                        on_click=_remove_selected,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(
                                                size=17,
                                                color=ft.Colors.RED_400),
                                            side=ft.BorderSide(
                                                0.5, ft.Colors.RED_300),
                                            padding=ft.Padding.symmetric(
                                                horizontal=6, vertical=0)),
                                    ),
                                ],
                                spacing=4,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    padding=ft.Padding.symmetric(horizontal=10, vertical=7),
                    bgcolor='#242424',
                    border=ft.Border.only(
                        bottom=ft.BorderSide(
                            0.5, ft.Colors.OUTLINE_VARIANT)),
                ),
                # drop / click zone（僅點擊選檔；路徑請用下方欄位貼上）
                ft.Container(
                    ref=r_drop_zone,
                    content=ft.Column(
                        controls=[
                            ft.Icon(
                                ft.Icons.UPLOAD_FILE_OUTLINED,
                                size=22, color=ft.Colors.OUTLINE),
                            ft.Text(
                                '點擊選擇檔案，或於下方貼上 .hex / .bin 路徑',
                                size=18, color=ft.Colors.OUTLINE,
                                text_align=ft.TextAlign.CENTER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=4, tight=True,
                    ),
                    alignment=ft.Alignment.CENTER,
                    padding=8, margin=6,
                    border=ft.Border.all(1.5, ft.Colors.OUTLINE_VARIANT),
                    border_radius=6,
                    ink=True,
                    on_hover=_on_drop_hover,
                    on_click=_pick_files,
                ),
                ft.TextField(
                    ref=r_path_field,
                    hint_text='貼上 .hex / .bin 完整路徑（可多行、分號分隔）後按 Enter',
                    dense=True,
                    text_size=14,
                    bgcolor=ft.Colors.SURFACE,
                    border_color=ft.Colors.OUTLINE_VARIANT,
                    on_submit=_on_path_submit,
                ),
                # file list
                ft.Container(
                    content=ft.ReorderableListView(
                        ref=r_file_col,
                        controls=[],
                        spacing=3,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                        on_reorder=_on_reorder_files,
                    ),
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=4, vertical=2),
                ),
            ],
            spacing=0,
            expand=True,
        ),
        width=state.sidebar_width,
    )

    # ── Channel area ──────────────────────────────────────────────────────────
    sidebar_split_grip = ft.GestureDetector(
        mouse_cursor=ft.MouseCursor.RESIZE_LEFT_RIGHT,
        on_horizontal_drag_update=_on_sidebar_split_drag,
        content=ft.Container(
            width=6,
            bgcolor='#2a2a2a',
            border=ft.Border.only(
                left=ft.BorderSide(0.5, ft.Colors.OUTLINE_VARIANT),
                right=ft.BorderSide(0.5, ft.Colors.OUTLINE_VARIANT)),
        ),
    )

    ch_area = ft.Container(
        content=ft.Column(
            ref=r_ch_col,
            controls=[],
            spacing=6,
            expand=True,
        ),
        expand=True,
        padding=8,
        bgcolor='#111111',
    )

    # ── Log panel ─────────────────────────────────────────────────────────────
    def _resize_log(e: ft.DragUpdateEvent):
        dy = e.global_delta.y if e.global_delta else 0
        state.log_height = max(120, min(420, state.log_height - dy))
        if r_log_panel.current:
            r_log_panel.current.height = state.log_height
        page.update()

    log_panel = ft.Container(
        ref=r_log_panel,
        content=ft.Column(
            controls=[
                ft.GestureDetector(
                    on_vertical_drag_update=_resize_log,
                    content=ft.Container(
                        height=8,
                        bgcolor=ft.Colors.OUTLINE_VARIANT,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(ft.Icons.DRAG_HANDLE, size=10,
                                        color=ft.Colors.OUTLINE),
                    ),
                ),
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Text('燒錄 Log', size=22,
                                    weight=ft.FontWeight.W_500,
                                    color=ft.Colors.OUTLINE),
                            ft.Row(
                                controls=[
                                    ft.Text(ref=r_ok_cnt, value='累積成功 0',
                                            size=22, color=ft.Colors.GREEN_400),
                                    ft.Text(ref=r_err_cnt, value='累積失敗 0',
                                            size=22, color=ft.Colors.RED_400),
                                    ft.Text(ref=r_ok_session, value='本次成功 0',
                                            size=22, color=ft.Colors.BLUE_300),
                                    ft.Text(ref=r_err_session, value='本次失敗 0',
                                            size=22, color=ft.Colors.ORANGE_300),
                                    ft.Text(ref=r_log_cnt, value='0 筆',
                                            size=22, color=ft.Colors.OUTLINE),
                                    ft.TextButton(
                                        '重置計數', on_click=_reset_counters,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=22),
                                            padding=ft.Padding.all(0)),
                                    ),
                                    ft.TextButton(
                                        '清除 Log', on_click=_clear_log,
                                        style=ft.ButtonStyle(
                                            text_style=ft.TextStyle(size=22),
                                            padding=ft.Padding.all(0)),
                                    ),
                                ],
                                spacing=8,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    padding=ft.Padding.symmetric(horizontal=12, vertical=5),
                    bgcolor='#242424',
                    border=ft.Border.only(
                        bottom=ft.BorderSide(
                            0.5, ft.Colors.OUTLINE_VARIANT)),
                ),
                ft.Container(
                    content=ft.ListView(
                        ref=r_log_list,
                        controls=[],
                        spacing=0,
                        expand=True,
                        auto_scroll=False,
                    ),
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=12, vertical=4),
                ),
            ],
            spacing=0,
            expand=True,
        ),
        height=state.log_height,
        border=ft.Border.only(
            top=ft.BorderSide(0.5, ft.Colors.OUTLINE_VARIANT)),
    )

    # ── Status bar ────────────────────────────────────────────────────────────
    status_bar = ft.Container(
        content=ft.Row(
            controls=[
                ft.Text(ref=r_status, value='就緒', size=22,
                        color=ft.Colors.OUTLINE),
                ft.Text(ref=r_sb_auto, value='',
                        size=22, color=ft.Colors.OUTLINE),
                ft.Text(ref=r_sb_autoid, value='',
                        size=22, color=ft.Colors.OUTLINE),
                ft.Text(ref=r_sb_prog, value='Programmer: nRF54LM20A (未連線)',
                        size=22, color=ft.Colors.OUTLINE,
                        expand=True),
                ft.Text(ref=r_sb_clk, value='',
                        size=22, color=ft.Colors.OUTLINE),
            ],
        ),
        padding=ft.Padding.symmetric(horizontal=12, vertical=3),
        bgcolor='#242424',
        border=ft.Border.only(
            top=ft.BorderSide(0.5, ft.Colors.OUTLINE_VARIANT)),
    )

    # ── Compose full layout ───────────────────────────────────────────────────
    page.add(
        ft.Column(
            controls=[
                menu_bar,
                toolbar,
                ft.Container(
                    content=ft.Row(
                        controls=[sidebar, sidebar_split_grip, ch_area],
                        spacing=0,
                        expand=True,
                        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    ),
                    expand=True,
                ),
                log_panel,
                status_bar,
            ],
            spacing=0,
            expand=True,
        )
    )

    # ── Initial render ────────────────────────────────────────────────────────
    w = page.window.width if page.window.width is not None else BASE_WIDTH
    state.scale = max(MIN_SCALE, min(MAX_SCALE, w / BASE_WIDTH))
    page.text_scale_factor = state.scale * FONT_BOOST
    _rebuild_file_list()
    _rebuild_channel_grid()
    _sync_channel_btn_state()
    _set_start_button_enabled(True)

    _add_log('SYS', 'muted', 'SWD Multi-Channel Programmer GUI v1.0 啟動')
    _add_log('SYS', 'muted',
             f'{"pyserial 已載入" if HAS_SERIAL else "pyserial 未安裝，使用模擬 COM ports"}')
    _add_log('SYS', 'muted',
             f'SWD {state.swd_freq} · {state.ch_count} 通道 · CRC16 驗證已啟用')
    _add_log('SYS', 'muted', f'偵測到 {len(com_ports)} 個 COM port')
    for i in range(4):
        _add_log(f'CH{i}', 'info',
                 f'Target: {CHIPS[i]} — RAM 256KB  Flash 1MB',
                 state.ch_state[i]['mac'])
    _add_log('SYS', 'muted', '請選擇 COM port 並點擊「連線」後開始燒錄。')
    
    def _clock_task():
        while True:
            time.sleep(1)
            if r_sb_clk.current:
                r_sb_clk.current.value = now_ts()
            try:
                page.update()
            except Exception:
                break

    threading.Thread(target=_clock_task, daemon=True).start()
    threading.Thread(target=_port_watch_loop, daemon=True).start()

    page.update()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ft.run(main)
