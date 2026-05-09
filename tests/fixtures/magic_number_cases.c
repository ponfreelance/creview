/* マジックナンバー統一 (B3) — 6 ケース fixture
 *
 * 統一ルール: allowlist {-1, 0, 1, 2} 以外の整数リテラルを全 flag。
 *           context で severity を切り替える。
 *           sizeof 内 / 文字列リテラル長 は除外。
 */
#include <stdlib.h>
#include <string.h>

/* CASE A: 配列宣言サイズ → flag (重大: buffer_size context) */
void case_a(void) {
    char buf[100];   /* expected: flag 100 (重大) */
    (void)buf;
}

/* CASE B: malloc 引数 → flag (重大: buffer_size context) */
void case_b(void) {
    char *p = malloc(256);  /* expected: flag 256 (重大) */
    free(p);
}

/* CASE C: ループ上限 → flag (保守危険: loop_bound) */
void case_c(void) {
    for (int i = 0; i < 32; i++) {  /* expected: flag 32 (保守危険) */
        (void)i;
    }
}

/* CASE D: ビットマスク → flag (保守危険: bitmask) */
unsigned case_d(unsigned x) {
    return x & 0xFF;  /* expected: flag 0xFF (保守危険) */
}

/* CASE E: 関数引数リテラル → flag (保守危険: general) */
int calculate_sum(int n);
void case_e(void) {
    int s = calculate_sum(10);  /* expected: flag 10 (保守危険) */
    (void)s;
}

/* CASE F: sizeof 引数の整数 → flag しない */
void case_f(void) {
    int n = sizeof(int);  /* expected: no issue (sizeof 内) */
    (void)n;
}
