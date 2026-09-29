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
    assert "function Die($m) { Write-Host \"Error: $m\" -ForegroundColor Red; Unlock; throw $m }" in PS1
    assert PS1.index("Unlock\n} catch {") > PS1.index("Open Settings > Devices in Odysseus to see this PC.")


def test_files_are_swapped_in_whole():
    fetch = PS1[PS1.index("function Fetch($name) {"):]
    fetch = fetch[:fetch.index("\n}\n") + 2]
    assert '-OutFile $tmp' in fetch and 'Move-Item -Force $tmp $final' in fetch
    # The lock comes before the first download.
    assert PS1.index("Global\\OdysseusInstall") < PS1.index("Fetch 'desktop_mcp_server.py'")


def test_a_timed_out_ssh_is_killed(tmp_path):
    # Eight installer ssh sessions outlived their timeout by hours and kept
    # the PC's server file open.
    import asyncio
    import time
    from src import machines
    marker = tmp_path / "still-running"
    r = asyncio.run(machines._run(["bash", "-c", f"sleep 3; touch {marker}"], timeout=0.5))
    assert r["rc"] == -1 and "timed out" in r["err"]
    time.sleep(3.5)
    assert not marker.exists()
