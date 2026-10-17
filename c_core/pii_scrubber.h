// pii_scrubber.h — Потоковое обезличивание ПДн в памяти (152-ФЗ УЗ-1, ст. 13 323-ФЗ)
// Замена телефонов / СНИЛС / полисов ОМС / e-mail / ФИО на детерминированный
// псевдоним PII_<hmac8>(pepper) ДО записи в СУБД. Медицинские числа не трогает.
#ifndef PII_SCRUBBER_H
#define PII_SCRUBBER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Биты найденных категорий (found_mask)
#define PII_FOUND_NONE   0u
#define PII_FOUND_PHONE  (1u << 0)
#define PII_FOUND_SNILS  (1u << 1)
#define PII_FOUND_OMS    (1u << 2)
#define PII_FOUND_EMAIL  (1u << 3)
#define PII_FOUND_FIO    (1u << 4)

// Обрабатывает NUL-терминированный текст in -> out (UTF-8), возвращая длину записи.
// found_mask (может быть NULL) получает биты PII_FOUND_*.
// Возвращает (size_t)-1, если out_cap не хватило (вызвать повторно с большим буфером).
// Замена детерминирована: одинаковый фрагмент + одинаковый pepper -> одинаковый псевдоним.
size_t pii_scrub_stream(const char* in, char* out, size_t out_cap,
                        const char* pepper, uint32_t* found_mask);

#ifdef __cplusplus
}
#endif

#endif // PII_SCRUBBER_H
