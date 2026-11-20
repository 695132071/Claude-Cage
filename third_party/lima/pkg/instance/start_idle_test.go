package instance

import (
	"context"
	"testing"
	"time"
)

func TestWithIdleDeadlineExtendsOnProgress(t *testing.T) {
	ctx, touch, expired := withIdleDeadline(context.Background(), 80*time.Millisecond)
	for range 5 {
		time.Sleep(40 * time.Millisecond)
		touch(true)
	}
	if ctx.Err() != nil || expired() {
		t.Fatal("progress did not postpone the idle deadline")
	}
	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("idle deadline never fired")
	}
	if !expired() {
		t.Fatal("idle expiry not reported")
	}
	touch(false)
}

func TestWithIdleDeadlineKeepsParentAndStop(t *testing.T) {
	parent, cancel := context.WithCancel(context.Background())
	ctx, touch, expired := withIdleDeadline(parent, time.Hour)
	cancel()
	<-ctx.Done()
	if expired() {
		t.Fatal("parent cancellation reported as idle")
	}
	touch(false)
	ctx, touch, expired = withIdleDeadline(context.Background(), 0)
	touch(true)
	if ctx.Err() != nil || expired() {
		t.Fatal("zero idle must disable the deadline")
	}
}

func TestWatchHostAgentTimeoutsUseSeparateKeys(t *testing.T) {
	ctx := WithWatchHostAgentTimeout(context.Background(), 2*time.Hour)
	ctx = WithWatchHostAgentIdleTimeout(ctx, 10*time.Minute)
	if got := watchHostAgentTimeout(ctx); got != 2*time.Hour {
		t.Fatalf("overall timeout = %v, want 2h: the idle timeout replaced it", got)
	}
	if got := watchHostAgentIdleTimeout(ctx); got != 10*time.Minute {
		t.Fatalf("idle timeout = %v, want 10m", got)
	}
	ctx = WithWatchHostAgentIdleTimeout(WithWatchHostAgentTimeout(context.Background(), time.Hour), time.Minute)
	ctx = WithWatchHostAgentTimeout(ctx, 3*time.Hour)
	if watchHostAgentIdleTimeout(ctx) != time.Minute || watchHostAgentTimeout(ctx) != 3*time.Hour {
		t.Fatal("setting the overall timeout after the idle one lost a value")
	}
}
