package bot

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"unicode/utf8"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type execer interface {
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
}

func logAction(ctx context.Context, db execer, cid, actor int64, action string, detail any) error {
	_, err := db.Exec(ctx, `INSERT INTO cpbot.audit(cluster_id,actor,action,detail,created)
		VALUES($1,$2,$3,$4,extract(epoch FROM now()))`, cid, actor, action, fmt.Sprint(detail))
	return err
}

func setting(ctx context.Context, db execer, cid int64, key, value string) error {
	_, err := db.Exec(ctx, `INSERT INTO cpbot.settings(cluster_id,key,value) VALUES($1,$2,$3)
		ON CONFLICT(cluster_id,key) DO UPDATE SET value=excluded.value`, cid, key, value)
	return err
}

func (b *Bot) requireAdmin(ctx context.Context, cid, uid int64) error {
	allowed, err := b.isAdmin(ctx, cid, uid)
	if err != nil {
		return err
	}
	if !allowed {
		return pgstore.UserError("仅当前集群管理员或 superadmin 可以操作。")
	}
	return nil
}

func (b *Bot) adminClusters(ctx context.Context, uid int64) ([]cluster, error) {
	rows, err := b.db.Query(ctx, `SELECT c.id,c.name FROM cpbot.clusters c LEFT JOIN cpbot.cluster_authorizations a ON c.id=a.cluster_id
		WHERE $1 OR a.cluster_id IS NOT NULL ORDER BY c.id`, b.admins[uid])
	if err != nil {
		return nil, err
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[cluster])
	if err != nil || b.admins[uid] {
		return list, err
	}
	allowed := make([]cluster, 0)
	for _, c := range list {
		admin, err := b.isAdmin(ctx, c.ID, uid)
		if err != nil {
			return nil, err
		}
		if admin {
			allowed = append(allowed, c)
		}
	}
	return allowed, nil
}

func (b *Bot) adminMenu(ctx context.Context, uid int64, state panel, page int) error {
	list, err := b.adminClusters(ctx, uid)
	if err != nil {
		return err
	}
	if len(list) == 0 {
		return pgstore.UserError("当前没有可管理的集群。")
	}
	if len(list) == 1 {
		state.cluster = list[0].ID
		return b.adminPanel(ctx, uid, state, "")
	}
	page = min(max(page, 0), (len(list)-1)/8)
	var buttons [][]choice
	for _, c := range list[page*8 : min(len(list), (page+1)*8)] {
		buttons = append(buttons, []choice{{c.Name, fmt.Sprintf("admin.open:%d", c.ID)}})
	}
	buttons = append(buttons, navigation(page, (page+1)*8 < len(list), "admin.pick:")...)
	buttons = append(buttons, []choice{{"返回主菜单", "home"}})
	return b.show(ctx, uid, panel{message: state.message}, "选择要管理的集群", buttons)
}

func (b *Bot) adminPanel(ctx context.Context, uid int64, state panel, notice string) error {
	var name string
	var teacher, ready, review int
	var paused bool
	err := b.db.QueryRow(ctx, `SELECT name,
		(SELECT count(*) FROM cpbot.teachers WHERE cluster_id=$1 AND deleted_at IS NULL),
		(SELECT count(*) FROM cpbot.bindings WHERE cluster_id=$1 AND active=1 AND mode IN ('thread','feed')),
		(SELECT count(*) FROM cpbot.reviews WHERE cluster_id=$1 AND status='accepted'),
		EXISTS(SELECT 1 FROM cpbot.settings WHERE (cluster_id,key,value)=($1,'publishing_paused','1')) FROM cpbot.clusters WHERE id=$1`,
		state.cluster).Scan(&name, &teacher, &ready, &review, &paused)
	if err != nil {
		return err
	}
	status := "▶️ 发布队列运行中"
	if paused {
		status = "⏸ 发布已暂停"
	}
	text := fmt.Sprintf("⚙️ 管理后台 · 集群 %s\n👩‍🏫 老师 %d 位 · 📣 可用发布频道 %d 个\n📝 有效车评 %d 条 · %s", name, teacher, ready, review, status)
	if notice != "" {
		text += "\n\n" + notice
	}
	state.input = ""
	rows := [][]choice{
		{{"修改集群名称", "admin.rename"}, {"选择集群", "admin.pick:0"}},
		{{"👩‍🏫 老师列表", "admin.teachers:0"}, {"➕ 添加老师", "admin.teachernew"}},
		{{"📥 批量导入老师", "admin.teacherbatch"}, {"🗑 老师回收站", "admin.trash:0"}},
		{{"📣 频道 / 群绑定", "admin.bindings:0"}, {"成员资格来源", "admin.sources:0"}},
		{{"🎯 在群判定目标", "admin.target"}, {"评论按钮", "admin.buttons"}},
		{{"🔐 管理权限", "admin.permissions:0"}},
		{{"📤 发布任务", "admin.jobs:0"}, {"🛠 车评处理", "admin.review"}},
		{{"💾 导出数据备份", "admin.export"}}, {{"返回主菜单", "home"}},
	}
	if b.admins[uid] {
		rows[len(rows)-2] = append(rows[len(rows)-2], choice{"📥 导入数据备份", "admin.import"})
	}
	return b.show(ctx, uid, state, text, rows)
}

func (b *Bot) promptAdmin(ctx context.Context, uid int64, state panel, name, text, back string) error {
	state.input = name
	return b.show(ctx, uid, state, text, [][]choice{{{"取消 / 返回", back}}})
}

func (b *Bot) adminAction(ctx context.Context, uid int64, state panel, kind, arg string) error {
	id, _ := strconv.ParseInt(arg, 10, 64)
	if kind == "admin.pick" {
		return b.adminMenu(ctx, uid, state, int(id))
	}
	if kind == "admin.open" {
		state.cluster = id
	}
	if err := b.requireAdmin(ctx, state.cluster, uid); err != nil {
		return err
	}
	if handled, err := b.permissionAction(ctx, uid, state, kind, id); handled {
		return err
	}
	switch kind {
	case "admin.open", "admin":
		return b.adminPanel(ctx, uid, state, "")
	case "admin.rename":
		return b.promptAdmin(ctx, uid, state, kind, "请发送集群名称，1～40 个可显示字符。", "admin")
	case "admin.jobs":
		return b.jobList(ctx, uid, state, int(id))
	case "admin.job":
		return b.jobDetail(ctx, uid, state, id)
	case "admin.paused", "admin.resumed":
		value, action := "1", "暂停发布"
		if kind == "admin.resumed" {
			value, action = "0", "继续发布"
		}
		err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			if err := setting(ctx, tx, state.cluster, "publishing_paused", value); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, action, "")
		})
		if err != nil {
			return err
		}
		return b.jobList(ctx, uid, state, 0)
	case "admin.retry":
		var current string
		err := b.db.QueryRow(ctx, `SELECT state FROM cpbot.jobs WHERE (cluster_id,id)=($1,$2)`, state.cluster, id).Scan(&current)
		if errors.Is(err, pgx.ErrNoRows) {
			return pgstore.UserError("任务不存在。")
		}
		if err != nil {
			return err
		}
		if current == "uncertain" {
			if err := b.publisher.RetryUncertain(ctx, state.cluster, id, uid); err != nil {
				return pgstore.UserError(err.Error())
			}
		} else if current == "blocked" {
			if err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
				if _, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state='pending',due=0,error='' WHERE (cluster_id,id,state)=($1,$2,'blocked')`, state.cluster, id); err != nil {
					return err
				}
				return logAction(ctx, tx, state.cluster, uid, "人工重试任务", id)
			}); err != nil {
				return err
			}
		} else {
			return pgstore.UserError("当前任务无需人工重试。")
		}
		return b.jobList(ctx, uid, state, 0)
	case "admin.jobdone":
		state.object = id
		return b.promptAdmin(ctx, uid, state, kind, "发送目标中已发出消息的数字 message_id。", "admin.jobs:0")
	}
	if handled, err := b.teacherAdminAction(ctx, uid, state, kind, arg); handled {
		return err
	}
	if handled, err := b.bindingAction(ctx, uid, state, kind, arg); handled {
		return err
	}
	if handled, err := b.reviewAdminAction(ctx, uid, state, kind, arg); handled {
		return err
	}
	if handled, err := b.backupAction(ctx, uid, state, kind); handled {
		return err
	}
	return pgstore.UserError("操作已失效，请返回管理后台。")
}

func (b *Bot) adminInput(ctx context.Context, message telegram.Message, state panel) error {
	uid, text := message.From.ID, strings.TrimSpace(message.Text)
	if err := b.requireAdmin(ctx, state.cluster, uid); err != nil {
		return err
	}
	switch state.input {
	case "admin.rename":
		if len(text) == 0 || utf8.RuneCountInString(text) > 40 || strings.ContainsAny(text, "\r\n\x00") {
			return pgstore.UserError("请发送 1～40 个可显示字符作为集群名称。")
		}
		err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, `UPDATE cpbot.clusters SET name=$2 WHERE id=$1`, state.cluster, text); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, "集群改名", text)
		})
		if err != nil {
			return err
		}
		return b.adminPanel(ctx, uid, state, "集群名称已保存。")
	case "admin.jobdone":
		id, err := strconv.ParseInt(text, 10, 64)
		if err != nil || id <= 0 {
			return pgstore.UserError("消息 ID 必须为正整数。")
		}
		if err := b.publisher.ConfirmUncertain(ctx, state.cluster, state.object, uid, id); err != nil {
			return pgstore.UserError(err.Error())
		}
		return b.jobList(ctx, uid, state, 0)
	}
	if handled, err := b.teacherAdminInput(ctx, uid, state, text); handled {
		return err
	}
	if handled, err := b.bindingInput(ctx, uid, state, text); handled {
		return err
	}
	if handled, err := b.reviewAdminInput(ctx, uid, state, text); handled {
		return err
	}
	if handled, err := b.backupInput(ctx, message, state); handled {
		return err
	}
	return pgstore.UserError("操作已失效，请返回管理后台。")
}

var jobStates = map[string]string{"pending": "⏳ 待处理", "sending": "📤 请求中", "done": "✅ 完成", "blocked": "⚠️ 受阻", "uncertain": "❓ 待核对", "superseded": "已替代"}

func (b *Bot) jobList(ctx context.Context, uid int64, state panel, page int) error {
	page = max(0, page)
	var paused bool
	err := b.db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.settings WHERE (cluster_id,key,value)=($1,'publishing_paused','1'))`, state.cluster).Scan(&paused)
	if err != nil {
		return err
	}
	text := "📤 发布任务 · ▶️ 运行中"
	if paused {
		text = "📤 发布任务 · ⏸ 已暂停"
	}
	counts, err := b.db.Query(ctx, `SELECT state,count(*) FROM cpbot.jobs WHERE cluster_id=$1 GROUP BY state ORDER BY state`, state.cluster)
	if err != nil {
		return err
	}
	for counts.Next() {
		var status string
		var count int
		if err := counts.Scan(&status, &count); err != nil {
			counts.Close()
			return err
		}
		text += fmt.Sprintf("\n%s：%d", jobStates[status], count)
	}
	if err := counts.Err(); err != nil {
		return err
	}
	rows, err := b.db.Query(ctx, `SELECT id,state,error FROM cpbot.jobs WHERE cluster_id=$1 AND state IN ('uncertain','blocked')
		ORDER BY id LIMIT 9 OFFSET $2`, state.cluster, page*8)
	if err != nil {
		return err
	}
	type issue struct {
		ID           int64
		State, Error string
	}
	issues, err := pgx.CollectRows(rows, pgx.RowToStructByPos[issue])
	if err != nil {
		return err
	}
	var buttons [][]choice
	for _, item := range issues[:min(len(issues), 8)] {
		reason := []rune(item.Error)
		buttons = append(buttons, []choice{{fmt.Sprintf("#%d %s · %s", item.ID, jobStates[item.State], string(reason[:min(20, len(reason))])), fmt.Sprintf("admin.job:%d", item.ID)}})
	}
	buttons = append(buttons, navigation(page, len(issues) > 8, "admin.jobs:")...)
	buttons = append(buttons, []choice{{"暂停任务", "admin.paused"}, {"继续任务", "admin.resumed"}}, []choice{{"返回", "admin"}})
	state.input = ""
	return b.show(ctx, uid, state, text, buttons)
}

func (b *Bot) jobDetail(ctx context.Context, user int64, state panel, id int64) error {
	var kind, status, detail string
	err := b.db.QueryRow(ctx, `SELECT kind,state,error FROM cpbot.jobs WHERE (cluster_id,id)=($1,$2)`, state.cluster, id).Scan(&kind, &status, &detail)
	if errors.Is(err, pgx.ErrNoRows) {
		return pgstore.UserError("任务不存在。")
	}
	if err != nil {
		return err
	}
	labels := map[string]string{"post": "创建老师主帖", "comment": "发布评价评论", "feed": "频道直发", "summary": "更新总评价", "checkpost": "巡检主帖",
		"check": "巡检评价", "health": "检查权限", "proof": "发布 / 巡检证明图", "commentbuttons": "更新评论按钮", "redactpost": "清除老师主帖",
		"redact": "清除评价正文", "redactproof": "清除证明图"}
	text := fmt.Sprintf("📤 任务 #%d · %s\n%s\n%s", id, labels[kind], jobStates[status], detail)
	var buttons [][]choice
	if status == "uncertain" {
		text += "\n请先核对发布目标，确认远端没有收到消息后再重试。"
		buttons = append(buttons, []choice{{"确认未发送，重试", fmt.Sprintf("admin.retry:%d", id)}, {"已发送，登记消息 ID", fmt.Sprintf("admin.jobdone:%d", id)}})
	} else if status == "blocked" {
		buttons = append(buttons, []choice{{"重试", fmt.Sprintf("admin.retry:%d", id)}})
	}
	return b.show(ctx, user, state, text, append(buttons, []choice{{"返回", "admin.jobs:0"}}))
}
