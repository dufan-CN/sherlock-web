#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sherlock Web —— 把 Sherlock CLI 包装成一个本地网页服务，附带国内站点、
平台检索、Maigret 第二引擎、邮箱/手机号/域名扩展工具和本地大模型分析。

只用 Python 标准库，不额外安装依赖。
启动：python sherlock_web.py   （默认监听 127.0.0.1:8090）
"""

import gzip
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
# Sherlock 仓库根目录：默认是本文件所在目录的上一级（即把 web/ 放在 clone 出来的
# Sherlock 根目录下）；也可以用环境变量 SHERLOCK_HOME 指向别处。
SHERLOCK_DIR = os.environ.get("SHERLOCK_HOME") or os.path.dirname(ROOT)
DATA_JSON = os.path.join(SHERLOCK_DIR, "sherlock_project", "resources", "data.json")
CN_SITES_JSON = os.path.join(ROOT, "sites_cn.json")
MERGED_JSON = os.path.join(ROOT, "data_merged.json")
INDEX_HTML = os.path.join(ROOT, "index.html")


def _find_exe():
    """找 sherlock 可执行文件：SHERLOCK_EXE > 自带 venv > PATH。"""
    env = os.environ.get("SHERLOCK_EXE")
    if env:
        return env
    for cand in (
        os.path.join(SHERLOCK_DIR, ".venv", "Scripts", "sherlock.exe"),
        os.path.join(SHERLOCK_DIR, ".venv", "Scripts", "sherlock"),
        os.path.join(SHERLOCK_DIR, ".venv", "bin", "sherlock"),
    ):
        if os.path.isfile(cand):
            return cand
    return shutil.which("sherlock") or "sherlock"


EXE = _find_exe()

# Maigret 是 Sherlock 的分支，站点库更大（5897 站），支持按国家/类别标签筛。
# 作为第二个引擎并行提供，不是替代。
# 默认留空：找不到时 Maigret 分支会提示、不影响主引擎。
# 指向本机 maigret 可执行文件即可启用，例如 MAIGRET_EXE=/usr/local/bin/maigret
MAIGRET_EXE = os.environ.get("MAIGRET_EXE", "")
MAIGRET_TIMEOUT = 300

# ---- 扩展工具：邮箱 / 手机号 / 域名 -----------------------------------
# 这几个工具的二进制来自一个独立的 osint 环境，默认放在本文件同级 osint-hub/，
# 也可以用环境变量 OSINT_HUB 指向别处；找不到时对应功能会提示，不影响其他功能。
OSINT_HUB = os.environ.get("OSINT_HUB") or os.path.join(ROOT, "osint-hub")

EXTRA_TOOLS = {
    "holehe": "邮箱 → 是否注册在 120+ 服务上（走忘记密码流程，不外发通知）",
    "ghunt": "Gmail → Google 账号资料（需先执行一次 ghunt login）",
    "phoneinfoga": "手机号 → 归属国家 / 运营商 / 公开信息",
    "harvester": "域名 → 子域名、邮箱（走本地 Docker 镜像）",
}

# GHunt 用的是你自己的 Google 登录态，默认不对外开；要开就设 SHERLOCK_GHUNT=1
ENABLE_GHUNT = os.environ.get("SHERLOCK_GHUNT", "0").lower() in ("1", "true", "on", "yes")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.\w{2,}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+$")

EXTRA_LOCK = threading.Lock()
EXTRA_RUNNING = set()          # 同一时刻同一工具只跑一个，防止被并发打爆


def extra_argv(tool, target):
    """组装扩展工具的命令行，返回 (cmd, timeout) 或 (None, 错误说明)。"""
    if tool == "holehe":
        exe = os.path.join(OSINT_HUB, ".venv", "Scripts", "holehe.exe")
        if not os.path.isfile(exe):
            return None, "找不到 holehe.exe（%s）" % exe
        return [exe, target, "--no-color", "--no-clear"], 300

    if tool == "ghunt":
        if not ENABLE_GHUNT:
            return None, "GHunt 默认关闭（会用你的 Google 登录态）。设环境变量 SHERLOCK_GHUNT=1 后重启服务再试"
        exe = os.path.join(OSINT_HUB, ".venv", "Scripts", "ghunt.exe")
        if not os.path.isfile(exe):
            return None, "找不到 ghunt.exe（%s）" % exe
        return [exe, "email", target], 120

    if tool == "phoneinfoga":
        exe = os.path.join(OSINT_HUB, "bin", "phoneinfoga.exe")
        if not os.path.isfile(exe):
            return None, "找不到 phoneinfoga.exe（%s）" % exe
        number = target.strip()
        if not number.startswith("+"):
            number = "+" + re.sub(r"[^\d]", "", number)
        return [exe, "scan", "-n", number], 180

    if tool == "harvester":
        return (["docker", "run", "--rm", "--entrypoint",
                 "/opt/venv/bin/theHarvester", "theharvester-local",
                 "-d", target, "-b", "crtsh,duckduckgo", "-l", "200"], 300)

    return None, "未知工具"


def extra_run(tool, target):
    """跑一个扩展工具，返回 {ok, output, error, elapsed, cmd}。"""
    cmd, note = extra_argv(tool, target)
    if cmd is None:
        return {"ok": False, "output": "", "error": note, "elapsed": 0}

    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=note)
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", NO_COLOR="1")

    started = time.time()
    try:
        proc = subprocess.run(cmd, env=env, **kwargs)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "elapsed": round(time.time() - started, 1),
                "error": "运行超时（%d 秒）" % note}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "", "elapsed": 0, "error": "启动失败：%s" % exc}

    text = decode_output((proc.stdout or b"") + (proc.stderr or b""))
    ok = proc.returncode == 0
    return {"ok": ok, "output": text.strip(), "elapsed": round(time.time() - started, 1),
            "error": "" if ok else "返回码 %s" % proc.returncode}


# ---- AI 搜索：本地 Ollama（DeepSeek R1）+ 站点库 ------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL") or "http://127.0.0.1:11434"
AI_MODEL = os.environ.get("AI_MODEL") or "deepseek-r1:14b"
AI_DB = os.environ.get("AI_DB") or os.path.join(ROOT, "ai.db")
AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "180"))

AI_RATE_WINDOW = 600
AI_RATE_LIMIT = 6        # 每 IP 10 分钟最多问 6 次（14B 跑一轮很吃显卡）
AI_GLOBAL_WINDOW = 3600
AI_GLOBAL_LIMIT = 30
AI_RATE = defaultdict(deque)
AI_RATE_LOCK = threading.Lock()
AI_HITS = deque()

# 中文口语 → 站点名，用来把"游戏/代码/音乐"这类词映射到库里真实存在的站
KEY_HINTS = {
    "游戏": ["Steam Community (User)", "Roblox", "Twitch", "Lichess", "Codeforces"],
    "代码": ["GitHub", "GitLab", "Gitee", "npm", "PyPi", "Hugging Face", "LeetCode", "HackerRank"],
    "开源": ["GitHub", "GitLab", "Gitee", "Hugging Face"],
    "音乐": ["SoundCloud", "Spotify"],
    "视频": ["YouTube", "TikTok", "Vimeo"],
    "图片": ["Flickr", "Instagram", "Behance", "Dribbble", "Pinterest"],
    "摄影": ["Flickr", "Instagram", "Behance"],
    "设计": ["Behance", "Dribbble", "Figma"],
    "社交": ["Twitter", "Reddit", "Telegram", "VK", "Bluesky", "threads", "mastodon.social"],
    "论坛": ["Reddit", "V2EX", "HackerNews", "tumblr"],
    "写作": ["Medium", "Wattpad", "tumblr"],
    "购物": ["Etsy", "kofi", "Patreon"],
    "赞助": ["Patreon", "kofi"],
    "国内": ["V2EX", "豆瓣", "博客园", "雪球", "哔哩哔哩UID", "Gitee", "抖音号", "QQ号", "爱范儿"],
    "中文": ["V2EX", "豆瓣", "博客园", "雪球", "哔哩哔哩UID", "Gitee", "抖音号", "QQ号", "爱范儿"],
    "区块链": ["Keybase"],
    "minecraft": ["Minecraft", "Steam Community (User)", "Roblox", "YouTube"],
    "我的世界": ["Minecraft", "Steam Community (User)", "Roblox", "YouTube"],
    "steam": ["Steam Community (User)", "Steam"],
    "小说": ["Wattpad", "Medium"],
}

AI_SYSTEM = """你是本地 Sherlock 网页里的 OSINT 检索助手，跑在一台离线服务器上。
用户会给你一个问题，同时给你一份从本地站点数据库检索出来的候选站点清单（站点名 / 主页 / 用户页模板），以及最近的查询记录。

规则：
1. 只推荐清单里出现过的站点，禁止编造站点名或 URL；清单没有合适站点时直说没有。
2. 严禁写出清单之外的任何链接、API 地址、子域名——包括你"知道"的官方 API，一律不许出现。
3. 用简体中文回答，先给结论，再给具体步骤；能列清单就列清单。
4. 讲清每个站点能查到什么、为什么适合这次需求，一句话即可，不要凑字数。
5. 你不联网、看不到实时页面，不要声称自己"查到了"具体账号或链接。
6. 涉及隐私、骚扰、人肉时拒绝并提示合法用途。
7. 回答控制在 400 字以内，用 Markdown 列表，不要写大段套话。"""


def ai_conn():
    conn = sqlite3.connect(AI_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def build_ai_db(sites, cn_names):
    """把站点目录灌进 SQLite，AI 检索就走这张表。每次启动重建，保证和 data.json 同步。"""
    conn = ai_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sites(
                name TEXT PRIMARY KEY, url TEXT, url_main TEXT,
                regex TEXT, error_type TEXT, source TEXT);
            CREATE INDEX IF NOT EXISTS idx_sites_name ON sites(name);
            CREATE TABLE IF NOT EXISTS runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, tool TEXT, target TEXT, hits INTEGER);
        """)
        conn.execute("DELETE FROM sites")
        rows = []
        for name, info in sites.items():
            if not isinstance(info, dict):
                continue
            rows.append((name, info.get("url", ""), info.get("urlMain", ""),
                         info.get("regexCheck", ""), info.get("errorType", ""),
                         "cn" if name in cn_names else "intl"))
        conn.executemany(
            "INSERT OR REPLACE INTO sites(name,url,url_main,regex,error_type,source) VALUES(?,?,?,?,?,?)",
            rows)
        conn.commit()
        print("AI 站点库已刷新：%d 条 -> %s" % (len(rows), AI_DB), file=sys.stderr)
    finally:
        conn.close()


def ai_log_run(tool, target, hits=0):
    try:
        conn = ai_conn()
        try:
            conn.execute("INSERT INTO runs(ts,tool,target,hits) VALUES(?,?,?,?)",
                         (time.time(), tool, str(target)[:64], int(hits)))
            conn.execute("DELETE FROM runs WHERE id <= (SELECT MAX(id) FROM runs) - 200")
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def ai_tokens(q):
    """英文按词切，中文切 2 字组合，够用了，不上向量模型。"""
    q = q.lower()
    toks = set(re.findall(r"[a-z0-9][a-z0-9._\-]{1,}", q))
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) <= 2:
            toks.add(seg)
        for i in range(len(seg) - 1):
            toks.add(seg[i:i + 2])
    return toks


def ai_sites(q, limit=16):
    """从 SQLite 站点库里给问题挑候选站点，返回 (站点列表, 命中数)。"""
    toks = ai_tokens(q)
    boost = set()
    for kw, names in KEY_HINTS.items():
        if kw in q:
            boost.update(names)

    conn = ai_conn()
    try:
        rows = conn.execute(
            "SELECT name,url,url_main,regex,source FROM sites").fetchall()
        recent = conn.execute(
            "SELECT tool,target,ts FROM runs ORDER BY id DESC LIMIT 6").fetchall()
    finally:
        conn.close()

    scored = []
    for r in rows:
        name = (r["name"] or "").lower()
        home = (r["url_main"] or "").lower()
        score = 0
        if r["name"] in boost:
            score += 80
        for t in toks:
            if t in name:
                score += 26 if len(t) >= 3 else 12
            elif t in home:
                score += 16
        if score:
            scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    picked = [dict(r) for _, r in scored[:limit]]

    # 召回不到 8 个时拿热门站补齐：让模型手里有真站点可选，别只有一两个就去编
    if len(picked) < 8:
        have = {s["name"] for s in picked}
        for r in rows:
            if len(picked) >= 10:
                break
            if r["name"] in POPULAR and r["name"] not in have:
                picked.append(dict(r))
                have.add(r["name"])
    return picked, len(scored), recent


def ai_strip_unknown_urls(answer, sources):
    """模型爱编 URL：只保留站点库里出现过的域名，其余链接剥成纯文字。"""
    hosts = set()
    for s in sources:
        for key in ("url", "url_main"):
            v = s.get(key) or ""
            if v.startswith("http"):
                hosts.add(urlparse(v).netloc.lower().split(":")[0])

    def repl_md(m):
        label, url = m.group(1), m.group(2)
        return label if urlparse(url).netloc.lower().split(":")[0] not in hosts else m.group(0)

    answer = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", repl_md, answer)

    def repl_bare(m):
        url = m.group(0)
        return url if urlparse(url).netloc.lower().split(":")[0] in hosts else ""

    return re.sub(r"https?://[^\s，。；）)\]]+", repl_bare, answer)


AI_AGENT_SYSTEM = """你是本机的 OSINT 执行助手。系统已经替你真实运行了工具，下面是它的原始输出。

规则：
1. 只依据给出的真实输出下结论，绝不推测输出里没有的账号。
2. 先说结论（命中多少、集中在哪类站），再挑 3-5 个最值得点开的说明理由。
3. 输出里没有的站点、链接一律不许出现，也不要写任何 API 地址。
4. 用简体中文，Markdown 列表，400 字以内，不要客套话。
5. 明显是同名不同人、或站点返回通用页面的，要提醒用户核对。

【给人看的下一步】用一句话说明如果再查一次该换哪个角度（例如换 Maigret 全量、补查邮箱、换更独特的昵称）。"""

AI_STOPWORDS = {
    "sherlock", "maigret", "holehe", "ghunt", "phoneinfoga", "theharvester",
    "harvester", "osint", "username", "user", "id", "api", "ai", "http", "https",
    "com", "www", "the", "and", "for", "with", "help", "please",
}


def ai_plan(q):
    """判断这句话是不是"让我去跑工具"，是的话返回 {tool, target}。"""
    s = q.strip()
    low = s.lower()

    email = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", s)
    if email:
        addr = email.group(0)
        if re.search(r"google|gmail|谷歌", low) and ENABLE_GHUNT:
            return {"tool": "ghunt", "target": addr, "why": "邮箱且提到 Google"}
        return {"tool": "holehe", "target": addr, "why": "识别到邮箱"}

    if re.search(r"手机|号码|电话|phone|号码|\+852|\+86", low):
        m = re.search(r"\+\d[\d\s\-]{5,18}|\b\d{7,15}\b", s)
        if m:
            return {"tool": "phoneinfoga", "target": m.group(0).strip(),
                    "why": "识别到手机号"}

    if re.search(r"域名|子域|资产|备案|网站资产|domain|harvester", low):
        m = re.search(r"[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+){1,}", s)
        if m and not email:
            return {"tool": "harvester", "target": m.group(0).rstrip("."),
                    "why": "识别到域名"}

    # 剩下按"用户名"处理：取最后一个不是站点名/工具名的英文词
    cands = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9._\-]{2,31}", s)
             if t.lower() not in AI_STOPWORDS and t not in SITES
             and not re.fullmatch(r"\d{4,}", t)]
    if cands and re.search(r"查|搜|找|看看|帮我|look|check|search|find|scan", low):
        user = cands[-1]
        scope = "cn" if re.search(r"中文|国内|大陆", s) else "top"
        return {"tool": "sherlock+maigret", "target": user,
                "why": "识别到用户名", "scope": scope}
    return None


def sherlock_once(username, timeout=10):
    """跑一轮热门站点（46 个），给 AI 当工具用。"""
    sites = [s for s in POPULAR if s in SITES]
    cmd = [EXE, username, "-j", MERGED_JSON, "--print-found", "--no-color",
           "--timeout", str(timeout)]
    for s in sites:
        cmd += ["--site", s]

    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=200)
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000

    started = time.time()
    try:
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return [], 0, "Sherlock 运行超时"
    except Exception as exc:  # noqa: BLE001
        return [], 0, "Sherlock 启动失败：%s" % exc

    found, seen = [], set()
    for line in decode_output(proc.stdout).splitlines():
        m = FOUND_RE.match(line.strip())
        if not m:
            continue
        site, url = m.group(1).strip(), m.group(2).strip()
        if site in seen:
            continue
        seen.add(site)
        found.append({"site": site, "url": url})
    return found, round(time.time() - started, 1), ""


def ai_run_tool(plan):
    """真的去跑工具，返回统一结构给模型看。"""
    tool, target = plan["tool"], plan["target"]

    if tool == "sherlock+maigret":
        found, secs, err = sherlock_once(target)
        if err:
            return {"tool": "sherlock", "target": target, "ok": False,
                    "elapsed": secs, "count": 0, "items": [], "digest": err, "error": err}

        scope = plan.get("scope", "top")
        items, total, merr = maigret_search(target, scope=scope, timeout=12)
        digest_parts = ["## Sherlock（热门 %d 站）命中 %d 个" % (len(POPULAR), len(found))]
        digest_parts += ["- %s -> %s" % (i["site"], i["url"]) for i in found[:45]]
        if merr:
            digest_parts.append("\n## Maigret 失败：%s" % merr)
        else:
            digest_parts.append("\n## Maigret（范围 %s，库内 %d 站）命中 %d 个"
                                % (scope, total, len(items)))
            digest_parts += ["- %s -> %s" % (i["site"], i["url"]) for i in items[:45]]
        return {"tool": "sherlock+maigret", "target": target, "ok": True,
                "elapsed": round(secs + 12, 1), "count": len(found) + len(items),
                "items": found + [{"site": i["site"], "url": i["url"]} for i in items],
                "sherlock": len(found), "maigret": len(items),
                "digest": "\n".join(digest_parts)[:4000], "error": ""}

    if tool == "maigret":
        items, total, err = maigret_search(target, scope="top", timeout=12)
        return {"tool": "maigret", "target": target, "ok": not err,
                "elapsed": 0, "count": len(items), "items": items,
                "digest": "\n".join(["- %s -> %s" % (i["site"], i["url"]) for i in items[:60]])
                           or (err or "没有命中"), "error": err}

    # holehe / ghunt / phoneinfoga / harvester 复用 extra_run
    res = extra_run(tool, target)
    text = res["output"]
    hits = []
    if tool == "holehe":
        hits = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[+]")]
        digest = "Holehe 在 %d 个站点找到该邮箱的账号：\n%s" % (
            len(hits), "\n".join(hits[:40]))
    else:
        digest = text[:4000]
    return {"tool": tool, "target": target, "ok": res["ok"], "elapsed": res["elapsed"],
            "count": len(hits) if tool == "holehe" else 0, "items": [],
            "digest": digest or res.get("error", ""), "error": res.get("error", "")}


def ai_agent(q):
    """AI 自己判断 + 自己跑工具 + 自己总结。识别不出目标就返回 None，交给建议模式。"""
    plan = ai_plan(q)
    if not plan:
        return None

    run = ai_run_tool(plan)
    recent = []
    ai_log_run(run["tool"], run["target"], run["count"])

    body = {
        "model": AI_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": AI_AGENT_SYSTEM},
            {"role": "user", "content": "【已执行】%s 查 %s，耗时 %ss\n\n【原始输出】\n%s\n\n【用户原问题】\n%s"
             % (run["tool"], run["target"], run["elapsed"], run["digest"], q)},
        ],
        "options": {"temperature": 0.2, "num_ctx": 4096, "num_predict": 800},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL.rstrip("/") + "/api/chat", data=data,
                                 headers={"Content-Type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    msg = payload.get("message") or {}
    answer = (msg.get("content") or msg.get("thinking") or "").strip()

    sources = []
    for it in run["items"][:12]:
        sources.append({"name": it.get("site"), "url": it.get("url"), "url_main": it.get("url")})

    answer = ai_strip_unknown_urls(answer, sources)

    return {"answer": answer, "run": {k: run[k] for k in ("tool", "target", "ok", "elapsed", "count")},
            "sources": sources, "elapsed": round(time.time() - started, 1)}


def ai_ask(q):
    """把问题 + 站点库上下文交给本地 R1，返回 (answer, sources, elapsed)。"""
    picked, matched, recent = ai_sites(q)

    if picked:
        ctx = "\n".join(
            "- %s | 主页 %s | 用户页 %s" % (s["name"], s["url_main"] or "-", s["url"] or "-")
            for s in picked)
    else:
        ctx = "（这次没有从库里检索到明显相关的站点）"

    if recent:
        runs = "\n".join(
            "- %s 查 %s" % (r["tool"], r["target"]) for r in recent)
    else:
        runs = "（还没有查询记录）"

    user_msg = ("【站点库检索结果（共 %d 个相关，取前 %d）】\n%s\n\n"
                "【最近查询记录】\n%s\n\n【用户问题】\n%s"
                % (matched, len(picked), ctx, runs, q))

    body = {
        "model": AI_MODEL,
        "stream": False,
        "messages": [
            {"role": "system", "content": AI_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "options": {"temperature": 0.2, "num_ctx": 4096, "num_predict": 800},
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL.rstrip("/") + "/api/chat", data=data,
                                 headers={"Content-Type": "application/json"})

    started = time.time()
    with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    msg = payload.get("message") or {}
    answer = (msg.get("content") or "").strip()
    if not answer:
        answer = (msg.get("thinking") or "").strip()
    answer = ai_strip_unknown_urls(answer, picked)
    return answer, picked, round(time.time() - started, 1)


def ai_rate_check(ip):
    now = time.time()
    with AI_RATE_LOCK:
        q = AI_RATE[ip]
        while q and now - q[0] > AI_RATE_WINDOW:
            q.popleft()
        while AI_HITS and now - AI_HITS[0] > AI_GLOBAL_WINDOW:
            AI_HITS.popleft()
        if len(q) >= AI_RATE_LIMIT:
            return False, "AI 查询太频繁（每 10 分钟 %d 次），稍后再试" % AI_RATE_LIMIT
        if len(AI_HITS) >= AI_GLOBAL_LIMIT:
            return False, "全站 AI 查询量已达上限，稍后再试"
        q.append(now)
        AI_HITS.append(now)
        return True, ""


HOST = os.environ.get("SHERLOCK_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("SHERLOCK_WEB_PORT", "8090"))

MAX_CONCURRENT = 2          # 同时最多跑 2 个查询
MAX_QUEUE = 20              # 排队上限
RATE_WINDOW = 600           # 速率窗口：10 分钟
RATE_LIMIT = 12             # 每 IP 每 10 分钟最多 12 次
GLOBAL_WINDOW = 3600        # 全站保护：1 小时
GLOBAL_LIMIT = 80
RUN_TIMEOUT = 300           # 单次查询硬超时
MAX_USERNAME_LEN = 32
MAX_BODY = 8192

# 中文用户名（B站昵称、豆瓣中文 id 等）也要能查，所以放开到 CJK 与常见符号
USERNAME_RE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]"
                         r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af._-]{0,%d}$"
                         % (MAX_USERNAME_LEN - 1))
FOUND_RE = re.compile(r"^\[\+\]\s*([^:]{1,80}):\s*(\S.*?)\s*$")

POPULAR = [
    # 国际
    "GitHub", "Twitter", "Instagram", "TikTok", "Reddit", "YouTube", "Telegram",
    "Pinterest", "Twitch", "Medium", "GitLab", "Docker Hub", "SoundCloud", "Spotify",
    "Snapchat", "tumblr", "VK", "Patreon", "Keybase", "HackerNews", "DEV Community",
    "Behance", "Dribbble", "Flickr", "Vimeo", "Wattpad", "Roblox", "LinkedIn",
    "Bluesky", "threads", "mastodon.social", "Lichess", "Codeforces", "LeetCode",
    "HackerRank", "Hugging Face", "npm", "PyPi", "Steam Community (User)", "kofi",
    # 国内
    "V2EX", "豆瓣", "博客园", "雪球", "爱范儿", "哔哩哔哩UID", "Gitee",
    "抖音号", "QQ号",
]
CN_POPULAR = ["V2EX", "豆瓣", "博客园", "雪球", "爱范儿", "哔哩哔哩UID", "Gitee", "抖音号", "QQ号"]

SITES = {}


def build_sites():
    """把本地上游 data.json 与国内站点 sites_cn.json 合并成 data_merged.json。

    走 -j 参数把这个文件交给 sherlock，改的都是 web 目录下的文件，
    仓库里的 data.json 保持干净，git pull 不会冲突。
    """
    with open(DATA_JSON, encoding="utf-8") as f:
        base = json.load(f)
    base.pop("$schema", None)
    merged = {k: v for k, v in base.items() if isinstance(v, dict) and "urlMain" in v}

    if os.path.isfile(CN_SITES_JSON):
        try:
            with open(CN_SITES_JSON, encoding="utf-8") as f:
                cn = json.load(f)
            added = 0
            for name, info in cn.items():
                if isinstance(info, dict) and "urlMain" in info and "url" in info:
                    merged[name] = info
                    added += 1
            print("已合并国内站点 %d 个" % added, file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print("读取 sites_cn.json 失败：%s" % exc, file=sys.stderr)

    with open(MERGED_JSON, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1, sort_keys=True)
    return merged


JOBS = {}
JOBS_LOCK = threading.Lock()
QUEUE = deque()
QUEUE_COND = threading.Condition()
RATE = defaultdict(deque)
RATE_LOCK = threading.Lock()
GLOBAL_HITS = deque()


def client_ip(handler):
    """cloudflared 会把真实来源放在 CF-Connecting-IP 里。只在本地连接时才信任。"""
    peer = handler.client_address[0]
    if peer in ("127.0.0.1", "::1"):
        for h in ("CF-Connecting-IP", "True-Client-IP"):
            v = handler.headers.get(h)
            if v:
                return v.strip()
    return peer


def rate_check(ip):
    now = time.time()
    with RATE_LOCK:
        q = RATE[ip]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        while GLOBAL_HITS and now - GLOBAL_HITS[0] > GLOBAL_WINDOW:
            GLOBAL_HITS.popleft()
        if len(q) >= RATE_LIMIT:
            return False, "该 IP 查询过于频繁，请稍后再试（每 10 分钟上限 %d 次）" % RATE_LIMIT
        if len(GLOBAL_HITS) >= GLOBAL_LIMIT:
            return False, "全站查询量已达上限，请稍后再试"
        q.append(now)
        GLOBAL_HITS.append(now)
        return True, ""


def decode_output(raw):
    """把子进程的字节流解成文本。

    sherlock 的 stdout 编码取决于启动时的控制台代码页：直接跑 python 时是
    utf-8，被 bat / 计划任务拉起时可能是 cp936。逐行尝试，两种都试，
    哪个解码后不含替换字符就用哪个。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        if "\ufffd" not in text:
            return text
    return raw.decode("utf-8", "replace")


def run_job(job):
    cmd = [EXE, job["username"], "-j", MERGED_JSON,
           "--print-found", "--no-color", "--timeout", str(job["timeout"])]
    for s in job["sites"]:
        cmd += ["--site", s]
    if job["nsfw"]:
        cmd.append("--nsfw")

    # 注意：不能给 subprocess 传 encoding="utf-8"，Windows 下 sherlock 的
    # stdout 走的是本地代码页（cp936），硬按 utf-8 解会把中文站点名变成乱码。
    # 所以这里只收字节，再按 utf-8 / gbk 依次尝试解码。
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=SHERLOCK_DIR,
                  timeout=RUN_TIMEOUT)
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    started = time.time()
    try:
        proc = subprocess.run(cmd, **kwargs)
        elapsed = round(time.time() - started, 2)
        found = []
        seen = set()
        for line in decode_output(proc.stdout).splitlines():
            m = FOUND_RE.match(line.strip())
            if not m:
                continue
            site, url = m.group(1).strip(), m.group(2).strip()
            key = (site, url)
            if key in seen:
                continue
            seen.add(key)
            found.append({"site": site, "url": url})
        with JOBS_LOCK:
            job["found"] = found
            job["count"] = len(found)
            job["elapsed"] = elapsed
            job["finished"] = time.time()
            if not found and proc.returncode not in (0, None):
                job["status"] = "error"
                job["error"] = (decode_output(proc.stderr).strip() or "sherlock 执行失败")[-400:]
            else:
                job["status"] = "done"
    except subprocess.TimeoutExpired:
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = "查询超时（%d 秒），请减少站点或提高超时值" % RUN_TIMEOUT
            job["finished"] = time.time()
    except Exception as exc:  # noqa: BLE001
        with JOBS_LOCK:
            job["status"] = "error"
            job["error"] = "启动 sherlock 失败：%s" % exc
            job["finished"] = time.time()


def worker():
    while True:
        with QUEUE_COND:
            while not QUEUE:
                QUEUE_COND.wait()
            job = QUEUE.popleft()
        with JOBS_LOCK:
            job["status"] = "running"
            job["started"] = time.time()
        run_job(job)


def cn_name_search(keyword, limit=12):
    """按中文昵称在 B 站搜用户，补上“按名字找 ID”这一环。

    Sherlock 只能验证「某个 ID 是否存在」，中文昵称通常不是 URL 里的 id，
    所以先搜昵称拿到 mid / 空间链接，再用 Sherlock 验证该 UID。
    只用标准库；B 站接口需要先取 buvid3 cookie，否则返回 412。
    """
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    try:
        opener.open(urllib.request.Request("https://www.bilibili.com/",
                                           headers={"User-Agent": ua}), timeout=10).read()
    except Exception:
        pass  # 拿不到 cookie 也继续试，接口可能仍可用

    query = urllib.parse.urlencode({"search_type": "bili_user", "keyword": keyword, "page": 1})
    url = "https://api.bilibili.com/x/web-interface/search/type?" + query
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Referer": "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(keyword),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip",
    })

    try:
        with opener.open(req, timeout=12) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            data = json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "B 站接口返回 HTTP %s（可能被风控，稍后再试）" % exc.code, "items": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "搜索失败：%s" % exc, "items": []}

    if data.get("code") != 0:
        return {"ok": False, "error": "B 站接口返回：%s" % data.get("message", "未知错误"), "items": []}

    items = []
    for row in ((data.get("data") or {}).get("result") or [])[:limit]:
        mid = str(row.get("mid") or "").strip()
        if not mid.isdigit():
            continue
        name = re.sub(r"<[^>]+>", "", str(row.get("uname") or "")).strip()
        items.append({
            "name": name,
            "uid": mid,
            "fans": row.get("fans"),
            "sign": re.sub(r"<[^>]+>", "", str(row.get("usign") or "")).strip()[:80],
            "url": "https://space.bilibili.com/%s" % mid,
        })
    return {"ok": True, "items": items}


# ---------------- 国内平台直查：微博 / 抖音 / QQ / 微信 ----------------
# 这三个平台在免登录下的判定方式各不相同，且都不能直接塞进 Sherlock：
#  · 微博  profile/info 必须带 visitor cookie，否则一律返回 ok:-100（要登录）
#  · 抖音  unique_id 接口对纯数字会按 uid / 短 ID 兜底命中，必须回比字段
#  · QQ    user.qzone.qq.com 对不存在的号返回“找不回来了”404 页
PLAT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_WEIBO_LOCK = threading.Lock()
_WEIBO_STATE = {"opener": None, "born": 0.0}
WEIBO_COOKIE_TTL = 900          # visitor cookie 大约能撑 15 分钟


def _weibo_opener():
    """返回带 visitor cookie 的 opener；cookie 过期会自动重建。

    微博 ajax 接口无 cookie 时固定返回 {"ok":-100,"url":".../login.php"}，
    所以必须先走 passport.weibo.com 的 genvisitor2 握手拿 SUB / SUBP。
    """
    with _WEIBO_LOCK:
        now = time.time()
        op = _WEIBO_STATE["opener"]
        if op is not None and now - _WEIBO_STATE["born"] < WEIBO_COOKIE_TTL:
            return op

        cj = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        try:
            body = op.open(urllib.request.Request(
                "https://passport.weibo.com/visitor/genvisitor2"
                "?cb=visitor_gray_callback&tid=&from=weibo",
                headers={"User-Agent": PLAT_UA}), timeout=10).read().decode("utf-8", "replace")
            m = re.search(r"visitor_gray_callback\((.*)\)", body, re.S)
            d = json.loads(m.group(1))["data"]
            for dom in ("weibo.com", "weibo.cn"):
                for name, val in (("SUB", d["sub"]), ("SUBP", d["subp"])):
                    cj.set_cookie(http.cookiejar.Cookie(
                        0, name, val, None, False, dom, False, False,
                        "/", True, False, None, False, None, None, {}))
        except Exception:  # noqa: BLE001
            pass  # 握手失败也让后续请求跑，调用方会看到“要求登录”的报错

        _WEIBO_STATE["opener"] = op
        _WEIBO_STATE["born"] = now
        return op


def _plat_json(url, referer, opener=None, timeout=12):
    """统一的 JSON 拉取：带 gzip 处理，返回 (dict, 错误串)。"""
    try:
        op = opener or urllib.request.build_opener()
        req = urllib.request.Request(url, headers={
            "User-Agent": PLAT_UA,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "gzip",
        })
        with op.open(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace")), None
    except urllib.error.HTTPError as exc:
        return None, "接口返回 HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001
        return None, "请求失败：%s" % exc


def weibo_profile(uid):
    """按 UID 查微博资料。返回 (items, error)。"""
    data, err = _plat_json("https://weibo.com/ajax/profile/info?uid=%s" % uid,
                           "https://weibo.com/u/%s" % uid,
                           opener=_weibo_opener())
    if err:
        return None, err
    if data.get("ok") == -100:
        return None, "微博要求登录（visitor cookie 握手失败或被风控），稍后再试"
    user = (data.get("data") or {}).get("user") or {}
    if not user.get("screen_name"):
        return [], None
    uid_str = str(user.get("idstr") or user.get("id") or uid)
    fans = user.get("followers_count")
    return [{
        "name": user.get("screen_name"),
        "uid": uid_str,
        "fans": fans,
        "sign": (user.get("verified_reason") or user.get("description") or "").strip()[:90],
        "url": "https://weibo.com/u/%s" % uid_str,
        "verified": bool(user.get("verified") or user.get("verified_reason")),
        "via": "UID",
    }], None


def weibo_search(keyword, limit=12):
    """按昵称搜微博用户。side/search 不登录也能用。"""
    data, err = _plat_json("https://weibo.com/ajax/side/search?q="
                           + urllib.parse.quote(keyword),
                           "https://weibo.com/", opener=_weibo_opener())
    if err:
        return None, err
    if data.get("ok") not in (1, None) and not data.get("data"):
        return None, "微博搜索失败：%s" % (data.get("message") or data.get("msg") or "未知原因")
    users = (data.get("data") or {}).get("users") or []
    items = []
    for u in users[:limit]:
        uid = str(u.get("id") or u.get("idstr") or "").strip()
        if not uid:
            continue
        items.append({
            "name": re.sub(r"<[^>]+>", "", str(u.get("screen_name") or "")).strip(),
            "uid": uid,
            "fans": u.get("followers_count"),
            "sign": (u.get("verified_reason") or u.get("description") or "").strip()[:90],
            "url": "https://weibo.com/u/%s" % uid,
            "verified": bool(u.get("verified") or u.get("verified_reason")),
            "via": "昵称",
        })
    return items, None


def douyin_lookup(query):
    """按抖音号查用户。返回 (items, error)。

    这个接口对纯数字输入会退化成按 uid / 短 ID 匹配（例如随便一个 9 位数
    也可能命中别人的账号），所以必须把返回的 unique_id / short_id 和输入
    回比，对不上的一律丢弃，否则就是误报。
    """
    data, err = _plat_json("https://www.iesdouyin.com/web/api/v2/user/info/?unique_id="
                           + urllib.parse.quote(query),
                           "https://www.douyin.com/")
    if err:
        return None, err
    info = data.get("user_info") or {}
    if data.get("status_code") != 0 or not info.get("nickname"):
        return [], None

    q = query.lower().lstrip("@")
    got_uid = str(info.get("unique_id") or "").lower()
    got_short = str(info.get("short_id") or "")
    if got_uid == q:
        via = "抖音号"
    elif got_short == q.lstrip("0") or got_short == q:
        via = "短 ID"
    else:
        return [], None  # 命中的是别人的 uid，不算

    sec = str(info.get("sec_uid") or "")
    url = ("https://www.douyin.com/user/%s" % sec) if sec else "https://www.douyin.com/"
    avatar = ((info.get("avatar_thumb") or {}).get("url_list") or [""])[0]
    return [{
        "name": info.get("nickname"),
        "uid": info.get("unique_id") or got_short or query,
        "fans": info.get("mplatform_followers_count"),
        "sign": re.sub(r"\s+", " ", str(info.get("signature") or "")).strip()[:90],
        "url": url,
        "verified": bool(info.get("custom_verify") or info.get("enterprise_verify_reason")),
        "via": via,
        "avatar": avatar,
    }], None


QQ_RE = re.compile(r"^[1-9][0-9]{4,10}$")


def qq_lookup(number):
    """按 QQ 号判断是否注册：未注册的号会落到“找不回来了”404 页。"""
    if not QQ_RE.match(number):
        return None, "QQ 号应为 5-11 位数字，且不以 0 开头"
    url = "https://user.qzone.qq.com/%s" % number
    try:
        req = urllib.request.Request(url, headers={"User-Agent": PLAT_UA})
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return [], None
        return None, "QQ 空间返回 HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001
        return None, "请求失败：%s" % exc

    if "找不回来了" in body:
        return [], None
    return [{
        "name": "QQ %s" % number,
        "uid": number,
        "fans": None,
        "sign": "该 QQ 号已注册（QQ 空间页面可访问）",
        "url": url,
        "verified": False,
        "via": "QQ 号",
        "avatar": "https://q1.qlogo.cn/g?b=qq&nk=%s&s=100" % number,
    }], None


def wechat_search(keyword, limit=12):
    """微信只做“公众号文章反查”：按关键词搜公众号文章，列出发文账号。

    个人微信号没有任何公开 Web 主页，任何接口都查不到“这个微信号是否存在”，
    所以这里不假装能查号，只回答“哪些公众号发过相关内容”。
    搜狗的公众号搜索页（type=1）已经返回“暂无”，文章搜索（type=2）还能用。
    """
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        op.open(urllib.request.Request("https://weixin.sogou.com/",
                                       headers={"User-Agent": PLAT_UA}), timeout=10).read()
    except Exception:  # noqa: BLE001
        pass  # 拿不到 cookie 也继续试

    qs = urllib.parse.urlencode({"type": 2, "query": keyword, "ie": "utf8"})
    url = "https://weixin.sogou.com/weixin?" + qs
    try:
        with op.open(urllib.request.Request(url, headers={
                "User-Agent": PLAT_UA,
                "Referer": "https://weixin.sogou.com/",
                "Accept-Language": "zh-CN,zh;q=0.9"}), timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, "搜狗微信请求失败：%s" % exc

    if "请输入验证码" in body or "antispider" in body:
        return None, "搜狗微信要求验证码，暂时查不了，稍后再试"

    starts = [m.start() for m in re.finditer(r'<div class="txt-box">', body)]
    items, seen = [], set()
    for i, st in enumerate(starts):
        seg = body[st:starts[i + 1] if i + 1 < len(starts) else st + 3000]
        m_title = re.search(r"<h3>(.*?)</h3>", seg, re.S)
        m_acct = re.search(r'<span class="all-time-y2">(.*?)</span>', seg, re.S)
        if not (m_title and m_acct):
            continue
        name = re.sub(r"<[^>]+>|\s+", "", m_acct.group(1)).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        title = re.sub(r"<!--.*?-->", "", m_title.group(1))
        title = re.sub(r"<[^>]+>|\s+", "", title).strip()
        m_href = re.search(r'<a[^>]+href="(/link\?url=[^"]+)"', seg)
        if m_href:
            link = "https://weixin.sogou.com" + m_href.group(1).replace("&amp;", "&")
        else:
            link = "https://weixin.sogou.com/weixin?type=1&query=" + urllib.parse.quote(name)
        items.append({
            "name": name,
            "uid": "",
            "fans": None,
            "sign": title[:90],
            "url": link,
            "verified": False,
            "via": "公众号",
        })
        if len(items) >= limit:
            break
    return items, None


MAIGRET_FOUND_RE = re.compile(r"^\[\+\]\s+(?!MAIGRET|Using rules|Using sites)([^:]{1,60}):\s+(https?://\S+)$")


def maigret_search(username, scope="cn", timeout=15):
    """用 Maigret 再扫一遍，补上 Sherlock 站点库里没有的站。

    返回 (items, total, error)，items 为 [{"site", "url"}]。
    注意：Maigret 的判定比 Sherlock 宽松，结果里可能混有误报，前端必须标注。
    """
    if not os.path.isfile(MAIGRET_EXE):
        return [], 0, "找不到 Maigret（%s），可用环境变量 MAIGRET_EXE 指向它" % MAIGRET_EXE

    cmd = [MAIGRET_EXE, username, "--no-progressbar", "--no-recursion",
           "--no-color", "--timeout", str(timeout), "--no-autoupdate", "-n", "30"]
    if scope == "cn":
        cmd += ["--tags", "cn"]
    elif scope == "top":
        cmd += ["--top-sites", "200"]
    else:
        cmd.append("-a")

    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=MAIGRET_TIMEOUT)
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

    try:
        proc = subprocess.run(cmd, env=env, **kwargs)
    except subprocess.TimeoutExpired:
        return [], 0, "Maigret 运行超时（%d 秒），换个小一点的范围再试" % MAIGRET_TIMEOUT
    except Exception as exc:  # noqa: BLE001
        return [], 0, "Maigret 启动失败：%s" % exc

    text = decode_output((proc.stdout or b"") + (proc.stderr or b""))
    items, total, seen = [], 0, set()
    for line in text.splitlines():
        m = re.search(r"on top (\d+) sites", line)
        if m:
            total = int(m.group(1))
            continue
        m = MAIGRET_FOUND_RE.match(line.strip())
        if not m:
            continue
        site, url = m.group(1).strip(), m.group(2).strip()
        if site in seen:
            continue
        seen.add(site)
        items.append({"site": site, "url": url})

    if not items and proc.returncode not in (0, 1):
        return [], total, "Maigret 返回码 %s" % proc.returncode
    return items, total, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "SherlockWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, obj)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, encoding="utf-8") as f:
                    html = f.read()
            except FileNotFoundError:
                self._send(500, "index.html 缺失", "text/plain; charset=utf-8")
                return
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/api/sites":
            popular = [s for s in POPULAR if s in SITES]
            self._json(200, {"count": len(SITES), "popular": popular, "all": sorted(SITES)})
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        if path.startswith("/api/job/"):
            jid = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                job = JOBS.get(jid)
                if job is None:
                    self._json(404, {"error": "没有这个任务"})
                    return
                snap = {k: job[k] for k in ("id", "username", "status", "count", "elapsed",
                                            "found", "error", "sites", "timeout", "nsfw") if k in job}
            self._json(200, snap)
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in ("/api/search", "/api/cn-search", "/api/plat-search",
                        "/api/maigret", "/api/extra", "/api/ai"):
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "Content-Length 无效"})
            return
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "请求体为空或过大"})
            return

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"error": "请求体不是合法 JSON"})
            return

        if path == "/api/ai":
            q = str(payload.get("q", "")).strip()
            if not (2 <= len(q) <= 300) or any(ord(c) < 32 for c in q):
                self._json(400, {"error": "问题需 2-300 个字符，不能换行"})
                return
            ok, msg = ai_rate_check(client_ip(self))
            if not ok:
                self._json(429, {"error": msg})
                return
            try:
                agent = ai_agent(q)
                if agent:
                    self._json(200, {"q": q, "mode": "tool", "answer": agent["answer"],
                                     "run": agent["run"], "elapsed": agent["elapsed"],
                                     "model": AI_MODEL, "sources": agent["sources"]})
                    return
                answer, sources, elapsed = ai_ask(q)
            except urllib.error.URLError as exc:
                self._json(502, {"error": "连不上本地 Ollama（%s）：先确认 ollama 在跑" % exc.reason})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"error": "本地模型调用失败：%s" % exc})
                return
            ai_log_run("ai", q[:60], len(sources))
            self._json(200, {"q": q, "mode": "advice", "answer": answer, "elapsed": elapsed,
                             "model": AI_MODEL, "sources": sources})
            return

        if path == "/api/extra":
            tool = str(payload.get("tool", "")).strip().lower()
            target = str(payload.get("target", "")).strip()
            if tool not in EXTRA_TOOLS:
                self._json(400, {"error": "tool 只支持 " + " / ".join(EXTRA_TOOLS)})
                return
            if not (1 <= len(target) <= 64) or any(ord(c) < 32 for c in target):
                self._json(400, {"error": "目标需为 1-64 个字符，且不能含控制字符"})
                return
            if tool in ("holehe", "ghunt") and not EMAIL_RE.match(target):
                self._json(400, {"error": "这里需要合法的邮箱地址"})
                return
            if tool == "harvester" and not DOMAIN_RE.match(target):
                self._json(400, {"error": "这里需要合法域名，例如 example.com"})
                return
            if tool == "phoneinfoga" and not 6 <= len(re.sub(r"[^\d]", "", target)) <= 15:
                self._json(400, {"error": "手机号需 6-15 位数字，建议带国家码，如 +852"})
                return

            ok, msg = rate_check(client_ip(self))
            if not ok:
                self._json(429, {"error": msg})
                return

            with EXTRA_LOCK:
                if tool in EXTRA_RUNNING:
                    self._json(429, {"error": "%s 正在运行，等这次跑完再试" % tool})
                    return
                EXTRA_RUNNING.add(tool)
            try:
                result = extra_run(tool, target)
            finally:
                with EXTRA_LOCK:
                    EXTRA_RUNNING.discard(tool)

            code = 200 if result["ok"] else 502
            ai_log_run(tool, target)
            self._json(code, {"tool": tool, "target": target, **result})
            return

        if path == "/api/maigret":
            username = str(payload.get("username", "")).strip()
            if not USERNAME_RE.match(username):
                self._json(400, {"error": "用户名 1-%d 位，可用中英文、数字和 . _ -" % MAX_USERNAME_LEN})
                return
            scope = str(payload.get("scope", "cn")).strip().lower()
            if scope not in ("cn", "top", "all"):
                self._json(400, {"error": "scope 只支持 cn / top / all"})
                return
            try:
                timeout = int(payload.get("timeout", 15))
            except (TypeError, ValueError):
                self._json(400, {"error": "timeout 必须是整数"})
                return
            timeout = max(5, min(timeout, 60))

            ok, msg = rate_check(client_ip(self))
            if not ok:
                self._json(429, {"error": msg})
                return
            items, total, err = maigret_search(username, scope, timeout)
            if err:
                self._json(502, {"error": err})
                return
            ai_log_run("maigret", username, len(items))
            self._json(200, {"username": username, "scope": scope,
                             "total": total, "items": items})
            return

        if path == "/api/cn-search":
            keyword = str(payload.get("keyword", "")).strip()
            if not (1 <= len(keyword) <= MAX_USERNAME_LEN) or any(ord(c) < 32 for c in keyword):
                self._json(400, {"error": "关键词需为 1-%d 个字符，且不能含控制字符" % MAX_USERNAME_LEN})
                return
            ok, msg = rate_check(client_ip(self))
            if not ok:
                self._json(429, {"error": msg})
                return
            result = cn_name_search(keyword)
            if not result["ok"]:
                self._json(502, {"error": result["error"]})
                return
            self._json(200, {"keyword": keyword, "platform": "哔哩哔哩", "items": result["items"]})
            return

        if path == "/api/plat-search":
            platform = str(payload.get("platform", "")).strip().lower()
            query = str(payload.get("query", "")).strip().lstrip("@")
            if not (1 <= len(query) <= MAX_USERNAME_LEN) or any(ord(c) < 32 for c in query):
                self._json(400, {"error": "查询内容需为 1-%d 个字符，且不能含控制字符" % MAX_USERNAME_LEN})
                return
            handlers = {"weibo": None, "douyin": None, "qq": None, "wechat": None}
            if platform not in handlers:
                self._json(400, {"error": "platform 只支持 weibo / douyin / qq / wechat"})
                return
            ok, msg = rate_check(client_ip(self))
            if not ok:
                self._json(429, {"error": msg})
                return

            if platform == "weibo":
                # 纯数字按 UID 查资料，否则按昵称搜人
                if query.isdigit() and len(query) <= 12:
                    items, err = weibo_profile(query)
                    mode = "UID"
                else:
                    items, err = weibo_search(query)
                    mode = "昵称"
            elif platform == "douyin":
                items, err = douyin_lookup(query)
                mode = "抖音号"
            elif platform == "wechat":
                items, err = wechat_search(query)
                mode = "公众号文章"
            else:
                items, err = qq_lookup(query)
                mode = "QQ 号"

            if err:
                self._json(502, {"error": err})
                return
            label = {"weibo": "微博", "douyin": "抖音", "qq": "QQ", "wechat": "微信"}[platform]
            self._json(200, {"platform": label, "mode": mode, "query": query,
                             "items": items or []})
            return

        username = str(payload.get("username", "")).strip()
        if not USERNAME_RE.match(username):
            self._json(400, {"error": "用户名 1-%d 位，可用中英文、数字和 . _ -" % MAX_USERNAME_LEN})
            return

        try:
            timeout = int(payload.get("timeout", 15))
        except (TypeError, ValueError):
            self._json(400, {"error": "timeout 必须是整数"})
            return
        timeout = max(3, min(timeout, 60))

        nsfw = bool(payload.get("nsfw", False))

        raw_sites = payload.get("sites") or []
        if isinstance(raw_sites, str):
            raw_sites = [s.strip() for s in raw_sites.split(",") if s.strip()]
        if not isinstance(raw_sites, list):
            self._json(400, {"error": "sites 必须是数组"})
            return
        sites = []
        for s in raw_sites:
            s = str(s).strip()
            if not s:
                continue
            if s not in SITES:
                self._json(400, {"error": "未知站点：%s" % s})
                return
            if s not in sites:
                sites.append(s)
        if len(sites) > 200:
            self._json(400, {"error": "一次最多指定 200 个站点"})
            return

        ok, msg = rate_check(client_ip(self))
        if not ok:
            self._json(429, {"error": msg})
            return

        with QUEUE_COND:
            if len(QUEUE) >= MAX_QUEUE:
                self._json(503, {"error": "排队人数过多，请稍后再试"})
                return

        job = {
            "id": uuid.uuid4().hex[:12],
            "username": username,
            "sites": sites,
            "timeout": timeout,
            "nsfw": nsfw,
            "status": "queued",
            "count": 0,
            "elapsed": 0,
            "found": [],
            "created": time.time(),
        }
        with JOBS_LOCK:
            # 只保留最近 200 个任务，防止内存膨胀
            if len(JOBS) > 200:
                for k in sorted(JOBS, key=lambda x: JOBS[x]["created"])[:50]:
                    JOBS.pop(k, None)
            JOBS[job["id"]] = job

        with QUEUE_COND:
            QUEUE.append(job)
            QUEUE_COND.notify()

        ai_log_run("sherlock", username)
        self._json(200, {"id": job["id"], "status": "queued", "sites": len(sites) or len(SITES)})


def main():
    if not os.path.isfile(EXE):
        print("找不到 sherlock.exe：%s" % EXE, file=sys.stderr)
        return 1
    SITES.update(build_sites())
    print("已加载 %d 个站点（含国内站点）" % len(SITES), file=sys.stderr)

    try:
        cn_names = set()
        if os.path.isfile(CN_SITES_JSON):
            with open(CN_SITES_JSON, encoding="utf-8") as f:
                cn_names = set(json.load(f).keys())
        build_ai_db(SITES, cn_names)
    except Exception as exc:  # noqa: BLE001
        print("AI 站点库初始化失败（不影响其它功能）：%s" % exc, file=sys.stderr)

    for _ in range(MAX_CONCURRENT):
        threading.Thread(target=worker, daemon=True).start()

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("Sherlock Web 已启动：http://%s:%d" % (HOST, PORT), file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("收到中断，退出", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
