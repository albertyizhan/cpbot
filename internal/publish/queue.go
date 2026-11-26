// Package publish runs cpbot's single-owner, persistent Telegram publishing queue.
package publish

import (
	"context"
	"errors"
	"fmt"
	"time"

	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

// Worker shares pacing across all authorized clusters. Call its methods serially.
// Its owner must hold the service's single-instance lock for the lifetime of the worker.
type Worker struct {
	db         *pgx.Conn
	api        *telegram.Client
	username   string
	botID      int64
	sqlitePath string
	nextGlobal time.Time
	nextChat   map[int64]time.Time
	lastCID    int64
}

func New(db *pgx.Conn, api *telegram.Client, username string, botID int64, sqlitePath string) *Worker {
	return &Worker{db: db, api: api, username: username, botID: botID, sqlitePath: sqlitePath, nextChat: make(map[int64]time.Time)}
}

type job struct {
	id, cid, binding, teacher, review, revision, channel int64
	kind, mode                                           string
	discussion, post, root, generation                   *int64
}

func (j job) replica() int64 {
	if j.mode == "thread" && j.generation != nil {
		return *j.generation
	}
	return 1
}

func (j job) destination() int64 {
	if j.discussion != nil {
		switch j.kind {
		case "comment", "check", "commentbuttons", "proof", "redact", "redactproof":
			return *j.discussion
		}
	}
	return j.channel
}

func seconds(t time.Time) float64 { return float64(t.UnixMicro()) / 1e6 }

func (w *Worker) Recover(ctx context.Context) error {
	_, err := w.db.Exec(ctx, `UPDATE cpbot.jobs SET state='uncertain',
		error='进程在请求期间中断，请核对远端消息' WHERE state='sending'`)
	return err
}

func (w *Worker) AutomaticForward(ctx context.Context, msg telegram.Message) (bool, error) {
	if !msg.AutomaticForward {
		return false, nil
	}
	var channel, message int64
	if msg.ForwardOrigin != nil {
		channel, message = msg.ForwardOrigin.Chat.ID, msg.ForwardOrigin.MessageID
	}
	if channel == 0 && msg.ForwardFromChat != nil {
		channel = msg.ForwardFromChat.ID
	}
	if message == 0 {
		message = msg.ForwardFromMessageID
	}
	if channel == 0 || message <= 0 || msg.ID <= 0 || msg.Chat.ID >= 0 {
		return true, nil
	}
	_, err := w.db.Exec(ctx, `UPDATE cpbot.posts p SET root=$1 FROM cpbot.bindings b
		JOIN cpbot.cluster_authorizations a USING (cluster_id)
		WHERE (p.cluster_id,p.binding)=(b.cluster_id,b.id) AND b.mode='thread'
		AND b.chat_id=$2 AND b.discussion=$3 AND p.message=$4`, msg.ID, channel, msg.Chat.ID, message)
	return true, err
}

func (w *Worker) PollTimeout(ctx context.Context) (int, error) {
	var pending bool
	err := w.db.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM cpbot.jobs j
		JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(j.cluster_id,j.binding)
		JOIN cpbot.cluster_authorizations a ON a.cluster_id=j.cluster_id
		LEFT JOIN cpbot.settings s ON (s.cluster_id,s.key)=(j.cluster_id,'publishing_paused')
		WHERE j.state='pending' AND b.active=1 AND s.value IS DISTINCT FROM '1')`).Scan(&pending)
	if pending {
		return 1, err
	}
	return 20, err
}

// Tick claims one ready job, makes its Telegram request outside the transaction,
// then commits the new message location and state together.
func (w *Worker) Tick(ctx context.Context, now time.Time) error {
	if now.Before(w.nextGlobal) {
		return nil
	}
	var j job
	err := w.db.QueryRow(ctx, `SELECT j.id,j.cluster_id,j.kind,j.binding,j.teacher,j.review,j.generation,
		b.mode,b.chat_id,b.discussion,p.message,p.root,p.generation
		FROM cpbot.jobs j
		JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(j.cluster_id,j.binding)
		JOIN cpbot.cluster_authorizations a ON a.cluster_id=j.cluster_id
		LEFT JOIN cpbot.settings s ON (s.cluster_id,s.key)=(j.cluster_id,'publishing_paused')
		LEFT JOIN cpbot.reviews r ON (r.cluster_id,r.id)=(j.cluster_id,j.review)
		LEFT JOIN cpbot.posts p ON (p.cluster_id,p.binding,p.teacher)=(j.cluster_id,j.binding,j.teacher)
		WHERE j.state='pending' AND j.due<=$1 AND b.active=1 AND s.value IS DISTINCT FROM '1'
		ORDER BY j.cluster_id<=$2,j.cluster_id,
		CASE j.kind WHEN 'health' THEN 0 WHEN 'post' THEN 1 WHEN 'checkpost' THEN 2 ELSE 3 END,
		coalesce(r.data->>'时间',''),j.review,j.id LIMIT 1`, seconds(now), w.lastCID).
		Scan(&j.id, &j.cid, &j.kind, &j.binding, &j.teacher, &j.review, &j.revision,
			&j.mode, &j.channel, &j.discussion, &j.post, &j.root, &j.generation)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	w.lastCID = j.cid
	if next := w.nextChat[j.destination()]; now.Before(next) {
		return w.deferJob(ctx, j, next, "")
	}
	if j.mode == "thread" && j.root == nil && (j.kind == "comment" || j.kind == "check" || j.kind == "proof") {
		return w.deferJob(ctx, j, now.Add(10*time.Second), "等待频道自动转发到讨论组")
	}
	reason, err := w.waitForOtherJobs(ctx, j)
	if err != nil {
		return err
	}
	if reason != "" {
		return w.deferJob(ctx, j, now.Add(10*time.Second), reason)
	}
	tag, err := w.db.Exec(ctx, `UPDATE cpbot.jobs SET state='sending',attempt_generation=$3,attempt_photo=false
		WHERE (cluster_id,id)=($1,$2) AND state='pending'`, j.cid, j.id, j.replica())
	if err != nil || tag.RowsAffected() == 0 {
		return err
	}
	w.nextGlobal = now.Add(1100 * time.Millisecond)
	targetDelay := 1100 * time.Millisecond
	if j.discussion != nil && j.destination() == *j.discussion {
		targetDelay = 3200 * time.Millisecond
	}
	w.nextChat[j.destination()] = now.Add(targetDelay)

	result, err := w.send(ctx, j)
	if err != nil {
		return w.handleFailure(ctx, j, now, err)
	}
	if result.waitForText {
		return w.deferJob(ctx, j, now.Add(3*time.Second), "等待评价正文发布")
	}
	return w.complete(ctx, j, result)
}

func (w *Worker) waitForOtherJobs(ctx context.Context, j job) (string, error) {
	var uncertain, earlier bool
	err := w.db.QueryRow(ctx, `SELECT
		EXISTS(SELECT 1 FROM cpbot.jobs x
			WHERE x.cluster_id=$1 AND x.binding=$2 AND x.teacher=$3 AND x.review=$4 AND x.id<>$5
			AND x.state IN ('sending','uncertain') AND
			(($6='proof' AND x.kind='proof')
			OR ($6 IN ('comment','check','feed') AND x.kind IN ('comment','check','feed'))
			OR ($6 IN ('post','checkpost','summary') AND x.kind IN ('post','checkpost','summary')))),
		EXISTS(SELECT 1 FROM cpbot.jobs x JOIN cpbot.reviews other ON (other.cluster_id,other.id)=(x.cluster_id,x.review)
			JOIN cpbot.reviews current ON (current.cluster_id,current.id)=($1,$4)
			WHERE $6 IN ('comment','check') AND x.cluster_id=$1 AND x.binding=$2 AND x.teacher=$3
			AND x.generation=$7 AND x.kind IN ('comment','proof') AND x.state NOT IN ('done','superseded')
			AND (other.data->>'时间',other.id)<(current.data->>'时间',current.id))`,
		j.cid, j.binding, j.teacher, j.review, j.id, j.kind, j.replica()).Scan(&uncertain, &earlier)
	if uncertain {
		return "等待管理员核对上一次发送结果", err
	}
	if earlier {
		return "等待较早评价发布完成", err
	}
	return "", err
}

func (w *Worker) deferJob(ctx context.Context, j job, until time.Time, reason string) error {
	_, err := w.db.Exec(ctx, `UPDATE cpbot.jobs SET state='pending',due=$3,
		error=CASE WHEN $4='' THEN error ELSE $4 END WHERE (cluster_id,id)=($1,$2)`, j.cid, j.id, seconds(until), reason)
	return err
}

type delivery struct {
	location    string
	message     int64
	waitForText bool
	photo       bool
	legacyProof bool
	note        string
}

func (w *Worker) complete(ctx context.Context, j job, result delivery) error {
	return pgx.BeginFunc(ctx, w.db, func(tx pgx.Tx) error {
		var err error
		switch result.location {
		case "post":
			_, err = tx.Exec(ctx, `UPDATE cpbot.posts SET message=$4 WHERE (cluster_id,binding,teacher)=($1,$2,$3)`,
				j.cid, j.binding, j.teacher, result.message)
		case "publications":
			_, err = tx.Exec(ctx, `INSERT INTO cpbot.publications(cluster_id,binding,review,generation,message,chat_id,photo)
				VALUES($1,$2,$3,$4,$5,$6,$7)`, j.cid, j.binding, j.review, j.replica(), result.message, j.destination(), result.photo)
		case "proof_publications":
			_, err = tx.Exec(ctx, `INSERT INTO cpbot.proof_publications(cluster_id,binding,review,generation,message,chat_id)
				VALUES($1,$2,$3,$4,$5,$6)`, j.cid, j.binding, j.review, j.replica(), result.message, j.destination())
		}
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state='done',error=$3 WHERE (cluster_id,id)=($1,$2)`, j.cid, j.id, result.note); err != nil {
			return err
		}
		if result.legacyProof {
			if err := Enqueue(ctx, tx, j.cid, "proof", j.binding, j.teacher, j.review, j.replica()); err != nil {
				return err
			}
		}
		if j.review != 0 && j.mode == "thread" && j.kind == "comment" {
			return Enqueue(ctx, tx, j.cid, "summary", j.binding, j.teacher, 0, j.review+1000000)
		}
		return nil
	})
}

func (w *Worker) handleFailure(ctx context.Context, j job, now time.Time, cause error) error {
	var apiError *telegram.Error
	var validation jobError
	if errors.As(cause, &validation) {
		return w.failJob(ctx, j, "blocked", validation.Error(), j.kind == "health")
	}
	if !errors.As(cause, &apiError) {
		// Storage failure during processing requires the owner to stop. Recover()
		// leaves the job awaiting reconciliation after the database is available.
		return fmt.Errorf("处理集群 %d 的发布任务 %d：%w", j.cid, j.id, cause)
	}
	text := apiError.Description
	if text == "" {
		text = apiError.Error()
	}
	switch {
	case unchanged(text):
		return w.complete(ctx, j, delivery{})
	case apiError.Code == 429:
		until := now.Add(time.Duration(max(1, apiError.RetryAfter)) * time.Second)
		if until.After(w.nextGlobal) {
			w.nextGlobal = until
		}
		return w.deferJob(ctx, j, until, "Telegram 限流")
	case apiError.Code == 0 || apiError.Code >= 500:
		return w.failJob(ctx, j, "uncertain", "请求结果不明，请核对后处理", false)
	case missingMessage(text):
		switch j.kind {
		case "redact", "redactproof", "redactpost", "commentbuttons":
			return w.complete(ctx, j, delivery{note: "远端消息已不存在"})
		}
		return w.handleMissing(ctx, j)
	default:
		runes := []rune(text)
		return w.failJob(ctx, j, "blocked", string(runes[:min(200, len(runes))]), apiError.Code == 403)
	}
}

func (w *Worker) failJob(ctx context.Context, j job, state, reason string, disable bool) error {
	return pgx.BeginFunc(ctx, w.db, func(tx pgx.Tx) error {
		if disable {
			_, err := tx.Exec(ctx, `UPDATE cpbot.bindings SET active=0,error=$3 WHERE (cluster_id,id)=($1,$2)`, j.cid, j.binding, reason)
			if err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state=$3,error=$4 WHERE (cluster_id,id)=($1,$2)`, j.cid, j.id, state, reason)
		return err
	})
}
