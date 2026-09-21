# 📮 CEP-XRay

ETL e consulta da base de CEP brasileira em **Firebird 3.0**, com API compatível
com a [Busca CEP dos Correios](https://www.correios.com.br/atendimento/developers/manuais/manual-api-busca-cep).

São 15 arquivos `.sql` do extrato DNEC, ~331 MB e 2.171.508 linhas, que viram
uma base Firebird de **239 MB** pronta para consulta.

## Como funciona

A base de produção é **somente leitura** e nunca é alterada no lugar. Toda
competência nova constrói uma base do zero num arquivo de staging e só entra em
produção pela troca do arquivo (blue-green). Isso torna a atualização atômica e
reversível até o último passo, e permite carregar sem nenhuma trava — sem
PRIMARY KEY, NOT NULL ou FOREIGN KEY.

Não é descuido: `logradouro.distrito_id` é nulo em 96% das linhas, e um
`NOT NULL` ingênuo mataria a carga. Os índices existem **só** para performance
de consulta e são criados depois. O relatório de
[`src/validation/qualidade.py`](src/validation/qualidade.py) mede a base pronta
em vez de abortar por causa dela.

## A fonte é SQL, não CSV

Esta é a diferença central em relação ao [CNPJ-XRay](https://github.com/robsonrbranco/CNPJ-XRay),
de onde o resto do projeto foi herdado. Cada arquivo é um `CREATE TABLE` seguido
de centenas de milhares de `INSERT ... VALUES (...),(...)`.

[`src/etl/leitura.py`](src/etl/leitura.py) é um **tokenizador de literais SQL em
streaming**, e é o único módulo sem molde. Três razões, todas medidas:

- há valores com **quebra de linha dentro das aspas** — um parser linha a linha
  corromperia dado em silêncio;
- o tokenizador distingue **`NULL` de string vazia**, que um leitor de CSV não
  consegue: no CSV essa informação já se perdeu no formato;
- as colunas vêm da **lista do `INSERT`**, nunca do `CREATE TABLE` — o
  `CREATE TABLE` de `TBL_CEP_2610_ESTADO_IBGE.sql` está errado, declara outra
  tabela com outra lista de colunas.

## Três decisões de schema, com o número que as justifica

**As 11 colunas `_SA` ("sem acento") não entram — 32,3% do conteúdo.** A base é
criada com `WIN1252 COLLATION WIN_PTBR`, que já compara ignorando acento e
caixa. Verificado na base construída:

```sql
SELECT cidade FROM cidade WHERE cidade = 'sao paulo';   -- devolve 'São Paulo'
```

**A coluna `logradouro` não entra — 33,5 MB.** É `tipo || ' ' || nome_logradouro`
em 1.720.956 das 1.720.964 linhas, e o contrato dos Correios pede
`tipoLogradouro` e `logradouro` separados — ou seja, pede as duas colunas de
origem.

**Latitude e longitude são `DOUBLE PRECISION`.** Medido sobre 3.441.928 valores:

| | bytes | pior erro físico | volta igual à fonte |
|---|---:|---:|---:|
| `VARCHAR` (fonte) | 37,6 MB | — | 100% |
| `FLOAT` | 13,8 MB | 0,39 m | **0%** |
| `DOUBLE PRECISION` | 27,5 MB | 0 | **100%** |
| `NUMERIC(16,13)` | 27,5 MB | 0 | 100% |

`FLOAT` custa metade e 39 cm é irrelevante para endereçamento postal, mas
`-23.5629` lê de volta como `-23.562900543212890625` — ruído em toda resposta.
`NUMERIC` acerta pelo motivo errado: exatidão decimal existe para centavos, não
para coordenada.

## Estrutura

```
src/
  db/          camada Firebird: schema, conexão, carga, índices
  etl/         tokenizador dos .sql e pipeline de construção
  blue_green/  troca de arquivo, validação e modos de acesso
  validation/  relatório de qualidade da base
  consulta/    ficha de CEP e listagens
  api/         API compatível com os Correios + manager
  publicacao/  manifesto e envio da base para o pod
```

## Pré-requisitos

- Python 3.13
- Firebird 3.0 rodando (o `fbclient.dll`/`libfbclient.so` acessível ao cliente)
- ~1 GB livre

## Instalação

```bash
git clone https://github.com/robsonrbranco/CEP-XRay.git
cd CEP-XRay
python -m venv .venv
```

```bash
.venv\Scripts\activate          # Windows;  no Linux: source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

## Uso

### Conferir a fonte, sem tocar no banco

```bash
python -m src.etl.leitura db/firebird-querys
```

Imprime as 15 contagens de linha. É o teste falsificável de que o tokenizador
está certo, e roda sem Firebird.

### Construir a base

```bash
python -m src.etl.pipeline --origem db/firebird-querys --switch --processos 4
```

Onze fases: base nova → modo carga → tabelas sem trava → carga em N processos →
índices → conferência de chave repetida → proveniência → estatísticas → modo
produção → validação → troca → read-only. Sem `--switch` a base fica em staging.
Com `--continuar` retoma de onde parou.

Leva cerca de 6 minutos.

### Consultar

```bash
python -m src.consulta.cep 01001000
```

```bash
python -m src.consulta.cep 01001-000 --json
```

### Relatório de qualidade

```bash
python -m src.validation.qualidade --producao
```

### Subir a API

```bash
python -m src.api.servir
```

## Consulta SQL direta

`JOIN` tem que ser `LEFT JOIN`: `bairro_id` é nulo em 6,5% dos CEPs e
`distrito_id` em 96%, e um `INNER JOIN` faria a maioria das linhas sumir.

```sql
SELECT l.cep, l.tipo, l.nome_logradouro, b.bairro, c.cidade, l.estado, c.ddd
FROM logradouro l
LEFT JOIN bairro b ON b.id_bairro = l.bairro_id
LEFT JOIN cidade c ON c.id_cidade = l.cidade_id
WHERE l.cep = '01001000';
```

A base usa `WIN1252` com collation `WIN_PTBR`, que compara **sem acento e sem
caixa**. O dado gravado continua fiel à fonte.

## Dados

| tabela | linhas (2026-10) |
|---|---:|
| `logradouro` | 1.720.964 |
| `log_complemento` | 241.725 |
| `bairro` / `bairro_faixa` | 76.969 |
| `log_grande_usuario` | 20.508 |
| `distrito` / `distrito_faixa` | 5.856 |
| `cidade` e as três tabelas de município | 5.571 |
| `paises_ibge` | 259 |
| `estado_faixa` | 64 |
| `estado` / `estado_ibge` | 27 |

## A API

Compatível com o contrato dos Correios, com três diferenças que são da fonte e
não do código — o retrato mensal, seis campos sempre nulos, e três recursos que
devolvem `501`. Detalhes em [`docs/api-compativel-correios.md`](docs/api-compativel-correios.md).

A gestão de credenciais e de consumo roda numa segunda aplicação, em processo e
porta separados e **não exposta** — ver [`docs/api-manager.md`](docs/api-manager.md).
O CEP consultado **não vai para o log**: um CNPJ identifica uma empresa, um CEP
identifica onde alguém mora.

## Origem dos dados

Extrato DNEC/Correios, entregue como scripts `.sql`. A competência vem do token
no nome do arquivo (`TBL_CEP_**2610**_LOGRADOURO.sql`), na convenção `AAMM` do
e-DNE — `2610` é outubro de 2026. O `sha256` de cada arquivo é gravado **dentro
da base**, em `metadados`, porque os arquivos chegam por um canal que não
republica manifesto: é a única forma de, meses depois, provar de qual entrega
aquela base saiu.
