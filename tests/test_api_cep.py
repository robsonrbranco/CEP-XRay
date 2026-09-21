"""Validação de CEP: só forma, nunca aritmética.

Este arquivo existe sobretudo para travar uma NÃO-regra. O molde (CNPJ-XRay)
valida dígito verificador, e é tentador copiar a ideia — mas **CEP não tem
dígito verificador**. Uma "validação" inventada aqui recusaria CEP legítimo com
400, e o cliente nunca chegaria ao 404 que o contrato promete.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src.api import cep as mod  # noqa: E402


@pytest.mark.parametrize(
    "entrada",
    ["01001000", "01001-000", "01.001-000", " 01001000 ", "01 001 000"],
)
def test_aceita_com_e_sem_pontuacao(entrada):
    assert mod.normalizar(entrada) == "01001000"


@pytest.mark.parametrize("entrada", ["0100100", "010010000", "", "abc", "0100100a"])
def test_recusa_o_que_nao_tem_forma_de_cep(entrada):
    with pytest.raises(mod.CEPInvalido):
        mod.normalizar(entrada)


def test_cep_nao_tem_digito_verificador():
    """A regra que este projeto NÃO tem, e não pode ganhar por descuido.

    Qualquer sequência de 8 dígitos é bem formada. Se um CEP de 8 dígitos não
    existe na base, a resposta é 404 — nunca 400. Inventar um DV aqui faria a
    API recusar CEP válido com o código errado.
    """
    for digitos in ("00000000", "99999999", "12345678", "87654321"):
        assert mod.normalizar(digitos) == digitos
        assert mod.valido(digitos)


def test_digito_repetido_nao_e_recusado():
    """Diferente do CNPJ, onde sequência repetida é inválida por definição.

    '20000000' é o início da faixa do Rio de Janeiro; recusá-lo por "parecer"
    inválido derrubaria um CEP real.
    """
    assert mod.normalizar("20000000") == "20000000"
    assert mod.normalizar("11111111") == "11111111"


def test_formatar():
    assert mod.formatar("01001000") == "01001-000"
    assert mod.formatar("01001-000") == "01001-000"


@pytest.mark.parametrize("uf,ok", [("SP", True), ("sp", True), ("S", False),
                                   ("SPX", False), ("", False), ("S1", False)])
def test_uf_valida(uf, ok):
    assert mod.uf_valida(uf) is ok
