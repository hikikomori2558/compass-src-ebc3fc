// pii_scrubber.c — потоковое обезличивание ПДн (Разработчик №1, контур безопасности).
//
// Как это устроено. Текст проходится ровно один раз, без аллокаций и без libc-регулярок:
// на каждой позиции сначала дёшево проверяем «может ли здесь вообще начинаться шаблон»
// (таблица can_start + прыжки через двухбайтовую строчную кириллицу), и только для
// кандидатов запускаем точные матчеры. Всё, что между кандидатами, копируется в выход
// одним memcpy. Шифт тут не нужен — HMAC-SHA256 с перцем из окружения даёт стойкий
// детерминированный псевдоним, которого достаточно для Zero-PII-контура.
#include <stdlib.h>
#include <string.h>

#include "pii_scrubber.h"

/* ============================ SHA-256 (FIPS 180-4) ============================ */
/* Своя реализация, а не OpenSSL: ноль внешних зависимостей у ядра, предсказуемое
 * поведение и та же производительность на блоках по 64 байта. */

typedef struct {
    uint32_t h[8];
    uint64_t len;          // сколько байт всего проглочено (включая запечённые паддинги)
    unsigned char buf[64];
    size_t buf_len;
} sha256_ctx;

static const uint32_t K256[64] = {
    0x428a2f98u, 0x71374491u, 0xb5c0fbcfu, 0xe9b5dba5u, 0x3956c25bu, 0x59f111f1u, 0x923f82a4u, 0xab1c5ed5u,
    0xd807aa98u, 0x12835b01u, 0x243185beu, 0x550c7dc3u, 0x72be5d74u, 0x80deb1feu, 0x9bdc06a7u, 0xc19bf174u,
    0xe49b69c1u, 0xefbe4786u, 0x0fc19dc6u, 0x240ca1ccu, 0x2de92c6fu, 0x4a7484aau, 0x5cb0a9dcu, 0x76f988dau,
    0x983e5152u, 0xa831c66du, 0xb00327c8u, 0xbf597fc7u, 0xc6e00bf3u, 0xd5a79147u, 0x06ca6351u, 0x14292967u,
    0x27b70a85u, 0x2e1b2138u, 0x4d2c6dfcu, 0x53380d13u, 0x650a7354u, 0x766a0abbu, 0x81c2c92eu, 0x92722c85u,
    0xa2bfe8a1u, 0xa81a664bu, 0xc24b8b70u, 0xc76c51a3u, 0xd192e819u, 0xd6990624u, 0xf40e3585u, 0x106aa070u,
    0x19a4c116u, 0x1e376c08u, 0x2748774cu, 0x34b0bcb5u, 0x391c0cb3u, 0x4ed8aa4au, 0x5b9cca4fu, 0x682e6ff3u,
    0x748f82eeu, 0x78a5636fu, 0x84c87814u, 0x8cc70208u, 0x90befffau, 0xa4506cebu, 0xbef9a3f7u, 0xc67178f2u
};

static uint32_t rotr32(uint32_t x, int n) { return (x >> n) | (x << (32 - n)); }

static void sha256_compress(sha256_ctx* c, const unsigned char* p) {
    uint32_t w[64];
    uint32_t a = c->h[0], b = c->h[1], cc = c->h[2], d = c->h[3];
    uint32_t e = c->h[4], f = c->h[5], g = c->h[6], h = c->h[7];
    int i;
    for (i = 0; i < 16; i++)
        w[i] = ((uint32_t)p[i * 4] << 24) | ((uint32_t)p[i * 4 + 1] << 16) |
               ((uint32_t)p[i * 4 + 2] << 8) | (uint32_t)p[i * 4 + 3];
    for (i = 16; i < 64; i++) {
        uint32_t s0 = rotr32(w[i - 15], 7) ^ rotr32(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr32(w[i - 2], 17) ^ rotr32(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    for (i = 0; i < 64; i++) {
        uint32_t t1 = h + (rotr32(e, 6) ^ rotr32(e, 11) ^ rotr32(e, 25)) + ((e & f) ^ (~e & g)) + K256[i] + w[i];
        uint32_t t2 = (rotr32(a, 2) ^ rotr32(a, 13) ^ rotr32(a, 22)) + ((a & b) ^ (a & cc) ^ (b & cc));
        h = g; g = f; f = e; e = d + t1; d = cc; cc = b; b = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f; c->h[6] += g; c->h[7] += h;
}

static void sha256_init(sha256_ctx* c) {
    static const uint32_t iv[8] = { 0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                                    0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u };
    memcpy(c->h, iv, sizeof(iv));
    c->len = 0;
    c->buf_len = 0;
}

static void sha256_update(sha256_ctx* c, const unsigned char* d, size_t n) {
    c->len += n;
    while (n) {
        size_t take = 64 - c->buf_len;
        if (take > n) take = n;
        memcpy(c->buf + c->buf_len, d, take);
        c->buf_len += take;
        d += take;
        n -= take;
        if (c->buf_len == 64) { sha256_compress(c, c->buf); c->buf_len = 0; }
    }
}

static void sha256_final(sha256_ctx* c, unsigned char out[32]) {
    const uint64_t bits = c->len * 8;
    unsigned char tail = 0x80, zero = 0x00, lenb[8];
    int i;
    sha256_update(c, &tail, 1);
    while (c->buf_len != 56) sha256_update(c, &zero, 1);
    for (i = 0; i < 8; i++) lenb[i] = (unsigned char)(bits >> (56 - i * 8));
    sha256_update(c, lenb, 8);
    for (i = 0; i < 8; i++) {
        out[i * 4]     = (unsigned char)(c->h[i] >> 24);
        out[i * 4 + 1] = (unsigned char)(c->h[i] >> 16);
        out[i * 4 + 2] = (unsigned char)(c->h[i] >> 8);
        out[i * 4 + 3] = (unsigned char)(c->h[i]);
    }
}

/* ===================== Скраббер: состояние и конструктор ===================== */

struct pii_scrubber {
    uint32_t ipad_h[8];    // состояние SHA-256 после блока key^0x36 — готово к до-hash'у сообщения
    uint32_t opad_h[8];    // то же для key^0x5c — готово к до-hash'у внутреннего digest
    unsigned char can_start[256]; // 1, если ASCII-байт может начинать хоть какой-то шаблон
};

// Считает «середину состояния»: результат проглатывания ровно одного 64-байтового блока.
static void sha256_midstate(const unsigned char block[64], uint32_t h_out[8]) {
    sha256_ctx c;
    sha256_init(&c);
    sha256_compress(&c, block);
    memcpy(h_out, c.h, sizeof(c.h));
}

static void hmac_keys_from_pepper(const char* pepper, uint32_t ipad_h[8], uint32_t opad_h[8]) {
    unsigned char k[64], ipad[64], opad[64], kh[32];
    size_t plen = strlen(pepper), i;

    // Классика HMAC: ключ длиннее блока сначала хешируется, короче — добивается нулями.
    memset(k, 0, sizeof(k));
    if (plen > 64) {
        sha256_ctx c;
        sha256_init(&c);
        sha256_update(&c, (const unsigned char*)pepper, plen);
        sha256_final(&c, kh);
        memcpy(k, kh, 32);
    } else if (plen) {
        memcpy(k, pepper, plen);
    }
    for (i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5c; }
    sha256_midstate(ipad, ipad_h);
    sha256_midstate(opad, opad_h);
}

// HMAC с готовыми серединами состояний: вместо 4 компрессий на фрагмент — всего 2.
static void hmac_sha256_cached(const uint32_t ipad_h[8], const uint32_t opad_h[8],
                               const unsigned char* msg, size_t msglen, unsigned char out[32]) {
    sha256_ctx c;
    unsigned char inner[32];
    memcpy(c.h, ipad_h, sizeof(c.h));
    c.len = 64;      // блок с паддингом уже учтён в середине
    c.buf_len = 0;
    sha256_update(&c, msg, msglen);
    sha256_final(&c, inner);
    memcpy(c.h, opad_h, sizeof(c.h));
    c.len = 64;
    c.buf_len = 0;
    sha256_update(&c, inner, 32);
    sha256_final(&c, out);
}

pii_scrubber* pii_scrubber_new(const char* pepper) {
    struct pii_scrubber* s;
    int b;
    if (!pepper || !*pepper) return NULL;
    s = (struct pii_scrubber*)calloc(1, sizeof(*s));
    if (!s) return NULL;

    hmac_keys_from_pepper(pepper, s->ipad_h, s->opad_h);

    // Шаблон может начать: цифра, '+', латиница и e-mail-спецсимволы.
    // Всё остальное ASCII (пробелы, пунктуация, разметка) пролетает мимо за одну проверку.
    memset(s->can_start, 0, sizeof(s->can_start));
    for (b = '0'; b <= '9'; b++) s->can_start[b] = 1;
    for (b = 'A'; b <= 'Z'; b++) s->can_start[b] = 1;
    for (b = 'a'; b <= 'z'; b++) s->can_start[b] = 1;
    s->can_start[(unsigned char)'+'] = 1;
    s->can_start[(unsigned char)'.'] = 1;
    s->can_start[(unsigned char)'_'] = 1;
    s->can_start[(unsigned char)'%'] = 1;
    s->can_start[(unsigned char)'-'] = 1;
    return s;
}

void pii_scrubber_free(pii_scrubber* s) { free(s); }

/* ============================ Матчеры шаблонов ============================ */

static int is_digit(char ch) { return ch >= '0' && ch <= '9'; }
static int is_lat_alnum(char ch) {
    return is_digit(ch) || (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
}
static int is_lat_special(char ch) {
    return ch == '.' || ch == '_' || ch == '%' || ch == '+' || ch == '-';
}

// Кириллица в UTF-8 двухбайтная. Заглавные живут в D0 90..AF плюс одинокая Ё (D0 81);
// строчные — D0 B0..BF и всё после D1 (включая ё, D1 91, у которой отдельный паспорт).
static int is_cyr_upper(const char* s) {
    unsigned char b0 = (unsigned char)s[0], b1 = (unsigned char)s[1];
    return (b0 == 0xD0 && ((b1 >= 0x90 && b1 <= 0xAF) || b1 == 0x81));
}
static int is_cyr_lower(const char* s) {
    unsigned char b0 = (unsigned char)s[0], b1 = (unsigned char)s[1];
    return (b0 == 0xD0 && b1 >= 0xB0) || (b0 == 0xD1 && ((b1 >= 0x80 && b1 <= 0x8F) || b1 == 0x91));
}

// Слово вида «Фамилия» / «Рябова-Круглова»: заглавная + хотя бы две строчные.
// Возвращает длину в байтах или 0, если это не оно.
static size_t match_cyr_capword(const char* s, const char* end) {
    const char* p = s;
    int lowers = 0;
    if (!is_cyr_upper(p)) return 0;
    p += 2;
    while (p + 1 < end) {
        if (is_cyr_lower(p)) { p += 2; lowers++; }
        else if (*p == '-' && p + 2 < end && is_cyr_lower(p + 1)) { p += 3; lowers++; }
        else break;
    }
    return (lowers >= 2) ? (size_t)(p - s) : 0;
}

// e-mail. Хитрость в конце: финальные точки домена — это пунктуация предложения
// («напишите на doctor@mail.ru.»), поэтому срезаем их до проверки.
static size_t match_email(const char* s, const char* end) {
    const char* p = s;
    const char* at = NULL;
    if (!is_lat_alnum(*s) && !is_lat_special(*s)) return 0;
    while (p < end) {
        if (*p == '@') { at = p; p++; break; }
        if (!is_lat_alnum(*p) && !is_lat_special(*p)) return 0;
        p++;
    }
    if (!at || at == s) return 0;
    if (p >= end || !is_lat_alnum(*p)) return 0;
    while (p < end && (is_lat_alnum(*p) || *p == '-' || *p == '.')) p++;
    while (p > at + 1 && p[-1] == '.') p--;
    if (p == at + 1 || p[-1] == '-') return 0;
    return (size_t)(p - s);
}

// Числовой блок с разделителями: «+7 (913) 123-45-67», «123-456-789 00».
// Разделители — это пробег из пробелов, дефисов и скобок, за которым обязана идти цифра,
// иначе это конец числа, а не его продолжение.
static size_t match_number_block(const char* s, const char* end, int* has_plus,
                                 int* groups, int* ngroups, int* ndigits) {
    const char* p = s;
    int dg = 0, ng = 0, plus = 0;
    if (*p == '+') { plus = 1; p++; }
    if (p >= end || !is_digit(*p)) return 0;
    while (p < end) {
        int g = 0;
        while (p < end && is_digit(*p)) { p++; g++; }
        dg += g;
        if (dg > 19) return 0;   // длиннее любого реального документа — не ПДн, не наше дело
        if (ng < 8) groups[ng++] = g;
        {
            const char* q = p;
            while (q < end && (*q == ' ' || *q == '-' || *q == '(' || *q == ')')) q++;
            if (q > p && q < end && is_digit(*q)) { p = q; continue; }
        }
        break;
    }
    *has_plus = plus;
    *ngroups = ng;
    *ndigits = dg;
    return (size_t)(p - s);
}

// «Фамилия И.О.» и «Фамилия И. О.» — самый частый формат в выписках.
static size_t match_fio_initials(const char* s, const char* end) {
    size_t wl = match_cyr_capword(s, end);
    const char* p;
    if (!wl) return 0;
    p = s + wl;
    if (p + 3 >= end || *p != ' ') return 0;
    p++;
    if (!is_cyr_upper(p)) return 0;
    p += 2;
    if (*p != '.') return 0;
    p++;
    if (*p == ' ') p++;
    if (p + 2 >= end || !is_cyr_upper(p)) return 0;
    p += 2;
    if (*p != '.') return 0;
    p++;
    return (size_t)(p - s);
}

// «Фамилия Имя Отчество» — три капитализированных слова подряд.
static size_t match_fio_full(const char* s, const char* end) {
    size_t w1 = match_cyr_capword(s, end), w2, w3;
    const char* p;
    if (!w1 || s + w1 >= end) return 0;
    p = s + w1;
    if (*p != ' ') return 0;
    p++;
    w2 = match_cyr_capword(p, end);
    if (!w2) return 0;
    p += w2;
    if (p >= end || *p != ' ') return 0;
    p++;
    w3 = match_cyr_capword(p, end);
    if (!w3) return 0;
    p += w3;
    return (size_t)(p - s);
}

// Диспетчер: что именно может начаться в этой позиции.
static size_t pii_scan_at(const char* in, size_t n, size_t i, uint32_t* kind) {
    const char* s = in + i;
    const char* end = in + n;
    char prev = (i > 0) ? in[i - 1] : '\0';
    size_t m;

    m = match_email(s, end);
    if (m && !is_lat_alnum(prev)) { *kind = PII_FOUND_EMAIL; return m; }

    if (is_digit(*s) || *s == '+') {
        if (!is_digit(prev)) {
            int groups[8], ng = 0, dg = 0, plus = 0;
            m = match_number_block(s, end, &plus, groups, &ng, &dg);
            if (m) {
                if (dg == 11) {
                    // 11 цифр группами 3-3-3-2 — это СНИЛС; слитно или через +7/8 — телефон.
                    int snils = (ng == 4 && groups[0] == 3 && groups[1] == 3 && groups[2] == 3 && groups[3] == 2);
                    if (snils) { *kind = PII_FOUND_SNILS; return m; }
                    if (plus || *s == '7' || *s == '8') { *kind = PII_FOUND_PHONE; return m; }
                    *kind = PII_FOUND_SNILS; return m;
                }
                if (dg == 16) { *kind = PII_FOUND_OMS; return m; }
            }
        }
    }

    m = match_fio_initials(s, end);
    if (m) { *kind = PII_FOUND_FIO; return m; }
    m = match_fio_full(s, end);
    if (m) { *kind = PII_FOUND_FIO; return m; }

    return 0;
}

/* ================================ Сканирование ================================ */

static const char HEXD[] = "0123456789abcdef";

size_t pii_scrubber_scrub(const pii_scrubber* s, const char* restrict in, char* restrict out,
                          size_t out_cap, uint32_t* found_mask) {
    const size_t n = strlen(in);
    const unsigned char* restrict cs = s->can_start;  // локальная копия указателя — компилятор не перечитывает память
    size_t i = 0, oi = 0;
    uint32_t mask = 0;

    if (found_mask) *found_mask = 0;
    if (!s) return (size_t)-1;

    while (i < n) {
        size_t run = i;

        // Быстрый пролёт: пока байт точно не может начать шаблон — вперёд.
        // Ветку кириллицы ставим первой: в русском тексте её два байта из трёх.
        while (i < n) {
            unsigned char b = (unsigned char)in[i];
            if (b == 0xD0 && i + 1 < n) {
                unsigned char b1 = (unsigned char)in[i + 1];
                if ((b1 >= 0x90 && b1 <= 0xAF) || b1 == 0x81) break; // заглавная — кандидат
                i += 2;                                              // строчная — прыжок через символ
            } else if (b == 0xD1 && i + 1 < n) {
                i += 2; // после D1 заглавных не бывает
            } else if (b < 0x80) {
                if (cs[b]) break;
                i++;
            } else {
                i++;
            }
        }

        if (i > run) {
            if (oi + (i - run) > out_cap) return (size_t)-1;
            memcpy(out + oi, in + run, i - run);
            oi += i - run;
        }
        if (i >= n) break;

        {
            uint32_t kind = 0;
            size_t ml = pii_scan_at(in, n, i, &kind);
            if (ml) {
                unsigned char mac[32];
                char tok[13];
                int j;
                mask |= kind;
                hmac_sha256_cached(s->ipad_h, s->opad_h, (const unsigned char*)(in + i), ml, mac);
                tok[0] = 'P'; tok[1] = 'I'; tok[2] = 'I'; tok[3] = '_';
                for (j = 0; j < 4; j++) {
                    tok[4 + j * 2] = HEXD[mac[j] >> 4];
                    tok[5 + j * 2] = HEXD[mac[j] & 0x0F];
                }
                tok[12] = '\0';
                if (oi + 12 > out_cap) return (size_t)-1;
                memcpy(out + oi, tok, 12);
                oi += 12;
                i += ml;
            } else {
                // Кандидат не сложился (например, заглавное слово без инициалов) —
                // честно копируем байт и едем дальше.
                if (oi + 1 > out_cap) return (size_t)-1;
                out[oi++] = in[i++];
            }
        }
    }

    if (found_mask) *found_mask = mask;
    return oi;
}

// Старая точка входа: собирает всё состояние на месте. Результат побайтово тот же,
// что и у кэшированного варианта, — на этом держится A/B-тест в benchmarks.
size_t pii_scrub_stream(const char* in, char* out, size_t out_cap,
                        const char* pepper, uint32_t* found_mask) {
    struct pii_scrubber tmp;
    size_t r;
    if (!pepper || !*pepper) return (size_t)-1;
    hmac_keys_from_pepper(pepper, tmp.ipad_h, tmp.opad_h);
    {
        int b;
        memset(tmp.can_start, 0, sizeof(tmp.can_start));
        for (b = '0'; b <= '9'; b++) tmp.can_start[b] = 1;
        for (b = 'A'; b <= 'Z'; b++) tmp.can_start[b] = 1;
        for (b = 'a'; b <= 'z'; b++) tmp.can_start[b] = 1;
        tmp.can_start[(unsigned char)'+'] = 1;
        tmp.can_start[(unsigned char)'.'] = 1;
        tmp.can_start[(unsigned char)'_'] = 1;
        tmp.can_start[(unsigned char)'%'] = 1;
        tmp.can_start[(unsigned char)'-'] = 1;
    }
    r = pii_scrubber_scrub(&tmp, in, out, out_cap, found_mask);
    // Не оставляем ключевой материал на стеке — привычка из криптоконтура.
    memset(&tmp, 0, sizeof(tmp));
    return r;
}
