#!/usr/bin/env python3
"""Start a Paseo product-sync chat only when one product needs attention."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = SKILL_DIR.parents[1]
DEFAULT_PYTHON = SKILL_DIR / ".venv" / "bin" / "python"
DEFAULT_MELI = SKILL_DIR / "scripts" / "meli.py"
DEFAULT_OUTCOME_MARKER = SKILL_DIR / "scripts" / "mark_workspace.py"
DEFAULT_SOURCE = SKILL_DIR / "work" / "source.json"
DEFAULT_RESULT = SKILL_DIR / "work" / "result.json"
DEFAULT_STATE = SKILL_DIR / "data" / "dispatch.json"
DEFAULT_SYNC_STATE = SKILL_DIR / "data" / "state.json"
DEFAULT_LOCK = SKILL_DIR / "data" / "dispatch.lock"

TERMINAL_AGENT_STATUSES = {
    "archived",
    "cancelled",
    "closed",
    "error",
    "failed",
    "idle",
    "stopped",
}


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{path.name} is not readable valid JSON") from error


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another dispatcher invocation is already running") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def command_output(
    command: list[str], *, detached: bool = False
) -> subprocess.CompletedProcess[str]:
    environment = None
    if detached:
        environment = os.environ.copy()
        environment.pop("PASEO_AGENT_ID", None)
        environment.pop("PASEO_AGENT_CWD", None)
    return subprocess.run(
        command,
        cwd=WORKSPACE_DIR,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def find_agent_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("agentId", "agent_id", "Id", "id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    for key in ("agent", "result", "data"):
        candidate = find_agent_id(value.get(key))
        if candidate:
            return candidate
    return None


def find_workspace_id(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("workspaceId", "workspace_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    for key in ("workspace", "result", "data"):
        candidate = find_workspace_id(value.get(key))
        if candidate:
            return candidate
    return None


def create_paseo_workspace(args: argparse.Namespace, title: str) -> str:
    created = command_output(
        [
            args.paseo_bin,
            "workspace",
            "create",
            "--isolation",
            "local",
            "--path",
            args.cwd,
            "--title",
            title,
            "--json",
        ],
        detached=True,
    )
    if created.returncode != 0:
        raise RuntimeError("Paseo could not create the product-sync workspace")
    try:
        payload = json.loads(created.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Paseo returned malformed workspace creation JSON") from error
    workspace_id = find_workspace_id(payload)
    if not workspace_id:
        raise RuntimeError("Paseo did not return the new workspace ID")
    return workspace_id


def previous_agent_is_active(paseo_bin: str, state_path: Path) -> tuple[bool, str | None]:
    state = read_json(state_path, {})
    agent_id = state.get("agent_id") if isinstance(state, dict) else None
    if not isinstance(agent_id, str) or not agent_id:
        return False, None

    inspected = command_output([paseo_bin, "inspect", agent_id, "--json"])
    if inspected.returncode != 0:
        raise RuntimeError("cannot inspect the previously dispatched Paseo agent")
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Paseo returned malformed agent status JSON") from error
    if bool(payload.get("Archived") or payload.get("archived")):
        return False, agent_id
    status = str(payload.get("Status") or payload.get("status") or "").lower()
    if not status:
        raise RuntimeError("Paseo agent status is missing")
    return status not in TERMINAL_AGENT_STATUSES, agent_id


def finalize_previous_workspace(args: argparse.Namespace, state_path: Path) -> None:
    state = read_json(state_path, {})
    if not isinstance(state, dict):
        return
    workspace_id = state.get("workspace_id")
    title = state.get("title")
    kind = state.get("kind")
    if not all(isinstance(value, str) and value for value in (workspace_id, title, kind)):
        return

    command = [
        args.python,
        str(Path(args.outcome_marker).resolve()),
        "--workspace-id",
        workspace_id,
        "--title",
        title,
        "--kind",
        kind,
        "--state",
        str(Path(args.sync_state).resolve()),
        "--result",
        str(Path(args.result).resolve()),
        "--paseo-bin",
        args.paseo_bin,
    ]
    shopify_id = state.get("shopify_id")
    if isinstance(shopify_id, str) and shopify_id:
        command.extend(["--shopify-id", shopify_id])
    marked = command_output(command, detached=True)
    if marked.returncode != 0:
        print(
            json.dumps(
                {
                    "outcome": "previous_workspace_rename_failed",
                    "workspace_id": workspace_id,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )


def start_paseo_agent(
    args: argparse.Namespace,
    *,
    title: str,
    prompt: str,
    kind: str,
    shopify_id: str | None = None,
) -> str:
    workspace_id = create_paseo_workspace(args, title)
    command = [
        args.paseo_bin,
        "run",
        "--background",
        "--json",
        "--title",
        title,
        "--provider",
        args.provider,
        "--workspace",
        workspace_id,
    ]
    agent_environment = {
        "PRODUCT_SYNC_PASEO_WORKSPACE_ID": workspace_id,
        "PRODUCT_SYNC_PRODUCT_TITLE": title,
        "PRODUCT_SYNC_SHOPIFY_ID": shopify_id or "",
        "PRODUCT_SYNC_DISPATCH_KIND": kind,
        "PRODUCT_SYNC_PASEO_BIN": args.paseo_bin,
    }
    for key, value in agent_environment.items():
        command.extend(["--env", f"{key}={value}"])
    if args.thinking:
        command.extend(["--thinking", args.thinking])
    if args.mode:
        command.extend(["--mode", args.mode])
    command.append(prompt)

    started = command_output(command, detached=True)
    if started.returncode != 0:
        command_output(
            [args.paseo_bin, "workspace", "rename", workspace_id, f"[F] {title}", "--json"],
            detached=True,
        )
        raise RuntimeError("Paseo could not start the product-sync chat")
    try:
        payload = json.loads(started.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Paseo returned malformed agent creation JSON") from error
    agent_id = find_agent_id(payload)
    if not agent_id:
        raise RuntimeError("Paseo did not return the new agent ID")
    atomic_json(
        Path(args.dispatch_state),
        {
            "agent_id": agent_id,
            "kind": kind,
            "shopify_id": shopify_id,
            "started_at": utc_now(),
            "title": title,
            "workspace_cwd": args.cwd,
            "workspace_id": workspace_id,
        },
    )
    return agent_id


def dispatch(args: argparse.Namespace) -> int:
    state_path = Path(args.dispatch_state).resolve()
    with exclusive_lock(Path(args.dispatch_lock).resolve()):
        active, agent_id = previous_agent_is_active(args.paseo_bin, state_path)
        if active:
            emit({"outcome": "agent_still_running", "agent_id": agent_id})
            return 0
        finalize_previous_workspace(args, state_path)

        checked = command_output(
            [args.python, str(Path(args.meli_script).resolve()), "next", "--output", args.source]
        )
        if checked.returncode != 0:
            prompt = (
                "The scheduled Shopify-to-Mercado-Libre preflight failed. Read "
                "prod/shopify-to-meli-one-by-one/work/result.json and diagnose the "
                "failure for the user in this Paseo chat. Do not publish a product, "
                "change credentials, edit code, or alter migration state. Before your "
                "final response, run /usr/bin/python3 "
                "prod/shopify-to-meli-one-by-one/scripts/mark_workspace.py so this "
                "workspace is labeled from the recorded outcome."
            )
            new_agent_id = start_paseo_agent(
                args,
                title="Shopify to Mercado Libre — preflight error",
                prompt=prompt,
                kind="preflight_error",
            )
            emit(
                {
                    "outcome": "error_chat_started",
                    "agent_id": new_agent_id,
                    "preflight_exit_code": checked.returncode,
                }
            )
            return 0

        source = read_json(Path(args.source).resolve(), None)
        if not isinstance(source, dict):
            raise RuntimeError("source.json does not contain an object")
        product = source.get("product")
        if product is None:
            emit({"outcome": "no-op", "reason": "no eligible unsynced product"})
            return 0
        if not isinstance(product, dict) or not product.get("id"):
            raise RuntimeError("source.json contains an invalid Shopify product")

        shopify_id = str(product["id"])
        product_title = " ".join(str(product.get("title") or "product").split())[:80]
        prompt = (
            "Open and follow prod/shopify-to-meli-one-by-one/SKILL.md. The cron "
            "dispatcher has selected a pending Shopify product. Run one production "
            "cycle now; begin with meli.py next so it safely resumes that selection. "
            "Publish at most one product, report the result in this Paseo chat, and "
            "stop. Before your final response, run /usr/bin/python3 "
            "prod/shopify-to-meli-one-by-one/scripts/mark_workspace.py. Do not edit "
            "code, skill files, credentials, or the stock-sync service."
        )
        new_agent_id = start_paseo_agent(
            args,
            title=product_title,
            prompt=prompt,
            kind="product_sync",
            shopify_id=shopify_id,
        )
        emit(
            {
                "outcome": "product_chat_started",
                "agent_id": new_agent_id,
                "shopify_id": shopify_id,
            }
        )
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cwd",
        default=str(WORKSPACE_DIR),
        help="Git folder used for each new standalone Paseo workspace",
    )
    parser.add_argument("--provider", required=True, help="Paseo provider/model")
    parser.add_argument("--thinking")
    parser.add_argument("--mode")
    parser.add_argument("--paseo-bin", default=os.environ.get("PASEO_BIN", "paseo"))
    parser.add_argument("--python", default=str(DEFAULT_PYTHON))
    parser.add_argument("--meli-script", default=str(DEFAULT_MELI))
    parser.add_argument("--outcome-marker", default=str(DEFAULT_OUTCOME_MARKER))
    parser.add_argument("--source", default=str(DEFAULT_SOURCE))
    parser.add_argument("--result", default=str(DEFAULT_RESULT))
    parser.add_argument("--sync-state", default=str(DEFAULT_SYNC_STATE))
    parser.add_argument("--dispatch-state", default=str(DEFAULT_STATE))
    parser.add_argument("--dispatch-lock", default=str(DEFAULT_LOCK))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args)
    except Exception as error:
        emit({"outcome": "dispatcher_failed", "reason": str(error)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
