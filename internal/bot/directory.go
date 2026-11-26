package bot

import (
	"context"
	"errors"
	"fmt"
	"html"
	"strconv"
	"strings"

	"cpbot/internal/pgstore"
	"github.com/jackc/pgx/v5"
)

type reviewListing struct {
	ID      int64
	Cluster string
	Teacher string
	Date    string
	Chat    *int64
	Message *int64
}

func (b *Bot) reviewList(ctx context.Context, uid int64, state panel, teacher int64, page int) error {
	page = max(0, page)
	var title, filter, order string
	var args []any
	if teacher > 0 {
		name, err := b.teacherName(ctx, state.cluster, teacher)
		if errors.Is(err, pgx.ErrNoRows) {
			return pgstore.UserError("这位老师已不存在或进入回收站。")
		}
		if err != nil {
			return err
		}
		title, filter = "👩‍🏫 "+name+" · 车评", "r.cluster_id=$1 AND r.teacher=$2"
		args = []any{state.cluster, teacher, page * 8}
		order = "r.data->>'时间' DESC,r.id DESC"
	} else {
		title, filter = "📋 我的车评", "r.author=$1 AND $2=0"
		args = []any{uid, 0, page * 8}
		order = "r.created DESC,r.cluster_id,r.id DESC"
	}
	rows, err := b.db.Query(ctx, `SELECT r.id,c.name,t.name,coalesce(r.data->>'时间',''),pub.chat_id,pub.message FROM cpbot.reviews r
		JOIN cpbot.clusters c ON c.id=r.cluster_id JOIN cpbot.teachers t ON (t.cluster_id,t.id)=(r.cluster_id,r.teacher) `+
		pgstore.BestPublication+` WHERE r.status='accepted' AND `+filter+` ORDER BY `+order+` LIMIT 9 OFFSET $3`, args...)
	if err != nil {
		return err
	}
	reviews, err := pgx.CollectRows(rows, pgx.RowToStructByPos[reviewListing])
	if err != nil {
		return err
	}
	lines := []string{fmt.Sprintf("%s · 第 %d 页", html.EscapeString(title), page+1)}
	buttons := [][]choice{}
	for _, item := range reviews[:min(len(reviews), 8)] {
		line := fmt.Sprintf("#%d · %s", item.ID, html.EscapeString(item.Date))
		if teacher == 0 {
			line += " · " + html.EscapeString(item.Teacher) + " · " + html.EscapeString(item.Cluster)
		}
		if item.Chat == nil || item.Message == nil {
			line += " · 暂无可用链接"
		} else {
			chat := strings.TrimPrefix(strconv.FormatInt(*item.Chat, 10), "-100")
			line = fmt.Sprintf(`<a href="https://t.me/c/%s/%d">%s</a>`, chat, *item.Message, line)
		}
		lines = append(lines, line)
	}
	if len(reviews) == 0 {
		lines = append(lines, "暂无有效车评。")
	}
	prefix := "mine:"
	if teacher > 0 {
		prefix = fmt.Sprintf("teacherreviews:%d:", teacher)
	}
	buttons = append(buttons, navigation(page, len(reviews) > 8, prefix)...)
	if teacher > 0 {
		var writable bool
		err := b.db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.bindings WHERE cluster_id=$1 AND active=1 AND mode IN ('thread','feed'))`, state.cluster).Scan(&writable)
		if err != nil {
			return err
		}
		if writable {
			buttons = append(buttons, []choice{{"给这位老师写评价", fmt.Sprintf("teacher:%d", teacher)}})
		}
		buttons = append(buttons, []choice{{"返回老师列表", "teacherall"}})
	}
	buttons = append(buttons, []choice{{"返回主菜单", "home"}})
	state.input = ""
	return b.showHTML(ctx, uid, state, strings.Join(lines, "\n"), buttons)
}
