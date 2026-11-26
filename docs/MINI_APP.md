# Telegram Mini App · 查车评

私聊机器人，点「📖 查车评」进入小程序。首页按提交时间从新到旧展示用户有权限查看的有效评价。在首页停留时，每 15 秒检查最新一页。往下浏览时，列表顶部会提示有更新，点击后回到最新一页。

在顶部输入老师姓名或 `@用户名`，也可以直接点车评里的老师名。老师主页按评价里填写的日期排序，支持从新到旧、从旧到新、指定日期和日期范围。范围包含开始、结束两天。同名老师显示所属集群。车评卡片显示综合分和过程，可以打开 Telegram 原帖。有效评价暂时没有可用原帖时，只展示老师、日期等目录信息。

## 部署

小程序和 Telegram 轮询由两个命令运行，共用 Go 二进制和 PostgreSQL。小程序以只读事务查询，最多使用 4 条 PostgreSQL 连接。使用现有机器人 Token 核验 Telegram 启动数据，并沿用机器人的集群访问规则。小程序对已确认的访问权限缓存 45 秒。

1. 用 Go 模式构建当前版本。为小程序使用一个独立的 HTTPS 域名，将根路径代理到 `127.0.0.1:8091`。保留 `Authorization` 请求头，避免将该请求头写入日志，并对 API 响应关闭共享缓存。
2. 在 cpbot 工作目录用与机器人相同的 `ENV_FILE` 启动 `.runtime/cpbot-go serve-mini-app`。建议用独立 systemd 服务管理。若设置了 `CPBOT_MINI_APP_LISTEN`，反向代理使用对应的回环地址。
3. 用 `curl -fsS http://127.0.0.1:8091/healthz` 检查数据库连接和 HTTP 服务。再把小程序 HTTPS 根地址写入 `CPBOT_MINI_APP_URL`，用 `bash manage.sh restart` 重读机器人配置。
4. 在 Telegram 私聊发送 `/start`。用有权限的成员验收首页、老师搜索、日期筛选和原帖跳转，再用无权限的成员核对访问受限状态。

临时隐藏入口时，清空 `CPBOT_MINI_APP_URL` 后重启机器人。需要同时关闭小程序服务时，停止对应的 systemd 服务。

## 独立环境验收

```bash
CPBOT_GO=go CPBOT_PG_BIN=/path/to/postgresql/bin \
  python3 lab/postgres_mini_app_e2e.py
```

脚本构建真实服务，启动临时 PostgreSQL 并输出 `result.json`。加 `--browser` 后，可用产物目录里的 `browser-url.txt` 在 Codex 内置浏览器复查。打开首页后，用 `kill -USR1 "$(cat /path/to/artifacts/runner.pid)"` 向验收库写入一条迟录的评价。首页应在一个轮询周期内显示这条新评价。复查完成后向该进程发送 `TERM`，脚本会停止小程序和临时 PostgreSQL。
