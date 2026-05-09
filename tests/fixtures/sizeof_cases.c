/* sizeof FP 修正 (B1) — 7 ケース fixture
 *
 * 期待:
 *   CASE 1, 2, 6, 7 → flag されない
 *   CASE 3, 4, 5    → flag される
 */
#include <stdlib.h>
#include <string.h>

/* CASE 1: ローカル配列 → flag しない */
void case1(void) {
    char buf[100];
    int n = sizeof(buf);  /* expected: no issue */
    (void)n;
}

/* CASE 2: ファイルスコープ配列 → flag しない */
static char g_buf[100];
void case2(void) {
    int n = sizeof(g_buf);  /* expected: no issue */
    (void)n;
}

/* CASE 3: 関数パラメータの配列形 → flag する (decay) */
void case3(char buf[100]) {
    int n = sizeof(buf);  /* expected: flag (sizeof returns pointer size) */
    (void)n;
}

/* CASE 4: 関数パラメータのポインタ形 → flag する */
void case4(char *buf) {
    int n = sizeof(buf);  /* expected: flag */
    (void)n;
}

/* CASE 5: ローカルポインタ → flag する (配列長 context) */
void case5(void) {
    char *buf = malloc(100);
    if (buf == NULL) return;
    memset(buf, 0, sizeof(buf));  /* expected: flag (sizeof in length context) */
    free(buf);
}

/* CASE 6: 構造体メンバ配列 → flag しない */
struct S { char arr[100]; };
void case6(struct S *s) {
    int n = sizeof(s->arr);  /* expected: no issue */
    (void)n;
}

/* CASE 7: typedef 配列 (ローカル変数として宣言) → flag しない */
typedef char buf_t[100];
void case7(void) {
    buf_t b;
    int n = sizeof(b);  /* expected: no issue */
    (void)n;
}
