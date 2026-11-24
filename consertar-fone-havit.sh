#!/usr/bin/env bash
# consertar-fone-havit — instala e corrige o fone Havit H2002d Pro no Ubuntu 22.04 com PulseAudio,
# pelo receptor 2.4G USB (040b:0897) ou pareado direto no Bluetooth.
#
# É o instalador e o conserto ao mesmo tempo. Num computador novo basta copiar a pasta e rodar
# este script (veja LEIA-ME.txt). A cada execução ele:
#   1. instala os pacotes do Ubuntu que FALTAREM (pede a senha só nesse caso; nunca atualiza
#      um pacote que já existe)
#   2. instala o painel "Fone Havit" e os atalhos (menu e Área de Trabalho) para este usuário
#   3. corrige o som: ajustes do mixer em ~/.local/share/pulseaudio/alsa-mixer/paths/, perfil,
#      saída/entrada padrão e volumes do receptor. Sem o receptor, usa o fone pelo Bluetooth (tenta
#      conectar se ele já estiver pareado). No Bluetooth o microfone só existe no modo Chamada.
#
# Uso:
#   consertar-fone-havit.sh            instala o que faltar, corrige e oferece testar som e microfone
#   consertar-fone-havit.sh --instalar só instala (pacotes, painel e atalhos), sem mexer no som
#   consertar-fone-havit.sh --status   só mostra o estado atual
#   consertar-fone-havit.sh --testar   testa esquerdo/direito e o microfone
#   consertar-fone-havit.sh --mic      testa só o microfone
#   consertar-fone-havit.sh --desfazer remove os ajustes do mixer feitos por este script
#   consertar-fone-havit.sh --ajuda
#
# O problema (descoberto em 21 e 22/11/2026):
#   O receptor informa uma faixa de volume falsa no controle "PCM" (0 a 0,39 dB). O PulseAudio
#   acaba deixando o volume de hardware em 0, e com 0 o firmware do receptor CALA O LADO DIREITO.
#   No perfil "Estéreo digital (IEC958)" — que aparece como opção nas Configurações — o
#   PulseAudio nem mexe nesse controle, então ele fica em 0 do jeito que o receptor liga.

set -u
export LC_ALL=C   # saída do pactl/amixer em inglês, para o script conseguir ler

USB_ID="040b:0897"
CARD_MATCH="H2002d_Pro"
PERFIL="output:analog-stereo+input:mono-fallback"
OVR_DIR="$HOME/.local/share/pulseaudio/alsa-mixer/paths"
MARCA="# gerenciado por consertar-fone-havit"
SONS="/usr/share/sounds/alsa"

if [ -t 1 ]; then
  VERDE=$'\e[32m'; AMARELO=$'\e[33m'; VERMELHO=$'\e[31m'; NEGRITO=$'\e[1m'; FIM=$'\e[0m'
else
  VERDE=""; AMARELO=""; VERMELHO=""; NEGRITO=""; FIM=""
fi
ok()    { echo "  ${VERDE}OK${FIM}  $*"; }
aviso() { echo "  ${AMARELO}!!${FIM}  $*"; }
erro()  { echo "  ${VERMELHO}ERRO${FIM} $*"; }
titulo(){ echo; echo "${NEGRITO}$*${FIM}"; }
avisar_desktop() { command -v notify-send >/dev/null && notify-send -i audio-headset "Fone Havit" "$*" 2>/dev/null || true; }

PASTA="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

if [ "$(id -u)" -eq 0 ]; then
  echo "Não rode com sudo: o som é da sua sessão de usuário."
  echo "Rode sem sudo — a senha é pedida sozinha se for preciso instalar algum pacote."
  exit 1
fi

# ---------- instalação (pacotes, painel e atalhos) ----------

# tudo que o conserto e o painel usam (Ubuntu 22.04)
PACOTES="pulseaudio pulseaudio-utils alsa-utils usbutils libnotify-bin
  python3 python3-gi python3-gi-cairo python3-cairo python3-numpy python3-dbus
  gir1.2-gtk-3.0 gir1.2-gstreamer-1.0 gir1.2-rsvg-2.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good
  gcc libc6-dev fonts-lato bluez pulseaudio-module-bluetooth"

pacotes_faltando() {
  local p falta=""
  for p in $PACOTES; do
    dpkg-query -W -f='${Status}\n' "$p" 2>/dev/null | grep -q "install ok installed" || falta="$falta $p"
  done
  echo $falta
}

instalar_dependencias() {
  titulo "Pacotes do Ubuntu"
  if [ -r /etc/os-release ]; then
    local ID="" VERSION_ID="" PRETTY_NAME=""
    . /etc/os-release
    [ "$ID" = ubuntu ] && [ "$VERSION_ID" = "22.04" ] \
      || aviso "feito para Ubuntu 22.04; este é ${PRETTY_NAME:-outro sistema} — pode não funcionar igual"
  fi
  local falta; falta=$(pacotes_faltando)
  if [ -z "$falta" ]; then ok "todos os pacotes necessários já estão instalados"; return 0; fi
  aviso "faltam: $falta"
  if [ -n "${HAVIT_PAINEL:-}" ]; then
    aviso "use o atalho \"Consertar fone Havit\" para instalar o que falta (ele pede a senha)"
    return 1
  fi
  local pre
  if [ -t 0 ] && command -v sudo >/dev/null; then pre=(sudo)
  elif command -v pkexec >/dev/null; then pre=(pkexec)
  else erro "sem sudo/pkexec para instalar; rode no terminal: sudo apt install $falta"; return 1; fi
  echo "       Vou instalar só o que falta (nada que já existe é atualizado). Pode pedir a sua senha."
  # --no-upgrade: pacote que já existe fica na versão que está
  local apt=(env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-upgrade -o DPkg::Lock::Timeout=180)
  # shellcheck disable=SC2086
  if ! "${pre[@]}" "${apt[@]}" $falta; then
    aviso "não deu de primeira; atualizando a LISTA de pacotes (não atualiza programas) e tentando de novo"
    # shellcheck disable=SC2086
    "${pre[@]}" apt-get update -o DPkg::Lock::Timeout=180 && "${pre[@]}" "${apt[@]}" $falta
  fi
  falta=$(pacotes_faltando)
  if [ -z "$falta" ]; then ok "pacotes instalados"; return 0; fi
  erro "ainda faltam: $falta  (sem internet? rode de novo quando tiver conexão)"
  return 1
}

escrever_se_mudou() {  # $1 = arquivo, $2 = conteúdo; reescreve no lugar (mantém permissões)
  [ -f "$1" ] && [ "$(cat "$1")" = "$2" ] && return 1
  printf '%s\n' "$2" > "$1"
}

lancador_painel() {
  cat <<FIM_ATALHO
[Desktop Entry]
Type=Application
Version=1.0
Name=Fone Havit
GenericName=Painel do fone
Comment=Volume do fone e do microfone, equalizador (graves e agudos) e correções do Havit H2002D Pro
Exec="$HOME/.local/bin/havit-fone"
Icon=$PASTA/imagens/havit-fone.png
Terminal=false
Categories=AudioVideo;Audio;
Keywords=fone;headset;havit;audio;som;microfone;equalizador;eq;graves;agudos;
StartupNotify=true
StartupWMClass=havit-fone
FIM_ATALHO
}

lancador_consertar() {
  cat <<FIM_ATALHO
[Desktop Entry]
Type=Application
Version=1.0
Name=Consertar fone Havit
GenericName=Corrigir áudio do fone
Comment=Instala o que faltar e corrige som (lado direito mudo), saída padrão e microfone do fone Havit H2002d Pro
Exec="$HOME/.local/bin/consertar-fone-havit" --janela
Icon=$PASTA/imagens/consertar-fone-havit.png
Terminal=true
Categories=AudioVideo;Audio;
Keywords=fone;headset;havit;audio;som;microfone;
StartupNotify=false
FIM_ATALHO
}

instalar_app() {
  titulo "Painel Fone Havit e atalhos"
  local apps="$HOME/.local/share/applications" bin="$HOME/.local/bin" primeira=0
  [ -f "$apps/havit-fone.desktop" ] || primeira=1
  chmod +x "$PASTA/consertar-fone-havit.sh" "$PASTA/havit-fone.py" 2>/dev/null
  mkdir -p "$bin" "$apps"
  ln -sfn "$PASTA/havit-fone.py" "$bin/havit-fone"
  ln -sfn "$PASTA/consertar-fone-havit.sh" "$bin/consertar-fone-havit"
  ok "comandos havit-fone e consertar-fone-havit em $bin"

  local mesa n conteudo
  mesa=$(xdg-user-dir DESKTOP 2>/dev/null)
  for n in havit-fone consertar-fone-havit; do
    if [ "$n" = havit-fone ]; then conteudo=$(lancador_painel); else conteudo=$(lancador_consertar); fi
    escrever_se_mudou "$apps/$n.desktop" "$conteudo" && ok "atalho no menu gravado: $n.desktop"
    # Área de Trabalho: cria na primeira instalação; depois só atualiza se o atalho ainda existir
    if [ -n "$mesa" ] && [ "$mesa" != "$HOME" ] && [ -d "$mesa" ] \
        && { [ -f "$mesa/$n.desktop" ] || [ $primeira = 1 ]; }; then
      escrever_se_mudou "$mesa/$n.desktop" "$conteudo" && ok "atalho na Área de Trabalho gravado: $n.desktop"
      chmod +x "$mesa/$n.desktop"
      gio set -t string "$mesa/$n.desktop" metadata::trusted true 2>/dev/null
    fi
  done
  command -v update-desktop-database >/dev/null && update-desktop-database "$apps" 2>/dev/null
  ok "atalhos do menu e da Área de Trabalho apontam para $PASTA"

  if command -v python3 >/dev/null && python3 "$PASTA/havit-fone.py" --filtros >/dev/null 2>&1; then
    ok "filtros do equalizador e do 'só minha voz' prontos (~/.local/lib/ladspa)"
  else
    aviso "não consegui compilar os filtros do equalizador/'só minha voz' (falta o gcc?)"
  fi
  return 0
}

instalar() {
  instalar_dependencias
  instalar_app
  if pactl info 2>/dev/null | grep -qi "pipewire"; then
    aviso "o servidor de som é o PipeWire; este conserto foi feito para o PulseAudio do Ubuntu 22.04"
  fi
}

# sem terminal (ex.: "Executar como programa" no Arquivos): reabre num terminal para mostrar o
# andamento e poder pedir a senha
abrir_no_terminal() {
  { [ -t 0 ] || [ -t 1 ]; } && return 1
  [ -n "${HAVIT_PAINEL:-}" ] && return 1
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 1
  local eu; eu="$(readlink -f "$0")"
  if command -v gnome-terminal >/dev/null; then
    gnome-terminal --title="Consertar fone Havit" -- bash "$eu" --janela && return 0
  fi
  if command -v x-terminal-emulator >/dev/null; then
    x-terminal-emulator -e bash "$eu" --janela && return 0
  fi
  return 1
}

# ---------- localizar o fone ----------

CARD=""; IDX=""; BASE=""; SINK=""; SOURCE=""; PREF_SAIDA=""; PREF_ENTRADA=""

receptor_conectado() { lsusb -d "$USB_ID" >/dev/null 2>&1; }

# equalizador do app visual (havit-fone.py): saída virtual "havit_eq" em cima do fone
EQ_SINK="havit_eq"
EQ_CFG="$HOME/.config/havit-fone/eq.conf"
eq_ligado() { pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx "$EQ_SINK"; }
# "só minha voz" do app visual: entrada virtual "havit_voz", alimentada pelo microfone do fone
VOZ_FONTE="havit_voz"
voz_ligado() {
  pactl list short sources 2>/dev/null | awk '{print $2}' | grep -qx "$VOZ_FONTE" &&
    pactl list short modules 2>/dev/null | grep -q "module-loopback.*source=${SOURCE:-x} .*sink=havit_voz_filtro"
}

achar_placa() {
  CARD=$(pactl list short cards 2>/dev/null | awk -v m="$CARD_MATCH" '$2 ~ m {print $2; exit}')
  [ -n "$CARD" ] || return 1
  BASE=${CARD#alsa_card.}
  PREF_SAIDA="alsa_output.$BASE."; PREF_ENTRADA="alsa_input.$BASE."
  IDX=""
  local f
  for f in /proc/asound/card*/usbid; do
    [ -r "$f" ] || continue
    if [ "$(cat "$f")" = "$USB_ID" ]; then IDX=$(basename "$(dirname "$f")"); IDX=${IDX#card}; break; fi
  done
  return 0
}

perfil_ativo() { pactl list cards 2>/dev/null | awk -v c="$CARD" '$0 ~ "Name: "c"$" {f=1} f && /Active Profile:/ {sub(/.*Active Profile: /,""); print; exit}'; }

achar_sink_source() {
  SINK=$(pactl list short sinks 2>/dev/null | awk -v p="$PREF_SAIDA" 'index($2, p)==1 {print $2; exit}')
  SOURCE=$(pactl list short sources 2>/dev/null | awk -v p="$PREF_ENTRADA" 'index($2, p)==1 && $2 !~ /\.monitor$/ {print $2; exit}')
}

# ---------- Bluetooth (fone pareado direto, sem o receptor USB) ----------

BT_NOMES_RE="h2002|havit"   # trechos do nome Bluetooth do fone (sem diferenciar maiúsculas)
PERFIL_BT_MUSICA="a2dp_sink"

achar_bt() {  # placa Bluetooth do fone, se ele estiver conectado
  CARD=$(pactl list cards 2>/dev/null | awk -v re="$BT_NOMES_RE" '
    /^Card #/ { if (nome ~ /^bluez_card\./ && casou) { print nome; achou = 1; exit } nome = ""; casou = 0; next }
    /^\tName: / { nome = $2 }
    /bluez\.alias = |device\.description = / { if (tolower($0) ~ re) casou = 1 }
    END { if (!achou && nome ~ /^bluez_card\./ && casou) print nome }')
  [ -n "$CARD" ] || return 1
  BASE=${CARD#bluez_card.}; IDX=""
  PREF_SAIDA="bluez_sink.$BASE."; PREF_ENTRADA="bluez_source.$BASE."
  return 0
}

bt_nome() {
  pactl list cards 2>/dev/null | awk -v c="$CARD" '$0 ~ "Name: "c"$" {f = 1} f && /bluez\.alias = / {sub(/.*= "/, ""); sub(/"$/, ""); print; exit}'
}

bt_desligado() { rfkill list bluetooth 2>/dev/null | grep -q "Soft blocked: yes"; }

bt_tentar_conectar() {  # fone já pareado, mas desconectado: tenta conectar
  command -v bluetoothctl >/dev/null || return 1
  local linha mac
  linha=$(bluetoothctl paired-devices 2>/dev/null | awk -v re="$BT_NOMES_RE" 'tolower($0) ~ re {print; exit}')
  mac=$(awk '{print $2}' <<<"$linha")
  [ -n "$mac" ] || return 1
  echo "       tentando conectar o fone pelo Bluetooth: ${linha#Device }"
  timeout 20 bluetoothctl connect "$mac" >/dev/null 2>&1
  esperar 10 achar_bt
}

# ---------- arquivos de ajuste do mixer (persistem entre reinícios e reconexões) ----------

OVR_ARQUIVOS="analog-output.conf iec958-stereo-output.conf analog-input-mic.conf"

conteudo_override() {  # $1 = nome do arquivo do sistema
  if [ "$1" = analog-input-mic.conf ]; then
    cat <<EOF
$MARCA (fone Havit H2002d Pro, receptor USB $USB_ID)
# Criado em 2026-11-22.
#
# O receptor também informa uma faixa de dB falsa (0 a 0,12 dB) no controle "Mic". Com o ganho do
# microfone abaixo de 100%, o PulseAudio grava 0 no hardware e, com 0, o receptor CORTA o microfone.
# Aqui o arquivo original do sistema é usado sem mudanças e só o Mic (e o Mic,1) fica fixo no
# máximo de hardware (31). O ganho passa a ser controlado pelo PulseAudio em software.
# Se 31 não couber na faixa de outra placa, o PulseAudio ignora o ajuste.
#
# Para desfazer: consertar-fone-havit.sh --desfazer   (ou apague este arquivo e reconecte o receptor)

.include /usr/share/pulseaudio/alsa-mixer/paths/$1

[Element Mic]
switch = mute
volume = 31

[Element Mic,1]
switch = on
volume = 31
EOF
    return
  fi
  cat <<EOF
$MARCA (fone Havit H2002d Pro, receptor USB $USB_ID)
# Criado em 2026-11-21, atualizado em 2026-11-22.
#
# O receptor informa uma faixa de dB falsa (0 a 0,39 dB) no controle "PCM". Sem este ajuste o
# PulseAudio grava 0 no hardware e, com o PCM em 0, o firmware do receptor cala o lado DIREITO.
# Aqui o arquivo original do sistema é usado sem mudanças e só o PCM (e o PCM,1, que é o volume
# geral do receptor) fica fixo no máximo de hardware (100). O volume passa a ser controlado pelo
# PulseAudio em software. Se 100 não couber na faixa de outra placa, o PulseAudio ignora o ajuste.
#
# Para desfazer: consertar-fone-havit.sh --desfazer   (ou apague este arquivo e reconecte o receptor)

.include /usr/share/pulseaudio/alsa-mixer/paths/$1

[Element PCM]
switch = mute
volume = 100

[Element PCM,1]
switch = on
volume = 100
EOF
}

instalar_overrides() {
  mkdir -p "$OVR_DIR"
  local nome destino novo mudou=0
  for nome in $OVR_ARQUIVOS; do
    destino="$OVR_DIR/$nome"
    novo=$(conteudo_override "$nome")
    if [ -f "$destino" ] && [ "$(cat "$destino")" = "$novo" ]; then
      ok "ajuste do mixer já instalado: $nome"
      continue
    fi
    # arquivo que não é deste script (nem a versão de 21/11) → guarda cópia antes de trocar
    if [ -f "$destino" ] && ! grep -qE "consertar-fone-havit|Havit H2002d" "$destino"; then
      cp -p "$destino" "$destino.bak-$(date +%Y%m%d-%H%M%S)"
      aviso "havia um $nome seu; cópia salva como $nome.bak-*"
    fi
    printf '%s\n' "$novo" > "$destino"
    ok "ajuste do mixer gravado: $nome"
    mudou=1
  done
  return $mudou
}

# ---------- correção ----------

esperar() {  # $1 = segundos, $2... = comando que precisa dar certo
  local n=$(( $1 * 4 )); shift
  while [ $n -gt 0 ]; do "$@" && return 0; sleep 0.25; n=$((n-1)); done
  return 1
}

ajustar_mixer_hw() {
  [ -n "$IDX" ] || { aviso "não achei a placa ALSA do receptor; pulei o ajuste do mixer"; return; }
  amixer -q -c "$IDX" sset 'PCM',0 100% unmute 2>/dev/null
  amixer -q -c "$IDX" sset 'PCM',1 100% unmute 2>/dev/null
  amixer -q -c "$IDX" sset 'Mic',0 100% cap 2>/dev/null
  amixer -q -c "$IDX" sset 'Mic',1 100% cap 2>/dev/null
  local pcm
  pcm=$(amixer -c "$IDX" cget name='PCM Playback Volume' 2>/dev/null | awk -F= '/: values=/ {print $2}')
  if [ "$pcm" = "100,100" ]; then ok "volume de hardware do receptor no máximo (esquerdo e direito = 100)"
  else aviso "volume de hardware do receptor ficou em [$pcm] (esperado 100,100)"; fi
}

volumes_sink() { pactl get-sink-volume "$SINK" 2>/dev/null | grep -o '[0-9]*%' | tr -d % | head -2 | xargs; }

equilibrar() {
  local v l r
  v=$(volumes_sink); l=${v% *}; r=${v#* }
  [ -n "$l" ] && [ -n "$r" ] || return
  if [ "$l" != "$r" ]; then
    local m=$(( l > r ? l : r ))
    pactl set-sink-volume "$SINK" "${m}%" "${m}%"
    ok "balanço corrigido (era esquerdo ${l}% / direito ${r}%, agora ${m}% nos dois)"
  else
    ok "balanço equilibrado (volume ${l}% nos dois lados)"
  fi
  [ "$l" -lt 15 ] 2>/dev/null && aviso "o volume do fone está baixo (${l}%) — aumente no ícone de som se precisar"
}

mover_fluxos() {
  local monitores id src cliente n=0
  # sons dos programas → fone (ou → equalizador, se ligado). Fluxos internos de módulos
  # (cliente "-", ex.: o próprio equalizador) ficam onde estão.
  while read -r id _ cliente _; do
    [ -n "$id" ] && [ "$cliente" != "-" ] || continue
    pactl move-sink-input "$id" "${ALVO:-$SINK}" 2>/dev/null && n=$((n+1))
  done < <(pactl list short sink-inputs 2>/dev/null)
  [ $n -gt 0 ] && ok "$n som(ns) de programa(s) aberto(s) mandado(s) para o fone"
  # gravações que usam microfone (não as que gravam o som da tela/monitor) → microfone do fone
  # (ou → "só minha voz", se ligado). Ficam onde estão: fluxos internos de módulos (cliente "-",
  # ex.: a ligação do "só minha voz") e o medidor do painel Fone Havit.
  [ -n "${ALVO_SRC:-$SOURCE}" ] || return 0
  local ignorar
  monitores=" $(pactl list short sources 2>/dev/null | awk '$2 ~ /\.monitor$/ {printf "%s ", $1}')"
  ignorar=" $(pactl list source-outputs 2>/dev/null | awk '/^Source Output #/ {id=substr($3,2)} /application.name = "Fone Havit/ {printf "%s ", id}')"
  n=0
  while read -r id src cliente _; do
    [ -n "$id" ] && [ "$cliente" != "-" ] || continue
    case "$monitores" in *" $src "*) continue ;; esac
    case "$ignorar" in *" $id "*) continue ;; esac
    pactl move-source-output "$id" "${ALVO_SRC:-$SOURCE}" 2>/dev/null && n=$((n+1))
  done < <(pactl list short source-outputs 2>/dev/null)
  [ $n -gt 0 ] && ok "$n programa(s) usando microfone passado(s) para o microfone do fone"
  return 0
}

consertar() {
  titulo "Corrigindo o fone Havit H2002d Pro"

  if ! receptor_conectado; then
    if achar_bt || { ! bt_desligado && bt_tentar_conectar; }; then
      consertar_bt
      return $?
    fi
    erro "o fone não está conectado: nem o receptor USB ($USB_ID) nem o Bluetooth."
    echo "       Coloque o receptor numa porta USB e ligue o fone no modo 2.4G,"
    if bt_desligado; then
      echo "       ou ligue o Bluetooth do computador e conecte o fone por ele."
    else
      echo "       ou conecte o fone pelo Bluetooth (Configurações → Bluetooth)."
    fi
    echo "       Depois rode de novo."
    avisar_desktop "Fone não encontrado (nem receptor USB nem Bluetooth). Conecte e rode de novo."
    return 1
  fi
  ok "receptor USB conectado"

  instalar_overrides
  local overrides_novos=$?

  if ! esperar 10 achar_placa; then
    erro "o receptor está no USB, mas o PulseAudio não criou a placa de som."
    echo "       Tente tirar e pôr o receptor de novo. Se continuar: systemctl --user restart pulseaudio"
    return 1
  fi
  ok "placa de som: ${CARD} (ALSA card ${IDX:-?})"

  local antes; antes=$(perfil_ativo)
  if [ "$antes" != "$PERFIL" ]; then
    pactl set-card-profile "$CARD" "$PERFIL" \
      && ok "perfil trocado: '${antes}' → '${PERFIL}' (estéreo analógico + microfone)" \
      || erro "não consegui trocar o perfil para $PERFIL"
  else
    ok "perfil correto: estéreo analógico + microfone"
  fi

  esperar 5 eval 'achar_sink_source; [ -n "$SINK" ] && [ -n "$SOURCE" ]' \
    || { erro "a saída/entrada do fone não apareceu no PulseAudio"; return 1; }

  ajustar_mixer_hw
  finalizar

  if [ "$overrides_novos" = 1 ]; then
    aviso "ajustes novos do mixer valem por completo a partir da próxima reconexão do receptor"
    echo "       (o volume já foi corrigido agora; não precisa fazer nada)"
  fi
  pronto
}

consertar_bt() {
  ok "fone conectado pelo Bluetooth: $(bt_nome)"
  local p; p=$(perfil_ativo)
  if [ -z "$p" ] || [ "$p" = off ]; then
    pactl set-card-profile "$CARD" "$PERFIL_BT_MUSICA" \
      && ok "som do Bluetooth ligado no modo Música" || erro "não consegui ligar o som do Bluetooth"
    p=$(perfil_ativo)
  fi
  case "$p" in
    a2dp*)       ok "modo Música (estéreo). O microfone só funciona no modo Chamada — troque no painel Fone Havit" ;;
    *head_unit*) ok "modo Chamada: microfone ligado (o som fica mono)" ;;
  esac
  esperar 5 eval 'achar_sink_source; [ -n "$SINK" ]' \
    || { erro "a saída do fone não apareceu no PulseAudio"; return 1; }
  finalizar
  pronto
}

finalizar() {  # saída/entrada padrão, mudo, balanço, programas abertos e painel
  ALVO="$SINK"
  if eq_ligado; then
    ALVO="$EQ_SINK"
    pactl set-default-sink "$ALVO" && ok "saída padrão: fone com equalizador (Fone Havit EQ)"
  else
    pactl set-default-sink "$SINK" && ok "fone definido como saída de som padrão"
  fi
  ALVO_SRC="$SOURCE"
  if [ -z "$SOURCE" ]; then
    :
  elif voz_ligado; then
    ALVO_SRC="$VOZ_FONTE"
    pactl set-default-source "$ALVO_SRC" && ok "entrada padrão: microfone do fone com 'só minha voz'"
  else
    pactl set-default-source "$SOURCE" && ok "microfone do fone definido como entrada padrão"
  fi
  pactl set-sink-mute "$SINK" 0
  if [ -n "$SOURCE" ]; then
    pactl set-source-mute "$SOURCE" 0
    ok "saída e microfone sem mudo"
  else
    ok "saída sem mudo"
  fi
  equilibrar
  mover_fluxos
  # painel Fone Havit: religa equalizador e "só minha voz" se estiverem ligados nas configurações
  if [ -f "$PASTA/havit-fone.py" ]; then
    python3 "$PASTA/havit-fone.py" --reaplicar 2>/dev/null
  fi
}

pronto() {
  echo
  echo "  ${VERDE}${NEGRITO}Pronto.${FIM} Se o som de algum programa não mudou para o fone, pause e dê play de novo nele."
  avisar_desktop "Fone corrigido: som no H2002d Pro."
  return 0
}

# ---------- estado ----------

status() {
  titulo "Estado do fone Havit H2002d Pro"
  local via=""
  if receptor_conectado; then
    ok "receptor USB $USB_ID conectado"
    if achar_placa; then via=usb; else erro "PulseAudio sem placa do receptor"; fi
  else
    echo "       receptor USB $USB_ID: não conectado"
    if achar_bt; then via=bt; ok "fone conectado pelo Bluetooth: $(bt_nome)"
    elif bt_desligado; then echo "       Bluetooth do computador: desligado"
    else echo "       Bluetooth: fone não conectado"; fi
  fi
  [ -n "$via" ] || { erro "fone não encontrado (nem receptor USB nem Bluetooth)"; return 1; }
  achar_sink_source
  local p; p=$(perfil_ativo)
  if [ "$via" = bt ]; then
    case "$p" in
      a2dp*)       ok "modo Bluetooth: Música (estéreo, sem microfone)" ;;
      *head_unit*) ok "modo Bluetooth: Chamada (microfone ligado, som mono)" ;;
      *)           aviso "modo Bluetooth: ${p:-?} (sem som — rode a correção)" ;;
    esac
  elif [ "$p" = "$PERFIL" ]; then ok "perfil: $p"; else aviso "perfil: $p   (o certo é $PERFIL)"; fi
  if [ -n "$IDX" ]; then
    local pcm pcm1
    pcm=$(amixer -c "$IDX" cget name='PCM Playback Volume' 2>/dev/null | awk -F= '/: values=/ {print $2}')
    pcm1=$(amixer -c "$IDX" cget name='PCM Playback Volume',index=1 2>/dev/null | awk -F= '/: values=/ {print $2}')
    if [ "$pcm" = "100,100" ]; then ok "volume de hardware PCM (esq,dir): $pcm"
    else aviso "volume de hardware PCM (esq,dir): $pcm   ← com 0 o lado direito fica mudo"; fi
    if [ "$pcm1" = "100" ]; then ok "volume geral do receptor (PCM,1): $pcm1"; else aviso "volume geral do receptor (PCM,1): $pcm1"; fi
    local mic mic1
    mic=$(amixer -c "$IDX" cget name='Mic Capture Volume' 2>/dev/null | awk -F= '/: values=/ {print $2}')
    mic1=$(amixer -c "$IDX" cget name='Mic Capture Volume',index=1 2>/dev/null | awk -F= '/: values=/ {print $2}')
    if [ "$mic" = "31" ] && [ "$mic1" = "31" ]; then ok "ganho de hardware do microfone (Mic, Mic,1): $mic, $mic1"
    else aviso "ganho de hardware do microfone (Mic, Mic,1): $mic, $mic1   ← com 0 o microfone fica mudo"; fi
  fi
  local ds dso
  ds=$(pactl get-default-sink 2>/dev/null); dso=$(pactl get-default-source 2>/dev/null)
  if [ "$ds" = "$SINK" ]; then ok "saída padrão: fone"
  elif [ "$ds" = "$EQ_SINK" ]; then ok "saída padrão: fone com equalizador"
  else aviso "saída padrão: $ds"; fi
  if eq_ligado; then
    local g a
    g=$(sed -n 's/^graves=//p' "$EQ_CFG" 2>/dev/null); a=$(sed -n 's/^agudos=//p' "$EQ_CFG" 2>/dev/null)
    ok "equalizador ligado (graves ${g:-0} dB, agudos ${a:-0} dB)"
  else
    echo "       equalizador: desligado"
  fi
  if [ -z "$SOURCE" ]; then echo "       entrada: sem microfone (Bluetooth no modo Música)"
  elif [ "$dso" = "$SOURCE" ]; then ok "entrada padrão: microfone do fone"
  elif [ "$dso" = "$VOZ_FONTE" ] && voz_ligado; then ok "entrada padrão: microfone do fone com 'só minha voz'"
  else aviso "entrada padrão: $dso"; fi
  if voz_ligado; then ok "só minha voz: ligado"
  elif pactl list short sources 2>/dev/null | awk '{print $2}' | grep -qx "$VOZ_FONTE"; then
    aviso "só minha voz: incompleto (rode a correção ou abra o painel Fone Havit)"
  else echo "       só minha voz: desligado"; fi
  [ -n "$SINK" ] && echo "       volume do fone: $(volumes_sink | sed 's/ /% \/ /;s/$/%/') (esq / dir), mudo: $(pactl get-sink-mute "$SINK" | awk '{print $2}')"
  [ -n "$SOURCE" ] && echo "       volume do microfone: $(pactl get-source-volume "$SOURCE" | grep -o '[0-9]*%' | head -1), mudo: $(pactl get-source-mute "$SOURCE" | awk '{print $2}')"
  local f
  if [ "$via" = usb ]; then
    for f in $OVR_ARQUIVOS; do
      if grep -q "consertar-fone-havit" "$OVR_DIR/$f" 2>/dev/null; then ok "ajuste do mixer instalado: $f"; else aviso "ajuste do mixer ausente: $f"; fi
    done
    if bt_desligado; then
      echo "       Bluetooth do computador: desligado (não afeta o modo 2.4G com receptor USB)"
    fi
  fi
}

# ---------- testes ----------

perguntar() {  # $1 = pergunta; retorna 0 se "s"
  local r; read -r -p "  $1 [s/N] " r </dev/tty || return 1
  case "$r" in s|S|sim|Sim|SIM|y|Y) return 0 ;; *) return 1 ;; esac
}

preparar_teste() {
  { receptor_conectado && achar_placa; } || achar_bt || { erro "fone não encontrado — rode a correção primeiro"; return 1; }
  achar_sink_source
  [ -n "$SINK" ] || { erro "a saída do fone não existe — rode a correção primeiro"; return 1; }
}

testar_som() {
  preparar_teste || return 1
  titulo "Teste de som (esquerdo e direito)"
  echo "  Coloque o fone. Vai tocar uma voz só no lado ESQUERDO e depois só no DIREITO."
  case "$(perfil_ativo)" in
    *head_unit*) aviso "Bluetooth no modo Chamada: o som é mono, os dois lados tocam juntos" ;;
  esac
  sleep 1
  echo "  → tocando no ESQUERDO..."; paplay --device="$SINK" "$SONS/Front_Left.wav"
  sleep 0.5
  echo "  → tocando no DIREITO...";  paplay --device="$SINK" "$SONS/Front_Right.wav"
  if [ -t 0 ]; then
    if perguntar "Ouviu os dois lados, cada um no seu ouvido?"; then ok "som esquerdo e direito funcionando"
    else
      aviso "rodando a correção de novo e repetindo o teste..."
      consertar >/dev/null
      paplay --device="$SINK" "$SONS/Front_Left.wav"; sleep 0.5; paplay --device="$SINK" "$SONS/Front_Right.wav"
      perguntar "E agora, ouviu os dois lados?" && ok "som funcionando" \
        || aviso "ainda com problema: tire e ponha o receptor USB, rode o script de novo e me conte o que acontece"
    fi
  fi
}

testar_mic() {
  preparar_teste || return 1
  if [ -z "$SOURCE" ]; then
    erro "sem microfone: no Bluetooth ele só funciona no modo Chamada (troque no painel Fone Havit)"
    return 1
  fi
  titulo "Teste do microfone"
  local tmp; tmp=$(mktemp --suffix=.wav)
  echo "  Quando aparecer FALE AGORA, fale normalmente por 5 segundos (ex.: conte de 1 a 10)."
  local i; for i in 3 2 1; do printf "  %s... " "$i"; sleep 1; done; echo
  echo "  ${NEGRITO}FALE AGORA${FIM} (gravando 5 s)"
  timeout 5 parecord --device="$SOURCE" --rate=48000 --channels=1 --format=s16le --file-format=wav "$tmp" 2>/dev/null
  echo "  gravação encerrada"
  local res
  res=$(python3 -I -c '
import sys, wave, audioop, math
w = wave.open(sys.argv[1]); fr = w.getframerate(); sw = w.getsampwidth()
d = w.readframes(w.getnframes()); blk = fr // 4 * sw
rms = [audioop.rms(d[i:i+blk], sw) for i in range(0, len(d) - blk + 1, blk)]
pico = audioop.max(d, sw) if d else 0
db = lambda v: 20 * math.log10(v / 32768) if v > 0 else -120.0
if not rms: print("vazio 0 0 0"); sys.exit()
print("ok %.1f %.1f %.1f" % (db(max(rms)), db(min(rms)), db(pico)))
' "$tmp" 2>/dev/null)
  local estado fala fundo pico
  read -r estado fala fundo pico <<<"$res"
  if [ "$estado" != "ok" ]; then
    erro "não gravou nada — o microfone do fone não está mandando áudio"
  else
    echo "  nível da fala: ${fala} dBFS   ruído de fundo: ${fundo} dBFS   pico: ${pico} dBFS"
    if awk "BEGIN{exit !($fala <= -100)}"; then
      erro "silêncio total: o microfone está mudo no próprio fone (botão de mudo ou haste levantada?)"
    elif awk "BEGIN{exit !($fala < -42)}"; then
      aviso "quase nada captado: a fala não apareceu. Confira o botão de mudo do fone e se a haste está abaixada"
    elif awk "BEGIN{exit !($fala < -30)}"; then
      aviso "microfone funcionando, mas BAIXO"
      if [ -t 0 ] && perguntar "Aumentar o ganho do microfone para 150%?"; then
        pactl set-source-volume "$SOURCE" 150% && ok "ganho do microfone em 150%"
      fi
    else
      ok "microfone funcionando com bom volume"
    fi
    if [ -t 0 ] && perguntar "Quer ouvir a gravação no fone?"; then paplay --device="$SINK" "$tmp"; fi
  fi
  rm -f "$tmp"
}

# ---------- desfazer ----------

desfazer() {
  titulo "Removendo os ajustes do consertar-fone-havit"
  local f n=0
  for f in $OVR_ARQUIVOS; do
    if grep -qE "consertar-fone-havit|Havit H2002d" "$OVR_DIR/$f" 2>/dev/null; then
      rm -f "$OVR_DIR/$f" && ok "removido $OVR_DIR/$f" && n=$((n+1))
    fi
  done
  [ $n -eq 0 ] && ok "nenhum ajuste deste script encontrado"
  echo "  Para valer: tire e ponha o receptor USB (ou: systemctl --user restart pulseaudio)."
}

ajuda() { awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"; }

# ---------- principal ----------

case "${1:-}" in
  --status|-s)   status ;;
  --testar|-t)   testar_som; testar_mic ;;
  --som)         testar_som ;;
  --mic|-m)      testar_mic ;;
  --desfazer)    desfazer ;;
  --ajuda|-h|--help) ajuda ;;
  --instalar)
    abrir_no_terminal && exit 0
    instalar
    echo; echo "  ${VERDE}${NEGRITO}Instalação em dia.${FIM} Rode sem --instalar para corrigir o som do fone." ;;
  ""|--janela)
    abrir_no_terminal && exit 0
    instalar
    consertar; rc=$?
    if [ $rc -eq 0 ] && [ -t 0 ] && [ -t 1 ]; then
      echo
      if perguntar "Quer testar agora o som (esquerdo/direito) e o microfone?"; then testar_som; testar_mic; fi
    fi
    # aberto pelo atalho do menu: segura a janela para dar tempo de ler
    if [ "${1:-}" = "--janela" ]; then echo; read -r -p "  Aperte Enter para fechar..." _ </dev/tty || sleep 15; fi
    exit $rc ;;
  *) echo "opção desconhecida: $1"; ajuda; exit 2 ;;
esac
