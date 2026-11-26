package miniapp

import (
	"errors"
	"io"
	"net/http"
	"os"
	"slices"
	"strconv"
	"time"

	"cpbot/internal/pgstore"
	"github.com/jackc/pgx/v5"
)

// A proof must have been published, either as the review itself or as a legacy separate photo.
// The JSON lookup keeps queries working before the bot applies the optional photo column upgrade.
const publishedProof = `(coalesce(r.data->>'出击证明','') ~ '^[a-f0-9]{32}\.jpg$' AND EXISTS (
	SELECT 1 FROM (
		SELECT p.binding,p.generation,p.message FROM cpbot.publications p
		WHERE (p.cluster_id,p.review)=(r.cluster_id,r.id) AND to_jsonb(p)->'photo'='true'::jsonb
		UNION ALL
		SELECT p.binding,p.generation,p.message FROM cpbot.proof_publications p
		WHERE (p.cluster_id,p.review)=(r.cluster_id,r.id)
	) p
	JOIN cpbot.bindings b ON (b.cluster_id,b.id)=(r.cluster_id,p.binding)
	LEFT JOIN cpbot.posts t ON (t.cluster_id,t.binding,t.teacher)=(r.cluster_id,b.id,r.teacher)
	WHERE b.active=1 AND p.message>0
	AND ((b.mode='thread' AND p.generation=t.generation) OR (b.mode='feed' AND p.generation=1))
))`

func (s *server) proof(w http.ResponseWriter, r *http.Request, ids []int64) error {
	missing := func() { problem(w, http.StatusNotFound, "出击证明不可查看。") }
	cid, ce := strconv.ParseInt(r.PathValue("cluster"), 10, 64)
	rid, re := strconv.ParseInt(r.PathValue("review"), 10, 64)
	if ce != nil || re != nil || rid < 1 || !slices.Contains(ids, cid) {
		missing()
		return nil
	}
	var name string
	err := s.db.QueryRow(r.Context(), `SELECT r.data->>'出击证明' FROM cpbot.reviews r
		JOIN cpbot.teachers t ON (t.cluster_id,t.id)=(r.cluster_id,r.teacher)
		JOIN cpbot.clusters c ON c.id=r.cluster_id
		WHERE (r.cluster_id,r.id)=($1,$2) AND `+visibleReview+` AND `+publishedProof, cid, rid).Scan(&name)
	if errors.Is(err, pgx.ErrNoRows) {
		missing()
		return nil
	}
	if err != nil {
		return err
	}
	path, err := pgstore.ProofPath(s.source, cid, name)
	if err != nil {
		missing()
		return nil
	}
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() == 0 || info.Size() > pgstore.MaxProofBytes {
		missing()
		return nil
	}
	file, err := os.Open(path)
	if err != nil {
		missing()
		return nil
	}
	defer file.Close()
	var header [512]byte
	n, err := file.ReadAt(header[:], 0)
	if err != nil && !errors.Is(err, io.EOF) {
		missing()
		return nil
	}
	kind := http.DetectContentType(header[:n])
	switch kind {
	case "image/jpeg", "image/png", "image/gif", "image/webp":
		w.Header().Set("Content-Type", kind)
		http.ServeContent(w, r, "proof", time.Time{}, file)
	default:
		missing()
	}
	return nil
}
