package bot

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"cpbot/internal/pgstore"
	"github.com/jackc/pgx/v5"
)

func (b *Bot) cleanup(ctx context.Context, now time.Time) error {
	rows, err := b.db.Query(ctx, `SELECT t.cluster_id,t.id FROM cpbot.teachers t
		JOIN cpbot.cluster_authorizations a USING(cluster_id)
		LEFT JOIN cpbot.settings s ON (s.cluster_id,s.key)=(t.cluster_id,'teacher_trash_days')
		WHERE t.deleted_at IS NOT NULL AND t.deleted_at <= $1-coalesce(s.value::integer,30)*86400
		AND NOT EXISTS(SELECT 1 FROM cpbot.jobs j WHERE (j.cluster_id,j.teacher)=(t.cluster_id,t.id)
		AND (j.kind IN ('redact','redactproof','redactpost') OR j.state IN ('sending','uncertain')) AND j.state NOT IN ('done','superseded'))`,
		float64(now.UnixMicro())/1e6)
	if err != nil {
		return err
	}
	type teacher struct{ CID, ID int64 }
	ids, err := pgx.CollectRows(rows, pgx.RowToStructByPos[teacher])
	if err != nil {
		return err
	}
	for _, t := range ids {
		if err := b.purgeTeacher(ctx, 0, t.CID, t.ID); err != nil {
			var problem pgstore.UserError
			if !errors.As(err, &problem) {
				return err
			}
			slog.Warn("回收站清理待重试", "cluster", t.CID, "teacher", t.ID, "reason", problem)
		}
	}
	_, err = b.db.Exec(ctx, `DELETE FROM cpbot.reviews r USING cpbot.cluster_authorizations a
		WHERE a.cluster_id=r.cluster_id AND r.status IN ('pending','rejected') AND r.expires<=$1`, float64(now.UnixMicro())/1e6)
	return err
}
