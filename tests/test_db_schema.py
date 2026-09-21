"""O schema é a fonte de verdade — se ele mente, tudo o mais mente junto.

Dele saem o DDL, o INSERT parametrizado, o DDL dos índices, o mapeamento
coluna-da-fonte e as expectativas do validador blue-green. Um erro aqui só
apareceria no meio de uma carga, ou pior, numa base promovida.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src.db import schema  # noqa: E402


def test_identificadores_cabem_em_31_caracteres():
    """Limite do Firebird 3.0. Estourar aparece como '-104 Name longer than
    database column size' no CREATE TABLE, tarde demais."""
    for tabela, cols in schema.TABLES.items():
        assert len(tabela) <= schema.MAX_IDENTIFIER, tabela
        for c in cols:
            assert len(c.name) <= schema.MAX_IDENTIFIER, f"{tabela}.{c.name}"
    for idx in schema.INDEXES:
        assert len(idx) <= schema.MAX_IDENTIFIER, idx


def test_nenhuma_tabela_declara_trava():
    """Sem PRIMARY KEY, NOT NULL ou FOREIGN KEY.

    `logradouro.distrito_id` é NULL em 96% das linhas: um NOT NULL ingênuo
    mataria a carga inteira.
    """
    for tabela in schema.TABLES:
        ddl = schema.create_table_sql(tabela).upper()
        assert "PRIMARY KEY" not in ddl
        assert "NOT NULL" not in ddl
        assert "FOREIGN KEY" not in ddl
        assert "REFERENCES" not in ddl
        assert "UNIQUE" not in ddl


def test_nenhuma_coluna_sa_sobrou():
    """A decisão de descartar as `_SA` vale 32,3% do conteúdo.

    Trava para que ninguém as reintroduza sem refazer a conta.
    """
    for tabela, cols in schema.TABLES.items():
        for c in cols:
            assert not c.name.endswith("_sa"), f"{tabela}.{c.name}"


def test_logradouro_nao_tem_a_coluna_derivada():
    """`logradouro.logradouro` é `tipo || ' ' || nome_logradouro`, e o contrato
    dos Correios pede as duas partes separadas."""
    nomes = schema.column_names("logradouro")
    assert "logradouro" not in nomes
    assert "tipo" in nomes and "nome_logradouro" in nomes


def test_latitude_e_longitude_sao_double_precision():
    """Não FLOAT (nenhum valor voltaria igual) nem NUMERIC (Decimal na saída)."""
    for tabela in ("logradouro", "bairro", "distrito", "cidade", "estado"):
        cols = {c.name: c for c in schema.columns(tabela)}
        for nome in ("latitude", "longitude"):
            assert cols[nome].fb_type == "DOUBLE PRECISION", f"{tabela}.{nome}"
            assert cols[nome].kind == schema.DOUBLE


def test_faixas_sao_texto_e_nao_inteiro():
    """CEP tem zero à esquerda ('01001000'), e `estado_faixa` tem valor que não
    é numérico ('72800/74000', medido, 11 caracteres)."""
    for tabela in ("bairro_faixa", "cidade_faixa", "distrito_faixa", "estado_faixa"):
        cols = {c.name: c for c in schema.columns(tabela)}
        for nome in ("faixa_ini", "faixa_fim"):
            assert cols[nome].kind == schema.TEXT, f"{tabela}.{nome}"
    largura = {c.name: c.max_len for c in schema.columns("estado_faixa")}
    assert largura["faixa_ini"] >= 11


def test_cep_e_texto_em_toda_tabela_que_o_tem():
    for tabela in ("logradouro", "log_complemento", "log_grande_usuario"):
        cols = {c.name: c for c in schema.columns(tabela)}
        assert cols["cep"].kind == schema.TEXT
        assert cols["cep"].max_len == 8


def test_indice_aponta_para_coluna_existente():
    """A trava roda no import; este teste garante que ela não foi afrouxada."""
    for nome, (tabela, *cols) in schema.INDEXES.items():
        assert tabela in schema.TABLES, nome
        conhecidas = set(schema.column_names(tabela))
        for c in cols:
            assert c in conhecidas, f"{nome}: {tabela}.{c}"


def test_chave_natural_existe_no_schema():
    for tabela, chave in schema.CHAVES_NATURAIS.items():
        conhecidas = set(schema.column_names(tabela))
        for c in chave:
            assert c in conhecidas, f"{tabela}.{c}"


def test_insert_usa_a_ordem_do_schema():
    sql = schema.insert_sql("log_complemento")
    assert sql == "INSERT INTO log_complemento (cep, complemento) VALUES (?, ?)"


def test_ddl_e_insert_tem_o_mesmo_numero_de_colunas():
    for tabela in schema.TABLES:
        n = len(schema.column_names(tabela))
        assert schema.insert_sql(tabela).count("?") == n, tabela


@pytest.mark.parametrize(
    "fonte,esperado",
    [
        ("TBL_CEP_2610_LOGRADOURO", "logradouro"),
        ("TBL_CEP_2610_LOG_COMPL", "log_complemento"),
        ("TBL_CEP_2610_CIDADE_IBGE_TERR", "cidade_ibge_terr"),
        ("TBL_CEP_2610_ESTADO_IBGE", "estado_ibge"),
        ("TBL_CEP_9901_LOGRADOURO", "logradouro"),
        ("TBL_CEP_2610_INEXISTENTE", None),
    ],
)
def test_tabela_da_fonte(fonte, esperado):
    assert schema.tabela_da_fonte(fonte) == esperado


def test_coluna_com_grafia_errada_na_fonte_e_mapeada():
    """A fonte grafa IDICE_DE_DESENV_HUMANO. Renomeamos para `idh_desenv_humano`
    e o mapeamento tem que continuar achando a coluna original."""
    mapa = schema.colunas_da_fonte("cidade_ibge")
    assert "IDICE_DE_DESENV_HUMANO" in mapa
    assert mapa["IDICE_DE_DESENV_HUMANO"].name == "idh_desenv_humano"


def test_metadados_fica_fora_de_tables():
    """`TABLES` dirige o ETL, e não há arquivo da fonte para `metadados`."""
    assert schema.METADADOS not in schema.TABLES
