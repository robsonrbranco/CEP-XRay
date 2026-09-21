#!/usr/bin/env python3
"""Constrói uma base CEP completa do zero e, opcionalmente, promove-a.

    python -m src.etl.pipeline                    constrói a base nova
    python -m src.etl.pipeline --switch           constrói e promove a produção
    python -m src.etl.pipeline --origem D:/sql    lê de outro diretório
    python -m src.etl.pipeline --continuar        retoma de onde parou

A base nunca é construída por cima da que está em produção: o ETL escreve em
`<nome>_staging.fdb`, e só a troca blue-green coloca a nova no lugar. A produção
continua respondendo durante a carga, e uma carga interrompida não deixa a
produção pela metade.

Ordem das fases, e o porquê de cada uma:

    1. base nova, do zero        arquivo anterior é descartado -- não há
                                 recarga parcial por cima de dado velho.
                                 É AQUI que USE_FULL é aplicado, antes de
                                 existir a primeira página de dado
    2. Force Write ASYNC         o engine para de forçar fsync por página
    3. tabelas SEM trava         sem PK/NOT NULL/FK: `distrito_id` é nulo em
                                 96% das linhas de logradouro, e um NOT NULL
                                 ingênuo mataria a carga
    4. carga em N processos      um arquivo por worker, maior primeiro --
                                 ver src/etl/carga
    5. índices                   agora, com as tabelas já povoadas, e só para
                                 performance de consulta, nunca como trava
    6. chave repetida            RELATÓRIO, não remoção: medido, a fonte não
                                 traz nenhuma. A checagem fica como guarda
                                 contra carregar o mesmo arquivo duas vezes
   6b. proveniência              competência, sha256 de cada .sql e a conta da
                                 limpeza de texto, gravados DENTRO da base
    7. SET STATISTICS            a seletividade gravada na criação do índice
                                 fica defasada depois de uma carga grande
    8. Force Write SYNC          devolve a durabilidade antes de virar produção
    9. validação                 nada é promovido sem estar completo
   10. troca + read-only         produção é base de consulta: o engine passa a
                                 recusar escrita

A numeração é a mesma do CNPJ-XRay de propósito, para os dois projetos se
lerem juntos. A única fase que muda de natureza é a 6.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
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
from src.db import connection, manage, schema  # noqa: E402
from src.db.config import FirebirdConfig, load_config  # noqa: E402
from src.etl import carga  # noqa: E402

console = Console()
logger = logging.getLogger("cep_xray.etl")

CHECKPOINT = _PROJECT_ROOT / "checkpoint.json"


# ---------------------------------------------------------------------------
# Checkpoint: quais tabelas já terminaram nesta construção
# ---------------------------------------------------------------------------

def ler_checkpoint() -> set[str]:
    if not CHECKPOINT.exists():
        return set()
    try:
        return set(json.loads(CHECKPOINT.read_text(encoding="utf-8")).get("prontas", []))
    except (json.JSONDecodeError, OSError):
        logger.warning("checkpoint.json ilegível — recomeçando do zero")
        return set()


def gravar_checkpoint(prontas: set[str]) -> None:
    CHECKPOINT.write_text(
        json.dumps({"prontas": sorted(prontas)}, indent=2), encoding="utf-8"
    )


def limpar_checkpoint() -> None:
    CHECKPOINT.unlink(missing_ok=True)


# ---------------------------------------------------------------------------

def _config_staging(cfg: FirebirdConfig) -> FirebirdConfig:
    return FirebirdConfig(**{**cfg.__dict__, "database": cfg.staging_database})


def _descartar_base_anterior(cfg_staging: FirebirdConfig) -> None:
    """Apaga o .fdb da staging para que a construção comece limpa."""
    arquivo = cfg_staging.caminho_local(cfg_staging.database)
    if not arquivo.exists():
        return
    from src.blue_green import manutencao

    console.print(f"[yellow]Descartando base anterior: {arquivo.name}[/yellow]")
    try:
        manutencao.desligar(cfg_staging, cfg_staging.database)
    except Exception as e:  # noqa: BLE001 — o arquivo pode nem estar registrado
        logger.debug("Não foi preciso desligar %s: %s", arquivo.name, e)
    arquivo.unlink()


def _conferir_chaves(con) -> dict[str, int]:
    """Fase 6: conta chave natural repetida, sem remover nada.

    O molde REMOVE aqui, porque a Receita publica chave repetida de verdade.
    Esta fonte não publica: medido, zero repetições em 14 tabelas. Então não há
    o que remover — e a contagem vira relatório, além de continuar alimentando
    a guarda do validador contra carga duplicada.
    """
    achados: dict[str, int] = {}
    cur = con.cursor()
    for tabela, chave in schema.CHAVES_NATURAIS.items():
        cols = ", ".join(chave)
        cur.execute(
            f"SELECT COUNT(*) FROM (SELECT {cols} FROM {tabela} "
            f"GROUP BY {cols} HAVING COUNT(*) > 1) d"
        )
        n = int(cur.fetchone()[0] or 0)
        if n:
            achados[tabela] = n
    return achados


def construir(
    cfg: FirebirdConfig,
    origem: Path,
    continuar: bool,
    processos: int,
    competencia: str | None = None,
) -> dict[str, int]:
    cfg_staging = _config_staging(cfg)
    arquivo = cfg_staging.caminho_local(cfg_staging.database)

    prontas: set[str] = set()
    if continuar and arquivo.exists():
        prontas = ler_checkpoint()
        if prontas:
            console.print(
                f"[yellow]Retomando — tabelas já carregadas: "
                f"{', '.join(sorted(prontas))}[/yellow]"
            )
    else:
        _descartar_base_anterior(cfg_staging)
        limpar_checkpoint()

    console.print("\n[bold]1. Base nova[/bold]")
    connection.create_if_not_exists(cfg_staging)
    console.print(f"   {arquivo}")

    console.print("\n[bold]2. Modo de carga[/bold]")
    connection.modo_carga(cfg_staging)

    por_tabela, _ = carga.mapear_arquivos(origem)
    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("Tabela")
    t.add_column("Arquivo")
    for tabela in carga.ORDEM:
        caminho = por_tabela.get(tabela)
        t.add_row(tabela, caminho.name if caminho else "[red]ausente[/red]")
    console.print()
    console.print(t)

    faltando = [tab for tab in schema.TABLES if tab not in por_tabela]
    if faltando:
        console.print(
            f"[bold red]Sem arquivo para: {', '.join(faltando)} — "
            f"confira --origem ({origem})[/bold red]"
        )
        raise SystemExit(2)

    console.print("\n[bold]3. Tabelas (sem índice)[/bold]")
    inicio = time.time()
    with connection.conectar(cfg_staging) as con:
        manage.criar_tabelas(con, preservar=prontas)

    def concluiu(tabela: str, linhas: int) -> None:
        prontas.add(tabela)
        gravar_checkpoint(prontas)
        console.print(f"   [green]{tabela}[/green] {linhas:,} linhas")

    console.print(f"\n[bold]4. Carga ({processos} processos)[/bold]\n")
    # O pool trabalha sem a conexão do pai: as tabelas já existem e os índices
    # vêm depois. Cada worker abre a sua própria conexão.
    resultado = carga.carregar_tudo_paralelo(
        por_tabela, cfg_staging, processos, pular=prontas, ao_concluir=concluiu
    )

    with connection.conectar(cfg_staging) as con:
        console.print("\n[bold]5. Índices[/bold]")
        ti = time.time()
        manage.criar_indices(con)
        console.print(f"   {len(schema.INDEXES)} índices em {time.time() - ti:.1f}s")

        # Depois dos índices: a busca por chave repetida agrupa a tabela
        # inteira, e o índice a torna viável.
        console.print("\n[bold]6. Chaves repetidas na fonte (relatório)[/bold]")
        repetidas = _conferir_chaves(con)
        if repetidas:
            for tab, n in repetidas.items():
                console.print(f"   [yellow]{tab}[/yellow]: {n:,} chave(s) repetida(s)")
        else:
            console.print("   [green]nenhuma[/green]")

        totais = {tab: manage.contar(con, tab) for tab in schema.TABLES}

        # Proveniência, gravada DENTRO da base.
        #
        # O manifesto de publicação é um arquivo ao lado, que pode se perder na
        # cópia para o volume. A base viaja com a própria identidade.
        console.print("\n[bold]6b. Proveniência[/bold]")
        hashes = {
            f"sha256_{tab}": carga.sha256(caminho)
            for tab, caminho in sorted(por_tabela.items())
        }
        manage.gravar_metadados(con, {
            "competencia": competencia or "?",
            "construida_em": datetime.now(timezone.utc)
                             .replace(microsecond=0).isoformat(),
            "origem": str(origem),
            "linhas_total": sum(totais.values()),
            "controles_removidos": resultado.controles_removidos,
            "caracteres_substituidos": resultado.substituidos,
            **{f"linhas_{tab}": n for tab, n in totais.items()},
            **hashes,
        })
        console.print(
            f"   competência [cyan]{competencia or '?'}[/cyan], "
            f"{sum(totais.values()):,} linhas, "
            f"{len(hashes)} hash(es) de origem"
        )
        if resultado.controles_removidos or resultado.substituidos:
            console.print(
                f"   [dim]limpeza de texto: {resultado.controles_removidos} "
                f"controle(s) C1 removido(s), {resultado.substituidos} "
                f"caractere(s) substituído(s)[/dim]"
            )

    console.print("\n[bold]7. Estatísticas dos índices[/bold]")
    connection.estatisticas(cfg_staging)

    console.print("\n[bold]8. Modo de produção[/bold]")
    connection.modo_producao(cfg_staging)

    carga_s = time.time() - inicio
    console.print(
        f"\n[bold green]Base construída em {carga_s / 60:.1f} min[/bold green]"
    )
    tamanho = arquivo.stat().st_size if arquivo.exists() else 0
    if tamanho:
        console.print(
            f"[bold]{arquivo.name}: {tamanho / 1024 / 1024:.1f} MB[/bold] "
            f"({tamanho / max(sum(totais.values()), 1):.0f} bytes por linha)"
        )
    # As contagens vêm do banco, não do somatório dos workers: é o que de fato
    # ficou gravado, e não o que o ETL acha que gravou.
    return totais


def main() -> None:
    p = argparse.ArgumentParser(
        prog="pipeline", description="Constrói uma base CEP completa no Firebird"
    )
    p.add_argument(
        "--origem",
        default=os.getenv("OUTPUT_FILES_PATH", "./db/firebird-querys"),
        help="diretório com os TBL_CEP_<AAMM>_*.sql",
    )
    p.add_argument(
        "--switch", action="store_true",
        help="promove a base nova para produção ao final",
    )
    p.add_argument(
        "--continuar", action="store_true",
        help="retoma uma construção interrompida em vez de recomeçar",
    )
    p.add_argument(
        "--processos", type=int, default=int(os.getenv("ETL_PROCESSOS", "4")),
        help="workers paralelos, um arquivo cada (padrão 4)",
    )
    p.add_argument(
        "--competencia", default=None,
        help="sobrepõe a competência lida do nome dos arquivos (AAAA-MM)",
    )
    p.add_argument("--log", default=os.getenv("LOG_LEVEL", "INFO"))
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    origem = Path(args.origem)
    if not origem.is_dir():
        console.print(f"[bold red]--origem não é um diretório: {origem}[/bold red]")
        raise SystemExit(2)

    try:
        _, competencia = carga.mapear_arquivos(origem)
    except ValueError as e:
        console.print(f"[bold red]{e}[/bold red]")
        raise SystemExit(2) from e
    competencia = args.competencia or competencia

    cfg = load_config()
    if not cfg.database:
        console.print("[bold red]DB_NAME não configurado no .env[/bold red]")
        raise SystemExit(2)

    console.print("\n[bold magenta]CEP-XRay — construção da base[/bold magenta]\n")
    console.print(cfg.describe())
    console.print(f"origem ...... {origem}")
    console.print(f"competência . {competencia or '(não identificada)'}")
    console.print(f"processos ... {args.processos}")

    sm = StateManager()
    if competencia:
        sm.update_staging_downloaded(
            source_month=competencia, database=cfg.staging_database
        )

    inicio = time.time()
    totais = construir(cfg, origem, args.continuar, args.processos, competencia)

    t = Table(title="Base construída", show_header=True, header_style="bold cyan")
    t.add_column("Tabela")
    t.add_column("Linhas", justify="right")
    for tabela, n in totais.items():
        t.add_row(tabela, f"{n:,}")
    console.print()
    console.print(t)

    console.print("\n[bold]9. Validação[/bold]")
    r = validar(cfg)
    if not r.is_valid:
        console.print(f"[bold red]{r.summary}[/bold red]")
        raise SystemExit(1)
    console.print(f"[green]{r.summary}[/green]")

    limpar_checkpoint()
    sm.update_staging_processed()

    if args.switch:
        console.print("\n[bold]10. Troca para produção[/bold]")
        res = BlueGreenSwitcher(cfg, sm).switch()
        cor = "green" if res.success else "red"
        console.print(f"[bold {cor}]{res.message}[/bold {cor}]")
        if not res.success:
            raise SystemExit(1)

        # A base de produção é base de consulta: read-only no header faz o
        # engine recusar qualquer escrita, em vez de isso ser só um combinado
        # operacional que um UPDATE acidental quebra.
        console.print("\n[bold]11. Produção em somente leitura[/bold]")
        connection.modo_somente_leitura(cfg)
        console.print("   [green]o engine passa a recusar qualquer escrita[/green]")
    else:
        console.print(
            "\n[dim]Para promover: python -m src.blue_green.cli switch[/dim]"
        )

    console.print(
        f"\n[bold green]Concluído em {(time.time() - inicio) / 60:.1f} min[/bold green]"
    )


if __name__ == "__main__":
    # Obrigatório no Windows: o pool usa 'spawn', e cada filho reimporta este
    # módulo. Sem a guarda, cada filho chamaria main() de novo.
    import multiprocessing as mp

    mp.freeze_support()
    main()
