# c_core/Makefile — быстрая сборка без cmake (та же семантика, что scripts/build_c_core.py).
# Предупреждения компилятора = блокер (-Werror). Запуск: make -C c_core
CC      ?= gcc
ARCH    ?= $(shell uname -m)
CFLAGS  ?= -std=c11 -O3 -Wall -Wextra -Werror -shared -fPIC
LDLIBS  ?= -lm

ifeq ($(findstring x86_64,$(ARCH)),x86_64)
    CFLAGS += -mavx2
else ifeq ($(findstring arm,$(ARCH)),arm)
    CFLAGS += -mcpu=native
endif

SRCS := $(wildcard *.c)
LIB  := libcompass_core.so

$(LIB): $(SRCS) $(wildcard *.h)
	$(CC) $(CFLAGS) $(SRCS) -o $(LIB) $(LDLIBS)

.PHONY: clean
clean:
	rm -f $(LIB)
