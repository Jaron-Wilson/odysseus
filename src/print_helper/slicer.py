"""The real slicing check: Bambu Studio's CLI on the Windows PC, over SSH.

Asked for: check the pieces the way the printer will see them (Bambu P1S with AMS, textured PEI, PLA).
The geometry checks here are estimates; the slicer has the final word.

How (learned on the knight model, 2026-10):
- the CLI wants FLATTENED profiles: each system preset merged along its `inherits` chain from
  resources\\profiles\\BBL\\{machine,process,filament}, then `inherits` set back to the system preset name
  and `compatible_printers` kept including the P1S, or it fails with "process not compatible with printer".
  They are built once on the PC (%LOCALAPPDATA%\\Odysseus\\bambu-profiles) unless a folder is set.
- supports stay off (Jaron prefers none: tall thin tree supports and support-filament interfaces caused
  spaghetti and huge purge). The slicer then reports what would need them:
  `max_cantilever_dist=` (scaled 1e6 per mm) and "floating cantilever" warnings in its --debug 5 log.
- the gcode header has the time and weight; `; FEATURE: Support` lines count support, and `M620 S` lines
  count filament loads (2 is just load and unload: no swaps).
- bambu-studio.exe is a GUI program, so it is started with Start-Process -Wait.
- the PC may be asleep or nobody signed in; the CLI works over SSH anyway. If it can't be reached, the
  check is skipped and the result says so.

Settings: DATA_DIR/print-helper/config.json (jobs.py). The PC is the one set there, else the Games PC
(src/games.py), reached with this server's SSH key like Settings > Devices does (src/machines.py).
"""
import base64
import getpass
import json
import os
import re
import secrets
import subprocess
from typing import Callable, Dict, List, Optional

DEFAULTS = {
    "pc_host": "",                  # blank: the Games PC
    "ssh_user": "",                 # blank: Settings > Devices' user for that machine, else this server's user
    "bambu_exe": r"C:\Program Files\Bambu Studio\bambu-studio.exe",
    "profiles_dir": "",             # blank: flatten the system presets on the PC once
    "machine": "Bambu Lab P1S 0.4 nozzle",
    "process": "0.20mm Standard @BBL X1C",
    "filament": "Bambu PLA Basic @BBL X1C",
}
SSH_TIMEOUT = 25
PIECE_TIMEOUT_S = 900

Progress = Optional[Callable[[float, str], None]]


class SliceError(RuntimeError):
    pass


def _ps(s: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(s).replace("'", "''") + "'"


def _resolve_target(cfg: Dict) -> Dict:
    from src import machines
    host = (cfg.get("pc_host") or "").strip()
    peer = None
    if host:
        try:
            peer = machines.find_peer(machines.peers(), host)
        except Exception:
            peer = None
        peer = peer or {"host": host, "dns": host, "ips": []}
    else:
        try:
            from src import games
            peer = games.pc_target()
        except Exception:
            peer = None
    if not peer:
        raise SliceError("no PC set: set it in the 3D print helper's slicer settings (or the Games PC)")
    addr = peer.get("dns") or (peer.get("ips") or [None])[0] or peer.get("host")
    user = (cfg.get("ssh_user") or "").strip()
    if not user:
        try:
            user = machines.load_prefs().get(peer.get("host") or "", {}).get("ssh_user") or ""
        except Exception:
            user = ""
    keys = machines._ssh_keys()
    if not keys:
        raise SliceError("this server has no SSH key for the PC (see Settings > Devices)")
    return {"addr": addr, "user": user or getpass.getuser(), "key": keys[0], "name": peer.get("name") or addr}


def _ssh_base(t: Dict) -> List[str]:
    return ["-i", t["key"], "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TIMEOUT}",
            "-o", "StrictHostKeyChecking=accept-new"]


def run_ps(t: Dict, script: str, timeout: float = 120) -> str:
    enc = base64.b64encode(("[Console]::OutputEncoding=[Text.Encoding]::UTF8\n" + script).encode("utf-16-le")).decode()
    if len(enc) > 30000:
        raise SliceError("internal: PowerShell script too long for one SSH command")
    argv = ["ssh", *_ssh_base(t), f"{t['user']}@{t['addr']}", f"powershell -NoProfile -NonInteractive -EncodedCommand {enc}"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SliceError(f"the PC did not answer within {timeout:.0f} s")
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().replace("\r", "")
        if "Permission denied" in err:
            raise SliceError("SSH to the PC was refused; add this server's key to it (Settings > Devices)")
        raise SliceError(f"SSH to the PC failed: {err[-300:] or 'no output'}")
    return r.stdout.replace("\r", "")


def _scp(t: Dict, src: List[str], dst: str, timeout: float = 600) -> None:
    argv = ["scp", "-q", *_ssh_base(t), *src, dst]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SliceError("copying files to or from the PC timed out")
    if r.returncode != 0:
        raise SliceError(f"copying files with the PC failed: {(r.stderr or '').strip()[-300:]}")


def _json_out(text: str):
    """The last line of PowerShell output that parses as JSON."""
    for line in reversed([x for x in text.splitlines() if x.strip()]):
        line = line.strip()
        if line[:1] in "[{":
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise SliceError(f"unexpected answer from the PC: {text.strip()[-300:] or 'nothing'}")


# ── PowerShell ──────────────────────────────────────────────────────────────

FLATTEN_PS = r"""
function Get-Preset($kind, $name) {
  $dir = Join-Path $res $kind
  $f = Join-Path $dir ($name + '.json')
  if (Test-Path -LiteralPath $f) { return (Get-Content -LiteralPath $f -Raw | ConvertFrom-Json) }
  foreach ($c in Get-ChildItem -LiteralPath $dir -Filter *.json -Recurse) {
    $j = Get-Content -LiteralPath $c.FullName -Raw | ConvertFrom-Json
    if ($j.name -eq $name) { return $j }
  }
  throw "no $kind preset named $name"
}
function Flatten($kind, $name, $out, $printer) {
  $chain = @(); $n = $name
  while ($n) { $j = Get-Preset $kind $n; $chain = ,$j + $chain; $n = $j.inherits; if ($chain.Count -gt 20) { break } }
  $m = [ordered]@{}
  foreach ($j in $chain) { foreach ($p in $j.PSObject.Properties) { $m[$p.Name] = $p.Value } }
  $m['inherits'] = $name; $m['from'] = 'User'; $m['name'] = $name + ' (Odysseus)'
  if ($kind -ne 'machine') {
    $cp = @($m['compatible_printers']) | Where-Object { $_ }
    if ($cp -notcontains $printer) { $cp = @($cp) + $printer }
    $m['compatible_printers'] = @($cp)
  }
  if ($kind -eq 'process') { $m['enable_support'] = '0' }
  [IO.File]::WriteAllText($out, ($m | ConvertTo-Json -Depth 20), (New-Object Text.UTF8Encoding $false))
}
"""


def prepare_script(cfg: Dict, token: str) -> str:
    return (
        "$ErrorActionPreference='Stop'\n"
        f"$exe={_ps(cfg['bambu_exe'])}\n"
        "if (-not (Test-Path -LiteralPath $exe)) { throw \"Bambu Studio is not at $exe\" }\n"
        "$res=Join-Path (Split-Path $exe) 'resources\\profiles\\BBL'\n"
        + FLATTEN_PS +
        f"$pd={_ps(cfg.get('profiles_dir') or '')}\n"
        "if (-not $pd) { $pd=Join-Path $env:LOCALAPPDATA 'Odysseus\\bambu-profiles' }\n"
        "New-Item -ItemType Directory -Force -Path $pd | Out-Null\n"
        f"$want=@{{machine={_ps(cfg['machine'])};process={_ps(cfg['process'])};filament={_ps(cfg['filament'])}}}\n"
        "$built=@()\n"
        "foreach ($k in 'machine','process','filament') {\n"
        "  $f=Join-Path $pd ($k + '.json')\n"
        "  if (-not (Test-Path -LiteralPath $f)) { Flatten $k $want[$k] $f $want['machine']; $built += $k }\n"
        "}\n"
        f"$w=Join-Path $env:TEMP ('odysseus-print\\' + {_ps(token)})\n"
        "New-Item -ItemType Directory -Force -Path $w | Out-Null\n"
        "@{work=$w; profiles=$pd; built=$built} | ConvertTo-Json -Compress\n")


def slice_script(cfg: Dict, work: str, profiles: str, names: List[str]) -> str:
    files = ",".join(_ps(n) for n in names)
    return (
        "$ErrorActionPreference='Continue'\n"
        f"$exe={_ps(cfg['bambu_exe'])}; $w={_ps(work)}; $p={_ps(profiles)}\n"
        f"$names=@({files})\n"
        "$res=@()\n"
        "foreach ($n in $names) {\n"
        "  $stem=[IO.Path]::GetFileNameWithoutExtension($n)\n"
        "  $o=Join-Path $w ('out_' + $stem); New-Item -ItemType Directory -Force -Path $o | Out-Null\n"
        "  $a=@('--slice','0','--arrange','1','--orient','0','--load-settings',\"`\"$p\\machine.json;$p\\process.json`\"\",\n"
        "       '--load-filaments',\"`\"$p\\filament.json`\"\",'--outputdir',\"`\"$o`\"\",'--export-3mf',($stem + '_sliced.3mf'),\n"
        "       '--debug','5',\"`\"$(Join-Path $w $n)`\"\")\n"
        "  $log=Join-Path $o 'stdout.txt'\n"
        "  $t0=Get-Date\n"
        "  $pr=Start-Process -FilePath $exe -ArgumentList $a -PassThru -NoNewWindow -RedirectStandardOutput $log -RedirectStandardError (Join-Path $o 'stderr.txt')\n"
        "  $null=$pr.Handle  # without this PowerShell forgets the exit code\n"
        f"  if (-not $pr.WaitForExit({PIECE_TIMEOUT_S * 1000})) {{ try {{ $pr.Kill() }} catch {{}}; $code=-1 }} else {{ $code=$pr.ExitCode }}\n"
        "  $r=[ordered]@{file=$n; exit=$code; seconds=[math]::Round(((Get-Date)-$t0).TotalSeconds,1)}\n"
        "  $lines=@(Get-Content -LiteralPath $log -ErrorAction SilentlyContinue)\n"
        "  $r.cantilever=@($lines | Select-String -Pattern 'max_cantilever_dist=(\\S+)' | ForEach-Object { $_.Matches[0].Groups[1].Value })\n"
        "  $r.warnings=@($lines | Select-String -Pattern 'message=(.*?), message_type=[12]' | ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Unique -First 20)\n"
        "  $r.noncritical=@($lines | Select-String -Pattern 'NON_CRITICAL' | ForEach-Object { $_.Line.Substring([Math]::Max(0, $_.Line.IndexOf(']', 30) + 1)).Trim() } | Select-Object -First 10)\n"
        "  $r.errors=@($lines | Select-String -Pattern '\\[error\\]' | ForEach-Object { $_.Line.Substring([Math]::Max(0, $_.Line.IndexOf(']', 30) + 1)).Trim() } | Select-Object -Last 5)\n"
        "  $g=Get-ChildItem -LiteralPath $o -Filter '*.gcode' | Select-Object -First 1\n"
        "  if ($g) {\n"
        "    $r.header=@(Get-Content -LiteralPath $g.FullName -TotalCount 12 | Where-Object { $_ -match 'time|weight|layer number|max_z' } | ForEach-Object { [string]$_ })\n"
        "    $r.support_lines=(Select-String -LiteralPath $g.FullName -Pattern '^; FEATURE: Support').Count\n"
        "    $r.m620=(Select-String -LiteralPath $g.FullName -Pattern '^M620 S').Count\n"
        "  }\n"
        "  $m=Get-ChildItem -LiteralPath $o -Filter '*_sliced.3mf' | Select-Object -First 1\n"
        "  if ($m) { $r.sliced=$m.FullName }\n"
        "  $res += [pscustomobject]$r\n"
        "}\n"
        "ConvertTo-Json -InputObject @($res) -Compress -Depth 5\n")


def cleanup_script(work: str) -> str:
    return (f"$w={_ps(work)}\n"
            "if ($w -like '*odysseus-print*') { Remove-Item -LiteralPath $w -Recurse -Force -ErrorAction SilentlyContinue }\n"
            "'{}'\n")


# ── Parsing ─────────────────────────────────────────────────────────────────

def parse_piece(r: Dict) -> Dict:
    """One piece's slicer output -> the numbers the page shows."""
    out = {"ok": r.get("exit") == 0 and bool(r.get("header")), "seconds": r.get("seconds")}
    vals = []
    for v in r.get("cantilever") or []:
        try:
            vals.append(float(v) / 1e6)
        except (TypeError, ValueError):
            pass
    out["max_cantilever_mm"] = round(max(vals), 1) if vals else None
    warn = [w for w in (r.get("warnings") or []) if isinstance(w, str)]
    out["warnings"] = warn[:10]
    text = " ".join(warn + [str(x) for x in r.get("noncritical") or []]).lower()
    out["floating_cantilever"] = "floating cantilever" in text
    out["floating_regions"] = "floating region" in text
    out["needs_support"] = "enable support" in text or out["floating_cantilever"] or out["floating_regions"]
    head = " ".join(r.get("header") or [])
    m = re.search(r"total estimated time:\s*([^;]+)", head) or re.search(r"model printing time:\s*([^;]+)", head)
    out["time"] = m.group(1).strip() if m else None
    m = re.search(r"total filament weight \[g\]\s*:\s*([\d.]+)", head)
    out["weight_g"] = float(m.group(1)) if m else None
    m = re.search(r"total layer number:\s*(\d+)", head)
    out["layers"] = int(m.group(1)) if m else None
    out["support_lines"] = r.get("support_lines")
    m620 = r.get("m620")
    out["filament_loads"] = m620
    out["filament_swaps"] = max(0, m620 - 2) if isinstance(m620, int) else None
    if not out["ok"]:
        out["error"] = "; ".join(r.get("errors") or []) or f"Bambu Studio exited with {r.get('exit')}"
    return out


# ── Entry point ─────────────────────────────────────────────────────────────

def slice_pieces(files: List[str], out_dir: str, cfg: Dict, opts: Dict, progress: Progress = None) -> Dict:
    """Slice each STL on the PC. Never raises for an unreachable PC: returns {"ok": False, "skipped": why}."""
    cfg = {**DEFAULTS, **{k: v for k, v in (cfg or {}).items() if v}}
    def tick(f, msg):
        if progress:
            progress(f, msg)
    t, work = None, None
    try:
        t = _resolve_target(cfg)
        tick(0.02, f"reaching {t['name']}")
        token = secrets.token_hex(6)
        prep = _json_out(run_ps(t, prepare_script(cfg, token), timeout=300))
        work = prep["work"]
        remote = f"{t['user']}@{t['addr']}:" + work.replace("\\", "/") + "/"
        tick(0.1, "copying the pieces to the PC")
        _scp(t, files, remote)
        names = [os.path.basename(f) for f in files]
        tick(0.2, f"slicing {len(names)} piece(s) in Bambu Studio")
        raw = _json_out(run_ps(t, slice_script(cfg, work, prep["profiles"], names),
                               timeout=PIECE_TIMEOUT_S * len(names) + 120))
        raw = raw if isinstance(raw, list) else [raw]
        tick(0.85, "fetching the sliced files")
        pieces = {}
        for r in raw:
            res = parse_piece(r)
            if r.get("sliced"):
                local = os.path.join(out_dir, os.path.basename(r["sliced"].replace("\\", "/")))
                try:
                    _scp(t, [f"{t['user']}@{t['addr']}:" + r["sliced"].replace("\\", "/")], local)
                    res["sliced_file"] = os.path.basename(local)
                except SliceError as e:
                    res["fetch_error"] = str(e)
            pieces[r.get("file")] = res
        tick(1.0, "sliced")
        return {"ok": True, "pc": t["name"], "profiles_built": prep.get("built") or [],
                "colors": opts.get("colors"), "pieces": pieces}
    except SliceError as e:
        return {"ok": False, "skipped": str(e)}
    except Exception as e:          # never lose the pieces over the check
        return {"ok": False, "skipped": f"the slice check failed: {type(e).__name__}: {str(e)[:300]}"}
    finally:
        if t is not None and work:
            try:
                run_ps(t, cleanup_script(work), timeout=60)
            except Exception:
                pass
