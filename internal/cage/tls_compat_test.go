package cage

import (
	"os"
	"os/exec"
	"strings"
	"testing"
)

func TestInstallTLS12PreludeOptInAndCleanup(t *testing.T) {
	if got := InstallTLS12Prelude(false); got != "" {
		t.Fatalf("TLS 1.2 compatibility must be off by default, got: %q", got)
	}
	if strings.Contains(ToolsUpdateScript(), "tls-max") || strings.Contains(ToolsUpdateScript(), "CURL_HOME") {
		t.Fatal("default ToolsUpdateScript must not force TLS 1.2")
	}
	prelude := InstallTLS12Prelude(true)
	if !strings.Contains(prelude, `tls-max = "1.2"`) || !strings.Contains(prelude, "CURL_HOME") {
		t.Fatalf("opt-in prelude missing curl TLS 1.2 configuration: %q", prelude)
	}
	if strings.Contains(strings.ToLower(prelude), "cage") {
		t.Fatalf("install curl prelude must not expose project identity: %q", prelude)
	}
	if !strings.Contains(ToolsUpdateScriptFor(true), `tls-max = "1.2"`) {
		t.Fatal("ToolsUpdateScriptFor(true) missing opt-in TLS 1.2 prelude")
	}
	dir := t.TempDir()
	cmd := exec.CommandContext(t.Context(), "bash", "-c", "set -euo pipefail\n"+prelude+`printf '%s\n' "$CURL_HOME"; cat "$CURL_HOME/.curlrc"`)
	cmd.Env = append(os.Environ(), "HOME="+dir, "TMPDIR="+dir)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("bash execution failed: %v\n%s", err, out)
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) != 2 || lines[1] != `tls-max = "1.2"` {
		t.Fatalf("unexpected curlrc output: %q", out)
	}
	if _, err := os.Stat(lines[0]); !os.IsNotExist(err) {
		t.Fatalf("temporary CURL_HOME %q was not cleaned up on exit: %v", lines[0], err)
	}
}
