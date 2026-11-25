// publish-driver is an isolated acceptance entry point for the Go publisher.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"cpbot/internal/publish"
	"cpbot/internal/telegram"
	"github.com/jackc/pgx/v5"
)

type step struct {
	Operation string           `json:"operation"`
	Now       time.Time        `json:"now"`
	Cluster   int64            `json:"cluster"`
	Job       int64            `json:"job"`
	RemoteID  int64            `json:"remote_id"`
	Message   telegram.Message `json:"message"`
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	ctx := context.Background()
	db, err := pgx.Connect(ctx, os.Getenv("DATABASE_URL"))
	if err != nil {
		return err
	}
	defer db.Close(ctx)
	api := telegram.At(os.Getenv("CPBOT_TEST_API"))
	worker := publish.New(db, api, "review_bot", 99, os.Getenv("CPBOT_TEST_SOURCE"))
	var steps []step
	if err := json.NewDecoder(os.Stdin).Decode(&steps); err != nil {
		return err
	}
	var results []any
	for _, next := range steps {
		var result any
		switch next.Operation {
		case "recover":
			err = worker.Recover(ctx)
		case "tick":
			err = worker.Tick(ctx, next.Now)
		case "nightly":
			err = worker.ScheduleNightly(ctx, next.Now)
		case "forward":
			result, err = worker.AutomaticForward(ctx, next.Message)
		case "poll-timeout":
			result, err = worker.PollTimeout(ctx)
		case "confirm":
			err = worker.ConfirmUncertain(ctx, next.Cluster, next.Job, 1, next.RemoteID)
		case "retry":
			err = worker.RetryUncertain(ctx, next.Cluster, next.Job, 1)
		default:
			return fmt.Errorf("unknown publish step %q", next.Operation)
		}
		if err != nil {
			return fmt.Errorf("%s: %w", next.Operation, err)
		}
		results = append(results, result)
	}
	return json.NewEncoder(os.Stdout).Encode(results)
}
