// pii_scrubber.c — потоковое обезличивание ПДн одним проходом, без аллокаций (Разработчик №1)
// Категории: телефоны (+7/8, форматы со скобками/дефисами/пробелами), СНИЛС (XXX-XXX-XXX XX),
// полис ОМС (16 цифр), e-mail, ФИО («Иванов И.И.» и «Иванов Иван Иванович»).
// Медицинские числа (креатинин 86, СКФ 74, АД 130/80, МНО 1.05) не трогаются:
// матчатся только структурные шаблоны ПДн.
#include <string.h>

#include "pii_scrubber.h"

/* ============================== SHA-256 (FIPS 180-4) ============================== */
typedef struct {
    uint32_t h[8];
    uint64_t len;
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
    unsigned char tail = 0x80;
    unsigned char zero = 0x00;
    unsigned char lenb[8];
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

static void hmac_sha256(const unsigned char* key, size_t keylen,
                        const unsigned char* msg, size_t msglen, unsigned char out[32]) {
    unsigned char k[64], ipad[64], opad[64], kh[32], digest[32];
    sha256_ctx c;
    size_t i;
    memset(k, 0, sizeof(k));
    if (keylen > 64) {
        sha256_init(&c);
        sha256_update(&c, key, keylen);
        sha256_final(&c, kh);
        memcpy(k, kh, 32);
    } else {
        memcpy(k, key, keylen);
    }
    for (i = 0; i < 64; i++) { ipad[i] = k[i] ^ 0x36; opad[i] = k[i] ^ 0x5c; }
    sha256_init(&c);
    sha256_update(&c, ipad, 64);
    sha256_update(&c, msg, msglen);
    sha256_final(&c, digest);
    sha256_init(&c);
    sha256_update(&c, opad, 64);
    sha256_update(&c, digest, 32);
    sha256_final(&c, out);
}

/* ============================== Сканеры шаблонов ============================== */
static int is_digit(char ch) { return ch >= '0' && ch <= '9'; }
static int is_lat_alnum(char ch) {
    return is_digit(ch) || (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
}
static int is_lat_special(char ch) {
    return ch == '.' || ch == '_' || ch == '%' || ch == '+' || ch == '-';
}

// UTF-8 кириллица: [А-ЯЁ] = D0 90..AF + D0 81; [а-яё] = D0 B0..BF, D1 80..8F + D1 91
static int is_cyr_upper(const char* s) {
    unsigned char b0 = (unsigned char)s[0], b1 = (unsigned char)s[1];
    return (b0 == 0xD0 && ((b1 >= 0x90 && b1 <= 0xAF) || b1 == 0x81));
}
static int is_cyr_lower(const char* s) {
    unsigned char b0 = (unsigned char)s[0], b1 = (unsigned char)s[1];
    return (b0 == 0xD0 && b1 >= 0xB0) || (b0 == 0xD1 && ((b1 >= 0x80 && b1 <= 0x8F) || b1 == 0x91));
}
static int is_cyr_letter(const char* s) { return is_cyr_upper(s) || is_cyr_lower(s); }

// Слово «Фамилия»: [А-ЯЁ] + [а-яё]{2,}, внутри допустим дефис. Возвращает длину в байтах или 0.
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

// e-mail: local@domain.tld
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
    // хвостовые точки домена — это пунктуация предложения, не часть адреса
    while (p > at + 1 && p[-1] == '.') p--;
    if (p == at + 1 || p[-1] == '-' ) return 0;
    return (size_t)(p - s);
}

// Числовой блок с разделителями ' '/'-'/'('/')': группы цифр, всего до 19 цифр.
// Возвращает длину блока и заполняет groups[]/ngroups/ndigits. Начало: цифра или '+'.
static size_t match_number_block(const char* s, const char* end, int* has_plus,
                                 int* groups, int* ngroups, int* ndigits) {
    const char* p = s;
    int dg = 0, ng = 0, plus = 0;
    if (*p == '+') { plus = 1; p++; if (p >= end || !is_digit(*p)) return 0; }
    if (!is_digit(*p)) return 0;
    // блок не может начинаться внутри числа
    if (p > s + (size_t)plus) {
        char prev = *(p - 1);
        if (is_digit(prev)) return 0;
    }
    while (p < end) {
        int g = 0;
        while (p < end && is_digit(*p)) { p++; g++; }
        dg += g;
        if (dg > 19) return 0;
        if (ng < 8) groups[ng++] = g;
        // разделитель-пробег (пробелы / дефисы / скобки), за которым идёт цифра
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

// ФИО с инициалами: «Фамилия И.О.» / «Фамилия И. О.»
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

// ФИО полностью: «Фамилия Имя Отчество» (три слова с заглавной)
static size_t match_fio_full(const char* s, const char* end) {
    size_t w1 = match_cyr_capword(s, end);
    size_t w2, w3;
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

// Возвращает длину совпадения шаблона ПДн в позиции i и его категорию
static size_t pii_scan_at(const char* in, size_t n, size_t i, uint32_t* kind) {
    const char* s = in + i;
    const char* end = in + n;
    char prev = (i > 0) ? in[i - 1] : '\0';
    size_t m;

    // e-mail
    m = match_email(s, end);
    if (m && !is_lat_alnum(prev)) { *kind = PII_FOUND_EMAIL; return m; }

    // числовые блоки: телефон / СНИЛС / ОМС
    if (is_digit(*s) || *s == '+') {
        if (!is_digit(prev)) {
            int groups[8], ng = 0, dg = 0, plus = 0;
            m = match_number_block(s, end, &plus, groups, &ng, &dg);
            if (m) {
                if (dg == 11) {
                    int snils_pattern = (ng == 4 && groups[0] == 3 && groups[1] == 3 && groups[2] == 3 && groups[3] == 2);
                    if (snils_pattern) { *kind = PII_FOUND_SNILS; return m; }
                    if (plus || *s == '7' || *s == '8') { *kind = PII_FOUND_PHONE; return m; }
                    *kind = PII_FOUND_SNILS; return m;
                }
                if (dg == 16) { *kind = PII_FOUND_OMS; return m; }
            }
        }
    }

    // ФИО
    m = match_fio_initials(s, end);
    if (m) { *kind = PII_FOUND_FIO; return m; }
    m = match_fio_full(s, end);
    if (m) { *kind = PII_FOUND_FIO; return m; }

    return 0;
}

static const char HEXD[] = "0123456789abcdef";

size_t pii_scrub_stream(const char* in, char* out, size_t out_cap,
                        const char* pepper, uint32_t* found_mask) {
    const size_t n = strlen(in);
    size_t i = 0, oi = 0;
    uint32_t mask = 0;
    unsigned char mac[32];
    char tok[13];

    if (found_mask) *found_mask = 0;

    while (i < n) {
        uint32_t kind = 0;
        size_t ml = pii_scan_at(in, n, i, &kind);
        if (ml) {
            int j;
            mask |= kind;
            hmac_sha256((const unsigned char*)pepper, strlen(pepper),
                        (const unsigned char*)(in + i), ml, mac);
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
            if (oi + 1 > out_cap) return (size_t)-1;
            out[oi++] = in[i++];
        }
    }
    if (found_mask) *found_mask = mask;
    return oi;
}
