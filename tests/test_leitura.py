"""O tokenizador não pode perder nem inventar dado.

É o único módulo do projeto sem molde no CNPJ-XRay, e o único ponto por onde
TODO o dado passa. Um erro aqui não levanta exceção: produz uma base que parece
certa e tem um logradouro a menos, ou um NULL onde havia string vazia.

Cada teste abaixo defende um caso que existe na fonte real, e a fixture
`dados/TBL_CEP_2610_LOGRADOURO.sql` foi escrita para conter todos eles.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from src.etl import leitura  # noqa: E402

DADOS = Path(__file__).resolve().parent / "dados"
LOGRADOURO = DADOS / "TBL_CEP_2610_LOGRADOURO.sql"
ESTADO_IBGE = DADOS / "TBL_CEP_2610_ESTADO_IBGE.sql"


def ler(caminho, **kw):
    """Junta todos os blocos num dicionário cep -> linha."""
    linhas = []
    tabela = None
    for t, df in leitura.blocos(caminho, **kw):
        tabela = t
        linhas += df.to_dicts()
    return tabela, linhas


@pytest.fixture
def logradouro():
    _, linhas = ler(LOGRADOURO)
    return {l["cep"]: l for l in linhas}


def test_le_todas_as_linhas(logradouro):
    assert len(logradouro) == 7


def test_valor_com_quebra_de_linha_nao_e_partido(logradouro):
    """Um parser linha a linha quebraria esta linha em duas, em silêncio."""
    assert logradouro["01001001"]["nome_logradouro"] == "Quebra\nde linha"


def test_aspas_escapadas_viram_uma_aspa(logradouro):
    assert logradouro["01001002"]["nome_logradouro"] == "D'Oeste"


def test_null_e_string_vazia_chegam_distintos(logradouro):
    """A distinção que o read_csv do molde não consegue fazer.

    Na mesma linha: `tipo` é string VAZIA (CEP geral de localidade, sem
    logradouro) e `cep_ativo` é NULL (ausente). Colapsar os dois faria o
    `tipoCEP` da API errar.
    """
    linha = logradouro["01001003"]
    assert linha["tipo"] == ""
    assert linha["nome_logradouro"] == ""
    assert linha["cep_ativo"] is None
    assert linha["bairro_id"] is None


def test_controle_c1_e_removido_e_nao_substituido(logradouro):
    """Remover restaura 'Águas'; trocar por '?' produziria 'A?guas'."""
    assert logradouro["01001005"]["nome_logradouro"] == "Recanto das Águas"


def test_limpeza_conta_o_que_removeu():
    limpeza = leitura.Limpeza()
    ler(LOGRADOURO, limpeza=limpeza)
    assert limpeza.controles_removidos == 1
    assert limpeza.total_substituidos == 0


def test_espaco_nas_pontas_chega_intacto_ao_loader(logradouro):
    """O leitor NÃO apara: quem apara é `db/loader.normalizar`.

    Separar as duas coisas é o que permite o relatório de qualidade contar
    quantos valores foram aparados — se o leitor já entregasse limpo, o número
    não existiria.
    """
    assert logradouro["01001004"]["nome_logradouro"] == "  espaço nas pontas  "


def test_latitude_de_treze_casas_atravessa_como_texto(logradouro):
    """O leitor entrega texto; a conversão para DOUBLE é do loader.

    Se o leitor convertesse, a precisão dependeria dele — e é justamente esta
    linha que FLOAT perderia.
    """
    assert logradouro["01001006"]["latitude"] == "-23.5628731234567"


def test_colunas_sa_nunca_viram_objeto_python(logradouro):
    """As 3 colunas `_SA` e a `logradouro` derivada são descartadas na leitura.

    Não é carregar e depois apagar: elas não chegam a existir. É o que faz a
    economia de 32,3% ser de memória e de tempo, não só de disco.
    """
    linha = logradouro["01001000"]
    assert set(linha) == {
        "cep", "tipo", "nome_logradouro", "bairro_id", "distrito_id",
        "cidade_id", "estado", "latitude", "longitude", "cep_ativo",
    }


def test_colunas_vem_do_insert_e_nao_do_create_table():
    """O defeito real de TBL_CEP_2610_ESTADO_IBGE.sql.

    O arquivo declara `CREATE TABLE TBL_CEP_2610_ESTADO` com outra lista de
    colunas, e os INSERTs alimentam `TBL_CEP_2610_ESTADO_IBGE`. Quem lesse o
    CREATE TABLE carregaria a tabela errada com as colunas erradas.
    """
    tabela, linhas = ler(ESTADO_IBGE)
    assert tabela == "estado_ibge"
    assert len(linhas) == 2
    assert linhas[0]["uf"] == "SP"
    assert linhas[0]["codigo_ibge"] == "35"


def test_marcador_do_ibge_e_virgula_decimal_passam_como_texto():
    """O leitor não interpreta número — entrega o texto ao loader.

    `'-'` (sem dado, do IBGE) e `'5,06'` (vírgula decimal) são resolvidos por
    `db/loader.normalizar`, e testá-los aqui garante que o leitor não os
    "conserta" antes, escondendo o caso de quem deve tratá-lo.
    """
    _, linhas = ler(ESTADO_IBGE)
    acre = [l for l in linhas if l["uf"] == "AC"][0]
    assert acre["densidade_demogr_hab_km2"] == "5,06"
    assert acre["matriculas_ensino_fund23"] == "-"


def test_numero_dentro_do_nome_de_coluna_nao_vira_valor():
    """`POPULACAO_RESIDENTE_2022` não pode produzir o valor 2022.

    É o que a ordem das alternativas do regex garante: `cabeçalho` é testado
    antes de `número`, e consome a lista de colunas inteira.
    """
    _, linhas = ler(ESTADO_IBGE)
    assert linhas[0]["populacao_residente_2022"] == "44411238"


def test_varchar_do_create_table_nao_vira_valor():
    """`VARCHAR(8)` no CREATE TABLE não pode virar o número 8.

    Nenhum valor é coletado antes do primeiro INSERT.
    """
    _, linhas = ler(LOGRADOURO)
    assert linhas[0]["cep"] == "01001000"


@pytest.mark.parametrize("chunk", [64, 128, 256, 512, 1024])
def test_fronteira_de_chunk_nao_corrompe_nada(monkeypatch, chunk, logradouro):
    """O caso que só aparece em arquivo grande, forçado aqui com chunk pequeno.

    Com chunks de 64 bytes, literais, números e o próprio cabeçalho INSERT caem
    partidos entre leituras. O resultado tem que ser idêntico ao da leitura em
    um chunk só — inclusive o caractere multibyte cortado ao meio.
    """
    monkeypatch.setattr(leitura, "BYTES_POR_CHUNK", chunk)
    _, linhas = ler(LOGRADOURO)
    assert {l["cep"]: l for l in linhas} == logradouro


def test_bloco_fecha_so_em_linha_completa(monkeypatch):
    """Nenhum DataFrame pode sair com uma tupla pela metade."""
    monkeypatch.setattr(leitura, "BYTES_POR_CHUNK", 128)
    total = 0
    for _, df in leitura.blocos(LOGRADOURO, linhas_por_bloco=2):
        assert df.width == 10
        assert df.height > 0
        total += df.height
    assert total == 7


def test_contar_devolve_tabela_e_linhas():
    assert leitura.contar(LOGRADOURO) == ("logradouro", 7)
    assert leitura.contar(ESTADO_IBGE) == ("estado_ibge", 2)


def test_tabela_desconhecida_aborta(tmp_path):
    f = tmp_path / "TBL_CEP_2610_INVENTADA.sql"
    f.write_text(
        "INSERT INTO TBL_CEP_2610_INVENTADA (A,B) VALUES\n\t ('x','y');\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="desconhecida"):
        ler(f)


def test_coluna_do_schema_que_o_insert_nao_traz_aborta(tmp_path):
    """Melhor abortar que carregar a tabela com uma coluna sempre nula.

    Se a fonte deixar de publicar uma coluna, a base sairia silenciosamente
    incompleta e só a consulta denunciaria, semanas depois.
    """
    f = tmp_path / "TBL_CEP_2610_LOG_COMPL.sql"
    f.write_text(
        "INSERT INTO TBL_CEP_2610_LOG_COMPL (CEP) VALUES\n\t ('01001000');\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="complemento"):
        ler(f)
