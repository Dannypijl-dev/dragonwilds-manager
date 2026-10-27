<#
.SYNOPSIS
  Install (or update) Dragonwilds Manager on Windows 10/11 or Windows Server as a Windows service.
.DESCRIPTION
  Downloads the latest dragonwilds-manager.exe from GitHub Releases (or uses -ExePath), puts it in
  C:\Program Files\DragonwildsManager, registers the "Dragonwilds Manager" service (automatic start),
  opens TCP 8080 in Windows Firewall for the panel and starts it. Data (servers, worlds, backups)
  goes to C:\ProgramData\DragonwildsManager. Run from an elevated PowerShell:

    Set-ExecutionPolicy -Scope Process Bypass -Force
    irm https://raw.githubusercontent.com/Consuelaaa/dragonwilds-manager/main/packaging/windows/install.ps1 | iex

  Game ports (UDP 7777, ...) are opened by the manager itself when a server starts.
.PARAMETER Uninstall
  Remove the service and the program. Data is kept.
#>
param(
  [string]$ExePath = "",
  [string]$Repo = "Consuelaaa/dragonwilds-manager",
  [int]$Port = 8080,
  [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Dir = Join-Path $env:ProgramFiles "DragonwildsManager"
$Exe = Join-Path $Dir "dragonwilds-manager.exe"
$Service = "DragonwildsManager"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Error "Run this in a PowerShell window started as Administrator."
}

if ($Uninstall) {
  if (Get-Service $Service -ErrorAction SilentlyContinue) {
    Stop-Service $Service -ErrorAction SilentlyContinue
    & $Exe service remove | Out-Null
  }
  Remove-NetFirewallRule -DisplayName "Dragonwilds Manager panel" -ErrorAction SilentlyContinue
  Remove-Item -Recurse -Force $Dir -ErrorAction SilentlyContinue
  Write-Host "Removed. Your data is still in $env:ProgramData\DragonwildsManager."
  exit 0
}

New-Item -ItemType Directory -Force $Dir | Out-Null
if ($ExePath) {
  Copy-Item $ExePath "$Exe.new" -Force
} else {
  Write-Host "Downloading the latest release from github.com/$Repo ..."
  $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "dragonwilds-manager-installer" }
  $asset = $release.assets | Where-Object { $_.name -eq "dragonwilds-manager.exe" } | Select-Object -First 1
  if (-not $asset) { Write-Error "No dragonwilds-manager.exe in the latest release of $Repo." }
  Invoke-WebRequest $asset.browser_download_url -OutFile "$Exe.new" -UseBasicParsing
}

$existing = Get-Service $Service -ErrorAction SilentlyContinue
if ($existing) {
  Write-Host "Stopping the running service (game servers are stopped cleanly; this can take a minute)..."
  Stop-Service $Service -ErrorAction SilentlyContinue
}
Move-Item "$Exe.new" $Exe -Force

if (-not $existing) {
  & $Exe service install | Out-Null
  Set-Service $Service -StartupType Automatic
  if ($Port -ne 8080) { [Environment]::SetEnvironmentVariable("DWM_PORT", "$Port", "Machine") }
}
if (-not (Get-NetFirewallRule -DisplayName "Dragonwilds Manager panel" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Dragonwilds Manager panel" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private,Domain | Out-Null
}
Start-Service $Service
$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "Dragonwilds Manager is running: http://${ip}:$Port  (also http://localhost:$Port on this machine)"
Write-Host "Open it in a browser to choose a password and create your first server."
Write-Host "To let friends join from the internet, forward the game port (UDP 7777) on your router to this PC."
