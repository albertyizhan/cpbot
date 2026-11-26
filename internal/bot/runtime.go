// Package bot owns Telegram polling and the cpbot conversation state machine.
package bot

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"syscall"
	"time"

	"cpbot/internal/pgstore"
	"cpbot/internal/publish"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
	"strings"
)

type Bot struct {
	db        *pgx.Conn
	api       *telegram.Client
	publisher *publish.Worker
	me        telegram.User
	source    string
	admins    map[int64]bool
	panels    map[int64]panel
	triggers  map[[2]int64]time.Time
}

func Run(ctx context.Context, db *pgx.Conn, api *telegram.Client, source string, admins map[int64]bool) error {
	path, err := filepath.Abs(source)
	if err != nil {
		return err
	}
	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDONLY, 0600)
	if err != nil {
		return fmt.Errorf("打开 cpbot 运行锁：%w", err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return errors.New("SQLite 路径已有运行中的 cpbot，请停止旧进程")
	}
	var version int
	if err := db.QueryRow(ctx, `SELECT version FROM cpbot.schema_version ORDER BY version DESC LIMIT 1`).Scan(&version); err != nil || version != 1 {
		return errors.New("请先将 cpbot 数据迁入 PostgreSQL schema 1")
	}
	var owner bool
	if err := db.QueryRow(ctx, `SELECT pg_try_advisory_lock(1129333327,0)`).Scan(&owner); err != nil {
		return err
	}
	if !owner {
		return errors.New("此数据库已有运行中的 cpbot")
	}
	// Close the owning connection to release a session lock, including on shutdown.
	defer db.Close(context.Background())
	if err := pgstore.Upgrade(ctx, db); err != nil {
		return fmt.Errorf("升级车评发布记录：%w", err)
	}
	var me telegram.User
	if err := api.Call(ctx, "getMe", telegram.Params{}, &me); err != nil {
		return fmt.Errorf("无法确认 Telegram 机器人身份：%w", err)
	}
	if me.ID <= 0 || me.Username == "" {
		return errors.New("Telegram 返回的机器人身份不完整")
	}
	me.Username = strings.ToLower(me.Username)
	worker := publish.New(db, api, me.Username, me.ID, path)
	if err := worker.Recover(ctx); err != nil {
		return err
	}
	if err := worker.ValidateBindings(ctx); err != nil {
		return fmt.Errorf("启动时无法核验绑定权限：%w", err)
	}
	commands := []telegram.Params{{"command": "start", "description": "打开主菜单"}, {"command": "cancel", "description": "取消当前操作"}}
	if err := api.Call(ctx, "setMyCommands", telegram.Params{"commands": commands}, nil); err != nil {
		return err
	}
	private := append(append([]telegram.Params{}, commands...), telegram.Params{"command": "register", "description": "使用激活码注册集群"}, telegram.Params{"command": "admin", "description": "打开集群管理后台"})
	if err := api.Call(ctx, "setMyCommands", telegram.Params{"scope": telegram.Params{"type": "all_private_chats"}, "commands": private}, nil); err != nil {
		return err
	}
	b := Bot{db: db, api: api, me: me, publisher: worker, source: path, admins: admins, panels: make(map[int64]panel), triggers: make(map[[2]int64]time.Time)}
	slog.Info("cpbot 已启动", "username", me.Username)
	return b.poll(ctx)
}

func (b *Bot) poll(ctx context.Context) error {
	var saved *string
	if err := b.db.QueryRow(ctx, `SELECT (SELECT value FROM cpbot.settings WHERE (cluster_id,key)=(1,'offset'))`).Scan(&saved); err != nil {
		return err
	}
	var offset int64
	if saved != nil {
		var err error
		offset, err = strconv.ParseInt(*saved, 10, 64)
		if err != nil || offset < 0 {
			return errors.New("数据库中的 Telegram offset 无效")
		}
	}
	var nextCleanup time.Time
	for ctx.Err() == nil {
		now := time.Now()
		if !now.Before(nextCleanup) {
			if err := b.cleanup(ctx, now); err != nil {
				return err
			}
			for user, state := range b.panels {
				if now.After(state.until) {
					delete(b.panels, user)
				}
			}
			nextCleanup = now.Add(time.Minute)
		}
		if err := b.publisher.ScheduleNightly(ctx, now); err != nil {
			return err
		}
		if err := b.publisher.Tick(ctx, now); err != nil {
			return err
		}
		pollTime, err := b.publisher.PollTimeout(ctx)
		if err != nil {
			return err
		}
		var updates []telegram.Update
		err = b.api.Call(ctx, "getUpdates", telegram.Params{"offset": offset, "timeout": pollTime, "limit": 20,
			"allowed_updates": []string{"message", "edited_message", "callback_query", "chat_member", "my_chat_member"}}, &updates)
		if ctx.Err() != nil {
			return nil
		}
		if err != nil {
			var apiError *telegram.Error
			if !errors.As(err, &apiError) {
				return err
			}
			if apiError.Code == 401 || apiError.Code == 409 {
				return errors.New("Token 无效或存在另一个轮询 / Webhook 实例，请检查配置")
			}
			slog.Warn("Telegram 轮询失败", "code", apiError.Code)
			wait(ctx, time.Duration(max(3, apiError.RetryAfter))*time.Second)
			continue
		}
		for _, update := range updates {
			if ctx.Err() != nil {
				return nil
			}
			if update.ID < offset {
				continue
			}
			err := b.handle(ctx, update)
			if err != nil && ctx.Err() != nil {
				return nil
			}
			if err != nil {
				var remote *telegram.Error
				if !errors.As(err, &remote) {
					return fmt.Errorf("处理 Telegram 更新 %d：%w", update.ID, err)
				}
				slog.Warn("Telegram 响应失败", "update", update.ID, "code", remote.Code)
				slog.Warn("Telegram 响应结果不明，不自动重放更新", "update", update.ID, "code", remote.Code)
				if remote.RetryAfter > 0 {
					wait(ctx, time.Duration(remote.RetryAfter)*time.Second)
				}
			}
			offset = update.ID + 1
			if _, err := b.db.Exec(ctx, `INSERT INTO cpbot.settings(cluster_id,key,value) VALUES(1,'offset',$1)
				ON CONFLICT (cluster_id,key) DO UPDATE SET value=excluded.value`, strconv.FormatInt(offset, 10)); err != nil {
				return err
			}
		}
	}
	return nil
}

func wait(ctx context.Context, duration time.Duration) {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

func (b *Bot) handle(ctx context.Context, update telegram.Update) error {
	var err error
	var chat int64
	switch {
	case update.Callback != nil:
		chat = update.Callback.From.ID
		err = b.callback(ctx, *update.Callback)
	case update.Message != nil:
		msg := *update.Message
		chat = msg.Chat.ID
		if msg.Chat.Type != "private" {
			err = b.groupMessage(ctx, msg, false)
		} else if msg.From.ID > 0 {
			err = b.observePrivateTeacher(ctx, msg.From, msg.Date)
			if err == nil {
				err = b.privateMessage(ctx, msg)
			}
		}
	case update.EditedMessage != nil && update.EditedMessage.Chat.Type != "private":
		err = b.groupMessage(ctx, *update.EditedMessage, true)
	case update.ChatMember != nil:
		err = b.chatMemberUpdate(ctx, *update.ChatMember)
	case update.MyChatMember != nil:
		err = b.myChatMember(ctx, *update.MyChatMember)
	}
	var userError pgstore.UserError
	if errors.As(err, &userError) {
		return b.send(ctx, chat, userError.Error())
	}
	return err
}
