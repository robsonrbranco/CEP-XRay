# Pod de consulta: Firebird 3 embedded + a API.
#
# Duas etapas para o engine. A primeira instala o Firebird inteiro só para
# extrair o que roda; a segunda leva apenas isso. O `firebird3.0-server` traz o
# executável do servidor, o security database e as ferramentas de linha de
# comando, e nada disso vai para a imagem final: o pod não escuta na 3050, não
# autentica e não faz manutenção de banco.
#
# O que de fato é necessário:
#
#   libfbclient.so.2          cliente
#   plugins/libEngine12.so    o engine, que em embedded roda no processo da app
#   firebird.conf             Providers = Engine12, ServerMode = Classic
#
# `ServerMode = Classic` não é detalhe: com `Super` o primeiro worker toma o
# arquivo e os demais morrem no attach. Medido no CNPJ-XRay, 1 de 4 contra 4 de 4.

FROM python:3.13-slim-bookworm AS engine

RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      firebird3.0-server \
 && rm -rf /var/lib/apt/lists/*


# A página de apresentação em https://hestia.ecomciencia.com/.
#
# Compilada aqui e servida pela própria aplicação, em vez de por um pod nginx à
# parte: a página documenta a API — tabela de endpoints, exemplo de
# autenticação, as diferenças em relação aos Correios — e versionar as duas
# juntas é o que impede a página de descrever uma API que já mudou.
#
# O custo é conhecido: `strategy: Recreate` no manifest, então corrigir uma
# vírgula no texto reinicia o pod que serve a API. São segundos.
FROM debian:bookworm-slim AS site

# Hugo FIXO, e não "o último".
#
# A versão era descoberta em tempo de build com uma chamada NÃO AUTENTICADA a
# `api.github.com`. Isso quebrou o deploy em 2026-09-21: a API respondeu sem
# `tag_name` (limite de requisições por IP do runner), o `grep -oP` saiu 1 e a
# cadeia `&&` parou. A mesma imagem havia sido construída com sucesso 40
# minutos antes.
#
# O problema maior não é a falha intermitente, é o que ela revela: dois builds
# do MESMO commit podiam produzir imagens com Hugos diferentes. Um commit que
# só muda Python levaria junto uma versão nova do compilador do site, sem que
# nada no diff dissesse isso.
#
# Para atualizar, as duas linhas mudam JUNTAS:
#
#   V=0.167.0
#   curl -sL https://github.com/gohugoio/hugo/releases/download/v$V/hugo_${V}_checksums.txt \
#     | grep hugo_extended_${V}_linux-amd64.tar.gz
#
# O `sha256sum -c` não está aqui por desconfiança do GitHub: ele é o que
# transforma "o download veio truncado" ou "o artefato foi trocado" num erro
# nomeado, em vez de um `tar` estourando com mensagem sobre formato.
ARG HUGO_VERSION=0.166.0
ARG HUGO_SHA256=0e39b901e3f919f1daae05c8ff64f0c14c8a348ef46886d63f8e6d1bb2653885

RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      wget ca-certificates \
 && wget -qO /tmp/hugo.tar.gz \
      "https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz" \
 && echo "${HUGO_SHA256}  /tmp/hugo.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/hugo.tar.gz -C /usr/local/bin hugo \
 && hugo version \
 && rm -rf /var/lib/apt/lists/* /tmp/hugo.tar.gz

WORKDIR /src
COPY site/ ./
RUN hugo --minify


FROM python:3.13-slim-bookworm

# Bibliotecas de que o engine depende. Sem elas o libEngine12.so carrega e
# falha no dlopen, com erro que não diz qual símbolo faltou.
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      libtommath1 libicu72 libncurses6 libatomic1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=engine /usr/lib/x86_64-linux-gnu/libfbclient.so.2* /usr/lib/x86_64-linux-gnu/

# A raiz do Firebird INTEIRA, não arquivo a arquivo.
#
# Custa ~9 MB e evita uma classe de erro difícil de diagnosticar: o engine
# resolve `intl/`, `firebird.msg` e os `.conf` a partir de `$(root)`, e cada
# peça faltando produz mensagem que aponta para outro lugar. Copiar só o
# `libEngine12.so` dá "CHARACTER SET WIN1252 is not defined", que não menciona
# arquivo nenhum — falta o `intl/libfbintl.so`, que implementa WIN1252 e a
# collation WIN_PTBR, das quais esta base inteira depende.
COPY --from=engine /usr/lib/x86_64-linux-gnu/firebird/3.0/ /usr/lib/x86_64-linux-gnu/firebird/3.0/
COPY --from=engine /etc/firebird/3.0/                      /etc/firebird/3.0/
COPY --from=engine /usr/share/firebird/                    /usr/share/firebird/

RUN ldconfig

COPY docker/firebird.conf /etc/firebird/3.0/firebird.conf

# O engine grava arquivos de lock mesmo com a base read-only. Num pod com raiz
# somente leitura isto precisa ser um tmpfs (ver k8s/hestia.yaml no infra-olympus).
RUN mkdir -p /tmp/firebird && chmod 1777 /tmp/firebird

WORKDIR /app

# MENOS pacotes do que o `pyproject.toml` declara, e isso é deliberado.
#
# O pod serve consulta e **nunca roda o ETL**. `polars` e `rich` são do ETL e
# da CLI, e não entram. Medido dentro da imagem anterior:
#
#     _polars_runtime_32   172 MB
#     polars                 9 MB
#     pygments               9 MB   (dependência do rich)
#     rich                   3 MB
#     ---------------------------
#                          193 MB de 252 MB de site-packages
#
# Isso não é palpite sobre o que "deve" ser preciso: o grafo de import da API
# inteira foi medido (141 módulos de topo) e nenhum deles alcança os três.
# `tests/test_imagem_enxuta.py` trava a propriedade, rodando com `polars`
# INSTALADO e reprovando se a superfície do pod encostar nele — porque em
# desenvolvimento o import passaria e o erro só apareceria ao subir o pod.
#
# `uvloop` (16 MB) FICA, ainda que também seja grande: ele troca o event loop
# do asyncio e é o pod usando, não o ETL.
#
# O `pip` sai depois de instalar. São 12 MB, e um container de produção não
# tem por que carregar um instalador de pacotes.
RUN pip install --no-cache-dir \
      "firebird-driver>=2.0.0" "python-dotenv>=1.0.0" \
      "fastapi>=0.115.0" "uvicorn[standard]>=0.30.0" \
 && pip uninstall -y pip setuptools wheel
# Não se apaga `__pycache__` aqui, e a tentação é grande: são dezenas de MB.
# Mas o pod tem `strategy: Recreate`, então cada deploy paga o start inteiro
# com a readiness esperando — e sem o .pyc cada import recompila. Trocar
# espaço em disco por latência de subida num serviço que reinicia a cada push
# é o lado errado da troca. Se um dia isso for medido e valer, fica aqui a
# nota de que foi considerado.

COPY src/ ./src/
COPY --from=site /src/public/ ./site/

# FIREBIRD é a RAIZ do engine, não o diretório de configuração. Apontá-lo para
# /etc/firebird/3.0 faz o engine procurar `intl/` e `firebird.msg` lá dentro,
# não achar, e falhar com "CHARACTER SET WIN1252 is not defined" — mensagem que
# não menciona caminho nenhum.
ENV FB_CLIENT_LIBRARY=/usr/lib/x86_64-linux-gnu/libfbclient.so.2 \
    FIREBIRD=/usr/lib/x86_64-linux-gnu/firebird/3.0 \
    PYTHONUNBUFFERED=1

# /data   base de CEP, montada read-only, trocada a cada competência
# /creds  credenciais e estatísticas, ciclo de vida próprio
# /logs   log analítico, mantido indefinidamente
VOLUME ["/data", "/creds", "/logs"]

# O nome do arquivo é TODO EM CAIXA BAIXA, e em Linux isso não é estilo: o
# caminho é comparado byte a byte. `cep_xray.fdb` aqui tem que bater exatamente
# com o que `deploy/trocar-base.sh` escreve no volume e com `OLYMPUS_NOME_BASE`
# no `.env` da estação. A fonte chega como `TBL_CEP_202609_FDB.FDB`, em caixa
# alta — é fácil copiar essa convenção sem querer, e o sintoma seria o pod
# subindo e o `/saude` devolvendo 503 "base inacessível" com o arquivo ali, ao
# lado, com o nome errado. `trocar-base.sh` recusa nome que não seja minúsculo.
ENV API_CREDENCIAIS_DIR=/creds \
    API_LOGS_DIR=/logs \
    API_SITE_DIR=/app/site \
    DB_NAME=/data/cep_xray.fdb \
    DB_CHARSET=WIN1252 \
    DB_COLLATION=WIN_PTBR

# FB_EMBEDDED muda a forma do DSN. Sem ele o caminho sairia como
# `inet://localhost:3050/...`, o Dispatcher tentaria o provider Remote e
# falharia com "unavailable database" — erro que não menciona o caminho e manda
# procurar no lugar errado. Embedded exige caminho sem host.
ENV FB_EMBEDDED=1

EXPOSE 8000
# A porta do /manager (8001) NÃO é exposta de propósito: é a barreira que não
# depende de nenhum segredo estar certo. Quem precisar dela publica
# explicitamente, sabendo o que está fazendo.

CMD ["python", "-m", "src.api.servir", "--host", "0.0.0.0", "--porta", "8000"]
