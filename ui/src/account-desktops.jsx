import React, { useRef, useState, useEffect, useCallback } from "react";
import {
  ChevronDown,
  ChevronRight,
  Monitor,
  Columns2,
  Pencil,
  Power,
  Plus,
  Trash2,
} from "lucide-react";
import RFB from "../.generated/novnc/core/rfb.js";
import { InlineSelect } from "./inline-select";
import { api } from "./api";
import { AccountIconNumber } from "./agent-icon";
import { runtimeSocketURL } from "./client-context.mjs";
import { translate, useTranslation } from "./interface-language.mjs";

const title = (a) => a.number ? `#${String(a.number).padStart(2, "0")} ${a.label}` : a.label;
const entry = (id) => "/desktop-instances/" + id;

function DesktopAccountLabel({ account }) {
  return <>
    <Monitor size={20} strokeWidth={1.75} aria-hidden="true">
      {account.number > 0 && <AccountIconNumber number={account.number} y={10} />}
    </Monitor>
    <span className="account-desktop-label">{account.label}</span>
  </>;
}

function DesktopRowAction({ label, icon: Icon, disabled, className = "", onClick }) {
  return (
    <button
      type="button"
      className={`icon-button ${className}`.trim()}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
    >
      <span className="terminal-action-label" aria-hidden="true">{label}</span>
      <Icon size={14} />
    </button>
  );
}

export function useAccountDesktops(onShow, onHide, notify, confirm, visible, revision) {
  const [expanded, setExpanded] = useState(false);
  const [catalog, setCatalog] = useState(null);
  const [panes, setPanes] = useState([]);
  const [pending, setPending] = useState("");
  const lock = useRef(false);
  const generation = useRef(0);
  const panesRef = useRef(panes);
  panesRef.current = panes;
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const refresh = useCallback(async (background = false) => {
    const g = ++generation.current;
    try {
      const next = await api("/desktop-instances");
      if (g !== generation.current) return;
      setCatalog(next);
      setPanes(old => {
        if (!next.instances || !old.length) return old;
        if (old.some(p => next.redirects?.[p.id] === "default")) return [];
        const seen = new Set();
        const updated = [];
        for (const pane of old) {
          const id = next.redirects?.[pane.id] || pane.id;
          const item = next.instances.find(a => a.id === id);
          if (!item || seen.has(id) || id === "default") continue;
          seen.add(id);
          updated.push(item);
        }
        return updated;
      });
      return next;
    } catch (e) {
      if (g === generation.current && !(background && e.status === 409)) notifyRef.current(e.message);
    }
  }, []);
  // Guest login and window lifecycle events advance the revision in the
  // normal workbench status response. Idle panes need no extra SSH polling.
  useEffect(() => {
    if (expanded || visible) void refresh(true);
  }, [expanded, visible, revision, refresh]);
  const focus = id => requestAnimationFrame(() =>
    document.getElementById("desktop-pane-" + id)?.querySelector("canvas")?.focus());
  async function act(account, action, beside = false, create = false, anchor = null) {
    if (lock.current) return;
    lock.current = true;
    try {
      if (
        action === "stop" &&
        !(await confirm({
          title: translate("关闭 {0}？", title(account)),
          body: translate("请先保存这个 Desktop 中的工作。"),
          confirm: translate("关闭实例"),
          anchor,
        }))
      )
        return;
      setPending(account.id || "create");
      if (create) {
        account = await api("/desktop-instances", {account_id: account.id});
        setPending(account.id);
      }
      if (action === "start" && account.id === "default") {
        setPanes([]); onShow(); return true;
      }
      if (action === "start" && panesRef.current.some(p => p.id === account.id) && account.running) {
        onShow(); focus(account.id); return true;
      }
      let job = await api(entry(account.id) + "/" + action, {});
      while (job.running) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        job = await api("/agent/jobs/" + job.id);
      }
      if (job.error || job.canceled) throw new Error(job.error || "操作已取消");
      if (action === "start") {
        setPanes((old) => {
          if (old.some((pane) => pane.id === account.id)) return old;
          if (!beside || !old.length) return [account];
          return [old[0], account];
        });
        onShow();
      } else {
        const closingOnly = panesRef.current.length === 1 && panesRef.current[0].id === account.id;
        setPanes((old) => old.filter((a) => a.id !== account.id));
        if (closingOnly) onHide();
      }
      await refresh();
      if (action === "start") focus(account.id);
      return true;
    } catch (e) {
      notify(e.message);
    } finally {
      lock.current = false;
      setPending("");
    }
  }
  async function rename(account, label) {
    const updated = await api(entry(account.id) + "/metadata", { label });
    setPanes((old) => old.map((pane) => pane.id === account.id ? { ...pane, label: updated.label } : pane));
    await refresh();
    return updated;
  }
  async function remove(account, anchor = null) {
    if (lock.current) return false;
    const confirmed = await confirm({
      title: translate("删除 {0}？", title(account)),
      body: account.running
        ? translate("删除会先关闭此 Desktop 实例，并清除该实例在环境内的本地应用数据与登录状态。不会删除 Claude 账号或 Claude Code 会话历史。")
        : translate("删除会移除此 Desktop 实例，并清除该实例在环境内的本地应用数据与登录状态。不会删除 Claude 账号或 Claude Code 会话历史。"),
      confirm: translate("删除实例"),
      pendingLabel: translate("正在删除…"),
      danger: true,
      anchor,
      submit: async () => {
        if (lock.current) throw new Error(translate("另一个操作仍在进行，请稍候"));
        lock.current = true;
        setPending(account.id);
        try {
          await api(entry(account.id), undefined, "DELETE");
          const closingOnly = panesRef.current.length === 1 && panesRef.current[0].id === account.id;
          setPanes((old) => old.filter((a) => a.id !== account.id));
          if (closingOnly) onHide();
          await refresh();
        } finally {
          lock.current = false;
          setPending("");
        }
      },
    });
    return Boolean(confirmed);
  }
  return {
    expanded,
    catalog,
    panes,
    pending,
    act,
    rename,
    remove,
    refresh,
    clear: () => setPanes([]),
    toggle: () => {
      setExpanded(!expanded);
    },
  };
}

export function DesktopList({ model, disabled, selected, onDefault, onRename }) {
  const tr = useTranslation();
  const { catalog, expanded, panes, pending } = model;
  const [creating, setCreating] = useState(false);
  const [accountID, setAccountID] = useState("");
  const instances = catalog?.instances ?? catalog?.accounts ?? [];
  const original = instances.find(a => a.id === "default");
  const defaultEntry = original || { id: "default", label: tr("默认 Desktop") };
  const defaultSelected = selected && panes.length === 0;
  const focusRowActions = (event) => {
    event.preventDefault();
    event.currentTarget.focus();
    event.currentTarget.parentElement?.querySelector(".terminal-row-actions button:not(:disabled)")?.focus();
  };
  const choose = async () => {
    if (await model.act({id:accountID}, "start", false, true)) setCreating(false);
  };
  return (
    <div className="account-desktop-nav">
      <button
        className={"sidebar-link " + (selected ? "selected" : "")}
        aria-expanded={expanded}
        onClick={model.toggle}
      >
        <Monitor size={17} />
        Claude Desktop{" "}
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
      {expanded && (
        <div className="account-desktop-list">
          {!catalog?.redirects?.default && (
            <div className={`account-desktop-row ${defaultSelected ? "active" : ""}`}>
              <button
                className="sidebar-link"
                title={title(defaultEntry)}
                aria-pressed={defaultSelected}
                disabled={disabled}
                onClick={onDefault}
                onContextMenu={focusRowActions}
                onKeyDown={(event) => {
                  if (event.key === "F10" && event.shiftKey) focusRowActions(event);
                }}
              >
                <DesktopAccountLabel account={defaultEntry} />
                {defaultEntry.running ? <span className="account-desktop-status" aria-hidden="true">●</span> : null}
              </button>
              {catalog?.supported && onRename && (
                <div className="terminal-row-actions" role="group" aria-label={tr("{0} 的操作", title(defaultEntry))}>
                  <DesktopRowAction
                    label={tr("重命名")}
                    icon={Pencil}
                    disabled={Boolean(pending) || disabled}
                    onClick={() => onRename(defaultEntry)}
                  />
                </div>
              )}
            </div>
          )}
          {catalog?.supported &&
            instances.filter(a => a.id !== "default").map((a) => {
              const active = selected && panes.some((p) => p.id === a.id);
              return (
                <div className={`account-desktop-row ${active ? "active" : ""}`} key={a.id}>
                  <button
                    title={title(a) + (a.running ? " " + tr("· 运行中") : " " + tr("· 未启动"))}
                    aria-label={title(a) + (pending === a.id ? " " + tr("· 处理中") : a.running ? " " + tr("· 运行中") : " " + tr("· 未启动"))}
                    aria-pressed={active}
                    className="sidebar-link"
                    disabled={Boolean(pending) || disabled}
                    onClick={() => model.act(a, "start")}
                    onContextMenu={focusRowActions}
                    onKeyDown={(event) => {
                      if (event.key === "F10" && event.shiftKey) focusRowActions(event);
                    }}
                  >
                    <DesktopAccountLabel account={a} />
                    {pending === a.id || a.running ? <span className="account-desktop-status" aria-hidden="true">{pending === a.id ? "…" : "●"}</span> : null}
                  </button>
                  <div className="terminal-row-actions" role="group" aria-label={tr("{0} 的操作", title(a))}>
                    {panes.length > 0 && !panes.some((p) => p.id === a.id) && (
                      <DesktopRowAction
                        label={tr("并排打开")}
                        icon={Columns2}
                        disabled={Boolean(pending) || disabled}
                        onClick={() => model.act(a, "start", true)}
                      />
                    )}
                    {a.running && (
                      <DesktopRowAction
                        label={tr("关闭")}
                        icon={Power}
                        disabled={Boolean(pending) || disabled}
                        onClick={(event) => model.act(a, "stop", false, false, event.currentTarget)}
                      />
                    )}
                    {onRename && (
                      <DesktopRowAction
                        label={tr("重命名")}
                        icon={Pencil}
                        disabled={Boolean(pending) || disabled}
                        onClick={() => onRename(a)}
                      />
                    )}
                    <DesktopRowAction
                      label={tr("删除")}
                      icon={Trash2}
                      className="desktop-delete-action"
                      disabled={Boolean(pending) || disabled}
                      onClick={(event) => model.remove(a, event.currentTarget)}
                    />
                  </div>
                </div>
              );
            })}
          {catalog?.supported && Array.isArray(catalog.instances) && <div className="desktop-create">
            <button type="button" className="sidebar-link" disabled={disabled || Boolean(pending)}
              aria-expanded={creating} onClick={() => setCreating(!creating)}><Plus size={16}/>{tr("新建 Desktop")}</button>
            {creating && <div className="desktop-create-fields">
              <label>{tr("所属账号")}<InlineSelect inline aria-label={tr("Desktop 所属账号")} value={accountID} onValueChange={setAccountID}>
                <option value="">{tr("在此实例中登录")}</option>
                {catalog.accounts.map(a => <option key={a.id} value={a.id}>{title(a)}</option>)}
              </InlineSelect></label>
              <button className="primary" disabled={disabled || Boolean(pending)} onClick={choose}>{tr("打开 Desktop")}</button>
            </div>}
          </div>}
        </div>
      )}
    </div>
  );
}

function DesktopPane({ account, visible, close, busy }) {
  const tr = useTranslation();
  const box = useRef(null);
  const [message, setMessage] = useState("");
  const [attempt, retry] = useState(0);
  useEffect(() => {
    if (!visible) return;
    let live = true,
      client;
    setMessage("正在连接…");
    const path = entry(account.id);
    api(path + "/vnc-credentials")
      .then(({ password }) => {
        if (!live) return;
        client = new RFB(box.current, runtimeSocketURL(path + "/vnc"), {
          credentials: { password },
        });
        client.scaleViewport = true;
        client.resizeSession = true;
        client.focusOnClick = true;
        client.addEventListener("connect", () => {
          if (live) setMessage("");
        });
        client.addEventListener("disconnect", () => {
          if (live) setMessage("画面已断开");
        });
      })
      .catch((e) => {
        if (live) setMessage(e.message);
      });
    return () => {
      live = false;
      client?.disconnect();
    };
  }, [account.id, account.generation, visible, attempt]);
  return (
    <section className="account-desktop-pane" id={"desktop-pane-"+account.id} aria-label={title(account)}>
      <header>
        <strong className="account-desktop-title" title={title(account)}>
          <DesktopAccountLabel account={account} />
        </strong>
        <button disabled={busy} onClick={close}>
          {tr("关闭实例")}
        </button>
      </header>
      <div className="account-desktop-screen" ref={box} />
      {message && (
        <div className="account-desktop-message" role="status">
          {message}{" "}
          {message !== "正在连接…" && (
            <button onClick={() => retry(attempt + 1)}>{tr("重新连接")}</button>
          )}
        </div>
      )}
    </section>
  );
}

export function DesktopPanes({ model, visible }) {
  return (
    <div className="account-desktop-panes">
      {model.panes.map((a) => (
        <DesktopPane
          key={a.id}
          account={a}
          visible={visible}
          busy={Boolean(model.pending)}
          close={() => model.act(a, "stop")}
        />
      ))}
    </div>
  );
}
