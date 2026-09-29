# Add this Windows PC to Odysseus. Served by Odysseus at /enroll/<code>/install.ps1
# with the placeholders below filled in; run it in PowerShell with:
#
#     iwr -useb <odysseus>/enroll/<code>/install.ps1 | iex
#
# Run PowerShell as Administrator if you can: the firewall rule (tailnet only)
# and the SSH server that Ping uses need it. Everything else works without.
#
# What it does:
#   1. checks Tailscale is installed and signed in, and reads this PC's
#      tailnet address and name;
#   2. installs the desktop MCP server into %USERPROFILE%\odysseus-mcp with a
#      hidden launcher and an at-logon task (OdysseusDesktopMCP) in your
#      desktop session, the only place app control works; and the same for
#      DaVinci Resolve's server if Resolve is installed (OdysseusResolveMCP);
#   3. as Administrator: allows the ports from the tailnet only, enables the
#      OpenSSH server, and adds Odysseus's SSH key so Ping can restart things;
#   4. tells Odysseus what it found so it can add and connect the servers.
# Safe to re-run: it upgrades in place.
$ErrorActionPreference = 'Stop'
$Base   = '__ODYSSEUS_BASE__'
$Code   = '__ENROLL_CODE__'
$PubKey = '__ODYSSEUS_PUBKEY__'
$Dest   = Join-Path $env:USERPROFILE 'odysseus-mcp'
# Match on the key itself, not its comment, so a key added earlier under
# another comment is not added twice.
$KeyOnly = ($PubKey -split ' ')[0..1] -join ' '

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Unlock { if ($script:lock) { try { $script:lock.ReleaseMutex() } catch {} ; $script:lock = $null } }
function Die($m) { Write-Host "Error: $m" -ForegroundColor Red; Unlock; throw $m }

# One install at a time: copies started over SSH piled up on 2026-09-29, each
# rewriting the MCP server's file, and one left it locked ("can't open file
# ... desktop_mcp_server.py: [Errno 13] Permission denied"), so the server
# died at every start.
$lock = New-Object System.Threading.Mutex($false, 'Global\OdysseusInstall')
try { $got = $lock.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $got = $true }
if (-not $got) { $lock = $null; Die 'Another Odysseus install is already running on this computer. Wait for it to finish, then run this again if needed.' }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ── Tailscale ──
$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = 'C:\Program Files\Tailscale\tailscale.exe' }
if (-not (Test-Path $ts)) { Die 'Tailscale is not installed. Install it from https://tailscale.com/download, sign in to the same tailnet as Odysseus, and run this again.' }
$ip = (& $ts ip -4 2>$null | Select-Object -First 1)
if (-not $ip) { Die 'Tailscale is installed but not signed in. Open Tailscale and sign in, then run this again.' }
$ip = $ip.Trim()
$dns = ((& $ts status --json | ConvertFrom-Json).Self.DNSName).TrimEnd('.')
Say "this is $dns ($ip)"

# ── Python (the real one, not the Microsoft Store stub) ──
$py = $null
foreach ($cand in @('py', 'python')) {
    $c = Get-Command $cand -ErrorAction SilentlyContinue
    if ($c -and $c.Source -notlike '*WindowsApps*') {
        try { $exe = (& $c.Source -c 'import sys; print(sys.executable)' 2>$null).Trim(); if ($exe) { $py = $exe; break } } catch {}
    }
}
if (-not $py) { Die 'Python 3 is not installed. Install it (winget install Python.Python.3.12, or python.org), then run this again.' }
Say "using Python at $py"

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
# Download next to the file, then swap it in, so a stopped download never
# leaves a half-written or locked file where the server reads it.
function Fetch($name) {
    $final = Join-Path $Dest $name
    $tmp = "$final.download"
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    Invoke-WebRequest "$Base/enroll/$Code/file/$name" -OutFile $tmp -UseBasicParsing -TimeoutSec 120
    for ($i = 0; $i -lt 10; $i++) {
        try { Move-Item -Force $tmp $final; return } catch { Start-Sleep -Milliseconds 500 }
    }
    Die "Could not replace $final (another program has it open). Close it, then run this again."
}

Say 'installing the desktop MCP server'
Fetch 'desktop_mcp_server.py'
# Below 1.13: newer mcp checks the Host header and refuses the tailnet name.
# winsdk: media sessions and Bluetooth (Hear it on); pyaudiowpatch: streaming
# this PC's sound to another computer.
Say 'installing its Python packages (pip)'
& $py -m pip install --quiet --disable-pip-version-check --no-input --prefer-binary --timeout 60 'mcp>=1.10,<1.13' uvicorn pyautogui pillow pycaw comtypes winsdk pyaudiowpatch
if ($LASTEXITCODE -ne 0) { Die 'pip install failed (see above).' }

Set-Content -Encoding ASCII (Join-Path $Dest 'run-hidden.vbs') @'
' Launch a command hidden. Arg 0 is the batch file to run.
Set sh = CreateObject("Wscript.Shell")
sh.Run """" & WScript.Arguments(0) & """", 0, False
'@

function Install-Server($task, $cmdName, $script, $envLines, $logName) {
    $cmd = "@echo off`r`n" + ($envLines -join "`r`n") + "`r`n`"$py`" `"$Dest\$script`" >> `"$Dest\$logName`" 2>&1`r`n"
    Set-Content -Encoding ASCII (Join-Path $Dest $cmdName) $cmd
    $a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$Dest\run-hidden.vbs`" `"$Dest\$cmdName`""
    $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $p = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
    $s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $task -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null
    # A running copy holds the port; stop it so the new version starts.
    $port = ($envLines | Where-Object { $_ -match '_PORT=(\d+)' } | ForEach-Object { $Matches[1] }) | Select-Object -First 1
    if ($port) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
    Start-ScheduledTask -TaskName $task
}

$servers = @()
Say 'starting the desktop MCP server'
Install-Server 'OdysseusDesktopMCP' 'run-desktop-mcp.cmd' 'desktop_mcp_server.py' @(
    "set DESKTOP_MCP_HOST=$ip", 'set DESKTOP_MCP_PORT=8931', 'set DESKTOP_MCP_ALLOW_INPUT=1') 'desktop-out.log'
$servers += @{ kind = 'desktop'; port = 8931 }

# The music overlay (Pop out in the music bar): a task with no trigger, started
# on demand by Odysseus, on the desktop.
Say 'installing the music overlay'
Fetch 'music_overlay.py'
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
Set-Content -Encoding ASCII (Join-Path $Dest 'run-music-overlay.cmd') "@echo off`r`n`"$pyw`" `"$Dest\music_overlay.py`"`r`n"
$oa = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$Dest\run-hidden.vbs`" `"$Dest\run-music-overlay.cmd`""
$op = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$ovs = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'MusicOverlay' -Action $oa -Principal $op -Settings $ovs -Force | Out-Null

$resolveExe = 'C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe'
if (Test-Path $resolveExe) {
    Say 'DaVinci Resolve found: installing its MCP server too'
    Fetch 'resolve_mcp_server.py'
    Install-Server 'OdysseusResolveMCP' 'run-resolve-mcp.cmd' 'resolve_mcp_server.py' @(
        "set RESOLVE_MCP_HOST=$ip", 'set RESOLVE_MCP_PORT=8930') 'out.log'
    $servers += @{ kind = 'resolve'; port = 8930 }
}

# ── Administrator-only parts ──
$ssh = $false
if ($isAdmin) {
    Say 'allowing the MCP ports from the tailnet only'
    $ports = ($servers | ForEach-Object { $_.port }) -join ','
    Get-NetFirewallRule -DisplayName 'Odysseus MCP (tailnet only)' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'Odysseus MCP (tailnet only)' -Direction Inbound -Action Allow -Protocol TCP `
        -LocalPort ($ports -split ',') -RemoteAddress '100.64.0.0/10' | Out-Null

    Say 'enabling the OpenSSH server (for Ping restarts)'
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
    if ($cap -and $cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service sshd -ErrorAction SilentlyContinue
    # Administrators' keys live in one shared file that only admins can write.
    $keys = 'C:\ProgramData\ssh\administrators_authorized_keys'
    if (-not (Test-Path $keys)) { New-Item -ItemType File -Force -Path $keys | Out-Null }
    if (-not (Select-String -Path $keys -SimpleMatch $KeyOnly -Quiet)) { Add-Content -Path $keys -Value $PubKey }
    icacls $keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
    $ssh = (Get-Service sshd -ErrorAction SilentlyContinue).Status -eq 'Running'
} else {
    Say 'not running as Administrator: skipped the firewall rule and SSH (Ping restarts). Re-run as Administrator to add them.'
    $userKeys = Join-Path $env:USERPROFILE '.ssh\authorized_keys'
    New-Item -ItemType Directory -Force -Path (Split-Path $userKeys) | Out-Null
    if (-not (Test-Path $userKeys) -or -not (Select-String -Path $userKeys -SimpleMatch $KeyOnly -Quiet)) { Add-Content -Path $userKeys -Value $PubKey }
    $ssh = (Get-Service sshd -ErrorAction SilentlyContinue).Status -eq 'Running'
}

$gpu = (Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match 'NVIDIA|AMD Radeon|Intel Arc' } |
        Select-Object -First 1 -ExpandProperty Name)

Say 'registering with Odysseus'
$body = @{ dns = $dns; ip = $ip; os = 'windows'; user = $env:USERNAME; gpu = "$gpu"; ssh = $ssh; servers = $servers } |
        ConvertTo-Json -Depth 4
try {
    $r = Invoke-RestMethod -Method Post -Uri "$Base/enroll/$Code/register" -ContentType 'application/json' -Body $body
    # The overlay's token (messages, the phone's song), where the overlay reads it.
    if ($r.overlay_token) {
        $od = Join-Path $env:APPDATA 'odysseus-music-overlay'
        New-Item -ItemType Directory -Force -Path $od | Out-Null
        $sp = Join-Path $od 'settings.json'
        # Windows PowerShell 5.1: no -AsHashtable; keep the overlay's other settings.
        $cur = [pscustomobject]@{}
        if (Test-Path $sp) { try { $cur = Get-Content $sp -Raw | ConvertFrom-Json } catch { $cur = [pscustomobject]@{} } }
        $cur | Add-Member -NotePropertyName token -NotePropertyValue $r.overlay_token -Force
        $cur | Add-Member -NotePropertyName url -NotePropertyValue ($Base.TrimEnd('/') + '/') -Force
        $cur | ConvertTo-Json | Set-Content -Encoding UTF8 $sp
        Say "saved the music overlay's token"
    }
    $r.PSObject.Properties.Remove('overlay_token')
    Say ("done: " + ($r | ConvertTo-Json -Compress))
    Write-Host 'Open Settings > Devices in Odysseus to see this PC.'
    Unlock
} catch {
    Die 'Odysseus did not accept the registration. The code may have expired: make a new one in Settings > Devices.'
}
