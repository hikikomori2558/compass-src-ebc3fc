// pii_scrubber.h — обезличивание персональных данных прямо в памяти, до записи в БД.
// Зачем это нужно: 152-ФЗ и врачебная тайна (ст. 13 323-ФЗ) запрещают тащить ФИО/телефоны
// в клинический контур, поэтому сырой текст ЭМК проходит через этот фильтр первым.
//
// Что ловим: телефоны (+7/8, любые расстановки скобок и дефисов), СНИЛС, полис ОМС,
// e-mail и ФИО («Иванов И.И.» и «Иванов Иван Иванович»).
// Что НЕ трогаем: медицинские числа — креатинин, СКФ, дозировки, даты. Это принципиально:
// после обезличивания парсер clinical_data должен видеть ровно те же лабы, что и до него.
//
// Замена детерминированная: один и тот же фрагмент при одном и том же перце всегда
// превращается в одинаковый псевдоним PII_xxxxxxxx (первые 8 hex-символов HMAC-SHA256).
// Псевдоним ничего не раскрывает, но позволяет склеивать записи одного пациента.
#ifndef PII_SCRUBBER_H
#define PII_SCRUBBER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Какие категории встретились в тексте (битовая маска, возвращается через found_mask).
#define PII_FOUND_NONE   0u
#define PII_FOUND_PHONE  (1u << 0)
#define PII_FOUND_SNILS  (1u << 1)
#define PII_FOUND_OMS    (1u << 2)
#define PII_FOUND_EMAIL  (1u << 3)
#define PII_FOUND_FIO    (1u << 4)

// Скраббер с запечённым перцем: HMAC-состояния для ipad/opad считаются один раз
// при создании, а не на каждом совпадении. Для потока текстов это в два раза
// меньше SHA-256-компрессий на каждый найденный фрагмент.
typedef struct pii_scrubber pii_scrubber;

pii_scrubber* pii_scrubber_new(const char* pepper);   // NULL, если перец пустой
void          pii_scrubber_free(pii_scrubber* s);

// Основной вход. in — NUL-терминированный UTF-8. Возвращает число записанных в out байт,
// (size_t)-1 — если out_cap маловато (вызывай снова с буфером побольше).
size_t pii_scrubber_scrub(const pii_scrubber* s, const char* in, char* out,
                          size_t out_cap, uint32_t* found_mask);

// Совместимость со старым вызовом: тот же результат, но перец и HMAC-состояния
// готовятся на каждый вызов. Оставлен для одноразовых сценариев и A/B-проверок.
size_t pii_scrub_stream(const char* in, char* out, size_t out_cap,
                        const char* pepper, uint32_t* found_mask);

#ifdef __cplusplus
}
#endif

#endif // PII_SCRUBBER_H
