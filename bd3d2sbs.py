# -*- coding: utf-8 -*-
"""
BD3D -> 3D 视频转换器
把 3D 蓝光（MVC 编码）转成 SBS/TAB 立体视频（HEVC，AMD GPU 硬件编码）。

流程：tsMuxeR 解流 -> FRIMSource 解码 MVC -> AviSynth 合成布局 -> ffmpeg 编码

用法（GUI）: python bd3d2sbs.py
用法（CLI）: python bd3d2sbs.py --cli --mpls "xxx.mpls" --out "xxx.mkv"
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
from tkinter import filedialog, messagebox

import customtkinter as ctk

APP_TITLE = "BD3D 转换器"
APP_VERSION = "v1.3"

# ---- 界面配色（深色专业风格）----
C_BG = "#17181c"
C_CARD = "#202127"
C_BORDER = "#2e3038"
C_INPUT = "#2a2c34"
C_TEXT = "#e8e9ed"
C_DIM = "#9a9ca8"
C_FAINT = "#6b6d78"
C_ACCENT = "#3574f0"
C_ACCENT_H = "#2b5fd0"
C_BTN2 = "#33363e"
C_BTN2_H = "#3d4149"
C_LOG_BG = "#121317"

F_TITLE = ("Microsoft YaHei UI", 15, "bold")
F_CARDT = ("Microsoft YaHei UI", 11, "bold")
F_LABEL = ("Microsoft YaHei UI", 11)
F_BTN = ("Microsoft YaHei UI", 12, "bold")
F_BIG = ("Microsoft YaHei UI", 22, "bold")
F_SMALL = ("Microsoft YaHei UI", 10)
F_MONO = ("Consolas", 10)
F_ARROW = ("Segoe UI Symbol", 11)

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
VIDEO_WEIGHT = 92.0
MUX_WEIGHT = 5.0

if getattr(sys, "frozen", False):
    _BIN_BASE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    _CFG_BASE = os.path.dirname(sys.executable)
else:
    _BIN_BASE = _CFG_BASE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(_BIN_BASE, "bin")
FFMPEG = os.path.join(BIN_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(BIN_DIR, "ffprobe.exe")
TSMUXER = os.path.join(BIN_DIR, "tsMuxeR.exe")
FRIMSOURCE = os.path.join(BIN_DIR, "FRIMSource.dll")
ICON_PATH = os.path.join(_BIN_BASE, "app.ico")
CONFIG_PATH = os.path.join(_CFG_BASE, "config.json")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class Cancelled(Exception):
    pass


def popen_hidden(cmd):
    return subprocess.Popen(
        cmd, creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", bufsize=1)


def fmt_time(seconds):
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "%d 分 %d 秒" % (seconds // 60, seconds % 60)
    return "%d 秒" % seconds


def probe_audio_tracks(mpls):
    """返回 ([(pos, label, codec, channels, lang), ...], m2ts_path)"""
    playlist_dir = os.path.dirname(os.path.abspath(mpls))
    stream_dir = os.path.join(os.path.dirname(playlist_dir), "STREAM")
    name = os.path.splitext(os.path.basename(mpls))[0]
    src = None
    for cand in (os.path.join(stream_dir, name + ".m2ts"),
                 os.path.join(stream_dir, "00000.m2ts")):
        if os.path.exists(cand):
            src = cand
            break
    if not src:
        return [], None
    try:
        p = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_name,channels:stream_tags=language",
             "-of", "json", src],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=CREATE_NO_WINDOW)
        streams = json.loads(p.stdout).get("streams", [])
    except Exception:
        return [], src
    tracks = []
    for pos, s in enumerate(streams):
        codec = s.get("codec_name", "?")
        ch = int(s.get("channels") or 0)
        lang = (s.get("tags") or {}).get("language", "und")
        label = "#%d  %s  %d.%d  %s" % (pos + 1, lang, ch - 2 if ch >= 2 else 0,
                                        1, codec)
        tracks.append((pos, label, codec, ch, lang))
    return tracks, src


class ConvertJob(threading.Thread):
    """单个片段的完整转换任务（解流 -> 视频编码 -> 混流）"""

    def __init__(self, mpls, out_file, layout="full_sbs", container="mkv",
                 encoder="gpu", rc="cqp", qp=18, bitrate=20, speed="quality",
                 gop=96, audio_mode="dual", audio_track=None,
                 open_after=False, max_frames=0, skip_demux=False,
                 on_log=None, on_progress=None, on_done=None, on_error=None):
        super().__init__(daemon=True)
        self.mpls = mpls
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

    def _find_m2ts(self):
        playlist_dir = os.path.dirname(os.path.abspath(self.mpls))
        stream_dir = os.path.join(os.path.dirname(playlist_dir), "STREAM")
        name = os.path.splitext(os.path.basename(self.mpls))[0]
        for cand in (os.path.join(stream_dir, name + ".m2ts"),
                     os.path.join(stream_dir, "00000.m2ts")):
            if os.path.exists(cand):
                return cand
        return None

    def run(self):
        try:
            name = os.path.splitext(os.path.basename(self.mpls))[0]
            out_dir = os.path.dirname(os.path.abspath(self.out_file))
            self.workdir = os.path.join(out_dir, "_bd3d_work_" + name)
            os.makedirs(self.workdir, exist_ok=True)
            base = os.path.join(self.workdir, name + ".track_4113.264")
            dep = os.path.join(self.workdir, name + ".track_4114.mvc")
            if self.skip_demux and os.path.exists(base) and os.path.exists(dep):
                self._log("[1/3] 跳过解流（使用已有流文件）")
                nframes = self._estimate_frames()
            else:
                self._log("[1/3] 解流（tsMuxeR）：" + self.mpls)
                base, dep, nframes = self._demux(name)
            if nframes <= 0:
                nframes = self._estimate_frames()
            if nframes > 0:
                self._log("解流完成：%d 帧，开始视频编码" % nframes)

            audio_src, audio_idx, audio_codec = None, None, ""
            if self.audio_mode != "none":
                audio_src, audio_idx = self._probe_audio()
                if audio_src:
                    tracks, _ = probe_audio_tracks(self.mpls)
                    if tracks and audio_idx < len(tracks):
                        audio_codec = tracks[audio_idx][2]
                        self._log("音轨 %s" % tracks[audio_idx][1])
                else:
                    self._log("未找到音轨输入，将输出无音轨视频")

            self._encode(base, dep, nframes, audio_src, audio_idx, audio_codec)
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
        meta_path = os.path.join(self.workdir, "demux.meta")
        meta = ("MUXOPT --no-pcr-on-video-pid --new-audio-pes --demux --vbr --vbv-len=500\n"
                'V_MPEG4/ISO/AVC, "%s", track=4113\n'
                'V_MPEG4/ISO/MVC, "%s", track=4114\n' % (self.mpls, self.mpls))
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
        base = os.path.join(self.workdir, name + ".track_4113.264")
        dep = os.path.join(self.workdir, name + ".track_4114.mvc")
        if p.returncode != 0 or not (os.path.exists(base) and os.path.exists(dep)):
            raise RuntimeError("解流失败（tsMuxeR 返回码 %s），请检查源文件" % p.returncode)
        return base, dep, nframes

    def _estimate_frames(self):
        src = self._find_m2ts()
        if not src:
            return 0
        try:
            p = subprocess.run(
                [FFPROBE, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", src],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW)
            dur = float(p.stdout.strip())
            return int(dur * 24000 / 1001) + 1
        except Exception:
            return 0

    def _probe_audio(self):
        tracks, src = probe_audio_tracks(self.mpls)
        idx = self.audio_track
        if idx is None and tracks:
            best_pos, best_score = 0, (-1, -1)
            for t in tracks:
                score = (1 if t[4] == "eng" else 0, t[3])
                if score > best_score:
                    best_pos, best_score = t[0], score
            idx = best_pos
        return src, (idx if idx is not None else 0)

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
               'num_frames=%d, cache=2, platform="sw")\n'
               'left  = SelectEven(interleaved)\n'
               'right = SelectOdd(interleaved)\n'
               '%s' % (FRIMSOURCE, base, dep, nframes, tail))
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

        # ---- 阶段 2B：混流 ----
        if self.audio_mode == "none" or not audio_src:
            if os.path.exists(self.out_file):
                os.remove(self.out_file)
            os.replace(tmp_video, self.out_file)
            return
        dur = total * 1001.0 / 24000.0 + 0.2
        is_mp4 = self.container == "mp4"
        main_copy_ok = (not is_mp4) or audio_codec in ("aac", "ac3", "mp3")
        cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
               "-i", tmp_video, "-t", "%.3f" % dur, "-i", audio_src,
               "-map", "0:v", "-map", "1:a:%d" % audio_idx]
        if self.audio_mode == "dual":
            cmd += ["-map", "1:a:%d" % audio_idx]
        if self.audio_mode == "aac_only":
            cmd += ["-c:a", "aac", "-b:a", "512k", "-ac", "6"]
        elif self.audio_mode == "copy" and main_copy_ok:
            cmd += ["-c:a", "copy"]
        elif main_copy_ok:
            cmd += ["-c:a:0", "copy"]
        else:
            cmd += ["-c:a", "ac3", "-b:a", "640k"]
        if self.audio_mode == "dual":
            cmd += ["-c:a:1", "aac", "-b:a:1", "512k", "-ac:a:1", "6",
                    "-metadata:s:a:1", "language=eng",
                    "-metadata:s:a:1", "title=AAC 5.1"]
        cmd += ["-c:v", "copy"]
        if is_mp4:
            cmd += ["-f", "mp4", "-tag:v", "hvc1", "-movflags", "+faststart"]
        else:
            cmd += ["-f", "matroska"]
        cmd += [self.out_file]

        p = popen_hidden(cmd)
        self.proc = p
        base_pct = DEMUX_WEIGHT + VIDEO_WEIGHT
        for line in p.stdout:
            self._check()
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    sec = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                frac = min(sec / max(dur, 1), 1.0)
                pct = base_pct + frac * MUX_WEIGHT
                self.on_progress("mux", pct, "混流封装中 %.0f%%" % (frac * 100))
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


def concat_mkvs(files, out_mkv, on_log=None, on_done=None, on_error=None):
    """用 concat 解复用器无损拼接多个 MKV"""
    def work():
        try:
            lst = os.path.join(os.path.dirname(out_mkv), "_concat_list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for p in files:
                    f.write("file '%s'\n" % p.replace("\\", "/"))
            cmd = [FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                   "-i", lst, "-c", "copy", "-map", "0", out_mkv]
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
                on_done(out_mkv)
        except Exception as e:
            if on_error:
                on_error(str(e))
    threading.Thread(target=work, daemon=True).start()


# ==================== GUI ====================
class Section(ctk.CTkFrame):
    """可折叠设置区：点击标题栏展开/收起，收起时显示当前参数摘要"""

    def __init__(self, parent, title):
        super().__init__(parent, fg_color=C_CARD, corner_radius=10,
                         border_width=1, border_color=C_BORDER)
        self._expanded = False
        self._summary = ""

        self.head = ctk.CTkFrame(self, fg_color="transparent", height=36,
                                 cursor="hand2")
        self.head.pack(fill="x")
        self.head.pack_propagate(False)
        self.arrow = ctk.CTkLabel(self.head, text="›", width=16, font=F_ARROW,
                                  text_color=C_DIM)
        self.arrow.pack(side="left", padx=(12, 2))
        self.title_lbl = ctk.CTkLabel(self.head, text=title, font=F_CARDT,
                                      text_color=C_TEXT)
        self.title_lbl.pack(side="left")
        self.sum_lbl = ctk.CTkLabel(self.head, text="", font=F_SMALL,
                                    text_color=C_FAINT)
        self.sum_lbl.pack(side="right", padx=14)
        for w in (self.head, self.arrow, self.title_lbl, self.sum_lbl):
            w.bind("<Button-1>", self.toggle)
            w.bind("<Enter>", lambda e: self.title_lbl.configure(text_color=C_ACCENT))
            w.bind("<Leave>", lambda e: self.title_lbl.configure(text_color=C_TEXT))

        self.body = ctk.CTkFrame(self, fg_color="transparent")

    def toggle(self, _=None):
        self._expanded = not self._expanded
        if self._expanded:
            self.arrow.configure(text="⌄")
            self.body.pack(fill="x", padx=14, pady=(0, 12))
            self.sum_lbl.configure(text="")
        else:
            self.arrow.configure(text="›")
            self.body.pack_forget()
            self.sum_lbl.configure(text=self._summary)

    def set_summary(self, text):
        self._summary = text
        if not self._expanded:
            self.sum_lbl.configure(text=text)


class App:
    def __init__(self, root: "ctk.CTk"):
        self.root = root
        self.job = None
        self.cfg = self._load_cfg()
        self.audio_tracks = []

        root.title(APP_TITLE)
        root.geometry("880x760")
        root.minsize(780, 620)
        root.configure(fg_color=C_BG)
        try:
            root.iconbitmap(ICON_PATH)
        except Exception:
            pass

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(8, weight=1)  # 日志区拉伸

        # ---------- 顶栏 ----------
        top = ctk.CTkFrame(root, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        ctk.CTkLabel(top, text=APP_TITLE, font=F_TITLE,
                     text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(top, text=APP_VERSION, font=F_SMALL, text_color=C_FAINT
                     ).pack(side="left", padx=(7, 0), pady=(4, 0))
        ctk.CTkLabel(top, text="3D 蓝光 → SBS / TAB · GPU 硬件加速",
                     font=F_SMALL, text_color=C_DIM).pack(side="right", pady=(4, 0))

        # ---------- 源与输出 ----------
        card = ctk.CTkFrame(root, fg_color=C_CARD, corner_radius=10,
                            border_width=1, border_color=C_BORDER)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=4)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=10)
        self.var_mpls = ctk.StringVar(value=self.cfg.get("mpls", ""))
        self._file_row(body, "源文件", self.var_mpls, self.pick_mpls,
                       "BDMV\\PLAYLIST 下的 .mpls")
        self.var_out = ctk.StringVar(value=self.cfg.get("out", ""))
        self._file_row(body, "输出到", self.var_out, self.pick_out,
                       "建议输出磁盘剩余空间 ≥ 45 GB")

        # ---------- 输出格式 ----------
        self.sec_fmt = Section(root, "输出格式")
        self.sec_fmt.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        r = ctk.CTkFrame(self.sec_fmt.body, fg_color="transparent")
        r.pack(fill="x", pady=2)
        ctk.CTkLabel(r, text="3D 布局", width=64, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_layout = ctk.StringVar(value=self.cfg.get("layout", LAYOUTS[0][0]))
        self._menu(r, self.var_layout, [x[0] for x in LAYOUTS],
                   250).pack(side="left", padx=(8, 20))
        ctk.CTkLabel(r, text="容器", width=40, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_container = ctk.StringVar(value=self.cfg.get("container", CONTAINERS[0][0]))
        self._menu(r, self.var_container, [x[0] for x in CONTAINERS], 210,
                   self._on_container_change).pack(side="left", padx=8)

        # ---------- 编码设置 ----------
        self.sec_enc = Section(root, "编码设置")
        self.sec_enc.grid(row=3, column=0, sticky="ew", padx=20, pady=4)
        r = ctk.CTkFrame(self.sec_enc.body, fg_color="transparent")
        r.pack(fill="x", pady=2)
        ctk.CTkLabel(r, text="编码器", width=64, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_encoder = ctk.StringVar(value=self.cfg.get("encoder", ENCODERS[0][0]))
        self._menu(r, self.var_encoder, [x[0] for x in ENCODERS],
                   240).pack(side="left", padx=(8, 20))
        ctk.CTkLabel(r, text="速度", width=40, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_speed = ctk.StringVar(value=self.cfg.get("speed", SPEEDS[0][0]))
        self._menu(r, self.var_speed, [x[0] for x in SPEEDS], 120).pack(side="left", padx=8)
        r = ctk.CTkFrame(self.sec_enc.body, fg_color="transparent")
        r.pack(fill="x", pady=2)
        ctk.CTkLabel(r, text="质量模式", width=64, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_rc = ctk.StringVar(value=self.cfg.get("rc", RC_MODES[0][0]))
        self._menu(r, self.var_rc, [x[0] for x in RC_MODES], 150,
                   self._on_rc_change).pack(side="left", padx=(8, 20))
        ctk.CTkLabel(r, text="质量", width=40, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_qp = ctk.StringVar(value=self.cfg.get("qp", QP_LEVELS[0][0]))
        self.menu_qp = self._menu(r, self.var_qp, [x[0] for x in QP_LEVELS], 190)
        self.menu_qp.pack(side="left", padx=(8, 20))
        ctk.CTkLabel(r, text="码率(M)", width=56, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_bitrate = ctk.StringVar(value=str(self.cfg.get("bitrate", 20)))
        self.entry_bitrate = ctk.CTkEntry(r, textvariable=self.var_bitrate, width=60,
                                          fg_color=C_INPUT, border_color=C_BORDER,
                                          text_color=C_TEXT, corner_radius=6)
        self.entry_bitrate.pack(side="left", padx=8)

        # ---------- 音频 ----------
        self.sec_aud = Section(root, "音频")
        self.sec_aud.grid(row=4, column=0, sticky="ew", padx=20, pady=4)
        r = ctk.CTkFrame(self.sec_aud.body, fg_color="transparent")
        r.pack(fill="x", pady=2)
        ctk.CTkLabel(r, text="主音轨", width=64, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_track = ctk.StringVar(value="自动（英语优先，最高声道）")
        self.menu_track = self._menu(r, self.var_track,
                                     ["自动（英语优先，最高声道）"], 290)
        self.menu_track.pack(side="left", padx=(8, 20))
        ctk.CTkLabel(r, text="音频输出", width=64, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_audio = ctk.StringVar(value=self.cfg.get("audio", AUDIO_MODES[0][0]))
        self._menu(r, self.var_audio, [x[0] for x in AUDIO_MODES], 210).pack(side="left", padx=8)

        # ---------- 高级 ----------
        self.sec_adv = Section(root, "高级")
        self.sec_adv.grid(row=5, column=0, sticky="ew", padx=20, pady=4)
        r = ctk.CTkFrame(self.sec_adv.body, fg_color="transparent")
        r.pack(fill="x", pady=2)
        ctk.CTkLabel(r, text="关键帧间隔", width=76, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_gop = ctk.StringVar(value=str(self.cfg.get("gop", 96)))
        ctk.CTkEntry(r, textvariable=self.var_gop, width=60, fg_color=C_INPUT,
                     border_color=C_BORDER, text_color=C_TEXT, corner_radius=6
                     ).pack(side="left", padx=(8, 20))
        self.var_open = ctk.BooleanVar(value=self.cfg.get("open_after", True))
        ctk.CTkCheckBox(r, text="完成后打开输出目录", variable=self.var_open,
                        fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color=C_DIM,
                        font=F_LABEL, checkbox_width=20, checkbox_height=20
                        ).pack(side="left", padx=(0, 20))
        ctk.CTkLabel(r, text="限制帧数（调试）", font=F_LABEL, text_color=C_DIM
                     ).pack(side="left")
        self.var_frames = ctk.StringVar(value="")
        ctk.CTkEntry(r, textvariable=self.var_frames, width=70, fg_color=C_INPUT,
                     border_color=C_BORDER, text_color=C_TEXT, corner_radius=6
                     ).pack(side="left", padx=8)

        # ---------- 按钮 ----------
        btns = ctk.CTkFrame(root, fg_color="transparent")
        btns.grid(row=6, column=0, sticky="ew", padx=20, pady=(10, 4))
        self.btn_start = ctk.CTkButton(
            btns, text="开始转换", command=self.start, width=130, height=36,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#ffffff",
            font=F_BTN, corner_radius=8)
        self.btn_start.pack(side="left")
        self.btn_cancel = ctk.CTkButton(
            btns, text="取消", command=self.cancel, width=86, height=36,
            fg_color=C_BTN2, hover_color=C_BTN2_H, text_color=C_TEXT,
            font=F_LABEL, corner_radius=8, state="disabled")
        self.btn_cancel.pack(side="left", padx=10)
        ctk.CTkButton(
            btns, text="无损拼接 MKV", command=self.concat, width=126, height=36,
            fg_color="transparent", hover_color=C_BTN2, text_color=C_DIM,
            border_width=1, border_color=C_BORDER, font=F_LABEL, corner_radius=8
        ).pack(side="right")

        # ---------- 进度 ----------
        card = ctk.CTkFrame(root, fg_color=C_CARD, corner_radius=10,
                            border_width=1, border_color=C_BORDER)
        card.grid(row=7, column=0, sticky="ew", padx=20, pady=4)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=14, pady=10)
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        self.lbl_pct = ctk.CTkLabel(head, text="0.0%", font=F_BIG, text_color=C_TEXT)
        self.lbl_pct.pack(side="left")
        self.lbl_stage = ctk.CTkLabel(head, text="就绪", font=F_LABEL, text_color=C_DIM)
        self.lbl_stage.pack(side="left", padx=(14, 0), pady=(7, 0))
        self.pb = ctk.CTkProgressBar(body, height=8, progress_color=C_ACCENT,
                                     fg_color=C_INPUT, corner_radius=4)
        self.pb.set(0)
        self.pb.pack(fill="x", pady=(7, 3))
        self.lbl_stat = ctk.CTkLabel(body, text=" ", font=F_SMALL, text_color=C_FAINT,
                                     anchor="w")
        self.lbl_stat.pack(fill="x")

        # ---------- 日志 ----------
        card = ctk.CTkFrame(root, fg_color=C_CARD, corner_radius=10,
                            border_width=1, border_color=C_BORDER)
        card.grid(row=8, column=0, sticky="nsew", padx=20, pady=(4, 14))
        ctk.CTkLabel(card, text="日志", font=F_CARDT, text_color=C_DIM
                     ).pack(anchor="w", padx=14, pady=(8, 2))
        self.log = ctk.CTkTextbox(card, font=F_MONO, fg_color=C_LOG_BG,
                                  text_color="#c8cad2", corner_radius=6,
                                  border_width=1, border_color=C_BORDER,
                                  scrollbar_button_color="#3a3d46")
        self.log.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.log.configure(state="disabled")

        # 摘要联动
        for v in (self.var_layout, self.var_container):
            v.trace_add("write", lambda *a: self._refresh_summaries())
        for v in (self.var_encoder, self.var_speed, self.var_rc, self.var_qp):
            v.trace_add("write", lambda *a: self._refresh_summaries())
        for v in (self.var_track, self.var_audio):
            v.trace_add("write", lambda *a: self._refresh_summaries())
        for v in (self.var_gop, self.var_open):
            v.trace_add("write", lambda *a: self._refresh_summaries())

        self._on_rc_change(self.var_rc.get())
        self._refresh_summaries()
        self.logline("就绪。选择 .mpls 源文件与输出路径后点击「开始转换」。")

    # ---------- UI 工具 ----------
    def _refresh_summaries(self):
        short = lambda s: s.split("  ")[0]
        self.sec_fmt.set_summary("%s · %s" % (
            short(self.var_layout.get()),
            self.var_container.get().split("（")[0]))
        enc = "GPU" if self.var_encoder.get() == ENCODERS[0][0] else "CPU"
        self.sec_enc.set_summary("%s · %s · %s" % (
            enc, self.var_qp.get().split("（")[0], self.var_speed.get()))
        self.sec_aud.set_summary("%s · %s" % (
            self.var_track.get().split("（")[0],
            self.var_audio.get().split("（")[0]))
        self.sec_adv.set_summary("GOP %s%s" % (
            self.var_gop.get(), " · 完成后打开" if self.var_open.get() else ""))

    def _menu(self, parent, var, values, width, command=None):
        return ctk.CTkOptionMenu(
            parent, variable=var, values=values, width=width,
            fg_color=C_INPUT, button_color=C_INPUT, button_hover_color=C_BTN2_H,
            text_color=C_TEXT, dropdown_fg_color=C_CARD, dropdown_text_color=C_TEXT,
            dropdown_hover_color=C_BTN2_H, corner_radius=6, font=F_LABEL,
            command=command)

    def _file_row(self, parent, label, var, browse_cmd, hint=""):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text=label, width=56, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        ctk.CTkEntry(row, textvariable=var, fg_color=C_INPUT,
                     border_color=C_BORDER, text_color=C_TEXT,
                     corner_radius=6).pack(side="left", fill="x", expand=True,
                                           padx=(8, 8))
        ctk.CTkButton(row, text="浏览", width=64, height=30, command=browse_cmd,
                      fg_color=C_BTN2, hover_color=C_BTN2_H, text_color=C_TEXT,
                      font=F_LABEL, corner_radius=6).pack(side="left")
        if hint:
            ctk.CTkLabel(parent, text=hint, font=F_SMALL, text_color=C_FAINT,
                         anchor="w").pack(fill="x", padx=(64, 0), pady=(0, 2))

    def _on_rc_change(self, value):
        is_cqp = value == RC_MODES[0][0]
        self.menu_qp.configure(state="normal" if is_cqp else "disabled")
        self.entry_bitrate.configure(state="disabled" if is_cqp else "normal")
        self._refresh_summaries()

    def _on_container_change(self, value):
        if value == CONTAINERS[1][0]:  # MP4
            cur = self.var_out.get()
            if cur and cur.lower().endswith(".mkv"):
                self.var_out.set(os.path.splitext(cur)[0] + ".mp4")

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        cfg = {"mpls": self.var_mpls.get(), "out": self.var_out.get(),
               "layout": self.var_layout.get(), "container": self.var_container.get(),
               "encoder": self.var_encoder.get(), "speed": self.var_speed.get(),
               "rc": self.var_rc.get(), "qp": self.var_qp.get(),
               "bitrate": self.var_bitrate.get(), "audio": self.var_audio.get(),
               "gop": self.var_gop.get(), "open_after": self.var_open.get()}
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 事件 ----------
    def pick_mpls(self):
        p = filedialog.askopenfilename(
            title="选择播放列表文件（.mpls）",
            filetypes=[("蓝光播放列表", "*.mpls"), ("所有文件", "*.*")])
        if p:
            self.var_mpls.set(p)
            if not self.var_out.get():
                base = os.path.splitext(os.path.basename(p))[0]
                self.var_out.set(os.path.join(os.path.dirname(p), "..",
                                              "..", "..", "sbs_" + base + ".mkv"))
            self._refresh_tracks(p)

    def _refresh_tracks(self, mpls):
        def work():
            tracks, _ = probe_audio_tracks(mpls)
            def apply():
                if not tracks:
                    return
                self.audio_tracks = tracks
                labels = ["自动（英语优先，最高声道）"] + [t[1] for t in tracks]
                self.menu_track.configure(values=labels)
                self.var_track.set(labels[0])
                self.logline("检测到 %d 条音轨" % len(tracks))
            self.root.after(0, apply)
        threading.Thread(target=work, daemon=True).start()

    def pick_out(self):
        p = filedialog.asksaveasfilename(
            title="保存输出文件", defaultextension=".mkv",
            filetypes=[("Matroska 视频", "*.mkv"), ("MP4 视频", "*.mp4")])
        if p:
            self.var_out.set(p)

    def logline(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", time.strftime("[%H:%M:%S] ") + s + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_progress(self, pct, info):
        self.pb.set(max(0.0, min(pct, 100.0)) / 100.0)
        self.lbl_pct.configure(text="%.1f%%" % pct)
        self.lbl_stat.configure(text=info)

    def _check_disk_space(self, out):
        try:
            total, used, free = shutil.disk_usage(os.path.dirname(os.path.abspath(out)))
            if free < 45 * 1024 ** 3:
                return messagebox.askyesno(
                    APP_TITLE,
                    "输出目录所在磁盘剩余空间为 %.1f GB，低于建议值 45 GB。\n"
                    "转换过程可能因空间不足而失败，是否仍要继续？" % (free / 1024 ** 3))
        except Exception:
            pass
        return True

    def _sel(self, presets, value, default):
        for name, val in presets:
            if name == value:
                return val
        return default

    # ---------- 任务 ----------
    def start(self):
        if self.job and self.job.is_alive():
            return
        mpls = self.var_mpls.get().strip()
        out = self.var_out.get().strip()
        if not mpls or not os.path.exists(mpls):
            messagebox.showerror(APP_TITLE, "请选择有效的 .mpls 源文件")
            return
        if not out:
            messagebox.showerror(APP_TITLE, "请选择输出文件路径")
            return
        if not self._check_disk_space(out):
            return
        container = self._sel(CONTAINERS, self.var_container.get(), "mkv")
        if container == "mp4" and not out.lower().endswith(".mp4"):
            out = os.path.splitext(out)[0] + ".mp4"
            self.var_out.set(out)
        try:
            max_frames = int(self.var_frames.get()) if self.var_frames.get().strip() else 0
            gop = max(12, int(self.var_gop.get()))
            bitrate = max(1, int(self.var_bitrate.get()))
        except ValueError:
            max_frames, gop, bitrate = 0, 96, 20
        qp = self._sel(QP_LEVELS, self.var_qp.get(), 18)
        audio_mode = self._sel(AUDIO_MODES, self.var_audio.get(), "dual")
        audio_track = None
        if self.audio_tracks:
            labels = ["自动（英语优先，最高声道）"] + [t[1] for t in self.audio_tracks]
            try:
                pos = labels.index(self.var_track.get()) - 1
                if pos >= 0:
                    audio_track = self.audio_tracks[pos][0]
            except ValueError:
                pass
        self._save_cfg()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.lbl_stage.configure(text="准备中")
        self.logline("开始任务：%s" % os.path.basename(mpls))
        self.logline("布局 %s · 容器 %s · 编码器 %s" % (
            self.var_layout.get().split("  ")[0], container.upper(),
            "GPU" if self.var_encoder.get() == ENCODERS[0][0] else "CPU"))
        self.job = ConvertJob(
            mpls, out,
            layout=self._sel(LAYOUTS, self.var_layout.get(), "full_sbs"),
            container=container,
            encoder=self._sel(ENCODERS, self.var_encoder.get(), "gpu"),
            rc=self._sel(RC_MODES, self.var_rc.get(), "cqp"),
            qp=qp, bitrate=bitrate,
            speed=self._sel(SPEEDS, self.var_speed.get(), "quality"),
            gop=gop, audio_mode=audio_mode, audio_track=audio_track,
            open_after=self.var_open.get(), max_frames=max_frames,
            on_log=lambda s: self.root.after(0, self.logline, s),
            on_progress=lambda st, pct, info: self.root.after(0, self._set_progress, pct, info),
            on_done=lambda o: self.root.after(0, self.done, o),
            on_error=lambda e: self.root.after(0, self.fail, e))
        self.job.start()

    def cancel(self):
        if self.job:
            self.logline("正在取消...")
            self.lbl_stage.configure(text="正在取消")
            self.job.cancel()

    def done(self, out):
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.lbl_stage.configure(text="完成")
        self._set_progress(100.0, "输出：" + out)
        messagebox.showinfo(APP_TITLE, "转换完成！\n\n" + out)

    def fail(self, err):
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.lbl_stage.configure(text="失败")
        if err != "已取消":
            messagebox.showerror(APP_TITLE, "任务失败：\n" + err)

    def concat(self):
        files = filedialog.askopenfilenames(
            title="按顺序选择要拼接的 MKV（第一段、第二段...）",
            filetypes=[("Matroska 视频", "*.mkv")])
        if not files or len(files) < 2:
            return
        out = filedialog.asksaveasfilename(
            title="保存合并后的文件", defaultextension=".mkv",
            filetypes=[("Matroska 视频", "*.mkv")])
        if not out:
            return
        self.logline("开始拼接 %d 个文件" % len(files))
        self.lbl_stage.configure(text="拼接中")
        concat_mkvs(list(files), out,
                    on_log=lambda s: self.root.after(0, self.logline, s),
                    on_done=lambda o: self.root.after(0, self._concat_done, o),
                    on_error=lambda e: self.root.after(0, self.fail, e))

    def _concat_done(self, out):
        self._set_progress(100.0, "拼接完成：" + out)
        self.lbl_stage.configure(text="完成")
        messagebox.showinfo(APP_TITLE, "拼接完成！\n\n" + out)


def main():
    args = sys.argv[1:]
    if "--cli" in args:
        def get(flag, default=None):
            if flag in args:
                return args[args.index(flag) + 1]
            return default
        mpls = get("--mpls")
        out = get("--out")
        frames = int(get("--frames", "0") or 0)
        noaudio = "--noaudio" in args
        skipdemux = "--skipdemux" in args
        qidx = int(get("--quality", "0") or 0)
        qp = QP_LEVELS[max(0, min(qidx, len(QP_LEVELS) - 1))][1]
        if not mpls or not out:
            print("用法: python bd3d2sbs.py --cli --mpls x.mpls --out x.mkv "
                  "[--layout full_sbs|half_sbs|full_tab|half_tab] "
                  "[--container mkv|mp4] [--encoder gpu|cpu] [--quality 0-3] "
                  "[--bitrate 20] [--frames N] [--noaudio] [--skipdemux]")
            return 1
        job = ConvertJob(
            mpls, out,
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
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    App(root)
    if "--selftest" in args:
        root.after(600, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
