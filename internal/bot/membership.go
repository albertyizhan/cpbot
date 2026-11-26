package bot

import (
	"context"
	"encoding/json"
	"errors"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

type membershipDB interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
	QueryRow(context.Context, string, ...any) pgx.Row
}

// LookupAccess uses the bot's cluster policy for both user interfaces.
type LookupAccess struct {
	db     membershipDB
	api    *telegram.Client
	botID  int64
	admins map[int64]bool
}

func NewLookupAccess(db membershipDB, api *telegram.Client, botID int64, admins map[int64]bool) LookupAccess {
	return LookupAccess{db: db, api: api, botID: botID, admins: admins}
}

func (b *Bot) lookupAccess() LookupAccess {
	return NewLookupAccess(b.db, b.api, b.me.ID, b.admins)
}

// VisibleClusterIDs returns only clusters this Telegram user may look up.
func (b LookupAccess) VisibleClusterIDs(ctx context.Context, uid int64) ([]int64, error) {
	clusters, err := b.availableClusters(ctx, false)
	if err != nil {
		return nil, err
	}
	ids := make([]int64, 0, len(clusters))
	for _, c := range clusters {
		if err := b.requireMember(ctx, c.ID, uid); err == nil {
			ids = append(ids, c.ID)
		} else if !memberDenied(err) {
			return nil, err
		}
	}
	return ids, nil
}

type cluster struct {
	ID   int64
	Name string
}

func (b *Bot) availableClusters(ctx context.Context, publishing bool) ([]cluster, error) {
	return b.lookupAccess().availableClusters(ctx, publishing)
}

func (b LookupAccess) availableClusters(ctx context.Context, publishing bool) ([]cluster, error) {
	rows, err := b.db.Query(ctx, `SELECT c.id,c.name FROM cpbot.clusters c JOIN cpbot.cluster_authorizations a ON c.id=a.cluster_id
		WHERE EXISTS (SELECT 1 FROM cpbot.bindings b WHERE b.cluster_id=c.id AND b.active=1 AND (NOT $1 OR b.mode IN ('thread','feed'))) ORDER BY c.id`, publishing)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowToStructByPos[cluster])
}

type member struct {
	Status     string `json:"status"`
	CanPromote bool   `json:"can_promote_members"`
	IsMember   *bool  `json:"is_member"`
}

func (m member) admin() bool { return m.Status == "administrator" || m.Status == "creator" }

func (m member) present() bool {
	return m.presentState() != nil && *m.presentState()
}

func (m member) presentState() *bool {
	return (telegram.ChatMember{Status: m.Status, IsMember: m.IsMember}).Present()
}

func (b *Bot) chatMember(ctx context.Context, chat, uid int64) (member, error) {
	return b.lookupAccess().chatMember(ctx, chat, uid)
}

func (b LookupAccess) chatMember(ctx context.Context, chat, uid int64) (member, error) {
	var result member
	err := b.api.Call(ctx, "getChatMember", telegram.Params{"chat_id": chat, "user_id": uid}, &result)
	return result, err
}

func (b *Bot) isAdmin(ctx context.Context, cid, uid int64) (bool, error) {
	return b.lookupAccess().isAdmin(ctx, cid, uid)
}

func (b LookupAccess) isAdmin(ctx context.Context, cid, uid int64) (bool, error) {
	if b.admins[uid] {
		return true, nil
	}
	chats, err := b.adminChats(ctx, cid)
	if err != nil {
		return false, err
	}
	ordinary := false
	for _, chat := range chats {
		person, err := b.chatMember(ctx, chat, uid)
		if err != nil || !person.admin() {
			continue
		}
		bot, err := b.chatMember(ctx, chat, b.botID)
		if err != nil || !bot.admin() {
			continue
		}
		if person.Status == "creator" || person.CanPromote {
			return true, nil
		}
		ordinary = true
	}
	if ordinary {
		policy, err := b.adminPolicy(ctx, cid)
		if err != nil {
			return false, err
		}
		listed := false
		for _, id := range policy.selected() {
			listed = listed || id == uid
		}
		return listed == (policy.Mode == "whitelist"), nil
	}
	return false, nil
}

func (b *Bot) requireMember(ctx context.Context, cid, uid int64) error {
	return b.lookupAccess().requireMember(ctx, cid, uid)
}

func (b LookupAccess) requireMember(ctx context.Context, cid, uid int64) error {
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
		bot, err := b.chatMember(ctx, chat, b.botID)
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
