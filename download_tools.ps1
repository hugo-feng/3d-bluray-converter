# 下载并部署 BD3D2SBS 所需工具链到 bin\ 目录
# 用法：powershell -ExecutionPolicy Bypass -File download_tools.ps1
$ErrorActionPreference = "Stop"
$bin = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Path $bin -Force | Out-Null
$tmp = Join-Path $env:TEMP "bd3d2sbs_tools"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

Write-Host "==> 下载 7zr (解压工具)" -ForegroundColor Cyan
curl.exe -L --progress-bar -o "$tmp\7zr.exe" "https://www.7-zip.org/a/7zr.exe"

Write-Host "==> 下载 ffmpeg (BtbN GPL build)" -ForegroundColor Cyan
curl.exe -L --progress-bar -o "$tmp\ffmpeg.zip" "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
Expand-Archive "$tmp\ffmpeg.zip" -DestinationPath "$tmp\ffmpeg" -Force
$ffbin = Get-ChildItem "$tmp\ffmpeg" -Recurse -Filter ffmpeg.exe | Select-Object -First 1
Copy-Item $ffbin.FullName "$bin\ffmpeg.exe" -Force
Copy-Item (Join-Path $ffbin.DirectoryName "ffprobe.exe") "$bin\ffprobe.exe" -Force

Write-Host "==> 下载 BD3D2MK3D 工具包 (含 tsMuxeR / FRIMSource / libmfxsw)" -ForegroundColor Cyan
curl.exe -L --progress-bar -o "$tmp\bd3d2mk3d.7z" "http://download.videohelp.com/BD3D2MK3D.7z"
& "$tmp\7zr.exe" x "$tmp\bd3d2mk3d.7z" -o"$tmp\bd3d2mk3d" -y | Out-Null
$ts = Get-ChildItem "$tmp\bd3d2mk3d" -Recurse -Filter tsMuxeR.exe | Select-Object -First 1
Copy-Item $ts.FullName "$bin\tsMuxeR.exe" -Force
$fr = Get-ChildItem "$tmp\bd3d2mk3d" -Recurse -Filter FRIMSource.dll | Where-Object { $_.FullName -match "plugins64bit" } | Select-Object -First 1
Copy-Item $fr.FullName "$bin\FRIMSource.dll" -Force
$mfx = Get-ChildItem "$tmp\bd3d2mk3d" -Recurse -Filter libmfxsw64.dll | Select-Object -First 1
Copy-Item $mfx.FullName "$bin\libmfxsw64.dll" -Force
foreach ($d in @("msvcp100.dll", "msvcr100.dll")) {
    $f = Get-ChildItem "$tmp\bd3d2mk3d" -Recurse -Filter $d | Select-Object -First 1
    if ($f) { Copy-Item $f.FullName "$bin\$d" -Force }
}

Write-Host "==> 下载 AviSynth+ (x64)" -ForegroundColor Cyan
curl.exe -L --progress-bar -o "$tmp\avs.7z" "https://github.com/AviSynth/AviSynthPlus/releases/download/v3.7.5/AviSynthPlus_3.7.5_20250420-filesonly.7z"
& "$tmp\7zr.exe" x "$tmp\avs.7z" -o"$tmp\avs" -y | Out-Null
$avs = Get-ChildItem "$tmp\avs" -Recurse -Filter AviSynth.dll | Where-Object { $_.FullName -match "x64" -and $_.FullName -notmatch "xp" } | Select-Object -First 1
Copy-Item $avs.FullName "$bin\AviSynth.dll" -Force

Write-Host ""
Write-Host "完成！工具链已部署到 $bin" -ForegroundColor Green
Get-ChildItem $bin | Select-Object Name, @{n = 'SizeMB'; e = { [math]::Round($_.Length / 1MB, 1) } } | Format-Table -AutoSize
