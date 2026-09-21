"""
Definição única do schema CEP-XRay para Firebird 3.0.

É a fonte de verdade de onde saem o DDL das tabelas, o INSERT parametrizado
usado na carga, o DDL dos índices e o mapeamento coluna-da-fonte → coluna-nossa
— assim as quatro coisas não saem de sincronia.

A fonte
-------
15 arquivos `.sql` do extrato DNEC/Correios, cada um com um `CREATE TABLE`
seguido de `INSERT INTO t (cols) VALUES (...),(...);` de 10 linhas por
statement. Competência `2610` = 2026-10 (a convenção do e-DNE é AAMM).

**O `CREATE TABLE` da fonte não é lido por nenhum código deste projeto.** Ele é
inútil nos dois sentidos:

* **errado** — `TBL_CEP_2610_ESTADO_IBGE.sql` declara `CREATE TABLE
  TBL_CEP_2610_ESTADO`, com uma lista de colunas que não tem nada a ver com os
  `INSERT` que vêm depois;
* **superdimensionado** — `CIDADE_FAIXA.ESTADO VARCHAR(255)` para conteúdo de 2
  caracteres; `PAISES_IBGE` inteiro em `VARCHAR(255)` para conteúdo de 2 a 46.

A ordem e o nome das colunas vêm da **lista de colunas do `INSERT`** (ver
`COLUNAS_FONTE`), e as larguras vêm da medição sobre a fonte real.

Dimensionamento dos VARCHAR
---------------------------
Medido sobre a competência 2610 inteira (2.170.877 linhas), arredondado para
cima com folga — VARCHAR curto demais trunca dado em silêncio.

    coluna                         medido   adotado
    logradouro.nome_logradouro         95      120
    logradouro.tipo                    14       30
    bairro.bairro                      72      100
    log_grande_usuario.grandes_usu     72      100
    log_complemento.complemento        84      120
    distrito.distrito                  51       80
    cidade.cidade                      32       60
    cidade_ibge.prefeito               50       80
    cidade_ibge_terr.regiao_geog_i     64       80
    estado.estado                      19       40
    estado_faixa.regiao                44       60
    paises_ibge.nome_pt                46       60

`estado_faixa.faixa_ini/faixa_fim` medem 11 e **não são numéricos**: há linhas
como `GO / 'Capital: Goiânia' / '72800/74000' / '73999/74894'`. As demais faixas
medem 8 e continuam `VARCHAR`, não `INTEGER`: CEP tem zero à esquerda
(`'01001000'`).

Regra de tipo: **identificador é texto, quantidade é número.** `cep`,
`cidade_ibge`, `ddd`, `sigla`, `codigo_ibge`, `mesorregiao` e as faixas ficam
`VARCHAR` mesmo contendo só dígitos — são chaves, não grandezas. Só as chaves
surrogate do próprio extrato (`id_bairro`, `id_cidade`, `id_distrito` e os
`*_id`) são `INTEGER`.

Charset
-------
As colunas de texto não declaram charset: herdam o padrão do banco, criado com
`DEFAULT CHARACTER SET WIN1252 COLLATION WIN_PTBR`.

WIN1252 e não UTF8 porque a fonte é o cadastro de endereços brasileiro, em
português do Brasil. Medido sobre os 331 MB de fonte: de 1.585.108 caracteres
não-ASCII, **exatamente 2 não cabem em WIN1252** — dois `U+0081` colados num
`Á` em `bairro` id 37996, que são resíduo de dupla codificação, não conteúdo.
UTF8 não acrescentaria nenhum caractere útil e cobraria 4 bytes por caractere
no tamanho declarado da coluna.

WIN_PTBR ordena acento como o português do Brasil espera, e compara
INSENSÍVEL a acento e a caixa, enquanto o dado guardado continua fiel:

    'SAO'  = 'SÃO'   -> verdadeiro
    'SE'   = 'Sé'    -> verdadeiro

Ou seja, quem consulta acha "São Paulo" digitando "sao paulo", sem normalizar
nada. É essa propriedade que torna as colunas `_SA` da fonte desnecessárias
(ver abaixo).

Três colunas da fonte que NÃO entram
------------------------------------
**1. Todas as `*_SA` ("sem acento") — 71,4 MB de 221,3 MB de conteúdo (32,3%).**
São `tipo_sa`, `nome_logradouro_sa`, `logradouro_sa`, `bairro_sa`, `cidade_sa`,
`distrito_sa`, `estado_sa`, `capital_sa`, `regiao_sa`, `complemento_sa`,
`grandes_usuarios_sa`. Elas existem para dar busca sem acento num banco que não
tem a collation. Este tem: `WIN_PTBR` entrega isso na comparação, de graça. A
coluna `_SA` é uma reimplementação manual, em disco, do mesmo efeito.

Contraponto honesto: `_SA` também serviria para *exibir* sem acento e para
ordenar fora do Firebird. Nenhum dos dois é caso de uso desta base — o contrato
dos Correios devolve o nome como está, e o `ORDER BY` roda aqui dentro.

**2. `logradouro.logradouro` — 33,5 MB.** É `tipo || ' ' || nome_logradouro` em
1.720.956 das 1.720.964 linhas (99,9995%). Além de derivada, ela é a versão
*truncada*: a fonte a declara `VARCHAR(100)` e 3 linhas batem exatamente em 100.
O contrato dos Correios pede `tipoLogradouro` e `logradouro` **separados**, ou
seja, pede exatamente as duas colunas de origem. Nas 8 linhas em que difere, é
porque a fonte truncou uma das duas — e nesses casos `nome_logradouro` é a que
perde, o que fica registrado aqui e medido por `validation/qualidade.py`.

**3. `cidade_faixa.cidade` e `cidade_faixa.estado` FICAM**, ainda que redundantes
com `cidade`. São 5.571 linhas (~190 KB) e tornam a tabela auto-suficiente para
responder faixa de localidade sem JOIN. Nesse tamanho, redundância é barata.

Latitude e longitude: DOUBLE PRECISION
--------------------------------------
Na fonte são `VARCHAR(50)`. Medido sobre os 3.441.928 valores de `logradouro`,
com até 13 casas decimais:

    tipo                bytes    pior erro físico   volta igual à fonte
    VARCHAR (fonte)     37,6 MB         —                  100%
    FLOAT (32 bits)     13,8 MB       0,39 m                 0%
    DOUBLE PRECISION    27,5 MB         0                   100%
    NUMERIC(16,13)      27,5 MB         0                   100%

FLOAT custa metade e 39 cm é irrelevante para endereçamento postal — 89% da
fonte tem 5 casas decimais, que já são ~1 m de resolução. O que o derruba é o
retorno: `-23.5629` lê de volta como `-23.562900543212890625`, ruído que
apareceria no JSON de toda resposta da API. Numa base cujo contrato é carregar
o que a fonte mandou, são 3,4 milhões de diferenças espúrias por 14 MB.

NUMERIC(16,13) acerta pelo motivo errado: exatidão decimal existe para centavos
(`float(1234.56)*100 == 123455.999…`), não para coordenada, e obrigaria a
formatar `Decimal('-23.5629000000000')` em toda serialização. DOUBLE custa o
mesmo, devolve todo valor exatamente, e é o tipo que qualquer GIS usa.

Ausência deliberada de travas
-----------------------------
Nenhuma tabela declara PRIMARY KEY, NOT NULL ou FOREIGN KEY. Não é esquecimento,
e aqui o agravante é medido: `logradouro.distrito_id` é **NULL em 1.645.011 das
1.720.964 linhas (96%)**, `bairro_id` em 111.700 e `cep_ativo` em 61.114. Um
`NOT NULL` ingênuo mataria a carga.

Os índices vêm depois da carga e existem para performance de consulta, não para
integridade. A conferência de qualidade é feita à parte, sobre a base já
carregada, e vira relatório — não aborto.

Medido nesta competência: **zero** chaves naturais repetidas e **zero** órfãos
em `bairro_id` / `cidade_id` / `distrito_id`. Ainda assim, `JOIN` com tabela de
domínio deve ser `LEFT JOIN` — 6,5% dos CEPs não têm bairro, e um `INNER JOIN`
os faria sumir da resposta.
"""

from dataclasses import dataclass

# Famílias de tipo. Determinam a conversão aplicada na carga.
TEXT = "text"
INT = "int"
NUMERIC = "numeric"
DOUBLE = "double"


@dataclass(frozen=True)
class Column:
    name: str
    kind: str
    # Tipo da coluna no Firebird.
    fb_type: str
    # Comprimento máximo em caracteres, para colunas TEXT. None nos demais.
    max_len: int | None = None
    # Nome da coluna na lista do INSERT da fonte. None = igual ao nosso, em
    # maiúsculas. Só difere onde a fonte erra a grafia ou abrevia demais.
    origem: str | None = None

    @property
    def fonte(self) -> str:
        return (self.origem or self.name).upper()

    @property
    def ddl(self) -> str:
        # Sem PRIMARY KEY, sem NOT NULL, sem FK: ver a nota sobre travas no topo
        # do módulo. Sem CHARACTER SET / COLLATE por coluna: o banco é criado
        # com DEFAULT CHARACTER SET WIN1252 COLLATION WIN_PTBR.
        return f"{self.name} {self.fb_type}"


def _text(name, size, **kw):
    return Column(name, TEXT, f"VARCHAR({size})", max_len=size, **kw)


def _int(name, fb_type="INTEGER", **kw):
    return Column(name, INT, fb_type, **kw)


def _num(name, precisao, escala, **kw):
    return Column(name, NUMERIC, f"NUMERIC({precisao},{escala})", **kw)


def _geo(name, **kw):
    return Column(name, DOUBLE, "DOUBLE PRECISION", **kw)


def _latlong():
    return [_geo("latitude"), _geo("longitude")]


TABLES: dict[str, list[Column]] = {
    # -----------------------------------------------------------------------
    # A tabela central: 1.720.964 CEPs, um por linha, sem repetição.
    # -----------------------------------------------------------------------
    "logradouro": [
        _text("cep", 8),
        _text("tipo", 30),
        _text("nome_logradouro", 120),
        _int("bairro_id"),
        _int("distrito_id"),
        _int("cidade_id"),
        _text("estado", 2),
        *_latlong(),
        _text("cep_ativo", 1),
    ],
    # Complemento em prosa ("lado ímpar", "até 318 lado par"). 241.725 CEPs.
    "log_complemento": [
        _text("cep", 8),
        _text("complemento", 120),
    ],
    # Grande usuário: o CEP pertence a uma única unidade. 20.508 CEPs — é a
    # única fonte de `nomeUnidade` no contrato dos Correios.
    "log_grande_usuario": [
        _text("cep", 8),
        _text("grandes_usuarios", 100),
    ],
    "bairro": [
        _int("id_bairro"),
        _text("bairro", 100),
        _int("cidade_id"),
        _text("estado", 2),
        *_latlong(),
    ],
    "bairro_faixa": [
        _int("id_bairro"),
        _text("faixa_ini", 8),
        _text("faixa_fim", 8),
    ],
    "distrito": [
        _int("id_distrito"),
        _text("distrito", 80),
        _int("cidade_id"),
        _text("estado", 2),
        *_latlong(),
    ],
    "distrito_faixa": [
        _int("id_distrito"),
        _text("faixa_ini", 8),
        _text("faixa_fim", 8),
    ],
    "cidade": [
        _int("id_cidade"),
        _text("cidade", 60),
        _text("estado", 2),
        _text("cidade_ibge", 7),
        _text("ddd", 2),
        *_latlong(),
    ],
    "cidade_faixa": [
        _int("id_cidade"),
        _text("cidade", 60),
        _text("estado", 2),
        _text("faixa_ini", 8),
        _text("faixa_fim", 8),
    ],
    # Estatísticas do IBGE por município. As escalas abaixo foram medidas; ver
    # a nota sobre '-' e vírgula decimal em `db/loader.py`.
    "cidade_ibge": [
        _int("id_cidade"),
        _text("cidade", 60),
        _text("estado", 2),
        _text("cidade_ibge", 7),
        _text("gentilico", 60),
        _text("prefeito", 80),
        _num("area_territorial_km2", 12, 3),
        _int("populacao_residente"),
        _num("densidade_demografica", 12, 2),
        _num("escolarizacao_6_a_14_anos", 5, 1),
        # A fonte grafa IDICE_DE_DESENV_HUMANO — erro de digitação dela.
        _num("idh_desenv_humano", 5, 3, origem="IDICE_DE_DESENV_HUMANO"),
        _num("mortalidade_infantil", 6, 2),
        _num("receitas_realizadas", 18, 6),
        _num("despesas_empenhadas", 18, 2),
        _num("pib_per_capita", 12, 2),
        # 'DD/MM', não data: não tem ano.
        _text("aniversario_municipio", 5),
    ],
    "cidade_ibge_terr": [
        _int("id_cidade"),
        _text("cidade", 60),
        _text("estado", 2),
        _text("cidade_ibge", 7),
        _text("regiao_intermediaria", 4),
        _text("regiao_intermed_nome", 50),
        _text("regiao_geog_imediata", 6),
        _text("regiao_geog_imednome", 80),
        _text("mesorregiao", 2),
        _text("mesorregiao_nome", 50),
        _text("microrregiao", 3),
        _text("microrregiao_nome", 50),
    ],
    "estado": [
        _text("sigla", 2),
        _text("faixa_ini", 8),
        _text("faixa_fim", 8),
        _text("estado", 40),
        _text("capital", 40),
        _text("regiao", 20),
        *_latlong(),
    ],
    # Até 3 faixas por UF, e faixa que NÃO é numérica ('72800/74000').
    "estado_faixa": [
        _text("sigla", 2),
        _text("regiao", 60),
        _text("faixa_ini", 11),
        _text("faixa_fim", 11),
    ],
    "estado_ibge": [
        _text("uf", 2),
        _text("codigo_ibge", 2),
        _text("gentilico", 60),
        _text("governador", 60),
        _text("capital", 40),
        _num("area_territorial_km2", 12, 3),
        _int("populacao_residente_2022"),
        _num("densidade_demogr_hab_km2", 10, 2),
        _int("matriculas_ensino_fund23"),
        _num("idh_indice_desenv_humano", 5, 3),
        _num("total_rec_realizadas", 18, 2),
        _num("total_des_empenhadas", 18, 2),
        _int("rendimento_mes_pcap"),
        _int("total_veiculos_2023"),
    ],
    "paises_ibge": [
        _text("id", 4),
        _text("sigla_iso", 3),
        _text("sigla", 2),
        _text("pais_bcb", 4),
        _text("pais_rbf", 3),
        _text("pais_sped", 4),
        _text("pais_siscomex", 3),
        _text("nome_pt", 60),
    ],
}


# Nome da tabela na fonte -> nome nosso. O prefixo `TBL_CEP_<AAMM>_` é removido
# pelo leitor, então a chave aqui é o sufixo. Só entram os que divergem de um
# lower() simples.
TABELA_DA_FONTE: dict[str, str] = {
    "LOG_COMPL": "log_complemento",
}


def tabela_da_fonte(nome_fonte: str) -> str | None:
    """`TBL_CEP_2610_LOG_COMPL` -> `log_complemento`. None se não conhecida."""
    sufixo = nome_fonte.upper()
    for prefixo in ("TBL_CEP_",):
        if sufixo.startswith(prefixo):
            # TBL_CEP_2610_LOGRADOURO -> LOGRADOURO
            partes = sufixo.split("_", 3)
            sufixo = partes[3] if len(partes) > 3 else ""
            break
    alvo = TABELA_DA_FONTE.get(sufixo, sufixo.lower())
    return alvo if alvo in TABLES else None


def colunas_da_fonte(table: str) -> dict[str, Column]:
    """Nome da coluna NA FONTE -> Column nossa.

    O leitor usa isto para escolher, na lista de colunas do INSERT, quais
    posições guardar — e é assim que as 12 colunas `_SA` e a `logradouro`
    derivada nunca chegam a virar objeto Python.
    """
    return {c.fonte: c for c in TABLES[table]}


# ---------------------------------------------------------------------------
# Índices
#
# Criados SEMPRE depois da carga: índice ativo durante INSERT em massa é o que
# mais custa numa carga Firebird.
#
# Cada um abaixo existe por um caminho de consulta da API. O que não tem
# caminho não entra: não há índice em `cep_ativo` (2 valores e nunca é filtro
# único), em `logradouro.tipo` (o filtro `tipoLogradouro` sempre vem junto com
# `uf` ou `localidade`) nem em latitude/longitude (não há consulta geográfica no
# contrato dos Correios — entra quando houver).
# ---------------------------------------------------------------------------
INDEXES: dict[str, tuple[str, ...]] = {
    # GET /cep/v2/enderecos/{cep} — o caminho quente.
    "logradouro_cep": ("logradouro", "cep"),
    "log_complemento_cep": ("log_complemento", "cep"),
    "log_grande_usuario_cep": ("log_grande_usuario", "cep"),
    # Os LEFT JOIN que montam UMA resposta de /enderecos/{cep}.
    "bairro_id": ("bairro", "id_bairro"),
    "cidade_id": ("cidade", "id_cidade"),
    "distrito_id": ("distrito", "id_distrito"),
    # GET /cep/v2/enderecos?uf=&localidade=&bairro=&logradouro= — listagem.
    # Sem índice, cada chamada varre 1,72 M linhas.
    "logradouro_cidade": ("logradouro", "cidade_id"),
    "logradouro_estado": ("logradouro", "estado"),
    "logradouro_bairro": ("logradouro", "bairro_id"),
    "logradouro_nome": ("logradouro", "nome_logradouro"),
    # GET /cep/v1/bairros/{uf}/localidades/{localidade}
    "bairro_cidade": ("bairro", "cidade_id"),
    # GET /cep/v1/localidades[/{uf}]
    "cidade_estado": ("cidade", "estado"),
    "cidade_nome": ("cidade", "cidade"),
    # Faixas: o CEP que não tem logradouro próprio cai na faixa, e as respostas
    # de localidade e UF carregam a faixa no corpo.
    "cidade_faixa_id": ("cidade_faixa", "id_cidade"),
    "cidade_faixa_ini": ("cidade_faixa", "faixa_ini"),
    "bairro_faixa_id": ("bairro_faixa", "id_bairro"),
    "distrito_faixa_id": ("distrito_faixa", "id_distrito"),
    "estado_faixa_sigla": ("estado_faixa", "sigla"),
    # GET /cep/v1/ufs[/{uf}]
    "estado_sigla": ("estado", "sigla"),
    "estado_ibge_uf": ("estado_ibge", "uf"),
    # Enriquecimento IBGE das localidades.
    "cidade_ibge_id": ("cidade_ibge", "id_cidade"),
    "cidade_ibge_terr_id": ("cidade_ibge_terr", "id_cidade"),
    "paises_ibge_id": ("paises_ibge", "id"),
}


# ---------------------------------------------------------------------------
# Proveniência
#
# A base não sabe de onde veio. `MON$CREATION_DATE` diz quando o arquivo foi
# criado, que não é a competência dos dados — recarregar uma competência antiga
# produziria arquivo novo com dado velho, e nada no banco denunciaria isso.
#
# Chave/valor em vez de colunas fixas: o conjunto de fatos sobre uma carga
# cresce, e uma tabela larga exigiria migração numa base que é read-only.
#
# Fica FORA de TABLES de propósito: `TABLES` dirige o ETL, e não há arquivo da
# fonte para carregar aqui.
# ---------------------------------------------------------------------------
METADADOS = "metadados"

CREATE_METADADOS = f"""
CREATE TABLE {METADADOS} (
    chave  VARCHAR(40),
    valor  VARCHAR(500)
)
"""


# Chave natural de cada tabela. NÃO é PRIMARY KEY no banco — serve para detectar
# carga duplicada (o mesmo arquivo carregado duas vezes) e para o relatório de
# qualidade.
#
# `estado_faixa` fica de fora: até 3 linhas por UF, sem identificador de linha.
CHAVES_NATURAIS: dict[str, tuple[str, ...]] = {
    "logradouro": ("cep",),
    "log_complemento": ("cep",),
    "log_grande_usuario": ("cep",),
    "bairro": ("id_bairro",),
    "bairro_faixa": ("id_bairro",),
    "distrito": ("id_distrito",),
    "distrito_faixa": ("id_distrito",),
    "cidade": ("id_cidade",),
    "cidade_faixa": ("id_cidade",),
    "cidade_ibge": ("id_cidade",),
    "cidade_ibge_terr": ("id_cidade",),
    "estado": ("sigla",),
    "estado_ibge": ("uf",),
    "paises_ibge": ("id",),
}


# Tabelas que legitimamente não crescem de uma competência para a outra —
# isentas da régua de crescimento do manifesto de publicação.
TABELAS_ESTAVEIS = ("estado", "estado_faixa", "estado_ibge", "paises_ibge")


def columns(table: str) -> list[Column]:
    return TABLES[table]


def column_names(table: str) -> list[str]:
    return [c.name for c in TABLES[table]]


def create_table_sql(table: str) -> str:
    cols = ",\n    ".join(c.ddl for c in TABLES[table])
    return f"CREATE TABLE {table} (\n    {cols}\n)"


def insert_sql(table: str) -> str:
    """INSERT parametrizado usado pelo carregador em massa."""
    cols = column_names(table)
    placeholders = ", ".join("?" * len(cols))
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"


def create_index_sql(name: str) -> str:
    table, *cols = INDEXES[name]
    return f"CREATE INDEX {name} ON {table} ({', '.join(cols)})"


# ---------------------------------------------------------------------------
# Trava de sanidade: o Firebird 3.0 limita identificador a 31 caracteres (o 4.0
# subiu para 63). Estourar isso só aparece como "-104 Name longer than database
# column size" na hora do CREATE TABLE — erro caro de descobrir tarde. Por isso
# a checagem roda na importação do módulo.
#
# O maior identificador aqui é `escolarizacao_6_a_14_anos` (25).
# ---------------------------------------------------------------------------
MAX_IDENTIFIER = 31


def _validar_identificadores() -> None:
    longos = []
    for tabela, cols in TABLES.items():
        if len(tabela) > MAX_IDENTIFIER:
            longos.append(f"tabela {tabela} ({len(tabela)})")
        for c in cols:
            if len(c.name) > MAX_IDENTIFIER:
                longos.append(f"{tabela}.{c.name} ({len(c.name)})")
    for idx in INDEXES:
        if len(idx) > MAX_IDENTIFIER:
            longos.append(f"índice {idx} ({len(idx)})")
    if longos:
        raise ValueError(
            f"identificadores acima de {MAX_IDENTIFIER} caracteres, "
            f"limite do Firebird 3.0: {', '.join(longos)}"
        )


def _validar_indices() -> None:
    """Índice apontando para coluna que não existe só apareceria na fase 5 do
    pipeline, depois da carga inteira. Aqui aparece no import."""
    erros = []
    for nome, (tabela, *cols) in INDEXES.items():
        if tabela not in TABLES:
            erros.append(f"{nome}: tabela {tabela} não existe")
            continue
        conhecidas = set(column_names(tabela))
        for c in cols:
            if c not in conhecidas:
                erros.append(f"{nome}: {tabela}.{c} não existe")
    for tabela, chave in CHAVES_NATURAIS.items():
        if tabela not in TABLES:
            erros.append(f"chave natural de {tabela}: tabela não existe")
            continue
        conhecidas = set(column_names(tabela))
        for c in chave:
            if c not in conhecidas:
                erros.append(f"chave natural {tabela}.{c} não existe")
    if erros:
        raise ValueError("; ".join(erros))


_validar_identificadores()
_validar_indices()
