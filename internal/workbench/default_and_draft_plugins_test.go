package workbench

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/sylvinus/agent-vm/internal/hostbridge"
)

func trustedPluginRequest(s *Server, method, target, body string) *http.Request {
	req := httptest.NewRequest(method, target, strings.NewReader(body))
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	req.AddCookie(&http.Cookie{Name: s.cookieName() + "-desktop", Value: trustedViewProof(s.token)})
	return req
}

func TestDefaultPluginsLifecycleUninstallAndRestore(t *testing.T) {
	s := &Server{profile: t.TempDir(), token: randomToken(), origin: "http://localhost:19001"}

	// First startup deploys bundled default plugins.
	s.ensureDefaultPlugins()
	view := s.hostToolsView()
	found := false
	for _, p := range view.Plugins {
		if p.Name == "extension-studio" {
			found = true
			if !p.Default || p.Kind != hostbridge.KindGuest || p.Problem != "" {
				t.Fatalf("unexpected default plugin view: %+v", p)
			}
		}
	}
	if !found {
		t.Fatalf("expected extension-studio deployed on first run: %+v", view.Plugins)
	}
	if len(view.DismissedDefaults) != 0 {
		t.Fatalf("expected no dismissed defaults initially: %+v", view.DismissedDefaults)
	}

	// Uninstall via POST /api/host-tools: folder moves to plugins-removed and dismissed=true.
	rec := httptest.NewRecorder()
	s.setHostTools(rec, trustedPluginRequest(s, http.MethodPost, "/api/host-tools", `{"remove":"extension-studio"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("uninstall status=%d body=%s", rec.Code, rec.Body.String())
	}
	if _, err := os.Lstat(filepath.Join(s.hostPluginDir(), "extension-studio")); !os.IsNotExist(err) {
		t.Fatalf("expected plugin folder removed, err=%v", err)
	}
	if aside, _ := filepath.Glob(filepath.Join(s.profile, "plugins-removed", "extension-studio-*", hostbridge.ManifestFile)); len(aside) != 1 {
		t.Fatalf("expected backup in plugins-removed, got %v", aside)
	}

	// Subsequent ensureDefaultPlugins() (simulating app upgrade) MUST NOT force it back.
	s.ensureDefaultPlugins()
	if _, err := os.Lstat(filepath.Join(s.hostPluginDir(), "extension-studio")); !os.IsNotExist(err) {
		t.Fatalf("dismissed default plugin was reinstalled on restart")
	}
	view = s.hostToolsView()
	if len(view.DismissedDefaults) != 1 || view.DismissedDefaults[0].Name != "extension-studio" {
		t.Fatalf("expected extension-studio in dismissed_defaults, got %+v", view.DismissedDefaults)
	}

	// User explicitly restores the default plugin.
	rec = httptest.NewRecorder()
	s.setHostTools(rec, trustedPluginRequest(s, http.MethodPost, "/api/host-tools", `{"restore_default":"extension-studio"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("restore status=%d body=%s", rec.Code, rec.Body.String())
	}
	if _, err := hostbridge.LoadManifest(filepath.Join(s.hostPluginDir(), "extension-studio")); err != nil {
		t.Fatalf("restored default plugin failed to load: %v", err)
	}
	view = s.hostToolsView()
	if len(view.DismissedDefaults) != 0 {
		t.Fatalf("expected dismissed_defaults empty after restore, got %+v", view.DismissedDefaults)
	}
}

func TestDefaultPluginsPreserveLocalEditsAndAutoUpgradeUnmodified(t *testing.T) {
	s := &Server{profile: t.TempDir(), token: randomToken(), origin: "http://localhost:19002"}
	s.ensureDefaultPlugins()

	// Case 1: unmodified plugin with older tracked version auto-upgrades cleanly.
	s.defaultPluginsMu.Lock()
	recs := s.readDefaultPluginsLocked()
	rec := recs["extension-studio"]
	rec.InstalledVersion = "0.0.1"
	recs["extension-studio"] = rec
	if err := s.writeDefaultPluginsLocked(recs); err != nil {
		t.Fatal(err)
	}
	s.defaultPluginsMu.Unlock()

	s.ensureDefaultPlugins()
	if aside, _ := filepath.Glob(filepath.Join(s.profile, "plugins-removed", "extension-studio-*", hostbridge.ManifestFile)); len(aside) != 1 {
		t.Fatalf("expected auto-upgrade to preserve old copy in plugins-removed: %v", aside)
	}

	// Case 2: user edited a file locally -> must NOT be overwritten on upgrade.
	customNote := filepath.Join(s.hostPluginDir(), "extension-studio", "custom.txt")
	if err := os.WriteFile(customNote, []byte("my local tweak\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	s.defaultPluginsMu.Lock()
	recs = s.readDefaultPluginsLocked()
	rec = recs["extension-studio"]
	rec.InstalledVersion = "0.0.1"
	recs["extension-studio"] = rec
	_ = s.writeDefaultPluginsLocked(recs)
	s.defaultPluginsMu.Unlock()

	s.ensureDefaultPlugins()
	if data, err := os.ReadFile(customNote); err != nil || string(data) != "my local tweak\n" {
		t.Fatalf("expected local modification preserved, got %q err=%v", string(data), err)
	}
}

func writeTestSubmission(t *testing.T, storeRoot, name, target string, permissions []string, serverBody string) {
	t.Helper()
	filesDir := filepath.Join(storeRoot, name, "files")
	if err := os.MkdirAll(filesDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if permissions == nil {
		permissions = []string{}
	}
	draftManifest := map[string]any{
		"name":        name,
		"displayName": "Custom " + name,
		"version":     "0.2.0",
		"description": "Performs a custom task.",
		"target":      target,
		"permissions": permissions,
		"categories":  []string{"developer"},
		"contributes": map[string]any{
			"mcpServers": []map[string]any{
				{"id": "main", "command": []string{"python3", "server.py"}},
			},
		},
	}
	manifestBytes, _ := json.MarshalIndent(draftManifest, "", "  ")
	if err := os.WriteFile(filepath.Join(filesDir, hostbridge.DraftManifestFile), manifestBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(filesDir, "server.py"), []byte(serverBody), 0o755); err != nil {
		t.Fatal(err)
	}
	submission := map[string]any{
		"id":          name,
		"name":        name,
		"displayName": "Custom " + name,
		"version":     "0.2.0",
		"description": "Performs a custom task.",
		"summary":     "Added helper tool",
		"target":      target,
		"permissions": permissions,
		"categories":  []string{"developer"},
		"contributes": draftManifest["contributes"],
		"submittedAt": "2026-11-22T17:00:00Z",
		"files": []map[string]any{
			{"path": "extension.json", "size": len(manifestBytes), "text": true},
			{"path": "server.py", "size": len(serverBody), "text": true},
		},
	}
	subBytes, _ := json.MarshalIndent(submission, "", "  ")
	if err := os.WriteFile(filepath.Join(storeRoot, name, "submission.json"), subBytes, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestDraftReviewInstallRevokesAlwaysGrantAndProtectsBuiltIn(t *testing.T) {
	store := t.TempDir()
	s := &Server{
		profile:            t.TempDir(),
		token:              randomToken(),
		origin:             "http://localhost:19003",
		draftStoreOverride: store,
	}

	writeTestSubmission(t, store, "my-helper", "workspace", []string{}, "print('v2')\n")

	// List drafts.
	rec := httptest.NewRecorder()
	s.getHostPluginDrafts(rec, httptest.NewRequest(http.MethodGet, "/api/host-tools/drafts", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("list drafts status=%d body=%s", rec.Code, rec.Body.String())
	}
	var list pluginDraftsListView
	if err := json.Unmarshal(rec.Body.Bytes(), &list); err != nil {
		t.Fatal(err)
	}
	if !list.Ready || len(list.Drafts) != 1 || list.Drafts[0].Name != "my-helper" || list.Drafts[0].RecommendedKind != hostbridge.KindGuest {
		t.Fatalf("unexpected draft list: %+v", list)
	}

	// Untrusted request cannot read draft file or install.
	rec = httptest.NewRecorder()
	s.getHostPluginDraftFile(rec, httptest.NewRequest(http.MethodGet, "/api/host-tools/drafts/file?id=my-helper&path=server.py", nil))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for untrusted file view, got %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	untrustedInstall := httptest.NewRequest(http.MethodPost, "/api/host-tools/drafts/install", strings.NewReader(`{"id":"my-helper"}`))
	untrustedInstall.Header.Set("Content-Type", "application/json")
	s.installHostPluginDraft(rec, untrustedInstall)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for untrusted install, got %d", rec.Code)
	}

	// Trusted file preview works and rejects path traversal.
	rec = httptest.NewRecorder()
	s.getHostPluginDraftFile(rec, trustedPluginRequest(s, http.MethodGet, "/api/host-tools/drafts/file?id=my-helper&path=server.py", ""))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "print('v2')") {
		t.Fatalf("unexpected file preview: %d %s", rec.Code, rec.Body.String())
	}
	rec = httptest.NewRecorder()
	s.getHostPluginDraftFile(rec, trustedPluginRequest(s, http.MethodGet, "/api/host-tools/drafts/file?id=my-helper&path=../submission.json", ""))
	if rec.Code == http.StatusOK {
		t.Fatalf("expected path traversal rejected, got 200")
	}

	// Install as host plugin first.
	rec = httptest.NewRecorder()
	s.installHostPluginDraft(rec, trustedPluginRequest(s, http.MethodPost, "/api/host-tools/drafts/install", `{"id":"my-helper","kind":"host"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("install draft status=%d body=%s", rec.Code, rec.Body.String())
	}
	installedManifest, err := hostbridge.LoadManifest(filepath.Join(s.hostPluginDir(), "my-helper"))
	if err != nil {
		t.Fatalf("installed plugin.json invalid: %v", err)
	}
	if installedManifest.Kind != hostbridge.KindHost {
		t.Fatalf("kind=%q want host", installedManifest.Kind)
	}
	if _, err := os.Lstat(filepath.Join(s.hostPluginDir(), "my-helper", hostbridge.DraftManifestFile)); !os.IsNotExist(err) {
		t.Fatalf("expected extension.json removed after translation")
	}
	// Submission should have been cleaned up after installation.
	if _, err := os.Lstat(filepath.Join(store, "my-helper")); !os.IsNotExist(err) {
		t.Fatalf("expected installed submission discarded")
	}

	// Grant "always allow" to my-helper, then replace via a new draft with the EXACT SAME permissions.
	if err := s.setHostPluginAlways("my-helper", "", true); err != nil {
		t.Fatal(err)
	}
	if !s.hostGrantStore().allowed(hostGrantPlugin+"my-helper", []string{}) {
		t.Fatal("expected always-allow grant recorded")
	}
	writeTestSubmission(t, store, "my-helper", "local", []string{}, "print('v3-changed')\n")

	// Replacing without replace=true must fail.
	rec = httptest.NewRecorder()
	s.installHostPluginDraft(rec, trustedPluginRequest(s, http.MethodPost, "/api/host-tools/drafts/install", `{"id":"my-helper","kind":"host"}`))
	if rec.Code == http.StatusOK {
		t.Fatal("expected overwrite without replace=true to fail")
	}

	// Replacing with replace=true succeeds, sets old copy aside, AND revokes the always-allow grant!
	rec = httptest.NewRecorder()
	s.installHostPluginDraft(rec, trustedPluginRequest(s, http.MethodPost, "/api/host-tools/drafts/install", `{"id":"my-helper","kind":"host","replace":true}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("replace install failed: %d %s", rec.Code, rec.Body.String())
	}
	if s.hostGrantStore().allowed(hostGrantPlugin+"my-helper", []string{}) {
		t.Fatal("SECURITY VIOLATION: always-allow grant survived draft overwrite")
	}
	if aside, _ := filepath.Glob(filepath.Join(s.profile, "plugins-removed", "my-helper-*", hostbridge.ManifestFile)); len(aside) != 1 {
		t.Fatalf("expected replaced plugin kept in plugins-removed: %v", aside)
	}

	// Built-in commands plugin can never be overwritten by a draft.
	writeTestSubmission(t, store, "commands", "workspace", []string{}, "print('evil')\n")
	rec = httptest.NewRecorder()
	s.installHostPluginDraft(rec, trustedPluginRequest(s, http.MethodPost, "/api/host-tools/drafts/install", `{"id":"commands","replace":true,"confirm_overwrite":true}`))
	if rec.Code == http.StatusOK {
		t.Fatal("expected built-in commands plugin overwrite to be rejected")
	}

	// Discard works.
	rec = httptest.NewRecorder()
	s.discardHostPluginDraft(rec, trustedPluginRequest(s, http.MethodPost, "/api/host-tools/drafts/discard", `{"id":"commands"}`))
	if rec.Code != http.StatusOK {
		t.Fatalf("discard failed: %d %s", rec.Code, rec.Body.String())
	}
	if _, err := os.Lstat(filepath.Join(store, "commands")); !os.IsNotExist(err) {
		t.Fatalf("expected discarded draft removed")
	}
}
