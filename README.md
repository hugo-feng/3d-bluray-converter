# BD3D 转换器

把 **3D 蓝光原盘** 转换成 **SBS / TAB 立体视频**（HEVC，AMD GPU 硬件编码），
供 AR 眼镜 / VR 头显 / 3D 电视观看。

## 下载

**免安装便携版**（内置全部工具链，解压即用）：

https://github.com/hugo-feng/bd3d2sbs/releases/download/v1.7.0/BD3D2SBS_v1.7.0_portable.zip

> 解压到**纯英文路径**，双击 `BD3D2SBS.exe` 运行。需要 Windows 10/11 x64。

## 源文件说明

3D 蓝光原盘在 `BDMV\STREAM` 目录下有两个视频流文件：

| 文件 | 内容 |
|---|---|
| `00000.m2ts` | 左眼画面（AVC 基础视图，音轨也在这个文件里） |
| `00001.m2ts` | 右眼画面（MVC 依赖视图） |

软件需要**分别选择这两个文件**——选择左眼文件后会自动配对同目录的右眼文件。

## 使用方法

1. 双击 `BD3D2SBS.exe` 打开软件（Win11 Fluent 深色界面）
2. 「左眼文件」→ 浏览选择 `BDMV\STREAM\00000.m2ts`（右眼会自动配对）
3. 「输出到」→ 选择保存路径（所在磁盘需 ≥ 45 GB 空闲空间，**建议用纯英文路径**）
4. 按需展开「输出格式 / 编码设置 / 音频 / 高级」调整参数（收起时右侧显示当前配置摘要）
5. 点击「开始转换」，进度区显示总进度、帧率与预计剩余时间
6. 若影片分两张碟，分别转换后用「无损拼接（完整片）」合并

**关于中文路径**：软件所在的目录必须全英文（否则内置解码器无法加载）。
如果输出路径含中文字符，软件会自动把中间文件放到同盘根目录（日志中有提示）；
建议输出路径也使用纯英文，最稳定。

**播放**：把成品拷到手机，用 **VLC** 全屏播放，眼镜切到「3D 左右」模式。
> 注意：部分手机自带播放器会缩放画面，导致眼镜端 3D 对齐异常，请用 VLC / MX Player
> （MX Player 需开启"使用外接显示器"）。

## 功能

- **4 种 3D 布局**：全宽 SBS 3840×1080（AR 眼镜推荐）/ 半宽 SBS 1920×1080 /
  全高 TAB 1920×2160 / 半高 TAB 1920×1080
- **编码器**：AMD GPU 硬编 HEVC（快，推荐，约为 CPU x265 的 10 倍） / CPU x265
- **质量模式**：恒定质量 CQP/CRF、目标平均码率、固定码率；4 档预设或自定义
- **编码速度**：质量优先 / 平衡 / 速度优先；可自定义关键帧间隔
- **音轨**：自动探测原盘全部音轨，可指定主音轨；输出模式支持
  原声 + AAC 兼容轨（推荐，手机也能放）/ 仅原声无损直通 / 仅 AAC / 无音轨
- **容器**：MKV（支持 DTS 原声）/ MP4（手机兼容性最好，自动处理音轨转码）
- 磁盘空间预检、完成后自动打开输出目录、参数自动记忆
- 深色专业界面、设置区折叠、窗口缩放流畅

## 环境要求

- Windows 10/11 x64
- AMD 显卡（RDNA 架构，支持 HEVC 硬件编码）
- Python 3.10+（仅源码运行需要；EXE 版免安装）

## 工具链

`bin/` 目录为内置离线工具链（已随 EXE 分发），如需从源码构建，运行：

```powershell
powershell -ExecutionPolicy Bypass -File download_tools.ps1
```

| 工具 | 用途 | 来源 |
|---|---|---|
| ffmpeg (BtbN build) | 编码 / 混流 | github.com/BtbN/FFmpeg-Builds |
| tsMuxeR | 蓝光 3D 解流 | github.com/justdan96/tsMuxer |
| AviSynth+ | 帧服务器 | github.com/AviSynth/AviSynthPlus |
| FRIMSource / libmfxsw | MVC (H.264 双视图) 解码 | BD3D2MK3D 发布包内置 |

## 命令行模式

```powershell
python bd3d2sbs.py --cli ^
  --left "G:\...\BDMV\STREAM\00000.m2ts" ^
  --right "G:\...\BDMV\STREAM\00001.m2ts" ^
  --out "G:\output\movie.mkv" ^
  [--layout full_sbs|half_sbs|full_tab|half_tab] ^
  [--container mkv|mp4] [--encoder gpu|cpu] ^
  [--quality 0-3] [--bitrate 20] [--frames N] [--noaudio] [--skipdemux]
```

## 打包 EXE

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --onedir --windowed --name BD3D2SBS ^
  --icon app.ico --add-data "app.ico;." bd3d2sbs.py
# 然后把 bin\ 复制到 dist\BD3D2SBS\bin\
```

## 工作原理

```
BD 3D 原盘（BDMV\STREAM）
  │  tsMuxeR 解出两路 ES
  ▼
left.264（左眼基础视图） + right.mvc（右眼依赖视图）
  │  AviSynth + FRIMSource 解码 MVC 双视图
  ▼
左右两路 1080p
  │  StackHorizontal / StackVertical + 可选缩放
  ▼
SBS / TAB 立体帧
  │  ffmpeg hevc_amf 硬件编码
  ▼
HEVC 视频（含原版音轨 + AAC 兼容轨）
```

## 版权声明

本工具仅供个人将自己购买的 3D 蓝光光碟转换为便于个人设备播放的格式，
请勿用于传播受版权保护的内容。
