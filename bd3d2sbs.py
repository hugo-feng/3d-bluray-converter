# -*- coding: utf-8 -*-
"""
BD3D 3D 视频转换器
把 3D 蓝光原盘（左右眼双流）转成 SBS/TAB 立体视频（HEVC，AMD GPU 硬件编码）。

源文件结构：BDMV\\STREAM 下
  00000.m2ts = 左眼（AVC 基础视图）
  00001.m2ts = 右眼（MVC 依赖视图）

流程：tsMuxeR 分别解出两路 ES -> FRIMSource 解码 MVC -> AviSynth 合成布局 -> ffmpeg 编码
界面：原生 tkinter/ttk（无 Canvas 控件），窗口缩放流畅

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
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_TITLE = "BD3D 转换器"
APP_VERSION = "v1.5"

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
F_BTN = ("Microsoft YaHei UI", 11, "bold")
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
VIDEO_WEIGHT = 88.0
AUDIO_WEIGHT = 4.0
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
            self.workdir = os.path.join(out_dir, "_bd3d_work_" + name)
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
            for line in p.stdout:
                self._check()
                line = line.strip()
                if line.startswith("out_time_us="):
                    try:
                        sec = int(line.split("=", 1)[1]) / 1e6
                    except ValueError:
                        continue
                    frac = min(sec / max(dur, 1), 1.0)
                    pct = base_pct + (ai + frac) / len(extract_jobs) * AUDIO_WEIGHT
                    self.on_progress("audio", pct, "音频提取 %d/%d" % (
                        ai + 1, len(extract_jobs)))
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


# ==================== GUI ====================
class Section(tk.Frame):
    """可折叠设置区（原生 tk 控件，缩放流畅）"""

    def __init__(self, parent, title):
        super().__init__(parent, bg=C_CARD, highlightthickness=1,
                         highlightbackground=C_BORDER, highlightcolor=C_BORDER)
        self._expanded = False
        self._summary = ""
        self.head = tk.Frame(self, bg=C_CARD, cursor="hand2", height=34)
        self.head.pack(fill="x")
        self.head.pack_propagate(False)
        self.arrow = tk.Label(self.head, text="›", bg=C_CARD, fg=C_DIM,
                              font=F_ARROW, width=2)
        self.arrow.pack(side="left", padx=(10, 0))
        self.title_lbl = tk.Label(self.head, text=title, bg=C_CARD, fg=C_TEXT,
                                  font=F_CARDT)
        self.title_lbl.pack(side="left")
        self.sum_lbl = tk.Label(self.head, text="", bg=C_CARD, fg=C_FAINT,
                                font=F_SMALL)
        self.sum_lbl.pack(side="right", padx=14)
        for w in (self.head, self.arrow, self.title_lbl, self.sum_lbl):
            w.bind("<Button-1>", self.toggle)
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
        self.body = tk.Frame(self, bg=C_CARD)

    def _hover(self, on):
        col = C_ACCENT if on else C_TEXT
        self.title_lbl.configure(fg=col)

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
    def __init__(self, root: tk.Tk):
        self.root = root
        self.job = None
        self.cfg = self._load_cfg()
        self.audio_tracks = []

        root.title(APP_TITLE)
        root.geometry("880x780")
        root.minsize(800, 640)
        root.configure(bg=C_BG)
        try:
            root.iconbitmap(ICON_PATH)
        except Exception:
            pass
        self._set_dark_titlebar()

        self._init_style()

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(8, weight=1)

        # ---------- 顶栏 ----------
        top = tk.Frame(root, bg=C_BG)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(14, 6))
        tk.Label(top, text=APP_TITLE, bg=C_BG, fg=C_TEXT, font=F_TITLE
                 ).pack(side="left")
        tk.Label(top, text=APP_VERSION, bg=C_BG, fg=C_FAINT, font=F_SMALL
                 ).pack(side="left", padx=(7, 0), pady=(4, 0))
        tk.Label(top, text="3D 蓝光双流 → SBS / TAB · GPU 硬件加速",
                 bg=C_BG, fg=C_DIM, font=F_SMALL).pack(side="right", pady=(4, 0))

        # ---------- 源与输出 ----------
        body = self._card(root, None)
        self.var_left = tk.StringVar(value=self.cfg.get("left", ""))
        self._file_row(body, "左眼文件", self.var_left, self.pick_left,
                       "BDMV\\STREAM 内的主视频流（如 00000.m2ts）")
        self.var_right = tk.StringVar(value=self.cfg.get("right", ""))
        self._file_row(body, "右眼文件", self.var_right, self.pick_right,
                       "同目录的另一条流（如 00001.m2ts，选左眼后自动配对）")
        self.var_out = tk.StringVar(value=self.cfg.get("out", ""))
        self._file_row(body, "输出到", self.var_out, self.pick_out,
                       "建议输出磁盘剩余空间 ≥ 45 GB")

        # ---------- 输出格式 ----------
        self.sec_fmt = Section(root, "输出格式")
        self.sec_fmt.grid(row=2, column=0, sticky="ew", padx=20, pady=3)
        r = tk.Frame(self.sec_fmt.body, bg=C_CARD)
        r.pack(fill="x", pady=2)
        tk.Label(r, text="3D 布局", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=9, anchor="w").pack(side="left")
        self.var_layout = tk.StringVar(value=self.cfg.get("layout", LAYOUTS[0][0]))
        self._combo(r, self.var_layout, [x[0] for x in LAYOUTS], 26
                    ).pack(side="left", padx=(6, 18))
        tk.Label(r, text="容器", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=5, anchor="w").pack(side="left")
        self.var_container = tk.StringVar(value=self.cfg.get("container", CONTAINERS[0][0]))
        self._combo(r, self.var_container, [x[0] for x in CONTAINERS], 22,
                    self._on_container_change).pack(side="left", padx=6)

        # ---------- 编码设置 ----------
        self.sec_enc = Section(root, "编码设置")
        self.sec_enc.grid(row=3, column=0, sticky="ew", padx=20, pady=3)
        r = tk.Frame(self.sec_enc.body, bg=C_CARD)
        r.pack(fill="x", pady=2)
        tk.Label(r, text="编码器", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=9, anchor="w").pack(side="left")
        self.var_encoder = tk.StringVar(value=self.cfg.get("encoder", ENCODERS[0][0]))
        self._combo(r, self.var_encoder, [x[0] for x in ENCODERS], 26
                    ).pack(side="left", padx=(6, 18))
        tk.Label(r, text="速度", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=5, anchor="w").pack(side="left")
        self.var_speed = tk.StringVar(value=self.cfg.get("speed", SPEEDS[0][0]))
        self._combo(r, self.var_speed, [x[0] for x in SPEEDS], 12
                    ).pack(side="left", padx=6)
        r = tk.Frame(self.sec_enc.body, bg=C_CARD)
        r.pack(fill="x", pady=2)
        tk.Label(r, text="质量模式", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=9, anchor="w").pack(side="left")
        self.var_rc = tk.StringVar(value=self.cfg.get("rc", RC_MODES[0][0]))
        self._combo(r, self.var_rc, [x[0] for x in RC_MODES], 16,
                    self._on_rc_change).pack(side="left", padx=(6, 18))
        tk.Label(r, text="质量", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=5, anchor="w").pack(side="left")
        self.var_qp = tk.StringVar(value=self.cfg.get("qp", QP_LEVELS[0][0]))
        self.cmb_qp = self._combo(r, self.var_qp, [x[0] for x in QP_LEVELS], 20)
        self.cmb_qp.pack(side="left", padx=(6, 18))
        tk.Label(r, text="码率(M)", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=7, anchor="w").pack(side="left")
        self.var_bitrate = tk.StringVar(value=str(self.cfg.get("bitrate", 20)))
        self.entry_bitrate = tk.Entry(
            r, textvariable=self.var_bitrate, width=6, bg=C_INPUT, fg=C_TEXT,
            font=F_LABEL, relief="flat", insertbackground=C_TEXT,
            highlightthickness=1, highlightbackground=C_BORDER,
            highlightcolor=C_ACCENT, disabledbackground="#24262c",
            disabledforeground=C_FAINT)
        self.entry_bitrate.pack(side="left", padx=6)

        # ---------- 音频 ----------
        self.sec_aud = Section(root, "音频")
        self.sec_aud.grid(row=4, column=0, sticky="ew", padx=20, pady=3)
        r = tk.Frame(self.sec_aud.body, bg=C_CARD)
        r.pack(fill="x", pady=2)
        tk.Label(r, text="主音轨", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=9, anchor="w").pack(side="left")
        self.var_track = tk.StringVar(value="自动（英语优先，最高声道）")
        self.cmb_track = self._combo(r, self.var_track,
                                     ["自动（英语优先，最高声道）"], 34)
        self.cmb_track.pack(side="left", padx=(6, 18))
        tk.Label(r, text="音频输出", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=7, anchor="w").pack(side="left")
        self.var_audio = tk.StringVar(value=self.cfg.get("audio", AUDIO_MODES[0][0]))
        self._combo(r, self.var_audio, [x[0] for x in AUDIO_MODES], 24
                    ).pack(side="left", padx=6)

        # ---------- 高级 ----------
        self.sec_adv = Section(root, "高级")
        self.sec_adv.grid(row=5, column=0, sticky="ew", padx=20, pady=3)
        r = tk.Frame(self.sec_adv.body, bg=C_CARD)
        r.pack(fill="x", pady=2)
        tk.Label(r, text="关键帧间隔", bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=9, anchor="w").pack(side="left")
        self.var_gop = tk.StringVar(value=str(self.cfg.get("gop", 96)))
        tk.Entry(r, textvariable=self.var_gop, width=6, bg=C_INPUT, fg=C_TEXT,
                 font=F_LABEL, relief="flat", insertbackground=C_TEXT,
                 highlightthickness=1, highlightbackground=C_BORDER,
                 highlightcolor=C_ACCENT).pack(side="left", padx=(6, 18))
        self.var_open = tk.BooleanVar(value=self.cfg.get("open_after", True))
        ttk.Checkbutton(r, text="完成后打开输出目录", variable=self.var_open,
                        style="Dark.TCheckbutton").pack(side="left", padx=(0, 18))
        tk.Label(r, text="限制帧数（调试）", bg=C_CARD, fg=C_DIM, font=F_LABEL
                 ).pack(side="left")
        self.var_frames = tk.StringVar(value="")
        tk.Entry(r, textvariable=self.var_frames, width=8, bg=C_INPUT, fg=C_TEXT,
                 font=F_LABEL, relief="flat", insertbackground=C_TEXT,
                 highlightthickness=1, highlightbackground=C_BORDER,
                 highlightcolor=C_ACCENT).pack(side="left", padx=6)

        # ---------- 按钮 ----------
        btns = tk.Frame(root, bg=C_BG)
        btns.grid(row=6, column=0, sticky="ew", padx=20, pady=(10, 4))
        self.btn_start = tk.Button(
            btns, text="开始转换", command=self.start, width=12, height=1,
            bg=C_ACCENT, fg="#ffffff", activebackground=C_ACCENT_H,
            activeforeground="#ffffff", font=F_BTN, relief="flat", bd=0,
            cursor="hand2", padx=14, pady=7)
        self.btn_start.pack(side="left")
        self.btn_cancel = tk.Button(
            btns, text="取消", command=self.cancel, width=8,
            bg=C_BTN2, fg=C_TEXT, activebackground=C_BTN2_H,
            activeforeground=C_TEXT, font=F_LABEL, relief="flat", bd=0,
            cursor="hand2", padx=12, pady=7, state="disabled")
        self.btn_cancel.pack(side="left", padx=10)
        tk.Button(
            btns, text="无损拼接（完整片）", command=self.concat, width=16,
            bg=C_CARD, fg=C_DIM, activebackground=C_BTN2,
            activeforeground=C_TEXT, font=F_LABEL, relief="flat", bd=0,
            cursor="hand2", padx=12, pady=7,
            highlightthickness=1, highlightbackground=C_BORDER
        ).pack(side="right")

        # ---------- 进度 ----------
        card = tk.Frame(root, bg=C_CARD, highlightthickness=1,
                        highlightbackground=C_BORDER)
        card.grid(row=7, column=0, sticky="ew", padx=20, pady=3)
        body = tk.Frame(card, bg=C_CARD)
        body.pack(fill="x", padx=14, pady=10)
        head = tk.Frame(body, bg=C_CARD)
        head.pack(fill="x")
        self.lbl_pct = tk.Label(head, text="0.0%", bg=C_CARD, fg=C_TEXT, font=F_BIG)
        self.lbl_pct.pack(side="left")
        self.lbl_stage = tk.Label(head, text="就绪", bg=C_CARD, fg=C_DIM,
                                  font=F_LABEL)
        self.lbl_stage.pack(side="left", padx=(14, 0), pady=(7, 0))
        self.pb = ttk.Progressbar(body, style="Dark.Horizontal.TProgressbar",
                                  maximum=100.0)
        self.pb.pack(fill="x", pady=(7, 3))
        self.lbl_stat = tk.Label(body, text=" ", bg=C_CARD, fg=C_FAINT,
                                 font=F_SMALL, anchor="w")
        self.lbl_stat.pack(fill="x")

        # ---------- 日志 ----------
        card = tk.Frame(root, bg=C_CARD, highlightthickness=1,
                        highlightbackground=C_BORDER)
        card.grid(row=8, column=0, sticky="nsew", padx=20, pady=(3, 14))
        tk.Label(card, text="日志", bg=C_CARD, fg=C_DIM, font=F_CARDT
                 ).pack(anchor="w", padx=14, pady=(8, 2))
        lf = tk.Frame(card, bg=C_LOG_BG)
        lf.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.log = tk.Text(lf, bg=C_LOG_BG, fg="#c8cad2", font=F_MONO,
                           relief="flat", bd=0, wrap="none", height=8,
                           insertbackground=C_TEXT, selectbackground=C_ACCENT,
                           padx=8, pady=6, state="disabled")
        sb = tk.Scrollbar(lf, command=self.log.yview, bg=C_BTN2,
                          troughcolor=C_LOG_BG, activebackground=C_BTN2_H,
                          relief="flat", bd=0, width=12)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        for v in (self.var_layout, self.var_container, self.var_encoder,
                  self.var_speed, self.var_rc, self.var_qp, self.var_track,
                  self.var_audio, self.var_gop, self.var_open):
            v.trace_add("write", lambda *a: self._refresh_summaries())

        self._on_rc_change(self.var_rc.get())
        self._refresh_summaries()
        self.logline("就绪。选择左眼/右眼视频流文件与输出路径后点击「开始转换」。")

    # ---------- 主题 ----------
    def _set_dark_titlebar(self):
        """Windows 深色标题栏"""
        try:
            import ctypes
            self.root.update()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            val = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass

    def _init_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Dark.TCombobox",
                        fieldbackground=C_INPUT, background=C_INPUT,
                        foreground=C_TEXT, arrowcolor=C_DIM,
                        bordercolor=C_BORDER, lightcolor=C_INPUT,
                        darkcolor=C_INPUT, selectbackground=C_INPUT,
                        selectforeground=C_TEXT, padding=(6, 3))
        style.map("Dark.TCombobox",
                  fieldbackground=[("readonly", C_INPUT)],
                  foreground=[("readonly", C_TEXT)],
                  bordercolor=[("focus", C_ACCENT)],
                  arrowcolor=[("active", C_TEXT)])
        style.configure("Dark.TCheckbutton", background=C_CARD, foreground=C_DIM,
                        focuscolor=C_CARD)
        style.map("Dark.TCheckbutton",
                  background=[("active", C_CARD)],
                  foreground=[("active", C_TEXT)])
        style.configure("Dark.Horizontal.TProgressbar",
                        troughcolor=C_INPUT, background=C_ACCENT,
                        bordercolor=C_CARD, lightcolor=C_ACCENT,
                        darkcolor=C_ACCENT, thickness=8)
        self.root.option_add("*TCombobox*Listbox.background", C_CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", C_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", C_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.font", "Microsoft YaHei UI 10")

    # ---------- UI 工具 ----------
    def _refresh_summaries(self):
        self.sec_fmt.set_summary("%s · %s" % (
            self.var_layout.get().split("  ")[0],
            self.var_container.get().split("（")[0]))
        enc = "GPU" if self.var_encoder.get() == ENCODERS[0][0] else "CPU"
        self.sec_enc.set_summary("%s · %s · %s" % (
            enc, self.var_qp.get().split("（")[0], self.var_speed.get()))
        self.sec_aud.set_summary("%s · %s" % (
            self.var_track.get().split("（")[0],
            self.var_audio.get().split("（")[0]))
        self.sec_adv.set_summary("GOP %s%s" % (
            self.var_gop.get(), " · 完成后打开" if self.var_open.get() else ""))

    def _card(self, parent, title, expand=False):
        card = tk.Frame(parent, bg=C_CARD, highlightthickness=1,
                        highlightbackground=C_BORDER)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=3)
        body = tk.Frame(card, bg=C_CARD)
        body.pack(fill="x", padx=14, pady=10)
        return body

    def _combo(self, parent, var, values, width, command=None):
        cb = ttk.Combobox(parent, textvariable=var, values=values, width=width,
                          style="Dark.TCombobox", state="readonly",
                          font=F_LABEL)
        if command:
            def on_sel(_=None):
                command(var.get())
            cb.bind("<<ComboboxSelected>>", on_sel)
        return cb

    def _file_row(self, parent, label, var, browse_cmd, hint=""):
        row = tk.Frame(parent, bg=C_CARD)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=C_CARD, fg=C_TEXT, font=F_LABEL,
                 width=8, anchor="w").pack(side="left")
        tk.Entry(row, textvariable=var, bg=C_INPUT, fg=C_TEXT, font=F_LABEL,
                 relief="flat", insertbackground=C_TEXT,
                 highlightthickness=1, highlightbackground=C_BORDER,
                 highlightcolor=C_ACCENT
                 ).pack(side="left", fill="x", expand=True, padx=(6, 8), ipady=4)
        tk.Button(row, text="浏览", command=browse_cmd, width=6,
                  bg=C_BTN2, fg=C_TEXT, activebackground=C_BTN2_H,
                  activeforeground=C_TEXT, font=F_LABEL, relief="flat", bd=0,
                  cursor="hand2", padx=8, pady=3).pack(side="left")
        if hint:
            tk.Label(parent, text=hint, bg=C_CARD, fg=C_FAINT, font=F_SMALL,
                     anchor="w").pack(fill="x", padx=(76, 0), pady=(0, 2))

    def _on_rc_change(self, value):
        is_cqp = value == RC_MODES[0][0]
        self.cmb_qp.configure(state="readonly" if is_cqp else "disabled")
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
        cfg = {"left": self.var_left.get(), "right": self.var_right.get(),
               "out": self.var_out.get(), "layout": self.var_layout.get(),
               "container": self.var_container.get(), "encoder": self.var_encoder.get(),
               "speed": self.var_speed.get(), "rc": self.var_rc.get(),
               "qp": self.var_qp.get(), "bitrate": self.var_bitrate.get(),
               "audio": self.var_audio.get(), "gop": self.var_gop.get(),
               "open_after": self.var_open.get()}
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 文件选择与配对 ----------
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
        target_path = os.path.join(d, target)
        if is_left and not self.var_right.get().strip():
            self.var_right.set(target_path)
            self.logline("已自动配对右眼文件：%s" % target)
        elif not is_left and not self.var_left.get().strip():
            self.var_left.set(target_path)
            self.logline("已自动配对左眼文件：%s" % target)

    def pick_left(self):
        p = filedialog.askopenfilename(
            title="选择左眼视频流（BDMV\\STREAM 内的主文件，如 00000.m2ts）",
            filetypes=[("蓝光视频流", "*.m2ts"), ("所有文件", "*.*")])
        if p:
            self.var_left.set(p)
            if not self.var_out.get():
                base = os.path.splitext(os.path.basename(p))[0]
                self.var_out.set(os.path.join(os.path.dirname(p), "..", "..",
                                              "..", "sbs_" + base + ".mkv"))
            self._auto_pair(p, is_left=True)
            self._refresh_tracks(p)

    def pick_right(self):
        p = filedialog.askopenfilename(
            title="选择右眼视频流（同目录的另一条流，如 00001.m2ts）",
            filetypes=[("蓝光视频流", "*.m2ts"), ("所有文件", "*.*")])
        if p:
            self.var_right.set(p)
            self._auto_pair(p, is_left=False)

    def _refresh_tracks(self, m2ts):
        def work():
            tracks = probe_audio_tracks(m2ts)
            def apply():
                if not tracks:
                    return
                self.audio_tracks = tracks
                labels = ["自动（英语优先，最高声道）"] + [t[1] for t in tracks]
                self.cmb_track.configure(values=labels)
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
        self.pb["value"] = max(0.0, min(pct, 100.0))
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
        left = self.var_left.get().strip()
        right = self.var_right.get().strip()
        out = self.var_out.get().strip()
        if not left or not os.path.exists(left):
            messagebox.showerror(APP_TITLE, "请选择有效的左眼视频流文件（.m2ts）")
            return
        if not right or not os.path.exists(right):
            messagebox.showerror(APP_TITLE, "请选择有效的右眼视频流文件（.m2ts）")
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
        self.logline("开始任务：%s" % os.path.basename(left))
        self.logline("布局 %s · 容器 %s · 编码器 %s" % (
            self.var_layout.get().split("  ")[0], container.upper(),
            "GPU" if self.var_encoder.get() == ENCODERS[0][0] else "CPU"))
        self.job = ConvertJob(
            left, right, out,
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
            title="按顺序选择要拼接的视频（第一段、第二段...）",
            filetypes=[("视频文件", "*.mkv *.mp4"), ("所有文件", "*.*")])
        if not files or len(files) < 2:
            return
        out = filedialog.asksaveasfilename(
            title="保存合并后的文件", defaultextension=".mkv",
            filetypes=[("Matroska 视频", "*.mkv")])
        if not out:
            return
        self.logline("开始拼接 %d 个文件" % len(files))
        self.lbl_stage.configure(text="拼接中")
        concat_files(list(files), out,
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
    root = tk.Tk()
    App(root)
    if "--selftest" in args:
        root.after(600, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
