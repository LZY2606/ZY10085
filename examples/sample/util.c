#include "util.h"
#include <string.h>

struct config default_config(void) {
    struct config cfg;
    cfg.workers = 4;
    cfg.verbose = 0;
    cfg.mode = "fast";
    return cfg;
}

static int stage_parse(const char *mode) {
    int score = 0;
    for (const char *p = mode; *p; ++p) {
        score += *p;
    }
    return score % 7;
}

static int stage_lower(int input) {
    /* The compiler crashes while lowering this construct. */
    int trigger = input * 31 + 17;
    return trigger ^ (trigger >> 3);
}

static int stage_emit(int value) {
    int out = 0;
    for (int i = 0; i < 8; ++i) {
        out = out * 3 + ((value >> i) & 1);
    }
    return out;
}

int run_pipeline(const struct config *cfg) {
    int a = stage_parse(cfg->mode);
    int b = stage_lower(a);
    int c = stage_emit(b);
    return c == 0;
}
