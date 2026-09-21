"""Carga em massa para Firebird 3.0.

Estratégia e por quê
--------------------
A carga usa `EXECUTE BLOCK` com N INSERTs por bloco, um statement preparado uma
única vez, dentro de transações longas, com o banco em Force Write ASYNC (ver
connection.modo_carga) e sem nenhum índice ativo.

As medições abaixo vêm do CNPJ-XRay, de onde este módulo foi herdado. Elas são
sobre o engine, não sobre o dado — o que se mediu foi o custo de um round-trip
e o teto de contextos do Firebird 3.0.14, que não mudam por a tabela ser outra.
Servidor real, página de 16 KB, cache de 2 GB, Force Write OFF, 200 mil linhas:

    executemany com SQL string ......  3.526 linhas/s
    executemany com Statement ......   3.550 linhas/s
    EXECUTE BLOCK,  64 inserts .....   7.185 linhas/s
    EXECUTE BLOCK, 128 inserts .....   7.508 linhas/s
    EXECUTE BLOCK, 256 inserts .....   7.572 linhas/s   <- adotado

O Firebird 3.0 não tem a batch API do 4.0, então `executemany` é um laço
client-side: uma ida ao servidor por linha. O EXECUTE BLOCK amortiza isso em um
round-trip por bloco e dobra a taxa. Acima de 256 INSERTs o ganho satura, e o
texto do statement começa a esbarrar no limite de tamanho do Firebird.

Duas medições que contrariam a intuição e definem o desenho deste módulo:

* Preparar o statement, sozinho, não muda nada (3.526 -> 3.550). O gargalo não
  é a preparação.
* O tempo é 99% servidor: numa carga de 200 mil linhas, montar as tuplas em
  Python levou 0,2 s e o `execute` levou 26,4 s. Otimizar o lado cliente não
  paga.

E a mais importante: **paralelismo piora**. Com o mesmo volume dividido entre
várias conexões concorrentes na mesma tabela:

    1 conexão ....  7.324 linhas/s   1,00x
    2 conexões ...  5.507 linhas/s   0,75x
    4 conexões ...  2.521 linhas/s   0,34x
    8 conexões ...  2.294 linhas/s   0,31x

É contenção do SuperServer na mesma tabela. Por isso a carga aqui é
deliberadamente sequencial — a versão PostgreSQL deste ETL usava 10 conexões
concorrentes, o que em Firebird seria o pior caminho possível.
"""

import logging
import sys
import time
from decimal import Decimal, InvalidOperation

import polars as pl

from . import schema
from .config import FirebirdConfig, load_config

logger = logging.getLogger(__name__)

# INSERTs por EXECUTE BLOCK.
#
# O teto não é escolha de tuning: é o limite de CONTEXTOS do engine, que
# aparece como "Too many Contexts of Relation/Procedure/Views. Maximum allowed
# is 256". Um INSERT consome 1 contexto, então cabem 256 por bloco — medido
# por bisseção contra o Firebird 3.0.14. Mexer nesse número sem medir de novo
# quebra a preparação do statement.
INSERTS_POR_BLOCO = 256


class TruncationReport:
    """Conta valores que estouraram o VARCHAR, por coluna.

    Truncar e avisar é deliberado: abortar uma carga de dezenas de milhões de
    linhas por causa de um nome fora do tamanho medido seria pior. O relatório
    sai no log ao fim de cada tabela para que a largura seja corrigida no
    schema na próxima carga.
    """

    def __init__(self):
        self.por_coluna: dict[str, int] = {}

    def registrar(self, coluna: str, n: int) -> None:
        if n:
            self.por_coluna[coluna] = self.por_coluna.get(coluna, 0) + n

    def __bool__(self) -> bool:
        return bool(self.por_coluna)

    def log(self, tabela: str) -> None:
        for coluna, n in sorted(self.por_coluna.items(), key=lambda kv: -kv[1]):
            logger.warning(
                "%s.%s: %d valores truncados — aumente a largura em db/schema.py",
                tabela, coluna, n,
            )


def normalizar(
    df: pl.DataFrame, tabela: str, report: TruncationReport | None = None
) -> pl.DataFrame:
    """Converte um DataFrame de strings cruas nos tipos do schema.

    Espera o CSV lido com todas as colunas como Utf8 — é assim que o ETL lê,
    para não deixar o parser adivinhar tipo errado.
    """
    report = report if report is not None else TruncationReport()
    exprs = []

    for col in schema.columns(tabela):
        nome = col.name
        if nome not in df.columns:
            continue
        e = pl.col(nome)

        if col.kind == schema.TEXT:
            # A fonte traz valor com espaço nas pontas (' Canguera ',
            # ' Caucaia do Alto' em distrito). O aparo é contado pelo
            # relatório de qualidade.
            e = e.str.strip_chars()
            # "" vira NULL.
            #
            # É decisão declarada, não efeito colateral: medido nesta
            # competência, `''` e `NULL` NUNCA coexistem na mesma coluna — `''`
            # só aparece em logradouro.tipo/nome_logradouro (onde significa
            # "CEP geral de localidade, sem logradouro", que é ausência), e
            # `NULL` só em bairro_id/distrito_id/cep_ativo. Enquanto isso valer,
            # a regra é sem perda.
            #
            # `validation/qualidade.py` vigia a premissa: no dia em que uma
            # coluna tiver os dois, esta linha passa a perder informação e o
            # relatório avisa.
            e = pl.when(e.str.len_chars() == 0).then(None).otherwise(e)
            if col.max_len:
                excedentes = (
                    df.select(
                        pl.col(nome).str.strip_chars().str.len_chars() > col.max_len
                    ).to_series().sum()
                )
                report.registrar(nome, int(excedentes or 0))
                e = e.str.slice(0, col.max_len)

        elif col.kind == schema.INT:
            # strict=False: valor inválido/vazio vira NULL em vez de exceção.
            e = e.str.strip_chars().cast(pl.Int64, strict=False)

        elif col.kind == schema.NUMERIC:
            # Vírgula decimal -> ponto. Fica texto aqui e vira Decimal na
            # geração das tuplas, para não passar por float.
            #
            # Duas armadilhas medidas nas estatísticas do IBGE, as duas
            # resolvidas por este caminho sem tratamento especial:
            #   '-'    marcador de "sem dado" do IBGE, 1.508 ocorrências em
            #          cidade_ibge.mortalidade_infantil e outras 5 colunas
            #   '1,28' uma única linha de densidade_demografica com vírgula
            #          decimal, num arquivo que usa ponto em todo o resto
            # O '-' não vira Decimal e cai para NULL em `_linhas`; o '1,28'
            # vira '1.28' aqui.
            e = e.str.strip_chars().str.replace(",", ".", literal=True)
            e = pl.when(e.str.len_chars() == 0).then(None).otherwise(e)

        elif col.kind == schema.DOUBLE:
            # Latitude e longitude. Float64 é IEEE 754 de 64 bits, que é
            # exatamente o que DOUBLE PRECISION guarda no Firebird — a tupla
            # vai para o driver sem conversão intermediária e volta idêntica
            # ao que a fonte mandou. Ver a nota sobre FLOAT em db/schema.py.
            e = e.str.strip_chars().str.replace(",", ".", literal=True)
            e = e.cast(pl.Float64, strict=False)

        exprs.append(e.alias(nome))

    return df.with_columns(exprs)


def _linhas(df: pl.DataFrame, tabela: str):
    """Gera as tuplas de parâmetros na ordem das colunas do schema."""
    cols = schema.columns(tabela)
    nomes = [c.name for c in cols]
    numericas = {i for i, c in enumerate(cols) if c.kind == schema.NUMERIC}

    for linha in df.select(nomes).iter_rows():
        if not numericas:
            yield linha
            continue
        valores = list(linha)
        for i in numericas:
            v = valores[i]
            if v is None:
                continue
            try:
                valores[i] = Decimal(v)
            except (InvalidOperation, TypeError):
                valores[i] = None
        yield tuple(valores)


def execute_block_sql(tabela: str, n_linhas: int) -> str:
    """Monta um EXECUTE BLOCK que insere `n_linhas` de uma vez.

    Os parâmetros são declarados com `TYPE OF COLUMN`, então o próprio Firebird
    resolve o tipo de cada um a partir da tabela — não há tipo duplicado aqui e
    no schema.
    """
    cols = schema.columns(tabela)
    nomes = [c.name for c in cols]

    params: list[str] = []
    corpo: list[str] = []
    for i in range(n_linhas):
        ps = [f"p{i}_{j}" for j in range(len(cols))]
        params += [
            f"{p} TYPE OF COLUMN {tabela}.{c.name} = ?" for p, c in zip(ps, cols)
        ]
        valores = ", ".join(f":{p}" for p in ps)
        corpo.append(f"INSERT INTO {tabela} ({', '.join(nomes)}) VALUES ({valores});")

    return (
        "EXECUTE BLOCK (\n  "
        + ",\n  ".join(params)
        + "\n) AS BEGIN\n  "
        + "\n  ".join(corpo)
        + "\nEND"
    )


class Carregador:
    """Carrega blocos numa conexão, reaproveitando os statements preparados.

    Existe por causa do custo de preparar: no perfil, preparar o EXECUTE BLOCK
    levava 1,30s — 3,9% do tempo de um bloco de 60 mil linhas — e era refeito a
    cada bloco. Como um worker processa dezenas de blocos da mesma tabela, o
    statement é preparado uma vez por tabela e reaproveitado.

    Isso também é o que torna barato usar blocos menores: sem o cache, reduzir
    o bloco multiplicaria o custo de preparação. Com ele, o tamanho do bloco
    passa a ser só uma escolha de quanta memória o worker ocupa.

    Os statements são liberados no fecha(); a conexão também os libera ao
    fechar, mas depender disso deixaria o `free()` para o coletor de lixo.
    """

    def __init__(self, con, cfg: FirebirdConfig | None = None):
        self._con = con
        self._cfg = cfg or load_config()
        self._cur = con.cursor()
        # (tabela) -> (statement de bloco cheio, statement de uma linha)
        self._stmts: dict[str, tuple] = {}
        self._desde_commit = 0

    def _preparar(self, tabela: str) -> tuple:
        if tabela not in self._stmts:
            self._stmts[tabela] = (
                self._cur.prepare(execute_block_sql(tabela, INSERTS_POR_BLOCO)),
                # Statement separado para a sobra: um EXECUTE BLOCK tem número
                # fixo de INSERTs, então o resto que não fecha um bloco vai
                # linha a linha.
                self._cur.prepare(execute_block_sql(tabela, 1)),
            )
        return self._stmts[tabela]

    def carregar(
        self, df: pl.DataFrame, tabela: str, report: TruncationReport | None = None
    ) -> int:
        """Insere o DataFrame na tabela. Devolve o número de linhas gravadas.

        O DataFrame entra CRU, com todas as colunas Utf8 — a conversão de tipo
        é feita aqui dentro, e é o único lugar que a faz. Normalizar antes de
        chamar faria a segunda passada encontrar coluna já tipada e falhar com
        "expected String type, got: f64".

        `report` acumula os truncamentos entre blocos. Passando um, quem chama
        fica responsável pelo `.log()` e recebe um aviso por ARQUIVO; sem ele,
        cada bloco loga o seu, o que num arquivo de 1,7 M linhas seriam 35
        avisos repetidos da mesma coluna.
        """
        total = df.height
        if total == 0:
            return 0

        proprio = report is None
        report = report if report is not None else TruncationReport()
        df = normalizar(df, tabela, report)
        bloco_stmt, linha_stmt = self._preparar(tabela)

        gravadas = 0
        inicio = time.time()
        lote: list = []

        for linha in _linhas(df, tabela):
            lote.append(linha)
            if len(lote) == INSERTS_POR_BLOCO:
                self._cur.execute(bloco_stmt, [v for t in lote for v in t])
                gravadas += INSERTS_POR_BLOCO
                self._desde_commit += INSERTS_POR_BLOCO
                lote.clear()

                if self._desde_commit >= self._cfg.commit_every:
                    self._con.commit()
                    self._desde_commit = 0

                _progresso(tabela, gravadas, total, inicio)

        for linha in lote:
            self._cur.execute(linha_stmt, list(linha))
            gravadas += 1
            self._desde_commit += 1

        _progresso(tabela, gravadas, total, inicio, fim=True)
        if proprio:
            report.log(tabela)
        return gravadas

    def fechar(self) -> None:
        """Commita o que restou e libera os statements.

        O commit é condicional: um worker cuja fatia não pegou nenhum bloco
        nunca abriu transação, e commitar sem transação ativa levanta
        AttributeError no driver.
        """
        if self._con.main_transaction.is_active():
            self._con.commit()
        for stmts in self._stmts.values():
            for st in stmts:
                st.free()
        self._stmts.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.fechar()


def carregar(
    con,
    df: pl.DataFrame,
    tabela: str,
    cfg: FirebirdConfig | None = None,
) -> int:
    """Carrega um DataFrame avulso, preparando e liberando o statement.

    Conveniência para carga pequena e para teste. Quem carrega muitos blocos
    deve usar `Carregador`, que reaproveita a preparação.
    """
    with Carregador(con, cfg) as c:
        return c.carregar(df, tabela)


# Intervalo mínimo entre atualizações de progresso, em segundos.
#
# O "\r" só reescreve a linha num terminal; redirecionado para arquivo ou pipe,
# cada atualização vira uma linha nova. Sem esse limite, uma carga de 300 mil
# linhas produziu 204 KB de log só de progresso.
_INTERVALO_PROGRESSO = 1.0
_ultimo_progresso = 0.0


def _progresso(tabela: str, feitas: int, total: int, inicio: float, fim: bool = False) -> None:
    global _ultimo_progresso
    agora = time.time()
    if not fim and agora - _ultimo_progresso < _INTERVALO_PROGRESSO:
        return
    _ultimo_progresso = agora

    decorrido = max(agora - inicio, 1e-6)
    taxa = feitas / decorrido
    pct = (feitas * 100 / total) if total else 100.0

    if sys.stdout.isatty():
        fim_linha = "\n" if fim else ""
        print(
            f"\r{tabela} {pct:6.2f}%  {feitas:,}/{total:,}  {taxa:,.0f} linhas/s{fim_linha}",
            end="",
            flush=True,
        )
    elif fim:
        # Sem terminal, só o resultado final interessa no log.
        print(
            f"{tabela}: {feitas:,} linhas em {decorrido:,.1f}s ({taxa:,.0f} linhas/s)",
            flush=True,
        )
