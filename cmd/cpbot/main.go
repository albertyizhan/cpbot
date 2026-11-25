package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"net"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"cpbot/internal/bot"
	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

const usage = "用法：cpbot {run | check | migrate-sqlite [--source <SQLite 文件>] [--check]}"

func main() {
	syscall.Umask(0077)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, os.Args[1:]); err != nil && ctx.Err() == nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(ctx context.Context, args []string) error {
	if len(args) == 1 && (args[0] == "--help" || args[0] == "-h") {
		fmt.Println(usage)
		return nil
	}
	if len(args) == 0 {
		return errors.New(usage)
	}
	if err := loadEnv(); err != nil {
		return err
	}
	source, err := defaultSource()
	if err != nil {
		return err
	}
	dsn := strings.TrimSpace(os.Getenv("DATABASE_URL"))
	switch args[0] {
	case "migrate-sqlite":
		return migrate(ctx, args[1:], source, dsn)
	case "run-lab":
		flags := flag.NewFlagSet("run-lab", flag.ContinueOnError)
		source := flags.String("source", source, "SQLite 路径，用于运行锁和证明图布局")
		endpoint := flags.String("api", "", "独立的回环 Bot API 端点")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		u, err := url.Parse(*endpoint)
		if err != nil || u.Scheme != "http" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || !net.ParseIP(u.Hostname()).IsLoopback() || u.Path != "" || flags.NArg() != 0 {
			return errors.New("run-lab 要求回环 HTTP Bot API 地址")
		}
		return runBot(ctx, dsn, *source, telegram.At(*endpoint+"/bot-test"), false)
	case "run", "check":
		if len(args) != 1 {
			return errors.New(usage)
		}
		token := strings.TrimSpace(os.Getenv("BOT_TOKEN"))
		if !tokenPattern.MatchString(token) {
			return errors.New("请设置有效的 BOT_TOKEN")
		}
		return runBot(ctx, dsn, source, telegram.New(token), args[0] == "check")
	default:
		return errors.New(usage)
	}
}

func migrate(ctx context.Context, args []string, defaultPath, dsn string) error {
	flags := flag.NewFlagSet("migrate-sqlite", flag.ContinueOnError)
	source := flags.String("source", defaultPath, "已停止的 Python cpbot 主 SQLite 文件")
	check := flags.Bool("check", false, "完整导入校验后回滚")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return nil
		}
		return err
	}
	if *source == "" || flags.NArg() != 0 {
		return errors.New(usage)
	}
	if dsn == "" {
		return errors.New("请为 cpbot 设置目标 DATABASE_URL")
	}
	report, err := pgstore.MigrateSQLite(ctx, dsn, *source, *check)
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	return encoder.Encode(report)
}

func runBot(ctx context.Context, dsn, source string, api *telegram.Client, check bool) error {
	if dsn == "" {
		return errors.New("请设置 DATABASE_URL")
	}
	admins, err := adminsFromEnv()
	if err != nil {
		return err
	}
	connection, cancel := context.WithTimeout(ctx, 10*time.Second)
	db, err := pgx.Connect(connection, dsn)
	cancel()
	if err != nil {
		return errors.New("无法连接 PostgreSQL，请检查 DATABASE_URL 和数据库可用性")
	}
	defer db.Close(context.Background())
	if check {
		var version int
		if err := db.QueryRow(ctx, `SELECT max(version) FROM cpbot.schema_version`).Scan(&version); err != nil || version != 1 {
			return errors.New("请先准备 PostgreSQL cpbot schema 1")
		}
		fmt.Println("cpbot PostgreSQL schema 1 可用")
		return nil
	}
	return bot.Run(ctx, db, api, source, admins)
}
