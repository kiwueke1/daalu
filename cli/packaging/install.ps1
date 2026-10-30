# Daalu CLI installer for Windows (PowerShell).
#
#   irm https://get.daalu.io/install.ps1 | iex
#
# Downloads the latest daalu.exe from GitHub Releases into
# %LOCALAPPDATA%\Programs\daalu and adds it to the user PATH.
$ErrorActionPreference = "Stop"

$repo = if ($env:DAALU_CLI_REPO) { $env:DAALU_CLI_REPO } else { "daalu-io/cli" }
$asset = "daalu-windows-x64.zip"
$url = "https://github.com/$repo/releases/latest/download/$asset"

$dest = Join-Path $env:LOCALAPPDATA "Programs\daalu"
New-Item -ItemType Directory -Force -Path $dest | Out-Null

$tmp = New-TemporaryFile
Write-Host "Downloading $asset…"
Invoke-WebRequest -Uri $url -OutFile $tmp.FullName
Expand-Archive -Path $tmp.FullName -DestinationPath $dest -Force
Remove-Item $tmp.FullName

# Add to the user PATH if missing.
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$dest*") {
  [Environment]::SetEnvironmentVariable("Path", "$userPath;$dest", "User")
  Write-Host "Added $dest to your PATH (restart your shell)."
}

Write-Host ""
Write-Host "Installed daalu to $dest\daalu.exe"
Write-Host "Next:  daalu login"
