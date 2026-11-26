package bot

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"cpbot/internal/pgstore"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

const maxBackupJSON = 15 << 20
const maxBackupZIP = 19 << 20
const maxBackupMedia = 100 << 20

var backupTables = []string{"people", "teachers", "drafts", "reviews", "codes", "bindings", "memberships", "history", "posts", "publications", "proof_publications", "jobs", "audit"}

type backupRecord map[string]json.RawMessage

type backupTarget struct {
	ID    int64  `json:"id"`
	Title string `json:"title"`
}

type backupData struct {
	Format    string                    `json:"format"`
	Version   int                       `json:"version"`
	Created   float64                   `json:"created"`
	SourceBot int64                     `json:"source_bot"`
	Cluster   int64                     `json:"cluster_id"`
	Tables    map[string][]backupRecord `json:"tables"`
	Target    *backupTarget             `json:"membership_target"`
	Sources   json.RawMessage           `json:"cluster_member_sources"`
	Buttons   json.RawMessage           `json:"comment_buttons,omitempty"`
	Bundle    string                    `json:"image_bundle,omitempty"`
	Files     []string                  `json:"image_files"`
	Media     map[string][]byte         `json:"media,omitempty"`
}

type pendingBackup struct {
	text, images []byte
	data         *backupData
	due          time.Time
}

func rawJSON(v any) json.RawMessage { raw, _ := json.Marshal(v); return raw }

func (b *Bot) buildBackup(ctx context.Context, cid int64, includeAdmins bool) ([]byte, []byte, error) {
	data := backupData{Format: "cpbot-data", Version: 1, Created: float64(time.Now().UnixMicro()) / 1e6, SourceBot: b.me.ID, Cluster: cid,
		Tables: map[string][]backupRecord{"admins": {}}, Sources: json.RawMessage("null")}
	names := map[string]bool{}
	err := pgx.BeginTxFunc(ctx, b.db, pgx.TxOptions{IsoLevel: pgx.RepeatableRead, AccessMode: pgx.ReadOnly}, func(tx pgx.Tx) error {
		for _, table := range backupTables {
			ident := pgx.Identifier{"cpbot", table}.Sanitize()
			rows, err := tx.Query(ctx, `SELECT to_jsonb(t)-'cluster_id' FROM `+ident+` t WHERE cluster_id=$1`, cid)
			if err != nil {
				return err
			}
			list, err := pgx.CollectRows(rows, pgx.RowTo[backupRecord])
			if err != nil {
				return err
			}
			if list == nil {
				list = []backupRecord{}
			}
			for _, row := range list {
				switch table {
				case "people":
					row["note"] = rawJSON("")
				case "jobs":
					if string(row["attempt_photo"]) != "true" {
						delete(row, "attempt_photo")
						delete(row, "attempt_generation")
					}
				case "publications":
					if string(row["photo"]) != "true" {
						delete(row, "photo")
					}
				case "reviews", "drafts":
					var field struct {
						Proof string `json:"出击证明"`
					}
					if err := json.Unmarshal(row["data"], &field); err != nil {
						return err
					}
					if field.Proof != "" {
						names[field.Proof] = true
					}
					row["data"] = rawJSON(string(row["data"]))
					if table == "reviews" {
						row["identity"] = rawJSON(string(row["identity"]))
					}
				}
			}
			data.Tables[table] = list
		}
		if includeAdmins {
			for id := range b.admins {
				data.Tables["admins"] = append(data.Tables["admins"], backupRecord{"user_id": rawJSON(id)})
			}
		}
		target := backupTarget{}
		err := tx.QueryRow(ctx, `SELECT chat_id,title FROM cpbot.membership_targets WHERE cluster_id=$1`, cid).Scan(&target.ID, &target.Title)
		if err == nil {
			data.Target = &target
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		var sources *string
		if err := tx.QueryRow(ctx, `SELECT (SELECT value FROM cpbot.settings WHERE (cluster_id,key)=($1,'cluster_member_sources'))`, cid).Scan(&sources); err != nil {
			return err
		}
		if sources != nil {
			data.Sources = json.RawMessage(*sources)
		}
		layout, err := loadLayout(ctx, tx, cid)
		if err != nil {
			return err
		}
		data.Buttons = rawJSON(layout)
		return nil
	})
	if err != nil {
		return nil, nil, err
	}
	data.Bundle, err = pgstore.NewToken(16)
	if err != nil {
		return nil, nil, err
	}
	data.Files = make([]string, 0, len(names))
	for name := range names {
		data.Files = append(data.Files, name)
	}
	slices.Sort(data.Files)
	text, err := json.Marshal(data)
	if err != nil {
		return nil, nil, err
	}
	if len(text) > maxBackupJSON {
		return nil, nil, pgstore.UserError("JSON 数据超过 15 MB，请使用服务器完整备份。")
	}
	var images bytes.Buffer
	archive := zip.NewWriter(&images)
	manifest, err := archive.Create("manifest.json")
	if err != nil {
		return nil, nil, err
	}
	if _, err := manifest.Write(rawJSON(map[string]string{"bundle": data.Bundle})); err != nil {
		return nil, nil, err
	}
	for _, name := range data.Files {
		path, err := pgstore.ProofPath(b.source, cid, name)
		if err != nil {
			return nil, nil, err
		}
		entry, err := archive.CreateHeader(&zip.FileHeader{Name: name, Method: zip.Store})
		if err != nil {
			return nil, nil, err
		}
		file, err := os.Open(path)
		if err != nil {
			return nil, nil, pgstore.UserError("缺少本地证明图，无法导出备份。")
		}
		size, copyErr := io.Copy(entry, io.LimitReader(file, pgstore.MaxProofBytes+1))
		closeErr := file.Close()
		if err := errors.Join(copyErr, closeErr); err != nil {
			return nil, nil, err
		}
		if size == 0 || size > pgstore.MaxProofBytes {
			return nil, nil, pgstore.UserError("本地证明图为空或超过 10 MB。")
		}
		if images.Len() > maxBackupZIP {
			return nil, nil, pgstore.UserError("图片 ZIP 超过 19 MB，请使用服务器完整备份。")
		}
	}
	if err := archive.Close(); err != nil {
		return nil, nil, err
	}
	if images.Len() > maxBackupZIP {
		return nil, nil, pgstore.UserError("图片 ZIP 超过 19 MB，请使用服务器完整备份。")
	}
	return text, images.Bytes(), nil
}

func parseBackup(text, images []byte, bot, cid int64) (*backupData, error) {
	bad := func(text string) (*backupData, error) { return nil, pgstore.UserError(text) }
	if len(text) > maxBackupJSON || len(images) > maxBackupZIP {
		return bad("备份文件超过大小限制。")
	}
	data := new(backupData)
	text = bytes.TrimPrefix(text, []byte{0xef, 0xbb, 0xbf})
	if err := json.Unmarshal(text, data); err != nil || data.Format != "cpbot-data" || data.Version != 1 || data.Created <= 0 || data.SourceBot < 0 {
		return bad("不是有效的 cpbot JSON 备份。")
	}
	if data.SourceBot != 0 && data.SourceBot != bot {
		return bad("这是其他机器人的备份，无法恢复到当前机器人。")
	}
	if data.Cluster == 0 {
		data.Cluster = 1
	}
	if data.Cluster != cid {
		return bad(fmt.Sprintf("备份属于集群 #%d，请切换到对应集群后恢复。", data.Cluster))
	}
	if data.Target != nil && (data.Target.ID >= 0 || data.Target.Title == "") {
		return bad("备份中的在群判定目标无效。")
	}
	if _, ok := data.Tables["proof_publications"]; !ok {
		data.Tables["proof_publications"] = []backupRecord{}
	}
	if len(data.Tables) != len(backupTables)+1 || data.Tables["admins"] == nil {
		return bad("备份数据表不完整或包含未知表。")
	}
	for _, table := range backupTables {
		if data.Tables[table] == nil {
			return bad("备份缺少数据表 " + table)
		}
	}
	if data.Bundle != "" {
		if len(images) == 0 {
			return bad("请继续上传该备份配套的 ZIP 图片集。")
		}
		archive, err := zip.NewReader(bytes.NewReader(images), int64(len(images)))
		if err != nil || len(archive.File) > 10001 {
			return bad("图片 ZIP 损坏或项目过多。")
		}
		expected := map[string]bool{}
		for _, name := range data.Files {
			if !backupProofName.MatchString(name) || expected[name] {
				return bad("备份图片清单无效。")
			}
			expected[name] = true
		}
		data.Media = map[string][]byte{}
		seen := map[string]bool{}
		var total uint64
		for _, file := range archive.File {
			if seen[file.Name] || file.Name != "manifest.json" && !expected[file.Name] {
				return bad("图片 ZIP 与数据清单不符。")
			}
			seen[file.Name] = true
			if file.UncompressedSize64 == 0 || file.UncompressedSize64 > pgstore.MaxProofBytes {
				return bad("证明图片大小无效。")
			}
			total += file.UncompressedSize64
			if total > maxBackupMedia {
				return bad("图片解压总量超过限制。")
			}
			entry, err := file.Open()
			if err != nil {
				return bad("图片 ZIP 无法解压。")
			}
			value, readErr := io.ReadAll(io.LimitReader(entry, int64(file.UncompressedSize64)+1))
			closeErr := entry.Close()
			if readErr != nil || closeErr != nil || uint64(len(value)) != file.UncompressedSize64 {
				return bad("图片 ZIP 损坏。")
			}
			if file.Name == "manifest.json" {
				var manifest struct {
					Bundle string `json:"bundle"`
				}
				if json.Unmarshal(value, &manifest) != nil || manifest.Bundle != data.Bundle {
					return bad("JSON 和图片 ZIP 不是同一批备份。")
				}
			} else {
				data.Media[file.Name] = value
			}
		}
		if !seen["manifest.json"] || len(expected) != len(data.Media) {
			return bad("图片 ZIP 缺少清单或部分证明图。")
		}
	}
	for name, bytes := range data.Media {
		if !backupProofName.MatchString(name) || len(bytes) == 0 || len(bytes) > pgstore.MaxProofBytes {
			return bad("备份证明图片名称或大小无效。")
		}
	}
	return data, nil
}

var backupProofName = regexp.MustCompile(`^[a-f0-9]{32}\.jpg$`)

func recordText(row backupRecord, key string) (string, error) {
	var text string
	err := json.Unmarshal(row[key], &text)
	return text, err
}

func prepareRecord(table string, row backupRecord, cid int64, definitions map[string]string, images map[string][]byte) (backupRecord, error) {
	bad := func() (backupRecord, error) { return nil, pgstore.UserError("备份字段无效：" + table) }
	result := make(backupRecord, len(definitions))
	for key, raw := range row {
		if table == "people" && key == "note" {
			continue
		}
		if _, ok := definitions[key]; !ok || key == "cluster_id" {
			return bad()
		}
		result[key] = raw
	}
	result["cluster_id"] = rawJSON(cid)
	if table == "teachers" {
		for _, key := range []string{"username", "deleted_at"} {
			if result[key] == nil {
				result[key] = rawJSON(nil)
			}
		}
	}
	if table == "jobs" {
		if result["attempt_generation"] == nil {
			result["attempt_generation"] = rawJSON(nil)
		}
		if result["attempt_photo"] == nil {
			result["attempt_photo"] = rawJSON(false)
		}
		if string(result["state"]) == `"sending"` {
			result["state"] = rawJSON("uncertain")
			result["error"] = rawJSON("恢复前的发送结果不明，请核对")
		}
	}
	if table == "publications" && result["photo"] == nil {
		result["photo"] = rawJSON(false)
	}
	if len(result) != len(definitions) {
		return bad()
	}
	for column, kind := range definitions {
		raw := result[column]
		if string(raw) == "null" {
			continue
		}
		switch kind {
		case "boolean":
			if string(raw) != "true" && string(raw) != "false" {
				return bad()
			}
		case "bigint", "integer":
			if _, err := strconv.ParseInt(string(raw), 10, 64); err != nil {
				return bad()
			}
		case "double precision":
			if _, err := strconv.ParseFloat(string(raw), 64); err != nil {
				return bad()
			}
		case "text":
			var value string
			if json.Unmarshal(raw, &value) != nil {
				return bad()
			}
		case "jsonb":
			value, err := recordText(result, column)
			if err != nil {
				return bad()
			}
			var object map[string]any
			dec := json.NewDecoder(strings.NewReader(value))
			dec.UseNumber()
			if !json.Valid([]byte(value)) || dec.Decode(&object) != nil || object == nil {
				return bad()
			}
			if table == "reviews" && column == "identity" && fmt.Sprint(object["id"]) != string(result["author"]) {
				return bad()
			}
			if column == "data" {
				if name, exists := object["出击证明"]; exists {
					file, ok := name.(string)
					if !ok || len(images[file]) == 0 {
						return nil, pgstore.UserError("备份缺少有效的证明图。")
					}
				}
				if table == "reviews" {
					if fmt.Sprint(object["老师"]) != string(result["teacher"]) {
						return bad()
					}
					if _, err := pgstore.ReviewDate(fmt.Sprint(object["时间"]), true); err != nil {
						return bad()
					}
					if score, ok := object["人照"].(string); ok {
						if len(score) == 0 || utf8.RuneCountInString(score) > 200 {
							return bad()
						}
					} else {
						score, ok := (pgstore.Draft{Data: object}).Score()
						saved, ok2 := object["综合"].(json.Number)
						value, err := saved.Float64()
						if !ok || !ok2 || err != nil || math.Abs(score-value) > 1e-9 {
							return bad()
						}
					}
				}
			}
			result[column] = json.RawMessage(value)
		default:
			return bad()
		}
	}
	if table == "history" {
		for _, key := range []string{"before_value", "after_value"} {
			raw, err := recordText(result, key)
			if err != nil {
				return bad()
			}
			var list []string
			if json.Unmarshal([]byte(raw), &list) != nil || len(list) != 2 {
				return bad()
			}
			result[key] = rawJSON(string(rawJSON([]string{"", list[1]})))
		}
	}
	return result, nil
}

func (b *Bot) restoreBackup(ctx context.Context, cid, uid int64, data *backupData, preview bool) error {
	var oldText, oldImages []byte
	if !preview {
		var err error
		oldText, oldImages, err = b.buildBackup(ctx, cid, true)
		if err != nil {
			return err
		}
	}
	tx, err := b.db.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(context.Background())
	for i := len(backupTables) - 1; i >= 0; i-- {
		table := pgx.Identifier{"cpbot", backupTables[i]}.Sanitize()
		if _, err := tx.Exec(ctx, `DELETE FROM `+table+` WHERE cluster_id=$1`, cid); err != nil {
			return err
		}
	}
	if _, err := tx.Exec(ctx, `DELETE FROM cpbot.membership_targets WHERE cluster_id=$1`, cid); err != nil {
		return err
	}
	if data.Target != nil {
		if err := claimChat(ctx, tx, cid, data.Target.ID); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `INSERT INTO cpbot.membership_targets(cluster_id,chat_id,title) VALUES($1,$2,$3)`, cid, data.Target.ID, data.Target.Title); err != nil {
			return err
		}
	}
	seen := map[int64]bool{}
	for _, row := range data.Tables["bindings"] {
		var chat int64
		var discussion *int64
		if json.Unmarshal(row["chat_id"], &chat) != nil || json.Unmarshal(row["discussion"], &discussion) != nil {
			return pgstore.UserError("备份中的绑定无效。")
		}
		mode, _ := recordText(row, "mode")
		if chat >= 0 || modes[mode] == "" || (mode == "thread") != (discussion != nil) || discussion != nil && *discussion >= 0 {
			return pgstore.UserError("备份中的绑定无效。")
		}
		for _, id := range []int64{chat, value(discussion)} {
			if id == 0 {
				continue
			}
			if seen[id] {
				return pgstore.UserError("备份中的目标重复占用。")
			}
			seen[id] = true
			if err := claimChat(ctx, tx, cid, id); err != nil {
				return err
			}
		}
	}
	for _, name := range backupTables {
		definition, err := tx.Query(ctx, `SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='cpbot' AND table_name=$1`, name)
		if err != nil {
			return err
		}
		cols := map[string]string{}
		for definition.Next() {
			var key, kind string
			if err := definition.Scan(&key, &kind); err != nil {
				definition.Close()
				return err
			}
			cols[key] = kind
		}
		if err := definition.Err(); err != nil {
			return err
		}
		rows := data.Tables[name]
		table := pgx.Identifier{"cpbot", name}.Sanitize()
		for start := 0; start < len(rows); start += 128 {
			var batch []backupRecord
			for _, row := range rows[start:min(start+128, len(rows))] {
				result, err := prepareRecord(name, row, cid, cols, data.Media)
				if err != nil {
					return err
				}
				batch = append(batch, result)
			}
			raw, err := json.Marshal(batch)
			if err != nil {
				return err
			}
			if _, err := tx.Exec(ctx, `INSERT INTO `+table+` SELECT * FROM jsonb_populate_recordset(NULL::`+table+`,$1::jsonb)`, raw); err != nil {
				var databaseErr *pgconn.PgError
				if errors.As(err, &databaseErr) {
					return pgstore.UserError("备份中的数据冲突或无效：" + name)
				}
				return err
			}
		}
	}
	if err := verifyBackupRelations(ctx, tx, cid); err != nil {
		return err
	}
	if data.Sources != nil {
		var sources []int64
		if json.Unmarshal(data.Sources, &sources) != nil {
			return pgstore.UserError("备份成员资格来源无效。")
		}
		if string(data.Sources) == "null" {
			if _, err := tx.Exec(ctx, `DELETE FROM cpbot.settings WHERE (cluster_id,key)=($1,'cluster_member_sources')`, cid); err != nil {
				return err
			}
		} else {
			checks := map[int64]bool{}
			for _, chat := range sources {
				var belongs bool
				err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.bindings WHERE cluster_id=$1 AND (chat_id=$2 OR discussion=$2)
					UNION ALL SELECT 1 FROM cpbot.membership_targets WHERE (cluster_id,chat_id)=($1,$2)
					UNION ALL SELECT 1 FROM cpbot.cluster_management_groups WHERE (cluster_id,chat_id)=($1,$2))`, cid, chat).Scan(&belongs)
				if err != nil {
					return err
				}
				if !belongs || checks[chat] {
					return pgstore.UserError("备份成员资格来源不属于此集群。")
				}
				checks[chat] = true
			}
			if err := setting(ctx, tx, cid, "cluster_member_sources", string(data.Sources)); err != nil {
				return err
			}
		}
	}
	if data.Buttons != nil {
		if err := setting(ctx, tx, cid, "comment_buttons", string(data.Buttons)); err != nil {
			return err
		}
		layout, err := loadLayout(ctx, tx, cid)
		if err != nil || layout.Rows < 1 || layout.Rows > 4 || layout.Columns < 1 || layout.Columns > 4 {
			return pgstore.UserError("备份评论按钮布局无效。")
		}
		for key, cell := range layout.Buttons {
			if !position.MatchString(key) || validateCell(cell) != nil {
				return pgstore.UserError("备份评论按钮无效。")
			}
		}
		for _, hidden := range layout.Hidden {
			if _, ok := layout.Buttons[hidden]; !ok {
				return pgstore.UserError("备份中隐藏按钮无效。 ")
			}
		}
	}
	if err := releaseUnusedChats(ctx, tx, cid); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `UPDATE cpbot.bindings SET active=0,error='备份已恢复，请重新检查权限及消息位置后启用' WHERE cluster_id=$1`, cid); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `DELETE FROM cpbot.settings WHERE (cluster_id,key)=($1,'last_check')`, cid); err != nil {
		return err
	}
	if err := setting(ctx, tx, cid, "publishing_paused", "1"); err != nil {
		return err
	}
	if err := queueButtons(ctx, tx, cid); err != nil {
		return err
	}
	if preview {
		return tx.Rollback(ctx)
	}
	if err := b.writeRecovery(cid, oldText, oldImages); err != nil {
		return err
	}
	for name, data := range data.Media {
		path, err := pgstore.ProofPath(b.source, cid, name)
		if err != nil {
			return err
		}
		if old, err := os.ReadFile(path); err == nil {
			if !bytes.Equal(old, data) {
				return pgstore.UserError("已存在同名异内容证明图，恢复已停止。")
			}
			continue
		} else if !errors.Is(err, os.ErrNotExist) {
			return err
		}
		if err := writePrivateFile(path, data); err != nil {
			return err
		}
	}
	for _, name := range []string{"people", "history", "teachers", "reviews", "codes", "bindings", "jobs", "audit"} {
		var next int64
		if err := tx.QueryRow(ctx, `SELECT greatest((SELECT coalesce(max(id),0) FROM cpbot.`+name+`),(SELECT last_value FROM cpbot.`+name+`_id_seq))+1`).Scan(&next); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, fmt.Sprintf(`ALTER SEQUENCE cpbot.%s_id_seq RESTART WITH %d`, name, next)); err != nil {
			return err
		}
	}
	if err := logAction(ctx, tx, cid, uid, "导入备份", "后台恢复"); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func verifyBackupRelations(ctx context.Context, tx pgx.Tx, cid int64) error {
	var invalid bool
	err := tx.QueryRow(ctx, `SELECT EXISTS(
		SELECT 1 FROM cpbot.reviews r WHERE r.cluster_id=$1 AND (r.status NOT IN ('accepted','pending','rejected','withdrawn','trashed')
		OR r.author<=0 OR r.anonymous NOT IN(0,1) OR r.manual NOT IN(0,1) OR (r.status IN ('pending','rejected'))<>(r.expires IS NOT NULL))
		UNION ALL SELECT 1 FROM cpbot.posts p LEFT JOIN cpbot.teachers t ON (t.cluster_id,t.id)=(p.cluster_id,p.teacher)
		JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(p.cluster_id,p.binding) WHERE p.cluster_id=$1 AND
		(t.id IS NULL OR b.mode<>'thread' OR p.generation<=0 OR p.message<=0 OR p.root<=0 OR p.root IS NOT NULL AND p.message IS NULL)
		UNION ALL SELECT 1 FROM (
			SELECT cluster_id,binding,generation,message,chat_id FROM cpbot.publications
			UNION ALL SELECT cluster_id,binding,generation,message,chat_id FROM cpbot.proof_publications
		) p
		LEFT JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(p.cluster_id,p.binding) WHERE p.cluster_id=$1 AND
		(b.id IS NULL OR b.mode NOT IN('feed','thread') OR p.generation<=0 OR p.message<=0 OR p.chat_id<>coalesce(b.discussion,b.chat_id))
		UNION ALL SELECT 1 FROM cpbot.jobs j LEFT JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(j.cluster_id,j.binding)
		LEFT JOIN cpbot.teachers t ON (t.cluster_id,t.id)=(j.cluster_id,j.teacher) LEFT JOIN cpbot.reviews r ON (r.cluster_id,r.id)=(j.cluster_id,j.review)
		WHERE j.cluster_id=$1 AND (b.id IS NULL OR j.generation<=0 OR j.kind NOT IN ('post','comment','feed','summary','checkpost','check','health','proof','redact','redactproof','redactpost','commentbuttons')
		OR j.kind<>'health' AND t.id IS NULL OR j.review<>0 AND (r.id IS NULL OR r.teacher<>j.teacher))
		UNION ALL SELECT 1 FROM cpbot.drafts d WHERE d.cluster_id=$1 AND d.data?'老师' AND NOT EXISTS(SELECT 1 FROM cpbot.teachers t
		WHERE t.cluster_id=$1 AND t.id::text=d.data->>'老师'))`, cid).Scan(&invalid)
	if err != nil {
		return err
	}
	if invalid {
		return pgstore.UserError("备份中存在无效的业务关联。")
	}
	return nil
}

func writePrivateFile(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, err = file.Write(data)
	if err == nil {
		err = file.Sync()
	}
	return errors.Join(err, file.Close())
}

func (b *Bot) writeRecovery(cid int64, text, images []byte) error {
	path, err := pgstore.ProofPath(b.source, cid, "00000000000000000000000000000000.jpg")
	if err != nil {
		return err
	}
	folder := filepath.Join(filepath.Dir(filepath.Dir(path)), "recovery")
	name := filepath.Join(folder, fmt.Sprintf("before-import-%d", time.Now().UnixNano()))
	if err := writePrivateFile(name+".zip", images); err != nil {
		return pgstore.UserError("无法保存恢复前的图片备份。")
	}
	if err := writePrivateFile(name+".json", text); err != nil {
		return pgstore.UserError("无法保存恢复前的数据备份。")
	}
	return nil
}

func (b *Bot) backupAction(ctx context.Context, uid int64, state panel, kind string) (bool, error) {
	switch kind {
	case "admin.export":
		text, images, err := b.buildBackup(ctx, state.cluster, b.admins[uid])
		if err != nil {
			return true, err
		}
		if err := b.api.Document(ctx, uid, "cpbot-data.json", text); err != nil {
			return true, err
		}
		if err := b.api.Document(ctx, uid, "cpbot-images.zip", images); err != nil {
			return true, err
		}
		return true, b.adminPanel(ctx, uid, state, "备份文件已发送，请一起保存 JSON 和 ZIP。")
	case "admin.import":
		if !b.admins[uid] {
			return true, pgstore.UserError("仅 superadmin 可以恢复备份。")
		}
		state.backup = &pendingBackup{due: time.Now().Add(10 * time.Minute)}
		return true, b.promptAdmin(ctx, uid, state, kind, "发送同一批的 JSON 和 ZIP。收齐并校验后，确认才覆盖当前集群。", "admin")
	case "admin.importyes":
		if !b.admins[uid] {
			return true, pgstore.UserError("仅 superadmin 可以恢复备份。")
		}
		if state.backup == nil || state.backup.data == nil || time.Now().After(state.backup.due) {
			return true, pgstore.UserError("预览已过期，请重新上传备份。")
		}
		if err := b.restoreBackup(ctx, state.cluster, uid, state.backup.data, false); err != nil {
			return true, err
		}
		state.backup = nil
		return true, b.adminPanel(ctx, uid, state, "备份已恢复。发布已暂停，请核对消息位置和权限，再启用绑定并继续队列。")
	}
	return false, nil
}

func (b *Bot) backupInput(ctx context.Context, msg telegram.Message, state panel) (bool, error) {
	if state.input != "admin.import" {
		return false, nil
	}
	uid := msg.From.ID
	if !b.admins[uid] {
		return true, pgstore.UserError("仅 superadmin 可以恢复备份。")
	}
	if state.backup == nil || time.Now().After(state.backup.due) {
		return true, pgstore.UserError("上传已过期，请返回后台重新开始。")
	}
	if msg.Document == nil {
		return true, pgstore.UserError("请以文件方式发送 JSON 或 ZIP。")
	}
	doc := msg.Document
	kind := strings.ToLower(filepath.Ext(doc.FileName))
	limit := 0
	switch kind {
	case ".json":
		limit = maxBackupJSON
	case ".zip":
		limit = maxBackupZIP
	default:
		return true, pgstore.UserError("请发送 JSON 或 ZIP 文件。")
	}
	if doc.FileSize > int64(limit) {
		return true, pgstore.UserError("备份文件超过大小限制。")
	}
	content, err := b.api.Download(ctx, doc.FileID, int64(limit))
	if err != nil {
		return true, pgstore.UserError("无法下载备份文件，请重新上传。")
	}
	if kind == ".json" {
		state.backup.text = content
	} else {
		state.backup.images = content
	}
	if len(state.backup.text) == 0 {
		return true, b.send(ctx, uid, "已收到图片 ZIP，请继续上传 JSON。")
	}
	var header struct {
		Bundle string `json:"image_bundle"`
	}
	if json.Unmarshal(bytes.TrimPrefix(state.backup.text, []byte{0xef, 0xbb, 0xbf}), &header) == nil && header.Bundle != "" && len(state.backup.images) == 0 {
		return true, b.send(ctx, uid, "已收到 JSON，请继续上传配套图片 ZIP。")
	}
	data, err := parseBackup(state.backup.text, state.backup.images, b.me.ID, state.cluster)
	if err != nil {
		return true, err
	}
	if err := b.restoreBackup(ctx, state.cluster, uid, data, true); err != nil {
		return true, err
	}
	state.backup.data = data
	state.backup.due = time.Now().Add(10 * time.Minute)
	state.input = ""
	text := fmt.Sprintf("备份校验通过 · 导出时间 %s\n老师 %d · 客户 %d · 车评 %d · 图片 %d · 绑定 %d\n确认后将覆盖当前集群的业务数据。", displayDate(&data.Created),
		len(data.Tables["teachers"]), len(data.Tables["people"]), len(data.Tables["reviews"]), len(data.Media), len(data.Tables["bindings"]))
	return true, b.show(ctx, uid, state, text, [][]choice{{{"确认覆盖并恢复", "admin.importyes"}}, {{"取消", "admin"}}})
}
