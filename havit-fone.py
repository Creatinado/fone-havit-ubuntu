#!/usr/bin/env python3
"""
Fone Havit — painel visual (tema escuro) do fone Havit H2002D Pro no Ubuntu 22.04.

  havit-fone.py                 abre o painel
  havit-fone.py --reaplicar     religa equalizador e "só minha voz" conforme o que está salvo
  havit-fone.py --eq G A        liga o equalizador com graves G e agudos A (-12 a +12 dB)
  havit-fone.py --eq-desligar   desliga o equalizador
  havit-fone.py --voz on|off    liga/desliga o modo "só minha voz"
  havit-fone.py --vigiar        (opcional) fica em segundo plano religando tudo quando o
                                receptor USB é reconectado
  havit-fone.py --filtros       compila os filtros do equalizador e do "só minha voz"

Não instala pacotes: quem instala o que faltar é o consertar-fone-havit.sh (veja LEIA-ME.txt).
Usa GTK3, GStreamer, pactl, numpy e dois filtros próprios (filtros/havit_eq.c e filtros/havit_voz.c,
compilados para ~/.local/lib/ladspa/).
"""
import audioop
import cmath
import fcntl
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import wave

PASTA = os.path.dirname(os.path.realpath(__file__))
APP = os.path.realpath(__file__)
SCRIPT = os.path.join(PASTA, "consertar-fone-havit.sh")
LADSPA_DIR = os.path.expanduser("~/.local/lib/ladspa")
PLUGINS = {"havit_eq": os.path.join(PASTA, "filtros", "havit_eq.c"),
           "havit_voz": os.path.join(PASTA, "filtros", "havit_voz.c")}
CFG_DIR = os.path.expanduser("~/.config/havit-fone")
CFG = os.path.join(CFG_DIR, "eq.conf")
CACHE = os.path.expanduser("~/.cache/havit-fone")
ICONE = os.path.join(PASTA, "imagens", "havit-fone.png")
CARD_MATCH = "H2002d_Pro"
USB_ID = "040b:0897"
SONS = "/usr/share/sounds/alsa"
ENV = dict(os.environ, LC_ALL="C")
PROPS = "org.freedesktop.DBus.Properties"

EQ_SINK = "havit_eq"
F_GRAVES, F_AGUDOS, LIMITE = 110.0, 7000.0, 12
PRESETS = [("Normal", 0, 0), ("Graves +", 6, 0), ("Voz", -4, 3), ("Agudos +", 0, 5)]

# cadeia "só minha voz":  mic → loopback → havit_voz_filtro (portão) → havit_voz_saida (nula)
#                         → monitor → havit_voz (entrada "Microfone Havit (só minha voz)")
VOZ_SAIDA, VOZ_FILTRO, VOZ_FONTE = "havit_voz_saida", "havit_voz_filtro", "havit_voz"
VOZ_REDUCAO = -40
NOSSOS_APPS = ("Fone Havit", "parec", "parecord")


def plugin_so(nome):
    return os.path.join(LADSPA_DIR, nome + ".so")


# ======================================================================
#  PulseAudio
# ======================================================================

def pactl(*args, timeout=6):
    try:
        r = subprocess.run(["pactl", *args], env=ENV, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def curta(tipo):
    _, out = pactl("list", "short", tipo)
    return [l.split("\t") for l in out.splitlines() if l.strip()]


def dispositivos():
    """(placa, saída do fone, microfone do fone, eq_existe). Usa o receptor USB; sem ele, o fone
    pareado por Bluetooth (no modo Música do Bluetooth não existe microfone: vem None)."""
    sinks = curta("sinks")
    sources = curta("sources")
    sink = next((c[1] for c in sinks if c[1].startswith("alsa_output.") and CARD_MATCH in c[1]), None)
    source = next((c[1] for c in sources if c[1].startswith("alsa_input.") and CARD_MATCH in c[1]), None)
    card = next((c[1] for c in curta("cards") if CARD_MATCH in c[1]), None)
    eq = any(c[1] == EQ_SINK for c in sinks)
    if not (card or sink):
        bt = fone_bluetooth()
        if bt:
            base = bt["placa"][len("bluez_card."):]
            card = bt["placa"]
            sink = next((c[1] for c in sinks if c[1].startswith(f"bluez_sink.{base}.")), None)
            source = next((c[1] for c in sources if c[1].startswith(f"bluez_source.{base}.")), None)
    return card, sink, source, eq


# ---------------- Bluetooth (fone pareado direto, sem o receptor USB) ----------------

BT_NOMES = ("h2002", "havit")          # trechos do nome Bluetooth do fone (sem diferenciar maiúsculas)
PERFIL_MUSICA = "a2dp_sink"            # estéreo, sem microfone
PERFIS_CHAMADA = ("handsfree_head_unit", "headset_head_unit")   # com microfone, som mono


def fone_bluetooth():
    """O fone conectado por Bluetooth: dict com placa, nome, endereco, perfil (ativo) e perfis
    (disponíveis); ou None."""
    _, out = pactl("list", "cards")
    atual, em_perfis = None, False
    for linha in out.splitlines() + ["Card #fim"]:
        if linha.startswith("Card #"):
            if atual and atual["placa"].startswith("bluez_card.") and \
                    any(n in (atual["nome"] + " " + atual["descricao"]).lower() for n in BT_NOMES):
                atual["nome"] = atual["nome"] or atual["descricao"]
                return atual
            atual = {"placa": "", "nome": "", "descricao": "", "endereco": "", "perfil": "", "perfis": [],
                     "bateria": None}
            em_perfis = False
            continue
        if atual is None:
            continue
        s = linha.strip()
        nivel = len(linha) - len(linha.lstrip("\t"))
        valor = s.split("=", 1)[1].strip().strip('"') if "=" in s else ""
        if nivel == 1:
            em_perfis = s == "Profiles:"
        if s.startswith("Name: "):
            atual["placa"] = s[6:]
        elif s.startswith("bluez.alias = "):
            atual["nome"] = valor
        elif s.startswith("device.description = "):
            atual["descricao"] = valor
        elif s.startswith("bluetooth.battery = "):
            num = re.sub(r"\D", "", valor)
            atual["bateria"] = max(0, min(100, int(num))) if num else None
        elif s.startswith("device.string = "):
            atual["endereco"] = valor
        elif s.startswith("Active Profile: "):
            atual["perfil"] = s[len("Active Profile: "):]
        elif em_perfis and nivel == 2:
            m = re.match(r"([\w-]+): .*available: (\w+)\)$", s)
            if m and m.group(2) != "no":
                atual["perfis"].append(m.group(1))
    return None


def modo_bt(perfil):
    if perfil == PERFIL_MUSICA:
        return "musica"
    if perfil in PERFIS_CHAMADA:
        return "chamada"
    return "desligado"


def bt_trocar_modo(modo):
    """Troca o Bluetooth para 'musica' (A2DP) ou 'chamada' (HFP/HSP) e leva junto o que estava tocando
    ou gravando no fone, o equalizador e o 'só minha voz'."""
    bt = fone_bluetooth()
    if not bt:
        return False, "o fone não está conectado pelo Bluetooth"
    if modo == "chamada":
        perfil = next((p for p in PERFIS_CHAMADA if p in bt["perfis"]), None)
    else:
        perfil = PERFIL_MUSICA if PERFIL_MUSICA in bt["perfis"] else None
    nome_modo = "Chamada" if modo == "chamada" else "Música"
    if not perfil:
        return False, f"o fone não oferece o modo {nome_modo} pelo Bluetooth"
    if bt["perfil"] == perfil:
        return True, f"Bluetooth já está no modo {nome_modo}"
    with Trava():
        _, sink_antes, source_antes, _ = dispositivos()
        idx = {c[0]: c[1] for c in curta("sinks")}
        sons = [c[0] for c in curta("sink-inputs")
                if len(c) > 2 and c[2] != "-" and idx.get(c[1]) in (sink_antes, EQ_SINK)]
        gravs = [g["id"] for g in gravacoes()
                 if g["cliente"] != "-" and g["fonte"] in (source_antes, VOZ_FONTE)
                 and not any(g["app"].startswith(n) for n in NOSSOS_APPS)]
        rc, _ = pactl("set-card-profile", bt["placa"], perfil)
        if rc != 0:
            return False, "o PulseAudio recusou trocar o modo do Bluetooth"
        # espera a saída nova aparecer e o equalizador antigo (preso à saída velha) sumir
        for _ in range(40):
            _, sink, source, _ = dispositivos()
            m = modulo("module-ladspa-sink", f"sink_name={EQ_SINK}")
            eq_velho = bool(m and sink_antes and any(c[0] == m and f"sink_master={sink_antes}" in c[2]
                                                       for c in curta("modules")))
            if sink and sink != sink_antes and (source or modo == "musica") and not eq_velho:
                break
            time.sleep(0.15)
    reaplicar()   # religa equalizador e "só minha voz" na saída/entrada nova
    _, sink, source, eq = dispositivos()
    alvo = EQ_SINK if eq else sink
    if alvo:
        pactl("set-default-sink", alvo)
        for i in sons:
            pactl("move-sink-input", i, alvo)
    entrada = VOZ_FONTE if (source and voz_saudavel(source)) else source
    if entrada:
        pactl("set-default-source", entrada)
        for i in gravs:
            pactl("move-source-output", i, entrada)
    return True, f"Bluetooth no modo {nome_modo}"


def receptor_usb():
    try:
        return subprocess.run(["lsusb", "-d", USB_ID], capture_output=True).returncode == 0
    except Exception:
        return False


def ler_volume(tipo, nome):
    _, out = pactl(f"get-{tipo}-volume", nome)
    pct = [int(x) for x in re.findall(r"(\d+)%", out)]
    _, m = pactl(f"get-{tipo}-mute", nome)
    return (max(pct) if pct else 0), ("yes" in m)


def mover_para(destino, de_sinks):
    """Move os sons dos programas (não os fluxos internos de módulos) que estão em de_sinks."""
    idx = {c[0]: c[1] for c in curta("sinks")}
    for c in curta("sink-inputs"):
        if len(c) < 3 or c[2] == "-":
            continue
        if idx.get(c[1]) in de_sinks:
            pactl("move-sink-input", c[0], destino)


def gravacoes():
    """Lista de (id, nome da fonte, cliente, app) das gravações abertas."""
    idx = {c[0]: c[1] for c in curta("sources")}
    _, out = pactl("list", "source-outputs")
    lista, atual = [], None
    for linha in out.splitlines():
        m = re.match(r"Source Output #(\d+)", linha)
        if m:
            atual = {"id": m.group(1), "fonte": None, "cliente": "-", "app": ""}
            lista.append(atual)
        elif atual is not None:
            s = linha.strip()
            if s.startswith("Source:"):
                atual["fonte"] = idx.get(s.split(":", 1)[1].strip())
            elif s.startswith("Client:"):
                atual["cliente"] = s.split(":", 1)[1].strip()
            elif s.startswith("application.name = "):
                atual["app"] = s.split("=", 1)[1].strip().strip('"')
    return lista


def mover_gravacoes(destino, de_fontes):
    for g in gravacoes():
        if g["cliente"] == "-" or g["fonte"] not in de_fontes:
            continue
        if any(g["app"].startswith(n) for n in NOSSOS_APPS):
            continue
        pactl("move-source-output", g["id"], destino)


def modulo(nome_mod, trecho):
    for c in curta("modules"):
        if len(c) > 2 and c[1] == nome_mod and trecho in c[2]:
            return c[0]
    return None


# ======================================================================
#  Configuração
# ======================================================================

# valores de fábrica do painel (também usados por "Restaurar padrões")
PADRAO = {"ativo": False, "graves": 0, "agudos": 0, "voz": False, "voz_limiar": -45.0}


def ler_cfg():
    c = dict(PADRAO)
    try:
        with open(CFG) as f:
            for linha in f:
                k, _, v = linha.strip().partition("=")
                v = v.strip()
                if k in ("ativo", "voz"):
                    c[k] = v == "1"
                elif k in ("graves", "agudos"):
                    c[k] = int(max(-LIMITE, min(LIMITE, round(float(v)))))
                elif k == "voz_limiar":
                    c[k] = max(-90.0, min(0.0, float(v)))
    except Exception:
        pass
    return c


def salvar_cfg(c):
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = CFG + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Fone Havit — configurações do painel\n"
                f"ativo={1 if c['ativo'] else 0}\ngraves={int(c['graves'])}\nagudos={int(c['agudos'])}\n"
                f"voz={1 if c['voz'] else 0}\n"
                "# limiar do 'só minha voz' em dBFS, referido a 100% de ganho do microfone\n"
                f"voz_limiar={c['voz_limiar']:.1f}\n")
    os.replace(tmp, CFG)


class Trava:
    """Evita que o painel, o script e o vigia mexam nos módulos ao mesmo tempo."""
    def __enter__(self):
        os.makedirs(CACHE, exist_ok=True)
        self.f = open(os.path.join(CACHE, "eq.lock"), "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def garantir_plugin(nome):
    so, src = plugin_so(nome), PLUGINS[nome]
    if os.path.exists(so) and (not os.path.exists(src) or os.path.getmtime(so) >= os.path.getmtime(src)):
        return True
    if not (os.path.exists(src) and shutil.which("gcc")):
        return os.path.exists(so)
    os.makedirs(LADSPA_DIR, exist_ok=True)
    tmp = so + ".novo"
    r = subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-o", tmp, src, "-lm"], capture_output=True)
    if r.returncode == 0:
        os.replace(tmp, so)
        return True
    return os.path.exists(so)


_dbus_con = None


def ladspa_dbus(sink, valores):
    """Muda os controles de um filtro ao vivo, sem cortar o som (D-Bus do PulseAudio)."""
    global _dbus_con
    try:
        import dbus
    except Exception:
        return False
    for _ in range(2):
        try:
            if _dbus_con is None:
                if not any(c[1] == "module-dbus-protocol" for c in curta("modules")):
                    pactl("load-module", "module-dbus-protocol")
                addr = dbus.SessionBus().get_object("org.PulseAudio1", "/org/pulseaudio/server_lookup1").Get(
                    "org.PulseAudio.ServerLookup1", "Address", dbus_interface=PROPS)
                _dbus_con = dbus.connection.Connection(addr)
            core = _dbus_con.get_object(object_path="/org/pulseaudio/core1")
            caminho = core.GetSinkByName(sink, dbus_interface="org.PulseAudio.Core1")
            _dbus_con.get_object(object_path=caminho).Set(
                "org.PulseAudio.Ext.Ladspa1", "AlgorithmParameters",
                dbus.Struct((dbus.Array([float(v) for v in valores], signature="d"),
                             dbus.Array([False] * len(valores), signature="b"))),
                dbus_interface=PROPS)
            return True
        except Exception:
            _dbus_con = None
    return False


# ======================================================================
#  Equalizador
# ======================================================================

def _carregar_eq(sink, graves, agudos):
    rc, _ = pactl("load-module", "module-ladspa-sink", f"sink_name={EQ_SINK}", f"sink_master={sink}",
                  f"plugin={plugin_so('havit_eq')}", "label=havit_eq", f"control={int(graves)},{int(agudos)}",
                  "sink_properties=\"device.description='Fone Havit (EQ)'\"")
    return rc == 0


def eq_ligar(graves, agudos):
    with Trava():
        _, sink, _, eq = dispositivos()
        if not sink:
            return False, "fone não conectado"
        if not eq:
            if not garantir_plugin("havit_eq"):
                return False, "filtro do equalizador não encontrado"
            if not _carregar_eq(sink, graves, agudos):
                return False, "o PulseAudio recusou criar a saída com equalizador"
        elif not ladspa_dbus(EQ_SINK, (graves, agudos)):
            m = modulo("module-ladspa-sink", f"sink_name={EQ_SINK}")
            if m:
                pactl("unload-module", m)
            _carregar_eq(sink, graves, agudos)
        pactl("set-default-sink", EQ_SINK)
        mover_para(EQ_SINK, {sink})
        return True, "equalizador ligado"


def eq_ajustar(graves, agudos):
    return ladspa_dbus(EQ_SINK, (graves, agudos)) or eq_ligar(graves, agudos)[0]


def eq_desligar():
    with Trava():
        _, sink, _, _ = dispositivos()
        if sink:
            pactl("set-default-sink", sink)
            mover_para(sink, {EQ_SINK})
        m = modulo("module-ladspa-sink", f"sink_name={EQ_SINK}")
        if m:
            pactl("unload-module", m)
    return True, "equalizador desligado"


def biquad_shelf(alto, fs, f0, db):
    A = 10 ** (db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    c, s = math.cos(w0), math.sin(w0)
    sa = 2 * math.sqrt(A) * (s / 2 * math.sqrt(2))
    if not alto:
        b = (A * ((A + 1) - (A - 1) * c + sa), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - sa))
        a = ((A + 1) + (A - 1) * c + sa, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sa)
    else:
        b = (A * ((A + 1) + (A - 1) * c + sa), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - sa))
        a = ((A + 1) - (A - 1) * c + sa, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sa)
    return b, a


def resposta_db(f, graves, agudos, fs=48000.0):
    z = cmath.exp(-1j * 2 * math.pi * f / fs)
    total = 0.0
    for alto, f0, g in ((False, F_GRAVES, graves), (True, F_AGUDOS, agudos)):
        b, a = biquad_shelf(alto, fs, f0, g)
        h = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
        total += 20 * math.log10(max(abs(h), 1e-9))
    return total


# ======================================================================
#  Só minha voz
# ======================================================================

def ganho_mic_db(source):
    """Ganho do microfone no PulseAudio em dB (o volume do PA é cúbico: 150% ≈ +10,6 dB)."""
    if not source:
        return 0.0
    pct, _ = ler_volume("source", source)
    return 60 * math.log10(pct / 100) if pct > 0 else -60.0


def limiar_efetivo(cfg, source):
    """O limiar salvo vale para 100% de ganho; acompanha o ganho atual do microfone."""
    return max(-80.0, min(-5.0, cfg["voz_limiar"] + ganho_mic_db(source)))


def _modulos_voz():
    return [modulo("module-remap-source", f"source_name={VOZ_FONTE}"),
            modulo("module-loopback", f"sink={VOZ_FILTRO}"),
            modulo("module-ladspa-sink", f"sink_name={VOZ_FILTRO}"),
            modulo("module-null-sink", f"sink_name={VOZ_SAIDA}")]


def voz_saudavel(source):
    m = _modulos_voz()
    if not all(m):
        return False
    lb = next((c for c in curta("modules") if c[0] == m[1]), None)
    return bool(lb and f"source={source}" in lb[2])


def _desmontar_voz():
    for m in _modulos_voz():
        if m:
            pactl("unload-module", m)


def voz_ligar(limiar):
    with Trava():
        card, _, source, _ = dispositivos()
        if not source:
            if card and card.startswith("bluez_card."):
                return False, "no Bluetooth o microfone só funciona no modo Chamada"
            return False, "microfone do fone não encontrado"
        if voz_saudavel(source):
            if not ladspa_dbus(VOZ_FILTRO, (limiar, VOZ_REDUCAO)):
                return False, "não consegui ajustar o 'só minha voz'"
        else:
            if not garantir_plugin("havit_voz"):
                return False, "filtro de voz não encontrado"
            _desmontar_voz()
            _, saida_antes = pactl("get-default-sink")
            passos = [
                ("module-null-sink", f"sink_name={VOZ_SAIDA}", "format=float32le", "rate=48000", "channels=1",
                 "channel_map=mono", "sink_properties=\"device.description='Havit voz (interno, nao usar)'\""),
                ("module-ladspa-sink", f"sink_name={VOZ_FILTRO}", f"sink_master={VOZ_SAIDA}",
                 f"plugin={plugin_so('havit_voz')}", "label=havit_voz", f"control={limiar:.1f},{VOZ_REDUCAO}",
                 "rate=48000", "channels=1", "channel_map=mono",
                 "sink_properties=\"device.description='Havit voz filtro (interno, nao usar)'\""),
                ("module-loopback", f"source={source}", f"sink={VOZ_FILTRO}", "latency_msec=30",
                 "source_dont_move=true", "sink_dont_move=true", "remix=false"),
                ("module-remap-source", f"source_name={VOZ_FONTE}", f"master={VOZ_SAIDA}.monitor",
                 "channels=1", "channel_map=mono", "master_channel_map=mono",
                 "source_properties=\"device.description='Microfone Havit (so minha voz)'\""),
            ]
            for p in passos:
                rc, _ = pactl("load-module", *p)
                if rc != 0:
                    _desmontar_voz()
                    return False, f"o PulseAudio recusou {p[0]}"
            # um dispositivo novo pode ter virado a saída padrão: devolve a anterior
            _, saida_agora = pactl("get-default-sink")
            if saida_antes.strip() and saida_agora.strip() != saida_antes.strip():
                pactl("set-default-sink", saida_antes.strip())
        pactl("set-default-source", VOZ_FONTE)
        mover_gravacoes(VOZ_FONTE, {source})
        return True, "só minha voz ligado"


def voz_ajustar(limiar):
    return ladspa_dbus(VOZ_FILTRO, (limiar, VOZ_REDUCAO)) or voz_ligar(limiar)[0]


def voz_desligar():
    with Trava():
        _, _, source, _ = dispositivos()
        if source:
            pactl("set-default-source", source)
            mover_gravacoes(source, {VOZ_FONTE})
        _desmontar_voz()
    return True, "só minha voz desligado"


def reaplicar():
    """Deixa EQ e 'só minha voz' como estão salvos (usado ao abrir, ao reconectar e pelo script)."""
    c = ler_cfg()
    _, sink, source, eq = dispositivos()
    msgs = []
    if c["ativo"] and sink and not eq:
        msgs.append(eq_ligar(c["graves"], c["agudos"])[1])
    elif not c["ativo"] and eq:
        msgs.append(eq_desligar()[1])
    partes = any(_modulos_voz())
    if c["voz"] and source:
        if not voz_saudavel(source):
            msgs.append(voz_ligar(limiar_efetivo(c, source))[1])
    elif partes:
        # desligado, ou o microfone sumiu (receptor desconectado): não deixa uma entrada muda
        # como padrão
        with Trava():
            _desmontar_voz()
        msgs.append("só minha voz desmontado" + ("" if c["voz"] else " (desligado)"))
    return msgs


def calibrar(silencio, fala, ganho_db):
    """Recebe duas gravações cruas (s16le mono 48 kHz) e devolve (limiar_ref, sala, voz, aviso)."""
    import numpy as np

    def niveis(raw):
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
        if len(x) < 4800:
            return np.array([-120.0])
        k = 480                                              # tira graves (< ~100 Hz)
        x = x - np.convolve(x, np.ones(k) / k, mode="same")
        n = len(x) // 240                                    # blocos de 5 ms
        p = (x[:n * 240].reshape(n, 240) ** 2).mean(axis=1)
        return 10 * np.log10(p + 1e-12)

    sala = float(np.percentile(niveis(silencio), 98))
    voz = float(np.percentile(niveis(fala), 80))
    dif = voz - sala
    aviso = ""
    if dif < 6:
        lim = sala + 2
        aviso = "Sua voz quase não ficou mais alta que a sala: aproxime a haste do microfone da boca e calibre de novo."
    else:
        lim = sala + max(3.0, 0.35 * dif)
    lim = max(-80.0, min(-5.0, lim))
    return lim - ganho_db, sala, voz, aviso


# ======================================================================
#  Vigia (opcional, segundo plano)
# ======================================================================

def vigiar():
    os.makedirs(CACHE, exist_ok=True)
    trava = open(os.path.join(CACHE, "vigia.lock"), "w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # já existe um vigia rodando
    while True:
        try:
            reaplicar()
            p = subprocess.Popen(["pactl", "subscribe"], env=ENV, stdout=subprocess.PIPE, text=True)
            for linha in p.stdout:
                if ("'new' on sink #" in linha or "'new' on card" in linha
                        or "'new' on source #" in linha or "'remove' on source #" in linha):
                    time.sleep(1.0)
                    reaplicar()
            p.wait()
        except Exception:
            pass
        time.sleep(3)  # o PulseAudio reiniciou ou caiu: tenta de novo


# ======================================================================
#  Painel (GTK3) — tema escuro de software de headset
# ======================================================================

VERSAO = "2.2.0"
IMAGEM_FONE = os.path.join(PASTA, "imagens", "havit-headset.png")
IMAGEM_FONE_SVG = os.path.join(PASTA, "imagens", "havit-headset.svg")
LARGURA_LATERAL = 244
VERDE = (0.29, 0.86, 0.52)
COR_ERRO, COR_AVISO = "#ff8080", "#f3c56b"


def _engrenagem():
    pts = []
    for i in range(8):
        base = 2 * math.pi * i / 8
        for da, r in ((-0.34, 7.0), (-0.2, 9.6), (0.2, 9.6), (0.34, 7.0)):
            pts.append(f"{12 + r * math.cos(base + da):.2f} {12 + r * math.sin(base + da):.2f}")
    return '<path d="M' + " L".join(pts) + ' Z"/><circle cx="12" cy="12" r="3.1"/>'


# ícones de traço (24×24); CUR = cor atual do texto
_MIC = '<rect x="9" y="2.8" width="6" height="11.6" rx="3"/><path d="M5.6 11.4a6.4 6.4 0 0 0 12.8 0M12 17.8v3.4"/>'
_ALTO = '<path d="M3.8 9.4h3.6L12 5.6v12.8l-4.6-3.8H3.8z"/>'
ICONES = {
    "inicio": '<path d="M3.6 10.4 12 3.8l8.4 6.6V19a1.6 1.6 0 0 1-1.6 1.6h-4.1v-5.9H9.3v5.9H5.2A1.6 1.6 0 0 1 3.6 19z"/>',
    "audio": '<path d="M3 10.6v2.8M6.6 7.6v8.8M10.2 4.4v15.2M13.8 7.6v8.8M17.4 9.6v4.8M21 11.2v1.6"/>',
    "equalizador": '<path d="M5.5 3.6v5.2M5.5 12.8v7.6M12 3.6v9.2M12 16.8v3.6M18.5 3.6v3.2M18.5 10.8v9.6"/>'
                   '<circle cx="5.5" cy="10.8" r="2"/><circle cx="12" cy="14.8" r="2"/><circle cx="18.5" cy="8.8" r="2"/>',
    "config": _engrenagem(),
    "sobre": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.6"/><circle cx="12" cy="7.7" r="0.7" fill="CUR"/>',
    "volume": _ALTO + '<path d="M15.6 9a4.2 4.2 0 0 1 0 6M18.3 6.4a7.8 7.8 0 0 1 0 11.2"/>',
    "volume-mudo": _ALTO + '<path d="M16 9.6l4.8 4.8M20.8 9.6 16 14.4"/>',
    "ajustes": '<path d="M8 3.6v4.6M8 12.2v8.2M16 3.6v8.6M16 16.2v4.2"/>'
               '<circle cx="8" cy="10.2" r="2"/><circle cx="16" cy="14.2" r="2"/>',
    "fone": '<path d="M4 16.2v-3.8a8 8 0 0 1 16 0v3.8"/>'
            '<path d="M4 14.4h2.6a1 1 0 0 1 1 1v4.2a1 1 0 0 1-1 1H5.6A1.6 1.6 0 0 1 4 19z"/>'
            '<path d="M20 14.4h-2.6a1 1 0 0 0-1 1v4.2a1 1 0 0 0 1 1h0.8A1.6 1.6 0 0 0 20 19z"/>',
    "mic": _MIC,
    "mic-mudo": _MIC + '<path d="M3.6 3.6l16.8 16.8"/>',
    "barras": '<path d="M5 9.4v5.2M9 5.4v13.2M13 8.4v7.2M17 3.8v16.4M21 10.4v3.2"/>',
    "voz": '<circle cx="9" cy="8.2" r="3.6"/><path d="M2.6 20.2a6.4 6.4 0 0 1 12.8 0"/>'
           '<path d="M17.2 6.4a4.2 4.2 0 0 1 0 5.6M19.8 3.8a8 8 0 0 1 0 10.8"/>',
    "alvo": '<circle cx="12" cy="12" r="8.6"/><circle cx="12" cy="12" r="4.4"/><circle cx="12" cy="12" r="1.1" fill="CUR"/>',
    "ferramenta": '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94'
                  'l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94z"/>',
    "raio": '<path d="M13.2 2.6 4.6 13.4h6.6l-1 8 8.6-10.8h-6.6z"/>',
    "pulso": '<path d="M2.6 12h4l2.6-7 5.2 14 2.6-7h4.4"/>',
    "desfazer": '<path d="M8.8 14.4 4 9.6l4.8-4.8"/><path d="M4 9.6h10.4a5.6 5.6 0 0 1 0 11.2H11"/>',
    "lista": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.4" cy="6" r="0.9" fill="CUR"/>'
             '<circle cx="4.4" cy="12" r="0.9" fill="CUR"/><circle cx="4.4" cy="18" r="0.9" fill="CUR"/>',
    "restaurar": '<path d="M3.6 12a8.4 8.4 0 1 0 2.5-6"/><path d="M3.6 3.4v4.8h4.8"/>',
    "seta": '<path d="M9.4 5.4 16 12l-6.6 6.6"/>',
    "play": '<path d="M7.6 5.2v13.6L18.8 12z" fill="CUR"/>',
    "menos": '<path d="M6 12h12"/>',
    "mais": '<path d="M6 12h12M12 6v12"/>',
}

CSS = """
* { font-family: Lato, "Noto Sans", Ubuntu, sans-serif; text-shadow: none; -gtk-icon-shadow: none;
    outline-color: rgba(255, 255, 255, 0.22); }
window.havit, window.havit.background { background-color: #080808; color: #f2f2f2; }
.lateral { background-color: #0e0e0e; border-right: 1px solid #1d1d1d; border-bottom-left-radius: 14px; }
.conteudo { background-color: #080808; border-bottom-right-radius: 14px; }
scrolledwindow, viewport { background-color: transparent; border: none; }

/* barra de título dividida (lateral + área principal) */
.hb-lateral, .hb-principal { background-image: none; box-shadow: none; border: none; min-height: 46px; padding: 0 10px; }
.hb-lateral { background-color: #0e0e0e; border-right: 1px solid #1d1d1d; border-top-left-radius: 14px; }
.hb-principal { background-color: #080808; border-top-right-radius: 14px; }
.hb-principal button.titlebutton { background-image: none; background-color: transparent; border: none; box-shadow: none;
    color: #cfcfcf; min-width: 36px; min-height: 30px; padding: 0; margin: 0 2px; border-radius: 8px; }
.hb-principal button.titlebutton:hover { background-color: #1c1c1c; color: #ffffff; }
.hb-principal button.titlebutton.close:hover { background-color: #3a1717; }
decoration { border-radius: 14px; box-shadow: 0 8px 26px rgba(0, 0, 0, 0.65), 0 0 0 1px #242424; }
window.maximized decoration, window.maximized .hb-lateral, window.maximized .hb-principal,
window.maximized .lateral, window.maximized .conteudo, window.tiled decoration { border-radius: 0; }

/* lateral */
.marca { color: #f5f5f5; font-size: 31px; font-weight: 800; }
.marca-sub { color: #9b9b9b; font-size: 16px; }
button.nav { background-image: none; background-color: transparent; border: none; box-shadow: none; border-radius: 12px;
    padding: 11px 16px; color: #a6a6a6; }
button.nav label { font-size: 15.5px; }
button.nav:hover { background-color: #171717; color: #ececec; }
button.nav.ativo { background-color: #232323; color: #ffffff;
    box-shadow: inset 0 1px rgba(255, 255, 255, 0.05), 0 4px 12px rgba(0, 0, 0, 0.45); }

/* cards */
.card { background-color: #181818; border: 1px solid #262626; border-radius: 18px; padding: 18px 22px;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.38), inset 0 1px rgba(255, 255, 255, 0.03); }
.card-topo { background-color: #161616;
    background-image: linear-gradient(100deg, #1f1f1f 0%, #171717 45%, #121212 100%); padding: 4px 24px 4px 8px; }
.card-linha { padding: 12px 22px 12px 24px; }

/* textos */
.titulo-grande { font-size: 33px; font-weight: 600; color: #f7f7f7; }
.subtitulo { font-size: 18px; color: #a3a3a3; }
.info { font-size: 14px; color: #8c8c8c; }
.titulo-pagina { font-size: 27px; font-weight: 600; color: #f5f5f5; }
.card-titulo { font-size: 17px; font-weight: 500; color: #ececec; }
.numero { font-size: 42px; font-weight: 500; color: #f8f8f8; }
.numero.apagado { color: #5e5e5e; }
.unidade { font-size: 13px; color: #8a8a8a; }
.dica { font-size: 13.5px; color: #8b8b8b; }
.dica-media { font-size: 14.5px; color: #9a9a9a; }
.dica-clara { font-size: 13.5px; color: #d6d6d6; }
.rotulo-forte { font-size: 12.5px; font-weight: 700; color: #d8d8d8; }
.valor-pequeno { font-size: 14px; font-weight: 700; color: #ffffff; min-width: 58px; }
.estado { font-size: 11.5px; font-weight: 700; color: #a8a8a8; }
.pilula { background-color: #111111; border: 1px solid #272727; border-radius: 20px; padding: 7px 16px 7px 12px; }
.pilula-texto { font-size: 15px; color: #f2f2f2; }
.bateria-texto { font-size: 18px; color: #f2f2f2; }
.mono { font-family: "DejaVu Sans Mono", monospace; font-size: 12px; color: #b8b8b8; }
.chave { font-size: 13.5px; color: #8b8b8b; }
.valor-info { font-size: 13.5px; color: #e6e6e6; }

/* botões */
button { background-image: none; box-shadow: none; text-shadow: none; -gtk-icon-shadow: none; }
button.botao { background-color: #1e1e1e; border: 1px solid #2c2c2c; border-radius: 12px; padding: 10px 16px; color: #d9d9d9; }
button.botao label { font-size: 15px; }
button.botao:hover { background-color: #252525; border-color: #3b3b3b; color: #ffffff; }
button.botao:active, button.botao:checked { background-color: #2d2d2d; border-color: #4a4a4a; color: #ffffff; }
button.botao:disabled { background-color: #141414; border-color: #1f1f1f; color: #555555; }
button.botao.principal { background-color: #f2f2f2; border-color: #f2f2f2; color: #0b0b0b; }
button.botao.principal label { font-weight: 700; }
button.botao.principal:hover { background-color: #ffffff; border-color: #ffffff; }
button.botao.principal:disabled { background-color: #3a3a3a; border-color: #3a3a3a; color: #777777; }
button.botao.pequeno { padding: 8px 18px; }
button.botao.quadrado { padding: 10px 15px; }
button.botao.mudo { padding: 6px 14px; }
button.botao.mudo:checked { background-color: #f2f2f2; border-color: #f2f2f2; color: #0b0b0b; }
button.mais-menos { background-color: #1e1e1e; border: 1px solid #2c2c2c; border-radius: 17px; min-width: 34px;
    min-height: 34px; padding: 0; color: #d9d9d9; }
button.mais-menos:hover { background-color: #262626; color: #ffffff; }
button.icone-btn { background-color: transparent; border: none; border-radius: 10px; padding: 5px; color: #cfcfcf;
    min-width: 0; min-height: 0; }
button.icone-btn:hover { background-color: #232323; color: #ffffff; }
button.icone-btn:checked { background-color: transparent; color: #7a7a7a; }
button.icone-btn:checked:hover { background-color: #232323; color: #bdbdbd; }
button.icone-btn:disabled { color: #4a4a4a; }
button.redondo { background-color: #1b1b1b; border: 1px solid #2c2c2c; border-radius: 22px; min-width: 42px;
    min-height: 42px; padding: 0; color: #e0e0e0; }
button.redondo:hover { background-color: #252525; border-color: #3a3a3a; }
button.preset { background-color: #1c1c1c; border: 1px solid #2a2a2a; border-radius: 12px; padding: 9px 8px; color: #bdbdbd; }
button.preset label { font-size: 15px; }
button.preset:hover { background-color: #222222; border-color: #383838; color: #f0f0f0; }
button.segmento { padding: 5px 14px; border-radius: 10px; }
button.segmento label { font-size: 13px; }
button.preset.ativo { background-color: #101010; border-color: #ededed; color: #ffffff;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.3); }
button.tile { background-color: #1b1b1b; border: 1px solid #2a2a2a; border-radius: 14px; padding: 12px 6px 10px;
    min-width: 118px; color: #cfcfcf; }
button.tile label { font-size: 13.5px; }
button.tile:hover { background-color: #222222; border-color: #3a3a3a; color: #ffffff; }
button.tile.destaque { background-color: #121212; border-color: #ececec; color: #ffffff;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.3); }
button.tile:disabled { color: #555555; border-color: #1f1f1f; box-shadow: none; }
button.card-botao { background-color: #181818; border: 1px solid #262626; border-radius: 18px; padding: 14px 20px;
    color: #ececec; box-shadow: 0 10px 28px rgba(0, 0, 0, 0.38), inset 0 1px rgba(255, 255, 255, 0.03); }
button.card-botao:hover { background-color: #1d1d1d; border-color: #333333; }
button.card-botao.versao { color: #9a9a9a; }

/* deslizantes */
scale { padding: 10px 2px; }
scale trough { background-image: none; background-color: #343434; border: none; border-radius: 4px; min-height: 6px; }
scale highlight { background-image: none; background-color: #f0f0f0; border: none; border-radius: 4px; min-height: 6px; }
scale slider { background-image: none; background-color: #fafafa; border: none; min-width: 24px; min-height: 24px;
    margin: -9px; border-radius: 13px; box-shadow: 0 2px 6px rgba(0, 0, 0, 0.65); }
scale slider:hover { background-color: #ffffff; box-shadow: 0 0 0 5px rgba(255, 255, 255, 0.08), 0 2px 6px rgba(0, 0, 0, 0.65); }
scale:disabled highlight { background-color: #4a4a4a; }
scale:disabled slider { background-color: #6a6a6a; box-shadow: none; }
scale marks, scale mark indicator { color: #484848; }
scale.sem-preenchimento highlight { background-color: transparent; }

/* chaves liga/desliga */
switch { background-image: none; background-color: #262626; border: 1px solid #383838; border-radius: 16px;
    box-shadow: none; color: transparent; font-size: 1px; padding: 0; }
switch:checked { background-color: #f2f2f2; border-color: #f2f2f2; }
switch slider { background-image: none; background-color: #f5f5f5; border: none; border-radius: 13px;
    min-width: 24px; min-height: 24px; margin: 1px; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.55); }
switch:checked slider { background-color: #0d0d0d; }
switch:disabled { background-color: #1a1a1a; border-color: #262626; }
switch:disabled slider { background-color: #555555; }
switch image { color: transparent; }

scrollbar { background-color: transparent; border: none; }
scrollbar slider { background-color: #2e2e2e; border: none; min-width: 6px; min-height: 40px; border-radius: 3px; }
scrollbar slider:hover { background-color: #444444; }
textview, textview text { background-color: #0d0d0d; color: #c8c8c8; font-family: "DejaVu Sans Mono", monospace; font-size: 12px; }
scrolledwindow.registro { border: 1px solid #222222; border-radius: 12px; background-color: #0d0d0d; }
tooltip, tooltip.background { background-color: #1d1d1d; border: 1px solid #333333; border-radius: 8px; color: #eeeeee; }
tooltip label { color: #eeeeee; }
messagedialog, dialog, messagedialog.background { background-color: #161616; color: #eeeeee; }
messagedialog headerbar, dialog headerbar { background-image: none; background-color: #161616; border: none; box-shadow: none; }
dialog button, messagedialog button { background-color: #222222; border: 1px solid #333333; border-radius: 10px;
    padding: 8px 18px; color: #eeeeee; }
dialog button:hover, messagedialog button:hover { background-color: #2c2c2c; }
""".encode()


def principal():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gst", "1.0")
    from gi.repository import Gtk, Gdk, GLib, Gst, GdkPixbuf, GObject, Pango
    try:
        gi.require_version("Rsvg", "2.0")
        from gi.repository import Rsvg
    except Exception:
        Rsvg = None
    import cairo
    Gst.init(None)
    LIGA = GObject.BindingFlags.BIDIRECTIONAL | GObject.BindingFlags.SYNC_CREATE
    V, H = Gtk.Orientation.VERTICAL, Gtk.Orientation.HORIZONTAL

    def classe(w, *nomes):
        for n in nomes:
            w.get_style_context().add_class(n)
        return w

    def rotulo(texto, *classes, xalign=0.0, **kw):
        return classe(Gtk.Label(label=texto, xalign=xalign, **kw), *classes)

    def db_txt(v):
        return f"{v:.0f} dB".replace("-", "−")

    def esc(t):
        return GLib.markup_escape_text(t)

    def sem_markup(m):
        return re.sub(r"<[^>]+>", "", m).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

    def caixa_arred(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def pintar_svg(cr, h, x, y, w, alt):
        try:
            r = Rsvg.Rectangle()
            r.x, r.y, r.width, r.height = x, y, w, alt
            h.render_document(cr, r)
        except AttributeError:
            cr.save()
            cr.translate(x, y)
            d = h.get_dimensions()
            cr.scale(w / d.width, alt / d.height)
            h.render_cairo(cr)
            cr.restore()

    svg_cache = {}

    class Icone(Gtk.DrawingArea):
        """Ícone de traço desenhado na cor do texto (acompanha hover, desativado etc.)."""
        def __init__(self, nome, tamanho=20):
            super().__init__()
            self.nome, self.t = nome, tamanho
            self.set_size_request(tamanho, tamanho)
            self.set_valign(Gtk.Align.CENTER)
            self.set_halign(Gtk.Align.CENTER)
            self.connect("draw", self.desenhar)

        def definir(self, nome):
            if nome != self.nome:
                self.nome = nome
                self.queue_draw()

        def desenhar(self, w, cr):
            if Rsvg is None:
                return
            c = self.get_style_context().get_color(self.get_state_flags())
            cor = "#%02x%02x%02x" % (int(c.red * 255), int(c.green * 255), int(c.blue * 255))
            chave = (self.nome, cor)
            h = svg_cache.get(chave)
            if h is None:
                svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24" fill="none" '
                       f'stroke="{cor}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
                       + ICONES.get(self.nome, "").replace("CUR", cor) + "</svg>")
                try:
                    h = Rsvg.Handle.new_from_data(svg.encode())
                except Exception:
                    h = False
                svg_cache[chave] = h
            if not h:
                return
            L, A = w.get_allocated_width(), w.get_allocated_height()
            if c.alpha < 0.99:
                cr.push_group()
            pintar_svg(cr, h, (L - self.t) / 2, (A - self.t) / 2, self.t, self.t)
            if c.alpha < 0.99:
                cr.pop_group_to_source()
                cr.paint_with_alpha(c.alpha)

    class ImagemFone(Gtk.DrawingArea):
        """Foto do fone (havit-headset.png; o desenho .svg é a reserva), encaixada e opcionalmente
        cortada/esmaecida."""
        def __init__(self, largura, altura, alfa=1.0, zoom=1.0, ax=0.5, ay=0.5, esmaecer=False):
            super().__init__()
            self.set_size_request(largura, altura)
            self.alfa, self.zoom, self.ax, self.ay, self.esmaecer = alfa, zoom, ax, ay, esmaecer
            self.pb = self.h = None
            self.escalada = None
            self.iw, self.ih = 420.0, 380.0
            try:
                self.pb = GdkPixbuf.Pixbuf.new_from_file(IMAGEM_FONE)
                self.iw, self.ih = float(self.pb.get_width()), float(self.pb.get_height())
            except Exception:
                if Rsvg and os.path.exists(IMAGEM_FONE_SVG):
                    try:
                        self.h = Rsvg.Handle.new_from_file(IMAGEM_FONE_SVG)
                    except Exception:
                        self.h = None
            self.connect("draw", self.desenhar)

        def desenhar(self, w, cr):
            if not (self.pb or self.h):
                return
            L, A = w.get_allocated_width(), w.get_allocated_height()
            e = min(L / self.iw, A / self.ih) * self.zoom
            iw, ia = max(1, round(self.iw * e)), max(1, round(self.ih * e))
            x, y = round((L - iw) * self.ax), round((A - ia) * self.ay)
            cr.push_group()
            if self.pb:
                if not self.escalada or self.escalada.get_width() != iw or self.escalada.get_height() != ia:
                    self.escalada = self.pb.scale_simple(iw, ia, GdkPixbuf.InterpType.HYPER)
                Gdk.cairo_set_source_pixbuf(cr, self.escalada, x, y)
                cr.paint()
            else:
                pintar_svg(cr, self.h, x, y, iw, ia)
            cr.pop_group_to_source()
            if self.esmaecer:
                g = cairo.LinearGradient(0, 0, 0, A)
                g.add_color_stop_rgba(0, 0, 0, 0, self.alfa)
                g.add_color_stop_rgba(0.55, 0, 0, 0, self.alfa * 0.8)
                g.add_color_stop_rgba(1, 0, 0, 0, 0)
                cr.mask(g)
            else:
                cr.paint_with_alpha(self.alfa)

    class Bateria(Gtk.DrawingArea):
        """Ícone de bateria: verde, amarelo abaixo de 25% e vermelho abaixo de 10%."""
        def __init__(self):
            super().__init__()
            self.set_size_request(40, 20)
            self.set_valign(Gtk.Align.CENTER)
            self.nivel = None
            self.connect("draw", self.desenhar)

        def definir(self, nivel):
            if nivel != self.nivel:
                self.nivel = nivel
                self.queue_draw()

        def desenhar(self, w, cr):
            A = w.get_allocated_height()
            x, y, larg, alt = 1.5, (A - 17) / 2, 33.0, 17.0
            cr.set_line_width(1.6)
            cr.set_source_rgb(0.85, 0.85, 0.85)
            caixa_arred(cr, x, y, larg, alt, 4)
            cr.stroke()
            caixa_arred(cr, x + larg + 1.5, y + 5, 3, alt - 10, 1.2)
            cr.fill()
            if self.nivel is None:
                return
            if self.nivel <= 10:
                cr.set_source_rgb(0.95, 0.33, 0.33)
            elif self.nivel <= 25:
                cr.set_source_rgb(0.95, 0.72, 0.27)
            else:
                cr.set_source_rgb(*VERDE)
            cheio = (larg - 5) * self.nivel / 100.0
            if cheio > 0.5:
                caixa_arred(cr, x + 2.5, y + 2.5, cheio, alt - 5, 2.2)
                cr.fill()

    class Led(Gtk.DrawingArea):
        CORES = {"ok": VERDE, "aviso": (0.95, 0.72, 0.27), "off": (0.33, 0.33, 0.33)}

        def __init__(self, tam=10):
            super().__init__()
            self.tam = tam
            self.set_size_request(tam + 8, tam + 8)
            self.set_valign(Gtk.Align.CENTER)
            self.estado = "off"
            self.connect("draw", self.desenhar)

        def definir(self, estado):
            estado = {True: "ok", False: "off"}.get(estado, estado)
            if estado != self.estado:
                self.estado = estado
                self.queue_draw()

        def desenhar(self, w, cr):
            x, y = w.get_allocated_width() / 2, w.get_allocated_height() / 2
            r, g, b = self.CORES.get(self.estado, self.CORES["off"])
            if self.estado != "off":
                cr.set_source_rgba(r, g, b, 0.22)
                cr.arc(x, y, self.tam / 2 + 3.5, 0, 2 * math.pi)
                cr.fill()
            cr.set_source_rgb(r, g, b)
            cr.arc(x, y, self.tam / 2, 0, 2 * math.pi)
            cr.fill()

    class Nivel(Gtk.DrawingArea):
        """Base dos medidores de nível do microfone (suaviza a queda e segura o pico)."""
        def __init__(self, altura):
            super().__init__()
            self.set_size_request(-1, altura)
            self.nivel = self.pico = -90.0
            self.pico_t = 0.0
            self.limiar = None
            self.connect("draw", self.desenhar)

        def definir(self, rms_db, pico_db):
            self.nivel = rms_db if rms_db > self.nivel else self.nivel * 0.6 + rms_db * 0.4
            agora = time.monotonic()
            if pico_db >= self.pico or agora - self.pico_t > 1.2:
                self.pico, self.pico_t = pico_db, agora
            self.queue_draw()

        @staticmethod
        def frac(db):
            return max(0.0, min(1.0, (db + 70) / 70))

    class Tracos(Nivel):
        """Indicador de nível em traços verticais (card do microfone da tela principal)."""
        def __init__(self):
            super().__init__(24)

        def desenhar(self, w, cr):
            L, A = w.get_allocated_width(), w.get_allocated_height()
            passo, alt = 19.5, 16.0
            n = max(8, min(18, int(L * 0.82 // passo)))
            x0 = (L - (n - 1) * passo) / 2
            y0 = (A - alt) / 2
            acesos = int(round(self.frac(self.nivel) * n))
            pico_i = int(round(self.frac(self.pico) * n)) - 1
            for i in range(n):
                if i < acesos:
                    cr.set_source_rgba(1, 1, 1, 0.92)
                elif i == pico_i and self.pico > -69:
                    cr.set_source_rgba(1, 1, 1, 0.55)
                else:
                    cr.set_source_rgba(1, 1, 1, 0.16)
                caixa_arred(cr, x0 + i * passo - 1.8, y0, 3.6, alt, 1.8)
                cr.fill()

    class Medidor(Nivel):
        """Medidor em segmentos; com limiar, desenha a linha verde do 'só minha voz'."""
        def __init__(self, altura=16):
            super().__init__(altura)

        def desenhar(self, w, cr):
            L, A = w.get_allocated_width(), w.get_allocated_height()
            n, gap = 32, 3
            seg = (L - gap * (n - 1)) / n
            acesos = int(round(self.frac(self.nivel) * n))
            pico_i = int(round(self.frac(self.pico) * n)) - 1
            for i in range(n):
                t = i / (n - 1)
                if i < acesos:
                    v = 0.55 + 0.42 * t
                    cr.set_source_rgb(v, v, v)
                elif i == pico_i and self.pico > -69:
                    cr.set_source_rgb(1, 1, 1)
                else:
                    cr.set_source_rgb(0.15, 0.15, 0.15)
                caixa_arred(cr, i * (seg + gap), 0, seg, A, min(2.0, seg / 2))
                cr.fill()
            if self.limiar is not None:
                x = self.frac(self.limiar) * L
                cr.set_source_rgb(*VERDE)
                caixa_arred(cr, x - 1.5, -2, 3, A + 4, 1.5)
                cr.fill()

    class PontosEQ(Gtk.DrawingArea):
        """Curva do equalizador em matriz de pontos (linha do meio = 0 dB, cada ponto = 3 dB)."""
        def __init__(self):
            super().__init__()
            self.set_size_request(-1, 104)
            self.g = self.a = 0
            self.ligado = False
            self.connect("draw", self.desenhar)

        def definir(self, g, a, ligado):
            self.g, self.a, self.ligado = g, a, ligado
            self.queue_draw()

        def desenhar(self, w, cr):
            L, A = w.get_allocated_width(), w.get_allocated_height()
            linhas = 9
            py = min(12.0, (A - 8) / (linhas - 1))
            y0 = (A - py * (linhas - 1)) / 2
            px = 22.0
            n = max(10, int((L - 10) // px) + 1)
            x0 = (L - (n - 1) * px) / 2
            for i in range(n):
                f = 20 * (1000 ** (i / (n - 1)))
                k = int(round(max(-12.0, min(12.0, resposta_db(f, self.g, self.a))) / 3))
                x = x0 + i * px
                for r in range(linhas):
                    nv = 4 - r
                    aceso = (0 <= nv <= k) or (k <= nv <= 0)
                    if aceso and nv == k:
                        a = 1.0 if self.ligado else 0.5
                    elif aceso:
                        a = 0.6 if self.ligado else 0.3
                    else:
                        a = 0.11
                    cr.set_source_rgba(1, 1, 1, a)
                    cr.arc(x, y0 + r * py, 2.3, 0, 2 * math.pi)
                    cr.fill()

    class Curva(Gtk.DrawingArea):
        """Curva do equalizador com eixos (20 Hz a 20 kHz, −12 a +12 dB)."""
        def __init__(self):
            super().__init__()
            self.set_size_request(-1, 220)
            self.g = self.a = 0
            self.ligado = False
            self.connect("draw", self.desenhar)

        def definir(self, g, a, ligado):
            self.g, self.a, self.ligado = g, a, ligado
            self.queue_draw()

        def desenhar(self, w, cr):
            L, A = w.get_allocated_width(), w.get_allocated_height()
            esq, dir_, topo, base = 38, 8, 10, 22
            pl, pa = L - esq - dir_, A - topo - base
            fx = lambda f: esq + pl * (math.log10(f) - math.log10(20)) / (math.log10(20000) - math.log10(20))
            fy = lambda db: topo + pa * (1 - (db + 13) / 26)
            cr.set_source_rgb(0.065, 0.065, 0.065)
            caixa_arred(cr, 0, 0, L, A, 12)
            cr.fill()
            cr.set_line_width(1)
            cr.select_font_face("Lato", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(10.5)
            for f, txt in ((50, "50"), (100, "100"), (500, "500"), (1000, "1k"), (5000, "5k"), (10000, "10k")):
                x = round(fx(f)) + 0.5
                cr.set_source_rgb(0.14, 0.14, 0.14)
                cr.move_to(x, topo); cr.line_to(x, topo + pa); cr.stroke()
                cr.set_source_rgb(0.45, 0.45, 0.45)
                cr.move_to(x - 7, A - 6); cr.show_text(txt)
            for db in (-12, -6, 0, 6, 12):
                y = round(fy(db)) + 0.5
                v = 0.3 if db == 0 else 0.14
                cr.set_source_rgb(v, v, v)
                cr.move_to(esq, y); cr.line_to(esq + pl, y); cr.stroke()
                cr.set_source_rgb(0.45, 0.45, 0.45)
                cr.move_to(6, y + 4); cr.show_text((f"{db:+d}" if db else " 0").replace("-", "−"))
            pts = []
            for i in range(121):
                f = 20 * (1000 ** (i / 120))
                pts.append((fx(f), fy(max(-13, min(13, resposta_db(f, self.g, self.a))))))
            cr.move_to(pts[0][0], fy(0))
            for x, y in pts:
                cr.line_to(x, y)
            cr.line_to(pts[-1][0], fy(0))
            cr.close_path()
            cr.set_source_rgba(1, 1, 1, 0.08 if self.ligado else 0.03)
            cr.fill()
            for larg, alfa in ((7, 0.10), (2.2, 1.0)):
                cr.set_line_width(larg)
                cr.move_to(*pts[0])
                for x, y in pts[1:]:
                    cr.line_to(x, y)
                v = 0.96 if self.ligado else 0.45
                cr.set_source_rgba(v, v, v, alfa)
                cr.stroke()

    class Painel:
        def __init__(self, captura=None, pagina="principal"):
            self.captura = captura
            self.sink = self.source = self.card = None
            self.eq_existe = False
            self.voz_existe = False
            self.cfg = ler_cfg()
            self.sync = False
            self.toque = {"sink": 0.0, "source": 0.0}
            self.pendente = {}
            self.atualizacao_marcada = False
            self.religar_marcado = False
            self.gravacao = None
            self.ocupado = False
            self.medidor = None
            self.lim_ef = self.cfg["voz_limiar"]

            # controles que aparecem em mais de uma página ficam ligados entre si
            self.rotulos = {}                       # botão → texto dele
            self.dep_conexao = []                   # só funcionam com a saída do fone presente
            self.dep_mic = []                       # só funcionam com o microfone do fone presente
            self.fone_bt = None                     # fone pelo Bluetooth (dict de fone_bluetooth) ou None
            self.bts_bt = []                        # botões Música/Chamada
            self.seletores_bt = []                  # caixas que só aparecem no Bluetooth
            self.aviso_mic_bt = False
            self.navs = {}
            self.vals = {"sink": [], "source": []}  # números de volume/ganho
            self.mudos = {"sink": [], "source": []}
            self.sws_eq, self.sws_voz = [], []
            self.bts_preset = []
            self.medidores = []
            self.leds_voz, self.lbls_aberto = [], []
            self.bts_calibrar, self.res_voz = [], []
            self.bts_mic, self.bts_ouvir, self.res_mic = [], [], []
            self.bts_script = []
            self.volta_mic = 0

            prov = Gtk.CssProvider()
            try:
                prov.load_from_data(CSS)
            except Exception as e:
                print("CSS:", e, file=sys.stderr)
            Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                     Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)

            self.adj = {"sink": Gtk.Adjustment(value=0, lower=0, upper=150, step_increment=1, page_increment=5),
                        "source": Gtk.Adjustment(value=0, lower=0, upper=150, step_increment=1, page_increment=5)}
            for tipo, adj in self.adj.items():
                adj.connect("value-changed", self.ao_mudar_volume, tipo)

            corpo = self.montar()
            titulo = self.montar_titulo()
            if captura:
                self.win = Gtk.OffscreenWindow()
                tudo = Gtk.Box(orientation=V)
                tudo.pack_start(titulo, False, False, 0)
                tudo.pack_start(corpo, True, True, 0)
                tudo.set_size_request(1400, int(os.environ.get("HAVIT_CAPTURA_ALTURA", "900")))
                self.win.add(tudo)
            else:
                self.win = Gtk.Window(title="Fone Havit")
                self.win.set_titlebar(titulo)
                if os.path.exists(ICONE):
                    try:
                        self.win.set_icon(GdkPixbuf.Pixbuf.new_from_file_at_size(ICONE, 64, 64))
                    except Exception:
                        pass
                self.win.add(corpo)
                self.win.connect("destroy", self.sair)
                self.win.set_default_size(1400, 900)
            classe(self.win, "havit")
            self.mostrar_eq()
            self.mostrar_voz()
            self.win.show_all()
            self.ir(pagina)

            self.atualizar()
            if not captura:
                # o vigia permanente (--vigiar) só roda se o usuário quiser; com o painel aberto,
                # o próprio painel religa EQ e "só minha voz" quando o receptor volta
                threading.Thread(target=self.ouvir_eventos, daemon=True).start()
                self.em_fundo(reaplicar, self.depois_reaplicar)
                self.log("Painel aberto. Use Consertar agora se o som falhar ou um lado ficar mudo.")
            else:
                GLib.timeout_add(1500, self.salvar_captura)

        # ---------------- peças reutilizáveis ----------------

        def botao(self, texto, icone=None, *classes):
            b = classe(Gtk.Button(), "botao", *classes)
            cx = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
            if icone:
                cx.pack_start(Icone(icone, 20), False, False, 0)
            lb = Gtk.Label(label=texto)
            cx.pack_start(lb, False, False, 0)
            b.add(cx)
            self.rotulos[b] = lb
            return b

        def texto_botao(self, b, texto):
            self.rotulos[b].set_text(texto)

        def botao_icone(self, icone, dica, acao, tam=22):
            b = classe(Gtk.Button(), "icone-btn")
            b.add(Icone(icone, tam))
            b.set_tooltip_text(dica)
            b.set_valign(Gtk.Align.CENTER)
            b.connect("clicked", lambda _b: acao())
            return b

        def botao_mudo(self, tipo, dica, texto=None):
            icone, icone_mudo = ("volume", "volume-mudo") if tipo == "sink" else ("mic", "mic-mudo")
            b = Gtk.ToggleButton()
            ic = Icone(icone, 24 if texto is None else 18)
            if texto:
                cx = Gtk.Box(spacing=8)
                cx.pack_start(ic, False, False, 0)
                cx.pack_start(Gtk.Label(label=texto), False, False, 0)
                b.add(cx)
                classe(b, "botao", "mudo")
            else:
                b.add(ic)
                classe(b, "icone-btn")
            b.set_valign(Gtk.Align.CENTER)
            b.set_tooltip_text(dica)
            b.connect("toggled", lambda bt: ic.definir(icone_mudo if bt.get_active() else icone))
            lista = self.mudos[tipo]
            if lista:
                lista[0].bind_property("active", b, "active", LIGA)
            else:
                b.connect("toggled", self.ao_mudo, tipo)
                b.connect("toggled", lambda bt: [(v.get_style_context().add_class if bt.get_active()
                                                  else v.get_style_context().remove_class)("apagado")
                                                 for v in self.vals[tipo]])
            lista.append(b)
            return b

        def chave_eq(self):
            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            if self.sws_eq:
                self.sws_eq[0].bind_property("active", sw, "active", LIGA)
            else:
                sw.set_active(self.cfg["ativo"])
                sw.connect("notify::active", self.ao_ligar_eq)
            self.sws_eq.append(sw)
            return sw

        def chave_voz(self):
            sw = Gtk.Switch(valign=Gtk.Align.CENTER)
            if self.sws_voz:
                self.sws_voz[0].bind_property("active", sw, "active", LIGA)
            else:
                sw.set_active(self.cfg["voz"])
                sw.connect("notify::active", self.ao_ligar_voz)
            self.sws_voz.append(sw)
            return sw

        def novo_card(self, *classes, orientacao=V, espaco=14):
            return classe(Gtk.Box(orientation=orientacao, spacing=espaco), "card", *classes)

        def cabecalho(self, icone, titulo, tam=24):
            cab = Gtk.Box(spacing=12)
            if isinstance(icone, Gtk.Widget):
                cab.pack_start(icone, False, False, 0)
            elif icone:
                cab.pack_start(Icone(icone, tam), False, False, 0)
            cab.pack_start(rotulo(titulo, "card-titulo"), True, True, 0)
            return cab

        def numero(self, tipo):
            v = rotulo("—", "numero")
            self.vals[tipo].append(v)
            return v

        def deslizante(self, tipo, marca=False):
            e = Gtk.Scale(orientation=H, adjustment=self.adj[tipo])
            e.set_draw_value(False)
            e.set_hexpand(True)
            e.set_valign(Gtk.Align.CENTER)
            if marca:
                e.add_mark(100, Gtk.PositionType.BOTTOM, None)
            return e

        def botoes_teste_lado(self):
            testes = Gtk.Box(spacing=14, homogeneous=True)
            for txt, arq, lado in (("Testar Esquerdo", "Front_Left.wav", "esquerdo"),
                                   ("Testar Direito", "Front_Right.wav", "direito")):
                b = self.botao(txt, "fone")
                b.set_tooltip_text(f"Toca uma voz só no lado {lado}")
                b.connect("clicked", self.testar_lado, arq, lado)
                testes.pack_start(b, True, True, 0)
            return testes

        def linha_presets(self):
            pr = Gtk.Box(spacing=12, homogeneous=True)
            for nome, gv, av in PRESETS:
                b = classe(Gtk.Button(label=nome.replace(" +", "+")), "preset")
                b.connect("clicked", self.ao_preset, gv, av)
                b.set_tooltip_text(f"Graves {gv:+d} dB, agudos {av:+d} dB")
                pr.pack_start(b, True, True, 0)
                self.bts_preset.append((b, gv, av))
            return pr

        def botao_calibrar(self, *classes):
            b = self.botao("Calibrar", "alvo", *classes)
            b.set_tooltip_text("Grava 4 s de silêncio e 4 s da sua voz para achar o limiar")
            b.set_valign(Gtk.Align.CENTER)
            b.connect("clicked", self.calibrar_clique)
            self.bts_calibrar.append(b)
            return b

        def seletor_bt(self):
            """Botões Música / Chamada do Bluetooth (só aparecem quando o fone está no Bluetooth)."""
            cx = Gtk.Box(spacing=8)
            cx.pack_start(rotulo("Bluetooth:", "dica"), False, False, 0)
            for modo, texto, dica in (("musica", "Música", "Som estéreo de alta qualidade; o microfone fica desligado"),
                                      ("chamada", "Chamada", "Liga o microfone; o som fica mono e mais simples")):
                b = classe(Gtk.Button(label=texto), "preset", "segmento")
                b.set_tooltip_text(dica)
                b.connect("clicked", lambda _b, m=modo: self.trocar_modo_bt(m))
                cx.pack_start(b, False, False, 0)
                self.bts_bt.append((b, modo))
            cx.set_no_show_all(True)
            self.seletores_bt.append(cx)
            return cx

        def titulo_pagina(self, titulo, sub):
            cx = Gtk.Box(orientation=V, spacing=4)
            cx.pack_start(rotulo(titulo, "titulo-pagina"), False, False, 0)
            cx.pack_start(rotulo(sub, "dica-media"), False, False, 0)
            cx.set_margin_top(6)
            cx.set_margin_bottom(4)
            return cx

        # ---------------- janela ----------------

        def montar_titulo(self):
            cx = Gtk.Box()
            esq = classe(Gtk.HeaderBar(), "hb-lateral")
            esq.set_show_close_button(False)
            esq.set_size_request(LARGURA_LATERAL, -1)
            dir_ = classe(Gtk.HeaderBar(), "hb-principal")
            dir_.set_show_close_button(True)
            dir_.set_decoration_layout(":minimize,maximize,close")
            cx.pack_start(esq, False, False, 0)
            cx.pack_start(dir_, True, True, 0)
            return cx

        def montar(self):
            raiz = Gtk.Box()
            raiz.pack_start(self.montar_lateral(), False, False, 0)
            self.paginas = classe(Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                            transition_duration=160), "conteudo")
            self.paginas.set_vhomogeneous(False)
            for nome, montar in (("principal", self.pagina_principal), ("audio", self.pagina_audio),
                                 ("equalizador", self.pagina_eq), ("config", self.pagina_config),
                                 ("sobre", self.pagina_sobre)):
                sw = Gtk.ScrolledWindow()
                sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                sw.set_propagate_natural_height(True)
                pg = montar()
                pg.set_margin_start(28)
                pg.set_margin_end(28)
                pg.set_margin_top(4)
                pg.set_margin_bottom(28)
                sw.add(pg)
                self.paginas.add_named(sw, nome)
            raiz.pack_start(self.paginas, True, True, 0)
            return raiz

        def montar_lateral(self):
            lat = classe(Gtk.Box(orientation=V), "lateral")
            lat.set_size_request(LARGURA_LATERAL, -1)
            img = ImagemFone(132, 104, ax=0.0)
            img.set_halign(Gtk.Align.START)
            img.set_margin_start(30)
            lat.pack_start(img, False, False, 0)
            marca = Gtk.Label(xalign=0)
            marca.set_markup('<span letter_spacing="4200">HAVIT</span>')
            classe(marca, "marca")
            marca.set_margin_start(34)
            marca.set_margin_top(10)
            lat.pack_start(marca, False, False, 0)
            sub = rotulo("H2002D PRO", "marca-sub")
            sub.set_margin_start(35)
            lat.pack_start(sub, False, False, 0)
            nav = Gtk.Box(orientation=V, spacing=6)
            nav.set_margin_start(18)
            nav.set_margin_end(18)
            nav.set_margin_top(38)
            for nome, icone, texto in (("principal", "inicio", "Principal"), ("audio", "audio", "Áudio"),
                                       ("equalizador", "equalizador", "Equalizador"),
                                       ("config", "config", "Configurações"), ("sobre", "sobre", "Sobre")):
                b = classe(Gtk.Button(), "nav")
                cx = Gtk.Box(spacing=16)
                cx.pack_start(Icone(icone, 22), False, False, 0)
                cx.pack_start(Gtk.Label(label=texto, xalign=0), False, False, 0)
                b.add(cx)
                b.connect("clicked", lambda _b, n=nome: self.ir(n))
                nav.pack_start(b, False, False, 0)
                self.navs[nome] = b
            lat.pack_start(nav, False, False, 0)
            deco = ImagemFone(LARGURA_LATERAL, 200, alfa=0.55, zoom=1.15, ax=0.25, ay=0.0, esmaecer=True)
            deco.set_valign(Gtk.Align.END)
            lat.pack_end(deco, False, False, 0)
            return lat

        def ir(self, nome):
            self.paginas.set_visible_child_name(nome)
            for n, b in self.navs.items():
                (b.get_style_context().add_class if n == nome else b.get_style_context().remove_class)("ativo")

        # ---------------- página principal ----------------

        def pagina_principal(self):
            pg = Gtk.Box(orientation=V, spacing=18)
            pg.pack_start(self.card_topo(), False, False, 0)
            grade = Gtk.Grid(column_spacing=20, row_spacing=18)
            vol, eq = self.card_volume(), self.card_eq_resumo()
            vol.set_size_request(440, -1)
            eq.set_size_request(580, -1)
            vol.set_hexpand(True)
            eq.set_hexpand(True)
            grade.attach(vol, 0, 0, 1, 1)
            grade.attach(eq, 1, 0, 1, 1)
            grade.attach(self.card_mic_resumo(), 0, 1, 1, 1)
            grade.attach(self.coluna_voz(), 1, 1, 1, 1)
            pg.pack_start(grade, False, False, 0)
            base = Gtk.Box(spacing=20)
            base.pack_start(self.card_ferramentas_resumo(), True, True, 0)
            base.pack_start(self.coluna_rodape(), False, False, 0)
            pg.pack_start(base, False, False, 0)
            return pg

        def card_topo(self):
            ov = Gtk.Overlay()
            c = self.novo_card("card-topo", orientacao=H, espaco=6)
            c.pack_start(ImagemFone(240, 150, zoom=1.06, ay=0.0), False, False, 0)
            tx = Gtk.Box(orientation=V, spacing=6, valign=Gtk.Align.CENTER)
            tx.set_margin_start(22)
            tx.set_margin_end(190)
            tx.pack_start(rotulo("Havit H2002D PRO", "titulo-grande"), False, False, 0)
            linha_tipo = Gtk.Box(spacing=12)
            self.lbl_tipo = rotulo("Headset Gaming · receptor 2.4G", "subtitulo")
            linha_tipo.pack_start(self.lbl_tipo, False, False, 0)
            self.caixa_bateria = Gtk.Box(spacing=8)
            self.caixa_bateria.set_margin_start(10)
            self.icone_bateria = Bateria()
            self.caixa_bateria.pack_start(self.icone_bateria, False, False, 0)
            self.lbl_bateria = rotulo("", "bateria-texto")
            self.caixa_bateria.pack_start(self.lbl_bateria, False, False, 0)
            self.caixa_bateria.set_tooltip_text("Bateria do fone (o Bluetooth informa; pelo receptor USB não dá)")
            self.caixa_bateria.set_no_show_all(True)
            linha_tipo.pack_start(self.caixa_bateria, False, False, 0)
            tx.pack_start(linha_tipo, False, False, 0)
            self.lbl_rota = rotulo("procurando o fone…", "info", ellipsize=Pango.EllipsizeMode.END)
            self.lbl_rota.set_margin_top(10)
            tx.pack_start(self.lbl_rota, False, False, 0)
            sel = self.seletor_bt()
            sel.set_margin_top(4)
            tx.pack_start(sel, False, False, 0)
            c.pack_start(tx, True, True, 0)
            ov.add(c)
            pil = classe(Gtk.Box(spacing=8), "pilula")
            self.led = Led(10)
            pil.pack_start(self.led, False, False, 0)
            self.estado = rotulo("Procurando…", "pilula-texto")
            pil.pack_start(self.estado, False, False, 0)
            pil.set_halign(Gtk.Align.END)
            pil.set_valign(Gtk.Align.START)
            pil.set_margin_top(18)
            pil.set_margin_end(20)
            ov.add_overlay(pil)
            chev = classe(Gtk.Button(), "redondo")
            chev.add(Icone("seta", 18))
            chev.set_tooltip_text("Detalhes do receptor e do painel")
            chev.connect("clicked", lambda _b: self.ir("sobre"))
            chev.set_halign(Gtk.Align.END)
            chev.set_valign(Gtk.Align.CENTER)
            chev.set_margin_end(26)
            chev.set_margin_top(34)
            ov.add_overlay(chev)
            return ov

        def card_volume(self):
            c = self.novo_card(espaco=12)
            cab = self.cabecalho(self.botao_mudo("sink", "Silenciar o fone"), "Volume")
            cab.pack_end(self.botao_icone("ajustes", "Mais ajustes de áudio", lambda: self.ir("audio")), False, False, 0)
            c.pack_start(cab, False, False, 0)
            c.pack_start(self.numero("sink"), False, False, 0)
            c.pack_start(self.deslizante("sink"), False, False, 0)
            t = self.botoes_teste_lado()
            t.set_margin_top(4)
            c.pack_start(t, False, False, 0)
            self.dep_conexao.append(c)
            return c

        def card_eq_resumo(self):
            c = self.novo_card(espaco=12)
            cab = self.cabecalho("barras", "Equalizador")
            cab.pack_end(self.chave_eq(), False, False, 0)
            c.pack_start(cab, False, False, 0)
            self.pontos = PontosEQ()
            self.pontos.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            self.pontos.connect("button-press-event", lambda *_a: self.ir("equalizador"))
            c.pack_start(self.pontos, True, True, 0)
            c.pack_start(self.linha_presets(), False, False, 0)
            return c

        def card_mic_resumo(self):
            c = self.novo_card(espaco=12)
            cab = self.cabecalho(self.botao_mudo("source", "Silenciar o microfone"), "Microfone")
            cab.pack_end(self.botao_icone("ajustes", "Mais ajustes do microfone", lambda: self.ir("audio")),
                         False, False, 0)
            c.pack_start(cab, False, False, 0)
            linha = Gtk.Box(spacing=28)
            linha.pack_start(self.numero("source"), False, False, 0)
            linha.pack_start(self.deslizante("source"), True, True, 0)
            c.pack_start(linha, False, False, 0)
            self.pilha_mic = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=200)
            tr = Tracos()
            self.medidores.append(tr)
            self.pilha_mic.add_named(tr, "medidor")
            self.msg_mic_p = rotulo("", "dica-clara", xalign=0.5, ellipsize=Pango.EllipsizeMode.END)
            self.pilha_mic.add_named(self.msg_mic_p, "msg")
            c.pack_start(self.pilha_mic, False, False, 0)
            bts = Gtk.Box(spacing=12)
            bm = self.botao("Testar Microfone", "mic")
            bm.set_tooltip_text("Grava 5 s do jeito que os outros vão te ouvir")
            bm.connect("clicked", self.testar_mic)
            self.bts_mic.append(bm)
            bts.pack_start(bm, True, True, 0)
            bo = classe(Gtk.Button(), "botao", "quadrado")
            bo.add(Icone("play", 18))
            bo.set_tooltip_text("Ouvir a gravação do teste")
            bo.connect("clicked", self.ouvir_gravacao)
            bo.set_no_show_all(True)
            self.bt_ouvir_p = bo
            self.bts_ouvir.append(bo)
            bts.pack_start(bo, False, False, 0)
            c.pack_start(bts, False, False, 0)
            self.dep_mic.append(c)
            return c

        def coluna_voz(self):
            col = Gtk.Box(orientation=V, spacing=18, homogeneous=True)
            c1 = self.novo_card("card-linha", orientacao=H, espaco=18)
            c1.pack_start(Icone("voz", 30), False, False, 0)
            tx = Gtk.Box(orientation=V, spacing=5, valign=Gtk.Align.CENTER)
            tx.pack_start(rotulo("Só minha voz", "card-titulo"), False, False, 0)
            self.sub_voz = rotulo("", "dica", ellipsize=Pango.EllipsizeMode.END)
            tx.pack_start(self.sub_voz, False, False, 0)
            c1.pack_start(tx, True, True, 0)
            c1.pack_end(self.chave_voz(), False, False, 0)
            col.pack_start(c1, True, True, 0)

            c2 = self.novo_card("card-linha", orientacao=H, espaco=18)
            c2.pack_start(Icone("alvo", 30), False, False, 0)
            tx = Gtk.Box(orientation=V, spacing=5, valign=Gtk.Align.CENTER)
            topo = Gtk.Box(spacing=10)
            self.lbl_limiar_p = rotulo("Limiar", "card-titulo")
            topo.pack_start(self.lbl_limiar_p, False, False, 0)
            led = Led(8)
            self.leds_voz.append(led)
            topo.pack_start(led, False, False, 0)
            ab = rotulo("—", "dica")
            self.lbls_aberto.append((ab, "voz passando", "fechado"))
            topo.pack_start(ab, False, False, 0)
            tx.pack_start(topo, False, False, 0)
            self.res_voz_p = rotulo("Calibre uma vez com a haste perto da boca.", "dica",
                                    ellipsize=Pango.EllipsizeMode.END)
            self.res_voz.append(self.res_voz_p)
            tx.pack_start(self.res_voz_p, False, False, 0)
            c2.pack_start(tx, True, True, 0)
            c2.pack_end(self.botao_calibrar("pequeno"), False, False, 0)
            col.pack_start(c2, True, True, 0)
            self.dep_mic += [c1, c2]
            return col

        def card_ferramentas_resumo(self):
            c = self.novo_card(espaco=16)
            c.pack_start(self.cabecalho("ferramenta", "Ferramentas"), False, False, 0)
            linha = Gtk.Box(spacing=14)
            for texto, icone, acao, destaque in (
                    ("Consertar agora", "raio", lambda b: self.rodar_script(b, None, "Consertar"), True),
                    ("Status", "pulso", lambda b: self.rodar_script(b, "--status", "Status"), False),
                    ("Desfazer ajustes", "desfazer", self.desfazer, False),
                    ("Registro", "lista", lambda b: self.ir("config"), False)):
                t = classe(Gtk.Button(), "tile")
                if destaque:
                    classe(t, "destaque")
                cx = Gtk.Box(orientation=V, spacing=8)
                cx.pack_start(Icone(icone, 24), False, False, 0)
                cx.pack_start(Gtk.Label(label=texto), False, False, 0)
                t.add(cx)
                t.connect("clicked", acao)
                linha.pack_start(t, False, False, 0)
                if texto != "Registro":
                    self.bts_script.append(t)
            self.bts_script[0].set_tooltip_text("Corrige lado direito mudo, microfone mudo e saída/entrada erradas")
            self.bts_script[1].set_tooltip_text("Mostra como estão o receptor, o mixer e as saídas")
            self.bts_script[2].set_tooltip_text("Remove a correção do volume de hardware do receptor")
            info = Gtk.Box(orientation=V, spacing=6, valign=Gtk.Align.CENTER)
            info.set_margin_start(12)
            info.pack_start(rotulo("Último registro", "rotulo-forte"), False, False, 0)
            self.ultimo = rotulo("—", "dica", wrap=True, lines=2, ellipsize=Pango.EllipsizeMode.END)
            self.ultimo.set_max_width_chars(30)
            info.pack_start(self.ultimo, False, False, 0)
            linha.pack_start(info, True, True, 0)
            c.pack_start(linha, False, False, 0)
            return c

        def coluna_rodape(self):
            col = Gtk.Box(orientation=V, spacing=16, homogeneous=True)
            col.set_size_request(290, -1)
            r = classe(Gtk.Button(), "card-botao")
            cx = Gtk.Box(spacing=16)
            cx.pack_start(Icone("restaurar", 26), False, False, 0)
            cx.pack_start(rotulo("Restaurar padrões", "card-titulo"), True, True, 0)
            cx.pack_end(Icone("seta", 18), False, False, 0)
            r.add(cx)
            r.set_tooltip_text("Desliga o equalizador e o 'só minha voz' e zera os ajustes do painel")
            r.connect("clicked", self.restaurar_padroes)
            col.pack_start(r, True, True, 0)
            v = classe(Gtk.Button(), "card-botao", "versao")
            cx = Gtk.Box(spacing=16)
            cx.pack_start(Icone("sobre", 24), False, False, 0)
            cx.pack_start(rotulo(f"Versão {VERSAO}", "dica-media"), True, True, 0)
            v.add(cx)
            v.connect("clicked", lambda _b: self.ir("sobre"))
            col.pack_start(v, True, True, 0)
            return col

        # ---------------- página Áudio ----------------

        def pagina_audio(self):
            pg = Gtk.Box(orientation=V, spacing=20)
            pg.pack_start(self.titulo_pagina("Áudio", "Volume do fone, ganho do microfone e o filtro “só minha voz”."),
                          False, False, 0)
            pg.pack_start(self.seletor_bt(), False, False, 0)
            cols = Gtk.Box(spacing=20, homogeneous=True)
            esq = Gtk.Box(orientation=V, spacing=20)
            esq.pack_start(self.card_fone_completo(), False, False, 0)
            esq.pack_start(self.card_mic_completo(), False, False, 0)
            cols.pack_start(esq, True, True, 0)
            dir_ = Gtk.Box(orientation=V, spacing=20)
            dir_.pack_start(self.card_voz_completo(), False, False, 0)
            cols.pack_start(dir_, True, True, 0)
            pg.pack_start(cols, False, False, 0)
            return pg

        def linha_volume(self, tipo):
            cx = Gtk.Box(spacing=12)
            adj = self.adj[tipo]
            menos = classe(Gtk.Button(), "mais-menos")
            menos.add(Icone("menos", 16))
            mais = classe(Gtk.Button(), "mais-menos")
            mais.add(Icone("mais", 16))
            menos.connect("clicked", lambda b: adj.set_value(max(0, adj.get_value() - 5)))
            mais.connect("clicked", lambda b: adj.set_value(min(150, adj.get_value() + 5)))
            menos.set_tooltip_text("Abaixar 5%")
            mais.set_tooltip_text("Aumentar 5%")
            for w in (menos, mais):
                w.set_valign(Gtk.Align.CENTER)
            cx.pack_start(menos, False, False, 0)
            cx.pack_start(self.deslizante(tipo, marca=True), True, True, 0)
            cx.pack_start(mais, False, False, 0)
            return cx

        def valor_com_unidade(self, tipo, unidade):
            cx = Gtk.Box(spacing=10)
            cx.pack_start(self.numero(tipo), False, False, 0)
            cx.pack_start(rotulo(unidade, "unidade", yalign=0.72), False, False, 0)
            return cx

        def card_fone_completo(self):
            c = self.novo_card(espaco=12)
            cab = self.cabecalho("fone", "Fone")
            cab.pack_end(self.botao_mudo("sink", "Silenciar o fone", texto="Mudo"), False, False, 0)
            c.pack_start(cab, False, False, 0)
            c.pack_start(self.valor_com_unidade("sink", "volume do fone"), False, False, 0)
            c.pack_start(self.linha_volume("sink"), False, False, 0)
            c.pack_start(self.botoes_teste_lado(), False, False, 4)
            self.dep_conexao.append(c)
            return c

        def card_mic_completo(self):
            c = self.novo_card(espaco=12)
            cab = self.cabecalho("mic", "Microfone")
            cab.pack_end(self.botao_mudo("source", "Silenciar o microfone", texto="Mudo"), False, False, 0)
            c.pack_start(cab, False, False, 0)
            c.pack_start(self.valor_com_unidade("source", "ganho do microfone"), False, False, 0)
            c.pack_start(self.linha_volume("source"), False, False, 0)
            nv = Gtk.Box(spacing=12)
            med = Medidor()
            med.set_valign(Gtk.Align.CENTER)
            self.medidores.append(med)
            nv.pack_start(med, True, True, 0)
            self.val_nivel = rotulo("— dB", "valor-pequeno", xalign=1)
            nv.pack_start(self.val_nivel, False, False, 0)
            c.pack_start(nv, False, False, 2)
            bts = Gtk.Box(spacing=12, homogeneous=True)
            bm = self.botao("Testar microfone", "mic")
            bm.connect("clicked", self.testar_mic)
            self.bts_mic.append(bm)
            bo = self.botao("Ouvir gravação", "play")
            bo.set_sensitive(False)
            bo.connect("clicked", self.ouvir_gravacao)
            self.bts_ouvir.append(bo)
            bts.pack_start(bm, True, True, 0)
            bts.pack_start(bo, True, True, 0)
            c.pack_start(bts, False, False, 0)
            res = rotulo("Fale e veja a barra acender. O teste grava 5 s do jeito que os outros vão te ouvir.",
                         "dica", wrap=True)
            res.set_max_width_chars(48)
            self.res_mic.append(res)
            c.pack_start(res, False, False, 0)
            self.dep_mic.append(c)
            return c

        def card_voz_completo(self):
            c = self.novo_card(espaco=12)
            cab = self.cabecalho("voz", "Só minha voz")
            cab.pack_end(self.chave_voz(), False, False, 0)
            self.lbl_voz = rotulo("DESLIGADO", "estado")
            cab.pack_end(self.lbl_voz, False, False, 4)
            c.pack_start(cab, False, False, 0)
            d = rotulo("Fecha o microfone quando você não está falando: as conversas da sala não passam. "
                       "Calibre uma vez com a haste perto da boca.", "dica", wrap=True)
            d.set_max_width_chars(52)
            c.pack_start(d, False, False, 0)
            nv = Gtk.Box(spacing=10)
            self.medidor_voz = Medidor(altura=20)
            self.medidor_voz.set_valign(Gtk.Align.CENTER)
            self.medidores.append(self.medidor_voz)
            nv.pack_start(self.medidor_voz, True, True, 0)
            led = Led(9)
            self.leds_voz.append(led)
            nv.pack_start(led, False, False, 0)
            ab = rotulo("FECHADO", "estado")
            ab.set_width_chars(8)
            self.lbls_aberto.append((ab, "VOZ", "FECHADO"))
            nv.pack_start(ab, False, False, 0)
            c.pack_start(nv, False, False, 6)
            topo = Gtk.Box(spacing=8)
            l = Gtk.Label(xalign=0)
            l.set_markup('<span weight="bold">Limiar</span>  '
                         '<span foreground="#7a7a7a" size="small">linha verde no medidor</span>')
            classe(l, "dica-clara")
            topo.pack_start(l, True, True, 0)
            self.val_limiar = rotulo("−45 dB", "valor-pequeno", xalign=1)
            topo.pack_end(self.val_limiar, False, False, 0)
            c.pack_start(topo, False, False, 0)
            self.esc_limiar = Gtk.Scale.new_with_range(H, -80, -5, 1)
            classe(self.esc_limiar, "sem-preenchimento")
            self.esc_limiar.set_draw_value(False)
            self.esc_limiar.set_has_origin(False)
            self.esc_limiar.set_value(self.lim_ef)
            self.esc_limiar.connect("value-changed", self.ao_mudar_limiar)
            c.pack_start(self.esc_limiar, False, False, 0)
            bts = Gtk.Box(spacing=14)
            bts.pack_start(self.botao_calibrar(), False, False, 0)
            res = rotulo("Mais à esquerda = pega vozes mais baixas; mais à direita = corta mais.", "dica", wrap=True)
            res.set_max_width_chars(40)
            self.res_voz.append(res)
            bts.pack_start(res, True, True, 0)
            c.pack_start(bts, False, False, 0)
            self.dep_mic.append(c)
            return c

        # ---------------- página Equalizador ----------------

        def linha_eq(self, nome, freq, chave):
            cx = Gtk.Box(orientation=V, spacing=2)
            topo = Gtk.Box(spacing=8)
            l = Gtk.Label(xalign=0)
            l.set_markup(f'<span weight="bold">{nome}</span>  <span foreground="#7a7a7a" size="small">{freq}</span>')
            classe(l, "dica-clara")
            topo.pack_start(l, True, True, 0)
            val = rotulo("0 dB", "valor-pequeno", xalign=1)
            topo.pack_end(val, False, False, 0)
            cx.pack_start(topo, False, False, 0)
            e = Gtk.Scale.new_with_range(H, -LIMITE, LIMITE, 1)
            classe(e, "sem-preenchimento")
            e.set_draw_value(False)
            e.set_has_origin(False)
            for v in (-12, -6, 0, 6, 12):
                e.add_mark(v, Gtk.PositionType.BOTTOM, None)
            e.set_value(self.cfg[chave])
            e.connect("value-changed", self.ao_mudar_eq, chave)
            cx.pack_start(e, False, False, 0)
            return cx, e, val

        def pagina_eq(self):
            pg = Gtk.Box(orientation=V, spacing=20)
            pg.pack_start(self.titulo_pagina("Equalizador", "Graves (110 Hz) e agudos (7 kHz) para tudo que toca "
                                                            "no fone. Fica salvo."), False, False, 0)
            c = self.novo_card(espaco=18)
            cab = self.cabecalho("barras", "Curva de resposta")
            cab.pack_end(self.chave_eq(), False, False, 0)
            self.lbl_eq = rotulo("DESLIGADO", "estado")
            cab.pack_end(self.lbl_eq, False, False, 4)
            c.pack_start(cab, False, False, 0)
            self.curva = Curva()
            c.pack_start(self.curva, False, False, 0)
            dois = Gtk.Box(spacing=28, homogeneous=True)
            g, self.esc_graves, self.val_graves = self.linha_eq("Graves", "110 Hz", "graves")
            a, self.esc_agudos, self.val_agudos = self.linha_eq("Agudos", "7 kHz", "agudos")
            dois.pack_start(g, True, True, 0)
            dois.pack_start(a, True, True, 0)
            c.pack_start(dois, False, False, 0)
            c.pack_start(self.linha_presets(), False, False, 0)
            dica = rotulo("Vale para tudo que toca no fone e fica salvo. Para não distorcer, o volume geral baixa um "
                          "pouco quando você reforça graves ou agudos.", "dica", wrap=True)
            c.pack_start(dica, False, False, 0)
            pg.pack_start(c, False, False, 0)
            return pg

        # ---------------- página Configurações ----------------

        def pagina_config(self):
            pg = Gtk.Box(orientation=V, spacing=20)
            pg.pack_start(self.titulo_pagina("Configurações", "Correção do receptor e registro do que o painel fez."),
                          False, False, 0)
            c = self.novo_card(espaco=14)
            c.pack_start(self.cabecalho("ferramenta", "Ferramentas"), False, False, 0)
            c.pack_start(rotulo("Corrige lado direito mudo, microfone mudo e saída/entrada erradas. "
                                "“Desfazer ajustes” tira a correção do mixer do receptor.", "dica", wrap=True),
                         False, False, 0)
            linha = Gtk.Box(spacing=12)
            st = self.botao("Status", "pulso")
            st.connect("clicked", self.rodar_script, "--status", "Status")
            de = self.botao("Desfazer ajustes", "desfazer")
            de.connect("clicked", self.desfazer)
            co = self.botao("Consertar agora", "raio", "principal")
            co.connect("clicked", self.rodar_script, None, "Consertar")
            self.bts_script += [st, de, co]
            linha.pack_start(st, False, False, 0)
            linha.pack_start(de, False, False, 0)
            linha.pack_end(co, False, False, 0)
            c.pack_start(linha, False, False, 4)
            pg.pack_start(c, False, False, 0)

            r = self.novo_card(espaco=14)
            r.pack_start(self.cabecalho("lista", "Registro"), False, False, 0)
            self.buf = Gtk.TextBuffer()
            tv = Gtk.TextView(buffer=self.buf, editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            tv.set_left_margin(12); tv.set_right_margin(12); tv.set_top_margin(10); tv.set_bottom_margin(10)
            self.tv = tv
            sw = classe(Gtk.ScrolledWindow(), "registro")
            sw.set_min_content_height(320)
            sw.add(tv)
            r.pack_start(sw, True, True, 0)
            pg.pack_start(r, True, True, 0)
            return pg

        # ---------------- página Sobre ----------------

        def pagina_sobre(self):
            pg = Gtk.Box(orientation=V, spacing=20)
            pg.pack_start(self.titulo_pagina("Sobre", "O painel, o receptor e onde cada coisa fica."), False, False, 0)
            c = self.novo_card(orientacao=H, espaco=24)
            c.pack_start(ImagemFone(280, 250), False, False, 0)
            tx = Gtk.Box(orientation=V, spacing=6, valign=Gtk.Align.CENTER)
            tx.pack_start(rotulo("Havit H2002D PRO", "titulo-grande"), False, False, 0)
            tx.pack_start(rotulo(f"Painel do fone para Ubuntu · versão {VERSAO}", "subtitulo"), False, False, 0)
            grade = Gtk.Grid(column_spacing=22, row_spacing=8)
            grade.set_margin_top(18)
            self.sobre = {}
            linhas = (("usb", "Receptor USB"), ("bt", "Bluetooth"), ("placa", "Placa"), ("saida", "Saída do fone"),
                      ("entrada", "Microfone"), ("padrao", "Padrões do sistema"))
            for i, (k, nome) in enumerate(linhas):
                grade.attach(rotulo(nome, "chave"), 0, i, 1, 1)
                v = rotulo("—", "valor-info", selectable=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
                v.set_hexpand(True)
                grade.attach(v, 1, i, 1, 1)
                self.sobre[k] = v
            fixos = (("Equalizador", f"graves {F_GRAVES:.0f} Hz e agudos {F_AGUDOS / 1000:.0f} kHz (filtro próprio "
                                     f"em {LADSPA_DIR})"),
                     ("Configuração", CFG))
            for j, (nome, val) in enumerate(fixos, start=len(linhas)):
                grade.attach(rotulo(nome, "chave"), 0, j, 1, 1)
                grade.attach(rotulo(val, "valor-info", selectable=True, ellipsize=Pango.EllipsizeMode.MIDDLE),
                             1, j, 1, 1)
            tx.pack_start(grade, False, False, 0)
            c.pack_start(tx, True, True, 0)
            pg.pack_start(c, False, False, 0)
            cl = self.novo_card(espaco=12)
            cl.pack_start(self.cabecalho("lista", "Linha de comando"), False, False, 0)
            uso = "\n".join(l[2:] for l in __doc__.strip().split("\n\n")[1].splitlines())
            cl.pack_start(rotulo(uso, "mono", selectable=True), False, False, 0)
            pg.pack_start(cl, False, False, 0)
            return pg

        # ---------------- estado ----------------

        def atualizar(self):
            self.atualizacao_marcada = False
            usb = receptor_usb()
            self.card, self.sink, self.source, self.eq_existe = dispositivos()
            self.voz_existe = bool(self.source) and voz_saudavel(self.source)
            self.fone_bt = fone_bluetooth() if (self.card or "").startswith("bluez_card.") else None
            modo = modo_bt(self.fone_bt["perfil"]) if self.fone_bt else None
            ok = bool(self.sink)
            ds = dso = ""
            self.lbl_tipo.set_text("Headset Gaming · " + ("Bluetooth" if self.fone_bt else "receptor 2.4G"))
            bateria = self.fone_bt["bateria"] if self.fone_bt else None
            if bateria is None:
                self.caixa_bateria.hide()
            else:
                self.icone_bateria.definir(bateria)
                self.lbl_bateria.set_text(f"{bateria}%")
                self.caixa_bateria.set_no_show_all(False)
                self.caixa_bateria.show_all()
            if ok:
                self.led.definir("ok")
                self.estado.set_text("Conectado")
                _, ds = pactl("get-default-sink")
                _, dso = pactl("get-default-source")
                ds, dso = ds.strip(), dso.strip()
                saida = ("fone com equalizador" if ds == EQ_SINK else
                         ("fone" if ds == self.sink else "outra saída, não o fone"))
                if not self.source and modo == "musica":
                    entrada = "sem microfone no modo Música"
                else:
                    entrada = ("só minha voz" if dso == VOZ_FONTE else
                               ("microfone do fone" if dso == self.source else "outra entrada, não o fone"))
                self.lbl_rota.set_text(f"Saída: {saida}   ·   Entrada: {entrada}")
            elif self.fone_bt:
                self.led.definir("aviso")
                self.estado.set_text("Bluetooth sem som")
                self.lbl_rota.set_text("O fone está no Bluetooth, mas sem saída de som — escolha Música abaixo.")
            elif usb:
                self.led.definir("aviso")
                self.estado.set_text("Receptor sem som")
                self.lbl_rota.set_text("O receptor está no USB, mas o som dele não apareceu — use Consertar agora.")
            else:
                self.led.definir("off")
                self.estado.set_text("Desconectado")
                self.lbl_rota.set_text("Conecte o receptor USB (ou o Bluetooth) e ligue o fone.")
            self.lbl_rota.set_tooltip_text(self.lbl_rota.get_text())
            for w in self.dep_conexao:
                w.set_sensitive(ok)
            for w in self.dep_mic:
                w.set_sensitive(bool(self.source))
            self.mostrar_bt(modo)
            self.sync = True
            try:
                agora = time.monotonic()
                for tipo, nome in (("sink", self.sink), ("source", self.source)):
                    if not nome:
                        continue
                    v, m = ler_volume(tipo, nome)
                    if agora - self.toque[tipo] > 0.8:
                        self.adj[tipo].set_value(v)
                    for l in self.vals[tipo]:
                        l.set_text(f"{int(self.adj[tipo].get_value())}%")
                    self.mudos[tipo][0].set_active(m)
                if self.source:
                    self.lim_ef = limiar_efetivo(self.cfg, self.source)
                    self.esc_limiar.set_value(self.lim_ef)
            finally:
                self.sync = False
            self.sobre["usb"].set_text(f"{USB_ID} · " + ("conectado" if usb else "não encontrado"))
            if self.fone_bt:
                nome_modo = {"musica": "Música", "chamada": "Chamada"}.get(modo, "sem som")
                bat = "" if self.fone_bt["bateria"] is None else f" · bateria {self.fone_bt['bateria']}%"
                self.sobre["bt"].set_text(f"{self.fone_bt['nome']} · {self.fone_bt['endereco']} · modo {nome_modo}{bat}")
            else:
                self.sobre["bt"].set_text("não conectado")
            self.sobre["placa"].set_text(self.card or "—")
            self.sobre["saida"].set_text(self.sink or "—")
            self.sobre["entrada"].set_text(self.source or "—")
            self.sobre["padrao"].set_text(f"saída {ds or '—'} · entrada {dso or '—'}")
            self.mostrar_eq()
            self.mostrar_voz()
            if not self.captura:
                self.ajustar_medidor()
            return False

        def mostrar_bt(self, modo):
            """Seletor Música/Chamada e o aviso de microfone quando o fone está no Bluetooth."""
            for cx in self.seletores_bt:
                if self.fone_bt:
                    cx.set_no_show_all(False)
                    cx.show_all()
                else:
                    cx.hide()
            for b, m in self.bts_bt:
                ctx = b.get_style_context()
                (ctx.add_class if m == modo else ctx.remove_class)("ativo")
                b.set_sensitive(not self.ocupado)
            sem_mic_bt = bool(self.fone_bt) and not self.source
            if sem_mic_bt and not self.aviso_mic_bt:
                self.aviso_mic_bt = True
                self.msg_mic("<b>No Bluetooth, o microfone só funciona no modo Chamada.</b>\n"
                             "Troque para Chamada no topo da tela Principal ou da página Áudio.")
            elif not sem_mic_bt and self.aviso_mic_bt:
                self.aviso_mic_bt = False
                self.pilha_mic.set_visible_child_name("medidor")
                for l in self.res_mic:
                    l.set_text("Fale e veja a barra acender. O teste grava 5 s do jeito que os outros vão te ouvir.")

        def trocar_modo_bt(self, modo):
            if self.ocupado or not self.fone_bt:
                return
            self.ocupado = True
            for b, _m in self.bts_bt:
                b.set_sensitive(False)
            self.log("Trocando o Bluetooth para o modo " + ("Chamada…" if modo == "chamada" else "Música…"))

            def fim(r):
                self.ocupado = False
                self.depois_modulo(r)
            self.em_fundo(lambda: bt_trocar_modo(modo), fim)

        def marcar_atualizacao(self):
            if not self.atualizacao_marcada:
                self.atualizacao_marcada = True
                GLib.timeout_add(250, self.atualizar)
            return False

        def depois_reaplicar(self, msgs):
            if isinstance(msgs, list):
                for m in msgs:
                    self.log("✔ " + m)
            self.marcar_atualizacao()

        def religar_se_preciso(self):
            self.religar_marcado = False
            self.em_fundo(reaplicar, self.depois_reaplicar)
            return False

        def marcar_religar(self):
            if not self.religar_marcado:
                self.religar_marcado = True
                GLib.timeout_add(1000, self.religar_se_preciso)
            return False

        def ouvir_eventos(self):
            while True:
                try:
                    p = subprocess.Popen(["pactl", "subscribe"], env=ENV, stdout=subprocess.PIPE, text=True)
                    for linha in p.stdout:
                        if ("on sink #" in linha or "on source #" in linha or "on card" in linha
                                or "on server" in linha):
                            GLib.idle_add(self.marcar_atualizacao)
                        if ("'new' on sink #" in linha or "'new' on card" in linha
                                or "'new' on source #" in linha or "'remove' on source #" in linha):
                            GLib.idle_add(self.marcar_religar)
                    p.wait()
                except Exception:
                    pass
                time.sleep(2)
                GLib.idle_add(self.marcar_atualizacao)

        def ajustar_medidor(self):
            alvo = self.source
            if self.medidor and self.medidor[0] == alvo:
                return
            if self.medidor:
                self.medidor[1].set_state(Gst.State.NULL)
                self.medidor = None
            if not alvo:
                for med in self.medidores:
                    med.definir(-90, -90)
                self.val_nivel.set_text("— dB")
                return
            try:
                p = Gst.parse_launch(
                    f'pulsesrc device="{alvo}" client-name="Fone Havit (medidor)" ! audioconvert ! '
                    'audio/x-raw,channels=1 ! level interval=50000000 ! fakesink sync=false')
                bus = p.get_bus()
                bus.add_signal_watch()
                bus.connect("message::element", self.ao_nivel)
                bus.connect("message::error", lambda b, m: GLib.idle_add(self.reiniciar_medidor))
                p.set_state(Gst.State.PLAYING)
                self.medidor = (alvo, p)
            except Exception as e:
                self.log(f"medidor do microfone indisponível: {e}")

        def reiniciar_medidor(self):
            if self.medidor:
                self.medidor[1].set_state(Gst.State.NULL)
                self.medidor = None
            GLib.timeout_add(1500, lambda: (self.ajustar_medidor(), False)[1])
            return False

        def ao_nivel(self, bus, msg):
            s = msg.get_structure()
            if not (s and s.get_name() == "level"):
                return
            rms, pico = s.get_value("rms")[0], s.get_value("peak")[0]
            for med in self.medidores:
                med.definir(rms, pico)
            self.val_nivel.set_text(db_txt(rms) if rms > -70 else "silêncio")
            aberto = rms > self.lim_ef - 3
            if self.voz_existe:
                for led in self.leds_voz:
                    led.definir(aberto)
                for l, t_ab, t_fe in self.lbls_aberto:
                    l.set_text(t_ab if aberto else t_fe)

        # ---------------- volume ----------------

        def ao_mudar_volume(self, adj, tipo):
            v = int(adj.get_value())
            for l in self.vals[tipo]:
                l.set_text(f"{v}%")
            if self.sync:
                return
            self.toque[tipo] = time.monotonic()
            if tipo not in self.pendente:
                self.pendente[tipo] = GLib.timeout_add(60, self.aplicar_volume, tipo)

        def aplicar_volume(self, tipo):
            self.pendente.pop(tipo, None)
            nome = self.sink if tipo == "sink" else self.source
            if nome:
                pactl(f"set-{tipo}-volume", nome, f"{int(self.adj[tipo].get_value())}%")
                if tipo == "source":
                    # o limiar do "só minha voz" acompanha o ganho do microfone
                    self.lim_ef = limiar_efetivo(self.cfg, self.source)
                    self.sync = True
                    self.esc_limiar.set_value(self.lim_ef)
                    self.sync = False
                    self.mostrar_voz()
                    if self.voz_existe and "voz" not in self.pendente:
                        self.pendente["voz"] = GLib.timeout_add(150, self.aplicar_voz)
            return False

        def ao_mudo(self, bt, tipo):
            if self.sync:
                return
            nome = self.sink if tipo == "sink" else self.source
            if nome:
                pactl(f"set-{tipo}-mute", nome, "1" if bt.get_active() else "0")

        # ---------------- equalizador ----------------

        def mostrar_eq(self):
            g, a, lig = self.cfg["graves"], self.cfg["agudos"], self.cfg["ativo"]
            self.val_graves.set_text(f"{g:+d} dB".replace("-", "−") if g else "0 dB")
            self.val_agudos.set_text(f"{a:+d} dB".replace("-", "−") if a else "0 dB")
            self.curva.definir(g, a, lig)
            self.pontos.definir(g, a, lig)
            if lig and self.eq_existe:
                estado = "LIGADO"
            elif lig and not self.sink:
                estado = "AGUARDANDO FONE"
            elif lig:
                estado = "LIGANDO…"
            else:
                estado = "DESLIGADO"
            self.lbl_eq.set_text(estado)
            self.pontos.set_tooltip_text(f"Equalizador {estado.lower()} · graves {g:+d} dB · agudos {a:+d} dB\n"
                                         "Clique para os ajustes finos")
            for b, gv, av in self.bts_preset:
                ctx = b.get_style_context()
                (ctx.add_class if (gv, av) == (g, a) else ctx.remove_class)("ativo")

        def ao_ligar_eq(self, sw, _p):
            self.cfg["ativo"] = sw.get_active()
            salvar_cfg(self.cfg)
            self.mostrar_eq()
            if self.cfg["ativo"]:
                g, a = self.cfg["graves"], self.cfg["agudos"]
                self.em_fundo(lambda: eq_ligar(g, a), self.depois_modulo)
            else:
                self.em_fundo(eq_desligar, self.depois_modulo)

        def depois_modulo(self, r):
            ok, msg = r if isinstance(r, tuple) else (bool(r), "")
            if msg:
                self.log(("✔ " if ok else "✖ ") + msg)
            self.atualizar()

        def ao_mudar_eq(self, e, chave):
            self.cfg[chave] = int(round(e.get_value()))
            self.mostrar_eq()
            if "eq" not in self.pendente:
                self.pendente["eq"] = GLib.timeout_add(80, self.aplicar_eq)

        def aplicar_eq(self):
            self.pendente.pop("eq", None)
            salvar_cfg(self.cfg)
            if self.cfg["ativo"] and self.eq_existe:
                if not eq_ajustar(self.cfg["graves"], self.cfg["agudos"]):
                    self.log("✖ não consegui ajustar o equalizador")
            return False

        def ao_preset(self, _b, g, a):
            self.esc_graves.set_value(g)
            self.esc_agudos.set_value(a)
            if not self.cfg["ativo"]:
                self.sws_eq[0].set_active(True)

        # ---------------- só minha voz ----------------

        def mostrar_voz(self):
            lig = self.cfg["voz"]
            self.val_limiar.set_text(db_txt(self.lim_ef))
            self.lbl_limiar_p.set_text(f"Limiar {db_txt(self.lim_ef)}")
            self.medidor_voz.limiar = self.lim_ef
            self.medidor_voz.queue_draw()
            if lig and self.voz_existe:
                estado, sub = "LIGADO", "Ligado · a conversa da sala não passa"
            elif lig and not self.source and self.fone_bt:
                estado, sub = "AGUARDANDO MIC", "Precisa do microfone: no Bluetooth, use o modo Chamada"
            elif lig and not self.source:
                estado, sub = "AGUARDANDO FONE", "Aguardando o fone"
            elif lig:
                estado, sub = "LIGANDO…", "Ligando…"
            elif self.fone_bt and not self.source:
                estado, sub = "DESLIGADO", "Precisa do microfone: no Bluetooth, use o modo Chamada"
            else:
                estado, sub = "DESLIGADO", "Corta o som da sala quando você não fala"
                for led in self.leds_voz:
                    led.definir(False)
                for l, _a, _f in self.lbls_aberto:
                    l.set_text("—")
            self.lbl_voz.set_text(estado)
            self.sub_voz.set_text(sub)

        def ao_ligar_voz(self, sw, _p):
            self.cfg["voz"] = sw.get_active()
            salvar_cfg(self.cfg)
            self.mostrar_voz()
            if self.cfg["voz"]:
                lim = self.lim_ef
                self.em_fundo(lambda: voz_ligar(lim), self.depois_modulo)
            else:
                self.em_fundo(voz_desligar, self.depois_modulo)

        def ao_mudar_limiar(self, e):
            self.lim_ef = e.get_value()
            self.mostrar_voz()
            if self.sync:
                return
            self.cfg["voz_limiar"] = self.lim_ef - ganho_mic_db(self.source)
            if "voz" not in self.pendente:
                self.pendente["voz"] = GLib.timeout_add(100, self.aplicar_voz)

        def aplicar_voz(self):
            self.pendente.pop("voz", None)
            salvar_cfg(self.cfg)
            if self.cfg["voz"] and self.voz_existe:
                if not voz_ajustar(self.lim_ef):
                    self.log("✖ não consegui ajustar o 'só minha voz'")
            return False

        def msg_voz(self, markup):
            for l in self.res_voz:
                if l is self.res_voz_p:
                    curto = sem_markup(markup).split("\n")[0]
                    l.set_text(curto)
                    l.set_tooltip_text(sem_markup(markup))
                else:
                    l.set_markup(markup)

        def calibrar_clique(self, _b):
            if not self.source or self.ocupado:
                return
            self.ocupado = True
            for b in self.bts_calibrar:
                b.set_sensitive(False)
            fonte = self.source
            ganho = ganho_mic_db(fonte)
            etapas = [("1/2  Fique em SILÊNCIO (deixe a sala como está)…", 4),
                      ("2/2  Agora FALE normalmente (conte de 1 a 10)…", 4)]
            gravs = []

            def etapa(i):
                if i == len(etapas):
                    self.msg_voz("Calculando…")
                    self.em_fundo(lambda: calibrar(gravs[0], gravs[1], ganho), fim)
                    return False
                txt, seg = etapas[i]
                self.msg_voz(f'<span foreground="#ffffff"><b>{esc(txt)}</b></span>')
                for b in self.bts_calibrar:
                    self.texto_botao(b, f"Etapa {i + 1}/2")

                def grava():
                    r = subprocess.run(["timeout", str(seg), "parec", f"--device={fonte}", "--raw",
                                        "--channels=1", "--rate=48000", "--format=s16le"],
                                       env=ENV, capture_output=True)
                    return r.stdout
                self.em_fundo(grava, lambda raw: (gravs.append(raw if isinstance(raw, bytes) else b""),
                                                  GLib.timeout_add(600, lambda: etapa(i + 1))))
                return False

            def fim(r):
                self.ocupado = False
                for b in self.bts_calibrar:
                    b.set_sensitive(True)
                    self.texto_botao(b, "Calibrar")
                if not isinstance(r, tuple) or len(r) != 4:
                    self.msg_voz(f'<span foreground="{COR_ERRO}"><b>Não consegui calibrar (nada gravado).</b></span>'
                                 '\nTente Consertar agora.')
                    return
                lim_ref, sala, voz, aviso = r
                self.cfg["voz_limiar"] = lim_ref
                salvar_cfg(self.cfg)
                self.lim_ef = limiar_efetivo(self.cfg, self.source)
                self.sync = True
                self.esc_limiar.set_value(self.lim_ef)
                self.sync = False
                self.mostrar_voz()
                txt = f"Sala {db_txt(sala)} · sua voz {db_txt(voz)} → limiar {db_txt(self.lim_ef)}."
                if aviso:
                    self.msg_voz(f'<span foreground="{COR_AVISO}"><b>{esc(aviso)}</b></span>\n{esc(txt)}')
                else:
                    self.msg_voz(f'<span foreground="#ffffff"><b>Calibrado.</b></span> {esc(txt)}')
                self.log("Calibração do 'só minha voz': " + txt + (" " + aviso if aviso else ""))
                if self.cfg["voz"]:
                    self.aplicar_voz()
                else:
                    self.sws_voz[0].set_active(True)
            etapa(0)

        # ---------------- testes ----------------

        def em_fundo(self, func, depois=None):
            def corre():
                try:
                    r = func()
                except Exception as e:
                    r = (False, f"erro: {e}")
                if depois:
                    GLib.idle_add(lambda: (depois(r), False)[1])
            threading.Thread(target=corre, daemon=True).start()

        def testar_lado(self, bt, arquivo, lado):
            if not self.sink:
                return
            bt.set_sensitive(False)
            self.log(f"Tocando a voz só no lado {lado.upper()}…")
            if self.fone_bt and modo_bt(self.fone_bt["perfil"]) == "chamada":
                self.log("  (Bluetooth no modo Chamada: o som é mono, os dois lados tocam juntos)")
            alvo = EQ_SINK if self.eq_existe else self.sink
            self.em_fundo(lambda: subprocess.run(["paplay", f"--device={alvo}", os.path.join(SONS, arquivo)], env=ENV),
                          lambda r: bt.set_sensitive(True))

        def msg_mic(self, markup, fixo=True):
            """Mensagem do teste do microfone: completa na página Áudio, uma linha na principal."""
            for l in self.res_mic:
                l.set_markup(markup)
            self.msg_mic_p.set_markup(markup.split("\n")[0])
            self.msg_mic_p.set_tooltip_text(sem_markup(markup))
            self.pilha_mic.set_visible_child_name("msg")
            if self.volta_mic:
                GLib.source_remove(self.volta_mic)
                self.volta_mic = 0
            if not fixo:
                def voltar():
                    self.volta_mic = 0
                    self.pilha_mic.set_visible_child_name("medidor")
                    return False
                self.volta_mic = GLib.timeout_add_seconds(10, voltar)

        def testar_mic(self, _bt):
            if not self.source or self.ocupado:
                return
            self.ocupado = True
            for b in self.bts_mic + self.bts_ouvir:
                b.set_sensitive(False)
            self.msg_mic("Prepare-se para falar…")
            self.contagem = 3
            fonte = VOZ_FONTE if self.voz_existe else self.source
            self.teste_com_voz = self.voz_existe

            def contar():
                if self.contagem > 0:
                    for b in self.bts_mic:
                        self.texto_botao(b, f"Começa em {self.contagem}…")
                    self.contagem -= 1
                    return True
                for b in self.bts_mic:
                    self.texto_botao(b, "● Gravando — fale agora")
                self.msg_mic("Fale normalmente por 5 segundos (ex.: conte de 1 a 10).")
                self.em_fundo(lambda: self.gravar(fonte), self.fim_teste_mic)
                return False
            contar()
            GLib.timeout_add(1000, contar)

        def gravar(self, fonte):
            r = subprocess.run(["timeout", "5", "parec", f"--device={fonte}", "--raw", "--channels=1",
                                "--rate=48000", "--format=s16le"], env=ENV, capture_output=True)
            d = r.stdout
            if len(d) < 9600:
                return None
            blocos = [audioop.rms(d[i:i + 12000], 2) for i in range(0, len(d) - 12000 + 1, 12000)]
            db = lambda v: 20 * math.log10(v / 32768) if v > 0 else -120.0
            pico = audioop.max(d, 2)
            os.makedirs(CACHE, exist_ok=True)
            arq = os.path.join(CACHE, "teste-microfone.wav")
            ganho = min(16.0, 0.5 * 32767 / pico) if pico else 1.0
            with wave.open(arq, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
                w.writeframes(audioop.mul(d, 2, ganho))
            return {"fala": db(max(blocos)), "fundo": db(min(blocos)), "pico": db(pico), "arquivo": arq}

        def fim_teste_mic(self, r):
            self.ocupado = False
            for b in self.bts_mic:
                self.texto_botao(b, "Testar Microfone" if b is self.bts_mic[0] else "Testar microfone")
                b.set_sensitive(True)
            if not r or isinstance(r, tuple):
                self.msg_mic(f'<span foreground="{COR_ERRO}"><b>Nada gravado.</b></span>\n'
                             'O microfone do fone não mandou áudio. Tente Consertar agora.', fixo=False)
                self.log("✖ teste do microfone: nada gravado")
                return
            fala, fundo, pico = r["fala"], r["fundo"], r["pico"]
            num = f"fala {db_txt(fala)} · pausas {db_txt(fundo)} · pico {db_txt(pico)}"
            if self.teste_com_voz:
                num += " · com só minha voz"
            if fala <= -100:
                txt, cor = "Silêncio total: o mic está mudo no próprio fone (botão de mudo ou haste levantada?).", COR_ERRO
            elif fala < -42:
                txt, cor = "Quase nada captado. Confira o botão de mudo do fone e se a haste está abaixada.", COR_ERRO
            elif fala < -30:
                txt, cor = "Funcionando, mas BAIXO. Suba o ganho do microfone (ex.: 150%).", COR_AVISO
            else:
                txt, cor = "Microfone funcionando com bom volume.", "#ffffff"
            self.msg_mic(f'<span foreground="{cor}"><b>{esc(txt)}</b></span>\n'
                         f'<span foreground="#8a8a8a">{num}</span>', fixo=False)
            self.log(f"Teste do microfone: {txt} ({num})")
            self.gravacao = r["arquivo"]
            for b in self.bts_ouvir:
                b.set_sensitive(True)
            self.bt_ouvir_p.set_no_show_all(False)
            self.bt_ouvir_p.show_all()

        def ouvir_gravacao(self, bt):
            if not (self.gravacao and self.sink):
                return
            bt.set_sensitive(False)
            self.log("Tocando a gravação (com volume normalizado para dar para ouvir).")
            alvo = EQ_SINK if self.eq_existe else self.sink
            self.em_fundo(lambda: subprocess.run(["paplay", f"--device={alvo}", self.gravacao], env=ENV),
                          lambda r: bt.set_sensitive(True))

        # ---------------- script do terminal ----------------

        def rodar_script(self, bt, opcao, nome):
            if self.ocupado:
                return
            self.ocupado = True
            for b in self.bts_script:
                b.set_sensitive(False)
            self.log(f"── {nome} ──")

            def corre():
                args = [SCRIPT] + ([opcao] if opcao else [])
                try:
                    # HAVIT_PAINEL: o script não abre terminal nem instala pacotes quando chamado daqui
                    r = subprocess.run(args, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL,
                                       env=dict(os.environ, HAVIT_PAINEL="1"))
                    saida = r.stdout + r.stderr
                except Exception as e:
                    saida = f"erro ao rodar o script: {e}"
                return re.sub(r"\x1b\[[0-9;]*m", "", saida).strip("\n")

            def fim(saida):
                self.ocupado = False
                for b in self.bts_script:
                    b.set_sensitive(True)
                self.log(saida if isinstance(saida, str) else str(saida))
                self.atualizar()
            self.em_fundo(corre, fim)

        def confirmar(self, titulo, texto):
            d = Gtk.MessageDialog(transient_for=self.win, modal=True, message_type=Gtk.MessageType.WARNING,
                                  buttons=Gtk.ButtonsType.OK_CANCEL, text=titulo)
            d.format_secondary_text(texto)
            r = d.run()
            d.destroy()
            return r == Gtk.ResponseType.OK

        def desfazer(self, _b):
            if self.confirmar("Desfazer os ajustes do mixer?",
                              "Isso remove a correção do volume de hardware do receptor (fone e microfone). "
                              "Depois de reconectar o receptor, o lado DIREITO e o microfone podem voltar a "
                              "ficar mudos.\n\nO equalizador e o 'só minha voz' não são afetados."):
                self.rodar_script(None, "--desfazer", "Desfazer ajustes")

        def restaurar_padroes(self, _b):
            if not self.confirmar("Restaurar os padrões do painel?",
                                  "Desliga o equalizador e o 'só minha voz', zera graves e agudos e volta o limiar "
                                  f"para {db_txt(PADRAO['voz_limiar'])}.\n\nO volume, o ganho do microfone e a "
                                  "correção do mixer do receptor não mudam."):
                return
            self.sws_eq[0].set_active(PADRAO["ativo"])
            self.sws_voz[0].set_active(PADRAO["voz"])
            self.esc_graves.set_value(PADRAO["graves"])
            self.esc_agudos.set_value(PADRAO["agudos"])
            self.cfg["voz_limiar"] = PADRAO["voz_limiar"]
            salvar_cfg(self.cfg)
            self.lim_ef = limiar_efetivo(self.cfg, self.source)
            self.sync = True
            self.esc_limiar.set_value(self.lim_ef)
            self.sync = False
            self.mostrar_eq()
            self.mostrar_voz()
            self.log("✔ padrões do painel restaurados")

        # ---------------- util ----------------

        def log(self, texto):
            fim = self.buf.get_end_iter()
            self.buf.insert(fim, (("\n" if self.buf.get_char_count() else "") + texto))
            GLib.idle_add(lambda: (self.tv.scroll_to_iter(self.buf.get_end_iter(), 0, False, 0, 1), False)[1])
            linhas = [l.strip() for l in texto.splitlines() if l.strip() and not l.strip().startswith("──")]
            if linhas:
                self.ultimo.set_text(linhas[-1])
                self.ultimo.set_tooltip_text(texto.strip())

        def salvar_captura(self):
            self.log("Painel aberto. Use Consertar agora se o som falhar ou um lado ficar mudo.")
            while Gtk.events_pending():
                Gtk.main_iteration()
            pb = self.win.get_pixbuf()
            pb.savev(self.captura, "png", [], [])
            print("captura salva:", self.captura, pb.get_width(), "x", pb.get_height())
            Gtk.main_quit()
            return False

        def sair(self, *_a):
            if self.medidor:
                self.medidor[1].set_state(Gst.State.NULL)
            Gtk.main_quit()

    captura, pagina = None, "principal"
    if "--captura" in sys.argv:
        i = sys.argv.index("--captura")
        captura = sys.argv[i + 1]
        if len(sys.argv) > i + 2:
            pagina = sys.argv[i + 2]
    Painel(captura, pagina)
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    Gtk.main()


# ======================================================================

if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--vigiar"]:
        sys.exit(vigiar())
    elif a[:1] == ["--filtros"]:
        tudo_ok = True
        for nome in PLUGINS:
            ok = garantir_plugin(nome)
            tudo_ok = tudo_ok and ok
            print(("  OK  " if ok else "  ERRO ") + f"filtro {nome}: {plugin_so(nome)}")
        sys.exit(0 if tudo_ok else 1)
    elif a[:1] == ["--reaplicar"]:
        for m in reaplicar():
            print("  OK  " + m)
    elif a[:1] == ["--eq"] and len(a) >= 3:
        c = ler_cfg()
        c.update(ativo=True, graves=int(max(-LIMITE, min(LIMITE, round(float(a[1]))))),
                 agudos=int(max(-LIMITE, min(LIMITE, round(float(a[2]))))))
        salvar_cfg(c)
        ok, msg = eq_ligar(c["graves"], c["agudos"])
        print(msg, f"(graves {c['graves']:+d} dB, agudos {c['agudos']:+d} dB)")
        sys.exit(0 if ok else 1)
    elif a[:1] == ["--eq-desligar"]:
        c = ler_cfg()
        c["ativo"] = False
        salvar_cfg(c)
        print(eq_desligar()[1])
    elif a[:1] == ["--voz"] and len(a) >= 2:
        c = ler_cfg()
        c["voz"] = a[1] in ("on", "1", "ligar", "sim")
        salvar_cfg(c)
        if c["voz"]:
            _, _, src, _ = dispositivos()
            ok, msg = voz_ligar(limiar_efetivo(c, src))
        else:
            ok, msg = voz_desligar()
        print(msg)
        sys.exit(0 if ok else 1)
    elif a[:1] in (["-h"], ["--ajuda"], ["--help"]):
        print(__doc__.strip())
    else:
        principal()

