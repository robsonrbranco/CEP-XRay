#!/usr/bin/env python3
"""Manifesto de publicação: o que a estação promete e o pod confere.

    python -m src.publicacao.manifesto gerar               na estação
    python -m src.publicacao.manifesto conferir --base ... no destino

Por que isto existe
-------------------
A publicação mensal apaga a base do volume antes de copiar a nova, porque não
há espaço para as duas. Isso é aceitável — a estação continua com o original, e
o pior caso é downtime estendido, não perda de dado — mas só é aceitável **se a
base copiada for conferida antes de o pod servir**. Depois de apagar a antiga
não há com o que comparar, então a conferência tem que vir de fora: um
manifesto gerado na origem.

É a mesma disciplina do download, e pelo mesmo motivo. Lá conferimos byte a
byte contra o WebDAV porque tamanho certo com conteúdo corrompido só apareceria
quatro horas depois, no meio do ETL. Aqui uma página corrompida no meio de
34 GB pode não aparecer por semanas, até alguém consultar justo aquele CNPJ.

O que é conferido
-----------------
* **SHA-256 do arquivo** — a única garantia real de que os 34 GB chegaram
  inteiros. É o passo caro; leva alguns minutos.
* **Contagem por tabela** — pega o arquivo que chegou íntegro mas não é o que
  se pensava (competência errada, carga incompleta promovida por engano).
* **Propriedades do header** — `read only`, page size e reserva de espaço, lidos
  de `MON$DATABASE` por SQL. Sem isso, uma base gravável entraria em produção
  silenciosamente e o primeiro UPDATE acidental de um cliente a corromperia.

Tudo por SQL e stdlib: a imagem do pod não precisa de `gstat`/`fbstat`, só do
engine e do cliente.

Regra de sanidade
-----------------
`--anterior` compara com o manifesto do mês passado. A base **cresce em número
de linhas** todo mês; encolher é sinal de carga incompleta.

A régua é linha, não byte: de 2026-08 para 2026-09 as linhas subiram 1,86
milhão e o **arquivo encolheu 10%**, porque `USE_FULL` entrou entre as duas
cargas. Uma regra baseada em tamanho teria reprovado a base correta.
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from ..db import connection, schema
from ..db.config import FirebirdConfig, load_config

load_dotenv()

# Blocos de 16 MB: o hash de 34 GB é dominado por leitura de disco, e bloco
# maior que isso não melhora mais nada.
BLOCO = 16 * 1024 * 1024

# Crescimento mensal acima disto pede olhar humano. A série observada cresce
# menos de 1% ao mês; 5% dá folga larga sem deixar passar duplicação de carga.
CRESCIMENTO_SUSPEITO = 0.05


def sha256(caminho: Path, progresso=None) -> str:
    h = hashlib.sha256()
    lidos = 0
    total = caminho.stat().st_size
    with open(caminho, "rb") as f:
        while bloco := f.read(BLOCO):
            h.update(bloco)
            lidos += len(bloco)
            if progresso and lidos % (BLOCO * 64) == 0:
                progresso(lidos, total)
    return h.hexdigest()


def propriedades(cfg: FirebirdConfig) -> dict:
    """Header e contagens, lidos por SQL — sem depender de gstat/fbstat."""
    with connection.conectar(cfg) as con:
        cur = con.cursor()
        cur.execute(
            "SELECT MON$PAGE_SIZE, MON$READ_ONLY, MON$RESERVE_SPACE, "
            "       MON$ODS_MAJOR, MON$ODS_MINOR, MON$CREATION_DATE "
            "FROM MON$DATABASE"
        )
        page_size, read_only, reserve, ods_maj, ods_min, criacao = cur.fetchone()

        cur.execute("SELECT TRIM(RDB$CHARACTER_SET_NAME) FROM RDB$DATABASE")
        charset = cur.fetchone()[0]

        contagens = {}
        for tabela in sorted(schema.TABLES):
            cur.execute(f"SELECT COUNT(*) FROM {tabela}")
            contagens[tabela] = cur.fetchone()[0]

        cur.execute(
            "SELECT COUNT(*) FROM RDB$INDICES WHERE RDB$SYSTEM_FLAG = 0"
        )
        indices = cur.fetchone()[0]

    return {
        "page_size": page_size,
        "read_only": bool(read_only),
        "reserve_space": bool(reserve),
        "ods": f"{ods_maj}.{ods_min}",
        "charset": charset,
        "criacao": criacao.isoformat() if criacao else None,
        "indices": indices,
        "contagens": contagens,
        "total_linhas": sum(contagens.values()),
    }


def gerar(cfg: FirebirdConfig, arquivo: Path, competencia: str | None) -> dict:
    print(f"lendo propriedades de {cfg.database} ...", flush=True)
    props = propriedades(cfg)

    print(f"calculando sha256 de {arquivo.stat().st_size / 1e9:.2f} GB ...", flush=True)

    def barra(lidos, total):
        print(f"\r  {lidos / total * 100:5.1f}%", end="", flush=True)

    digest = sha256(arquivo, barra)
    print("\r  100.0%", flush=True)

    return {
        "gerado_em": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "competencia": competencia,
        "arquivo": arquivo.name,
        "bytes": arquivo.stat().st_size,
        "sha256": digest,
        **props,
    }


def _comparar(esperado: dict, real: dict, anterior: dict | None) -> list[str]:
    """Devolve a lista de problemas. Vazia significa aprovado."""
    problemas = []

    if esperado["sha256"] != real["sha256"]:
        problemas.append(
            f"sha256 não bate — o arquivo que chegou não é o que saiu\n"
            f"       esperado {esperado['sha256']}\n"
            f"       obtido   {real['sha256']}"
        )
    if esperado["bytes"] != real["bytes"]:
        problemas.append(
            f"tamanho: esperado {esperado['bytes']:,}, obtido {real['bytes']:,}"
        )

    if not real["read_only"]:
        problemas.append(
            "a base NÃO está read-only — produção gravável aceita UPDATE acidental"
        )
    for campo in ("page_size", "charset", "ods"):
        if esperado[campo] != real[campo]:
            problemas.append(
                f"{campo}: esperado {esperado[campo]}, obtido {real[campo]}"
            )
    if esperado["indices"] != real["indices"]:
        problemas.append(
            f"índices: esperados {esperado['indices']}, encontrados {real['indices']}"
        )

    for tabela, n in esperado["contagens"].items():
        obtido = real["contagens"].get(tabela)
        if obtido != n:
            problemas.append(f"{tabela}: esperado {n:,}, obtido {obtido:,}")

    # Sanidade contra o mês anterior: linha cresce, nunca cai.
    if anterior:
        for tabela, antes in anterior["contagens"].items():
            agora = real["contagens"].get(tabela, 0)
            if tabela in schema.DOMAIN_TABLES:
                continue          # domínio oscila para baixo sem ser erro
            if agora < antes:
                problemas.append(
                    f"{tabela} ENCOLHEU: {antes:,} -> {agora:,} "
                    f"(carga incompleta?)"
                )
            elif antes and (agora - antes) / antes > CRESCIMENTO_SUSPEITO:
                problemas.append(
                    f"{tabela} cresceu {(agora / antes - 1) * 100:.1f}% "
                    f"({antes:,} -> {agora:,}) — acima de "
                    f"{CRESCIMENTO_SUSPEITO:.0%}, confira antes de publicar"
                )

    return problemas


def conferir(cfg: FirebirdConfig, arquivo: Path, manifesto: dict,
             anterior: dict | None) -> int:
    print(f"conferindo {arquivo} contra o manifesto de "
          f"{manifesto.get('competencia') or '?'}", flush=True)

    print(f"  sha256 de {arquivo.stat().st_size / 1e9:.2f} GB ...", flush=True)
    real = {"sha256": sha256(arquivo), "bytes": arquivo.stat().st_size}
    print("  lendo a base ...", flush=True)
    real.update(propriedades(cfg))

    problemas = _comparar(manifesto, real, anterior)

    if problemas:
        print(f"\nREPROVADA — {len(problemas)} problema(s):", flush=True)
        for p in problemas:
            print(f"  - {p}", flush=True)
        print("\nNÃO suba o pod. Recopie a base da estação.", flush=True)
        return 1

    print(f"\nAPROVADA — {real['total_linhas']:,} linhas em "
          f"{len(real['contagens'])} tabelas, read-only, "
          f"{real['charset']}, page {real['page_size']}", flush=True)
    return 0


def _config_para(caminho: str | None) -> FirebirdConfig:
    cfg = load_config()
    if caminho:
        cfg = FirebirdConfig(**{**cfg.__dict__, "database": caminho})
    return cfg


def main() -> int:
    p = argparse.ArgumentParser(
        prog="manifesto",
        description="Gera e confere o manifesto de publicação da base",
    )
    sub = p.add_subparsers(dest="acao", required=True)

    g = sub.add_parser("gerar", help="na estação, sobre a base pronta")
    g.add_argument("--base", help="caminho do .fdb como o servidor o vê")
    g.add_argument("--arquivo", help="caminho local do .fdb (padrão: o mesmo)")
    g.add_argument("--competencia", help="AAAA-MM")
    g.add_argument("--saida", default="manifesto.json")

    c = sub.add_parser("conferir", help="no destino, antes de subir o pod")
    c.add_argument("--base", help="caminho do .fdb como o engine o vê")
    c.add_argument("--arquivo", help="caminho local do .fdb (padrão: o mesmo)")
    c.add_argument("--manifesto", default="manifesto.json")
    c.add_argument("--anterior", help="manifesto do mês passado, para a régua de crescimento")

    args = p.parse_args()
    cfg = _config_para(args.base)
    arquivo = Path(args.arquivo or args.base or cfg.database)

    if not arquivo.exists():
        print(f"arquivo não encontrado: {arquivo}", file=sys.stderr)
        return 2

    if args.acao == "gerar":
        m = gerar(cfg, arquivo, args.competencia)
        # Cria o diretório antes de escrever: o manifesto é gerado uma vez por
        # competência, depois de minutos de SHA-256, e falhar aqui por causa de
        # uma pasta inexistente jogaria esse trabalho fora.
        Path(args.saida).parent.mkdir(parents=True, exist_ok=True)
        Path(args.saida).write_text(
            json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nmanifesto escrito em {args.saida}", flush=True)
        print(f"  {m['total_linhas']:,} linhas, {m['bytes'] / 1e9:.2f} GB", flush=True)
        print(f"  sha256 {m['sha256']}", flush=True)
        return 0

    manifesto = json.loads(Path(args.manifesto).read_text(encoding="utf-8"))
    anterior = (json.loads(Path(args.anterior).read_text(encoding="utf-8"))
                if args.anterior else None)
    return conferir(cfg, arquivo, manifesto, anterior)


if __name__ == "__main__":
    sys.exit(main())
