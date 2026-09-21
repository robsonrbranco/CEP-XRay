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

RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      wget ca-certificates \
 && HUGO_VERSION=$(wget -qO- https://api.github.com/repos/gohugoio/hugo/releases/latest \
      | grep -oP '"tag_name": "v\K[^"]+') \
 && wget -qO /tmp/hugo.tar.gz \
      "https://github.com/gohugoio/hugo/releases/download/v${HUGO_VERSION}/hugo_extended_${HUGO_VERSION}_linux-amd64.tar.gz" \
 && tar -xzf /tmp/hugo.tar.gz -C /usr/local/bin hugo \
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
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "firebird-driver>=2.0.0" "python-dotenv>=1.0.0" "rich>=13.0.0" \
      "polars>=1.42.1" "fastapi>=0.115.0" "uvicorn[standard]>=0.30.0"

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
