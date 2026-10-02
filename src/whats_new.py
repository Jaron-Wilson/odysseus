"""What's new: the pull requests merged into dev, newest first.

Asked for on 2026-10-01: "add a whats new page? on it? and then basically for
each pr i can see then ask questions too."

Where the list comes from:
- git is the source of truth: the "Merge pull request #N from ..." commits on
  the first-parent history of the checkout this server runs from (the same
  checkout src/build_info.py reads). The title is the merge commit's first
  body line, the files and line counts come from the diff against the first
  parent. That alone is enough, offline.
- GitHub adds the PR's description, author and merge date (REST API, no token
  needed for a public repo; GITHUB_TOKEN or GH_TOKEN is used only if one is
  already set). Kept in DATA_DIR/whats_new/github.json and refreshed in the
  background, so the page works from the cache when GitHub can't be reached.
- A short plain-language summary is written once per PR by the Utility model
  and cached (summaries.json); until then, the first paragraph of the
  description stands in.

"Running" means the server process already has that merge: the merge commit
is an ancestor of the commit the process started with (build_info, read at
import). When the checkout's HEAD is ahead, the page says a restart would
load the rest.

"Ask about this" starts a chat with the PRs' details and a capped diff kept
as context for the chat's whole life (asks/<session id>.json, added to every
turn by routes/chat_helpers.build_chat_context).
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from typing import Dict, List, Optional

from src import build_info
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

REPO_DIR = build_info.BASE_DIR
CACHE_DIR = os.path.join(DATA_DIR, "whats_new")
DEFAULT_REPO = "Jaron-Wilson/odysseus"
GITHUB_API = "https://api.github.com"
MAX_MERGES = 500
MAX_GITHUB_PAGES = 5
REFRESH_EVERY_S = 30 * 60
SUMMARIES_PER_REFRESH = 8
NEW_WINDOW_S = 7 * 86400          # what counts as new for someone who has never looked
MAX_FILES_SHOWN = 300

# "Ask about this": the context a chat keeps. The diff gets most of it.
MAX_ASK_PRS = 10
MAX_DIFF_CHARS = 40_000
MAX_BODY_CHARS = 6_000
MAX_FILES_LISTED = 60
# Diffs nobody reads: lock files, minified and vendored code, images.
_NOISY = re.compile(r"(package-lock\.json|\.lock$|\.min\.(js|css)$|^static/lib/|\.(png|jpe?g|gif|webp|ico|svg|woff2?|ttf|pdf)$)")

_MERGE_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
_lock = threading.Lock()
_state: Dict = {"refreshed": 0.0, "github_error": "", "summarizing": False}
_ancestors: Dict[str, set] = {}
_merges_cache: Dict[str, List[Dict]] = {}


# ── Storage ──────────────────────────────────────────────────────────────

def _path(name: str) -> str:
    return os.path.join(CACHE_DIR, name)


def _read(name: str, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def _write(name: str, data) -> None:
    path = _path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".wn_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── git ──────────────────────────────────────────────────────────────────

def _git(*args: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def github_repo() -> str:
    """owner/name of this checkout's origin, if it is on GitHub."""
    m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$", _git("remote", "get-url", "origin"))
    return m.group(1) if m else DEFAULT_REPO


def head_commit() -> str:
    return _git("rev-parse", "HEAD").strip()


def running_commit() -> str:
    return (build_info.INFO.get("commit_full") or build_info.INFO.get("commit") or "").strip()


def merges(rev: str = "HEAD") -> List[Dict]:
    """The PR merges on `rev`'s first-parent line, newest first, with the
    files each changed (diffed against the first parent)."""
    head = _git("rev-parse", rev).strip()
    if not head:
        return []
    if head in _merges_cache:
        return _merges_cache[head]
    out: List[Dict] = []
    raw = _git("log", "--first-parent", "--merges", f"-{MAX_MERGES}", "--diff-merges=first-parent",
               "--numstat", "--format=%x1e%H%x1f%P%x1f%ct%x1f%an%x1f%s%x1f%b%x1f", head, timeout=60)
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        parts = rec.split("\x1f")
        if len(parts) < 7:
            continue
        sha, parents, ct, author, subject, body, stat = parts[:7]
        m = _MERGE_RE.match(subject.strip())
        if not m:
            continue
        lines = [ln.strip() for ln in body.strip().splitlines()]
        files, add, dele = [], 0, 0
        for ln in stat.strip().splitlines():
            bits = ln.split("\t")
            if len(bits) != 3:
                continue
            a = int(bits[0]) if bits[0].isdigit() else 0
            d = int(bits[1]) if bits[1].isdigit() else 0
            files.append({"path": bits[2], "additions": a, "deletions": d, "binary": bits[0] == "-"})
            add += a
            dele += d
        source = m.group(2)
        out.append({
            "pr": int(m.group(1)), "sha": sha, "parent": (parents.split() or [""])[0],
            "time": int(ct or 0), "committer": author,
            "branch": source.split("/", 1)[-1], "from": source,
            "title": (lines[0] if lines and lines[0] else subject.strip())[:200],
            "files": files, "additions": add, "deletions": dele,
        })
    _merges_cache.clear()
    _merges_cache[head] = out
    return out


def _ancestors_of(commit: str) -> set:
    if not commit:
        return set()
    if commit not in _ancestors:
        _ancestors[commit] = set(_git("rev-list", commit, timeout=60).split())
    return _ancestors[commit]


def is_running(sha: str, running: Optional[str] = None) -> bool:
    """Does the server process have this merge? (Same answer as
    `git merge-base --is-ancestor sha running`, from one rev-list.)"""
    running = running if running is not None else running_commit()
    if not running or not sha:
        return False
    anc = _ancestors_of(running)
    if not anc:                                       # a short hash git could not expand
        r = subprocess.run(["git", "-C", REPO_DIR, "merge-base", "--is-ancestor", sha, running],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    return sha in anc


def pr_diff(m: Dict) -> str:
    if not m.get("parent"):
        return ""
    return _git("diff", "-M", m["parent"], m["sha"], timeout=60)


# ── GitHub ───────────────────────────────────────────────────────────────

def _token() -> str:
    return (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()


def _headers(etag: str = "") -> Dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "odysseus-whats-new",
         "X-GitHub-Api-Version": "2022-11-28"}
    tok = _token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if etag:
        h["If-None-Match"] = etag
    return h


def _keep(pr: Dict) -> Dict:
    return {"title": (pr.get("title") or "")[:300], "body": pr.get("body") or "",
            "author": ((pr.get("user") or {}).get("login") or ""), "merged_at": pr.get("merged_at") or "",
            "url": pr.get("html_url") or "", "merge_commit": pr.get("merge_commit_sha") or "",
            "branch": ((pr.get("head") or {}).get("ref") or "")}


def fetch_github(wanted: set, force: bool = False) -> Dict:
    """Bring the GitHub cache up to date for the PR numbers in `wanted`.
    Page 1 (the most recently updated) is read every time, with its ETag so
    an unchanged answer does not count against the rate limit; further pages
    only while some wanted PR is still missing."""
    import httpx
    cache = _read("github.json", {})
    prs: Dict[str, Dict] = cache.get("prs") or {}
    etag = cache.get("etag") or ""
    repo = github_repo()
    error = ""
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            for page in range(1, MAX_GITHUB_PAGES + 1):
                if page > 1 and not (wanted - {int(k) for k in prs}):
                    break
                r = client.get(f"{GITHUB_API}/repos/{repo}/pulls",
                               params={"state": "closed", "base": "dev", "sort": "updated",
                                       "direction": "desc", "per_page": 100, "page": page},
                               headers=_headers(etag if page == 1 and not force else ""))
                if r.status_code == 304:
                    continue
                if r.status_code in (403, 429):
                    reset = r.headers.get("x-ratelimit-reset") or ""
                    error = "GitHub's rate limit was reached" + (
                        f"; it resets at {time.strftime('%H:%M', time.localtime(int(reset)))}" if reset.isdigit() else "")
                    break
                if r.status_code != 200:
                    error = f"GitHub answered {r.status_code}"
                    break
                if page == 1:
                    etag = r.headers.get("etag") or ""
                items = r.json() or []
                for pr in items:
                    if pr.get("merged_at") and pr.get("number"):
                        prs[str(pr["number"])] = _keep(pr)
                if len(items) < 100:
                    break
    except Exception as e:
        error = f"GitHub could not be reached ({type(e).__name__})"
    _write("github.json", {"prs": prs, "etag": etag, "fetched": time.time() if not error else cache.get("fetched", 0),
                           "error": error, "tried": time.time()})
    return {"prs": prs, "error": error}


# ── Summaries ────────────────────────────────────────────────────────────

def first_paragraph(body: str, limit: int = 320) -> str:
    """The first real paragraph of a PR description: no headings, no list
    markers, no markdown links' URLs."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for para in re.split(r"\n\s*\n", text):
        lines = [ln.strip() for ln in para.strip().splitlines()
                 if ln.strip() and not ln.strip().startswith(("#", "|", "---"))]
        if not lines:
            continue
        s = " ".join(re.sub(r"^([-*+]|\d+\.)\s+", "", ln) for ln in lines)
        s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
        s = re.sub(r"[*_`]{1,3}", "", s).strip()
        if len(s) < 8:
            continue
        return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "..."
    return ""


_SUMMARY_PROMPT = (
    "You write the release notes for Odysseus, a self-hosted AI assistant web app. Given one pull "
    "request, write one or two short sentences in plain language saying what changed for the person "
    "using the app. No jargon, no file names, no markdown, no em dashes, American English. Start with "
    "the change itself, not with 'This PR'."
)


def _summary_key(title: str, body: str) -> str:
    import hashlib
    return hashlib.sha1(f"{title}\n{body}".encode("utf-8", "replace")).hexdigest()[:12]


def summarize_missing(limit: int = SUMMARIES_PER_REFRESH) -> int:
    """Have the Utility model write summaries for the newest PRs without one."""
    if _state["summarizing"]:
        return 0
    from src.endpoint_resolver import resolve_endpoint
    url, model, headers = resolve_endpoint("utility")
    if not (url and model):
        return 0
    _state["summarizing"] = True
    done = 0
    try:
        from src.llm_core import llm_call
        sums = _read("summaries.json", {})
        gh = (_read("github.json", {}).get("prs") or {})
        for m in merges():
            if done >= limit:
                break
            g = gh.get(str(m["pr"])) or {}
            title, body = g.get("title") or m["title"], g.get("body") or ""
            key = _summary_key(title, body)
            if (sums.get(str(m["pr"])) or {}).get("key") == key:
                continue
            files = ", ".join(f["path"] for f in m["files"][:15])
            user = f"Title: {title}\n\nDescription:\n{body[:4000] or '(none)'}\n\nFiles: {files}"
            try:
                text = llm_call(url, model, [{"role": "system", "content": _SUMMARY_PROMPT},
                                             {"role": "user", "content": user}],
                                temperature=0.2, max_tokens=200, headers=headers, timeout=60)
            except Exception as e:
                logger.info("[whats-new] summary for #%s failed: %s", m["pr"], e)
                break
            text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
            text = " ".join(re.sub(r"\s*\u2014\s*", ", ", text).split())[:400]
            if not text:
                continue
            sums[str(m["pr"])] = {"key": key, "text": text, "model": model, "at": time.time()}
            _write("summaries.json", sums)
            done += 1
    finally:
        _state["summarizing"] = False
    return done


# ── Refresh ──────────────────────────────────────────────────────────────

def refresh(force: bool = False, summaries: bool = True) -> Dict:
    """Read git again, update the GitHub cache, then (in the background)
    write any missing summaries."""
    with _lock:
        _merges_cache.clear()
        ms = merges()
        res = fetch_github({m["pr"] for m in ms}, force=force)
        _state["refreshed"] = time.time()
        _state["github_error"] = res["error"]
    if summaries:
        threading.Thread(target=summarize_missing, daemon=True, name="whats-new-summaries").start()
    return {"ok": True, "count": len(ms), "github_error": res["error"]}


async def refresh_forever() -> None:
    """Started with the app: a first refresh soon after startup, then every half hour."""
    import asyncio
    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(refresh)
        except Exception as e:
            logger.warning("[whats-new] refresh failed: %s", e)
        await asyncio.sleep(REFRESH_EVERY_S)


# ── Seen ─────────────────────────────────────────────────────────────────

def _owner_key(owner: Optional[str]) -> str:
    return owner or "_"


def last_seen(owner: Optional[str]) -> float:
    rec = _read("seen.json", {}).get(_owner_key(owner)) or {}
    return float(rec.get("at") or 0)


def mark_seen(owner: Optional[str], at: Optional[float] = None) -> float:
    with _lock:
        d = _read("seen.json", {})
        when = float(at if at is not None else time.time())
        d[_owner_key(owner)] = {"at": when}
        _write("seen.json", d)
    return when


def _new_after(owner: Optional[str]) -> float:
    seen = last_seen(owner)
    return seen if seen else time.time() - NEW_WINDOW_S


def unseen_count(owner: Optional[str]) -> int:
    after = _new_after(owner)
    return sum(1 for m in merges() if m["time"] > after)


# ── Entries ──────────────────────────────────────────────────────────────

def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else ""


def _entry(m: Dict, gh: Dict, sums: Dict, repo: str, running: str, after: float) -> Dict:
    g = gh.get(str(m["pr"])) or {}
    body = g.get("body") or ""
    title = g.get("title") or m["title"]
    s = sums.get(str(m["pr"])) or {}
    if s.get("text") and s.get("key") == _summary_key(title, body):
        summary, source = s["text"], "ai"
    else:
        summary = first_paragraph(body)
        source = "description" if summary else "title"
    return {
        "pr": m["pr"], "title": title, "summary": summary, "summary_source": source,
        "body": body, "has_body": bool(body.strip()),
        "merged_at": g.get("merged_at") or _iso(m["time"]), "time": m["time"],
        "author": g.get("author") or m["from"].split("/", 1)[0],
        "branch": g.get("branch") or m["branch"],
        "sha": m["sha"], "short": m["sha"][:8],
        "url": g.get("url") or f"https://github.com/{repo}/pull/{m['pr']}",
        "files": m["files"][:MAX_FILES_SHOWN], "file_count": len(m["files"]),
        "additions": m["additions"], "deletions": m["deletions"],
        "running": is_running(m["sha"], running), "new": m["time"] > after,
        "from_github": bool(g),
    }


def entries(owner: Optional[str]) -> Dict:
    ms = merges()
    gh_cache = _read("github.json", {})
    gh = gh_cache.get("prs") or {}
    sums = _read("summaries.json", {})
    repo, running, head = github_repo(), running_commit(), head_commit()
    after = _new_after(owner)
    items = [_entry(m, gh, sums, repo, running, after) for m in ms]
    pending = [e["pr"] for e in items if not e["running"]]
    return {
        "entries": items, "repo": repo,
        "running_commit": running[:8], "head_commit": head[:8],
        "running_pr": build_info.INFO.get("pr"), "started": build_info.INFO.get("started"),
        # Merged into the checkout, not loaded by this process yet.
        "pending": pending, "restart_needed": bool(running and head and head != running and pending),
        "github": {"fetched": gh_cache.get("fetched") or 0, "error": gh_cache.get("error") or "",
                   "token": bool(_token())},
        "last_seen": last_seen(owner), "new_after": after,
        "new_count": sum(1 for e in items if e["new"]),
    }


def find(pr: int) -> Optional[Dict]:
    gh = (_read("github.json", {}).get("prs") or {})
    sums = _read("summaries.json", {})
    for m in merges():
        if m["pr"] == pr:
            return dict(_entry(m, gh, sums, github_repo(), running_commit(), time.time()), _merge=m)
    return None


# ── Ask about this ───────────────────────────────────────────────────────

def _file_list(e: Dict, limit: int = MAX_FILES_LISTED) -> str:
    files = e["_merge"]["files"]
    rows = [f"- {f['path']} (+{f['additions']} -{f['deletions']})" if not f.get("binary") else f"- {f['path']} (binary)"
            for f in files[:limit]]
    if len(files) > limit:
        rest = files[limit:]
        tops: Dict[str, int] = {}
        for f in rest:
            top = f["path"].split("/", 1)[0] if "/" in f["path"] else "(top level)"
            tops[top] = tops.get(top, 0) + 1
        where = ", ".join(f"{k} ({v})" for k, v in sorted(tops.items(), key=lambda kv: -kv[1])[:6])
        rows.append(f"- ...and {len(rest)} more files (+{sum(f['additions'] for f in rest)} "
                    f"-{sum(f['deletions'] for f in rest)}), under {where}")
    return "\n".join(rows)


def _diff_within(diff: str, budget: int) -> str:
    """The diff, file by file, until the budget runs out. Noisy files are
    left out, a file too big for what is left is cut, and the rest are named."""
    chunks = re.split(r"(?m)^(?=diff --git )", diff or "")
    out, omitted, used = [], [], 0
    for ch in chunks:
        if not ch.strip():
            continue
        m = re.match(r"diff --git a/(\S+) b/(\S+)", ch)
        path = m.group(2) if m else "?"
        if _NOISY.search(path):
            omitted.append(path)
            continue
        if used + len(ch) <= budget:
            out.append(ch)
            used += len(ch)
        elif budget - used > 1500:
            room = budget - used - 80
            cut = ch[:room].rsplit("\n", 1)[0]
            more = ch[len(cut):].count("\n")
            out.append(cut + f"\n[... {more} more lines of {path} not shown]\n")
            used = budget
        else:
            omitted.append(path)
    text = "".join(out)
    if omitted:
        text += f"\n[Diff not shown for {len(omitted)} file(s): {', '.join(omitted[:40])}" + (
            ", ..." if len(omitted) > 40 else "") + "]\n"
    return text


def build_ask_context(prs: List[int], diff_budget: int = MAX_DIFF_CHARS) -> Dict:
    """What a chat about these PRs is given: each one's title, description,
    files and a share of the diff budget. Returns {text, prs, titles, diff_cut}."""
    found = [e for e in (find(int(n)) for n in prs[:MAX_ASK_PRS]) if e]
    if not found:
        return {"text": "", "prs": [], "titles": [], "diff_cut": False}
    share = max(2000, diff_budget // len(found))
    parts = [
        "This chat is about changes to Odysseus (the app the user is talking to you in): the pull "
        f"request{'s' if len(found) > 1 else ''} below, merged into its dev branch. Answer the user's "
        "questions about them from this material: what changed, why, how it works, what to try. "
        "Quote file names and code where it helps. If something is not covered here (the diff may be "
        "cut short), say so rather than guess. 'Running' says whether the live server already has the "
        "change; if not, it arrives with the next restart.",
    ]
    cut = False
    for e in found:
        diff = _diff_within(pr_diff(e["_merge"]), share)
        cut = cut or "not shown" in diff
        body = e["body"].strip()
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n[description cut short]"
        when = (e["merged_at"] or "")[:10]
        parts.append(
            f"## PR #{e['pr']}: {e['title']}\n"
            f"Merged {when} by {e['author']} from branch {e['branch']}, merge commit {e['short']}. "
            f"Running on this server: {'yes' if e['running'] else 'no, waiting for a restart'}.\n{e['url']}\n\n"
            f"### Description\n{body or '(no description)'}\n\n"
            f"### Files changed ({e['file_count']} files, +{e['additions']} -{e['deletions']})\n"
            f"{_file_list(e)}\n\n"
            f"### Diff\n`````diff\n{diff.rstrip()}\n`````"
        )
    return {"text": "\n\n".join(parts), "prs": [e["pr"] for e in found],
            "titles": [e["title"] for e in found], "diff_cut": cut}


def save_ask(session_id: str, owner: Optional[str], ctx: Dict) -> None:
    _write(os.path.join("asks", f"{session_id}.json"),
           {"owner": owner or "", "prs": ctx["prs"], "titles": ctx["titles"], "text": ctx["text"],
            "created": time.time()})


def chat_context(session_id: str) -> str:
    """The PR context a chat was started with, or "" (most chats)."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", session_id):
        return ""
    p = _path(os.path.join("asks", f"{session_id}.json"))
    if not os.path.exists(p):
        return ""
    return (_read(os.path.join("asks", f"{session_id}.json"), {}).get("text") or "")


def ask_info(session_id: str) -> Dict:
    if not chat_context(session_id):
        return {}
    d = _read(os.path.join("asks", f"{session_id}.json"), {})
    return {"prs": d.get("prs") or [], "titles": d.get("titles") or []}


# ── The agent's tool ─────────────────────────────────────────────────────

def run_tool(content: str) -> Dict:
    """whats_new: list recent PRs (optionally matching a query), or get one."""
    raw = (content or "").strip()
    args: Dict = {}
    if raw.startswith("{"):
        try:
            args = json.loads(raw) or {}
        except ValueError:
            args = {}
    else:
        args = {"query": raw}
    action = (args.get("action") or "").lower()
    num = args.get("pr") or args.get("number")
    if not action:
        action = "get" if num else "list"
    if action == "get":
        try:
            e = find(int(str(num).lstrip("#")))
        except (TypeError, ValueError):
            e = None
        if not e:
            return {"error": f"No merged PR #{num} in this checkout's history.", "exit_code": 1}
        files = "\n".join(f"- {f['path']} (+{f['additions']} -{f['deletions']})" for f in e["_merge"]["files"][:80])
        body = e["body"][:MAX_BODY_CHARS] or "(no description)"
        return {"output": (
            f"PR #{e['pr']}: {e['title']}\nMerged {e['merged_at'][:10]} by {e['author']} ({e['branch']}), "
            f"commit {e['short']}. Running on this server: {'yes' if e['running'] else 'no (needs a restart)'}.\n"
            f"{e['url']}\n\nSummary: {e['summary'] or '(none)'}\n\nDescription:\n{body}\n\n"
            f"Files ({e['file_count']}, +{e['additions']} -{e['deletions']}):\n{files}"), "exit_code": 0}
    q = str(args.get("query") or "").strip().lower()
    limit = max(1, min(int(args.get("limit") or 15), 50))
    items = entries(None)["entries"]
    if q:
        words = [w for w in re.split(r"\W+", q) if len(w) > 2 and w not in ("the", "with", "what", "changed", "change")]
        def score(e):
            hay = " ".join([e["title"], e["summary"], e["body"][:3000], e["branch"],
                            " ".join(f["path"] for f in e["files"][:60])]).lower()
            return sum(hay.count(w) for w in words)
        scored = [(score(e), e) for e in items]
        items = [e for s, e in sorted(scored, key=lambda t: -t[0]) if s > 0] if words else items
    if not items:
        return {"output": f"No merged PR matches {q!r}.", "exit_code": 0}
    lines = [f"#{e['pr']} {e['title']} ({e['merged_at'][:10]}{'' if e['running'] else ', not running yet'})"
             + (f": {e['summary']}" if e["summary"] else "") for e in items[:limit]]
    return {"output": (f"Merged PRs matching {q!r}, best match first" if q else "Merged PRs, newest first") + ":\n"
            + "\n".join(lines) + "\n\nUse {\"action\": \"get\", \"pr\": N} for one PR's description and files.",
            "exit_code": 0}
