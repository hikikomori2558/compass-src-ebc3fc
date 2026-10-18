// simd_lexer.c — Ахо—Корасик по медицинскому словарю (Разработчик №1, день 5, блок 3).
//
// Имя «simd_lexer» — из архитектурной документации; честно: внутри классический
// байтовый автомат, векторализация оставлена компилятору (-O3). Этого достаточно:
// 3 КБ выписки сканируются единицами микросекунд, а словарь растёт без цены.
//
// Автомат: goto[state][байт] — плотная таблица uint16 (состояний ~1.5К, таблица
// ~750 КБ — копейки против пользы: переход за O(1) без хешей). fail[] — суффиксные
// ссылки, вывод — цепочка dict_id через emit_next (одно состояние может закрывать
// несколько слов-суффиксов: «флуоксетин» и «оксетин», если бы оба были в словаре).
#include <stdlib.h>
#include <string.h>

#include "simd_lexer.h"

/* ----------------------------- словарь (одна правда) ----------------------------- */
#define D(w, c) w, c
static const char* const kDict[] = {
    // МКБ-10: коды кейса и частых коморбидных состояний (строчные, как в тексте)
    "f41.1", "f43.2", "f32.2", "f41.0", "f33.", "f34.1", "f40.", "f45.", "f48.0",
    "f06.4", "f07.", "r45.7", "z60.0", "e05.", "e03.", "i10", "n18.",
    // МНН и торговые названия (инструкция Авиандра + КР Минздрава)
    "маритупирдин", "авиандр", "метопролол", "дулоксетин", "ципрофлоксацин",
    "флуконазол", "пароксетин", "флуоксетин", "сертралин", "амитриптилин",
    "трамадол", "суматриптан", "золмитриптан", "хинидин", "кетоконазол",
    "гидроксихлорохин", "дифенгидрамин", "тербинафин", "фенелзин",
    "эсциталопрам", "венлафаксин", "гидроксизин", "буспирон", "зверобой",
    // Шкалы (латиницей и по-русски, с дефисами и без)
    "phq-9", "phq9", "gad-7", "gad7", "гтр-7", "c-ssrs", "cssrs", "mdq",
    "moca", "коп-25", "hads", "hads-a", "hads-d", "sigh-a", "madrs",
    // Сокращения и лабмаркеры выписок
    "жал.", "креат.", "креатинин", "трев.", "инсомн", "астени", "б/х",
    "скф", "кфк", "ттг", "т4", "мно", "алт", "аст", "чсс", "хбп", "мкб",
    "пиелонефрит", "цирроз", "эпилепс", "беремен", "лактаз", "лактоз",
    "непереносимост", "преследован", "галлюцин", "оклики", "гипомани",
    "азол",   // антимикотики-азолы; специально: СУФФИКС «флуконазол»/«кетоконазол» —
              // живой регресс-тест наследования вывода суффиксов в автомате
};
// Категории — 1:1 к kDict. Раскладка: ICD10=17, DRUG=24, SCALE=15, ABBR=29 (итого 85).
// Расхождение ловит lexer_init (-2): параллельные массивы не должны «тихо съезжать».
static const uint8_t kCat[] = {
    LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10,
    LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10,
    LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_ICD10,
    LEX_CAT_ICD10, LEX_CAT_ICD10, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG,
    LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG,
    LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG,
    LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG,
    LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG, LEX_CAT_DRUG,
    LEX_CAT_DRUG, LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE,
    LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE,
    LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE, LEX_CAT_SCALE,
    LEX_CAT_SCALE, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR,
    LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR,
    LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR,
    LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR,
    LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR,
    LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR, LEX_CAT_ABBR,
};

#define DICT_SIZE ((uint32_t)(sizeof(kDict) / sizeof(kDict[0])))

/* ----------------------------- автомат ----------------------------- */
typedef struct {
    uint16_t* goto_row;     // 256 uint16 на состояние
    uint16_t  fail;
    int32_t   emit;         // dict_id первого слова, заканчивающегося здесь, или -1
} node_t;

static node_t*  nodes   = NULL;
static uint32_t n_nodes = 0;
static int32_t* emit_next = NULL;   // цепочка слов-суффиксов: emit_next[dict_id] -> следующий
static uint16_t* dict_len = NULL;   // длины слов: strlen не в горячем цикле сканирования
static int      built   = 0;

// Байтовый регистр: ASCII-латиницу сворачиваем; остальное (кириллица UTF-8,
// цифры, дефисы, точки) идёт как есть — словарь уже в нижнем регистре.
static uint8_t fold(uint8_t b) {
    if (b >= 'A' && b <= 'Z') return (uint8_t)(b + 32);
    return b;
}

int lexer_init(void) {
    if (built) return 0;
    // Параллельные массивы обязаны совпадать по длине: расхождение — ошибка сборки
    // словаря, а не повод для тихого сдвига категорий.
    if (sizeof(kCat) != DICT_SIZE) return -2;

    // Верхняя оценка состояний: сумма длин слов + корень
    size_t cap = 1;
    for (uint32_t i = 0; i < DICT_SIZE; ++i) cap += strlen(kDict[i]);

    nodes = (node_t*)calloc(cap, sizeof(node_t));
    emit_next = (int32_t*)malloc(DICT_SIZE * sizeof(int32_t));
    dict_len = (uint16_t*)malloc(DICT_SIZE * sizeof(uint16_t));
    if (!nodes || !emit_next || !dict_len) { free(nodes); free(emit_next); free(dict_len); return -1; }
    for (uint32_t i = 0; i < DICT_SIZE; ++i) {
        emit_next[i] = -1;
        dict_len[i] = (uint16_t)strlen(kDict[i]);
    }

    // 1) trie
    n_nodes = 1;
    nodes[0].goto_row = (uint16_t*)calloc(256, sizeof(uint16_t)); // нули = нет перехода
    nodes[0].fail = 0;
    nodes[0].emit = -1;
    if (!nodes[0].goto_row) return -1;

    for (uint32_t w = 0; w < DICT_SIZE; ++w) {
        uint16_t cur = 0;
        for (const char* p = kDict[w]; *p; ++p) {
            uint8_t b = fold((uint8_t)*p);
            if (nodes[cur].goto_row[b] == 0) {
                nodes[n_nodes].goto_row = (uint16_t*)calloc(256, sizeof(uint16_t));
                if (!nodes[n_nodes].goto_row) return -1;
                nodes[n_nodes].fail = 0;
                nodes[n_nodes].emit = -1;
                nodes[cur].goto_row[b] = (uint16_t)n_nodes;
                cur = (uint16_t)n_nodes++;
            } else {
                cur = nodes[cur].goto_row[b];
            }
        }
        // слово заканчивается в cur: добавляем в начало цепочки emit этого состояния
        emit_next[w] = nodes[cur].emit;
        nodes[cur].emit = (int32_t)w;
    }

    // 2) fail-ссылки BFS: классика — переход по b из s: если нет прямого, идём по fail
    uint16_t* queue = (uint16_t*)malloc(n_nodes * sizeof(uint16_t));
    if (!queue) return -1;
    size_t qh = 0, qt = 0;
    for (int b = 0; b < 256; ++b) {
        uint16_t t = nodes[0].goto_row[b];
        if (t) { nodes[t].fail = 0; queue[qt++] = t; }
    }
    while (qh < qt) {
        uint16_t s = queue[qh++];
        for (int b = 0; b < 256; ++b) {
            uint16_t t = nodes[s].goto_row[b];
            if (!t) continue;
            // fail(t): суффикс-ссылка — идём от fail(s), пока есть куда, иначе корень.
            // goto(f,b) не может оказаться самим t: f строго мельче s, а t = depth(s)+1.
            uint16_t f = nodes[s].fail;
            while (f != 0 && nodes[f].goto_row[b] == 0) f = nodes[f].fail;
            nodes[t].fail = nodes[f].goto_row[b] ? nodes[f].goto_row[b] : 0;

            // Наследуем вывод суффикса: слова, заканчивающиеся в fail(t), тоже матчатся
            // в этой позиции. Цепочку суффикса приклеиваем В ХВОСТ собственной цепочки t
            // (не только при пустом emit!): иначе слово-суффикс терялось бы, когда в t
            // уже заканчивается собственное слово. fail(t) строго мельче — к этому моменту
            // его цепь уже финализирована (BFS идёт по неубыванию глубины).
            {
                int32_t tail = nodes[t].emit;
                if (tail < 0) {
                    nodes[t].emit = nodes[nodes[t].fail].emit;
                } else {
                    while (emit_next[tail] >= 0) tail = emit_next[tail];
                    emit_next[tail] = nodes[nodes[t].fail].emit;
                }
            }
            queue[qt++] = t;
        }
    }
    free(queue);

    built = 1;
    return 0;
}

size_t lexer_scan(const char* restrict utf8_low, lexeme_t* restrict out, size_t out_cap) {
    if (!built) return (size_t)-1;
    size_t n = 0, i = 0;
    uint16_t cur = 0;
    for (const char* p = utf8_low; *p; ++p, ++i) {
        uint8_t b = fold((uint8_t)*p);
        while (cur != 0 && nodes[cur].goto_row[b] == 0) cur = nodes[cur].fail;
        cur = nodes[cur].goto_row[b];

        // все слова, заканчивающиеся в этой позиции (цепочка собственных + суффиксных)
        for (int32_t e = nodes[cur].emit; e >= 0; e = emit_next[e]) {
            if (n == out_cap) return (size_t)-1;
            out[n].start = (uint32_t)(i + 1 - dict_len[e]);
            out[n].length = dict_len[e];
            out[n].dict_id = (uint16_t)e;
            ++n;
        }
    }
    return n;
}

uint32_t    lexer_dict_size(void)        { return DICT_SIZE; }
const char* lexer_dict_word(uint32_t id) { return id < DICT_SIZE ? kDict[id] : NULL; }
uint8_t     lexer_dict_cat(uint32_t id)  { return id < DICT_SIZE ? kCat[id] : 0; }
