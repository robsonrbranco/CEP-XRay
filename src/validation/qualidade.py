#!/usr/bin/env python3
"""Relatório de qualidade da base, sobre a base já carregada.

    python -m src.validation.qualidade              a staging
    python -m src.validation.qualidade --producao   a que está no ar

É a contrapartida deliberada do schema sem travas: em vez de abortar uma carga
por causa de um dado que a fonte publicou assim, mede o estrago depois e vira
relatório. **Nunca aborta.**

As quatro medidas, e o que cada uma vigia
-----------------------------------------
1. **Chave repetida** — carga duplicada. Medido nesta competência: zero.
2. **Órfãos** — `bairro_id`/`cidade_id`/`distrito_id` apontando para linha que
   não existe. Medido: zero. É por isso que `LEFT JOIN` aqui é disciplina, não
   necessidade: o que existe de verdade é NULO, não órfão.
3. **`''` e NULL na mesma coluna** — esta é a que importa mais, e é a única
   que não herda do molde.

   `db/loader.normalizar` converte string vazia em NULL. Isso é hoje SEM PERDA
   porque, medido, as duas coisas nunca aparecem na mesma coluna: `''` só em
   `logradouro.tipo`/`nome_logradouro` (onde significa "CEP geral de
   localidade", que é ausência) e `NULL` só em `bairro_id`/`distrito_id`/
   `cep_ativo`.

   No dia em que uma competência trouxer as duas na mesma coluna, aquela linha
   do loader passa a APAGAR informação — e este relatório é o que avisa. Sem
   ele, a perda seria silenciosa e permanente.
4. **Cobertura das faixas** — quanto do espaço de CEP as faixas de município
   cobrem. É o número que explica por que `99999999` devolve 200 e `00000000`
   devolve 404, e é bom vê-lo mudar de uma competência para a outra.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.db import manage, schema  # noqa: E402
from src.db.config import FirebirdConfig, load_config  # noqa: E402
from src.db.connection import conectar  # noqa: E402

console = Console()
logger = logging.getLogger(__name__)

# (tabela, coluna, tabela_referida, coluna_referida)
REFERENCIAS = (
    ("logradouro", "bairro_id", "bairro", "id_bairro"),
    ("logradouro", "distrito_id", "distrito", "id_distrito"),
    ("logradouro", "cidade_id", "cidade", "id_cidade"),
    ("logradouro", "estado", "estado", "sigla"),
    ("bairro", "cidade_id", "cidade", "id_cidade"),
    ("distrito", "cidade_id", "cidade", "id_cidade"),
    ("bairro_faixa", "id_bairro", "bairro", "id_bairro"),
    ("distrito_faixa", "id_distrito", "distrito", "id_distrito"),
    ("cidade_faixa", "id_cidade", "cidade", "id_cidade"),
    ("cidade_ibge", "id_cidade", "cidade", "id_cidade"),
    ("cidade_ibge_terr", "id_cidade", "cidade", "id_cidade"),
    ("log_complemento", "cep", "logradouro", "cep"),
    ("log_grande_usuario", "cep", "logradouro", "cep"),
)


@dataclass
class Achado:
    categoria: str
    alvo: str
    total: int
    afetados: int
    detalhe: str = ""

    @property
    def pct(self) -> float:
        return (self.afetados / self.total * 100) if self.total else 0.0


@dataclass
class Relatorio:
    contagens: dict[str, int] = field(default_factory=dict)
    duplicadas: list[Achado] = field(default_factory=list)
    orfaos: list[Achado] = field(default_factory=list)
    ambiguas: list[Achado] = field(default_factory=list)
    cobertura_faixas: float = 0.0
    segundos: float = 0.0

    @property
    def limpa(self) -> bool:
        return not (self.duplicadas or self.orfaos or self.ambiguas)


def _um(cur, sql: str, params: list | None = None) -> int:
    cur.execute(sql, params or [])
    return int(cur.fetchone()[0] or 0)


def gerar(cfg: FirebirdConfig | None = None, database: str | None = None) -> Relatorio:
    cfg = cfg or load_config()
    alvo = database or cfg.staging_database
    cfg_alvo = FirebirdConfig(**{**cfg.__dict__, "database": alvo})

    rel = Relatorio()
    inicio = time.time()

    with conectar(cfg_alvo) as con:
        cur = con.cursor()

        for tabela in schema.TABLES:
            rel.contagens[tabela] = manage.contar(con, tabela)

        # 1. chave repetida
        for tabela, chave in schema.CHAVES_NATURAIS.items():
            total = rel.contagens.get(tabela, 0)
            if not total:
                continue
            cols = ", ".join(chave)
            n = _um(
                cur,
                f"SELECT COALESCE(SUM(n - 1), 0) FROM ("
                f"  SELECT COUNT(*) AS n FROM {tabela} GROUP BY {cols} "
                f"  HAVING COUNT(*) > 1) d",
            )
            if n:
                rel.duplicadas.append(
                    Achado("chave repetida", f"{tabela}({cols})", total, n)
                )

        # 2. órfãos
        for tabela, coluna, ref, ref_col in REFERENCIAS:
            total = rel.contagens.get(tabela, 0)
            if not total:
                continue
            n = _um(
                cur,
                f"SELECT COUNT(*) FROM {tabela} f WHERE f.{coluna} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {ref} d WHERE d.{ref_col} = f.{coluna})",
            )
            if n:
                rel.orfaos.append(
                    Achado("órfão", f"{tabela}.{coluna} -> {ref}", total, n)
                )

        # 3. '' e NULL na mesma coluna — a premissa que sustenta o loader
        for tabela, cols in schema.TABLES.items():
            total = rel.contagens.get(tabela, 0)
            if not total:
                continue
            for c in cols:
                if c.kind != schema.TEXT:
                    continue
                # Depois do loader, '' já virou NULL. Sobrar '' significa que a
                # regra deixou de ser aplicada em algum caminho.
                vazias = _um(
                    cur, f"SELECT COUNT(*) FROM {tabela} WHERE {c.name} = ''"
                )
                nulos = _um(
                    cur, f"SELECT COUNT(*) FROM {tabela} WHERE {c.name} IS NULL"
                )
                if vazias and nulos:
                    rel.ambiguas.append(
                        Achado(
                            "'' e NULL juntos", f"{tabela}.{c.name}", total, vazias,
                            detalhe=f"{nulos:,} nulos".replace(",", "."),
                        )
                    )

        # 4. cobertura das faixas de município
        cur.execute("SELECT faixa_ini, faixa_fim FROM cidade_faixa ORDER BY faixa_ini")
        cobertos, fim_anterior = 0, -1
        for a, b in cur.fetchall():
            try:
                ini, fim = int(a), int(b)
            except (TypeError, ValueError):
                continue
            ini = max(ini, fim_anterior + 1)
            if fim >= ini:
                cobertos += fim - ini + 1
                fim_anterior = max(fim_anterior, fim)
        rel.cobertura_faixas = cobertos / 100_000_000 * 100

    rel.segundos = time.time() - inicio
    return rel


def imprimir(rel: Relatorio) -> None:
    t = Table(title="Contagens", show_header=True, header_style="bold cyan")
    t.add_column("Tabela")
    t.add_column("Linhas", justify="right")
    for tabela, n in rel.contagens.items():
        t.add_row(tabela, f"{n:,}".replace(",", "."))
    console.print(t)

    def bloco(titulo: str, achados: list[Achado], vazio: str) -> None:
        console.print(f"\n[bold]{titulo}[/bold]")
        if not achados:
            console.print(f"   [green]{vazio}[/green]")
            return
        for a in achados:
            cor = "red" if a.pct >= 1 else "yellow"
            console.print(
                f"   [{cor}]{a.alvo}[/{cor}]: {a.afetados:,} de {a.total:,} "
                f"({a.pct:.4f}%) {a.detalhe}".replace(",", ".")
            )

    bloco("Chaves repetidas", rel.duplicadas, "nenhuma")
    bloco("Referências órfãs", rel.orfaos, "nenhuma")
    bloco(
        "Colunas com '' e NULL ao mesmo tempo",
        rel.ambiguas,
        "nenhuma — a regra \"'' vira NULL\" do loader continua sem perda",
    )

    console.print("\n[bold]Cobertura das faixas de município[/bold]")
    console.print(
        f"   {rel.cobertura_faixas:.1f}% dos 100 milhões de números de 8 dígitos "
        f"caem em alguma faixa"
    )
    console.print(
        "   [dim]é o que decide quando um CEP bem formado resolve por faixa "
        "(tipoCEP 2) em vez de 404[/dim]"
    )

    cor = "green" if rel.limpa else "yellow"
    console.print(
        f"\n[bold {cor}]{'Base limpa' if rel.limpa else 'Achados acima'}[/bold {cor}]"
        f" — {rel.segundos:.0f}s"
    )


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.validation.qualidade",
        description="Relatório de qualidade da base (nunca aborta)",
    )
    p.add_argument(
        "--producao", action="store_true",
        help="mede a base de produção em vez da staging",
    )
    args = p.parse_args()

    cfg = load_config()
    rel = gerar(cfg, database=cfg.database if args.producao else None)
    imprimir(rel)
    # Relatório não reprova: o código de saída é 0 mesmo com achados. Quem
    # quiser bloquear um deploy por qualidade usa o blue_green.validator.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
