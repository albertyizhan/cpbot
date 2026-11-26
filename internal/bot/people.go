package bot

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5/pgconn"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

type person struct {
	ID, Version int64
	User        *int64
	Username    *string
	Name, Tags  string
	Observed    float64
}

const personFields = `id,version,user_id,username,name,tags,observed`

func readPerson(row pgx.Row) (person, error) {
	var p person
	err := row.Scan(&p.ID, &p.Version, &p.User, &p.Username, &p.Name, &p.Tags, &p.Observed)
	return p, err
}

func (b *Bot) person(ctx context.Context, cid, id int64) (person, error) {
	p, err := readPerson(b.db.QueryRow(ctx, `SELECT `+personFields+` FROM cpbot.people WHERE (cluster_id,id)=($1,$2)`, cid, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return p, pgstore.UserError("资料不存在，请重新查找。")
	}
	return p, err
}

func displayName(user telegram.User) string {
	name := strings.TrimSpace(strings.Join([]string{user.FirstName, user.LastName}, " "))
	if name == "" {
		return strconv.FormatInt(user.ID, 10)
	}
	runes := []rune(name)
	return string(runes[:min(160, len(runes))])
}

func observePerson(ctx context.Context, tx pgx.Tx, cid int64, user telegram.User, stamp float64) (person, error) {
	if user.ID <= 0 || user.IsBot {
		return person{}, nil
	}
	byID, err := readPerson(tx.QueryRow(ctx, `SELECT `+personFields+` FROM cpbot.people WHERE (cluster_id,user_id)=($1,$2)`, cid, user.ID))
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return person{}, err
	}
	name := strings.ToLower(user.Username)
	var byName person
	if name != "" {
		byName, err = readPerson(tx.QueryRow(ctx, `SELECT `+personFields+` FROM cpbot.people WHERE cluster_id=$1 AND lower(username COLLATE "C")=$2`, cid, name))
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return person{}, err
		}
	}
	if byName.User != nil && *byName.User != user.ID && stamp < byName.Observed {
		return byID, nil
	}
	p := byID
	if p.ID == 0 && byName.User == nil {
		p = byName
	}
	if byName.ID != 0 && byName.ID != p.ID {
		if _, err := tx.Exec(ctx, `UPDATE cpbot.people SET username=NULL WHERE (cluster_id,id)=($1,$2)`, cid, byName.ID); err != nil {
			return person{}, err
		}
	}
	if p.ID == 0 {
		p, err = readPerson(tx.QueryRow(ctx, `INSERT INTO cpbot.people(cluster_id,user_id) VALUES($1,$2) RETURNING `+personFields, cid, user.ID))
		if err != nil {
			return p, err
		}
	}
	if stamp < p.Observed {
		return p, nil
	}
	var username *string
	if name != "" {
		username = &name
	}
	if p.User == nil || (p.Username == nil) != (username == nil) || p.Username != nil && *p.Username != name ||
		p.Name != displayName(user) || p.Observed != stamp {
		p, err = readPerson(tx.QueryRow(ctx, `UPDATE cpbot.people SET user_id=$3,username=$4,name=$5,observed=$6
			WHERE (cluster_id,id)=($1,$2) RETURNING `+personFields, cid, p.ID, user.ID, username, displayName(user), stamp))
		if err != nil {
			return p, err
		}
	}
	if username != nil {
		_, err = tx.Exec(ctx, `UPDATE cpbot.teachers SET user_id=$3 WHERE cluster_id=$1 AND deleted_at IS NULL AND user_id IS NULL
			AND lower(username COLLATE "C")=$2 AND NOT EXISTS(SELECT 1 FROM cpbot.teachers WHERE (cluster_id,user_id)=($1,$3))`, cid, name, user.ID)
	} else {
		// A current observation without a username supersedes the saved hint.
		_, err = tx.Exec(ctx, `UPDATE cpbot.teachers SET username=NULL WHERE (cluster_id,user_id)=($1,$2) AND username IS NOT NULL`,
			cid, user.ID)
	}
	return p, err
}

func (b *Bot) observe(ctx context.Context, cid int64, user telegram.User, stamp int64, chat *telegram.Chat, present bool, joined *int64) (person, error) {
	var p person
	err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		var err error
		p, err = observePerson(ctx, tx, cid, user, float64(stamp))
		if err != nil || p.ID == 0 || chat == nil {
			return err
		}
		return saveMembership(ctx, tx, cid, p.ID, *chat, present, float64(stamp), joined)
	})
	return p, err
}

func saveMembership(ctx context.Context, tx interface {
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
}, cid, pid int64, chat telegram.Chat, present bool, observed float64, joined *int64) error {
	state := 0
	if present {
		state = 1
	}
	var joinedAt *float64
	if joined != nil {
		value := float64(*joined)
		joinedAt = &value
	}
	title := chat.Title
	if title == "" {
		title = strconv.FormatInt(chat.ID, 10)
	}
	_, err := tx.Exec(ctx, `INSERT INTO cpbot.memberships(cluster_id,person,chat_id,title,joined,present,observed)
		VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(cluster_id,person,chat_id) DO UPDATE SET
		joined=coalesce(excluded.joined,memberships.joined),title=excluded.title,present=excluded.present,observed=excluded.observed
		WHERE excluded.observed>=memberships.observed AND (excluded.title,excluded.present,excluded.observed,
		coalesce(excluded.joined,memberships.joined)) IS DISTINCT FROM (memberships.title,memberships.present,memberships.observed,memberships.joined)`,
		cid, pid, chat.ID, title, joinedAt, state, observed)
	return err
}

func (b *Bot) observePrivateTeacher(ctx context.Context, user telegram.User, stamp int64) error {
	if user.ID <= 0 || user.IsBot {
		return nil
	}
	rows, err := b.db.Query(ctx, `SELECT t.cluster_id FROM cpbot.teachers t JOIN cpbot.cluster_authorizations a USING(cluster_id)
		WHERE t.deleted_at IS NULL AND (t.user_id=$1 OR t.user_id IS NULL AND lower(t.username COLLATE "C")=$2)`,
		user.ID, strings.ToLower(user.Username))
	if err != nil {
		return err
	}
	clusters, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return err
	}
	for _, cid := range clusters {
		if _, err := b.observe(ctx, cid, user, stamp, nil, false, nil); err != nil {
			return err
		}
	}
	return nil
}

type target struct {
	user           *telegram.User
	username, name string
	stamp          int64
}

var mention = regexp.MustCompile(`@[A-Za-z][A-Za-z0-9_]{3,31}`)

func identityTargets(msg telegram.Message) []target {
	var candidates []target
	origin := msg.ForwardOrigin
	switch {
	case origin != nil && origin.Type == "user" && origin.Sender != nil:
		candidates = append(candidates, target{user: origin.Sender, stamp: origin.Date})
	case msg.ForwardFrom != nil:
		candidates = append(candidates, target{user: msg.ForwardFrom, stamp: msg.ForwardDate})
	case origin != nil && origin.Type == "hidden_user":
		candidates = append(candidates, target{name: origin.SenderName})
	case msg.ForwardSenderName != "":
		candidates = append(candidates, target{name: msg.ForwardSenderName})
	}
	if reply := msg.Reply; reply != nil && reply.SenderChat == nil && reply.From.ID != 0 {
		candidates = append(candidates, target{user: &reply.From, stamp: reply.Date})
	}
	for _, entities := range [][]telegram.Entity{msg.Entities, msg.CaptionEntities} {
		for _, entity := range entities {
			if entity.Type == "text_mention" && entity.User != nil {
				candidates = append(candidates, target{user: entity.User, stamp: msg.Date})
			}
		}
	}
	for _, text := range []string{msg.Text, msg.Caption} {
		for _, bounds := range mention.FindAllStringIndex(text, -1) {
			if !mentionBoundary(text, bounds[0], bounds[1]) {
				continue
			}
			candidates = append(candidates, target{username: strings.ToLower(text[bounds[0]+1 : bounds[1]])})
		}
	}
	seen := map[string]bool{}
	var out []target
	for _, t := range candidates {
		key := "name:" + t.name
		if t.user != nil {
			if t.user.IsBot || t.user.ID <= 0 {
				continue
			}
			key = "id:" + strconv.FormatInt(t.user.ID, 10)
		} else if t.username != "" {
			key = "username:" + t.username
		}
		if !seen[key] {
			seen[key] = true
			out = append(out, t)
		}
		if len(out) == 5 {
			break
		}
	}
	return out
}

func mentionBoundary(text string, start, end int) bool {
	word := func(r rune) bool { return unicode.IsLetter(r) || unicode.IsNumber(r) || r == '_' }
	if start > 0 {
		r, _ := utf8.DecodeLastRuneInString(text[:start])
		if word(r) || r == '@' {
			return false
		}
	}
	if end < len(text) {
		r, _ := utf8.DecodeRuneInString(text[end:])
		return !word(r)
	}
	return true
}

func (b *Bot) resolve(ctx context.Context, cid int64, t target) (person, string, error) {
	if t.user != nil {
		p, err := b.observe(ctx, cid, *t.user, t.stamp, nil, false, nil)
		return p, "", err
	}
	if t.username != "" {
		p, err := readPerson(b.db.QueryRow(ctx, `SELECT `+personFields+` FROM cpbot.people WHERE cluster_id=$1 AND lower(username COLLATE "C")=$2`, cid, t.username))
		if errors.Is(err, pgx.ErrNoRows) {
			p, err = readPerson(b.db.QueryRow(ctx, `INSERT INTO cpbot.people(cluster_id,username) VALUES($1,$2) RETURNING `+personFields, cid, t.username))
		}
		return p, "", err
	}
	if t.name == "" {
		return person{}, "", pgstore.UserError("无法确认转发来源，请直接回复当事人的消息查询。")
	}
	rows, err := b.db.Query(ctx, `SELECT id FROM cpbot.people WHERE (cluster_id,name)=($1,$2) ORDER BY id LIMIT 2`, cid, t.name)
	if err != nil {
		return person{}, "", err
	}
	ids, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return person{}, "", err
	}
	if len(ids) != 1 {
		return person{}, "", pgstore.UserError("转发来源已隐藏，无法确认身份。请直接回复当事人的消息查询。")
	}
	p, err := b.person(ctx, cid, ids[0])
	return p, fmt.Sprintf("按转发名称「%s」匹配，身份尚未确认。", t.name), err
}

func (b *Bot) lookupPrompt(ctx context.Context, uid int64, state panel) error {
	state.input = "lookup"
	return b.show(ctx, uid, state, "发送客户 ID、@用户名，或转发目标消息。", [][]choice{{{"返回主菜单", "home"}}})
}

func (b *Bot) lookupMessage(ctx context.Context, msg telegram.Message, state panel) error {
	uid := msg.From.ID
	if id, err := strconv.ParseInt(strings.TrimSpace(msg.Text), 10, 64); err == nil && id > 0 {
		p, err := readPerson(b.db.QueryRow(ctx, `INSERT INTO cpbot.people(cluster_id,user_id) VALUES($1,$2)
			ON CONFLICT(cluster_id,user_id) DO UPDATE SET user_id=excluded.user_id RETURNING `+personFields, state.cluster, id))
		if err != nil {
			return err
		}
		return b.personPanel(ctx, uid, state, p, "")
	}
	for _, t := range identityTargets(msg) {
		if t.username == strings.ToLower(b.me.Username) {
			continue
		}
		p, notice, err := b.resolve(ctx, state.cluster, t)
		if err != nil {
			return err
		}
		if p.ID > 0 {
			return b.personPanel(ctx, uid, state, p, notice)
		}
	}
	return pgstore.UserError("请发送客户 ID、@用户名，或转发目标消息。")
}

func (b *Bot) card(ctx context.Context, cid int64, p person, chat int64) (string, error) {
	name, username, user, tags := p.Name, "未知 / 未设置", "待识别", p.Tags
	if name == "" {
		name = "未知"
	}
	if tags == "" {
		tags = "无"
	}
	if p.Username != nil {
		username = "@" + *p.Username
	}
	if p.User != nil {
		user = strconv.FormatInt(*p.User, 10)
	}
	text := fmt.Sprintf("档案编号：%d\n👤 当前名称：%s\nTelegram ID：%s\n用户名：%s\n🏷 群标签：%s", p.ID, name, user, username, tags)
	var target telegram.Chat
	err := b.db.QueryRow(ctx, `SELECT chat_id,title FROM cpbot.membership_targets WHERE cluster_id=$1`, cid).Scan(&target.ID, &target.Title)
	if err == nil {
		prefix := fmt.Sprintf("\n\n🎯 判定群：%s（%d）\n", target.Title, target.ID)
		if p.User == nil {
			return text + prefix + "是否在群：无法确认（尚未识别用户数字 ID）", nil
		}
		bot, err := b.chatMember(ctx, target.ID, b.me.ID)
		if err != nil || !bot.admin() {
			return text + prefix + "是否在群：暂时无法确认", nil
		}
		member, err := b.chatMember(ctx, target.ID, *p.User)
		present := member.presentState()
		if err != nil || present == nil {
			return text + prefix + "是否在群：暂时无法确认", nil
		}
		now := time.Now()
		if err := saveMembership(ctx, b.db, cid, p.ID, target, *present, float64(now.UnixMicro())/1e6, nil); err != nil {
			return "", err
		}
		var joined *float64
		err = b.db.QueryRow(ctx, `SELECT joined FROM cpbot.memberships WHERE (cluster_id,person,chat_id)=($1,$2,$3)`, cid, p.ID, target.ID).Scan(&joined)
		return fmt.Sprintf("%s%s是否在群：%s\n入群时间：%s\n查询时间：%s", text, prefix, yesNo(*present), displayDate(joined), now.In(shanghai).Format("2006-01-02 15:04")), err
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return "", err
	}
	rows, err := b.db.Query(ctx, `SELECT title,joined,present,observed FROM cpbot.memberships WHERE (cluster_id,person)=($1,$2)
		AND ($3=0 OR chat_id=$3) ORDER BY observed DESC LIMIT 8`, cid, p.ID, chat)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	count := 0
	for rows.Next() {
		var title string
		var joined *float64
		var present *int
		var observed float64
		if err := rows.Scan(&title, &joined, &present, &observed); err != nil {
			return "", err
		}
		status := "未知"
		if present != nil {
			status = yesNo(*present == 1)
		}
		text += fmt.Sprintf("\n\n%s\n是否在群：%s\n入群时间：%s\n状态更新：%s", title, status, displayDate(joined), displayDate(&observed))
		count++
	}
	if count == 0 {
		text += "\n是否在群：未知\n入群时间：未知"
	}
	return text, rows.Err()
}

var shanghai, _ = time.LoadLocation("Asia/Shanghai")

func displayDate(value *float64) string {
	if value == nil || *value == 0 {
		return "未知"
	}
	return time.Unix(int64(*value), 0).In(shanghai).Format("2006-01-02 15:04")
}

func yesNo(value bool) string {
	if value {
		return "是"
	}
	return "否"
}

func (b *Bot) personPanel(ctx context.Context, user int64, state panel, p person, notice string) error {
	card, err := b.card(ctx, state.cluster, p, 0)
	if err != nil {
		return err
	}
	if notice != "" {
		card = notice + "\n\n" + card
	}
	id := strconv.FormatInt(p.ID, 10)
	state.input = ""
	return b.show(ctx, user, state, card, [][]choice{{{"新增标签", "edit:" + id}, {"删除标签", "askdelete:" + id}},
		{{"修改记录", "history:" + id + ":0"}}, {{"返回主菜单", "home"}}})
}

func (b *Bot) requireEditor(ctx context.Context, cid, uid int64) error {
	admin, err := b.isAdmin(ctx, cid, uid)
	if err != nil || admin {
		return err
	}
	var teacher bool
	if err := b.db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.teachers WHERE (cluster_id,user_id)=($1,$2) AND deleted_at IS NULL)`, cid, uid).Scan(&teacher); err != nil {
		return err
	}
	if !teacher {
		return pgstore.UserError("仅已绑定老师和集群管理员可以修改学生标签。")
	}
	return nil
}

func (b *Bot) personAction(ctx context.Context, user telegram.User, state panel, kind, argument string) error {
	pidText, extra, _ := strings.Cut(argument, ":")
	pid, _ := strconv.ParseInt(pidText, 10, 64)
	if kind == "entry" {
		return b.historyEntry(ctx, user.ID, state, pid)
	}
	p, err := b.person(ctx, state.cluster, pid)
	if err != nil {
		return err
	}
	if kind == "person" {
		return b.personPanel(ctx, user.ID, state, p, "")
	}
	if kind == "history" {
		page, _ := strconv.Atoi(extra)
		return b.history(ctx, user.ID, state, pid, page)
	}
	if err := b.requireEditor(ctx, state.cluster, user.ID); err != nil {
		return err
	}
	state.object, state.versionID = pid, p.Version
	back := []choice{{"返回资料", "person:" + pidText}}
	switch kind {
	case "edit":
		state.input = "addtags"
		return b.show(ctx, user.ID, state, "发送要追加的标签。", [][]choice{back})
	case "askdelete":
		state.input = ""
		return b.show(ctx, user.ID, state, "确定清空标签？保留身份和修改记录。", [][]choice{
			{{"确认删除", fmt.Sprintf("delete:%d:%d", pid, p.Version)}}, back})
	case "delete":
		version, _ := strconv.ParseInt(extra, 10, 64)
		return b.saveTags(ctx, user, state, p, version, "", "删除标签")
	}
	return nil
}

func (b *Bot) addTags(ctx context.Context, message telegram.Message, state panel) error {
	if err := b.requireEditor(ctx, state.cluster, message.From.ID); err != nil {
		return err
	}
	label := strings.TrimSpace(message.Text)
	if label == "" || strings.ContainsAny(label, "\r\n") {
		return pgstore.UserError("请发送一行标签。")
	}
	p, err := b.person(ctx, state.cluster, state.object)
	if err != nil {
		return err
	}
	tags := label
	if p.Tags != "" {
		tags = p.Tags + "、" + label
	}
	return b.saveTags(ctx, message.From, state, p, state.versionID, tags, "新增标签")
}

func (b *Bot) saveTags(ctx context.Context, user telegram.User, state panel, p person, version int64, tags, action string) error {
	if utf8.RuneCountInString(tags) > 300 {
		return pgstore.UserError("标签最多 300 字。")
	}
	err := pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE cpbot.people SET tags=$4,version=version+1
			WHERE (cluster_id,id,version)=($1,$2,$3)`, state.cluster, p.ID, version, tags)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return pgstore.UserError("资料已被别人修改，请重新打开后编辑。")
		}
		before, _ := json.Marshal([]string{"", p.Tags})
		after, _ := json.Marshal([]string{"", tags})
		_, err = tx.Exec(ctx, `INSERT INTO cpbot.history(cluster_id,person,actor,actor_name,action,before_value,after_value,created)
			VALUES($1,$2,$3,$4,$5,$6,$7,extract(epoch FROM now()))`, state.cluster, p.ID, user.ID, displayName(user), action, string(before), string(after))
		return err
	})
	if err != nil {
		return err
	}
	p.Tags = tags
	p.Version = version + 1
	return b.personPanel(ctx, user.ID, state, p, action+"已保存。")
}

func (b *Bot) history(ctx context.Context, uid int64, state panel, pid int64, page int) error {
	page = max(0, page)
	rows, err := b.db.Query(ctx, `SELECT id,actor_name,action,created FROM cpbot.history
		WHERE (cluster_id,person)=($1,$2) ORDER BY id DESC LIMIT 4 OFFSET $3`, state.cluster, pid, page*3)
	if err != nil {
		return err
	}
	type historyRow struct {
		ID            int64
		Actor, Action string
		Created       float64
	}
	items, err := pgx.CollectRows(rows, pgx.RowToStructByPos[historyRow])
	if err != nil {
		return err
	}
	lines := []string{fmt.Sprintf("修改记录 · 第 %d 页", page+1)}
	buttons := [][]choice{}
	for _, r := range items[:min(len(items), 3)] {
		lines = append(lines, fmt.Sprintf("%s · %s · %s", displayDate(&r.Created), r.Actor, r.Action))
		buttons = append(buttons, []choice{{fmt.Sprintf("查看记录 #%d", r.ID), fmt.Sprintf("entry:%d", r.ID)}})
	}
	if len(items) == 0 {
		lines = append(lines, "暂无记录。")
	}
	buttons = append(buttons, navigation(page, len(items) > 3, fmt.Sprintf("history:%d:", pid))...)
	buttons = append(buttons, []choice{{"返回资料", fmt.Sprintf("person:%d", pid)}})
	state.input = ""
	return b.show(ctx, uid, state, strings.Join(lines, "\n\n"), buttons)
}

func (b *Bot) historyEntry(ctx context.Context, uid int64, state panel, id int64) error {
	var pid, actor int64
	var name, before, after string
	var created float64
	err := b.db.QueryRow(ctx, `SELECT person,actor,actor_name,before_value,after_value,created FROM cpbot.history
		WHERE (cluster_id,id)=($1,$2)`, state.cluster, id).Scan(&pid, &actor, &name, &before, &after, &created)
	if errors.Is(err, pgx.ErrNoRows) {
		return pgstore.UserError("记录不存在。")
	}
	if err != nil {
		return err
	}
	unpack := func(value string) string {
		var fields [2]string
		if json.Unmarshal([]byte(value), &fields) == nil && fields[1] != "" {
			return fields[1]
		}
		return "无"
	}
	text := fmt.Sprintf("操作人：%s（%d）\n时间：%s\n修改前：%s\n修改后：%s", name, actor, displayDate(&created), unpack(before), unpack(after))
	return b.show(ctx, uid, state, text, [][]choice{{{"返回记录", fmt.Sprintf("history:%d:0", pid)}}})
}
