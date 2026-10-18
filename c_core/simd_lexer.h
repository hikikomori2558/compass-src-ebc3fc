// simd_lexer.h — сверхбыстрая предразметка медицинских сущностей в тексте ЭМК.
//
// Зачем: Python-парсер гоняет regex по каждому тексту; словарные сущности (МКБ-10,
// МНН, шкалы, врачебные сокращения) ищутся automатом Ахо—Корасика за один проход
// независимо от размера словаря. Дальше парсер валидирует контекст — лексер только
// отдаёт кандидатов с позициями, ничего не «понимая».
//
// Договорённости:
//  * вход — lowercase UTF-8 (потребитель делает .lower() заранее; ASCII-регистр
//    сворачивается на лету, кириллицу сворачивает вызывающая сторона);
//  * словарь — единственный источник истины, живёт прямо здесь (kDict/kCat):
//    Python-мост читает его через lexer_dict_* и никогда не дублирует;
//  * лексер находит подстроки; границы слов проверяет потребитель (для
//    предразметки это осознанная цена скорости).
#ifndef SIMD_LEXER_H
#define SIMD_LEXER_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LEX_CAT_ICD10 1   // коды МКБ-10 и их префиксы
#define LEX_CAT_DRUG  2   // МНН и торговые названия
#define LEX_CAT_SCALE 3   // психометрические шкалы
#define LEX_CAT_ABBR  4   // врачебные сокращения и лабораторные маркеры

typedef struct {
    uint32_t start;    // байт-смещение от начала текста
    uint16_t length;   // длина в байтах
    uint16_t dict_id;  // индекс в словаре (lexer_dict_word/cat по нему)
} lexeme_t;

// Строит автомат (идемпотентно; вызвать до scan). 0 — успех.
int lexer_init(void);

// Один проход по тексту. Возвращает число совпадений или (size_t)-1, если out_cap
// мало (вызови снова с большим буфером; содержимое out после -1 не определено).
size_t lexer_scan(const char* restrict utf8_low, lexeme_t* restrict out, size_t out_cap);

// Зеркало словаря для Python-моста: одна копия правды — здесь, в C.
uint32_t    lexer_dict_size(void);
const char* lexer_dict_word(uint32_t dict_id);
uint8_t     lexer_dict_cat(uint32_t dict_id);

#ifdef __cplusplus
}
#endif

#endif // SIMD_LEXER_H
