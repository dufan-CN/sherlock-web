# NOTICE — 归属与改动说明

## 1. 与上游的关系

本项目（Sherlock Web · 中文增强版）是
[**sherlock-project/sherlock**](https://github.com/sherlock-project/sherlock) 的**第三方网页封装**。

- **不是官方项目**，与上游无隶属关系，也未获上游背书或认可。
- 本仓库**不包含、不再分发**上游的任何代码或数据文件；运行时需要你先自行安装上游 Sherlock。
- 上游项目版权归 Sherlock Project 及其贡献者所有，采用 MIT 许可证。
  上游许可证全文见 https://github.com/sherlock-project/sherlock/blob/master/LICENSE
- 本仓库仅新增了调用上游 CLI 的网页层与若干国内平台查询逻辑，该部分以 MIT 许可证发布，
  版权归本仓库作者所有（见 LICENSE）。

## 2. 相对上游做了什么

上游提供的是命令行工具：给一个用户名，逐站检查是否存在。本仓库在原样使用该 CLI 的前提下，新增：

| 改动 | 说明 |
| --- | --- |
| 网页服务 | `sherlock_web.py`，仅用 Python 标准库，把 CLI 包装成异步任务 + JSON API，带限流与并发保护 |
| 前端页面 | `index.html`，单页三块卡片：全站查询、中文昵称搜索、国内平台直查 |
| 国内站点 | `sites_cn.json` 内 8 个经实测可稳定判定的站点，启动时与上游 `data.json` 合并为 `data_merged.json` |
| 中文用户名 | 放开用户名正则到 CJK 范围，中文昵称亦可作为查询词 |
| 中文昵称搜索 | 调用 B 站公开搜索接口按昵称搜人，拿到 UID 后再交由 Sherlock 验证 |
| 微博查询 | 昵称搜索 + UID 资料，含 visitor cookie 握手机制 |
| 抖音号查询 | 调用 `iesdouyin` 用户信息接口，并做 ID 回比以消除纯数字误命中 |
| QQ 号查询 | 依据 QQ 空间页面判定账号是否注册 |
| 微信查询 | 降级实现：按关键词反查发过相关文章的公众号（个人微信号无公开主页，无法查存在性） |
| Maigret 引擎 | 接入 [soxoj/maigret](https://github.com/soxoj/maigret)（Sherlock 的第三方分支，独立仓库、独立安装）作为第二引擎，可按国家/类别标签扫描；本项目仅调用其命令行，不包含其代码 |
| 编码修复 | 子进程输出按 utf-8 / gbk 自适应解码，修正 Windows 计划任务下中文站点名乱码 |

上游的 `data.json`、`sherlock.py` 等文件**未被修改**，因此对上游执行 `git pull` 不会产生冲突。

## 3. 数据来源

查询结果来自各平台的公开页面与公开接口，本项目不存储、不缓存用户查询记录，也不建立任何个人档案数据库。
平台接口随时可能变更或失效，本项目不保证长期可用。
