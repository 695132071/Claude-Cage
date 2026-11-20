package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/sylvinus/agent-vm/internal/cli"
	"github.com/sylvinus/agent-vm/internal/workbench"
	"golang.org/x/term"
)

// followRunDirectory makes `cage run` run from the directory it was started
// in: the same place in a shared project, inside the environment. An explicit
// --workdir is kept as given. A directory that is not shared is shared only
// when the person at the terminal confirms; otherwise the command does not
// run, so it never starts somewhere else.
func followRunDirectory(configPath string, args []string, hostDir string, confirm workbench.RunDirectoryConfirm) ([]string, error) {
	workdir, _, _, command, err := cli.SplitRun(args[1:])
	if err != nil || workdir != "" || len(command) == 0 {
		// The run reports its own usage errors.
		return args, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	guest, err := runDirectory(ctx, configPath, hostDir, confirm)
	if err != nil {
		return nil, err
	}
	return append([]string{"run", "--workdir", guest}, args[1:]...), nil
}

// runDirectory is workbench.RunDirectory; a variable for tests.
var runDirectory = workbench.RunDirectory

// terminalConfirm asks on the terminal, when stdin and stderr are one; nil
// otherwise, so a script is never left waiting for an answer.
func terminalConfirm(stdin *os.File, stderr *os.File) workbench.RunDirectoryConfirm {
	if !term.IsTerminal(int(stdin.Fd())) || !term.IsTerminal(int(stderr.Fd())) {
		return nil
	}
	return func(folder string) (bool, error) { return askShare(stdin, stderr, folder) }
}

func askShare(in io.Reader, out io.Writer, folder string) (bool, error) {
	fmt.Fprintf(out, "当前目录尚未共享给 Cage：\n  %s\n共享后，环境中的程序可以读取和修改这个文件夹，包括其中的隐藏文件和 Git 仓库。\n是否共享并在其中运行？[y/N] ", folder)
	// Read one byte at a time: what follows the answer is the command's input.
	var line []byte
	buf := make([]byte, 1)
	for reads := 0; len(line) < 64 && reads < 256; reads++ {
		n, err := in.Read(buf)
		if n == 1 {
			if buf[0] == '\n' {
				break
			}
			line = append(line, buf[0])
			continue
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return false, err
		}
	}
	switch strings.ToLower(strings.TrimSpace(string(line))) {
	case "y", "yes", "是":
		return true, nil
	}
	return false, nil
}
