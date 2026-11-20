#!/usr/bin/python3
"""Launch the official guest CLI and export only its subscription status fields."""
import runpy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

_controller_context = {**os.environ, **globals().get("_controller_context", {})}
runtime_source = Path(__file__).resolve()
runtime_path = runtime_source.with_name("client_runtime.py")
if not runtime_path.exists():
    runtime_path = runtime_source.parent.parent / "client_runtime.py"
_runtime = runpy.run_path(runtime_path, init_globals={"_controller_context": _controller_context})
PRIVATE_MODE = _runtime.get("PRIVATE_MODE", 0o700)


if os.name == "nt":
    platform_path = Path(__file__).with_name("windows_cli_platform.py")
    if not platform_path.exists():
        platform_path = Path(__file__).parent.parent / "workbench/windows_cli_platform.py"
    _platform = runpy.run_path(platform_path, init_globals={"_controller_context": _controller_context})
    fcntl = _platform["FileLock"]
else:
    import fcntl

SHIM = "/usr/local/lib/cage-claude-bin"
OPEN = "/usr/local/libexec/cage-cli/bin/cage-open" if sys.platform == "darwin" else "/usr/local/bin/cage-open"
HELPER = "/usr/local/bin/cage-claude-account"
DEFAULT = "default"


def shared_root():
    return Path(_controller_context["CAGE_CLI_SHARED_ROOT"]) if os.name == "nt" else Path.home() / ".local/share/cage/claude-shared"


class SharedResourceConflict(ValueError):
    def __init__(self, message, local=None, shared=None):
        super().__init__(message)
        self.local, self.shared = local, shared


class SubscriptionStatusError(ValueError):
    pass


class SubscriptionStatusTimeout(SubscriptionStatusError):
    pass


def profile(account_id):
    if account_id != DEFAULT and not re.fullmatch("[0-9a-f]{24}", account_id):
        raise ValueError("invalid account ID")
    root = Path(_controller_context["CAGE_CLI_ACCOUNT_ROOT"]) if os.name == "nt" else Path.home() / ".local/share/cage/claude-accounts"
    return root / account_id


def config_directory(base):
    return Path.home() / ".claude" if base.name == DEFAULT else base / "config"


def cli_environment(base):
    env = os.environ.copy()
    # Executing the real binary must never recurse into the terminal's claude shim.
    path = [p for p in env.get("PATH", os.defpath).split(os.pathsep) if p and p != SHIM]
    env["PATH"] = os.pathsep.join([str(Path.home() / ".local/bin"), *path])
    if sys.platform == "darwin" and Path("/usr/local/libexec/cage-cli/bin/claude").is_file():
        env["PATH"] = "/usr/local/libexec/cage-cli/bin:/usr/local/bin:" + env["PATH"]
    if base.name == DEFAULT:
        # Let Claude resolve its normal ~/.claude.json and ~/.claude itself.
        # Preserve provider variables: an API-authenticated shell is not a
        # signed-in default subscription, even if a previous login exists.
        return _runtime["environment"](env)
    env["CLAUDE_CONFIG_DIR"] = str(config_directory(base))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                 "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                 "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_GATEWAY"):
        env.pop(name, None)
    return _runtime["environment"](env)


def identity(binary, env, cwd, settings=(), timeout=10, pass_fds=()):
    sink = subprocess.PIPE if _controller_context.get("CAGE_REMOTE_APP_CONTAINER") == "1" else subprocess.DEVNULL
    try:
        result = subprocess.run([_runtime["path"](binary), *settings, "auth", "status"], cwd=cwd, env=env, timeout=timeout,
                                stdin=sink, stdout=subprocess.PIPE, stderr=sink, pass_fds=pass_fds)
    except subprocess.TimeoutExpired as error:
        raise SubscriptionStatusTimeout("Subscription status timed out") from error
    # Exit code 1 with JSON is the CLI's documented logged-out response. Any
    # other failure (including missing JSON) must not sign a known user out.
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise SubscriptionStatusError("Invalid subscription status") from error
    if not isinstance(data, dict):
        raise SubscriptionStatusError("Invalid subscription status")
    reported_dir = data.get("configDirectory")
    correct_dir = isinstance(reported_dir, str) and Path(reported_dir).resolve() == Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").resolve()
    if result.returncode not in (0, 1) or not correct_dir:
        raise SubscriptionStatusError("Unexpected subscription status")
    ok = (result.returncode == 0 and data.get("loggedIn") is True and data.get("authMethod") == "claude.ai"
          and data.get("apiProvider") == "firstParty")
    if not ok:
        return {"logged_in": False}
    return {"logged_in": True, "email": data.get("email"), "organization": data.get("orgId") or data.get("orgName"),
            "plan": data.get("subscriptionType")}


def subject(summary):
    if not summary.get("logged_in") or not summary.get("email"):
        return None
    email = summary["email"].strip().lower()
    organization = (summary.get("organization") or "").strip()
    return hashlib.sha256(f"{email}\n{organization}".encode()).hexdigest()


def recover_login(binary, target, source_id, expected, *, archive=False):
    """Recover a missing file-backed login without moving an account's history."""
    if target.name == DEFAULT or (source_id == DEFAULT and not archive) or source_id == target.name:
        raise ValueError("recovery requires distinct private profiles")
    source = profile(source_id)
    env = cli_environment(target)
    # auth status itself initializes .claude.json in an empty profile. A new
    # archive needs to publish its verified identity before the CLI first runs.
    if not archive or any(config_directory(target).iterdir()):
        current = identity(binary, env, Path.home())
        if subject(current) == expected:
            return current
        if archive or subject(current) is not None:
            return {"logged_in": False}
    authenticated = identity(binary, cli_environment(source), Path.home())
    if not expected or subject(authenticated) != expected:
        return {"logged_in": False}
    credential = config_directory(source) / ".credentials.json"
    destination = config_directory(target) / ".credentials.json"
    paths = [target, config_directory(target), credential]
    if source_id != DEFAULT:
        paths += [source, config_directory(source)]
    elif config_directory(source).is_symlink() or os.name != "nt" and config_directory(source).stat().st_uid != os.getuid():
        raise ValueError("unexpected default login directory")
    for path in paths:
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
            raise ValueError("unexpected login file permissions")
    if not credential.is_file():
        raise ValueError("file-backed login required")
    if archive:
        # The controller allocated this fresh directory and publishes its ID
        # only after verification. O_EXCL protects concurrent file creation.
        if source.name == DEFAULT:
            source_directory = cli_environment(source).get("CLAUDE_CONFIG_DIR")
            if source_directory and Path(source_directory).resolve() != config_directory(source).resolve():
                raise ValueError("default subscription uses a different directory")
        metadata = (Path.home() if source.name == DEFAULT else config_directory(source)) / ".claude.json"
        if metadata.is_symlink():
            raise ValueError("unexpected login metadata")
        account = json_file(metadata).get("oauthAccount")
        if isinstance(account, dict):
            with (config_directory(target) / ".claude.json").open("x", encoding="utf-8") as output:
                json.dump({"oauthAccount": account}, output)
        with credential.open("rb") as stream, destination.open("xb") as output:
            shutil.copyfileobj(stream, output)
        recovered = identity(binary, env, Path.home())
        return recovered if subject(recovered) == expected else {"logged_in": False}
    # Publish opaque bytes only inside the guest. The hard link is atomic and
    # refuses to overwrite a credential created by a concurrent original login.
    with tempfile.NamedTemporaryFile(dir=destination.parent) as temporary:
        with credential.open("rb") as stream:
            shutil.copyfileobj(stream, temporary)
        temporary.flush()
        os.fsync(temporary.fileno())
        os.link(temporary.name, destination)
    # The CLI can refresh or replace this path independently of the controller.
    # Retain published credentials on failure: checking file identity before an
    # unlink cannot prevent deleting a concurrent login's replacement.
    recovered = identity(binary, env, Path.home())
    return recovered if subject(recovered) == expected else {"logged_in": False}


def json_file(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def valid_window(data, now):
    if not isinstance(data, dict):
        return None
    used, reset = data.get("used_percentage"), data.get("resets_at")
    if type(used) not in (int, float) or not 0 <= used <= 100:
        return None
    if type(reset) != int or (reset != 0 and reset <= now):
        return None
    return {"used_percentage": used, "resets_at": reset}


def read_quota(base):
    try:
        stored = json_file(base / "quota.json")
    except (ValueError, OSError):
        return None
    if not isinstance(stored, dict):
        return None
    digest = stored.get("identity", "")
    seen = stored.get("observed_at")
    now = int(time.time())
    if not isinstance(digest, str) or not re.fullmatch("[a-f0-9]{64}", digest) or type(seen) != int or not 0 < seen <= now:
        return None
    result = {}
    for key in ("five_hour", "seven_day"):
        window = valid_window(stored.get(key), now)
        if window:
            window_seen = stored[key].get("observed_at", seen)
            if type(window_seen) == int and 0 < window_seen <= now:
                result[key] = {**window, "observed_at": window_seen}
    if result:
        return {**result, "identity": digest, "observed_at": seen}
    return None


def save_usage(base, data, expected, confirm_subject, query_started=None, deadline=None):
    rate = data.get("rate_limits")
    if not isinstance(rate, dict):
        return
    now = int(time.time())
    with (base / "quota.lock").open("a") as lock:
        if deadline is None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        else:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("usage cache busy")
                    time.sleep(.05)
        path = base / "quota.json"
        previous = read_quota(base) or {}
        if previous.get("identity") != expected:
            previous = {}
        windows = {}
        changed = False
        for key in ("five_hour", "seven_day"):
            old = previous.get(key)
            new = valid_window(rate.get(key), now)
            # A redraw of an older terminal must not roll back the more recent
            # account window observed in another conversation.
            redraw = query_started is None and old and new and all(old[field] == new[field] for field in ("resets_at", "used_percentage"))
            older = old and new and (0 < new["resets_at"] < old["resets_at"] or
                    (query_started is None and old["resets_at"] == new["resets_at"] and new["used_percentage"] < old["used_percentage"]))
            if old and (not new or redraw or older or (query_started is not None and old["observed_at"] > query_started)):
                windows[key] = old
            elif new:
                windows[key] = {**new, "observed_at": now}
                changed = True
        # A user can run /login in an open Claude process. Ask the CLI for
        # its identity on changed readings only; never infer it from a path.
        if changed and expected and confirm_subject() == expected:
            windows["observed_at"] = now
            windows["identity"] = expected
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=base, delete=False) as tmp:
                json.dump(windows, tmp, separators=(",", ":"))
                tmp.flush()
                os.fsync(tmp.fileno())
                temporary = tmp.name
            try:
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise


def reported_window(percent, stamp, now):
    reset = 0
    if stamp is not None:
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            reset = int(dt.timestamp())
        except (AttributeError, ValueError, OverflowError):
            return None
    return valid_window({"used_percentage": percent, "resets_at": reset}, now)


def usage_windows(raw, now):
    # SDKControlGetUsageResponse is the structured /usage report. The input
    # contains one control request and no user prompt or generate request.
    report = None
    for line in raw.splitlines():
        message = json.loads(line)
        if message.get("type") in ("user", "assistant"):
            raise ValueError("unexpected conversation during usage query")
        if message.get("type") == "result" and (message.get("is_error") or message.get("num_turns") != 0):
            raise ValueError("unexpected inference result")
        if message.get("type") == "control_response":
            response = message.get("response") or {}
            if response.get("request_id") == "quota":
                if report is not None or response.get("subtype") != "success":
                    raise ValueError("failed usage response")
                report = response.get("response")
    if not isinstance(report, dict) or type(report.get("rate_limits_available")) is not bool:
        raise ValueError("missing structured usage response")
    session = report.get("session")
    if not isinstance(session, dict) or session.get("total_cost_usd") != 0 or session.get("model_usage"):
        raise ValueError("unexpected model use")
    if not report["rate_limits_available"]:
        return False, {}
    rate = report.get("rate_limits") or {}
    windows = {}
    for key in ("five_hour", "seven_day"):
        meter = rate.get(key)
        if isinstance(meter, dict):
            reading = reported_window(meter.get("utilization"), meter.get("resets_at"), now)
            if reading:
                windows[key] = reading
    # Some servers supply the unscoped windows in limits[] instead. A
    # model-specific quota never substitutes for an account's weekly window.
    candidates = {"session": "five_hour", "weekly_all": "seven_day"}
    rows = {}
    ambiguous = set()
    for row in rate.get("limits") or ():
        if not isinstance(row, dict):
            continue
        key = candidates.get(row.get("kind"))
        if not key or key in windows or key in ambiguous:
            continue
        reading = reported_window(row.get("percent"), row.get("resets_at"), now)
        if reading and key in rows and rows[key] != reading:
            ambiguous.add(key)
            del rows[key]
        elif reading:
            rows[key] = reading
    return True, {**rows, **windows}


def changed_identity():
    return {"logged_in": False, "usage_status": "identity_changed", "usage_checked_at": int(time.time())}


def invoke_usage(binary, env, cwd, options, timeout):
    command = [_runtime["path"](binary), *options, "-p", "--input-format", "stream-json", "--verbose", "--output-format", "stream-json",
               "--no-session-persistence", "--strict-mcp-config", "--tools", ""]
    request = {"type": "control_request", "request_id": "quota", "request": {"subtype": "get_usage", "skip_behaviors": True}}
    result = subprocess.run(command, cwd=cwd, env=env, timeout=timeout, input=(json.dumps(request) + "\n").encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode != 0 or len(result.stdout) > (2 << 20):
        raise ValueError("usage command failed")
    return usage_windows(result.stdout, int(time.time()))


def query_usage(binary, env, base, expected):
    if not expected or not re.fullmatch("[a-f0-9]{64}", expected):
        raise ValueError("missing expected account")
    deadline = time.monotonic() + 12
    started = int(time.time())
    status = "error"
    summary = {}
    options = ["--setting-sources", "user", "--settings", '{"disableAllHooks":true}']
    with tempfile.TemporaryDirectory(prefix="cage-claude-usage-") as cwd:
        try:
            summary = identity(binary, env, cwd, options, timeout=4)
            if subject(summary) != expected:
                return changed_identity()
            # Reserve time to recheck the actual identity after the command.
            timeout = min(8, deadline - time.monotonic() - 2)
            try:
                available, windows = invoke_usage(binary, env, cwd, options, max(.1, timeout))
            except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
                windows = None
            after = identity(binary, env, cwd, options, timeout=max(.1, min(4, deadline - time.monotonic())))
            if subject(after) != expected:
                return changed_identity()
            summary = after
            if windows:
                base.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
                save_usage(base, {"rate_limits": windows}, expected, lambda: expected, started, deadline)
            if windows is not None:
                status = {0: "unavailable", 1: "partial", 2: "reported"}[len(windows)] if available else "not_applicable"
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            # The CLI can print project/account data even on failure.
            # The host needs a reason category and only verified window data.
            pass
    if subject(summary) == expected:
        previous = read_quota(base)
        if previous and previous["identity"] == expected:
            summary["quota"] = previous
            if status == "unavailable":
                status = "cached"
    summary.update(usage_status=status, usage_checked_at=int(time.time()))
    return summary


def has_status_line(base):
    # Preserve existing user, project and managed status lines, including an
    # explicit disable. If a settings file is unreadable, defer to Claude.
    if os.name == "nt":
        policy = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ClaudeCode"
    elif sys.platform == "darwin":
        policy = Path("/Library/Application Support/ClaudeCode")
    else:
        policy = Path("/etc/claude-code")
    common = Path.home() / ".claude/settings.json"
    paths = [config_directory(base) / "settings.json", common, policy / "managed-settings.json"]
    if os.name != "nt":
        paths.extend((policy / "managed-settings.d").glob("*.json"))
    for folder in (Path.cwd(), *Path.cwd().parents):
        paths += [folder / ".claude/settings.json", folder / ".claude/settings.local.json"]
    for path in paths:
        try:
            settings = json_file(path)
            if not isinstance(settings, dict) or settings.get("allowManagedHooksOnly"):
                return True
            # Cage's status line runs the shared command itself (user_status_line),
            # so quota readings continue without hiding the user's own line.
            if "statusLine" in settings and not (path == common and user_status_line() is not None):
                return True
        except (OSError, ValueError):
            return True
    return False


def user_status_line():
    """The shared command status line, if any, that Cage's status line delegates to."""
    try:
        line = json_file(Path.home() / ".claude/settings.json").get("statusLine")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(line, dict) and line.get("type") == "command" and isinstance(line.get("command"), str) and line["command"].strip():
        return line
    return None


def status_line(command):
    setting = {"type": "command", "command": command}
    line = user_status_line()
    if line and type(line.get("padding")) is int:
        setting["padding"] = line["padding"]
    return setting


def settings_flags(args):
    flags = []
    iterator = iter(args)
    for arg in iterator:
        if arg == "--":
            break
        key, equals, _ = arg.partition("=")
        if key in ("--settings", "--setting-sources"):
            flags.append(arg)
            if not equals:
                value = next(iterator, None)
                if value is None:
                    raise ValueError("missing CLI setting")
                flags.append(value)
    return flags


# Only local behavior is shared. Never copy env, apiKeyHelper, login/org
# constraints, OAuth metadata, or account-synced remote configuration.
SHARED_SETTINGS = frozenset({"model", "permissions", "hooks", "statusLine", "attribution", "language",
                             "effortLevel", "alwaysThinkingEnabled", "includeCoAuthoredBy", "respectGitignore",
                             "spinnerTipsEnabled", "outputStyle", "disableAllHooks", "includeGitInstructions",
                             "theme", "skipDangerousModePermissionPrompt", "tui", "autoMemoryEnabled"})
# The terminal status line also records quota readings; a copied user status
# line would disable that for terminals, and Desktop does not draw one.
PROFILE_SETTINGS = SHARED_SETTINGS - {"statusLine"}


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_record(base):
    """Cage's private bookkeeping for one profile, outside its configuration."""
    try:
        record = json_file(base / "shared-settings.json")
    except (ValueError, OSError):
        record = {}
    return record if isinstance(record, dict) else {}


def dict_field(record, key):
    value = record.get(key)
    if not isinstance(value, dict):
        value = {}
        record[key] = value
    return value


def write_record(base, record):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=base, delete=False) as stream:
        json.dump(record, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    try:
        os.replace(temporary, base / "shared-settings.json")
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def json_snapshot(path):
    """Return the raw bytes and parsed JSON; None means leave the file alone."""
    if path.is_symlink() or os.name == "nt" and path.is_junction():
        return b"", None
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError:
        return b"", None
    try:
        data = json.loads(raw)
    except ValueError:
        # Leave a file the user is still editing to the CLI's own diagnostics.
        return raw, None
    return raw, data if isinstance(data, dict) else None


BACKUP_MAX_COUNT = 20
BACKUP_MAX_AGE = 30 * 24 * 60 * 60


def unique_backup_path(prefix):
    prefix.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    stamp = time.time_ns()
    while True:
        candidate = prefix.with_name(f"{prefix.name}-{stamp}")
        if not candidate.exists() and not linked(candidate):
            return candidate
        stamp += 1


def write_backup_bytes(prefix, data):
    prefix.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    stamp = time.time_ns()
    while True:
        candidate = prefix.with_name(f"{prefix.name}-{stamp}")
        try:
            with candidate.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            return candidate
        except FileExistsError:
            stamp += 1


def prune_backup_series(prefix, now=None, keep_count=BACKUP_MAX_COUNT, max_age=BACKUP_MAX_AGE):
    """Bound backup growth by count and age while always keeping the newest backup.

    Age is the creation time in each backup's name (nanoseconds). Copies keep
    their source's modification time, so that time says nothing about when the
    backup was made. A backup at the bare prefix comes from an older version
    that did not name the time: it sorts as the oldest and is removed only when
    the series exceeds its count, never by an age it cannot know.
    """
    backups_root = (shared_root() / "backups").resolve()
    parent = prefix.parent
    if not parent.is_dir():
        return
    try:
        if not parent.resolve().is_relative_to(backups_root):
            return
    except OSError:
        return
    head = prefix.name + "-"
    entries = []
    try:
        children = list(parent.iterdir())
    except OSError:
        return
    for child in children:
        if linked(child):
            continue
        if child.name == prefix.name:
            created = None
        elif child.name.startswith(head) and child.name[len(head):].isdigit() and len(child.name[len(head):]) >= 15:
            created = int(child.name[len(head):]) / 1e9
        else:
            continue
        entries.append((created is not None, created or 0, child))
    if len(entries) <= 1:
        return
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    cutoff = (time.time() if now is None else now) - max_age
    limit = max(1, keep_count)
    for index, (named, created, child) in enumerate(entries):
        # The newest backup of a series is kept regardless of its age.
        if index == 0 or (index < limit and (not named or created >= cutoff)):
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            pass


def json_bytes(data):
    """The exact bytes replace_json publishes, so an intent can name its outcome."""
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def bytes_digest(data):
    return hashlib.sha256(data).hexdigest()


def replace_json(path, data, original, backup):
    """Atomically publish JSON unless another writer changed the file since it was read."""
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != original:
        return False
    backup_prefix = backup
    created_backup = write_backup_bytes(backup_prefix, original) if original is not None else None
    path.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
        stream.write(json_bytes(data))
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    try:
        # The official CLI may save its own change meanwhile. Keep that one and
        # let the next start synchronize again rather than failing this launch.
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != original:
            Path(temporary).unlink(missing_ok=True)
            if created_backup is not None:
                created_backup.unlink(missing_ok=True)
            return False
        os.replace(temporary, path)
        prune_backup_series(backup_prefix)
        return True
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        if created_backup is not None:
            created_backup.unlink(missing_ok=True)
        raise


MISSING = object()


def three_way(account, base, common):
    """Merge per entry; a side's change wins over the other's unchanged value.

    When both sides changed one entry differently, each keeps its own value and
    the entry stays unresolved until one of them changes again.
    """
    account_result, common_result, base_result = dict(account), dict(common), {}
    for key in set(account) | set(base) | set(common):
        a, b, c = (mapping.get(key, MISSING) for mapping in (account, base, common))
        if a == c:
            value = a
        elif a == b:
            value = c
        elif c == b:
            value = a
        else:
            if b is not MISSING:
                base_result[key] = b
            continue
        for result in (account_result, common_result):
            if value is MISSING:
                result.pop(key, None)
            else:
                result[key] = value
        if value is not MISSING:
            base_result[key] = value
    return account_result, common_result, base_result


def resource_link(local, shared):
    local.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    if os.name == "nt":
        _platform["resource_link"](local, shared)
    else:
        local.symlink_to(shared, target_is_directory=shared.is_dir())


def linked(path):
    return path.is_symlink() or os.name == "nt" and path.is_junction()


def remove_link(path):
    if os.name == "nt" and path.is_junction():
        path.rmdir()
    else:
        path.unlink()


def same_path(first, second):
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def safe_resolve(path):
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


# Entries below a profile's plugins folder that hold account identity (synced
# from claude.ai, named by account and organization), per-account plugin data or
# per-account bookkeeping. They never become links; tests pin this list.
PRIVATE_PLUGIN_ENTRIES = frozenset({"synced", "store", "data", "installed_plugins.json", "installed_plugins_v2.json",
                                    "known_marketplaces.json", "plugin-directory-cache-v2.json",
                                    "install-counts-cache.json", ".last_inuse_sweep"})
# The CLI rebuilds marketplaces/<name> and adds cache/<marketplace>/<plugin>/<version>
# inside these folders, so linking the folders survives updates.
SHARED_PLUGIN_FOLDERS = (("marketplaces", 0), ("cache", 2))
PLUGIN_SETTINGS = ("enabledPlugins", "extraKnownMarketplaces")
PLUGIN_REGISTRIES = (("known_marketplaces.json", None), ("installed_plugins.json", "plugins"))
PRIVATE_PLUGIN_KEYS = frozenset({
    "headers", "authorization", "auth", "token", "accesstoken", "refreshtoken",
    "apikey", "secret", "password", "credentials",
    "claudeaipluginid", "marketplaceid", "organizationid", "orgid",
    "organizationuuid", "accountid", "accountuuid", "userid", "synced",
})
PRIVATE_PLUGIN_HOSTS = ("claude.ai", "anthropic.com")
PRIVATE_MARKETPLACE_SOURCES = frozenset({"claude.ai", "claudeai", "synced", "org", "organization", "enterprise", "managed"})


def has_private_plugin_marker(value):
    """True if a plugin/marketplace config carries credentials, URL userinfo, or claude.ai/org identity."""
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in PRIVATE_PLUGIN_KEYS or "claudeai" in lowered:
                return True
            if lowered == "source" and isinstance(item, str) and item.strip().lower() in PRIVATE_MARKETPLACE_SOURCES:
                return True
            if has_private_plugin_marker(item):
                return True
        return False
    if isinstance(value, list):
        return any(has_private_plugin_marker(item) for item in value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        parsed = urlsplit(text)
        if parsed.scheme in ("http", "https", "ssh", "git", "ftp", "ftps"):
            if parsed.username or parsed.password:
                return True
            host = (parsed.hostname or "").lower()
            if any(host == blocked or host.endswith("." + blocked) for blocked in PRIVATE_PLUGIN_HOSTS):
                return True
        lower = text.lower()
        if any(host in lower for host in ("claude.ai/", "claude.ai:", ".claude.ai", "anthropic.com/", ".anthropic.com")):
            return True
        if "@" in text and ":" in text.split("@", 1)[0] and "://" not in text:
            return True
    return False


def valid_plugin_name(name):
    return (isinstance(name, str) and bool(name) and name not in PRIVATE_PLUGIN_ENTRIES
            and "/" not in name and "\\" not in name and ".." not in name
            and "claude.ai" not in name.lower())


def valid_neutral_plugin_subpath(value, folder):
    """Verify a neutralized plugin path resolves strictly under <plugins>/<folder>/ and never private entries."""
    if not isinstance(value, str) or not value.startswith(NEUTRAL_PLUGINS):
        return False
    rest = value[len(NEUTRAL_PLUGINS):].replace("\\", "/")
    parts = [part for part in rest.split("/") if part != ""]
    if len(parts) < 2 or parts[0] != folder:
        return False
    if any(part in (".", "..") for part in parts):
        return False
    if parts[1] in PRIVATE_PLUGIN_ENTRIES:
        return False
    return True


def shareable_marketplace_entry(name, entry):
    if not valid_plugin_name(name) or not isinstance(entry, dict):
        return False
    if has_private_plugin_marker(entry):
        return False
    source = entry.get("source")
    if not isinstance(source, dict) or not source or has_private_plugin_marker(source):
        return False
    for key in ("path", "directory", "location"):
        loc = source.get(key)
        if isinstance(loc, str):
            segments = {part.lower() for part in loc.replace("\\", "/").split("/") if part}
            if segments & {"synced", "store", "data"}:
                return False
    location = entry.get("installLocation")
    if location is not None and not valid_neutral_plugin_subpath(location, "marketplaces"):
        return False
    return True


def shareable_extra_marketplace(name, entry):
    if not valid_plugin_name(name) or not isinstance(entry, dict):
        return False
    if has_private_plugin_marker(entry):
        return False
    source = entry.get("source")
    if source is not None and (not isinstance(source, dict) or not source or has_private_plugin_marker(source)):
        return False
    return True


def shareable_installed_plugin(plugin_id, records, private_marketplaces):
    if not isinstance(plugin_id, str) or "@" not in plugin_id or not isinstance(records, list) or not records:
        return False
    plugin_name, _, marketplace = plugin_id.rpartition("@")
    if not valid_plugin_name(plugin_name) or not valid_plugin_name(marketplace) or marketplace in private_marketplaces:
        return False
    for entry in records:
        if not isinstance(entry, dict) or has_private_plugin_marker(entry):
            return False
        scope = entry.get("scope")
        if isinstance(scope, str) and scope.lower() in PRIVATE_MARKETPLACE_SOURCES:
            return False
        if not valid_neutral_plugin_subpath(entry.get("installPath"), "cache"):
            return False
    return True


def shareable_enabled_plugin(plugin_id, private_marketplaces, private_plugins):
    if not isinstance(plugin_id, str) or plugin_id in private_plugins:
        return False
    if "@" in plugin_id:
        plugin_name, _, marketplace = plugin_id.rpartition("@")
        if not valid_plugin_name(plugin_name) or not valid_plugin_name(marketplace) or marketplace in private_marketplaces:
            return False
    elif not valid_plugin_name(plugin_id):
        return False
    return True


def without_last_updated(entry):
    if not isinstance(entry, dict):
        return entry
    return {key: value for key, value in entry.items() if key != "lastUpdated"}


def three_way_marketplaces(account, base, common):
    """Three-way merge for known_marketplaces.json ignoring volatile lastUpdated refreshes."""
    account_result, common_result, base_result = dict(account), dict(common), {}
    for key in set(account) | set(base) | set(common):
        a, b, c = (mapping.get(key, MISSING) for mapping in (account, base, common))
        if a is not MISSING and c is not MISSING and without_last_updated(a) == without_last_updated(c):
            value = dict(a)
            stamps = [item["lastUpdated"] for item in (a, c) if isinstance(item, dict) and isinstance(item.get("lastUpdated"), str)]
            if stamps:
                value["lastUpdated"] = max(stamps)
        elif a == c:
            value = a
        elif a == b or (a is not MISSING and b is not MISSING and without_last_updated(a) == without_last_updated(b)):
            value = c
        elif c == b or (c is not MISSING and b is not MISSING and without_last_updated(c) == without_last_updated(b)):
            value = a
        else:
            if b is not MISSING:
                base_result[key] = b
            continue
        for result in (account_result, common_result):
            if value is MISSING:
                result.pop(key, None)
            else:
                result[key] = value
        if value is not MISSING:
            base_result[key] = value
    return account_result, common_result, base_result


def committed_common_baseline(previous, local_before, common_after):
    """Advance baseline only for keys where local and the newly written common now agree."""
    updated = dict(previous)
    for key in set(previous) | set(local_before) | set(common_after):
        a = local_before.get(key, MISSING)
        c = common_after.get(key, MISSING)
        if a == c:
            if c is MISSING:
                updated.pop(key, None)
            else:
                updated[key] = c
    return updated


# Removing every shared entry of a side at once is what a reset or lost file
# looks like (a reinstalled CLI, a deleted plugins folder, a settings file
# overwritten with {}), not a series of uninstalls. Such a run restores instead
# of spreading the removal to the shared configuration and every other account.
MASS_REMOVAL_MINIMUM = 2


def removes_everything(before, after):
    removed = [key for key in before if key not in after]
    return len(removed) >= MASS_REMOVAL_MINIMUM and len(removed) == len(before)


def guarded_merge(merger, local, base, common, reset, label):
    """Three-way merge that never lets a reset side delete entries elsewhere.

    reset is true when one side's file is missing or was reset: no entry is then
    known to have been removed on purpose, so the merge runs as a first sync and
    each side only gains what the other has. Returns the merge and the baseline
    it actually used.
    """
    if base and not reset:
        merged_local, merged_common, merged_base = merger(local, base, common)
        if not (removes_everything(common, merged_common) or removes_everything(local, merged_local)):
            return merged_local, merged_common, merged_base, base
        print(f"{label}：一侧一次移除了全部共用条目，已按异常处理，未同步删除，并从另一侧补回。"
              "若确实要全部移除，请分次移除（每次至少保留一项）。", file=sys.stderr)
    merged_local, merged_common, merged_base = merger(local, {}, common)
    return merged_local, merged_common, merged_base, {}


def share_plugin_folders(base, lock_dir):
    """Link the marketplace and plugin caches; False keeps this profile's plugins private."""
    config, common = config_directory(base), Path.home() / ".claude"
    local_root, shared_root_dir = config / "plugins", common / "plugins"
    local_root.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    shared_root_dir.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    known = []
    private_marketplaces = set()
    for root in (local_root, shared_root_dir):
        _, data = json_snapshot(root / "known_marketplaces.json")
        if data is None:
            return False
        neutral = plugin_paths(data, plugin_root_forms(root.parent), NEUTRAL_PLUGINS)
        known.append(neutral)
        for mkt_name, entry in neutral.items():
            if not shareable_marketplace_entry(mkt_name, entry):
                private_marketplaces.add(mkt_name)
    def source(index, name):
        entry = known[index].get(name)
        return entry.get("source") if isinstance(entry, dict) else None
    def conflicting(local, shared, depth, marketplace):
        for child in local.iterdir():
            target, name = shared / child.name, marketplace or child.name
            if linked(child) or name in PRIVATE_PLUGIN_ENTRIES or name in private_marketplaces:
                return True
            if not (target.exists() or linked(target)):
                continue
            # A cached copy is reproducible from its marketplace. Two different
            # marketplaces under one name (or an unverified source at leaf overlap)
            # are not, so nothing is chosen for them.
            s0, s1 = source(0, name), source(1, name)
            if None not in (s0, s1) and s0 != s1:
                return True
            if depth == 0 and (s0 is None or s1 is None or s0 != s1):
                return True
            if child.is_dir() != target.is_dir() or linked(target):
                return True
            if depth and child.is_dir() and conflicting(child, target, depth - 1, name):
                return True
        return False
    def has_private_directory(local):
        return any(linked(child) or child.name in PRIVATE_PLUGIN_ENTRIES or child.name in private_marketplaces
                   for child in local.iterdir())
    plan = []
    for name, depth in SHARED_PLUGIN_FOLDERS:
        local, shared = local_root / name, shared_root_dir / name
        if (shared.exists() or linked(shared)) and not shared.is_dir():
            return False
        if linked(local):
            local_resolved, shared_resolved = safe_resolve(local), safe_resolve(shared)
            if local_resolved is None or shared_resolved is None or local_resolved != shared_resolved:
                return False
            continue
        if local.exists() and not local.is_dir():
            return False
        if local.exists() and has_private_directory(local):
            return False
        if local.exists() and shared.exists() and conflicting(local, shared, depth, None):
            return False
        plan.append((local, shared, depth))
    def move(local, shared, depth, relative):
        for child in sorted(local.iterdir()):
            target = shared / child.name
            if not (target.exists() or linked(target)):
                target.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
                child.rename(target)
            elif depth and child.is_dir() and target.is_dir():
                move(child, target, depth - 1, relative / child.name)
            else:
                # Keep the profile's own copy recoverable instead of deleting it.
                backup_prefix = lock_dir / "backups" / base.name / "plugins" / relative / child.name
                backup = unique_backup_path(backup_prefix)
                child.rename(backup)
                prune_backup_series(backup_prefix)
        local.rmdir()
    # Every rename is atomic, so an interrupted migration resumes from here.
    for local, shared, depth in plan:
        shared.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
        if local.exists():
            move(local, shared, depth, Path(local.name))
        resource_link(local, shared)
    return True


NEUTRAL_PLUGINS = "\0plugins" + os.sep


def plugin_root_forms(config):
    """Spellings under which the CLI may record a profile's plugins folder.

    Derives the alias directly from client_runtime.path so platforms without
    path aliasing (such as _remote_windows) never emit non-existent alias paths.
    """
    real = str(config / "plugins") + os.sep
    if config == Path.home() / ".claude":
        return [real]
    try:
        aliased = _runtime["path"](config)
    except (OSError, ValueError):
        return [real]
    alias_root = str(Path(aliased) / "plugins") + os.sep
    return [alias_root, real] if alias_root != real else [real]


def plugin_paths(value, old, new):
    """Re-anchor recorded locations from one plugins folder spelling to another."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in ("installPath", "installLocation") and isinstance(item, str):
                prefix = next((form for form in old if item.startswith(form)), None)
                result[key] = new + item[len(prefix):] if prefix is not None else item
            else:
                result[key] = plugin_paths(item, old, new)
        return result
    if isinstance(value, list):
        return [plugin_paths(item, old, new) for item in value]
    return value


def plugin_form(value, forms):
    """The spelling a registry already uses, so rewritten entries match the CLI's own."""
    found = []
    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in ("installPath", "installLocation") and isinstance(child, str):
                    found.extend(form for form in forms if child.startswith(form))
                else:
                    walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    walk(value)
    return found[0] if found else forms[0]


def clear_active_plugin_orphans(common_root, installed_plugins):
    """Remove stale .orphaned_at markers for plugin versions still installed in the shared registry."""
    cache_root = common_root / "cache"
    if not cache_root.is_dir():
        return
    try:
        resolved_cache = cache_root.resolve()
    except OSError:
        return
    for records in installed_plugins.values():
        if not isinstance(records, list):
            continue
        for entry in records:
            install_path = entry.get("installPath") if isinstance(entry, dict) else None
            if not isinstance(install_path, str) or not install_path:
                continue
            try:
                target = Path(install_path).resolve()
            except OSError:
                continue
            if target == resolved_cache or not target.is_relative_to(resolved_cache):
                continue
            marker = target / ".orphaned_at"
            try:
                if marker.is_file() and not marker.is_symlink():
                    marker.unlink(missing_ok=True)
            except OSError:
                pass


def marketplace_of(plugin_id):
    return plugin_id.rpartition("@")[2] if isinstance(plugin_id, str) else None


def sync_plugin_registries(base, lock_dir, record):
    """Merge public installed plugins and known marketplaces in both directions.

    Private/org/claude.ai/credentialed entries and entries whose paths do not
    resolve strictly under <plugins>/cache/ or <plugins>/marketplaces/ stay
    private on their owning side. Returns (private_marketplaces, private_plugins).

    Uninstalls and marketplace removals must synchronize alongside installs:
    accounts share plugins/cache and plugins/marketplaces, and the official CLI
    marks uninstalled cache versions with .orphaned_at for later deletion (and
    immediately deletes removed marketplace directories). Keeping a plugin
    installed in another account while one account uninstalled it would allow
    the official CLI to delete files still referenced by the other account.

    A registry that is missing records no uninstall, and one side removing
    every shared plugin at once, or a marketplace whose plugins stay installed,
    looks like a reset rather than a choice. Both registries are then merged
    as on a first sync: each side regains entries and nothing is removed.
    """
    config, common = config_directory(base), Path.home() / ".claude"
    local_root, common_root = config / "plugins", common / "plugins"
    local_forms, common_forms = plugin_root_forms(config), plugin_root_forms(common)
    bases = dict_field(record, "plugins")
    private_marketplaces, private_plugins = set(), set()
    plans = []
    for name, field in PLUGIN_REGISTRIES:
        local_raw, local = json_snapshot(local_root / name)
        common_raw, shared = json_snapshot(common_root / name)
        if local is None or shared is None:
            continue
        if field and (local.get("version", 2) != 2 or shared.get("version", 2) != 2):
            continue
        local_map = local.get(field, {}) if field else local
        common_map = shared.get(field, {}) if field else shared
        if not isinstance(local_map, dict) or not isinstance(common_map, dict):
            continue
        previous_raw = bases.get(name) if isinstance(bases.get(name), dict) else {}
        local_neutral = plugin_paths(local_map, local_forms, NEUTRAL_PLUGINS)
        common_neutral = plugin_paths(common_map, common_forms, NEUTRAL_PLUGINS)
        if name == "known_marketplaces.json":
            allow = shareable_marketplace_entry
            merger = three_way_marketplaces
        else:
            allow = lambda key, value: shareable_installed_plugin(key, value, private_marketplaces)
            merger = three_way
        excluded = set()
        for mapping in (local_neutral, common_neutral):
            for key, value in mapping.items():
                if not allow(key, value):
                    excluded.add(key)
        if name == "known_marketplaces.json":
            private_marketplaces |= excluded
        else:
            private_plugins |= excluded
        plan = {
            "name": name, "field": field, "merger": merger,
            "local_raw": local_raw, "local": local, "local_map": local_map,
            "common_raw": common_raw, "shared": shared, "common_map": common_map,
            "local_form": plugin_form(local_map, local_forms), "common_form": plugin_form(common_map, common_forms),
            "local_shareable": {k: v for k, v in local_neutral.items() if k not in excluded},
            "common_shareable": {k: v for k, v in common_neutral.items() if k not in excluded},
            "previous": {k: v for k, v in previous_raw.items() if k not in excluded and allow(k, v)},
            "local_private": {k: v for k, v in local_map.items() if k in excluded},
            "common_private": {k: v for k, v in common_map.items() if k in excluded},
        }
        plan["merged"] = merger(plan["local_shareable"], plan["previous"], plan["common_shareable"])
        plans.append(plan)
    # Judge both registries together before anything is written.
    reset = any(plan["local_raw"] is None or plan["common_raw"] is None for plan in plans)
    suspicious = None
    installed = next((plan for plan in plans if plan["name"] == "installed_plugins.json"), None)
    for plan in plans:
        merged_local, merged_common, _ = plan["merged"]
        if not plan["previous"]:
            continue
        if removes_everything(plan["local_shareable"], merged_local) or removes_everything(plan["common_shareable"], merged_common):
            suspicious = suspicious or ("插件市场登记" if plan["name"] == "known_marketplaces.json" else "已安装插件登记")
        if plan["name"] == "known_marketplaces.json" and installed is not None:
            for side, merged, plugins in ((0, merged_local, installed["merged"][0]), (1, merged_common, installed["merged"][1])):
                before = plan["local_shareable"] if side == 0 else plan["common_shareable"]
                removed = {key for key in before if key not in merged}
                if any(marketplace_of(plugin) in removed for plugin in plugins):
                    suspicious = suspicious or "插件市场登记"
    if suspicious:
        print(f"{suspicious}：一侧一次移除了全部共用条目，或移除了仍有插件在用的插件市场，已按异常处理，"
              "未同步删除，并从另一侧补回。若确实要全部移除，请分次移除（每次至少保留一项），"
              "移除插件市场前先卸载其中的插件。", file=sys.stderr)
    for plan in plans:
        name, field, merger = plan["name"], plan["field"], plan["merger"]
        previous = plan["previous"]
        if reset or suspicious:
            # Nothing is known to be removed on purpose: merge as a first sync.
            previous = {}
            plan["merged"] = merger(plan["local_shareable"], previous, plan["common_shareable"])
        merged_local_n, merged_common_n, merged_base = plan["merged"]
        local_raw, local, local_map = plan["local_raw"], plan["local"], plan["local_map"]
        common_raw, shared, common_map = plan["common_raw"], plan["shared"], plan["common_map"]
        merged_common = {**plan["common_private"], **plugin_paths(merged_common_n, [NEUTRAL_PLUGINS], plan["common_form"])}
        common_written = False
        if merged_common != common_map:
            data = {**shared, "version": 2, field: merged_common} if field else merged_common
            if not replace_json(common_root / name, data, common_raw, lock_dir / "backups" / "common" / "plugins" / name):
                continue
            common_written = True
            # Persist common-side agreement immediately so a local race below
            # cannot resurrect a removed or modified entry on the next launch.
            bases[name] = committed_common_baseline(previous, plan["local_shareable"], merged_common_n)
            write_record(base, record)
        merged_local = {**plan["local_private"], **plugin_paths(merged_local_n, [NEUTRAL_PLUGINS], plan["local_form"])}
        if merged_local != local_map:
            data = {**local, "version": 2, field: merged_local} if field else merged_local
            if not replace_json(local_root / name, data, local_raw, lock_dir / "backups" / base.name / "plugins" / name):
                if name == "installed_plugins.json" and common_written:
                    clear_active_plugin_orphans(common_root, merged_common)
                continue
        if name == "installed_plugins.json":
            clear_active_plugin_orphans(common_root, merged_common)
        bases[name] = merged_base
    return private_marketplaces, private_plugins


def withdraw_additions(result, additions):
    for key, value in (additions.get("keys") or {}).items():
        if key in result and result[key] == value:
            del result[key]
    hooks = result.get("hooks")
    if isinstance(hooks, dict):
        for event, groups in (additions.get("hooks") or {}).items():
            present = hooks.get(event)
            if not isinstance(present, list) or not isinstance(groups, list):
                continue
            for group in groups:
                if group in present:
                    present.remove(group)
            if not present:
                del hooks[event]
        if not hooks and additions.get("hooks_created"):
            del result["hooks"]


def sync_profile_settings(base, lock_dir, record, plugins_shared, private_marketplaces=frozenset(), private_plugins=frozenset()):
    """Copy shared settings into a profile for clients that take no flags.

    Only keys the profile does not set are added, and hook groups are appended
    per event. The record lists the additions, so later runs refresh or withdraw
    exactly those entries and never a value the user changed. Public plugin
    choices are merged in both directions; private/credentialed entries stay
    local to each side. Callers hold resources.lock.
    """
    config = config_directory(base)
    path, common_path = config / "settings.json", Path.home() / ".claude/settings.json"
    original, current = json_snapshot(path)
    common_raw, common = json_snapshot(common_path)
    if current is None or common is None or "hooks" in current and not isinstance(current["hooks"], dict):
        return
    result = json.loads(json.dumps(current))
    previous = {key: record.get(key) for key in ("keys", "hooks", "hooks_created") if key in record}
    if not all(isinstance(previous.get(name, {}), dict) for name in ("keys", "hooks")):
        previous = {}
    previous_pending = record.get("pending")
    # An interrupted earlier run may have written its additions without
    # completing the record. Its intent names the exact file it was about to
    # publish; only that file proves the additions landed and are Cage's.
    pending = confirmed_intent(previous_pending, original)
    if common_raw is None:
        # A missing shared file says nothing about which shared values were
        # removed: keep every earlier addition as it is and add nothing.
        kept = pending or previous
        additions = {"keys": dict(kept.get("keys") or {}), "hooks": dict(kept.get("hooks") or {}),
                     "hooks_created": bool(kept.get("hooks_created"))}
    else:
        withdraw_additions(result, previous)
        withdraw_additions(result, pending)
        added = {key: common[key] for key in sorted(PROFILE_SETTINGS - {"hooks"}) if key in common and key not in result}
        result.update(added)
        added_hooks, created = {}, "hooks" not in result
        if isinstance(common.get("hooks"), dict):
            target = result.setdefault("hooks", {})
            for event, groups in common["hooks"].items():
                present = target.get(event, [])
                if not isinstance(groups, list) or not isinstance(present, list):
                    continue
                missing = []
                for group in groups:
                    if group not in present and group not in missing:
                        missing.append(group)
                if missing:
                    target[event] = present + missing
                    added_hooks[event] = missing
            if created and not target:
                del result["hooks"]
        additions = {"keys": added, "hooks": added_hooks, "hooks_created": created and bool(added_hooks)}
    common_result = dict(common)
    plugin_record = dict_field(record, "plugins")
    plugin_bases = dict(plugin_record)
    common_baselines = {}
    if plugins_shared:
        excluded_marketplaces = set(private_marketplaces)
        # A settings file that is missing or was reset to {} records no
        # deliberate removal; it must not disable plugins everywhere else.
        reset = original is None or current == {} or common_raw is None or common == {}
        # Process extraKnownMarketplaces first so any credentialed or org
        # marketplace excluded here also blocks its entries in enabledPlugins.
        for key in ("extraKnownMarketplaces", "enabledPlugins"):
            mine, theirs = result.get(key, {}), common.get(key, {})
            if not isinstance(mine, dict) or not isinstance(theirs, dict):
                continue
            known_raw = plugin_bases.get(key) if isinstance(plugin_bases.get(key), dict) else {}
            if key == "extraKnownMarketplaces":
                allow = shareable_extra_marketplace
            else:
                allow = lambda name, _val: shareable_enabled_plugin(name, excluded_marketplaces, private_plugins)
            excluded = {k for mapping in (mine, theirs) for k, v in mapping.items() if not allow(k, v)}
            if key == "extraKnownMarketplaces":
                excluded_marketplaces |= excluded
            mine_shareable = {k: v for k, v in mine.items() if k not in excluded}
            theirs_shareable = {k: v for k, v in theirs.items() if k not in excluded}
            known = {k: v for k, v in known_raw.items() if k not in excluded and allow(k, v)}
            mine_private = {k: v for k, v in mine.items() if k in excluded}
            theirs_private = {k: v for k, v in theirs.items() if k in excluded}
            label = "插件启用设置" if key == "enabledPlugins" else "额外插件市场设置"
            merged_mine, merged_theirs, plugin_bases[key], known = guarded_merge(
                three_way, mine_shareable, known, theirs_shareable, reset, label)
            common_baselines[key] = committed_common_baseline(known, mine_shareable, merged_theirs)
            final_mine = {**mine_private, **merged_mine}
            final_theirs = {**theirs_private, **merged_theirs}
            for target, value, original_map in ((result, final_mine, current), (common_result, final_theirs, common)):
                if value or key in original_map:
                    target[key] = value
    if common_result != common:
        # Publish plugin choices first: if this is refused, nothing else moves.
        if not replace_json(common_path, common_result, common_raw, lock_dir / "backups" / "common" / "settings.json"):
            return
        # Persist common-side baselines immediately so a local race below
        # cannot resurrect a disabled plugin on the next launch.
        if common_baselines:
            plugin_record.update(common_baselines)
            write_record(base, record)
    if result != current:
        write_record(base, {**record, "pending": {**additions, "digest": bytes_digest(json_bytes(result))}})
        if not replace_json(path, result, original, lock_dir / "backups" / base.name / "settings.json"):
            if previous_pending is None:
                record.pop("pending", None)
            else:
                record["pending"] = previous_pending
            write_record(base, record)
            return
    record.pop("pending", None)
    record.update(additions)
    plugin_record.update({key: plugin_bases[key] for key in PLUGIN_SETTINGS if key in plugin_bases})


def confirmed_intent(intent, original):
    """An interrupted write's intent, only if the file is exactly what it was about to publish."""
    if not isinstance(intent, dict) or original is None or intent.get("digest") != bytes_digest(original):
        return {}
    return intent


def global_config_path(base):
    return Path.home() / ".claude.json" if base.name == DEFAULT else config_directory(base) / ".claude.json"


def shared_mcp_state():
    """User-scope servers of the default configuration; None when that is unknown.

    A missing or unreadable ~/.claude.json, or one without an mcpServers map,
    says nothing about which servers the user removed. Never its account fields.
    """
    raw, data = json_snapshot(Path.home() / ".claude.json")
    if raw is None or not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        return None
    return {name: value for name, value in data["mcpServers"].items() if isinstance(value, dict)}


def shared_mcp_servers():
    return shared_mcp_state() or {}


def sync_profile_mcp(base, lock_dir, record, add_missing):
    """Add shared user-scope MCP servers the profile lacks; refresh or withdraw earlier additions.

    Only the mcpServers entry changes. OAuth tokens for MCP servers stay in each
    profile's credential store and are never read here.
    """
    path = global_config_path(base)
    original, current = json_snapshot(path)
    if current is None or "mcpServers" in current and not isinstance(current["mcpServers"], dict):
        return
    shared = shared_mcp_state()
    if shared is None:
        # Keep earlier additions until the shared definitions are readable again.
        return
    servers = dict(current.get("mcpServers") or {})
    ours = record.get("mcp") if isinstance(record.get("mcp"), dict) else {}
    previous_pending = record.get("pending_mcp")
    pending = confirmed_intent(previous_pending, original).get("servers")
    pending = pending if isinstance(pending, dict) else {}
    withdrawn = [name for added in (ours, pending) for name, value in added.items()
                 if servers.get(name) == value and name not in shared]
    if not shared and len(set(withdrawn)) >= MASS_REMOVAL_MINIMUM:
        # The account may now hold the only copy of these definitions.
        print("用户级 MCP 服务器：默认配置一次移除了全部共用服务器，已按异常处理，各账号中的副本保留不动。"
              "若确实要全部移除，请分次移除（每次至少保留一项）。", file=sys.stderr)
        return
    for added in (ours, pending):
        for name, value in added.items():
            if servers.get(name) == value:
                del servers[name]
    additions = {}
    for name, value in shared.items():
        if name not in servers and (add_missing or name in ours or name in pending):
            servers[name] = additions[name] = value
    result = {key: value for key, value in current.items() if key != "mcpServers"}
    if servers or "mcpServers" in current:
        result["mcpServers"] = servers
    if result != current:
        write_record(base, {**record, "pending_mcp": {"servers": additions, "digest": bytes_digest(json_bytes(result))}})
        if not replace_json(path, result, original, lock_dir / "backups" / base.name / "claude.json"):
            if previous_pending is None:
                record.pop("pending_mcp", None)
            else:
                record["pending_mcp"] = previous_pending
            write_record(base, record)
            return
    record.pop("pending_mcp", None)
    record["mcp"] = additions


def launch_mcp_servers(base):
    """Shared servers this profile's own configuration does not already define."""
    if base.name == DEFAULT:
        return {}
    _, own = json_snapshot(global_config_path(base))
    present = own.get("mcpServers") if isinstance(own, dict) else None
    present = present if isinstance(present, dict) else {}
    return {name: value for name, value in shared_mcp_servers().items() if name not in present}


# A running terminal keeps using the --mcp-config file it was started with
# (the CLI can read it again, for example to reconnect a server). Each launch
# refreshes the time of the file it uses; only files no launch used for this
# long are removed.
MCP_LAUNCH_FILE_MAX_AGE = 30 * 24 * 60 * 60


def prune_stale_mcp_launch_files(folder, keep=None, now=None):
    if not folder.is_dir():
        return
    cutoff = (time.time() if now is None else now) - MCP_LAUNCH_FILE_MAX_AGE
    for candidate in folder.glob("mcp-*.json"):
        if candidate == keep or linked(candidate):
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                candidate.unlink(missing_ok=True)
        except OSError:
            pass


def shared_mcp_arguments(base, args):
    """Offer the shared user-scope MCP servers to a terminal through --mcp-config."""
    options = args[:args.index("--")] if "--" in args else args
    if "--strict-mcp-config" in options:
        return args
    folder = base / "launch-settings"
    servers = launch_mcp_servers(base)
    if not servers:
        prune_stale_mcp_launch_files(folder)
        return args
    data = json.dumps({"mcpServers": servers}, separators=(",", ":")).encode()
    folder.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    path = folder / ("mcp-" + hashlib.sha256(data).hexdigest() + ".json")
    try:
        # Content-addressed and reused: mark it as used by this launch.
        os.utime(path)
        reused = True
    except FileNotFoundError:
        reused = False
    if not reused:
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        try:
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    prune_stale_mcp_launch_files(folder, keep=path)
    value = _runtime["path"](path)
    # --mcp-config takes several space-separated files. Add this one to an
    # existing flag rather than relying on how repeated flags combine.
    if "--mcp-config" in options:
        index = args.index("--mcp-config")
        return [*args[:index + 1], value, *args[index + 1:]]
    # The next argument is always an option (--settings), which ends the list.
    return ["--mcp-config", value, *args]


def can_heal_shared_file(base, local, shared, record):
    """True when an atomic save replaced a link with a newer edit and shared is unchanged."""
    links = dict_field(record, "links")
    name = str(local.relative_to(config_directory(base)))
    if linked(local) or linked(shared):
        return False
    if not (local.is_file() and shared.is_file()) or local.read_bytes() == shared.read_bytes():
        return False
    return links.get(name) == file_digest(shared)


def heal_shared_file(base, local, shared, record):
    """Fold back a linked instruction file that an atomic save turned into a copy."""
    links = dict_field(record, "links")
    name = str(local.relative_to(config_directory(base)))
    if linked(local):
        if not linked(shared) and safe_resolve(local) == safe_resolve(shared) and shared.is_file():
            links[name] = file_digest(shared)
        return
    if not can_heal_shared_file(base, local, shared, record):
        return
    backup_prefix = shared_root() / "backups" / "common" / shared.name
    write_backup_bytes(backup_prefix, shared.read_bytes())
    with tempfile.NamedTemporaryFile(dir=shared.parent, delete=False) as stream:
        stream.write(local.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    try:
        os.chmod(temporary, stat.S_IMODE(shared.stat().st_mode))
        os.replace(temporary, shared)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    prune_backup_series(backup_prefix)


# A conflict that stays unresolved is explained again at most this often, so a
# terminal does not repeat the same paragraph on every launch.
NOTICE_INTERVAL = 24 * 60 * 60


def shown_path(path):
    """A path as the user would type it in the guest, without Home spelled out."""
    try:
        return "~/" + Path(path).relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def path_state(path):
    try:
        info = path.lstat()
    except OSError:
        return "missing"
    target = ""
    if linked(path):
        try:
            target = os.readlink(path)
        except OSError:
            target = "?"
    return f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}:{target}"


class Notices:
    """Show each launch problem when it first appears or changes, then daily."""

    def __init__(self, record, now=None):
        stored = record.get("notices")
        self.previous = stored if isinstance(stored, dict) else {}
        self.current = {}
        self.record = record
        self.now = time.time() if now is None else now

    def show(self, key, state, message):
        entry = self.previous.get(key)
        if (isinstance(entry, dict) and entry.get("state") == state and type(entry.get("at")) in (int, float)
                and 0 <= self.now - entry["at"] < NOTICE_INTERVAL):
            self.current[key] = entry
            return
        print(message, file=sys.stderr)
        self.current[key] = {"state": state, "at": self.now}

    def finish(self):
        # Resolved problems are forgotten, so a recurrence is explained again.
        if self.current:
            self.record["notices"] = self.current
        else:
            self.record.pop("notices", None)


def resource_conflict_notice(notices, local, shared):
    local_text, shared_text = shown_path(local), shown_path(shared)
    if linked(local):
        message = (f"共用资料同名冲突：账号中的 {local_text} 是指向共用位置以外的链接。本次未共享这一项，"
                   f"原链接保留，启动不受影响。若不再需要它，删除 {local_text} 后下次启动即改为共享 {shared_text}。")
    else:
        message = (f"共用资料同名冲突：账号中的 {local_text} 与共用的 {shared_text} 内容不同。本次未共享这一项，"
                   f"两份都原样保留，启动不受影响。请对照两份内容，把要保留的部分并入 {shared_text}，"
                   f"再删除 {local_text}，下次启动即改为共享。")
    notices.show("resource:" + str(local), path_state(local) + "|" + path_state(shared), message)


def shared_profile_resources(base, desktop=False):
    """Share local customizations; credentials, synced skills and history stay private."""
    config = config_directory(base)
    common = Path.home() / ".claude"
    common.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    if safe_resolve(config) == safe_resolve(common):
        return
    lock_dir = shared_root()
    lock_dir.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    with (lock_dir / "resources.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = read_record(base)
        notices = Notices(record)
        conflict = "账号的技能或指令与共用资料同名冲突，请先核对 ~/.claude 中的对应文件；原文件已保留"
        healable_names = {"CLAUDE.md", "AGENTS.md"}
        def check(local, shared):
            if same_path(local, shared):
                return
            if linked(local):
                if safe_resolve(local) != safe_resolve(shared):
                    raise SharedResourceConflict(conflict, local, shared)
            elif local.exists() and shared.exists():
                if local.name in healable_names and local.parent == config and can_heal_shared_file(base, local, shared, record):
                    return
                if local.is_dir() and shared.is_dir():
                    for child in local.iterdir():
                        check(child, shared / child.name)
                elif not (local.is_file() and shared.is_file() and local.read_bytes() == shared.read_bytes()):
                    raise SharedResourceConflict(conflict, local, shared)
        def merge(local, shared):
            if same_path(local, shared):
                return
            if linked(local):
                if safe_resolve(local) == safe_resolve(shared):
                    return
                raise SharedResourceConflict(conflict, local, shared)
            if local.exists():
                if local.is_dir() and shared.is_dir():
                    for child in list(local.iterdir()):
                        merge(child, shared / child.name)
                        if linked(child):
                            remove_link(child)
                    local.rmdir()
                elif not shared.exists():
                    shared.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
                    local.rename(shared)
                elif local.is_file() and shared.is_file() and local.read_bytes() == shared.read_bytes():
                    # An identical copy; the caller backed up the whole entry first.
                    local.unlink()
                else:
                    # Never choose which conflicting instruction or skill wins.
                    raise SharedResourceConflict(conflict, local, shared)
            if not shared.exists():
                if local.name in ("CLAUDE.md", "AGENTS.md"):
                    shared.touch(mode=0o600)
                else:
                    shared.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
            resource_link(local, shared)
        # claude.ai downloads skills into skills/synced. Keep that subtree
        # account-specific; link only locally installed skill entries.
        local_skills, shared_skills = config / "skills", common / "skills"
        shared_skills.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
        resources = [(config / name, common / name) for name in
                     ("commands", "agents", "rules", "agent-memory", "CLAUDE.md", "AGENTS.md")]
        if linked(local_skills):
            # If config/skills is already a symlink to ~/.claude/skills, never
            # iterate and unlink entries through the self-alias.
            if safe_resolve(local_skills) != safe_resolve(shared_skills):
                resource_conflict_notice(notices, local_skills, shared_skills)
        else:
            local_skills.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
            names = {p.name for folder in (local_skills, shared_skills) for p in folder.iterdir()} - {"synced"}
            resources += [(local_skills / name, shared_skills / name) for name in sorted(names)]
        # Validate before mutating. A conflicting entry is left exactly as it
        # is on both sides and explained; terminals and Desktop still start and
        # everything else (plugins, settings, MCP and memory) still syncs.
        ready_resources = []
        for source, destination in resources:
            try:
                check(source, destination)
                ready_resources.append((source, destination))
            except SharedResourceConflict as error:
                resource_conflict_notice(notices, error.local or source, error.shared or destination)
        for source, destination in ready_resources:
            if source.name in healable_names and source.parent == config:
                heal_shared_file(base, source, destination, record)
        for source, destination in ready_resources:
            backup_prefix = None
            if source.exists() and not linked(source) and not same_path(source, destination):
                backup_prefix = lock_dir / "backups" / base.name / source.relative_to(config)
                backup = unique_backup_path(backup_prefix)
                if source.is_dir():
                    shutil.copytree(source, backup, symlinks=True)
                else:
                    shutil.copy2(source, backup)
            try:
                merge(source, destination)
            except SharedResourceConflict as error:
                # Changed after validation (a concurrent save): each rename is
                # atomic, so what is not yet linked stays where it was.
                resource_conflict_notice(notices, error.local or source, error.shared or destination)
            if backup_prefix is not None:
                prune_backup_series(backup_prefix)
        for source, destination in ready_resources:
            if source.name in healable_names and source.parent == config:
                heal_shared_file(base, source, destination, record)
        plugins_shared = share_plugin_folders(base, lock_dir)
        private_marketplaces, private_plugins = frozenset(), frozenset()
        if plugins_shared:
            private_marketplaces, private_plugins = sync_plugin_registries(base, lock_dir, record)
        else:
            notices.show("plugins", "separate", "账号已有的插件与共用插件来源不同，插件继续按账号分开保存；其他共用资料不受影响")
        sync_profile_settings(base, lock_dir, record, plugins_shared, private_marketplaces, private_plugins)
        sync_profile_mcp(base, lock_dir, record, desktop)
        if desktop:
            link_known_project_memory(base)
        for prefix in (lock_dir / "backups" / base.name / "settings.json",
                       lock_dir / "backups" / base.name / "claude.json",
                       lock_dir / "backups" / base.name / "plugins/installed_plugins.json",
                       lock_dir / "backups" / base.name / "plugins/known_marketplaces.json",
                       lock_dir / "backups" / "common" / "settings.json",
                       lock_dir / "backups" / "common" / "plugins/installed_plugins.json",
                       lock_dir / "backups" / "common" / "plugins/known_marketplaces.json",
                       lock_dir / "backups" / "common" / "CLAUDE.md",
                       lock_dir / "backups" / "common" / "AGENTS.md"):
            prune_backup_series(prefix)
        notices.finish()
        write_record(base, record)


def project_key(directory):
    """The official CLI's project folder name: UTF-16 units, 200 characters, then a hash."""
    units = str(directory).encode("utf-16-le")
    codes = [int.from_bytes(units[index:index + 2], "little") for index in range(0, len(units), 2)]
    key = "".join(chr(code) if code < 128 and (chr(code).isascii() and chr(code).isalnum()) else "-" for code in codes)
    if len(key) <= 200:
        return key
    value = 0
    for code in codes:
        value = (value * 31 + code) & 0xFFFFFFFF
    value = abs(value - (1 << 32) if value >= 1 << 31 else value)
    digits = ""
    while True:
        value, rest = divmod(value, 36)
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"[rest] + digits
        if not value:
            break
    return key[:200] + "-" + digits


def legacy_project_key(directory):
    key = re.sub(r"[^a-zA-Z0-9]", "-", str(directory))
    return key[:140] + "-" + hashlib.sha256(str(directory).encode()).hexdigest()[:32] if len(key) > 180 else key


def link_project_memory(own, memory, backup):
    """Move a memory folder's notes into the shared one and leave a link behind."""
    if same_path(own, memory) or own == memory or linked(own):
        return
    backup_prefix = None
    if own.exists():
        entries = list(own.rglob("*"))
        if any(linked(entry) for entry in entries):
            raise SharedResourceConflict("账号的项目记忆包含异常链接，原记忆已保留，请先核对")
        sources = [source for source in entries if source.is_file()]
        for source in sources:
            destination = memory / source.relative_to(own)
            if linked(destination) or destination.exists() and (not destination.is_file() or destination.read_bytes() != source.read_bytes()):
                raise SharedResourceConflict("账号的项目记忆与共用记忆冲突，原记忆已保留，请先核对")
        for source in sources:
            destination = memory / source.relative_to(own)
            if linked(destination) or destination.exists() and destination.read_bytes() != source.read_bytes():
                raise SharedResourceConflict("账号的项目记忆与共用记忆冲突，原记忆已保留，请先核对")
            destination.parent.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copy2(source, destination)
        backup_prefix = backup
        backup = unique_backup_path(backup_prefix)
        own.rename(backup)
    resource_link(own, memory)
    if backup_prefix is not None:
        prune_backup_series(backup_prefix)


def shared_project_memory(base, directory):
    """Locate the shared memory of a project, folding in older Cage locations."""
    key = project_key(directory)
    projects = Path.home() / ".claude/projects"
    memory = projects / key / "memory"
    memory.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    legacy = legacy_project_key(directory)
    if legacy != key and (projects / legacy / "memory").is_dir():
        link_project_memory(projects / legacy / "memory", memory, shared_root() / "backups/common/projects" / legacy / "memory")
    for name in sorted({key, legacy}):
        own = config_directory(base) / "projects" / name / "memory"
        if name == key or own.is_dir() and not linked(own):
            link_project_memory(own, memory, shared_root() / "backups" / base.name / "projects" / name / "memory")
    return key, memory


def link_known_project_memory(base):
    """Give clients without launch flags (Desktop) the shared memory of known projects."""
    shared, own = Path.home() / ".claude/projects", config_directory(base) / "projects"
    keys = {path.parent.name for path in shared.glob("*/memory") if path.is_dir() and not linked(path)}
    keys |= {path.parent.name for path in own.glob("*/memory") if path.is_dir() and not linked(path)}
    for key in sorted(keys):
        try:
            memory = shared / key / "memory"
            memory.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
            link_project_memory(own / key / "memory", memory, shared_root() / "backups" / base.name / "projects" / key / "memory")
        except (SharedResourceConflict, OSError) as error:
            print(str(error), file=sys.stderr)


def shared_launch_arguments(base, args, settings_fd=None):
    """Apply a stable project memory location without sharing auth/settings files."""
    lock_dir = shared_root()
    lock_dir.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    with (lock_dir / "resources.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return shared_launch_arguments_locked(base, args, settings_fd)


def shared_memory_path(memory):
    """Recover only an owned memory directory; never replace runtime executables."""
    try:
        return _runtime["path"](memory)
    except _runtime["RuntimePathConflict"] as error:
        alias = error.alias
    if linked(alias) or not alias.is_dir():
        raise SharedResourceConflict("项目记忆的运行路径异常，原数据已保留，请同步环境组件后重试")
    if os.name != "nt" and alias.stat().st_uid != os.getuid():
        raise SharedResourceConflict("项目记忆的运行路径归属异常，原数据已保留，请检查目录权限")
    # Inspect the entire tree before copying. A conflicting note must never be
    # overwritten, and links must not redirect recovery outside this directory.
    entries = sorted(alias.rglob("*"))
    for source in entries:
        destination = memory / source.relative_to(alias)
        if linked(source) or linked(destination):
            raise SharedResourceConflict("项目记忆包含异常链接，原数据已保留，请先核对记忆目录")
        if source.is_dir():
            conflict = destination.exists() and not destination.is_dir()
        elif source.is_file():
            conflict = destination.exists() and (not destination.is_file() or source.read_bytes() != destination.read_bytes())
        else:
            conflict = True
        if conflict:
            raise SharedResourceConflict("项目记忆存在同名内容冲突，两份记忆均已保留，请先核对后重试")
    backup_prefix = shared_root() / "backups/runtime-memory" / alias.name
    backup = unique_backup_path(backup_prefix)
    shutil.copytree(alias, backup, symlinks=True)
    for source in entries:
        destination = memory / source.relative_to(alias)
        if source.is_dir():
            destination.mkdir(mode=PRIVATE_MODE, exist_ok=True)
        else:
            # Exclusive publication also preserves a note written by an active
            # CLI after preflight. On failure both the source and backup remain.
            try:
                with destination.open("xb") as output, source.open("rb") as input:
                    shutil.copyfileobj(input, output)
            except FileExistsError:
                if not destination.is_file() or destination.read_bytes() != source.read_bytes():
                    raise SharedResourceConflict("项目记忆在恢复期间发生冲突，两份记忆均已保留，请先核对后重试")
    # Retain the original directory too: an older process may still hold it
    # open or write to it. Nothing in recovery recursively deletes user notes.
    alias.rename(alias.with_name(backup.name + ".recovered"))
    prune_backup_series(backup_prefix)
    return _runtime["path"](memory)


def merged_hooks(shared, supplied):
    """Run launch-specific hooks in addition to the shared user hooks."""
    if not isinstance(shared, dict) or not isinstance(supplied, dict):
        return None
    result = {event: list(groups) for event, groups in shared.items() if isinstance(groups, list)}
    for event, groups in supplied.items():
        if not isinstance(groups, list):
            return None
        result[event] = result.get(event, []) + groups
    return result


def shared_launch_arguments_locked(base, args, settings_fd):
    directory = Path.cwd().resolve()
    try:
        git = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             cwd=directory, capture_output=True, text=True, timeout=2)
        if git.returncode == 0:
            common_git = Path(git.stdout.strip())
            if common_git.name == ".git":
                directory = common_git.parent.resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    _, memory = shared_project_memory(base, directory)
    settings = {"autoMemoryDirectory": shared_memory_path(memory)}
    _, common_settings = json_snapshot(Path.home() / ".claude/settings.json")
    shared_keys = SHARED_SETTINGS
    if isinstance(common_settings, dict):
        settings.update({key: value for key, value in common_settings.items() if key in shared_keys})
    # Hook groups already present in the profile's own settings (including
    # those copied there for Desktop) run from that file. Pass only the rest.
    _, own_settings = json_snapshot(config_directory(base) / "settings.json")
    own_hooks = own_settings.get("hooks") if isinstance(own_settings, dict) else None
    if isinstance(settings.get("hooks"), dict) and isinstance(own_hooks, dict):
        remaining = {}
        for event, groups in settings["hooks"].items():
            present = own_hooks.get(event)
            groups = [group for group in groups if not (isinstance(present, list) and group in present)] if isinstance(groups, list) else groups
            if groups:
                remaining[event] = groups
        settings["hooks"] = remaining
        if not remaining:
            del settings["hooks"]
    # Respect an explicit memory location in existing user/project settings.
    paths = [Path.home() / ".claude/settings.json", config_directory(base) / "settings.json"]
    for parent in reversed((Path.cwd(), *Path.cwd().parents)):
        paths += [parent / ".claude/settings.json", parent / ".claude/settings.local.json"]
    for path in paths:
        _, stored = json_snapshot(path)
        if path != Path.home() / ".claude/settings.json" and isinstance(stored, dict):
            # Let the CLI's normal profile/project precedence decide these
            # values. A shared default must not turn into a command-line override.
            # The CLI combines hooks from every source instead of choosing one,
            # so project or account hooks must not disable the shared ones.
            for key in shared_keys & stored.keys() - {"hooks"}:
                settings.pop(key, None)
        if isinstance(stored, dict) and "autoMemoryDirectory" in stored:
            settings["autoMemoryDirectory"] = stored["autoMemoryDirectory"]
    result, iterator = [], iter(args)
    for arg in iterator:
        if arg == "--":
            result += [arg, *iterator]
            break
        if arg == "--settings" or arg.startswith("--settings="):
            value = next(iterator) if arg == "--settings" else arg.split("=", 1)[1]
            supplied = json.loads(value) if value.lstrip().startswith("{") else json.loads(Path(value).read_text(encoding="utf-8"))
            hooks = merged_hooks(settings.get("hooks"), supplied.get("hooks")) if isinstance(supplied, dict) else None
            settings.update(supplied)
            if hooks is not None:
                settings["hooks"] = hooks
        else:
            result.append(arg)
    data = json.dumps(settings, separators=(",", ":")).encode()
    if settings_fd is not None:
        # Mobile launch deliberately keeps settings in an anonymous file.
        # Sharing local defaults must not persist its provider material.
        with os.fdopen(os.dup(settings_fd), "r+b") as snapshot:
            snapshot.seek(0)
            snapshot.truncate()
            snapshot.write(data)
            snapshot.flush()
            snapshot.seek(0)
        return ["--settings", "/dev/fd/" + str(settings_fd), *result]
    folder = base / "launch-settings"
    folder.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(data).hexdigest() + ".json")
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        try:
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
    return ["--settings", _runtime["path"](path), *result]


def main(args, *, mobile_settings_fd=None, verify_mobile=None):
    mode, account_id, *rest = args
    if mode == "quotas":
        result = {}
        for candidate in [account_id, *rest]:
            quota = read_quota(profile(candidate))
            if quota:
                result[candidate] = quota
        print(json.dumps(result))
        return 0
    base = profile(account_id)
    if account_id == DEFAULT and mode == "login":
        raise ValueError("new logins require a private profile")
    if mode in ("usage", "snapshot") and account_id == DEFAULT and not config_directory(base).is_dir():
        print(json.dumps(changed_identity() if mode == "usage" else {"logged_in": False}))
        return 0
    original_umask = os.umask(0o077)
    if account_id != DEFAULT:
        config_directory(base).mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    if mode == "share":
        # Desktop starts its own CLI without arguments; prepare the profile.
        if account_id != DEFAULT:
            shared_profile_resources(base, desktop=True)
        return 0
    elif mode == "statusline":
        base.mkdir(mode=PRIVATE_MODE, parents=True, exist_ok=True)
    env = cli_environment(base)
    if account_id == DEFAULT:
        directory = env.get("CLAUDE_CONFIG_DIR") or str(config_directory(base))
        endpoint = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        if Path(directory).resolve() != config_directory(base).resolve() or endpoint not in ("", "https://api.anthropic.com"):
            if mode in ("snapshot", "usage"):
                print(json.dumps(changed_identity() if mode == "usage" else {"logged_in": False}))
                return 0
            raise ValueError("not the default subscription configuration")
    binary = os.environ.get("CAGE_CLI_BINARY") if os.name == "nt" else shutil.which("claude", path=env["PATH"])
    if not binary:
        raise ValueError("Claude is not installed")
    if mode == "archive-login":
        # Only authentication follows a user who signs into a different account.
        if len(rest) != 2 or base.name == DEFAULT or base.name == rest[0]:
            raise ValueError("invalid account archive")
        print(json.dumps(recover_login(binary, base, *rest, archive=True)))
        return 0
    if mode == "recover-login":
        if len(rest) != 2:
            raise ValueError("recovery requires source and expected identity")
        print(json.dumps(recover_login(binary, base, *rest)))
        return 0
    if mode == "usage":
        if len(rest) != 1:
            raise ValueError("usage requires an expected account")
        print(json.dumps(query_usage(binary, env, base, rest[0])))
        return 0
    expected = _controller_context.get("CAGE_CLAUDE_SUBJECT")
    if mode == "statusline":
        raw = sys.stdin.buffer.read((1 << 20) + 1)
        if len(raw) <= 1 << 20:
            try:
                payload = json.loads(raw)
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                save_usage(base, payload, expected, lambda: subject(identity(binary, env, Path.cwd())))
        line = user_status_line()
        if line is None:
            print("Claude")
            return 0
        # Draw the user's own status line from the same input, as the CLI would.
        try:
            shown = subprocess.run(line["command"], shell=True, input=raw, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            shown = b""
        sys.stdout.buffer.write(shown)
        return 0
    if mode == "snapshot":
        summary = identity(binary, env, Path.home())
        quota = read_quota(base)
        if quota and quota["identity"] == subject(summary):
            summary["quota"] = quota
        print(json.dumps(summary))
        return 0
    owner = _controller_context.get("CAGE_LOGIN_OWNER", "")
    owner_args = ["--account-id", owner] if re.fullmatch("[0-9a-f]{24}", owner) else []
    scoped = _controller_context.get("CAGE_ACCOUNT_BROWSER")
    if mode == "login":
        if os.name != "nt":
            env["BROWSER"] = _runtime["launcher"](scoped or OPEN, executable=True, arguments=[*owner_args, "--login-id", account_id])
        # Set the handler before spawning so SSH/window closure also disposes
        # the ephemeral browser and its cookies.
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt()
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, interrupted)
        signal.signal(signal.SIGTERM, interrupted)
        try:
            (base / "quota.json").unlink(missing_ok=True)
            return subprocess.call([_runtime["path"](binary), "auth", "login", *([] if os.name == "nt" else ["--claudeai"])], cwd=Path.home(), env=env)
        finally:
            if os.name != "nt":
                finish = [scoped or OPEN, *owner_args, "--login-id", account_id, "--finish-login"]
                subprocess.run(finish, env=os.environ, timeout=8, check=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if mode == "run":
        if os.name != "nt" and (owner_args or scoped or sys.platform == "darwin"):
            env["BROWSER"] = _runtime["launcher"](scoped or OPEN, executable=True, arguments=owner_args, login_config=config_directory(base))
        shared_profile_resources(base)
        if not settings_flags(rest) and not has_status_line(base):
            rest = ["--settings", json.dumps({"statusLine": status_line(_runtime["command_text"](__file__, ["statusline", account_id]))}), *rest]
        rest = shared_launch_arguments(base, rest, mobile_settings_fd)
        rest = shared_mcp_arguments(base, rest)
        # Profile/project settings can override inherited authentication. Check
        # the actual CLI method in the project before launching the conversation.
        options = settings_flags(rest)
        # A mobile turn verifies and executes with the same anonymous settings
        # snapshot. Keep its descriptor open across the auth-status child.
        pass_fds = () if mobile_settings_fd is None else (mobile_settings_fd,)
        reading = identity(binary, env, Path.cwd(), options, pass_fds=pass_fds) if expected else None
        if not expected or subject(reading) != expected:
            print("此项目未使用所选 Claude 订阅。请检查项目配置和账号登录状态。", file=sys.stderr)
            return 1
        if mobile_settings_fd is not None:
            verify_mobile(reading)
            os.lseek(mobile_settings_fd, 0, os.SEEK_SET)
        # The private profile is ready. Claude and its tools must create files
        # in the shared project using the user's usual umask.
        os.umask(original_umask)
        if os.name == "nt":
            return subprocess.call([_runtime["path"](binary), *rest], env=env)
        os.execve(binary, [binary, *rest], env)
    raise ValueError("unknown operation")


def macos_open(args):
    login = _controller_context.get("CAGE_CLAUDE_LOGIN", "")
    account = ""
    while len(args) >= 3:
        flag, value, *args = args
        if not re.fullmatch("[a-f0-9]{24}", value):
            return 2
        if flag == "--login-id":
            login = value
        elif flag == "--account-id":
            account = value
        else:
            return 2
    if len(args) != 1:
        return 2
    finish = args[0] == "--finish-login"
    if not finish:
        from urllib.parse import urlsplit
        url = urlsplit(args[0])
        if len(args[0]) > 8192 or url.scheme != "https" or url.username or url.password or url.port not in (None,443):
            return 2
        if url.hostname not in ("claude.ai", "claude.com", "console.anthropic.com", "platform.claude.com", "platform.anthropic.com"):
            if not login:
                os.execv("/usr/bin/open", ["open", args[0]])
            return 2
        if not login:
            login = hashlib.sha256(args[0].encode()).hexdigest()[:24]
    if not re.fullmatch("[0-9a-f]{24}", login):
        return 2
    data = {"action": "login-finish" if finish else "login-open", "login": login, "account": account}
    if not finish:
        data["url"] = args[0]
    address = Path.home() / "Library/Application Support/Cage/Login/open.sock"
    owner = address.lstat()
    if not stat.S_ISSOCK(owner.st_mode) or owner.st_uid != os.getuid() or owner.st_mode & 0o077:
        return 1
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(12)
        client.connect(str(address))
        client.sendall(json.dumps(data).encode())
        client.shutdown(socket.SHUT_WR)
        with client.makefile("rb") as reply:
            return 0 if reply.read(4) == b"OK" else 1


def entrypoint(args):
    try:
        return macos_open(args) if sys.platform == "darwin" and Path(sys.argv[0]).name == "cage-open" else main(args)
    except KeyboardInterrupt:
        return 130
    except SharedResourceConflict as error:
        print(str(error), file=sys.stderr)
    except SubscriptionStatusTimeout:
        print("读取 Claude 订阅状态超时，请稍后重试；现有登录信息已保留。", file=sys.stderr)
    except SubscriptionStatusError:
        print("无法读取 Claude 订阅状态，请确认官方 CLI 可正常运行后重试；现有登录信息已保留。", file=sys.stderr)
    except _runtime["RuntimePathConflict"]:
        print("终端运行路径发生冲突，原数据已保留，请同步环境组件后重试。", file=sys.stderr)
    except (OSError, ValueError, subprocess.SubprocessError, TypeError):
        # No raw CLI output, paths, credentials or callback URLs go to logs.
        print("准备终端或启动官方 Claude CLI 失败，请检查目录权限并同步环境组件后重试；现有登录信息已保留。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(entrypoint(sys.argv[1:]))
