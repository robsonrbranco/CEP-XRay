# CLAUDE.md — CEP-XRay

Contexto para quem (ou o que) for trabalhar neste repositório.

## O que é

ETL e consulta da base de CEP brasileira (extrato DNEC/Correios) em **Firebird
3.0**, com API que espelha o contrato da Busca CEP dos Correios. Projeto irmão
do `CNPJ-XRay`, do qual herdou a camada de banco, o blue-green, a API de
credenciais e a publicação.

## Quatro regras que sustentam o desenho

Herdadas do CNPJ-XRay, e valem pelas mesmas razões.

1. **Firebird 3.0 é requisito fixo**, não preferência. Não propor 4.0/5.0.
2. **Produção é base de CONSULTA.** Usuário nunca altera dado. O banco fica
   `read-only` no header — não é combinado operacional, é o engine recusando
   escrita.
3. **Nunca há atualização incremental.** Cada competência constrói uma base nova
   do zero em `_staging.fdb` e entra em produção pela troca de arquivo.
4. **A carga é otimista, sem nenhuma trava.** Sem PRIMARY KEY, NOT NULL ou
   FOREIGN KEY. Índices existem só para performance de consulta e são criados
   depois da carga.

A razão da regra 4 aqui é diferente da do CNPJ, e é medida:
`logradouro.distrito_id` é **NULL em 1.645.011 das 1.720.964 linhas (96%)** e
`bairro_id` em 111.700. Um `NOT NULL` ingênuo mataria a carga.

## O que muda em relação ao CNPJ-XRay

| | CNPJ-XRay | CEP-XRay |
|---|---|---|
| fonte | CSV em `.zip`, baixado por HTTP | `.sql` entregues como arquivo |
| leitor | `pl.read_csv` | **tokenizador de literais SQL** |
| volume | 222 M linhas, 34,2 GB | 2,17 M linhas, 239 MB |
| carga | ~6 h, fatiada por bloco | ~6 min, um arquivo por worker |
| chave repetida | existe na fonte, é REMOVIDA | não existe; a checagem vira relatório |
| publicação | apaga a antiga, transmite, 1–4 h fora do ar | transmite com o pod no ar, para segundos, **com rollback** |
| pod | `themis` | `hestia` |

Não existem aqui: `etl/download.py` (os dados chegam como arquivo) e
`db/dedup.py` (nada a remover — mas a DETECÇÃO fica no validador, como guarda
contra carregar o mesmo arquivo duas vezes).

## Mapa do código

| módulo | papel |
|---|---|
| `src/db/schema.py` | **fonte da verdade**: tabelas, larguras, índices, chaves naturais, mapeamento coluna-da-fonte |
| `src/etl/leitura.py` | **o único módulo sem molde**: tokenizador de literais SQL |
| `src/etl/carga.py` | um arquivo por worker; competência e sha256 da fonte |
| `src/etl/pipeline.py` | as 11 fases da construção |
| `src/db/loader.py` | carga em massa com `EXECUTE BLOCK` de 256 inserts |
| `src/consulta/cep.py` | ficha de CEP, com o fallback por faixa |
| `src/api/correios.py` | tradução para o contrato dos Correios |
| `src/api/publica.py` | as rotas, os 501, o OpenAPI |
| `src/api/mcp.py` | a camada MCP para agentes (`POST /mcp`), com token de audiência própria — ver `docs/mcp.md` |
| `src/validation/qualidade.py` | relatório, nunca aborto |

## Comandos

```bash
python -m src.etl.leitura db/firebird-querys
```

```bash
python -m src.etl.pipeline --origem db/firebird-querys --switch --processos 4
```

```bash
python -m src.consulta.cep 01001000
```

```bash
python -m src.validation.qualidade --producao
```

## Armadilhas já pagas

- **A fonte tem valor com quebra de linha DENTRO das aspas.** Um parser linha a
  linha corrompe em silêncio. São 2 nesta competência, e a corrupção só
  apareceria meses depois.
- **O `CREATE TABLE` da fonte não presta.** `TBL_CEP_2610_ESTADO_IBGE.sql`
  declara `CREATE TABLE TBL_CEP_2610_ESTADO`, com outra lista de colunas.
  **Nenhum código deste projeto lê `CREATE TABLE` da fonte** — as colunas vêm do
  `INSERT`.
- **A ordem das alternativas do regex do tokenizador é semântica.** `cabeçalho`
  antes de `número`, senão o `2022` de `POPULACAO_RESIDENTE_2022` vira um valor.
  E nenhum valor é coletado antes do primeiro `INSERT`, senão os `VARCHAR(100)`
  do `CREATE TABLE` viram números.
- **Controle C1 é REMOVIDO, não substituído.** Os 2 `U+0081` da fonte estão
  colados num `Á`: remover restaura `Águas`, trocar por `?` produz `A?guas`.
- **`NULL` e `''` são casos distintos e precisam sobreviver ao leitor.** Medido:
  nunca coexistem na mesma coluna, o que torna a regra `"" → NULL` do loader
  hoje sem perda. `qualidade.py` vigia a premissa.
- **`FLOAT` perderia o round-trip de 100% das coordenadas.** Ver a tabela em
  `db/schema.py`. Não trocar sem refazer a medição.
- **`estado_faixa.faixa_ini` não é numérico** — há `'72800/74000'`. As faixas
  são `VARCHAR`, e CEP tem zero à esquerda.
- **As três rotas 501 têm que ser registradas ANTES de
  `/cep/v1/localidades/{uf}`**, senão o Starlette casa `cliques` como sigla de
  UF. Travado por teste.
- **O nome do `.fdb` é minúsculo, e em Linux isso importa.** A fonte chega como
  `TBL_CEP_202609_FDB.FDB`, em caixa alta. `deploy/trocar-base.sh` e
  `publicacao/enviar.py` recusam nome com maiúscula, porque o sintoma seria o
  pod subindo e o `/saude` devolvendo 503 com o arquivo ali ao lado.
- **Não normalizar o DataFrame antes de `Carregador.carregar`** — ele normaliza
  por dentro, e a segunda passada falha com "expected String type, got: f64".

## Duas tentações medidas e recusadas

Estão registradas em comentário no código para que ninguém as "descubra" de novo:

- **derivar cliques / caixas postais / agências modulares por `LIKE` na prosa do
  complemento.** Medido: `Clique e Retire Correios` em 8.481 linhas, `locker` em
  119, `caixa postal` em 4, `agência modular` em 0. Esses textos descrevem
  *aquele CEP*, não a cobertura do município;
- **inferir `numeroInicial`/`numeroFinal` por regex** sobre `'até 318 lado par'`.
  `'ao fim'` não é número, e o cliente receberia um intervalo que a fonte nunca
  afirmou.

## Ao medir performance

Confirmar com um sinal que não venha do mesmo cronômetro, e checar se os dois
braços de uma comparação medem o mesmo escopo. Tempo dentro de uma chamada não é
tempo gasto por ela, e probe num caminho não decide sobre outro.
