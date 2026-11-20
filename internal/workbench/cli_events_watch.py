import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(os.environ["CAGE_CLI_STATE_ROOT"]) if os.name == "nt" and os.environ.get("CAGE_CLI_STATE_ROOT") else Path.home() / ".local/share/cage/cli-channels"


def read_json(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def watch(request):
    active_connections = set(request.get("connections") or ())
    interval = max(0.05, min(2.0, float(request.get("interval", 0.2))))
    heartbeat = max(1.0, float(request.get("heartbeat", 15.0)))
    file_stats = {}
    last_done = {}
    seen_approvals = set()

    def scan(initial=False):
        emitted = False
        if not ROOT.is_dir():
            return False
        for path in ROOT.glob("*/state.json"):
            conn = path.parent.name
            try:
                st = path.stat()
                sig = (getattr(st, "st_ino", 0), st.st_size, st.st_mtime_ns)
            except OSError:
                continue
            if file_stats.get(conn) == sig:
                continue
            file_stats[conn] = sig
            state = read_json(path)
            done = state.get("turn_done")
            done = done if isinstance(done, int) and done > 0 else 0
            approvals = state.get("approvals") if isinstance(state.get("approvals"), list) else []
            allow_emit = (not initial) or (conn in active_connections)
            if done > last_done.get(conn, 0):
                last_done[conn] = done
                if allow_emit and not state.get("closed"):
                    emit({
                        "id": f"{conn}:turn:{done}",
                        "event": "turn_completed",
                        "connection": conn,
                        "cli_id": state.get("cli_id", ""),
                        "seq": done,
                    })
                    emitted = True
            for item in approvals:
                aid = item.get("id") if isinstance(item, dict) else None
                if isinstance(aid, str) and aid and aid not in seen_approvals:
                    seen_approvals.add(aid)
                    if allow_emit and not state.get("closed"):
                        emit({
                            "id": f"{conn}:approval:{aid}",
                            "event": "approval_requested",
                            "connection": conn,
                            "cli_id": state.get("cli_id", ""),
                        })
                        emitted = True
        return emitted

    scan(initial=True)
    last_ping = time.monotonic()
    while True:
        time.sleep(interval)
        if scan(initial=False):
            last_ping = time.monotonic()
        elif time.monotonic() - last_ping >= heartbeat:
            emit({"event": "ping"})
            last_ping = time.monotonic()


if __name__ == "__main__":
    try:
        watch(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {})
    except BrokenPipeError:
        pass
