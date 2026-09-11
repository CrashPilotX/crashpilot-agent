"""CrashPilot CLI: entry point."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import typer
import uvicorn
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(
    name="crashpilot",
    help="AI-powered Linux crash forensics",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@app.command()
def analyze(
    force: bool = typer.Option(False, "--force", "-f", help="Re-analyze even if already done"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    json_output: bool = typer.Option(False, "--json", help="Output raw JSON"),
) -> None:
    """[bold]Analyze[/bold] the previous boot for crashes and root cause."""
    _setup_logging(verbose)

    from .monitor import check_and_analyze

    console.print(Panel.fit(
        "[bold cyan]CrashPilot[/bold cyan]: AI Crash Forensics",
        subtitle="Analyzing previous boot...",
    ))

    report = asyncio.run(check_and_analyze(force=force))

    if report is None:
        console.print("[green]✓[/green] Previous boot ended cleanly: no crash detected.")
        return

    analysis = report.get("analysis", {})

    if json_output:
        import json
        console.print_json(json.dumps(report, default=str, indent=2))
        return

    # Pretty-print the report
    crash_type = report.get("crash_type", "unknown")
    severity = report.get("severity", "unknown")
    severity_color = {
        "critical": "red", "high": "orange3",
        "medium": "yellow", "low": "blue", "info": "dim",
    }.get(severity, "white")

    console.print()
    console.print(Panel(
        f"[bold]Crash Type:[/bold] [cyan]{crash_type}[/cyan]\n"
        f"[bold]Severity:[/bold] [{severity_color}]{severity.upper()}[/{severity_color}]\n"
        f"[bold]Time:[/bold] {report.get('crash_time', 'unknown')}\n"
        f"[bold]Report ID:[/bold] [dim]{report['id']}[/dim]",
        title="[bold red]⚠ Crash Detected[/bold red]",
        border_style=severity_color,
    ))

    if analysis.get("root_cause"):
        console.print()
        console.print(Panel(
            analysis["root_cause"],
            title="[bold]Root Cause[/bold]",
            border_style="cyan",
        ))

    if analysis.get("summary"):
        console.print()
        console.print(f"[bold]Summary:[/bold] {analysis['summary']}")

    confidence = analysis.get("confidence") or analysis.get("heuristic", {}).get("confidence", 0)
    console.print(f"\n[bold]Confidence:[/bold] {confidence:.0%}")

    # Evidence table
    evidence = analysis.get("evidence", [])
    if evidence:
        console.print()
        table = Table(title="Key Evidence", show_lines=True)
        table.add_column("Source", style="cyan", width=10)
        table.add_column("Finding", width=50)
        table.add_column("Weight", justify="center", width=8)
        for ev in evidence[:8]:
            weight = ev.get("weight", 0)
            table.add_row(
                ev.get("source", ""),
                ev.get("interpretation", ev.get("excerpt", ""))[:80],
                f"{weight:.0%}",
            )
        console.print(table)

    # Remediation
    remediation = analysis.get("remediation", [])
    if remediation:
        console.print()
        console.print("[bold]Remediation Steps:[/bold]")
        for step in sorted(remediation, key=lambda s: s.get("priority", 99)):
            p = step.get("priority", "?")
            console.print(f"  [{p}] [cyan]{step.get('action', '')}[/cyan]")
            if step.get("rationale"):
                console.print(f"      [dim]{step['rationale']}[/dim]")

    console.print()
    console.print("[dim]Report saved to database. Run [bold]sudo crashpilot serve[/bold] to view in browser.[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="API server host"),
    port: int = typer.Option(7878, "--port", help="API server port"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """[bold]Serve[/bold] the local API for the web dashboard."""
    _setup_logging(verbose)

    from .storage.store import init_db
    init_db()

    console.print(Panel.fit(
        f"[bold cyan]CrashPilot API[/bold cyan] running at "
        f"[link=http://{host}:{port}]http://{host}:{port}[/link]\n"
        f"Dashboard: [link=https://crashpilotx.com]"
        f"https://crashpilotx.com[/link]",
        title="CrashPilot Server",
    ))

    uvicorn.run(
        "crashpilot.api.server:app",
        host=host,
        port=port,
        log_level="info",
    )


@app.command()
def list_reports(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of reports to show"),
) -> None:
    """[bold]List[/bold] stored crash reports."""
    from .storage.store import init_db
    from .storage.store import list_reports as _list
    init_db()

    reports = _list(limit=limit)
    if not reports:
        console.print("[dim]No crash reports found.[/dim]")
        return

    table = Table(title=f"Crash Reports (last {len(reports)})", show_lines=False)
    table.add_column("ID", style="dim", width=16)
    table.add_column("Time", width=20)
    table.add_column("Type", style="cyan", width=20)
    table.add_column("Severity", width=10)
    table.add_column("AI", justify="center", width=5)
    table.add_column("Summary", width=40)

    for r in reports:
        severity = r.get("severity", "unknown")
        severity_color = {
            "critical": "red", "high": "orange3",
            "medium": "yellow", "low": "blue", "info": "dim",
        }.get(severity, "white")
        ai_icon = "✓" if r.get("ai_analyzed") else "-"
        analysis = r.get("analysis") or {}
        summary = analysis.get("root_cause") or r.get("summary") or ""
        table.add_row(
            r["id"][:14],
            (r.get("crash_time") or r.get("detected_at") or "")[:19],
            r.get("crash_type", "unknown"),
            f"[{severity_color}]{severity}[/{severity_color}]",
            ai_icon,
            summary[:50],
        )
    console.print(table)


@app.command()
def install_service() -> None:
    """[bold]Install[/bold] the CrashPilot systemd service (requires root)."""
    import shutil
    import subprocess

    # __file__ = <project>/agent/crashpilot/main.py  →  .parent×3 = <project>/
    service_src = Path(__file__).parent.parent.parent / "systemd" / "crashpilot.service"
    if not service_src.exists():
        console.print(f"[red]Service file not found at {service_src}[/red]")
        raise typer.Exit(1)

    dest = Path("/etc/systemd/system/crashpilot.service")
    try:
        shutil.copy(service_src, dest)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "crashpilot"], check=True)
        console.print("[green]✓[/green] CrashPilot service installed and enabled.")
        console.print("Start with: [cyan]sudo systemctl start crashpilot[/cyan]")
    except PermissionError as exc:
        console.print("[red]Permission denied: run with sudo[/red]")
        raise typer.Exit(1) from exc


@app.command()
def token(
    regenerate: bool = typer.Option(False, "--regenerate", "-r", help="Force a new token"),
    raw: bool = typer.Option(False, "--raw", help="Print bare token only (for scripting)"),
) -> None:
    """[bold]Show[/bold] connection status and the direct-mode API token."""
    from .api.server import _token_file, get_agent_token
    from .config import get_settings
    from .storage.store import init_db

    init_db()  # ensures data_dir exists
    cfg = get_settings()

    if regenerate:
        tf = _token_file()
        if tf.exists():
            tf.unlink()
        if not raw:
            console.print("[yellow]Regenerated direct-mode token: update any connected systems.[/yellow]\n")

    t = get_agent_token()

    # --raw: just print the token, nothing else (used by scripting)
    if raw:
        print(t)
        return

    dashboard = "https://crashpilotx.com"

    # ── Push mode (recommended) ──────────────────────────────────────────────
    push_configured = bool(cfg.supabase_url and cfg.supabase_system_id and cfg.supabase_token)

    if push_configured:
        console.print()
        console.print(Panel(
            f"[bold green]✓ Push mode is active[/bold green]\n\n"
            f"  System ID : [dim]{cfg.supabase_system_id}[/dim]\n"
            f"  Supabase  : [dim]{cfg.supabase_url}[/dim]\n\n"
            f"  The agent pushes heartbeats every 60 s and reports after each analysis.\n"
            f"  View your dashboard at [link={dashboard}]{dashboard}[/link]",
            title="[bold]CrashPilot Status[/bold]",
            border_style="green",
        ))
        console.print()
        console.print(
            "[dim]To send a heartbeat now: [/dim][cyan]sudo crashpilot heartbeat[/cyan]\n"
            "[dim]To run an analysis:       [/dim][cyan]sudo crashpilot analyze[/cyan]\n"
        )
        return

    # ── Push mode not configured: guide the user ────────────────────────────
    console.print()
    console.print(Panel(
        f"[bold yellow]Not connected yet[/bold yellow]\n\n"
        f"  CrashPilot connects outbound to the dashboard: no public URL or\n"
        f"  open ports needed.\n\n"
        f"  [bold]To connect:[/bold]\n"
        f"  1. Go to [link={dashboard}]{dashboard}[/link]\n"
        f"     Sign in → Systems → Add system → enter a name → Create system\n"
        f"  2. Run the one-line command it shows you, e.g.:\n"
        f"     [cyan]sudo crashpilot configure cpilot_<connection-string>[/cyan]",
        title="[bold]CrashPilot: Connect to Dashboard[/bold]",
        border_style="yellow",
    ))

    # ── Local API token (for the localhost dashboard / scripting) ─────────────
    console.print()
    console.print(
        "[dim]Local API token (only needed to query this agent's REST API on "
        f"http://127.0.0.1:{cfg.api_port} directly):[/dim]"
    )
    console.print(Panel(
        f"[bold cyan]{t}[/bold cyan]",
        title="[bold]Local API Token[/bold]",
        border_style="dim",
    ))


def _read_secret(value: str, env_name: str | None = None) -> str:
    """A secret from its argument, from standard input when the argument is
    "-", or else from the environment. The last two keep it off the command
    line, which every local user can read in /proc/<pid>/cmdline."""
    import os
    import sys

    if value.strip() == "-":
        return sys.stdin.readline().strip()
    return value or (os.environ.get(env_name, "") if env_name else "")


@app.command()
def configure(
    connection_string: str = typer.Argument("", help="Connection string from the CrashPilot dashboard (starts with cpilot_), or - to read it from standard input. Defaults to CRASHPILOT_CONNECT."),
) -> None:
    """[bold]Configure[/bold] push mode using the connection string from the dashboard."""
    import base64
    import json

    from . import config as cfg_mod
    from .cloud_push import push_heartbeat

    connection_string = _read_secret(connection_string, "CRASHPILOT_CONNECT")
    if not connection_string.strip():
        console.print(
            "[red]No connection string.[/red] Pass it, use [cyan]-[/cyan] to read it from standard "
            "input, or set CRASHPILOT_CONNECT."
        )
        raise typer.Exit(1)

    # Strip prefix
    raw = connection_string.strip()
    if raw.startswith("cpilot_"):
        raw = raw[len("cpilot_"):]

    try:
        decoded = base64.b64decode(raw + "==").decode()
        cfg_data = json.loads(decoded)
    except Exception as e:
        console.print(f"[red]Invalid connection string: {e}[/red]")
        raise typer.Exit(1) from e

    required = ("url", "key", "system_id", "token")
    missing = [k for k in required if not cfg_data.get(k)]
    if missing:
        console.print(f"[red]Connection string missing fields: {missing}[/red]")
        raise typer.Exit(1)

    # Every heartbeat and crash report goes to this URL - reject anything
    # that isn't https:// so a malformed or tampered connection string can't
    # downgrade the agent to sending telemetry in plaintext.
    if not str(cfg_data["url"]).startswith("https://"):
        console.print("[red]Connection string's Supabase URL must be https:// - refusing to configure a non-TLS endpoint.[/red]")
        raise typer.Exit(1)

    from .enrollment import env_file_for_write, write_env_values

    env_path = env_file_for_write(cfg_mod._find_env_file())
    try:
        write_env_values(env_path, {
            "CRASHPILOT_SUPABASE_URL": cfg_data["url"],
            "CRASHPILOT_SUPABASE_ANON_KEY": cfg_data["key"],
            "CRASHPILOT_SUPABASE_SYSTEM_ID": cfg_data["system_id"],
            "CRASHPILOT_SUPABASE_TOKEN": cfg_data["token"],
        })
    except (OSError, ValueError) as e:
        console.print(f"[red]Could not save credentials to {env_path}: {e}[/red]")
        raise typer.Exit(1) from e

    console.print(f"[green]✓[/green] Connected: credentials saved to {env_path}")

    # Reload settings so the heartbeat below picks up the new credentials.
    cfg_mod._settings = None

    timer_enabled = _enable_push_timers()

    # Send one heartbeat now so the system appears online immediately.
    cfg2 = cfg_mod.get_settings()
    try:
        asyncio.run(push_heartbeat(
            supabase_url=cfg2.supabase_url,
            anon_key=cfg2.supabase_anon_key,
            system_id=cfg2.supabase_system_id,
            agent_token=cfg2.supabase_token,
        ))
        console.print("[green]✓[/green] Heartbeat sent: your system is now online in the dashboard.")
    except Exception as e:
        console.print(f"[yellow]![/yellow] Connected, but the first heartbeat failed: {e}")

    if not timer_enabled:
        console.print()
        console.print(
            "[dim]Heartbeat timer not enabled automatically (no systemd?). "
            "Ensure something runs [/dim][cyan]crashpilot heartbeat[/cyan][dim] every ~60s "
            "to stay online.[/dim]"
        )


def _enable_push_timers() -> bool:
    """Enable the heartbeat and update timers, and the clean-shutdown sign-off.

    Best-effort: systemd may be absent (WSL without systemd, minimal images).
    Returns whether the heartbeat timer is running.
    """
    import shutil
    import subprocess

    if not shutil.which("systemctl"):
        return False
    try:
        subprocess.run(
            ["systemctl", "enable", "--now", "crashpilot-heartbeat.timer"],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    for unit in ("crashpilot-update.timer", "crashpilot-signoff.service"):
        try:
            subprocess.run(["systemctl", "enable", "--now", unit], check=False, capture_output=True)
        except OSError:
            pass
    return True


def _credentials_work(url: str, anon_key: str, system_id: str, agent_token: str) -> bool:
    """Whether the stored credentials are accepted right now."""
    import httpx

    from .config import InsecureSupabaseURL, require_https

    if not (url and anon_key and system_id and agent_token):
        return False
    try:
        resp = httpx.post(
            f"{require_https(url).rstrip('/')}/rest/v1/rpc/agent_system_status",
            headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
            json={"p_system_id": system_id, "p_agent_token": agent_token},
            timeout=10.0,
        )
    except (httpx.HTTPError, InsecureSupabaseURL):
        return False
    return resp.status_code == 200


def _state_file(name: str) -> Path:
    """A small state file in the data dir: "reenroll-attempt" (the automatic
    enrollment cooldown), "retired", "identity-check"."""
    from .config import get_settings

    return Path(get_settings().data_dir or ".") / name


def _retired_help(marker: Path) -> str:
    try:
        since = marker.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        since = ""
    return (
        f"This node was retired from the dashboard{f' ({since})' if since else ''}, so it no "
        "longer enrolls by itself. Restore it in the dashboard, then run "
        "`sudo crashpilot enroll` on this machine."
    )


def _mark_retired(marker: Path) -> None:
    from datetime import datetime, timezone

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n", encoding="utf-8")
    except OSError:
        pass


def _enroll_and_store(
    join_token: str, external_id: str, *, persist_token: bool, detected: str = "",
) -> dict:
    """Enroll with a join token and save the node's credentials. Raises on failure.

    ``external_id`` is an identity given to this command; ``detected`` one the
    caller has just detected (a machine copied from another's image).
    Otherwise the pinned identity is reused, and only a node without one
    detects it.
    """
    from . import config as cfg_mod
    from .cloud_push import _agent_version, _hostname
    from .enrollment import (
        enroll,
        env_file_for_write,
        identity_moved,
        parse_join_token,
        resolve_identity,
        write_env_values,
    )

    cfg = cfg_mod.get_settings()
    token = parse_join_token(join_token)
    if external_id.strip():
        identity, source = resolve_identity(external_id), "explicit"
    elif detected:
        identity, source = detected, "detected"
    elif cfg.external_id:
        # Enrolled before the source was recorded, or set in the environment:
        # treated as chosen, so it is never replaced by a detected one.
        identity, source = cfg.external_id, cfg.external_id_source or "explicit"
        if source == "detected":
            # A copy of another machine that got here (its credentials were
            # rejected, or `enroll --force`) must not enroll as that machine.
            identity = identity_moved(identity, cfg.node_name) or identity
    else:
        identity, source = resolve_identity("", cfg.node_name), "detected"
    env_path = env_file_for_write(cfg_mod._find_env_file())

    # Saved before trying, so a machine that cannot reach the dashboard yet
    # (cloud-init before the network is up) still enrolls on a later
    # heartbeat, and under the identity it was given.
    early: dict[str, str] = {}
    # Keep the join token only when it was handed to this command. One that
    # arrives through the environment (a Kubernetes Secret) stays there
    # rather than being copied onto the node's disk.
    if persist_token:
        early["CRASHPILOT_ENROLL_TOKEN"] = join_token.strip().strip("'\"")
    if external_id.strip():
        early.update({"CRASHPILOT_EXTERNAL_ID": identity, "CRASHPILOT_EXTERNAL_ID_SOURCE": source})
    if early:
        write_env_values(env_path, early)
        cfg_mod._settings = None

    result = enroll(token, identity, _hostname(), _agent_version())

    values = {
        "CRASHPILOT_SUPABASE_URL": token.url,
        "CRASHPILOT_SUPABASE_ANON_KEY": token.anon_key,
        "CRASHPILOT_SUPABASE_SYSTEM_ID": str(result["system_id"]),
        "CRASHPILOT_SUPABASE_TOKEN": str(result["agent_token"]),
        # Pin the identity this node enrolled under. Re-enrollment must use
        # exactly the same one: detected afresh, an explicit --external-id is
        # forgotten and a flaky metadata service changes the answer, and
        # either way the node enrolls as a brand-new system. That created
        # duplicates and walked straight past a retire-by-hand. Whether it
        # was detected decides if a copy of this machine may replace it.
        "CRASHPILOT_EXTERNAL_ID": identity,
        "CRASHPILOT_EXTERNAL_ID_SOURCE": source,
    }
    write_env_values(env_path, values)
    cfg_mod._settings = None
    _state_file("retired").unlink(missing_ok=True)
    return {**result, "identity": identity, "env_path": str(env_path)}


def _auto_enroll(join_token: str, *, detected: str = "") -> None:
    """Enroll from the heartbeat, which never does so for a node retired on
    purpose, and at most once per cooldown, so a revoked or expired token is
    not retried every minute. Exits 1 when it cannot."""
    from .enrollment import REENROLL_COOLDOWN_SECONDS, EnrollmentRetired, reenroll_allowed

    retired = _state_file("retired")
    if retired.exists():
        console.print(f"[red]✗ Not enrolling:[/red] {_retired_help(retired)}")
        raise typer.Exit(1)
    if not reenroll_allowed(_state_file("reenroll-attempt")):
        console.print(
            "[red]✗ Not enrolled:[/red] the last attempt was less than "
            f"{REENROLL_COOLDOWN_SECONDS // 60} minutes ago; the next attempt is on a later heartbeat."
        )
        raise typer.Exit(1)
    try:
        _enroll_and_store(join_token, "", persist_token=False, detected=detected)
    except EnrollmentRetired as e:
        _mark_retired(retired)
        console.print(f"[red]✗ Not enrolling:[/red] {_retired_help(retired)}")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]✗ Enrollment failed:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def enroll(
    join_token: str = typer.Argument("", help="Join token from the dashboard (starts with cpjoin_), or - to read it from standard input. Defaults to CRASHPILOT_ENROLL_TOKEN."),
    external_id: str = typer.Option("", "--external-id", help="Identity to enroll under. Detected automatically when omitted: Kubernetes node name, then cloud instance ID, then machine ID."),
    force: bool = typer.Option(False, "--force", help="Enroll even if this machine already has working credentials."),
) -> None:
    """[bold]Enroll[/bold] this machine with a join token, for fleets that add machines automatically."""
    from . import config as cfg_mod
    from .cloud_push import push_heartbeat
    from .enrollment import EnrollmentError, EnrollmentRetired, note_enroll_attempt

    cfg = cfg_mod.get_settings()
    # One read from standard input counts as handed to this command, so it is
    # saved like an argument (install.sh passes it that way).
    join_token = _read_secret(join_token)
    token_str = join_token or cfg.enroll_token
    if not token_str:
        console.print(
            "[red]No join token.[/red] Pass one ([cyan]crashpilot enroll cpjoin_...[/cyan], or "
            "[cyan]-[/cyan] to read it from standard input) or set CRASHPILOT_ENROLL_TOKEN. "
            "Create one on the dashboard's Systems page."
        )
        raise typer.Exit(1)

    if not force and _credentials_work(
        cfg.supabase_url, cfg.supabase_anon_key, cfg.supabase_system_id, cfg.supabase_token,
    ):
        console.print(f"[green]✓[/green] Already enrolled as system [dim]{cfg.supabase_system_id}[/dim]; nothing to do.")
        raise typer.Exit(0)

    # Run by hand, this is never held back by the automatic cooldown (and it
    # is how a retired node comes back), but it starts one, so a heartbeat
    # that finds the token saved below does not enroll alongside it.
    note_enroll_attempt(_state_file("reenroll-attempt"))
    try:
        result = _enroll_and_store(
            token_str, external_id,
            persist_token=bool(join_token) and join_token.strip() != cfg.enroll_token,
        )
    except EnrollmentRetired as e:
        _mark_retired(_state_file("retired"))
        console.print(f"[red]✗ Enrollment failed:[/red] {e}")
        raise typer.Exit(1) from e
    except EnrollmentError as e:
        console.print(f"[red]✗ Enrollment failed:[/red] {e}")
        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]✗ Enrollment failed:[/red] could not reach the dashboard: {e}")
        raise typer.Exit(1) from e

    console.print(
        f"[green]✓[/green] Enrolled as [bold]{result.get('name') or result['identity']}[/bold] "
        f"(identity [dim]{result['identity']}[/dim]); credentials saved to {result['env_path']}"
    )

    timer_enabled = _enable_push_timers()
    cfg2 = cfg_mod.get_settings()
    try:
        asyncio.run(push_heartbeat(
            supabase_url=cfg2.supabase_url,
            anon_key=cfg2.supabase_anon_key,
            system_id=cfg2.supabase_system_id,
            agent_token=cfg2.supabase_token,
        ))
        console.print("[green]✓[/green] Heartbeat sent: this machine is now online in the dashboard.")
    except Exception as e:
        console.print(f"[yellow]![/yellow] Enrolled, but the first heartbeat failed: {e}")
    if not timer_enabled:
        console.print(
            "[dim]Heartbeat timer not enabled automatically (no systemd?). Ensure something runs "
            "[/dim][cyan]crashpilot heartbeat[/cyan][dim] every ~60s to stay online.[/dim]"
        )


@app.command("sign-off")
def sign_off_command(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print nothing on success (used at shutdown)."),
) -> None:
    """[bold]Sign off[/bold]: tell the dashboard this machine is shutting down cleanly.

    Run at shutdown by crashpilot-signoff.service, and by the container
    entrypoint when a container or pod stops. The next heartbeat undoes it,
    so a reboot needs nothing extra. Always exits 0: a failed sign-off must
    never hold up a shutdown.
    """
    try:
        # Imports and settings inside the try too: a malformed .env must not
        # turn into a failed ExecStop.
        from .config import get_settings
        from .enrollment import sign_off

        cfg = get_settings()
        if not (cfg.supabase_url and cfg.supabase_anon_key and cfg.supabase_system_id and cfg.supabase_token):
            raise typer.Exit(0)
        sign_off(cfg.supabase_url, cfg.supabase_anon_key, cfg.supabase_system_id, cfg.supabase_token)
    except typer.Exit:
        raise
    except Exception as e:
        console.print(f"[yellow]![/yellow] Could not sign off: {e}")
        raise typer.Exit(0) from None
    if not quiet:
        console.print("[green]✓[/green] Signed off: the dashboard will expect this machine to be quiet.")


@app.command()
def heartbeat(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress success output (used by the systemd timer)"),
) -> None:
    """[bold]Send[/bold] a heartbeat to the CrashPilot cloud (called by the systemd timer)."""
    import asyncio
    import os

    from .config import get_settings

    cfg = get_settings()

    # A node given only a join token (a Kubernetes Secret, an image with
    # CRASHPILOT_ENROLL_TOKEN baked in) enrolls on its first heartbeat.
    if cfg.enroll_token and not (cfg.supabase_system_id and cfg.supabase_token):
        _auto_enroll(cfg.enroll_token)
        cfg = get_settings()
    elif (
        cfg.enroll_token and cfg.external_id and cfg.external_id_source == "detected"
        and "CRASHPILOT_EXTERNAL_ID" not in os.environ
    ):
        # A machine made from an image of an enrolled one carries its pinned
        # identity and credentials, and would report as that machine. Once
        # per boot, check the detected identity is still this machine's. A
        # copy that cannot enroll as itself (retired, cooling down, offline)
        # sends nothing rather than borrow the other machine's credentials.
        from .enrollment import copied_identity, current_boot_id

        moved, fresh = copied_identity(
            cfg.external_id, cfg.node_name, _state_file("identity-check"), current_boot_id(),
        )
        if moved:
            if fresh:
                # A cooldown recorded before the copy was made is not this
                # machine's; enroll as itself before its first heartbeat.
                _state_file("reenroll-attempt").unlink(missing_ok=True)
            console.print(
                f"[yellow]![/yellow] This machine is {moved}, not {cfg.external_id} as enrolled "
                "(copied from its image?); enrolling as itself."
            )
            _auto_enroll(cfg.enroll_token, detected=moved)
            cfg = get_settings()

    # Push mode requires url + anon_key + system_id + token. Tell the user exactly
    # what's missing instead of silently doing nothing.
    missing = [
        name for name, val in (
            ("CRASHPILOT_SUPABASE_URL", cfg.supabase_url),
            ("CRASHPILOT_SUPABASE_ANON_KEY", cfg.supabase_anon_key),
            ("CRASHPILOT_SUPABASE_SYSTEM_ID", cfg.supabase_system_id),
            ("CRASHPILOT_SUPABASE_TOKEN", cfg.supabase_token),
        ) if not val
    ]
    if missing:
        if not quiet:
            console.print(
                "[yellow]Push mode is not configured[/yellow]: missing: "
                + ", ".join(missing) + "\n"
                "Run [cyan]sudo crashpilot configure cpilot_<connection-string>[/cyan] "
                "(get the string from the dashboard → Systems → Add system → Push mode)."
            )
        # Exit 0 so the systemd timer treats an unconfigured agent as a no-op.
        raise typer.Exit(0)

    from .cloud_push import push_heartbeat, push_report
    from .storage.store import init_db, list_unpushed, mark_push_rejected, mark_pushed

    init_db()

    async def _heartbeat_and_backfill() -> int:
        await push_heartbeat(
            supabase_url=cfg.supabase_url,
            anon_key=cfg.supabase_anon_key,
            system_id=cfg.supabase_system_id,
            agent_token=cfg.supabase_token,
        )
        # Backfill: flush any reports that never reached the cloud (e.g. a crash
        # whose boot-time push failed before the network was up).
        #
        # list_unpushed() returns oldest-first, so stopping on ANY failure
        # (as this used to do) means one report the backend will never
        # accept - a payload that trips a Postgres constraint, an
        # oversized JSONB field - permanently starves every newer report
        # behind it: each cycle re-fetches the same oldest (broken) report
        # first, fails, and breaks, so the rest of the queue is never even
        # attempted again. Only a connection-level failure (backend
        # unreachable - every other report would fail identically too)
        # should stop the whole cycle; an HTTP 4xx means this specific
        # report's payload was rejected, so skip just that one and keep
        # going. Each refusal is counted, and a report refused
        # MAX_PUSH_REJECTIONS times is set aside rather than re-sent every
        # minute forever. Auth (401/403), a function missing while the
        # schema reloads (404), a timeout or a rate limit (408/429) would
        # refuse every report alike: about the server, not the report, so
        # they stop the cycle like a 5xx and are not counted.
        import httpx

        flushed = 0
        for rep in list_unpushed():
            try:
                await push_report(
                    supabase_url=cfg.supabase_url,
                    anon_key=cfg.supabase_anon_key,
                    system_id=cfg.supabase_system_id,
                    agent_token=cfg.supabase_token,
                    report=rep,
                )
                mark_pushed(rep["id"])
                flushed += 1
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if 400 <= status < 500 and status not in (401, 403, 404, 408, 429):
                    mark_push_rejected(rep["id"])
                    logging.getLogger(__name__).warning(
                        "Backfill push rejected for report %s (HTTP %d) - skipping it, "
                        "continuing with the rest of the queue: %s",
                        rep.get("id"), status, exc,
                    )
                    continue
                logging.getLogger(__name__).warning("Backfill push failed: %s", exc)
                break
            except Exception as exc:
                # Connection/timeout/unexpected errors likely affect every
                # remaining report too - stop and retry the whole queue
                # next cycle rather than burning through it on a dead link.
                logging.getLogger(__name__).warning("Backfill push failed: %s", exc)
                break
        if cfg.webhook_url:
            from .notifications import flush_webhook_deliveries

            await flush_webhook_deliveries(secret=cfg.webhook_secret)
        return flushed

    from .cloud_push import CredentialsRejected

    try:
        flushed = asyncio.run(_heartbeat_and_backfill())
    except CredentialsRejected as e:
        # A newer enrollment of this same identity, or a restore from the
        # dashboard, replaced these credentials. A node that has its join
        # token enrolls again, unless it was retired on purpose, and at most
        # once per cooldown so a node that keeps failing does not hammer the
        # enrollment endpoint.
        if not cfg.enroll_token:
            console.print(f"[red]✗ Heartbeat failed:[/red] {e}")
            raise typer.Exit(1) from e
        console.print("[yellow]![/yellow] Credentials were rejected.")
        _auto_enroll(cfg.enroll_token)
        cfg = get_settings()
        try:
            flushed = asyncio.run(_heartbeat_and_backfill())
        except Exception as e2:
            console.print(f"[red]✗ Heartbeat failed after enrolling again:[/red] {e2}")
            raise typer.Exit(1) from e2
    except Exception as e:
        # Always surface the reason: for manual runs and for `journalctl` when
        # the timer fires. Detailed text comes from cloud_push._explain_http_error.
        console.print(f"[red]✗ Heartbeat failed:[/red] {e}")
        raise typer.Exit(1) from e

    # Credentials that work mean the node is not retired (whatever restored
    # it, a configure or an enroll by hand).
    _state_file("retired").unlink(missing_ok=True)

    if not quiet:
        console.print(
            f"[green]✓ Heartbeat sent[/green]: system [dim]{cfg.supabase_system_id}[/dim] "
            "is now online in the dashboard."
        )
        if flushed:
            console.print(f"[green]✓ Backfilled {flushed} pending report(s) to the cloud.[/green]")


@app.command()
def snapshot(
    deep: bool = typer.Option(False, "--deep", help="Also measure top-level directory usage"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress success output"),
) -> None:
    """[bold]Record[/bold] a flight-recorder snapshot."""
    from .flight_recorder import record_snapshot

    try:
        result = record_snapshot(deep=deep)
    except Exception as exc:
        console.print(f"[red]✗ Snapshot failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    if not quiet:
        console.print(
            "[green]✓ Flight recorder snapshot saved[/green] "
            f"[dim]({result['captured_at']})[/dim]"
        )


@app.command("support-bundle")
def support_bundle(
    output: str = typer.Option(
        "crashpilot-support.tar.gz",
        "--output",
        "-o",
        help="Destination archive",
    ),
) -> None:
    """Create a sanitized support bundle with diagnostics and recent reports."""
    import importlib.metadata
    import io
    import json
    import tarfile

    from .config import get_settings
    from .flight_recorder import summarize_window
    from .redaction import redact_value
    from .storage.store import init_db, list_reports

    cfg = get_settings()
    init_db()
    payloads: dict[str, Any] = {
        "system.json": {
            "agent_version": importlib.metadata.version("crashpilot"),
            "data_dir": str(cfg.data_dir),
            "push_configured": bool(cfg.supabase_url and cfg.supabase_system_id),
            "system_id": cfg.supabase_system_id or None,
        },
        "flight-recorder.json": summarize_window(hours=24),
        "recent-reports.json": list_reports(limit=10),
    }
    # Snapshots stored by an older agent still hold full command lines.
    payloads, _ = redact_value(payloads)
    output_path = Path(output).expanduser().resolve()
    with tarfile.open(output_path, "w:gz") as archive:
        for name, payload in payloads.items():
            data = json.dumps(payload, indent=2, default=str).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    console.print(f"[green]✓ Sanitized support bundle created:[/green] {output_path}")


@app.command()
def update(
    force: bool = typer.Option(False, "--force", help="Reinstall even when the bundle is unchanged"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress unchanged/success output"),
) -> None:
    """[bold]Update[/bold] the agent from the verified CrashPilotX public bundle."""
    from .updater import install_latest

    try:
        result = install_latest(force=force)
    except Exception as exc:
        console.print(f"[red]✗ Agent update failed:[/red] {exc}")
        raise typer.Exit(1) from exc

    if quiet:
        return
    if result.get("packaged"):
        console.print(f"[dim]{result['message']}[/dim]")
    elif result["updated"]:
        console.print("[green]✓ CrashPilotX agent updated successfully.[/green]")
        console.print("[dim]The next heartbeat will use the updated agent code.[/dim]")
    else:
        console.print("[green]✓ CrashPilotX agent is already up to date.[/green]")


@app.command()
def doctor() -> None:
    """[bold]Diagnose[/bold] the agent setup and its connection to the dashboard."""
    import shutil
    import subprocess

    from . import config as cfg_mod
    from .cloud_push import push_heartbeat
    from .storage.store import count_reports, count_set_aside, count_unpushed, init_db

    init_db()
    cfg = cfg_mod.get_settings()

    problems = 0

    def report(label: str, status: str, detail: str = "", hint: str = "") -> None:
        nonlocal problems
        icon = {
            "ok":   "[green]✓[/green]",
            "warn": "[yellow]![/yellow]",
            "fail": "[red]✗[/red]",
        }[status]
        if status == "fail":
            problems += 1
        line = f"  {icon} {label}"
        if detail:
            line += f" [dim]- {detail}[/dim]"
        console.print(line)
        if hint:
            console.print(f"      [dim]{hint}[/dim]")

    console.print()
    console.print("[bold cyan]CrashPilot Doctor[/bold cyan]: checking your setup\n")

    # 1. Config file
    env_path = cfg_mod._find_env_file()
    if env_path.exists():
        report("Config file", "ok", str(env_path))
    else:
        report("Config file", "warn", "none found",
               "Run the install command or `sudo crashpilot configure cpilot_<string>`.")

    # 2. Anthropic API key (optional: heuristic analysis works without it)
    if cfg.anthropic_api_key:
        report("Anthropic API key", "ok", "set: AI analysis enabled")
    else:
        report("Anthropic API key", "warn", "not set",
               "Heuristic analysis still runs. Set CRASHPILOT_ANTHROPIC_API_KEY for AI root-cause.")

    # 3. Push mode credentials
    missing = [
        name for name, val in (
            ("CRASHPILOT_SUPABASE_URL", cfg.supabase_url),
            ("CRASHPILOT_SUPABASE_ANON_KEY", cfg.supabase_anon_key),
            ("CRASHPILOT_SUPABASE_SYSTEM_ID", cfg.supabase_system_id),
            ("CRASHPILOT_SUPABASE_TOKEN", cfg.supabase_token),
        ) if not val
    ]
    push_configured = not missing
    if push_configured and not cfg.supabase_url.lower().startswith("https://"):
        # Nothing is sent to a plaintext URL (cloud_push refuses it), so the
        # connection check below would only repeat this.
        push_configured = False
        report("Push mode configured", "fail", "CRASHPILOT_SUPABASE_URL is not https://",
               "Uploads are refused so the agent token is never sent in plaintext; use the "
               "https:// URL from the dashboard's connection string.")
    elif push_configured:
        report("Push mode configured", "ok", f"system {cfg.supabase_system_id}")
    else:
        report("Push mode configured", "fail", "missing: " + ", ".join(missing),
               "Create a system in the dashboard, then run the configure command it shows.")
    retired = _state_file("retired")
    if retired.exists():
        report("Enrollment", "fail", "retired from the dashboard: automatic enrollment is off",
               _retired_help(retired))

    # 4. Live connection to the dashboard (also validates schema/RPCs + token)
    if push_configured:
        try:
            asyncio.run(push_heartbeat(
                supabase_url=cfg.supabase_url,
                anon_key=cfg.supabase_anon_key,
                system_id=cfg.supabase_system_id,
                agent_token=cfg.supabase_token,
            ))
            report("Dashboard connection", "ok", "heartbeat delivered: system is online")
        except Exception as e:
            lines = str(e).split("\n")
            report("Dashboard connection", "fail", lines[0].strip(),
                   "\n      ".join(line.strip() for line in lines[1:]) or "")
    else:
        report("Dashboard connection", "warn", "skipped (push mode not configured)")

    # 5. systemd units (best-effort; absent in WSL or minimal Ubuntu environments)
    if shutil.which("systemctl"):
        def _systemctl(*args: str) -> str:
            try:
                return subprocess.run(
                    ["systemctl", *args], capture_output=True, text=True,
                ).stdout.strip()
            except OSError:
                return ""

        timer_state = _systemctl("is-active", "crashpilot-heartbeat.timer")
        if timer_state == "active":
            report("Heartbeat timer", "ok", "active (pings every ~60s)")
        else:
            report("Heartbeat timer", "fail", timer_state or "not found",
                   "Enable it: sudo systemctl enable --now crashpilot-heartbeat.timer")

        boot_state = _systemctl("is-enabled", "crashpilot.service")
        if boot_state == "enabled":
            report("Boot-time analysis", "ok", "enabled (runs once per boot)")
        else:
            report("Boot-time analysis", "warn", boot_state or "not found",
                   "Enable it: sudo systemctl enable crashpilot.service")

        update_state = _systemctl("is-active", "crashpilot-update.timer")
        if update_state == "active":
            report("Automatic updates", "ok", "hourly verified update check enabled")
        else:
            report("Automatic updates", "warn", update_state or "not found",
                   "Re-run the installer to enable crashpilot-update.timer.")

        snapshot_state = _systemctl("is-active", "crashpilot-snapshot.timer")
        if snapshot_state == "active":
            report("Flight recorder", "ok", "one-minute snapshots enabled")
        else:
            report("Flight recorder", "warn", snapshot_state or "not found",
                   "Re-run the installer to enable crashpilot-snapshot.timer.")
    else:
        report("systemd", "warn", "not available",
               "Ensure something runs `crashpilot heartbeat` every ~60s to stay online.")

    # 6. Stored reports + pending uploads (backfill queue)
    report("Local reports", "ok", f"{count_reports()} stored")
    pending = count_unpushed()
    if pending:
        report("Pending uploads", "warn", f"{pending} report(s) not yet in the cloud",
               "They retry on each heartbeat. Run `sudo crashpilot heartbeat` to flush now.")
    else:
        report("Pending uploads", "ok", "none: all reports delivered")
    set_aside = count_set_aside()
    if set_aside:
        report("Refused uploads", "warn", f"{set_aside} report(s) the dashboard kept refusing",
               "No longer retried; they stay on this machine (`crashpilot list-reports`).")

    console.print()
    if problems:
        console.print(
            f"[red]✗ {problems} problem(s) found.[/red] Fix the items marked "
            "[red]✗[/red] above, then re-run [cyan]sudo crashpilot doctor[/cyan]."
        )
        raise typer.Exit(1)
    console.print("[green]✓ All checks passed: CrashPilot is healthy.[/green]")


if __name__ == "__main__":
    app()
