package hostbridge

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestDefaultPluginsBundledAndDigestMatchesDeployed(t *testing.T) {
	defaults := DefaultPlugins()
	if len(defaults) == 0 {
		t.Fatal("expected at least one bundled default plugin")
	}
	studio, ok := DefaultPluginByName("extension-studio")
	if !ok {
		t.Fatal("expected extension-studio in bundled defaults")
	}
	if studio.Manifest.Kind != KindGuest {
		t.Fatalf("kind = %q, want %q", studio.Manifest.Kind, KindGuest)
	}
	if len(studio.Manifest.Permissions) != 0 {
		t.Fatalf("permissions = %v, want empty", studio.Manifest.Permissions)
	}
	if len(studio.I18n) != 15 {
		t.Fatalf("i18n languages = %d, want 15", len(studio.I18n))
	}

	dir := t.TempDir()
	digest, err := InstallDefaultPlugin("extension-studio", dir)
	if err != nil {
		t.Fatalf("InstallDefaultPlugin: %v", err)
	}
	if digest != studio.Digest {
		t.Fatalf("digest mismatch: installed %s vs embedded %s", digest, studio.Digest)
	}
	deployedDigest, err := PluginDigest(filepath.Join(dir, "extension-studio"), "extension-studio")
	if err != nil {
		t.Fatalf("PluginDigest: %v", err)
	}
	if deployedDigest != studio.Digest {
		t.Fatalf("deployed PluginDigest %s != embedded %s", deployedDigest, studio.Digest)
	}
	// Installing again must not overwrite an existing folder.
	if _, err := InstallDefaultPlugin("extension-studio", dir); err == nil {
		t.Fatal("expected error when default plugin already exists")
	}
}

func TestExtensionStudioEndToEndAndNeutrality(t *testing.T) {
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 unavailable")
	}
	installRoot := t.TempDir()
	if _, err := InstallDefaultPlugin("extension-studio", installRoot); err != nil {
		t.Fatal(err)
	}
	studioDir := filepath.Join(installRoot, "extension-studio")
	draftsDir := t.TempDir()
	submissionsDir := t.TempDir()

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	cmd := exec.CommandContext(ctx, python, filepath.Join(studioDir, "server.py"))
	cmd.Dir = studioDir
	cmd.Env = append(os.Environ(), "EXT_DRAFTS_DIR="+draftsDir, "EXT_SUBMISSIONS_DIR="+submissionsDir)

	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil)
	session, err := client.Connect(ctx, &mcp.CommandTransport{Command: cmd, TerminateDuration: 2 * time.Second}, nil)
	if err != nil {
		t.Fatalf("connect extension-studio: %v", err)
	}
	defer session.Close()

	listed, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatalf("ListTools: %v", err)
	}
	if len(listed.Tools) != 5 {
		t.Fatalf("got %d tools, want 5", len(listed.Tools))
	}
	for _, tool := range listed.Tools {
		if word, found := CheckWording(tool); found {
			t.Fatalf("tool %s violates CheckWording with reserved word %q", tool.Name, word)
		}
	}

	callText := func(name string, args map[string]any) (string, bool) {
		t.Helper()
		res, err := session.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
		if err != nil {
			t.Fatalf("CallTool %s: %v", name, err)
		}
		var sb strings.Builder
		for _, c := range res.Content {
			if tc, ok := c.(*mcp.TextContent); ok {
				sb.WriteString(tc.Text)
			}
		}
		return sb.String(), res.IsError
	}

	// Verify the returned guide never exposes reserved environment/product wording.
	guide, isErr := callText("ext_get_guide", map[string]any{})
	if isErr || guide == "" {
		t.Fatalf("ext_get_guide failed: err=%v text=%q", isErr, guide)
	}
	if word, found := CheckWording(&mcp.Tool{Name: "guide", Description: guide}); found {
		t.Fatalf("ext_get_guide output leaks reserved word %q", word)
	}

	// Scaffold a Python MCP + view extension.
	scaffoldOut, isErr := callText("ext_scaffold", map[string]any{
		"name":         "demo-widget",
		"template":     "mcp-with-view-python",
		"display_name": "Demo Widget",
		"description":  "Shows a simple timestamp status panel.",
	})
	if isErr {
		t.Fatalf("ext_scaffold error: %s", scaffoldOut)
	}

	// Validate the freshly scaffolded extension (includes live MCP smoke test).
	valOut, isErr := callText("ext_validate", map[string]any{"name": "demo-widget"})
	if isErr {
		t.Fatalf("ext_validate failed: %s", valOut)
	}
	var report struct {
		OK     bool     `json:"ok"`
		Errors []string `json:"errors"`
		Tools  []struct {
			Name string `json:"name"`
		} `json:"tools"`
	}
	if err := json.Unmarshal([]byte(valOut), &report); err != nil {
		t.Fatalf("decode validation report: %v\n%s", err, valOut)
	}
	if !report.OK || len(report.Tools) != 1 || report.Tools[0].Name != "demo_widget_run" {
		t.Fatalf("unexpected validation report: %+v", report)
	}

	// Inject an inline script to confirm CSP validation catches it.
	htmlPath := filepath.Join(draftsDir, "demo-widget", "view", "index.html")
	origHTML, err := os.ReadFile(htmlPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(htmlPath, append(origHTML, []byte("<script>alert(1)</script>")...), 0o644); err != nil {
		t.Fatal(err)
	}
	badOut, _ := callText("ext_validate", map[string]any{"name": "demo-widget"})
	if !strings.Contains(badOut, "inline <script>") {
		t.Fatalf("expected CSP inline script error, got: %s", badOut)
	}
	// Restore clean HTML and submit.
	if err := os.WriteFile(htmlPath, origHTML, 0o644); err != nil {
		t.Fatal(err)
	}
	subOut, isErr := callText("ext_submit_draft", map[string]any{
		"name":    "demo-widget",
		"summary": "Initial demo widget with sidebar view",
	})
	if isErr {
		t.Fatalf("ext_submit_draft failed: %s", subOut)
	}

	// Host-side ValidateDraftDir should translate extension.json into valid plugin.json.
	submittedFiles := filepath.Join(submissionsDir, "demo-widget", "files")
	manifest, manifestBytes, err := ValidateDraftDir(submittedFiles, "demo-widget", "", "")
	if err != nil {
		t.Fatalf("ValidateDraftDir: %v", err)
	}
	if manifest.Kind != KindGuest || manifest.Engines == nil || manifest.Engines.Cage != DefaultDraftEngineRange {
		t.Fatalf("unexpected translated manifest: %+v", manifest)
	}
	if !strings.Contains(string(manifestBytes), `"kind": "guest"`) {
		t.Fatalf("manifest bytes missing guest kind: %s", manifestBytes)
	}

	// Host override to kind=host works too.
	hostManifest, _, err := ValidateDraftDir(submittedFiles, "demo-widget", KindHost, "")
	if err != nil {
		t.Fatalf("ValidateDraftDir(host): %v", err)
	}
	if hostManifest.Kind != KindHost {
		t.Fatalf("kind = %q, want host", hostManifest.Kind)
	}

	// Symlink inside draft directory must be rejected on host side.
	if err := os.Symlink("/etc/hosts", filepath.Join(submittedFiles, "escape.txt")); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ValidateDraftDir(submittedFiles, "demo-widget", "", ""); err == nil {
		t.Fatal("expected symlink rejection")
	}
}

func TestParseDraftManifestRejectsUnknownFields(t *testing.T) {
	bad := []byte(`{
		"name": "bad-ext",
		"version": "0.1.0",
		"description": "Test",
		"target": "workspace",
		"permissions": [],
		"engines": {"cage": ">=0.2.0"},
		"contributes": {"mcpServers": [{"id": "s", "command": ["python3", "server.py"]}]}
	}`)
	if _, err := ParseDraftManifest(bad); err == nil {
		t.Fatal("expected unknown field 'engines' to be rejected in neutral extension.json")
	}
}
