package main

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/sylvinus/agent-vm/internal/workbench"
)

func TestRunFollowsCallerDirectoryUnlessWorkdirGiven(t *testing.T) {
	original := runDirectory
	t.Cleanup(func() { runDirectory = original })
	calls := 0
	runDirectory = func(_ context.Context, configPath, hostDir string, confirm workbench.RunDirectoryConfirm) (string, error) {
		calls++
		if configPath != "/profile/config.json" || hostDir != "/host/demo/src" || confirm != nil {
			t.Fatalf("RunDirectory(%q, %q, confirm=%v)", configPath, hostDir, confirm != nil)
		}
		return "/host/demo/src", nil
	}
	got, err := followRunDirectory("/profile/config.json", []string{"run", "--tty", "claude"}, "/host/demo/src", nil)
	if err != nil || !slices.Equal(got, []string{"run", "--workdir", "/host/demo/src", "--tty", "claude"}) {
		t.Fatalf("followed run = %q, %v", got, err)
	}
	for _, args := range [][]string{
		{"run", "--workdir", "/srv/elsewhere", "pwd"},
		{"run", "--tty", "--workdir=/srv/elsewhere", "pwd"},
		{"run"},
		{"run", "--tty"},
	} {
		got, err := followRunDirectory("/profile/config.json", args, "/host/demo/src", nil)
		if err != nil || !slices.Equal(got, args) {
			t.Fatalf("%q became %q, %v", args, got, err)
		}
	}
	if calls != 1 {
		t.Fatalf("RunDirectory called %d times", calls)
	}
	runDirectory = func(context.Context, string, string, workbench.RunDirectoryConfirm) (string, error) {
		return "", errors.New("当前目录尚未共享给 Cage，命令没有运行")
	}
	if got, err := followRunDirectory("/profile/config.json", []string{"run", "pwd"}, "/host/other", nil); err == nil || got != nil {
		t.Fatalf("unshared run = %q, %v", got, err)
	}
}

func TestAskShareReadsOnlyTheAnswer(t *testing.T) {
	for answer, want := range map[string]bool{"y\nrest": true, "YES\n": true, "是\n": true, "\n": false, "n\n": false, "": false, "yes please\n": false} {
		in := strings.NewReader(answer)
		var out strings.Builder
		got, err := askShare(in, &out, "/host/demo")
		if err != nil || got != want || !strings.Contains(out.String(), "/host/demo") {
			t.Fatalf("answer %q = %v, %v", answer, got, err)
		}
		if answer == "y\nrest" {
			if rest := answer[len(answer)-in.Len():]; rest != "rest" {
				t.Fatalf("prompt consumed the command's input: left %q", rest)
			}
		}
	}
}
