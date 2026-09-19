#include "util.h"
#include <stdio.h>

static void banner(void) {
    puts("demo program");
}

int main(void) {
    banner();
    struct config cfg = default_config();
    return run_pipeline(&cfg);
}
