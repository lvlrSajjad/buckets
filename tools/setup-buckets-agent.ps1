# One-time setup of the buckets agent on the Windows machine: lets buckets on the Mac run checks in a copy of the
# repo here, and run commands you send with `buckets win`. Run it once, in PowerShell as administrator, from the
# folder that holds this script, windows-agent.cjs and agent.token (made by `buckets win setup` on the Mac, which
# can put it straight into a folder shared from here: `buckets win setup --to /Volumes/<share>/buckets-setup`):
#
#   powershell -ExecutionPolicy Bypass -File .\setup-buckets-agent.ps1
#
# What it does: checks Node 22+ and Git for Windows (installs them with winget if missing), copies the agent to
# C:\buckets, opens port 8766 to this local network only, and starts the agent now and at every login. After this,
# buckets updates the agent itself; you shouldn't need to come back here.
param([int]$Port = 8766, [string]$Home_ = "C:\buckets")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Need($name, $test, $winget) {
  if (-not (& $test)) {
    Write-Host "$name is missing; installing it with winget…"
    winget install --silent --accept-source-agreements --accept-package-agreements -e --id $winget
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  }
}
Need "Node.js 22+" { try { [int]((node --version) -replace '^v(\d+).*', '$1') -ge 22 } catch { $false } } "OpenJS.NodeJS.LTS"
Need "Git for Windows" { Test-Path "C:\Program Files\Git\bin\bash.exe" } "Git.Git"
corepack enable 2>$null  # yarn and pnpm as the repos ask for them

New-Item -ItemType Directory -Force -Path $Home_, "$Home_\repos" | Out-Null
Copy-Item -Force "$here\windows-agent.cjs" "$Home_\windows-agent.cjs"
Copy-Item -Force "$here\agent.token" "$Home_\agent.token"
icacls "$Home_\agent.token" /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null  # only you can read it
Remove-Item -Force "$here\agent.token"  # the copy that came here (perhaps through a shared folder) goes

# The loop restarts the agent when it updates itself (or crashes).
$start = @"
while (`$true) {
  & node "$Home_\windows-agent.cjs" --root "$Home_\repos" --token-file "$Home_\agent.token" --port $Port *>> "$Home_\agent.log"
  Start-Sleep -Seconds 2
}
"@
Set-Content -Path "$Home_\start-buckets-agent.ps1" -Value $start

# This network only (the Mac), never public networks.
Get-NetFirewallRule -DisplayName "buckets agent" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "buckets agent" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow `
  -Profile Private,Domain -RemoteAddress LocalSubnet | Out-Null

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Home_\start-buckets-agent.ps1`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "buckets agent" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "buckets agent"

Start-Sleep -Seconds 3
$token = (Get-Content "$Home_\agent.token" -Raw).Trim()
try {
  $s = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/status" -Headers @{ Authorization = "Bearer $token" }
  Write-Host "buckets agent $($s.version) is running on port $Port. Back on the Mac: buckets win status"
} catch {
  Write-Host "The agent didn't answer yet; see $Home_\agent.log"
}
