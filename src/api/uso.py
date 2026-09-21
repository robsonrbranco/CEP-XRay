"""Log de consultas e estatísticas de uso — analítico e sintético.

O log **não vai para o SQLite no caminho quente**. Uma linha por requisição,
vindas de N workers, num único arquivo SQLite, é contenção de lock exatamente
onde ela dói mais. Cada worker acrescenta ao próprio `.jsonl`, sem lock e sem
coordenação; a consolidação lê os arquivos fechados depois.

Isso dá de graça a separação entre os dois tipos de estatística:

* **analítico** — os `.jsonl`, uma linha por consulta, sujeitos a uma janela de
  retenção
* **sintético** — o agregado em SQLite, que **sobrevive à poda do analítico**

Assim o uso de 2024 continua disponível depois que os detalhes de 2024 foram
apagados.

Sobre `ni` (o CNPJ consultado): registrar o que cada cliente consulta é
registrar a atividade comercial dele — prospecção, diligência, concorrência.
Não é dado pessoal, mas é sensível. Por isso o sintético nunca guarda o `ni`, e
`registrar()` aceita `ni=None` para quem preferir não gravá-lo nem no
analítico.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

# O SERPRO documenta estes códigos como transações NÃO faturáveis. Contar tudo
# igual inflaria o uso do contratante com erros que não são dele -- e, no caso
# do 500, são nossos.
NAO_FATURAVEIS = frozenset({400, 401, 403, 500, 502, 504})

ESQUEMA = """
CREATE TABLE IF NOT EXISTS uso_mensal (
    competencia     TEXT NOT NULL,          -- AAAA-MM
    consumer_key    TEXT NOT NULL,
    rota            TEXT NOT NULL,
    status_http     INTEGER NOT NULL,
    faturavel       INTEGER NOT NULL,
    consultas       INTEGER NOT NULL DEFAULT 0,
    duracao_ms_soma INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (competencia, consumer_key, rota, status_http, faturavel)
);
CREATE INDEX IF NOT EXISTS idx_uso_key ON uso_mensal(consumer_key, competencia);

-- Marca até onde cada arquivo ja foi consolidado, para a consolidacao ser
-- idempotente: rodar duas vezes nao conta duas vezes.
CREATE TABLE IF NOT EXISTS consolidado (
    arquivo     TEXT PRIMARY KEY,
    linhas      INTEGER NOT NULL,
    em          TEXT NOT NULL
);
"""


@dataclass
class Consulta:
    instante: str
    consumer_key: str
    rota: str
    status_http: int
    faturavel: bool
    duracao_ms: int
    ni: str | None = None
    worker_pid: int | None = None


def faturavel(status_http: int) -> bool:
    return status_http not in NAO_FATURAVEIS


def competencia_de(instante: str) -> str:
    return instante[:7]


class Log:
    """Escrita do analítico. Um arquivo por worker por dia, só append."""

    def __init__(self, diretorio: str | Path, pid: int | None = None):
        self._dir = Path(diretorio)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._pid = pid if pid is not None else os.getpid()

    def _arquivo(self, dia: str) -> Path:
        return self._dir / f"consultas-{self._pid}-{dia}.jsonl"

    def registrar(self, consumer_key: str, rota: str, status_http: int,
                  duracao_ms: int, ni: str | None = None) -> Consulta:
        instante = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        c = Consulta(
            instante=instante, consumer_key=consumer_key, rota=rota,
            status_http=status_http, faturavel=faturavel(status_http),
            duracao_ms=duracao_ms, ni=ni, worker_pid=self._pid,
        )
        # Append em modo texto: a escrita de uma linha curta é atômica o
        # bastante, e cada worker tem o seu arquivo -- não há concorrência.
        with open(self._arquivo(instante[:10]), "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")
        return c

    def arquivos(self) -> list[Path]:
        return sorted(self._dir.glob("consultas-*.jsonl"))


class Estatisticas:
    """Consolidação e leitura do sintético."""

    def __init__(self, caminho: str | Path):
        self._caminho = Path(caminho)
        self._caminho.parent.mkdir(parents=True, exist_ok=True)
        with self._conectar() as con:
            con.executescript(ESQUEMA)

    @contextmanager
    def _conectar(self):
        con = sqlite3.connect(self._caminho, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    def consolidar(self, diretorio_logs: str | Path,
                   incluir_hoje: bool = False) -> dict:
        """Lê os .jsonl e soma no sintético.

        Por padrão ignora os arquivos de hoje: eles ainda estão recebendo
        escrita, e consolidar um arquivo vivo obrigaria a controlar deslocamento
        de leitura. Arquivo de dia fechado é lido inteiro, uma vez.

        Idempotente: `consolidado` guarda o que já foi lido, então rodar duas
        vezes não conta duas vezes.
        """
        diretorio = Path(diretorio_logs)
        hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lidos, linhas_total = 0, 0

        with self._conectar() as con:
            ja = {r["arquivo"] for r in con.execute("SELECT arquivo FROM consolidado")}

            for arq in sorted(diretorio.glob("consultas-*.jsonl")):
                if arq.name in ja:
                    continue
                if not incluir_hoje and hoje in arq.name:
                    continue

                linhas = 0
                for linha in arq.read_text(encoding="utf-8").splitlines():
                    if not linha.strip():
                        continue
                    try:
                        c = json.loads(linha)
                    except json.JSONDecodeError:
                        # Linha truncada por queda no meio da escrita: descarta
                        # esta e segue. Perder um registro de uso é melhor que
                        # abortar a consolidação inteira.
                        continue
                    con.execute(
                        "INSERT INTO uso_mensal (competencia, consumer_key, rota, "
                        "status_http, faturavel, consultas, duracao_ms_soma) "
                        "VALUES (?,?,?,?,?,1,?) "
                        "ON CONFLICT(competencia, consumer_key, rota, status_http, faturavel) "
                        "DO UPDATE SET consultas = consultas + 1, "
                        "              duracao_ms_soma = duracao_ms_soma + excluded.duracao_ms_soma",
                        (competencia_de(c["instante"]), c["consumer_key"], c["rota"],
                         c["status_http"], int(bool(c["faturavel"])), c["duracao_ms"]),
                    )
                    linhas += 1

                con.execute(
                    "INSERT INTO consolidado (arquivo, linhas, em) VALUES (?,?,?)",
                    (arq.name, linhas,
                     datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
                )
                lidos += 1
                linhas_total += linhas

        return {"arquivos": lidos, "linhas": linhas_total}

    def podar(self, diretorio_logs: str | Path, dias: int = 90) -> int:
        """Apaga .jsonl mais velhos que `dias`, **se já consolidados**.

        O sintético permanece: é exatamente esta assimetria que permite manter a
        estatística de uso depois de descartar o detalhe do que foi consultado.
        """
        from datetime import timedelta
        limite = (datetime.now(timezone.utc) - timedelta(days=dias)).strftime("%Y-%m-%d")
        apagados = 0
        with self._conectar() as con:
            ja = {r["arquivo"] for r in con.execute("SELECT arquivo FROM consolidado")}
        for arq in sorted(Path(diretorio_logs).glob("consultas-*.jsonl")):
            dia = arq.stem.split("-", 2)[-1]
            if dia < limite and arq.name in ja:
                arq.unlink()
                apagados += 1
        return apagados

    # -- leitura --------------------------------------------------------

    def resumo(self, consumer_key: str | None = None,
               competencia: str | None = None) -> dict:
        """Sintético. Sem `consumer_key`, é o geral."""
        onde, args = [], []
        if consumer_key:
            onde.append("consumer_key = ?")
            args.append(consumer_key)
        if competencia:
            onde.append("competencia = ?")
            args.append(competencia)
        filtro = (" WHERE " + " AND ".join(onde)) if onde else ""

        with self._conectar() as con:
            total = con.execute(
                f"SELECT COALESCE(SUM(consultas),0) t, "
                f"       COALESCE(SUM(CASE WHEN faturavel=1 THEN consultas END),0) f, "
                f"       COALESCE(SUM(duracao_ms_soma),0) d "
                f"FROM uso_mensal{filtro}", args
            ).fetchone()
            por_rota = con.execute(
                f"SELECT rota, SUM(consultas) n FROM uso_mensal{filtro} "
                f"GROUP BY rota ORDER BY n DESC", args
            ).fetchall()
            por_status = con.execute(
                f"SELECT status_http, SUM(consultas) n FROM uso_mensal{filtro} "
                f"GROUP BY status_http ORDER BY status_http", args
            ).fetchall()
            por_mes = con.execute(
                f"SELECT competencia, SUM(consultas) n, "
                f"       SUM(CASE WHEN faturavel=1 THEN consultas END) f "
                f"FROM uso_mensal{filtro} GROUP BY competencia ORDER BY competencia", args
            ).fetchall()

        consultas = total["t"]
        return {
            "consumerKey": consumer_key,
            "competencia": competencia,
            "consultas": consultas,
            "faturaveis": total["f"],
            "duracaoMediaMs": round(total["d"] / consultas, 1) if consultas else 0.0,
            "porRota": {r["rota"]: r["n"] for r in por_rota},
            "porStatus": {str(r["status_http"]): r["n"] for r in por_status},
            "porMes": [
                {"competencia": r["competencia"], "consultas": r["n"],
                 "faturaveis": r["f"] or 0} for r in por_mes
            ],
        }

    def consumo_do_mes(self, consumer_key: str, competencia: str | None = None) -> int:
        """Faturáveis no mês — é isto que se compara com a quota contratada."""
        competencia = competencia or datetime.now(timezone.utc).strftime("%Y-%m")
        with self._conectar() as con:
            r = con.execute(
                "SELECT COALESCE(SUM(consultas),0) n FROM uso_mensal "
                "WHERE consumer_key = ? AND competencia = ? AND faturavel = 1",
                (consumer_key, competencia),
            ).fetchone()
        return r["n"]
