package bot

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

type adminPolicy struct {
	Mode      string  `json:"mode"`
	Blacklist []int64 `json:"blacklist"`
	Whitelist []int64 `json:"whitelist"`
}

func (p adminPolicy) selected() []int64 {
	if p.Mode == "whitelist" {
		return p.Whitelist
	}
	return p.Blacklist
}

func (b LookupAccess) adminPolicy(ctx context.Context, cid int64) (adminPolicy, error) {
	policy := adminPolicy{Mode: "blacklist"}
	var raw *string
	if err := b.db.QueryRow(ctx, `SELECT (SELECT value FROM cpbot.settings WHERE (cluster_id,key)=($1,'admin_policy'))`, cid).Scan(&raw); err != nil {
		return policy, err
	}
	if raw != nil {
		if err := json.Unmarshal([]byte(*raw), &policy); err != nil {
			return policy, err
		}
	}
	if policy.Mode != "blacklist" && policy.Mode != "whitelist" {
		return policy, fmt.Errorf("invalid admin policy mode %q", policy.Mode)
	}
	return policy, nil
}

func (b LookupAccess) adminChats(ctx context.Context, cid int64) ([]int64, error) {
	rows, err := b.db.Query(ctx, `SELECT chat_id FROM cpbot.cluster_management_groups JOIN cpbot.cluster_authorizations USING(cluster_id) WHERE cluster_id=$1
        UNION SELECT chat_id FROM cpbot.bindings JOIN cpbot.cluster_authorizations USING(cluster_id) WHERE cluster_id=$1 AND active=1
        UNION SELECT discussion FROM cpbot.bindings JOIN cpbot.cluster_authorizations USING(cluster_id) WHERE cluster_id=$1 AND active=1 AND discussion IS NOT NULL`, cid)
	if err != nil {
		return nil, err
	}
	return pgx.CollectRows(rows, pgx.RowTo[int64])
}

type listedAdmin struct {
	User       telegram.User `json:"user"`
	Status     string        `json:"status"`
	CanPromote bool          `json:"can_promote_members"`
	Protected  bool          `json:"-"`
}

func (b *Bot) clusterAdmins(ctx context.Context, cid int64) ([]listedAdmin, error) {
	chats, err := b.lookupAccess().adminChats(ctx, cid)
	if err != nil {
		return nil, err
	}
	admins := map[int64]listedAdmin{}
	for _, chat := range chats {
		bot, err := b.chatMember(ctx, chat, b.me.ID)
		if err != nil || !bot.admin() {
			return nil, pgstore.UserError("无法读取集群管理员，请检查机器人在所有绑定群和频道的管理员权限。")
		}
		var members []listedAdmin
		if err := b.api.Call(ctx, "getChatAdministrators", telegram.Params{"chat_id": chat}, &members); err != nil {
			return nil, pgstore.UserError("无法读取集群管理员，请检查绑定群和频道的状态后重试。")
		}
		for _, item := range members {
			if item.User.IsBot {
				continue
			}
			old := admins[item.User.ID]
			item.Protected = old.Protected || item.Status == "creator" || item.CanPromote || b.admins[item.User.ID]
			admins[item.User.ID] = item
		}
	}
	result := make([]listedAdmin, 0, len(admins))
	for _, item := range admins {
		result = append(result, item)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].User.ID < result[j].User.ID })
	return result, nil
}

func (b *Bot) permissionAction(ctx context.Context, uid int64, state panel, kind string, id int64) (bool, error) {
	if kind != "admin.permissions" && kind != "admin.permmode" && kind != "admin.permtoggle" {
		return false, nil
	}
	admins, err := b.clusterAdmins(ctx, state.cluster)
	if err != nil {
		return true, err
	}
	privileged := b.admins[uid]
	for _, item := range admins {
		if item.User.ID == uid && item.Protected {
			privileged = true
		}
	}
	if !privileged {
		return true, pgstore.UserError("仅群主、拥有添加管理员权限的管理员或 superadmin 可以管理此名单。")
	}
	policy, err := b.lookupAccess().adminPolicy(ctx, state.cluster)
	if err != nil {
		return true, err
	}
	if kind == "admin.permmode" || kind == "admin.permtoggle" {
		if kind == "admin.permmode" {
			if policy.Mode == "blacklist" {
				policy.Mode = "whitelist"
			} else {
				policy.Mode = "blacklist"
			}
		} else {
			found := false
			for _, item := range admins {
				if item.User.ID == id && !item.Protected {
					found = true
				}
			}
			if !found {
				return true, pgstore.UserError("该管理员不存在或拥有不受名单限制的权限。")
			}
			selected := policy.selected()
			next := make([]int64, 0, len(selected)+1)
			exists := false
			for _, entry := range selected {
				if entry == id {
					exists = true
				} else {
					next = append(next, entry)
				}
			}
			if !exists {
				next = append(next, id)
			}
			if policy.Mode == "whitelist" {
				policy.Whitelist = next
			} else {
				policy.Blacklist = next
			}
		}
		raw, err := json.Marshal(policy)
		if err != nil {
			return true, err
		}
		err = pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			if err := setting(ctx, tx, state.cluster, "admin_policy", string(raw)); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, "修改管理权限", string(raw))
		})
		if err != nil {
			return true, err
		}
	}
	page := state.page
	if kind == "admin.permissions" {
		page = int(id)
	}
	page = min(max(page, 0), max(0, (len(admins)-1)/8))
	state.page = page
	label := "黑名单"
	if policy.Mode == "whitelist" {
		label = "白名单"
	}
	text := "🔐 管理权限 · " + label + "\n点击管理员切换名单状态。群主及拥有添加管理员权限的成员始终可以管理机器人。"
	var rows [][]choice
	for _, item := range admins[page*8 : min(len(admins), (page+1)*8)] {
		name := strings.TrimSpace(item.User.FirstName + " " + item.User.LastName)
		if name == "" {
			name = fmt.Sprint(item.User.ID)
		}
		if item.User.Username != "" {
			name += " @" + item.User.Username
		}
		mark := "○"
		for _, entry := range policy.selected() {
			if entry == item.User.ID {
				mark = "✓"
			}
		}
		action := fmt.Sprintf("admin.permtoggle:%d", item.User.ID)
		if item.Protected {
			mark = "🔒"
			action = fmt.Sprintf("admin.permissions:%d", page)
		}
		rows = append(rows, []choice{{fmt.Sprintf("%s %s · %d", mark, name, item.User.ID), action}})
	}
	rows = append(rows, navigation(page, (page+1)*8 < len(admins), "admin.permissions:")...)
	rows = append(rows, []choice{{"切换黑名单 / 白名单", "admin.permmode"}}, []choice{{"返回", "admin"}})
	return true, b.show(ctx, uid, state, text, rows)
}
