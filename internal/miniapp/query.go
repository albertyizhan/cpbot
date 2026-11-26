package miniapp

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"cpbot/internal/pgstore"
	"cpbot/internal/teacherprofile"
	"github.com/jackc/pgx/v5"
)

const pageSize = 20
const clusterEnabled = `EXISTS (SELECT 1 FROM cpbot.cluster_authorizations a WHERE a.cluster_id=c.id)
	AND EXISTS (SELECT 1 FROM cpbot.bindings active WHERE active.cluster_id=c.id AND active.active=1)`

const teacherSelect = `SELECT t.cluster_id,t.id,t.name,c.name,` + teacherprofile.Username + `
	FROM cpbot.teachers t JOIN cpbot.clusters c ON c.id=t.cluster_id` + teacherprofile.JoinPeople

type teacher struct {
	ClusterID int64  `json:"clusterId,string"`
	ID        int64  `json:"id,string"`
	Name      string `json:"name"`
	Cluster   string `json:"cluster"`
	Username  string `json:"username,omitempty"`
}

type review struct {
	ClusterID int64   `json:"clusterId,string"`
	ID        int64   `json:"id,string"`
	TeacherID int64   `json:"teacherId,string"`
	Teacher   string  `json:"teacher"`
	Cluster   string  `json:"cluster"`
	Date      string  `json:"date,omitempty"`
	Text      string  `json:"text,omitempty"`
	Score     string  `json:"score,omitempty"`
	Link      string  `json:"link,omitempty"`
	HasProof  bool    `json:"hasProof,omitempty"`
	Created   float64 `json:"created"`
}

const visibleReview = `r.status='accepted' AND t.deleted_at IS NULL AND ` + clusterEnabled

type reviewPage struct {
	Items   []review `json:"items"`
	Next    string   `json:"next,omitempty"`
	Teacher *teacher `json:"teacher,omitempty"`
}

type position struct {
	Created float64 `json:"c,omitempty"`
	Date    string  `json:"d,omitempty"`
	Cluster int64   `json:"k"`
	ID      int64   `json:"i"`
}

func unpack(raw string) (position, bool) {
	if raw == "" {
		return position{}, true
	}
	var p position
	if len(raw) > 384 {
		return p, false
	}
	data, err := base64.RawURLEncoding.DecodeString(raw)
	if err != nil || json.Unmarshal(data, &p) != nil || p.ID < 1 || p.Cluster < 1 {
		return p, false
	}
	return p, true
}

func (p position) encode() string {
	data, _ := json.Marshal(p)
	return base64.RawURLEncoding.EncodeToString(data)
}

func (s *server) teachers(w http.ResponseWriter, r *http.Request, ids []int64) error {
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	nameQuery, prefix := strings.CutPrefix(q, "@")
	if nameQuery == "" || !utf8.ValidString(q) || utf8.RuneCountInString(q) > 80 || strings.ContainsRune(q, 0) {
		problem(w, http.StatusBadRequest, "请输入 1–80 个字的老师姓名或用户名。")
		return nil
	}
	rows, err := s.db.Query(r.Context(), teacherSelect+`
		WHERE t.cluster_id=ANY($1) AND t.deleted_at IS NULL AND `+clusterEnabled+`
		AND ((NOT $3::boolean AND strpos(lower(t.name),lower($2))>0)
		OR strpos(lower((`+teacherprofile.Username+`) COLLATE "C"),lower($2 COLLATE "C"))=1)
		ORDER BY (lower(t.name)=lower($2)) DESC,t.name,t.cluster_id,t.id LIMIT 24`, ids, nameQuery, prefix)
	if err != nil {
		return err
	}
	items, err := pgx.CollectRows(rows, pgx.RowToStructByPos[teacher])
	if err != nil {
		return err
	}
	respond(w, http.StatusOK, map[string]any{"items": items})
	return nil
}

func dateEnd(s string) (string, bool) {
	if s == "" {
		return "", true
	}
	d, err := time.Parse("2006-01-02", s)
	if err != nil || len(s) != 10 || d.Year() < 1 || d.Year() > 9998 {
		return "", false
	}
	return d.AddDate(0, 0, 1).Format("2006-01-02"), true
}

func (s *server) feed(w http.ResponseWriter, r *http.Request, ids []int64) error {
	cursor, ok := unpack(r.URL.Query().Get("cursor"))
	if !ok {
		problem(w, http.StatusBadRequest, "分页已失效，请刷新列表。")
		return nil
	}
	page, err := s.reviews(r.Context(), nil, ids, cursor, false, "", "")
	if err != nil {
		return err
	}
	respond(w, http.StatusOK, page)
	return nil
}

func (s *server) teacherReviews(w http.ResponseWriter, r *http.Request, ids []int64) error {
	cid, ce := strconv.ParseInt(r.PathValue("cluster"), 10, 64)
	tid, te := strconv.ParseInt(r.PathValue("teacher"), 10, 64)
	if ce != nil || te != nil || tid < 1 || !slices.Contains(ids, cid) {
		problem(w, http.StatusNotFound, "这位老师不可查看。")
		return nil
	}
	q := r.URL.Query()
	cursor, ok := unpack(q.Get("cursor"))
	if !ok || (cursor.ID > 0 && cursor.Cluster != cid) {
		problem(w, http.StatusBadRequest, "分页已失效，请刷新列表。")
		return nil
	}
	order, from, to := q.Get("order"), q.Get("from"), q.Get("to")
	_, fromOK := dateEnd(from)
	end, toOK := dateEnd(to)
	if !fromOK || !toOK || (from != "" && to != "" && from > to) || (order != "" && order != "desc" && order != "asc") {
		problem(w, http.StatusBadRequest, "请选择有效的排序和日期范围。")
		return nil
	}
	rows, err := s.db.Query(r.Context(), teacherSelect+`
		WHERE (t.cluster_id,t.id)=($1,$2) AND t.deleted_at IS NULL AND `+clusterEnabled, cid, tid)
	if err != nil {
		return err
	}
	profile, err := pgx.CollectExactlyOneRow(rows, pgx.RowToStructByPos[teacher])
	if errors.Is(err, pgx.ErrNoRows) {
		problem(w, http.StatusNotFound, "这位老师不可查看。")
		return nil
	}
	if err != nil {
		return err
	}
	page, err := s.reviews(r.Context(), &profile, ids, cursor, order == "asc", from, end)
	if err != nil {
		return err
	}
	respond(w, http.StatusOK, page)
	return nil
}

const reviewDate = `coalesce(r.data->>'时间','')`

func (s *server) reviews(ctx context.Context, profile *teacher, ids []int64, cursor position, asc bool, from, end string) (reviewPage, error) {
	args := []any{ids, pageSize + 1}
	filter := ""
	order := "r.created DESC,r.cluster_id DESC,r.id DESC"
	if profile == nil {
		if cursor.ID > 0 {
			filter = ` AND (r.created,r.cluster_id,r.id)<($3,$4,$5)`
			args = append(args, cursor.Created, cursor.Cluster, cursor.ID)
		}
	} else {
		filter = ` AND (r.cluster_id,r.teacher)=($3,$4) AND ($5='' OR ` + reviewDate + ` >= $5) AND ($6='' OR ` + reviewDate + ` < $6)`
		args = append(args, profile.ClusterID, profile.ID, from, end)
		direction, compare := " DESC", "<"
		if asc {
			direction, compare = " ASC", ">"
		}
		order = reviewDate + direction + ",r.id" + direction
		if cursor.ID > 0 {
			filter += ` AND (` + reviewDate + `,r.id)` + compare + `($7,$8)`
			args = append(args, cursor.Date, cursor.ID)
		}
	}
	rows, err := s.db.Query(ctx, `SELECT r.cluster_id,r.id,r.teacher,t.name,c.name,`+reviewDate+`,
		CASE WHEN pub.chat_id IS NULL THEN '' ELSE coalesce(r.data->>'过程','') END,
		CASE WHEN pub.chat_id IS NULL THEN '' ELSE coalesce(r.data->>'综合','') END,
		CASE WHEN pub.chat_id::text ~ '^-100[1-9][0-9]*$' AND pub.message>0
			THEN 'https://t.me/c/'||substr(pub.chat_id::text,5)||'/'||pub.message END,`+publishedProof+`,r.created
		FROM cpbot.reviews r
		JOIN cpbot.teachers t ON (t.cluster_id,t.id)=(r.cluster_id,r.teacher)
		JOIN cpbot.clusters c ON c.id=r.cluster_id `+pgstore.BestPublication+`
		WHERE r.cluster_id=ANY($1) AND `+visibleReview+filter+`
		ORDER BY `+order+` LIMIT $2`, args...)
	page := reviewPage{Items: make([]review, 0, pageSize), Teacher: profile}
	if err != nil {
		return page, err
	}
	defer rows.Close()
	for rows.Next() {
		var item review
		var link *string
		if err := rows.Scan(&item.ClusterID, &item.ID, &item.TeacherID, &item.Teacher, &item.Cluster, &item.Date, &item.Text, &item.Score, &link, &item.HasProof, &item.Created); err != nil {
			return page, err
		}
		if len(page.Items) == pageSize {
			last := page.Items[pageSize-1]
			page.Next = position{Created: last.Created, Date: last.Date, Cluster: last.ClusterID, ID: last.ID}.encode()
			break
		}
		if link != nil {
			item.Link = *link
		}
		score, err := strconv.ParseFloat(item.Score, 64)
		if err == nil && !math.IsNaN(score) && !math.IsInf(score, 0) {
			item.Score = strconv.FormatFloat(score, 'f', 1, 64)
		} else {
			item.Score = ""
		}
		page.Items = append(page.Items, item)
	}
	return page, rows.Err()
}
