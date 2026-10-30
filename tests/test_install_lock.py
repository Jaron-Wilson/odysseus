"""The Windows install runs one at a time and never leaves a locked or
half-written server file.

Seen 2026-09-29: installs over SSH piled up, and the desktop MCP then failed
at every start with "python: can't open file ... desktop_mcp_server.py:
[Errno 13] Permission denied".
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PS1 = open(os.path.join(ROOT, "tools", "enroll", "install.ps1"), encoding="utf-8").read()


def test_one_install_at_a_time():
    assert "New-Object System.Threading.Mutex($false, 'Global\\OdysseusInstall')" in PS1
    assert "Another Odysseus install is already running on this computer." in PS1
    # Released when it ends and when it stops on an error (iex keeps the console).
    die = PS1[PS1.index("function Die($m) {"):]
    die = die[:die.index("\n}\n")]
    assert "[Console]::Error.WriteLine($m); exit 1" in die and "throw $m" in die
    assert die.index("Unlock") < die.index("exit 1")
    assert "WaitOne(60000)" in PS1
    assert PS1.index("Unlock\n} catch {") > PS1.index("Open Settings > Devices in Odysseus to see this PC.")


def test_files_are_swapped_in_whole():
    fetch = PS1[PS1.index("function Fetch($name) {"):]
    fetch = fetch[:fetch.index("\n}\n") + 2]
    assert '-OutFile $tmp' in fetch and 'Move-Item -Force $tmp $final' in fetch
    # The lock comes before the first download.
    assert PS1.index("Global\\OdysseusInstall") < PS1.index("Fetch 'desktop_mcp_server.py'")


def test_a_stalled_download_cannot_hang_the_install():
    # 2026-10-27: a download stalled at 25 of 75 KB, Invoke-WebRequest waited
    # forever, and its held lock failed every install after it for days.
    assert "$ProgressPreference = 'SilentlyContinue'" in PS1
    assert PS1.index("$ProgressPreference") < PS1.index("function Fetch($name) {")
    fetch = PS1[PS1.index("function Fetch($name) {"):]
    fetch = fetch[:fetch.index("\n}\n") + 2]
    assert "--max-time 180" in fetch and "-fsS" in fetch and "$LASTEXITCODE -ne 0" in fetch
    assert fetch.index("& $curl") < fetch.index("Invoke-WebRequest $url")   # curl.exe first, iwr only without it


def test_an_install_stuck_for_20_minutes_is_stopped():
    stuck = PS1[PS1.index("function Stop-StuckInstalls {"):]
    stuck = stuck[:stuck.index("\n}\n")]
    assert "AddMinutes(-20)" in stuck and "$_.ProcessId -ne $PID" in stuck
    # Odysseus starts installs with -EncodedCommand: decoded before matching.
    assert "FromBase64String" in stuck and "/enroll/[^/\\s]+/install\\.ps1" in stuck
    # Only after the minute's wait, then the lock is asked for again.
    assert PS1.index("WaitOne(60000)") < PS1.index("(Stop-StuckInstalls) -gt 0") < PS1.index("WaitOne(15000)")
