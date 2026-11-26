package bot

import (
	"context"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"cpbot/internal/pgstore"
	"cpbot/internal/publish"
	"cpbot/internal/teacherprofile"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type teacherEntry struct {
	Name     string
	User     *int64
	Username *string
}

func (b *Bot) teacherAdminAction(ctx context.Context, uid int64, state panel, kind, arg string) (bool, error) {
	id, _ := strconv.ParseInt(arg, 10, 64)
	switch kind {
	case "admin.teachers", "admin.trash":
		return true, b.adminTeachers(ctx, uid, state, int(id), kind == "admin.trash")
	case "admin.teacher":
		return true, b.teacherDetail(ctx, uid, state, id, "")
	case "admin.teachernew":
		return true, b.promptAdmin(ctx, uid, state, kind, "发送老师名称即可（1～80 字）。本人账号可在详情另行绑定。", "admin")
	case "admin.teacherbatch":
		state.batch = nil
		return true, b.promptAdmin(ctx, uid, state, kind, "每行一位，最多 100 位。格式为「姓名 @用户名 数字ID」，后两项可省略。确认预览后才保存。", "admin")
	case "admin.batchpage":
		return true, b.batchPanel(ctx, uid, state, int(id))
	case "admin.batchsave":
		if len(state.batch) == 0 || time.Now().After(state.batchUntil) {
			return true, pgstore.UserError("预览已过期，请重新发送名单。")
		}
		entries := state.batch
		if err := b.addTeachers(ctx, state.cluster, uid, entries, true); err != nil {
			return true, err
		}
		state.batch = nil
		return true, b.adminPanel(ctx, uid, state, fmt.Sprintf("已导入 %d 位老师。", len(entries)))
	case "admin.trashdays":
		return true, b.promptAdmin(ctx, uid, state, kind, "请发送保留天数（1～3650）。", "admin.trash:0")
	case "admin.trename", "admin.tbind", "admin.tusername":
		state.object = id
		text := "发送新的老师名称（1～80 字）。"
		if kind == "admin.tbind" {
			text = "发送老师的 Telegram 数字 ID。"
		} else if kind == "admin.tusername" {
			text = "发送老师的 Telegram @用户名，或发送 - 清除登记。已绑定账号会优先使用机器人观测到的用户名。"
		}
		return true, b.promptAdmin(ctx, uid, state, kind, text, fmt.Sprintf("admin.teacher:%d", id))
	case "admin.tunbind", "admin.tdelete", "admin.tpurge":
		label := map[string]string{"admin.tunbind": "解除本人绑定", "admin.tdelete": "删除老师并清除公开评价", "admin.tpurge": "彻底删除老师和评价"}[kind]
		return true, b.show(ctx, uid, state, "确定"+label+"？", [][]choice{{{"确认", fmt.Sprintf("%syes:%d", kind, id)}}, {{"取消", fmt.Sprintf("admin.teacher:%d", id)}}})
	case "admin.tunbindyes":
		if err := b.setTeacher(ctx, uid, state.cluster, id, "tunbind", ""); err != nil {
			return true, err
		}
		return true, b.teacherDetail(ctx, uid, state, id, "已解除本人绑定。")
	case "admin.tdeleteyes":
		if err := b.trashTeacher(ctx, uid, state.cluster, id); err != nil {
			return true, err
		}
		return true, b.adminTeachers(ctx, uid, state, 0, true)
	case "admin.tpurgeyes":
		if err := b.purgeTeacher(ctx, uid, state.cluster, id); err != nil {
			return true, err
		}
		return true, b.adminTeachers(ctx, uid, state, 0, true)
	}
	return false, nil
}

func (b *Bot) teacherAdminInput(ctx context.Context, uid int64, state panel, text string) (bool, error) {
	switch state.input {
	case "admin.teachernew":
		if err := b.addTeachers(ctx, state.cluster, uid, []teacherEntry{{Name: text}}, false); err != nil {
			return true, err
		}
		return true, b.adminPanel(ctx, uid, state, "老师已添加。")
	case "admin.teacherbatch":
		list, err := parseTeacherBatch(text)
		if err != nil {
			return true, err
		}
		if err = validateTeacherBatch(ctx, b.db, state.cluster, list); err != nil {
			return true, err
		}
		state.batch = list
		state.batchUntil = time.Now().Add(10 * time.Minute)
		return true, b.batchPanel(ctx, uid, state, 0)
	case "admin.trashdays":
		days, err := strconv.Atoi(text)
		if err != nil || days < 1 || days > 3650 {
			return true, pgstore.UserError("保留天数须为 1～3650 的整数。")
		}
		if err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
			if err := setting(ctx, tx, state.cluster, "teacher_trash_days", text); err != nil {
				return err
			}
			return logAction(ctx, tx, state.cluster, uid, "设置老师回收站保留天数", days)
		}); err != nil {
			return true, err
		}
		return true, b.adminTeachers(ctx, uid, state, 0, true)
	case "admin.trename", "admin.tbind", "admin.tusername":
		if err := b.setTeacher(ctx, uid, state.cluster, state.object, strings.TrimPrefix(state.input, "admin."), text); err != nil {
			return true, err
		}
		return true, b.teacherDetail(ctx, uid, state, state.object, "老师资料已保存。")
	}
	return false, nil
}

func (b *Bot) adminTeachers(ctx context.Context, uid int64, state panel, page int, trash bool) error {
	page = max(0, page)
	rows, err := b.db.Query(ctx, `SELECT id,name FROM cpbot.teachers WHERE cluster_id=$1 AND (deleted_at IS NOT NULL)=$2 ORDER BY id LIMIT 9 OFFSET $3`, state.cluster, trash, page*8)
	if err != nil {
		return err
	}
	type item struct {
		ID   int64
		Name string
	}
	list, err := pgx.CollectRows(rows, pgx.RowToStructByPos[item])
	if err != nil {
		return err
	}
	var buttons [][]choice
	for _, t := range list[:min(len(list), 8)] {
		buttons = append(buttons, []choice{{fmt.Sprintf("%s · #%d", t.Name, t.ID), fmt.Sprintf("admin.teacher:%d", t.ID)}})
	}
	text, prefix := "👩‍🏫 老师列表", "admin.teachers:"
	if trash {
		text, prefix = "🗑 老师回收站", "admin.trash:"
		var days string
		if err := b.db.QueryRow(ctx, `SELECT coalesce((SELECT value FROM cpbot.settings WHERE (cluster_id,key)=($1,'teacher_trash_days')),'30')`, state.cluster).Scan(&days); err != nil {
			return err
		}
		text += "\n保留天数：" + days
		buttons = append(buttons, []choice{{"设置保留天数", "admin.trashdays"}})
	}
	buttons = append(buttons, navigation(page, len(list) > 8, prefix)...)
	state.input = ""
	return b.show(ctx, uid, state, text, append(buttons, []choice{{"返回管理后台", "admin"}}))
}

func (b *Bot) teacherDetail(ctx context.Context, uid int64, state panel, id int64, notice string) error {
	var t teacherEntry
	var deleted *float64
	var handle string
	err := b.db.QueryRow(ctx, `SELECT t.name,t.user_id,t.username,t.deleted_at,`+teacherprofile.Username+
		` FROM cpbot.teachers t`+teacherprofile.JoinPeople+`WHERE (t.cluster_id,t.id)=($1,$2)`, state.cluster, id).
		Scan(&t.Name, &t.User, &t.Username, &deleted, &handle)
	if errors.Is(err, pgx.ErrNoRows) {
		return pgstore.UserError("老师不存在。")
	}
	if err != nil {
		return err
	}
	text := fmt.Sprintf("👩‍🏫 %s · #%d", t.Name, id)
	if t.User != nil {
		text += fmt.Sprintf("\n已绑定本人账号：%d", *t.User)
	} else if t.Username != nil {
		text += "\n等待识别 @" + *t.Username
	} else {
		text += "\n未绑定本人账号，可按名称搜索。"
	}
	if handle != "" {
		text += "\n车评显示：@" + strings.TrimPrefix(handle, "@")
	} else {
		text += "\n车评用户名：暂无"
	}
	if notice != "" {
		text += "\n" + notice
	}
	var rows [][]choice
	button := func(label, action string) choice { return choice{label, fmt.Sprintf("%s:%d", action, id)} }
	if deleted != nil {
		rows = [][]choice{{button("彻底删除", "admin.tpurge")}, {{"返回回收站", "admin.trash:0"}}}
	} else {
		rows = [][]choice{{button("修改名称", "admin.trename"), button("绑定本人账号（可选）", "admin.tbind")}}
		rows = append(rows, []choice{button("登记用户名", "admin.tusername")})
		if t.User != nil || t.Username != nil {
			rows = append(rows, []choice{button("解除本人绑定", "admin.tunbind")})
		}
		rows = append(rows, []choice{button("删除老师", "admin.tdelete")}, []choice{{"返回老师列表", "admin.teachers:0"}})
	}
	state.input = ""
	return b.show(ctx, uid, state, text, rows)
}

var teacherUsername = regexp.MustCompile(`^[a-z][a-z0-9_]{3,31}$`)
var teacherSeparator = regexp.MustCompile(`[|｜+＋]`)
var decimal = regexp.MustCompile(`^[0-9]+$`)

func parseTeacherBatch(text string) ([]teacherEntry, error) {
	var entries []teacherEntry
	for line, raw := range strings.Split(text, "\n") {
		raw = strings.TrimSpace(raw)
		if raw == "" {
			continue
		}
		var name string
		var fields []string
		if teacherSeparator.MatchString(raw) {
			parts := teacherSeparator.Split(raw, -1)
			if len(parts) > 3 {
				return nil, pgstore.UserError(fmt.Sprintf("第 %d 行字段过多。", line+1))
			}
			name, fields = strings.TrimSpace(parts[0]), parts[1:]
		} else {
			parts := strings.Fields(raw)
			end := len(parts)
			for end > 1 && (strings.HasPrefix(parts[end-1], "@") || decimal.MatchString(parts[end-1])) {
				end--
			}
			name, fields = strings.Join(parts[:end], " "), parts[end:]
		}
		entry := teacherEntry{Name: name}
		for _, field := range fields {
			field = strings.TrimSpace(field)
			if field == "" {
				continue
			}
			if decimal.MatchString(field) {
				id, err := strconv.ParseInt(field, 10, 64)
				if err != nil || id <= 0 || entry.User != nil {
					return nil, pgstore.UserError(fmt.Sprintf("第 %d 行数字 ID 无效或重复。", line+1))
				}
				entry.User = &id
			} else {
				user := strings.ToLower(strings.TrimPrefix(field, "@"))
				if !teacherUsername.MatchString(user) || entry.Username != nil {
					return nil, pgstore.UserError(fmt.Sprintf("第 %d 行用户名无效或重复。", line+1))
				}
				entry.Username = &user
			}
		}
		entries = append(entries, entry)
	}
	if len(entries) < 1 || len(entries) > 100 {
		return nil, pgstore.UserError("每批请填写 1～100 位老师。")
	}
	return entries, nil
}

func validateTeacherBatch(ctx context.Context, db interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, cid int64, list []teacherEntry) error {
	names := map[string]bool{}
	users := map[int64]bool{}
	usernames := map[string]bool{}
	for i, entry := range list {
		if err := validTeacherName(entry.Name); err != nil {
			return err
		}
		if names[entry.Name] || entry.User != nil && users[*entry.User] || entry.Username != nil && usernames[*entry.Username] {
			return pgstore.UserError(fmt.Sprintf("第 %d 位老师的名称或账号在本批中重复。", i+1))
		}
		var found bool
		if err := db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.teachers WHERE cluster_id=$1
			AND (name=$2 OR user_id=$3 OR lower(username COLLATE "C")=$4))`, cid, entry.Name, entry.User, entry.Username).Scan(&found); err != nil {
			return err
		}
		if found {
			return pgstore.UserError(fmt.Sprintf("第 %d 位老师的名称或账号已经存在。", i+1))
		}
		names[entry.Name] = true
		if entry.User != nil {
			users[*entry.User] = true
		}
		if entry.Username != nil {
			usernames[*entry.Username] = true
		}
	}
	return nil
}

func validTeacherName(name string) error {
	if strings.TrimSpace(name) == "" || utf8.RuneCountInString(name) > 80 || strings.ContainsAny(name, "\r\n\x00") {
		return pgstore.UserError("老师名称需要 1～80 字。")
	}
	return nil
}

func (b *Bot) batchPanel(ctx context.Context, uid int64, state panel, page int) error {
	if len(state.batch) == 0 || time.Now().After(state.batchUntil) {
		return pgstore.UserError("预览已过期，请重新发送名单。")
	}
	page = min(max(0, page), (len(state.batch)-1)/8)
	text := fmt.Sprintf("批量导入预览 · 共 %d 位 · 第 %d 页", len(state.batch), page+1)
	for _, t := range state.batch[page*8 : min(len(state.batch), (page+1)*8)] {
		text += "\n" + t.Name
		if t.User != nil {
			text += fmt.Sprintf(" · 绑定 %d", *t.User)
		} else if t.Username != nil {
			text += " · 等待识别 @" + *t.Username
		}
	}
	buttons := navigation(page, len(state.batch) > (page+1)*8, "admin.batchpage:")
	buttons = append(buttons, []choice{{"确认导入", "admin.batchsave"}, {"重新填写", "admin.teacherbatch"}}, []choice{{"取消", "admin"}})
	state.input = ""
	return b.show(ctx, uid, state, text, buttons)
}

func (b *Bot) addTeachers(ctx context.Context, cid, actor int64, list []teacherEntry, batch bool) error {
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		if batch {
			if err := validateTeacherBatch(ctx, tx, cid, list); err != nil {
				return err
			}
		}
		for _, entry := range list {
			if err := validTeacherName(entry.Name); err != nil {
				return err
			}
			var tid int64
			if err := tx.QueryRow(ctx, `INSERT INTO cpbot.teachers(cluster_id,name,user_id,username) VALUES($1,$2,$3,$4) RETURNING id`, cid, entry.Name, entry.User, entry.Username).Scan(&tid); err != nil {
				return err
			}
			if err := queueTeacherSummary(ctx, tx, cid, tid); err != nil {
				return err
			}
			if err := logAction(ctx, tx, cid, actor, "保存老师", tid); err != nil {
				return err
			}
		}
		return nil
	})
}

func queueTeacherSummary(ctx context.Context, tx pgx.Tx, cid, tid int64) error {
	rows, err := tx.Query(ctx, `SELECT id FROM cpbot.bindings WHERE cluster_id=$1 AND mode='thread'`, cid)
	if err != nil {
		return err
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return err
	}
	for _, bid := range ids {
		if err := publish.EnsurePost(ctx, tx, cid, bid, tid); err != nil {
			return err
		}
		if err := publish.Enqueue(ctx, tx, cid, "summary", bid, tid, 0, time.Now().UnixMicro()); err != nil {
			return err
		}
	}
	return nil
}

func (b *Bot) setTeacher(ctx context.Context, uid, cid, id int64, kind, text string) error {
	err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		var tag pgconn.CommandTag
		var err error
		switch kind {
		case "trename":
			if err := validTeacherName(text); err != nil {
				return err
			}
			tag, err = tx.Exec(ctx, `UPDATE cpbot.teachers SET name=$3 WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NULL`, cid, id, text)
		case "tbind":
			account, parseErr := strconv.ParseInt(text, 10, 64)
			if parseErr != nil || account <= 0 {
				return pgstore.UserError("请输入有效的 Telegram 数字 ID。")
			}
			tag, err = tx.Exec(ctx, `UPDATE cpbot.teachers SET user_id=$3,
				username=CASE WHEN user_id IS NULL OR user_id=$3 THEN username END
				WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NULL`, cid, id, account)
		case "tusername":
			var handle *string
			if text != "-" {
				name := strings.ToLower(strings.TrimPrefix(text, "@"))
				if !teacherUsername.MatchString(name) {
					return pgstore.UserError("请输入有效的 Telegram @用户名，或发送 - 清除登记。")
				}
				handle = &name
			}
			var occupied bool
			if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.teachers t JOIN cpbot.people p
				ON p.cluster_id=t.cluster_id AND lower(p.username COLLATE "C")=$3
				WHERE (t.cluster_id,t.id)=($1,$2) AND t.user_id<>p.user_id)`, cid, id, handle).Scan(&occupied); err != nil {
				return err
			}
			if occupied {
				return pgstore.UserError("此用户名属于另一个已知账号，请核对老师的数字 ID。")
			}
			tag, err = tx.Exec(ctx, `UPDATE cpbot.teachers SET username=$3
				WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NULL`, cid, id, handle)
		case "tunbind":
			tag, err = tx.Exec(ctx, `UPDATE cpbot.teachers SET user_id=NULL,username=NULL WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NULL`, cid, id)
		}
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return pgstore.UserError("老师不存在或已进入回收站。")
		}
		if kind == "trename" {
			if err := queueTeacherSummary(ctx, tx, cid, id); err != nil {
				return err
			}
		}
		return logAction(ctx, tx, cid, uid, "修改老师资料", id)
	})
	var constraint *pgconn.PgError
	if errors.As(err, &constraint) && constraint.Code == "23505" {
		return pgstore.UserError("此账号已绑定其他老师，请先解除原绑定。")
	}
	return err
}

func (b *Bot) trashTeacher(ctx context.Context, uid, cid, id int64) error {
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE cpbot.teachers SET deleted_at=extract(epoch FROM now()) WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NULL`, cid, id)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return pgstore.UserError("老师不存在或已进入回收站。")
		}
		statements := []string{
			`UPDATE cpbot.reviews SET status='trashed',expires=NULL WHERE (cluster_id,teacher)=($1,$2)`,
			`UPDATE cpbot.jobs SET state='superseded' WHERE (cluster_id,teacher)=($1,$2) AND state IN ('pending','blocked')`,
			`INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
			SELECT p.cluster_id,'redact',p.binding,r.teacher,r.id,p.generation FROM cpbot.publications p JOIN cpbot.reviews r ON (r.cluster_id,r.id)=(p.cluster_id,p.review) WHERE (r.cluster_id,r.teacher)=($1,$2)
			UNION ALL SELECT p.cluster_id,'redactproof',p.binding,r.teacher,r.id,p.generation FROM cpbot.proof_publications p JOIN cpbot.reviews r ON (r.cluster_id,r.id)=(p.cluster_id,p.review) WHERE (r.cluster_id,r.teacher)=($1,$2)
			UNION ALL SELECT cluster_id,'redactpost',binding,teacher,0,generation FROM cpbot.posts WHERE (cluster_id,teacher)=($1,$2) AND message IS NOT NULL
			ON CONFLICT(cluster_id,kind,binding,teacher,review,generation) DO NOTHING`,
		}
		for _, sql := range statements {
			if _, err := tx.Exec(ctx, sql, cid, id); err != nil {
				return err
			}
		}
		return logAction(ctx, tx, cid, uid, "删除老师（移入回收站）", id)
	})
}

func (b *Bot) purgeTeacher(ctx context.Context, uid, cid, id int64) error {
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		var trash, pending bool
		err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.teachers WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NOT NULL),
			EXISTS(SELECT 1 FROM cpbot.jobs WHERE (cluster_id,teacher)=($1,$2) AND (kind IN('redact','redactproof','redactpost') OR state='uncertain') AND state NOT IN('done','superseded'))`, cid, id).Scan(&trash, &pending)
		if err != nil {
			return err
		}
		if !trash {
			return pgstore.UserError("只能彻底删除回收站中的老师。")
		}
		if pending {
			return pgstore.UserError("公开内容仍在清除或待核对，请处理相关发布任务。")
		}
		rows, err := tx.Query(ctx, `SELECT DISTINCT r.data->>'出击证明' FROM cpbot.reviews r WHERE (r.cluster_id,r.teacher)=($1,$2) AND coalesce(r.data->>'出击证明','')<>''
			AND NOT EXISTS(SELECT 1 FROM cpbot.reviews x WHERE x.cluster_id=$1 AND x.teacher<>$2 AND x.data->>'出击证明'=r.data->>'出击证明')
			AND NOT EXISTS(SELECT 1 FROM cpbot.drafts x WHERE x.cluster_id=$1 AND x.data->>'出击证明'=r.data->>'出击证明')`, cid, id)
		if err != nil {
			return err
		}
		names, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			return err
		}
		queries := []string{
			`UPDATE cpbot.drafts SET data=data-'老师' WHERE cluster_id=$1 AND data->>'老师'=$2::bigint::text`,
			`DELETE FROM cpbot.publications WHERE cluster_id=$1 AND review IN(SELECT id FROM cpbot.reviews WHERE (cluster_id,teacher)=($1,$2))`,
			`DELETE FROM cpbot.proof_publications WHERE cluster_id=$1 AND review IN(SELECT id FROM cpbot.reviews WHERE (cluster_id,teacher)=($1,$2))`,
			`DELETE FROM cpbot.jobs WHERE (cluster_id,teacher)=($1,$2)`,
			`DELETE FROM cpbot.posts WHERE (cluster_id,teacher)=($1,$2)`,
			`DELETE FROM cpbot.codes WHERE (cluster_id,teacher)=($1,$2)`,
			`DELETE FROM cpbot.reviews WHERE (cluster_id,teacher)=($1,$2)`,
			`DELETE FROM cpbot.teachers WHERE (cluster_id,id)=($1,$2)`,
		}
		for _, sql := range queries {
			if _, err := tx.Exec(ctx, sql, cid, id); err != nil {
				return err
			}
		}
		for _, name := range names {
			path, err := pgstore.ProofPath(b.source, cid, name)
			if err != nil {
				return err
			}
			if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return pgstore.UserError("证明图删除失败，请检查服务器文件权限后重试。")
			}
		}
		return logAction(ctx, tx, cid, uid, "彻底删除回收站老师", id)
	})
}
