/*
 * havit_voz — "só minha voz": portão de ruído para o microfone do fone (plugin LADSPA, mono)
 * para o app "Fone Havit". Carregado pelo PulseAudio via module-ladspa-sink.
 *
 * A voz de quem usa o fone chega ao microfone (na haste, perto da boca) bem mais forte que as
 * conversas da sala. O portão deixa passar o som só quando ele passa do limiar; abaixo disso o
 * microfone é abaixado (redução), então a sala não aparece nas pausas.
 *
 *   porta 0  Limiar (dB)     -80 .. -5    nível (RMS de ~5 ms, dBFS) a partir do qual abre
 *   porta 1  Redução (dB)    -80 ..  0    quanto abaixa quando fechado
 *   porta 2  entrada de áudio
 *   porta 3  saída de áudio
 *
 * Detalhes: passa-altas de 90 Hz (tira ronco/batidas), antecipação de 4 ms (abre antes da
 * primeira sílaba), histerese de 6 dB, segura aberto 250 ms, abre em 2 ms e fecha em 90 ms.
 *
 * Compilar:  gcc -O2 -fPIC -shared -o havit_voz.so havit_voz.c -lm
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>

/* ---- interface LADSPA 1.1 ---- */
typedef float LADSPA_Data;
typedef int LADSPA_Properties;
typedef int LADSPA_PortDescriptor;
typedef int LADSPA_PortRangeHintDescriptor;
typedef struct { LADSPA_PortRangeHintDescriptor HintDescriptor; LADSPA_Data LowerBound, UpperBound; } LADSPA_PortRangeHint;
typedef void *LADSPA_Handle;
typedef struct _LADSPA_Descriptor {
    unsigned long UniqueID;
    const char *Label;
    LADSPA_Properties Properties;
    const char *Name, *Maker, *Copyright;
    unsigned long PortCount;
    const LADSPA_PortDescriptor *PortDescriptors;
    const char *const *PortNames;
    const LADSPA_PortRangeHint *PortRangeHints;
    void *ImplementationData;
    LADSPA_Handle (*instantiate)(const struct _LADSPA_Descriptor *, unsigned long SampleRate);
    void (*connect_port)(LADSPA_Handle, unsigned long Port, LADSPA_Data *DataLocation);
    void (*activate)(LADSPA_Handle);
    void (*run)(LADSPA_Handle, unsigned long SampleCount);
    void (*run_adding)(LADSPA_Handle, unsigned long SampleCount);
    void (*set_run_adding_gain)(LADSPA_Handle, LADSPA_Data Gain);
    void (*deactivate)(LADSPA_Handle);
    void (*cleanup)(LADSPA_Handle);
} LADSPA_Descriptor;

#define LADSPA_PROPERTY_REALTIME        0x1
#define LADSPA_PROPERTY_HARD_RT_CAPABLE 0x4
#define LADSPA_PORT_INPUT   0x1
#define LADSPA_PORT_OUTPUT  0x2
#define LADSPA_PORT_CONTROL 0x4
#define LADSPA_PORT_AUDIO   0x8
#define LADSPA_HINT_BOUNDED_BELOW 0x1
#define LADSPA_HINT_BOUNDED_ABOVE 0x2
#define LADSPA_HINT_DEFAULT_MIDDLE 0xC0

#define P_LIMIAR 0
#define P_REDUCAO 1
#define P_IN 2
#define P_OUT 3
#define NPORTS 4

#define ANTECIPA_MAX 1024          /* amostras (>= 4 ms a 192 kHz não precisa; 48 kHz usa 192) */
#define HISTERESE_DB 6.0

typedef struct {
    LADSPA_Data *port[NPORTS];
    double fs;
    /* passa-altas (biquad, forma direta II transposta) */
    double b0, b1, b2, a1, a2, z1, z2;
    /* detector */
    double env, c_env;
    /* portão */
    int aberto;
    long segura, segura_max;
    double ganho, c_abre, c_fecha;
    /* parâmetros em uso */
    float limiar_atual, reducao_atual;
    double p_abre, p_fecha, piso;
    /* antecipação */
    float atraso[ANTECIPA_MAX];
    unsigned pos, n_atraso;
} Voz;

static double limita(double v, double lo, double hi) {
    if (!(v == v)) return lo;
    return v < lo ? lo : (v > hi ? hi : v);
}

static void parametros(Voz *v, float limiar, float reducao) {
    double l = limita(limiar, -80.0, -5.0);
    double r = limita(reducao, -80.0, 0.0);
    v->p_abre = pow(10.0, l / 10.0);                    /* potência (RMS²) */
    v->p_fecha = pow(10.0, (l - HISTERESE_DB) / 10.0);
    v->piso = pow(10.0, r / 20.0);
    v->limiar_atual = limiar;
    v->reducao_atual = reducao;
}

static LADSPA_Handle instanciar(const LADSPA_Descriptor *d, unsigned long fs) {
    (void)d;
    Voz *v = calloc(1, sizeof(Voz));
    if (!v) return NULL;
    v->fs = fs > 0 ? (double)fs : 48000.0;
    /* passa-altas 90 Hz, Q = 0,707 (RBJ) */
    double w0 = 2.0 * M_PI * 90.0 / v->fs, c = cos(w0), alpha = sin(w0) / (2.0 * 0.70710678);
    double a0 = 1.0 + alpha;
    v->b0 = (1.0 + c) / 2.0 / a0;
    v->b1 = -(1.0 + c) / a0;
    v->b2 = (1.0 + c) / 2.0 / a0;
    v->a1 = -2.0 * c / a0;
    v->a2 = (1.0 - alpha) / a0;
    v->c_env = 1.0 - exp(-1.0 / (0.005 * v->fs));     /* RMS de ~5 ms */
    v->c_abre = 1.0 - exp(-1.0 / (0.002 * v->fs));    /* abre em ~2 ms */
    v->c_fecha = 1.0 - exp(-1.0 / (0.090 * v->fs));   /* fecha em ~90 ms */
    v->segura_max = (long)(0.250 * v->fs);
    v->n_atraso = (unsigned)(0.004 * v->fs);
    if (v->n_atraso >= ANTECIPA_MAX) v->n_atraso = ANTECIPA_MAX - 1;
    if (v->n_atraso < 1) v->n_atraso = 1;
    parametros(v, -45.0f, -40.0f);
    v->ganho = v->piso;
    return v;
}

static void conectar(LADSPA_Handle h, unsigned long p, LADSPA_Data *loc) {
    if (p < NPORTS) ((Voz *)h)->port[p] = loc;
}

static void ativar(LADSPA_Handle h) {
    Voz *v = h;
    v->z1 = v->z2 = v->env = 0.0;
    v->aberto = 0;
    v->segura = 0;
    v->ganho = v->piso;
    memset(v->atraso, 0, sizeof v->atraso);
    v->pos = 0;
}

static void rodar(LADSPA_Handle h, unsigned long n) {
    Voz *v = h;
    float limiar = v->port[P_LIMIAR] ? *v->port[P_LIMIAR] : -45.0f;
    float reducao = v->port[P_REDUCAO] ? *v->port[P_REDUCAO] : -40.0f;
    if (limiar != v->limiar_atual || reducao != v->reducao_atual) parametros(v, limiar, reducao);
    const LADSPA_Data *in = v->port[P_IN];
    LADSPA_Data *out = v->port[P_OUT];
    if (!in || !out) return;

    for (unsigned long i = 0; i < n; i++) {
        double x = in[i];
        if (!(x == x)) x = 0.0;
        /* passa-altas */
        double y = v->b0 * x + v->z1;
        v->z1 = v->b1 * x - v->a1 * y + v->z2;
        v->z2 = v->b2 * x - v->a2 * y;
        if (fabs(v->z1) < 1e-20) v->z1 = 0.0;
        if (fabs(v->z2) < 1e-20) v->z2 = 0.0;

        /* detector (olha o som "do futuro": antes do atraso) */
        v->env += v->c_env * (y * y - v->env);
        if (v->env < 1e-20) v->env = 0.0;
        if (v->env > v->p_abre) {
            v->aberto = 1;
            v->segura = v->segura_max;
        } else if (v->env < v->p_fecha || !v->aberto) {
            if (v->segura > 0) v->segura--;
            else v->aberto = 0;
        } else if (v->segura < v->segura_max / 2) {
            v->segura = v->segura_max / 2;               /* entre os limiares: continua aberto */
        }

        double alvo = v->aberto ? 1.0 : v->piso;
        v->ganho += (alvo - v->ganho) * (alvo > v->ganho ? v->c_abre : v->c_fecha);

        /* antecipação: o áudio sai atrasado em n_atraso amostras */
        float atrasado = v->atraso[v->pos];
        v->atraso[v->pos] = (float)y;
        v->pos = (v->pos + 1) % v->n_atraso;

        double s = atrasado * v->ganho;
        if (s > 1.0) s = 1.0; else if (s < -1.0) s = -1.0;
        out[i] = (LADSPA_Data)s;
    }
}

static void limpar(LADSPA_Handle h) { free(h); }

static const LADSPA_PortDescriptor portas[NPORTS] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
};
static const char *const nomes[NPORTS] = { "Limiar (dB)", "Reducao (dB)", "Entrada", "Saida" };
static const LADSPA_PortRangeHint faixas[NPORTS] = {
    { LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_DEFAULT_MIDDLE, -80.0f, -5.0f },
    { LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_DEFAULT_MIDDLE, -80.0f, 0.0f },
    { 0, 0, 0 },
    { 0, 0, 0 },
};

static const LADSPA_Descriptor descritor = {
    .UniqueID = 4788,
    .Label = "havit_voz",
    .Properties = LADSPA_PROPERTY_REALTIME | LADSPA_PROPERTY_HARD_RT_CAPABLE,
    .Name = "Havit voz (portao de ruido)",
    .Maker = "Fone Havit",
    .Copyright = "uso pessoal",
    .PortCount = NPORTS,
    .PortDescriptors = portas,
    .PortNames = nomes,
    .PortRangeHints = faixas,
    .ImplementationData = NULL,
    .instantiate = instanciar,
    .connect_port = conectar,
    .activate = ativar,
    .run = rodar,
    .run_adding = NULL,
    .set_run_adding_gain = NULL,
    .deactivate = NULL,
    .cleanup = limpar,
};

const LADSPA_Descriptor *ladspa_descriptor(unsigned long i) {
    return i == 0 ? &descritor : NULL;
}
