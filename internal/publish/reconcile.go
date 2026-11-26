package publish

import (
	"context"
	"errors"
	"strconv"
	"time"
	_ "time/tzdata"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type executor interface {
	Exec(context.Context, string, ...any) (pgconn.CommandTag, error)
}

func Enqueue(ctx context.Context, db executor, cid int64, kind string, binding, teacher, review, generation int64) error {
	_, err := db.Exec(ctx, `INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
		VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (cluster_id,kind,binding,teacher,review,generation) DO NOTHING`,
		cid, kind, binding, teacher, review, generation)
	return err
}

func (w *Worker) handleMissing(ctx context.Context, j job) error {
	return pgx.BeginFunc(ctx, w.db, func(tx pgx.Tx) error {
		if j.mode == "thread" {
			if err := rebuild(ctx, tx, j.cid, j.binding, j.teacher); err != nil {
				return err
			}
			_, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state='superseded' WHERE (cluster_id,id)=($1,$2)`, j.cid, j.id)
			return err
		}
		if j.kind == "proof" {
			_, err := tx.Exec(ctx, `DELETE FROM cpbot.proof_publications WHERE (cluster_id,binding,review,generation)=($1,$2,$3,1)`, j.cid, j.binding, j.review)
			if err != nil {
				return err
			}
		}
		_, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state='blocked',error='原消息已丢失' WHERE (cluster_id,id)=($1,$2)`, j.cid, j.id)
		return err
	})
}

func rebuild(ctx context.Context, tx pgx.Tx, cid, bid, tid int64) error {
	_, err := tx.Exec(ctx, `UPDATE cpbot.posts SET message=NULL,root=NULL,generation=generation+1
		WHERE (cluster_id,binding,teacher)=($1,$2,$3)`, cid, bid, tid)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE cpbot.jobs SET state='superseded'
		WHERE (cluster_id,binding,teacher)=($1,$2,$3) AND state IN ('pending','blocked')`, cid, bid, tid)
	if err != nil {
		return err
	}
	return EnsurePost(ctx, tx, cid, bid, tid)
}

// EnsurePost seeds one teacher replica, then plans publications for all of its accepted reviews.
func EnsurePost(ctx context.Context, tx pgx.Tx, cid, bid, tid int64) error {
	_, err := tx.Exec(ctx, `INSERT INTO cpbot.posts(cluster_id,binding,teacher) VALUES($1,$2,$3)
		ON CONFLICT (cluster_id,binding,teacher) DO NOTHING`, cid, bid, tid)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
		SELECT cluster_id,'post',binding,teacher,0,generation FROM cpbot.posts
		WHERE (cluster_id,binding,teacher)=($1,$2,$3) AND message IS NULL
		UNION ALL
	SELECT p.cluster_id,'comment',p.binding,p.teacher,r.id,p.generation FROM cpbot.posts p
		JOIN cpbot.reviews r ON (r.cluster_id,r.teacher)=(p.cluster_id,p.teacher) AND r.status='accepted'
		WHERE (p.cluster_id,p.binding,p.teacher)=($1,$2,$3)
		ON CONFLICT (cluster_id,kind,binding,teacher,review,generation) DO NOTHING`, cid, bid, tid)
	return err
}

var shanghai = time.FixedZone("Asia/Shanghai", 8*3600)

func init() {
	// tzdata is embedded for Debian systems without a system timezone file.
	if location, err := time.LoadLocation("Asia/Shanghai"); err == nil {
		shanghai = location
	}
}

// ScheduleNightly catches up missed 02:30 inspection after a restart, once per cluster and date.
func (w *Worker) ScheduleNightly(ctx context.Context, now time.Time) error {
	local := now.In(shanghai)
	if local.Hour()*60+local.Minute() < 150 {
		return nil
	}
	day := local.Format("2006-01-02")
	revision, err := strconv.ParseInt(local.Format("20060102"), 10, 64)
	if err != nil {
		return err
	}
	rows, err := w.db.Query(ctx, `SELECT a.cluster_id FROM cpbot.cluster_authorizations a
		LEFT JOIN cpbot.settings s ON (s.cluster_id,s.key)=(a.cluster_id,'last_check')
		WHERE s.value IS DISTINCT FROM $1 ORDER BY a.cluster_id`, day)
	if err != nil {
		return err
	}
	clusters, err := pgx.CollectRows(rows, pgx.RowTo[int64])
	if err != nil {
		return err
	}
	for _, cid := range clusters {
		if err := pgx.BeginFunc(ctx, w.db, func(tx pgx.Tx) error {
			return scheduleCluster(ctx, tx, cid, revision, day)
		}); err != nil {
			return err
		}
	}
	return nil
}

func scheduleCluster(ctx context.Context, tx pgx.Tx, cid, revision int64, day string) error {
	_, err := tx.Exec(ctx, `INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
		SELECT cluster_id,'health',id,0,0,$2 FROM cpbot.bindings WHERE cluster_id=$1 AND mode='thread' AND active=1
		ON CONFLICT (cluster_id,kind,binding,teacher,review,generation) DO NOTHING`, cid, revision)
	if err != nil {
		return err
	}
	rows, err := tx.Query(ctx, `SELECT b.id AS binding,t.id AS teacher FROM cpbot.bindings b
		JOIN cpbot.teachers t USING (cluster_id)
		WHERE b.cluster_id=$1 AND b.mode='thread' AND b.active=1 AND t.deleted_at IS NULL ORDER BY b.id,t.id`, cid)
	if err != nil {
		return err
	}
	type replica struct{ Binding, Teacher int64 }
	replicas, err := pgx.CollectRows(rows, pgx.RowToStructByPos[replica])
	if err != nil {
		return err
	}
	for _, r := range replicas {
		if err := EnsurePost(ctx, tx, cid, r.Binding, r.Teacher); err != nil {
			return err
		}
		var misplaced bool
		err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM (
			SELECT pub.message,lag(pub.message) OVER (ORDER BY r.data->>'时间',r.id) AS previous
			FROM cpbot.posts p JOIN cpbot.publications pub ON (p.cluster_id,p.binding,p.generation)=(pub.cluster_id,pub.binding,pub.generation)
			JOIN cpbot.reviews r ON (r.cluster_id,r.id,r.teacher)=(pub.cluster_id,pub.review,p.teacher)
			WHERE (p.cluster_id,p.binding,p.teacher)=($1,$2,$3) AND r.status='accepted'
		) seq WHERE previous>message)`, cid, r.Binding, r.Teacher).Scan(&misplaced)
		if err != nil {
			return err
		}
		if misplaced {
			if err := rebuild(ctx, tx, cid, r.Binding, r.Teacher); err != nil {
				return err
			}
		}
		if err := Enqueue(ctx, tx, cid, "checkpost", r.Binding, r.Teacher, 0, revision); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
			SELECT $1,k.kind,$2,$3,r.id,$4 FROM cpbot.reviews r CROSS JOIN (VALUES ('check'),('proof')) AS k(kind)
			WHERE (r.cluster_id,r.teacher)=($1,$3) AND r.status='accepted'
			AND (k.kind='check' OR EXISTS (
				SELECT 1 FROM cpbot.posts p JOIN cpbot.proof_publications proof
				ON (proof.cluster_id,proof.binding,proof.generation)=(p.cluster_id,p.binding,p.generation)
				WHERE (p.cluster_id,p.binding,p.teacher,proof.review)=($1,$2,$3,r.id)))
			ON CONFLICT (cluster_id,kind,binding,teacher,review,generation) DO NOTHING`, cid, r.Binding, r.Teacher, revision)
		if err != nil {
			return err
		}
	}
	_, err = tx.Exec(ctx, `INSERT INTO cpbot.settings(cluster_id,key,value) VALUES($1,'last_check',$2)
		ON CONFLICT (cluster_id,key) DO UPDATE SET value=excluded.value`, cid, day)
	return err
}

// RetryUncertain requeues only after the caller has verified the remote send failed.
func (w *Worker) RetryUncertain(ctx context.Context, cid, id, actor int64) error {
	return pgx.BeginFunc(ctx, w.db, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state='pending',due=0,error=''
			WHERE (cluster_id,id)=($1,$2) AND state='uncertain'`, cid, id)
		if err != nil {
			return err
		}
		if tag.RowsAffected() != 1 {
			return jobError("任务不在待核对状态")
		}
		_, err = tx.Exec(ctx, `INSERT INTO cpbot.audit(cluster_id,actor,action,detail,created)
			VALUES($1,$2,'确认未发送，重试任务',$3,extract(epoch from now()))`, cid, actor, strconv.FormatInt(id, 10))
		return err
	})
}

// ConfirmUncertain records a verified remote message for the replica owned by this job.
func (w *Worker) ConfirmUncertain(ctx context.Context, cid, id, actor, message int64) error {
	if message <= 0 {
		return jobError("消息 ID 必须大于 0")
	}
	return pgx.BeginFunc(ctx, w.db, func(tx pgx.Tx) error {
		j := job{cid: cid, id: id}
		var attemptGeneration *int64
		var attemptPhoto bool
		err := tx.QueryRow(ctx, `SELECT j.kind,j.binding,j.teacher,j.review,j.generation,b.mode,b.chat_id,b.discussion,
			p.message,p.generation,j.attempt_generation,j.attempt_photo FROM cpbot.jobs j
			JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(j.cluster_id,j.binding)
			LEFT JOIN cpbot.posts p ON (p.cluster_id,p.binding,p.teacher)=(j.cluster_id,j.binding,j.teacher)
			WHERE (j.cluster_id,j.id)=($1,$2) AND j.state='uncertain' FOR UPDATE OF j`, cid, id).
			Scan(&j.kind, &j.binding, &j.teacher, &j.review, &j.revision, &j.mode, &j.channel, &j.discussion, &j.post, &j.generation, &attemptGeneration, &attemptPhoto)
		if errors.Is(err, pgx.ErrNoRows) {
			return jobError("任务不在待核对状态")
		}
		if err != nil {
			return err
		}
		generation := j.replica()
		if attemptGeneration != nil {
			generation = *attemptGeneration
		} else if j.mode == "thread" && (j.kind == "comment" || j.kind == "post") {
			generation = j.revision
		}
		switch j.kind {
		case "post", "checkpost", "summary":
			if j.replica() != generation {
				return jobError("专帖已换代，请核对旧讨论串后处理")
			}
			_, err = tx.Exec(ctx, `UPDATE cpbot.posts SET message=$4 WHERE (cluster_id,binding,teacher)=($1,$2,$3)`, j.cid, j.binding, j.teacher, message)
		case "proof":
			_, err = tx.Exec(ctx, `INSERT INTO cpbot.proof_publications (cluster_id,binding,review,generation,message,chat_id)
				VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT (cluster_id,binding,review,generation) DO UPDATE
				SET message=excluded.message,chat_id=excluded.chat_id`, cid, j.binding, j.review, generation, message, j.destination())
		case "comment", "check", "feed":
			var insertedPhoto bool
			err = tx.QueryRow(ctx, `INSERT INTO cpbot.publications (cluster_id,binding,review,generation,message,chat_id,photo)
				VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (cluster_id,binding,review,generation) DO UPDATE
				SET message=excluded.message,chat_id=excluded.chat_id RETURNING photo`, cid, j.binding, j.review, generation, message, j.destination(), attemptPhoto).Scan(&insertedPhoto)
			if err == nil && !insertedPhoto && (j.mode != "thread" || j.replica() == generation) {
				_, err = tx.Exec(ctx, `INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
					SELECT $1,'proof',$2,$3,$4,$5 FROM cpbot.reviews
					WHERE (cluster_id,id)=($1,$4) AND coalesce(data->>'出击证明','')<>''
					ON CONFLICT (cluster_id,kind,binding,teacher,review,generation) DO NOTHING`, cid, j.binding, j.teacher, j.review, generation)
			}
		}
		if err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, `UPDATE cpbot.jobs SET state='done',error='' WHERE (cluster_id,id)=($1,$2)`, cid, id); err != nil {
			return err
		}
		_, err = tx.Exec(ctx, `INSERT INTO cpbot.audit(cluster_id,actor,action,detail,created)
			VALUES($1,$2,'登记发送结果',$3,extract(epoch from now()))`, cid, actor, strconv.FormatInt(id, 10))
		return err
	})
}

// EnqueueReview adds only the newly submitted review to each publishing binding.
func EnqueueReview(ctx context.Context, tx pgx.Tx, cid, tid, rid int64) error {
	if _, err := tx.Exec(ctx, `INSERT INTO cpbot.posts(cluster_id,binding,teacher)
		SELECT cluster_id,id,$2 FROM cpbot.bindings WHERE cluster_id=$1 AND mode='thread'
		ON CONFLICT (cluster_id,binding,teacher) DO NOTHING`, cid, tid); err != nil {
		return err
	}
	if _, err := tx.Exec(ctx, `INSERT INTO cpbot.jobs(cluster_id,kind,binding,teacher,review,generation)
		SELECT p.cluster_id,k.kind,p.binding,p.teacher,CASE WHEN k.kind='post' THEN 0 ELSE $3 END,p.generation
		FROM cpbot.posts p CROSS JOIN (VALUES ('post'),('comment')) AS k(kind)
		WHERE p.cluster_id=$1 AND p.teacher=$2 AND (k.kind<>'post' OR p.message IS NULL)
		UNION ALL SELECT b.cluster_id,'feed',b.id,$2,$3,1 FROM cpbot.bindings b WHERE b.cluster_id=$1 AND b.mode='feed'
		ON CONFLICT (cluster_id,kind,binding,teacher,review,generation) DO NOTHING`, cid, tid, rid); err != nil {
		return err
	}
	return nil
}

// ValidateBindings rechecks enabled targets before attempting any publication.
func (w *Worker) ValidateBindings(ctx context.Context) error {
	rows, err := w.db.Query(ctx, `SELECT b.cluster_id,b.id,b.chat_id,b.discussion,b.mode FROM cpbot.bindings b
		JOIN cpbot.cluster_authorizations a USING(cluster_id) WHERE b.active=1 ORDER BY b.cluster_id,b.id`)
	if err != nil {
		return err
	}
	var bindings []job
	for rows.Next() {
		var j job
		if err := rows.Scan(&j.cid, &j.binding, &j.channel, &j.discussion, &j.mode); err != nil {
			rows.Close()
			return err
		}
		bindings = append(bindings, j)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for _, j := range bindings {
		if err := w.checkBinding(ctx, j); err != nil {
			var problem jobError
			if !errors.As(err, &problem) {
				return err
			}
			if _, err := w.db.Exec(ctx, `UPDATE cpbot.bindings SET active=0,error=$3 WHERE (cluster_id,id)=($1,$2)`,
				j.cid, j.binding, problem.Error()); err != nil {
				return err
			}
		}
	}
	return nil
}
