#!/usr/bin/env python3
"""Cluster inference proxy and explicit, serialized two-rank model hot swaps.

ops/state/THERMAL_QUALIFICATION holds normal traffic and all swaps. Monitored
probes use the existing X-Cluster-Swap-Token; /health stays available. Release
the marker only after sustained two-rank thermal qualification passes.

Registry engine_container is an exact Docker name on both ranks, or explicit
null when recipe identity is dynamic/unknown. Unknown identity skips core
cleanup, never the recipe's actual stop command. Local body-bearing replies
close instead of reusing unread input; incomplete upstream streams also close.
"""
import hmac
import http.client
import json
import os
import re
import select
import shlex
import socket
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
VLLM_HOST = os.environ.get("SWAP_VLLM_HOST", "192.0.2.1")
VLLM_PORT = int(os.environ.get("SWAP_VLLM_PORT", "8000"))
LISTEN_PORT = int(os.environ.get("SWAP_PORT", "8010"))
REGISTRY_FILE = HOME / "ops/state/cluster-profiles.json"
STATE_FILE = HOME / "ops/state/cluster-swap-state.json"
TOKEN_FILE = HOME / ".config/cluster-swap/token"
QUALIFICATION_HOLD = HOME / "ops/state/THERMAL_QUALIFICATION"
LOG = HOME / "cluster-swap.log"
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization", "proxy-authenticate"}
STREAM_CHUNK_SIZE = 16384
lock = threading.RLock()
last_activity = time.time()
registry_mtime = None
registry_rejected = None


def parse_registry():
    entries = json.loads(REGISTRY_FILE.read_text())
    if not isinstance(entries, list) or not entries:
        raise ValueError("Registry must be a nonempty list")
    required = {"id": str, "display_name": str, "engine": str, "recipe_dir": str,
                "served_model": str, "context_length": int, "slots": int,
                "uncensored": bool, "est_swap_s": int,
                "start_command": list, "stop_command": list}
    result = {}
    for entry in entries:
        if not isinstance(entry, dict) or any(
            key not in entry or type(entry[key]) is not kind
            for key, kind in required.items()
        ):
            raise ValueError("Invalid profile fields")
        if "engine_container" not in entry:
            raise ValueError("Profile engine_container is required")
        container = entry["engine_container"]
        if container is not None and (
            not isinstance(container, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container) is None
        ):
            raise ValueError("engine_container must be an exact Docker name or null")
        if not entry["id"] or entry["id"] in result or not entry["recipe_dir"].startswith("/"):
            raise ValueError("Duplicate or invalid profile identity/path")
        if type(entry.get("hidden", False)) is not bool or not all(
            isinstance(word, str) and word for command in
            (entry["start_command"], entry["stop_command"]) for word in command
        ) or not entry["start_command"] or not entry["stop_command"]:
            raise ValueError("Invalid profile commands or visibility")
        if entry["context_length"] <= 0 or entry["slots"] <= 0 or entry["est_swap_s"] <= 0:
            raise ValueError("Invalid profile limits")
        result[entry["id"]] = entry
    return result


def refresh_registry():
    """Atomic registry edits become visible to swap routes; never lose active rollback."""
    global profiles, registry_mtime, registry_rejected
    with lock:
        try:
            stat = REGISTRY_FILE.stat()
            marker = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if marker == registry_mtime or marker == registry_rejected:
                return
            updated = parse_registry()
            active = {state["current"]}
            if state.get("target"):
                active.add(state["target"])
            if not active <= updated.keys():
                raise ValueError("Registry cannot remove current or target profile")
            profiles = updated
            registry_mtime = marker
            registry_rejected = None
        except (OSError, ValueError, TypeError, KeyError) as failure:
            if locals().get("marker") != registry_rejected:
                log(f"registry reload failed: {failure}")
            registry_rejected = locals().get("marker")



def log(message):
    line = f"{datetime.now(UTC).isoformat()} {message}"
    try:
        with LOG.open("a") as output:
            output.write(line + "\n")
    except OSError:
        print(line, file=sys.stderr, flush=True)


def timestamp():
    return datetime.now(UTC).isoformat()


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_name(STATE_FILE.name + f".{os.getpid()}.tmp")
    with temporary.open("w") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE_FILE)


def update_state(**changes):
    with lock:
        state.update(changes)
        _loaded_cache["at"] = 0.0
        try:
            save_state()
            return True
        except OSError as failure:
            # Keep rollback operational through disk exhaustion, but never
            # admit a fresh swap unless its intent was durably recorded.
            log(f"state persistence failed: {failure}")
            return False


def _connection(timeout):
    return http.client.HTTPConnection(VLLM_HOST, VLLM_PORT, timeout=timeout)


def engine_request(method, path, body=None, timeout=5):
    connection = _connection(timeout)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, json.loads(payload) if payload else {}
    finally:
        connection.close()


LOADED_MODEL_TTL_S = float(os.environ.get("SWAP_LOADED_TTL_S", "2"))
_loaded_cache = {"at": 0.0, "value": None}


def loaded_model(timeout=5, fresh=False):
    """Report the resident model id, reusing a reading newer than LOADED_MODEL_TTL_S.

    Pollers (health checks, telemetry, fleet facts) call this several times a
    second; each call used to add an engine GET /v1/models, and that polling was
    itself a measurable CPU load on the rank that trips the thermal guard. The
    cache is invalidated whenever the swap state changes, so a lifecycle step
    never reads a stale identity.
    """
    now = time.monotonic()
    with lock:
        if not fresh and _loaded_cache["at"] and now - _loaded_cache["at"] < LOADED_MODEL_TTL_S:
            return _loaded_cache["value"]
    value = None
    try:
        status, payload = engine_request("GET", "/v1/models", timeout=timeout)
        if status == 200:
            value = payload["data"][0]["id"]
    except (OSError, ValueError, KeyError, IndexError):
        pass
    with lock:
        _loaded_cache["at"] = time.monotonic()
        _loaded_cache["value"] = value
    return value


def gate(profile):
    if loaded_model(timeout=8, fresh=True) != profile["served_model"]:
        return False
    try:
        status, payload = engine_request("POST", "/tokenize", {"model": profile["served_model"], "prompt": "OK"}, timeout=15)
        if status == 200 and isinstance(payload, dict):
            return payload.get("max_model_len") == profile["context_length"]
    except (OSError, ValueError):
        pass
    try:
        status, payload = engine_request("POST", "/v1/chat/completions", {
            "model": profile["served_model"], "messages": [{"role": "user", "content": "Reply OK"}],
            "max_tokens": 1, "temperature": 0,
        }, timeout=20)
        return status == 200 and bool(payload.get("choices"))
    except (OSError, ValueError):
        return False


def external_lifecycle():
    """Reject independently started recipe scripts; do not mistake our own process for one."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if any(Path(os.fsdecode(arg)).name in {"start.sh", "stop.sh", "start_tp2.sh", "cluster-mode"}
                   for arg in args if arg):
                return True
        except (OSError, PermissionError):
            continue
    return False


def run_command(profile, command, deadline):
    environment = os.environ.copy()
    environment.update(profile.get("env", {}))
    if profile.get("hf_token"):
        # The gated checkpoint needs the existing local token. Never put it in argv or logs.
        environment["HF_TOKEN"] = (HOME / ".cache/huggingface/token").read_text().strip()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("swap deadline reached")
    with ExitStack() as output_stack:
        try:
            output = output_stack.enter_context(LOG.open("a"))
        except OSError as failure:
            log(f"command log unavailable ({failure}); writing command output to /dev/null")
            output = subprocess.DEVNULL
        result = subprocess.run(command, check=False, cwd=profile["recipe_dir"], env=environment,
                                stdout=output, stderr=subprocess.STDOUT, timeout=remaining)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} exited {result.returncode}; inspect Spark cluster-swap.log")


# Per rank: ssh prefix and any local image that has python3, used only to delete core files from a
# stopped engine. Published copy: the image tags are this pair's local tags; use one you have on each node.
RANK_NODES = (
    ([], "vllm-fn-nfs:local"),
    (["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
      "-o", "ConnectTimeout=8", "user@192.0.2.2"], "tensorfold-glm53:v0.6.0"),
)


def engine_pids(profile):
    result = []
    container = profile["engine_container"]
    if container is None:
        log(f"engine cleanup identity unknown for profile {profile['id']}; skipping core cleanup")
        return result
    for prefix, image in RANK_NODES:
        try:
            pid = subprocess.check_output(
                prefix + ["docker", "inspect", "--format", "{{.State.Pid}}", container],
                text=True, timeout=15).strip()
            if pid.isdecimal() and int(pid) > 0:
                result.append((prefix, image, pid))
        except (OSError, subprocess.SubprocessError):
            log(f"engine PID unavailable on rank {len(result)}; core cleanup will be incomplete")
    return result


def cleanup_engine_cores(pids, since):
    # Only cores from this stop, bearing its exact container host PID.
    code = (
        "from pathlib import Path; import sys; "
        "pid=sys.argv[1]; since=float(sys.argv[2]); "
        "files=[p for p in Path('/cores').glob('core.*.'+pid+'.*') "
        "if p.stat().st_mtime >= since-5]; "
        "[(print('removing engine core',p.name,p.stat().st_size,flush=True),p.unlink()) for p in files]"
    )
    for prefix, image, pid in pids:
        command = [
            "docker", "run", "--rm", "--network", "none", "--entrypoint", "python3",
            "-v", "/var/lib/apport/coredump:/cores", image, "-c", code, pid, str(since),
        ]
        if prefix:
            command = prefix + [shlex.join(command)]
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f"engine core cleanup failed on PID {pid}: {result.stderr[-300:]}")
        if result.stdout:
            log(result.stdout.strip())


def stop_profile(profile, deadline):
    pids = engine_pids(profile)
    started = time.time()
    try:
        run_command(profile, profile["stop_command"], deadline)
    finally:
        cleanup_engine_cores(pids, started)


def launch(profile, deadline):
    run_command(profile, profile["start_command"], deadline)
    while time.monotonic() < deadline:
        if gate(profile):
            return
        time.sleep(5)
    raise TimeoutError(f"{profile['id']} failed the model and tokenize/completion gates before deadline")


def swap_worker(previous_id, target_id):
    previous, target = profiles[previous_id], profiles[target_id]
    deadline = time.monotonic() + target["est_swap_s"] * 3
    try:
        update_state(step=f"stopping {previous_id}")
        stop_profile(previous, deadline)
        update_state(step=f"starting {target_id}")
        run_command(target, target["start_command"], deadline)
        update_state(step="verifying")
        while time.monotonic() < deadline:
            if gate(target):
                update_state(current=target_id, state="idle", target=None, step=None,
                             finished_at=timestamp(), error=None)
                log(f"swap {previous_id} -> {target_id} passed gates")
                return
            time.sleep(5)
        raise TimeoutError(f"{target_id} verification timed out")
    except (OSError, RuntimeError, subprocess.SubprocessError) as failure:
        message = f"{type(failure).__name__}: {failure}"
        log(f"swap {previous_id} -> {target_id} failed: {message}")
        update_state(step="rolling back")
        try:
            # A failed target may have left partial rank containers; its own stop cleans both.
            stop_profile(target, time.monotonic() + previous["est_swap_s"] * 3)
            launch(previous, time.monotonic() + previous["est_swap_s"] * 3)
            log(f"rollback restored {previous_id}")
        except (OSError, RuntimeError, subprocess.SubprocessError) as rollback_failure:
            message += f"; rollback failed: {type(rollback_failure).__name__}: {rollback_failure}"
            log(message)
        update_state(current=previous_id, state="failed", target=None, step=None,
                     finished_at=timestamp(), error=message[:500])


def profile_status(include_profiles=True):
    with lock:
        payload = {"object": "cluster.profiles", **state}
    if include_profiles:
        payload["profiles"] = [
            {key: profile[key] for key in ("id", "display_name", "engine", "served_model",
                                             "context_length", "slots", "uncensored", "est_swap_s")}
            for profile in profiles.values()
            if not profile.get("hidden") or profile["id"] in {state["current"], state.get("target")}
        ]
    return payload


def watch_for_hangup(client, upstream, done):
    while not done.is_set():
        try:
            readable, _, _ = select.select([client], [], [], 1)
            if not readable:
                continue
            if client.recv(1, socket.MSG_PEEK):
                return
        except OSError:
            pass
        break
    else:
        return
    try:
        upstream.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # TCP_NODELAY on accepted sockets: a kept-alive reply's header and body sends otherwise wait ~40 ms for a delayed ACK
    disable_nagle_algorithm = True

    def log_message(self, *args):
        pass

    def respond(self, code, payload):
        # Early admission/controller replies may leave the request body unread.
        # Close instead of draining untrusted or oversized input before reuse.
        try:
            has_body = int(self.headers.get("Content-Length", "0")) != 0
        except ValueError:
            has_body = True
        if has_body or self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def error(self, code, kind, message):
        self.respond(code, {"error": {"type": kind, "message": message}})

    def proxy(self, body):
        global last_activity
        last_activity = time.time()
        connection = None
        started = False
        done = threading.Event()
        try:
            connection = _connection(int(os.environ.get("SWAP_READ_TIMEOUT_S", "1200")))
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in HOP_HEADERS | {"host", "x-cluster-swap-token"}}
            if body is not None:
                headers["Content-Length"] = str(len(body))
            connection.request(self.command, self.path, body=body, headers=headers)
            threading.Thread(target=watch_for_hangup,
                             args=(self.connection, connection.sock, done), daemon=True).start()
            response = connection.getresponse()
            self.send_response(response.status)
            has_length = False
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
                    has_length |= key.lower() == "content-length"
            if not has_length:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            started = True
            while chunk := response.read1(STREAM_CHUNK_SIZE):
                self.wfile.write(chunk)
                self.wfile.flush()
            if response.length not in (None, 0):
                raise http.client.IncompleteRead(b"", response.length)
            last_activity = time.time()
        except (OSError, http.client.HTTPException) as exc:
            self.close_connection = True
            if not started:
                self.error(502, "upstream_unavailable", str(exc))
            else:
                log(f"proxy stream ended early: {exc}")
        finally:
            done.set()
            if connection is not None:
                connection.close()

    def handle_request(self):
        path = self.path.split("?", 1)[0]
        if ((QUALIFICATION_HOLD.exists() or QUALIFICATION_HOLD.is_symlink())
                and path != "/health"
                and (path == "/swap" or not hmac.compare_digest(
                    self.headers.get("X-Cluster-Swap-Token", ""), token))):
            self.close_connection = True
            return self.error(503, "thermal_qualification",
                              "Normal traffic is held during monitored thermal qualification")
        if path in {"/swap/profiles", "/swap"}:
            if not hmac.compare_digest(self.headers.get("X-Cluster-Swap-Token", ""), token):
                return self.error(401, "unauthorized", "Cluster swap token required")
            refresh_registry()
            if path == "/swap/profiles" and self.command == "GET":
                return self.respond(200, profile_status())
            if path != "/swap" or self.command != "POST":
                return self.error(405, "method_not_allowed", "Unsupported swap method")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("Expected a JSON profile request")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get("profile"), str):
                    raise TypeError("profile must be a string")
                target_id = data["profile"]
            except (ValueError, TypeError, json.JSONDecodeError):
                return self.error(400, "bad_request", "Expected JSON with a string profile")
            with lock:
                if state["state"] == "swapping" or external_lifecycle():
                    return self.error(409, "swap_in_progress", "Another lifecycle operation is running")
                if target_id not in profiles:
                    return self.error(404, "unknown_profile", "Profile is not available")
                if target_id == state["current"] and loaded_model(fresh=True) == profiles[target_id]["served_model"]:
                    return self.error(409, "already_loaded", "Profile is already loaded")
                minimum_gib = int(os.environ.get("SWAP_MIN_FREE_GIB", "10"))
                free_bytes = os.statvfs(STATE_FILE.parent)
                if free_bytes.f_bavail * free_bytes.f_frsize < minimum_gib * (1 << 30):
                    return self.error(503, "storage_unavailable",
                                      f"Spark needs at least {minimum_gib} GiB available before a swap")
                previous = state["current"]
                old_state = state.copy()
                if not update_state(state="swapping", target=target_id, step=f"stopping {previous}",
                                    started_at=timestamp(), finished_at=None, error=None):
                    state.update(old_state)
                    return self.error(503, "storage_unavailable", "Could not persist swap intent")
                threading.Thread(target=swap_worker, args=(previous, target_id), daemon=True).start()
                return self.respond(202, profile_status(include_profiles=False))
        if path == "/health":
            with lock:
                current = state["current"]
                swapping = state["state"] == "swapping"
            loaded = loaded_model(timeout=1) if not swapping else None
            return self.respond(200, {"proxy": "ok", "vllm_up": bool(loaded), "loaded": loaded,
                                      "profile": current, "swap_state": state["state"],
                                      "upstream": f"http://{VLLM_HOST}:{VLLM_PORT}"})
        with lock:
            if state["state"] == "swapping":
                return self.error(503, "model_swapping", "Model hot swap in progress")
            current = profiles[state["current"]]
            # Hold lock only to identify the selected profile. Existing in-flight requests may finish.
        if loaded_model(fresh=True) != current["served_model"]:
            return self.error(503, "resident_unavailable", "Loaded model does not match the current profile")
        length = self.headers.get("Content-Length", "0")
        try:
            count = int(length)
            if count < 0 or count > 100_000_000:
                raise ValueError
        except ValueError:
            return self.error(400, "bad_request", "Invalid content length")
        body = self.rfile.read(count) if count else None
        if body:
            try:
                payload = json.loads(body)
                if isinstance(payload, dict) and "model" in payload:
                    known = {p["served_model"] for p in profiles.values()}
                    if payload["model"] in known:
                        payload["model"] = current["served_model"]
                        body = json.dumps(payload).encode()
            except (ValueError, TypeError):
                pass
        return self.proxy(body)

    do_GET = do_POST = handle_request


if __name__ == "__main__":
    token = TOKEN_FILE.read_text().strip()
    if not token:
        raise SystemExit("Empty controller token")
    profiles = parse_registry()
    initial = REGISTRY_FILE.stat()
    registry_mtime = (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if state["current"] not in profiles:
            raise SystemExit("Persisted profile is absent from registry")
        if state["state"] == "swapping":
            candidate = state.get("target")
            if candidate in profiles and gate(profiles[candidate]):
                update_state(current=candidate, state="idle", target=None, step=None,
                             finished_at=timestamp(), error=None)
            else:
                update_state(state="failed", target=None, step=None, finished_at=timestamp(),
                             error="Controller restarted during swap; inspect engine before retry")
    else:
        state = {"current": "dsv41-1017-tf", "state": "idle", "target": None, "step": None,
                 "started_at": None, "finished_at": None, "error": None}
        update_state()
    raw = os.environ.get("SWAP_LISTEN_HOSTS", "127.0.0.1,192.0.2.1")
    hosts = [host.strip() for host in raw.split(",") if host.strip() and host.strip() != "0.0.0.0"]
    if not hosts:
        raise SystemExit("No listener hosts")
    for host in hosts:
        server = ThreadingHTTPServer((host, LISTEN_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log(f"cluster-swap listening on {host}:{LISTEN_PORT}")
    threading.Event().wait()
