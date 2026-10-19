/*
 * c_core/bitap_ru_lexer.h
 * =======================
 * Нативный C11 нечеткий клинический NER-лексер на базе алгоритма Wu-Manber / Bitap
 * (Shift-Or k-difference на 64-битных регистрах) с каноническим однобайтовым
 * декодированием русского UTF-8 (Case-Folding + склейка Ё/ё -> е) и детекцией
 * контекстных клинических отрицаний.
 *
 * Проект: COMPASS-HYBRID v2.6 (Команда Rapira)
 */

#ifndef COMPASS_BITAP_RU_LEXER_H
#define COMPASS_BITAP_RU_LEXER_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define BITAP_MAX_PATTERNS      64
#define BITAP_MAX_PATTERN_LEN   63
#define BITAP_MAX_ERRORS        2
#define BITAP_MAX_TEXT_CHARS    65536

/* Категории клинических сущностей для правил ОХЛП Авиандра */
#define LEX_CAT_TARGET_DRUG         1  /* Авиандр */
#define LEX_CAT_CONTRAINDICATION    2  /* Абсолютные противопоказания (ИМАО, цирроз Child-Pugh C и др.) */
#define LEX_CAT_CYP2D6_INHIBITOR    3  /* Ингибиторы CYP2D6 (флуоксетин, пароксетин, хинидин, бупропион) */
#define LEX_CAT_CYP3A4_MODULATOR    4  /* Ингибиторы/индукторы CYP3A4 (кетоконазол, карбамазепин, кларитромицин) */
#define LEX_CAT_ADVERSE_SYMPTOM     5  /* Маркеры НЯ / серотонинового синдрома / удлинения QTc */

/* Скомпилированный 64-битный Bitap-автомат для одного клинического паттерна */
typedef struct {
    int32_t rule_id;
    int32_t category_code;
    int32_t pattern_len;
    int32_t max_allowed_errors;
    uint64_t char_masks[256];      /* PM[c]: бит i == 0 <=> pattern[i] == c */
    char canonical_name[64];
    double ddi_ki_ratio_impact;    /* Вклад в [I]/K_i при обнаружении ко-медикамента */
} CompassBitapPattern;

/* Контекст словаря лексера */
typedef struct {
    int32_t num_patterns;
    CompassBitapPattern patterns[BITAP_MAX_PATTERNS];
} CompassBitapDictionary;

/* Найденная клиническая сущность в тексте ЭМК */
typedef struct {
    int32_t rule_id;
    int32_t category_code;
    int32_t edit_distance;         /* 0 = точное совпадение, 1 или 2 = опечатка Левенштейна */
    int32_t is_negated;            /* 1 = сущность под клиническим отрицанием ("отрицает", "без", "нет") */
    uint32_t utf8_byte_start;      /* Начальный байт в исходной UTF-8 строке */
    uint32_t utf8_byte_end;        /* Конечный байт в исходной UTF-8 строке */
    double ddi_ki_ratio_impact;
    char canonical_name[64];
} CompassLexMatch;

/*
 * Инициализирует словарь встроенной базой клинических маркеров ОХЛП Авиандра,
 * ингибиторов CYP2D6/CYP3A4, ИМАО и нежелательных явлений.
 */
void compass_bitap_init_clinical_dictionary(CompassBitapDictionary *dict);

/*
 * Добавляет пользовательский русскоязычный или латинский паттерн в словарь Bitap.
 */
int32_t compass_bitap_add_pattern(
    CompassBitapDictionary *dict,
    const char *utf8_pattern,
    int32_t rule_id,
    int32_t category_code,
    int32_t max_errors,
    double ddi_impact,
    const char *canonical_name
);

/*
 * Сканирует сырой русскоязычный UTF-8 текст ЭМК с помощью 64-битного автомата Wu-Manber,
 * находить сущности с опечатками (k <= 2), проверяет окно отрицаний и записывает
 * дедуплицированные результаты в out_matches. Возвращает число находок.
 */
int32_t compass_bitap_scan_utf8_ehr(
    const CompassBitapDictionary *dict,
    const char *utf8_text,
    size_t utf8_byte_len,
    CompassLexMatch *out_matches,
    int32_t max_matches_capacity
);

#ifdef __cplusplus
}
#endif

#endif /* COMPASS_BITAP_RU_LEXER_H */