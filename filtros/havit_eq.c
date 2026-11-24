/*
 * havit_eq — equalizador simples de graves e agudos (plugin LADSPA, mono)
 * para o app "Fone Havit". Carregado pelo PulseAudio via module-ladspa-sink.
 *
 *   porta 0  Graves (dB)   -12 .. +12   prateleira baixa  (low-shelf)  em 110 Hz
 *   porta 1  Agudos (dB)   -12 .. +12   prateleira alta   (high-shelf) em 7 kHz
 *   porta 2  entrada de áudio
 *   porta 3  saída de áudio
 *
 * Filtros biquad do "Audio EQ Cookbook" (R. Bristow-Johnson), S = 1.
 * Quando há reforço, aplica um pré-ganho negativo automático para não distorcer.
 *
 * Compilar:  gcc -O2 -fPIC -shared -o havit_eq.so havit_eq.c -lm
 * (as definições abaixo seguem a interface pública LADSPA 1.1, sem precisar do ladspa-sdk)
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
#define LADSPA_HINT_DEFAULT_0     0x200

/* ---- filtro ---- */
#define P_BASS 0
#define P_TREB 1
#define P_IN   2
#define P_OUT  3
#define NPORTS 4

#define F_BASS 110.0
#define F_TREB 7000.0
#define LIMITE 12.0

typedef struct { double b0, b1, b2, a1, a2, z1, z2; } Biquad;

typedef struct {
    LADSPA_Data *port[NPORTS];
    double fs;
    float bass_atual, treb_atual;   /* valores para os quais os coeficientes foram calculados */
    double pre;                     /* pré-ganho linear */
    Biquad lo, hi;
} Eq;

static double limitar(double v) {
    if (!(v == v)) return 0.0;          /* NaN */
    if (v > LIMITE) return LIMITE;
    if (v < -LIMITE) return -LIMITE;
    return v;
}

/* shelf: tipo 0 = low-shelf, 1 = high-shelf. Mantém o estado (z1, z2) para não estalar. */
static void calc_shelf(Biquad *f, int alto, double fs, double f0, double db) {
    double A = pow(10.0, db / 40.0);
    double w0 = 2.0 * M_PI * f0 / fs;
    double c = cos(w0), s = sin(w0);
    double alpha = s / 2.0 * sqrt(2.0);          /* S = 1 */
    double sa = 2.0 * sqrt(A) * alpha;
    double b0, b1, b2, a0, a1, a2;
    if (!alto) {
        b0 = A * ((A + 1) - (A - 1) * c + sa);
        b1 = 2 * A * ((A - 1) - (A + 1) * c);
        b2 = A * ((A + 1) - (A - 1) * c - sa);
        a0 = (A + 1) + (A - 1) * c + sa;
        a1 = -2 * ((A - 1) + (A + 1) * c);
        a2 = (A + 1) + (A - 1) * c - sa;
    } else {
        b0 = A * ((A + 1) + (A - 1) * c + sa);
        b1 = -2 * A * ((A - 1) + (A + 1) * c);
        b2 = A * ((A + 1) + (A - 1) * c - sa);
        a0 = (A + 1) - (A - 1) * c + sa;
        a1 = 2 * ((A - 1) - (A + 1) * c);
        a2 = (A + 1) - (A - 1) * c - sa;
    }
    f->b0 = b0 / a0; f->b1 = b1 / a0; f->b2 = b2 / a0;
    f->a1 = a1 / a0; f->a2 = a2 / a0;
}

static inline double passo(Biquad *f, double x) {   /* forma direta II transposta */
    double y = f->b0 * x + f->z1;
    f->z1 = f->b1 * x - f->a1 * y + f->z2;
    f->z2 = f->b2 * x - f->a2 * y;
    if (fabs(f->z1) < 1e-20) f->z1 = 0.0;          /* evita números denormais no silêncio */
    if (fabs(f->z2) < 1e-20) f->z2 = 0.0;
    return y;
}

static void atualizar(Eq *e, float bass, float treb) {
    double b = limitar(bass), t = limitar(treb);
    double fs = e->fs;
    double ft = F_TREB < fs * 0.45 ? F_TREB : fs * 0.45;
    calc_shelf(&e->lo, 0, fs, F_BASS, b);
    calc_shelf(&e->hi, 1, fs, ft, t);
    double reforco = b > t ? b : t;
    if (reforco < 0) reforco = 0;
    e->pre = pow(10.0, -0.6 * reforco / 20.0);     /* tira 60% do maior reforço */
    e->bass_atual = bass;
    e->treb_atual = treb;
}

static LADSPA_Handle instanciar(const LADSPA_Descriptor *d, unsigned long fs) {
    (void)d;
    Eq *e = calloc(1, sizeof(Eq));
    if (!e) return NULL;
    e->fs = fs > 0 ? (double)fs : 48000.0;
    atualizar(e, 0.0f, 0.0f);
    return e;
}

static void conectar(LADSPA_Handle h, unsigned long p, LADSPA_Data *loc) {
    if (p < NPORTS) ((Eq *)h)->port[p] = loc;
}

static void ativar(LADSPA_Handle h) {
    Eq *e = h;
    e->lo.z1 = e->lo.z2 = e->hi.z1 = e->hi.z2 = 0.0;
}

static void rodar(LADSPA_Handle h, unsigned long n) {
    Eq *e = h;
    float bass = e->port[P_BASS] ? *e->port[P_BASS] : 0.0f;
    float treb = e->port[P_TREB] ? *e->port[P_TREB] : 0.0f;
    if (bass != e->bass_atual || treb != e->treb_atual) atualizar(e, bass, treb);
    const LADSPA_Data *in = e->port[P_IN];
    LADSPA_Data *out = e->port[P_OUT];
    if (!in || !out) return;
    for (unsigned long i = 0; i < n; i++) {
        double y = passo(&e->hi, passo(&e->lo, (double)in[i] * e->pre));
        if (y > 1.0) y = 1.0; else if (y < -1.0) y = -1.0;
        out[i] = (LADSPA_Data)y;
    }
}

static void limpar(LADSPA_Handle h) { free(h); }

static const LADSPA_PortDescriptor portas[NPORTS] = {
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_CONTROL,
    LADSPA_PORT_INPUT | LADSPA_PORT_AUDIO,
    LADSPA_PORT_OUTPUT | LADSPA_PORT_AUDIO,
};
static const char *const nomes[NPORTS] = { "Graves (dB)", "Agudos (dB)", "Entrada", "Saida" };
static const LADSPA_PortRangeHint faixas[NPORTS] = {
    { LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_DEFAULT_0, -LIMITE, LIMITE },
    { LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE | LADSPA_HINT_DEFAULT_0, -LIMITE, LIMITE },
    { 0, 0, 0 },
    { 0, 0, 0 },
};

static const LADSPA_Descriptor descritor = {
    .UniqueID = 4787,               /* faixa livre para uso local */
    .Label = "havit_eq",
    .Properties = LADSPA_PROPERTY_REALTIME | LADSPA_PROPERTY_HARD_RT_CAPABLE,
    .Name = "Havit EQ (graves e agudos)",
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
