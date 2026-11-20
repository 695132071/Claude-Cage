package cage

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestConfigChangeRequiresStop(t *testing.T) {
	dir := t.TempDir()
	os.MkdirAll(filepath.Join(dir, "lima", "test"), 0700)
	c := DefaultConfig()
	if err := c.RecordRunning(dir, "test"); err != nil {
		t.Fatal(err)
	}
	if err := c.CheckRunning(dir); err != nil {
		t.Fatal(err)
	}
	c.Timezone = "Asia/Tokyo"
	if c.CheckRunning(dir) == nil {
		t.Fatal("running profile silently changed")
	}
}

func TestRunningRecordRejectsReusedPID(t *testing.T) {
	dir := t.TempDir()
	instance := filepath.Join(dir, "lima", "test")
	os.MkdirAll(instance, 0700)
	c := DefaultConfig()
	if err := c.RecordRunning(dir, "test"); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(instance, "cage-running.json"))
	if err != nil {
		t.Fatal(err)
	}
	var record runningProfile
	if err := json.Unmarshal(b, &record); err != nil {
		t.Fatal(err)
	}
	if record.Started == "" {
		t.Skip("process start time is unavailable on this platform")
	}
	if !record.alive() {
		t.Fatal("current owner was not recognized")
	}
	legacy := runningProfile{PID: record.PID}
	if !legacy.alive() {
		t.Fatal("legacy PID-only record was rejected")
	}
	reused := runningProfile{PID: record.PID, Started: record.Started + "0", Fingerprint: record.Fingerprint}
	if reused.alive() {
		t.Fatal("record from an earlier process with the same PID was treated as running")
	}
	b, _ = json.Marshal(reused)
	if err := os.WriteFile(filepath.Join(instance, "cage-running.json"), b, 0600); err != nil {
		t.Fatal(err)
	}
	c.Timezone = "Asia/Tokyo"
	if err := c.CheckRunning(dir); err != nil {
		t.Fatalf("stale record blocked a configuration change: %v", err)
	}
}

// A PID reused by a process whose start time cannot be read, such as a
// protected Windows service after a reboot, is not the recorded owner (#167).
func TestRunningRecordRejectsUnreadableStartTime(t *testing.T) {
	if processStartToken(os.Getpid()) == "" {
		t.Skip("process start time is unavailable on this platform")
	}
	cmd := exec.Command(os.Args[0], "-test.run=^$")
	if err := cmd.Run(); err != nil {
		t.Fatal(err)
	}
	gone := cmd.Process.Pid
	if processStartToken(gone) != "" {
		t.Skip("the exited child's PID was reused")
	}
	if ProcessStartMatches(gone, "1") {
		t.Fatal("an unreadable start time matched a recorded owner")
	}
	if !ProcessStartMatches(gone, "") {
		t.Fatal("a legacy record without a start time lost its PID-only check")
	}
}

// A power loss can leave an empty or cut-off running record behind. It must
// not block the next start, while a live record is still checked.
func TestUnreadableRunningRecordDoesNotBlockStart(t *testing.T) {
	dir := t.TempDir()
	c := DefaultConfig()
	for name, content := range map[string]string{"empty": "", "cut": `{"pid":12`} {
		instance := filepath.Join(dir, "lima", name)
		if err := os.MkdirAll(instance, 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(instance, "cage-running.json"), []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.CheckRunning(dir); err != nil {
		t.Fatal("unreadable record blocked the start:", err)
	}
	live := filepath.Join(dir, "lima", "live")
	if err := os.MkdirAll(live, 0700); err != nil {
		t.Fatal(err)
	}
	if err := c.RecordRunning(dir, "live"); err != nil {
		t.Fatal(err)
	}
	changed := c
	changed.Timezone = "Asia/Tokyo"
	if changed.CheckRunning(dir) == nil {
		t.Fatal("a live record next to unreadable ones was ignored")
	}
	// The next start replaces the unreadable record whole.
	if err := c.RecordRunning(dir, "empty"); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(dir, "lima", "empty", "cage-running.json"))
	if err != nil || !json.Valid(b) {
		t.Fatalf("record not replaced: %q %v", b, err)
	}
	if leftovers, _ := filepath.Glob(filepath.Join(dir, "lima", "*", ".running-*")); len(leftovers) != 0 {
		t.Fatal("temporary records left behind:", leftovers)
	}
}

func TestTruncatedRunningAndGateRecordsAllowRecovery(t *testing.T) {
	dir := t.TempDir()
	instance := filepath.Join(dir, "lima", "interrupted")
	if err := os.MkdirAll(instance, 0700); err != nil {
		t.Fatal(err)
	}
	for _, content := range []string{"", `{"pid":`} {
		if err := os.WriteFile(filepath.Join(instance, "cage-running.json"), []byte(content), 0600); err != nil {
			t.Fatal(err)
		}
		if err := stoppedStorage(dir); err != nil {
			t.Fatalf("unreadable cage-running.json %q blocked storage check: %v", content, err)
		}
	}
	// Truncated cage-gate.json left by a crash is replaced cleanly on next start.
	gatePath := filepath.Join(instance, gateRecordName)
	if err := os.WriteFile(gatePath, []byte(`{"socket":`), 0600); err != nil {
		t.Fatal(err)
	}
	cfg := DefaultConfig()
	cfgPath := filepath.Join(dir, "config.json")
	rawCfg, _ := json.Marshal(cfg)
	if err := os.WriteFile(cfgPath, rawCfg, 0600); err != nil {
		t.Fatal(err)
	}
	gate, err := NewEgressGate(cfg)
	if err != nil {
		t.Fatal(err)
	}
	stop, err := StartGateControl(cfgPath, instance, gate)
	if err != nil {
		t.Fatalf("StartGateControl failed over truncated gate record: %v", err)
	}
	defer stop()
	rawGate, err := os.ReadFile(gatePath)
	if err != nil || !json.Valid(rawGate) {
		t.Fatalf("gate record not replaced cleanly: %q %v", rawGate, err)
	}
	if leftovers, _ := filepath.Glob(filepath.Join(instance, ".gate-*")); len(leftovers) != 0 {
		t.Fatal("temporary gate records left behind:", leftovers)
	}
}
