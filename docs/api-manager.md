# O `/manager`

Gestão de credenciais e de consumo. É a contraparte administrativa da
[API pública](api-compativel-correios.md), e não imita contrato de ninguém —
este é nosso.

## O que ele muda no desenho do pod

O pod deixa de ser imutável.

Sem `/manager`, `/data` seria o único volume e tudo nele read-only: uma imagem
sem estado, servindo um arquivo que não muda. Com ele, aparecem dois volumes
graváveis — credenciais e log — e com eles a necessidade de backup, de retenção
e de pensar no que acontece quando o pod reinicia no meio de uma escrita.

O ganho que paga por isso: **revogar uma credencial passa a valer
imediatamente.** O token é um JWT stateless e continua criptograficamente
válido até expirar, mas a credencial é conferida a cada requisição — então
revogar derruba o acesso na próxima chamada, e não daqui a uma hora.

## Armazenamento: três volumes, três ciclos de vida

| montagem | conteúdo | ciclo |
|---|---|---|
| `/data` | a base de CEP | **read-only**, trocada a cada competência |
| `/creds` | `credenciais.db` e `estatisticas.db` | **permanente** |
| `/logs` | `consultas-<pid>-<AAAA-MM-DD>.jsonl` | janela de retenção própria |

A separação não é organização. A base é apagada e substituída todo mês; se as
credenciais estivessem na mesma pasta, **um consumidor cadastrado sumiria na
virada**. Há teste garantindo que os caminhos são distintos.

### Por que SQLite, e não Firebird

O Firebird do pod é **embedded e read-only** — é o que faz o pod não ter
servidor, não ter `security3.fdb` e não ter credencial de banco. Gravar
credenciais nele exigiria uma segunda base gravável e desfaria metade dessa
propriedade.

SQLite é uma biblioteca, um arquivo, sem processo e sem porta. `WAL` ligado, de
modo que leitores não bloqueiem as escritas do `/manager`.

### O log não passa pelo SQLite no caminho quente

Duas camadas, e a divisão é o ponto:

```
/logs/consultas-<pid>-2026-10-01.jsonl     analítico: uma linha por consulta
/creds/estatisticas.db                      sintético: agregado por mês
```

**Analítico** — uma linha JSON por requisição, append-only, **um arquivo por
worker por dia**. É isso que dispensa lock: nenhum arquivo é compartilhado entre
processos, então quatro workers escrevem em paralelo sem coordenação.

**Sintético** — `uso_mensal`, agregado por (competência, chave, rota, status,
faturável). Alimentado sob demanda por `POST /manager/manutencao/consolidar`,
que é **idempotente**: uma tabela `consolidado` guarda quais arquivos já foram
lidos, e o arquivo de hoje é pulado por padrão, porque ainda está sendo escrito.

A assimetria entre as duas é a razão de existirem duas: **podar apaga o
analítico e preserva o sintético.** O detalhe some, o total fica.

## O que o log registra

```json
{"instante":"2026-10-01T12:00:00+00:00","consumer_key":"djaR21PG...",
 "rota":"/cep/v2/enderecos/{cep}","status_http":200,"faturavel":true,
 "duracao_ms":12,"ni":null,"worker_pid":7}
```

### O CEP consultado NÃO é registrado

`ni` existe no formato e é **sempre nulo**. É decisão, não omissão.

O molde (CNPJ-XRay) preenche esse campo com o CNPJ consultado, e faz sentido lá:
um CNPJ identifica uma empresa, e saber quais empresas um cliente pesquisa é
informação comercial dele. Aqui um CEP identifica **onde alguém mora**. Guardar
quais CEPs cada cliente consulta é montar, sem precisar, um histórico de
endereços pesquisados.

O que se perde é real: não dá para investigar "por que este cliente recebeu 404
ontem" a partir do log. O que se ganha é não ter esse dado para vazar, e não
precisar de política de retenção para ele.

Pelo mesmo motivo, `rota` entra como **padrão** (`/cep/v2/enderecos/{cep}`) e
não como caminho concreto — senão o CEP voltaria por essa porta, e agrupar por
rota viraria uma chave por CEP consultado. Há teste travando isso.

### `faturavel`

Resolvido na escrita, não na leitura — assim o critério fica congelado junto com
o registro, e mudá-lo depois não reescreve o passado.

```python
NAO_FATURAVEIS = frozenset({400, 401, 403, 500, 502, 504})
```

Ou seja: **200 e 404 são faturáveis**, o resto não. 404 é consulta atendida — o
CEP foi procurado e não existe, que é uma resposta. 400 é erro do cliente, e
500 é nosso.

É esse campo que a quota consome: `consumo_do_mes` soma só os faturáveis.

## Rotas

Todas sob `/manager`, na porta **8001**.

| método | rota | o que faz |
|---|---|---|
| `GET` | `/manager/credenciais` | lista; aceita `?status=ativa\|suspensa\|revogada` |
| `POST` | `/manager/credenciais` | cria — **201**, e o único lugar onde o segredo aparece |
| `GET` | `/manager/credenciais/{chave}` | consulta, com o consumo do mês |
| `PATCH` | `/manager/credenciais/{chave}` | altera nome, documento, e-mail, quota, observação ou status |
| `POST` | `/manager/credenciais/{chave}/rotacionar` | novo segredo, mesma chave |
| `POST` | `/manager/credenciais/{chave}/token-mcp` | token de longa duração para o `/mcp`; `?dias=` de 1 a 365, padrão 90 — ver `docs/mcp.md` |
| `DELETE` | `/manager/credenciais/{chave}` | revoga (não apaga) |
| `GET` | `/manager/estatisticas` | resumo geral; aceita `?competencia=AAAA-MM` |
| `GET` | `/manager/estatisticas/{chave}` | por credencial, com `percentualDaQuota` |
| `POST` | `/manager/manutencao/consolidar` | analítico → sintético; `?incluir_hoje=true` opcional |
| `POST` | `/manager/manutencao/podar` | apaga `.jsonl` consolidado; exige `?dias=` |

Erros aqui saem como `{"detail": ...}`, do `HTTPException` do FastAPI — a API
pública sai como `{"message": ...}`, para casar com o contrato dos Correios. Os
dois formatos convivem de propósito.

O `422` do FastAPI **fica** nesta aplicação, ao contrário da pública: aqui os
corpos são modelos pydantic de verdade, validados, e o 422 realmente acontece.

## Credenciais

| campo | |
|---|---|
| `consumer_key` | 28 caracteres alfanuméricos, gerados por `secrets` |
| `secret_hash` / `secret_salt` | PBKDF2-HMAC-SHA256, 240.000 iterações, salt por credencial |
| `contratante_nome` | obrigatório |
| `contratante_documento` / `contratante_email` | opcionais |
| `status` | `ativa`, `suspensa` ou `revogada` |
| `quota_mensal` | consultas faturáveis por mês; nulo = sem limite |
| `criada_em` / `revogada_em` | ISO-8601 UTC |

Quatro decisões que valem estar escritas:

**O segredo aparece uma única vez.** Só o hash é guardado. Não há rota que o
devolva, e não há como recuperá-lo — apenas rotacionar. Há teste que varre o
arquivo `.db` em busca do segredo em claro.

**O token do MCP é emitido aqui, e só aqui.** Ele vive meses, e um token
de meses é coisa que só o operador cunha — a API pública emite apenas o
token de uma hora do contrato dos Correios. Ele cai na hora se a
credencial for revogada ou suspensa, e cai também se o segredo for
rotacionado. Detalhes em `docs/mcp.md`.

**Rotacionar mantém a chave.** Trocar o segredo não obriga o cliente a mudar a
identidade dele em nenhum outro sistema — e o histórico de uso continua ligado
à mesma chave.

**Revogar não apaga.** Marca `status` e `revogada_em`. Apagar destruiria o
histórico de consumo junto, e é justamente o histórico de um cliente que saiu
que alguém vai querer consultar depois.

**`autenticar` não revela qual das três condições falhou.** Chave inexistente,
segredo errado e credencial inativa devolvem o mesmo `None`. E numa chave
inexistente ainda se calcula um hash descartável, para que o tempo de resposta
não denuncie se a chave existe.

## Quota: contratada contra observada

`quota_mensal` é o que foi **contratado**; `consumo_do_mes` é o que foi
**observado**, vindo do sintético. A API pública compara os dois na dependência
de autenticação e devolve **403** quando estourou.

403 e não 429, pela mesma razão do molde: é restrição contratual, e 429
introduziria um código que um cliente escrito para o contrato original não
espera.

Consequência operacional: a quota só é exata depois de consolidar. Entre
consolidações o consumo do dia corrente não conta — o que erra **a favor do
cliente**, e é o lado certo para errar.

## Autenticação do `/manager`

**Não tem.** E é essa a proteção.

A barreira é a **porta não estar exposta**: não há Service, não há Ingress, e o
manifest não menciona 8001 em lugar nenhum. Uma credencial capaz de criar
credenciais seria escalada de privilégio, e uma senha de admin daria a falsa
impressão de que expor a porta é aceitável desde que a senha seja boa.

Há teste garantindo que nenhuma das duas aplicações contém as rotas da outra.

### Como usar na prática

O `CMD` da imagem sobe **só** a API pública. No cluster, o `/manager` roda como
**segundo container do mesmo pod**, ligado a `127.0.0.1:8001` — não alcançável
de outro pod nem da rede. O `kubectl port-forward` chega nele mesmo assim,
porque o encaminhamento entra no namespace de rede do pod:

```bash
kubectl -n olympus port-forward deploy/hestia 8001:8001
```

Segundo *container*, e não segundo processo no mesmo container: a separação em
processos já é exigência do desenho de `servir.py`, e containers do mesmo pod
compartilham a rede — então a propriedade de segurança é idêntica, e em troca o
kubelet reinicia cada um por conta própria, sem supervisor escrito à mão.

O sidecar **não monta `/data`**: o `/manager` não consulta a base, só lê
credenciais e consolida log. Não montar o que não se usa é a diferença entre
"não lê a base" e "não pode". Custo medido no pod: 35 MB de RSS.

```bash
curl -s -X POST localhost:8001/manager/credenciais \
  -H 'Content-Type: application/json' \
  -d '{"contratante_nome":"ACME Logística Ltda","quota_mensal":100000}'
```

Localmente, sem cluster:

```bash
python -m src.api.servir --manager
```

O `--host` padrão é `127.0.0.1` de propósito, e `servir.py` avisa no stderr se
alguém pedir o manager escutando em `0.0.0.0`.

## Rotina sugerida

```bash
# mensal, antes de faturar
curl -s -X POST localhost:8001/manager/manutencao/consolidar
curl -s localhost:8001/manager/estatisticas?competencia=2026-10
```

```bash
# quando o volume de log incomodar — exige o prazo, não adivinha
curl -s -X POST 'localhost:8001/manager/manutencao/podar?dias=90'
```

`podar` **recusa** rodar sem `dias`. Não há janela de retenção automática:
`API_RETENCAO_DIAS` é `0` por padrão, e adivinhar um prazo apagaria log que
alguém achava que tinha. Ele também se recusa a apagar arquivo ainda não
consolidado — o detalhe pode ir embora, o total não.

## Em aberto

- **Backup do `/creds`.** Hoje não há nenhum, e é o único dado do pod que não é
  reconstruível: a base vem do extrato DNEC e o log é descartável, mas as
  credenciais existem só ali. Um `sqlite3 .backup` periódico para fora do node
  resolveria, e ainda não está feito.
- **Suspender por quota** em vez de só recusar com 403 — hoje o cliente que
  estoura volta a ser atendido na virada do mês, sem ação nenhuma.
