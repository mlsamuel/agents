"""
cli.py - Support agent CLI interface.

Exposes backend support tools as CLI commands with structured JSON output,
so AI agents can invoke them via subprocess — no protocol server required.

Usage:
    python cli.py <namespace> <command> [--flags]

Namespaces:
    crm      Customer record operations
    orders   Order management
    tickets  Ticket operations
    comms    Customer communication
    kb       Knowledge base search

Examples:
    python cli.py crm lookup-customer --keyword "Jane Smith"
    python cli.py orders check-status --order-ref ORD-00123456
    python cli.py kb search --query "refund policy" --category billing
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import click
from dotenv import load_dotenv

import store
from tool_registry import BY_NAMESPACE as _NAMESPACE_METHODS, BY_NS_FN as _NS_FN_TO_CLI
from tools import (
    check_order_status,
    create_ticket,
    escalate_to_human,
    get_ticket_history,
    lookup_customer,
    process_refund,
    search_agent_guidelines,
    search_knowledge_base,
    send_reply,
)

load_dotenv(Path(__file__).parent / ".env")

_SANDBOX_RUNNER = Path(__file__).parent / "sandbox_runner.py"
_DOCKER_IMAGE   = "python:3.12-slim"


def _dispatch_to_cli(ns: str, fn: str, kwargs: dict) -> dict:
    """Dispatch a sandboxed __CALL__ by invoking cli.py as a subprocess."""
    mapping = _NS_FN_TO_CLI.get((ns, fn))
    if not mapping:
        return {"__error__": f"Tool {ns}.{fn} not available"}
    cli_ns, cli_cmd, param_map = mapping
    cmd = [sys.executable, str(Path(__file__).resolve()), cli_ns, cli_cmd]
    for py_param, cli_flag in param_map.items():
        value = kwargs.get(py_param)
        if value is not None and value != "":
            cmd += [f"--{cli_flag}", str(value)]
    result = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
    try:
        return json.loads(result.stdout) if result.stdout.strip() else {"__error__": result.stderr.strip()}
    except Exception:
        return {"__error__": result.stderr.strip() or "CLI returned non-JSON"}


def _run_code_docker(code: str, allowed_tools: list[str], timeout: int) -> dict:
    """Run code in an isolated Docker container, dispatching tool calls through cli.py."""
    code_b64 = base64.b64encode(code.encode()).decode()
    name = f"sandbox-{uuid.uuid4().hex[:8]}"

    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--network", "none",
        "--memory", "128m",
        "--cpus", "0.5",
        "--read-only",
        "--tmpfs", "/tmp:size=32m,noexec",
        "-e", f"SANDBOX_CODE={code_b64}",
        "-e", f"ALLOWED_TOOLS={json.dumps(allowed_tools)}",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "PYTHONUNBUFFERED=1",
        "-e", f"NAMESPACE_METHODS={json.dumps(_NAMESPACE_METHODS)}",
        "-v", f"{_SANDBOX_RUNNER.resolve()}:/runner.py:ro",
        _DOCKER_IMAGE,
        "python", "/runner.py",
    ]

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return {"stdout": "", "error": "docker executable not found", "exit_code": -1}

    output_parts: list[str] = []
    timed_out = False

    def _kill_container():
        nonlocal timed_out
        timed_out = True
        subprocess.run(["docker", "kill", name], capture_output=True)

    timer = threading.Timer(timeout, _kill_container)
    timer.start()

    try:
        while True:
            raw_line = proc.stdout.readline()
            if not raw_line:
                break
            line = raw_line.decode(errors="replace")
            if line.startswith("__CALL__:"):
                try:
                    call = json.loads(line[9:])
                    result = _dispatch_to_cli(call["ns"], call["fn"], call["kwargs"])
                except Exception as exc:
                    result = {"__error__": str(exc)}
                proc.stdin.write(f"__RESULT__:{json.dumps(result)}\n".encode())
                proc.stdin.flush()
            else:
                output_parts.append(line)
    except BrokenPipeError:
        pass
    finally:
        timer.cancel()
        try:
            proc.stdin.close()
        except Exception:
            pass

    proc.wait()
    exit_code = proc.returncode
    stderr_text = proc.stderr.read().decode(errors="replace").strip()

    if timed_out:
        error: str | None = f"Code execution timed out after {timeout}s"
        exit_code = -1
    elif exit_code != 0:
        error = stderr_text or f"Process exited with code {exit_code}"
    else:
        error = None

    return {"stdout": "".join(output_parts)[:8192], "error": error, "exit_code": exit_code}


def _out(data) -> None:
    """Print result as JSON to stdout."""
    print(json.dumps(data, default=str))


def _err(message: str) -> None:
    """Print error as JSON to stdout and exit non-zero."""
    print(json.dumps({"error": message}))
    sys.exit(1)


# ── root ───────────────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Support agent CLI — structured JSON output for every command."""


# ── crm ───────────────────────────────────────────────────────────────────────

@cli.group()
def crm():
    """Customer record operations."""


@crm.command("lookup-customer")
@click.option("--keyword", required=True, help="Name or subject keyword to search for")
def crm_lookup(keyword):
    """Look up a customer by name or keyword."""
    try:
        _out(lookup_customer(keyword))
    except Exception as exc:
        _err(str(exc))


@crm.command("ticket-history")
@click.option("--customer-id", required=True, help="Customer ID (e.g. CUST-12345)")
def crm_history(customer_id):
    """Retrieve the last 3 support tickets for a customer."""
    try:
        _out(get_ticket_history(customer_id))
    except Exception as exc:
        _err(str(exc))


# ── orders ────────────────────────────────────────────────────────────────────

@cli.group()
def orders():
    """Order management operations."""


@orders.command("check-status")
@click.option("--order-ref", required=True, help="Order reference number or keyword")
def orders_check(order_ref):
    """Look up the status of an order."""
    try:
        _out(check_order_status(order_ref))
    except Exception as exc:
        _err(str(exc))


@orders.command("process-refund")
@click.option("--order-ref", required=True, help="Order reference number")
@click.option("--reason",    required=True, help="Reason for the refund")
def orders_refund(order_ref, reason):
    """Initiate a refund for an order."""
    try:
        _out(process_refund(order_ref, reason))
    except Exception as exc:
        _err(str(exc))


# ── tickets ───────────────────────────────────────────────────────────────────

@cli.group()
def tickets():
    """Ticket operations."""


@tickets.command("create")
@click.option("--subject",  required=True, help="Ticket subject line")
@click.option("--body",     required=True, help="Ticket body / description")
@click.option("--queue",    required=True, help="Support queue (e.g. billing, technical)")
@click.option("--priority", required=True, help="Priority level (low, medium, high, urgent)")
@click.option("--type",     "ticket_type", required=True, help="Ticket type (e.g. Incident, Question)")
def tickets_create(subject, body, queue, priority, ticket_type):
    """Create a new support ticket."""
    try:
        _out(create_ticket(subject, body, queue, priority, ticket_type))
    except Exception as exc:
        _err(str(exc))


# ── comms ─────────────────────────────────────────────────────────────────────

@cli.group()
def comms():
    """Customer communication operations."""


@comms.command("send-reply")
@click.option("--message",   required=True, help="Reply message to send to the customer")
@click.option("--ticket-id", default="",   help="Associated ticket ID (optional)")
def comms_reply(message, ticket_id):
    """Send a reply to the customer."""
    try:
        _out(send_reply(message, ticket_id))
    except Exception as exc:
        _err(str(exc))


@comms.command("escalate")
@click.option("--ticket-id", required=True, help="Ticket ID to escalate")
@click.option("--reason",    required=True, help="Reason for escalation")
def comms_escalate(ticket_id, reason):
    """Escalate a ticket to a human agent."""
    try:
        _out(escalate_to_human(ticket_id, reason))
    except Exception as exc:
        _err(str(exc))


# ── kb ────────────────────────────────────────────────────────────────────────

@cli.group("kb")
def kb_group():
    """Knowledge base and guidelines search."""


@kb_group.command("search")
@click.option("--query",    required=True, help="Customer question or topic")
@click.option("--category", default="",   help="Filter: billing, returns, technical, general")
@click.option("--top-k",    default=3,    type=int, help="Max results (default 3)")
def kb_search(query, category, top_k):
    """Search the support knowledge base."""
    async def _run():
        await store.get_pool()
        return await search_knowledge_base(query, category, top_k)
    try:
        _out(asyncio.run(_run()))
    except Exception as exc:
        _err(str(exc))


@kb_group.command("guidelines")
@click.option("--query",    required=True, help="Description of the current customer situation")
@click.option("--category", default="",   help="Filter: billing, returns, technical, general")
def kb_guidelines(query, category):
    """Search agent handling guidelines."""
    async def _run():
        await store.get_pool()
        return await search_agent_guidelines(query, category)
    try:
        _out(asyncio.run(_run()))
    except Exception as exc:
        _err(str(exc))


# ── code ──────────────────────────────────────────────────────────────────────

@cli.group("code")
def code_group():
    """Execute agent-generated Python in an isolated Docker sandbox."""


@code_group.command("run")
@click.option("--code-b64",      required=True, help="Base64-encoded Python code to execute")
@click.option("--allowed-tools", default="[]",  help="JSON list of tool namespaces to expose")
@click.option("--timeout",       default=10, type=int, help="Max execution seconds (default 10)")
def code_run(code_b64, allowed_tools, timeout):
    """Run Python code in Docker with access to CLI tool namespaces.

    Tool calls from the sandboxed code are dispatched through cli.py commands,
    so the same CLI interface used by agents is used inside the sandbox too.
    """
    try:
        code    = base64.b64decode(code_b64).decode()
        allowed = json.loads(allowed_tools)
        timeout = max(1, min(timeout, 30))
        _out(_run_code_docker(code, allowed, timeout))
    except Exception as exc:
        _err(str(exc))


if __name__ == "__main__":
    cli()
