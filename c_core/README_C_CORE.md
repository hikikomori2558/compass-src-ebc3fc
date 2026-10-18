# Нативное ядро C-Core (libcompass_core.so)

## Состав (18.10.2026)
- **Готово:** `compass_core.h/.c` (Михаил: СКФ, Чайлд-Пью, РК4 168 ч, DDI, маски, voice DSP), `pii_scrubber.h/.c` + `simd_lexer.h/.c` (Всеволод: HMAC-обезличивание ПДн ~0.5 ГБ/с, Ахо—Корасик по 85 мед. словам).
- Скомпилированный `libcompass_core.so` в git **не** коммитится (см. `.gitignore`) — собирается локально на каждой машине.

## План по новым модулям
- Михаил: `pk_cyp2d6_solver.c`, `ohpl_hardstops.c`, `voice_dsp.c` (выделение из монолита — по мере времени).
- Всеволод: `pii_scrubber.c` и `simd_lexer.c` — **готовы**; из нового — только поддержка.

## Сборка и проверка
VS Code: `Cmd+Shift+B` (macOS) / `Ctrl+Shift+B` (Windows/Linux), либо в терминале из корня репозитория:

```bash
python3 scripts/build_c_core.py        # соберёт c_core/libcompass_core.so из всех c_core/*.c
python3 scripts/smoke_test_c_core.py   # бенчмарк отклика (Метрика №1, цель < 0.15 мс)
```

Python-мост к ядру: `backend/trajectory_clustering.py` (`RapiraCCoreBridge`) — путь к `.so`
резолвится через `backend/bio_paths.py`, вручную указывать не нужно.
