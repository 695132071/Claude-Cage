package workbench

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/sylvinus/agent-vm/internal/cage"
	"github.com/sylvinus/agent-vm/internal/paths"
)

// RunDirectoryConfirm asks the person at the terminal whether to share
// folder. A nil RunDirectoryConfirm means nobody can be asked.
type RunDirectoryConfirm func(folder string) (bool, error)

// runShareClient is the workbench API that `cage run` uses: the same
// handlers, validation and saved grants as the project dialog.
type runShareClient interface {
	projects(ctx context.Context) (runProjects, error)
	open(ctx context.Context, workspace string) error
	save(ctx context.Context, folder string) error
}

type runProjects struct {
	Ready    bool           `json:"ready"`
	Projects []string       `json:"projects"`
	Groups   []projectGroup `json:"project_groups"`
}

// RunDirectory maps the caller's host directory to the directory in the
// environment where `cage run` runs, connecting the share it lies in. A
// directory outside every shared project is shared only after confirm says
// yes, through the same request as the project dialog; otherwise nothing
// runs. The environment's own directory needs no share.
func RunDirectory(ctx context.Context, configPath, hostDir string, confirm RunDirectoryConfirm) (string, error) {
	return runDirectory(ctx, filepath.Dir(configPath), hostDir, agentShareClient{configPath}, confirm)
}

func runDirectory(ctx context.Context, profile, hostDir string, client runShareClient, confirm RunDirectoryConfirm) (string, error) {
	dir, err := filepath.Abs(hostDir)
	if err == nil {
		dir, err = paths.Resolve(dir)
	}
	if err != nil {
		return "", fmt.Errorf("无法读取当前目录：%w", err)
	}
	if env, err := paths.Resolve(cage.EnvironmentDir(profile)); err == nil && cage.StorageContains(env, dir) {
		return guestPath(dir), nil
	}
	state, err := client.projects(ctx)
	if err != nil {
		return "", fmt.Errorf("无法确认当前目录是否已共享：%w\n请先打开 Cage 并等待环境就绪，或用 --workdir 指定环境中的目录。", err)
	}
	if !state.Ready {
		return "", errors.New("Cage 环境尚未就绪，命令没有运行。请在 Cage 中完成启动后重试。")
	}
	root, workspace := sharedRoot(state, dir)
	if root == "" {
		// Not shared: ask, or say how to share. Never fall back to another directory.
		instructions := "在 Cage 的“项目”中添加该文件夹，或在终端中直接运行 cage run 并确认共享；也可以用 --workdir 指定环境中的目录。"
		if confirm == nil {
			return "", fmt.Errorf("当前目录尚未共享给 Cage，命令没有运行：%s\n%s", dir, instructions)
		}
		yes, err := confirm(dir)
		if err != nil {
			return "", fmt.Errorf("未能确认是否共享当前目录，命令没有运行：%w", err)
		}
		if !yes {
			return "", fmt.Errorf("已取消共享，命令没有运行：%s", dir)
		}
		if err := client.save(ctx, dir); err != nil {
			return "", fmt.Errorf("共享当前目录失败，命令没有运行：%w", err)
		}
		return guestPath(dir), nil
	}
	if err := client.open(ctx, workspace); err != nil {
		return "", fmt.Errorf("无法连接当前目录所在的项目，命令没有运行：%w", err)
	}
	return guestPath(root) + guestRemainder(root, dir), nil
}

// sharedRoot is the deepest shared folder that holds dir, and the project
// that folder belongs to. Folders shared from a remote client live in the
// environment only and never contain a host directory.
func sharedRoot(state runProjects, dir string) (root, workspace string) {
	consider := func(folder, owner string) {
		if folder == "" || isClientMountPath(folder) || !filepath.IsAbs(folder) || !cage.StorageContains(folder, dir) {
			return
		}
		if root == "" || len(folder) > len(root) {
			root, workspace = folder, owner
		}
	}
	for _, folder := range state.Projects {
		consider(folder, folder)
	}
	for _, group := range state.Groups {
		for _, folder := range group.Folders {
			consider(folder, group.Workspace)
		}
	}
	return root, workspace
}

// guestRemainder is dir below root in the environment's spelling: the host's
// own case is kept even where the host ignores case.
func guestRemainder(root, dir string) string {
	root, dir = filepath.Clean(root), filepath.Clean(dir)
	rel := ""
	if strings.HasPrefix(dir, root) {
		rel = dir[len(root):]
	} else if r, err := filepath.Rel(paths.Fold(root), paths.Fold(dir)); err == nil && r != "." {
		rel = string(filepath.Separator) + r
	}
	rel = strings.TrimLeft(filepath.ToSlash(rel), "/")
	if rel == "" {
		return ""
	}
	return "/" + rel
}

type agentShareClient struct{ configPath string }

func (c agentShareClient) call(ctx context.Context, operation string, input any) (json.RawMessage, error) {
	var body json.RawMessage
	if input != nil {
		data, err := json.Marshal(input)
		if err != nil {
			return nil, err
		}
		body = data
	}
	result, err := AgentCall(ctx, c.configPath, operation, body)
	var agentErr *AgentError
	if errors.As(err, &agentErr) {
		return nil, errors.New(strings.TrimSpace(agentErr.Message))
	}
	return result, err
}

func (c agentShareClient) projects(ctx context.Context) (runProjects, error) {
	var state runProjects
	data, err := c.call(ctx, "status", nil)
	if err == nil {
		err = json.Unmarshal(data, &state)
	}
	return state, err
}

func (c agentShareClient) open(ctx context.Context, workspace string) error {
	_, err := c.call(ctx, "project.open", map[string]any{"workspace": workspace, "add": false})
	return err
}

func (c agentShareClient) save(ctx context.Context, folder string) error {
	name := []rune(filepath.Base(folder))
	if len(name) > 100 {
		name = name[:100]
	}
	_, err := c.call(ctx, "project.save", projectGroup{Name: string(name), Folders: []string{folder}})
	return err
}
