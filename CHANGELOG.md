# 📝 Changelog — CEP-XRay

## [0.2.1] - 2026-09-21

### 🐛 Correção

- **`criar_app` deixou de depender de quem carregou o ambiente.** `ConfigAPI`
  cobria só metade do que a aplicação precisa — JWT, volumes, site — e a outra
  metade (`DB_NAME`, `FB_EMBEDDED`, …) era lida do `os.environ` lá no fundo, na
  **primeira consulta**. Era uma dependência que nenhuma assinatura declarava.

  O sintoma vale guardar, porque é dos que não parecem um bug: a aplicação
  funcionava se a primeira requisição do processo fosse de CEP e devolvia 500
  se fosse a busca paginada. A causa são os imports preguiçosos de `Consultas`
  — o caminho do CEP importa `consulta/cep.py`, que é **também uma CLI** e faz
  `load_dotenv` no topo do módulo; o caminho da busca importa
  `consulta/listagem.py`, que não é CLI e não faz. O comportamento da API
  dependia da **ordem das rotas**, por efeito colateral de import.

  E o erro apontava para o lugar errado: sem `FB_EMBEDDED` o DSN vira
  `inet://localhost:3050/` e o driver responde *"Your user name and password
  are not defined. Ask your database administrator to set up a Firebird
  login"* — mandando procurar servidor, login e administrador que não existem,
  porque a base é embedded num arquivo ao lado. Custou três rodadas de
  investigação no lugar errado.

  **Produção nunca sofreu**: o `Dockerfile` define as variáveis por `ENV` e o
  entrypoint é o `servir.py`. Quem pagava era todo o resto — scripts, testes de
  integração, um `uvicorn src.api.publica:app` futuro.

  A correção é a configuração do banco entrar pelo `ConfigAPI`, como já entram
  JWT e volumes: `do_ambiente()` monta tudo de uma vez e `criar_app` repassa ao
  `ConexaoViva`. `load_dotenv` volta a ser assunto exclusivo dos entrypoints.
  Duas guardas novas trocam erro tardio e enganoso por erro imediato e honesto:
  `do_ambiente()` recusa `DB_NAME` vazio nomeando `DB_NAME`, e `criar_app`
  recusa uma `ConfigAPI` sem banco quando vai de fato abrir o Firebird.

- **A versão do pacote acompanha o CHANGELOG.** Mesma convenção adotada no
  CNPJ-XRay: `pyproject.toml` e o topo deste arquivo descrevem o mesmo estado.
  Os `version=` de `api/publica.py` (2.0) e `api/manager.py` (1.0) não entram
  nessa conta — versionam o contrato HTTP no OpenAPI, e o `2.0` corresponde às
  rotas `/cep/v2/` dos Correios.

### 📊 Resultado

**213 testes** (+6). Os novos estão em `tests/test_api_autossuficiencia.py` e
olham a **construção**, não a consulta — é o que dá para falsificar sem
Firebird, e é justamente por olhar só a consulta, com dublê injetado, que a
suíte antiga não via nada.

---

## [0.2.0] - 2026-09-21

O projeto sai do disco e entra em produção: **https://hestia.ecomciencia.com**,
servindo a competência 2026-10 no cluster Olympus.

Quase tudo abaixo foi encontrado *fazendo* — subindo o serviço, rodando o
procedimento de publicação, lendo o manifest do serviço irmão. Nenhum teste de
rota teria pego a maioria.

### 🚀 Em produção

- Repositório público, deployment `hestia` no namespace `olympus`, DNS pelo
  Terraform do `infra-olympus`, TLS terminando no Cloudflare.
- Base publicada pelo caminho desenhado (`preparar` → `enviar` → `trocar`):
  239 MB comprimidos para 79 MB (3,02x), com **downtime de segundos** — a
  inversão de ordem em relação ao Themis funcionou como prometido.
- Garantias de somente-leitura confirmadas nos dois níveis:
  `touch /data/teste` → *Read-only file system* (kernel);
  `UPDATE` → *attempted update on read-only database* (engine).
- Backup diário do `/creds` para o R2, com snapshot consistente dos SQLite
  pela API de backup online — `cp` num banco em WAL produz backup rasgado, que
  parece ter funcionado e só falha no dia do restore.

### ✨ Novo

- **`/manager` como sidecar**, ligado a `127.0.0.1:8001`. A primeira versão
  usava `kubectl exec` em duas sessões, com o argumento de que um processo
  sempre no ar seria superfície a mais. O Themis já tinha medido e o argumento
  não se sustenta: 35 MB de RSS. E o sidecar ganha o que o `exec` não dá — o
  kubelet reinicia o processo sozinho, sem supervisor escrito à mão.
- **Credencial de CI estreita.** O `KUBECONFIG_OLYMPUS` seria o kubeconfig do
  k3s — `system:admin` em `system:masters`, poder total sobre os sete serviços,
  baseado em certificado X509 que o k3s não revoga individualmente. No lugar,
  uma ServiceAccount que escreve **apenas no `hestia`**, por `resourceName`.
- **`docs/api-manager.md`**, com as decisões de armazenamento, credencial e
  quota.

### ⚡ Decisões medidas

- **A imagem caiu 42%: 140 MB → 81 MB.** O pod carregava o ETL inteiro e nunca
  o roda. Saíram `polars` (181 MB), `rich` + `pygments` (12 MB) e `pip`
  (12 MB); `site-packages` foi de 252 MB para 46 MB e o pull de 17,3 s para
  4,9 s. Não foi palpite: o grafo de import da API foi medido num processo
  limpo — 141 módulos de topo, nenhum alcançando os três — e confirmado no
  processo servindo (`grep -c polars /proc/1/maps` → 0).
- **O CEP consultado NÃO vai para o log**, e isso é decisão declarada, não
  omissão. O molde registra o CNPJ consultado, e faz sentido lá: um CNPJ
  identifica uma empresa. Um CEP identifica **onde alguém mora**. O que se
  perde é real — não dá para investigar "por que este cliente recebeu 404
  ontem" pelo log. O que se ganha é não ter esse dado para vazar.
- **O fallback por faixa cobre 91,6%** dos 100 milhões de números de 8 dígitos,
  medido. Então `99999999` devolve 200 (topo da faixa do RS) e `00000000`
  devolve 404. Ele responde "a que município este CEP pertence", não "este CEP
  está cadastrado" — quem precisa da distinção olha `tipoCEP`.

### 🐛 Correções que custaram caro

- **O `/manager` não construía.** Ao reescrever `api/esquemas.py` para o
  contrato dos Correios, os modelos que só ele usa ficaram de fora, e a
  aplicação estourava `AttributeError` no import. Nenhum teste pegou porque
  nenhum teste construía o manager. A correção de verdade foi
  `tests/test_api_manager.py`, cujo primeiro teste é literalmente "o app
  monta" — parece trivial e era o que faltava. O bug só apareceria na primeira
  vez que alguém fosse cadastrar um consumidor, via `kubectl exec`, no pior
  momento.
- **As três rotas 501 casavam como UF.** Registradas depois de
  `/cep/v1/localidades/{uf}`, o Starlette casava `cliques` como sigla e
  devolvia 400 "UF inválida" no lugar do 501 que explica o que falta. A ordem
  de registro virou requisito, travado por teste.
- **Colisão de rótulo no cursor.** `logradouro.estado` (a sigla) e
  `estado.estado` (o nome) chegavam com o mesmo nome e o segundo sobrescrevia
  o primeiro: a resposta traria "São Paulo" onde promete "SP". Todo apelido do
  SQL é explícito agora.
- **`set image` tocava só um container.** O `manager` ficava preso na tag
  `latest` enquanto a API avançava pelo SHA. Na prática coincidiriam quase
  sempre — e "quase sempre" é o pior tipo de garantia: num rollback por SHA os
  dois leriam o mesmo SQLite com códigos de versões diferentes.
- **`trocar-base.sh` oferecia rollback inexistente.** Na primeira publicação
  ele detectava corretamente que não havia base anterior, e mesmo assim
  imprimia o procedimento de rollback apontando para um arquivo que não
  existe. Quem estivesse em apuros seguiria a instrução e rodaria um `mv` que
  falha.
- **O CI reprovava por DNS ausente.** O passo que confere o `/saude` público
  não verifica o deploy — o `rollout status` já faz isso, e melhor, porque a
  readiness do pod *é* um `GET /saude`. Ele verifica o caminho público. São
  falhas diferentes: host que não resolve agora sai como aviso, host que
  responde errado continua reprovando.

### 📊 Resultado

**207 testes**, sem Firebird e sem rede. Serviço no ar com pipeline verde de
ponta a ponta: push → testes → imagem → `set image` com credencial estreita →
rollout → conferência do `/saude` público.

---

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
