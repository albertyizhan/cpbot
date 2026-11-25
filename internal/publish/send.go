package publish

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

type jobError string

func (e jobError) Error() string { return string(e) }

type review struct {
	data     map[string]json.RawMessage
	status   string
	name     string
	username string
	author   int64
	anon     int
}

func (w *Worker) review(ctx context.Context, j job) (review, error) {
	var r review
	err := w.db.QueryRow(ctx, `SELECT r.data,r.status,t.name,r.author,r.anonymous,coalesce(r.identity->>'username','')
		FROM cpbot.reviews r JOIN cpbot.teachers t ON (t.cluster_id,t.id)=(r.cluster_id,r.teacher)
		WHERE (r.cluster_id,r.id)=($1,$2)`, j.cid, j.review).Scan(&r.data, &r.status, &r.name, &r.author, &r.anon, &r.username)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, jobError("评价已不存在")
	}
	return r, err
}

func renderReview(j job, r review) (string, error) {
	var lines []string
	for _, field := range []string{"时间", "老师", "人照", "颜值", "身材", "服务", "态度", "环境", "综合", "过程"} {
		var value string
		switch field {
		case "老师":
			value = r.name
		case "综合":
			var score float64
			if err := json.Unmarshal(r.data[field], &score); err != nil {
				return "", jobError("评价综合分数据无效")
			}
			value = fmt.Sprintf("%.1f", score)
		default:
			if raw := r.data[field]; len(raw) != 0 && json.Unmarshal(raw, &value) != nil {
				value = string(raw)
			}
			if field == "时间" && value != "" {
				parsed, err := time.Parse("2006-01-02", value)
				if err != nil {
					parsed, err = time.Parse("2006-01-02 15:04", value)
				}
				if err != nil {
					return "", jobError("评价日期数据无效")
				}
				value = parsed.Format("2006-01-02")
			}
		}
		lines = append(lines, fmt.Sprintf("【%s】：%s", field, html.EscapeString(value)))
	}
	if r.anon == 0 {
		line := fmt.Sprintf(`【作者】：<a href="tg://user?id=%d">%d</a>`, r.author, r.author)
		if r.username != "" {
			line += " @" + html.EscapeString(r.username)
		}
		lines = append(lines, line)
	}
	return strings.Join(append(lines, fmt.Sprintf("【评价编号】：%d", j.review)), "\n"), nil
}

func (w *Worker) reviewLink(j job) string {
	return fmt.Sprintf("https://t.me/%s?start=c%d_review_%d", w.username, j.cid, j.teacher)
}

func keyboard(rows [][]button) telegram.Params { return telegram.Params{"inline_keyboard": rows} }
func emptyKeyboard() telegram.Params           { return keyboard([][]button{}) }

type button struct {
	Text  string `json:"text"`
	URL   string `json:"url"`
	Style string `json:"style,omitempty"`
}

func (w *Worker) commentKeyboard(ctx context.Context, j job) (telegram.Params, error) {
	const action = "review_current_teacher"
	const label = "评价当前老师"
	var raw string
	err := w.db.QueryRow(ctx, `SELECT value FROM cpbot.settings WHERE cluster_id=$1 AND key='comment_buttons'`, j.cid).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		return keyboard([][]button{{{Text: label, URL: w.reviewLink(j)}}}), nil
	}
	if err != nil {
		return nil, err
	}
	type cell struct {
		Text    string `json:"text"`
		Content string `json:"content"`
		Style   string `json:"style"`
	}
	layout := struct {
		Rows    int             `json:"rows"`
		Columns int             `json:"columns"`
		Buttons map[string]cell `json:"buttons"`
		Hidden  []string        `json:"hidden"`
	}{}
	if strings.HasPrefix(strings.TrimSpace(raw), "[") {
		var legacy []json.RawMessage
		if json.Unmarshal([]byte(raw), &legacy) != nil || len(legacy) != 2 {
			return nil, jobError("旧版评论按钮布局无效")
		}
		layout.Rows, layout.Columns, layout.Buttons = 1, 2, make(map[string]cell)
		for index, value := range legacy {
			key := fmt.Sprintf("1:%d", index+1)
			if string(value) == "null" {
				continue
			}
			var old struct {
				Text string `json:"text"`
				URL  string `json:"url"`
			}
			if index == 0 && string(value) == `"`+label+`"` {
				layout.Buttons[key] = cell{Text: label, Content: action}
			} else if json.Unmarshal(value, &old) == nil && old.Text != "" && old.URL != "" {
				layout.Buttons[key] = cell{Text: old.Text, Content: old.URL}
			} else {
				return nil, jobError("旧版评论按钮内容无效")
			}
		}
	} else if json.Unmarshal([]byte(raw), &layout) != nil || layout.Rows < 1 || layout.Rows > 4 || layout.Columns < 1 || layout.Columns > 4 {
		return nil, jobError("评论按钮布局无效")
	}
	replacer := strings.NewReplacer("{cluster_id}", strconv.FormatInt(j.cid, 10), "{teacher_id}", strconv.FormatInt(j.teacher, 10),
		"{review_id}", strconv.FormatInt(j.review, 10))
	hidden := make(map[string]bool, len(layout.Hidden))
	for _, key := range layout.Hidden {
		hidden[key] = true
	}
	rows := make([][]button, 0, layout.Rows)
	for row := 1; row <= layout.Rows; row++ {
		var buttons []button
		for column := 1; column <= layout.Columns; column++ {
			key := fmt.Sprintf("%d:%d", row, column)
			if item, ok := layout.Buttons[key]; ok && !hidden[key] {
				url := w.reviewLink(j)
				if item.Content != action {
					url = replacer.Replace(item.Content)
				}
				buttons = append(buttons, button{Text: item.Text, URL: url, Style: item.Style})
			}
		}
		if len(buttons) > 0 {
			rows = append(rows, buttons)
		}
	}
	return keyboard(rows), nil
}

func (w *Worker) send(ctx context.Context, j job) (delivery, error) {
	switch j.kind {
	case "health":
		return delivery{}, w.checkBinding(ctx, j)
	case "post", "checkpost", "summary":
		return w.sendPost(ctx, j)
	case "redactpost":
		if j.post != nil {
			return delivery{}, w.edit(ctx, j.channel, *j.post, "此老师已删除。历史评价已移入回收站。", emptyKeyboard())
		}
		return delivery{}, nil
	case "redact", "redactproof":
		return w.redact(ctx, j)
	case "commentbuttons", "proof", "comment", "check", "feed":
		return w.sendReview(ctx, j)
	default:
		return delivery{}, jobError("不支持的发布任务类型")
	}
}

func (w *Worker) sendPost(ctx context.Context, j job) (delivery, error) {
	if j.generation == nil {
		return delivery{}, jobError("缺少老师专帖记录")
	}
	var name string
	var count int64
	var average *float64
	err := w.db.QueryRow(ctx, `SELECT t.name,count(r.id),avg((r.data->>'综合')::double precision)
		FROM cpbot.teachers t LEFT JOIN cpbot.reviews r
		ON (t.cluster_id,t.id)=(r.cluster_id,r.teacher) AND r.status='accepted'
		WHERE (t.cluster_id,t.id)=($1,$2) GROUP BY t.name`, j.cid, j.teacher).Scan(&name, &count, &average)
	if errors.Is(err, pgx.ErrNoRows) {
		return delivery{}, jobError("老师不存在")
	}
	if err != nil {
		return delivery{}, err
	}
	score := "暂无"
	if average != nil {
		score = fmt.Sprintf("%.1f / 10", *average)
	}
	text := fmt.Sprintf("【老师】：%s\n【综合评分】：%s\n【有效评价】：%d\n\n历史评价请查看本帖评论区。\n\n✍️ <a href=\"%s\">给这位老师写评价</a>",
		html.EscapeString(name), score, count, w.reviewLink(j))
	params := telegram.Params{"chat_id": j.channel, "text": text, "parse_mode": "HTML",
		"reply_markup": emptyKeyboard(), "link_preview_options": telegram.Params{"is_disabled": true}}
	if j.post != nil {
		params["message_id"] = *j.post
		return delivery{}, w.api.Call(ctx, "editMessageText", params, nil)
	}
	return w.newMessage(ctx, "post", params)
}

func (w *Worker) newMessage(ctx context.Context, location string, params telegram.Params) (delivery, error) {
	var message telegram.Message
	if err := w.api.Call(ctx, "sendMessage", params, &message); err != nil {
		return delivery{}, err
	}
	if message.ID <= 0 {
		return delivery{}, &telegram.Error{}
	}
	return delivery{location: location, message: message.ID}, nil
}

func (w *Worker) publication(ctx context.Context, table string, j job, generation int64) (int64, int64, error) {
	var message, chat int64
	name := pgx.Identifier{"cpbot", table}.Sanitize()
	err := w.db.QueryRow(ctx, `SELECT message,chat_id FROM `+name+` WHERE (cluster_id,binding,review,generation)=($1,$2,$3,$4)`,
		j.cid, j.binding, j.review, generation).Scan(&message, &chat)
	if errors.Is(err, pgx.ErrNoRows) {
		err = nil
	}
	return message, chat, err
}

func (w *Worker) sendReview(ctx context.Context, j job) (delivery, error) {
	if j.kind == "commentbuttons" && (j.mode != "thread" || j.replica() != j.revision) {
		return delivery{}, nil
	}
	r, err := w.review(ctx, j)
	if err != nil {
		return delivery{}, err
	}
	message, chat, err := w.publication(ctx, "publications", j, j.replica())
	if err != nil {
		return delivery{}, err
	}
	if j.kind == "proof" {
		return w.sendProof(ctx, j, r, message)
	}
	var markup telegram.Params
	if j.mode == "thread" {
		markup = emptyKeyboard()
		if r.status == "accepted" {
			markup, err = w.commentKeyboard(ctx, j)
			if err != nil {
				return delivery{}, err
			}
		}
	}
	if j.kind == "commentbuttons" {
		if r.status != "accepted" || message == 0 {
			return delivery{}, nil
		}
		return delivery{}, w.api.Call(ctx, "editMessageReplyMarkup", telegram.Params{"chat_id": chat, "message_id": message, "reply_markup": markup}, nil)
	}
	text := "此评价已由管理员撤下。"
	if r.status == "accepted" {
		text, err = renderReview(j, r)
		if err != nil {
			return delivery{}, err
		}
	}
	if message != 0 {
		return delivery{}, w.edit(ctx, chat, message, text, markup)
	}
	if r.status != "accepted" {
		return delivery{}, nil
	}
	params := telegram.Params{"chat_id": j.destination(), "text": text, "parse_mode": "HTML"}
	if j.mode == "thread" {
		params["reply_parameters"] = reply(*j.root)
		params["reply_markup"] = markup
	}
	return w.newMessage(ctx, "publications", params)
}

func reply(message int64) telegram.Params {
	return telegram.Params{"message_id": message, "allow_sending_without_reply": false}
}

var validProof = regexp.MustCompile(`^[a-f0-9]{32}\.jpg$`)

func (w *Worker) proofPath(cid int64, name string) (string, error) {
	if !validProof.MatchString(name) {
		return "", jobError("出击证明文件名无效")
	}
	folder := filepath.Dir(w.sqlitePath)
	if cid != 1 {
		folder = filepath.Join(w.sqlitePath+".clusters", strconv.FormatInt(cid, 10))
	}
	return filepath.Join(folder, "proofs", name), nil
}

func (w *Worker) sendProof(ctx context.Context, j job, r review, textMessage int64) (delivery, error) {
	if textMessage == 0 {
		return delivery{waitForText: true}, nil
	}
	message, chat, err := w.publication(ctx, "proof_publications", j, j.replica())
	if err != nil {
		return delivery{}, err
	}
	if r.status != "accepted" {
		if message != 0 {
			return delivery{}, w.neutralProof(ctx, chat, message, "此评价已由管理员撤下。")
		}
		return delivery{}, nil
	}
	caption := fmt.Sprintf("📷 出击证明 · 车评 #%d", j.review)
	if message != 0 {
		return delivery{}, w.api.Call(ctx, "editMessageCaption", telegram.Params{"chat_id": chat, "message_id": message, "caption": caption}, nil)
	}
	var name string
	if json.Unmarshal(r.data["出击证明"], &name) != nil {
		return delivery{}, jobError("评价缺少出击证明")
	}
	path, err := w.proofPath(j.cid, name)
	if err != nil {
		return delivery{}, err
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > 10<<20 {
		return delivery{}, jobError("出击证明图片缺失或大小无效")
	}
	content, err := os.ReadFile(path)
	if err != nil {
		return delivery{}, jobError("无法读取出击证明图片")
	}
	var sent telegram.Message
	err = w.api.Photo(ctx, "sendPhoto", telegram.Params{"chat_id": j.destination(), "caption": caption,
		"reply_parameters": reply(textMessage)}, "proof.jpg", content, &sent)
	if err != nil {
		return delivery{}, err
	}
	if sent.ID <= 0 {
		return delivery{}, &telegram.Error{}
	}
	return delivery{location: "proof_publications", message: sent.ID}, nil
}

func (w *Worker) edit(ctx context.Context, chat, message int64, text string, markup telegram.Params) error {
	params := telegram.Params{"chat_id": chat, "message_id": message, "text": text, "parse_mode": "HTML"}
	if markup != nil {
		params["reply_markup"] = markup
	}
	return w.api.Call(ctx, "editMessageText", params, nil)
}

func (w *Worker) redact(ctx context.Context, j job) (delivery, error) {
	table := "publications"
	if j.kind == "redactproof" {
		table = "proof_publications"
	}
	message, chat, err := w.publication(ctx, table, j, j.revision)
	if err != nil || message == 0 {
		return delivery{}, err
	}
	err = w.api.Call(ctx, "deleteMessage", telegram.Params{"chat_id": chat, "message_id": message}, nil)
	var remote *telegram.Error
	if !errors.As(err, &remote) || !strings.Contains(strings.ToLower(remote.Description), "message can't be deleted") {
		return delivery{}, err
	}
	if j.kind == "redact" {
		err = w.edit(ctx, chat, message, "此评价已删除。", emptyKeyboard())
	} else {
		err = w.neutralProof(ctx, chat, message, "此评价已删除。")
	}
	return delivery{note: "超过 Telegram 删除时限，已清除公开内容"}, err
}

func (w *Worker) neutralProof(ctx context.Context, chat, message int64, caption string) error {
	imageData := image.NewGray(image.Rect(0, 0, 320, 180))
	for i := range imageData.Pix {
		imageData.Pix[i] = color.Gray{Y: 232}.Y
	}
	var content bytes.Buffer
	if err := png.Encode(&content, imageData); err != nil {
		return err
	}
	return w.api.Photo(ctx, "editMessageMedia", telegram.Params{"chat_id": chat, "message_id": message,
		"media": telegram.Params{"type": "photo", "media": "attach://photo", "caption": caption, "parse_mode": "HTML"}}, "removed.png", content.Bytes(), nil)
}

func unchanged(description string) bool {
	return strings.Contains(strings.ToLower(description), "message is not modified")
}

func missingMessage(description string) bool {
	text := strings.ToLower(description)
	for _, reason := range []string{"message to delete not found", "message to edit not found", "message_id_invalid",
		"message to be replied not found", "message to be replied to not found"} {
		if strings.Contains(text, reason) {
			return true
		}
	}
	return false
}

func (w *Worker) checkBinding(ctx context.Context, j job) error {
	var channel telegram.Chat
	if err := w.api.Call(ctx, "getChat", telegram.Params{"chat_id": j.channel}, &channel); err != nil {
		return bindingError(err, "无法访问绑定的频道或群")
	}
	if j.mode == "lookup" && channel.Type != "group" && channel.Type != "supergroup" ||
		j.mode != "lookup" && channel.Type != "channel" {
		return jobError("目标类型不符合绑定模式")
	}
	if err := w.checkRights(ctx, j.channel, j.mode != "lookup"); err != nil {
		return err
	}
	if j.mode != "thread" {
		return nil
	}
	if j.discussion == nil || channel.LinkedID != *j.discussion {
		return jobError("频道讨论组关联已变化，请重新绑定")
	}
	var group telegram.Chat
	if err := w.api.Call(ctx, "getChat", telegram.Params{"chat_id": *j.discussion}, &group); err != nil {
		return bindingError(err, "无法访问频道关联讨论组")
	}
	if group.LinkedID != j.channel {
		return jobError("频道与讨论组关联关系不一致")
	}
	return w.checkRights(ctx, *j.discussion, false)
}

func (w *Worker) checkRights(ctx context.Context, chat int64, channel bool) error {
	var member struct {
		Status  string `json:"status"`
		CanPost bool   `json:"can_post_messages"`
		CanEdit bool   `json:"can_edit_messages"`
	}
	if err := w.api.Call(ctx, "getChatMember", telegram.Params{"chat_id": chat, "user_id": w.botID}, &member); err != nil {
		return bindingError(err, "无法检查机器人的群或频道权限")
	}
	if member.Status == "creator" {
		return nil
	}
	if member.Status != "administrator" || channel && (!member.CanPost || !member.CanEdit) {
		return jobError("机器人缺少所需的管理员权限")
	}
	return nil
}

func bindingError(err error, message string) error {
	var remote *telegram.Error
	if errors.As(err, &remote) && (remote.Code == 400 || remote.Code == 403) {
		return jobError(message)
	}
	return err
}
