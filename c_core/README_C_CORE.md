# Нативное ядро C-Core (libcompass_core.so)

## Текущее состояние (17.10.2026, Михаил)
- `compass_core.h` — бинарный контракт C11 (структуры входа/выхода, битовые флаги 8 противопоказаний ОХЛП, DDI-маски, эндокринные маски, `extern "C"` для связки с C++).
- `compass_core.c` — монолитное ядро: СКФ по CKD-EPI 2021, Чайлд-Пью, решатель РК4 фармакокинетики на 168 ч (маритупирдин + метаболит М1, фенотипы CYP2D6), матрица DDI, эндокринно-соматические маски, voice DSP (F0, Jitter, Shimmer, Pause Ratio).
- Скомпилированный `libcompass_core.so` в git **не** коммитится (см. `.gitignore`) — собирается локально на каждой машине.

## План по новым модулям
- Михаил: `pk_cyp2d6_solver.c`, `ohpl_hardstops.c`, `voice_dsp.c` (выделение из монолита — по мере времени).
- Всеволод: `pii_scrubber.c` (потоковое обезличивание ПДн в памяти, 152-ФЗ УЗ-1), `simd_lexer.c` (Ахо — Корасик по МКБ-10 / токенам ГРЛС).

## Сборка и проверка
VS Code: `Cmd+Shift+B` (macOS) / `Ctrl+Shift+B` (Windows/Linux), либо в терминале из корня репозитория:

```bash
python3 scripts/build_c_core.py        # соберёт c_core/libcompass_core.so из всех c_core/*.c
python3 scripts/smoke_test_c_core.py   # бенчмарк отклика (Метрика №1, цель < 0.15 мс)
```

Python-мост к ядру: `backend/trajectory_clustering.py` (`RapiraCCoreBridge`) — путь к `.so`
резолвится через `backend/bio_paths.py`, вручную указывать не нужно.
