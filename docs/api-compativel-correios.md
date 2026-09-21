# A API compatível com a Busca CEP dos Correios

O objetivo é que um cliente escrito para a API dos Correios funcione aqui
trocando apenas a URL base e as credenciais.

## O que é compatível, e o que não pode ser

**Compatível:** caminhos, nomes de campo, camelCase, aninhamento, códigos de
retorno, e o fluxo de autenticação em dois passos.

**Não compatível, e sem conserto possível:**

1. **A base é um retrato de uma competência**, não o DNE ao vivo. Um CEP criado
   depois do fechamento não está aqui.
2. **Seis campos saem sempre nulos** (abaixo).
3. **Três recursos devolvem 501** (abaixo).

As três são propriedades da fonte, não do código. Estão documentadas no topo da
descrição da API e na página em `/` porque descobri-las meses depois, num
chamado de suporte, é o pior momento.

## Autenticação

```
POST /token/v1/autentica
Authorization: Basic base64(usuario:codigoAcesso)

201
{
  "token": "eyJhbGciOiJIUzI1NiJ9...",
  "emissao": "2026-10-01T11:00:00+00:00",
  "expiraEm": "2026-10-01T12:00:00+00:00",
  "ambiente": "PRODUCAO",
  "apis": ["cep"]
}
```

O token é um JWT HS256 assinado por `API_JWT_SEGREDO`, com `sub`, `iat`, `exp` e
`scope`. `src/api/token.py` **nunca lê o `alg` do cabeçalho**: calcula HS256 e
compara, então `alg: none` e confusão RS256→HS256 são rejeitados como assinatura
ruim comum.

O token é stateless, mas a credencial é conferida a cada requisição — revogar
uma credencial vale **imediatamente**, sem esperar o token expirar.

Quota estourada devolve **403**, não 429: é restrição contratual, e 429
introduziria um código que um cliente escrito para o contrato original não
espera.

## Endpoints

| rota | códigos |
|---|---|
| `GET /cep/v2/enderecos/{cep}` | 200 400 401 403 404 500 |
| `GET /cep/v2/enderecos` | 200 400 401 404 500 501 |
| `GET /cep/v1/localidades[/{uf}]` | 200 400 401 404 500 501 |
| `GET /cep/v1/bairros/{uf}/localidades/{id}` | 200 400 401 404 500 |
| `GET /cep/v1/ufs[/{uf}]` | 200 400 401 404 500 |
| `GET /cep/v1/atualizacao` | 200 401 |
| `GET /cep/v1/localidades/{cliques,caixas-postais,agencias-modulares}` | 401 **501** |
| `GET /saude` | 200 503 — sem autenticação, fora de `/cep/` |

## 400 contra 404: a diferença estrutural com o CNPJ

No CNPJ-XRay a validação de dígito verificador permite reprovar um número mal
formado **antes** de ir ao banco, e isso importa porque 404 é faturável e 400
não.

**CEP não tem dígito verificador.** A validação aqui é de forma e só:

- comprimento diferente de 8 dígitos, ou caractere que não é dígito → **400**;
- 8 dígitos que não existem na base → **404**.

Qualquer sequência de 8 dígitos é bem formada, inclusive `00000000` e
`11111111`. Há um teste (`test_cep_nao_tem_digito_verificador`) só para travar
essa não-regra: inventar um DV faria a API recusar CEP legítimo com o código
errado.

## O fallback por faixa, e o que ele afirma

Um CEP que não está em `logradouro` ainda pode ser válido: é o CEP geral de um
município, sem logradouro individualizado. Nesse caso a resposta vem da faixa
(`cidade_faixa`) e sai com `tipoCEP: 2`.

Isso responde **"a que município este CEP pertence"** — não "este CEP está
cadastrado". Medido: as 5.571 faixas cobrem **91,6%** dos 100 milhões de números
de 8 dígitos. Consequência prática:

```
01001000 -> 200, tipoCEP 1   (logradouro próprio: Praça da Sé)
12345678 -> 200, tipoCEP 2   (faixa de Jacareí/SP)
00000000 -> 404
20000000 -> 404
```

Quem precisa da distinção olha `tipoCEP`. Estreitar o fallback exigiria inventar
um critério de "CEP geral" que a fonte não publica.

## De onde vem cada campo

| campo | origem |
|---|---|
| `cep` | `logradouro.cep` |
| `uf` | `logradouro.estado` |
| `localidade` | `cidade.cidade` via `cidade_id` |
| `tipoLogradouro` | `logradouro.tipo` |
| `logradouro` | `logradouro.nome_logradouro` |
| `bairro` | `bairro.bairro` via `bairro_id` — nulo em 6,5% dos CEPs |
| `complemento` | `log_complemento.complemento` — presente em 14% |
| `nomeUnidade` | `log_grande_usuario.grandes_usuarios` — 20.508 CEPs |
| `tipoCEP` | derivado: 3 se há grande usuário, 2 se veio de faixa ou `tipo` vazio, senão 1 |
| `lado` | derivado do texto do complemento |
| `numeroLocalidade` | **sempre nulo** |
| `abreviatura` | **sempre nulo** |
| `cepUnidadeOperacional` | **sempre nulo** |
| `numeroInicial` / `numeroFinal` | **sempre nulos** |

Os campos nulos vêm **presentes na resposta**, nunca ausentes: um cliente gerado
do OpenAPI espera a chave, e omiti-la quebraria a desserialização.

### Duas decisões que valem justificar

**`numeroLocalidade` nunca recebe o código do IBGE.** Temos `cidade.id_cidade`
(chave surrogate do extrato) e `cidade.cidade_ibge` (numeração do IBGE). O campo
promete `LOC_NU`, o código de localidade dos Correios, que é outra coisa.
Entregar a numeração do IBGE ali seria o pior erro possível: o cliente não teria
como perceber, e usaria um número que casa com nada. O código do IBGE sai sob o
seu próprio nome, `codigoIbge`, no recurso de localidade.

**`numeroInicial`/`numeroFinal` não são inferidos da prosa.** O complemento traz
`'até 318 lado par'` e `'de 320 ao fim lado par'`, e um regex tiraria números
dali. `'ao fim'` não é número, e o cliente receberia um intervalo que a fonte
nunca afirmou. O texto cru continua em `complemento`.

**`lado` é derivado**, e com cobertura declarada: medido nos 241.725
complementos, 22.440 dizem `lado par`, 22.434 dizem `lado ímpar`, e 51 contêm os
dois (faixa composta). Os 51 saem **nulos** — quando o campo não tem resposta
única, escolher uma seria chutar.

**`tipoCEP` nunca emite 4 nem 5.** Unidade operacional e caixa postal
comunitária não são distinguíveis no extrato.

## Os três 501

`cliques`, `caixas-postais` e `agencias-modulares` respondem "quais LOCALIDADES
oferecem tal serviço", e nenhuma das 15 tabelas tem atributo de serviço.

A tentação foi medida: `log_complemento.complemento` contém `Clique e Retire
Correios` em **8.481** linhas, `locker` em **119**, `caixa postal` em **4** e
`agência modular` em **0**. Daria para derivar as listas por `LIKE`.

Não dá. Esses textos descrevem *aquele CEP*, não a cobertura da localidade; uma
lista derivada assim pareceria autoritativa e seria um palpite — e para
caixas-postais, com 4 acertos, seria um palpite obviamente errado.

Filtro não alimentável na listagem (`siglaUnidade`, `clique`, `caixaPostal`,
`locker`, `agenciaModular`, `numeroCaixaPostal`, `numero`) também devolve 501
nomeando o filtro. A alternativa — ignorá-lo e devolver 200 com o conjunto não
filtrado — é pior: o cliente acha que filtrou.

## Uma ressalva honesta

O **corpo** das respostas de erro não foi conferido contra uma resposta real dos
Correios: o Swagger deles exige credencial CWS, e o manual público lista os
códigos, não o JSON. Seguimos `{"message": "..."}`, centralizado em
`MENSAGENS` de `src/api/publica.py`. Se um cliente real depender do formato do
corpo, é ali que se ajusta.

## O `/manager`

Roda em processo e porta separados (8001), **não exposta**. Uma credencial capaz
de criar credenciais seria escalada de privilégio, e separar por processo é a
única barreira que não depende de nenhum segredo estar certo. Há teste
garantindo que nenhuma das duas aplicações contém as rotas da outra.
