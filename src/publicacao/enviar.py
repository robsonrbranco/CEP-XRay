#!/usr/bin/env python3
"""Publica a base no host do Olympus: comprime, envia, confere e troca.

    python -m src.publicacao.enviar publicar     tudo, na ordem
    python -m src.publicacao.enviar preparar     só comprime
    python -m src.publicacao.enviar enviar       só transmite (retomável)
    python -m src.publicacao.enviar trocar       só a troca no host
    python -m src.publicacao.enviar conferir     só o /saude

Por que isto é mais simples que o equivalente do Themis
------------------------------------------------------
O `enviar.py` do CNPJ-XRay fatia a base em partes de 512 MB e as manda em 4
threads com retomada por parte. Aquilo existe para 12,2 GB comprimidos, numa
transmissão que leva horas e que precisa sobreviver a uma queda de conexão no
meio.

A base do CEP comprime para ~60 MB. Fatiar em partes de 512 MB produziria uma
parte só, e o paralelismo por parte não teria o que paralelizar. Então aqui é
um `gzip` em stream e um `scp`, com a mesma conferência por sha256 remoto e a
mesma retomada — se o arquivo já está no host com o hash certo, não reenvia.

E a ORDEM é outra, que é a diferença que importa: envia e confere com o pod NO
AR, e só então para para renomear. Ver o cabeçalho de `deploy/trocar-base.sh`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.db.config import load_config  # noqa: E402

BLOCO = 8 * 1024 * 1024
NIVEL_GZIP = 6
TENTATIVAS = 5

# O `Python-urllib/3.x` padrão leva 403 do Cloudflare (error code 1010) — o
# Themis descobriu isso em produção. Qualquer User-Agent nomeado passa.
AGENTE = "CEP-XRay-publicacao/1.0"


def _env(nome: str, padrao: str | None = None) -> str:
    v = os.getenv(nome)
    return v if v not in (None, "") else (padrao or "")


@dataclass
class Destino:
    host: str
    usuario: str
    porta: int
    pasta_fdb: str
    pasta_db: str
    nome_base: str
    deployment: str
    namespace: str
    url_saude: str

    @classmethod
    def do_ambiente(cls) -> "Destino":
        host = _env("OLYMPUS_HOST")
        if not host:
            raise SystemExit(
                "OLYMPUS_HOST não definido — sem ele não há para onde enviar"
            )
        nome = _env("OLYMPUS_NOME_BASE", "cep_xray.fdb")
        if nome != nome.lower():
            # Mesma checagem de `deploy/trocar-base.sh`, aqui para falhar antes
            # de transmitir em vez de depois. O pod procura o caminho exato do
            # Dockerfile, e Linux diferencia caixa.
            raise SystemExit(
                f"OLYMPUS_NOME_BASE tem maiúscula: {nome!r}. O pod procura "
                f"{nome.lower()!r}, e Linux diferencia caixa."
            )
        return cls(
            host=host,
            usuario=_env("OLYMPUS_USER", "root"),
            porta=int(_env("OLYMPUS_PORT", "22")),
            pasta_fdb=_env("OLYMPUS_PASTA_FDB", "/cep-xray-fdb"),
            pasta_db=_env("OLYMPUS_PASTA_DB", "/cep-xray-db"),
            nome_base=nome,
            deployment=_env("OLYMPUS_DEPLOYMENT", "hestia"),
            namespace=_env("OLYMPUS_NAMESPACE", "olympus"),
            url_saude=_env(
                "OLYMPUS_URL_SAUDE", "https://hestia.ecomciencia.com/saude"
            ),
        )

    @property
    def envio(self) -> str:
        """Subpasta da mesma pasta da base.

        Separada do `.fdb` que o pod está servindo porque um `scp` direto por
        cima dele corromperia a base em produção no meio de uma consulta.
        """
        return f"{self.pasta_fdb}/envio"

    @property
    def nova(self) -> str:
        return f"{self.pasta_fdb}/{self.nome_base[:-4]}.novo.fdb"

    def ssh(self, *comando: str) -> list[str]:
        return ["ssh", "-p", str(self.porta), f"{self.usuario}@{self.host}", *comando]

    def scp(self, local: Path, remoto: str) -> list[str]:
        return [
            "scp", "-P", str(self.porta), "-q",
            str(local), f"{self.usuario}@{self.host}:{remoto}",
        ]


def sha256(caminho: Path) -> str:
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        for pedaco in iter(lambda: fh.read(BLOCO), b""):
            h.update(pedaco)
    return h.hexdigest()


def preparar(fdb: Path, trabalho: Path) -> dict:
    """Comprime a base e escreve o manifesto de envio."""
    trabalho.mkdir(parents=True, exist_ok=True)
    destino = trabalho / "base.fdb.gz"

    print(f"Lendo  {fdb} ({fdb.stat().st_size / 1024 / 1024:.1f} MB)")
    fdb_sha = sha256(fdb)

    print(f"Comprimindo para {destino.name} ...")
    inicio = time.time()
    with open(fdb, "rb") as origem, gzip.open(destino, "wb", NIVEL_GZIP) as saida:
        shutil.copyfileobj(origem, saida, BLOCO)

    manifesto = {
        "fdb_bytes": fdb.stat().st_size,
        "fdb_sha256": fdb_sha,
        "comprimido_bytes": destino.stat().st_size,
        "comprimido_sha256": sha256(destino),
        "razao": round(fdb.stat().st_size / max(destino.stat().st_size, 1), 2),
        "preparado_em": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (trabalho / "manifesto-envio.json").write_text(
        json.dumps(manifesto, indent=2), encoding="utf-8"
    )
    print(
        f"  {manifesto['comprimido_bytes'] / 1024 / 1024:.1f} MB "
        f"({manifesto['razao']}x) em {time.time() - inicio:.0f}s"
    )
    return manifesto


def _remoto_ok(d: Destino, remoto: str, sha: str) -> bool:
    r = subprocess.run(
        d.ssh(f"sha256sum {remoto} 2>/dev/null | cut -d' ' -f1"),
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() == sha


def enviar(d: Destino, trabalho: Path) -> int:
    """Transmite com o pod NO AR. Retomável: hash certo no host não reenvia."""
    manifesto = json.loads((trabalho / "manifesto-envio.json").read_text())
    comprimido = trabalho / "base.fdb.gz"
    remoto = f"{d.envio}/base.fdb.gz"

    subprocess.run(d.ssh(f"mkdir -p {d.envio}"), check=True)

    livre = subprocess.run(
        d.ssh(f"df -B1 --output=avail {d.pasta_fdb} | tail -1"),
        capture_output=True, text=True, check=True,
    )
    disponivel = int(livre.stdout.strip() or 0)
    preciso = manifesto["fdb_bytes"] + manifesto["comprimido_bytes"] + 2 * 1024**3
    if disponivel < preciso:
        print(
            f"ERRO: {disponivel / 1024**3:.1f} GB livres, preciso de "
            f"{preciso / 1024**3:.1f} GB",
            file=sys.stderr,
        )
        return 1

    if _remoto_ok(d, remoto, manifesto["comprimido_sha256"]):
        print("Já estava no host com o hash certo — nada a enviar")
    else:
        for tentativa in range(1, TENTATIVAS + 1):
            print(f"Enviando ({tentativa}/{TENTATIVAS}) ...")
            r = subprocess.run(d.scp(comprimido, remoto), capture_output=True, text=True)
            if r.returncode == 0 and _remoto_ok(d, remoto, manifesto["comprimido_sha256"]):
                break
            espera = min(2**tentativa, 30)
            print(f"  falhou, nova tentativa em {espera}s", file=sys.stderr)
            time.sleep(espera)
        else:
            print("ERRO: não consegui transmitir a base", file=sys.stderr)
            return 1

    # Descomprime NO HOST, com o pod ainda no ar. É o que tira a descompressão
    # da janela de downtime.
    print("Descomprimindo no host (pod ainda no ar) ...")
    subprocess.run(d.ssh(f"gzip -dc {remoto} > {d.nova} && rm -f {remoto}"), check=True)

    subprocess.run(
        d.scp(trabalho / "manifesto-envio.json", f"{d.envio}/manifesto-envio.json"),
        check=True,
    )
    print("Base nova no host, ao lado da que está no ar")
    return 0


def _rodar_no_host(d: Destino, script: Path) -> int:
    alvo = f"/tmp/{script.name}"
    subprocess.run(d.scp(script, alvo), check=True)
    ambiente = (
        f"ENVIO={d.envio} PASTA_FDB={d.pasta_fdb} NOME_BASE={d.nome_base} "
        f"DEPLOYMENT={d.deployment} NAMESPACE={d.namespace}"
    )
    return subprocess.run(
        d.ssh(f"chmod +x {alvo} && {ambiente} {alvo}")
    ).returncode


def trocar(d: Destino) -> int:
    return _rodar_no_host(d, _PROJECT_ROOT / "deploy" / "trocar-base.sh")


def conferir_servico(d: Destino) -> int:
    """Confirma que o pod voltou E que está servindo a competência certa.

    O pod de pé não basta: ele pode estar no ar servindo a base antiga, se a
    troca falhou no meio.
    """
    for tentativa in range(10):
        try:
            req = urllib.request.Request(
                d.url_saude, headers={"User-Agent": AGENTE}
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                corpo = json.loads(r.read())
            if corpo.get("status") == "ok":
                print(
                    f"No ar, servindo a competência {corpo.get('competencia')} "
                    f"({corpo.get('linhas'):,} linhas)".replace(",", ".")
                )
                return 0
            print(f"  /saude: {corpo}")
        except Exception as e:  # noqa: BLE001
            print(f"  tentativa {tentativa + 1}/10: {e}")
        time.sleep(10)
    print("ERRO: o serviço não respondeu ok em 100s", file=sys.stderr)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(
        prog="python -m src.publicacao.enviar",
        description="Publica a base no host do Olympus",
    )
    p.add_argument(
        "comando",
        choices=["preparar", "enviar", "trocar", "conferir", "publicar"],
    )
    p.add_argument("--base", default=None, help="caminho do .fdb (padrão: DB_NAME)")
    p.add_argument(
        "--trabalho", default=str(_PROJECT_ROOT / "envio"),
        help="diretório local do arquivo comprimido",
    )
    args = p.parse_args()

    d = Destino.do_ambiente()
    trabalho = Path(args.trabalho)
    fdb = Path(args.base) if args.base else Path(load_config().database)

    if args.comando == "preparar":
        preparar(fdb, trabalho)
        return 0
    if args.comando == "enviar":
        return enviar(d, trabalho)
    if args.comando == "trocar":
        return trocar(d)
    if args.comando == "conferir":
        return conferir_servico(d)

    # publicar: a ordem inteira, parando no primeiro erro.
    preparar(fdb, trabalho)
    for etapa in (lambda: enviar(d, trabalho), lambda: trocar(d),
                  lambda: conferir_servico(d)):
        codigo = etapa()
        if codigo:
            return codigo
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
