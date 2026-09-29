"""Stop the agent copying big files from the user's other machines onto this
server.

Seen 2026-09-29: with the PC's desktop MCP down, the agent set out to "fetch
it over the tailnet to the machine that has my shell, and then stream it from
here" for a render the user said was 63 GB, onto a server with under 7 GB
free. A file stays on the machine it is on: that machine's share_media
streams it into the chat. So a shell command that pulls a video, audio file,
image sequence or a whole folder from another machine is refused, with what to
do instead. Small pulls (a log, a config file) still work.
"""
import re
import shlex
from typing import List, Optional

# Media and project files that are big as a rule.
_MEDIA_EXT = re.compile(
    r"\.(?:mp4|m4v|mov|mkv|avi|webm|wmv|flv|mxf|mts|m2ts|ts|braw|r3d|ari|dng|prores|"
    r"wav|flac|aiff?|mp3|m4a|aac|ogg|opus|wma|"
    r"iso|img|vhd|vhdx|vmdk|qcow2|zip|7z|rar|tar|gz|tgz|drp|dra)$",
    re.IGNORECASE)
# user@host:path, host:path, or a Windows drive after the host (host:C:\...).
_REMOTE = re.compile(r"^(?:[\w.-]+@)?[\w.-]+:(?!//)")
# A tailnet address or name in a URL.
_TAILNET_URL = re.compile(r"https?://(?:100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+|[\w-]+\.[\w-]+\.ts\.net)\b",
                          re.IGNORECASE)

MESSAGE = (
    "Not run: this copies {what} from {host} onto this server, and files stay on the machine they are on "
    "(this server has little storage, and renders can be tens of GB). To play or show it in the chat, "
    "call that machine's share_media with the file's path (find_media finds it by name) and put the "
    "returned link on its own line. If that machine's MCP server is offline or has no share_media, "
    "do not copy the file: tell the user it needs Install/Update or Ping in Settings > Devices.")


def _segments(cmd: str) -> List[List[str]]:
    out = []
    for part in re.split(r"\|\||&&|;|\n", cmd):
        try:
            toks = shlex.split(part, posix=True)
        except ValueError:
            toks = part.split()
        if toks:
            out.append(toks)
    return out


def _host(spec: str) -> str:
    return spec.split(":", 1)[0].split("@")[-1]


def _looks_big(path: str, recursive: bool) -> bool:
    p = path.rstrip("/\\'\"")
    return bool(_MEDIA_EXT.search(p)) or recursive or p.endswith(("*", "/", "\\")) or "*" in p


def refusal(cmd: str) -> Optional[str]:
    """The message to return instead of running `cmd`, or None to run it."""
    for toks in _segments(cmd or ""):
        prog = toks[0].rsplit("/", 1)[-1]
        if prog in ("sudo", "nice", "ionice", "timeout") and len(toks) > 1:
            toks = toks[1:] if prog != "timeout" else toks[2:]
            prog = toks[0].rsplit("/", 1)[-1] if toks else ""
        if prog in ("scp", "rsync"):
            flags = [t for t in toks[1:] if t.startswith("-")]
            recursive = any(("r" in f.lstrip("-") and not f.startswith("--")) or f in ("--recursive", "--archive")
                            or (prog == "rsync" and "a" in f.lstrip("-") and not f.startswith("--"))
                            for f in flags)
            operands = [t for t in toks[1:] if not t.startswith("-")]
            if len(operands) >= 2 and not _REMOTE.match(operands[-1]):
                for src in operands[:-1]:
                    if _REMOTE.match(src) and _looks_big(src.split(":", 1)[1], recursive):
                        return MESSAGE.format(what=src.split(":", 1)[1] or "a folder", host=_host(src))
        elif prog == "sftp":
            return MESSAGE.format(what="files", host=next((_host(t) for t in toks[1:] if not t.startswith("-")), "another machine"))
        elif prog == "ssh":
            # ssh host 'cat big.mp4' > big.mp4, or ssh host 'tar c dir' | tar x: the
            # redirect or pipe is outside the quoted remote command.
            host, rest, skip = None, [], False
            for t in toks[1:]:
                if skip:
                    skip = False
                elif host is None and t in ("-i", "-o", "-p", "-l", "-F", "-J", "-b", "-c", "-E"):
                    skip = True
                elif host is None and t.startswith("-"):
                    pass
                elif host is None:
                    host = t
                else:
                    rest.append(t)
            remote = " ".join(r for r in rest if not r.startswith(">"))
            to_file = any(r.startswith(">") and r.lstrip(">").strip() not in ("", "nul", "/dev/null") for r in rest) \
                or any(r == ">" for r in rest)
            piped = bool(re.search(r"\bssh\b.*\|\s*(?:tar|dd|pv|cat|tee)\b", cmd))
            names = re.findall(r"[^\s'\"]+", remote.replace("\\", "/"))
            if host and (to_file or piped) and (any(_MEDIA_EXT.search(n) for n in names) or re.search(r"\btar\b", remote)):
                return MESSAGE.format(what="a file", host=host.split("@")[-1])
        elif prog in ("curl", "wget"):
            url = next((t for t in toks[1:] if _TAILNET_URL.match(t)), None)
            writes = prog == "wget" or any(t in ("-o", "-O", "--output", "--remote-name") or t.startswith("-o")
                                           for t in toks[1:]) or re.search(r"(?<![0-9&])>\s*\S", cmd)
            if url and writes and (_MEDIA_EXT.search(url.split("?")[0]) or "/media/" in url):
                return MESSAGE.format(what=url, host=re.sub(r"^https?://", "", url).split("/")[0])
    return None
