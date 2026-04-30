# One-click PowerShell installer for hotkey-controlled-whisper-streaming.
# Downloads the v0.1.0 GitHub release source zip + the prebuilt
# streaming_dictation.exe artifact, extracts to %USERPROFILE%\<repo>,
# pip-installs requirements, and creates a Start Menu folder with
# Start / Stop / Open Folder / Uninstall shortcuts.

[CmdletBinding()]
param(
    [string]$ReleaseTag = "v0.1.0",
    [string]$InstallParentDirectory = $env:USERPROFILE
)

$ErrorActionPreference = "Stop"

$gitHubRepoOwnerAndName = "RandyHaylor/hotkey-controlled-whisper-streaming"
$installFolderName = "hotkey-controlled-whisper-streaming"
$installFullPath = Join-Path $InstallParentDirectory $installFolderName

Write-Host "==> Installing $gitHubRepoOwnerAndName $ReleaseTag to: $installFullPath"

# 1. Verify Python is available.
$pythonExecutable = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $pythonExecutable) {
    Write-Host ""
    Write-Host "==========================================================" -ForegroundColor Red
    Write-Host " ERROR: Python was not found on PATH." -ForegroundColor Red
    Write-Host "==========================================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "This installer needs Python 3.11 or newer. Please:"
    Write-Host ""
    Write-Host "  1. Download Python from https://www.python.org/downloads/windows/"
    Write-Host "  2. Run the installer. On the first screen, MAKE SURE the box"
    Write-Host "     'Add python.exe to PATH' is CHECKED before clicking Install Now."
    Write-Host "  3. After Python finishes installing, OPEN A NEW terminal/PowerShell"
    Write-Host "     window so PATH is refreshed."
    Write-Host "  4. Re-run this installer."
    Write-Host ""
    Write-Host "Press any key to exit..." -ForegroundColor Yellow
    [void][System.Console]::ReadKey($true)
    exit 1
}
Write-Host "    Python: $($pythonExecutable.Source)"

# 2. Create install directory (idempotent — overwrite contents on reinstall).
if (Test-Path $installFullPath) {
    Write-Host "    Removing previous install at $installFullPath"
    Remove-Item -Path $installFullPath -Recurse -Force
}
New-Item -Path $installFullPath -ItemType Directory -Force | Out-Null

# 3. Download release source zipball (GitHub auto-generates one per release).
$sourceZipDownloadUrl = "https://github.com/$gitHubRepoOwnerAndName/archive/refs/tags/$ReleaseTag.zip"
$sourceZipDownloadPath = Join-Path $env:TEMP "$installFolderName-source.zip"
Write-Host "==> Downloading source: $sourceZipDownloadUrl"
Invoke-WebRequest -Uri $sourceZipDownloadUrl -OutFile $sourceZipDownloadPath -UseBasicParsing

# 4. Extract source zip — it extracts as <repo>-<tag-without-v>/, so we
#    have to copy contents up one level.
$temporaryExtractDirectory = Join-Path $env:TEMP "$installFolderName-extract"
if (Test-Path $temporaryExtractDirectory) {
    Remove-Item -Path $temporaryExtractDirectory -Recurse -Force
}
Expand-Archive -Path $sourceZipDownloadPath -DestinationPath $temporaryExtractDirectory -Force
$extractedTopLevelFolder = Get-ChildItem -Path $temporaryExtractDirectory -Directory | Select-Object -First 1
if (-not $extractedTopLevelFolder) {
    Write-Error "Source zip extraction produced no top-level folder."
    exit 1
}
Write-Host "==> Copying source files to $installFullPath"
Copy-Item -Path (Join-Path $extractedTopLevelFolder.FullName "*") -Destination $installFullPath -Recurse -Force

# 5. Download prebuilt streaming_dictation.exe asset and place it in
#    <install>\windows\.
$exeAssetDownloadUrl = "https://github.com/$gitHubRepoOwnerAndName/releases/download/$ReleaseTag/streaming_dictation.exe"
$windowsSubfolderInsideInstall = Join-Path $installFullPath "windows"
New-Item -Path $windowsSubfolderInsideInstall -ItemType Directory -Force | Out-Null
$exeDestinationPath = Join-Path $windowsSubfolderInsideInstall "streaming_dictation.exe"
Write-Host "==> Downloading streaming_dictation.exe: $exeAssetDownloadUrl"
Invoke-WebRequest -Uri $exeAssetDownloadUrl -OutFile $exeDestinationPath -UseBasicParsing

# 6. Initialize whisper_streaming submodule contents. The source zipball
#    DOES NOT contain submodule files; we need to fetch them separately.
$whisperStreamingDirectory = Join-Path $installFullPath "whisper_streaming"
if (-not (Test-Path (Join-Path $whisperStreamingDirectory "whisper_online_server.py"))) {
    Write-Host "==> Fetching whisper_streaming submodule contents"
    $whisperStreamingZipDownloadUrl = "https://github.com/ufal/whisper_streaming/archive/refs/heads/main.zip"
    $whisperStreamingZipPath = Join-Path $env:TEMP "whisper_streaming-main.zip"
    Invoke-WebRequest -Uri $whisperStreamingZipDownloadUrl -OutFile $whisperStreamingZipPath -UseBasicParsing
    $whisperStreamingExtractDirectory = Join-Path $env:TEMP "whisper_streaming-extract"
    if (Test-Path $whisperStreamingExtractDirectory) {
        Remove-Item -Path $whisperStreamingExtractDirectory -Recurse -Force
    }
    Expand-Archive -Path $whisperStreamingZipPath -DestinationPath $whisperStreamingExtractDirectory -Force
    $whisperStreamingExtractedTop = Get-ChildItem -Path $whisperStreamingExtractDirectory -Directory | Select-Object -First 1
    Remove-Item -Path $whisperStreamingDirectory -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item -Path $whisperStreamingExtractedTop.FullName -Destination $whisperStreamingDirectory -Recurse -Force
}

# 7. Install Python dependencies.
$requirementsTextPath = Join-Path $installFullPath "requirements.txt"
Write-Host "==> Installing Python dependencies"
& python -m pip install --upgrade pip
& python -m pip install -r $requirementsTextPath
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed (exit $LASTEXITCODE)"
    exit $LASTEXITCODE
}

# 8. Create Start Menu folder with shortcuts.
$startMenuFolder = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\HotkeyWhisperStreaming"
if (Test-Path $startMenuFolder) {
    Remove-Item -Path $startMenuFolder -Recurse -Force
}
New-Item -Path $startMenuFolder -ItemType Directory -Force | Out-Null

$wScriptShellComObject = New-Object -ComObject WScript.Shell

function New-Shortcut(
    [string]$ShortcutFilePath,
    [string]$TargetExecutablePath,
    [string]$ArgumentString,
    [string]$WorkingDirectoryPath,
    [string]$IconFilePath
) {
    $shortcutObject = $wScriptShellComObject.CreateShortcut($ShortcutFilePath)
    $shortcutObject.TargetPath = $TargetExecutablePath
    $shortcutObject.Arguments = $ArgumentString
    $shortcutObject.WorkingDirectory = $WorkingDirectoryPath
    if ($IconFilePath) {
        $shortcutObject.IconLocation = $IconFilePath
    }
    $shortcutObject.Save()
}

$startVoiceToTextBatPath = Join-Path $installFullPath "windows\installer\start_voice_to_text.bat"
$stopVoiceToTextBatPath = Join-Path $installFullPath "windows\installer\stop_voice_to_text.bat"
$uninstallBatPath = Join-Path $installFullPath "windows\installer\uninstall.bat"

New-Shortcut `
    -ShortcutFilePath (Join-Path $startMenuFolder "Start Voice-to-Text.lnk") `
    -TargetExecutablePath $startVoiceToTextBatPath `
    -ArgumentString "" `
    -WorkingDirectoryPath $installFullPath `
    -IconFilePath ""

New-Shortcut `
    -ShortcutFilePath (Join-Path $startMenuFolder "Stop Voice-to-Text.lnk") `
    -TargetExecutablePath $stopVoiceToTextBatPath `
    -ArgumentString "" `
    -WorkingDirectoryPath $installFullPath `
    -IconFilePath ""

New-Shortcut `
    -ShortcutFilePath (Join-Path $startMenuFolder "Open Install Folder.lnk") `
    -TargetExecutablePath "explorer.exe" `
    -ArgumentString "`"$installFullPath`"" `
    -WorkingDirectoryPath $installFullPath `
    -IconFilePath ""

New-Shortcut `
    -ShortcutFilePath (Join-Path $startMenuFolder "Uninstall.lnk") `
    -TargetExecutablePath $uninstallBatPath `
    -ArgumentString "" `
    -WorkingDirectoryPath $installFullPath `
    -IconFilePath ""

Write-Host ""
Write-Host "========================================================="
Write-Host "Install complete."
Write-Host "Install folder:  $installFullPath"
Write-Host "Start Menu folder: HotkeyWhisperStreaming"
Write-Host ""
Write-Host "To start dictation: Start Menu -> HotkeyWhisperStreaming -> Start Voice-to-Text"
Write-Host "Hotkeys:           Ctrl+F12 = start dictation,  Shift+F12 = stop"
Write-Host "========================================================="
