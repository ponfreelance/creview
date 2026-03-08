#!/usr/bin/env python3
"""creview 静的解析チェックのユニットテスト"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import creview


def run_checks(source: str) -> list:
    """ソース文字列に対してローカル解析を実行し、Issue リストを返す"""
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                      encoding='utf-8') as f:
        f.write(source)
        f.flush()
        path = f.name
    try:
        ignore = creview.IgnoreConfig()
        return creview.run_local_analysis(path, ignore)
    finally:
        os.unlink(path)


def has_issue(issues, severity=None, keyword=None):
    for iss in issues:
        if severity and iss.severity != severity:
            continue
        if keyword and keyword not in iss.message:
            continue
        return True
    return False


class TestFormatString(unittest.TestCase):
    def test_detect_printf_variable(self):
        src = 'void f(char *s) { printf(s); }'
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "書式文字列"))

    def test_safe_printf(self):
        src = 'void f(char *s) { printf("%s", s); }'
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="書式文字列"))


class TestUseAfterFree(unittest.TestCase):
    def test_detect_deref(self):
        src = '''void f() {
            char *p = malloc(10);
            free(p);
            p[0] = 1;
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "use-after-free"))

    def test_detect_arrow(self):
        src = '''void f() {
            struct node *p = malloc(sizeof(struct node));
            free(p);
            p->next = 0;
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "use-after-free"))

    def test_null_assign_stops_tracking(self):
        src = '''void f() {
            char *p = malloc(10);
            free(p);
            p = NULL;
            int x = 1;
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="use-after-free"))


class TestUninitialized(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            int x;
            if (x > 0) {}
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "未初期化"))

    def test_initialized_safe(self):
        src = '''void f() {
            int x;
            x = 5;
            if (x > 0) {}
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="未初期化"))


class TestSignCompare(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            unsigned int a = 1;
            int b = -1;
            if (b < a) {}
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.DESIGN, "unsigned"))


class TestIntegerOverflow(unittest.TestCase):
    def test_detect_malloc_multiply(self):
        src = '''void f(size_t n) {
            char *p = malloc(n * sizeof(char));
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "オーバーフロー"))


class TestResourceLeak(unittest.TestCase):
    def test_open_no_close(self):
        src = '''void f() {
            int fd = open("/tmp/x", 0);
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "fdリーク"))

    def test_socket_no_close(self):
        src = '''void f() {
            int s = socket(AF_INET, SOCK_STREAM, 0);
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "ソケットリーク"))


class TestSnprintfRetval(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            char buf[64];
            snprintf(buf, 64, "test");
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.MAINT, "snprintf"))


class TestNocheck(unittest.TestCase):
    def test_nocheck_suppresses(self):
        src = '''void f() {
            char *p = malloc(100);  // NOCHECK
        }'''
        issues = run_checks(src)
        # malloc NULLチェックなしの指摘が抑制されるはず
        null_issues = [i for i in issues if "NULLチェックなし" in i.message]
        self.assertEqual(len(null_issues), 0)


class TestMarkdownFormat(unittest.TestCase):
    def test_output(self):
        issues = [
            creview.Issue(creview.Severity.CRITICAL, "test.c", 10, "テスト重大"),
            creview.Issue(creview.Severity.DESIGN, "test.c", 20, "テスト設計"),
        ]
        md = creview.format_markdown(issues, "test.c")
        self.assertIn("## test.c", md)
        self.assertIn("重大", md)
        self.assertIn("設計不明", md)
        self.assertIn("**L10**", md)


class TestExclude(unittest.TestCase):
    def test_excluded(self):
        ig = creview.IgnoreConfig()
        ig.exclude_patterns = ["generated_*.c"]
        self.assertTrue(creview.is_excluded("generated_code.c", ig))
        self.assertFalse(creview.is_excluded("main.c", ig))


# 既存チェックの回帰テスト
class TestNullDeref(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            char *p = malloc(100);
            *p = 'a';
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "NULLチェックなし"))


class TestUnsafeFuncs(unittest.TestCase):
    def test_gets(self):
        src = '''void f() { char b[10]; gets(b); }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "gets"))

    def test_sprintf(self):
        src = '''void f() { char b[10]; sprintf(b, "hi"); }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "sprintf"))


class TestDoubleFree(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            char *p = malloc(10);
            free(p);
            free(p);
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "二重free"))


class TestSizeofPointer(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            int *p = malloc(10);
            int sz = sizeof(p);
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "sizeof"))


class TestFdLeak(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            FILE *fp = fopen("test", "r");
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "fclose"))


# ── v0.7.0 新チェックのテスト ──

class TestBufferOverrun(unittest.TestCase):
    def test_detect_strncpy_overflow(self):
        src = '''void f() {
            char buf[10];
            strncpy(buf, src, 20);
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "バッファオーバーラン"))

    def test_safe_strncpy(self):
        src = '''void f() {
            char buf[32];
            strncpy(buf, src, 32);
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="バッファオーバーラン"))


class TestInfiniteLoop(unittest.TestCase):
    def test_detect_no_break(self):
        src = '''void f() {
            while (1) {
                int x = 1;
            }
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.DESIGN, "無限ループ"))

    def test_safe_with_break(self):
        src = '''void f() {
            while (1) {
                if (done) break;
            }
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="無限ループ"))


class TestEnumSwitch(unittest.TestCase):
    def test_detect_no_default(self):
        src = '''enum Color { RED, GREEN, BLUE };
        void f() {
            enum Color c = RED;
            switch (c) {
                case RED: break;
            }
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.DESIGN, "defaultなし"))

    def test_safe_with_default(self):
        src = '''enum Color { RED, GREEN, BLUE };
        void f() {
            enum Color c = RED;
            switch (c) {
                case RED: break;
                default: break;
            }
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="defaultなし"))


class TestRecursiveNoLimit(unittest.TestCase):
    def test_detect(self):
        src = '''int factorial(int n) {
            return n * factorial(n - 1);
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.MAINT, "自己再帰"))

    def test_safe_with_depth(self):
        src = '''int f(int n, int depth) {
            if (depth > 100) return 0;
            return f(n - 1, depth + 1);
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="自己再帰"))


class TestMutexUnlock(unittest.TestCase):
    def test_detect_no_unlock(self):
        src = '''void f() {
            pthread_mutex_lock(&mtx);
            do_work();
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "デッドロック"))

    def test_safe_with_unlock(self):
        src = '''void f() {
            pthread_mutex_lock(&mtx);
            do_work();
            pthread_mutex_unlock(&mtx);
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="デッドロック"))


class TestRuleOff(unittest.TestCase):
    def test_rule_off_disables_check(self):
        import tempfile
        src = '''void f() {
            char *p = malloc(100);
            *p = 'a';
        }'''
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            f.flush()
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            ignore.rule_off.add("null_deref")
            issues = creview.run_local_analysis(path, ignore)
            self.assertFalse(has_issue(issues, keyword="NULLチェックなし"))
        finally:
            os.unlink(path)


class TestSeverityFilter(unittest.TestCase):
    def test_filter(self):
        issues = [
            creview.Issue(creview.Severity.CRITICAL, "t.c", 1, "重大"),
            creview.Issue(creview.Severity.DESIGN, "t.c", 2, "設計"),
            creview.Issue(creview.Severity.MAINT, "t.c", 3, "保守"),
        ]
        filtered = [i for i in issues if i.severity == creview.Severity.CRITICAL]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].message, "重大")


class TestSarifFormat(unittest.TestCase):
    def test_output_valid_json(self):
        import json
        issues = {
            "test.c": [
                creview.Issue(creview.Severity.CRITICAL, "test.c", 10, "テスト指摘"),
            ]
        }
        sarif = creview.format_sarif(issues)
        data = json.loads(sarif)
        self.assertEqual(data["version"], "2.1.0")
        self.assertEqual(len(data["runs"][0]["results"]), 1)
        self.assertEqual(data["runs"][0]["results"][0]["level"], "error")


# ── v0.8.0 新チェックのテスト ──

class TestToctou(unittest.TestCase):
    def test_detect_access_open(self):
        src = '''void f(const char *path) {
            if (access(path, R_OK) == 0) {
                int fd = open(path, O_RDONLY);
            }
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "TOCTOU"))

    def test_no_false_positive(self):
        src = '''void f() {
            int fd = open("/tmp/x", O_RDONLY);
            close(fd);
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="TOCTOU"))


class TestSignalUnsafe(unittest.TestCase):
    def test_detect_printf_in_handler(self):
        src = '''void handler(int sig) {
            printf("signal %d\\n", sig);
        }
        void setup() {
            signal(SIGINT, handler);
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.CRITICAL, "async-signal-unsafe"))

    def test_safe_handler(self):
        src = '''volatile sig_atomic_t flag = 0;
        void handler(int sig) {
            flag = 1;
        }
        void setup() {
            signal(SIGINT, handler);
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="async-signal-unsafe"))


class TestCastTruncation(unittest.TestCase):
    def test_detect(self):
        src = '''void f() {
            long big = 0x100000000L;
            int small = (int)big;
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.DESIGN, "切り詰め"))


class TestBitfieldSign(unittest.TestCase):
    def test_detect(self):
        src = '''struct flags {
            int ready : 1;
        };'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.DESIGN, "符号未指定"))

    def test_unsigned_safe(self):
        src = '''struct flags {
            unsigned int ready : 1;
        };'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="符号未指定"))


class TestVla(unittest.TestCase):
    def test_detect(self):
        src = '''void f(int n) {
            char buf[n];
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.MAINT, "可変長配列"))

    def test_constant_safe(self):
        src = '''void f() {
            char buf[BUFSIZE];
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="可変長配列"))


class TestGotoMisuse(unittest.TestCase):
    def test_detect_backward_jump(self):
        src = '''void f() {
        retry:
            do_work();
            if (failed) goto retry;
        }'''
        issues = run_checks(src)
        self.assertTrue(has_issue(issues, creview.Severity.MAINT, "前方"))

    def test_forward_goto_safe(self):
        src = '''void f() {
            if (error) goto cleanup;
            do_work();
        cleanup:
            free(ptr);
        }'''
        issues = run_checks(src)
        self.assertFalse(has_issue(issues, keyword="前方"))


class TestFixHint(unittest.TestCase):
    def test_hint_present(self):
        hint = creview.get_fix_hint("pにNULLチェックなし。malloc失敗時クラッシュ")
        self.assertIn("if (ptr == NULL)", hint)

    def test_hint_format_text(self):
        issues = [
            creview.Issue(creview.Severity.CRITICAL, "t.c", 1, "gets使用。バッファ長制限なし"),
        ]
        output = creview.format_text(issues, fix_hint=True)
        self.assertIn("fgets", output)


class TestListRules(unittest.TestCase):
    def test_all_rules_defined(self):
        # checksリストとRULE_DESCRIPTIONSが一致すること
        self.assertIn("toctou", creview.RULE_DESCRIPTIONS)
        self.assertIn("signal_unsafe", creview.RULE_DESCRIPTIONS)
        self.assertIn("cast_truncation", creview.RULE_DESCRIPTIONS)
        self.assertIn("bitfield_sign", creview.RULE_DESCRIPTIONS)
        self.assertIn("vla", creview.RULE_DESCRIPTIONS)
        self.assertIn("goto_misuse", creview.RULE_DESCRIPTIONS)
        self.assertIn("stack_usage", creview.RULE_DESCRIPTIONS)
        self.assertEqual(len(creview.RULE_DESCRIPTIONS), 34)


class TestBaseline(unittest.TestCase):
    def test_filter_known_issues(self):
        # ベースラインに含まれる指摘は除外される
        issues = [
            creview.Issue(creview.Severity.CRITICAL, "t.c", 1, "テスト指摘"),
            creview.Issue(creview.Severity.DESIGN, "t.c", 2, "新しい指摘"),
        ]
        baseline = [{"file": "t.c", "line": 1, "message": "テスト指摘"}]
        baseline_set = set()
        for bi in baseline:
            baseline_set.add((bi["file"], bi["line"], bi["message"]))
        filtered = [i for i in issues if (i.filepath, i.line, i.message) not in baseline_set]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].message, "新しい指摘")


class TestPreset(unittest.TestCase):
    def test_memory_preset_rules(self):
        """memoryプリセットはメモリ関連ルールのみ含む"""
        preset = creview.PRESETS["memory"]
        self.assertIn("null_deref", preset["rules"])
        self.assertIn("double_free", preset["rules"])
        self.assertIn("use_after_free", preset["rules"])
        self.assertNotIn("magic_numbers", preset["rules"])

    def test_memory_preset_filters(self):
        """memoryプリセット使用時、メモリ関連以外の指摘が出ない"""
        src = '''
        int g = 0;
        void f() {
            g = 42;
            char *p = malloc(100);
        }
        '''
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            f.flush()
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            only_rules = creview.PRESETS["memory"]["rules"]
            issues = creview.run_local_analysis(path, ignore, only_rules=only_rules)
            # NULLチェック漏れは出るはず
            self.assertTrue(has_issue(issues, keyword="NULLチェック"))
            # グローバル変数やマジックナンバーは出ないはず
            self.assertFalse(has_issue(issues, keyword="グローバル変数"))
        finally:
            os.unlink(path)

    def test_security_preset_exists(self):
        self.assertIn("security", creview.PRESETS)
        self.assertIn("unsafe_funcs", creview.PRESETS["security"]["rules"])

    def test_pr_preset_all_rules(self):
        """prプリセットは全ルール実行"""
        self.assertIsNone(creview.PRESETS["pr"].get("rules"))
        self.assertTrue(creview.PRESETS["pr"]["options"]["diff"])
        self.assertTrue(creview.PRESETS["pr"]["options"]["fix_hint"])

    def test_resolve_preset_unknown(self):
        """不明なプリセットはsys.exit"""
        import argparse
        args = argparse.Namespace()
        with self.assertRaises(SystemExit):
            creview.resolve_preset("nonexistent", args)

    def test_list_presets(self):
        """全プリセットにdescriptionがある"""
        for name, preset in creview.PRESETS.items():
            self.assertIn("description", preset)


class TestOnlyRules(unittest.TestCase):
    def test_only_rules_filters(self):
        """only_rules指定で特定ルールのみ実行"""
        src = '''
        void f() {
            char *p = malloc(100);
            printf(p);
        }
        '''
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            f.flush()
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            # format_stringのみ実行
            issues = creview.run_local_analysis(path, ignore,
                                                 only_rules={"format_string"})
            self.assertTrue(has_issue(issues, keyword="書式文字列"))
            # null_derefは出ない
            self.assertFalse(has_issue(issues, keyword="NULLチェック"))
        finally:
            os.unlink(path)


class TestAskResult(unittest.TestCase):
    def test_apply_ask_result_empty(self):
        """空の結果は通常モードにフォールバック"""
        import argparse
        args = argparse.Namespace(severity=None, fix_hint=False,
                                   diff=False, exit_code="critical")
        result = creview.apply_ask_result({}, args)
        self.assertIsNone(result)

    def test_apply_ask_result_with_rules(self):
        """ルール指定ありの結果を正しく反映"""
        import argparse
        args = argparse.Namespace(severity=None, fix_hint=False,
                                   diff=False, exit_code="critical")
        ask_result = {
            "severity": "critical",
            "fix_hint": True,
            "rules": ["null_deref", "double_free"],
        }
        only_rules = creview.apply_ask_result(ask_result, args)
        self.assertEqual(only_rules, {"null_deref", "double_free"})
        self.assertTrue(args.fix_hint)
        self.assertEqual(args.severity, "critical")

    def test_apply_ask_result_with_preset(self):
        """プリセット指定のask結果"""
        import argparse
        args = argparse.Namespace(severity=None, fix_hint=False,
                                   diff=False, exit_code="critical")
        ask_result = {"preset": "memory"}
        only_rules = creview.apply_ask_result(ask_result, args)
        self.assertEqual(only_rules, creview.PRESETS["memory"]["rules"])

    def test_apply_ask_result_invalid_rules_ignored(self):
        """不正なルール名は除外される"""
        import argparse
        args = argparse.Namespace(severity=None, fix_hint=False,
                                   diff=False, exit_code="critical")
        ask_result = {"rules": ["null_deref", "invalid_rule_xyz"]}
        only_rules = creview.apply_ask_result(ask_result, args)
        self.assertIn("null_deref", only_rules)
        self.assertNotIn("invalid_rule_xyz", only_rules)


class TestGenerateFix(unittest.TestCase):
    def test_malloc_null_fix(self):
        """malloc NULLチェック漏れの修正案が生成される"""
        msg = "pにNULLチェックなし。malloc/calloc/realloc失敗時クラッシュ"
        src = "    char *p = malloc(100);"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("if (p == NULL)", fix.minimal)
        self.assertIn("if (p == NULL)", fix.recommended)
        self.assertIn("fprintf", fix.recommended)

    def test_gets_fix(self):
        """gets使用の修正案"""
        msg = "gets使用。バッファ長制限なし、確実にオーバーフロー可能"
        src = "    gets(buf);"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("fgets(buf", fix.minimal)
        self.assertIn("strcspn", fix.recommended)

    def test_sprintf_fix(self):
        """sprintf使用の修正案"""
        msg = "sprintf使用。出力バッファ長未検証でオーバーフロー可能"
        src = '    sprintf(buf, "%s", name);'
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("snprintf(buf, sizeof(buf)", fix.minimal)
        self.assertIn("int ret =", fix.recommended)

    def test_format_string_fix(self):
        """フォーマット文字列脆弱性の修正案"""
        msg = "書式文字列脆弱性: printf()に変数直接渡し"
        src = "    printf(user_input);"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn('printf("%s"', fix.minimal)

    def test_double_free_fix(self):
        """二重freeの修正案"""
        msg = "二重free: pは既にfree済み"
        src = "    free(p);"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("NULL", fix.minimal)

    def test_use_after_free_fix(self):
        """use-after-freeの修正案"""
        msg = "free済みpを参照(use-after-free)。5行目でfree済み"
        src = "    p->next = 0;"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("free済み", fix.minimal)

    def test_uninit_fix(self):
        """未初期化変数の修正案"""
        msg = "未初期化変数xを使用(3行目で宣言、初期化なし)。不定値"
        src = "    int x;"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("int x = 0", fix.minimal)

    def test_toctou_fix(self):
        """TOCTOU修正案"""
        msg = "TOCTOU競合: access()とopen()間にレース条件"
        src = "    if (access(path, R_OK) == 0) {"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("open()", fix.minimal)

    def test_vla_fix(self):
        """VLA修正案"""
        msg = "可変長配列(VLA)使用。スタックオーバーフローの危険"
        src = "    char buf[n];"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("malloc", fix.minimal)
        self.assertIn("free", fix.recommended)

    def test_bitfield_fix(self):
        """ビットフィールド符号修正案"""
        msg = "ビットフィールド符号未指定: flagは1ビットで符号不定"
        src = "    int flag : 1;"
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("unsigned int flag", fix.minimal)

    def test_snprintf_fix(self):
        """snprintf戻り値無視の修正案"""
        msg = "snprintf戻り値無視。切り詰め未検出"
        src = '    snprintf(buf, sizeof(buf), "hello");'
        fix = creview.generate_fix(msg, src)
        self.assertIsNotNone(fix)
        self.assertIn("(void)", fix.minimal)
        self.assertIn("int ret =", fix.recommended)

    def test_unknown_returns_none(self):
        """不明な指摘にはNone"""
        fix = creview.generate_fix("未知の指摘メッセージ", "    x = 1;")
        self.assertIsNone(fix)


class TestFixFormatText(unittest.TestCase):
    def test_fix_in_text_output(self):
        """--fix指定時にtext出力に修正案が含まれる"""
        import tempfile
        src = '#include <stdlib.h>\nvoid f() {\n    char *p = malloc(100);\n}\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            f.flush()
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            issues = creview.run_local_analysis(path, ignore,
                                                 only_rules={"null_deref"})
            self.assertTrue(len(issues) > 0)
            output = creview.format_text(issues, fix=True)
            self.assertIn("[最小修正]", output)
            self.assertIn("[推奨修正]", output)
        finally:
            os.unlink(path)

    def test_fix_in_markdown_output(self):
        """--fix指定時にmarkdown出力に修正案が含まれる"""
        import tempfile
        src = '#include <stdlib.h>\nvoid f() {\n    char *p = malloc(100);\n}\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            f.flush()
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            issues = creview.run_local_analysis(path, ignore,
                                                 only_rules={"null_deref"})
            output = creview.format_markdown(issues, path, fix=True)
            self.assertIn("**最小修正:**", output)
            self.assertIn("**推奨修正:**", output)
        finally:
            os.unlink(path)


class TestExtractSimilarPattern(unittest.TestCase):
    def test_malloc_pattern(self):
        """malloc NULLチェック漏れの類似パターン抽出"""
        msg = "pにNULLチェックなし。malloc/calloc/realloc失敗時クラッシュ"
        pat = creview._extract_similar_pattern(msg, "char *p = malloc(100);")
        self.assertIsNotNone(pat)
        import re
        self.assertTrue(re.search(pat, "x = malloc(50);"))
        self.assertTrue(re.search(pat, "buf = calloc(10, 4);"))

    def test_sprintf_pattern(self):
        """sprintf類似パターン抽出"""
        msg = "sprintf使用。出力バッファ長未検証でオーバーフロー可能"
        pat = creview._extract_similar_pattern(msg, 'sprintf(buf, "%d", x);')
        self.assertIsNotNone(pat)
        import re
        self.assertTrue(re.search(pat, 'sprintf(out, "%s", name);'))

    def test_unknown_returns_none(self):
        """不明な指摘にはNone"""
        pat = creview._extract_similar_pattern("未知の指摘", "x = 1;")
        self.assertIsNone(pat)


class TestFindSimilarIssues(unittest.TestCase):
    def test_find_similar_across_files(self):
        """複数ファイルから類似パターンを検出"""
        import tempfile
        # ファイル1: mallocあり (指摘元)
        src1 = '#include <stdlib.h>\nvoid f() {\n    char *p = malloc(100);\n}\n'
        # ファイル2: 別のmalloc (類似箇所として検出されるべき)
        src2 = '#include <stdlib.h>\nvoid g() {\n    int *q = malloc(sizeof(int));\n}\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f1:
            f1.write(src1)
            path1 = f1.name
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f2:
            f2.write(src2)
            path2 = f2.name
        try:
            issue = creview.Issue(creview.Severity.CRITICAL, path1, 3,
                                   "pにNULLチェックなし。malloc/calloc/realloc失敗時クラッシュ")
            cache = {}
            similar = creview.find_similar_issues(issue, [path1, path2], cache)
            # path2の3行目にmallocがあるので検出されるべき
            self.assertTrue(len(similar) > 0)
            found_path2 = any(s.filepath == path2 for s in similar)
            self.assertTrue(found_path2)
            # 自分自身(path1:3)は含まれないこと
            self_found = any(s.filepath == path1 and s.line == 3 for s in similar)
            self.assertFalse(self_found)
        finally:
            os.unlink(path1)
            os.unlink(path2)

    def test_no_similar_for_unknown(self):
        """パターン不明の指摘は類似0件"""
        import tempfile
        src = 'int main() { return 0; }\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            path = f.name
        try:
            issue = creview.Issue(creview.Severity.MAINT, path, 1, "不明な指摘")
            cache = {}
            similar = creview.find_similar_issues(issue, [path], cache)
            self.assertEqual(len(similar), 0)
        finally:
            os.unlink(path)


class TestSimilarFormatOutput(unittest.TestCase):
    def test_similar_in_text_output(self):
        """--similar指定時にtext出力に類似箇所が含まれる"""
        issues = [creview.Issue(creview.Severity.CRITICAL, "a.c", 5,
                                 "sprintf使用。出力バッファ長未検証でオーバーフロー可能")]
        sim_map = {
            ("a.c", 5): [
                creview.SimilarLocation("b.c", 10, 'sprintf(buf, "%d", n);'),
                creview.SimilarLocation("c.c", 20, 'sprintf(out, "%s", s);'),
            ]
        }
        output = creview.format_text(issues, similar_map=sim_map)
        self.assertIn("[類似箇所] 2件", output)
        self.assertIn("b.c:10", output)
        self.assertIn("c.c:20", output)

    def test_similar_in_markdown_output(self):
        """--similar指定時にmarkdown出力に類似箇所が含まれる"""
        issues = [creview.Issue(creview.Severity.CRITICAL, "a.c", 5,
                                 "sprintf使用。出力バッファ長未検証でオーバーフロー可能")]
        sim_map = {
            ("a.c", 5): [
                creview.SimilarLocation("b.c", 10, 'sprintf(buf, "%d", n);'),
            ]
        }
        output = creview.format_markdown(issues, "a.c", similar_map=sim_map)
        self.assertIn("類似箇所", output)
        self.assertIn("`b.c:10`", output)

    def test_no_similar_section_when_none(self):
        """similar_map=Noneなら類似箇所セクションなし"""
        issues = [creview.Issue(creview.Severity.CRITICAL, "a.c", 5,
                                 "sprintf使用。出力バッファ長未検証でオーバーフロー可能")]
        output = creview.format_text(issues, similar_map=None)
        self.assertNotIn("類似箇所", output)


class TestStackAnalysis(unittest.TestCase):
    def _analyze(self, src):
        """ヘルパー: ソースを解析してStackInfo一覧を返す"""
        raw = src.split("\n")
        cl = creview.strip_comments_and_strings(src)
        return creview.analyze_file_stack("test.c", raw, cl)

    def test_small_function(self):
        """小さい関数のスタック推定"""
        src = 'void f() {\n    int x;\n    char c;\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].func_name, "f")
        # int(4) + char(1) = 5
        self.assertEqual(infos[0].estimated_bytes, 5)

    def test_large_array(self):
        """大きな配列でスタック超過検出"""
        src = 'void big() {\n    char buf[16384];\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].estimated_bytes, 16384)

    def test_multiple_arrays(self):
        """複数配列の合計"""
        src = 'void f() {\n    int a[100];\n    double b[200];\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        # int[100]=400, double[200]=1600
        self.assertEqual(infos[0].estimated_bytes, 2000)

    def test_2d_array(self):
        """2次元配列"""
        src = 'void f() {\n    int mat[10][20];\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        # int[10][20] = 4 * 10 * 20 = 800
        self.assertEqual(infos[0].estimated_bytes, 800)

    def test_alloca_detection(self):
        """alloca検出"""
        src = 'void f() {\n    char *p = alloca(100);\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertTrue(infos[0].has_alloca)

    def test_vla_detection(self):
        """VLA検出"""
        src = 'void f(int n) {\n    char buf[n];\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertTrue(infos[0].has_vla)

    def test_recursive_detection(self):
        """再帰検出"""
        src = 'void f(int n) {\n    int buf[1024];\n    f(n - 1);\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertTrue(infos[0].is_recursive)
        self.assertEqual(infos[0].estimated_bytes, 4096)

    def test_multiple_functions(self):
        """複数関数の解析"""
        src = ('void small() {\n    int x;\n}\n'
               'void big() {\n    char buf[8192];\n}\n')
        infos = self._analyze(src)
        self.assertEqual(len(infos), 2)
        names = {i.func_name for i in infos}
        self.assertIn("small", names)
        self.assertIn("big", names)

    def test_pointer_not_counted_as_array(self):
        """ポインタ変数は配列として大きく計上されない"""
        src = 'void f() {\n    char *p;\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        # ポインタは小さい (8以下)
        self.assertTrue(infos[0].estimated_bytes <= 8)


class TestCheckStackUsage(unittest.TestCase):
    def test_exceeds_threshold(self):
        """閾値超過で指摘"""
        import tempfile
        src = '#include <stdlib.h>\nvoid f() {\n    char buf[16384];\n}\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            issues = creview.run_local_analysis(path, ignore,
                                                 only_rules={"stack_usage"},
                                                 stack_threshold=8192)
            stack_issues = [i for i in issues if "スタック" in i.message]
            self.assertTrue(len(stack_issues) > 0)
            self.assertIn("16384", stack_issues[0].message)
        finally:
            os.unlink(path)

    def test_under_threshold_no_issue(self):
        """閾値以下は指摘なし"""
        import tempfile
        src = '#include <stdlib.h>\nvoid f() {\n    int x;\n}\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            issues = creview.run_local_analysis(path, ignore,
                                                 only_rules={"stack_usage"})
            stack_issues = [i for i in issues if "スタック" in i.message]
            self.assertEqual(len(stack_issues), 0)
        finally:
            os.unlink(path)

    def test_alloca_flagged(self):
        """alloca使用で指摘"""
        import tempfile
        src = '#include <stdlib.h>\nvoid f(int n) {\n    char *p = alloca(n);\n}\n'
        with tempfile.NamedTemporaryFile(mode='w', suffix='.c', delete=False,
                                          encoding='utf-8') as f:
            f.write(src)
            path = f.name
        try:
            ignore = creview.IgnoreConfig()
            issues = creview.run_local_analysis(path, ignore,
                                                 only_rules={"stack_usage"})
            alloca_issues = [i for i in issues if "alloca" in i.message]
            self.assertTrue(len(alloca_issues) > 0)
        finally:
            os.unlink(path)


class TestStackReport(unittest.TestCase):
    def test_report_format(self):
        """スタックレポートのフォーマット"""
        infos = [
            creview.StackInfo("big_func", 10, 16384,
                              [("char buf[16384]", 16384)],
                              False, False, False),
            creview.StackInfo("small_func", 20, 64, [],
                              False, False, False),
        ]
        report = creview.format_stack_report("test.c", infos)
        self.assertIn("big_func", report)
        self.assertIn("small_func", report)
        self.assertIn("16,384", report)
        self.assertIn("!!超過", report)
        self.assertIn("スタック解析", report)

    def test_report_with_flags(self):
        """alloca/VLA/再帰フラグの表示"""
        infos = [
            creview.StackInfo("risky", 5, 4096,
                              [("alloca()", -1)],
                              True, True, True),
        ]
        report = creview.format_stack_report("test.c", infos)
        self.assertIn("alloca", report)
        self.assertIn("VLA", report)
        self.assertIn("再帰", report)


class TestResolveSizeExpr(unittest.TestCase):
    def test_numeric(self):
        self.assertEqual(creview._resolve_size_expr("256", {}), 256)

    def test_sizeof_known(self):
        self.assertEqual(creview._resolve_size_expr("sizeof(buf)", {"buf": 1024}), 1024)

    def test_sizeof_minus(self):
        self.assertEqual(creview._resolve_size_expr("sizeof(buf) - 1", {"buf": 64}), 63)

    def test_unknown(self):
        self.assertEqual(creview._resolve_size_expr("len", {}), -1)


class TestBufUsage(unittest.TestCase):
    def _analyze(self, src):
        raw = src.split("\n")
        cl = creview.strip_comments_and_strings(src)
        return creview.analyze_buf_usage("test.c", raw, cl)

    def test_snprintf_bounded(self):
        """snprintfのサイズ制限を検出"""
        src = 'void f() {\n    char buf[1024];\n    snprintf(buf, 256, "hello");\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].name, "buf")
        self.assertEqual(infos[0].decl_size, 1024)
        self.assertEqual(infos[0].max_write, 256)
        self.assertAlmostEqual(infos[0].usage_pct, 25.0, places=0)

    def test_snprintf_sizeof(self):
        """snprintfでsizeof使用時、100%"""
        src = 'void f() {\n    char buf[64];\n    snprintf(buf, sizeof(buf), "x");\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].max_write, 64)
        self.assertAlmostEqual(infos[0].usage_pct, 100.0, places=0)

    def test_strcpy_unbounded(self):
        """strcpyは不定"""
        src = 'void f(char *s) {\n    char buf[64];\n    strcpy(buf, s);\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].max_write, -1)
        self.assertLess(infos[0].usage_pct, 0)

    def test_strcpy_literal(self):
        """strcpyでリテラルコピーはサイズ判定可能"""
        src = 'void f() {\n    char buf[64];\n    strcpy(buf, "hello");\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        # "hello" = 5文字 + NUL = 6バイト
        self.assertEqual(infos[0].max_write, 6)

    def test_strncpy_sizeof_minus_1(self):
        """strncpy(buf, src, sizeof(buf)-1)の解決"""
        src = 'void f(char *s) {\n    char buf[128];\n    strncpy(buf, s, sizeof(buf) - 1);\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].max_write, 127)

    def test_memcpy_fixed(self):
        """memcpy固定サイズ"""
        src = 'void f() {\n    char buf[256];\n    memcpy(buf, "data", 32);\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].max_write, 32)
        self.assertAlmostEqual(infos[0].usage_pct, 12.5, places=0)

    def test_read_sizeof(self):
        """readでsizeof使用"""
        src = 'void f() {\n    char buf[4096];\n    read(0, buf, sizeof(buf));\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].max_write, 4096)
        self.assertAlmostEqual(infos[0].usage_pct, 100.0, places=0)

    def test_fgets_bounded(self):
        """fgetsのサイズ制限"""
        src = 'void f() {\n    char line[256];\n    fgets(line, sizeof(line), stdin);\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].max_write, 256)

    def test_multiple_writes_max(self):
        """複数書き込みの最大値を使用"""
        src = ('void f() {\n    char buf[1024];\n'
               '    snprintf(buf, 100, "a");\n'
               '    snprintf(buf, 500, "b");\n}\n')
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].max_write, 500)
        self.assertEqual(len(infos[0].writes), 2)

    def test_no_writes_skipped(self):
        """書き込みなしのバッファはスキップ"""
        src = 'void f() {\n    char buf[64];\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 0)

    def test_sprintf_unbounded(self):
        """sprintfは不定"""
        src = 'void f(char *s) {\n    char buf[64];\n    sprintf(buf, "%s", s);\n}\n'
        infos = self._analyze(src)
        self.assertEqual(len(infos), 1)
        self.assertLess(infos[0].usage_pct, 0)


class TestBufReport(unittest.TestCase):
    def test_report_format(self):
        """レポートフォーマット"""
        infos = [
            creview.BufInfo("buf", 5, 1024, 1,
                            [creview.BufWrite(10, "snprintf", 256, "snprintf(buf, 256, ...);")],
                            256, 25.0),
        ]
        report = creview.format_buf_report("test.c", infos)
        self.assertIn("buf", report)
        self.assertIn("1,024B", report)
        self.assertIn("256B", report)
        self.assertIn("25%", report)
        self.assertIn("バッファ使用率", report)

    def test_report_unbounded(self):
        """不定バッファの表示"""
        infos = [
            creview.BufInfo("danger", 5, 64, 1,
                            [creview.BufWrite(10, "strcpy", -1, "strcpy(danger, input);")],
                            -1, -1.0),
        ]
        report = creview.format_buf_report("test.c", infos)
        self.assertIn("!!危険", report)
        self.assertIn("不定", report)


if __name__ == "__main__":
    unittest.main()
