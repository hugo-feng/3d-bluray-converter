# -*- coding: utf-8 -*-
"""
BD3D 转换器（Qt 版）
把 3D 蓝光原盘（左右眼双流）转成 SBS/TAB 立体视频（HEVC，AMD GPU 硬件编码）。

源文件结构：BDMV\\STREAM 下
  00000.m2ts = 左眼（AVC 基础视图，含音轨）
  00001.m2ts = 右眼（MVC 依赖视图）

流程：tsMuxeR 分别解出两路 ES -> FRIMSource 解码 MVC -> AviSynth 合成布局 -> ffmpeg 编码
界面：Qt (PySide6)，框架级双缓冲，窗口缩放即时无闪烁

用法（GUI）: python bd3d2sbs.py
用法（CLI）: python bd3d2sbs.py --cli --left "00000.m2ts" --right "00001.m2ts" --out "x.mkv"
             [--layout full_sbs|half_sbs|full_tab|half_tab]
             [--container mkv|mp4] [--encoder gpu|cpu] [--quality 0-3]
             [--bitrate 20] [--frames N] [--noaudio] [--skipdemux]
"""
import os
import re
import sys
import json
import time
import shutil
import threading
import subprocess

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QIcon, QFont
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton,
    QProgressBar, QLabel, QComboBox, QCheckBox, QPlainTextEdit, QFrame,
    QFileDialog, QMessageBox, QScrollArea)

APP_TITLE = "BD3D 转换器"
APP_VERSION = "v1.7"

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
    ("AMD GPU 硬编 HEVC（快，推荐）", "gpu"),
    ("CPU x265（慢，同码率画质略好）", "cpu"),
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

DEMUX_WEIGHT = 3.0
VIDEO_WEIGHT = 88.0
AUDIO_WEIGHT = 4.0
MUX_WEIGHT = 5.0

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
    _BIN_BASE = _CFG_BASE = _ICO_BASE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(_BIN_BASE, "bin")
FFMPEG = os.path.join(BIN_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(BIN_DIR, "ffprobe.exe")
TSMUXER = os.path.join(BIN_DIR, "tsMuxeR.exe")
FRIMSOURCE = os.path.join(BIN_DIR, "FRIMSource.dll")
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


QSS_TEMPLATE = """
QWidget { color: #e8e9ed;
          font-family: "Microsoft YaHei UI"; font-size: 10.5pt; }
#mainwin { background: #17181c; }
QFrame#card { background: #202127; border: 1px solid #2e3038; border-radius: 8px; }
QLabel { background: transparent; }
QLineEdit { background: #2a2c34; border: 1px solid #2e3038; border-radius: 6px;
            padding: 5px 8px; color: #e8e9ed; }
QLineEdit:focus { border: 1px solid #3574f0; }
QLineEdit:disabled { color: #6b6d78; background: #24262c; }
QComboBox { background: #2a2c34; border: 1px solid #2e3038; border-radius: 6px;
            padding: 4px 30px 4px 8px; color: #e8e9ed; }
QComboBox:hover { border: 1px solid #3d4149; }
QComboBox:focus { border: 1px solid #3574f0; }
QComboBox:disabled { color: #6b6d78; background: #24262c; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right;
                       width: 26px; border: none; background: transparent; }
QComboBox::down-arrow { image: url("<ICONS>/chevron-down.svg");
                        width: 16px; height: 16px; }
QComboBox QAbstractItemView { background: #202127; border: 1px solid #2e3038;
            selection-background-color: #3574f0; outline: none; color: #e8e9ed;
            padding: 4px; }
QPushButton { background: #33363e; border: 1px solid #2e3038; border-radius: 6px;
              padding: 6px 14px; color: #e8e9ed; }
QPushButton:hover { background: #3d4149; }
QPushButton:disabled { color: #6b6d78; }
QPushButton#accent { background: #3574f0; border: 1px solid #3574f0;
                     color: #ffffff; font-weight: 600; }
QPushButton#accent:hover { background: #2b5fd0; }
QPushButton#accent:disabled { background: #2b3f66; border-color: #2b3f66;
                              color: #9aa5b8; }
QProgressBar { background: #2e3038; border: none; border-radius: 4px; }
QProgressBar::chunk { background: #3574f0; border-radius: 4px; }
QCheckBox { color: #c9cbd3; spacing: 8px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px;
                       border: 1px solid #3d4149; background: #2a2c34; }
QCheckBox::indicator:checked { background: #3574f0; border-color: #3574f0; }
QCheckBox::indicator:hover { border-color: #3574f0; }
QPlainTextEdit { background: #121317; border: 1px solid #2e3038; border-radius: 6px;
                 color: #c8cad2; padding: 6px; }
QScrollBar:vertical { background: #17181c; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a3d46; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4a4e58; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
"""


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
                 container="mkv", encoder="gpu", rc="cqp", qp=18, bitrate=20,
                 speed="quality", gop=96, audio_mode="dual", audio_track=None,
                 open_after=False, max_frames=0, skip_demux=False,
                 on_log=None, on_progress=None, on_done=None, on_error=None):
        super().__init__(daemon=True)
        self.left_file = left_file
        self.right_file = right_file
        self.out_file = out_file
        self.layout = layout
        self.container = container
        self.encoder = encoder
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
        self.on_log = on_log or (lambda s: None)
        self.on_progress = on_progress or (lambda stage, pct, info: None)
        self.on_done = on_done or (lambda out: None)
        self.on_error = on_error or (lambda err: None)
        self.cancel_flag = False
        self.proc = None
        self.workdir = ""

    def cancel(self):
        self.cancel_flag = True
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def _check(self):
        if self.cancel_flag:
            raise Cancelled()

    def _log(self, s):
        self.on_log(s)

    def run(self):
        try:
            name = os.path.splitext(os.path.basename(self.left_file))[0]
            out_dir = os.path.dirname(os.path.abspath(self.out_file))
            preferred = os.path.join(out_dir, "_bd3d_work_" + name)
            self.workdir = ascii_workdir(preferred, name)
            if os.path.normcase(self.workdir) != os.path.normcase(preferred):
                self._log("输出路径含非 ASCII 字符，中间文件改用：" + self.workdir)
            os.makedirs(self.workdir, exist_ok=True)
            left_es = os.path.join(self.workdir, "left.264")
            right_es = os.path.join(self.workdir, "right.mvc")
            if self.skip_demux and os.path.exists(left_es) and os.path.exists(right_es):
                self._log("[1/3] 跳过解流（使用已有流文件）")
                nframes = self._estimate_frames()
            else:
                self._log("[1/3] 解流（tsMuxeR）：左眼 + 右眼")
                left_es, right_es, nframes = self._demux(name)
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
                else:
                    self._log("未找到音轨输入，将输出无音轨视频")

            self._encode(left_es, right_es, nframes, audio_src, audio_idx, audio_codec)
            self._check()
            self._log("完成：" + self.out_file)
            self.on_progress("done", 100.0, "全部完成")
            if self.open_after:
                try:
                    os.startfile(os.path.dirname(os.path.abspath(self.out_file)))
                except Exception:
                    pass
            self.on_done(self.out_file)
        except Cancelled:
            self._log("任务已取消")
            self.on_error("已取消")
        except Exception as e:
            self._log("错误：" + str(e))
            self.on_error(str(e))

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
        for line in p.stdout:
            self._check()
            line = line.strip()
            if not line:
                continue
            m = re.search(r"([\d.]+)% complete", line)
            if m:
                self.on_progress("demux", float(m.group(1)), "解流中")
            m = re.search(r"Processed (\d+) video frames", line)
            if m:
                nframes = max(nframes, int(m.group(1)))
            if re.search(r"error|Error|错误", line):
                self._log("  tsMuxeR: " + line)
        p.wait()
        self.proc = None
        self._check()
        if p.returncode != 0:
            raise RuntimeError("解流失败（tsMuxeR 返回码 %s）" % p.returncode)
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
            p = run_hidden([FFPROBE, "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", self.left_file])
            dur = float(p.stdout.strip())
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
        args = []
        if self.encoder == "gpu":
            args += ["-c:v", "hevc_amf", "-usage", "transcoding",
                     "-quality", self.speed]
            if self.rc == "cqp":
                args += ["-rc", "cqp", "-qp_i", str(self.qp), "-qp_p", str(self.qp + 2)]
            elif self.rc == "vbr":
                args += ["-rc", "vbr_peak", "-b:v", "%dM" % self.bitrate,
                         "-maxrate", "%dM" % int(self.bitrate * 1.5),
                         "-bufsize", "%dM" % (self.bitrate * 2)]
            else:
                args += ["-rc", "cbr", "-b:v", "%dM" % self.bitrate,
                         "-maxrate", "%dM" % self.bitrate,
                         "-bufsize", "%dM" % (self.bitrate * 2)]
        else:
            args += ["-c:v", "libx265", "-preset",
                     X265_PRESET.get(self.speed, "medium")]
            if self.rc == "cqp":
                args += ["-crf", str(self.qp)]
            else:
                args += ["-b:v", "%dM" % self.bitrate]
        args += ["-g", str(self.gop), "-color_primaries", "bt709",
                 "-color_trc", "bt709", "-colorspace", "bt709",
                 "-color_range", "tv"]
        return args

    def _encode(self, base, dep, nframes, audio_src, audio_idx, audio_codec):
        total = nframes
        if self.max_frames:
            total = min(nframes, self.max_frames)
        avs_path = os.path.join(self.workdir, "decode.avs")
        with open(avs_path, "w", encoding="utf-8") as f:
            f.write(self._build_avs(base, dep, nframes, total))

        # ---- 阶段 2A：编码纯视频 ----
        tmp_video = os.path.join(self.workdir, "video_only.mkv")
        cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
               "-i", avs_path, "-an"] + self._video_args()
        if self.max_frames:
            cmd += ["-frames:v", str(total)]
        cmd += ["-f", "matroska", tmp_video]

        p = popen_hidden(cmd)
        self.proc = p
        cur = 0
        t0 = time.time()
        for line in p.stdout:
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
        p.wait()
        self.proc = None
        self._check()
        if p.returncode != 0:
            raise RuntimeError("视频编码失败（ffmpeg 返回码 %s）" % p.returncode)
        for f in (base, dep):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass

        # ---- 阶段 2B：提取音频到独立文件（顺序 I/O）----
        if self.audio_mode == "none" or not audio_src:
            if os.path.exists(self.out_file):
                os.remove(self.out_file)
            os.replace(tmp_video, self.out_file)
            return
        dur = total * 1001.0 / 24000.0 + 0.2
        is_mp4 = self.container == "mp4"
        main_copy_ok = (not is_mp4) or audio_codec in ("aac", "ac3", "mp3")
        extract_jobs = []
        if self.audio_mode in ("dual", "copy"):
            opts = ["-c:a", "copy"] if main_copy_ok else ["-c:a", "ac3", "-b:a", "640k"]
            extract_jobs.append((os.path.join(self.workdir, "audio_main.mka"), opts))
        if self.audio_mode in ("dual", "aac_only"):
            extract_jobs.append((os.path.join(self.workdir, "audio_aac.mka"),
                                 ["-c:a", "aac", "-b:a", "512k", "-ac", "6"]))
        audio_files = []
        base_pct = DEMUX_WEIGHT + VIDEO_WEIGHT
        for ai, (apath, aopts) in enumerate(extract_jobs):
            self._log("提取音频 %d/%d ..." % (ai + 1, len(extract_jobs)))
            cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
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

        # ---- 阶段 2C：混流（纯顺序 I/O）----
        cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
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
        try:
            os.remove(tmp_video)
        except OSError:
            pass
        if not os.path.exists(self.out_file) or os.path.getsize(self.out_file) < 1024 * 1024:
            raise RuntimeError("输出文件异常，请查看日志")


def concat_files(files, out_file, on_log=None, on_done=None, on_error=None):
    """用 concat 解复用器无损拼接多个视频文件"""
    def work():
        try:
            lst = os.path.join(os.path.dirname(out_file), "_concat_list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for p in files:
                    f.write("file '%s'\n" % p.replace("\\", "/"))
            cmd = [FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                   "-i", lst, "-c", "copy", "-map", "0", out_file]
            p = popen_hidden(cmd)
            lines = []
            for line in p.stdout:
                lines.append(line)
                if on_log and ("error" in line.lower() or "Error" in line):
                    on_log("  ffmpeg: " + line.strip())
            p.wait()
            if p.returncode != 0:
                raise RuntimeError("拼接失败：\n" + "".join(lines[-8:]))
            if on_done:
                on_done(out_file)
        except Exception as e:
            if on_error:
                on_error(str(e))
    threading.Thread(target=work, daemon=True).start()


# ==================== UI ====================
class Bridge(QObject):
    """线程 -> UI 的信号桥"""
    log = Signal(str)
    progress = Signal(float, str)
    done = Signal(str)
    error = Signal(str)


class Section(QFrame):
    """可折叠设置区（Qt 版）"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.PointingHandCursor)
        self._expanded = False
        self._summary = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 6, 14, 8)
        lay.setSpacing(8)

        self._head = QWidget()
        self._head.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(self._head)
        h.setContentsMargins(0, 3, 0, 3)
        self._arrow = QLabel()
        self._arrow.setFixedWidth(18)
        _pm = icon_pixmap("chevron-right.svg", 16)
        if _pm is not None:
            self._arrow.setPixmap(_pm)
        else:
            self._arrow.setText("›")
            self._arrow.setStyleSheet("color:#9a9ca8;")
        self._title = QLabel(title)
        self._title.setStyleSheet("font-weight:600;")
        self._sum = QLabel("")
        self._sum.setStyleSheet("color:#6b6d78;")
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

    def _on_click(self, event):
        self.toggle()
        event.accept()

    def toggle(self):
        self._expanded = not self._expanded
        _pm = icon_pixmap("chevron-down.svg" if self._expanded
                          else "chevron-right.svg", 16)
        if _pm is not None:
            self._arrow.setPixmap(_pm)
        else:
            self._arrow.setText("⌄" if self._expanded else "›")
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
        self.job = None
        self.audio_tracks = []
        self.bridge = Bridge()
        self.bridge.log.connect(self._log)
        self.bridge.progress.connect(self._progress)
        self.bridge.done.connect(self._done)
        self.bridge.error.connect(self._fail)

        self.setWindowTitle(APP_TITLE)
        self.setObjectName("mainwin")
        self.resize(900, 800)
        self.setMinimumSize(800, 640)
        try:
            self.setWindowIcon(QIcon(ICON_PATH))
        except Exception:
            pass

        # ---------- 滚动容器：展开折叠区时向下延伸，不挤压其它控件 ----------
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: #17181c; }")
        outer.addWidget(scroll)
        content = QWidget()
        content.setObjectName("scrollcontent")
        content.setStyleSheet("background: #17181c;")
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
        v.setStyleSheet("color:#6b6d78;")
        top.addWidget(v)
        top.addStretch(1)
        top.addWidget(QLabel("3D 蓝光双流 → SBS / TAB · GPU 硬件加速"))
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
        self._file_row(cv, "输出到", self.le_out, self.pick_out,
                       "建议输出磁盘剩余空间 ≥ 45 GB")
        root.addWidget(card)

        # ---------- 输出格式 ----------
        self.sec_fmt = Section("输出格式")
        r = QHBoxLayout()
        r.addWidget(QLabel("3D 布局"))
        self.cmb_layout = QComboBox()
        self.cmb_layout.addItems([x[0] for x in LAYOUTS])
        self._set_combo(self.cmb_layout, self.cfg.get("layout", LAYOUTS[0][0]))
        r.addWidget(self.cmb_layout, 2)
        r.addWidget(QLabel("容器"))
        self.cmb_container = QComboBox()
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
        self.cmb_encoder = QComboBox()
        self.cmb_encoder.addItems([x[0] for x in ENCODERS])
        self._set_combo(self.cmb_encoder, self.cfg.get("encoder", ENCODERS[0][0]))
        r.addWidget(self.cmb_encoder, 2)
        r.addWidget(QLabel("速度"))
        self.cmb_speed = QComboBox()
        self.cmb_speed.addItems([x[0] for x in SPEEDS])
        self._set_combo(self.cmb_speed, self.cfg.get("speed", SPEEDS[0][0]))
        r.addWidget(self.cmb_speed, 1)
        r.addStretch(1)
        self.sec_enc.body_layout.addLayout(r)
        r = QHBoxLayout()
        r.addWidget(QLabel("质量模式"))
        self.cmb_rc = QComboBox()
        self.cmb_rc.addItems([x[0] for x in RC_MODES])
        self._set_combo(self.cmb_rc, self.cfg.get("rc", RC_MODES[0][0]))
        r.addWidget(self.cmb_rc, 1)
        r.addWidget(QLabel("质量"))
        self.cmb_qp = QComboBox()
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
        self.cmb_track = QComboBox()
        self.cmb_track.addItem("自动（英语优先，最高声道）")
        r.addWidget(self.cmb_track, 3)
        r.addWidget(QLabel("音频输出"))
        self.cmb_audio = QComboBox()
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
        self.chk_open = QCheckBox("完成后打开输出目录")
        self.chk_open.setChecked(bool(self.cfg.get("open_after", True)))
        r.addWidget(self.chk_open)
        r.addWidget(QLabel("限制帧数（调试）"))
        self.le_frames = QLineEdit("")
        self.le_frames.setFixedWidth(80)
        r.addWidget(self.le_frames)
        r.addStretch(1)
        self.sec_adv.body_layout.addLayout(r)
        root.addWidget(self.sec_adv)

        # ---------- 按钮 ----------
        brow = QHBoxLayout()
        self.btn_start = QPushButton("开始转换")
        self.btn_start.setObjectName("accent")
        self.btn_start.setFixedWidth(130)
        self.btn_start.clicked.connect(self.start)
        brow.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setFixedWidth(90)
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel)
        brow.addWidget(self.btn_cancel)
        brow.addStretch(1)
        self.btn_concat = QPushButton("无损拼接（完整片）")
        self.btn_concat.clicked.connect(self.concat)
        brow.addWidget(self.btn_concat)
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
        self.lbl_stage.setStyleSheet("color:#9a9ca8;")
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
        self.lbl_stat.setStyleSheet("color:#6b6d78; font-size:9.5pt;")
        cv.addWidget(self.lbl_stat)
        root.addWidget(card)

        # ---------- 日志 ----------
        card = QFrame()
        card.setObjectName("card")
        cv = QVBoxLayout(card)
        cv.setContentsMargins(14, 10, 14, 12)
        cv.setSpacing(6)
        lt = QLabel("日志")
        lt.setStyleSheet("color:#9a9ca8; font-weight:600;")
        cv.addWidget(lt)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFixedHeight(200)
        cv.addWidget(self.log)
        root.addWidget(card)

        # 联动摘要
        for cb in (self.cmb_layout, self.cmb_container, self.cmb_encoder,
                   self.cmb_speed, self.cmb_qp, self.cmb_audio):
            cb.currentTextChanged.connect(lambda _=None: self._refresh_summaries())
        self.cmb_rc.currentTextChanged.connect(self._on_rc_change)
        self.le_gop.textChanged.connect(lambda _=None: self._refresh_summaries())
        self.chk_open.stateChanged.connect(lambda _=None: self._refresh_summaries())

        self._on_rc_change(self.cmb_rc.currentText())
        self._refresh_summaries()
        self._log("就绪。选择左眼/右眼视频流文件与输出路径后点击「开始转换」。")

    # ---------- UI 工具 ----------
    @staticmethod
    def _set_combo(cb, value):
        i = cb.findText(value)
        if i >= 0:
            cb.setCurrentIndex(i)

    def _file_row(self, parent, label, le, browse_cmd, hint=""):
        row = QHBoxLayout()
        lb = QLabel(label)
        lb.setFixedWidth(64)
        row.addWidget(lb)
        row.addWidget(le, 1)
        b = QPushButton("浏览")
        b.setFixedWidth(64)
        b.clicked.connect(browse_cmd)
        row.addWidget(b)
        parent.addLayout(row)
        h = QLabel(hint)
        h.setStyleSheet("color:#6b6d78; font-size:9.5pt;")
        h.setContentsMargins(72, 0, 0, 4)
        parent.addWidget(h)

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

    def _refresh_summaries(self):
        self.sec_fmt.set_summary("%s · %s" % (
            self.cmb_layout.currentText().split("  ")[0],
            self.cmb_container.currentText().split("（")[0]))
        enc = "GPU" if self.cmb_encoder.currentText() == ENCODERS[0][0] else "CPU"
        self.sec_enc.set_summary("%s · %s · %s" % (
            enc, self.cmb_qp.currentText().split("（")[0],
            self.cmb_speed.currentText()))
        self.sec_aud.set_summary("自动 · %s" % self.cmb_audio.currentText().split("（")[0])
        self.sec_adv.set_summary("GOP %s%s" % (
            self.le_gop.text(), " · 完成后打开" if self.chk_open.isChecked() else ""))

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        cfg = {"left": self.le_left.text(), "right": self.le_right.text(),
               "out": self.le_out.text(), "layout": self.cmb_layout.currentText(),
               "container": self.cmb_container.currentText(),
               "encoder": self.cmb_encoder.currentText(),
               "speed": self.cmb_speed.currentText(),
               "rc": self.cmb_rc.currentText(), "qp": self.cmb_qp.currentText(),
               "bitrate": self.le_bitrate.text(),
               "audio": self.cmb_audio.currentText(),
               "gop": self.le_gop.text(),
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
            if not self.le_out.text():
                base = os.path.splitext(os.path.basename(p))[0]
                self.le_out.setText(os.path.join(os.path.dirname(p), "..",
                                                 "..", "..", "sbs_" + base + ".mkv"))
            self._auto_pair(p, True)
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

    def _refresh_tracks(self, m2ts):
        def work():
            tracks = probe_audio_tracks(m2ts)
            if tracks:
                QTimer.singleShot(0, lambda: self._apply_tracks(tracks))
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

    def _progress(self, pct, info):
        self.pb.setValue(int(max(0.0, min(pct, 100.0)) * 10))
        self.lbl_pct.setText("%.1f%%" % pct)
        self.lbl_stat.setText(info)

    def _done(self, out):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.lbl_stage.setText("完成")
        self._progress(100.0, "输出：" + out)
        QMessageBox.information(self, APP_TITLE, "转换完成！\n\n" + out)

    def _fail(self, err):
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.lbl_stage.setText("失败")
        if err != "已取消":
            QMessageBox.critical(self, APP_TITLE, "任务失败：\n" + err)

    def _check_disk_space(self, out):
        try:
            total, used, free = shutil.disk_usage(os.path.dirname(os.path.abspath(out)))
            if free < 45 * 1024 ** 3:
                r = QMessageBox.question(
                    self, APP_TITLE,
                    "输出目录所在磁盘剩余空间为 %.1f GB，低于建议值 45 GB。\n"
                    "转换过程可能因空间不足而失败，是否仍要继续？" % (free / 1024 ** 3),
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                return r == QMessageBox.Yes
        except Exception:
            pass
        return True

    def start(self):
        if self.job and self.job.is_alive():
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
        self._save_cfg()
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.lbl_stage.setText("准备中")
        self._log("========== 任务参数 ==========")
        self._log("左眼源文件：%s" % left)
        self._log("右眼源文件：%s" % right)
        self._log("输出文件：%s" % out)
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
            encoder=self._sel(ENCODERS, self.cmb_encoder.currentText(), "gpu"),
            rc=self._sel(RC_MODES, self.cmb_rc.currentText(), "cqp"),
            qp=qp, bitrate=bitrate,
            speed=self._sel(SPEEDS, self.cmb_speed.currentText(), "quality"),
            gop=gop, audio_mode=audio_mode, audio_track=audio_track,
            open_after=self.chk_open.isChecked(), max_frames=max_frames,
            on_log=lambda s: self.bridge.log.emit(s),
            on_progress=lambda st, pct, info: self.bridge.progress.emit(pct, info),
            on_done=lambda o: self.bridge.done.emit(o),
            on_error=lambda e: self.bridge.error.emit(e))
        self.job.start()

    def cancel(self):
        if self.job:
            self._log("正在取消...")
            self.lbl_stage.setText("正在取消")
            self.job.cancel()

    def concat(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "按顺序选择要拼接的视频（第一段、第二段...）", "",
            "视频文件 (*.mkv *.mp4);;所有文件 (*)")
        if not files or len(files) < 2:
            return
        out, _ = QFileDialog.getSaveFileName(
            self, "保存合并后的文件", "", "Matroska 视频 (*.mkv)")
        if not out:
            return
        self._log("开始拼接 %d 个文件" % len(files))
        self.lbl_stage.setText("拼接中")
        concat_files(list(files), out,
                     on_log=lambda s: self.bridge.log.emit(s),
                     on_done=lambda o: self.bridge.done.emit(o),
                     on_error=lambda e: self.bridge.error.emit(e))

    def closeEvent(self, event):
        if self.job and self.job.is_alive():
            r = QMessageBox.question(self, APP_TITLE, "任务进行中，确定要退出吗？",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                event.ignore()
                return
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
        qidx = int(get("--quality", "0") or 0)
        qp = QP_LEVELS[max(0, min(qidx, len(QP_LEVELS) - 1))][1]
        if not left or not right or not out:
            print("用法: python bd3d2sbs.py --cli --left 00000.m2ts --right 00001.m2ts "
                  "--out x.mkv [--layout full_sbs|half_sbs|full_tab|half_tab] "
                  "[--container mkv|mp4] [--encoder gpu|cpu] [--quality 0-3] "
                  "[--bitrate 20] [--frames N] [--noaudio] [--skipdemux]")
            return 1
        job = ConvertJob(
            left, right, out,
            layout=get("--layout", "full_sbs"),
            container=get("--container", "mkv"),
            encoder=get("--encoder", "gpu"),
            bitrate=int(get("--bitrate", "20") or 20),
            qp=qp,
            audio_mode=("none" if noaudio else "dual"),
            max_frames=frames, skip_demux=skipdemux,
            on_log=lambda s: print(s, flush=True),
            on_progress=lambda st, pct, info: print(
                "[%5.1f%%] %s %s" % (pct, st, info), flush=True))
        job.run()
        return 0
    app = QApplication(sys.argv)
    # Fusion 风格：所有控件完全由 QSS 渲染（消除 Windows 原生风格与深色主题混杂）
    app.setStyle("Fusion")
    app.setStyleSheet(QSS_TEMPLATE.replace("<ICONS>", ICONS_DIR.replace("\\", "/")))
    win = MainWindow()
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
                assert os.path.exists(TSMUXER), "找不到 tsMuxeR: " + TSMUXER
                assert os.path.exists(FRIMSOURCE), "找不到 FRIMSource: " + FRIMSOURCE
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
