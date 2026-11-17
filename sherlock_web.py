#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sherlock Web —— 把 D:\\Sherlock 里的 sherlock CLI 包装成一个网页服务。

只用 Python 标准库，不额外安装依赖。
启动：python sherlock_web.py   （默认监听 127.0.0.1:8090）
"""

import gzip
import http.cookiejar
import json
import os
import re
import shutil
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
MAIGRET_EXE = os.environ.get("MAIGRET_EXE") or r"D:\Maigret\.venv\Scripts\maigret.exe"
MAIGRET_TIMEOUT = 300


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
        if path not in ("/api/search", "/api/cn-search", "/api/plat-search", "/api/maigret"):
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

        self._json(200, {"id": job["id"], "status": "queued", "sites": len(sites) or len(SITES)})


def main():
    if not os.path.isfile(EXE):
        print("找不到 sherlock.exe：%s" % EXE, file=sys.stderr)
        return 1
    SITES.update(build_sites())
    print("已加载 %d 个站点（含国内站点）" % len(SITES), file=sys.stderr)

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
