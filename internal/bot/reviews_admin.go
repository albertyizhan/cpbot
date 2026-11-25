package bot

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"cpbot/internal/pgstore"
	"github.com/jackc/pgx/v5"
)

func (b *Bot) reviewAdminAction(ctx context.Context, uid int64, state panel, kind, arg string) (bool, error) {
	id, _ := strconv.ParseInt(arg, 10, 64)
	switch kind {
	case "admin.review":
		return true, b.promptAdmin(ctx, uid, state, kind, "发送要处理的车评编号。", "admin")
	case "admin.reviewdetail":
		return true, b.reviewAdminPanel(ctx, uid, state, id, "")
	case "admin.reviewedit":
		state.object = id
		return true, b.promptAdmin(ctx, uid, state, kind, `发送要更正的日期、评分或过程，例如 {"人照":8,"过程":"更正内容"}。`, fmt.Sprintf("admin.reviewdetail:%d", id))
	case "admin.withdraw":
		return true, b.show(ctx, uid, state, "撤下后车评不再计分，公开正文和证明图会换成撤下提示。", [][]choice{
			{{"确认撤下", fmt.Sprintf("admin.withdrawyes:%d", id)}}, {{"取消", fmt.Sprintf("admin.reviewdetail:%d", id)}}})
	case "admin.withdrawyes":
		if err := b.changeReview(ctx, uid, state.cluster, id, nil); err != nil {
			return true, err
		}
		return true, b.reviewAdminPanel(ctx, uid, state, id, "车评已撤下，公开消息正在排队更新。")
	case "admin.buttons":
		return true, b.buttonGrid(ctx, uid, state)
	case "admin.button":
		state.mode = arg
		return true, b.buttonDetail(ctx, uid, state)
	case "admin.btcontent", "admin.btname", "admin.bturl", "admin.btcolor":
		hints := map[string]string{"admin.btcontent": "分两行发送显示名称和 HTTP 或 HTTPS 链接。链接可带 {cluster_id}、{teacher_id}、{review_id}。",
			"admin.btname": "发送新的显示名称（1～40 字）。", "admin.bturl": "发送新的 HTTP 或 HTTPS 链接。", "admin.btcolor": "发送蓝色、绿色、红色或无颜色。"}
		return true, b.promptAdmin(ctx, uid, state, kind, hints[kind], "admin.button:"+state.mode)
	case "admin.bttemplate", "admin.btclear", "admin.btshow":
		if err := b.editButton(ctx, uid, state, kind, ""); err != nil {
			return true, err
		}
		return true, b.buttonGrid(ctx, uid, state)
	}
	return false, nil
}

func (b *Bot) reviewAdminInput(ctx context.Context, uid int64, state panel, text string) (bool, error) {
	switch state.input {
	case "admin.review":
		id, err := strconv.ParseInt(text, 10, 64)
		if err != nil || id <= 0 {
			return true, pgstore.UserError("请输入车评编号。")
		}
		return true, b.reviewAdminPanel(ctx, uid, state, id, "")
	case "admin.reviewedit":
		changes := map[string]any{}
		d := json.NewDecoder(strings.NewReader(text))
		d.UseNumber()
		if json.Unmarshal([]byte(text), new(map[string]any)) != nil || d.Decode(&changes) != nil || len(changes) == 0 {
			return true, pgstore.UserError("请发送包含更正字段的 JSON 对象。")
		}
		if err := b.changeReview(ctx, uid, state.cluster, state.object, changes); err != nil {
			return true, err
		}
		return true, b.reviewAdminPanel(ctx, uid, state, state.object, "更正已保存，评分和公开消息正在排队同步。")
	case "admin.btcontent", "admin.btname", "admin.bturl", "admin.btcolor":
		if err := b.editButton(ctx, uid, state, state.input, text); err != nil {
			return true, err
		}
		return true, b.buttonDetail(ctx, uid, state)
	}
	return false, nil
}

func (b *Bot) reviewAdminPanel(ctx context.Context, uid int64, state panel, id int64, notice string) error {
	var status, date, teacher, process string
	err := b.db.QueryRow(ctx, `SELECT r.status,coalesce(r.data->>'时间',''),t.name,coalesce(r.data->>'过程','') FROM cpbot.reviews r
		JOIN cpbot.teachers t ON (t.cluster_id,t.id)=(r.cluster_id,r.teacher) WHERE (r.cluster_id,r.id)=($1,$2)`, state.cluster, id).Scan(&status, &date, &teacher, &process)
	if errors.Is(err, pgx.ErrNoRows) {
		return pgstore.UserError("车评不存在。")
	}
	if err != nil {
		return err
	}
	text := fmt.Sprintf("车评 #%d · %s\n老师：%s\n时间：%s\n过程：%s", id, status, teacher, date, process)
	if notice != "" {
		text += "\n" + notice
	}
	var rows [][]choice
	if status == "accepted" {
		rows = append(rows, []choice{{"更正字段", fmt.Sprintf("admin.reviewedit:%d", id)}, {"撤下车评", fmt.Sprintf("admin.withdraw:%d", id)}})
	}
	rows = append(rows, []choice{{"返回", "admin"}})
	state.input = ""
	return b.show(ctx, uid, state, text, rows)
}

func (b *Bot) changeReview(ctx context.Context, uid, cid, id int64, changes map[string]any) error {
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		var raw []byte
		var teacher int64
		err := tx.QueryRow(ctx, `SELECT teacher,data FROM cpbot.reviews WHERE (cluster_id,id,status)=($1,$2,'accepted') FOR UPDATE`, cid, id).Scan(&teacher, &raw)
		if errors.Is(err, pgx.ErrNoRows) {
			return pgstore.UserError("仅可处理正式有效车评。")
		}
		if err != nil {
			return err
		}
		status := "withdrawn"
		data := map[string]any{}
		decoder := json.NewDecoder(strings.NewReader(string(raw)))
		decoder.UseNumber()
		if err := decoder.Decode(&data); err != nil {
			return err
		}
		if changes != nil {
			status = "accepted"
			scoreChanged := false
			for k, v := range changes {
				switch {
				case slices.Contains(pgstore.ScoreFields[:], k):
					n, ok := v.(json.Number)
					score, err := n.Int64()
					if !ok || err != nil || score < 1 || score > 10 {
						return pgstore.UserError("分数需要 1～10 的整数。")
					}
					scoreChanged = true
				case k == "时间":
					date, err := pgstore.ReviewDate(fmt.Sprint(v), false)
					if err != nil {
						return err
					}
					v = date
				case k == "过程":
					text, ok := v.(string)
					if !ok || strings.TrimSpace(text) == "" || utf8.RuneCountInString(text) > 1600 {
						return pgstore.UserError("过程需要 1～1600 字。")
					}
				default:
					return pgstore.UserError("包含不可修改的字段。")
				}
				data[k] = v
			}
			if scoreChanged {
				score, ok := (pgstore.Draft{Data: data}).Score()
				if !ok {
					return pgstore.UserError("旧版车评需要同时补充人照分数。")
				}
				data["综合"] = score
			}
		}
		if _, err := tx.Exec(ctx, `UPDATE cpbot.reviews SET status=$3,data=$4,version=version+1 WHERE (cluster_id,id)=($1,$2)`, cid, id, status, data); err != nil {
			return err
		}
		queries := []string{
			`INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
			SELECT b.cluster_id,CASE WHEN b.mode='thread' THEN 'check' ELSE 'feed' END,b.id,$3,$2,$4
			FROM cpbot.bindings b WHERE b.cluster_id=$1 AND EXISTS(SELECT 1 FROM cpbot.publications p WHERE (p.cluster_id,p.binding,p.review)=(b.cluster_id,b.id,$2))
			UNION ALL SELECT b.cluster_id,'proof',b.id,$3,$2,$4 FROM cpbot.bindings b
			WHERE b.cluster_id=$1 AND EXISTS(SELECT 1 FROM cpbot.proof_publications p WHERE (p.cluster_id,p.binding,p.review)=(b.cluster_id,b.id,$2))
			UNION ALL SELECT b.cluster_id,'summary',b.id,$3,0,$4 FROM cpbot.bindings b WHERE b.cluster_id=$1 AND b.mode='thread'
			ON CONFLICT(cluster_id,kind,binding,teacher,review,generation) DO NOTHING`,
		}
		for _, sql := range queries {
			if _, err := tx.Exec(ctx, sql, cid, id, teacher, time.Now().UnixMicro()); err != nil {
				return err
			}
		}
		action := "撤下车评"
		if changes != nil {
			action = "更正车评"
		}
		return logAction(ctx, tx, cid, uid, action, id)
	})
}

type commentCell struct {
	Text    string  `json:"text"`
	Content string  `json:"content"`
	Style   *string `json:"style"`
}
type commentLayout struct {
	Rows    int                    `json:"rows"`
	Columns int                    `json:"columns"`
	Buttons map[string]commentCell `json:"buttons"`
	Hidden  []string               `json:"hidden,omitempty"`
}

var reviewButton = commentCell{Text: "评价当前老师", Content: "review_current_teacher"}
var position = regexp.MustCompile(`^[1-4]:[1-4]$`)

func loadLayout(ctx context.Context, db interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, cid int64) (commentLayout, error) {
	layout := commentLayout{Rows: 1, Columns: 2, Buttons: map[string]commentCell{"1:1": reviewButton}}
	var raw string
	err := db.QueryRow(ctx, `SELECT value FROM cpbot.settings WHERE (cluster_id,key)=($1,'comment_buttons')`, cid).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return layout, nil
	}
	if err != nil {
		return layout, err
	}
	layout.Buttons = map[string]commentCell{}
	if strings.HasPrefix(strings.TrimSpace(raw), "[") {
		var legacy []json.RawMessage
		if json.Unmarshal([]byte(raw), &legacy) != nil || len(legacy) != 2 {
			return layout, pgstore.UserError("旧版评论按钮布局无效。")
		}
		for i, item := range legacy {
			key := fmt.Sprintf("1:%d", i+1)
			if string(item) == "null" {
				continue
			}
			var name string
			if i == 0 && json.Unmarshal(item, &name) == nil && name == reviewButton.Text {
				layout.Buttons[key] = reviewButton
				continue
			}
			var old struct {
				Text string `json:"text"`
				URL  string `json:"url"`
			}
			if json.Unmarshal(item, &old) != nil {
				return layout, pgstore.UserError("旧版评论按钮内容无效。")
			}
			layout.Buttons[key] = commentCell{Text: old.Text, Content: old.URL}
		}
		return layout, nil
	}
	if err := json.Unmarshal([]byte(raw), &layout); err != nil {
		return layout, err
	}
	return layout, nil
}

func (b *Bot) buttonGrid(ctx context.Context, uid int64, state panel) error {
	layout, err := loadLayout(ctx, b.db, state.cluster)
	if err != nil {
		return err
	}
	var rows [][]choice
	for r := 1; r <= 4; r++ {
		var row []choice
		for c := 1; c <= 4; c++ {
			key := fmt.Sprintf("%d:%d", r, c)
			label := "＋"
			if button, ok := layout.Buttons[key]; ok {
				label = "✓ " + button.Text
				if r > layout.Rows || c > layout.Columns || slices.Contains(layout.Hidden, key) {
					label = "隐藏 · " + button.Text
				}
			}
			row = append(row, choice{label, "admin.button:" + key})
		}
		rows = append(rows, row)
	}
	state.input = ""
	return b.show(ctx, uid, state, "评论按钮 · 选择格子编辑", append(rows, []choice{{"返回", "admin"}}))
}

func (b *Bot) buttonDetail(ctx context.Context, uid int64, state panel) error {
	layout, err := loadLayout(ctx, b.db, state.cluster)
	if err != nil {
		return err
	}
	key := state.mode
	if !position.MatchString(key) {
		return pgstore.UserError("按钮位置无效。")
	}
	text := "评论按钮 · " + key
	rows := [][]choice{{{"填写名称和链接", "admin.btcontent"}}, {{"套用评价当前老师", "admin.bttemplate"}}}
	if cell, ok := layout.Buttons[key]; ok {
		text += "\n" + cell.Text + "\n" + cell.Content
		if cell.Style != nil {
			text += "\n颜色：" + *cell.Style
		}
		rows = append(rows, []choice{{"改名称", "admin.btname"}, {"改链接", "admin.bturl"}, {"改颜色", "admin.btcolor"}}, []choice{{"清空", "admin.btclear"}})
		if int(key[0]-'0') > layout.Rows || int(key[2]-'0') > layout.Columns || slices.Contains(layout.Hidden, key) {
			rows = append(rows, []choice{{"显示到评论", "admin.btshow"}})
		}
	}
	state.input = ""
	return b.show(ctx, uid, state, text, append(rows, []choice{{"返回格子", "admin.buttons"}}))
}

func validateCell(cell commentCell) error {
	if cell.Text == "" || utf8.RuneCountInString(cell.Text) > 40 || strings.TrimSpace(cell.Text) != cell.Text || strings.ContainsAny(cell.Text, "\r\n") || strings.ContainsFunc(cell.Text, unicode.IsControl) {
		return pgstore.UserError("显示名称需要一行，1～40 字。")
	}
	if cell.Content == reviewButton.Content {
		return nil
	}
	if len(cell.Content) > 1024 || strings.Contains(cell.Content, `\`) || strings.ContainsFunc(cell.Content, func(r rune) bool { return unicode.IsSpace(r) || unicode.IsControl(r) }) {
		return pgstore.UserError("链接无效。")
	}
	r := strings.NewReplacer("{cluster_id}", "1", "{teacher_id}", "1", "{review_id}", "1").Replace(cell.Content)
	u, err := url.Parse(r)
	if err != nil || u.Hostname() == "" || u.User != nil || u.Scheme != "https" && u.Scheme != "http" || strings.ContainsAny(r, "{}") {
		return pgstore.UserError("请输入有效的 HTTP 或 HTTPS 链接。")
	}
	return nil
}

func (b *Bot) editButton(ctx context.Context, uid int64, state panel, kind, text string) error {
	key := state.mode
	if !position.MatchString(key) {
		return pgstore.UserError("按钮位置无效。")
	}
	return pgx.BeginFunc(ctx, b.db, func(tx pgx.Tx) error {
		layout, err := loadLayout(ctx, tx, state.cluster)
		if err != nil {
			return err
		}
		if layout.Buttons == nil {
			layout.Buttons = map[string]commentCell{}
		}
		cell, found := layout.Buttons[key]
		if !found && (kind != "admin.btcontent" && kind != "admin.bttemplate") {
			return pgstore.UserError("此按钮尚未配置。")
		}
		for pos := range layout.Buttons {
			if position.MatchString(pos) && (int(pos[0]-'0') > layout.Rows || int(pos[2]-'0') > layout.Columns) && !slices.Contains(layout.Hidden, pos) {
				layout.Hidden = append(layout.Hidden, pos)
			}
		}
		switch kind {
		case "admin.bttemplate":
			cell = reviewButton
		case "admin.btcontent":
			lines := strings.Split(text, "\n")
			if len(lines) != 2 {
				return pgstore.UserError("请分两行发送显示名称和链接。")
			}
			cell.Text, cell.Content = strings.TrimSpace(lines[0]), strings.TrimSpace(lines[1])
		case "admin.btname":
			cell.Text = text
		case "admin.bturl":
			cell.Content = text
		case "admin.btcolor":
			colors := map[string]string{"蓝色": "primary", "绿色": "success", "红色": "danger", "无颜色": "", "primary": "primary", "success": "success", "danger": "danger"}
			value, ok := colors[text]
			if !ok {
				return pgstore.UserError("颜色可选蓝色、绿色、红色或无颜色。")
			}
			cell.Style = nil
			if value != "" {
				cell.Style = &value
			}
		}
		if kind == "admin.btclear" {
			delete(layout.Buttons, key)
		} else {
			if err := validateCell(cell); err != nil {
				return err
			}
			layout.Buttons[key] = cell
		}
		if kind == "admin.btclear" || kind == "admin.btshow" {
			layout.Hidden = slices.DeleteFunc(layout.Hidden, func(s string) bool { return s == key })
		}
		layout.Rows, layout.Columns = 4, 4
		raw, err := json.Marshal(layout)
		if err != nil {
			return err
		}
		if err := setting(ctx, tx, state.cluster, "comment_buttons", string(raw)); err != nil {
			return err
		}
		if err := queueButtons(ctx, tx, state.cluster); err != nil {
			return err
		}
		return logAction(ctx, tx, state.cluster, uid, "设置评论按钮", key)
	})
}

func queueButtons(ctx context.Context, tx pgx.Tx, cid int64) error {
	_, err := tx.Exec(ctx, `INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
		SELECT r.cluster_id,'commentbuttons',pub.binding,r.teacher,r.id,pub.generation FROM cpbot.publications pub
		JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(pub.cluster_id,pub.binding) AND b.mode='thread'
		JOIN cpbot.reviews r ON (r.cluster_id,r.id)=(pub.cluster_id,pub.review) AND r.status='accepted'
		JOIN cpbot.posts p ON (p.cluster_id,p.binding,p.teacher,p.generation)=(pub.cluster_id,pub.binding,r.teacher,pub.generation)
		WHERE pub.cluster_id=$1 ON CONFLICT(cluster_id,kind,binding,teacher,review,generation) DO UPDATE SET state='pending',due=0,error=''
		WHERE jobs.state NOT IN ('pending','sending')`, cid)
	return err
}
