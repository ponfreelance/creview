#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>

/* テスト: フォーマット文字列脆弱性 */
void test_format_string(char *user_input) {
    printf(user_input);          /* 検出すべき */
    printf("%s", user_input);    /* 安全 */
}

/* テスト: use-after-free */
void test_use_after_free() {
    char *p = malloc(100);
    free(p);
    p->field = 1;                /* 検出すべき */
}

/* テスト: 未初期化変数 */
void test_uninit() {
    int x;
    if (x > 0) {                 /* 検出すべき */
        printf("positive\n");
    }
}

/* テスト: signed/unsigned比較 */
void test_sign_compare() {
    unsigned int len = 10;
    int idx = -1;
    if (idx < len) {             /* 検出すべき */
        printf("ok\n");
    }
}

/* テスト: 整数オーバーフロー */
void test_int_overflow(size_t n) {
    char *p = malloc(n * sizeof(char));  /* 検出すべき */
}

/* テスト: リソースリーク */
void test_resource_leak() {
    int fd = open("/tmp/test", 0);  /* 検出すべき: closeなし */
    int sock = socket(AF_INET, SOCK_STREAM, 0); /* 検出すべき */
}

/* テスト: snprintf戻り値無視 */
void test_snprintf() {
    char buf[64];
    snprintf(buf, sizeof(buf), "hello %s", "world"); /* 検出すべき */
}

/* テスト: NOCHECK抑制 */
void test_nocheck() {
    char *q = malloc(100);  // NOCHECK
}
