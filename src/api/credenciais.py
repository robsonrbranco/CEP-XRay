"""Armazenamento de credenciais: SQLite, sem dependência de framework web.

Fica separado da base CNPJ de propósito. Aquela é read-only e é **apagada e
substituída todo mês** — um consumidor cadastrado nela sumiria na virada.

SQLite e não Firebird: volume baixo, escrita rara, `sqlite3` é stdlib e o
arquivo é trivial de copiar e versionar. Reaproveitar o engine Firebird que já
está na imagem seria possível, mas embedded gravável com vários workers é
problema diferente de embedded somente leitura, e não há motivo para importá-lo
aqui.

Duas regras que valem a pena entender antes de mexer:

* **O segredo em claro nunca é guardado.** Ele existe uma vez, no retorno de
  `criar()` e `rotacionar()`, e depois só o hash. Perdeu, rotaciona.
* **Revogar não apaga.** `status` e `revogada_em` em vez de DELETE, porque o
  uso histórico daquela credencial precisa continuar íntegro nas estatísticas.
  Credencial apagada deixaria buraco no relatório do mês.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Tamanhos alinhados aos exemplos do SERPRO (28 caracteres alfanuméricos), para
# que um cliente escrito contra a API deles não esbarre em limite de campo.
TAMANHO_KEY = 28
TAMANHO_SECRET = 28

# PBKDF2 em vez de hash simples: o segredo é gerado por nós e tem entropia alta,
# mas o custo de iterar é irrelevante numa operação que acontece uma vez por
# emissão de token, e protege contra tabela pré-computada se o arquivo vazar.
ITERACOES = 240_000

STATUS_VALIDOS = ("ativa", "suspensa", "revogada")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS credencial (
    consumer_key            TEXT PRIMARY KEY,
    secret_hash             TEXT NOT NULL,
    secret_salt             TEXT NOT NULL,
    contratante_nome        TEXT NOT NULL,
    contratante_documento   TEXT,
    contratante_email       TEXT,
    criada_em               TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'ativa',
    revogada_em             TEXT,
    quota_mensal            INTEGER,
    observacao              TEXT
);
CREATE INDEX IF NOT EXISTS idx_credencial_status ON credencial(status);
"""


def agora() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _gerar(tamanho: int) -> str:
    """Token alfanumérico no formato dos exemplos do SERPRO."""
    alfabeto = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def _hash(secret: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode(), bytes.fromhex(salt), ITERACOES
    ).hex()


@dataclass
class Credencial:
    consumer_key: str
    contratante_nome: str
    contratante_documento: str | None
    contratante_email: str | None
    criada_em: str
    status: str
    revogada_em: str | None
    quota_mensal: int | None
    observacao: str | None

    @property
    def ativa(self) -> bool:
        return self.status == "ativa"

    def para_json(self) -> dict:
        return {
            "consumerKey": self.consumer_key,
            "contratante": {
                "nome": self.contratante_nome,
                "documento": self.contratante_documento,
                "email": self.contratante_email,
            },
            "criadaEm": self.criada_em,
            "status": self.status,
            "revogadaEm": self.revogada_em,
            "quotaMensal": self.quota_mensal,
            "observacao": self.observacao,
        }


class CredencialNaoEncontrada(LookupError):
    pass


class Credenciais:
    """Acesso ao arquivo de credenciais. Uma instância por processo."""

    def __init__(self, caminho: str | Path):
        self._caminho = Path(caminho)
        self._caminho.parent.mkdir(parents=True, exist_ok=True)
        with self._conectar() as con:
            con.executescript(ESQUEMA)

    @contextmanager
    def _conectar(self):
        con = sqlite3.connect(self._caminho, timeout=10)
        con.row_factory = sqlite3.Row
        # WAL: leitura não bloqueia escrita. Importa porque vários workers
        # consultam credencial a cada emissão de token enquanto o /manager
        # pode estar gravando.
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # -- escrita --------------------------------------------------------

    def criar(self, contratante_nome: str, contratante_documento: str | None = None,
              contratante_email: str | None = None, quota_mensal: int | None = None,
              observacao: str | None = None) -> tuple[Credencial, str]:
        """Cria uma credencial. Devolve (credencial, segredo em claro).

        O segredo só existe neste retorno. Depois disto, só o hash.
        """
        key = _gerar(TAMANHO_KEY)
        secret = _gerar(TAMANHO_SECRET)
        salt = secrets.token_hex(16)

        with self._conectar() as con:
            con.execute(
                "INSERT INTO credencial (consumer_key, secret_hash, secret_salt, "
                "contratante_nome, contratante_documento, contratante_email, "
                "criada_em, status, quota_mensal, observacao) "
                "VALUES (?,?,?,?,?,?,?,'ativa',?,?)",
                (key, _hash(secret, salt), salt, contratante_nome,
                 contratante_documento, contratante_email, agora(),
                 quota_mensal, observacao),
            )
        return self.obter(key), secret

    def rotacionar(self, consumer_key: str) -> str:
        """Novo segredo, mesma chave. O cliente atualiza um valor, não dois, e o
        histórico de uso continua ligado à mesma credencial."""
        self.obter(consumer_key)          # levanta se não existir
        secret = _gerar(TAMANHO_SECRET)
        salt = secrets.token_hex(16)
        with self._conectar() as con:
            con.execute(
                "UPDATE credencial SET secret_hash = ?, secret_salt = ? "
                "WHERE consumer_key = ?",
                (_hash(secret, salt), salt, consumer_key),
            )
        return secret

    def alterar(self, consumer_key: str, **campos) -> Credencial:
        permitidos = {"contratante_nome", "contratante_documento",
                      "contratante_email", "quota_mensal", "observacao", "status"}
        desconhecidos = set(campos) - permitidos
        if desconhecidos:
            raise ValueError(f"campo(s) não alterável(is): {', '.join(sorted(desconhecidos))}")
        if "status" in campos and campos["status"] not in STATUS_VALIDOS:
            raise ValueError(f"status inválido: {campos['status']}")

        self.obter(consumer_key)
        if not campos:
            return self.obter(consumer_key)

        sets = ", ".join(f"{c} = ?" for c in campos)
        with self._conectar() as con:
            con.execute(
                f"UPDATE credencial SET {sets} WHERE consumer_key = ?",
                (*campos.values(), consumer_key),
            )
        return self.obter(consumer_key)

    def revogar(self, consumer_key: str) -> Credencial:
        """Marca como revogada. NÃO apaga — o uso histórico precisa sobreviver."""
        self.obter(consumer_key)
        with self._conectar() as con:
            con.execute(
                "UPDATE credencial SET status = 'revogada', revogada_em = ? "
                "WHERE consumer_key = ?",
                (agora(), consumer_key),
            )
        return self.obter(consumer_key)

    # -- leitura --------------------------------------------------------

    def obter(self, consumer_key: str) -> Credencial:
        with self._conectar() as con:
            linha = con.execute(
                "SELECT * FROM credencial WHERE consumer_key = ?", (consumer_key,)
            ).fetchone()
        if linha is None:
            raise CredencialNaoEncontrada(consumer_key)
        return self._para_objeto(linha)

    def listar(self, status: str | None = None) -> list[Credencial]:
        sql = "SELECT * FROM credencial"
        args: tuple = ()
        if status:
            sql += " WHERE status = ?"
            args = (status,)
        sql += " ORDER BY criada_em"
        with self._conectar() as con:
            return [self._para_objeto(l) for l in con.execute(sql, args).fetchall()]

    def autenticar(self, consumer_key: str, secret: str) -> Credencial | None:
        """Devolve a credencial se a chave existir, o segredo bater e ela estiver
        ATIVA. Nos três casos de falha devolve None, sem distinguir — dizer
        qual dos três falhou ajudaria quem está tentando adivinhar.
        """
        with self._conectar() as con:
            linha = con.execute(
                "SELECT * FROM credencial WHERE consumer_key = ?", (consumer_key,)
            ).fetchone()
        if linha is None:
            # Gasta o mesmo tempo de um hash real, para a resposta não revelar
            # pela duração se a chave existe.
            _hash(secret, secrets.token_hex(16))
            return None

        esperado = linha["secret_hash"]
        if not hmac.compare_digest(esperado, _hash(secret, linha["secret_salt"])):
            return None
        cred = self._para_objeto(linha)
        return cred if cred.ativa else None

    @staticmethod
    def _para_objeto(linha: sqlite3.Row) -> Credencial:
        return Credencial(
            consumer_key=linha["consumer_key"],
            contratante_nome=linha["contratante_nome"],
            contratante_documento=linha["contratante_documento"],
            contratante_email=linha["contratante_email"],
            criada_em=linha["criada_em"],
            status=linha["status"],
            revogada_em=linha["revogada_em"],
            quota_mensal=linha["quota_mensal"],
            observacao=linha["observacao"],
        )
