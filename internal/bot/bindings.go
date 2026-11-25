package bot

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strconv"
	"strings"

	"cpbot/internal/pgstore"
	"cpbot/internal/publish"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

var modes = map[string]string{"thread": "老师专帖", "feed": "车评直发", "lookup": "内部查档"}

type binding struct {
	ID, Chat    int64
	Discussion  *int64
	Mode, Title string
	Active      bool
	Error       string
}

func (b *Bot) bindings(ctx context.Context, uid int64, state panel, page int) error {
	page = max(page, 0)
	rows, err := b.db.Query(ctx, `SELECT id,chat_id,discussion,mode,title,active=1,error FROM cpbot.bindings WHERE cluster_id=$1
		ORDER BY id LIMIT 13 OFFSET $2`, state.cluster, page*12)
	if err != nil {
		return err
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[binding])
	if err != nil {
		return err
	}
	buttons := make([][]choice, 0, 16)
	for _, item := range list[:min(len(list), 12)] {
		label := modes[item.Mode] + " · " + item.Title
		if !item.Active {
			label += "（暂停）"
		}
		buttons = append(buttons, []choice{{label, fmt.Sprintf("admin.binding:%d", item.ID)}})
	}
	buttons = append(buttons, navigation(page, len(list) > 12, "admin.bindings:")...)
	for _, mode := range []string{"thread", "feed", "lookup"} {
		buttons = append(buttons, []choice{{"添加" + modes[mode], "admin.bind:" + mode}})
	}
	buttons = append(buttons, []choice{{"返回", "admin"}})
	state.input = ""
	return b.show(ctx, uid, state, "📣 频道 / 群绑定", buttons)
}

func (b *Bot) getBinding(ctx context.Context, cid, bid int64) (binding, error) {
	var item binding
	err := b.db.QueryRow(ctx, `SELECT id,chat_id,discussion,mode,title,active=1,error FROM cpbot.bindings
		WHERE (cluster_id,id)=($1,$2)`, cid, bid).Scan(&item.ID, &item.Chat, &item.Discussion, &item.Mode, &item.Title, &item.Active, &item.Error)
	if errors.Is(err, pgx.ErrNoRows) {
		return item, pgstore.UserError("绑定不存在。")
	}
	return item, err
}

func (b *Bot) bindingPanel(ctx context.Context, uid int64, state panel, bid int64, notice string) error {
	item, err := b.getBinding(ctx, state.cluster, bid)
	if err != nil {
		return err
	}
	text := fmt.Sprintf("📣 %s · %s\n目标 ID：%d", modes[item.Mode], item.Title, item.Chat)
	if item.Discussion != nil {
		text += fmt.Sprintf("\n关联讨论组：%d", *item.Discussion)
	}
	if item.Active {
		text += "\n✅ 已启用"
	} else {
		text += "\n⏸ 已暂停 · " + item.Error
	}
	var pending, uncertain, waiting, teachers int
	err = b.db.QueryRow(ctx, `SELECT
		(SELECT count(*) FROM cpbot.jobs WHERE (cluster_id,binding,state)=($1,$2,'pending')),
		(SELECT count(*) FROM cpbot.jobs WHERE (cluster_id,binding,state)=($1,$2,'uncertain')),
		(SELECT count(*) FROM cpbot.posts WHERE (cluster_id,binding)=($1,$2) AND message IS NOT NULL AND root IS NULL),
		(SELECT count(*) FROM cpbot.teachers WHERE cluster_id=$1 AND deleted_at IS NULL)`, state.cluster, bid).
		Scan(&pending, &uncertain, &waiting, &teachers)
	if err != nil {
		return err
	}
	text += fmt.Sprintf("\n待处理 %d 项 · 待核对 %d 项", pending, uncertain)
	if item.Mode == "thread" {
		if teachers == 0 {
			text += "\n先添加老师，机器人会为老师创建主帖。"
		} else if waiting > 0 {
			text += fmt.Sprintf("\n%d 个主帖等待讨论串。", waiting)
		}
	}
	if notice != "" {
		text += "\n\n" + notice
	}
	rows := [][]choice{{{"检查权限并恢复", fmt.Sprintf("admin.bindingcheck:%d", bid)}}}
	if item.Mode == "thread" {
		rows = append(rows, []choice{{"登记讨论根消息", fmt.Sprintf("admin.root:%d", bid)}})
	}
	rows = append(rows, []choice{{"解除绑定", fmt.Sprintf("admin.unbind:%d", bid)}, {"返回绑定列表", "admin.bindings:0"}})
	state.input = ""
	return b.show(ctx, uid, state, text, rows)
}

func claimChat(ctx context.Context, tx pgx.Tx, cid, chat int64) error {
	if chat >= 0 {
		return pgstore.UserError("请选择群或频道。")
	}
	var owner int64
	err := tx.QueryRow(ctx, `INSERT INTO cpbot.chat_ownership(chat_id,cluster_id) VALUES($1,$2)
		ON CONFLICT(chat_id) DO UPDATE SET chat_id=excluded.chat_id RETURNING cluster_id`, chat, cid).Scan(&owner)
	if err != nil {
		return err
	}
	if owner != cid {
		return pgstore.UserError(fmt.Sprintf("群或频道 %d 已归属集群 #%d。", chat, owner))
	}
	return nil
}

func releaseUnusedChats(ctx context.Context, tx pgx.Tx, cid int64) error {
	if _, err := tx.Exec(ctx, `DELETE FROM cpbot.chat_ownership o WHERE cluster_id=$1
		AND NOT EXISTS(SELECT 1 FROM cpbot.bindings b WHERE b.cluster_id=o.cluster_id AND (b.chat_id=o.chat_id OR b.discussion=o.chat_id))
		AND NOT EXISTS(SELECT 1 FROM cpbot.membership_targets m WHERE (m.cluster_id,m.chat_id)=(o.cluster_id,o.chat_id))
		AND NOT EXISTS(SELECT 1 FROM cpbot.cluster_management_groups m WHERE (m.cluster_id,m.chat_id)=(o.cluster_id,o.chat_id))`, cid); err != nil {
		return err
	}
	_, err := tx.Exec(ctx, `UPDATE cpbot.settings s SET value=(SELECT coalesce(jsonb_agg(x.chat_id ORDER BY x.chat_id),'[]'::jsonb)::text
		FROM jsonb_array_elements_text(s.value::jsonb) AS ids(value) JOIN cpbot.chat_ownership x ON x.chat_id=ids.value::bigint
		AND x.cluster_id=s.cluster_id) WHERE (s.cluster_id,s.key)=($1,'cluster_member_sources')`, cid)
	return err
}

func (b *Bot) bindingAction(ctx context.Context, uid int64, state panel, kind, arg string) (bool, error) {
	id, _ := strconv.ParseInt(arg, 10, 64)
	switch kind {
	case "admin.bindings":
		return true, b.bindings(ctx, uid, state, int(id))
	case "admin.bind":
		state.mode = arg
		return true, b.promptAdmin(ctx, uid, state, kind, "发送目标群或频道的数字 ID 或 @用户名。请先把机器人设为管理员。", "admin.bindings:0")
	case "admin.binding":
		return true, b.bindingPanel(ctx, uid, state, id, "")
	case "admin.bindingcheck":
		item, err := b.getBinding(ctx, state.cluster, id)
		if err != nil {
			return true, err
		}
		_, discussion, err := b.validateBinding(ctx, uid, item.Mode, item.Chat)
		if err != nil {
			return true, err
		}
		if value(discussion) != value(item.Discussion) {
			return true, pgstore.UserError("讨论组已变更，请解除旧绑定后重新绑定。")
		}
		err = pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, `UPDATE cpbot.bindings SET active=1,error='' WHERE (cluster_id,id)=($1,$2)`, state.cluster, id); err != nil {
				return err
			}
			_, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state='pending',due=0,error='' WHERE (cluster_id,binding,state)=($1,$2,'blocked')`, state.cluster, id)
			return err
		})
		if err != nil {
			return true, err
		}
		return true, b.bindingPanel(ctx, uid, state, id, "权限及关联已核验，绑定已恢复。")
	case "admin.unbind":
		return true, b.show(ctx, uid, state, "解除后此目标停止服务，已发消息保留。", [][]choice{
			{{"确认解除", fmt.Sprintf("admin.unbindyes:%d", id)}}, {{"取消", fmt.Sprintf("admin.binding:%d", id)}}})
	case "admin.unbindyes":
		err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			for _, table := range []string{"publications", "proof_publications", "jobs", "posts", "bindings"} {
				column := "binding"
				if table == "bindings" {
					column = "id"
				}
				if _, err := tx.Exec(ctx, `DELETE FROM cpbot.`+table+` WHERE (cluster_id,`+column+`)=($1,$2)`, state.cluster, id); err != nil {
					return err
				}
			}
			if err := releaseUnusedChats(ctx, tx, state.cluster); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, "解除绑定", id)
		})
		if err != nil {
			return true, err
		}
		return true, b.bindings(ctx, uid, state, 0)
	case "admin.root":
		state.object = id
		return true, b.promptAdmin(ctx, uid, state, kind, "发送老师编号与讨论组根消息 ID，以空格分隔。", fmt.Sprintf("admin.binding:%d", id))
	case "admin.target":
		var title string
		err := b.db.QueryRow(ctx, `SELECT title FROM cpbot.membership_targets WHERE cluster_id=$1`, state.cluster).Scan(&title)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return true, err
		}
		if title == "" {
			title = "未设置"
		}
		return true, b.show(ctx, uid, state, "在群判定目标："+title, [][]choice{
			{{"设置判定目标", "admin.targetset"}, {"取消判定目标", "admin.targetclear"}}, {{"返回", "admin"}}})
	case "admin.targetset":
		return true, b.promptAdmin(ctx, uid, state, kind, "发送目标群的数字 ID 或 @用户名。", "admin.target")
	case "admin.targetclear":
		if err := b.changeTarget(ctx, uid, state.cluster, nil); err != nil {
			return true, err
		}
		return true, b.adminPanel(ctx, uid, state, "已取消在群判定目标。")
	case "admin.sources":
		return true, b.memberSources(ctx, uid, state, int(id))
	case "admin.toggle":
		err := b.toggleSource(ctx, uid, state.cluster, id)
		if err != nil {
			return true, err
		}
		return true, b.memberSources(ctx, uid, state, 0)
	}
	return false, nil
}

func value(value *int64) int64 {
	if value == nil {
		return 0
	}
	return *value
}

func chatInput(text string) (any, error) {
	if id, err := strconv.ParseInt(text, 10, 64); err == nil && id < 0 {
		return id, nil
	}
	if mention.FindString(text) == text && text != "" {
		return text, nil
	}
	return nil, pgstore.UserError("请输入群或频道的负数 ID 或 @用户名。")
}

func (b *Bot) checkedChat(ctx context.Context, input any) (telegram.Chat, error) {
	var chat telegram.Chat
	if err := b.api.Call(ctx, "getChat", telegram.Params{"chat_id": input}, &chat); err != nil {
		return chat, pgstore.UserError("无法访问目标，请检查 ID 或用户名，并确认机器人已加入。")
	}
	if chat.ID >= 0 {
		return chat, pgstore.UserError("请选择群或频道。")
	}
	if chat.Title == "" {
		chat.Title = strconv.FormatInt(chat.ID, 10)
	}
	return chat, nil
}

func (b *Bot) botRights(ctx context.Context, chat telegram.Chat, uid int64, channel bool) error {
	var rights struct {
		Status  string `json:"status"`
		CanPost bool   `json:"can_post_messages"`
		CanEdit bool   `json:"can_edit_messages"`
	}
	if err := b.api.Call(ctx, "getChatMember", telegram.Params{"chat_id": chat.ID, "user_id": b.me.ID}, &rights); err != nil {
		return pgstore.UserError(fmt.Sprintf("无法核验机器人在「%s」中的管理员权限。", chat.Title))
	}
	if rights.Status != "creator" && (rights.Status != "administrator" || channel && (!rights.CanPost || !rights.CanEdit)) {
		return pgstore.UserError(fmt.Sprintf("机器人在「%s」缺少所需的管理员权限。", chat.Title))
	}
	if !b.admins[uid] {
		m, err := b.chatMember(ctx, chat.ID, uid)
		if err != nil || !m.admin() {
			return pgstore.UserError(fmt.Sprintf("你必须是「%s」的当前群主或管理员。", chat.Title))
		}
	}
	return nil
}

func (b *Bot) validateBinding(ctx context.Context, uid int64, mode string, input any) (telegram.Chat, *int64, error) {
	chat, err := b.checkedChat(ctx, input)
	if err != nil {
		return chat, nil, err
	}
	if mode == "lookup" && chat.Type != "group" && chat.Type != "supergroup" || mode != "lookup" && chat.Type != "channel" {
		return chat, nil, pgstore.UserError("目标类型不符合绑定模式。")
	}
	if err := b.botRights(ctx, chat, uid, mode != "lookup"); err != nil {
		return chat, nil, err
	}
	if mode != "thread" {
		return chat, nil, nil
	}
	if chat.LinkedID == 0 {
		return chat, nil, pgstore.UserError("老师专帖需要频道先关联讨论组。")
	}
	group, err := b.checkedChat(ctx, chat.LinkedID)
	if err != nil {
		return chat, nil, err
	}
	if group.LinkedID != chat.ID {
		return chat, nil, pgstore.UserError("频道与讨论组关联关系不一致。")
	}
	return chat, &group.ID, b.botRights(ctx, group, uid, false)
}

func (b *Bot) bindingInput(ctx context.Context, uid int64, state panel, text string) (bool, error) {
	switch state.input {
	case "admin.bind", "admin.targetset":
		input, err := chatInput(text)
		if err != nil {
			return true, err
		}
		if state.input == "admin.targetset" {
			chat, _, err := b.validateBinding(ctx, uid, "lookup", input)
			if err != nil {
				return true, err
			}
			if err := b.changeTarget(ctx, uid, state.cluster, &chat); err != nil {
				return true, err
			}
			return true, b.adminPanel(ctx, uid, state, "在群判定目标已保存。")
		}
		chat, discussion, err := b.validateBinding(ctx, uid, state.mode, input)
		if err != nil {
			return true, err
		}
		var bid int64
		err = pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			for _, id := range []int64{chat.ID, value(discussion)} {
				if id == 0 {
					continue
				}
				if err := claimChat(ctx, tx, state.cluster, id); err != nil {
					return err
				}
				var occupied bool
				if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.bindings WHERE chat_id=$1 OR discussion=$1)`, id).Scan(&occupied); err != nil {
					return err
				}
				if occupied {
					return pgstore.UserError("频道或讨论组已被其他绑定占用。")
				}
			}
			if err := tx.QueryRow(ctx, `INSERT INTO cpbot.bindings(cluster_id,mode,chat_id,discussion,title)
				VALUES($1,$2,$3,$4,$5) RETURNING id`, state.cluster, state.mode, chat.ID, discussion, chat.Title).Scan(&bid); err != nil {
				return err
			}
			if state.mode == "thread" {
				rows, err := tx.Query(ctx, `SELECT id FROM cpbot.teachers WHERE cluster_id=$1 AND deleted_at IS NULL`, state.cluster)
				if err != nil {
					return err
				}
				ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
				if err != nil {
					return err
				}
				for _, tid := range ids {
					if err := publish.EnsurePost(ctx, tx, state.cluster, bid, tid); err != nil {
						return err
					}
				}
			}
			return logAction(ctx, tx, state.cluster, uid, "绑定 "+state.mode, chat.ID)
		})
		if err != nil {
			return true, err
		}
		return true, b.bindingPanel(ctx, uid, state, bid, "绑定已保存。")
	case "admin.root":
		fields := strings.Fields(text)
		if len(fields) != 2 {
			return true, pgstore.UserError("请发送老师编号与讨论根消息 ID，以空格分隔。")
		}
		tid, _ := strconv.ParseInt(fields[0], 10, 64)
		mid, _ := strconv.ParseInt(fields[1], 10, 64)
		if tid <= 0 || mid <= 0 {
			return true, pgstore.UserError("老师编号与讨论根消息 ID 均须为正整数。")
		}
		err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			tag, err := tx.Exec(ctx, `UPDATE cpbot.posts SET root=$4 WHERE (cluster_id,binding,teacher)=($1,$2,$3) AND message IS NOT NULL`, state.cluster, state.object, tid, mid)
			if err != nil {
				return err
			}
			if tag.RowsAffected() != 1 {
				return pgstore.UserError("该老师在此绑定中尚无有效主帖。")
			}
			return logAction(ctx, tx, state.cluster, uid, "登记讨论根消息", mid)
		})
		if err != nil {
			return true, err
		}
		return true, b.bindingPanel(ctx, uid, state, state.object, "讨论根消息已登记。")
	}
	return false, nil
}

func (b *Bot) changeTarget(ctx context.Context, uid, cid int64, chat *telegram.Chat) error {
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		if chat == nil {
			if _, err := tx.Exec(ctx, `DELETE FROM cpbot.membership_targets WHERE cluster_id=$1`, cid); err != nil {
				return err
			}
		} else {
			if err := claimChat(ctx, tx, cid, chat.ID); err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO cpbot.membership_targets(cluster_id,chat_id,title) VALUES($1,$2,$3)
				ON CONFLICT(cluster_id) DO UPDATE SET chat_id=excluded.chat_id,title=excluded.title`, cid, chat.ID, chat.Title); err != nil {
				return err
			}
		}
		if err := releaseUnusedChats(ctx, tx, cid); err != nil {
			return err
		}
		return logAction(ctx, tx, cid, uid, "设置在群判定目标", chat)
	})
}

const memberSourcesSQL = `SELECT DISTINCT ON (chat) chat,title FROM (
	SELECT chat_id AS chat,title FROM cpbot.cluster_management_groups WHERE cluster_id=$1
	UNION ALL SELECT chat_id,title FROM cpbot.bindings WHERE cluster_id=$1 AND active=1
	UNION ALL SELECT discussion,title||' · 讨论组' FROM cpbot.bindings WHERE cluster_id=$1 AND active=1 AND discussion IS NOT NULL
	UNION ALL SELECT chat_id,title FROM cpbot.membership_targets WHERE cluster_id=$1
) sources ORDER BY chat,title`

func getSources(ctx context.Context, db interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, cid int64) ([]int64, error) {
	var raw string
	err := db.QueryRow(ctx, `SELECT value FROM cpbot.settings WHERE (cluster_id,key)=($1,'cluster_member_sources')`, cid).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return []int64{}, nil
	}
	if err != nil {
		return nil, err
	}
	ids := []int64{}
	err = json.Unmarshal([]byte(raw), &ids)
	return ids, err
}

func (b *Bot) memberSources(ctx context.Context, uid int64, state panel, page int) error {
	selected, err := getSources(ctx, b.db, state.cluster)
	if err != nil {
		return err
	}
	page = max(page, 0)
	rows, err := b.db.Query(ctx, memberSourcesSQL+` LIMIT 9 OFFSET $2`, state.cluster, page*8)
	if err != nil {
		return err
	}
	type source struct {
		Chat  int64
		Title string
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[source])
	if err != nil {
		return err
	}
	var buttons [][]choice
	for _, item := range list[:min(len(list), 8)] {
		mark := "◻️ "
		if slices.Contains(selected, item.Chat) {
			mark = "✅ "
		}
		buttons = append(buttons, []choice{{mark + item.Title, fmt.Sprintf("admin.toggle:%d", item.Chat)}})
	}
	buttons = append(buttons, navigation(page, len(list) > 8, "admin.sources:")...)
	buttons = append(buttons, []choice{{"返回", "admin"}})
	return b.show(ctx, uid, state, "成员资格来源\n加入任一启用来源的成员可以使用本集群。判定群始终计入来源。", buttons)
}

func (b *Bot) toggleSource(ctx context.Context, uid, cid, chat int64) error {
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		var exists bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM (`+memberSourcesSQL+`) s WHERE chat=$2)`, cid, chat).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return pgstore.UserError("该群或频道暂不可用，请检查绑定状态。")
		}
		ids, err := getSources(ctx, tx, cid)
		if err != nil {
			return err
		}
		idx := slices.Index(ids, chat)
		if idx < 0 {
			ids = append(ids, chat)
		} else {
			ids = slices.Delete(ids, idx, idx+1)
		}
		slices.Sort(ids)
		raw, _ := json.Marshal(ids)
		if err := setting(ctx, tx, cid, "cluster_member_sources", string(raw)); err != nil {
			return err
		}
		return logAction(ctx, tx, cid, uid, "设置集群成员资格来源", chat)
	})
}
