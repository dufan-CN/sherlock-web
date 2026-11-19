# Sherlock Web · 中文增强版（非官方）

> **English**: An unofficial web front-end for [sherlock-project/sherlock](https://github.com/sherlock-project/sherlock).
> It adds a browser UI, a set of Chinese sites, Chinese-nickname search, and direct lookups for
> Weibo / Douyin / QQ / WeChat. This project is **not** affiliated with, endorsed by, or maintained
> by the upstream Sherlock project. Upstream code is not redistributed here — install it yourself,
> then drop this repo in as its `web/` directory.

把 Sherlock 这个命令行工具（输入一个用户名，逐站检查它有没有公开主页）包成一个网页版：

- 浏览器里填个名字就能查，不用记命令行参数；
- 加了 8 个国内站点，中文用户名也能查；
- 中文昵称可以先搜人（B 站搜索），拿到 ID 再用 Sherlock 验证；
- 微博 / 抖音号 / QQ 号 / 微信 有专门的一栏直查；
- 接了第二个引擎 Maigret（站点库 5897 个，是 Sherlock 的分支），可按国家/类别标签扫；
- 邮箱 / 手机号 / 域名 可以接着交给 Holehe、GHunt、PhoneInfoga、theHarvester 深挖；
- 接了本地 Ollama 大模型：直接用中文提问，它会自己挑工具查完再给结论（含调用轨迹与来源）。

> ### ⚠️ AI 那一栏的模型，跑在**你自己的电脑**上，不是作者的
>
> 仓库里**没有模型权重**（模型动辄 9-10 GB），也**没有任何云端 API 密钥**，更没有作者的公网地址。
> 代码里的默认值是 `OLLAMA_URL = http://127.0.0.1:11434`——`127.0.0.1` 是回环地址，
> 在谁的机器上运行，指的就是**谁本机的 Ollama**。
>
> - 你不装 Ollama → AI 搜索直接返回 `502`，其余八块功能照常；
> - 你装了 → 算力花在**你自己的显卡**上，跟本仓库作者无关；
> - 作者不提供任何托管模型服务，也不承担你的推理开销。
>
> 具体三步见第 2 节「可选：让 AI 搜索能用」。

只用 Python 标准库，不需要额外装依赖（AI 搜索除外：它需要一个本机 Ollama 服务，见第 2 节）。

---

## 1. 这不是官方项目，改了哪些东西

上游只提供 CLI。本仓库的全部内容就是**外面那层网页**，上游代码一行没改，也不需要改：

| 类型 | 内容 |
| --- | --- |
| 新增 | `sherlock_web.py` —— 标准库 HTTP 服务，把 sherlock CLI 包成异步任务 + JSON 接口 |
| 新增 | `index.html` —— 单页前端，九块卡片（全站查询 / AI 搜索 / 中文昵称 / 国内平台直查 / Maigret / Holehe / GHunt / PhoneInfoga / theHarvester） |
| 新增 | `sites_cn.json` —— 8 个国内站点定义，启动时与上游 `data.json` 合并成 `data_merged.json` |
| 新增 | 微博 / 抖音 / QQ / 微信 的直查逻辑（`/api/plat-search`） |
| 新增 | 中文昵称搜索（`/api/cn-search`，走 B 站公开搜索接口） |
| 新增 | Maigret 第二引擎（`/api/maigret`，站点库 5897 个，可按标签筛） |
| 新增 | 扩展工具调用（`/api/extra`）：Holehe / GHunt / PhoneInfoga / theHarvester，按输入类型逐个校验（邮箱、域名、号码格式错了直接拒） |
| 新增 | 本地大模型助手（`/api/ai`）：接 Ollama，用中文提问，可自动挑检索工具跑完再作答，返回调用轨迹与来源 |
| 新增 | 限流、并发上限、排队、超时、`CF-Connecting-IP` 识别 |
| 改动 | 无。上游 `data.json`、`sherlock.py` 都不动，`git pull` 不会冲突 |

上游的站点数据仍然决定「全站扫描」能查多少个站（本机实测 489 个 = 上游 481 + 本仓库 8）。

---

## 2. 快速开始

### 前置：先装上游

```bash
git clone https://github.com/sherlock-project/sherlock.git
cd sherlock
python -m venv .venv
# Windows
.venv\Scripts\pip install -r requirements.txt
# Linux / macOS
.venv/bin/pip install -r requirements.txt
```

### 再把这个仓库放进 `sherlock/web`

```bash
# 假设上游 clone 在 D:\Sherlock
git clone https://github.com/<你的账号>/sherlock-web.git D:\Sherlock\web
```

目录应该长这样：

```
D:\Sherlock\
├─ sherlock_project\        # 上游
├─ requirements.txt         # 上游
└─ web\                     # 本仓库
   ├─ sherlock_web.py
   ├─ index.html
   ├─ sites_cn.json
   └─ run_web.bat
```

### 启动

```bash
python D:\Sherlock\web\sherlock_web.py
# 打开 http://127.0.0.1:8090
```

或者用附带的重启守护脚本：`D:\Sherlock\web\run_web.bat`（崩溃自动重启，日志写 `service.log`）。

首次启动会自动生成 `data_merged.json`（已合并国内站点），之后改 `sites_cn.json` 需要重启服务才会生效。

### 可选：让 AI 搜索能用（模型在你自己机器上）

AI 搜索**不是**调某个公共 API，也**不会**连作者的机器。它只连 `OLLAMA_URL`，默认
`http://127.0.0.1:11434`，也就是**你自己电脑上的 Ollama**。不装就是这一栏不可用，其余八块照常。

```bash
# 1) 装 Ollama：https://ollama.com/download （Windows / macOS / Linux 都有）
# 2) 拉模型：deepseek-r1:14b 约 9 GB；显存小于 12 GB 建议换成 qwen2.5:7b
ollama pull deepseek-r1:14b
# 3) 确认它在跑
ollama list
curl http://127.0.0.1:11434/api/tags
```

拉完照常启动网页服务就行。想换模型、换到局域网另一台机器上的模型：

```bash
# Windows（写在 run_web.bat 里，或启动前 set）
set AI_MODEL=qwen2.5:7b
set OLLAMA_URL=http://192.168.1.50:11434

# Linux / macOS
export AI_MODEL=qwen2.5:7b
export OLLAMA_URL=http://192.168.1.50:11434
```

把 `OLLAMA_URL` 指向局域网另一台机器时，**对方那台**要设 `OLLAMA_HOST=0.0.0.0`，否则只监听它自己的回环。

> 换句话说：这个仓库**不附带模型、不附带算力、不附带密钥**。谁的机器跑，烧谁的显卡。

### Linux / macOS

```bash
git clone https://github.com/<你的账号>/sherlock-web.git ./web
.venv/bin/python web/sherlock_web.py
```

脚本会自己找 `../.venv/bin/sherlock`，找不到就退回 `PATH` 里的 `sherlock`。

---

## 3. 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SHERLOCK_HOME` | 脚本所在目录的上一级 | 上游 Sherlock 仓库根目录 |
| `SHERLOCK_EXE` | 自动查找 | `sherlock` 可执行文件路径 |
| `MAIGRET_EXE` | 空 | Maigret 可执行文件路径；不设置或找不到时这一栏提示「找不到 Maigret」，其余功能不受影响 |
| `OSINT_HUB` | 脚本同级 `osint-hub/` | Holehe / GHunt / PhoneInfoga / theHarvester 所在环境目录 |
| `SHERLOCK_GHUNT` | `0` | 设为 `1` 才启用 GHunt（会用你本机的 Google 登录态，默认关闭） |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | 本地 Ollama 服务地址 |
| `AI_MODEL` | `deepseek-r1:14b` | AI 搜索用的模型名 |
| `AI_DB` | 脚本同级 `ai.db` | AI 会话缓存库（自动创建，不该提交到仓库） |
| `SHERLOCK_WEB_HOST` | `127.0.0.1` | 改成 `0.0.0.0` 局域网/公网才能访问 |
| `SHERLOCK_WEB_PORT` | `8090` | 端口 |

---

## 4. HTTP 接口

所有 `POST` 都要带 `Content-Type: application/json`，并且**请求体必须是 UTF-8**。
在 Windows PowerShell 下用 `Invoke-RestMethod` 发中文体常被截断，建议改用：

```bash
curl.exe -s -X POST http://127.0.0.1:8090/api/search \
  -H "Content-Type: application/json" \
  --data-binary "@body.json"
```

### `GET /` 
网页。

### `GET /api/sites`
```json
{ "count": 489, "popular": ["GitHub", "..."], "all": ["GitHub", "..."] }
```

### `POST /api/search` —— 起一个全站/指定站点查询
```json
{ "username": "torvalds", "sites": ["GitHub", "豆瓣"], "timeout": 15, "nsfw": false }
```
- `sites` 省略或空数组 = 全部站点（慢，几分钟）
- 返回 `{ "id": "...", "status": "queued", "sites": 2 }`

### `GET /api/job/<id>` —— 轮询结果
```json
{
  "id": "...", "username": "torvalds", "status": "running|done|error",
  "count": 83, "elapsed": 35.2,
  "found": [{ "site": "GitHub", "url": "https://github.com/torvalds" }],
  "sites": ["GitHub"], "timeout": 15, "nsfw": false
}
```

### `POST /api/cn-search` —— 中文昵称搜人（B 站）
```json
{ "keyword": "老番茄" }
```
返回 `{ "keyword": "...", "platform": "哔哩哔哩", "items": [{ "name", "uid", "fans", "sign", "url", "avatar" }] }`。
拿到 `uid` 后可以再 `POST /api/search`，`sites: ["哔哩哔哩UID"]` 验证它确实存在。

### `POST /api/plat-search` —— 国内平台直查
```json
{ "platform": "weibo|douyin|qq|wechat", "query": "李荣浩" }
```
返回 `{ "platform": "微博", "mode": "...", "query": "...", "items": [...] }`，`items` 为空表示没查到（不是出错）。

| 字段 | 含义 |
| --- | --- |
| `name` / `uid` | 昵称与平台 ID |
| `fans` | 粉丝数（拿不到就是 `null`） |
| `sign` | 简介 / 认证 / 文章标题 |
| `url` | 主页或文章链接 |
| `verified` / `badge` | 是否认证及认证文案 |
| `avatar` | 头像地址 |
| `via` | 这条结果来自哪（如「公众号」） |

### `POST /api/maigret` —— 第二个引擎（Maigret）
```json
{ "username": "torvalds", "scope": "cn|top|all", "timeout": 15 }
```
- `cn` 只扫中国站点（约 43 站，20-60 秒）；`top` 扫热门 200 站；`all` 全量 5897 站（很慢）
- 返回 `{ "username": "...", "scope": "...", "total": 扫了多少站, "items": [{ "site", "url" }] }`
- **它的判据比 Sherlock 宽松，误报也更多**：实测拿一个不存在的乱码用户名扫中国站点，
  仍会返回 1 条命中（`bbs.aw-ol.com`）。所以这栏结果只当线索，必须点开核对。

### `POST /api/extra` —— 扩展工具（邮箱 / 手机号 / 域名）
```json
{ "tool": "holehe|ghunt|phoneinfoga|harvester", "target": "someone@example.com" }
```
- `target` 会被按工具类型校验：`holehe` / `ghunt` 必须是合法邮箱，`harvester` 必须是域名，
  `phoneinfoga` 需要 6-15 位数字（建议带国家码）。格式不对直接 400，不会去跑命令。
- 依赖 `OSINT_HUB` 指向的环境；没装则返回「找不到」提示，不影响其他卡片。
- `ghunt` 默认关闭，服务端启动时必须 `SHERLOCK_GHUNT=1` 才可用。

### `POST /api/ai` —— 本地大模型助手
```json
{ "q": "帮我查一下 torvalds 在哪些平台有账号" }
```
- `q` 为 2-300 字符的中文/英文自然语言。
- 优先走「工具模式」：模型自己决定调用 `/api/search`、`/api/maigret` 等，跑完再总结；
  返回 `{ "mode": "tool", "answer": ..., "run": [调用轨迹], "sources": [...], "model": ..., "elapsed": ... }`。
- 不需要工具时退回纯对话模式，返回 `{ "answer": ..., "sources": [...] }`。
- 连不上 Ollama 会返回 502 并提示先启动 `ollama serve`。
- **模型归属**：这个接口只转发给**使用者自己**的 Ollama（默认 `127.0.0.1:11434`），
  服务端不代理任何第三方推理服务，仓库里没有作者的地址、密钥或模型文件。
  别人 clone 后不装 Ollama，这一栏永远 502；作者不提供托管版，也不为你的调用买单。
- **结果里会写清楚哪些是查到的、哪些是模型推断的**——推断部分不能当证据。

---

## 5. 国内站点：判据与取舍

`sites_cn.json` 里的每一站都是**实测过**「存在一个页面、不存在另一个页面」才收进来的。
HEAD 与 GET 分别测过（有些站 HEAD 一律 200，必须 `"request_method": "GET"`）。

| 站点 | 探测地址 | 判据 |
| --- | --- | --- |
| V2EX | `v2ex.com/member/{u}` | 需 GET，否则 HEAD 恒 200 |
| 豆瓣 | `douban.com/people/{u}/` | 404 判否 |
| 博客园 | `cnblogs.com/{u}/` | 404 判否 |
| 雪球 | `xueqiu.com/{u}` | 404 判否 |
| 爱范儿 | `ifanr.com/author/{u}` | 需 GET |
| 哔哩哔哩UID | `space.bilibili.com/{u}` | `regexCheck` 要求纯数字 |
| 抖音号 | 显示 `douyin.com/search/{u}?type=user`，探测 `iesdouyin` 接口（`urlProbe`） | 接口返回「参数不合法」判否 |
| QQ号 | `user.qzone.qq.com/{u}` | 页面含「找不回来了」判否 |

**明确排除的站点**（不是漏做，是做了但会误报）：开源中国、虎扑、掘金、少数派、网易云音乐、米游社、NGA、36氪、虎嗅、SegmentFault、知乎、简书、贴吧、CSDN、AcFun、什么值得买、LOFTER、阿里云、腾讯云、华为云、果壳、起点、煎蛋、数字尾巴……
它们对「存在」和「不存在」返回同一个状态码（一律 200 或一律 403），硬塞进来只会制造假结果。
网页上给了这些平台的「去搜同名」直达链接，手动看更靠谱。

---

## 6. 三个平台的实现细节（为什么必须在服务端做）

Sherlock 的模型是「发一个请求 → 看状态码或正则」，它不能带 cookie、也不能回比返回内容里的字段。
所以下面这些判断放在 `sherlock_web.py`，而不是 `sites_cn.json`。

### 微博
- 昵称：`https://weibo.com/ajax/side/search?q=<昵称>`，**免登录可用**。
- UID：`https://weibo.com/ajax/profile/info?uid=<UID>` 无 cookie 时固定返回 `{"ok":-100, ...login.php}`，
  所以先做 visitor 握手（`passport.weibo.com/visitor/genvisitor2` 拿到 `sub` / `subp` 写进 `SUB` / `SUBP` cookie），
  握手结果缓存 15 分钟。真实号返回完整资料，不存在的号返回 `{"ok":1,"message":"用户不存在"}`。

### 抖音
- `https://www.iesdouyin.com/web/api/v2/user/info/?unique_id=<抖音号>`，
  `status_code: 0` + 有昵称 = 存在，`status_code: 5`「参数不合法」= 不存在。
- **坑**：纯数字输入会退化成按 uid / 短 ID 兜底匹配（随便一个 9 位数有相当概率命中别人的号）。
  因此服务端把返回的 `unique_id` / `short_id` 和输入回比，对不上直接丢弃。
- 全站扫描用的站点定义加了 `regexCheck`（必须含至少一个英文字母），纯数字抖音号在批量扫描里会被跳过，
  避免误报；要查纯数字请用「国内平台直查」卡片。

### QQ 号
- `https://user.qzone.qq.com/<QQ号>`：不存在的号落到「你访问的页面找不回来了」404 页；
  存在的号页面正常、或 302 到 `i.qq.com` 登录页、或显示「未开通空间」。
- 漏判风险：QQ 空间已注销/关闭的真实老号也会落 404，会被当成「不存在」。

### 微信（降级实现）
- **个人微信号没有任何公开主页**，微信也不提供对外的号查询接口，任何免登录方案都查不到
  「这个微信号是否存在」。
- 搜狗的公众号搜索页（`type=1`）也已经失效（连「人民日报」都返回「暂无相关订阅号」）。
- 所以这一栏做的是：按关键词搜公众号文章（`weixin.sogou.com/weixin?type=2`），
  列出**发过相关内容的公众号** + 文章链接。它不是查号，界面上和文档里都写明了。

---

## 7. 已知限制

- 微博 visitor cookie 可能被风控，握手失败会明确报「要求登录，稍后再试」。
- 抖音只在返回的 ID 与输入一致时才认，宁可漏判不误报。
- QQ 用 QQ 空间页面判定，空间已关闭的真实号会漏判。
- 微信只能反查公众号文章，查不到个人微信号。
- 中文昵称搜索只取 B 站前 12 条；接口风控时可能 412，等一会儿重试。
- Maigret 引擎的命中比 Sherlock 多，但**误报率也更高**（不存在的用户名也可能命中个别站点），
  只适合当线索，不能当结论。它还依赖单独安装（`pip install maigret`），没装则这一栏不可用。
- 查到的结果只证明「该用户名在该站有公开主页」，**不代表是同一个人**。
- 扩展工具（Holehe / GHunt / PhoneInfoga / theHarvester）要额外装进 `OSINT_HUB` 指向的环境；
  没装时对应卡片报「找不到」，其余功能不受影响。GHunt 还需先执行一次 `ghunt login`，
  且服务端要 `SHERLOCK_GHUNT=1` 才放行。
- AI 搜索依赖本机 Ollama 和模型（默认 `deepseek-r1:14b`）；没跑 Ollama 时 `/api/ai` 返回 502。
  模型给出的结论属于推断，只有 `sources` 里列出的才是实际查到的。
- 默认限流：每 IP 每 10 分钟 12 次，全站每小时 80 次，同时最多 2 个查询，排队上限 20。

---

## 8. 放到公网（可选）

服务本身只监听本机。要给别人用，可以：

**方案一：Cloudflare Tunnel**（不用开端口、不用公网 IP）

```bash
cloudflared tunnel create sherlock-web
cloudflared tunnel route dns sherlock-web your-domain.example
cloudflared --config cloudflared.yml tunnel run --token-file tunnel_token.txt
```

配一个 `cloudflared.yml`：

```yaml
tunnel: sherlock-web
credentials-file: C:\Users\you\.cloudflared\<uuid>.json
ingress:
  - hostname: your-domain.example
    service: http://127.0.0.1:8090
  - service: http_status:404
```

拿到 token 后存到 `tunnel_token.txt`，用
`cloudflared --config cloudflared.yml tunnel run --token-file tunnel_token.txt`
（注意：`--config` 是全局参数，必须写在 `tunnel run` 前面）。

**方案二：Nginx 反代 + 端口映射**

> ⚠️ 东西放到公网就是「谁拿到网址都能查」。建议至少加一层 Cloudflare Access（邮箱验证码）
> 或者自己在 `sherlock_web.py` 里加访问口令。`tunnel_token.txt`、`cloudflared.yml` 千万不要提交到仓库。

> ⚠️ **尤其注意 AI 搜索那一栏**：公网可访时，任何陌生人点一下「AI 搜索」，跑的**都是你这台机器的
> 显卡和 Ollama**（一次 14B 推理几十秒，排队上限也会被占满）。放公网前请务必
> 给整站加访问控制（Cloudflare Access / Nginx Basic Auth），或者干脆只在内网用。
> 这一栏不是云服务，没有别人替你分摊算力。

---

## 9. 合规与免责

- 本项目只查询**平台上公开可见**的信息，不登录他人账号、不抓取非公开数据。
- 请遵守各平台的服务条款与当地法律。**不得**用于人肉搜索、骚扰、跟踪、开盒或任何侵犯他人隐私的用途。
- 作者不对查询结果的准确性、完整性负责，也不对使用本项目造成的后果负责。
- 上游 Sherlock 同样仅供合法用途，详见其仓库说明。

---

## 10. 许可证与致谢

本仓库代码以 **MIT** 发布，见 [LICENSE](LICENSE)。

致谢：

- [sherlock-project/sherlock](https://github.com/sherlock-project/sherlock)（MIT）—— 没有它就没有这一切；
  本项目与其**无隶属关系**，也未获其背书。上游的站点数据与 CLI 逻辑版权归上游作者所有。
- [Maigret](https://github.com/soxoj/maigret)（Sherlock 的第三方分支）—— 第二引擎，仅调用其命令行，不打包其代码。
- [holehe](https://github.com/megadose/holehe) / [GHunt](https://github.com/mxrch/GHunt) /
  [PhoneInfoga](https://github.com/sundowndev/phoneinfoga) /
  [theHarvester](https://github.com/laramies/theHarvester) —— 扩展工具，同样只调用命令行、不打包代码，
  使用前请各自确认其许可证。
- [Ollama](https://ollama.com) —— 本地模型推理，AI 搜索那一栏靠它。
- 各平台公开接口（微博、B 站、抖音、QQ 空间、搜狗微信）—— 仅用于查询公开信息，接口随时可能变更。

如果你觉得有用，去给上游点个 star；这个仓库只是它的一层壳。
