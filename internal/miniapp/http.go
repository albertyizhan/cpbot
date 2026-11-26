// Package miniapp serves the authenticated, read-only Telegram review browser.
package miniapp

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/singleflight"
)

//go:embed web/*
var assets embed.FS

type accessPolicy interface {
	VisibleClusterIDs(context.Context, int64) ([]int64, error)
}

type accessEntry struct {
	ids   []int64
	until time.Time
}

type server struct {
	db     *pgxpool.Pool
	access accessPolicy
	key    []byte
	mu     sync.Mutex
	cache  map[int64]accessEntry
	checks singleflight.Group
	slots  chan struct{}
}

func Handler(db *pgxpool.Pool, access accessPolicy, token string) http.Handler {
	key := hmac.New(sha256.New, []byte("WebAppData"))
	key.Write([]byte(token))
	s := &server{db: db, access: access, key: key.Sum(nil), cache: make(map[int64]accessEntry), slots: make(chan struct{}, 16)}
	web, err := fs.Sub(assets, "web")
	if err != nil {
		panic(err)
	}
	files := http.FileServerFS(web)
	mux := http.NewServeMux()
	mux.Handle("GET /{$}", files)
	mux.Handle("GET /app.js", files)
	mux.Handle("GET /app.css", files)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), time.Second)
		defer cancel()
		if err := s.db.Ping(ctx); err != nil {
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		w.Write([]byte("ok\n"))
	})
	mux.Handle("GET /api/feed", s.authorize(s.feed))
	mux.Handle("GET /api/teachers", s.authorize(s.teachers))
	mux.Handle("GET /api/teachers/{cluster}/{teacher}", s.authorize(s.teacherReviews))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self' https://web.telegram.org https://*.web.telegram.org")
		mux.ServeHTTP(w, r)
	})
}

func (s *server) identify(raw string) (int64, bool) {
	values, err := url.ParseQuery(raw)
	if err != nil {
		return 0, false
	}
	digest, err := hex.DecodeString(values.Get("hash"))
	if err != nil || len(digest) != sha256.Size {
		return 0, false
	}
	pairs := make([]string, 0, len(values))
	for key, vals := range values {
		if len(vals) != 1 {
			return 0, false
		}
		if key != "hash" {
			pairs = append(pairs, key+"="+vals[0])
		}
	}
	sort.Strings(pairs)
	mac := hmac.New(sha256.New, s.key)
	mac.Write([]byte(strings.Join(pairs, "\n")))
	if !hmac.Equal(mac.Sum(nil), digest) {
		return 0, false
	}
	stamp, err := strconv.ParseInt(values.Get("auth_date"), 10, 64)
	now := time.Now().Unix()
	if err != nil || stamp < now-8*3600 || stamp > now+60 {
		return 0, false
	}
	var user struct {
		ID int64 `json:"id"`
	}
	err = json.Unmarshal([]byte(values.Get("user")), &user)
	return user.ID, err == nil && user.ID > 0
}

func (s *server) clusterIDs(ctx context.Context, uid int64) ([]int64, error) {
	s.mu.Lock()
	entry := s.cache[uid]
	s.mu.Unlock()
	if time.Now().Before(entry.until) {
		return entry.ids, nil
	}
	result := s.checks.DoChan(strconv.FormatInt(uid, 10), func() (any, error) {
		check, cancel := context.WithTimeout(context.WithoutCancel(ctx), 7*time.Second)
		defer cancel()
		ids, err := s.access.VisibleClusterIDs(check, uid)
		if err != nil {
			return nil, err
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		now := time.Now()
		if len(s.cache) >= 2048 {
			for id, old := range s.cache {
				if !now.Before(old.until) {
					delete(s.cache, id)
				}
			}
		}
		if len(s.cache) < 2048 {
			ttl := 45 * time.Second
			if len(ids) == 0 {
				ttl = 5 * time.Second
			}
			s.cache[uid] = accessEntry{ids: ids, until: now.Add(ttl)}
		}
		return ids, nil
	})
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case out := <-result:
		if out.Err != nil {
			return nil, out.Err
		}
		return out.Val.([]int64), nil
	}
}

type endpoint func(http.ResponseWriter, *http.Request, []int64) error

func (s *server) authorize(next endpoint) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Telegram ")
		uid, valid := s.identify(raw)
		if !ok || !valid {
			problem(w, http.StatusUnauthorized, "请从 Telegram 机器人打开或重新打开小程序。")
			return
		}
		select {
		case s.slots <- struct{}{}:
			defer func() { <-s.slots }()
		default:
			problem(w, http.StatusServiceUnavailable, "访问较多，请稍后重试。")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
		defer cancel()
		ids, err := s.clusterIDs(ctx, uid)
		if err == nil && len(ids) == 0 {
			problem(w, http.StatusForbidden, "请先加入有查评权限的群或频道，再重试。")
			return
		}
		if err == nil {
			err = next(w, r.WithContext(ctx), ids)
		}
		if err != nil && !errors.Is(err, context.Canceled) {
			slog.Warn("Mini App 查询失败", "route", r.Pattern, "error", err)
			problem(w, http.StatusServiceUnavailable, "暂时无法读取车评，请稍后重试。")
		}
	})
}

func problem(w http.ResponseWriter, status int, message string) {
	respond(w, status, map[string]string{"message": message})
}

func respond(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
