# c_core — нативное ядро C11 (libcompass_core.so)

Один общий shared-объект из всех `.c`; в git не коммитится (`.gitignore: *.so`).

## Сборка (три равнозначных пути, флаги обязаны совпадать)

| Путь | Команда | Когда |
|---|---|---|
| Python (канонический) | `python3 scripts/build_c_core.py` | используется авто-пересборкой `backend/bridges/loader.py`, Docker-образом и VS Code Task 1 |
| CMake | `cmake -S c_core -B c_core/build && cmake --build c_core/build` | IDE / кросс-архитектурная проверка флагов (x86_64 AVX2, ARM `-mcpu=native`) |
| Make | `make -C c_core` | быстрый ручной путь |

Общие инварианты всех трёх путей: `-std=c11 -O3 -Wall -Wextra` + `-Werror`
(предупреждение = блокер сборки — это медицинское ядро), все `*.c` собираются разом.

## Карта модулей

| Модуль | Роль | Python-мост |
|---|---|---|
| `compass_core.c/.h` | СКФ CKD-EPI 2021, Чайлд-Пью, RK4 PK 168 ч, DDI, эндокринные маски, голосовой DSP v26 | `backend/bridges/core.py` |
| `pk_rk45_solver.c` | Дорман—Принс RK45(5)4, FSAL, Михаэлис—Ментен, CYP2D6/CYP3A4 | `backend/bridges/lexers.py` (C11PopPKAndBitapEngine) |
| `bitap_ru_lexer.c` | НЕЧЁТКИЙ лексер: 64-битный Bitap/Wu-Manber, k<=2 опечатки, русский UTF-8 case-folding, отрицания | `backend/bridges/lexers.py` |
| `simd_lexer.c` | ТОЧНЫЙ лексер: Ахо—Корасик по мед. словарю (предразметка сущностей для парсера ЭМК) | `backend/bridges/lexers.py` (MedicalLexer) |
| `ot_xai_mahalanobis.c` | Sinkhorn OT, Ledoit—Wolf + Махаланобис OOD, точные значения Оуэна | `backend/bridges/ot_xai.py` |
| `bio_dsp_dst.c` | LPC-4 глоттальный фильтр, TKEO, Weibull MLE, Демпстер—Шефер + W2-адаптация | `backend/bridges/bio_acoustic.py`, `bio_fusion.py` |
| `micronets_int8.c` | W8A8 INT8-инференс микросетей + конформные множества Mondrian RAPS | `backend/bridges/bio_native.py` |
| `pii_scrubber.c` | Потоковое HMAC-обезличивание ПДн (152-ФЗ) | `backend/bridges/pii.py` |

## Два лексера — это НЕ дублирование

`simd_lexer.c` (Ахо—Корасик) и `bitap_ru_lexer.c` (Bitap) решают разные задачи и
не взаимозаменяемы: первый ищет ТОЧНЫЕ вхождения всех словарных сущностей за один
проход (дешёвая предразметка), второй — НЕЧЁТКИЕ совпадения с допустимостью двух
опечаток (врач печатает «креатнин», «флуконзол») с нормализацией регистра.
Оба биндятся в одном модуле `backend/bridges/lexers.py` с документированным
разделением зон ответственности.
