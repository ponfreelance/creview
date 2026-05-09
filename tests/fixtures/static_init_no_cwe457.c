/* 静的記憶域 CWE-457 回帰テスト (B5)
 *
 * 期待: line 11 (static int g_initialized;) を CWE-457 系
 *       (未初期化変数) message に紐づけないこと。
 *
 * ISO C により、静的記憶域期間を持つ変数は明示初期化子がなくても
 * 算術型は 0、ポインタは NULL に自動初期化される。
 * したがって CWE-457 (Use of Uninitialized Variable) には該当しない。
 */
#include <stdio.h>

static int g_initialized;  /* C 標準により 0 に自動初期化される */

static int *g_ptr;  /* 同上、NULL に自動初期化 */

void init(void) {
    g_initialized = 1;
    g_ptr = (int *)0;
}

void use(void) {
    if (g_initialized) {
        printf("ok\n");
    }
}
