#!/usr/bin/env python3
"""CLI de operação da troca blue-green.

    python -m src.blue_green.cli status     estado das bases
    python -m src.blue_green.cli validate   confere a base nova
    python -m src.blue_green.cli switch     valida e promove a produção
    python -m src.blue_green.cli cleanup    descarta a _old remanescente
"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.blue_green.state import StateManager  # noqa: E402
from src.blue_green.switch import BlueGreenSwitcher  # noqa: E402
from src.blue_green.validator import validar  # noqa: E402
from src.db.config import load_config  # noqa: E402

console = Console()


def _fmt(value) -> str:
    return str(value) if value is not None else "[dim]—[/dim]"


def cmd_status(_args) -> None:
    cfg = load_config()
    state = StateManager().read()

    console.print("\n[bold magenta]Blue-Green — Status[/bold magenta]\n")

    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("Base")
    t.add_column("Arquivo")
    t.add_column("Existe")
    for rotulo, caminho in (
        ("produção", cfg.database),
        ("nova (staging)", cfg.staging_database),
        ("anterior (old)", cfg.old_database),
    ):
        local = cfg.caminho_local(caminho)
        existe = local.exists()
        tamanho = f" ({local.stat().st_size / 1024**3:,.1f} GB)" if existe else ""
        t.add_row(
            rotulo,
            str(local),
            ("[green]sim[/green]" + tamanho) if existe else "[dim]não[/dim]",
        )
    console.print(t)

    active, staging = state.get("active"), state.get("staging")
    if not active and not staging:
        console.print("\n[yellow]Nenhuma troca registrada ainda.[/yellow]")
        return

    t2 = Table(show_header=True, header_style="bold cyan")
    t2.add_column("Campo")
    t2.add_column("Em produção", style="green")
    t2.add_column("Nova", style="yellow")
    for key, label in (
        ("source_month", "Competência"),
        ("downloaded_at", "Baixada em"),
        ("processed_at", "Processada em"),
        ("switched_at", "Promovida em"),
    ):
        t2.add_row(label, _fmt((active or {}).get(key)), _fmt((staging or {}).get(key)))
    console.print()
    console.print(t2)


def cmd_validate(_args) -> None:
    cfg = load_config()
    console.print(f"\n[bold]Validando {cfg.caminho_local(cfg.staging_database).name}...[/bold]\n")
    r = validar(cfg)

    if r.is_valid:
        t = Table(show_header=True, header_style="bold cyan")
        t.add_column("Tabela")
        t.add_column("Linhas", justify="right")
        for tabela, n in r.contagens.items():
            t.add_row(tabela, f"{n:,}")
        console.print(t)
        console.print(f"\n[bold green]OK — {r.summary}[/bold green]")
        sys.exit(0)

    console.print(f"[bold red]FALHOU — {r.summary}[/bold red]")
    sys.exit(1)


def cmd_switch(args) -> None:
    console.print("\n[bold]Promovendo a base nova para produção...[/bold]\n")
    r = BlueGreenSwitcher().switch(force=args.force)

    if r.success:
        console.print(f"[bold green]OK — {r.message}[/bold green]")
        if r.source_month:
            console.print(f"[green]   Competência: {r.source_month}[/green]")
        sys.exit(0)

    console.print(f"[bold red]FALHOU — {r.message}[/bold red]")
    sys.exit(1)


def cmd_cleanup(_args) -> None:
    console.print(f"\n[blue]{BlueGreenSwitcher().descartar_antiga()}[/blue]")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="blue_green", description="Troca blue-green da base CEP-XRay"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Estado das bases de produção, nova e anterior")
    sub.add_parser("validate", help="Confere a base nova antes da troca")
    p = sub.add_parser("switch", help="Valida e promove a base nova para produção")
    p.add_argument("--force", action="store_true", help="Pula a validação")
    sub.add_parser("cleanup", help="Descarta a base _old remanescente")

    args = parser.parse_args()
    {
        "status": cmd_status,
        "validate": cmd_validate,
        "switch": cmd_switch,
        "cleanup": cmd_cleanup,
    }[args.command](args)


if __name__ == "__main__":
    main()
