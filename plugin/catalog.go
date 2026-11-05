package main

import (
	"context"
	"net/http"
	"strconv"
	"time"
)

type catalogLoad struct {
	done chan struct{}
	rows []object
	err  error
}

func waitContext(ctx context.Context, delay time.Duration) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func retryDelay(value string, now time.Time, attempt int) time.Duration {
	fallback := time.Second * time.Duration(1<<attempt)
	if seconds, err := strconv.ParseInt(value, 10, 64); err == nil && seconds >= 0 {
		if seconds > 600 {
			return 11 * time.Minute
		}
		return max(fallback, time.Duration(seconds)*time.Second)
	}
	if date, err := http.ParseTime(value); err == nil {
		return max(fallback, date.Sub(now))
	}
	return fallback
}

// One bounded scan serves all callers. A short HTTP request may time out, but
// does not discard pagination progress or start a second scan on its next poll.
// Publish only a complete snapshot: partial results could look like deletions.
func (b *Bridge) catalog(ctx context.Context) ([]object, error) {
	const path = "/admin/catalog/items"
	b.mu.Lock()
	if saved, ok := b.rows[path]; ok && time.Now().Before(saved.until) {
		b.mu.Unlock()
		return saved.rows, nil
	}
	if b.catalogError != nil && time.Now().Before(b.catalogRetryAt) {
		err := b.catalogError
		b.mu.Unlock()
		return nil, err
	}
	load := b.catalogFlight
	if load == nil {
		load = &catalogLoad{done: make(chan struct{})}
		b.catalogFlight = load
		go func() {
			scanCtx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
			defer cancel()
			rows, err := b.collection(scanCtx, path, true)
			b.mu.Lock()
			defer b.mu.Unlock()
			load.rows, load.err = rows, err
			b.catalogError = err
			if err == nil {
				b.catalogUpdated = time.Now().UTC()
				b.rows[path] = cachedRows{b.catalogUpdated.Add(5 * time.Minute), rows}
			} else {
				b.catalogRetryAt = time.Now().Add(10 * time.Second)
			}
			b.catalogFlight = nil
			close(load.done)
		}()
	}
	b.mu.Unlock()
	select {
	case <-ctx.Done():
		return nil, fail(503, "catalog scan is still running; retry shortly")
	case <-load.done:
		return load.rows, load.err
	}
}
