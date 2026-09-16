# BD3D → SBS 3D 视频转换器

把 **3D 蓝光原盘（MVC 编码）** 转换为 **左右并排（Full SBS 3840×1080）** 的 HEVC 视频，
可直接在 AR 眼镜 / VR 头显 / 3D 电视上以"左右格式"观看。

- 全程使用 **AMD GPU 硬件编码**（`hevc_amf`），速度约为 CPU x265 的 10 倍以上
- 内置完整离线工具链（ffmpeg + tsMuxeR + FRIMSource MVC 解码器 + AviSynth+）
- 图形界面带**分阶段进度条**（解流 / 视频编码 / 混流）、实时帧率与预计剩余时间
- 无损保留原始音轨（DTS-HD MA 等），并附加一条 AAC 5.1 兼容轨

## 使用方法

1. 首次使用：双击 `启动转换器.bat`（或运行打包好的 `BD3D2SBS.exe`）
2. 选择源文件：`BDMV\PLAYLIST\` 下的 `.mpls`
3. 选择输出 `.mkv` 路径（需要至少 45 GB 空闲空间）
4. 选择画质档位，点击「开始转换」
5. 转换完成后把 MKV 拷到手机，用 **VLC** 等播放器全屏播放，眼镜切到 3D 左右模式

> 注意：请使用 VLC / MX Player（开启"外接显示器"输出）等支持原生分辨率输出
> 的播放器。部分手机自带播放器会缩放画面，导致眼镜端 3D 对齐异常。

## 环境要求

- Windows 10/11 x64
- AMD 显卡（RDNA 架构，支持 HEVC 硬件编码）
- Python 3.10+（仅源码运行需要；EXE 版可直接运行）

## 工具链获取

`bin/` 目录不入库，运行以下命令自动下载（需要网络）：

```powershell
powershell -ExecutionPolicy Bypass -File download_tools.ps1
```

工具链来源（均为开源/免费发布）：

| 工具 | 用途 | 来源 |
|---|---|---|
| ffmpeg (BtbN build) | 编码 / 混流 | github.com/BtbN/FFmpeg-Builds |
| tsMuxeR | 蓝光 3D 解流 | github.com/justdan96/tsMuxer |
| AviSynth+ | 帧服务器 | github.com/AviSynth/AviSynthPlus |
| FRIMSource / libmfxsw | MVC (H.264 双视图) 解码 | BD3D2MK3D 发布包内置 |

## 打包 EXE

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name BD3D2SBS --add-data "bin;bin" bd3d2sbs.py
```

## 工作原理

```
BD 3D 原盘 (SSIF / MVC)
  │  tsMuxeR 解流
  ▼
基础视图 .264 (左眼) + 依赖视图 .mvc (右眼)
  │  AviSynth + FRIMSource 解码双视图
  ▼
左右两路 1080p
  │  StackHorizontal 拼接
  ▼
3840×1080 Full SBS
  │  ffmpeg hevc_amf 硬件编码
  ▼
HEVC MKV (含原版音轨 + AAC 兼容轨)
```

## 版权声明

本工具仅供个人将自己购买的 3D 蓝光光碟转换为便于个人设备播放的格式，
请勿用于传播受版权保护的内容。
