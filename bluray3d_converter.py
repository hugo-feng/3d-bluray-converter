# -*- coding: utf-8 -*-
"""
3D 蓝光转换器（3D Blu-ray Converter）
把 3D 蓝光原盘（左右眼双流）转成 SBS/TAB 立体视频（HEVC 硬件编码：AMD / NVIDIA / Intel）。

源文件结构：BDMV\\STREAM 下
  00000.m2ts = 左眼（AVC 基础视图，含音轨）
  00001.m2ts = 右眼（MVC 依赖视图）

流程：tsMuxeR 分别解出两路 ES -> FRIMSource 解码 MVC -> AviSynth 合成布局 -> ffmpeg 编码
界面：Qt (PySide6)，框架级双缓冲，窗口缩放即时无闪烁

用法（GUI）: python bluray3d_converter.py
用法（CLI）: python bluray3d_converter.py --cli --left "00000.m2ts" --right "00001.m2ts" --out "x.mkv"
             [--layout full_sbs|half_sbs|full_tab|half_tab]
             [--container mkv|mp4] [--encoder amf|nvenc|qsv|cpu] [--quality 0-3]
[--bitrate 20] [--frames N] [--noaudio] [--skipdemux] [--reuse]
"""
import os
import re
import sys
import json
import time
import shutil
import tempfile
import threading
import subprocess

from PySide6.QtCore import (Qt, QObject, Signal, QTimer, QRectF, QSize,
                            QPropertyAnimation, QEasingCurve, QAbstractAnimation)
from PySide6.QtGui import QIcon, QFont, QPainter, QPainterPath, QColor
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QProgressBar, QLabel, QComboBox, QCheckBox, QPlainTextEdit, QFrame, QStyle,
    QStyledItemDelegate, QStyleOptionViewItem, QFileDialog, QMessageBox, QScrollArea)

APP_TITLE = "3D 蓝光转换器"
APP_VERSION = "v2.4.1"

# ---- 选项定义 ----
LAYOUTS = [
    ("全宽 SBS  3840×1080（AR 眼镜推荐）", "full_sbs"),
    ("半宽 SBS  1920×1080（兼容性最好）", "half_sbs"),
    ("全高 TAB  1920×2160（上下格式）", "full_tab"),
    ("半高 TAB  1920×1080（上下压缩）", "half_tab"),
]
CONTAINERS = [
    ("MKV（推荐，支持 DTS 原声）", "mkv"),
    ("MP4（手机兼容性最好）", "mp4"),
]
ENCODERS = [
    ("AMD AMF（A 卡 / 核显，推荐）", "amf"),
    ("NVIDIA NVENC（N 卡，最快）", "nvenc"),
    ("Intel QSV（I 卡 / 核显）", "qsv"),
    ("CPU x265（最慢，画质略好）", "cpu"),
]
SPEEDS = [
    ("质量优先", "quality"),
    ("平衡", "balanced"),
    ("速度优先", "speed"),
]
RC_MODES = [
    ("恒定质量（推荐）", "cqp"),
    ("目标平均码率", "vbr"),
    ("固定码率", "cbr"),
]
QP_LEVELS = [
    ("高画质（约 28 Mbps）", 18),
    ("标准（约 21 Mbps）", 20),
    ("压缩（约 15 Mbps）", 22),
    ("高压缩（约 10 Mbps）", 24),
]
AUDIO_MODES = [
    ("原声 + AAC 兼容轨（推荐）", "dual"),
    ("仅原声（无损直通）", "copy"),
    ("仅 AAC 5.1（兼容手机）", "aac_only"),
    ("无音轨", "none"),
]
X265_PRESET = {"quality": "medium", "balanced": "fast", "speed": "veryfast"}
NVENC_PRESET = {"quality": "p7", "balanced": "p5", "speed": "p3"}
QSV_PRESET = {"quality": "slow", "balanced": "medium", "speed": "veryfast"}

# 输出大小预估：3840×1080 基准码率（按 CQP 档），其他分辨率/帧率按比例缩放
BASE_MBPS = {18: 28.0, 20: 21.0, 22: 15.0, 24: 10.0}
LAYOUT_PIXELS = {"full_sbs": (3840, 1080), "half_sbs": (1920, 1080),
                 "full_tab": (1920, 2160), "half_tab": (1920, 1080)}


def estimate_output_size(dur, fps, layout, encoder, rc, qp, bitrate_mbps,
                         audio_kbps, audio_mode):
    """预估输出大小：返回 (总 MB, 视频 kbps, 音频 kbps)"""
    w, h = LAYOUT_PIXELS.get(layout, (3840, 1080))
    if rc == "cqp":
        base = BASE_MBPS.get(int(qp), 28.0)
        v_kbps = base * 1000.0 * (w * h) / (3840.0 * 1080.0) * (max(fps, 1.0) / 23.976)
        if encoder == "cpu":
            v_kbps *= 0.85
    else:
        v_kbps = bitrate_mbps * 1000.0
    a_kbps = 0.0
    if audio_mode != "none":
        if audio_mode in ("dual", "copy"):
            a_kbps += audio_kbps if audio_kbps > 0 else 1509.0
        if audio_mode in ("dual", "aac_only"):
            a_kbps += 320.0
    total_mb = (v_kbps + a_kbps) * max(dur, 0.0) / 8.0 / 1024.0
    return total_mb, v_kbps, a_kbps


FFMPEG_VERSIONS = [
    ("自动（按显卡驱动匹配）", "auto"),
    ("最新 master（需 NVIDIA 驱动 610+）", "master"),
    ("兼容 8.0（需 NVIDIA 驱动 570+）", "8.0"),
]


def ffmpeg_exe(ver):
    """返回指定版本 ffmpeg 路径（缺失时回退到默认版本）"""
    p = FFMPEG_COMPAT if ver == "8.0" else FFMPEG
    if os.path.exists(p):
        return p
    return FFMPEG if os.path.exists(FFMPEG) else p


def pick_ffmpeg_by_driver(ver_num):
    """按 NVIDIA 驱动版本选择 ffmpeg：>=610 用最新 master，否则用兼容版 8.0"""
    if ver_num is None:
        return "master"
    return "master" if ver_num >= 610 else "8.0"


def probe_nvidia_driver_version():
    """探测 NVIDIA 驱动版本：返回 (float 版本, 原始字符串)，失败返回 (None, "")"""
    try:
        for exe in ("nvidia-smi", r"C:\Windows\System32\nvidia-smi.exe"):
            try:
                r = run_hidden([exe, "--query-gpu=driver_version",
                                "--format=csv,noheader"])
                out = (r.stdout or "").strip()
                if out:
                    raw = out.splitlines()[0].strip()
                    m = re.match(r"(\d+)\.(\d+)", raw)
                    if m:
                        return int(m.group(1)) + int(m.group(2)) / 100.0, raw
            except Exception:
                continue
        r = run_hidden(["powershell", "-NoProfile", "-Command",
                        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                        "(Get-CimInstance Win32_VideoController | Where-Object "
                        "{$_.Name -match 'NVIDIA'}).DriverVersion -join ';'"])
        for chunk in (r.stdout or "").split(";"):
            m = re.match(r"^\d+\.\d+\.(\d+)\.(\d+)$", chunk.strip())
            if m:
                s = m.group(1) + m.group(2).zfill(4)
                if s.startswith("1") and len(s) >= 6:
                    s = s[1:]
                if len(s) >= 5:
                    return int(s[:3]) + int(s[3:5]) / 100.0, chunk.strip()
    except Exception:
        pass
    return None, ""


MKV_COPY_AUDIO = ("aac", "ac3", "eac3", "dts", "truehd", "flac",
                  "mp3", "opus", "vorbis", "alac")
MP4_COPY_AUDIO = ("aac", "ac3", "eac3", "mp3", "alac")


def audio_extract_opts(audio_codec, is_mp4):
    """音频提取参数：可直通的编码原样复制，MKV 不支持的（如 pcm_bluray）无损转 FLAC"""
    if audio_codec in (MP4_COPY_AUDIO if is_mp4 else MKV_COPY_AUDIO):
        return ["-c:a", "copy"]
    if is_mp4:
        return ["-c:a", "ac3", "-b:a", "640k"]
    return ["-c:a", "flac", "-compression_level", "8"]


def build_video_args(encoder, speed, rc, qp, bitrate, gop):
    """构造视频编码参数（视频流参数，供转换与可用性预检共用）"""
    args = []
    if encoder == "amf":
        args += ["-c:v", "hevc_amf", "-usage", "transcoding",
                 "-quality", speed]
        if rc == "cqp":
            args += ["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp + 2)]
        elif rc == "vbr":
            args += ["-rc", "vbr_peak", "-b:v", "%dM" % bitrate,
                     "-maxrate", "%dM" % int(bitrate * 1.5),
                     "-bufsize", "%dM" % (bitrate * 2)]
        else:
            args += ["-rc", "cbr", "-b:v", "%dM" % bitrate,
                     "-maxrate", "%dM" % bitrate,
                     "-bufsize", "%dM" % (bitrate * 2)]
    elif encoder == "nvenc":
        args += ["-c:v", "hevc_nvenc", "-preset",
                 NVENC_PRESET.get(speed, "p5")]
        if rc == "cqp":
            args += ["-rc", "constqp", "-qp", str(qp)]
        else:
            rate = "vbr" if rc == "vbr" else "cbr"
            args += ["-rc", rate, "-b:v", "%dM" % bitrate,
                     "-maxrate", "%dM" % int(bitrate * (1.5 if rc == "vbr" else 1.0)),
                     "-bufsize", "%dM" % (bitrate * 2)]
    elif encoder == "qsv":
        args += ["-c:v", "hevc_qsv", "-preset",
                 QSV_PRESET.get(speed, "medium")]
        if rc == "cqp":
            args += ["-global_quality", str(qp)]
        else:
            args += ["-b:v", "%dM" % bitrate,
                     "-maxrate", "%dM" % int(bitrate * (1.5 if rc == "vbr" else 1.0)),
                     "-bufsize", "%dM" % (bitrate * 2)]
    else:
        args += ["-c:v", "libx265", "-preset",
                 X265_PRESET.get(speed, "medium")]
        if rc == "cqp":
            args += ["-crf", str(qp)]
        else:
            args += ["-b:v", "%dM" % bitrate]
    args += ["-g", str(gop), "-color_primaries", "bt709",
             "-color_trc", "bt709", "-colorspace", "bt709",
             "-color_range", "tv"]
    return args


def probe_encoder(encoder, speed, rc, qp, bitrate, gop, ffmpeg=None):
    """快速验证编码器在本机可用（3 帧测试编码），返回 (ok, 错误信息)"""
    cmd = [ffmpeg or FFMPEG, "-hide_banner", "-v", "error", "-f", "lavfi",
           "-i", "color=black:s=256x256:r=24", "-frames:v", "3", "-an"]
    cmd += build_video_args(encoder, speed, rc, qp, bitrate, gop)
    cmd += ["-f", "null", "-"]
    try:
        r = run_hidden(cmd)
    except Exception as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    msg = ((r.stderr or "") + (r.stdout or "")).strip()
    lines = [ln for ln in msg.splitlines() if ln.strip()]
    return False, "\n".join(lines[-4:])[:400]


def probe_duration(path):
    """读取媒体时长（秒）：优先 tsMuxeR 读头探测（对纯 MVC 从属流也是秒级），
    失败时回退 ffprobe。失败返回 0.0"""
    try:
        r = run_hidden([TSMUXER, path])
        out = (r.stdout or "") + (r.stderr or "")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
        if m:
            return (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                    + float(m.group(3)))
    except Exception:
        pass
    try:
        r = run_hidden([FFPROBE, "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=nw=1:nk=1", path])
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _name_number(name):
    """提取文件名中的第一段数字（如 00004.m2ts -> 4），无则返回 None"""
    m = re.search(r"(\d+)", name)
    return int(m.group(1)) if m else None


AUDIO_KBPS_EST = {
    "LPCM": 4608, "DTS-HD": 2500, "DTS-EXPRESS": 768, "DTS": 1509,
    "TRUE-HD": 3000, "E-AC3": 768, "AC3": 640, "AAC": 384,
    "MPEG-AUDIO": 320, "MPEG-2 AUDIO": 320, "VORBIS": 320, "OPUS": 320,
}


def probe_source_stats(path):
    """读取源统计数据（tsMuxeR 单次读头，秒级）：(时长秒, 帧率, 音轨总码率 kbps)"""
    try:
        r = run_hidden([TSMUXER, path])
        out = (r.stdout or "") + (r.stderr or "")
        dur = 0.0
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
        if m:
            dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                   + float(m.group(3)))
        fps = 0.0
        m = re.search(r"Frame rate:\s*([\d.]+)", out)
        if m:
            fps = float(m.group(1))
        a_kbps = 0.0
        for t in re.findall(r"Stream type:\s*([A-Za-z0-9\- ]+)", out):
            t = t.strip().upper()
            for key, kbps in AUDIO_KBPS_EST.items():
                if key in t:
                    a_kbps += kbps
                    break
        if dur > 0 or fps > 0:
            return (dur, fps, a_kbps)
    except Exception:
        pass
    try:
        r = run_hidden([FFPROBE, "-v", "error", "-show_entries",
                        "stream=codec_type,r_frame_rate,bit_rate:format=duration",
                        "-of", "default=nw=1", path])
        if r.returncode != 0:
            return None
        dur, fps, a_kbps = 0.0, 0.0, 0.0
        cur = ""
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == "codec_type":
                cur = v
            elif k == "duration":
                dur = float(v)
            elif k == "r_frame_rate":
                if "/" in v:
                    a, b = v.split("/")
                    fps = float(a) / max(float(b), 1.0)
                else:
                    fps = float(v)
            elif k == "bit_rate" and cur == "audio":
                try:
                    a_kbps += float(v) / 1000.0
                except ValueError:
                    pass
        return (dur, fps, a_kbps)
    except Exception:
        return None

DEMUX_WEIGHT = 3.0
VIDEO_WEIGHT = 88.0
AUDIO_WEIGHT = 4.0
MUX_WEIGHT = 5.0

STAGE_NAMES = {
    "demux": "解流中",
    "encode": "编码中",
    "audio": "音频提取",
    "mux": "混流封装",
    "done": "完成",
}

if getattr(sys, "frozen", False):
    _EXE_DIR = os.path.dirname(sys.executable)
    _CFG_BASE = _EXE_DIR
    _ICO_BASE = getattr(sys, "_MEIPASS", _EXE_DIR)

    def _find_bin_base():
        """兼容两种打包：bin 在 exe 旁（onedir 部署）或打包内（onefile）"""
        cands = [os.path.join(_EXE_DIR, "bin")]
        mp = getattr(sys, "_MEIPASS", None)
        if mp:
            cands.append(os.path.join(mp, "bin"))
        for c in cands:
            if os.path.exists(os.path.join(c, "ffmpeg.exe")):
                return os.path.dirname(c)
        return _EXE_DIR

    _BIN_BASE = _find_bin_base()
else:
    _EXE_DIR = os.path.dirname(os.path.abspath(__file__))
    _BIN_BASE = _CFG_BASE = _ICO_BASE = _EXE_DIR
BIN_DIR = os.path.join(_BIN_BASE, "bin")
FFMPEG_DIR = os.path.join(BIN_DIR, "ffmpeg")
FFMPEG = os.path.join(FFMPEG_DIR, "master", "ffmpeg.exe")
FFMPEG_COMPAT = os.path.join(FFMPEG_DIR, "8.0", "ffmpeg.exe")
FFPROBE = os.path.join(BIN_DIR, "ffprobe.exe")
TSMUXER = os.path.join(BIN_DIR, "tsMuxeR.exe")
FRIMSOURCE = os.path.join(BIN_DIR, "FRIMSource.dll")
MKVMERGE = os.path.join(BIN_DIR, "mkvmerge.exe")
ICON_PATH = os.path.join(_ICO_BASE, "app.ico")
ICONS_DIR = os.path.join(_ICO_BASE, "icons")
CONFIG_PATH = os.path.join(_CFG_BASE, "config.json")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def icon_pixmap(name, size=16):
    """加载开源图标（Tabler Icons, MIT）"""
    try:
        pm = QIcon(os.path.join(ICONS_DIR, name)).pixmap(size, size)
        if not pm.isNull():
            return pm
    except Exception:
        pass
    return None

_ACTIVE = {"theme": "dark"}

THEMES = {
    "dark": dict(
        bg="#17181c", card="#202127", border="#2e3038", text="#e8e9ed",
        dim="#9a9ca8", faint="#6b6d78", input="#2a2c34", input_border="#2e3038",
        accent="#3574f0", accent_h="#2b5fd0", btn="#33363e", btn_h="#3d4149",
        disabled_bg="#24262c",
        danger="#c62828", danger_h="#d33b3b", danger_p="#a51f1f",
        hover="#26272e", chk_border="#3d4149",
        chk_bg="#2a2b31", ok="#7ee08a", ok_bg="rgba(76,175,80,46)",
        warn="#ff8a8a", warn_bg="rgba(211,47,47,51)",
        chev_right="chevron-right.svg", chev_down="chevron-down.svg",
        theme_icon="sun.svg"),
    "light": dict(
        bg="#f3f3f3", card="#ffffff", border="#e4e4e4", text="#1b1b1b",
        dim="#5f6368", faint="#8a8d93", input="#ffffff", input_border="#d6d6d6",
        accent="#3574f0", accent_h="#2b5fd0", btn="#f5f5f5", btn_h="#ebebeb",
        disabled_bg="#eeeeee",
        danger="#c62828", danger_h="#d33b3b", danger_p="#a51f1f",
        hover="#ededed", chk_border="#c0c0c0",
        chk_bg="#e7e7ea", ok="#1e7e34", ok_bg="#e6f4ea",
        warn="#c62828", warn_bg="#fdecea",
        chev_right="chevron-right-light.svg", chev_down="chevron-down-light.svg",
        theme_icon="moon.svg"),
}

QSS_TMPL = """
QWidget { color: @TEXT@;
          font-family: "Microsoft YaHei UI"; font-size: 10.5pt; }
#mainwin { background: @BG@; }
QScrollArea { border: none; background: @BG@; }
#scrollcontent { background: @BG@; }
QFrame#card { background: @CARD@; border: 1px solid @BORDER@; border-radius: 8px; }
QLabel { background: transparent; }
#sectionhead { background: transparent; border-radius: 6px; }
#sectionhead:hover { background: @HOVER@; }
#sectiontitle { font-weight: 600; color: @TEXT@; }
#sectiondesc { color: @FAINT@; }
#dimlabel { color: @DIM@; }
#faintlabel { color: @FAINT@; font-size: 9.5pt; }
#themebtn { background: transparent; border: none; border-radius: 6px; padding: 4px; }
#themebtn:hover { background: @HOVER@; }
QLineEdit { background: @INPUT@; border: 1px solid @INPUT_BORDER@; border-radius: 6px;
            padding: 5px 8px; color: @TEXT@; }
QLineEdit:focus { border: 1px solid @ACCENT@; }
QLineEdit:disabled { color: @FAINT@; background: @DISABLED_BG@; }
QComboBox { background: @INPUT@; border: 1px solid @INPUT_BORDER@; border-radius: 6px;
            padding: 4px 30px 4px 8px; color: @TEXT@; }
QComboBox:hover { border: 1px solid @DIM@; }
QComboBox:focus { border: 1px solid @ACCENT@; }
QComboBox:disabled { color: @FAINT@; background: @DISABLED_BG@; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
                       width: 26px; border: none; background: transparent; }
QComboBox::down-arrow { image: url("@ICONS@/@CHEV_DOWN@"); width: 16px; height: 16px; }
QComboBox QAbstractItemView { background: @CARD@; border: 1px solid @BORDER@;
            border-radius: 8px; selection-background-color: transparent;
            outline: none; color: @TEXT@; padding: 5px; }
QComboBox QAbstractItemView::item { border-radius: 5px; padding: 4px 9px;
            min-height: 22px; color: @TEXT@; }
QPushButton { background: @BTN@; border: 1px solid @BORDER@; border-radius: 6px;
              padding: 6px 14px; color: @TEXT@; }
QPushButton:hover { background: @BTN_H@; }
QPushButton:disabled { color: @FAINT@; }
QPushButton#accent { background: @ACCENT@; border: 1px solid @ACCENT@;
                     color: #ffffff; font-weight: 600; }
QPushButton#accent:hover { background: @ACCENT_H@; }
QPushButton#accent:disabled { background: #2b3f66; border-color: #2b3f66;
                              color: #9aa5b8; }
QPushButton#danger { background: @DANGER@; color: #ffffff; border: none;
            border-radius: 6px; padding: 7px 14px; font-weight: 600; }
QPushButton#danger:hover { background: @DANGER_H@; }
QPushButton#danger:pressed { background: @DANGER_P@; }
QPushButton#danger:disabled { background: @DISABLED_BG@; color: @DISABLED_FG@; }
QProgressBar { background: @BORDER@; border: none; border-radius: 4px; }
QProgressBar::chunk { background: @ACCENT@; border-radius: 4px; }
QCheckBox { color: @DIM@; spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px;
                       border: 1px solid @CHK_BORDER@; background: @CHK_BG@; }
QCheckBox::indicator:checked { background: @ACCENT@; border-color: @ACCENT@;
                               image: url("@ICONS@/check.svg"); }
QCheckBox::indicator:hover { border-color: @ACCENT@; }
QLabel#oklabel { color: @OK@; background: @OK_BG@; border-radius: 9px;
                 padding: 3px 12px; font-weight: 600; }
QLabel#warnlabel { color: @WARN@; background: @WARN_BG@; border-radius: 9px;
                   padding: 3px 12px; font-weight: 600; }
QLabel#plainlabel { color: transparent; background: transparent; padding: 3px 12px; }
QPlainTextEdit { background: #101114; border: 1px solid #2e3038; border-radius: 6px;
                 color: #ccced6; padding: 6px; }
QScrollBar:vertical { background: @BG@; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: @DIM@; border-radius: 6px; min-height: 36px; }
QScrollBar::handle:vertical:hover { background: @TEXT@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""


def build_qss(theme, icons_dir):
    """按主题生成完整样式表（@TOKEN@ 占位符替换）"""
    _ACTIVE["theme"] = theme
    colors = dict(THEMES.get(theme, THEMES["dark"]))
    qss = QSS_TMPL.replace("@ICONS@", icons_dir.replace("\\", "/"))
    for k, v in colors.items():
        qss = qss.replace("@%s@" % k.upper(), v)
    return qss


class Cancelled(Exception):
    pass


def popen_hidden(cmd):
    return subprocess.Popen(
        cmd, creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", bufsize=1)


def run_hidden(cmd):
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=CREATE_NO_WINDOW)


def fmt_time(seconds):
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "%d 分 %d 秒" % (seconds // 60, seconds % 60)
    return "%d 秒" % seconds


def fmt_size(nbytes):
    """字节数人性化显示"""
    try:
        n = float(nbytes)
    except (TypeError, ValueError):
        return "—"
    if n <= 0:
        return "—"
    for unit, div in (("TB", 1024.0 ** 4), ("GB", 1024.0 ** 3),
                      ("MB", 1024.0 ** 2), ("KB", 1024.0)):
        if n >= div:
            return "%.2f %s" % (n / div, unit)
    return "%.0f B" % n


def to_short_path(path):
    """转 8.3 短路径，规避 AviSynth 插件对非 ASCII 路径的兼容问题"""
    if os.name != "nt":
        return path
    try:
        import ctypes
        from ctypes import wintypes
        fn = ctypes.windll.kernel32.GetShortPathNameW
        fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(2048)
        n = fn(path, buf, 2048)
        if n and n < 2048 and buf.value:
            return buf.value
    except Exception:
        pass
    return path


def is_ascii(s):
    return all(ord(c) < 128 for c in s)


def safe_plugin_path(dll_path):
    """保证 AviSynth 插件路径全 ASCII（FRIMSource 不兼容非 ASCII 路径）"""
    if is_ascii(dll_path):
        return dll_path
    sp = to_short_path(dll_path)
    if is_ascii(sp):
        return sp
    import tempfile
    for base in (os.environ.get("TEMP", ""), tempfile.gettempdir(),
                 os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")):
        if not base or not is_ascii(base):
            continue
        dst_dir = os.path.join(base, "bd3d_plugin")
        try:
            os.makedirs(dst_dir, exist_ok=True)
            src_dir = os.path.dirname(dll_path)
            for f in ("FRIMSource.dll", "libmfxsw64.dll",
                      "msvcp100.dll", "msvcr100.dll"):
                src = os.path.join(src_dir, f)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(dst_dir, f))
            cand = os.path.join(dst_dir, "FRIMSource.dll")
            if os.path.exists(cand):
                return cand
        except Exception:
            continue
    return dll_path


def ascii_workdir(preferred, name):
    """中间文件工作目录必须全 ASCII；否则改用同盘根目录"""
    if is_ascii(preferred):
        return preferred
    drive = os.path.splitdrive(os.path.abspath(preferred))[0]
    if drive:
        alt = os.path.join(drive + os.sep, "_bd3d_work_" + name)
        if is_ascii(alt):
            return alt
    return preferred


def probe_stream_pid(path, kind):
    """用 tsMuxeR 探测指定类型（AVC/MVC）轨道的 PID"""
    try:
        p = run_hidden([TSMUXER, path])
        cur = None
        for ln in (p.stdout or "").splitlines() + (p.stderr or "").splitlines():
            ln = ln.strip()
            m = re.match(r"Track ID:\s*(\d+)", ln)
            if m:
                cur = int(m.group(1))
                continue
            m = re.match(r"Stream type:\s*(\S+)", ln)
            if m and cur is not None:
                st = m.group(1).upper()
                if kind == "AVC" and st.startswith("AVC"):
                    return cur
                if kind == "MVC" and st.startswith("MVC"):
                    return cur
    except Exception:
        pass
    return 4113 if kind == "AVC" else 4114


def probe_audio_tracks(m2ts):
    """返回 [(pos, label, codec, channels, lang), ...]"""
    try:
        p = run_hidden(
            [FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name,channels:stream_tags=language",
             "-of", "json", m2ts])
        streams = json.loads(p.stdout).get("streams", [])
    except Exception:
        return []
    tracks = []
    for pos, s in enumerate(streams):
        codec = s.get("codec_name", "?")
        ch = int(s.get("channels") or 0)
        lang = (s.get("tags") or {}).get("language", "und")
        label = "#%d  %s  %d.%d  %s" % (pos + 1, lang, ch - 2 if ch >= 2 else 0,
                                        1, codec)
        tracks.append((pos, label, codec, ch, lang))
    return tracks


class ConvertJob(threading.Thread):
    """单个片段的完整转换任务（解流 -> 视频编码 -> 音频提取 -> 混流）"""

    def __init__(self, left_file, right_file, out_file, layout="full_sbs",
                 container="mkv", encoder="amf", rc="cqp", qp=18, bitrate=20,
                 speed="quality", gop=96, audio_mode="dual", audio_track=None,
                 open_after=False, max_frames=0, skip_demux=False, ffmpeg=None,
                 reuse_video=False,
                 on_log=None, on_progress=None, on_done=None, on_error=None):
        super().__init__(daemon=True)
        self.left_file = left_file
        self.right_file = right_file
        self.out_file = out_file
        self.layout = layout
        self.container = container
        self.encoder = "amf" if encoder == "gpu" else encoder
        self.rc = rc
        self.qp = qp
        self.bitrate = bitrate
        self.speed = speed
        self.gop = gop
        self.audio_mode = audio_mode
        self.audio_track = audio_track
        self.open_after = open_after
        self.max_frames = max_frames
        self.skip_demux = skip_demux
        self.reuse_video = reuse_video
        self.on_log = on_log or (lambda s: None)
        self.on_progress = on_progress or (lambda stage, pct, info: None)
        self.on_done = on_done or (lambda out, stats=None: None)
        self.on_error = on_error or (lambda err: None)
        self.cancel_flag = False
        self.paused = False
        self.stats = {}
        self.proc = None
        self.workdir = ""
        self.ffmpeg = ffmpeg or FFMPEG

    def cancel(self):
        self.cancel_flag = True
        p = self.proc
        if p is not None:
            try:
                if self.paused:
                    self.resume()
                p.terminate()
            except Exception:
                pass

    def pause(self):
        """暂停：挂起当前子进程，进度保留（支持断点续转）"""
        if self.paused:
            return True
        p = self.proc
        if p is None or p.poll() is not None:
            return False
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1F0FFF, False, p.pid)
            if not h:
                return False
            ctypes.windll.ntdll.NtSuspendProcess(ctypes.c_void_p(h))
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
            self.paused = True
            self._log("已暂停（进度保留，点击「继续转换」可断点续转）")
            return True
        except Exception as e:
            self._log("暂停失败：" + str(e))
            return False

    def resume(self):
        """继续：恢复被挂起的子进程"""
        if not self.paused:
            return True
        p = self.proc
        try:
            import ctypes
            if p is not None and p.poll() is None:
                h = ctypes.windll.kernel32.OpenProcess(0x1F0FFF, False, p.pid)
                if h:
                    ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(h))
                    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(h))
            self.paused = False
            self._log("已继续转换")
            return True
        except Exception as e:
            self._log("继续失败：" + str(e))
            return False

    def _check(self):
        if self.cancel_flag:
            raise Cancelled()

    def _log(self, s):
        self.on_log(s)

    def run(self):
        try:
            t_all = time.time()
            name = os.path.splitext(os.path.basename(self.left_file))[0]
            out_dir = os.path.dirname(os.path.abspath(self.out_file))
            preferred = os.path.join(out_dir, "_bd3d_work_" + name)
            self.workdir = ascii_workdir(preferred, name)
            if os.path.normcase(self.workdir) != os.path.normcase(preferred):
                self._log("输出路径含非 ASCII 字符，中间文件改用：" + self.workdir)
            os.makedirs(self.workdir, exist_ok=True)
            try:
                self.stats["src_size"] = (os.path.getsize(self.left_file)
                                          + os.path.getsize(self.right_file))
            except OSError:
                pass
            left_es = os.path.join(self.workdir, "left.264")
            right_es = os.path.join(self.workdir, "right.mvc")
            if self.skip_demux and os.path.exists(left_es) and os.path.exists(right_es):
                self._log("[1/3] 跳过解流（使用已有流文件）")
                nframes = self._estimate_frames()
                self.stats["demux"] = 0.0
            else:
                self._log("[1/3] 解流（tsMuxeR）：左眼 + 右眼")
                t_demux = time.time()
                left_es, right_es, nframes = self._demux(name)
                self.stats["demux"] = time.time() - t_demux
            if nframes <= 0:
                nframes = self._estimate_frames()
            if nframes > 0:
                self._log("解流完成：%d 帧，开始视频编码" % nframes)

            audio_src, audio_idx, audio_codec = None, None, ""
            if self.audio_mode != "none":
                audio_src, audio_idx = self._probe_audio()
                if audio_src:
                    tracks = probe_audio_tracks(audio_src)
                    if tracks and audio_idx < len(tracks):
                        audio_codec = tracks[audio_idx][2]
                        self._log("音轨 %s" % tracks[audio_idx][1])
                        if audio_codec and audio_extract_opts(
                                audio_codec, self.container == "mp4")[1] == "flac":
                            self._log("提示：%s 无法直接封装进 MKV，"
                                      "将在提取时无损转为 FLAC 音轨" % audio_codec)
                else:
                    self._log("未找到音轨输入，将输出无音轨视频")

            self._encode(left_es, right_es, nframes, audio_src, audio_idx, audio_codec)
            self._check()
            try:
                self.stats["out_size"] = os.path.getsize(self.out_file)
            except OSError:
                pass
            self.stats["total"] = time.time() - t_all
            self._log("完成：" + self.out_file)
            self._cleanup_workdir()
            self.on_progress("done", 100.0, "全部完成")
            if self.open_after:
                try:
                    os.startfile(os.path.dirname(os.path.abspath(self.out_file)))
                except Exception:
                    pass
            self.on_done(self.out_file, self.stats)
        except Cancelled:
            self._log("任务已取消，正在删除本次转换的中间文件...")
            self._cleanup_workdir()
            self.on_error("已取消")
        except Exception as e:
            self._log("错误：" + str(e))
            if self.workdir and os.path.isdir(self.workdir):
                self._log("中间文件夹已保留：%s（勾选「复用已完成的编码」重跑，"
                          "可跳过已完成的部分）" % self.workdir)
            self.on_error(str(e))

    # ---------- 任务文件夹管理 ----------
    def _cleanup_workdir(self):
        """转换成功：删除本次任务的中间文件夹（失败时保留以便复用已完成部分）"""
        wd = self.workdir
        if not wd or not os.path.isdir(wd):
            return
        if not os.path.basename(os.path.normpath(wd)).startswith("_bd3d_work_"):
            return
        size = 0
        try:
            for dirpath, dirs, files in os.walk(wd):
                for fn in files:
                    try:
                        size += os.path.getsize(os.path.join(dirpath, fn))
                    except OSError:
                        pass
        except OSError:
            pass
        self.stats["work_size"] = size
        self.stats["workdir"] = wd
        self._log("清理中间文件夹：%s（%s）" % (wd, fmt_size(size)))
        try:
            shutil.rmtree(wd, ignore_errors=True)
            self.stats["cleaned"] = not os.path.isdir(wd)
        except Exception:
            pass

    def _start_watchdog(self, tag):
        """长时间无输出时周期性提示（避免大文件写盘 / 初始化被误认为卡死）"""
        holder = {"t": time.time(), "stop": threading.Event()}

        def loop():
            while not holder["stop"].wait(20):
                if self.paused:
                    holder["t"] = time.time()
                    continue
                gap = time.time() - holder["t"]
                if gap > 60:
                    self._log("…%s 仍在运行（已 %.0f 秒无新输出；大文件写盘 / "
                              "解码器初始化可能较慢，请耐心等待）" % (tag, gap))
                    holder["t"] = time.time()

        threading.Thread(target=loop, daemon=True).start()
        return holder

    # ---------- 阶段 1：解流 ----------
    def _demux(self, name):
        left_pid = probe_stream_pid(self.left_file, "AVC")
        right_pid = probe_stream_pid(self.right_file, "MVC")
        self._log("探测到轨道：左眼 AVC PID=%d，右眼 MVC PID=%d" % (left_pid, right_pid))
        meta_path = os.path.join(self.workdir, "demux.meta")
        meta = ("MUXOPT --no-pcr-on-video-pid --new-audio-pes --demux --vbr --vbv-len=500\n"
                'V_MPEG4/ISO/AVC, "%s", track=%d\n'
                'V_MPEG4/ISO/MVC, "%s", track=%d\n'
                % (self.left_file, left_pid, self.right_file, right_pid))
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(meta)
        p = popen_hidden([TSMUXER, meta_path, self.workdir])
        self.proc = p
        nframes = 0
        tail = []
        last_chk = -1
        cur_pct = 0.0
        wd = self._start_watchdog("tsMuxeR 解流")
        for line in p.stdout:
            wd["t"] = time.time()
            self._check()
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 15:
                del tail[0]
            if "flushing" in line.lower() or "write buffer" in line.lower():
                self.on_progress("demux", cur_pct,
                                 "正在写入磁盘缓存（数据量较大，请稍候）...")
            m = re.search(r"([\d.]+)% complete", line)
            if m:
                pct = float(m.group(1))
                cur_pct = pct
                self.on_progress("demux", pct, "解流中")
                if int(pct) % 5 == 0 and int(pct) != last_chk:
                    last_chk = int(pct)
                    try:
                        _, _, free = shutil.disk_usage(self.workdir)
                        if free < 2 * 1024 ** 3:
                            try:
                                p.terminate()
                            except Exception:
                                pass
                            raise RuntimeError(
                                "解流中止：输出磁盘剩余空间不足 2 GB，"
                                "请清理空间或更换输出位置后重试")
                    except OSError:
                        pass
            m = re.search(r"Processed (\d+) video frames", line)
            if m:
                nframes = max(nframes, int(m.group(1)))
            if re.search(r"error|Error|错误|Cannot|cannot|space", line):
                self._log("  tsMuxeR: " + line)
        wd["stop"].set()
        p.wait()
        self.proc = None
        self._check()
        if p.returncode != 0:
            self._log("tsMuxeR 输出（末尾）：")
            for ln in tail:
                self._log("  " + ln)
            raise RuntimeError(
                "解流失败（tsMuxeR 返回码 %s）。\n"
                "常见原因：\n"
                "  1. 输出磁盘空间不足（解流中间文件与源文件大小接近）\n"
                "  2. 源文件损坏或读取错误\n"
                "  3. 安全软件拦截了写入\n"
                "详细输出见日志窗口（已记录 tsMuxeR 末尾输出）。" % p.returncode)
        left_name = os.path.splitext(os.path.basename(self.left_file))[0]
        right_name = os.path.splitext(os.path.basename(self.right_file))[0]
        cand_left = os.path.join(self.workdir, "%s.track_%d.264" % (left_name, left_pid))
        cand_right = os.path.join(self.workdir, "%s.track_%d.mvc" % (right_name, right_pid))
        if not (os.path.exists(cand_left) and os.path.exists(cand_right)):
            outs = os.listdir(self.workdir)
            l264 = [f for f in outs if f.endswith(".264")]
            mvc = [f for f in outs if f.endswith(".mvc")]
            if not (l264 and mvc):
                raise RuntimeError("解流输出文件缺失，请检查源文件")
            cand_left = os.path.join(self.workdir, l264[0])
            cand_right = os.path.join(self.workdir, mvc[0])
        left_es = os.path.join(self.workdir, "left.264")
        right_es = os.path.join(self.workdir, "right.mvc")
        for src, dst in ((cand_left, left_es), (cand_right, right_es)):
            if os.path.normcase(src) != os.path.normcase(dst):
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
        return left_es, right_es, nframes

    def _estimate_frames(self):
        try:
            dur = probe_duration(self.left_file)
            if dur <= 0:
                return 0
            return max(int(dur * 24000 / 1001) - 3, 1)
        except Exception:
            return 0

    def _probe_audio(self):
        tracks = probe_audio_tracks(self.left_file)
        idx = self.audio_track
        if idx is None and tracks:
            best_pos, best_score = 0, (-1, -1)
            for t in tracks:
                score = (1 if t[4] == "eng" else 0, t[3])
                if score > best_score:
                    best_pos, best_score = t[0], score
            idx = best_pos
        return self.left_file, (idx if idx is not None else 0)

    # ---------- 阶段 2：编码 ----------
    def _build_avs(self, base, dep, nframes, total):
        if self.layout == "full_sbs":
            tail = "StackHorizontal(left, right)\n"
        elif self.layout == "half_sbs":
            tail = "StackHorizontal(left, right).LanczosResize(1920, 1080)\n"
        elif self.layout == "full_tab":
            tail = "StackVertical(left, right)\n"
        else:  # half_tab
            tail = "StackVertical(left, right).LanczosResize(1920, 1080)\n"
        avs = ('LoadPlugin("%s")\n'
               'interleaved = FRIMSource("mvc", "%s", "%s", layout="alt", '
               'num_frames=%d, cache=1, platform="sw")\n'
               'left  = SelectEven(interleaved)\n'
               'right = SelectOdd(interleaved)\n'
               '%s' % (safe_plugin_path(FRIMSOURCE), base, dep, nframes, tail))
        if self.max_frames:
            avs += "Trim(0, %d)\n" % (total - 1)
        return avs

    def _video_args(self):
        return build_video_args(self.encoder, self.speed, self.rc,
                                self.qp, self.bitrate, self.gop)

    def _encode(self, base, dep, nframes, audio_src, audio_idx, audio_codec):
        total = nframes
        if self.max_frames:
            total = min(nframes, self.max_frames)
        # ---- 阶段 2A：编码纯视频（可复用上次已完成的编码结果）----
        tmp_video = os.path.join(self.workdir, "video_only.mkv")
        reuse = (self.reuse_video and os.path.exists(tmp_video)
                 and os.path.getsize(tmp_video) > 50 * 1024 * 1024)
        if reuse:
            self._log("复用已完成的编码结果（video_only.mkv，%.1f GB），跳过视频编码阶段"
                      % (os.path.getsize(tmp_video) / 1024.0 ** 3))
            self._log("  提示：若修改过布局 / 编码器 / 质量等画面参数，"
                      "请取消勾选「复用已完成的编码」后重新开始")
            self.on_progress("encode", DEMUX_WEIGHT + VIDEO_WEIGHT,
                             "复用已完成的编码结果")
            self.stats["encode"] = 0.0
            self.stats["reused"] = True
        else:
            t_enc = time.time()
            avs_path = os.path.join(self.workdir, "decode.avs")
            with open(avs_path, "w", encoding="utf-8") as f:
                f.write(self._build_avs(base, dep, nframes, total))

            # ---- 阶段 2A：编码纯视频 ----
            tmp_video = os.path.join(self.workdir, "video_only.mkv")
            cmd = [self.ffmpeg, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
                   "-i", avs_path, "-an"] + self._video_args()
            if self.max_frames:
                cmd += ["-frames:v", str(total)]
            cmd += ["-f", "matroska", tmp_video]

            self._log("正在启动编码器（首次打开大文件 / 解码器初始化可能需要一些时间）...")
            p = popen_hidden(cmd)
            self.proc = p
            cur = 0
            t0 = time.time()
            last_free_chk = t0
            wd = self._start_watchdog("编码器")
            for line in p.stdout:
                wd["t"] = time.time()
                self._check()
                line = line.strip()
                if not line:
                    continue
                if line.startswith("frame="):
                    try:
                        cur = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                    if cur <= 0:
                        continue
                    now = time.time()
                    if now - last_free_chk > 10:
                        last_free_chk = now
                        try:
                            _, _, free = shutil.disk_usage(self.workdir)
                            if free < 2 * 1024 ** 3:
                                try:
                                    p.terminate()
                                except Exception:
                                    pass
                                raise RuntimeError(
                                    "编码中止：输出磁盘剩余空间不足 2 GB，"
                                    "请清理空间或更换输出位置后重试")
                        except OSError:
                            pass
                    speed = cur / max(time.time() - t0, 0.001)
                    remain = (total - cur) / speed if speed > 0.01 else 0
                    pct = DEMUX_WEIGHT + (cur / max(total, 1)) * VIDEO_WEIGHT
                    info = "%d/%d 帧 · %.0f fps · 剩余约 %s" % (
                        cur, total, speed, fmt_time(remain))
                    self.on_progress("encode", pct, info)
                elif line.startswith("progress=") and line.endswith("end"):
                    break
                elif "=" not in line:
                    self._log("  ffmpeg: " + line)
            wd["stop"].set()
            p.wait()
            self.proc = None
            self._check()
            if p.returncode != 0:
                raise RuntimeError("视频编码失败（ffmpeg 返回码 %s）" % p.returncode)
            self.stats["encode"] = time.time() - t_enc


        # ---- 阶段 2B：提取音频到独立文件（顺序 I/O）----
        if self.audio_mode == "none" or not audio_src:
            if os.path.exists(self.out_file):
                os.remove(self.out_file)
            os.replace(tmp_video, self.out_file)
            self.stats["audio"] = 0.0
            self.stats["mux"] = 0.0
            return
        dur = total * 1001.0 / 24000.0 + 0.2
        is_mp4 = self.container == "mp4"
        extract_jobs = []
        if self.audio_mode in ("dual", "copy"):
            extract_jobs.append((os.path.join(self.workdir, "audio_main.mka"),
                                 audio_extract_opts(audio_codec, is_mp4)))
        if self.audio_mode in ("dual", "aac_only"):
            extract_jobs.append((os.path.join(self.workdir, "audio_aac.mka"),
                                 ["-c:a", "aac", "-b:a", "512k", "-ac", "6"]))
        audio_files = []
        base_pct = DEMUX_WEIGHT + VIDEO_WEIGHT
        t_aud = time.time()
        for ai, (apath, aopts) in enumerate(extract_jobs):
            mode_txt = {"copy": "原样直通", "flac": "无损转 FLAC",
                        "ac3": "转码 AC3", "aac": "转码 AAC"}.get(aopts[1], aopts[1])
            self._log("提取音频 %d/%d（%s）..." % (ai + 1, len(extract_jobs), mode_txt))
            cmd = [self.ffmpeg, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
                   "-t", "%.3f" % dur, "-i", audio_src,
                   "-map", "0:a:%d" % audio_idx] + aopts + [apath]
            p = popen_hidden(cmd)
            self.proc = p
            t_a = time.time()
            for line in p.stdout:
                self._check()
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        sec = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    frac = min(sec / max(dur, 1), 1.0)
                    speed = sec / max(time.time() - t_a, 0.001)
                    remain = (dur - sec) / speed if speed > 0.01 else 0
                    pct = base_pct + (ai + frac) / len(extract_jobs) * AUDIO_WEIGHT
                    self.on_progress(
                        "audio", pct,
                        "音频提取 %d/%d · %.0f%% · 剩余约 %s" % (
                            ai + 1, len(extract_jobs), frac * 100,
                            fmt_time(remain)))
                elif line.startswith("progress=") and line.endswith("end"):
                    break
                elif "=" not in line and line:
                    self._log("  ffmpeg: " + line)
            p.wait()
            self.proc = None
            self._check()
            if p.returncode != 0:
                raise RuntimeError("音频提取失败（ffmpeg 返回码 %s）" % p.returncode)
            audio_files.append(apath)
        self.stats["audio"] = time.time() - t_aud

        # ---- 阶段 2C：混流（纯顺序 I/O）----
        t_mux = time.time()
        cmd = [self.ffmpeg, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
               "-i", tmp_video]
        for a in audio_files:
            cmd += ["-i", a]
        cmd += ["-map", "0:v"]
        for i in range(len(audio_files)):
            cmd += ["-map", "%d:a" % (i + 1)]
        cmd += ["-c", "copy"]
        if len(audio_files) >= 1:
            cmd += ["-metadata:s:a:0", "language=eng",
                    "-metadata:s:a:0", "title=Original"]
        if len(audio_files) >= 2:
            cmd += ["-metadata:s:a:1", "language=eng",
                    "-metadata:s:a:1", "title=AAC 5.1"]
        if is_mp4:
            cmd += ["-f", "mp4", "-tag:v", "hvc1", "-movflags", "+faststart"]
        else:
            cmd += ["-f", "matroska"]
        cmd += [self.out_file]

        p = popen_hidden(cmd)
        self.proc = p
        base_pct = DEMUX_WEIGHT + VIDEO_WEIGHT + AUDIO_WEIGHT
        t_m = time.time()
        for line in p.stdout:
            self._check()
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    sec = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                frac = min(sec / max(dur, 1), 1.0)
                speed = sec / max(time.time() - t_m, 0.001)
                remain = (dur - sec) / speed if speed > 0.01 else 0
                pct = base_pct + frac * MUX_WEIGHT
                self.on_progress(
                    "mux", pct, "混流封装中 %.0f%% · 剩余约 %s" % (
                        frac * 100, fmt_time(remain)))
            elif line.startswith("progress=") and line.endswith("end"):
                break
            elif "=" not in line and line:
                self._log("  ffmpeg: " + line)
        p.wait()
        self.proc = None
        self._check()
        if p.returncode != 0:
            raise RuntimeError("混流失败（ffmpeg 返回码 %s）" % p.returncode)
        self.stats["mux"] = time.time() - t_mux
        try:
            os.remove(tmp_video)
        except OSError:
            pass
        if not os.path.exists(self.out_file) or os.path.getsize(self.out_file) < 1024 * 1024:
            raise RuntimeError("输出文件异常，请查看日志")


def probe_media_info(path):
    """读取媒体信息（只读文件头，秒级）：(时长秒, 宽, 高, 视频编码)"""
    try:
        r = run_hidden([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height,codec_name",
                        "-show_entries", "format=duration",
                        "-of", "default=nw=1", path])
        if r.returncode != 0:
            return None
        dur, w, h, vcodec = 0.0, 0, 0, ""
        for line in (r.stdout or "").splitlines():
            if "=" not in line:
                continue
            k, v = line.strip().split("=", 1)
            if k == "width":
                w = int(v)
            elif k == "height":
                h = int(v)
            elif k == "codec_name":
                vcodec = v
            elif k == "duration":
                dur = float(v)
        return (dur, w, h, vcodec)
    except Exception:
        return None


def probe_first_keyframe(path):
    """检查视频第一帧是否为关键帧（只读一帧，快）；无法判断返回 None"""
    try:
        r = run_hidden([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "frame=key_frame",
                        "-read_intervals", "%+#1", "-of", "csv=p=0", path])
        out = (r.stdout or "").strip()
        return out.splitlines()[0].startswith("1") if out else None
    except Exception:
        return None


def concat_files(files, out_file, on_log=None, on_progress=None, on_done=None,
                 on_error=None, proc_holder=None):
    """无损拼接多段视频：mkvmerge 直接封装，不重编码（最省时间、零画质损失）。

    时间戳由 mkvmerge 自动对齐，全部音轨/字幕/章节原样保留；
    先写入同目录 .partial 临时文件，成功后原子替换到目标位置——
    中途失败/取消不会破坏已存在的目标文件。
    """
    def log(s):
        if on_log:
            on_log(s)

    part = os.path.splitext(out_file)[0] + ".partial" + os.path.splitext(out_file)[1]
    t0 = time.time()

    def work():
        try:
            files_abs = [os.path.abspath(p) for p in files]
            for p in files_abs:
                if not os.path.exists(p):
                    raise RuntimeError("找不到文件：" + p)

            log("读取各段信息（仅读文件头）...")
            infos = []
            for i, p in enumerate(files_abs, 1):
                info = probe_media_info(p)
                if not info:
                    raise RuntimeError("无法读取媒体信息：" + p)
                infos.append(info)
                log("  第 %d 段：%s · %dx%d · %s"
                    % (i, fmt_time(info[0]), info[1], info[2], info[3]))
            base = infos[0]
            for i, info in enumerate(infos[1:], 2):
                if (info[1], info[2], info[3]) != (base[1], base[2], base[3]):
                    log("  警告：第 %d 段为 %dx%d（%s），与第一段 %dx%d（%s）不一致"
                        % (i, info[1], info[2], info[3], base[1], base[2], base[3]))
            if len(files_abs) > 1:
                kf = probe_first_keyframe(files_abs[1])
                if kf is False:
                    log("  提示：第二段起始帧不是关键帧，接缝处可能极短暂花屏（后续正常）")
            total = sum(i[0] for i in infos)
            log("预计全片时长：%s（直封装不重编码，速度仅受磁盘限制）" % fmt_time(total))

            if os.path.exists(part):
                os.remove(part)
            if on_progress:
                on_progress(1.0, "正在读取各段信息，准备拼接...")

            ok = False
            if os.path.exists(MKVMERGE):
                cmd = [MKVMERGE, "--gui-mode", "-o", part]
                for i, p in enumerate(files_abs):
                    if i:
                        cmd.append("+")
                    cmd.append(p)
                proc = popen_hidden(cmd)
                if proc_holder is not None:
                    proc_holder.append(proc)
                for line in proc.stdout:
                    m = re.search(r"#GUI#progress (\d+)%", line)
                    if m and on_progress:
                        pct = max(2.0, min(float(m.group(1)), 98.0))
                        el = time.time() - t0
                        if pct > 2 and el >= 1.0:
                            remain = el * (100.0 - pct) / pct
                            info = "拼接中（直封装）%.0f%% · %s" % (
                                pct, "即将完成" if remain < 3
                                else "剩余约 " + fmt_time(remain))
                        else:
                            info = "拼接中（直封装）%.0f%% · 正在估算剩余时间..." % pct
                        on_progress(pct, info)
                    elif re.search(r"error|warning|not be appended|unsupported",
                                   line, re.I):
                        log("  mkvmerge: " + line.strip())
                proc.wait()
                ok = proc.returncode in (0, 1)
                if not ok:
                    log("mkvmerge 无法拼接这些轨道（退出码 %d，常见于含 FLAC 音轨的成品）"
                        % proc.returncode)
                    log("正在改用 ffmpeg 无损重封装方式拼接（音轨将被重新封装，质量不变）...")
            if not ok:
                if os.path.exists(part):
                    os.remove(part)
                lst = os.path.join(tempfile.gettempdir(), "_bd3d_concat_list.txt")
                with open(lst, "w", encoding="utf-8") as f:
                    for p in files_abs:
                        f.write("file '%s'\n" % p.replace("\\", "/"))
                proc = popen_hidden([FFMPEG, "-hide_banner", "-y", "-nostats",
                                     "-progress", "pipe:1",
                                     "-f", "concat", "-safe", "0", "-i", lst,
                                     "-c", "copy", "-map", "0", part])
                if proc_holder is not None:
                    proc_holder.append(proc)
                t_ff = time.time()
                for line in proc.stdout:
                    line = line.strip()
                    if on_progress and line.startswith("out_time_us="):
                        try:
                            sec = int(line.split("=", 1)[1]) / 1e6
                        except ValueError:
                            continue
                        pct = max(2.0, min(sec / max(total, 1) * 100.0, 98.0))
                        el = time.time() - t_ff
                        speed = sec / el if el > 0.5 and sec > 0.5 else 0
                        remain = (total - sec) / speed if speed > 0.01 else 0
                        info = "拼接中（ffmpeg 重封装）%.0f%% · %s" % (
                            pct, ("剩余约 " + fmt_time(remain)) if remain > 3
                            else "即将完成")
                        on_progress(pct, info)
                    elif "error" in line.lower():
                        log("  ffmpeg: " + line.strip())
                proc.wait()
                if proc.returncode != 0:
                    raise RuntimeError("拼接失败（ffmpeg 返回码 %d）" % proc.returncode)

            out_info = probe_media_info(part)
            if out_info and out_info[0] > 0:
                delta = out_info[0] - total
                log("成品校验：时长 %s（两段合计 %s，差异 %+.0f 秒）"
                    % (fmt_time(out_info[0]), fmt_time(total), delta))
                if abs(delta) > 8:
                    log("  注意：差异可能来自源文件时长元数据与实际内容不符；"
                        "建议快速播放接缝处，确认两段内容完整衔接")
            os.replace(part, out_file)
            log("拼接完成：" + out_file)
            if on_progress:
                on_progress(100.0, "拼接完成")
            if on_done:
                on_done(out_file)
        except Exception as e:
            try:
                if os.path.exists(part):
                    os.remove(part)
            except Exception:
                pass
            if on_error:
                on_error(str(e))
    threading.Thread(target=work, daemon=True).start()


# ==================== UI ====================
class Bridge(QObject):
    """线程 -> UI 的信号桥"""
    log = Signal(str)
    progress = Signal(str, float, str)
    done = Signal(str, object)
    error = Signal(str)
    concat_progress = Signal(float, str)
    concat_done = Signal(str)
    src_stats = Signal(object)
    dur_check = Signal(object, object)
    right_match = Signal(object)
    tracks = Signal(object)
    gpu_info = Signal(object)


class SmoothScrollArea(QScrollArea):
    """平滑滚动区：滚轮按动画过渡，避免一格格跳变"""

    STEP = 110

    def __init__(self, parent=None):
        super().__init__(parent)
        self._target = 0
        self._anim = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def smooth_wheel(self, event):
        sb = self.verticalScrollBar()
        dy = event.angleDelta().y()
        if dy == 0 or sb.maximum() <= sb.minimum():
            return False
        running = self._anim.state() == QAbstractAnimation.State.Running
        base = self._target if running else sb.value()
        target = max(sb.minimum(), min(base - (dy / 120.0) * self.STEP, sb.maximum()))
        if not running and target == sb.value():
            return False
        self._target = target
        self._anim.stop()
        self._anim.setStartValue(sb.value())
        self._anim.setEndValue(target)
        self._anim.start()
        return True

    def wheelEvent(self, event):
        if self.smooth_wheel(event):
            event.accept()
        else:
            event.ignore()


class PopupItemDelegate(QStyledItemDelegate):
    """下拉选项自绘：圆角高亮（Qt 弹层路径下 ::item:selected 不生效，故自绘）"""

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width() + 20, max(s.height() + 10, 30))

    def paint(self, painter, option, index):
        c = THEMES[_ACTIVE["theme"]]
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = option.rect.adjusted(1, 1, -1, -1)
        if option.state & QStyle.State_Selected:
            bg, fg = QColor(c["accent"]), QColor("#ffffff")
        elif option.state & QStyle.State_MouseOver:
            bg, fg = QColor(c["hover"]), QColor(c["text"])
        else:
            bg, fg = None, QColor(c["text"])
        if bg is not None:
            path = QPainterPath()
            path.addRoundedRect(QRectF(r), 5, 5)
            painter.fillPath(path, bg)
        painter.setPen(fg)
        painter.setFont(option.font)
        painter.drawText(r.adjusted(9, 0, -9, 0),
                         Qt.AlignVCenter | Qt.AlignLeft,
                         index.data(Qt.DisplayRole) or "")
        painter.restore()


class NoWheelComboBox(QComboBox):
    """下拉框：忽略滚轮（滚动页面而不是改变选项），弹出列表圆角"""

    def __init__(self, parent=None):
        super().__init__(parent)
        try:
            v = self.view()
            v.setFrameShape(QFrame.NoFrame)
            v.setItemDelegate(PopupItemDelegate(v))
            v.viewport().setAttribute(Qt.WA_Hover, True)
            v.viewport().setMouseTracking(True)
            win = v.window()
            win.setWindowFlags(win.windowFlags() | Qt.FramelessWindowHint
                               | Qt.NoDropShadowWindowHint)
            win.setAttribute(Qt.WA_TranslucentBackground, True)
            win.setObjectName("comboPopup")
            win.setStyleSheet("#comboPopup { background: transparent; border: none; }")
        except Exception:
            pass

    def wheelEvent(self, event):
        if self.view().isVisible():
            super().wheelEvent(event)
            return
        w = self.parentWidget()
        while w is not None:
            if isinstance(w, SmoothScrollArea) and w.smooth_wheel(event):
                event.accept()
                return
            w = w.parentWidget()
        event.ignore()


class Section(QFrame):
    """可折叠设置区（Win11 Expander 风格：悬停高亮、圆角）"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._expanded = False
        self._summary = ""
        self._theme = "dark"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 6, 14, 8)
        lay.setSpacing(8)

        self._head = QWidget()
        self._head.setObjectName("sectionhead")
        self._head.setAttribute(Qt.WA_StyledBackground, True)
        self._head.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self._head)
        h.setContentsMargins(0, 4, 0, 4)
        self._arrow = QLabel()
        self._arrow.setFixedWidth(18)
        self._title = QLabel(title)
        self._title.setObjectName("sectiontitle")
        self._sum = QLabel("")
        self._sum.setObjectName("sectiondesc")
        h.addWidget(self._arrow)
        h.addWidget(self._title)
        h.addStretch(1)
        h.addWidget(self._sum)
        lay.addWidget(self._head)

        self.body = QWidget()
        self.body_outer = lay
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(6)
        self.body.setVisible(False)
        lay.addWidget(self.body)

        self._head.mousePressEvent = self._on_click
        self.refresh_icon()

    def _icon_name(self, base):
        return base + (".svg" if self._theme == "dark" else "-light.svg")

    def refresh_icon(self):
        name = self._icon_name("chevron-down" if self._expanded
                               else "chevron-right")
        _pm = icon_pixmap(name, 16)
        if _pm is not None:
            self._arrow.setPixmap(_pm)
        else:
            self._arrow.setText("⌄" if self._expanded else "›")

    def set_theme(self, theme):
        self._theme = theme
        self.refresh_icon()

    def _on_click(self, event):
        self.toggle()
        event.accept()

    def toggle(self):
        self._expanded = not self._expanded
        self.refresh_icon()
        self.body.setVisible(self._expanded)
        self._sum.setText("" if self._expanded else self._summary)

    def set_summary(self, text):
        self._summary = text
        if not self._expanded:
            self._sum.setText(text)


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = self._load_cfg()
        self._theme = self.cfg.get("theme", "dark")
        self.job = None
        self._gpu_names = ""
        self._nv_driver = (None, "")
        self._encoder_touched = False
        self._enc_user_fixed = bool(self.cfg.get("encoder_touched", False))
        self.audio_tracks = []
        self._concat_running = False
        self._concat_procs = []
        self.bridge = Bridge()
        self.bridge.log.connect(self._log)
        self.bridge.progress.connect(self._progress)
        self.bridge.done.connect(self._done)
        self.bridge.error.connect(self._fail)
        self.bridge.concat_progress.connect(self._concat_progress)
        self.bridge.concat_done.connect(self._concat_done)
        self.bridge.src_stats.connect(self._apply_src_stats)
        self.bridge.dur_check.connect(self._apply_dur_check)
        self.bridge.right_match.connect(self._apply_right_match)
        self.bridge.tracks.connect(self._apply_tracks)
        self.bridge.gpu_info.connect(self._apply_gpu_info)

        self.setWindowTitle(APP_TITLE)
        self.setObjectName("mainwin")
        self.setMinimumSize(800, 640)
        self._fit_screen()

        self._src_dur = 0.0
        self._src_fps = 0.0
        self._src_a_kbps = 0.0
        self._dur_pair = (None, None)
        self._dur_ready = False
        self._src_timer = QTimer(self)
        self._src_timer.setSingleShot(True)
        self._src_timer.setInterval(350)
        self._src_timer.timeout.connect(self._src_probe_now)
        try:
            self.setWindowIcon(QIcon(ICON_PATH))
        except Exception:
            pass

        # ---------- 滚动容器：展开折叠区时向下延伸，不挤压其它控件 ----------
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll, 1)
        content = QWidget()
        content.setObjectName("scrollcontent")
        scroll.setWidget(content)

        root = QVBoxLayout(content)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(8)

        # ---------- 顶栏 ----------
        top = QHBoxLayout()
        t = QLabel(APP_TITLE)
        t.setStyleSheet("font-size:16pt; font-weight:700;")
        top.addWidget(t)
        v = QLabel(APP_VERSION)
        v.setObjectName("faintlabel")
        top.addWidget(v)
        top.addStretch(1)
        top.addWidget(QLabel("3D 蓝光双流 → SBS / TAB · GPU 硬件加速"))
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("themebtn")
        self.btn_theme.setFixedSize(34, 30)
        self.btn_theme.setCursor(Qt.PointingHandCursor)
        self.btn_theme.setToolTip("切换深色 / 浅色主题")
        self.btn_theme.clicked.connect(self._toggle_theme)
        top.addWidget(self.btn_theme)
        root.addLayout(top)

        # ---------- 源与输出 ----------
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 14, 12)
        cv.setSpacing(6)
        self.le_left = QLineEdit(self.cfg.get("left", ""))
        self.le_right = QLineEdit(self.cfg.get("right", ""))
        self.le_out = QLineEdit(self.cfg.get("out", ""))
        self._file_row(cv, "左眼文件", self.le_left, self.pick_left,
                       "BDMV\\STREAM 内的主视频流（如 00000.m2ts）")
        self._file_row(cv, "右眼文件", self.le_right, self.pick_right,
                       "同目录的另一条流（如 00001.m2ts，选左眼后自动配对）")
        self.lbl_out_size = self._file_row(cv, "输出到", self.le_out, self.pick_out,
                                           "建议输出磁盘剩余空间 ≥ 45 GB")
        self.lbl_out_size.setWordWrap(True)
        root.addWidget(card)

        # ---------- 输出格式 ----------
        self.sec_fmt = Section("输出格式")
        r = QHBoxLayout()
        r.addWidget(QLabel("3D 布局"))
        self.cmb_layout = NoWheelComboBox()
        self.cmb_layout.addItems([x[0] for x in LAYOUTS])
        self._set_combo(self.cmb_layout, self.cfg.get("layout", LAYOUTS[0][0]))
        r.addWidget(self.cmb_layout, 2)
        r.addWidget(QLabel("容器"))
        self.cmb_container = NoWheelComboBox()
        self.cmb_container.addItems([x[0] for x in CONTAINERS])
        self._set_combo(self.cmb_container, self.cfg.get("container", CONTAINERS[0][0]))
        r.addWidget(self.cmb_container, 2)
        r.addStretch(1)
        self.sec_fmt.body_layout.addLayout(r)
        root.addWidget(self.sec_fmt)

        # ---------- 编码设置 ----------
        self.sec_enc = Section("编码设置")
        r = QHBoxLayout()
        r.addWidget(QLabel("编码器"))
        self.cmb_encoder = NoWheelComboBox()
        self.cmb_encoder.addItems([x[0] for x in ENCODERS])
        self._set_combo(self.cmb_encoder, self.cfg.get("encoder", ENCODERS[0][0]))
        self.cmb_encoder.activated.connect(
            lambda _=None: setattr(self, "_encoder_touched", True))
        r.addWidget(self.cmb_encoder, 2)
        r.addWidget(QLabel("速度"))
        self.cmb_speed = NoWheelComboBox()
        self.cmb_speed.addItems([x[0] for x in SPEEDS])
        self._set_combo(self.cmb_speed, self.cfg.get("speed", SPEEDS[0][0]))
        r.addWidget(self.cmb_speed, 1)
        r.addStretch(1)
        self.sec_enc.body_layout.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("质量模式"))
        self.cmb_rc = NoWheelComboBox()
        self.cmb_rc.addItems([x[0] for x in RC_MODES])
        self._set_combo(self.cmb_rc, self.cfg.get("rc", RC_MODES[0][0]))
        r.addWidget(self.cmb_rc, 1)
        r.addWidget(QLabel("质量"))
        self.cmb_qp = NoWheelComboBox()
        self.cmb_qp.addItems([x[0] for x in QP_LEVELS])
        self._set_combo(self.cmb_qp, self.cfg.get("qp", QP_LEVELS[0][0]))
        r.addWidget(self.cmb_qp, 2)
        r.addWidget(QLabel("码率(M)"))
        self.le_bitrate = QLineEdit(str(self.cfg.get("bitrate", 20)))
        self.le_bitrate.setFixedWidth(60)
        r.addWidget(self.le_bitrate)
        r.addStretch(1)
        self.sec_enc.body_layout.addLayout(r)
        root.addWidget(self.sec_enc)

        # ---------- 音频 ----------
        self.sec_aud = Section("音频")
        r = QHBoxLayout()
        r.addWidget(QLabel("主音轨"))
        self.cmb_track = NoWheelComboBox()
        self.cmb_track.addItem("自动（英语优先，最高声道）")
        r.addWidget(self.cmb_track, 3)
        r.addWidget(QLabel("音频输出"))
        self.cmb_audio = NoWheelComboBox()
        self.cmb_audio.addItems([x[0] for x in AUDIO_MODES])
        self._set_combo(self.cmb_audio, self.cfg.get("audio", AUDIO_MODES[0][0]))
        r.addWidget(self.cmb_audio, 2)
        r.addStretch(1)
        self.sec_aud.body_layout.addLayout(r)
        root.addWidget(self.sec_aud)

        # ---------- 高级 ----------
        self.sec_adv = Section("高级")
        r = QHBoxLayout()
        r.addWidget(QLabel("关键帧间隔"))
        self.le_gop = QLineEdit(str(self.cfg.get("gop", 96)))
        self.le_gop.setFixedWidth(60)
        r.addWidget(self.le_gop)
        r.addStretch(1)
        self.sec_adv.body_layout.addLayout(r)

        r = QHBoxLayout()
        r.addWidget(QLabel("FFmpeg 版本"))
        self.cmb_ffver = NoWheelComboBox()
        self.cmb_ffver.addItems([x[0] for x in FFMPEG_VERSIONS])
        self._set_combo(self.cmb_ffver, self.cfg.get("ffver", FFMPEG_VERSIONS[0][0]))
        self.cmb_ffver.setMinimumWidth(300)
        r.addWidget(self.cmb_ffver)
        r.addStretch(1)
        self.sec_adv.body_layout.addLayout(r)
        tip2 = QLabel("N 卡 NVENC 报「驱动版本不满足」时，可切换兼容版 8.0 或改选其他编码器")
        tip2.setObjectName("faintlabel")
        tip2.setContentsMargins(70, 0, 0, 4)
        self.sec_adv.body_layout.addWidget(tip2)

        r = QHBoxLayout()
        self.chk_open = QCheckBox("完成后打开输出目录")
        self.chk_open.setChecked(bool(self.cfg.get("open_after", True)))
        r.addWidget(self.chk_open)
        r.addStretch(1)
        self.sec_adv.body_layout.addLayout(r)

        r = QHBoxLayout()
        self.chk_reuse = QCheckBox(
            "复用已完成的编码（重跑 / 失败续跑时跳过视频编码，仅重做音频与混流）")
        self.chk_reuse.setChecked(bool(self.cfg.get("reuse_video", True)))
        self.chk_reuse.stateChanged.connect(lambda _=None: self._refresh_summaries())
        r.addWidget(self.chk_reuse)
        r.addStretch(1)
        self.sec_adv.body_layout.addLayout(r)

        r = QHBoxLayout()
        r.addWidget(QLabel("限制帧数（调试）"))
        self.le_frames = QLineEdit("")
        self.le_frames.setFixedWidth(80)
        r.addWidget(self.le_frames)
        r.addStretch(1)
        self.sec_adv.body_layout.addLayout(r)
        root.addWidget(self.sec_adv)

        # ---------- 无损拼接（多段合成全片） ----------
        self.sec_cat = Section("无损拼接（多段合成全片）")
        self.seg_rows = []
        self.seg_list_layout = QVBoxLayout()
        self.seg_list_layout.setContentsMargins(0, 0, 0, 0)
        self.seg_list_layout.setSpacing(6)
        self.sec_cat.body_layout.addLayout(self.seg_list_layout)
        saved_segs = self.cfg.get("segs")
        if not isinstance(saved_segs, list) or len(saved_segs) < 2:
            saved_segs = [self.cfg.get("seg1", ""), self.cfg.get("seg2", "")]
        for p in saved_segs[:16]:
            self._add_seg_row(str(p))
        while len(self.seg_rows) < 2:
            self._add_seg_row("")
        self.le_cat_out = QLineEdit(self.cfg.get("cat_out", ""))
        self._file_row(self.sec_cat.body_layout, "保存全片", self.le_cat_out,
                       self.pick_cat_out, "先选好保存位置，再点右侧「开始拼接」",
                       right_pad=30)
        r = QHBoxLayout()
        self.btn_seg_add = QPushButton("添加分段")
        self.btn_seg_add.setFixedWidth(90)
        self.btn_seg_add.clicked.connect(self._on_add_seg)
        r.addWidget(self.btn_seg_add)
        self.lbl_cat_size = QLabel("不重编码，直接封装 · 音轨、字幕、章节全部保留")
        self.lbl_cat_size.setObjectName("faintlabel")
        r.addWidget(self.lbl_cat_size, 1)
        self.btn_concat_run = QPushButton("开始拼接")
        self.btn_concat_run.clicked.connect(self.concat_start)
        r.addWidget(self.btn_concat_run)
        self.sec_cat.body_layout.addLayout(r)
        root.addWidget(self.sec_cat)

        # ---------- 按钮 ----------
        brow = QHBoxLayout()
        self.btn_start = QPushButton("开始转换")
        self.btn_start.setObjectName("accent")
        self.btn_start.setFixedWidth(130)
        self.btn_start.clicked.connect(self._start_or_pause)
        brow.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setFixedWidth(90)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        brow.addWidget(self.btn_cancel)
        self.lbl_dur_check = QLabel("")
        self.lbl_dur_check.setObjectName("plainlabel")
        brow.addWidget(self.lbl_dur_check)
        brow.addStretch(1)
        root.addLayout(brow)

        # ---------- 进度 ----------
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 14, 12)
        cv.setSpacing(6)
        r = QHBoxLayout()
        self.lbl_pct = QLabel("0.0%")
        self.lbl_pct.setStyleSheet("font-size:22pt; font-weight:700;")
        r.addWidget(self.lbl_pct)
        self.lbl_stage = QLabel("就绪")
        self.lbl_stage.setObjectName("dimlabel")
        r.addWidget(self.lbl_stage)
        r.addStretch(1)
        cv.addLayout(r)
        self.pb = QProgressBar()
        self.pb.setRange(0, 1000)
        self.pb.setValue(0)
        self.pb.setFixedHeight(8)
        self.pb.setTextVisible(False)
        cv.addWidget(self.pb)
        self.lbl_stat = QLabel(" ")
        self.lbl_stat.setObjectName("faintlabel")
        cv.addWidget(self.lbl_stat)
        root.addWidget(card)

        # ---------- 日志 ----------
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 14, 12)
        cv.setSpacing(6)
        lt = QLabel("日志")
        lt.setObjectName("dimlabel")
        cv.addWidget(lt)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFixedHeight(180)
        cv.addWidget(self.log)
        root.addWidget(card)

        # 让滚动内容底部不留白（内容不足时贴顶）
        root.addStretch(0)

        # 联动摘要
        for cb in (self.cmb_layout, self.cmb_container, self.cmb_encoder,
                   self.cmb_speed, self.cmb_qp, self.cmb_audio, self.cmb_ffver):
            cb.currentTextChanged.connect(lambda _=None: self._refresh_summaries())
        self.cmb_rc.currentTextChanged.connect(self._on_rc_change)
        self.le_gop.textChanged.connect(lambda _=None: self._refresh_summaries())
        self.le_cat_out.textChanged.connect(lambda _=None: self._refresh_summaries())
        self.le_bitrate.textChanged.connect(lambda _=None: self._refresh_summaries())
        self.le_left.textChanged.connect(self._schedule_src_probe)
        self.le_right.textChanged.connect(self._schedule_src_probe)
        self.chk_open.stateChanged.connect(lambda _=None: self._refresh_summaries())

        self._on_rc_change(self.cmb_rc.currentText())
        self._refresh_summaries()
        self._log("就绪。选择左眼/右眼视频流文件与输出路径后点击「开始转换」。")
        self._src_timer.start()
        self._update_start_enabled()
        QTimer.singleShot(300, self._detect_gpu_async)

    # ---------- UI 工具 ----------
    def _fit_screen(self):
        """启动尺寸：宽高比与显示器一致，高度约占屏幕 78%"""
        try:
            scr = QApplication.primaryScreen()
            g = scr.availableGeometry() if scr else None
            if g is None or g.height() <= 0 or g.width() <= 0:
                self.resize(900, 800)
                return
            h = int(g.height() * 0.78)
            w = int(h * g.width() / g.height())
            if w > int(g.width() * 0.96):
                w = int(g.width() * 0.96)
                h = int(w * g.height() / g.width())
            self.resize(max(w, 800), max(h, 640))
        except Exception:
            self.resize(900, 800)

    @staticmethod
    def _set_combo(cb, value):
        i = cb.findText(value)
        if i >= 0:
            cb.setCurrentIndex(i)

    def _file_row(self, parent, label, le, browse_cmd, hint="", right_pad=0):
        row = QHBoxLayout()
        lb = QLabel(label)
        lb.setFixedWidth(64)
        row.addWidget(lb)
        row.addWidget(le, 1)
        b = QPushButton("浏览")
        b.setFixedWidth(64)
        b.clicked.connect(browse_cmd)
        row.addWidget(b)
        if right_pad:
            spacer = QWidget()
            spacer.setFixedWidth(right_pad)
            row.addWidget(spacer)
        parent.addLayout(row)
        h = QLabel(hint)
        h.setObjectName("faintlabel")
        h.setContentsMargins(70, 0, 0, 4)
        parent.addWidget(h)
        return h

    def _sel(self, presets, value, default):
        for name, val in presets:
            if name == value:
                return val
        return default

    def _on_rc_change(self, value):
        is_cqp = value == RC_MODES[0][0]
        self.cmb_qp.setEnabled(is_cqp)
        self.le_bitrate.setEnabled(not is_cqp)
        self._refresh_summaries()

    def _toggle_theme(self):
        self._theme = "light" if self._theme == "dark" else "dark"
        self._apply_theme()
        self._save_cfg()

    def _apply_theme(self):
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss(self._theme, ICONS_DIR))
        pm = icon_pixmap(THEMES[self._theme]["theme_icon"], 18)
        if pm is not None:
            self.btn_theme.setIcon(QIcon(pm))
            self.btn_theme.setIconSize(pm.size())
        for sec in (self.sec_fmt, self.sec_enc, self.sec_aud, self.sec_adv, self.sec_cat):
            sec.set_theme(self._theme)
        for item in getattr(self, "seg_rows", []):
            pm = icon_pixmap("x.svg" if self._theme == "dark" else "x-light.svg", 14)
            if pm is not None:
                item["rm"].setIcon(QIcon(pm))
                item["rm"].setIconSize(pm.size())
        self._apply_checkbox_qss()
        if getattr(self, "_dur_pair", (None, None)) != (None, None):
            self._apply_dur_check(*self._dur_pair)

    def _apply_checkbox_qss(self):
        """复选框样式：勾选＝蓝底+√；未勾选＝灰底+无√（控件级样式，主题联动）"""
        c = THEMES.get(self._theme, THEMES["dark"])
        ck_qss = (
            "QCheckBox { color: %(dim)s; spacing: 8px; }"
            "QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px;"
            " border: 1px solid %(border)s; background: %(bg)s; }"
            "QCheckBox::indicator:checked { background: %(accent)s;"
            " border-color: %(accent)s; image: url(\"%(icon)s\"); }"
            "QCheckBox::indicator:hover { border-color: %(accent)s; }"
            % {"dim": c["dim"], "border": c["chk_border"], "bg": c["chk_bg"],
               "accent": c["accent"], "icon": ICONS_DIR.replace("\\", "/") + "/check.svg"}
        )
        for ck in (getattr(self, "chk_open", None), getattr(self, "chk_reuse", None)):
            if ck is not None:
                ck.setStyleSheet(ck_qss)
        self._apply_titlebar()

    def _apply_titlebar(self):
        """让 Windows 标题栏跟随主题"""
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            val = ctypes.c_int(0 if self._theme == "light" else 2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def _refresh_summaries(self):
        self.sec_fmt.set_summary("%s · %s" % (
            self.cmb_layout.currentText().split("  ")[0],
            self.cmb_container.currentText().split("（")[0]))
        enc_name = {"amf": "AMD AMF", "nvenc": "NVENC", "qsv": "QSV", "cpu": "CPU"}
        enc = enc_name.get(
            self._sel(ENCODERS, self.cmb_encoder.currentText(), "amf"), "AMD AMF")
        self.sec_enc.set_summary("%s · %s · %s" % (
            enc, self.cmb_qp.currentText().split("（")[0],
            self.cmb_speed.currentText()))
        self.sec_aud.set_summary("自动 · %s" % self.cmb_audio.currentText().split("（")[0])
        self.sec_adv.set_summary("GOP %s%s · FFmpeg %s%s" % (
            self.le_gop.text(),
            " · 完成后打开" if self.chk_open.isChecked() else "",
            self.cmb_ffver.currentText().split("（")[0],
            " · 复用编码" if getattr(self, "chk_reuse", None) is not None
            and self.chk_reuse.isChecked() else ""))
        n = sum(1 for p in self._seg_paths() if p)
        if n >= 2 and self.le_cat_out.text().strip():
            self.sec_cat.set_summary("%d 段已就绪 · 可开始拼接" % n)
        elif n >= 2:
            self.sec_cat.set_summary("%d 段已就绪，请选保存位置" % n)
        elif n == 1:
            self.sec_cat.set_summary("已选 1 段，还差 1 段")
        else:
            self.sec_cat.set_summary("未选择分段")
        self._update_size_estimate()
        self._update_cat_estimate()

    # ---------- 预估 ----------
    def _schedule_src_probe(self, *_):
        self._src_dur = 0.0
        self._src_fps = 0.0
        self._src_a_kbps = 0.0
        self._dur_pair = (None, None)
        self._dur_ready = False
        self.lbl_dur_check.setText("")
        self.lbl_dur_check.setVisible(False)
        self._update_start_enabled()
        self._src_timer.start()

    def _src_probe_now(self):
        left = self.le_left.text().strip()
        right = self.le_right.text().strip()
        if not left or not right or not (os.path.exists(left) and os.path.exists(right)):
            self._update_size_estimate()
            self._apply_dur_check(None, None)
            return

        def work():
            res = {}

            def probe_dur(k, p):
                res[k] = probe_duration(p)

            def probe_stats(p):
                res["stats"] = probe_source_stats(p)

            ts = [threading.Thread(target=probe_dur, args=("l", left)),
                  threading.Thread(target=probe_dur, args=("r", right)),
                  threading.Thread(target=probe_stats, args=(left,))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.bridge.src_stats.emit(res.get("stats"))
            self.bridge.dur_check.emit(res.get("l") or None, res.get("r") or None)

        threading.Thread(target=work, daemon=True).start()

    def _apply_dur_check(self, ld, rd):
        """左右眼时长校验：一致显示绿色圆弧标签，不一致红色警示（防止选错配对）"""
        self._dur_pair = (ld, rd)
        c = THEMES.get(self._theme, THEMES["dark"])
        base = "border-radius: 9px; padding: 3px 12px; font-weight: 600;"
        if not ld or not rd:
            self.lbl_dur_check.setText("")
            self.lbl_dur_check.setStyleSheet("background: transparent;")
            self.lbl_dur_check.setVisible(False)
            self._dur_ready = False
            self._update_start_enabled()
            return
        diff = abs(ld - rd)
        if diff <= 1.0:
            self.lbl_dur_check.setText("✓ 左右眼时长校验一致（%s）" % fmt_time(ld))
            self.lbl_dur_check.setStyleSheet(
                "color: %s; background: %s; %s" % (c["ok"], c["ok_bg"], base))
        else:
            self.lbl_dur_check.setText(
                "✗ 左右眼时长不一致（左 %s / 右 %s，相差 %s），请检查配对！"
                % (fmt_time(ld), fmt_time(rd), fmt_time(diff)))
            self.lbl_dur_check.setStyleSheet(
                "color: %s; background: %s; %s" % (c["warn"], c["warn_bg"], base))
        self.lbl_dur_check.setVisible(True)
        self._dur_ready = True
        self._update_start_enabled()

    def _update_start_enabled(self):
        """仅当左右眼都已选择且校验尚未完成时，短暂禁用开始按钮；其余情况不受影响。

        注意：时长校验只服务于「3D 蓝光左右眼 → SBS」的转换场景，
        与「无损拼接」流程完全无关（拼接按钮不受任何影响）。
        """
        if (self.job and self.job.is_alive()) or self._concat_running:
            return
        left = self.le_left.text().strip()
        right = self.le_right.text().strip()
        if not (left and right):
            self.btn_start.setEnabled(True)
            return
        self.btn_start.setEnabled(bool(getattr(self, "_dur_ready", False)))

    def _apply_src_stats(self, stats):
        if stats:
            self._src_dur, self._src_fps, self._src_a_kbps = stats
        else:
            self._src_dur = self._src_fps = self._src_a_kbps = 0.0
        self._update_size_estimate()

    def _update_size_estimate(self):
        if self._src_dur <= 0:
            self.lbl_out_size.setText(
                "预计输出大小：选择左右眼文件后自动估算（随布局 / 编码器 / 质量档变化）")
            return
        layout = self._sel(LAYOUTS, self.cmb_layout.currentText(), "full_sbs")
        encoder = self._sel(ENCODERS, self.cmb_encoder.currentText(), "amf")
        rc = self._sel(RC_MODES, self.cmb_rc.currentText(), "cqp")
        qp = self._sel(QP_LEVELS, self.cmb_qp.currentText(), 18)
        try:
            bitrate = max(1, int(self.le_bitrate.text()))
        except ValueError:
            bitrate = 20
        audio_mode = self._sel(AUDIO_MODES, self.cmb_audio.currentText(), "dual")
        mb, v_kbps, a_kbps = estimate_output_size(
            self._src_dur, self._src_fps or 23.976, layout, encoder, rc, qp,
            bitrate, self._src_a_kbps, audio_mode)
        if mb >= 1024:
            size_txt = "约 %.1f GB" % (mb / 1024.0)
        else:
            size_txt = "约 %.0f MB" % mb
        self.lbl_out_size.setText(
            "预计输出大小：%s（视频约 %.1f Mbps + 音频约 %.1f Mbps；"
            "实际随画面复杂度浮动）" % (size_txt, v_kbps / 1000.0, a_kbps / 1000.0))

    def _update_cat_estimate(self):
        total = 0
        n = 0
        for p in self._seg_paths():
            try:
                if p and os.path.exists(p):
                    total += os.path.getsize(p)
                    n += 1
            except OSError:
                pass
        if total > 0:
            if total >= 1024 ** 3:
                txt = "约 %.1f GB" % (total / 1024.0 ** 3)
            else:
                txt = "约 %.0f MB" % (total / 1024.0 ** 2)
            self.lbl_cat_size.setText(
                "预计全片大小：%s（%d 段之和，无损直封装）" % (txt, n))
        else:
            self.lbl_cat_size.setText("不重编码，直接封装 · 音轨、字幕、章节全部保留")

    # ---------- 硬件检测 ----------
    def _detect_gpu_async(self):
        def work():
            names = ""
            try:
                r = run_hidden(["powershell", "-NoProfile", "-Command",
                                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                                "(Get-CimInstance Win32_VideoController).Name -join ' / '"])
                names = (r.stdout or "").strip()
            except Exception:
                pass
            drv, drv_raw = probe_nvidia_driver_version()
            self.bridge.gpu_info.emit((names, drv, drv_raw))
        threading.Thread(target=work, daemon=True).start()

    def _apply_gpu_info(self, payload):
        names, drv, drv_raw = payload
        if names:
            self._gpu_names = names
            self._log("检测到显卡：%s" % names)
        self._nv_driver = (drv, drv_raw)
        if drv_raw:
            self._log("NVIDIA 驱动版本：%s" % drv_raw)
        t = (names or "").lower()
        target = None
        for val, keys in (("nvenc", ("nvidia", "geforce", "rtx", "quadro")),
                          ("qsv", ("intel", "arc", "iris", "uhd graphics")),
                          ("amf", ("amd", "radeon"))):
            if any(k in t for k in keys):
                target = val
                break
        if target and not (self._encoder_touched or self._enc_user_fixed):
            idx = next((i for i, (_, v) in enumerate(ENCODERS) if v == target), -1)
            if idx >= 0 and idx != self.cmb_encoder.currentIndex():
                self.cmb_encoder.setCurrentIndex(idx)
                self._log("已按检测到的显卡自动选择编码器：%s" % ENCODERS[idx][0])
                self._refresh_summaries()
        if target == "nvenc" and drv is not None:
            self._log("FFmpeg 版本：自动模式将使用 %s" % pick_ffmpeg_by_driver(drv))
        tip = ""
        if names:
            tip = "检测到的显卡：\n%s\n" % names
        if drv_raw:
            tip += "NVIDIA 驱动：%s\n" % drv_raw
        tip += ("\n提示：选择未安装对应硬件的硬件编码器会转换失败；"
                "N 卡驱动过旧时可在「高级 → FFmpeg 版本」切换兼容版 8.0。")
        self.cmb_encoder.setToolTip(tip)

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        cfg = {"theme": self._theme,
               "left": self.le_left.text(), "right": self.le_right.text(),
               "out": self.le_out.text(), "layout": self.cmb_layout.currentText(),
               "container": self.cmb_container.currentText(),
               "encoder": self.cmb_encoder.currentText(),
               "speed": self.cmb_speed.currentText(),
               "rc": self.cmb_rc.currentText(), "qp": self.cmb_qp.currentText(),
               "bitrate": self.le_bitrate.text(),
               "audio": self.cmb_audio.currentText(),
               "gop": self.le_gop.text(),
               "ffver": self.cmb_ffver.currentText(),
               "segs": self._seg_paths(),
               "cat_out": self.le_cat_out.text(),
               "encoder_touched": bool(self._encoder_touched or self._enc_user_fixed),
               "reuse_video": self.chk_reuse.isChecked(),
               "open_after": self.chk_open.isChecked()}
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 文件选择 ----------
    def pick_left(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择左眼视频流（BDMV\\STREAM 内的主文件，如 00000.m2ts）",
            os.path.dirname(self.le_left.text()) or "",
            "蓝光视频流 (*.m2ts *.mts);;所有文件 (*)")
        if p:
            self.le_left.setText(p)
            self.le_right.clear()
            if not self.le_out.text():
                base = os.path.splitext(os.path.basename(p))[0]
                self.le_out.setText(os.path.join(os.path.dirname(p), "..",
                                                 "..", "..", "sbs_" + base + ".mkv"))
            self._auto_match_right(p)
            self._refresh_tracks(p)

    def pick_right(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择右眼视频流（同目录的另一条流，如 00001.m2ts）",
            os.path.dirname(self.le_right.text()) or "",
            "蓝光视频流 (*.m2ts *.mts);;所有文件 (*)")
        if p:
            self.le_right.setText(p)
            self._auto_pair(p, False)

    def pick_out(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "保存输出文件", self.le_out.text() or "",
            "Matroska 视频 (*.mkv);;MP4 视频 (*.mp4)")
        if p:
            self.le_out.setText(p)

    def _auto_pair(self, picked, is_left):
        d = os.path.dirname(picked)
        try:
            others = [f for f in os.listdir(d)
                      if f.lower().endswith((".m2ts", ".mts"))
                      and os.path.normcase(os.path.join(d, f)) != os.path.normcase(picked)]
        except Exception:
            return
        if not others:
            return
        prefer = "00001.m2ts" if is_left else "00000.m2ts"
        target = None
        for f in others:
            if f.lower() == prefer:
                target = f
                break
        if target is None and len(others) == 1:
            target = others[0]
        if target is None:
            return
        tp = os.path.join(d, target)
        if is_left and not self.le_right.text().strip():
            self.le_right.setText(tp)
            self._log("已自动配对右眼文件：%s" % target)
        elif not is_left and not self.le_left.text().strip():
            self.le_left.setText(tp)
            self._log("已自动配对左眼文件：%s" % target)

    def _auto_match_right(self, left_path):
        """扫描左眼所在目录的同格式文件，按「文件名相邻优先 + 时长一致」自动匹配右眼"""
        def work():
            d = os.path.dirname(left_path)
            ext = os.path.splitext(left_path)[1].lower()
            left_dur = probe_duration(left_path)
            if left_dur <= 0:
                self.bridge.right_match.emit((left_path, None))
                return
            try:
                cands = [os.path.join(d, f) for f in os.listdir(d)
                         if f.lower().endswith(ext)
                         and os.path.normcase(os.path.join(d, f))
                         != os.path.normcase(left_path)]
            except OSError:
                self.bridge.right_match.emit((left_path, None))
                return
            if not cands:
                self.bridge.right_match.emit((left_path, None))
                return
            lnum = _name_number(os.path.basename(left_path))
            if lnum is not None:
                def rank(p):
                    n = _name_number(os.path.basename(p))
                    return abs(n - lnum) if n is not None else 10 ** 9
                cands.sort(key=rank)
            self.bridge.log.emit("正在自动匹配右眼（核对候选文件时长）...")
            best = None
            for i, p in enumerate(cands):
                if i >= 8:
                    break
                dur = probe_duration(p)
                if dur > 0 and abs(dur - left_dur) <= 1.0:
                    best = p
                    break
            self.bridge.right_match.emit((left_path, best))

        threading.Thread(target=work, daemon=True).start()

    def _apply_right_match(self, payload):
        left_snapshot, matched = payload
        if self.le_left.text().strip() != left_snapshot:
            return
        if matched and not self.le_right.text().strip():
            self.le_right.setText(matched)
            self._log("已自动匹配右眼：%s（时长与左眼一致）"
                      % os.path.basename(matched))
        elif not matched:
            self._log("未在左眼目录找到时长一致的同格式文件，请手动选择右眼")

    def _refresh_tracks(self, m2ts):
        def work():
            tracks = probe_audio_tracks(m2ts)
            if tracks:
                self.bridge.tracks.emit(tracks)
        threading.Thread(target=work, daemon=True).start()

    def _apply_tracks(self, tracks):
        self.audio_tracks = tracks
        self.cmb_track.clear()
        self.cmb_track.addItem("自动（英语优先，最高声道）")
        for t in tracks:
            self.cmb_track.addItem(t[1])
        self._log("检测到 %d 条音轨" % len(tracks))

    # ---------- 运行 ----------
    def _log(self, s):
        self.log.appendPlainText(time.strftime("[%H:%M:%S] ") + s)

    def _progress(self, stage, pct, info):
        self.pb.setValue(int(max(0.0, min(pct, 100.0)) * 10))
        self.lbl_pct.setText("%.1f%%" % pct)
        name = STAGE_NAMES.get(stage)
        if name:
            self.lbl_stage.setText(name)
        self.lbl_stat.setText(info)

    def _concat_progress(self, pct, info):
        self.pb.setValue(int(max(0.0, min(pct, 100.0)) * 10))
        self.lbl_pct.setText("%.1f%%" % pct)
        self.lbl_stat.setText(info)

    def _repolish(self, w):
        w.style().unpolish(w)
        w.style().polish(w)

    def _reset_start_btn(self):
        self.btn_start.setEnabled(True)
        self.btn_start.setText("开始转换")
        self.btn_start.setObjectName("accent")
        self._repolish(self.btn_start)
        self._update_start_enabled()

    def _start_or_pause(self):
        """开始 / 暂停 / 继续 三态按钮"""
        if self.job and self.job.is_alive():
            if self.job.paused:
                self.job.resume()
                self.btn_start.setText("暂停")
                self.btn_start.setObjectName("danger")
                self._repolish(self.btn_start)
                self.lbl_stage.setText("转换中")
            else:
                if self.job.pause():
                    self.btn_start.setText("继续转换")
                    self.btn_start.setObjectName("accent")
                    self._repolish(self.btn_start)
                    self.lbl_stage.setText("已暂停")
                else:
                    self._log("当前阶段无法暂停（可能在阶段切换中），请稍后再试")
        else:
            self.start()

    def _save_log(self, tag=""):
        """把日志窗口全部内容保存到软件目录 log/ 文件夹（返回路径或 None）"""
        try:
            logdir = os.path.join(_EXE_DIR, "log")
            os.makedirs(logdir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            base = os.path.splitext(os.path.basename(self.le_out.text().strip()))[0]
            base = re.sub(r'[\\/:*?"<>|]', "_", base) or "转换"
            path = os.path.join(logdir, "%s_%s%s.log" % (base, ts, tag))
            self._log("日志已保存：%s" % path)
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.toPlainText())
            return path
        except Exception as e:
            self._log("日志保存失败：" + str(e))
            return None

    def _done(self, out, stats):
        self._reset_start_btn()
        self.btn_cancel.setEnabled(False)
        self.lbl_stage.setText("完成")
        self._progress("done", 100.0, "输出：" + out)
        st = stats or {}
        lines = ["转换完成！", "", out, "", "各阶段耗时："]
        lines.append("  解流：%s" % fmt_time(st.get("demux")))
        lines.append("  编码：%s%s" % (
            fmt_time(st.get("encode")),
            "（复用已完成结果）" if st.get("reused") else ""))
        lines.append("  音频提取：%s" % fmt_time(st.get("audio")))
        lines.append("  混流封装：%s" % fmt_time(st.get("mux")))
        lines.append("  合计：%s" % fmt_time(st.get("total")))
        lines.append("")
        lines.append("文件大小：")
        lines.append("  源文件合计：%s" % fmt_size(st.get("src_size")))
        lines.append("  输出成品：%s" % fmt_size(st.get("out_size")))
        if st.get("work_size"):
            if st.get("cleaned"):
                lines.append("  中间文件：%s（已自动清理）" % fmt_size(st.get("work_size")))
            else:
                lines.append("  中间文件：%s（保留在 %s）" % (
                    fmt_size(st.get("work_size")), st.get("workdir") or ""))
        log_path = self._save_log()
        if log_path:
            lines.append("")
            lines.append("日志已保存：" + log_path)
        QMessageBox.information(self, APP_TITLE, "\n".join(lines))

    def _fail(self, err):
        self._reset_start_btn()
        self.btn_cancel.setEnabled(False)
        self._concat_running = False
        self._concat_procs = []
        self.btn_concat_run.setEnabled(True)
        self.btn_concat_run.setText("开始拼接")
        self.lbl_stage.setText("失败")
        if err != "已取消":
            self._log("错误：" + err)
            p = self._save_log("_失败")
            QMessageBox.critical(self, APP_TITLE,
                                 "任务失败：\n" + err
                                 + (("\n\n日志已保存：\n" + p) if p else ""))

    def _source_total_bytes(self):
        total = 0
        for p in (self.le_left.text().strip(), self.le_right.text().strip()):
            try:
                if p and os.path.exists(p):
                    total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def _estimated_output_mb(self):
        if self._src_dur <= 0:
            return 0.0
        layout = self._sel(LAYOUTS, self.cmb_layout.currentText(), "full_sbs")
        encoder = self._sel(ENCODERS, self.cmb_encoder.currentText(), "amf")
        rc = self._sel(RC_MODES, self.cmb_rc.currentText(), "cqp")
        qp = self._sel(QP_LEVELS, self.cmb_qp.currentText(), 18)
        try:
            bitrate = max(1, int(self.le_bitrate.text()))
        except ValueError:
            bitrate = 20
        audio_mode = self._sel(AUDIO_MODES, self.cmb_audio.currentText(), "dual")
        mb, _, _ = estimate_output_size(
            self._src_dur, self._src_fps or 23.976, layout, encoder, rc, qp,
            bitrate, self._src_a_kbps, audio_mode)
        return mb

    def _check_disk_space(self, out):
        """输出盘空间预检：连成品都放不下则直接阻止；中间文件可能不足则强提醒"""
        try:
            _, _, free = shutil.disk_usage(os.path.dirname(os.path.abspath(out)))
        except Exception:
            return True
        free_gb = free / 1024.0 ** 3
        est_out = self._estimated_output_mb() * 1024 * 1024
        src_total = self._source_total_bytes()
        if est_out > 0 and free < est_out * 1.1:
            QMessageBox.critical(
                self, APP_TITLE,
                "输出磁盘剩余空间不足，无法开始：\n\n"
                "剩余：%.1f GB\n成品预计就需要：约 %.1f GB\n\n"
                "请清理空间或更换输出位置后重试。"
                % (free_gb, est_out / 1024.0 ** 3))
            return False
        need = src_total + est_out + 3 * 1024 ** 3
        if need > 0 and free < need:
            r = QMessageBox.question(
                self, APP_TITLE,
                "输出磁盘空间可能不足：\n\n"
                "剩余：%.1f GB\n"
                "预计需要：解流中间文件约 %.1f GB + 成品约 %.1f GB（共约 %.1f GB）\n\n"
                "空间不足会导致解流 / 编码中途失败（如解流进度到一半报错）。\n"
                "是否仍要继续？" % (
                    free_gb, src_total / 1024.0 ** 3, est_out / 1024.0 ** 3,
                    (src_total + est_out) / 1024.0 ** 3),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            return r == QMessageBox.Yes
        return True

    def start(self):
        if self.job and self.job.is_alive():
            return
        if self._concat_running:
            QMessageBox.information(self, APP_TITLE, "拼接任务进行中，请等待完成后再开始转换")
            return
        left = self.le_left.text().strip()
        right = self.le_right.text().strip()
        out = self.le_out.text().strip()
        if not left or not os.path.exists(left):
            QMessageBox.critical(self, APP_TITLE, "请选择有效的左眼视频流文件（.m2ts）")
            return
        if not right or not os.path.exists(right):
            QMessageBox.critical(self, APP_TITLE, "请选择有效的右眼视频流文件（.m2ts）")
            return
        if not out:
            QMessageBox.critical(self, APP_TITLE, "请选择输出文件路径")
            return
        out_dir = os.path.dirname(os.path.abspath(out))
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            QMessageBox.critical(self, APP_TITLE,
                                 "输出目录无法创建：\n%s\n\n%s" % (out_dir, e))
            return
        if not self._check_disk_space(out):
            return
        container = self._sel(CONTAINERS, self.cmb_container.currentText(), "mkv")
        if container == "mp4" and not out.lower().endswith(".mp4"):
            out = os.path.splitext(out)[0] + ".mp4"
            self.le_out.setText(out)
        try:
            max_frames = int(self.le_frames.text()) if self.le_frames.text().strip() else 0
            gop = max(12, int(self.le_gop.text()))
            bitrate = max(1, int(self.le_bitrate.text()))
        except ValueError:
            max_frames, gop, bitrate = 0, 96, 20
        qp = self._sel(QP_LEVELS, self.cmb_qp.currentText(), 18)
        audio_mode = self._sel(AUDIO_MODES, self.cmb_audio.currentText(), "dual")
        audio_track = None
        if self.audio_tracks and self.cmb_track.currentIndex() > 0:
            pos = self.cmb_track.currentIndex() - 1
            if 0 <= pos < len(self.audio_tracks):
                audio_track = self.audio_tracks[pos][0]
        # FFmpeg 版本解析（自动：按 NVIDIA 驱动版本匹配）
        ffver_mode = self._sel(FFMPEG_VERSIONS, self.cmb_ffver.currentText(), "auto")
        drv, drv_raw = self._nv_driver
        if ffver_mode == "auto":
            if drv is None:
                drv, drv_raw = probe_nvidia_driver_version()
                self._nv_driver = (drv, drv_raw)
            eff_ver = pick_ffmpeg_by_driver(drv)
            self._log("FFmpeg 版本：自动 → %s%s" % (
                eff_ver, "（NVIDIA 驱动 %s）" % drv_raw if drv_raw else ""))
        else:
            eff_ver = ffver_mode
            self._log("FFmpeg 版本：%s（手动指定）" % eff_ver)
        ff_exe = ffmpeg_exe(eff_ver)
        # 编码器可用性预检（3 帧测试，避免解流后才失败）
        enc_val = self._sel(ENCODERS, self.cmb_encoder.currentText(), "amf")
        speed_val = self._sel(SPEEDS, self.cmb_speed.currentText(), "quality")
        rc_val = self._sel(RC_MODES, self.cmb_rc.currentText(), "cqp")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            ok, emsg = probe_encoder(enc_val, speed_val, rc_val, qp, bitrate, gop,
                                     ffmpeg=ff_exe)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok:
            self._log("编码器预检失败（%s / FFmpeg %s）：%s" % (
                self.cmb_encoder.currentText(), eff_ver, emsg))
            QMessageBox.critical(
                self, APP_TITLE,
                "所选编码器在本机不可用：\n\n%s\n\n%s\n\n"
                "处理建议：\n"
                "  · NVIDIA 驱动版本不满足时，到「高级 → FFmpeg 版本」切换兼容版 8.0\n"
                "  · 或在「编码设置 → 编码器」改选与本机显卡匹配的编码器\n\n"
                "本机检测到的显卡：%s" % (
                    self.cmb_encoder.currentText(),
                    emsg or "（未返回详细信息）",
                    self._gpu_names or "未检测到"))
            return
        self._log("编码器预检通过：%s（FFmpeg %s）" % (
            self.cmb_encoder.currentText(), eff_ver))
        self._save_cfg()
        self.btn_start.setEnabled(True)
        self.btn_start.setText("暂停")
        self.btn_start.setObjectName("danger")
        self._repolish(self.btn_start)
        self.btn_cancel.setEnabled(True)
        self.lbl_stage.setText("准备中")
        self._log("========== 任务参数 ==========")
        self._log("左眼源文件：%s" % left)
        self._log("右眼源文件：%s" % right)
        self._log("输出文件：%s" % out)
        src_gb = self._source_total_bytes() / 1024.0 ** 3
        if src_gb > 0:
            self._log("源文件合计：%.1f GB（解流中间文件约占同等大小）" % src_gb)
        self._log("3D 布局：%s" % self.cmb_layout.currentText())
        self._log("输出容器：%s" % self.cmb_container.currentText())
        self._log("编码器：%s" % self.cmb_encoder.currentText())
        self._log("编码速度：%s" % self.cmb_speed.currentText())
        self._log("质量模式：%s" % self.cmb_rc.currentText())
        if self.cmb_rc.currentText() == RC_MODES[0][0]:
            self._log("质量档位：%s（CQP %d / %d）" % (
                self.cmb_qp.currentText(), qp, qp + 2))
        else:
            self._log("目标码率：%d Mbps" % bitrate)
        self._log("关键帧间隔：%d" % gop)
        if self.cmb_track.currentIndex() > 0:
            self._log("主音轨：%s" % self.cmb_track.currentText())
        else:
            self._log("主音轨：自动（英语优先，最高声道）")
        self._log("音频输出：%s" % self.cmb_audio.currentText())
        if max_frames:
            self._log("限制帧数：%d（调试模式）" % max_frames)
        self._log("完成后打开目录：%s" % ("是" if self.chk_open.isChecked() else "否"))
        self._log("=============================")
        self.job = ConvertJob(
            left, right, out,
            layout=self._sel(LAYOUTS, self.cmb_layout.currentText(), "full_sbs"),
            container=container,
            encoder=self._sel(ENCODERS, self.cmb_encoder.currentText(), "amf"),
            ffmpeg=ff_exe,
            reuse_video=self.chk_reuse.isChecked(),
            rc=self._sel(RC_MODES, self.cmb_rc.currentText(), "cqp"),
            qp=qp, bitrate=bitrate,
            speed=self._sel(SPEEDS, self.cmb_speed.currentText(), "quality"),
            gop=gop, audio_mode=audio_mode, audio_track=audio_track,
            open_after=self.chk_open.isChecked(), max_frames=max_frames,
            on_log=lambda s: self.bridge.log.emit(s),
            on_progress=lambda st, pct, info: self.bridge.progress.emit(st, pct, info),
            on_done=lambda o, st: self.bridge.done.emit(o, st),
            on_error=lambda e: self.bridge.error.emit(e))
        self.job.start()

    def cancel(self):
        if self._concat_running:
            r = QMessageBox.warning(
                self, APP_TITLE,
                "确定要取消拼接吗？\n\n已写入的临时文件会被清理，需要重新开始。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
            self._log("正在取消拼接...")
            for proc in self._concat_procs:
                try:
                    proc.terminate()
                except Exception:
                    pass
            return
        if self.job:
            r = QMessageBox.warning(
                self, APP_TITLE,
                "确定要取消本次转换吗？\n\n"
                "取消会【删除所有中间文件】——包括已完成的解流、已编码的视频等\n"
                "全部进行中的进度，之后无法断点续转。\n\n"
                "若想保留进度稍后继续，请改点「暂停」或直接关闭窗口。\n\n"
                "确定取消并删除全部进度吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
            self._log("正在取消（将删除中间文件）...")
            self.lbl_stage.setText("正在取消")
            self.job.cancel()

    # ---------- 无损拼接（多段） ----------
    def _add_seg_row(self, path=""):
        if len(self.seg_rows) >= 16:
            return
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        lb = QLabel("")
        lb.setFixedWidth(64)
        h.addWidget(lb)
        le = QLineEdit(path)
        le.setPlaceholderText("按顺序选择要拼接的视频（mkv / mp4 / ts / m2ts）")
        le.textChanged.connect(lambda _=None: self._refresh_summaries())
        h.addWidget(le, 1)
        b = QPushButton("浏览")
        b.setFixedWidth(64)
        b.clicked.connect(lambda _=None, e=le: self._pick_seg(e))
        h.addWidget(b)
        rm = QPushButton()
        rm.setObjectName("themebtn")
        rm.setFixedSize(30, 30)
        rm.setToolTip("移除此段")
        rm.clicked.connect(lambda _=None, r=row: self._remove_seg_row(r))
        h.addWidget(rm)
        self.seg_list_layout.addWidget(row)
        self.seg_rows.append({"row": row, "label": lb, "edit": le, "rm": rm})
        pm = icon_pixmap("x.svg" if self._theme == "dark" else "x-light.svg", 14)
        if pm is not None:
            rm.setIcon(QIcon(pm))
            rm.setIconSize(pm.size())
        self._relabel_segs()

    def _remove_seg_row(self, row):
        if len(self.seg_rows) <= 2:
            return
        for item in list(self.seg_rows):
            if item["row"] is row:
                self.seg_rows.remove(item)
                row.setParent(None)
                row.deleteLater()
                break
        self._relabel_segs()

    def _relabel_segs(self):
        for i, item in enumerate(self.seg_rows, 1):
            item["label"].setText("第 %d 段" % i)
            item["rm"].setEnabled(len(self.seg_rows) > 2)
        if hasattr(self, "btn_seg_add"):
            self.btn_seg_add.setEnabled(len(self.seg_rows) < 16)
        if hasattr(self, "lbl_cat_size"):
            self._refresh_summaries()

    def _on_add_seg(self):
        if len(self.seg_rows) >= 16:
            return
        self._add_seg_row("")

    def _seg_paths(self):
        return [r["edit"].text().strip() for r in self.seg_rows]

    def _pick_seg(self, edit):
        p, _ = QFileDialog.getOpenFileName(
            self, "选择要拼接的视频段", edit.text() or "",
            "视频文件 (*.mkv *.mp4 *.ts *.m2ts);;所有文件 (*)")
        if p:
            edit.setText(p)
            if not self.le_cat_out.text().strip():
                base = os.path.splitext(os.path.basename(p))[0]
                self.le_cat_out.setText(
                    os.path.join(os.path.dirname(p), base + "_完整片.mkv"))

    def pick_cat_out(self):
        paths = [p for p in self._seg_paths() if p]
        first = paths[0] if paths else ""
        base = os.path.splitext(os.path.basename(first))[0] or "完整片"
        default = self.le_cat_out.text().strip() or os.path.join(
            os.path.dirname(first), base + "_完整片.mkv")
        p, _ = QFileDialog.getSaveFileName(
            self, "选择完整片的保存位置", default, "Matroska 视频 (*.mkv)")
        if p:
            if not p.lower().endswith(".mkv"):
                p += ".mkv"
            self.le_cat_out.setText(p)

    def concat_start(self):
        if self._concat_running:
            return
        if self.job and self.job.is_alive():
            QMessageBox.information(self, APP_TITLE, "转换任务进行中，请等待完成后再拼接")
            return
        segs = [p for p in self._seg_paths() if p]
        if len(segs) < 2:
            QMessageBox.critical(self, APP_TITLE, "请至少按顺序选择 2 段视频")
            return
        if len(segs) > 16:
            QMessageBox.critical(self, APP_TITLE, "最多支持 16 段视频")
            return
        for p in segs:
            if not os.path.exists(p):
                QMessageBox.critical(self, APP_TITLE, "找不到文件：\n" + p)
                return
        seen = set()
        for p in segs:
            k = os.path.normcase(os.path.abspath(p))
            if k in seen:
                QMessageBox.critical(self, APP_TITLE, "分段列表存在重复文件：\n" + p)
                return
            seen.add(k)
        out = self.le_cat_out.text().strip()
        if not out:
            base = os.path.splitext(os.path.basename(segs[0]))[0]
            out, _ = QFileDialog.getSaveFileName(
                self, "选择完整片的保存位置",
                os.path.join(os.path.dirname(segs[0]), base + "_完整片.mkv"),
                "Matroska 视频 (*.mkv)")
            if not out:
                return
            if not out.lower().endswith(".mkv"):
                out += ".mkv"
            self.le_cat_out.setText(out)
        self._concat_running = True
        self._concat_procs = []
        self.btn_concat_run.setEnabled(False)
        self.btn_concat_run.setText("拼接中...")
        self.btn_cancel.setEnabled(True)
        self.lbl_stage.setText("拼接中")
        self._log("========== 无损拼接（%d 段） ==========" % len(segs))
        for i, p in enumerate(segs, 1):
            self._log("第 %d 段：%s" % (i, p))
        self._log("保存全片：%s" % out)
        concat_files(segs, out,
                     on_log=lambda s: self.bridge.log.emit(s),
                     on_progress=lambda pct, info: self.bridge.concat_progress.emit(pct, info),
                     on_done=lambda o: self.bridge.concat_done.emit(o),
                     on_error=lambda e: self.bridge.error.emit(e),
                     proc_holder=self._concat_procs)

    def _concat_done(self, out):
        self._concat_running = False
        self._concat_procs = []
        self.btn_concat_run.setEnabled(True)
        self.btn_concat_run.setText("开始拼接")
        self.btn_cancel.setEnabled(False)
        self.lbl_stage.setText("拼接完成")
        log_path = self._save_log("_拼接")
        QMessageBox.information(self, APP_TITLE,
                                "无损拼接完成！\n\n" + out
                                + (("\n\n日志已保存：\n" + log_path) if log_path else ""))
        if self.chk_open.isChecked():
            try:
                os.startfile(os.path.dirname(os.path.abspath(out)))
            except Exception:
                pass

    def closeEvent(self, event):
        if (self.job and self.job.is_alive()) or self._concat_running:
            r = QMessageBox.question(
                self, APP_TITLE,
                "任务仍在进行中，确定要退出吗？\n\n"
                "退出会终止当前任务，但【保留】中间文件进度——\n"
                "下次对同一任务点击开始，会自动断点续转。",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                event.ignore()
                return
            for proc in self._concat_procs:
                try:
                    proc.terminate()
                except Exception:
                    pass
            if self.job:
                self.job.cancel()
        event.accept()


def main():
    args = sys.argv[1:]
    if "--cli" in args:
        def get(flag, default=None):
            if flag in args:
                return args[args.index(flag) + 1]
            return default
        left = get("--left")
        right = get("--right")
        out = get("--out")
        frames = int(get("--frames", "0") or 0)
        noaudio = "--noaudio" in args
        skipdemux = "--skipdemux" in args
        reuse_video = "--reuse" in args
        qidx = int(get("--quality", "0") or 0)
        qp = QP_LEVELS[max(0, min(qidx, len(QP_LEVELS) - 1))][1]
        if not left or not right or not out:
            print("用法: python bd3d2sbs.py --cli --left 00000.m2ts --right 00001.m2ts "
                  "--out x.mkv [--layout full_sbs|half_sbs|full_tab|half_tab] "
                  "[--container mkv|mp4] [--encoder amf|nvenc|qsv|cpu] [--quality 0-3] "
                  "[--ffver master|8.0] "
                  "[--bitrate 20] [--frames N] [--noaudio] [--skipdemux] [--reuse]")
            return 1
        job = ConvertJob(
            left, right, out,
            layout=get("--layout", "full_sbs"),
            container=get("--container", "mkv"),
            encoder=get("--encoder", "amf"),
            ffmpeg=ffmpeg_exe(get("--ffver", "master")),
            bitrate=int(get("--bitrate", "20") or 20),
            qp=qp,
            audio_mode=("none" if noaudio else "dual"),
            max_frames=frames, skip_demux=skipdemux, reuse_video=reuse_video,
            on_log=lambda s: print(s, flush=True),
            on_progress=lambda st, pct, info: print(
                "[%5.1f%%] %s %s" % (pct, st, info), flush=True))
        job.run()
        return 0
    app = QApplication(sys.argv)
    # Fusion 风格：所有控件完全由 QSS 渲染（消除 Windows 原生风格与深色主题混杂）
    app.setStyle("Fusion")
    theme = "dark"
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            theme = json.load(f).get("theme", "dark")
    except Exception:
        pass
    app.setStyleSheet(build_qss(theme, ICONS_DIR))
    win = MainWindow()
    win._theme = theme
    win._apply_theme()
    win.show()
    if "--selftest" in args:
        def _check():
            msg = ""
            try:
                win.sec_fmt.toggle()
                assert win.sec_fmt._expanded, "折叠区展开失败"
                win.sec_fmt.toggle()
                assert not win.sec_fmt._expanded, "折叠区收起失败"
                assert os.path.exists(FFMPEG), "找不到 ffmpeg: " + FFMPEG
                assert os.path.exists(FFMPEG_COMPAT), "找不到兼容版 ffmpeg: " + FFMPEG_COMPAT
                assert os.path.exists(TSMUXER), "找不到 tsMuxeR: " + TSMUXER
                assert os.path.exists(FRIMSOURCE), "找不到 FRIMSource: " + FRIMSOURCE
                assert os.path.exists(MKVMERGE), "找不到 mkvmerge: " + MKVMERGE
                _p1 = icon_pixmap("chevron-right.svg")
                _p2 = icon_pixmap("chevron-down.svg")
                assert _p1 is not None and _p2 is not None, "图标加载失败: " + ICONS_DIR
                msg = "SELFTEST OK"
            except Exception as e:
                msg = "SELFTEST FAIL: %s" % e
            try:
                with open(os.path.join(_CFG_BASE, "selftest.log"), "w",
                          encoding="utf-8") as f:
                    f.write(msg + "\nBIN_DIR=" + BIN_DIR + "\n")
            except Exception:
                pass
            print(msg, flush=True)
            app.quit()
        QTimer.singleShot(500, _check)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
