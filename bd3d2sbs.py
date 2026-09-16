# -*- coding: utf-8 -*-
"""
BD3D -> SBS 3D 视频转换器
把 3D 蓝光（MVC 编码）转成左右并排（Full SBS 3840x1080）的 HEVC 视频。
流程：tsMuxeR 解流 -> FRIMSource 解码 MVC -> ffmpeg 合成 SBS 并用 AMD GPU 编码

用法：
  GUI  : python bd3d2sbs.py
  CLI  : python bd3d2sbs.py --cli --mpls "xxx.mpls" --out "xxx.mkv"
         [--frames N] [--quality 0-2] [--noaudio] [--skipdemux]
"""
import os
import re
import sys
import json
import time
import threading
import subprocess
from tkinter import filedialog, messagebox

import customtkinter as ctk

APP_TITLE = "BD3D → SBS 转换器"
APP_VERSION = "v1.1"

# ---- 界面配色（深色专业风格）----
C_BG = "#17181c"
C_CARD = "#202127"
C_BORDER = "#2e3038"
C_INPUT = "#2a2c34"
C_TEXT = "#e8e9ed"
C_DIM = "#9a9ca8"
C_ACCENT = "#3574f0"
C_ACCENT_H = "#2b5fd0"
C_BTN2 = "#33363e"
C_BTN2_H = "#3d4149"
C_LOG_BG = "#121317"

F_TITLE = ("Microsoft YaHei UI", 17, "bold")
F_CARDT = ("Microsoft YaHei UI", 12, "bold")
F_LABEL = ("Microsoft YaHei UI", 12)
F_BTN = ("Microsoft YaHei UI", 12, "bold")
F_BIG = ("Microsoft YaHei UI", 24, "bold")
F_STAT = ("Microsoft YaHei UI", 12)
F_MONO = ("Consolas", 10)

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：资源在 _MEIPASS，配置写在 exe 旁边
    _BIN_BASE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    _CFG_BASE = os.path.dirname(sys.executable)
else:
    _BIN_BASE = _CFG_BASE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(_BIN_BASE, "bin")
FFMPEG = os.path.join(BIN_DIR, "ffmpeg.exe")
FFPROBE = os.path.join(BIN_DIR, "ffprobe.exe")
TSMUXER = os.path.join(BIN_DIR, "tsMuxeR.exe")
FRIMSOURCE = os.path.join(BIN_DIR, "FRIMSource.dll")
CONFIG_PATH = os.path.join(_CFG_BASE, "config.json")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

QUALITY_PRESETS = [
    ("高画质（约 28 Mbps）", 20, 22),
    ("标准（约 21 Mbps）", 22, 24),
    ("压缩（约 16 Mbps）", 24, 26),
]

DEMUX_WEIGHT = 3.0    # 解流阶段占总进度百分比
VIDEO_WEIGHT = 92.0   # 视频编码阶段
MUX_WEIGHT = 5.0      # 混流阶段


class Cancelled(Exception):
    pass


def popen_hidden(cmd):
    return subprocess.Popen(
        cmd, creationflags=CREATE_NO_WINDOW,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", bufsize=1)


class ConvertJob(threading.Thread):
    """单个片段的完整转换任务（解流 -> 视频编码 -> 混流）"""

    def __init__(self, mpls, out_mkv, qp_i, qp_p, max_frames=0, no_audio=False,
                 skip_demux=False,
                 on_log=None, on_progress=None, on_done=None, on_error=None):
        super().__init__(daemon=True)
        self.mpls = mpls
        self.out_mkv = out_mkv
        self.qp_i = qp_i
        self.qp_p = qp_p
        self.max_frames = max_frames
        self.no_audio = no_audio
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
            out_dir = os.path.dirname(os.path.abspath(self.out_mkv))
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
                self._log("解流完成：%d 帧，开始视频编码（FRIMSource 解码 + GPU 编码）" % nframes)
            audio_src, audio_idx = (None, None) if self.no_audio else self._probe_audio(name)
            if audio_src:
                self._log("音轨：%s（音频流 #%d）" % (os.path.basename(audio_src), audio_idx))
            else:
                self._log("未找到音轨输入，将输出无音轨视频")
            self._encode(base, dep, nframes, audio_src, audio_idx)
            self._check()
            self._log("完成：%s" % self.out_mkv)
            self.on_progress("done", 100.0, "全部完成")
            self.on_done(self.out_mkv)
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

    # ---------- 音轨探测 ----------
    def _probe_audio(self, name):
        cand = self._find_m2ts()
        if not cand:
            return None, None
        try:
            p = subprocess.run(
                [FFPROBE, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index,channels:stream_tags=language",
                 "-of", "json", cand],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", creationflags=CREATE_NO_WINDOW)
            streams = json.loads(p.stdout).get("streams", [])
        except Exception:
            return None, None
        if not streams:
            return None, None
        best_pos, best_score = 0, (-1, -1)
        for pos, s in enumerate(streams):
            lang = (s.get("tags") or {}).get("language", "")
            score = (1 if lang == "eng" else 0, int(s.get("channels") or 0))
            if score > best_score:
                best_pos, best_score = pos, score
        return cand, best_pos

    # ---------- 阶段 2：编码 ----------
    def _encode(self, base, dep, nframes, audio_src, audio_idx):
        avs_path = os.path.join(self.workdir, "decode.avs")
        avs = ('LoadPlugin("%s")\n'
               'interleaved = FRIMSource("mvc", "%s", "%s", layout="alt", '
               'num_frames=%d, cache=2, platform="sw")\n'
               'left  = SelectEven(interleaved)\n'
               'right = SelectOdd(interleaved)\n'
               'StackHorizontal(left, right)\n' % (FRIMSOURCE, base, dep, nframes))
        total = nframes
        if self.max_frames:
            total = min(nframes, self.max_frames)
            avs += "Trim(0, %d)\n" % (total - 1)
        with open(avs_path, "w", encoding="utf-8") as f:
            f.write(avs)

        # ---- 阶段 2A：编码纯视频（音频留到混流阶段，避免音频超前撑爆缓冲）----
        tmp_video = os.path.join(self.workdir, "video_only.mkv")
        cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
               "-i", avs_path, "-an",
               "-c:v", "hevc_amf", "-usage", "transcoding", "-quality", "quality",
               "-rc", "cqp", "-qp_i", str(self.qp_i), "-qp_p", str(self.qp_p),
               "-g", "96", "-color_primaries", "bt709", "-color_trc", "bt709",
               "-colorspace", "bt709", "-color_range", "tv"]
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
        # 视频编码完成，清理解流文件释放磁盘空间
        for f in (base, dep):
            try:
                if os.path.exists(f):
                    os.remove(f)
            except OSError:
                pass

        # ---- 阶段 2B：混流（视频 copy + 音轨），纯 I/O 操作不会缓冲溢出 ----
        if not audio_src:
            if os.path.exists(self.out_mkv):
                os.remove(self.out_mkv)
            os.replace(tmp_video, self.out_mkv)
            return
        dur = total * 1001.0 / 24000.0 + 0.2
        cmd = [FFMPEG, "-hide_banner", "-y", "-nostats", "-progress", "pipe:1",
               "-i", tmp_video, "-t", "%.3f" % dur, "-i", audio_src,
               "-map", "0:v", "-map", "1:a:%d" % audio_idx, "-map", "1:a:%d" % audio_idx,
               "-c", "copy", "-c:a:1", "aac", "-b:a:1", "512k", "-ac:a:1", "6",
               "-metadata:s:a:0", "language=eng",
               "-metadata:s:a:0", "title=Original",
               "-metadata:s:a:1", "language=eng",
               "-metadata:s:a:1", "title=AAC 5.1",
               "-f", "matroska", self.out_mkv]

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
        if not os.path.exists(self.out_mkv) or os.path.getsize(self.out_mkv) < 1024 * 1024:
            raise RuntimeError("输出文件异常，请查看日志")


def fmt_time(seconds):
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return "%d 小时 %d 分" % (seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "%d 分 %d 秒" % (seconds // 60, seconds % 60)
    return "%d 秒" % seconds


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
class App:
    def __init__(self, root: "ctk.CTk"):
        self.root = root
        self.job = None
        self.cfg = self._load_cfg()

        root.title(APP_TITLE)
        root.geometry("940x730")
        root.minsize(880, 660)
        root.configure(fg_color=C_BG)

        # ---------- 顶栏 ----------
        top = ctk.CTkFrame(root, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 2))
        ctk.CTkLabel(top, text=APP_TITLE, font=F_TITLE, text_color=C_TEXT).pack(side="left")
        ctk.CTkLabel(top, text=APP_VERSION, font=("Microsoft YaHei UI", 11),
                     text_color=C_DIM).pack(side="left", padx=(8, 0), pady=(5, 0))
        ctk.CTkLabel(top, text="3D 蓝光 → 左右并排 HEVC · GPU 硬件加速",
                     font=("Microsoft YaHei UI", 11), text_color=C_DIM
                     ).pack(side="right", pady=(5, 0))

        # ---------- 源与输出 ----------
        body = self._card(root, "源与输出")
        self.var_mpls = ctk.StringVar(value=self.cfg.get("mpls", ""))
        self._file_row(body, "源文件", self.var_mpls, self.pick_mpls,
                       hint="BDMV\\PLAYLIST 下的 .mpls")
        self.var_out = ctk.StringVar(value=self.cfg.get("out", ""))
        self._file_row(body, "输出到", self.var_out, self.pick_out,
                       hint="建议输出到剩余空间 ≥ 45 GB 的磁盘")

        # ---------- 输出参数 ----------
        body = self._card(root, "输出参数")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=3)
        ctk.CTkLabel(row, text="画质", width=56, anchor="w", font=F_LABEL,
                     text_color=C_TEXT).pack(side="left")
        self.var_q = ctk.StringVar(value=self.cfg.get("quality", QUALITY_PRESETS[0][0]))
        ctk.CTkOptionMenu(
            row, variable=self.var_q, values=[q[0] for q in QUALITY_PRESETS],
            width=190, fg_color=C_INPUT, button_color=C_INPUT,
            button_hover_color=C_BTN2_H, text_color=C_TEXT,
            dropdown_fg_color=C_CARD, dropdown_text_color=C_TEXT,
            dropdown_hover_color=C_BTN2_H, corner_radius=6, font=F_LABEL
        ).pack(side="left", padx=(8, 20))
        self.var_noaudio = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="仅视频", variable=self.var_noaudio,
                        fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color=C_DIM,
                        font=F_LABEL, corner_radius=4, width=20, height=20,
                        checkbox_width=20, checkbox_height=20).pack(side="left")
        ctk.CTkLabel(row, text="限制帧数（调试）", font=F_LABEL, text_color=C_DIM
                     ).pack(side="left", padx=(24, 8))
        self.var_frames = ctk.StringVar(value="")
        ctk.CTkEntry(row, textvariable=self.var_frames, width=80, fg_color=C_INPUT,
                     border_color=C_BORDER, text_color=C_TEXT, corner_radius=6
                     ).pack(side="left")

        # ---------- 操作按钮 ----------
        btns = ctk.CTkFrame(root, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(10, 4))
        self.btn_start = ctk.CTkButton(
            btns, text="开始转换", command=self.start, width=130, height=36,
            fg_color=C_ACCENT, hover_color=C_ACCENT_H, text_color="#ffffff",
            font=F_BTN, corner_radius=8)
        self.btn_start.pack(side="left")
        self.btn_cancel = ctk.CTkButton(
            btns, text="取消", command=self.cancel, width=90, height=36,
            fg_color=C_BTN2, hover_color=C_BTN2_H, text_color=C_TEXT,
            font=F_LABEL, corner_radius=8, state="disabled")
        self.btn_cancel.pack(side="left", padx=10)
        ctk.CTkButton(
            btns, text="无损拼接 MKV", command=self.concat, width=130, height=36,
            fg_color="transparent", hover_color=C_BTN2, text_color=C_DIM,
            border_width=1, border_color=C_BORDER, font=F_LABEL, corner_radius=8
        ).pack(side="right")

        # ---------- 进度 ----------
        body = self._card(root, "进度")
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")
        self.lbl_pct = ctk.CTkLabel(head, text="0.0%", font=F_BIG, text_color=C_TEXT)
        self.lbl_pct.pack(side="left")
        self.lbl_stage = ctk.CTkLabel(head, text="就绪", font=F_STAT, text_color=C_DIM)
        self.lbl_stage.pack(side="left", padx=(16, 0), pady=(10, 0))
        self.pb = ctk.CTkProgressBar(body, height=10, progress_color=C_ACCENT,
                                     fg_color=C_INPUT, corner_radius=5)
        self.pb.set(0)
        self.pb.pack(fill="x", pady=(8, 4))
        self.lbl_stat = ctk.CTkLabel(body, text=" ", font=F_STAT, text_color=C_DIM,
                                     anchor="w")
        self.lbl_stat.pack(fill="x")

        # ---------- 日志 ----------
        body = self._card(root, "日志", expand=True)
        self.log = ctk.CTkTextbox(body, height=170, font=F_MONO, fg_color=C_LOG_BG,
                                  text_color="#c8cad2", corner_radius=6,
                                  border_width=1, border_color=C_BORDER,
                                  scrollbar_button_color="#3a3d46")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

        self.logline("就绪。选择 .mpls 源文件与输出路径后点击「开始转换」。")

    # ---------- UI 工具 ----------
    def _card(self, parent, title, expand=False):
        card = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=10,
                            border_width=1, border_color=C_BORDER)
        card.pack(fill="both" if expand else "x", expand=expand,
                  padx=20, pady=6)
        ctk.CTkLabel(card, text=title, font=F_CARDT, text_color=C_DIM
                     ).pack(anchor="w", padx=14, pady=(10, 4))
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="both" if expand else "x", expand=expand,
                  padx=14, pady=(0, 12))
        return body

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
            ctk.CTkLabel(parent, text=hint, font=("Microsoft YaHei UI", 10),
                         text_color="#6b6d78", anchor="w"
                         ).pack(fill="x", padx=(64, 0), pady=(0, 2))

    # ---------- 配置 ----------
    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        cfg = {"mpls": self.var_mpls.get(), "out": self.var_out.get(),
               "quality": self.var_q.get()}
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

    def pick_out(self):
        p = filedialog.asksaveasfilename(
            title="保存输出文件", defaultextension=".mkv",
            filetypes=[("Matroska 视频", "*.mkv")])
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
        qname = self.var_q.get()
        qp_i, qp_p = next(((a, b) for n, a, b in QUALITY_PRESETS if n == qname),
                          (22, 24))
        try:
            max_frames = int(self.var_frames.get()) if self.var_frames.get().strip() else 0
        except ValueError:
            max_frames = 0
        self._save_cfg()
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.lbl_stage.configure(text="准备中")
        self.logline("开始任务：%s" % os.path.basename(mpls))
        self.logline("输出：%s" % out)
        self.job = ConvertJob(
            mpls, out, qp_i, qp_p, max_frames, self.var_noaudio.get(),
            on_log=lambda s: self.root.after(0, self.logline, s),
            on_progress=lambda st, pct, info: self.root.after(
                0, self._on_stage, pct, info),
            on_done=lambda o: self.root.after(0, self.done, o),
            on_error=lambda e: self.root.after(0, self.fail, e))
        self.job.start()

    def _on_stage(self, pct, info):
        self._set_progress(pct, info)
        if self.job:
            stages = {"demux": "解流", "encode": "视频编码", "mux": "混流封装"}
        self.lbl_stage.configure(text="处理中")

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
        qidx = int(get("--quality", "1") or 1)
        qp_i, qp_p = QUALITY_PRESETS[max(0, min(qidx, len(QUALITY_PRESETS) - 1))][1:]
        if not mpls or not out:
            print("用法: python bd3d2sbs.py --cli --mpls x.mpls --out x.mkv "
                  "[--frames N] [--quality 0-2] [--noaudio] [--skipdemux]")
            return 1
        job = ConvertJob(mpls, out, qp_i, qp_p, frames, noaudio, skipdemux,
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
