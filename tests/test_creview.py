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


if __name__ == "__main__":
    unittest.main()
