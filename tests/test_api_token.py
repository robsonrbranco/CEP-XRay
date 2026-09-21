"""Testes do token de acesso.

O grosso aqui é ataque, não caminho feliz. Como a implementação é HMAC da
stdlib em vez de uma biblioteca de JWT, a responsabilidade pelas armadilhas
conhecidas é nossa — e a principal é **nunca deixar o token escolher o
algoritmo com que será verificado**.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api.token import (  # noqa: E402
    TokenInvalido,
    credenciais_basic,
    do_cabecalho,
    emitir,
    verificar,
)

SEGREDO = "segredo-de-assinatura-do-pod"


def _b64(o) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(o, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# emissão e verificação
# ---------------------------------------------------------------------------

def test_token_emitido_verifica():
    token, expira = emitir("KEY1", SEGREDO)

    claims = verificar(token, SEGREDO)

    assert claims["sub"] == "KEY1"
    assert claims["scope"] == "default"
    assert expira == 3600
    assert claims["exp"] - claims["iat"] == 3600


def test_expires_in_casa_com_o_serpro():
    assert emitir("KEY1", SEGREDO)[1] == 3600


def test_token_tem_tres_partes():
    token, _ = emitir("KEY1", SEGREDO)
    assert len(token.split(".")) == 3


def test_token_nao_leva_preenchimento_base64():
    """RFC 7515 manda base64url SEM '='. Cliente estrito rejeita com."""
    token, _ = emitir("KEY1", SEGREDO)
    assert "=" not in token


# ---------------------------------------------------------------------------
# ataques
# ---------------------------------------------------------------------------

def test_alg_none_e_rejeitado():
    """O ataque clássico: token diz que não tem algoritmo, biblioteca ingênua
    acredita. `verificar` nunca lê o `alg` do cabeçalho."""
    cabecalho = _b64({"alg": "none", "typ": "JWT"})
    payload = _b64({"sub": "INVASOR", "exp": 9999999999})

    with pytest.raises(TokenInvalido, match="assinatura"):
        verificar(f"{cabecalho}.{payload}.", SEGREDO)


def test_alg_trocado_no_cabecalho_nao_muda_a_verificacao():
    """Mesmo declarando outro algoritmo, a verificação continua HS256."""
    token, _ = emitir("KEY1", SEGREDO)
    _, payload, assinatura = token.split(".")
    forjado = f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{payload}.{assinatura}"

    with pytest.raises(TokenInvalido, match="assinatura"):
        verificar(forjado, SEGREDO)


def test_payload_adulterado_e_rejeitado():
    """Trocar o `sub` para outra credencial é o ataque que mais importa: seria
    consumir a quota alheia."""
    token, _ = emitir("KEY1", SEGREDO)
    cabecalho, _, assinatura = token.split(".")
    forjado = f"{cabecalho}.{_b64({'sub': 'OUTRA', 'exp': 9999999999})}.{assinatura}"

    with pytest.raises(TokenInvalido, match="assinatura"):
        verificar(forjado, SEGREDO)


def test_segredo_errado_e_rejeitado():
    token, _ = emitir("KEY1", SEGREDO)

    with pytest.raises(TokenInvalido, match="assinatura"):
        verificar(token, "outro-segredo")


def test_token_expirado_e_rejeitado():
    token, _ = emitir("KEY1", SEGREDO, validade_s=3600, agora=1_000_000)

    with pytest.raises(TokenInvalido, match="expirado"):
        verificar(token, SEGREDO, agora=1_000_000 + 3601)


def test_token_valido_ate_o_limite():
    token, _ = emitir("KEY1", SEGREDO, validade_s=3600, agora=1_000_000)

    assert verificar(token, SEGREDO, agora=1_000_000 + 3599)["sub"] == "KEY1"


def test_assinatura_conferida_antes_da_expiracao():
    """Ordem importa: conferir `exp` primeiro significaria ler payload não
    autenticado para decidir o caminho de código."""
    cabecalho = _b64({"alg": "HS256", "typ": "JWT"})
    payload = _b64({"sub": "X", "exp": 1})            # expirado E sem assinatura

    with pytest.raises(TokenInvalido, match="assinatura"):
        verificar(f"{cabecalho}.{payload}.qualquercoisa", SEGREDO)


def test_formato_quebrado_e_rejeitado():
    for ruim in ("", "abc", "a.b", "a.b.c.d"):
        with pytest.raises(TokenInvalido):
            verificar(ruim, SEGREDO)


def test_token_sem_exp_e_rejeitado():
    """Token sem expiração seria eterno — inclusive um que nós mesmos emitíssemos
    por engano."""
    cabecalho = _b64({"alg": "HS256", "typ": "JWT"})
    payload = _b64({"sub": "X"})
    from src.api.token import _assinar
    sem_exp = f"{cabecalho}.{payload}.{_assinar(f'{cabecalho}.{payload}', SEGREDO)}"

    with pytest.raises(TokenInvalido, match="sem expiração"):
        verificar(sem_exp, SEGREDO)


# ---------------------------------------------------------------------------
# cabeçalhos HTTP
# ---------------------------------------------------------------------------

def test_extrai_bearer():
    assert do_cabecalho("Bearer abc.def.ghi") == "abc.def.ghi"
    assert do_cabecalho("bearer abc.def.ghi") == "abc.def.ghi"


def test_bearer_malformado():
    for ruim in (None, "", "abc.def.ghi", "Basic abc", "Bearer"):
        with pytest.raises(TokenInvalido):
            do_cabecalho(ruim)


def test_extrai_basic_no_formato_do_serpro():
    b64 = base64.b64encode(b"djaR21PGoYp1iyK2n2ACOH9REdUb:ObRsAJWOL4fv2Tp27D1vd8fB3Ote").decode()

    chave, segredo = credenciais_basic(f"Basic {b64}")

    assert chave == "djaR21PGoYp1iyK2n2ACOH9REdUb"
    assert segredo == "ObRsAJWOL4fv2Tp27D1vd8fB3Ote"


def test_basic_malformado():
    for ruim in (None, "", "Basic naoehbase64!!", "Bearer abc",
                 f"Basic {base64.b64encode(b'semdoispontos').decode()}"):
        with pytest.raises(TokenInvalido):
            credenciais_basic(ruim)
