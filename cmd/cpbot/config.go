package main

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var tokenPattern = regexp.MustCompile(`^[0-9]+:[A-Za-z0-9_-]+$`)

// Use the same non-executing ENV_FILE convention as the Python release.
func loadEnv() error {
	path := os.Getenv("ENV_FILE")
	if path == "" {
		path = ".env"
	}
	if path == "~" || strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return err
		}
		path = filepath.Join(home, strings.TrimPrefix(path, "~"))
	}
	contents, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	for _, line := range strings.Split(string(contents), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		key = strings.TrimSpace(key)
		if !ok || key == "" {
			continue
		}
		if _, exists := os.LookupEnv(key); !exists {
			if err := os.Setenv(key, strings.Trim(strings.TrimSpace(value), `"'`)); err != nil {
				return fmt.Errorf("配置文件 %s 含无效变量", path)
			}
		}
	}
	return nil
}

func adminsFromEnv() (map[int64]bool, error) {
	admins := make(map[int64]bool)
	for _, key := range []string{"SUPERADMIN_IDS", "SUPERADMIN_ID"} {
		for _, raw := range strings.FieldsFunc(os.Getenv(key), func(r rune) bool {
			return r == ',' || r == '，' || strings.ContainsRune(" \t\r\n", r)
		}) {
			id, err := strconv.ParseInt(raw, 10, 64)
			if id <= 0 || err != nil {
				return nil, errors.New("Superadmin ID 必须是正整数")
			}
			admins[id] = true
		}
	}
	if len(admins) == 0 {
		return nil, errors.New("请设置 SUPERADMIN_IDS")
	}
	return admins, nil
}

func defaultSource() (string, error) {
	path := os.Getenv("DATABASE_PATH")
	if path == "" {
		return "data/cpbot.sqlite3", nil
	}
	if strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		path = filepath.Join(home, strings.TrimPrefix(path, "~/"))
	}
	return path, nil
}
