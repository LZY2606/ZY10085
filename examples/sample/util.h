#ifndef UTIL_H
#define UTIL_H

struct config {
    int workers;
    int verbose;
    const char *mode;
};

struct config default_config(void);
int run_pipeline(const struct config *cfg);

#endif
