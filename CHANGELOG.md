# 📝 Changelog — CEP-XRay

## [0.1.0] - 2026-09-21

Primeira versão. O projeto nasce inteiro: ETL, base, API, testes e deploy.

### ✨ Novo

- **Tokenizador de literais SQL** (`src/etl/leitura.py`) — o único módulo sem
  molde no CNPJ-XRay. A fonte é SQL, não CSV, e nem `read_csv` nem um parser
  linha a linha servem.
- **Schema declarativo** (`src/db/schema.py`) como fonte única de DDL, `INSERT`
  parametrizado, DDL de índice e mapeamento coluna-da-fonte. 15 tabelas, 23
  índices, guarda de 31 caracteres no import.
- **Pipeline de 11 fases** com checkpoint e retomada, no molde do CNPJ-XRay.
- **API compatível com a Busca CEP dos Correios**, com todas as rotas do manual
  na especificação e `501` nas três que a base não alimenta.
- **Publicação invertida** — envia e confere com o pod no ar, para segundos para
  renomear, **com rollback em disco**.
- **Relatório de qualidade** que mede a base pronta e nunca aborta.
- 189 testes, sem Firebird e sem rede.

### ⚡ Decisões medidas

- **Colunas `_SA` fora** — 71,4 MB de 221,3 MB do conteúdo (32,3%). A collation
  `WIN_PTBR` já compara sem acento e sem caixa; confirmado na base construída.
- **Coluna `logradouro` fora** — 33,5 MB. É `tipo || ' ' || nome_logradouro` em
  1.720.956 das 1.720.964 linhas, e o contrato pede as duas partes separadas.
- **Lat/long em `DOUBLE PRECISION`.** `FLOAT` custaria metade e erraria no
  máximo 39 cm, mas **nenhum** dos 3.441.928 valores voltaria igual à fonte.
  `NUMERIC(16,13)` custa o mesmo que `DOUBLE` e obrigaria a formatar `Decimal`.
- **Controle C1 removido, não substituído.** Os 2 `U+0081` da fonte estão
  colados num `Á`: remover restaura `Águas`, `?` produziria `A?guas`.
- **Paralelismo por arquivo, não por bloco.** Tokenizar SQL é CPU; reler o
  arquivo em N workers multiplicaria o parsing por N em vez de dividi-lo.
- **`dedup.py` não existe** — zero chaves repetidas medidas. A DETECÇÃO fica no
  validador, como guarda contra carregar o mesmo arquivo duas vezes.

### 🐛 Correções que custaram caro

- **Ordem de registro das rotas 501.** Registradas depois de
  `/cep/v1/localidades/{uf}`, o Starlette casava `cliques` como sigla de UF e
  devolvia 400 no lugar do 501.
- **Colisão de rótulo no cursor.** `logradouro.estado` (sigla) e
  `estado.estado` (nome) chegavam com o mesmo nome e o segundo sobrescrevia o
  primeiro: a resposta traria "São Paulo" onde promete "SP".
- **Dupla normalização.** `Carregador.carregar` normaliza por dentro;
  normalizar antes fazia a segunda passada falhar com "expected String type,
  got: f64".

### 📊 Resultado

**2.171.508 linhas em 238,8 MB**, construídas em 5,8 min com 4 processos. O FDB
2.5 recebido tem os mesmos dados em 498 MB — **52% maior**, por carregar as
colunas redundantes e não ter `USE_FULL`.

---

Versionamento: [SemVer](https://semver.org/lang/pt-BR/).
MAJOR quebra compatibilidade, MINOR acrescenta, PATCH corrige.
