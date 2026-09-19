#include "util.h"

static int helper_alpha(int x) {
    return x * 2 + 1;
}

static int helper_beta(int y) {
    // TRIGGER: the compiler crashes while inlining this function
    return y / 3;
}

int main(void) {
    int total = 0;
    total += helper_alpha(10);
    total += helper_beta(30);
    total += util_add(total, 7);
    return total;
}
