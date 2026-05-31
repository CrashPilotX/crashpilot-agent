"""CrashPilot CLI — entry point."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

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
        "[bold cyan]CrashPilot[/bold cyan] — AI Crash Forensics",
        subtitle="Analyzing previous boot...",
    ))

    report = asyncio.run(check_and_analyze(force=force))

    if report is None:
        console.print("[green]✓[/green] Previous boot ended cleanly — no crash detected.")
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
        f"Dashboard: [link=https://kdigitalsystems.github.io/CrashPilot]"
        f"https://kdigitalsystems.github.io/CrashPilot[/link]",
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
        ai_icon = "✓" if r.get("ai_analyzed") else "–"
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
    except PermissionError:
        console.print("[red]Permission denied — run with sudo[/red]")
        raise typer.Exit(1)


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
            console.print("[yellow]Regenerated direct-mode token — update any connected systems.[/yellow]\n")

    t = get_agent_token()

    # --raw: just print the token, nothing else (used by scripting)
    if raw:
        print(t)
        return

    dashboard = "https://kdigitalsystems.github.io/CrashPilot"

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

    # ── Push mode not configured — guide the user ────────────────────────────
    console.print()
    console.print(Panel(
        f"[bold yellow]Push mode not configured yet[/bold yellow]\n\n"
        f"  Push mode is the recommended way to connect — no public URL or\n"
        f"  Cloudflare tunnel needed. The agent connects outbound to the cloud.\n\n"
        f"  [bold]To set it up:[/bold]\n"
        f"  1. Go to [link={dashboard}]{dashboard}[/link]\n"
        f"     Sign in → Systems → Add system → Push mode\n"
        f"  2. Copy the configure command and run it here:\n"
        f"     [cyan]sudo crashpilot configure cpilot_<connection-string>[/cyan]",
        title="[bold]CrashPilot — Connect to Dashboard[/bold]",
        border_style="yellow",
    ))

    # ── Direct mode info (secondary / advanced) ──────────────────────────────
    console.print()
    console.print("[dim]─── Direct mode (advanced — requires an HTTPS URL) ───────────────────[/dim]")
    console.print(
        "[dim]If you have a Cloudflare Tunnel or another HTTPS reverse proxy, you can\n"
        "use the bearer token below with [bold]Systems → Add system → Direct mode[/bold].[/dim]\n"
    )
    console.print(Panel(
        f"[bold cyan]{t}[/bold cyan]",
        title="[bold]Direct-mode API Token[/bold]",
        border_style="dim",
    ))

    if cfg.public_url:
        import urllib.parse
        encoded_url = urllib.parse.quote(cfg.public_url, safe="")
        deep_link = f"{dashboard}/#/add?url={encoded_url}&token={t}"
        console.print()
        console.print(Panel(
            f"[bold green]One-click add (HTTPS tunnel detected):[/bold green]\n\n"
            f"[link={deep_link}]{deep_link}[/link]",
            title="[bold]✨ One-click Setup[/bold]",
            border_style="green",
        ))
    else:
        console.print(
            "[dim]Agent URL (local network only — not usable from the cloud dashboard):\n"
            f"  http://<server-ip>:{cfg.api_port}[/dim]\n\n"
            "[dim]For direct mode to work from the internet, expose the agent over HTTPS\n"
            "(e.g. Cloudflare Tunnel) and re-run this command.[/dim]\n"
        )


@app.command()
def configure(
    connection_string: str = typer.Argument(..., help="Connection string from the CrashPilot dashboard (starts with cpilot_)"),
) -> None:
    """[bold]Configure[/bold] push mode using the connection string from the dashboard."""
    import base64
    import json
    import re

    # Strip prefix
    raw = connection_string.strip()
    if raw.startswith("cpilot_"):
        raw = raw[len("cpilot_"):]

    try:
        decoded = base64.b64decode(raw + "==").decode()
        cfg_data = json.loads(decoded)
    except Exception as e:
        console.print(f"[red]Invalid connection string: {e}[/red]")
        raise typer.Exit(1)

    required = ("url", "key", "system_id", "token")
    missing = [k for k in required if not cfg_data.get(k)]
    if missing:
        console.print(f"[red]Connection string missing fields: {missing}[/red]")
        raise typer.Exit(1)

    from .config import get_settings, _find_env_file
    import subprocess

    cfg = get_settings()
    env_path = _find_env_file()

    lines_to_add = {
        "CRASHPILOT_SUPABASE_URL": cfg_data["url"],
        "CRASHPILOT_SUPABASE_ANON_KEY": cfg_data["key"],
        "CRASHPILOT_SUPABASE_SYSTEM_ID": cfg_data["system_id"],
        "CRASHPILOT_SUPABASE_TOKEN": cfg_data["token"],
    }

    # Read existing .env
    existing = ""
    if env_path.exists():
        existing = env_path.read_text()

    new_lines = []
    for key, value in lines_to_add.items():
        pattern = re.compile(rf"^{key}=.*", re.MULTILINE)
        if pattern.search(existing):
            existing = pattern.sub(f"{key}={value}", existing)
        else:
            new_lines.append(f"{key}={value}")

    content = existing.rstrip("\n") + "\n" + "\n".join(new_lines) + "\n"
    env_path.write_text(content)

    console.print(f"[green]✓[/green] Push mode configured — credentials saved to {env_path}")
    console.print()
    console.print("[bold]Next:[/bold] Enable the heartbeat timer so CrashPilot stays online:")
    console.print("  [cyan]sudo systemctl enable --now crashpilot-heartbeat.timer[/cyan]")
    console.print()
    console.print("[dim]The agent will now push heartbeats and crash reports directly to the dashboard — no Cloudflare needed.[/dim]")


@app.command()
def heartbeat(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress success output (used by the systemd timer)"),
) -> None:
    """[bold]Send[/bold] a heartbeat to the CrashPilot cloud (called by the systemd timer)."""
    import asyncio
    from .config import get_settings

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
                "[yellow]Push mode is not configured[/yellow] — missing: "
                + ", ".join(missing) + "\n"
                "Run [cyan]sudo crashpilot configure cpilot_<connection-string>[/cyan] "
                "(get the string from the dashboard → Systems → Add system → Push mode)."
            )
        # Exit 0 so the systemd timer treats an unconfigured agent as a no-op.
        raise typer.Exit(0)

    from .cloud_push import push_heartbeat

    try:
        asyncio.run(push_heartbeat(
            supabase_url=cfg.supabase_url,
            anon_key=cfg.supabase_anon_key,
            system_id=cfg.supabase_system_id,
            agent_token=cfg.supabase_token,
        ))
    except Exception as e:
        # Always surface the reason — for manual runs and for `journalctl` when
        # the timer fires. Detailed text comes from cloud_push._explain_http_error.
        console.print(f"[red]✗ Heartbeat failed:[/red] {e}")
        raise typer.Exit(1)

    if not quiet:
        console.print(
            f"[green]✓ Heartbeat sent[/green] — system [dim]{cfg.supabase_system_id}[/dim] "
            "is now online in the dashboard."
        )


if __name__ == "__main__":
    app()
