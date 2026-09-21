"""Emissão e verificação do token de acesso (JWT HS256).

O cliente trata o token como opaco — devolve o que recebeu — então nada obriga
a ser JWT. Ele é JWT porque é autocontido: sobrevive a reinício de worker sem
tabela de sessão, e no pod cada worker é um processo isolado, sem estado
compartilhado.

Por que HMAC da stdlib e não uma biblioteca de JWT
--------------------------------------------------
O perigo clássico de JWT está em **aceitar o algoritmo que o token declara** —
`alg: none`, ou trocar RS256 por HS256 usando a chave pública como segredo.
Esses ataques existem porque bibliotecas genéricas precisam suportar vários
algoritmos e deixam a escolha vir do cabeçalho.

Aqui só emitimos e verificamos os nossos próprios tokens, com um algoritmo
fixo. `verificar()` **nunca lê o `alg` do cabeçalho**: calcula a assinatura com
HS256 e compara. Um token dizendo `alg: none` é rejeitado como qualquer outro
com assinatura errada — e há teste para isso.

Com essa regra, o que sobra é `hmac` e `base64` da stdlib, no espírito do resto
do projeto.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

ALGORITMO = "HS256"
# Uma hora, para casar com o `expires_in` do SERPRO.
VALIDADE_S = 3600


class TokenInvalido(ValueError):
    """Assinatura errada, formato quebrado ou token expirado — vira HTTP 401."""


def _b64(dados: bytes) -> str:
    """Base64url sem preenchimento, como manda a RFC 7515."""
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


def _des_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _assinar(cabecalho_payload: str, segredo: str) -> str:
    return _b64(hmac.new(segredo.encode(), cabecalho_payload.encode(),
                         hashlib.sha256).digest())


def emitir(consumer_key: str, segredo: str, validade_s: int = VALIDADE_S,
           escopo: str = "default", agora: int | None = None) -> tuple[str, int]:
    """Devolve (token, expires_in)."""
    iat = int(agora if agora is not None else time.time())
    cabecalho = {"alg": ALGORITMO, "typ": "JWT"}
    payload = {
        "sub": consumer_key,
        "iat": iat,
        "exp": iat + validade_s,
        "scope": escopo,
    }
    # separators sem espaço: JSON compacto, para o token não crescer à toa.
    partes = ".".join(
        _b64(json.dumps(p, separators=(",", ":"), sort_keys=True).encode())
        for p in (cabecalho, payload)
    )
    return f"{partes}.{_assinar(partes, segredo)}", validade_s


def verificar(token: str, segredo: str, agora: float | None = None) -> dict:
    """Devolve as claims, ou levanta TokenInvalido.

    A ordem importa: assinatura ANTES de expiração. Conferir `exp` primeiro
    significaria ler um payload não autenticado para decidir, o que dá a quem
    forja o token controle sobre o caminho de código percorrido.
    """
    pedacos = token.split(".")
    if len(pedacos) != 3:
        raise TokenInvalido("formato inesperado")

    cabecalho_payload = f"{pedacos[0]}.{pedacos[1]}"
    esperada = _assinar(cabecalho_payload, segredo)
    # compare_digest: comparação em tempo constante, para a duração não vazar
    # quantos bytes da assinatura estavam certos.
    if not hmac.compare_digest(esperada, pedacos[2]):
        raise TokenInvalido("assinatura não confere")

    try:
        claims = json.loads(_des_b64(pedacos[1]))
    except (ValueError, json.JSONDecodeError) as e:
        raise TokenInvalido(f"payload ilegível: {e}") from e

    agora = time.time() if agora is None else agora
    if "exp" not in claims:
        raise TokenInvalido("sem expiração")
    if float(claims["exp"]) <= agora:
        raise TokenInvalido("expirado")

    return claims


def do_cabecalho(authorization: str | None) -> str:
    """Extrai o token de `Authorization: Bearer <token>`."""
    if not authorization:
        raise TokenInvalido("sem cabeçalho Authorization")
    partes = authorization.split(None, 1)
    if len(partes) != 2 or partes[0].lower() != "bearer":
        raise TokenInvalido("esperado 'Bearer <token>'")
    return partes[1].strip()


def credenciais_basic(authorization: str | None) -> tuple[str, str]:
    """Extrai (consumer_key, secret) de `Authorization: Basic base64(k:s)`.

    É o formato que o SERPRO usa na emissão do token, e por isso o que um
    cliente escrito para eles vai enviar.
    """
    if not authorization:
        raise TokenInvalido("sem cabeçalho Authorization")
    partes = authorization.split(None, 1)
    if len(partes) != 2 or partes[0].lower() != "basic":
        raise TokenInvalido("esperado 'Basic <base64>'")
    try:
        bruto = base64.b64decode(partes[1].strip(), validate=True).decode()
    except Exception as e:                                  # noqa: BLE001
        raise TokenInvalido("base64 inválido") from e
    if ":" not in bruto:
        raise TokenInvalido("esperado 'chave:segredo'")
    chave, _, segredo = bruto.partition(":")
    return chave, segredo
