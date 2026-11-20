package nativeclient

import (
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestResumeRebindsReclaimedLoopbackListener(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "mobile.html"), []byte("<!doctype html><html>ok</html>"), 0o600); err != nil {
		t.Fatal(err)
	}
	c := &Client{}
	launchURL, err := c.StartUI(dir)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()

	base := strings.SplitN(launchURL, "/link-bootstrap", 2)[0]
	resp, err := http.Get(base + "/link-bootstrap")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 before reclaim, got %d", resp.StatusCode)
	}

	// Simulate iOS reclaiming the listening socket while the app is suspended.
	c.mu.Lock()
	oldListener := c.listener
	c.mu.Unlock()
	if err := oldListener.Close(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		c.mu.Lock()
		cleared := c.listener == nil
		c.mu.Unlock()
		if cleared {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}

	if !c.Resume() {
		t.Fatal("Resume failed to rebind reclaimed loopback listener")
	}

	resp, err = http.Get(base + "/link-bootstrap")
	if err != nil {
		t.Fatalf("loopback request failed after Resume: %v", err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("expected 200 after Resume, got %d", resp.StatusCode)
	}
}
