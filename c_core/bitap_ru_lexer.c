/*
 * c_core/bitap_ru_lexer.c
 * =======================
 * Реализация 64-битного нечеткого автомата Wu-Manber / Bitap с каноническим
 * декодером русского UTF-8 (Case-Folding А-Я -> а-я, Ё/ё -> е), проверкой
 * границ слов и детектором русских клинических отрицаний.
 */

#include "bitap_ru_lexer.h"
#include <string.h>

/*
 * Каноническая таблица кодирования:
 * ASCII 'a'..'z', '0'..'9' -> сохраняют свои коды (верхний регистр 'A'..'Z' -> 'a'..'z').
 * Русская кириллица UTF-8 (2 байта: 0xD0 0x81..0xBF, 0xD1 0x80..0x91)
 * отображается в однобайтовый диапазон 0x80..0x9F (32 буквы 'а'..'я', где 'ё' -> 'е').
 */
static inline int32_t is_canonical_word_char(uint8_t c) {
    if (c >= 'a' && c <= 'z') return 1;
    if (c >= '0' && c <= '9') return 1;
    if (c >= 0x80u && c <= 0x9Fu) return 1;
    return 0;
}

/*
 * Декодирует UTF-8 строку в однобайтовый канонический массив folded_out
 * и сохраняет точные байтовые смещения каждого символа в utf8_offsets.
 * Возвращает длину свернутой строки в символах.
 */
static size_t decode_and_fold_utf8_ru(
    const char *utf8_in,
    size_t byte_len,
    uint8_t *folded_out,
    uint32_t *utf8_offsets,
    size_t max_chars
) {
    size_t i = 0;
    size_t out_len = 0;

    while (i < byte_len && out_len + 1 < max_chars) {
        uint8_t b0 = (uint8_t)utf8_in[i];
        uint32_t start_byte = (uint32_t)i;

        if (b0 < 0x80u) {
            /* Однобайтовый ASCII */
            uint8_t ch = b0;
            if (ch >= 'A' && ch <= 'Z') {
                ch = (uint8_t)(ch + ('a' - 'A'));
            }
            folded_out[out_len] = ch;
            if (utf8_offsets) utf8_offsets[out_len] = start_byte;
            out_len++;
            i += 1;
        } else if ((b0 == 0xD0u || b0 == 0xD1u) && (i + 1 < byte_len)) {
            /* Двухбайтовая кириллица UTF-8 */
            uint8_t b1 = (uint8_t)utf8_in[i + 1];
            uint8_t mapped = ' ';

            if (b0 == 0xD0u && b1 == 0x81u) {
                /* Заглавная 'Ё' -> каноническая 'е' (индекс 5 -> 0x85) */
                mapped = 0x85u;
            } else if (b0 == 0xD1u && b1 == 0x91u) {
                /* Строчная 'ё' -> каноническая 'е' (индекс 5 -> 0x85) */
                mapped = 0x85u;
            } else if (b0 == 0xD0u && b1 >= 0x90u && b1 <= 0xAFu) {
                /* Заглавные 'А'..'Я' (0x90..0xAF) -> 0x80..0x9F */
                mapped = (uint8_t)(0x80u + (b1 - 0x90u));
            } else if (b0 == 0xD0u && b1 >= 0xB0u && b1 <= 0xBFu) {
                /* Строчные 'а'..'п' (0xB0..0xBF) -> 0x80..0x8F */
                mapped = (uint8_t)(0x80u + (b1 - 0xB0u));
            } else if (b0 == 0xD1u && b1 >= 0x80u && b1 <= 0x8Fu) {
                /* Строчные 'р'..'я' (0x80..0x8F) -> 0x90..0x9F */
                mapped = (uint8_t)(0x90u + (b1 - 0x80u));
            }

            folded_out[out_len] = mapped;
            if (utf8_offsets) utf8_offsets[out_len] = start_byte;
            out_len++;
            i += 2;
        } else {
            /* Прочие многобайтовые UTF-8 символы (пунктуация, спецсимволы) */
            size_t step = 1;
            if ((b0 & 0xE0u) == 0xC0u) step = 2;
            else if ((b0 & 0xF0u) == 0xE0u) step = 3;
            else if ((b0 & 0xF8u) == 0xF0u) step = 4;

            folded_out[out_len] = ' ';
            if (utf8_offsets) utf8_offsets[out_len] = start_byte;
            out_len++;
            i += (i + step <= byte_len) ? step : 1;
        }
    }

    if (utf8_offsets) {
        utf8_offsets[out_len] = (uint32_t)byte_len;
    }
    folded_out[out_len] = 0;
    return out_len;
}

int32_t compass_bitap_add_pattern(
    CompassBitapDictionary *dict,
    const char *utf8_pattern,
    int32_t rule_id,
    int32_t category_code,
    int32_t max_errors,
    double ddi_impact,
    const char *canonical_name
) {
    if (!dict || !utf8_pattern || dict->num_patterns >= BITAP_MAX_PATTERNS) {
        return -1;
    }

    uint8_t folded_pat[128];
    size_t pat_len = decode_and_fold_utf8_ru(
        utf8_pattern, strlen(utf8_pattern), folded_pat, NULL, sizeof(folded_pat)
    );
    if (pat_len == 0 || pat_len > BITAP_MAX_PATTERN_LEN) {
        return -2;
    }

    CompassBitapPattern *p = &dict->patterns[dict->num_patterns];
    p->rule_id             = rule_id;
    p->category_code       = category_code;
    p->pattern_len         = (int32_t)pat_len;
    p->ddi_ki_ratio_impact = ddi_impact;

    /* Защита коротких медицинских аббревиатур от ложных срабатываний:
     * m <= 4 -> только точное совпадение (k=0),
     * 5 <= m <= 7 -> не более 1 ошибки (k<=1),
     * m >= 8 -> до 2 ошибок Левенштейна (k<=2).
     */
    int32_t safe_k = max_errors;
    if (safe_k < 0) safe_k = 0;
    if (safe_k > BITAP_MAX_ERRORS) safe_k = BITAP_MAX_ERRORS;
    if (pat_len <= 4 && safe_k > 0) safe_k = 0;
    else if (pat_len <= 7 && safe_k > 1) safe_k = 1;
    p->max_allowed_errors = safe_k;

    strncpy(p->canonical_name, canonical_name ? canonical_name : utf8_pattern, sizeof(p->canonical_name) - 1);
    p->canonical_name[sizeof(p->canonical_name) - 1] = '\0';

    /* Инициализация битовых масок PM[256] единицами (~0ULL) */
    for (int c = 0; c < 256; ++c) {
        p->char_masks[c] = ~0ULL;
    }
    /* Для каждой позиции i в шаблоне сбрасываем бит i в 0 для символа folded_pat[i] */
    for (size_t i = 0; i < pat_len; ++i) {
        p->char_masks[folded_pat[i]] &= ~(1ULL << i);
    }

    dict->num_patterns++;
    return 0;
}

void compass_bitap_init_clinical_dictionary(CompassBitapDictionary *dict) {
    if (!dict) return;
    memset(dict, 0, sizeof(CompassBitapDictionary));

    /* 1. Целевой препарат */
    compass_bitap_add_pattern(dict, "авиандр", 100, LEX_CAT_TARGET_DRUG, 1, 0.0, "Авиандр (Aviandr)");

    /* 2. Абсолютные противопоказания по ОХЛП (Hard Stops) */
    compass_bitap_add_pattern(dict, "транилципромин", 201, LEX_CAT_CONTRAINDICATION, 2, 0.0, "MAOI: Транилципромин");
    compass_bitap_add_pattern(dict, "фенелзин",       202, LEX_CAT_CONTRAINDICATION, 2, 0.0, "MAOI: Фенелзин");
    compass_bitap_add_pattern(dict, "моклобемид",     203, LEX_CAT_CONTRAINDICATION, 2, 0.0, "MAOI: Моклобемид");
    compass_bitap_add_pattern(dict, "селегилин",      204, LEX_CAT_CONTRAINDICATION, 2, 0.0, "MAOI: Селегилин");
    compass_bitap_add_pattern(dict, "линезолид",      205, LEX_CAT_CONTRAINDICATION, 2, 0.0, "MAOI-like: Линезолид");

    /* 3. Ингибиторы CYP2D6 (влияющие на [I]/K_i в решателе RK45) */
    compass_bitap_add_pattern(dict, "флуоксетин",     301, LEX_CAT_CYP2D6_INHIBITOR, 2, 4.2, "CYP2D6 Strong Inh: Флуоксетин");
    compass_bitap_add_pattern(dict, "пароксетин",     302, LEX_CAT_CYP2D6_INHIBITOR, 2, 4.8, "CYP2D6 Strong Inh: Пароксетин");
    compass_bitap_add_pattern(dict, "бупропион",      303, LEX_CAT_CYP2D6_INHIBITOR, 2, 2.9, "CYP2D6 Strong Inh: Бупропион");
    compass_bitap_add_pattern(dict, "хинидин",        304, LEX_CAT_CYP2D6_INHIBITOR, 1, 5.5, "CYP2D6 Strong Inh: Хинидин");
    compass_bitap_add_pattern(dict, "дулоксетин",     305, LEX_CAT_CYP2D6_INHIBITOR, 2, 1.8, "CYP2D6 Mod Inh: Дулоксетин");
    /* Слабые ингибиторы и субстрат CYP2D6: верифицированно безопасно (РКИ),
     * вклад в DDI нулевой/минимальный — нужны для NER и клинических фенотипов */
    compass_bitap_add_pattern(dict, "метопролол",     306, LEX_CAT_CYP2D6_INHIBITOR, 2, 0.0, "Метопролол (РКИ: без DDI)");
    compass_bitap_add_pattern(dict, "эсциталопрам",   307, LEX_CAT_CYP2D6_INHIBITOR, 2, 0.3, "Эсциталопрам (СИОЗС)");
    compass_bitap_add_pattern(dict, "сертралин",      308, LEX_CAT_CYP2D6_INHIBITOR, 2, 0.4, "Сертралин (СИОЗС)");

    /* 4. Модуляторы CYP3A4 */
    compass_bitap_add_pattern(dict, "кетоконазол",    401, LEX_CAT_CYP3A4_MODULATOR, 2, 6.0, "CYP3A4 Strong Inh: Кетоконазол");
    compass_bitap_add_pattern(dict, "кларитромицин",  402, LEX_CAT_CYP3A4_MODULATOR, 2, 3.8, "CYP3A4 Strong Inh: Кларитромицин");
    compass_bitap_add_pattern(dict, "итраконазол",    403, LEX_CAT_CYP3A4_MODULATOR, 2, 5.2, "CYP3A4 Strong Inh: Итраконазол");
    compass_bitap_add_pattern(dict, "карбамазепин",   404, LEX_CAT_CYP3A4_MODULATOR, 2, -0.6, "CYP3A4 Inducer: Карбамазепин");
    /* Флуконазол — ингибитор CYP2C19: клинически значимых изменений PK маритупирдина
     * и М1 не выявлено (РКИ) — вклад нулевой; бензодиазепины — маркер седативного риска */
    compass_bitap_add_pattern(dict, "флуконазол",     405, LEX_CAT_CYP3A4_MODULATOR, 2, 0.0, "Флуконазол (РКИ: без DDI)");
    compass_bitap_add_pattern(dict, "феназепам",      406, LEX_CAT_CYP3A4_MODULATOR, 2, 0.0, "Феназепам (БДЗ)");
    compass_bitap_add_pattern(dict, "алпразолам",     407, LEX_CAT_CYP3A4_MODULATOR, 2, 0.0, "Алпразолам (БДЗ)");

    /* 5. Клинические маркеры токсичности / нежелательных явлений */
    compass_bitap_add_pattern(dict, "серотониновый",  501, LEX_CAT_ADVERSE_SYMPTOM,  2, 0.0, "AE: Серотониновый синдром");
    compass_bitap_add_pattern(dict, "акатизия",       502, LEX_CAT_ADVERSE_SYMPTOM,  2, 0.0, "AE: Акатизия");
    compass_bitap_add_pattern(dict, "брадикардия",    503, LEX_CAT_ADVERSE_SYMPTOM,  2, 0.0, "AE: Брадикардия");
}

/*
 * Проверяет окно [win_start .. win_end) в свернутом тексте на наличие
 * русских клинических маркеров отрицания или отмены препарата:
 * "нет ", "без ", "отрица", "отмен", "исключ", "не приним"
 */
static int32_t check_russian_negation_window(
    const uint8_t *folded_text,
    size_t text_len,
    size_t match_start,
    size_t match_end
) {
    static const char *neg_stems[] = {
        "нет",
        "без",
        "отрица",
        "отмен",
        "исключ",
        "не приним",
        "не выявл"
    };
    const int num_stems = 7;

    uint8_t folded_stems[7][24];
    size_t stem_lens[7];
    for (int s = 0; s < num_stems; ++s) {
        stem_lens[s] = decode_and_fold_utf8_ru(
            neg_stems[s], strlen(neg_stems[s]), folded_stems[s], NULL, 24
        );
    }

    // Правый lookahead 96 байт: русская клиническая норма «Приём A и B категорически
    // отрицает» ставит отрицание ПОСЛЕ списка препаратов (от конца слова до «отрица»
    // бывает ~50 байт). Зажим по границе предложения ниже не даёт окну перелезть
    // на соседние фразы.
    size_t win_left  = (match_start > 36) ? (match_start - 36) : 0;
    size_t win_right = (match_end + 96 < text_len) ? (match_end + 96) : text_len;

    /* Ограничиваем окно текущей фразой (до точки или точки с запятой) */
    for (size_t pos = match_start; pos > win_left; --pos) {
        uint8_t c = folded_text[pos - 1];
        if (c == '.' || c == ';' || c == '\n') {
            win_left = pos;
            break;
        }
    }
    for (size_t pos = match_end; pos < win_right; ++pos) {
        uint8_t c = folded_text[pos];
        if (c == '.' || c == ';' || c == '\n') {
            win_right = pos;
            break;
        }
    }

    for (int s = 0; s < num_stems; ++s) {
        size_t slen = stem_lens[s];
        if (slen == 0 || win_right < win_left + slen) continue;

        for (size_t pos = win_left; pos + slen <= win_right; ++pos) {
            /* Проверяем левую границу слова для коротких отрицаний ("нет", "без") */
            if (pos > 0 && is_canonical_word_char(folded_text[pos - 1])) {
                continue;
            }
            if (memcmp(folded_text + pos, folded_stems[s], slen) == 0) {
                /* Для коротких слов "нет" и "без" требуем правую границу слова */
                if (slen <= 3 && (pos + slen < text_len) && is_canonical_word_char(folded_text[pos + slen])) {
                    continue;
                }
                return 1;
            }
        }
    }
    return 0;
}

int32_t compass_bitap_scan_utf8_ehr(
    const CompassBitapDictionary *dict,
    const char *utf8_text,
    size_t utf8_byte_len,
    CompassLexMatch *out_matches,
    int32_t max_matches_capacity
) {
    if (!dict || !utf8_text || !out_matches || max_matches_capacity <= 0) {
        return 0;
    }

    static uint8_t folded_buf[BITAP_MAX_TEXT_CHARS];
    static uint32_t offset_buf[BITAP_MAX_TEXT_CHARS + 1];

    size_t n_chars = decode_and_fold_utf8_ru(
        utf8_text, utf8_byte_len, folded_buf, offset_buf, BITAP_MAX_TEXT_CHARS
    );
    if (n_chars == 0) {
        return 0;
    }

    int32_t total_found = 0;

    for (int32_t p_idx = 0; p_idx < dict->num_patterns; ++p_idx) {
        const CompassBitapPattern *pat = &dict->patterns[p_idx];
        int32_t m = pat->pattern_len;
        uint64_t accept_bit = 1ULL << (m - 1);

        /* Инициализация регистров состояния Wu-Manber R[0..k_max].
         * memset(0xFF) — тождество алгоритма («все единицы») И защита от gcc
         * -Wmaybe-uninitialized: кламп k_max гарантирует отсутствие выхода за R. */
        uint64_t R[BITAP_MAX_ERRORS + 1];
        memset(R, 0xFF, sizeof(R));
        int32_t k_max = pat->max_allowed_errors;
        if (k_max > BITAP_MAX_ERRORS) k_max = BITAP_MAX_ERRORS;
        for (int32_t d = 0; d <= k_max; ++d) {
            R[d] = (~0ULL) << d;
        }

        for (size_t pos = 0; pos < n_chars; ++pos) {
            uint8_t ch = folded_buf[pos];
            uint64_t pm = pat->char_masks[ch];

            uint64_t old_R0 = R[0];
            R[0] = ((R[0] << 1) & (~1ULL)) | pm;

            for (int32_t d = 1; d <= k_max; ++d) {
                uint64_t old_Rd = R[d];
                uint64_t match_step = ((old_Rd << 1) & (~1ULL)) | pm;
                uint64_t ins_step   = old_R0;
                uint64_t del_step   = (R[d - 1] << 1) & (~1ULL);
                uint64_t sub_step   = (old_R0 << 1) & (~1ULL);

                R[d] = match_step & ins_step & del_step & sub_step;
                old_R0 = old_Rd;
            }

            /* Проверяем, сработал ли бит конца паттерна (m - 1) на уровне ошибок d = 0..k_max */
            int32_t matched_d = -1;
            for (int32_t d = 0; d <= k_max; ++d) {
                if ((R[d] & accept_bit) == 0ULL) {
                    matched_d = d;
                    break;
                }
            }

            if (matched_d >= 0) {
                /* Вычисляем границы слова в свернутом тексте */
                size_t word_end = pos + 1;
                while (word_end < n_chars && is_canonical_word_char(folded_buf[word_end])) {
                    word_end++;
                }
                size_t word_start = pos;
                while (word_start > 0 && is_canonical_word_char(folded_buf[word_start - 1])) {
                    word_start--;
                }

                /* Фильтр ложных внутрисловных срабатываний:
                 * длина реального слова в тексте должна отличаться от длины паттерна не более чем на k_max + 3
                 * (с учетом русских падежных окончаний: -а, -ом, -у, -ин -> -ина)
                 */
                size_t word_len = word_end - word_start;
                if (word_len + (size_t)k_max < (size_t)m || word_len > (size_t)(m + k_max + 3)) {
                    continue;
                }

                uint32_t b_start = offset_buf[word_start];
                uint32_t b_end   = offset_buf[word_end];

                /* Non-Maximum Suppression (NMS): если предыдущее срабатывание того же правила
                 * пересекается с текущим словом, оставляем вариант с меньшим edit_distance
                 */
                if (total_found > 0) {
                    CompassLexMatch *prev = &out_matches[total_found - 1];
                    if (prev->rule_id == pat->rule_id && b_start <= prev->utf8_byte_end + 2u) {
                        if (matched_d < prev->edit_distance) {
                            prev->edit_distance  = matched_d;
                            prev->utf8_byte_start = b_start;
                            prev->utf8_byte_end   = b_end;
                        }
                        continue;
                    }
                }

                if (total_found < max_matches_capacity) {
                    CompassLexMatch *cur = &out_matches[total_found++];
                    cur->rule_id             = pat->rule_id;
                    cur->category_code       = pat->category_code;
                    cur->edit_distance       = matched_d;
                    cur->utf8_byte_start     = b_start;
                    cur->utf8_byte_end       = b_end;
                    cur->ddi_ki_ratio_impact = pat->ddi_ki_ratio_impact;
                    cur->is_negated          = check_russian_negation_window(
                        folded_buf, n_chars, word_start, word_end
                    );
                    strncpy(cur->canonical_name, pat->canonical_name, sizeof(cur->canonical_name) - 1);
                    cur->canonical_name[sizeof(cur->canonical_name) - 1] = '\0';
                }
            }
        }
    }

    return total_found;
}