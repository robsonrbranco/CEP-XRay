"""Leitura dos `.sql` do extrato DNEC/Correios.

O molde (CNPJ-XRay) fatia bytes em linhas e joga no `pl.read_csv`. Aqui não há
CSV: há SQL. Cada arquivo é um `CREATE TABLE` seguido de centenas de milhares de
`INSERT INTO t (cols) VALUES (...),(...);`, com 10 linhas por statement.

Por que um tokenizador, e não as alternativas
---------------------------------------------
**Linha a linha** corromperia dado em silêncio: existem valores com quebra de
linha dentro das aspas. São só 2 nesta competência (ambas em
`NOME_LOGRADOURO_SA`, coluna que o schema descarta), mas a corrupção que elas
causariam só apareceria meses depois, como um logradouro de nome estranho.

**Um parser SQL de verdade** (`sqlparse` e afins) montaria uma AST completa de
307 MB para extrair tuplas de literais — ordens de grandeza de trabalho a mais
do que o problema pede.

**Executar os `.sql` no Firebird** desperdiçaria tudo o que `db/loader.py`
conquistou: 217 mil `INSERT` interpretados um a um, sem `EXECUTE BLOCK`, sem
controle de largura, e com o `CREATE TABLE` errado do `ESTADO_IBGE` no meio.

O que o tokenizador entrega de graça: NULL ≠ ''
----------------------------------------------
No molde, `pl.read_csv` lê um CSV e não tem como distinguir campo vazio de campo
ausente — a informação já se perdeu no formato. Aqui o DataFrame não vem de um
parser de texto, vem de tuplas Python que o tokenizador construiu, então `None`
vira null do polars e `""` vira string vazia, **sem sentinela e sem convenção**.

Medido nesta competência: `''` e `NULL` nunca coexistem na mesma coluna — `''`
só aparece em `tipo`/`nome_logradouro`, `NULL` só em `bairro_id`/`distrito_id`/
`cep_ativo`. Isso torna a regra `"" -> NULL` de `db/loader.py` hoje **sem
perda**, e `validation/qualidade.py` vigia a premissa: no dia em que uma coluna
tiver os dois, a regra vira lossy e o relatório avisa.

A ordem das alternativas do regex importa
-----------------------------------------
`cabeçalho` é testado antes de `número`. Sem isso, o `2022` de
`POPULACAO_RESIDENTE_2022` na lista de colunas viraria um valor. E `literal` é
testado antes de `NULL` e de `número`, então a palavra NULL ou um dígito dentro
de uma string nunca é confundida com token.

Nenhum valor é coletado antes do primeiro cabeçalho — é o que impede os
`VARCHAR(100)` do `CREATE TABLE` de virarem números, e é também o que imuniza
contra o defeito de `TBL_CEP_2610_ESTADO_IBGE.sql`, cujo `CREATE TABLE` declara
outra tabela, com outra lista de colunas. **As colunas vêm do `INSERT`.**

UTF-8 na fonte, WIN1252 no banco
--------------------------------
Medido sobre os 331 MB: de 1.585.108 caracteres não-ASCII, exatamente **2** não
cabem em WIN1252 — dois `U+0081` colados num `Á` em `bairro` id 37996
(`'Residencial Recanto das Á\\u0081guas'`). É resíduo de dupla codificação.

Política, em duas faixas e sem abortar nunca:

* **controles C1 (`U+0080`-`U+009F`) são REMOVIDOS**, não substituídos. Removê-los
  restaura `Águas`; trocá-los por `?` produziria `A?guas`. É a diferença entre
  consertar e estragar;
* qualquer outro caractere que o `cp1252` recuse vira `?`, contado **e listado
  por caractere distinto**, para a próxima competência decidir com informação.

Se um dia essa contagem saltar para milhares, a decisão volta à mesa — e o
número estará no relatório de qualidade para provocar isso.
"""

from __future__ import annotations

import argparse
import codecs
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import polars as pl

from ..db import schema

logger = logging.getLogger(__name__)

# Bytes lidos por vez. O buffer de texto cresce até fechar um token, então
# este número é piso, não teto.
BYTES_POR_CHUNK = 8 * 1024 * 1024

# Linhas por DataFrame entregue ao carregador. Em linhas, e não em bytes como
# no molde, porque aqui o pico de memória é dominado pela lista de tuplas
# Python, não pelo texto bruto.
LINHAS_POR_BLOCO = 50_000

# A ordem das alternativas é semântica, não estética. Ver o docstring.
TOKEN = re.compile(
    r"""
      (?P<cab> INSERT \s+ INTO \s+ (?P<tabela>\w+) \s* \( (?P<colunas>[^)]*) \) \s* VALUES )
    | (?P<txt> ' (?: [^'] | '' )* ' )
    | (?P<nulo> \bNULL\b )
    | (?P<num> -? \d+ (?: \. \d+ )? )
    """,
    re.X | re.I | re.S,
)

# Controles C1: não existem em WIN1252 e não representam texto.
_SEM_C1 = {c: None for c in range(0x80, 0xA0)}


@dataclass
class Limpeza:
    """O que foi mexido no texto, para o log e para o relatório de qualidade."""

    controles_removidos: int = 0
    substituidos: Counter = field(default_factory=Counter)

    @property
    def total_substituidos(self) -> int:
        return sum(self.substituidos.values())

    def __bool__(self) -> bool:
        return bool(self.controles_removidos or self.substituidos)

    def log(self, origem: str) -> None:
        if self.controles_removidos:
            logger.info(
                "%s: %d controle(s) C1 removido(s)", origem, self.controles_removidos
            )
        if self.substituidos:
            detalhe = ", ".join(
                f"U+{ord(c):04X}x{n}" for c, n in self.substituidos.most_common(10)
            )
            logger.warning(
                "%s: %d caractere(s) fora do WIN1252 trocados por '?' (%s)",
                origem,
                self.total_substituidos,
                detalhe,
            )


def _cabe_em_win1252(ch: str) -> bool:
    try:
        ch.encode("cp1252")
        return True
    except UnicodeEncodeError:
        return False


def _limpar(valor: str, limpeza: Limpeza) -> str:
    """Devolve o valor pronto para WIN1252.

    O caminho rápido é `isascii()`, que é uma checagem de flag no objeto str:
    a esmagadora maioria dos valores é ASCII puro e sai daqui sem tocar em mais
    nada.
    """
    if valor.isascii():
        return valor
    sem_c1 = valor.translate(_SEM_C1)
    if len(sem_c1) != len(valor):
        limpeza.controles_removidos += len(valor) - len(sem_c1)
    try:
        sem_c1.encode("cp1252")
    except UnicodeEncodeError:
        saida = []
        for ch in sem_c1:
            if _cabe_em_win1252(ch):
                saida.append(ch)
            else:
                limpeza.substituidos[ch] += 1
                saida.append("?")
        sem_c1 = "".join(saida)
    return sem_c1


def _valor(m: re.Match, limpeza: Limpeza) -> str | None:
    """Converte um token de valor no que vai para o DataFrame."""
    grupo = m.lastgroup
    if grupo == "nulo":
        return None
    if grupo == "num":
        return m.group()
    # Literal: tira as aspas externas e desdobra o escape '' -> '.
    bruto = m.group()[1:-1]
    if "''" in bruto:
        bruto = bruto.replace("''", "'")
    return _limpar(bruto, limpeza)


def _posicoes(colunas_fonte: list[str], tabela: str) -> tuple[list[int], list[str]]:
    """Quais posições da tupla guardar, e sob que nome.

    As colunas da fonte que não estão no schema são descartadas AQUI — é assim
    que as 12 colunas `_SA` e a `logradouro` derivada nunca chegam a virar
    objeto Python nem a trafegar para o banco.
    """
    mapa = schema.colunas_da_fonte(tabela)
    guardar, nomes = [], []
    for i, nome in enumerate(colunas_fonte):
        col = mapa.get(nome.strip().upper())
        if col is not None:
            guardar.append(i)
            nomes.append(col.name)
    faltando = set(mapa) - {c.strip().upper() for c in colunas_fonte}
    if faltando:
        raise ValueError(
            f"{tabela}: o schema espera coluna(s) que o INSERT não traz: "
            f"{', '.join(sorted(faltando))}"
        )
    return guardar, nomes


def blocos(
    origem: Path,
    linhas_por_bloco: int = LINHAS_POR_BLOCO,
    limpeza: Limpeza | None = None,
) -> Iterator[tuple[str, pl.DataFrame]]:
    """Percorre um `.sql` e entrega DataFrames prontos para `db/loader.py`.

    Cada item é `(tabela, df)`. Um bloco só fecha em fronteira de linha
    completa — nunca no meio de uma tupla.

    `limpeza` pode ser passada pelo chamador que queira os números do que foi
    mexido no texto — o pipeline os grava na proveniência.
    """
    limpeza = limpeza if limpeza is not None else Limpeza()
    decodificador = codecs.getincrementaldecoder("utf-8")()

    tabela: str | None = None
    guardar: list[int] = []
    nomes: list[str] = []
    n_fonte = 0

    valores: list[str | None] = []
    linhas: list[tuple] = []
    buf = ""

    def montar() -> pl.DataFrame:
        return pl.DataFrame(
            linhas,
            schema=[(n, pl.Utf8) for n in nomes],
            orient="row",
        )

    with open(origem, "rb") as fh:
        fim = False
        while not fim:
            bruto = fh.read(BYTES_POR_CHUNK)
            fim = not bruto
            buf += decodificador.decode(bruto, fim)

            achados = list(TOKEN.finditer(buf))
            if not fim and achados:
                # A última correspondência pode estar cortada pela fronteira do
                # chunk: um literal cujo fecha-aspas está no pedaço seguinte
                # pareceria completo, e um número cortado ao meio pareceria um
                # número menor. Ela é descartada e re-casada na próxima volta.
                achados.pop()

            consumido = achados[-1].end() if achados else 0

            for m in achados:
                if m.group("cab") is not None:
                    nova = schema.tabela_da_fonte(m.group("tabela"))
                    if nova is None:
                        raise ValueError(
                            f"{origem.name}: tabela desconhecida no INSERT: "
                            f"{m.group('tabela')}"
                        )
                    if nova != tabela:
                        if linhas:
                            yield tabela, montar()
                            linhas = []
                        if valores:
                            raise ValueError(
                                f"{origem.name}: {len(valores)} valor(es) sobrando "
                                f"ao trocar de tabela para {nova}"
                            )
                        colunas_fonte = m.group("colunas").split(",")
                        guardar, nomes = _posicoes(colunas_fonte, nova)
                        n_fonte = len(colunas_fonte)
                        tabela = nova
                    continue

                if tabela is None:
                    # Antes do primeiro INSERT só existe o CREATE TABLE, cujos
                    # VARCHAR(100) casariam como número.
                    continue

                valores.append(_valor(m, limpeza))
                if len(valores) == n_fonte:
                    linhas.append(tuple(valores[i] for i in guardar))
                    valores = []
                    if len(linhas) >= linhas_por_bloco:
                        yield tabela, montar()
                        linhas = []

            buf = buf[consumido:]

    if valores:
        raise ValueError(
            f"{origem.name}: {len(valores)} valor(es) sobrando no fim do arquivo "
            f"(esperado múltiplo de {n_fonte})"
        )
    if linhas:
        yield tabela, montar()
    limpeza.log(origem.name)


def contar(origem: Path) -> tuple[str | None, int]:
    """(tabela, linhas) de um arquivo, sem tocar no banco.

    É o checkpoint da etapa 1: falsificável contra as contagens conhecidas.
    """
    tabela, total = None, 0
    for t, df in blocos(origem):
        tabela = t
        total += df.height
    return tabela, total


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.etl.leitura",
        description="Lê os .sql da fonte e conta as linhas, sem Firebird.",
    )
    p.add_argument("origem", type=Path, help="arquivo .sql ou diretório com eles")
    p.add_argument("--log", default="INFO")
    args = p.parse_args()
    logging.basicConfig(
        level=args.log.upper(), format="%(levelname)s %(name)s: %(message)s"
    )

    arquivos = (
        sorted(args.origem.glob("*.sql"))
        if args.origem.is_dir()
        else [args.origem]
    )
    if not arquivos:
        print(f"nenhum .sql em {args.origem}")
        return 2

    total = 0
    print(f"{'arquivo':<40} {'tabela':<20} {'linhas':>10}")
    print("-" * 72)
    for f in arquivos:
        tabela, n = contar(f)
        total += n
        print(f"{f.name:<40} {tabela or '?':<20} {n:>10,}".replace(",", "."))
    print("-" * 72)
    print(f"{'TOTAL':<61} {total:>10,}".replace(",", "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
