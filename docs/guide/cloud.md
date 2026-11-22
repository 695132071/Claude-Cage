---
title: 云端部署与 SSH
description: 将工作环境放到自己的 Linux VPS，通过桌面 Cage 部署、连接和管理服务器上的任务。
group: 远程使用
order: 8
keywords: 云端 远程部署 VPS SSH 无KVM rootless 服务器 主机 指纹 远程项目
---
## 什么时候选择云端

希望任务在服务器上持续运行，或本机不适合运行虚拟机时，可以使用云端模式。终端、浏览器、编辑器、Claude Desktop 和网络出口位于服务器，电脑显示同一套工作台。本机虚拟机可以保持关闭。

服务器模式通过 rootless 容器运行，不需要 KVM。它仍需要满足系统、权限和资源条件，并非所有 VPS 都可以直接部署。

本机模式在电脑上运行 Debian 虚拟机，云端模式在服务器的容器里运行同一套 Linux 环境；需要服务器常驻任务时选择云端。两种方式沿用同一套工作台。本机环境见[Debian 环境](guest-systems.md)。

## 准备服务器

当前目标为 Linux x86_64，具备 systemd 用户服务、cgroup v2、rootless 用户命名空间、至少 65,536 个 subordinate UID/GID，以及 `/dev/fuse`、`/dev/net/tun`。普通 SSH 用户需要相应权限；还需 OpenSSH、curl、tar、sha256sum、iproute2 与 iptables 等基础工具。

完整前提和维护命令见[部署说明](https://github.com/Lee-xeo/Claude-Cage/blob/main/deploy/cloud/README.md)。权限缺失应修复服务器配置，不要改用特权容器或把 Docker socket 暴露给工作环境。

需要在退出 SSH 后持续运行或在服务器重启后恢复服务时，由服务器管理员配置 systemd 用户服务、linger 与相应启动策略。

<figure class="guide-shot">
  <a href="../../www/public/docs/screenshots/ssh-sidebar.jpg" target="_blank" rel="noopener" aria-label="查看原图：SSH 主机侧栏：添加自己的服务器"><img src="../../www/public/docs/screenshots/ssh-sidebar.jpg" alt="SSH 主机侧栏：添加自己的服务器" width="2560" height="1764" loading="lazy" decoding="async" /></a>
  <figcaption>SSH 主机侧栏：添加自己的服务器。截图版本：0.1.6-alpha-preview（开发构建） · macOS 原生应用 · 2026-11-07。<a href="../../www/public/docs/screenshots/ssh-sidebar.jpg" target="_blank" rel="noopener">查看原图（2560 × 1764） ↗</a></figcaption>
</figure>

## 从侧栏部署并连接

1. 打开 Cage，进入左栏“SSH 主机”，点击加号填写名称、地址、端口、用户名和本机密钥文件或 SSH agent。自定义路径可在“云端安装路径”设置。使用 SSH agent 时见下文[指定 SSH agent](#指定-ssh-agent)。
2. 点击“连接此主机”，将 SSH 主机指纹与服务器独立显示的记录核对。指纹变化时先核查原因，再继续。
3. 首次使用空配置目录时，点击“部署并连接”。Cage 传输匹配的程序和脚本，在服务器准备独立引擎与持久环境。
4. 已部署且就绪时直接进入；能确认归属的用户服务已停止时会尝试启动。安装中断后可用同一版本重试。
5. 云端工作台打开后，创建或解锁服务器自己的 Cage 账户，再配置服务器的代理和项目。

本机有任务时，连接卡片提供保留或停止本机环境的选项，停止前先保存内容。不要在未经备份的已有本机数据目录上首次部署云端实例。

### 指定 SSH agent

登录方式选“本机 SSH agent”时，“SSH agent 套接字”留空即沿用原有行为：macOS 和 Linux 使用启动 Cage 时的 `SSH_AUTH_SOCK`；Windows 先用 `SSH_AUTH_SOCK`，未设置时使用 Windows OpenSSH agent（`\\.\pipe\openssh-ssh-agent`）。

密码管理器等程序自带 agent、而 `SSH_AUTH_SOCK` 指向另一个没有密钥的 agent 时（例如 macOS 默认的 launchd agent），在这一栏填写该程序给出的 agent 位置，与 SSH 配置中 `IdentityAgent` 的值相同，但要写成完整路径：

- macOS 和 Linux：填写 Unix 套接字的绝对路径，不展开 `~` 和环境变量，长度不超过系统上限（macOS 103 字节，Linux 107 字节）。连接前核对它确实是套接字、属于当前用户，且组和其他用户没有写权限；可以是指向这样的套接字的符号链接。权限不符时按提示改为仅自己可读写（如 `chmod 600`）。
- Windows：只接受本机命名管道，格式为 `\\.\pipe\名称`，不接受网络路径和其他文件路径。

Cage 在连接时使用已保存的 agent 设置。需要读取本机 SSH 配置时，从下文的导入入口操作。连接失败时会区分 agent 无法连接、无法读取密钥列表和没有可用密钥。私钥始终留在 agent 内。

### 导入本机 SSH 配置

在 macOS、Linux 或 Windows 的“SSH 主机”侧栏点击“导入本机 SSH 配置”。Cage 从当前用户的配置读取主机（macOS 和 Linux 使用 `~/.ssh/config`，Windows 使用 `%USERPROFILE%\.ssh\config`）。核对名称、地址、用户名与端口后点击“导入所选主机”。导入只追加本机连接记录。同名（不区分大小写）或相同地址、端口、用户名的已有连接会跳过，保留其认证设置与已核对的指纹。超出 100 台主机上限或保存失败时整批不导入。

导入的登录方式固定为“本机 SSH agent”。请先用 `ssh-add` 将密钥加入本机 agent，再用 `ssh-add -l` 检查。Cage 忽略 `IdentityFile`，不会打开该文件、保存私钥路径、复制原始配置或私钥，也不会向客机或远端转发 agent。

未指定 `IdentityAgent` 或填写 `IdentityAgent SSH_AUTH_SOCK` 时，使用[上述本机 agent 默认设置](#指定-ssh-agent)。显式指定时，macOS 和 Linux 使用 Unix 套接字绝对路径，Windows 使用本机命名管道。导入不展开 `~`、环境变量或路径中的 OpenSSH 标记。agent 不可用或没有密钥时，连接会报错。

支持 UTF-8（含 BOM）、LF/CRLF、双引号、`key=value`、多个别名、重复 `Host` 段、`*`/`?` 通配和 `!` 排除。除行首注释外，导入也接受不在引号内的行内 `#` 注释。按“第一个匹配值生效”的规则提取 `HostName`、`User`、`Port` 和 `IdentityAgent`。`HostName` 支持 `%h`。只导入明确列出的别名。默认端口为 22，未指定 `User` 时使用当前用户（Windows 取 `USERNAME`）。导入默认为 Linux 远端。Windows 远端可在导入后编辑“远程系统”。

以下限制会明确报告，现有手动添加方式保持可用：

- 含 `Include` 或 `Match` 的文件整体拒绝导入，避免漏掉条件配置后连接到错误目标。不会执行 `Match exec`、`ProxyCommand`、`LocalCommand`，也不调用 `ssh -G` 或外部解析程序。
- 依赖代理或跳板机、主机名规范化、指定 IPv4/IPv6 地址族、`IdentitiesOnly yes`、`IdentityAgent none`、证书或其他不兼容的认证策略时跳过该主机。`PubkeyAuthentication` 允许 `yes`，`PreferredAuthentications` 必须包含 `publickey`。遇到未知选项时，跳过原因只显示指令名，不显示其值。
- 忽略转发、保活、`ConnectTimeout`、连接复用（`ControlMaster`/`ControlPath`/`ControlPersist`）和 `UpdateHostKeys` 等选项。`IgnoreUnknown` 不会扩大可导入的选项范围。主机身份由 Cage 的首次指纹核对与保存的指纹管理，不继承 OpenSSH 的 `known_hosts` 或 `CheckHostIP` 设置。
- 只读取用户配置中的普通文件，Windows 还要求文件位于本机磁盘。配置最多 1 MiB，每行小于 64 KiB，最多 100 个明确别名。预览后如果文件变化，需要重新读取并核对。

例如，下面的 `dev` 可以导入。连接前请用 `ssh-add` 把对应密钥加入本机 agent。

```sshconfig
Host dev
    HostName dev.example.com
    User developer
    Port 2222
```

配置语义参考 [OpenSSH ssh_config](https://man.openbsd.org/ssh_config)；Windows agent 的启用与密钥加载见 [Microsoft OpenSSH 密钥管理](https://learn.microsoft.com/windows-server/administration/openssh/openssh_keymanagement)。导入是一次性读取，之后修改原始 SSH 配置不会自动更新已保存连接。

<figure class="guide-shot">
  <a href="../../www/public/docs/screenshots/ssh-host-form.jpg" target="_blank" rel="noopener" aria-label="查看原图：远程主机地址、认证与私钥路径表单"><img src="../../www/public/docs/screenshots/ssh-host-form.jpg" alt="远程主机地址、认证与私钥路径表单" width="2560" height="1764" loading="lazy" decoding="async" /></a>
  <figcaption>远程主机地址、认证与私钥路径表单。截图版本：0.1.6-alpha-preview（开发构建） · macOS 原生应用 · 2026-11-07。<a href="../../www/public/docs/screenshots/ssh-host-form.jpg" target="_blank" rel="noopener">查看原图（2560 × 1764） ↗</a></figcaption>
</figure>

## 服务器项目与本机共享

远端工作台可选择服务器目录。云端账户、浏览资料、保险箱、节点与系统语言属于该服务器，本机已有配置不会自动复制过去。操作前看清当前主机名称。

需要使用本机源码时，按界面授权一个具体项目，选择所需的只读或读写方式。共享仅在授权连接期间有效。Git 目录与历史随项目共享：只读共享允许查看，读写共享允许暂存、提交和切换分支。授权目录内的隐藏文件、凭据和工具配置也会共享，目录外访问仍被拒绝。读写共享会直接修改本机源文件与仓库，先备份并审查改动。

上传由本机选择器读取你选定的文件，下载由服务器提供文件内容，再明确保存到本机。远端文件与本地副本的保存位置分别显示。

## 云端插件、自动同步与本机插件反向共享

- **自动增量同步**：连上并解锁云端主机后，桌面端会自动把本机兼容的环境内插件（`guest`）与无头主机插件（`host`）同步到云端（单向增量，不删除云端已有插件，不同步授权与密钥；需要图形桌面的屏幕/键鼠插件会自动跳过）。
- **在远程宿主机受控执行命令**：开启云端插件开关后，云端容器内的 Claude 可通过内置 `commands` 插件在云端已打开的服务器项目目录中运行命令（不触碰客户端反向挂载的本机目录）；需要确认时，原生弹窗会回流到你面前的桌面端并明确标出当前远程主机名。
- **本机桌面插件反向共享**：若希望云端 Claude 临时使用你面前这台电脑上的插件（如 Blender、桌面控制），可在云端插件设置中按插件开启「共享本机插件给当前远程」。工具调用经加密 SSH 隧道回到本机审批并执行，断开连接自动收回。详情见[插件与插件市场](plugins.md#远程主机中的插件)。

## 断开、锁定与停止

关闭本地窗口或断开 SSH 会释放客户端连接，服务器环境和任务继续运行。重新连接读取仍存活的会话。需要结束远端任务时，在对应云端工作台明确“停止环境”。

切换主机后，旧连接的文件选择、输入和请求不能继续进入新主机。连接失败时不会自动改为操作本机。对发送、保存等结果不明确的操作，重连后先核对状态再重试。

<figure class="guide-shot">
  <a href="../../www/public/docs/screenshots/ssh-install-paths.jpg" target="_blank" rel="noopener" aria-label="查看原图：自定义服务器上的程序与配置路径"><img src="../../www/public/docs/screenshots/ssh-install-paths.jpg" alt="自定义服务器上的程序与配置路径" width="2560" height="1764" loading="lazy" decoding="async" /></a>
  <figcaption>自定义服务器上的程序与配置路径。截图版本：0.1.6-alpha-preview（开发构建） · macOS 原生应用 · 2026-11-07。<a href="../../www/public/docs/screenshots/ssh-install-paths.jpg" target="_blank" rel="noopener">查看原图（2560 × 1764） ↗</a></figcaption>
</figure>

## 更新与故障恢复

客户端更新只更新本机程序，服务器维护另行执行。更新服务器前保存任务、停止相关环境与服务，完整保留配置、项目、容器文件系统及引擎数据，用原绝对路径恢复。

不要删除容器或执行清理全部引擎数据的命令来升级；容器可写文件系统也保存持久资料。出现连接、指纹或部署前提错误时，查[SSH 与云端故障](troubleshooting.md#ssh-或云端部署失败)。
