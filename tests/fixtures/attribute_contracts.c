/* attribute 契約 (B2) — fixture
 *
 * 期待:
 *   ATTR-NONNULL-001  : process_data に nonnull 属性なしで unconditional dereference
 *   ATTR-WUR-001      : validate_data に warn_unused_result 属性なしで int 返却
 *   NULL-CALLSITE-001 : nonnull 宣言済み関数に NULL リテラルを渡す callsite
 */
#include <stdio.h>
#include <string.h>

/* === ATTR-NONNULL-001 違反例 ===
 * ポインタ引数 data を NULL 検査せず printf("%s", ...) / strlen で使用しているが、
 * 関数宣言に __attribute__((nonnull)) がない。 */
void process_data(char *data) {
    printf("Data: %s\n", data);
    int len = (int)strlen(data);
    printf("Length: %d\n", len);
}

/* === ATTR-NONNULL-001 を満たす例 (flag されないことの対照) === */
void process_data_safe(char *data) __attribute__((nonnull(1)));
void process_data_safe(char *data) {
    printf("Data: %s\n", data);
}

/* === ATTR-WUR-001 違反例 ===
 * int を返すエラー系関数 (prefix が validate_) で warn_unused_result 属性なし。 */
int validate_data(const char *data) {
    if (data == NULL) return -1;
    return 0;
}

/* === ATTR-WUR-001 を満たす例 === */
int check_id(int id) __attribute__((warn_unused_result));
int check_id(int id) {
    return (id >= 0) ? 0 : -1;
}

/* === NULL-CALLSITE-001 違反例 ===
 * nonnull 宣言済みの process_data_safe に NULL リテラルを渡している。 */
void caller(void) {
    process_data_safe(NULL);  /* expected: NULL-CALLSITE-001 flag */
}
