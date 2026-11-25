package bot

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

type panel struct {
	backup     *pendingBackup
	batch      []teacherEntry
	batchUntil time.Time
	version    string
	until      time.Time
	message    int64
	cluster    int64
	actions    map[string]string
	input      string
	mode       string
	object     int64
	versionID  int64
	search     string
	page       int
	submitted  string
}

type choice struct{ label, action string }

func (b *Bot) show(ctx context.Context, uid int64, state panel, text string, rows [][]choice) error {
	return b.showPanel(ctx, uid, state, text, rows, false)
}

func (b *Bot) showHTML(ctx context.Context, uid int64, state panel, text string, rows [][]choice) error {
	return b.showPanel(ctx, uid, state, text, rows, true)
}

func (b *Bot) showPanel(ctx context.Context, uid int64, state panel, text string, rows [][]choice, parseHTML bool) error {
	nonce, err := pgstore.NewToken(5)
	if err != nil {
		return err
	}
	state.version, state.until, state.actions = nonce, time.Now().Add(10*time.Minute), make(map[string]string)
	keyboard := make([][]telegram.Params, 0, len(rows))
	for _, row := range rows {
		buttons := make([]telegram.Params, 0, len(row))
		for _, button := range row {
			index := strconv.Itoa(len(state.actions))
			state.actions[index] = button.action
			buttons = append(buttons, telegram.Params{"text": button.label, "callback_data": nonce + "|" + index})
		}
		keyboard = append(keyboard, buttons)
	}
	params := telegram.Params{"chat_id": uid, "text": text, "reply_markup": telegram.Params{"inline_keyboard": keyboard}}
	if parseHTML {
		params["parse_mode"] = "HTML"
		params["link_preview_options"] = telegram.Params{"is_disabled": true}
	}
	if state.message != 0 {
		params["message_id"] = state.message
		err = b.api.Call(ctx, "editMessageText", params, nil)
		if err == nil {
			b.panels[uid] = state
			return nil
		}
		var remote *telegram.Error
		if !errors.As(err, &remote) || remote.Code != 400 {
			return err
		}
		delete(params, "message_id")
	}
	var result telegram.Message
	if err := b.api.Call(ctx, "sendMessage", params, &result); err != nil {
		return err
	}
	state.message = result.ID
	b.panels[uid] = state
	return nil
}

func (b *Bot) send(ctx context.Context, uid int64, text string) error {
	return b.api.Call(ctx, "sendMessage", telegram.Params{"chat_id": uid, "text": text}, nil)
}

func (b *Bot) alert(ctx context.Context, id, text string, modal bool) error {
	return b.api.Call(ctx, "answerCallbackQuery", telegram.Params{"callback_query_id": id, "text": text, "show_alert": modal}, nil)
}

func (b *Bot) home(ctx context.Context, uid int64, state panel) error {
	text := "🏠 车评主菜单"
	clusters, err := b.availableClusters(ctx, false)
	if err != nil {
		return err
	}
	rows := [][]choice{{{"📋 我的车评", "mine:0"}}}
	if len(clusters) != 0 {
		writable, err := b.availableClusters(ctx, true)
		if err != nil {
			return err
		}
		first := []choice{{"🔎 找老师", "directory"}}
		if len(writable) > 0 {
			first = append([]choice{{"🌷 写车评", "write"}}, first...)
		}
		rows = append([][]choice{first}, []choice{{"👤 查学生", "lookup"}, {"📋 我的车评", "mine:0"}})
	} else {
		text = "⚠️ 尚未配置可用的发布绑定，请联系管理员。"
	}
	managed, err := b.adminClusters(ctx, uid)
	if err != nil {
		return err
	}
	if len(managed) > 0 {
		rows = append(rows, []choice{{"⚙️ 管理后台", "admin.pick:0"}})
	}
	if b.admins[uid] {
		rows = append(rows, []choice{{"👑 Superadmin 后台", "super.pick:0"}})
	}
	return b.show(ctx, uid, panel{message: state.message}, text, rows)
}

func (b *Bot) clusterMenu(ctx context.Context, uid int64, state panel) error {
	clusters, err := b.availableClusters(ctx, state.mode == "write")
	if err != nil {
		return err
	}
	var eligible []cluster
	for _, c := range clusters {
		err := b.requireMember(ctx, c.ID, uid)
		if err == nil {
			eligible = append(eligible, c)
		} else if !memberDenied(err) {
			return err
		}
	}
	if len(eligible) == 0 {
		return pgstore.UserError("暂无可用集群。请先加入对应群或频道，或联系管理员。")
	}
	if len(eligible) == 1 {
		state.cluster = eligible[0].ID
		return b.openClusterMode(ctx, uid, state)
	}
	page := min(max(0, state.page), (len(eligible)-1)/8)
	rows := make([][]choice, 0, 10)
	for _, c := range eligible[page*8 : min(len(eligible), (page+1)*8)] {
		rows = append(rows, []choice{{c.Name, "cluster:" + strconv.FormatInt(c.ID, 10)}})
	}
	rows = append(rows, navigation(page, len(eligible) > (page+1)*8, "writepage:")...)
	return b.show(ctx, uid, panel{message: state.message, mode: state.mode}, "选择集群", append(rows, []choice{{"返回主菜单", "home"}}))
}

func (b *Bot) openClusterMode(ctx context.Context, uid int64, state panel) error {
	switch state.mode {
	case "directory":
		return b.teacherList(ctx, uid, state)
	case "lookup":
		return b.lookupPrompt(ctx, uid, state)
	default:
		return b.draft(ctx, uid, state, "")
	}
}

func navigation(page int, more bool, name string) [][]choice {
	var row []choice
	if page > 0 {
		row = append(row, choice{"上一页", name + strconv.Itoa(page-1)})
	}
	if more {
		row = append(row, choice{"下一页", name + strconv.Itoa(page+1)})
	}
	if len(row) == 0 {
		return nil
	}
	return [][]choice{row}
}

func (b *Bot) draft(ctx context.Context, uid int64, state panel, notice string) error {
	d, err := pgstore.GetDraft(ctx, b.db, state.cluster, uid)
	if err != nil {
		return err
	}
	if d.Token == "" {
		d, err = pgstore.SaveDraft(ctx, b.db, state.cluster, uid, d)
		if err != nil {
			return err
		}
	}
	missing := d.Missing()
	var name string
	var paused bool
	err = b.db.QueryRow(ctx, `SELECT name,EXISTS(SELECT 1 FROM cpbot.settings WHERE (cluster_id,key,value)=($1,'publishing_paused','1'))
		FROM cpbot.clusters WHERE id=$1`, state.cluster).Scan(&name, &paused)
	if err != nil {
		return err
	}
	lines := []string{fmt.Sprintf("集群：%s\n\n📝 车评草稿 · 已填写 %d/%d 项", name, len(pgstore.InputFields)-len(missing), len(pgstore.InputFields))}
	if notice != "" {
		lines = append(lines, notice)
	}
	if paused {
		lines = append(lines, "⏸ 本集群已暂停发布。你仍可提交，恢复后会自动发布。")
	}
	anon := "✅ 匿名"
	if d.Anonymous() {
		lines = append(lines, "🔒 匿名发布。证明图将公开，请先遮挡个人信息。")
	} else {
		anon = "✅ 实名"
		lines = append(lines, "👤 实名发布：公开 Telegram ID、身份链接及已有用户名。证明图将公开。")
	}
	var rows [][]choice
	for i, key := range pgstore.InputFields {
		value := "未填写"
		if valueRaw := d.Data[key]; valueRaw != nil {
			value = fmt.Sprint(valueRaw)
		}
		switch key {
		case "出击证明":
			if !missing[key] {
				value = "已上传 ✅（点击可替换）"
			}
		case "老师":
			if d.TeacherID() > 0 {
				value, err = b.teacherName(ctx, state.cluster, d.TeacherID())
				if errors.Is(err, pgx.ErrNoRows) {
					value, missing[key] = "老师已不可用，请重新选择", true
				} else if err != nil {
					return err
				}
			}
		case "时间":
			if normalized, err := pgstore.ReviewDate(value, true); err == nil {
				value = normalized
			}
		}
		if key == "过程" {
			score := "六项评分填齐后自动计算"
			if number, ok := d.Score(); ok {
				score = fmt.Sprintf("%.1f（自动计算）", number)
			}
			lines = append(lines, "【综合】："+score)
		}
		lines = append(lines, "【"+key+"】："+value)
		label := key
		if !missing[key] {
			label += " ✅"
		}
		if i%2 == 0 {
			rows = append(rows, []choice{})
		}
		rows[len(rows)-1] = append(rows[len(rows)-1], choice{label, "field:" + key})
	}
	rows = append(rows, []choice{{anon, "anon"}, {"预览", "preview"}}, []choice{{"提交", "submit"}, {"返回主菜单", "home"}})
	state.input, state.search, state.page, state.submitted, state.mode = "", "", 0, d.Token, "write"
	return b.show(ctx, uid, state, strings.Join(lines, "\n"), rows)
}

func (b *Bot) preview(ctx context.Context, uid int64, state panel) error {
	d, err := pgstore.GetDraft(ctx, b.db, state.cluster, uid)
	if err != nil {
		return err
	}
	if len(d.Missing()) > 0 {
		return pgstore.UserError("请补齐所有字段后预览。")
	}
	name, err := b.teacherName(ctx, state.cluster, d.TeacherID())
	if errors.Is(err, pgx.ErrNoRows) {
		return pgstore.UserError("这位老师已不存在或进入回收站。")
	}
	if err != nil {
		return err
	}
	lines := []string{"车评预览 · 证明图将随正文公开"}
	for _, key := range pgstore.InputFields {
		if key == "出击证明" {
			continue
		}
		value := fmt.Sprint(d.Data[key])
		if key == "老师" {
			value = name
		}
		if key == "过程" {
			score, _ := d.Score()
			lines = append(lines, fmt.Sprintf("【综合】：%.1f", score))
		}
		lines = append(lines, "【"+key+"】："+value)
	}
	if !d.Anonymous() {
		lines = append(lines, fmt.Sprintf("【作者】：%d", uid))
	}
	return b.show(ctx, uid, state, strings.Join(lines, "\n"), [][]choice{{{"提交", "submit"}, {"返回草稿", "draft"}}})
}

func (b *Bot) teacherName(ctx context.Context, cid, tid int64) (string, error) {
	var name string
	err := b.db.QueryRow(ctx, `SELECT name FROM cpbot.teachers WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NULL`, cid, tid).Scan(&name)
	return name, err
}

func (b *Bot) teacherList(ctx context.Context, uid int64, state panel) error {
	state.page = max(0, state.page)
	rows, err := b.db.Query(ctx, `SELECT t.id,t.name FROM cpbot.teachers t LEFT JOIN cpbot.people p
		ON (p.cluster_id,p.user_id)=(t.cluster_id,t.user_id)
		WHERE t.cluster_id=$1 AND t.deleted_at IS NULL AND
		(($2 NOT LIKE '@%' AND strpos(lower(t.name),lower($2))>0) OR lower(coalesce(p.username,t.username) COLLATE "C")=lower(ltrim($2,'@') COLLATE "C"))
		ORDER BY t.id LIMIT 9 OFFSET $3`, state.cluster, state.search, state.page*8)
	if err != nil {
		return err
	}
	type teacher struct {
		ID   int64
		Name string
	}
	teachers, err := pgx.CollectRows(rows, pgx.RowToStructByPos[teacher])
	if err != nil {
		return err
	}
	choices := make([][]choice, 0, 12)
	for _, t := range teachers[:min(len(teachers), 8)] {
		action := "teacher:" + strconv.FormatInt(t.ID, 10)
		if state.mode == "directory" {
			action = "teacherreviews:" + strconv.FormatInt(t.ID, 10) + ":0"
		}
		choices = append(choices, []choice{{fmt.Sprintf("%s · #%d", t.Name, t.ID), action}})
	}
	choices = append(choices, []choice{{"搜索老师", "teachersearch"}})
	if state.search != "" {
		choices = append(choices, []choice{{"查看全部老师", "teacherall"}})
	}
	choices = append(choices, navigation(state.page, len(teachers) > 8, "teacherpage:")...)
	back := choice{"返回草稿", "draft"}
	if state.mode == "directory" {
		back = choice{"返回主菜单", "home"}
	}
	choices = append(choices, []choice{back})
	state.input = "teachersearch"
	return b.show(ctx, uid, state, "👩‍🏫 选择老师。也可直接发送姓名或 @用户名搜索。", choices)
}

var reviewStart = regexp.MustCompile(`^(?:c([1-9][0-9]{0,18})_)?(review|edit|delete)_([0-9]{1,19})$`)

func (b *Bot) privateMessage(ctx context.Context, msg telegram.Message) error {
	uid := msg.From.ID
	text := strings.TrimSpace(msg.Text)
	parts := strings.Fields(text)
	state, valid := b.panels[uid]
	if len(parts) != 0 {
		command, suffix, _ := strings.Cut(parts[0], "@")
		if suffix != "" && !strings.EqualFold(suffix, b.me.Username) {
			return nil
		}
		switch command {
		case "/admin":
			return b.adminMenu(ctx, uid, panel{}, 0)
		case "/register":
			return b.registerPrompt(ctx, uid, panel{})
		}
		if command == "/start" || command == "/cancel" {
			delete(b.panels, uid)
			if command == "/start" && len(parts) == 2 {
				if match := reviewStart.FindStringSubmatch(parts[1]); match != nil {
					cid := int64(1)
					if match[1] != "" {
						cid, _ = strconv.ParseInt(match[1], 10, 64)
					}
					tid, _ := strconv.ParseInt(match[3], 10, 64)
					if err := b.requireMember(ctx, cid, uid); err != nil {
						return err
					}
					switch match[2] {
					case "review":
						return b.chooseTeacher(ctx, uid, panel{cluster: cid}, tid)
					case "edit":
						return b.personAction(ctx, msg.From, panel{cluster: cid}, "edit", match[3])
					case "delete":
						return b.personAction(ctx, msg.From, panel{cluster: cid}, "askdelete", match[3])
					}
				}
			}
			return b.home(ctx, uid, panel{})
		}
	}
	if !valid || state.input == "" || time.Now().After(state.until) {
		return nil
	}
	if strings.HasPrefix(state.input, "admin.") {
		return b.adminInput(ctx, msg, state)
	}
	if strings.HasPrefix(state.input, "super.") || strings.HasPrefix(state.input, "register.") {
		return b.registrationInput(ctx, msg, state)
	}
	if err := b.requireMember(ctx, state.cluster, uid); err != nil {
		return err
	}
	if state.input == "lookup" {
		return b.lookupMessage(ctx, msg, state)
	}
	if state.input == "addtags" {
		return b.addTags(ctx, msg, state)
	}
	if state.input == "teachersearch" {
		if text == "全部" {
			text = ""
		}
		state.search, state.page = text, 0
		return b.teacherList(ctx, uid, state)
	}
	d, err := pgstore.GetDraft(ctx, b.db, state.cluster, uid)
	if err != nil {
		return err
	}
	switch state.input {
	case "时间":
		d.Data[state.input], err = pgstore.ReviewDate(text, false)
		if err != nil {
			return err
		}
	case "过程":
		if text == "" || utf8.RuneCountInString(text) > 1600 {
			return pgstore.UserError("请发送过程文字，最多 1600 字。")
		}
		d.Data[state.input] = text
	case "出击证明":
		if len(msg.Photos) == 0 {
			return pgstore.UserError("请以照片方式发送一张出击证明，最大 10 MB。")
		}
		file := msg.Photos[len(msg.Photos)-1]
		if file.FileID == "" || file.FileSize > pgstore.MaxProofBytes {
			return pgstore.UserError("照片无效或超过 10 MB，请重新上传。")
		}
		data, err := b.api.Download(ctx, file.FileID, pgstore.MaxProofBytes)
		if err != nil {
			return pgstore.UserError("出击证明下载失败，请重新上传。")
		}
		name, err := pgstore.SaveProof(b.source, state.cluster, data)
		if err != nil {
			return pgstore.UserError("图片保存失败，请检查磁盘空间后重试。")
		}
		d.Data[state.input] = name
	}
	if _, err := pgstore.SaveDraft(ctx, b.db, state.cluster, uid, d); err != nil {
		return err
	}
	return b.draft(ctx, uid, state, "")
}

func (b *Bot) chooseTeacher(ctx context.Context, uid int64, state panel, tid int64) error {
	if _, err := b.teacherName(ctx, state.cluster, tid); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return pgstore.UserError("这位老师已不存在或进入回收站。")
		}
		return err
	}
	d, err := pgstore.GetDraft(ctx, b.db, state.cluster, uid)
	if err != nil {
		return err
	}
	d.Data["老师"] = tid
	if _, err := pgstore.SaveDraft(ctx, b.db, state.cluster, uid, d); err != nil {
		return err
	}
	return b.draft(ctx, uid, state, "")
}

func (b *Bot) callback(ctx context.Context, q telegram.Callback) error {
	state := b.panels[q.From.ID]
	nonce, index, _ := strings.Cut(q.Data, "|")
	action, ok := state.actions[index]
	if q.Message == nil || q.Message.Chat.Type != "private" || q.Message.Chat.ID != q.From.ID ||
		q.Message.ID != state.message || !ok || nonce != state.version || time.Now().After(state.until) {
		return b.alert(ctx, q.ID, "此按钮已过期，请发送 /start。", true)
	}
	if err := b.alert(ctx, q.ID, "", false); err != nil {
		var remote *telegram.Error
		if !errors.As(err, &remote) || remote.Code != 400 {
			return err
		}
	}
	return b.action(ctx, q.From, state, action)
}

func (b *Bot) action(ctx context.Context, user telegram.User, state panel, action string) error {
	kind, argument, _ := strings.Cut(action, ":")
	id, _ := strconv.ParseInt(argument, 10, 64)
	if kind == "admin" || strings.HasPrefix(kind, "admin.") {
		return b.adminAction(ctx, user.ID, state, kind, argument)
	}
	if strings.HasPrefix(kind, "super.") {
		return b.superAction(ctx, user.ID, state, kind, argument)
	}
	switch kind {
	case "home":
		return b.home(ctx, user.ID, state)
	case "write", "directory", "lookup", "writepage":
		if kind != "writepage" {
			state.mode = kind
		}
		state.page = int(id)
		return b.clusterMenu(ctx, user.ID, state)
	case "mine":
		return b.reviewList(ctx, user.ID, state, 0, int(id))
	case "cluster":
		state.cluster = id
	}
	if err := b.requireMember(ctx, state.cluster, user.ID); err != nil {
		return err
	}
	switch kind {
	case "cluster":
		return b.openClusterMode(ctx, user.ID, state)
	case "draft":
		return b.draft(ctx, user.ID, state, "")
	case "teacherreviews":
		tid, page, _ := strings.Cut(argument, ":")
		teacherID, _ := strconv.ParseInt(tid, 10, 64)
		number, _ := strconv.Atoi(page)
		return b.reviewList(ctx, user.ID, state, teacherID, number)
	case "person", "edit", "askdelete", "delete", "history", "entry":
		return b.personAction(ctx, user, state, kind, argument)
	case "field":
		return b.field(ctx, user.ID, state, argument)
	case "teacher":
		state.mode = "write"
		return b.chooseTeacher(ctx, user.ID, state, id)
	case "teachersearch":
		state.input = "teachersearch"
		back := choice{"返回草稿", "draft"}
		if state.mode == "directory" {
			back = choice{"返回老师列表", "teacherall"}
		}
		return b.show(ctx, user.ID, state, "请输入姓名关键词或 @用户名。", [][]choice{{back}})
	case "teacherall", "teacherpage":
		if kind == "teacherall" {
			state.search = ""
		}
		state.page = int(id)
		return b.teacherList(ctx, user.ID, state)
	case "anon", "score":
		d, err := pgstore.GetDraft(ctx, b.db, state.cluster, user.ID)
		if err != nil {
			return err
		}
		if kind == "anon" {
			d.Data["匿名"] = !d.Anonymous()
		} else {
			field, value, _ := strings.Cut(argument, ":")
			score, _ := strconv.Atoi(value)
			d.Data[field] = score
		}
		if _, err := pgstore.SaveDraft(ctx, b.db, state.cluster, user.ID, d); err != nil {
			return err
		}
		return b.draft(ctx, user.ID, state, "")
	case "preview":
		return b.preview(ctx, user.ID, state)
	case "submit":
		review, err := pgstore.Submit(ctx, b.db, b.source, state.cluster, user.ID, state.submitted, user, time.Now())
		if err != nil {
			return err
		}
		return b.show(ctx, user.ID, state, fmt.Sprintf("✅ 车评 #%d 已提交，等待发布。", review), [][]choice{{{"返回主菜单", "home"}}})
	}
	return nil
}

func (b *Bot) field(ctx context.Context, uid int64, state panel, field string) error {
	state.input = ""
	back := []choice{{"返回草稿", "draft"}}
	switch field {
	case "老师":
		return b.teacherList(ctx, uid, state)
	case "时间", "过程", "出击证明":
		state.input = field
		hints := map[string]string{"时间": "请输入评价日期 YYYY-MM-DD。", "过程": "请发送过程文字，最多 1600 字。", "出击证明": "请以照片发送一张证明图，最大 10 MB。图片将公开，请先遮挡个人信息。"}
		return b.show(ctx, uid, state, hints[field], [][]choice{back})
	default:
		var rows [][]choice
		for score := 1; score <= 10; score++ {
			if score%5 == 1 {
				rows = append(rows, nil)
			}
			value := strconv.Itoa(score)
			rows[len(rows)-1] = append(rows[len(rows)-1], choice{value, "score:" + field + ":" + value})
		}
		return b.show(ctx, uid, state, "请选择「"+field+"」的评分。", append(rows, back))
	}
}
