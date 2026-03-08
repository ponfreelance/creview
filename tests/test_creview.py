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


if __name__ == "__main__":
    unittest.main()
