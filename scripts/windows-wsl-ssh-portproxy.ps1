# Run once per machine in Admin PowerShell:
#   powershell -ExecutionPolicy Bypass -File \\wsl$\Ubuntu\home\quicazan\e14\scripts\windows-wsl-ssh-portproxy.ps1
# Or from repo on Windows: powershell -ExecutionPolicy Bypass -File C:\path\to\e14\scripts\windows-wsl-ssh-portproxy.ps1

$ErrorActionPreference = "Stop"
$listenPort = 2222

$wslIp = ((wsl hostname -I) -as [string]).Trim() -split '\s+' | Select-Object -First 1
if (-not $wslIp) { throw "Could not read WSL IP (is WSL running?)" }
Write-Host "WSL IP: $wslIp"

netsh interface portproxy delete v4tov4 listenport=$listenPort listenaddress=0.0.0.0 2>$null | Out-Null
netsh interface portproxy add v4tov4 listenport=$listenPort listenaddress=0.0.0.0 connectport=22 connectaddress=$wslIp

Write-Host "`nPort proxy rules:"
netsh interface portproxy show all

$rule = Get-NetFirewallRule -DisplayName "WSL SSH Tailscale" -ErrorAction SilentlyContinue
if (-not $rule) {
  New-NetFirewallRule -DisplayName "WSL SSH Tailscale" -Direction Inbound -Protocol TCP -LocalPort $listenPort -Action Allow | Out-Null
  Write-Host "Firewall rule created."
} else {
  Write-Host "Firewall rule already present."
}

Write-Host "`nTest locally: ssh -p $listenPort $env:USERNAME@127.0.0.1"