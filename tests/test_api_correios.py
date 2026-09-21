"""A tradução para o contrato dos Correios.

Camada pura, sem banco e sem HTTP. O que ela defende é a honestidade da
resposta: campo que a base não tem sai NULO E PRESENTE, e nenhum deles recebe
um valor parecido vindo de outro lugar.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src.api import correios  # noqa: E402

CAMPOS = {
    "cep", "uf", "localidade", "numeroLocalidade", "logradouro",
    "tipoLogradouro", "complemento", "abreviatura", "bairro", "tipoCEP",
    "cepUnidadeOperacional", "lado", "numeroInicial", "numeroFinal",
    "nomeUnidade",
}

SEMPRE_NULOS = (
    "numeroLocalidade", "abreviatura", "cepUnidadeOperacional",
    "numeroInicial", "numeroFinal",
)


def ficha(**kw):
    base = {
        "cep": "01001000",
        "origem": "logradouro",
        "tipo": "Praça",
        "nome_logradouro": "da Sé",
        "estado": "SP",
        "bairro": {"id": 1, "nome": "Sé"},
        "cidade": {"id": 1, "nome": "São Paulo", "ibge": "3550308", "ddd": "11"},
        "complemento": "lado ímpar",
        "grande_usuario": None,
    }
    base.update(kw)
    return base


def test_resposta_tem_exatamente_os_campos_do_contrato():
    assert set(correios.endereco(ficha(), "01001000")) == CAMPOS


def test_campos_que_a_base_nao_tem_saem_nulos_e_presentes():
    """Presentes, não ausentes: um cliente gerado do OpenAPI espera a chave."""
    r = correios.endereco(ficha(), "01001000")
    for campo in SEMPRE_NULOS:
        assert campo in r, campo
        assert r[campo] is None, campo


def test_numero_localidade_nunca_recebe_o_codigo_ibge():
    """O pior erro possível aqui.

    `numeroLocalidade` é o código de localidade dos Correios (LOC_NU);
    `codigoIbge` é outra numeração. Entregar uma no campo da outra passaria
    despercebido e casaria com nada.
    """
    r = correios.endereco(ficha(), "01001000")
    assert r["numeroLocalidade"] is None
    assert "3550308" not in str(r.values())


def test_tipo_e_nome_do_logradouro_saem_separados():
    """O contrato pede `tipoLogradouro` e `logradouro` como campos distintos —
    que é exatamente o par de colunas da fonte. É por isso que a coluna
    concatenada não entra no schema."""
    r = correios.endereco(ficha(), "01001000")
    assert r["tipoLogradouro"] == "Praça"
    assert r["logradouro"] == "da Sé"


def test_tipo_cep_1_para_logradouro():
    assert correios.endereco(ficha(), "01001000")["tipoCEP"] == 1


def test_tipo_cep_2_para_cep_resolvido_por_faixa():
    r = correios.endereco(
        ficha(origem="faixa", tipo=None, nome_logradouro=None, bairro=None,
              complemento=None),
        "12345678",
    )
    assert r["tipoCEP"] == 2
    assert r["logradouro"] is None


def test_tipo_cep_2_para_tipo_vazio():
    """String vazia em `tipo` significa CEP geral de localidade — e é distinta
    de NULL, que o leitor preserva justamente para isto."""
    assert correios.endereco(ficha(tipo=""), "01001000")["tipoCEP"] == 2


def test_tipo_cep_3_e_nome_unidade_quando_ha_grande_usuario():
    r = correios.endereco(ficha(grande_usuario="UNESP"), "01001900")
    assert r["tipoCEP"] == 3
    assert r["nomeUnidade"] == "UNESP"


def test_tipo_cep_nunca_emite_4_nem_5():
    """Unidade operacional e caixa postal comunitária não são distinguíveis no
    extrato. Emitir um deles por palpite seria pior que não emitir."""
    for kw in ({}, {"origem": "faixa"}, {"grande_usuario": "X"}, {"tipo": ""}):
        assert correios.endereco(ficha(**kw), "01001000")["tipoCEP"] in (1, 2, 3)


@pytest.mark.parametrize(
    "complemento,esperado",
    [
        ("lado ímpar", "I"),
        ("lado impar", "I"),
        ("lado par", "P"),
        ("até 318 lado par", "P"),
        ("LADO PAR", "P"),
        (None, None),
        ("Bloco A", None),
        # Faixa composta: 51 casos medidos. Ambíguo sai nulo, não escolhe um.
        ("de 1 a 99 lado ímpar, de 2 a 98 lado par", None),
    ],
)
def test_lado_derivado_do_complemento(complemento, esperado):
    assert correios.lado(complemento) == esperado


def test_numero_inicial_e_final_nao_sao_inferidos_da_prosa():
    """'até 318 lado par' tem um 318, e ele NÃO vira numeroFinal.

    Transformar prosa em campo estruturado é inventar dado: 'ao fim' não é
    número, e o cliente receberia um intervalo que a fonte nunca afirmou.
    """
    r = correios.endereco(ficha(complemento="até 318 lado par"), "01001000")
    assert r["numeroInicial"] is None
    assert r["numeroFinal"] is None
    assert r["complemento"] == "até 318 lado par"


def test_cep_inexistente_levanta():
    with pytest.raises(correios.CEPNaoEncontrado):
        correios.endereco(None, "99999999")


def test_bairro_ausente_sai_nulo_e_nao_quebra():
    """6,5% dos CEPs não têm bairro."""
    r = correios.endereco(ficha(bairro=None), "01001000")
    assert r["bairro"] is None


def test_pagina_calcula_total_de_paginas():
    p = correios.pagina([{"a": 1}], numero=0, tamanho=50, total=101)
    assert p["page"] == {
        "number": 0, "size": 50, "totalElements": 101, "totalPages": 3
    }
    assert p["itens"] == [{"a": 1}]


def test_uf_traz_todas_as_faixas():
    """A base tem até 3 faixas por UF; devolver uma daria resposta que parece
    completa e não é."""
    r = correios.uf({
        "sigla": "GO", "estado": "Goiás", "capital": "Goiânia",
        "regiao": "Centro-Oeste", "faixa_ini": "72800000", "faixa_fim": "76799999",
        "faixas": [{"faixaInicial": "72800/74000", "faixaFinal": "73999/74894"}],
    })
    assert r["uf"] == "GO"
    assert len(r["faixas"]) == 1


def test_localidade_expoe_o_ibge_sob_o_proprio_nome():
    r = correios.localidade({
        "id_cidade": 1, "cidade": "São Paulo", "estado": "SP",
        "cidade_ibge": "3550308", "ddd": "11",
    })
    assert r["codigoIbge"] == "3550308"
    assert r["numeroLocalidade"] is None
