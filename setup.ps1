# キッザニア福岡 館内音響自動化システム セットアップスクリプト
#
# 新しいPC(インターネット接続あり)でこのプロジェクトを動かすために必要な
# 環境を一括で整える。
#
# 実行方法(PowerShellで、このファイルがあるフォルダで実行):
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# 実施内容:
#   1. Python 3.12 のインストール確認(無ければ winget でインストール)
#   2. VLC メディアプレーヤーのインストール確認(無ければ winget でインストール)
#   3. 仮想環境(.venv)の作成とpipパッケージのインストール
#   4. デスクトップにショートカットを作成

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "=== キッザニア館内音響自動化システム セットアップ ===" -ForegroundColor Cyan

function Update-SessionPath {
    $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:PATH = "$machinePath;$userPath"
}

# ------------------------------------------------------------------
# 1. Python
# ------------------------------------------------------------------
$pythonOk = $false
try {
    # python.exe はインストールが無くてもWindows Storeの実行エイリアスとして
    # PATHに存在してしまい `python --version` の判定が不安定なため、
    # 実体の有無を直接確認する。
    $pythonExe = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python" -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonExe) {
        $pythonOk = $true
    }
} catch {}

if (-not $pythonOk) {
    Write-Host "[1/4] Pythonが見つからないため、wingetでインストールします..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
    Update-SessionPath
} else {
    Write-Host "[1/4] Pythonは既にインストールされています。" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 2. VLC
# ------------------------------------------------------------------
$vlcPath64 = "C:\Program Files\VideoLAN\VLC\vlc.exe"
$vlcPath32 = "C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"
if (-not (Test-Path $vlcPath64) -and -not (Test-Path $vlcPath32)) {
    Write-Host "[2/4] VLCが見つからないため、wingetでインストールします..." -ForegroundColor Yellow
    winget install --id VideoLAN.VLC -e --source winget --accept-package-agreements --accept-source-agreements
} else {
    Write-Host "[2/4] VLCは既にインストールされています。" -ForegroundColor Green
}

# ------------------------------------------------------------------
# 3. 仮想環境 + 依存パッケージ
# ------------------------------------------------------------------
Write-Host "[3/4] Python仮想環境と依存パッケージを準備します..." -ForegroundColor Yellow
Set-Location $ProjectRoot

if (-not (Test-Path "$ProjectRoot\.venv")) {
    python -m venv .venv
}

$venvPython = "$ProjectRoot\.venv\Scripts\python.exe"
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r "$ProjectRoot\requirements.txt"

# ------------------------------------------------------------------
# 4. デスクトップショートカット
# ------------------------------------------------------------------
Write-Host "[4/4] デスクトップにショートカットを作成します..." -ForegroundColor Yellow

$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop "キッザニア館内音響システム.lnk"
$pythonwPath = "$ProjectRoot\.venv\Scripts\pythonw.exe"

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwPath
$shortcut.Arguments = "-m src.kidzania_sound.main"
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.IconLocation = $pythonwPath
$shortcut.Description = "キッザニア福岡 館内音響自動化システム"
$shortcut.Save()

Write-Host ""
Write-Host "=== セットアップ完了 ===" -ForegroundColor Cyan
Write-Host "デスクトップの「キッザニア館内音響システム」から起動できます。"
Write-Host "media/ フォルダへの音源・動画配置と、config/ 配下の設定編集を忘れずに行ってください。"

