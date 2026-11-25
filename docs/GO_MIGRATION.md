# cpbot 迁移到 Go + PostgreSQL

Go 版继续使用原机器人 Token、集群编号和历史 Telegram 消息位置。PostgreSQL 的 `cpbot` schema 存放全部集群的业务数据。证明图沿用原 `DATABASE_PATH` 对应的目录。所有集群共用一个轮询进程和一套发布限速。

## 演练迁移

准备一个 cpbot 专用 PostgreSQL 库和可建 schema 的账号。在备份副本中保持主 SQLite、`proofs/` 和 `<主库文件名>.clusters/` 的原有相对位置。使用 Go 1.24 或以上构建。离线导入使用 Python 3.9 或以上的标准库读取旧 SQLite。

```bash
bash manage.sh build-go
bash manage.sh migrate-sqlite --source /path/to/copy/cpbot.sqlite3 --check
```

从 `.env` 或进程环境读取 `DATABASE_URL`。`--check` 实际导入并执行约束、图片检查和序列初始化，随后回滚。输出列出每张表的行数和仍被草稿或车评引用的图片数。导入时正在发送的任务会变为「待核对」。回滚试跑和正式导入都要求目标库尚无 `cpbot` schema。

## 服务器切换

1. 停止现有服务，保持 `CPBOT_ENGINE=python`，运行 `bash manage.sh backup`。数据库和证明图应来自同一次停机窗口。
2. 在 `.env` 设置 cpbot 专用库的 `DATABASE_URL`。用 `bash manage.sh migrate-sqlite --check` 核对源数据，再执行 `bash manage.sh migrate-sqlite`。该命令默认读取 `DATABASE_PATH`。
3. 保留原 `DATABASE_PATH` 和证明图位置。把 `.env` 的 `CPBOT_ENGINE` 设为 `go`，执行 `bash manage.sh install` 更新 systemd 入口，再查看 `bash manage.sh logs`。
4. 检查已启用绑定的权限、旧 `/start` 链接和发布任务。待核对任务需要查证 Telegram 上的实际结果。已发送的登记消息 ID，确认未发送的再重试。

当 Go 服务尚未处理新更新或发送消息时，可将 `CPBOT_ENGINE` 改回 `python` 并重新安装服务入口。Go 服务开始处理业务后，先暂停并核对新增写入、轮询位置及远端消息，再进行回退。旧 SQLite 快照停留在切换时点。

## PostgreSQL 服务器备份

停服务后执行 `bash manage.sh backup`。归档包含 `cpbot.pgdump`、各集群证明图、配置和 Go 二进制。`pg_dump` 客户端需能连接源 PostgreSQL。后台的 JSON + ZIP 仍可按集群单独导出和恢复。

恢复时将 `cpbot.pgdump` 用 `pg_restore --single-transaction --no-owner --no-acl` 载入新的 cpbot 专用库，再还原同一归档的证明图。归档根目录的 `proofs/` 对应集群 1，`clusters/<编号>/proofs/` 对应其他集群。全库快照若早于最后一次发布，启动前须暂停队列和核对轮询位置、待办任务与 Telegram 消息。归档包含 Token 和业务资料，应妥善保管。

## 本地验收

```bash
CPBOT_PG_BIN="$(pg_config --bindir)" python3 lab/postgres_migration_e2e.py
```

脚本构建 Go 服务，在临时目录创建独立 PostgreSQL 和回环 Bot API。运行后输出留有操作日志、Bot API 请求和 `result.json` 的目录。真实 Telegram 验收需使用独立 Token 和测试频道、讨论组及查档群，验证权限、自动转发根消息与链接跳转。
