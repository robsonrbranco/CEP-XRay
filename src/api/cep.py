"""Normalização e validação de CEP.

Existe pela mesma razão que `api/cnpj.py` no molde: **400 e 404 são casos
diferentes**, e o banco não sabe distingui-los. Uma consulta que não acha nada
pode ser um CEP bem formado que não existe (404) ou uma string que nunca
poderia ser um CEP (400), e quem decide isso é o código, antes de ir ao banco.

A diferença estrutural em relação ao CNPJ, e ela é grande
---------------------------------------------------------
**CEP não tem dígito verificador.** O CNPJ tem dois, calculáveis, e é por isso
que `api/cnpj.py` do molde consegue reprovar `11222333000182` sem consultar
nada. Aqui não há aritmética a fazer: a validação é de FORMA e só.

A consequência prática: um CEP de 8 dígitos que não existe na base devolve
**404**, nunca 400. Só entra em 400 o que não é um CEP — comprimento errado,
caractere que não é dígito. Isso está travado por teste
(`cep_nao_tem_digito_verificador`) para que ninguém invente uma regra de
validação que não existe no dado e passe a recusar CEP válido.

Sequência de dígito repetido também **não** é recusada, ao contrário do CNPJ:
`00000000` é inválido por não estar em nenhuma faixa, não por ser repetido, e
`20000000` é o início da faixa do Rio de Janeiro.
"""

from __future__ import annotations

import re

TAMANHO = 8

_NAO_DIGITO = re.compile(r"\D")


class CEPInvalido(ValueError):
    """A string não tem a forma de um CEP. Vira 400, nunca 404."""


def so_digitos(valor: str) -> str:
    return _NAO_DIGITO.sub("", valor or "")


def valido(cep: str) -> bool:
    return len(so_digitos(cep)) == TAMANHO


def normalizar(cep: str) -> str:
    """Aceita com ou sem pontuação: '01001-000', '01001000', '01.001-000'.

    Levanta `CEPInvalido` para o que não tem forma de CEP.
    """
    digitos = so_digitos(cep)
    if len(digitos) != TAMANHO:
        raise CEPInvalido(
            f"CEP deve ter {TAMANHO} dígitos, recebido {len(digitos)}: {cep!r}"
        )
    return digitos


def formatar(cep: str) -> str:
    """'01001000' -> '01001-000'."""
    d = normalizar(cep)
    return f"{d[:5]}-{d[5:]}"


def uf_valida(uf: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z]{2}", uf or ""))
