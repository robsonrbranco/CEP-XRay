#!/usr/bin/env bash
#
# Troca a base do Hestia no host do Olympus.
#
# Roda NO HOST, copiado para lá por `src/publicacao/enviar.py`.
#
# Este script diverge do equivalente do Themis de propósito, e a divergência é
# a parte importante.
#
# Todo o desenho do Themis — apagar a base antiga ANTES de transmitir, fatiar
# em partes de 512 MB, `rm` dentro do pipe do gunzip, e nenhum rollback —
# existe por uma restrição: 34,2 GB de base contra ~41 GB livres no node. Com
# essa conta, as duas bases não cabem juntas no disco, e o downtime vai de 1 a
# 4 horas porque a transmissão inteira acontece com o serviço fora do ar.
#
# A base do CEP tem ~240 MB. Nenhuma dessas restrições existe aqui, e copiar o
# script do Themis carregaria a complexidade sem a razão dela.
#
# Então a ordem inverte:
#
#   1. a base nova é enviada e CONFERIDA com o pod NO AR
#   2. o pod para
#   3. dois `mv`  (a antiga vira .anterior, a nova vira a oficial)
#   4. o pod sobe
#   5. /saude confirma a competência
#   6. só então a anterior é apagada
#
# Três ganhos, todos consequência da mesma inversão:
#
#   * downtime cai de horas para SEGUNDOS — são dois rename e um restart;
#   * passa a EXISTIR ROLLBACK: se o /saude vier errado, um `mv` desfaz;
#   * a conferência acontece antes de encostar no pod, não depois de apagar a
#     única cópia que havia.
#
# Por isso também não existe `parar-e-limpar.sh` neste projeto: não há nada a
# limpar antes.

set -euo pipefail

ENVIO="${ENVIO:?ENVIO nao definido}"
PASTA_FDB="${PASTA_FDB:?PASTA_FDB nao definido}"
NOME_BASE="${NOME_BASE:-cep_xray.fdb}"
DEPLOYMENT="${DEPLOYMENT:-hestia}"
NAMESPACE="${NAMESPACE:-olympus}"

ALVO="$PASTA_FDB/$NOME_BASE"
NOVA="$PASTA_FDB/${NOME_BASE%.fdb}.novo.fdb"
ANTERIOR="$PASTA_FDB/${NOME_BASE%.fdb}.anterior.fdb"
MANIFESTO="$ENVIO/manifesto-envio.json"

# Espaço mínimo livre no node para a troca sequer começar.
#
# Não é sobre esta base, que é pequena: é sobre os outros seis serviços. Disco
# cheio num k3s nao derruba so o Hestia, gera DiskPressure e despeja pods dos
# vizinhos. O containerd deste node ja cresceu 10 GB numa unica sessao de
# deploys (item 9 do LICOES-APRENDIDAS.md do infra-olympus), entao a folga tem
# que ser bem maior que o tamanho da base.
MINIMO_LIVRE_GB=5

erro() { echo "ERRO: $*" >&2; exit 1; }
info() { echo "==> $*"; }

# ---------------------------------------------------------------------------
# 0. O nome do arquivo tem que ser minusculo
#
# Em Linux o caminho e comparado byte a byte, e o Dockerfile fixa
# DB_NAME=/data/cep_xray.fdb. A fonte chega como TBL_CEP_202609_FDB.FDB, em
# caixa alta, e e facil copiar essa convencao sem querer.
#
# O sintoma seria cruel: o pod sobe, o arquivo esta la, e o /saude devolve 503
# "base inacessivel" apontando para um caminho que existe -- com outro nome.
# Barato checar aqui, caro descobrir la.
# ---------------------------------------------------------------------------
if [ "$NOME_BASE" != "$(echo "$NOME_BASE" | tr '[:upper:]' '[:lower:]')" ]; then
  erro "NOME_BASE tem maiuscula: '$NOME_BASE'. O pod procura o caminho exato" \
       "definido no Dockerfile (minusculo), e Linux diferencia caixa."
fi

# ---------------------------------------------------------------------------
# 1. Conferir ANTES de encostar no pod
# ---------------------------------------------------------------------------
[ -f "$MANIFESTO" ] || erro "manifesto nao encontrado: $MANIFESTO"
[ -f "$NOVA" ] || erro "base nova nao encontrada: $NOVA"

info "Conferindo a base nova contra o manifesto"
esperado_sha=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['fdb_sha256'])" "$MANIFESTO")
esperado_bytes=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['fdb_bytes'])" "$MANIFESTO")

real_bytes=$(stat -c%s "$NOVA")
[ "$real_bytes" = "$esperado_bytes" ] \
  || erro "tamanho diverge: $real_bytes != $esperado_bytes"

real_sha=$(sha256sum "$NOVA" | cut -d' ' -f1)
[ "$real_sha" = "$esperado_sha" ] \
  || erro "sha256 diverge -- a transmissao corrompeu a base. Reenvie."

info "  $real_bytes bytes, sha256 confere"

# ---------------------------------------------------------------------------
# 2. Espaço
# ---------------------------------------------------------------------------
livre=$(df -B1 --output=avail "$PASTA_FDB" | tail -1)
livre_gb=$((livre / 1024 / 1024 / 1024))
if [ "$livre_gb" -lt "$MINIMO_LIVRE_GB" ]; then
  erro "so ${livre_gb} GB livres em $PASTA_FDB (minimo $MINIMO_LIVRE_GB)." \
       "Trocar agora arrisca DiskPressure e despejo dos outros servicos."
fi
info "Espaco livre: ${livre_gb} GB"

# ---------------------------------------------------------------------------
# 3. A janela de downtime começa aqui, e é curta
# ---------------------------------------------------------------------------
religar() {
  kubectl -n "$NAMESPACE" scale deployment "$DEPLOYMENT" --replicas=1 || true
}
trap religar EXIT

info "Parando $DEPLOYMENT"
kubectl -n "$NAMESPACE" scale deployment "$DEPLOYMENT" --replicas=0
kubectl -n "$NAMESPACE" wait --for=delete pod -l "app=$DEPLOYMENT" --timeout=180s || true

# Sobrou uma `.anterior` de uma troca passada que ninguem apagou.
[ -f "$ANTERIOR" ] && rm -f "$ANTERIOR"

if [ -f "$ALVO" ]; then
  mv "$ALVO" "$ANTERIOR"
  TINHA_ANTERIOR=1
  info "Base atual guardada como $(basename "$ANTERIOR") -- e o rollback"
else
  TINHA_ANTERIOR=0
  info "Primeira publicacao: nao havia base anterior"
fi

mv "$NOVA" "$ALVO"
info "Base nova no lugar"

trap - EXIT
kubectl -n "$NAMESPACE" scale deployment "$DEPLOYMENT" --replicas=1
kubectl -n "$NAMESPACE" rollout status deployment/"$DEPLOYMENT" --timeout=300s

rm -f "$MANIFESTO"
rmdir "$ENVIO" 2>/dev/null || true

echo
info "Troca concluida."
echo

# O epilogo depende de TER havido base anterior. Na primeira publicacao nao ha
# rollback a oferecer, e imprimir o procedimento apontando para um arquivo que
# nao existe mandaria quem estivesse em apuros rodar um `mv` que falha.
if [ "$TINHA_ANTERIOR" -eq 1 ]; then
  echo "    A base anterior continua em $(basename "$ANTERIOR")."
  echo
  echo "    Confira o /saude e a competencia servida. Se estiver errado:"
  echo "      kubectl -n $NAMESPACE scale deployment $DEPLOYMENT --replicas=0"
  echo "      mv '$ANTERIOR' '$ALVO'"
  echo "      kubectl -n $NAMESPACE scale deployment $DEPLOYMENT --replicas=1"
  echo
  echo "    Estando certo, apague a anterior -- esquecer acumula uma base por mes:"
  echo "      rm -f '$ANTERIOR'"
else
  echo "    Primeira publicacao: NAO HA ROLLBACK para esta troca."
  echo "    Confira o /saude e a competencia servida; a partir da proxima"
  echo "    competencia a base atual passa a ser o rollback."
fi
