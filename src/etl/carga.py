"""Orquestra a carga dos 15 arquivos da fonte no Firebird.

Paralelismo por ARQUIVO, e não por bloco
----------------------------------------
No CNPJ-XRay cada worker relê o arquivo inteiro e fica com um bloco a cada N.
Aquilo é barato lá porque ler e parsear o CSV consome 0,6% do tempo — a conta
é dominada pelo `execute`.

Aqui não é. Tokenizar 307 MB de SQL em Python é trabalho de CPU comparável ao
`execute`, e reler o arquivo em N workers multiplicaria o parsing por N em vez
de dividi-lo. Então cada worker pega um arquivo inteiro.

E o volume é outro: 2,17 milhões de linhas contra 222 milhões. A carga inteira
leva poucos minutos; o paralelismo aqui compra minutos, não horas.

As tarefas saem ordenadas por tamanho decrescente, então `logradouro` (307 MB,
1,72 M linhas) começa no primeiro worker livre enquanto os 14 arquivos pequenos
passam pelos outros. Isso dá as duas coisas: melhor tempo de parede, e erro de
ambiente aparecendo em segundos — porque os arquivos pequenos terminam antes de
`logradouro` chegar na primeira transação.

Se um dia `logradouro` sozinho passar de ~10 min, o passo seguinte é fatiá-lo
por deslocamento de bytes, cortando em `\\nINSERT INTO ` — fronteira não ambígua,
porque nenhum literal da fonte contém quebra de linha seguida dessa palavra.
Não construir isso antes de medir.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing as mp
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..db import schema
from ..db.config import FirebirdConfig, load_config

logger = logging.getLogger(__name__)

# TBL_CEP_2610_LOGRADOURO.sql -> ('2610', 'LOGRADOURO')
ARQUIVO = re.compile(r"^TBL_CEP_(\d{4})_(.+)\.sql$", re.I)

# Ordem de referência, do menor para o maior. Usada no relatório de cobertura;
# o despacho paralelo usa tamanho de arquivo (ver docstring).
ORDEM = (
    "estado",
    "estado_faixa",
    "estado_ibge",
    "paises_ibge",
    "cidade",
    "cidade_faixa",
    "cidade_ibge",
    "cidade_ibge_terr",
    "distrito",
    "distrito_faixa",
    "log_grande_usuario",
    "bairro",
    "bairro_faixa",
    "log_complemento",
    "logradouro",
)


def competencia_iso(token: str) -> str:
    """'2610' -> '2026-10'.

    A convenção do e-DNE é AAMM, confirmada por vários importadores
    independentes (`1611` = nov/2016, `1701` = jan/2017). Bate com o resto: o
    `.FDB` fornecido junto chama-se `202609` e o header dele diz que foi criado
    em 2026-09-16, ou seja, estes `.sql` são a competência seguinte, publicada
    com antecedência.

    O século é fixado em 20xx: a base de CEP não existia em 19xx neste formato,
    e a alternativa (janela deslizante) inventaria uma regra sem dado que a
    sustente.
    """
    if not re.fullmatch(r"\d{4}", token):
        raise ValueError(f"token de competência fora do formato AAMM: {token!r}")
    ano, mes = int(token[:2]), int(token[2:])
    if not 1 <= mes <= 12:
        raise ValueError(f"mês inválido no token de competência: {token!r}")
    return f"20{ano:02d}-{mes:02d}"


def mapear_arquivos(diretorio: Path) -> tuple[dict[str, Path], str | None]:
    """Mapeia tabela -> arquivo, e devolve a competência encontrada.

    Falha se houver mais de um token de competência no mesmo diretório.
    Misturar competências é o erro silencioso que este projeto pode cometer:
    produziria uma base meio outubro, meio setembro, sem nada denunciando.
    """
    por_tabela: dict[str, Path] = {}
    tokens: set[str] = set()

    for caminho in sorted(diretorio.glob("*.sql")):
        m = ARQUIVO.match(caminho.name)
        if not m:
            logger.warning("Ignorando arquivo fora do padrão: %s", caminho.name)
            continue
        tokens.add(m.group(1))
        tabela = schema.tabela_da_fonte(caminho.stem)
        if tabela is None:
            logger.warning("Ignorando tabela desconhecida: %s", caminho.name)
            continue
        if tabela in por_tabela:
            raise ValueError(
                f"dois arquivos para a mesma tabela {tabela}: "
                f"{por_tabela[tabela].name} e {caminho.name}"
            )
        por_tabela[tabela] = caminho

    if len(tokens) > 1:
        raise ValueError(
            f"mais de uma competência em {diretorio}: {', '.join(sorted(tokens))} — "
            f"carregar as duas juntas produziria uma base misturada"
        )

    competencia = competencia_iso(next(iter(tokens))) if tokens else None
    return por_tabela, competencia


def sha256(caminho: Path, bloco: int = 8 * 1024 * 1024) -> str:
    """Hash de um arquivo da fonte, gravado na proveniência dentro da base.

    O molde não precisa disto: lá a origem é um WebDAV com manifesto publicado,
    conferível a qualquer momento. Aqui os arquivos chegam por um canal que não
    controlamos e não republica nada. Gravar o hash DENTRO do `.fdb` é a única
    forma de, meses depois, provar de qual entrega aquela base saiu.
    """
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for pedaco in iter(lambda: fh.read(bloco), b""):
            h.update(pedaco)
    return h.hexdigest()


def carregar_arquivo(
    con, tabela: str, arquivo: Path, cfg: FirebirdConfig
) -> tuple[int, int, int]:
    """Carrega um arquivo inteiro numa tabela.

    Devolve `(linhas, controles_removidos, substituidos)` — os dois últimos são
    a conta do que a conversão UTF-8 -> WIN1252 mexeu, e vão para a
    proveniência gravada dentro da base.
    """
    from . import leitura
    from ..db import loader

    total = 0
    report = loader.TruncationReport()
    limpeza = leitura.Limpeza()
    with loader.Carregador(con, cfg) as carregador:
        for lida, df in leitura.blocos(arquivo, limpeza=limpeza):
            if lida != tabela:
                raise ValueError(
                    f"{arquivo.name}: esperava {tabela}, o INSERT diz {lida}"
                )
            # O DataFrame vai CRU: `Carregador.carregar` é o único lugar que
            # converte tipo, e normalizar aqui faria a segunda passada topar
            # com coluna já tipada.
            total += carregador.carregar(df, tabela, report)
    report.log(tabela)
    limpeza.log(arquivo.name)
    return total, limpeza.controles_removidos, limpeza.total_substituidos


def _worker(tarefa: tuple[str, str, str]) -> tuple[str, int, float, str, int, int]:
    """Roda num processo próprio. Ver a nota sobre `database` abaixo."""
    tabela, arquivo, database = tarefa
    # Imports adiados: com 'spawn' (o padrão no Windows) o filho reimporta este
    # módulo, e trazer o driver e o polars no topo custaria em cada spawn.
    from ..db import connection

    # O caminho do banco VIAJA NA TAREFA e não sai de load_config(): DB_NAME
    # aponta para a produção, e a carga escreve na staging. Ler do ambiente
    # aqui gravaria na base que está no ar.
    cfg = FirebirdConfig(**{**load_config().__dict__, "database": database})

    inicio = time.time()
    try:
        with connection.conectar(cfg) as con:
            n, c1, subs = carregar_arquivo(con, tabela, Path(arquivo), cfg)
        return tabela, n, time.time() - inicio, "", c1, subs
    except Exception as e:  # noqa: BLE001 — o erro precisa atravessar o pool
        return tabela, 0, time.time() - inicio, f"{type(e).__name__}: {e}", 0, 0


@dataclass
class Resultado:
    """O que a carga produziu, além das linhas.

    `controles_removidos` e `substituidos` entram na proveniência gravada dentro
    da base: são a conta do que a conversão UTF-8 -> WIN1252 mexeu no texto, e é
    olhando a série dessa conta por competência que se percebe a fonte mudar de
    comportamento.
    """

    totais: dict[str, int] = field(default_factory=dict)
    controles_removidos: int = 0
    substituidos: int = 0


def carregar_tudo_paralelo(
    por_tabela: dict[str, Path],
    cfg: FirebirdConfig,
    processos: int,
    pular: set[str] | None = None,
    ao_concluir: Callable[[str, int], None] | None = None,
) -> Resultado:
    pular = pular or set()
    tarefas = [
        (tabela, str(caminho), cfg.database)
        for tabela, caminho in por_tabela.items()
        if tabela not in pular
    ]
    # Maior primeiro: `logradouro` entra no primeiro worker livre e os pequenos
    # passam pelos outros enquanto isso.
    tarefas.sort(key=lambda t: Path(t[1]).stat().st_size, reverse=True)

    res = Resultado()
    if not tarefas:
        return res

    erros: list[str] = []
    processos = max(1, min(processos, len(tarefas)))

    if processos == 1:
        for t in tarefas:
            _registrar(_worker(t), res, erros, ao_concluir)
    else:
        with mp.Pool(processes=processos) as pool:
            for r in pool.imap_unordered(_worker, tarefas):
                _registrar(r, res, erros, ao_concluir)

    if erros:
        raise RuntimeError("falha na carga: " + " | ".join(erros))
    return res


def _registrar(resultado, res: Resultado, erros, ao_concluir) -> None:
    tabela, n, seg, erro, c1, subs = resultado
    if erro:
        erros.append(f"{tabela}: {erro}")
        logger.error("%s falhou: %s", tabela, erro)
        return
    res.totais[tabela] = n
    res.controles_removidos += c1
    res.substituidos += subs
    logger.info("%s: %d linhas em %.1fs", tabela, n, seg)
    if ao_concluir:
        ao_concluir(tabela, n)
