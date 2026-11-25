package bot

import (
	"context"
	"encoding/json"
	"errors"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

type cluster struct {
	ID   int64
	Name string
}

func (b *Bot) availableClusters(ctx context.Context, publishing bool) ([]cluster, error) {
	rows, err := b.db.Query(ctx, `SELECT c.id,c.name FROM cpbot.clusters c JOIN cpbot.cluster_authorizations a ON c.id=a.cluster_id
		WHERE EXISTS (SELECT 1 FROM cpbot.bindings b WHERE b.cluster_id=c.id AND b.active=1 AND (NOT $1 OR b.mode IN ('thread','feed'))) ORDER BY c.id`, publishing)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowToStructByPos[cluster])
}

type member struct {
	Status   string `json:"status"`
	IsMember *bool  `json:"is_member"`
}

func (m member) admin() bool { return m.Status == "administrator" || m.Status == "creator" }

func (m member) present() bool {
	return m.presentState() != nil && *m.presentState()
}

func (m member) presentState() *bool {
	return (telegram.ChatMember{Status: m.Status, IsMember: m.IsMember}).Present()
}

func (b *Bot) chatMember(ctx context.Context, chat, uid int64) (member, error) {
	var result member
	err := b.api.Call(ctx, "getChatMember", telegram.Params{"chat_id": chat, "user_id": uid}, &result)
	return result, err
}

func (b *Bot) isAdmin(ctx context.Context, cid, uid int64) (bool, error) {
	if b.admins[uid] {
		return true, nil
	}
	rows, err := b.db.Query(ctx, `SELECT chat_id FROM cpbot.cluster_management_groups JOIN cpbot.cluster_authorizations USING(cluster_id) WHERE cluster_id=$1
		UNION SELECT chat_id FROM cpbot.bindings JOIN cpbot.cluster_authorizations USING(cluster_id) WHERE cluster_id=$1 AND active=1
		UNION SELECT discussion FROM cpbot.bindings JOIN cpbot.cluster_authorizations USING(cluster_id) WHERE cluster_id=$1 AND active=1 AND discussion IS NOT NULL`, cid)
	if err != nil {
		return false, err
	}
	chats, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return false, err
	}
	for _, chat := range chats {
		person, err := b.chatMember(ctx, chat, uid)
		if err != nil || !person.admin() {
			continue
		}
		bot, err := b.chatMember(ctx, chat, b.me.ID)
		if err == nil && bot.admin() {
			return true, nil
		}
	}
	return false, nil
}

func (b *Bot) requireMember(ctx context.Context, cid, uid int64) error {
	var authorized, ready bool
	var total int
	var setting *string
	var target *int64
	err := b.db.QueryRow(ctx, `SELECT
		EXISTS(SELECT 1 FROM cpbot.cluster_authorizations WHERE cluster_id=$1),
		EXISTS(SELECT 1 FROM cpbot.bindings WHERE cluster_id=$1 AND active=1),
		(SELECT count(*) FROM cpbot.clusters),
		(SELECT value FROM cpbot.settings WHERE (cluster_id,key)=($1,'cluster_member_sources')),
		(SELECT chat_id FROM cpbot.membership_targets WHERE cluster_id=$1)`, cid).Scan(&authorized, &ready, &total, &setting, &target)
	if err != nil {
		return err
	}
	if !authorized {
		return pgstore.UserError("该集群尚未激活，请联系管理员。")
	}
	if !ready {
		return pgstore.UserError("该集群暂无可用的业务绑定。")
	}
	if total == 1 && setting == nil {
		return nil
	}
	admin, err := b.isAdmin(ctx, cid, uid)
	if err != nil || admin {
		return err
	}
	var chats []int64
	if setting != nil {
		if err := json.Unmarshal([]byte(*setting), &chats); err != nil {
			return err
		}
	}
	if target != nil {
		chats = append(chats, *target)
	}
	seen := map[int64]bool{}
	unknown := false
	for _, chat := range chats {
		if seen[chat] {
			continue
		}
		seen[chat] = true
		var active bool
		err := b.db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.cluster_management_groups WHERE (cluster_id,chat_id)=($1,$2)
			UNION ALL SELECT 1 FROM cpbot.membership_targets WHERE (cluster_id,chat_id)=($1,$2)
			UNION ALL SELECT 1 FROM cpbot.bindings WHERE cluster_id=$1 AND active=1 AND (chat_id=$2 OR discussion=$2))`, cid, chat).Scan(&active)
		if err != nil {
			return err
		}
		if !active {
			unknown = true
			continue
		}
		bot, err := b.chatMember(ctx, chat, b.me.ID)
		if err != nil || !bot.admin() {
			unknown = true
			continue
		}
		person, err := b.chatMember(ctx, chat, uid)
		if err == nil && person.present() {
			return nil
		}
		unknown = unknown || err != nil || person.presentState() == nil
	}
	if unknown {
		return pgstore.UserError("暂时无法确认成员身份，请稍后重试。")
	}
	return pgstore.UserError("请先加入该集群指定的群或频道。")
}

func memberDenied(err error) bool {
	var userError pgstore.UserError
	return errors.As(err, &userError)
}
