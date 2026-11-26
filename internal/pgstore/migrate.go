// Package pgstore owns the PostgreSQL schema and the offline cpbot migration.
package pgstore

import (
	"context"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

//go:embed schema.sql
var schema string

//go:embed photo_publications.sql
var photoPublications string

// Upgrade adds photo delivery metadata. The original column contract remains usable.
// Call under the same database owner lock used by the running bot.
func Upgrade(ctx context.Context, db *pgx.Conn) error {
	return pgx.BeginFunc(ctx, db, func(tx pgx.Tx) error {
		var applied bool
		if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM information_schema.columns
			WHERE table_schema='cpbot' AND table_name='publications' AND column_name='photo')`).Scan(&applied); err != nil || applied {
			return err
		}
		_, err := tx.Exec(ctx, photoPublications)
		return err
	})
}

//go:embed sqlite_export.py
var sqliteExport string

type MigrationReport struct {
	Applied          bool             `json:"applied"`
	Rows             map[string]int64 `json:"rows"`
	ReferencedProofs int64            `json:"referenced_proofs"`
}

type sqliteRecord struct {
	Table            string                       `json:"table"`
	Rows             []map[string]json.RawMessage `json:"rows"`
	Complete         bool                         `json:"complete"`
	ClusterSequence  int64                        `json:"cluster_sequence"`
	ReferencedProofs int64                        `json:"referenced_proofs"`
}

// MigrateSQLite imports a stopped Python installation into a fresh cpbot schema.
// checkOnly runs the same import, constraints and sequence initialization, then rolls back.
func MigrateSQLite(ctx context.Context, dsn, source string, checkOnly bool) (MigrationReport, error) {
	report := MigrationReport{Rows: make(map[string]int64)}
	path, err := filepath.Abs(source)
	if err != nil {
		return report, err
	}
	info, err := os.Stat(path)
	if err != nil {
		return report, err
	}
	if !info.Mode().IsRegular() {
		return report, errors.New("SQLite 源路径必须是普通文件")
	}
	lock, err := os.OpenFile(path+".lock", os.O_CREATE|os.O_RDONLY, 0600)
	if err != nil {
		return report, fmt.Errorf("打开 SQLite 运行锁：%w", err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		return report, fmt.Errorf("无法取得 SQLite 运行锁，请停止旧机器人：%w", err)
	}

	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		// Invalid connection strings may contain credentials or unparsed query options.
		return report, errors.New("无法连接 PostgreSQL，请检查 DATABASE_URL 和数据库可用性")
	}
	defer conn.Close(context.Background())
	tx, err := conn.Begin(ctx)
	if err != nil {
		return report, err
	}
	defer func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = tx.Rollback(cleanup)
	}()
	var locked, exists bool
	err = tx.QueryRow(ctx, `SELECT pg_try_advisory_xact_lock(hashtext('cpbot migration'), 0),
		EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'cpbot')`).Scan(&locked, &exists)
	if err != nil {
		return report, err
	}
	if !locked {
		return report, errors.New("已有 cpbot 导入正在进行")
	}
	if exists {
		return report, errors.New("目标已存在 cpbot schema，请使用新的数据库")
	}
	if _, err := tx.Exec(ctx, schema); err != nil {
		return report, fmt.Errorf("创建 cpbot schema：%w", err)
	}
	columns, err := importColumns(ctx, tx, report.Rows)
	if err != nil {
		return report, err
	}
	footer, err := importSQLiteRows(ctx, tx, path, columns, report.Rows)
	if err != nil {
		return report, err
	}
	if _, err := tx.Exec(ctx, photoPublications); err != nil {
		return report, err
	}
	report.ReferencedProofs = footer.ReferencedProofs
	if err := restartSequences(ctx, tx, footer.ClusterSequence); err != nil {
		return report, err
	}
	if checkOnly {
		return report, tx.Rollback(ctx)
	}
	if err := tx.Commit(ctx); err != nil {
		return report, fmt.Errorf("提交迁移：%w", err)
	}
	report.Applied = true
	return report, nil
}

func importColumns(ctx context.Context, tx pgx.Tx, counts map[string]int64) (map[string]map[string]bool, error) {
	rows, err := tx.Query(ctx, `SELECT table_name, column_name
		FROM information_schema.columns WHERE table_schema = 'cpbot' AND table_name <> 'schema_version'`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns := make(map[string]map[string]bool)
	for rows.Next() {
		var table, name string
		if err := rows.Scan(&table, &name); err != nil {
			return nil, err
		}
		if columns[table] == nil {
			columns[table] = make(map[string]bool)
			counts[table] = 0
		}
		columns[table][name] = true
	}
	return columns, rows.Err()
}

func importSQLiteRows(ctx context.Context, tx pgx.Tx, path string, columns map[string]map[string]bool, counts map[string]int64) (sqliteRecord, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	cmd := exec.CommandContext(ctx, "python3", "-I", "-S", "-c", sqliteExport, path)
	cmd.Stderr = os.Stderr
	output, err := cmd.StdoutPipe()
	if err != nil {
		return sqliteRecord{}, err
	}
	if err := cmd.Start(); err != nil {
		return sqliteRecord{}, fmt.Errorf("启动 SQLite 导出器（需要 Python 3.9+）：%w", err)
	}
	footer, importErr := readSQLiteRows(ctx, tx, json.NewDecoder(output), columns, counts)
	if importErr != nil {
		cancel()
	}
	waitErr := cmd.Wait()
	if importErr != nil {
		return sqliteRecord{}, importErr
	}
	if waitErr != nil {
		return sqliteRecord{}, fmt.Errorf("SQLite 导出器失败：%w", waitErr)
	}
	return footer, nil
}

func readSQLiteRows(ctx context.Context, tx pgx.Tx, decoder *json.Decoder, columns map[string]map[string]bool, counts map[string]int64) (sqliteRecord, error) {
	for {
		var record sqliteRecord
		if err := decoder.Decode(&record); err != nil {
			return sqliteRecord{}, fmt.Errorf("SQLite 导出未完整结束：%w", err)
		}
		if record.Complete {
			var extra json.RawMessage
			if err := decoder.Decode(&extra); err != io.EOF {
				return sqliteRecord{}, errors.New("SQLite 导出结束标记后存在额外数据")
			}
			return record, nil
		}
		if err := insertBatch(ctx, tx, record, columns, counts); err != nil {
			return sqliteRecord{}, err
		}
	}
}

func insertBatch(ctx context.Context, tx pgx.Tx, record sqliteRecord, columns map[string]map[string]bool, counts map[string]int64) error {
	expected, known := columns[record.Table]
	if !known || len(record.Rows) == 0 {
		return fmt.Errorf("SQLite 导出包含意外表批次：%q", record.Table)
	}
	for _, row := range record.Rows {
		if len(row) != len(expected) {
			return fmt.Errorf("表 %s 的 SQLite 字段清单与迁移版本不匹配", record.Table)
		}
		for name := range row {
			if !expected[name] {
				return fmt.Errorf("表 %s 包含意外字段 %q", record.Table, name)
			}
		}
	}
	payload, err := json.Marshal(record.Rows)
	if err != nil {
		return err
	}
	// The table name comes from information_schema for the transaction's new schema.
	table := pgx.Identifier{"cpbot", record.Table}.Sanitize()
	command := fmt.Sprintf("INSERT INTO %s SELECT * FROM jsonb_populate_recordset(NULL::%s, $1::jsonb)", table, table)
	tag, err := tx.Exec(ctx, command, payload)
	if err != nil {
		var databaseErr *pgconn.PgError
		if errors.As(err, &databaseErr) {
			return fmt.Errorf("表 %s 导入失败（SQLSTATE %s，约束 %s）", record.Table, databaseErr.Code, databaseErr.ConstraintName)
		}
		return fmt.Errorf("表 %s 导入失败：%w", record.Table, err)
	}
	if tag.RowsAffected() != int64(len(record.Rows)) {
		return fmt.Errorf("表 %s 的导入行数不匹配", record.Table)
	}
	counts[record.Table] += tag.RowsAffected()
	return nil
}

func restartSequences(ctx context.Context, tx pgx.Tx, clusterFloor int64) error {
	tables := []string{"clusters", "cluster_activation_codes", "people", "history", "teachers", "reviews", "codes", "bindings", "jobs", "audit"}
	for _, table := range tables {
		var largest int64
		query := fmt.Sprintf("SELECT coalesce(max(id), 0) FROM cpbot.%s", table)
		if err := tx.QueryRow(ctx, query).Scan(&largest); err != nil {
			return err
		}
		if table == "clusters" {
			largest = max(largest, clusterFloor)
		}
		if largest == math.MaxInt64 {
			return fmt.Errorf("表 %s 的编号已用尽", table)
		}
		// RESTART takes effect only if this transaction commits.
		query = fmt.Sprintf("ALTER SEQUENCE cpbot.%s_id_seq RESTART WITH %d", table, max(largest+1, 1))
		if _, err := tx.Exec(ctx, query); err != nil {
			return err
		}
	}
	return nil
}
