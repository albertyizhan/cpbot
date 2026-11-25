package pgstore

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"cpbot/internal/publish"
	"github.com/jackc/pgx/v5"
)

var ScoreFields = [...]string{"人照", "颜值", "身材", "服务", "态度", "环境"}
var InputFields = [...]string{"时间", "老师", "出击证明", "人照", "颜值", "身材", "服务", "态度", "环境", "过程"}

const MaxProofBytes = 10 << 20

type UserError string

func (e UserError) Error() string { return string(e) }

type Draft struct {
	Data  map[string]any
	Token string
}

func (d Draft) TeacherID() int64 {
	id, _ := strconv.ParseInt(fmt.Sprint(d.Data["老师"]), 10, 64)
	return id
}

func (d Draft) Anonymous() bool { return d.Data["匿名"] != false }

func (d Draft) Score() (float64, bool) {
	sum := 0
	for _, field := range ScoreFields {
		score, err := strconv.Atoi(fmt.Sprint(d.Data[field]))
		switch d.Data[field].(type) {
		case int, json.Number:
		default:
			return 0, false
		}
		if err != nil || score < 1 || score > 10 {
			return 0, false
		}
		sum += score
	}
	return float64(sum) / float64(len(ScoreFields)), true
}

func (d Draft) Missing() map[string]bool {
	out := make(map[string]bool)
	for _, key := range InputFields {
		if d.Data[key] == nil || strings.TrimSpace(fmt.Sprint(d.Data[key])) == "" {
			out[key] = true
		}
	}
	for _, field := range ScoreFields {
		score, err := strconv.Atoi(fmt.Sprint(d.Data[field]))
		switch d.Data[field].(type) {
		case int, json.Number:
		default:
			out[field] = true
		}
		if err != nil || score < 1 || score > 10 {
			out[field] = true
		}
	}
	return out
}

func NewToken(size int) (string, error) {
	buffer := make([]byte, size)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	return hex.EncodeToString(buffer), nil
}

func ReviewDate(value string, legacy bool) (string, error) {
	when, err := time.Parse("2006-01-02", value)
	if err == nil && when.Year() > 0 {
		return when.Format("2006-01-02"), nil
	}
	if legacy {
		if when, err = time.Parse("2006-01-02 15:04", value); err == nil && when.Year() > 0 {
			return when.Format("2006-01-02"), nil
		}
	}
	return "", UserError("日期格式应为 YYYY-MM-DD，不进行预约日期核验。")
}

func GetDraft(ctx context.Context, db interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, cid, user int64) (Draft, error) {
	d := Draft{Data: map[string]any{"匿名": true}}
	var raw []byte
	err := db.QueryRow(ctx, `SELECT data,token FROM cpbot.drafts WHERE (cluster_id,user_id)=($1,$2)`, cid, user).Scan(&raw, &d.Token)
	if errors.Is(err, pgx.ErrNoRows) {
		return d, nil
	}
	if err != nil {
		return d, err
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	return d, decoder.Decode(&d.Data)
}

func SaveDraft(ctx context.Context, db *pgx.Conn, cid, uid int64, d Draft) (Draft, error) {
	var err error
	if d.Token == "" {
		d.Token, err = NewToken(16)
		if err != nil {
			return d, err
		}
	}
	_, err = db.Exec(ctx, `INSERT INTO cpbot.drafts(cluster_id,user_id,data,token) VALUES($1,$2,$3,$4)
		ON CONFLICT (cluster_id,user_id) DO UPDATE SET data=excluded.data,token=excluded.token`, cid, uid, d.Data, d.Token)
	return d, err
}

var proofName = regexp.MustCompile(`^[a-f0-9]{32}\.jpg$`)

func ProofPath(source string, cid int64, name string) (string, error) {
	if !proofName.MatchString(name) {
		return "", UserError("出击证明无效，请重新上传图片。")
	}
	dir := filepath.Dir(source)
	if cid != 1 {
		dir = filepath.Join(source+".clusters", strconv.FormatInt(cid, 10))
	}
	return filepath.Join(dir, "proofs", name), nil
}

func SaveProof(source string, cid int64, data []byte) (string, error) {
	if len(data) == 0 || len(data) > MaxProofBytes {
		return "", UserError("出击证明图片为空或超过 10 MB。")
	}
	token, err := NewToken(16)
	if err != nil {
		return "", err
	}
	name := token + ".jpg"
	path, err := ProofPath(source, cid, name)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return "", err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return "", err
	}
	_, err = file.Write(data)
	if err == nil {
		err = file.Sync()
	}
	err = errors.Join(err, file.Close())
	if err != nil {
		return "", err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return "", err
	}
	err = errors.Join(dir.Sync(), dir.Close())
	return name, err
}

// Submit creates exactly one review and enqueues only its new publications.
func Submit(ctx context.Context, db *pgx.Conn, source string, cid, user int64, token string, identity any, now time.Time) (int64, error) {
	if strings.TrimSpace(token) == "" {
		return 0, UserError("草稿已变更，请重新打开。")
	}
	var id int64
	err := pgx.BeginFunc(ctx, db, func(tx pgx.Tx) error {
		var ready bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.bindings b JOIN cpbot.cluster_authorizations a USING(cluster_id)
			WHERE b.cluster_id=$1 AND b.active=1 AND b.mode IN ('feed','thread'))`, cid).Scan(&ready); err != nil {
			return err
		}
		if !ready {
			return UserError("写评需要一个可用的车评直发或老师专帖频道。")
		}
		err := tx.QueryRow(ctx, `SELECT id FROM cpbot.reviews WHERE (cluster_id,author,submission)=($1,$2,$3)`, cid, user, token).Scan(&id)
		if err == nil {
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		d, err := GetDraft(ctx, tx, cid, user)
		if err != nil {
			return err
		}
		if token != d.Token {
			return UserError("草稿已变更，请重新打开。")
		}
		if len(d.Missing()) != 0 {
			return UserError("请补齐所有字段。")
		}
		name, _ := d.Data["出击证明"].(string)
		path, err := ProofPath(source, cid, name)
		if err != nil {
			return err
		}
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > MaxProofBytes {
			return UserError("出击证明文件缺失或无效，请重新上传图片。")
		}
		tid := d.TeacherID()
		if err := tx.QueryRow(ctx, `SELECT id FROM cpbot.teachers WHERE (cluster_id,id)=($1,$2) AND deleted_at IS NULL`, cid, tid).Scan(&tid); err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return UserError("这位老师已不存在或进入回收站。")
			}
			return err
		}
		date, err := ReviewDate(fmt.Sprint(d.Data["时间"]), true)
		if err != nil {
			return err
		}
		process, ok := d.Data["过程"].(string)
		if !ok || utf8.RuneCountInString(process) > 1600 {
			return UserError("过程最多 1600 字。")
		}
		creation := float64(now.UnixMicro()) / 1e6
		var last *float64
		if err := tx.QueryRow(ctx, `SELECT (SELECT created FROM cpbot.reviews WHERE (cluster_id,author)=($1,$2) ORDER BY id DESC LIMIT 1)`, cid, user).Scan(&last); err != nil {
			return err
		}
		if last != nil && creation-*last < 30 {
			return UserError("提交过于频繁，请稍后再试。")
		}
		score, _ := d.Score()
		delete(d.Data, "留名")
		d.Data["时间"], d.Data["老师"], d.Data["综合"] = date, tid, score
		anonymous := 0
		if d.Anonymous() {
			anonymous = 1
		}
		err = tx.QueryRow(ctx, `INSERT INTO cpbot.reviews(cluster_id,author,identity,teacher,data,anonymous,status,created,submission)
			VALUES($1,$2,$3,$4,$5,$6,'accepted',$7,$8) RETURNING id`, cid, user, identity, tid, d.Data, anonymous, creation, token).Scan(&id)
		if err != nil {
			return err
		}
		if err := publish.EnqueueReview(ctx, tx, cid, tid, id); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `DELETE FROM cpbot.drafts WHERE (cluster_id,user_id,token)=($1,$2,$3)`, cid, user, token)
		return err
	})
	return id, err
}
