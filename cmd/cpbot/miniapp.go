package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"cpbot/internal/bot"
	"cpbot/internal/miniapp"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5/pgxpool"
)

func miniAppURL() (string, error) {
	raw := strings.TrimSpace(os.Getenv("CPBOT_MINI_APP_URL"))
	if raw == "" {
		return "", nil
	}
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "https" || u.Hostname() == "" || u.User != nil || u.Fragment != "" || u.RawQuery != "" || (u.Path != "" && u.Path != "/") {
		return "", errors.New("CPBOT_MINI_APP_URL 必须是小程序 HTTPS 域名的根地址")
	}
	return u.String(), nil
}

func serveMiniApp(ctx context.Context, dsn, token string) error {
	if !tokenPattern.MatchString(token) {
		return errors.New("请设置有效的 BOT_TOKEN")
	}
	botID, _, _ := strings.Cut(token, ":")
	id, err := strconv.ParseInt(botID, 10, 64)
	if err != nil || id < 1 {
		return errors.New("BOT_TOKEN 缺少有效的机器人 ID")
	}
	admins, err := adminsFromEnv()
	if err != nil {
		return err
	}
	if dsn == "" {
		return errors.New("请设置 DATABASE_URL")
	}
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return errors.New("DATABASE_URL 无效")
	}
	cfg.MaxConns = 4
	cfg.MinConns = 0
	cfg.MinIdleConns = 0
	cfg.ConnConfig.RuntimeParams["default_transaction_read_only"] = "on"
	startup, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	db, err := pgxpool.NewWithConfig(startup, cfg)
	if err != nil {
		return errors.New("无法建立小程序数据库连接池")
	}
	defer db.Close()
	var version int
	if err := db.QueryRow(startup, `SELECT max(version) FROM cpbot.schema_version`).Scan(&version); err != nil || version != 1 {
		return errors.New("小程序无法读取 cpbot PostgreSQL schema 1")
	}
	addr := strings.TrimSpace(os.Getenv("CPBOT_MINI_APP_LISTEN"))
	if addr == "" {
		addr = "127.0.0.1:8091"
	}
	host, _, err := net.SplitHostPort(addr)
	if err != nil || !net.ParseIP(host).IsLoopback() {
		return errors.New("CPBOT_MINI_APP_LISTEN 必须是回环监听地址，例如 127.0.0.1:8091")
	}
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		return err
	}
	access := bot.NewLookupAccess(db, telegram.New(token), id, admins)
	svc := &http.Server{
		Handler: miniapp.Handler(db, access, token), ReadHeaderTimeout: 4 * time.Second,
		ReadTimeout: 10 * time.Second, WriteTimeout: 12 * time.Second, IdleTimeout: 45 * time.Second,
		MaxHeaderBytes: 16 << 10,
	}
	stopped := make(chan error, 1)
	go func() { stopped <- svc.Serve(ln) }()
	slog.Info("cpbot Mini App 已启动", "listen", ln.Addr().String())
	select {
	case err := <-stopped:
		return err
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return svc.Shutdown(shutdown)
	}
}
