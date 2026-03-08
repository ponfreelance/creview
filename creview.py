#!/usr/bin/env python3
"""
creview v0.14.0 - C言語設計レビュー専用CLI
指摘専用。コード生成・修正案・改善案 一切なし。
ローカル静的解析 + Claude API深層レビュー 二段構成。
"""

import sys
import os
import re
import argparse
import json
import time
import signal
from dataclasses import dataclass
from enum import Enum
from typing import List, Dict, Set, Optional, Tuple
from pathlib import Path
from fnmatch import fnmatch
import subprocess

# ─── バージョン / 定数 ────────────────────────────────
VERSION = "0.14.1"
IGNOREFILE = ".creviewignore"
CONFIGFILE = "config.txt"
MAX_CHUNK_BYTES = 80_000
MAX_CHUNK_LINES = 800   # config.txtで変更可
DEFAULT_TIMEOUT = 60
DEFAULT_MODEL = "claude-sonnet-4-20250514"
# モデル廃止時のフォールバックチェーン
# config.txt指定 → DEFAULT → フォールバック順に試行
MODEL_FALLBACKS = [
    "claude-sonnet-4-20250514",
    "claude-sonnet-4-5-20250929",
    "claude-sonnet-4-20250514",
]
DEBUG = False


# ─── 固定プロンプト(コードレビュー用) ─────────────────
SYSTEM_PROMPT_CODE = """\
あなたはC言語の設計レビュー専門エンジニアです。
コード生成は禁止です。修正案提示は禁止です。改善案提示は禁止です。

あなたの役割は「危険箇所の指摘のみ」です。

次の3種類だけ出力してください：

【重大】
クラッシュ・未定義動作・メモリ破壊の可能性

【設計不明】
仕様が曖昧で将来事故になりうる箇所

【保守危険】
将来バグを誘発する設計依存

出力ルール：
- 初心者向け解説は禁止
- 一般論は禁止
- 抽象説明は禁止
- 行番号を必ず書く
- 指摘は具体コード根拠必須
- 推測で修正提案しない
- コードを書き換えない
- 新規コードを出さない
- 書き直し例を出さない

もし危険箇所が無い場合は
「重大なし」「設計不明なし」「保守危険なし」
のみ出力。

余計な文章は一切書かない。\
"""

# ─── 固定プロンプト(仕様レビュー用) ─────────────────
SYSTEM_PROMPT_SPEC = """\
あなたはCソフト設計レビュー専門です。

以下仕様から
- 実装時クラッシュ原因になる曖昧点
- 状態遷移不整合
- エラー処理不足
- API境界曖昧

のみ指摘してください。

禁止：
- 実装例提示
- 擬似コード提示
- 設計案提示
- ベストプラクティス説明

出力は
【重大】
【設計不明】
【保守危険】
のみ。

余計な文章は一切書かない。\
"""


class Severity(Enum):
    CRITICAL = "重大"
    DESIGN = "設計不明"
    MAINT = "保守危険"


@dataclass
class Issue:
    severity: Severity
    filepath: str
    line: int
    message: str


@dataclass
class IgnoreConfig:
    global_ok: bool = False
    macro_allow: bool = False
    volatile_ok: bool = False
    packed_ok: bool = False
    magic_ok: bool = False
    exclude_patterns: List[str] = None
    rule_off: Set[str] = None

    def __post_init__(self):
        if self.exclude_patterns is None:
            self.exclude_patterns = []
        if self.rule_off is None:
            self.rule_off = set()


@dataclass
class AppConfig:
    api_key: str = ""
    model: str = DEFAULT_MODEL
    timeout: int = DEFAULT_TIMEOUT
    max_chunk_lines: int = MAX_CHUNK_LINES
    debug: bool = False


def debug_log(msg: str, config: Optional['AppConfig'] = None):
    """DEBUG=trueのときだけstderrに出力"""
    if config and config.debug:
        print(f"[DEBUG] {msg}", file=sys.stderr)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# config.txt 読み込み
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def find_config() -> Optional[str]:
    """config.txtを探す: カレント → 実行ファイル隣 → HOME"""
    candidates = [
        os.path.join(os.getcwd(), CONFIGFILE),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIGFILE),
        os.path.join(Path.home(), f".creview/{CONFIGFILE}"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def check_config_sample_hint():
    """config.txtが無いがconfig.txt.sampleがある場合にヒント表示"""
    sample_candidates = [
        os.path.join(os.getcwd(), CONFIGFILE + ".sample"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIGFILE + ".sample"),
    ]
    for s in sample_candidates:
        if os.path.isfile(s):
            print(f"ヒント: {s} を config.txt にリネームしてAPIキーを入力してください",
                  file=sys.stderr)
            return


# テンプレ値のまま使おうとしている場合のキー
_TEMPLATE_KEYS = {"ここにAPIキー", "ここにClaudeキー", "your-api-key-here", "sk-ant-xxx", ""}


def load_config() -> AppConfig:
    cfg = AppConfig()
    path = find_config()
    if not path:
        check_config_sample_hint()
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().upper()
                val = val.strip()
                if key == "API_KEY":
                    # テンプレ値のままなら空として扱う
                    if val in _TEMPLATE_KEYS:
                        cfg.api_key = ""
                    else:
                        cfg.api_key = val
                elif key == "MODEL":
                    cfg.model = val
                elif key == "TIMEOUT":
                    try:
                        cfg.timeout = int(val)
                    except ValueError:
                        pass
                elif key == "MAX_CHUNK_LINES":
                    try:
                        cfg.max_chunk_lines = int(val)
                    except ValueError:
                        pass
                elif key == "DEBUG":
                    cfg.debug = val.lower() in ("true", "1", "yes")
    except OSError:
        pass
    return cfg


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# .creviewignore 読み込み
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_ignore(start_dir: str) -> IgnoreConfig:
    cfg = IgnoreConfig()
    path = os.path.join(start_dir, IGNOREFILE)
    if not os.path.isfile(path):
        return cfg
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            token = raw.strip().split("#")[0].strip()  # #以降はコメント
            if not token:
                continue
            if token == "GLOBAL_OK":
                cfg.global_ok = True
            elif token == "MACRO_ALLOW":
                cfg.macro_allow = True
            elif token == "VOLATILE_OK":
                cfg.volatile_ok = True
            elif token == "PACKED_OK":
                cfg.packed_ok = True
            elif token == "MAGIC_OK":
                cfg.magic_ok = True
            elif token.startswith("EXCLUDE "):
                pattern = token[8:].strip()
                if pattern:
                    cfg.exclude_patterns.append(pattern)
            elif token.startswith("RULE_OFF "):
                rule = token[9:].strip()
                if rule:
                    cfg.rule_off.add(rule)
    return cfg


def _ignore_has_any(ig: IgnoreConfig) -> bool:
    """ignoreフラグが1つでもTrueか"""
    return any([ig.global_ok, ig.macro_allow, ig.volatile_ok,
                ig.packed_ok, ig.magic_ok,
                len(ig.exclude_patterns) > 0, len(ig.rule_off) > 0])


def is_excluded(filepath: str, ignore: IgnoreConfig) -> bool:
    """ファイルがEXCLUDEパターンに一致するか"""
    name = os.path.basename(filepath)
    rel = filepath
    for pat in ignore.exclude_patterns:
        if fnmatch(name, pat) or fnmatch(rel, pat):
            return True
    return False


def find_ignore(file_path: str) -> IgnoreConfig:
    """ファイルのディレクトリ → カレントディレクトリの順で.creviewignoreを探す"""
    file_dir = os.path.dirname(os.path.abspath(file_path))
    ig = load_ignore(file_dir)
    if not _ignore_has_any(ig):
        ig = load_ignore(os.getcwd())
    return ig


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 前処理: コメント・文字列リテラル除去
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def strip_comments_and_strings(source: str) -> List[str]:
    """行番号を保持したまま、コメントと文字列リテラルを空白に置換"""
    lines = source.split("\n")
    in_block = False
    out_lines = []

    for line in lines:
        out = list(line)
        j = 0
        while j < len(out):
            if in_block:
                if j + 1 < len(out) and out[j] == '*' and out[j + 1] == '/':
                    out[j] = ' '
                    out[j + 1] = ' '
                    in_block = False
                    j += 2
                else:
                    out[j] = ' '
                    j += 1
            elif out[j] == '/' and j + 1 < len(out) and out[j + 1] == '/':
                for k in range(j, len(out)):
                    out[k] = ' '
                break
            elif out[j] == '/' and j + 1 < len(out) and out[j + 1] == '*':
                out[j] = ' '
                out[j + 1] = ' '
                in_block = True
                j += 2
            elif out[j] == '"':
                out[j] = ' '
                j += 1
                while j < len(out) and out[j] != '"':
                    if out[j] == '\\' and j + 1 < len(out):
                        out[j] = ' '
                        j += 1
                    out[j] = ' '
                    j += 1
                if j < len(out):
                    out[j] = ' '
                    j += 1
            elif out[j] == "'":
                out[j] = ' '
                j += 1
                while j < len(out) and out[j] != "'":
                    if out[j] == '\\' and j + 1 < len(out):
                        out[j] = ' '
                        j += 1
                    out[j] = ' '
                    j += 1
                if j < len(out):
                    out[j] = ' '
                    j += 1
            else:
                j += 1
        out_lines.append("".join(out))

    return out_lines


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ローカル静的解析パス群 (Phase 1)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def check_null_deref(fp, raw, cl, issues):
    alloc_re = re.compile(r'\b(\w+)\s*=\s*(?:malloc|calloc|realloc)\s*\(')
    for i, line in enumerate(cl):
        m = alloc_re.search(line)
        if m:
            var = m.group(1)
            found = False
            for j in range(i + 1, min(i + 6, len(cl))):
                if re.search(rf'\b{re.escape(var)}\s*==\s*NULL\b', cl[j]) or \
                   re.search(rf'!\s*{re.escape(var)}\b', cl[j]) or \
                   re.search(rf'\b{re.escape(var)}\s*!=\s*NULL\b', cl[j]):
                    found = True
                    break
            if not found:
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"{var}にNULLチェックなし。malloc/calloc/realloc失敗時クラッシュ"))


def check_unsafe_funcs(fp, raw, cl, issues):
    dangerous = {
        "gets":    "gets使用。バッファ長制限なし、確実にオーバーフロー可能",
        "sprintf": "sprintf使用。出力バッファ長未検証でオーバーフロー可能",
        "strcpy":  "strcpy使用。コピー元長未検証でオーバーフロー可能",
        "strcat":  "strcat使用。結合後長未検証でオーバーフロー可能",
    }
    for i, line in enumerate(cl):
        for func, msg in dangerous.items():
            if re.search(rf'\b{func}\s*\(', line):
                issues.append(Issue(Severity.CRITICAL, fp, i + 1, msg))


def check_memcpy_no_null(fp, raw, cl, issues):
    mem_re = re.compile(r'\b(memcpy|memmove|memset)\s*\(\s*(\w+)')
    for i, line in enumerate(cl):
        m = mem_re.search(line)
        if m:
            func, dest = m.group(1), m.group(2)
            found = False
            for j in range(max(0, i - 5), i):
                if re.search(rf'\b{re.escape(dest)}\s*==\s*NULL', cl[j]) or \
                   re.search(rf'!\s*{re.escape(dest)}\b', cl[j]) or \
                   re.search(rf'\b{re.escape(dest)}\s*!=\s*NULL', cl[j]):
                    found = True
                    break
            if not found:
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"{dest}のNULL未検証で{func}実行。NULLポインタ渡しでクラッシュ"))


def check_array_index(fp, raw, cl, issues):
    scanf_vars: Dict[str, int] = {}
    scanf_re = re.compile(r'\bscanf\s*\(.*?&(\w+)')
    for i, line in enumerate(cl):
        m = scanf_re.search(line)
        if m:
            scanf_vars[m.group(1)] = i + 1
    idx_re = re.compile(r'\w+\[\s*(\w+)\s*\]')
    for i, line in enumerate(cl):
        for m in idx_re.finditer(line):
            var = m.group(1)
            if var in scanf_vars:
                found = False
                for j in range(scanf_vars[var], i):
                    if re.search(rf'\b{re.escape(var)}\s*[<>]=?\s*\d+', cl[j]) or \
                       re.search(rf'\d+\s*[<>]=?\s*{re.escape(var)}', cl[j]):
                        found = True
                        break
                if not found:
                    issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                        f"外部入力{var}を境界チェックなしで配列添字に使用。範囲外アクセス"))


def check_double_free(fp, raw, cl, issues):
    free_re = re.compile(r'\bfree\s*\(\s*(\w+)\s*\)')
    freed: Dict[str, int] = {}
    for i, line in enumerate(cl):
        m = free_re.search(line)
        if m:
            var = m.group(1)
            if var in freed:
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"{var}の二重free。{freed[var]}行目で既にfree済み"))
            freed[var] = i + 1
            if i + 1 < len(cl):
                if not re.search(rf'\b{re.escape(var)}\s*=\s*NULL', cl[i + 1]):
                    issues.append(Issue(Severity.MAINT, fp, i + 1,
                        f"free({var})直後にNULL代入なし。dangling pointer残存"))
        # 再代入でfreed解除
        for fvar in list(freed.keys()):
            if re.search(rf'\b{re.escape(fvar)}\s*=\s*(?!.*free)', line) and freed.get(fvar, -1) != i + 1:
                del freed[fvar]


def check_return_inconsistency(fp, raw, cl, issues):
    func_re = re.compile(r'^(\w[\w\s\*]*?)\s+(\w+)\s*\(')
    i = 0
    while i < len(cl):
        fm = func_re.match(cl[i])
        if fm and '{' in cl[i]:
            ret_type = fm.group(1).strip()
            func_name = fm.group(2)
            if ret_type == "void":
                i += 1
                continue
            brace = 0
            has_return_val = False
            has_bare_return = False
            func_start = i
            for j in range(i, len(cl)):
                brace += cl[j].count('{') - cl[j].count('}')
                if re.search(r'\breturn\s+\S', cl[j]):
                    has_return_val = True
                elif re.search(r'\breturn\s*;', cl[j]):
                    has_bare_return = True
                if brace <= 0 and j > i:
                    if has_return_val and has_bare_return:
                        issues.append(Issue(Severity.DESIGN, fp, func_start + 1,
                            f"関数{func_name}: 値returnと空returnが混在。戻り値仕様不一致"))
                    i = j
                    break
        i += 1


def check_globals(fp, raw, cl, issues, ignore):
    if ignore.global_ok:
        return
    brace_depth = 0
    global_vars: Set[str] = set()
    var_decl_re = re.compile(
        r'^(?:static\s+)?(?:volatile\s+)?(?:const\s+)?'
        r'(?:unsigned\s+|signed\s+)?'
        r'(?:int|char|short|long|float|double|size_t|uint\w+|int\w+)\s+'
        r'(\w+)\s*[;=\[]')
    for i, line in enumerate(cl):
        brace_depth += line.count('{') - line.count('}')
        if brace_depth == 0:
            m = var_decl_re.match(line.strip())
            if m:
                global_vars.add(m.group(1))
    brace_depth = 0
    in_func = False
    for i, line in enumerate(cl):
        if '{' in line and brace_depth == 0:
            in_func = True
        brace_depth += line.count('{') - line.count('}')
        if brace_depth == 0:
            in_func = False
        if in_func:
            for gv in global_vars:
                if re.search(rf'\b{re.escape(gv)}\s*[+\-\*/%&|^]?=\s*', line):
                    issues.append(Issue(Severity.MAINT, fp, i + 1,
                        f"グローバル変数{gv}を関数内で直接更新。競合・副作用リスク"))


def check_macros(fp, raw, cl, issues, ignore):
    if ignore.macro_allow:
        return
    for i, rl in enumerate(raw):
        stripped = rl.strip()
        if stripped.startswith("#define"):
            if stripped.count(';') >= 2:
                issues.append(Issue(Severity.MAINT, fp, i + 1,
                    "複数文マクロ。do{}while(0)未使用ならif文内で暴発"))
            m = re.match(r'#define\s+\w+\(([^)]+)\)\s+(.*)', stripped)
            if m:
                params = [p.strip() for p in m.group(1).split(',')]
                body = m.group(2)
                for p in params:
                    if re.search(rf'(?<!\(){re.escape(p)}(?!\))\s*[\+\-\*/]', body) or \
                       re.search(rf'[\+\-\*/]\s*(?<!\(){re.escape(p)}(?!\))', body):
                        issues.append(Issue(Severity.DESIGN, fp, i + 1,
                            f"マクロ引数{p}が括弧未保護。展開時に演算子優先順位誤り"))
                        break


def check_switch_fallthrough(fp, raw, cl, issues):
    in_switch = False
    case_line = -1
    has_break = False
    for i, line in enumerate(cl):
        if re.search(r'\bswitch\s*\(', line):
            in_switch = True
            has_break = True
            case_line = -1
        if in_switch:
            if re.search(r'\bcase\s+', line) or re.search(r'\bdefault\s*:', line):
                if case_line >= 0 and not has_break:
                    issues.append(Issue(Severity.DESIGN, fp, case_line,
                        "case fall-through。break/return無しで次caseに落下"))
                case_line = i + 1
                has_break = False
            if re.search(r'\bbreak\s*;', line) or re.search(r'\breturn\b', line):
                has_break = True


def check_sizeof_pointer(fp, raw, cl, issues):
    ptr_vars: Set[str] = set()
    ptr_re = re.compile(r'\b\w+\s*\*\s*(\w+)\s*[;=]')
    for line in cl:
        for m in ptr_re.finditer(line):
            ptr_vars.add(m.group(1))
    sizeof_re = re.compile(r'\bsizeof\s*\(\s*(\w+)\s*\)')
    for i, line in enumerate(cl):
        for m in sizeof_re.finditer(line):
            var = m.group(1)
            if var in ptr_vars:
                if not re.search(rf'sizeof\s*\(\s*{re.escape(var)}\s*\)\s*/', line):
                    issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                        f"sizeof({var})はポインタサイズ(4/8byte)を返す。配列長にならない"))


def check_fd_leak(fp, raw, cl, issues):
    fopen_re = re.compile(r'(\w+)\s*=\s*fopen\s*\(')
    opened: Dict[str, int] = {}
    for i, line in enumerate(cl):
        m = fopen_re.search(line)
        if m:
            opened[m.group(1)] = i + 1
    full_text = "\n".join(cl)
    for var, line_num in opened.items():
        if not re.search(rf'\bfclose\s*\(\s*{re.escape(var)}\s*\)', full_text):
            issues.append(Issue(Severity.CRITICAL, fp, line_num,
                f"fopen結果{var}に対応するfcloseなし。ファイルディスクリプタリーク"))


def check_magic_numbers(fp, raw, cl, issues, ignore):
    if ignore.magic_ok:
        return
    magic_re = re.compile(r'(?<!\w)(\d{2,})(?!\w)')
    brace_depth = 0
    trivial = {"0", "1", "2", "10", "100", "1000", "00", "01"}
    for i, line in enumerate(cl):
        prev_depth = brace_depth
        brace_depth += line.count('{') - line.count('}')
        if prev_depth >= 1:
            for m in magic_re.finditer(line):
                val = m.group(1)
                if val not in trivial:
                    if not raw[i].strip().startswith("#"):
                        issues.append(Issue(Severity.MAINT, fp, i + 1,
                            f"マジックナンバー{val}。定数定義なしで意味不明・変更困難"))
                        break


def check_volatile(fp, raw, cl, issues, ignore):
    """volatile変数の非アトミック複合操作検出"""
    if ignore.volatile_ok:
        return
    volatile_vars: Set[str] = set()
    vol_re = re.compile(
        r'\bvolatile\s+(?:unsigned\s+|signed\s+)?'
        r'(?:int|char|short|long|float|double|uint\w+|int\w+|size_t|_Bool|bool)\s+'
        r'(\w+)')
    vol_re2 = re.compile(
        r'(?:unsigned\s+|signed\s+)?'
        r'(?:int|char|short|long|float|double|uint\w+|int\w+|size_t|_Bool|bool)\s+'
        r'volatile\s+(\w+)')
    for line in cl:
        for m in vol_re.finditer(line):
            volatile_vars.add(m.group(1))
        for m in vol_re2.finditer(line):
            volatile_vars.add(m.group(1))
    if not volatile_vars:
        return
    for i, line in enumerate(cl):
        for var in volatile_vars:
            if re.search(rf'\b{re.escape(var)}\s*\+\+', line) or \
               re.search(rf'\+\+\s*{re.escape(var)}\b', line) or \
               re.search(rf'\b{re.escape(var)}\s*--', line) or \
               re.search(rf'--\s*{re.escape(var)}\b', line):
                issues.append(Issue(Severity.DESIGN, fp, i + 1,
                    f"volatile変数{var}に++/--使用。read-modify-writeは非アトミック、割り込み競合の危険"))
            elif re.search(rf'\b{re.escape(var)}\s*(?:[+\-*/%&|^]|<<|>>)=', line):
                issues.append(Issue(Severity.DESIGN, fp, i + 1,
                    f"volatile変数{var}に複合代入使用。read-modify-writeは非アトミック、割り込み競合の危険"))


def check_format_string(fp, raw, cl, issues):
    """printf系関数のフォーマット文字列脆弱性検出"""
    # printf系で第一引数(or第二引数)が変数のケース
    # printf(var), fprintf(fp, var), sprintf(buf, var), snprintf(buf, n, var)
    # 安全: printf("literal"), printf("%d", var)
    fmt_funcs_1arg = re.compile(r'\b(printf|puts)\s*\(\s*(\w+)\s*[,)]')
    fmt_funcs_2arg = re.compile(r'\b(fprintf|vfprintf)\s*\(\s*\w+\s*,\s*(\w+)\s*[,)]')
    fmt_funcs_3arg = re.compile(r'\b(sprintf|snprintf)\s*\(\s*\w+\s*,\s*(?:\w+\s*,\s*)?(\w+)\s*[,)]')
    for i, line in enumerate(cl):
        for pat in [fmt_funcs_1arg, fmt_funcs_2arg, fmt_funcs_3arg]:
            m = pat.search(line)
            if m:
                func, var = m.group(1), m.group(2)
                # リテラル文字列は strip_comments_and_strings で消えているので
                # 変数名が残っている = ユーザ入力の可能性
                if var not in ("NULL", "stderr", "stdout", "stdin"):
                    issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                        f"{func}の書式文字列に変数{var}を直接使用。"
                        f"攻撃者制御で任意メモリ読み書き可能"))


def check_use_after_free(fp, raw, cl, issues):
    """free後のポインタ使用検出"""
    free_re = re.compile(r'\bfree\s*\(\s*(\w+)\s*\)')
    for i, line in enumerate(cl):
        m = free_re.search(line)
        if m:
            var = m.group(1)
            # free直後の行からスコープ末尾まで使用を追跡
            for j in range(i + 1, min(i + 30, len(cl))):
                # NULL代入 → 追跡終了
                if re.search(rf'\b{re.escape(var)}\s*=\s*NULL\b', cl[j]):
                    break
                # 再代入 → 追跡終了
                if re.search(rf'\b{re.escape(var)}\s*=\s*(?!NULL)', cl[j]):
                    break
                # スコープ終了
                if cl[j].strip() == '}':
                    break
                # 再度free → double-free（別チェッカーで検出済み）
                if re.search(rf'\bfree\s*\(\s*{re.escape(var)}\s*\)', cl[j]):
                    break
                # ポインタ参照（デリファレンス、メンバアクセス、配列添字、関数引数）
                if re.search(rf'\b{re.escape(var)}\s*->', cl[j]) or \
                   re.search(rf'\b{re.escape(var)}\s*\[', cl[j]) or \
                   re.search(rf'\*\s*{re.escape(var)}\b', cl[j]):
                    issues.append(Issue(Severity.CRITICAL, fp, j + 1,
                        f"free済み{var}を参照(use-after-free)。{i+1}行目でfree済み"))
                    break


def check_uninitialized(fp, raw, cl, issues):
    """未初期化ローカル変数の使用検出"""
    # ポインタ・整数型のローカル変数宣言（初期化子なし）を検出
    decl_re = re.compile(
        r'^\s+(?:const\s+)?(?:unsigned\s+|signed\s+)?'
        r'(?:int|char|short|long|float|double|size_t|uint\w+|int\w+|_Bool|bool)\s+'
        r'(\w+)\s*;')
    ptr_decl_re = re.compile(
        r'^\s+(?:const\s+)?(?:unsigned\s+|signed\s+)?'
        r'(?:int|char|short|long|float|double|size_t|uint\w+|int\w+|void|struct\s+\w+)\s*'
        r'\*\s*(\w+)\s*;')
    brace_depth = 0
    for i, line in enumerate(cl):
        brace_depth += line.count('{') - line.count('}')
        if brace_depth < 1:
            continue
        m = decl_re.match(line) or ptr_decl_re.match(line)
        if not m:
            continue
        var = m.group(1)
        # 後続行で初期化前に使用されていないか確認
        initialized = False
        for j in range(i + 1, min(i + 15, len(cl))):
            # 代入で初期化
            if re.search(rf'\b{re.escape(var)}\s*=', cl[j]):
                initialized = True
                break
            # 関数引数に&varで渡す（出力パラメータ）
            if re.search(rf'&\s*{re.escape(var)}\b', cl[j]):
                initialized = True
                break
            # スコープ終了
            if cl[j].strip() == '}':
                break
            # 使用検出（右辺値、関数引数、配列添字、演算）
            if re.search(rf'(?<!=)\b{re.escape(var)}\b(?!\s*=)', cl[j]) and \
               not re.search(rf'&\s*{re.escape(var)}\b', cl[j]):
                # 宣言行でないことを確認
                if not decl_re.match(cl[j]) and not ptr_decl_re.match(cl[j]):
                    issues.append(Issue(Severity.CRITICAL, fp, j + 1,
                        f"未初期化変数{var}を使用({i+1}行目で宣言、初期化なし)。不定値"))
                    break


def check_sign_compare(fp, raw, cl, issues):
    """signed/unsigned混合比較検出"""
    # unsigned型変数の収集
    unsigned_vars: Set[str] = set()
    unsigned_re = re.compile(
        r'\b(?:unsigned\s+(?:int|char|short|long)|uint\w+|size_t)\s+(\w+)')
    signed_re = re.compile(
        r'\b(?:(?:signed\s+)?(?:int|short|long))\s+(\w+)')
    signed_vars: Set[str] = set()
    for line in cl:
        for m in unsigned_re.finditer(line):
            unsigned_vars.add(m.group(1))
        for m in signed_re.finditer(line):
            signed_vars.add(m.group(1))
    if not unsigned_vars or not signed_vars:
        return
    cmp_re = re.compile(r'(\w+)\s*([<>!=]=?)\s*(\w+)')
    for i, line in enumerate(cl):
        for m in cmp_re.finditer(line):
            lhs, op, rhs = m.group(1), m.group(2), m.group(3)
            if (lhs in unsigned_vars and rhs in signed_vars) or \
               (lhs in signed_vars and rhs in unsigned_vars):
                issues.append(Issue(Severity.DESIGN, fp, i + 1,
                    f"signed({lhs if lhs in signed_vars else rhs})と"
                    f"unsigned({lhs if lhs in unsigned_vars else rhs})の比較。"
                    f"暗黙変換で負値が巨大正値に化ける"))


def check_integer_overflow(fp, raw, cl, issues):
    """整数オーバーフロー検出（malloc引数の乗算等）"""
    # malloc(n * sizeof(...)) パターン
    mul_alloc_re = re.compile(
        r'\b(?:malloc|calloc|realloc)\s*\(\s*(\w+)\s*\*\s*(?:sizeof\b|(\w+))')
    for i, line in enumerate(cl):
        m = mul_alloc_re.search(line)
        if m:
            var = m.group(1)
            # 直前にオーバーフローチェックがあるか
            found = False
            for j in range(max(0, i - 5), i):
                if re.search(rf'\b{re.escape(var)}\b.*(?:MAX|LIMIT|SIZE_MAX|overflow)', cl[j], re.IGNORECASE) or \
                   re.search(rf'if\s*\(.*{re.escape(var)}', cl[j]):
                    found = True
                    break
            if not found:
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"malloc引数で{var}を乗算。オーバーフロー未検証でヒープ不足クラッシュ"))


def check_resource_leak(fp, raw, cl, issues):
    """socket/open/pipe等のリソースリーク検出"""
    # open() のfd
    open_re = re.compile(r'(\w+)\s*=\s*\bopen\s*\(')
    socket_re = re.compile(r'(\w+)\s*=\s*\bsocket\s*\(')
    pipe_re = re.compile(r'\bpipe\s*\(\s*(\w+)\s*\)')
    full_text = "\n".join(cl)
    for i, line in enumerate(cl):
        m = open_re.search(line)
        if m:
            var = m.group(1)
            if not re.search(rf'\bclose\s*\(\s*{re.escape(var)}\s*\)', full_text):
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"open結果{var}に対応するcloseなし。fdリーク"))
        m = socket_re.search(line)
        if m:
            var = m.group(1)
            if not re.search(rf'\bclose\s*\(\s*{re.escape(var)}\s*\)', full_text) and \
               not re.search(rf'\bclosesocket\s*\(\s*{re.escape(var)}\s*\)', full_text):
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"socket結果{var}に対応するcloseなし。ソケットリーク"))
        m = pipe_re.search(line)
        if m:
            arr = m.group(1)
            if not re.search(rf'\bclose\s*\(\s*{re.escape(arr)}\s*\[', full_text):
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"pipe({arr})に対応するcloseなし。fdリーク"))


def check_snprintf_retval(fp, raw, cl, issues):
    """snprintf戻り値無視検出"""
    # snprintf単独呼び出し（戻り値を変数に代入していない）
    snprintf_re = re.compile(r'^\s*snprintf\s*\(')
    for i, line in enumerate(cl):
        if snprintf_re.match(line):
            issues.append(Issue(Severity.MAINT, fp, i + 1,
                "snprintf戻り値を未確認。切り詰め発生を検出できない"))


def check_buffer_overrun(fp, raw, cl, issues):
    """固定バッファへのstrncpy/memcpyサイズ超過検出"""
    # char buf[N] の宣言を収集
    arr_sizes: Dict[str, Tuple[int, int]] = {}  # var -> (size, line)
    arr_re = re.compile(r'\bchar\s+(\w+)\s*\[\s*(\d+)\s*\]')
    for i, line in enumerate(cl):
        for m in arr_re.finditer(line):
            arr_sizes[m.group(1)] = (int(m.group(2)), i + 1)
    if not arr_sizes:
        return
    # strncpy(dst, src, n) / memcpy(dst, src, n) でnがバッファサイズ超過
    copy_re = re.compile(r'\b(strncpy|memcpy|memmove)\s*\(\s*(\w+)\s*,\s*[^,]+,\s*(\d+)\s*\)')
    for i, line in enumerate(cl):
        for m in copy_re.finditer(line):
            func, dst, size_str = m.group(1), m.group(2), m.group(3)
            size = int(size_str)
            if dst in arr_sizes:
                buf_size = arr_sizes[dst][0]
                if size > buf_size:
                    issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                        f"{func}の第3引数{size}がバッファ{dst}[{buf_size}]を超過。バッファオーバーラン"))


def check_null_deref_branch(fp, raw, cl, issues):
    """NULLチェック直後のfall-through使用検出"""
    # if (ptr == NULL) { error処理 } の直後にptrを使用（}漏れなど）
    null_check_re = re.compile(r'\bif\s*\(\s*(\w+)\s*==\s*NULL\s*\)')
    null_check_neg_re = re.compile(r'\bif\s*\(\s*!\s*(\w+)\s*\)')
    for i, line in enumerate(cl):
        m = null_check_re.search(line) or null_check_neg_re.search(line)
        if not m:
            continue
        var = m.group(1)
        # if文のブロックを追跡
        if '{' not in line:
            continue
        brace = 0
        block_end = -1
        for j in range(i, min(i + 20, len(cl))):
            brace += cl[j].count('{') - cl[j].count('}')
            if brace <= 0 and j > i:
                block_end = j
                break
        if block_end < 0:
            continue
        # ブロック内にreturn/exit/gotoがなければ、NULLチェックが不完全
        has_exit = False
        for j in range(i, block_end + 1):
            if re.search(r'\b(return|exit|goto|abort|_exit)\b', cl[j]):
                has_exit = True
                break
        if not has_exit:
            continue
        # ブロック直後でポインタ使用（NULLの場合を除外できていない可能性はelseで処理されるが
        # elseなしの場合をチェック）
        for j in range(block_end + 1, min(block_end + 5, len(cl))):
            if re.search(r'\belse\b', cl[j]):
                break
            if re.search(rf'\b{re.escape(var)}\s*->', cl[j]) or \
               re.search(rf'\b{re.escape(var)}\s*\[', cl[j]) or \
               re.search(rf'\*\s*{re.escape(var)}\b', cl[j]):
                # NULLチェック後にreturn等で抜けているなら安全
                # ここに到達 = チェック後にfall-throughで使用
                break  # 正常パターン（NULLならreturn、非NULLなら使用）


def check_infinite_loop(fp, raw, cl, issues):
    """break/returnなしの無限ループ検出"""
    loop_re = re.compile(r'\b(while\s*\(\s*1\s*\)|while\s*\(\s*true\s*\)|for\s*\(\s*;\s*;\s*\))')
    for i, line in enumerate(cl):
        if not loop_re.search(line):
            continue
        if '{' not in line:
            # 次の行に{があるか
            if i + 1 < len(cl) and '{' in cl[i + 1]:
                start = i + 1
            else:
                continue
        else:
            start = i
        brace = 0
        has_break = False
        for j in range(start, min(start + 200, len(cl))):
            brace += cl[j].count('{') - cl[j].count('}')
            if re.search(r'\b(break|return|goto|exit|abort|_exit)\s*[;(]', cl[j]) and j > start:
                has_break = True
            if brace <= 0 and j > start:
                if not has_break:
                    issues.append(Issue(Severity.DESIGN, fp, i + 1,
                        "無限ループにbreak/return/gotoなし。意図的でなければハングアップ"))
                break


def check_enum_switch(fp, raw, cl, issues):
    """enum型switchでdefaultなし検出"""
    # enum宣言の収集
    enum_re = re.compile(r'\benum\s+(\w+)')
    enum_types: Set[str] = set()
    for line in cl:
        m = enum_re.search(line)
        if m:
            enum_types.add(m.group(1))
    if not enum_types:
        return
    # enum型変数の収集
    enum_vars: Set[str] = set()
    for etype in enum_types:
        var_re = re.compile(rf'\benum\s+{re.escape(etype)}\s+(\w+)')
        for line in cl:
            for m in var_re.finditer(line):
                enum_vars.add(m.group(1))
    if not enum_vars:
        return
    # switch文でenum変数が使われているか、defaultがあるか
    switch_re = re.compile(r'\bswitch\s*\(\s*(\w+)\s*\)')
    for i, line in enumerate(cl):
        m = switch_re.search(line)
        if not m:
            continue
        var = m.group(1)
        if var not in enum_vars:
            continue
        # switchブロック内にdefaultがあるか
        brace = 0
        has_default = False
        for j in range(i, min(i + 100, len(cl))):
            brace += cl[j].count('{') - cl[j].count('}')
            if re.search(r'\bdefault\s*:', cl[j]):
                has_default = True
            if brace <= 0 and j > i:
                if not has_default:
                    issues.append(Issue(Severity.DESIGN, fp, i + 1,
                        f"enum変数{var}のswitchにdefaultなし。enum追加時に未処理ケース発生"))
                break


def check_recursive_no_limit(fp, raw, cl, issues):
    """深さ制限なし自己再帰関数検出"""
    func_re = re.compile(r'^(\w[\w\s\*]*?)\s+(\w+)\s*\(')
    i = 0
    while i < len(cl):
        fm = func_re.match(cl[i])
        if not fm or '{' not in cl[i]:
            i += 1
            continue
        func_name = fm.group(2)
        # 関数本体を探索
        brace = 0
        has_self_call = False
        has_depth_check = False
        func_start = i
        for j in range(i, len(cl)):
            brace += cl[j].count('{') - cl[j].count('}')
            if j > i:
                # 自己呼び出し検出
                if re.search(rf'\b{re.escape(func_name)}\s*\(', cl[j]):
                    has_self_call = True
                # 深さ制限パターン検出
                if re.search(r'\b(depth|level|count|limit|max_depth|recursion)\b', cl[j], re.IGNORECASE):
                    has_depth_check = True
            if brace <= 0 and j > i:
                if has_self_call and not has_depth_check:
                    issues.append(Issue(Severity.MAINT, fp, func_start + 1,
                        f"関数{func_name}が自己再帰。深さ制限なしでスタックオーバーフローの危険"))
                i = j
                break
        i += 1


def check_mutex_unlock(fp, raw, cl, issues):
    """pthread_mutex_lockに対応するunlockなし検出"""
    lock_re = re.compile(r'\bpthread_mutex_lock\s*\(\s*&?\s*(\w+)\s*\)')
    unlock_re_tmpl = r'\bpthread_mutex_unlock\s*\(\s*&?\s*{}\s*\)'
    full_text = "\n".join(cl)
    for i, line in enumerate(cl):
        m = lock_re.search(line)
        if m:
            mutex = m.group(1)
            if not re.search(unlock_re_tmpl.format(re.escape(mutex)), full_text):
                issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                    f"pthread_mutex_lock({mutex})に対応するunlockなし。デッドロック"))


def check_toctou(fp, raw, cl, issues):
    """TOCTOU (Time-of-check to time-of-use) 競合検出"""
    # access() → open() パターン
    check_funcs = {"access", "stat", "lstat", "fstat"}
    use_funcs = {"open", "fopen", "remove", "unlink", "rename", "chmod", "chown", "mkdir"}
    for i, line in enumerate(cl):
        for cfunc in check_funcs:
            if not re.search(rf'\b{cfunc}\s*\(', line):
                continue
            # 直後数行でuse関数が呼ばれていないか
            for j in range(i + 1, min(i + 10, len(cl))):
                for ufunc in use_funcs:
                    if re.search(rf'\b{ufunc}\s*\(', cl[j]):
                        issues.append(Issue(Severity.CRITICAL, fp, i + 1,
                            f"{cfunc}()後に{ufunc}()を使用(TOCTOU)。"
                            f"チェックと操作の間にファイルが変更される競合条件"))
                        break
                else:
                    continue
                break


def check_signal_unsafe(fp, raw, cl, issues):
    """シグナルハンドラ内のasync-signal-unsafe関数呼び出し検出"""
    # async-signal-unsafeな関数群
    unsafe_in_signal = {
        "printf", "fprintf", "sprintf", "snprintf", "puts",
        "malloc", "calloc", "realloc", "free",
        "exit", "fopen", "fclose", "fread", "fwrite",
        "syslog", "strerror", "localtime", "gmtime",
    }
    # signal()/sigaction()でハンドラ関数名を収集
    handler_re = re.compile(r'\bsignal\s*\(\s*\w+\s*,\s*(\w+)\s*\)')
    sa_handler_re = re.compile(r'\.sa_handler\s*=\s*(\w+)')
    handlers: Set[str] = set()
    for line in cl:
        m = handler_re.search(line)
        if m and m.group(1) not in ("SIG_IGN", "SIG_DFL"):
            handlers.add(m.group(1))
        m = sa_handler_re.search(line)
        if m:
            handlers.add(m.group(1))
    if not handlers:
        return
    # ハンドラ関数の本体を探索
    func_re = re.compile(r'^(?:static\s+)?(?:void|int)\s+(\w+)\s*\(')
    i = 0
    while i < len(cl):
        fm = func_re.match(cl[i])
        if not fm or fm.group(1) not in handlers or '{' not in cl[i]:
            i += 1
            continue
        func_name = fm.group(1)
        brace = 0
        for j in range(i, len(cl)):
            brace += cl[j].count('{') - cl[j].count('}')
            if j > i:
                for uf in unsafe_in_signal:
                    if re.search(rf'\b{uf}\s*\(', cl[j]):
                        issues.append(Issue(Severity.CRITICAL, fp, j + 1,
                            f"シグナルハンドラ{func_name}内で{uf}()使用。"
                            f"async-signal-unsafe関数でデッドロック・未定義動作"))
            if brace <= 0 and j > i:
                i = j
                break
        i += 1


def check_cast_truncation(fp, raw, cl, issues):
    """暗黙切り詰めキャスト検出"""
    # (int)long_var, (short)int_var, (char)int_var 等
    wider_types = {"long", "size_t", "uint64_t", "int64_t", "ssize_t", "off_t", "ptrdiff_t"}
    narrow_types = {"int", "short", "char", "uint8_t", "int8_t", "uint16_t", "int16_t", "uint32_t", "int32_t"}
    # 明示的キャスト
    cast_re = re.compile(r'\(\s*((?:unsigned\s+)?(?:' +
                          '|'.join(narrow_types) +
                          r'))\s*\)\s*(\w+)')
    # wide型変数の収集
    wide_vars: Set[str] = set()
    wide_re = re.compile(r'\b(' + '|'.join(wider_types) + r')\s+(\w+)')
    for line in cl:
        for m in wide_re.finditer(line):
            wide_vars.add(m.group(2))
    if not wide_vars:
        return
    for i, line in enumerate(cl):
        for m in cast_re.finditer(line):
            target_type, var = m.group(1), m.group(2)
            if var in wide_vars:
                issues.append(Issue(Severity.DESIGN, fp, i + 1,
                    f"({target_type}){var}でワイド型を切り詰め。上位ビット消失"))


def check_bitfield_sign(fp, raw, cl, issues):
    """ビットフィールドの符号未指定検出"""
    # struct内の int field:N (1ビットのintは-1 or 0で処理系依存)
    # unsigned int / signed int は明示済みなので除外
    bf_re = re.compile(r'(?<!\bunsigned\s)(?<!\bsigned\s)\bint\s+(\w+)\s*:\s*(\d+)\s*;')
    for i, line in enumerate(cl):
        m = bf_re.search(line)
        if m:
            field, bits = m.group(1), int(m.group(2))
            if bits <= 16:
                issues.append(Issue(Severity.DESIGN, fp, i + 1,
                    f"ビットフィールド{field}:{bits}が符号未指定int。"
                    f"signedかunsignedか処理系依存"))


def check_vla(fp, raw, cl, issues):
    """可変長配列(VLA)使用検出"""
    # 関数内で type arr[var] パターン (varが数値リテラルでない)
    arr_re = re.compile(
        r'\b(?:int|char|short|long|float|double|unsigned\s+\w+|uint\w+|int\w+)\s+'
        r'(\w+)\s*\[\s*([a-zA-Z_]\w*)\s*\]')
    brace_depth = 0
    for i, line in enumerate(cl):
        brace_depth += line.count('{') - line.count('}')
        if brace_depth < 1:
            continue
        m = arr_re.search(line)
        if m:
            arr_name, size_var = m.group(1), m.group(2)
            # sizeof, 定数名の除外
            if size_var.isupper() or size_var.startswith("sizeof"):
                continue
            issues.append(Issue(Severity.MAINT, fp, i + 1,
                f"可変長配列{arr_name}[{size_var}]使用。"
                f"大きな値でスタックオーバーフロー"))


def check_goto_misuse(fp, raw, cl, issues):
    """goto前方ジャンプ検出（エラー処理以外）"""
    # gotoの使用箇所とラベル位置を収集
    goto_re = re.compile(r'\bgoto\s+(\w+)\s*;')
    label_re = re.compile(r'^(\w+)\s*:(?!:)')  # ::はC++スコープ解決演算子除外
    labels: Dict[str, int] = {}  # label -> line
    gotos: List[Tuple[int, str]] = []  # (line, label)
    for i, line in enumerate(cl):
        m = label_re.match(line.strip())
        if m and m.group(1) not in ("default", "case", "public", "private", "protected"):
            labels[m.group(1)] = i
        m = goto_re.search(line)
        if m:
            gotos.append((i, m.group(1)))
    # 前方ジャンプ（上方向へのgoto）を検出
    for goto_line, label_name in gotos:
        if label_name in labels:
            label_line = labels[label_name]
            if label_line < goto_line:
                issues.append(Issue(Severity.MAINT, fp, goto_line + 1,
                    f"goto {label_name}が前方(上方向)ジャンプ。"
                    f"ループ構造化を推奨。可読性・保守性低下"))


def check_packed(fp, raw, cl, issues, ignore):
    """packed構造体の危険パターン検出"""
    if ignore.packed_ok:
        return
    # #pragma packスコープ追跡
    pack_depth = 0
    pack_lines = []
    in_pack = [False] * len(raw)
    for i, rl in enumerate(raw):
        stripped = rl.strip()
        if re.search(r'#\s*pragma\s+pack\s*\(\s*push', stripped):
            pack_depth += 1
            pack_lines.append(i + 1)
        elif re.search(r'#\s*pragma\s+pack\s*\(\s*pop', stripped) or \
             re.search(r'#\s*pragma\s+pack\s*\(\s*\)', stripped):
            if pack_depth > 0:
                pack_depth -= 1
                pack_lines.pop()
        in_pack[i] = pack_depth > 0
    for ln in pack_lines:
        issues.append(Issue(Severity.DESIGN, fp, ln,
            "#pragma pack(push)に対応するpack(pop)なし。後続構造体のアラインメントに影響波及"))
    # packed構造体のポインタメンバ検出
    packed_re = re.compile(r'__attribute__\s*\(\s*\(\s*packed\s*\)\s*\)')
    i = 0
    while i < len(cl):
        line = cl[i]
        if not re.search(r'\bstruct\b', line) or '{' not in line:
            i += 1
            continue
        struct_start = i
        brace = 0
        has_ptr = False
        is_packed = bool(packed_re.search(line)) or in_pack[i]
        for j in range(i, len(cl)):
            brace += cl[j].count('{') - cl[j].count('}')
            if j > i and re.search(r'\w+\s*\*\s*\w+\s*;', cl[j]):
                has_ptr = True
            if brace <= 0 and j > i:
                if packed_re.search(cl[j]):
                    is_packed = True
                if is_packed and has_ptr:
                    issues.append(Issue(Severity.DESIGN, fp, struct_start + 1,
                        "packed構造体にポインタメンバ。アラインメント違反で一部アーキテクチャでクラッシュ"))
                i = j
                break
        i += 1


def _get_obj_func_sizes(filepath: str) -> Optional[Dict[str, int]]:
    """Cソースをgcc -cでコンパイルし、nmで関数ごとのオブジェクトサイズを取得。
    コンパイル失敗時はNoneを返す。"""
    import tempfile
    import shutil
    # gccが使えるか確認
    gcc = shutil.which("gcc")
    if not gcc:
        return None
    obj_fd = None
    obj_path = None
    try:
        obj_fd, obj_path = tempfile.mkstemp(suffix=".o")
        os.close(obj_fd)
        obj_fd = None
        # -c: コンパイルのみ, -w: 警告抑制, -O2: 最適化で実質コードサイズを測定
        # -fomit-frame-pointer: フレームポインタ省略でABI差を最小化
        # -fno-asynchronous-unwind-tables: .eh_frame除外でサイズ計測精度向上
        result = subprocess.run(
            [gcc, "-c", "-w", "-O2", "-fomit-frame-pointer",
             "-fno-asynchronous-unwind-tables", "-o", obj_path, filepath],
            capture_output=True, timeout=30)
        if result.returncode != 0:
            return None
        # まずGNU nm -S を試行（Linux: サイズ列あり）
        nm_result = subprocess.run(
            ["nm", "-S", "--defined-only", obj_path],
            capture_output=True, text=True, timeout=10)
        if nm_result.returncode != 0:
            # macOS等GNU nm非対応: --defined-only なしで再試行
            nm_result = subprocess.run(
                ["nm", "-n", obj_path],
                capture_output=True, text=True, timeout=10)
            if nm_result.returncode != 0:
                return None
        # シンボル解析: アドレス順ソートして隣接アドレス差からサイズ計算
        # GNU nm -S: "addr size type name" (4列)
        # macOS/BSD nm: "addr type name" (3列, サイズなし)
        # ltmp等の内部ラベルもアドレス境界として収集し、最終結果からは除外
        all_symbols = []  # [(addr, fname, size, is_user)]
        has_size_col = False
        for line in nm_result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ('T', 't'):
                # GNU nm -S 形式: addr size type name
                fname = parts[3]
                # macOS: シンボル名の先頭 _ を除去
                if fname.startswith('_'):
                    fname = fname[1:]
                is_user = not fname.startswith('ltmp')
                try:
                    addr = int(parts[0], 16)
                    size = int(parts[1], 16)
                    if size > 0 and is_user:
                        has_size_col = True
                    all_symbols.append((addr, fname, size, is_user))
                except ValueError:
                    pass
            elif len(parts) >= 3 and parts[1] in ('T', 't'):
                # BSD nm形式: addr type name
                fname = parts[2]
                if fname.startswith('_'):
                    fname = fname[1:]
                is_user = not fname.startswith('ltmp')
                try:
                    addr = int(parts[0], 16)
                    all_symbols.append((addr, fname, 0, is_user))
                except ValueError:
                    pass
        if not any(s[3] for s in all_symbols):
            return None
        # GNU nmでサイズ列が有効ならそのまま使用
        if has_size_col:
            sizes: Dict[str, int] = {}
            for _, fname, size, is_user in all_symbols:
                if is_user:
                    sizes[fname] = size
            # サイズの妥当性検証: 複数関数が全て同一サイズ=アライメント由来
            # 単一関数=検証不能 → いずれもソース解析にフォールバック
            unique_sizes = set(sizes.values())
            if len(unique_sizes) <= 1:
                return None
            else:
                return sizes if sizes else None
        # サイズ列がない場合: 全シンボルをアドレス順ソートし隣接差から推定
        all_symbols.sort(key=lambda s: s[0])
        reliable: Dict[str, int] = {}
        for idx, (addr, fname, _, is_user) in enumerate(all_symbols):
            if not is_user:
                continue
            # 次のシンボル(内部ラベル含む)のアドレスとの差でサイズ推定
            next_addr = None
            for j in range(idx + 1, len(all_symbols)):
                if all_symbols[j][0] > addr:
                    next_addr = all_symbols[j][0]
                    break
            if next_addr is not None:
                reliable[fname] = next_addr - addr
            # 最後のシンボル: 隣接シンボルがないためサイズ推定不能
            # (セクションパディングで膨らむので信頼できない)
            # → reliable に含めず、他に信頼できるサイズがなければNone返却
        # 信頼できるサイズが1つもなければソース解析にフォールバック
        return reliable if reliable else None
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        if obj_fd is not None:
            try:
                os.close(obj_fd)
            except OSError:
                pass
        if obj_path and os.path.exists(obj_path):
            try:
                os.unlink(obj_path)
            except OSError:
                pass


def check_tiny_function(fp, raw, cl, issues):
    """コンパイル後のオブジェクトサイズが極小の関数を検出。
    スタブ・空関数・リンク事故の疑い。gcc未使用環境ではソース解析にフォールバック"""
    TINY_THRESHOLD = 8  # バイト(x86/ARM共通で空関数を捕捉)
    # ソース行から関数定義の行番号マップを作成
    func_re = re.compile(r'^(\w[\w\s\*]*?)\s+(\w+)\s*\(')
    func_lines: Dict[str, int] = {}
    i = 0
    while i < len(cl):
        fm = func_re.match(cl[i])
        if fm and '{' in cl[i]:
            func_lines[fm.group(2)] = i + 1  # 1-indexed
        i += 1

    # オブジェクトサイズ取得を試行
    obj_sizes = _get_obj_func_sizes(fp)
    if obj_sizes is not None:
        for fname, size in obj_sizes.items():
            if size <= TINY_THRESHOLD:
                line = func_lines.get(fname, 0)
                issues.append(Issue(Severity.MAINT, fp, line,
                    f"関数{fname}: オブジェクトサイズ{size}バイト({TINY_THRESHOLD}バイト以下)。"
                    f"スタブまたは空関数の疑い"))
        return

    # フォールバック: ソースコード解析(gcc無い環境向け)
    i = 0
    while i < len(cl):
        fm = func_re.match(cl[i])
        if not fm or '{' not in cl[i]:
            i += 1
            continue
        func_name = fm.group(2)
        brace = 0
        body_chars = []
        func_start = i
        for j in range(i, len(cl)):
            brace += cl[j].count('{') - cl[j].count('}')
            if j > i:
                stripped = cl[j].strip().rstrip('}').strip()
                if stripped:
                    body_chars.append(stripped)
            if brace <= 0 and j > i:
                body_text = "".join(body_chars)
                effective = len(re.sub(r'\s+', '', body_text))
                if effective < 5:
                    issues.append(Issue(Severity.MAINT, fp, func_start + 1,
                        f"関数{func_name}: 本体が{effective}バイト(ソース解析)。"
                        f"スタブまたは空関数の疑い(gcc未検出のためソース解析)"))
                i = j
                break
        i += 1


def check_linkage(fp, raw, cl, issues):
    """extern宣言に対応する定義が同一ファイル内に存在するか簡易チェック。
    ヘッダファイル(.h)ではスキップ(宣言のみが正常)"""
    if fp.endswith('.h'):
        return
    extern_re = re.compile(
        r'\bextern\s+[\w\s\*]+\b(\w+)\s*\(')
    for i, line in enumerate(cl):
        m = extern_re.search(line)
        if not m:
            continue
        func_name = m.group(1)
        # 同一ファイル内に定義(extern無し)があるか
        def_pat = re.compile(
            rf'^(?!.*\bextern\b)\w[\w\s\*]*\b{re.escape(func_name)}\s*\([^)]*\)\s*\{{')
        found = False
        for k, dl in enumerate(cl):
            if k == i:
                continue
            if def_pat.match(dl):
                found = True
                break
        if not found:
            # プロトタイプ宣言のみは許容(extern宣言として正常)
            # ただし.cファイル内のexternは外部依存を示す → リンケージ警告
            issues.append(Issue(Severity.MAINT, fp, i + 1,
                f"extern宣言 {func_name}() の定義が同一ファイル内になし。"
                f"リンク時に未解決シンボルの可能性"))


def check_undefined_call(fp, raw, cl, issues):
    """呼び出し関数がファイル内で宣言・定義されていないパターン検出。
    標準ライブラリ・POSIX関数は除外"""
    # 標準・POSIX関数(よく使われるもの)
    KNOWN_FUNCS = {
        # stdio
        "printf", "fprintf", "sprintf", "snprintf", "scanf", "fscanf",
        "sscanf", "puts", "fputs", "fgets", "gets", "putchar", "getchar",
        "fopen", "fclose", "fread", "fwrite", "fseek", "ftell", "rewind",
        "fflush", "feof", "ferror", "perror", "remove", "rename", "tmpfile",
        "setbuf", "setvbuf", "vprintf", "vfprintf", "vsprintf", "vsnprintf",
        # stdlib
        "malloc", "calloc", "realloc", "free", "abort", "exit", "_exit",
        "atexit", "atoi", "atol", "atof", "strtol", "strtoul", "strtoll",
        "strtoull", "strtod", "strtof", "abs", "labs", "llabs", "div",
        "rand", "srand", "qsort", "bsearch", "getenv", "system",
        # string
        "memcpy", "memmove", "memset", "memcmp", "memchr",
        "strcpy", "strncpy", "strcat", "strncat", "strcmp", "strncmp",
        "strchr", "strrchr", "strstr", "strtok", "strlen", "strerror",
        "strdup", "strndup", "strnlen", "strcasecmp", "strncasecmp",
        # ctype
        "isalpha", "isdigit", "isalnum", "isspace", "isupper", "islower",
        "toupper", "tolower", "isprint", "isxdigit",
        # math
        "sin", "cos", "tan", "sqrt", "pow", "log", "log10", "exp",
        "ceil", "floor", "fabs", "fmod", "round",
        # POSIX
        "open", "close", "read", "write", "lseek", "stat", "fstat",
        "lstat", "access", "unlink", "mkdir", "rmdir", "opendir",
        "readdir", "closedir", "fork", "exec", "execl", "execv",
        "execvp", "execlp", "wait", "waitpid", "pipe", "dup", "dup2",
        "socket", "bind", "listen", "accept", "connect", "send", "recv",
        "sendto", "recvfrom", "select", "poll", "epoll_create",
        "epoll_ctl", "epoll_wait", "setsockopt", "getsockopt",
        "getaddrinfo", "freeaddrinfo", "gai_strerror",
        "pthread_create", "pthread_join", "pthread_detach",
        "pthread_mutex_init", "pthread_mutex_lock", "pthread_mutex_unlock",
        "pthread_mutex_destroy", "pthread_cond_init", "pthread_cond_wait",
        "pthread_cond_signal", "pthread_cond_broadcast",
        "signal", "sigaction", "kill", "raise", "alarm",
        "mmap", "munmap", "mprotect", "shmget", "shmat", "shmdt",
        "sem_init", "sem_wait", "sem_post", "sem_destroy",
        "clock_gettime", "nanosleep", "usleep", "sleep", "time",
        "localtime", "gmtime", "strftime", "difftime", "mktime",
        "syslog", "openlog", "closelog",
        "ioctl", "fcntl",
        # assert
        "assert",
        # setjmp
        "setjmp", "longjmp",
        # errno
        "errno",
    }
    # ファイル内の関数定義・宣言を収集
    func_def_re = re.compile(r'^(\w[\w\s\*]*?)\s+(\w+)\s*\(')
    declared_funcs: Set[str] = set()
    for line in cl:
        m = func_def_re.match(line)
        if m:
            declared_funcs.add(m.group(2))
    # extern宣言も収集
    extern_re = re.compile(r'\bextern\s+[\w\s\*]+\b(\w+)\s*\(')
    for line in cl:
        m = extern_re.search(line)
        if m:
            declared_funcs.add(m.group(1))
    # #include先のヘッダで宣言されている可能性 → 関数呼び出しで宣言も定義もないものを検出
    # マクロ呼び出しは大文字のみのものとして除外
    call_re = re.compile(r'\b([a-z_]\w*)\s*\(')
    # 型キャストパターン除外
    cast_types = {"int", "char", "long", "short", "float", "double",
                  "unsigned", "signed", "void", "size_t", "ssize_t",
                  "uint8_t", "uint16_t", "uint32_t", "uint64_t",
                  "int8_t", "int16_t", "int32_t", "int64_t",
                  "if", "while", "for", "switch", "return", "sizeof",
                  "typeof", "alignof", "offsetof"}
    reported: Set[str] = set()
    for i, line in enumerate(cl):
        for m in call_re.finditer(line):
            fname = m.group(1)
            if fname in cast_types:
                continue
            if fname in KNOWN_FUNCS:
                continue
            if fname in declared_funcs:
                continue
            if fname in reported:
                continue
            # マクロ(大文字+アンダースコアのみ)は除外済み(小文字始まりのみ対象)
            reported.add(fname)
            issues.append(Issue(Severity.MAINT, fp, i + 1,
                f"関数{fname}()の宣言・定義がファイル内に見つからない。"
                f"ヘッダinclude漏れまたはリンクエラーの可能性"))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# スタックサイズ推定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 基本型サイズ (LP64 ABI想定)
TYPE_SIZES = {
    "char": 1, "signed char": 1, "unsigned char": 1,
    "short": 2, "unsigned short": 2,
    "int": 4, "unsigned int": 4, "unsigned": 4, "signed": 4,
    "long": 8, "unsigned long": 8,
    "long long": 8, "unsigned long long": 8,
    "float": 4, "double": 8, "long double": 16,
    "size_t": 8, "ssize_t": 8, "ptrdiff_t": 8,
    "int8_t": 1, "uint8_t": 1,
    "int16_t": 2, "uint16_t": 2,
    "int32_t": 4, "uint32_t": 4,
    "int64_t": 8, "uint64_t": 8,
    "pid_t": 4, "off_t": 8, "time_t": 8,
    "bool": 1, "_Bool": 1,
}

DEFAULT_STACK_THRESHOLD = 8192  # 8KB


@dataclass
class StackInfo:
    func_name: str
    line: int
    estimated_bytes: int
    details: List[Tuple[str, int]]   # (変数宣言, 推定サイズ)
    has_alloca: bool
    has_vla: bool
    is_recursive: bool


def _get_type_size(type_str: str) -> int:
    """型名からサイズを推定"""
    t = type_str.strip()
    # ポインタ
    if '*' in t:
        return 8
    # struct/union/enumは推定不能、デフォルト値
    if t.startswith(("struct ", "union ", "enum ")):
        return 0  # 不明
    # 型修飾子を除去
    for qual in ("const ", "volatile ", "static ", "register ", "restrict "):
        t = t.replace(qual, "")
    t = t.strip()
    return TYPE_SIZES.get(t, 0)


def _parse_array_size(size_expr: str) -> Optional[int]:
    """配列サイズ式から定数値を推定"""
    s = size_expr.strip()
    # 単純な数値
    try:
        return int(s)
    except ValueError:
        pass
    # 16進数
    if s.startswith("0x") or s.startswith("0X"):
        try:
            return int(s, 16)
        except ValueError:
            pass
    # 単純な乗算 (例: 4 * 1024)
    m = re.match(r'(\d+)\s*\*\s*(\d+)$', s)
    if m:
        return int(m.group(1)) * int(m.group(2))
    # sizeof(type) * N パターン
    m = re.match(r'sizeof\s*\(\s*(\w+)\s*\)\s*\*\s*(\d+)$', s)
    if m:
        ts = _get_type_size(m.group(1))
        if ts > 0:
            return ts * int(m.group(2))
    # 変数式 → VLAとして扱い、サイズ不明
    return None


def analyze_function_stack(func_name: str, func_start: int,
                           body_lines: List[str], cl_lines: List[str]) -> StackInfo:
    """関数本体のローカル変数からスタック使用量を推定"""
    total = 0
    details: List[Tuple[str, int]] = []
    has_alloca = False
    has_vla = False
    is_recursive = False

    # 型名パターン (ポインタ含む)
    type_pat = (
        r'(?:(?:const|volatile|static|register)\s+)*'
        r'(?:(?:unsigned|signed)\s+)?'
        r'(?:struct\s+\w+|union\s+\w+|enum\s+\w+|'
        r'char|short|int|long\s+long|long|float|double|'
        r'size_t|ssize_t|ptrdiff_t|'
        r'(?:u?int(?:8|16|32|64)_t)|'
        r'pid_t|off_t|time_t|bool|_Bool)'
        r'(?:\s*\*)*'
    )

    # 配列宣言: type var[SIZE]  or  type var[SIZE1][SIZE2]
    arr_re = re.compile(
        rf'({type_pat})\s+(\w+)\s*'
        r'\[\s*([^\]]*)\s*\]'
        r'(?:\s*\[\s*([^\]]*)\s*\])?'
        r'\s*[;=]'
    )

    # スカラ変数宣言: type var;  or  type var = ...;
    scalar_re = re.compile(
        rf'({type_pat})\s+(\w+)\s*[;=,]'
    )

    # alloca検出
    alloca_re = re.compile(r'\balloca\s*\(')

    # 自己再帰検出
    call_re = re.compile(rf'\b{re.escape(func_name)}\s*\(')

    for i, line in enumerate(cl_lines):
        if i == 0:
            continue  # 関数定義行はスキップ (自己再帰の誤検出防止)
        stripped = line.strip()
        if not stripped:
            continue

        # alloca
        if alloca_re.search(stripped):
            has_alloca = True
            details.append(("alloca()", -1))

        # 自己再帰呼び出し
        if call_re.search(stripped):
            is_recursive = True

        # 配列宣言
        m = arr_re.search(stripped)
        if m:
            type_str = m.group(1)
            var_name = m.group(2)
            size1_str = m.group(3)
            size2_str = m.group(4)

            elem_size = _get_type_size(type_str)
            if elem_size == 0:
                elem_size = 4  # 不明型はint相当で推定

            size1 = _parse_array_size(size1_str)
            if size1 is None:
                has_vla = True
                details.append((f"{type_str} {var_name}[{size1_str}]", -1))
                continue

            arr_bytes = elem_size * size1
            if size2_str is not None:
                size2 = _parse_array_size(size2_str)
                if size2 is not None:
                    arr_bytes *= size2
                else:
                    has_vla = True

            total += arr_bytes
            decl = f"{type_str} {var_name}[{size1_str}]"
            if size2_str:
                decl += f"[{size2_str}]"
            details.append((decl, arr_bytes))
            continue

        # スカラ変数 (配列でないもの)
        m = scalar_re.search(stripped)
        if m:
            type_str = m.group(1)
            sz = _get_type_size(type_str)
            if sz > 0:
                total += sz
                # 小さいスカラは集約して表示しないが計上はする

    return StackInfo(
        func_name=func_name,
        line=func_start + 1,
        estimated_bytes=total,
        details=[d for d in details if d[1] != 0],  # 不明(0)は除外
        has_alloca=has_alloca,
        has_vla=has_vla,
        is_recursive=is_recursive,
    )


def analyze_file_stack(filepath: str, raw: List[str],
                       cl: List[str]) -> List[StackInfo]:
    """ファイル内の全関数のスタック使用量を推定"""
    func_re = re.compile(r'^(\w[\w\s\*]*?)\s+(\w+)\s*\(')
    results: List[StackInfo] = []

    i = 0
    while i < len(cl):
        fm = func_re.match(cl[i])
        if fm and '{' in cl[i]:
            func_name = fm.group(2)
            # static/inline等のキーワードを除外
            if func_name in ("if", "while", "for", "switch", "return"):
                i += 1
                continue

            func_start = i
            brace = 0
            func_end = i
            for j in range(i, len(cl)):
                brace += cl[j].count('{') - cl[j].count('}')
                if brace <= 0 and j > i:
                    func_end = j
                    break

            body_raw = raw[func_start:func_end + 1]
            body_cl = cl[func_start:func_end + 1]
            info = analyze_function_stack(func_name, func_start,
                                          body_raw, body_cl)
            results.append(info)
            i = func_end + 1
        else:
            i += 1

    return results


def check_stack_usage(fp, raw, cl, issues, threshold=DEFAULT_STACK_THRESHOLD):
    """スタック使用量が閾値を超える関数を指摘"""
    stack_infos = analyze_file_stack(fp, raw, cl)
    for info in stack_infos:
        warnings = []

        if info.estimated_bytes > threshold:
            warnings.append(
                f"関数{info.func_name}: 推定スタック使用{info.estimated_bytes}バイト"
                f"(閾値{threshold}超過)。スタックオーバーフローの危険")

        if info.has_alloca:
            warnings.append(
                f"関数{info.func_name}: alloca()使用。"
                f"実行時スタック量不定、オーバーフローの危険")

        if info.estimated_bytes > threshold // 2 and info.is_recursive:
            warnings.append(
                f"関数{info.func_name}: 再帰関数でスタック使用{info.estimated_bytes}バイト。"
                f"再帰深度次第でスタック枯渇")

        for msg in warnings:
            issues.append(Issue(Severity.CRITICAL, fp, info.line, msg))


def format_stack_report(filepath: str, stack_infos: List[StackInfo]) -> str:
    """--stack用: 関数別スタック使用量レポート"""
    if not stack_infos:
        return f"{filepath}: 関数なし\n"

    lines = [f"── スタック解析: {filepath} ──"]
    lines.append(f"{'関数名':<30} {'推定バイト':>10}  {'備考'}")
    lines.append("─" * 65)

    # サイズ降順
    sorted_infos = sorted(stack_infos, key=lambda x: x.estimated_bytes,
                          reverse=True)
    for info in sorted_infos:
        notes = []
        if info.has_alloca:
            notes.append("alloca")
        if info.has_vla:
            notes.append("VLA")
        if info.is_recursive:
            notes.append("再帰")
        if info.estimated_bytes > DEFAULT_STACK_THRESHOLD:
            notes.append("!!超過")

        note_str = ", ".join(notes) if notes else "-"
        size_str = f"{info.estimated_bytes:,}" if info.estimated_bytes >= 0 else "不定"
        lines.append(f"{info.func_name:<30} {size_str:>10}  {note_str}")

        # 大きな変数の内訳
        big_details = [(d, s) for d, s in info.details
                       if s > 256 or s == -1]
        for decl, sz in big_details:
            if sz == -1:
                lines.append(f"  └─ {decl}  (サイズ不定)")
            else:
                lines.append(f"  └─ {decl}  ({sz:,}バイト)")

    total = sum(i.estimated_bytes for i in stack_infos)
    lines.append("─" * 65)
    lines.append(f"関数数: {len(stack_infos)}  合計推定: {total:,}バイト")
    lines.append("")
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# バッファ使用率解析 (--buf-usage)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class BufWrite:
    line: int
    operation: str     # "snprintf", "read", etc.
    max_bytes: int     # 最大書き込みバイト数 (-1=不定)
    source_line: str   # 元ソース行


@dataclass
class BufInfo:
    name: str
    decl_line: int
    decl_size: int       # 宣言サイズ(バイト)
    elem_size: int       # 要素サイズ
    writes: List[BufWrite]
    max_write: int       # 全書き込みの最大値 (-1=不定)
    usage_pct: float     # 使用率% (-1=不定)


def _resolve_size_expr(expr: str, buf_sizes: Dict[str, int]) -> int:
    """サイズ式を解決。sizeof(buf)やbuf_sizeなどを定数に変換"""
    s = expr.strip()
    # sizeof(buf) - N パターン (sizeof単体より先に判定)
    m = re.match(r'sizeof\s*\(\s*(\w+)\s*\)\s*-\s*(\d+)', s)
    if m:
        var = m.group(1)
        sub = int(m.group(2))
        if var in buf_sizes:
            return buf_sizes[var] - sub
    # sizeof(var) パターン
    m = re.match(r'sizeof\s*\(\s*(\w+)\s*\)$', s)
    if m:
        var = m.group(1)
        if var in buf_sizes:
            return buf_sizes[var]
        sz = _get_type_size(var)
        if sz > 0:
            return sz
        return -1
    # 数値リテラル
    try:
        return int(s)
    except ValueError:
        pass
    # 16進
    if s.startswith("0x") or s.startswith("0X"):
        try:
            return int(s, 16)
        except ValueError:
            pass
    # 乗算 A * B
    m = re.match(r'(\w+)\s*\*\s*(\w+)$', s)
    if m:
        a = _resolve_size_expr(m.group(1), buf_sizes)
        b = _resolve_size_expr(m.group(2), buf_sizes)
        if a > 0 and b > 0:
            return a * b
    # 変数名 → 不定
    return -1


def analyze_buf_usage(filepath: str, raw: List[str],
                      cl: List[str]) -> List[BufInfo]:
    """ファイル内のバッファ宣言と書き込み操作を解析"""
    results: List[BufInfo] = []

    # Step 1: バッファ(配列)宣言を収集
    buf_decls: Dict[str, Tuple[int, int, int]] = {}  # name → (line, total_bytes, elem_size)
    arr_decl_re = re.compile(
        r'(?:(?:const|volatile|static|register)\s+)*'
        r'(?:(?:unsigned|signed)\s+)?'
        r'(char|short|int|long|float|double|uint8_t|int8_t|'
        r'uint16_t|int16_t|uint32_t|int32_t|uint64_t|int64_t|'
        r'unsigned\s+char|signed\s+char)'
        r'(?:\s*\*)*'
        r'\s+(\w+)\s*\[\s*([^\]]+)\s*\]\s*[;=]'
    )

    for i, line in enumerate(cl):
        m = arr_decl_re.search(line.strip())
        if m:
            type_str = m.group(1)
            buf_name = m.group(2)
            size_expr = m.group(3)
            elem_sz = _get_type_size(type_str)
            if elem_sz == 0:
                elem_sz = 1
            arr_size = _parse_array_size(size_expr)
            if arr_size is not None and arr_size > 0:
                total = elem_sz * arr_size
                buf_decls[buf_name] = (i + 1, total, elem_sz)

    if not buf_decls:
        return results

    # buf_sizes マップ (sizeof解決用)
    buf_sizes: Dict[str, int] = {name: info[1] for name, info in buf_decls.items()}

    # Step 2: 各バッファへの書き込み操作を検出
    buf_writes: Dict[str, List[BufWrite]] = {name: [] for name in buf_decls}

    for i, line in enumerate(cl):
        stripped = line.strip()
        raw_line = raw[i].strip() if i < len(raw) else ""

        for buf_name in buf_decls:
            # snprintf(buf, size, ...)
            m = re.search(rf'\bsnprintf\s*\(\s*{re.escape(buf_name)}\s*,\s*([^,]+)\s*,',
                          stripped)
            if m:
                sz = _resolve_size_expr(m.group(1), buf_sizes)
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "snprintf", sz, raw_line))
                continue

            # strncpy(buf, src, size)
            m = re.search(rf'\bstrncpy\s*\(\s*{re.escape(buf_name)}\s*,\s*[^,]+\s*,\s*([^,;]+?)\s*\)\s*;',
                          stripped)
            if m:
                sz = _resolve_size_expr(m.group(1), buf_sizes)
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "strncpy", sz, raw_line))
                continue

            # memcpy(buf, src, size) / memmove
            m = re.search(rf'\b(memcpy|memmove)\s*\(\s*{re.escape(buf_name)}\s*,\s*[^,]+\s*,\s*([^,;]+?)\s*\)\s*;',
                          stripped)
            if m:
                sz = _resolve_size_expr(m.group(2), buf_sizes)
                buf_writes[buf_name].append(
                    BufWrite(i + 1, m.group(1), sz, raw_line))
                continue

            # memset(buf, val, size)
            m = re.search(rf'\bmemset\s*\(\s*{re.escape(buf_name)}\s*,\s*[^,]+\s*,\s*([^,;]+?)\s*\)\s*;',
                          stripped)
            if m:
                sz = _resolve_size_expr(m.group(1), buf_sizes)
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "memset", sz, raw_line))
                continue

            # read(fd, buf, size) / recv(sock, buf, size, ...)
            m = re.search(rf'\b(read|recv)\s*\(\s*[^,]+\s*,\s*{re.escape(buf_name)}\s*,\s*(\w+(?:\s*\([^)]*\))?(?:\s*[+\-*/]\s*\d+)?)',
                          stripped)
            if m:
                sz = _resolve_size_expr(m.group(2), buf_sizes)
                buf_writes[buf_name].append(
                    BufWrite(i + 1, m.group(1), sz, raw_line))
                continue

            # fgets(buf, size, fp)
            m = re.search(rf'\bfgets\s*\(\s*{re.escape(buf_name)}\s*,\s*([^,]+)\s*,',
                          stripped)
            if m:
                sz = _resolve_size_expr(m.group(1), buf_sizes)
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "fgets", sz, raw_line))
                continue

            # fread(buf, elem_size, count, fp)
            m = re.search(rf'\bfread\s*\(\s*{re.escape(buf_name)}\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,',
                          stripped)
            if m:
                es = _resolve_size_expr(m.group(1), buf_sizes)
                cnt = _resolve_size_expr(m.group(2), buf_sizes)
                sz = es * cnt if es > 0 and cnt > 0 else -1
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "fread", sz, raw_line))
                continue

            # sprintf(buf, ...) - サイズ制限なし
            m = re.search(rf'\bsprintf\s*\(\s*{re.escape(buf_name)}\s*,', stripped)
            if m:
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "sprintf", -1, raw_line))
                continue

            # strcpy(buf, src) - リテラルならサイズ判定可能
            m = re.search(rf'\bstrcpy\s*\(\s*{re.escape(buf_name)}\s*,', stripped)
            if m:
                # raw行でリテラルチェック
                m_lit = re.search(
                    rf'\bstrcpy\s*\(\s*{re.escape(buf_name)}\s*,\s*"([^"]*)"',
                    raw_line)
                if m_lit:
                    sz = len(m_lit.group(1)) + 1  # +NUL
                else:
                    sz = -1
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "strcpy", sz, raw_line))
                continue

            # strcat(buf, src) - サイズ制限なし
            m = re.search(rf'\bstrcat\s*\(\s*{re.escape(buf_name)}\s*,', stripped)
            if m:
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "strcat", -1, raw_line))
                continue

            # gets(buf) - サイズ制限なし
            m = re.search(rf'\bgets\s*\(\s*{re.escape(buf_name)}\s*\)', stripped)
            if m:
                buf_writes[buf_name].append(
                    BufWrite(i + 1, "gets", -1, raw_line))
                continue

    # Step 3: 結果集計
    for buf_name, (decl_line, decl_size, elem_sz) in buf_decls.items():
        writes = buf_writes[buf_name]
        if not writes:
            continue  # 書き込みなしのバッファはスキップ

        max_write = -1
        has_unbounded = False
        for w in writes:
            if w.max_bytes == -1:
                has_unbounded = True
            elif max_write == -1 or w.max_bytes > max_write:
                max_write = w.max_bytes

        if has_unbounded:
            usage_pct = -1.0  # 不定
            if max_write == -1:
                max_write = -1
        else:
            usage_pct = (max_write / decl_size * 100) if decl_size > 0 else -1.0

        results.append(BufInfo(
            name=buf_name,
            decl_line=decl_line,
            decl_size=decl_size,
            elem_size=elem_sz,
            writes=writes,
            max_write=max_write,
            usage_pct=usage_pct,
        ))

    results.sort(key=lambda b: b.decl_line)
    return results


def format_buf_report(filepath: str, buf_infos: List[BufInfo]) -> str:
    """--buf-usage用: バッファ使用率レポート"""
    if not buf_infos:
        return ""

    lines = [f"── バッファ使用率: {filepath} ──"]
    lines.append(f"{'バッファ':<20} {'宣言':>8}  {'最大書込':>8}  {'使用率':>7}  {'操作'}")
    lines.append("─" * 75)

    for buf in buf_infos:
        decl_str = f"{buf.decl_size:,}B"

        if buf.max_write == -1:
            max_str = "不定"
            pct_str = "!!危険"
        else:
            max_str = f"{buf.max_write:,}B"
            if buf.usage_pct < 0:
                pct_str = "不定"
            elif buf.usage_pct > 100:
                pct_str = f"{buf.usage_pct:.0f}% !!"
            else:
                pct_str = f"{buf.usage_pct:.0f}%"

        ops = set(w.operation for w in buf.writes)
        ops_str = ", ".join(sorted(ops))

        lines.append(f"{buf.name:<20} {decl_str:>8}  {max_str:>8}  {pct_str:>7}  {ops_str}")

        # 書き込み詳細
        for w in buf.writes:
            if w.max_bytes == -1:
                sz_detail = "不定(危険)"
            else:
                sz_detail = f"最大{w.max_bytes:,}B"
            lines.append(f"  L{w.line}: {w.operation}  {sz_detail}")

    # サマリー
    safe = sum(1 for b in buf_infos if 0 <= b.usage_pct <= 100)
    over = sum(1 for b in buf_infos if b.usage_pct > 100)
    unbounded = sum(1 for b in buf_infos if b.usage_pct < 0)
    lines.append("─" * 75)
    lines.append(f"バッファ数: {len(buf_infos)}  "
                 f"安全: {safe}  超過: {over}  不定: {unbounded}")
    lines.append("")
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 1: ローカル静的解析エンジン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_local_analysis(filepath: str, ignore: IgnoreConfig,
                       only_rules: Optional[Set[str]] = None,
                       stack_threshold: int = DEFAULT_STACK_THRESHOLD) -> List[Issue]:
    issues: List[Issue] = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as e:
        issues.append(Issue(Severity.CRITICAL, filepath, 0, f"ファイル読み込み失敗: {e}"))
        return issues

    raw = source.split("\n")
    cl = strip_comments_and_strings(source)

    off = ignore.rule_off
    # ルール名→チェック関数マッピング (ignore引数が必要なものはlambda)
    checks = [
        ("null_deref",          lambda: check_null_deref(filepath, raw, cl, issues)),
        ("unsafe_funcs",        lambda: check_unsafe_funcs(filepath, raw, cl, issues)),
        ("memcpy_no_null",      lambda: check_memcpy_no_null(filepath, raw, cl, issues)),
        ("array_index",         lambda: check_array_index(filepath, raw, cl, issues)),
        ("double_free",         lambda: check_double_free(filepath, raw, cl, issues)),
        ("return_inconsistency", lambda: check_return_inconsistency(filepath, raw, cl, issues)),
        ("globals",             lambda: check_globals(filepath, raw, cl, issues, ignore)),
        ("macros",              lambda: check_macros(filepath, raw, cl, issues, ignore)),
        ("switch_fallthrough",  lambda: check_switch_fallthrough(filepath, raw, cl, issues)),
        ("sizeof_pointer",      lambda: check_sizeof_pointer(filepath, raw, cl, issues)),
        ("fd_leak",             lambda: check_fd_leak(filepath, raw, cl, issues)),
        ("magic_numbers",       lambda: check_magic_numbers(filepath, raw, cl, issues, ignore)),
        ("volatile",            lambda: check_volatile(filepath, raw, cl, issues, ignore)),
        ("packed",              lambda: check_packed(filepath, raw, cl, issues, ignore)),
        ("format_string",       lambda: check_format_string(filepath, raw, cl, issues)),
        ("use_after_free",      lambda: check_use_after_free(filepath, raw, cl, issues)),
        ("uninitialized",       lambda: check_uninitialized(filepath, raw, cl, issues)),
        ("sign_compare",        lambda: check_sign_compare(filepath, raw, cl, issues)),
        ("integer_overflow",    lambda: check_integer_overflow(filepath, raw, cl, issues)),
        ("resource_leak",       lambda: check_resource_leak(filepath, raw, cl, issues)),
        ("snprintf_retval",     lambda: check_snprintf_retval(filepath, raw, cl, issues)),
        ("buffer_overrun",      lambda: check_buffer_overrun(filepath, raw, cl, issues)),
        ("null_deref_branch",   lambda: check_null_deref_branch(filepath, raw, cl, issues)),
        ("infinite_loop",       lambda: check_infinite_loop(filepath, raw, cl, issues)),
        ("enum_switch",         lambda: check_enum_switch(filepath, raw, cl, issues)),
        ("recursive_no_limit",  lambda: check_recursive_no_limit(filepath, raw, cl, issues)),
        ("mutex_unlock",        lambda: check_mutex_unlock(filepath, raw, cl, issues)),
        ("toctou",              lambda: check_toctou(filepath, raw, cl, issues)),
        ("signal_unsafe",       lambda: check_signal_unsafe(filepath, raw, cl, issues)),
        ("cast_truncation",     lambda: check_cast_truncation(filepath, raw, cl, issues)),
        ("bitfield_sign",       lambda: check_bitfield_sign(filepath, raw, cl, issues)),
        ("vla",                 lambda: check_vla(filepath, raw, cl, issues)),
        ("goto_misuse",         lambda: check_goto_misuse(filepath, raw, cl, issues)),
        ("stack_usage",         lambda: check_stack_usage(filepath, raw, cl, issues,
                                                            threshold=stack_threshold)),
        ("tiny_function",       lambda: check_tiny_function(filepath, raw, cl, issues)),
        ("linkage",             lambda: check_linkage(filepath, raw, cl, issues)),
        ("undefined_call",      lambda: check_undefined_call(filepath, raw, cl, issues)),
    ]
    for rule_name, check_fn in checks:
        if rule_name in off:
            continue
        if only_rules is not None and rule_name not in only_rules:
            continue
        check_fn()

    # NOCHECK行単位抑制: 元ソースのコメントに "NOCHECK" があれば除外
    nocheck_lines: Set[int] = set()
    for i, rl in enumerate(raw):
        if "NOCHECK" in rl:
            nocheck_lines.add(i + 1)
    if nocheck_lines:
        issues = [iss for iss in issues if iss.line not in nocheck_lines]

    issues.sort(key=lambda x: x.line)
    return issues


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 2: Claude API 深層レビュー
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class APIError(Exception):
    pass


def split_into_chunks(source: str, filepath: str,
                      config: Optional[AppConfig] = None) -> List[Tuple[str, int]]:
    """ソースをチャンクに分割。関数境界を考慮して切断。
    各チャンクに行番号プレフィクス付与。
    戻り値: [(チャンクテキスト, 開始行番号), ...]"""
    max_lines = config.max_chunk_lines if config else MAX_CHUNK_LINES
    lines = source.split("\n")

    # 小さいファイルは分割不要
    total_bytes = len(source.encode("utf-8"))
    if len(lines) <= max_lines and total_bytes <= MAX_CHUNK_BYTES:
        numbered = [f"{i + 1}: {line}" for i, line in enumerate(lines)]
        return [("\n".join(numbered), 1)]

    # 関数境界(ブレース深さ0に戻る行)を検出
    brace_depth = 0
    boundary_lines: Set[int] = {0}  # 先頭は常に境界
    for i, line in enumerate(lines):
        # コメント・文字列内のブレースも数えるが、概算として十分
        brace_depth += line.count('{') - line.count('}')
        if brace_depth <= 0:
            boundary_lines.add(i + 1)  # 次の行が境界
            brace_depth = 0  # 負にならないよう補正

    chunks = []
    current_lines = []
    current_bytes = 0
    chunk_start = 1

    for i, line in enumerate(lines):
        numbered = f"{i + 1}: {line}"
        line_bytes = len(numbered.encode("utf-8"))

        # チャンクサイズ超過チェック
        over_lines = len(current_lines) >= max_lines
        over_bytes = current_bytes + line_bytes > MAX_CHUNK_BYTES

        if (over_lines or over_bytes) and current_lines:
            # 関数境界で切れるか探す (現在位置から遡って最寄りの境界)
            best_cut = len(current_lines)  # デフォルト: 現在位置で切る
            for back in range(0, min(100, len(current_lines))):
                candidate = len(current_lines) - back
                actual_line = chunk_start + candidate - 1
                if actual_line in boundary_lines:
                    best_cut = candidate
                    break

            # best_cutで分割
            chunk_text = "\n".join(current_lines[:best_cut])
            chunks.append((chunk_text, chunk_start))

            # 残りを次のチャンクに繰り越し
            leftover = current_lines[best_cut:]
            chunk_start = chunk_start + best_cut
            current_lines = leftover
            current_bytes = sum(len(l.encode("utf-8")) for l in leftover)

        current_lines.append(numbered)
        current_bytes += line_bytes

    if current_lines:
        chunks.append(("\n".join(current_lines), chunk_start))

    return chunks


def _call_claude_api_single(system_prompt: str, user_content: str,
                            config: AppConfig, model: str) -> str:
    """単一モデルでAPI呼び出し。モデル不存在時はNone相当のAPIErrorを送出。"""
    import urllib.request
    import urllib.error

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": config.api_key,
        "anthropic-version": "2023-06-01",
    }
    body = json.dumps({
        "model": model,
        "max_tokens": 4096,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_content}
        ]
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    debug_log(f"API送信: {model}, {len(body)}bytes", config)

    try:
        with urllib.request.urlopen(req, timeout=config.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        # モデル不存在 → フォールバック用の特殊例外
        if e.code in (400, 404) and ("model" in error_body.lower() or
                                       "not_found" in error_body.lower()):
            raise APIError(f"MODEL_NOT_FOUND:{model}")
        if e.code == 401:
            raise APIError("API認証失敗。config.txtのAPI_KEYを確認")
        elif e.code == 429:
            raise APIError("APIレート制限。時間をおいて再実行")
        elif e.code == 400 and "token" in error_body.lower():
            raise APIError("トークン上限超過。ファイルが大きすぎる")
        else:
            raise APIError(f"API HTTP {e.code}")
    except urllib.error.URLError as e:
        raise APIError(f"API接続失敗: {e.reason}")
    except TimeoutError:
        raise APIError(f"APIタイムアウト({config.timeout}秒)")
    except Exception:
        raise APIError("API通信エラー")

    try:
        content_blocks = data.get("content", [])
        texts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        return "\n".join(texts)
    except (KeyError, TypeError):
        raise APIError("APIレスポンス解析失敗")


def call_claude_api(system_prompt: str, user_content: str,
                    config: AppConfig) -> str:
    """Claude APIを呼び出し。モデル廃止時は自動フォールバック。
    失敗時はAPIErrorを送出。AIの途中文章は一切見せない。"""
    try:
        import urllib.request
        import urllib.error
    except ImportError:
        raise APIError("HTTP通信モジュール不可")

    # まず設定モデルで試行
    try:
        return _call_claude_api_single(system_prompt, user_content, config, config.model)
    except APIError as e:
        if not str(e).startswith("MODEL_NOT_FOUND:"):
            raise

    # モデル不存在 → フォールバックチェーン
    debug_log(f"モデル{config.model}が利用不可。フォールバック試行...", config)
    for fallback in MODEL_FALLBACKS:
        if fallback == config.model:
            continue  # 既に失敗済み
        try:
            debug_log(f"フォールバック: {fallback}", config)
            return _call_claude_api_single(system_prompt, user_content, config, fallback)
        except APIError as e:
            if str(e).startswith("MODEL_NOT_FOUND:"):
                continue
            raise

    raise APIError(f"利用可能なモデルなし。config.txtのMODELを確認")


def run_api_review_code(filepath: str, source: str, config: AppConfig) -> str:
    """コードレビュー: チャンク分割 → API → 結果結合"""
    chunks = split_into_chunks(source, filepath, config)
    debug_log(f"{filepath}: {len(chunks)}チャンクに分割", config)
    results = []

    for idx, (chunk_text, start_line) in enumerate(chunks):
        debug_log(f"チャンク{idx+1}/{len(chunks)} 送信中...", config)
        if len(chunks) > 1:
            header = f"ファイル: {filepath} (チャンク {idx + 1}/{len(chunks)}, {start_line}行目から)\n\n"
        else:
            header = f"ファイル: {filepath}\n\n"

        user_msg = header + chunk_text
        result = call_claude_api(SYSTEM_PROMPT_CODE, user_msg, config)
        results.append(result)

    return "\n\n".join(results)


def run_api_review_spec(spec_path: str, config: AppConfig) -> str:
    """仕様レビュー: ファイル読み込み → API"""
    try:
        with open(spec_path, "r", encoding="utf-8", errors="replace") as f:
            spec_text = f.read()
    except OSError as e:
        raise APIError(f"仕様ファイル読み込み失敗: {e}")

    # トークン概算チェック
    byte_size = len(spec_text.encode("utf-8"))
    if byte_size > MAX_CHUNK_BYTES * 3:
        raise APIError("仕様ファイルが大きすぎる。分割してください")

    user_msg = f"以下の仕様をレビューしてください：\n\n{spec_text}"
    return call_claude_api(SYSTEM_PROMPT_SPEC, user_msg, config)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ルール定義・ヒントマッピング
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

RULE_DESCRIPTIONS = {
    "null_deref":          "malloc/calloc/realloc NULLチェック漏れ",
    "unsafe_funcs":        "gets/sprintf/strcpy/strcat使用",
    "memcpy_no_null":      "memcpy/memmove/memset NULLポインタ未検証",
    "array_index":         "外部入力の配列添字バウンドチェック漏れ",
    "double_free":         "二重free",
    "return_inconsistency": "return値と空returnの混在",
    "globals":             "グローバル変数の関数内直接更新",
    "macros":              "マクロ引数の括弧未保護",
    "switch_fallthrough":  "switch case fall-through",
    "sizeof_pointer":      "sizeof(ポインタ)",
    "fd_leak":             "fopen/fcloseリーク",
    "magic_numbers":       "マジックナンバー",
    "volatile":            "volatile変数の非アトミック操作",
    "packed":              "packed構造体のアラインメント問題",
    "format_string":       "フォーマット文字列脆弱性",
    "use_after_free":      "use-after-free",
    "uninitialized":       "未初期化変数使用",
    "sign_compare":        "signed/unsigned混合比較",
    "integer_overflow":    "整数オーバーフロー(malloc乗算)",
    "resource_leak":       "open/socket/pipeリソースリーク",
    "snprintf_retval":     "snprintf戻り値無視",
    "buffer_overrun":      "バッファオーバーラン",
    "null_deref_branch":   "NULLチェック分岐後のポインタ使用",
    "infinite_loop":       "無限ループ(break/returnなし)",
    "enum_switch":         "enum型switchでdefaultなし",
    "recursive_no_limit":  "深さ制限なし自己再帰",
    "mutex_unlock":        "pthread_mutex_lock/unlock不整合",
    "toctou":              "TOCTOU競合(access→open等)",
    "signal_unsafe":       "シグナルハンドラ内async-signal-unsafe関数",
    "cast_truncation":     "暗黙切り詰めキャスト",
    "bitfield_sign":       "ビットフィールド符号未指定",
    "vla":                 "可変長配列(VLA)使用",
    "goto_misuse":         "goto前方ジャンプ",
    "stack_usage":         "スタック使用量超過/alloca検出",
}

# --fix-hint用: メッセージキーワード → 修正ヒント
FIX_HINTS = {
    "NULLチェックなし": "→ if (ptr == NULL) で戻り値を検証",
    "gets使用": "→ fgets(buf, sizeof(buf), stdin) に置換",
    "sprintf使用": "→ snprintf(buf, sizeof(buf), ...) に置換",
    "strcpy使用": "→ strncpy + 終端NUL保証に置換",
    "strcat使用": "→ strncat + バッファ残量計算に置換",
    "NULL未検証": "→ 呼び出し前に if (ptr == NULL) を追加",
    "二重free": "→ free直後に ptr = NULL を追加",
    "dangling pointer": "→ free直後に ptr = NULL を追加",
    "use-after-free": "→ free直後に ptr = NULL; 以降参照禁止",
    "未初期化変数": "→ 宣言時に = 0 または適切な初期値を設定",
    "書式文字列": "→ printf(\"%s\", var) のようにフォーマット指定子経由で出力",
    "オーバーフロー未検証": "→ 乗算前に SIZE_MAX / sizeof(T) > n を検証",
    "fdリーク": "→ 対応するclose()を追加",
    "ソケットリーク": "→ 対応するclose()を追加",
    "fclose": "→ 対応するfclose()を追加",
    "sizeof": "→ sizeof(*ptr) または sizeof(配列) / sizeof(配列[0]) を使用",
    "fall-through": "→ break; または /* fall through */ コメントを追加",
    "バッファオーバーラン": "→ 第3引数をsizeof(dst)以下に制限",
    "デッドロック": "→ 全パスでpthread_mutex_unlock()を確保",
    "TOCTOU": "→ open()の戻り値で直接操作。access()を除去",
    "async-signal-unsafe": "→ write()やvolatile sig_atomic_tフラグを使用",
    "切り詰め": "→ キャスト前に値域チェック、またはワイド型のまま使用",
    "符号未指定": "→ unsigned int field:N または signed int field:N に明示",
    "可変長配列": "→ malloc+freeまたは固定サイズ配列に置換",
    "前方": "→ while/forループに構造化、またはエラー処理gotoのみ使用",
    "snprintf": "→ int ret = snprintf(...); if (ret >= sizeof(buf)) で切り詰め検出",
    "マジックナンバー": "→ #define NAME value または enum で定数定義",
    "defaultなし": "→ default: ケースを追加してassert/ログ出力",
    "自己再帰": "→ depth引数を追加し上限チェック",
    "無限ループ": "→ break/return条件を追加、またはコメントで意図を明示",
    "推定スタック使用": "→ 大きな配列はmalloc/freeに変更",
    "alloca()使用": "→ malloc/freeに置換、またはサイズ上限チェック追加",
    "再帰関数でスタック使用": "→ 大きなローカル変数をmallocに変更、または再帰をループ化",
}


def get_fix_hint(message: str) -> str:
    """メッセージからfix-hintを返す"""
    for keyword, hint in FIX_HINTS.items():
        if keyword in message:
            return hint
    return ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# --fix 修正案生成 (最小修正 + 推奨修正)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class FixSuggestion:
    minimal: str       # 最小修正コード
    recommended: str   # 推奨修正コード


def _extract_var_from_msg(message: str, pattern: str) -> str:
    """メッセージから変数名を抽出"""
    m = re.match(pattern, message)
    return m.group(1) if m else "ptr"


def generate_fix(message: str, source_line: str) -> Optional[FixSuggestion]:
    """指摘メッセージとソース行から修正案を生成"""
    line = source_line.strip()
    indent = source_line[:len(source_line) - len(source_line.lstrip())]

    # --- malloc/calloc/realloc NULLチェック漏れ ---
    if "NULLチェックなし" in message and ("malloc" in message or "calloc" in message or "realloc" in message):
        var = _extract_var_from_msg(message, r'^(\w+)に')
        return FixSuggestion(
            minimal=(
                f"{indent}{line}\n"
                f"{indent}if ({var} == NULL) return;"
            ),
            recommended=(
                f"{indent}{line}\n"
                f"{indent}if ({var} == NULL) {{\n"
                f"{indent}    fprintf(stderr, \"メモリ確保失敗: {var}\\n\");\n"
                f"{indent}    return -1;\n"
                f"{indent}}}"
            ),
        )

    # --- gets使用 ---
    if "gets使用" in message:
        m = re.search(r'gets\s*\(\s*(\w+)\s*\)', line)
        buf = m.group(1) if m else "buf"
        return FixSuggestion(
            minimal=f"{indent}fgets({buf}, sizeof({buf}), stdin);",
            recommended=(
                f"{indent}if (fgets({buf}, sizeof({buf}), stdin) != NULL) {{\n"
                f"{indent}    {buf}[strcspn({buf}, \"\\n\")] = '\\0';\n"
                f"{indent}}}"
            ),
        )

    # --- sprintf使用 ---
    if "sprintf使用" in message:
        m = re.search(r'sprintf\s*\(\s*(\w+)\s*,\s*(.+)\)', line)
        if m:
            buf, args = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=f"{indent}snprintf({buf}, sizeof({buf}), {args});",
                recommended=(
                    f"{indent}int ret = snprintf({buf}, sizeof({buf}), {args});\n"
                    f"{indent}if (ret >= (int)sizeof({buf})) {{\n"
                    f"{indent}    /* 切り詰め発生 */\n"
                    f"{indent}}}"
                ),
            )

    # --- strcpy使用 ---
    if "strcpy使用" in message:
        m = re.search(r'strcpy\s*\(\s*(\w+)\s*,\s*(.+?)\s*\)', line)
        if m:
            dst, src = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=(
                    f"{indent}strncpy({dst}, {src}, sizeof({dst}) - 1);\n"
                    f"{indent}{dst}[sizeof({dst}) - 1] = '\\0';"
                ),
                recommended=(
                    f"{indent}size_t len = strlen({src});\n"
                    f"{indent}if (len >= sizeof({dst})) {{\n"
                    f"{indent}    /* エラー処理: 入力が長すぎる */\n"
                    f"{indent}    return -1;\n"
                    f"{indent}}}\n"
                    f"{indent}memcpy({dst}, {src}, len + 1);"
                ),
            )

    # --- strcat使用 ---
    if "strcat使用" in message:
        m = re.search(r'strcat\s*\(\s*(\w+)\s*,\s*(.+?)\s*\)', line)
        if m:
            dst, src = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=(
                    f"{indent}strncat({dst}, {src}, sizeof({dst}) - strlen({dst}) - 1);"
                ),
                recommended=(
                    f"{indent}size_t remain = sizeof({dst}) - strlen({dst}) - 1;\n"
                    f"{indent}if (strlen({src}) > remain) {{\n"
                    f"{indent}    /* エラー処理: バッファ不足 */\n"
                    f"{indent}    return -1;\n"
                    f"{indent}}}\n"
                    f"{indent}strncat({dst}, {src}, remain);"
                ),
            )

    # --- NULL未検証 (memcpy等) ---
    if "NULL未検証" in message:
        m = re.search(r'(memcpy|memmove|memset)\s*\(\s*(\w+)', line)
        if m:
            func, var = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=(
                    f"{indent}if ({var} != NULL) {{\n"
                    f"{indent}    {line}\n"
                    f"{indent}}}"
                ),
                recommended=(
                    f"{indent}if ({var} == NULL) {{\n"
                    f"{indent}    /* エラー処理 */\n"
                    f"{indent}    return;\n"
                    f"{indent}}}\n"
                    f"{indent}{line}"
                ),
            )

    # --- 二重free ---
    if "二重free" in message:
        m = re.search(r'free\s*\(\s*(\w+)\s*\)', line)
        var = m.group(1) if m else "ptr"
        return FixSuggestion(
            minimal=f"{indent}if ({var} != NULL) {{ free({var}); {var} = NULL; }}",
            recommended=(
                f"{indent}/* 初回free時にNULL代入済みのため、この行を削除 */\n"
                f"{indent}/* free({var}); */"
            ),
        )

    # --- use-after-free ---
    if "use-after-free" in message:
        m = re.search(r'free済み(\w+)を参照', message)
        var = m.group(1) if m else "ptr"
        return FixSuggestion(
            minimal=(
                f"{indent}/* {var}はfree済み。この行の前でfree後に{var} = NULLを追加 */\n"
                f"{indent}/* {line}  ← 削除またはfreeの前に移動 */"
            ),
            recommended=(
                f"{indent}/* free直後に{var} = NULL; を追加し、使用前にNULLチェック */\n"
                f"{indent}if ({var} != NULL) {{\n"
                f"{indent}    {line}\n"
                f"{indent}}}"
            ),
        )

    # --- 未初期化変数 ---
    if "未初期化変数" in message:
        m = re.search(r'未初期化変数(\w+)を', message)
        var = m.group(1) if m else "x"
        m2 = re.search(r'(int|char|short|long|float|double|size_t|unsigned)\s+' + re.escape(var), line)
        if m2:
            typ = m2.group(1)
            init_val = "0" if typ in ("int", "short", "long", "size_t", "unsigned") else \
                       "'\\0'" if typ == "char" else "0.0"
            return FixSuggestion(
                minimal=f"{indent}{typ} {var} = {init_val};",
                recommended=(
                    f"{indent}{typ} {var} = {init_val};  /* 明示的初期化 */\n"
                    f"{indent}/* 可能なら宣言を初回代入箇所に移動 */"
                ),
            )

    # --- 書式文字列脆弱性 ---
    if "書式文字列" in message:
        m = re.search(r'(printf|fprintf|syslog)\s*\(\s*(\w+)\s*\)', line)
        if m:
            func, var = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=f"{indent}{func}(\"%s\", {var});",
                recommended=f"{indent}fputs({var}, stdout);  /* 書式不要なら出力関数変更 */",
            )
        # fprintf(fp, var) パターン
        m = re.search(r'fprintf\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)', line)
        if m:
            fp, var = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=f"{indent}fprintf({fp}, \"%s\", {var});",
                recommended=f"{indent}fputs({var}, {fp});",
            )

    # --- 整数オーバーフロー ---
    if "オーバーフロー未検証" in message:
        m = re.search(r'malloc\s*\(\s*(\w+)\s*\*\s*(\w+)', line)
        if m:
            a, b = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=f"{indent}/* {a} * {b} のオーバーフローチェックを追加 */\n{indent}{line}",
                recommended=(
                    f"{indent}if ({a} > 0 && {b} > SIZE_MAX / {a}) {{\n"
                    f"{indent}    /* オーバーフロー検出 */\n"
                    f"{indent}    return NULL;\n"
                    f"{indent}}}\n"
                    f"{indent}{line}"
                ),
            )

    # --- sizeof(ポインタ) ---
    if "sizeof" in message and "ポインタ" in message:
        m = re.search(r'sizeof\s*\(\s*(\w+)\s*\)', line)
        if m:
            var = m.group(1)
            return FixSuggestion(
                minimal=f"{indent}/* sizeof({var}) → sizeof(*{var}) に変更 */",
                recommended=f"{indent}/* sizeof({var}) は配列なら sizeof(配列)/sizeof(配列[0])、ポインタなら sizeof(*{var}) */",
            )

    # --- fdリーク / fclose ---
    if "fclose" in message and "リーク" in message:
        m = re.search(r'(\w+)\s*=\s*fopen', line)
        var = m.group(1) if m else "fp"
        return FixSuggestion(
            minimal=f"{indent}{line}\n{indent}/* ... */\n{indent}fclose({var});",
            recommended=(
                f"{indent}{line}\n"
                f"{indent}if ({var} == NULL) {{\n"
                f"{indent}    perror(\"fopen\");\n"
                f"{indent}    return -1;\n"
                f"{indent}}}\n"
                f"{indent}/* ... 処理 ... */\n"
                f"{indent}fclose({var});"
            ),
        )

    # --- リソースリーク (open/socket) ---
    if "リソースリーク" in message or "close()なし" in message:
        m = re.search(r'(\w+)\s*=\s*(open|socket|pipe)', line)
        if m:
            var = m.group(1)
            return FixSuggestion(
                minimal=f"{indent}{line}\n{indent}/* ... */\n{indent}close({var});",
                recommended=(
                    f"{indent}{line}\n"
                    f"{indent}if ({var} < 0) {{\n"
                    f"{indent}    perror(\"{m.group(2)}\");\n"
                    f"{indent}    return -1;\n"
                    f"{indent}}}\n"
                    f"{indent}/* ... 処理 ... */\n"
                    f"{indent}close({var});"
                ),
            )

    # --- fall-through ---
    if "fall-through" in message:
        return FixSuggestion(
            minimal=f"{indent}break;  /* case末尾に追加 */",
            recommended=f"{indent}break;\n{indent}/* 意図的なfall-throughなら: */\n{indent}/* FALLTHROUGH */",
        )

    # --- バッファオーバーラン ---
    if "バッファオーバーラン" in message:
        m = re.search(r'(strncpy|memcpy)\s*\(\s*(\w+)\s*,\s*(.+?)\s*,\s*(.+?)\s*\)', line)
        if m:
            func, dst = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=f"{indent}{func}({dst}, ..., sizeof({dst}));",
                recommended=(
                    f"{indent}{func}({dst}, ..., sizeof({dst}) - 1);\n"
                    f"{indent}{dst}[sizeof({dst}) - 1] = '\\0';"
                    if func == "strncpy" else
                    f"{indent}{func}({dst}, ..., sizeof({dst}));"
                ),
            )

    # --- デッドロック (mutex) ---
    if "デッドロック" in message or "unlock" in message.lower():
        return FixSuggestion(
            minimal=f"{indent}pthread_mutex_unlock(&mutex);  /* 全パスに追加 */",
            recommended=(
                f"{indent}pthread_mutex_lock(&mutex);\n"
                f"{indent}/* ... 排他処理 ... */\n"
                f"{indent}pthread_mutex_unlock(&mutex);\n"
                f"{indent}/* エラーパスにもunlockを忘れずに */"
            ),
        )

    # --- TOCTOU ---
    if "TOCTOU" in message:
        return FixSuggestion(
            minimal=f"{indent}/* access()を削除し、open()の戻り値で直接判定 */",
            recommended=(
                f"{indent}int fd = open(path, O_RDONLY);\n"
                f"{indent}if (fd < 0) {{\n"
                f"{indent}    /* ファイルなし or 権限なし */\n"
                f"{indent}    return -1;\n"
                f"{indent}}}\n"
                f"{indent}/* fdを使って操作 */"
            ),
        )

    # --- async-signal-unsafe ---
    if "async-signal-unsafe" in message:
        return FixSuggestion(
            minimal=f"{indent}/* シグナルハンドラ内ではwrite()を使用 */",
            recommended=(
                f"{indent}static volatile sig_atomic_t flag = 0;\n"
                f"{indent}/* ハンドラ内: */\n"
                f"{indent}void handler(int sig) {{ flag = 1; }}\n"
                f"{indent}/* メインループで: */\n"
                f"{indent}if (flag) {{ /* 安全に処理 */ }}"
            ),
        )

    # --- snprintf戻り値 (切り詰めより先に判定) ---
    if "snprintf" in message and "戻り値" in message:
        m = re.search(r'snprintf\s*\(\s*(\w+)\s*,\s*([^,]+)\s*,', line)
        if m:
            buf, size = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=f"{indent}(void)snprintf({buf}, {size}, ...);  /* 明示的に無視 */",
                recommended=(
                    f"{indent}int ret = snprintf({buf}, {size}, ...);\n"
                    f"{indent}if (ret < 0 || ret >= (int){size}) {{\n"
                    f"{indent}    /* 出力エラーまたは切り詰め発生 */\n"
                    f"{indent}}}"
                ),
            )

    # --- 切り詰めキャスト ---
    if "切り詰め" in message:
        return FixSuggestion(
            minimal=f"{indent}/* キャスト前に値域チェックを追加 */",
            recommended=(
                f"{indent}if (value > TARGET_MAX || value < TARGET_MIN) {{\n"
                f"{indent}    /* 範囲外エラー */\n"
                f"{indent}    return -1;\n"
                f"{indent}}}\n"
                f"{indent}target = (target_type)value;"
            ),
        )

    # --- ビットフィールド符号 ---
    if "符号未指定" in message:
        m = re.search(r'\bint\s+(\w+)\s*:\s*(\d+)', line)
        if m:
            field, bits = m.group(1), m.group(2)
            return FixSuggestion(
                minimal=f"{indent}unsigned int {field} : {bits};",
                recommended=f"{indent}unsigned int {field} : {bits};  /* または signed int {field} : {bits}; 意図を明示 */",
            )

    # --- VLA ---
    if "可変長配列" in message:
        m = re.search(r'(\w+)\s+(\w+)\s*\[\s*(\w+)\s*\]', line)
        if m:
            typ, arr, size = m.group(1), m.group(2), m.group(3)
            return FixSuggestion(
                minimal=f"{indent}{typ} *{arr} = malloc({size} * sizeof({typ}));",
                recommended=(
                    f"{indent}{typ} *{arr} = malloc({size} * sizeof({typ}));\n"
                    f"{indent}if ({arr} == NULL) {{\n"
                    f"{indent}    return -1;\n"
                    f"{indent}}}\n"
                    f"{indent}/* ... 処理 ... */\n"
                    f"{indent}free({arr});"
                ),
            )

    # --- goto ---
    if "前方" in message and "goto" in message.lower():
        return FixSuggestion(
            minimal=f"{indent}/* gotoをwhile/forループに構造化 */",
            recommended=(
                f"{indent}/* エラー処理パターン (唯一許容されるgoto): */\n"
                f"{indent}if (error) goto cleanup;\n"
                f"{indent}/* ... */\n"
                f"{indent}cleanup:\n"
                f"{indent}    free(resource);\n"
                f"{indent}    return -1;"
            ),
        )

    # --- マジックナンバー ---
    if "マジックナンバー" in message:
        m = re.search(r'(\d+)', line)
        num = m.group(1) if m else "VALUE"
        return FixSuggestion(
            minimal=f"{indent}#define MAGIC_{num} {num}  /* 定数定義を追加 */",
            recommended=(
                f"{indent}enum {{ BUFFER_SIZE = {num} }};  /* または */\n"
                f"{indent}static const int PARAM = {num};"
            ),
        )

    # --- defaultなし ---
    if "defaultなし" in message:
        return FixSuggestion(
            minimal=f"{indent}default: break;",
            recommended=(
                f"{indent}default:\n"
                f"{indent}    assert(0 && \"未定義のenum値\");\n"
                f"{indent}    break;"
            ),
        )

    # --- グローバル変数 ---
    if "グローバル変数" in message:
        m = re.search(r'(\w+)を直接更新', message)
        var = m.group(1) if m else "g_var"
        return FixSuggestion(
            minimal=f"{indent}/* {var} を引数経由で渡す */",
            recommended=(
                f"{indent}/* 関数の引数にポインタで渡す: */\n"
                f"{indent}void func({type(var).__name__} *{var}_ptr) {{\n"
                f"{indent}    *{var}_ptr = new_value;\n"
                f"{indent}}}"
            ),
        )

    # --- 自己再帰 ---
    if "自己再帰" in message:
        return FixSuggestion(
            minimal=f"{indent}/* depth引数を追加し上限チェック */",
            recommended=(
                f"{indent}void func(int depth) {{\n"
                f"{indent}    if (depth > MAX_DEPTH) return;\n"
                f"{indent}    /* ... */\n"
                f"{indent}    func(depth + 1);\n"
                f"{indent}}}"
            ),
        )

    # --- 無限ループ ---
    if "無限ループ" in message:
        return FixSuggestion(
            minimal=f"{indent}/* break/return条件を追加 */",
            recommended=(
                f"{indent}int count = 0;\n"
                f"{indent}while (1) {{\n"
                f"{indent}    if (++count > MAX_ITER || done) break;\n"
                f"{indent}    /* ... */\n"
                f"{indent}}}"
            ),
        )

    # --- signed/unsigned比較 ---
    if "signed" in message and "unsigned" in message and "比較" in message:
        return FixSuggestion(
            minimal=f"{indent}/* 比較前にキャストで型を揃える */",
            recommended=(
                f"{indent}/* signed値が負でないことを確認してからキャスト: */\n"
                f"{indent}if (signed_val >= 0 && (unsigned)signed_val < unsigned_val) {{\n"
                f"{indent}    /* ... */\n"
                f"{indent}}}"
            ),
        )

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# --similar 水平展開 (類似パターン検索)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class SimilarLocation:
    filepath: str
    line: int
    source: str  # 該当行のソース


def _extract_similar_pattern(message: str, source_line: str) -> Optional[str]:
    """指摘メッセージとソース行から類似パターン検索用の正規表現を生成"""
    line = source_line.strip()

    # malloc/calloc/realloc NULLチェック漏れ → 同じalloc呼び出しパターン
    if "NULLチェックなし" in message and ("malloc" in message or "calloc" in message or "realloc" in message):
        return r'\b\w+\s*=\s*(?:malloc|calloc|realloc)\s*\('

    # gets使用
    if "gets使用" in message:
        return r'\bgets\s*\('

    # sprintf使用
    if "sprintf使用" in message:
        return r'\bsprintf\s*\('

    # strcpy使用
    if "strcpy使用" in message:
        return r'\bstrcpy\s*\('

    # strcat使用
    if "strcat使用" in message:
        return r'\bstrcat\s*\('

    # NULL未検証 (memcpy等)
    if "NULL未検証" in message:
        return r'\b(?:memcpy|memmove|memset)\s*\('

    # 二重free
    if "二重free" in message:
        return r'\bfree\s*\('

    # use-after-free
    if "use-after-free" in message:
        return r'\bfree\s*\('

    # 書式文字列脆弱性
    if "書式文字列" in message:
        return r'\b(?:printf|fprintf|syslog)\s*\(\s*\w+\s*\)'

    # 整数オーバーフロー
    if "オーバーフロー未検証" in message:
        return r'\bmalloc\s*\(\s*\w+\s*\*\s*\w+'

    # sizeof(ポインタ)
    if "sizeof" in message and "ポインタ" in message:
        return r'\bsizeof\s*\(\s*\w+\s*\)'

    # fclose漏れ
    if "fclose" in message and "リーク" in message:
        return r'\bfopen\s*\('

    # リソースリーク (open/socket)
    if "リソースリーク" in message or "close()なし" in message:
        return r'\b(?:open|socket)\s*\('

    # fall-through
    if "fall-through" in message:
        return r'\bcase\s+\w+'

    # バッファオーバーラン
    if "バッファオーバーラン" in message:
        return r'\b(?:strncpy|memcpy)\s*\('

    # デッドロック (mutex)
    if "デッドロック" in message:
        return r'\bpthread_mutex_lock\s*\('

    # TOCTOU
    if "TOCTOU" in message:
        return r'\baccess\s*\('

    # async-signal-unsafe
    if "async-signal-unsafe" in message:
        return r'\bsignal\s*\('

    # 未初期化変数
    if "未初期化変数" in message:
        return r'\b(?:int|char|short|long|float|double|size_t|unsigned)\s+\w+\s*;'

    # 可変長配列
    if "可変長配列" in message:
        return r'\b\w+\s+\w+\s*\[\s*[a-zA-Z_]\w*\s*\]'

    # snprintf戻り値
    if "snprintf" in message and "戻り値" in message:
        return r'\bsnprintf\s*\('

    # マジックナンバー
    if "マジックナンバー" in message:
        return r'\b(?:if|while|for|case|return)\s*.*\b\d{2,}\b'

    # signed/unsigned比較
    if "signed" in message and "unsigned" in message:
        return r'(?:signed|unsigned)\s+\w+'

    # 自己再帰
    if "自己再帰" in message:
        m = re.search(r'(\w+)が自己再帰', message)
        if m:
            func = m.group(1)
            return rf'\b{re.escape(func)}\s*\('

    return None


def find_similar_issues(issue: Issue, all_files: List[str],
                        file_cache: Dict[str, List[str]]) -> List[SimilarLocation]:
    """指摘と同じパターンが他のファイル・行にないか検索"""
    src_line = _read_source_line(issue.filepath, issue.line)
    pattern_str = _extract_similar_pattern(issue.message, src_line)
    if pattern_str is None:
        return []

    pattern = re.compile(pattern_str)
    results: List[SimilarLocation] = []

    for fpath in all_files:
        if fpath not in file_cache:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
                file_cache[fpath] = strip_comments_and_strings(source)
            except OSError:
                continue

        cl = file_cache[fpath]
        for i, line in enumerate(cl):
            line_num = i + 1
            # 自分自身の指摘行はスキップ
            if fpath == issue.filepath and line_num == issue.line:
                continue
            if pattern.search(line):
                raw_line = _read_source_line(fpath, line_num)
                results.append(SimilarLocation(fpath, line_num, raw_line.strip()))

    return results


# --preset用: プリセットグループ定義
PRESETS = {
    "memory": {
        "description": "メモリ関連バグ検出",
        "rules": {"null_deref", "double_free", "use_after_free", "fd_leak",
                  "resource_leak", "memcpy_no_null", "integer_overflow",
                  "buffer_overrun", "stack_usage"},
    },
    "security": {
        "description": "セキュリティ脆弱性検出",
        "rules": {"unsafe_funcs", "format_string", "array_index", "toctou",
                  "buffer_overrun", "vla", "integer_overflow", "uninitialized"},
    },
    "concurrency": {
        "description": "並行処理・排他制御チェック",
        "rules": {"mutex_unlock", "volatile", "signal_unsafe", "toctou"},
    },
    "style": {
        "description": "コーディング規約・保守性チェック",
        "rules": {"globals", "macros", "magic_numbers", "snprintf_retval",
                  "recursive_no_limit", "goto_misuse", "switch_fallthrough",
                  "enum_switch", "tiny_function", "linkage", "undefined_call"},
    },
    "pr": {
        "description": "PR前チェック (diff + fix-hint有効化)",
        "options": {"diff": True, "fix_hint": True},
        "rules": None,  # 全ルール
    },
    "strict": {
        "description": "全ルール + 全指摘で終了コード1",
        "options": {"exit_code": "maint"},
        "rules": None,  # 全ルール
    },
}


def resolve_preset(preset_name: str, args):
    """プリセットをargsに反映。ルール制限セットを返す(Noneなら全ルール)"""
    if preset_name not in PRESETS:
        available = ", ".join(PRESETS.keys())
        print(f"エラー: 不明なプリセット '{preset_name}'。利用可能: {available}",
              file=sys.stderr)
        sys.exit(1)
    preset = PRESETS[preset_name]
    # オプション上書き
    opts = preset.get("options", {})
    for key, val in opts.items():
        setattr(args, key, val)
    return preset.get("rules")


def interpret_ask_with_api(instruction: str, config) -> dict:
    """自然言語の指示をClaude APIで解釈し、オプションJSONを返す"""
    preset_list = "\n".join(f"  {name}: {p['description']}" for name, p in PRESETS.items())
    rule_list = "\n".join(f"  {name}: {desc}" for name, desc in RULE_DESCRIPTIONS.items())

    system_prompt = """あなたはC言語静的解析ツール「creview」のオプション解釈AIです。
ユーザーの自然言語の指示を解析し、適切なオプションをJSON形式で返してください。

利用可能なプリセット:
""" + preset_list + """

利用可能な個別ルール:
""" + rule_list + """

利用可能なオプション:
  severity: "critical" | "design" | "maint" (表示する重大度フィルタ)
  fix_hint: true | false (修正ヒント表示)
  diff: true | false (git diff変更行のみ)
  exit_code: "critical" | "design" | "maint" (終了コード閾値)
  rules: ["rule1", "rule2", ...] (実行するルール名リスト。省略で全ルール)
  preset: "preset_name" (プリセット名。rulesより優先)

以下の形式でJSONのみを返してください。説明文は不要です:
{"severity": null, "fix_hint": false, "diff": false, "exit_code": "critical", "rules": null, "preset": null}

nullは「指定なし(デフォルト)」を意味します。"""

    user_content = f"指示: {instruction}"

    try:
        result = call_claude_api(system_prompt, user_content, config)
        # JSON部分を抽出
        m = re.search(r'\{[^{}]*\}', result, re.DOTALL)
        if m:
            return json.loads(m.group())
        return {}
    except (APIError, json.JSONDecodeError):
        return {}


def apply_ask_result(ask_result: dict, args):
    """--ask APIの解釈結果をargsに反映。ルール制限セットを返す"""
    if not ask_result:
        print("指示の解釈に失敗しました。通常モードで実行します。", file=sys.stderr)
        return None

    # プリセットが指定された場合
    preset_name = ask_result.get("preset")
    if preset_name and preset_name in PRESETS:
        return resolve_preset(preset_name, args)

    # 個別オプション反映
    if ask_result.get("severity"):
        args.severity = ask_result["severity"]
    if ask_result.get("fix_hint"):
        args.fix_hint = True
    if ask_result.get("diff"):
        args.diff = True
    if ask_result.get("exit_code"):
        args.exit_code = ask_result["exit_code"]

    # ルール制限
    rules = ask_result.get("rules")
    if rules and isinstance(rules, list):
        valid_rules = set(RULE_DESCRIPTIONS.keys())
        return set(r for r in rules if r in valid_rules) or None
    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 出力フォーマッタ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_diff_lines(filepath: str) -> Optional[Set[int]]:
    """git diffから変更行番号を取得。git外ならNone"""
    try:
        result = subprocess.run(
            ["git", "diff", "--unified=0", "HEAD", "--", filepath],
            capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            # ステージング済みも試行
            result = subprocess.run(
                ["git", "diff", "--unified=0", "--cached", "--", filepath],
                capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    changed: Set[int] = set()
    for line in result.stdout.split("\n"):
        # @@ -a,b +c,d @@ 形式をパース
        m = re.match(r'^@@ .* \+(\d+)(?:,(\d+))? @@', line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            for ln in range(start, start + count):
                changed.add(ln)
    return changed


def filter_by_diff(issues: List[Issue], diff_lines: Set[int]) -> List[Issue]:
    """差分行に該当する指摘のみ残す"""
    return [iss for iss in issues if iss.line in diff_lines]


def _read_source_line(filepath: str, line_num: int) -> str:
    """ファイルの指定行を読み取る"""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i == line_num:
                    return line.rstrip("\n")
    except OSError:
        pass
    return ""


def format_text(issues: List[Issue], fix_hint: bool = False,
                fix: bool = False,
                similar_map: Optional[Dict] = None) -> str:
    if not issues:
        return "重大なし\n設計不明なし\n保守危険なし"
    lines = []
    for iss in issues:
        lines.append(f"[{iss.severity.value}]")
        lines.append(f"{iss.filepath}:{iss.line}")
        lines.append(iss.message)
        if fix_hint:
            hint = get_fix_hint(iss.message)
            if hint:
                lines.append(hint)
        if fix and iss.line > 0:
            src_line = _read_source_line(iss.filepath, iss.line)
            suggestion = generate_fix(iss.message, src_line)
            if suggestion:
                lines.append("  [最小修正]")
                for sl in suggestion.minimal.split("\n"):
                    lines.append(f"    {sl}")
                lines.append("  [推奨修正]")
                for sl in suggestion.recommended.split("\n"):
                    lines.append(f"    {sl}")
        if similar_map is not None:
            key = (iss.filepath, iss.line)
            similar = similar_map.get(key, [])
            if similar:
                lines.append(f"  [類似箇所] {len(similar)}件")
                for loc in similar:
                    lines.append(f"    {loc.filepath}:{loc.line}  {loc.source}")
        lines.append("")
    return "\n".join(lines)


def format_markdown(issues: List[Issue], filepath: str,
                     fix_hint: bool = False, fix: bool = False,
                     similar_map: Optional[Dict] = None) -> str:
    """Markdown形式で指摘を出力"""
    lines = [f"## {filepath}\n"]
    if not issues:
        lines.append("問題なし\n")
        return "\n".join(lines)
    severity_map = {Severity.CRITICAL: "🔴", Severity.DESIGN: "🟡", Severity.MAINT: "🔵"}
    for sev in [Severity.CRITICAL, Severity.DESIGN, Severity.MAINT]:
        sev_issues = [i for i in issues if i.severity == sev]
        if not sev_issues:
            continue
        icon = severity_map[sev]
        lines.append(f"### {icon} {sev.value} ({len(sev_issues)}件)\n")
        for iss in sev_issues:
            hint_str = ""
            if fix_hint:
                hint = get_fix_hint(iss.message)
                if hint:
                    hint_str = f" `{hint}`"
            lines.append(f"- **L{iss.line}**: {iss.message}{hint_str}")
            if fix and iss.line > 0:
                src_line = _read_source_line(iss.filepath, iss.line)
                suggestion = generate_fix(iss.message, src_line)
                if suggestion:
                    lines.append(f"  - **最小修正:**")
                    lines.append(f"    ```c")
                    lines.append(f"    {suggestion.minimal}")
                    lines.append(f"    ```")
                    lines.append(f"  - **推奨修正:**")
                    lines.append(f"    ```c")
                    lines.append(f"    {suggestion.recommended}")
                    lines.append(f"    ```")
            if similar_map is not None:
                key = (iss.filepath, iss.line)
                similar = similar_map.get(key, [])
                if similar:
                    lines.append(f"  - **類似箇所** ({len(similar)}件):")
                    for loc in similar:
                        lines.append(f"    - `{loc.filepath}:{loc.line}` {loc.source}")
        lines.append("")
    return "\n".join(lines)


def format_sarif(all_issues: Dict[str, List[Issue]]) -> str:
    """SARIF 2.1.0形式で出力（GitHub Code Scanning連携用）"""
    severity_to_level = {
        Severity.CRITICAL: "error",
        Severity.DESIGN: "warning",
        Severity.MAINT: "note",
    }
    results = []
    rules_seen: Dict[str, int] = {}
    rules = []
    for filepath, issues in all_issues.items():
        for iss in issues:
            # ルールIDを生成（メッセージの最初の数文字をハッシュ化）
            rule_key = iss.message.split("。")[0] if "。" in iss.message else iss.message[:30]
            if rule_key not in rules_seen:
                rules_seen[rule_key] = len(rules)
                rules.append({
                    "id": f"CRV{len(rules)+1:03d}",
                    "shortDescription": {"text": rule_key},
                })
            results.append({
                "ruleId": rules[rules_seen[rule_key]]["id"],
                "level": severity_to_level.get(iss.severity, "note"),
                "message": {"text": iss.message},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": filepath},
                        "region": {"startLine": iss.line}
                    }
                }]
            })
    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "creview",
                    "version": VERSION,
                    "rules": rules,
                }
            },
            "results": results,
        }]
    }
    return json.dumps(sarif, ensure_ascii=False, indent=2)


def format_json_v2(issues: List[Issue], target_label: str) -> str:
    obj = {
        "version": 2,
        "tool": "creview",
        "tool_version": VERSION,
        "target": target_label,
        "issue_count": len(issues),
        "issues": [
            {
                "severity": iss.severity.value,
                "file": iss.filepath,
                "line": iss.line,
                "message": iss.message
            }
            for iss in issues
        ]
    }
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ファイル収集
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def collect_c_files(paths: List[str]) -> List[str]:
    result = []
    for p in paths:
        if os.path.isfile(p):
            result.append(p)
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                for fn in sorted(files):
                    if fn.endswith((".c", ".h")):
                        result.append(os.path.join(root, fn))
    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# メインCLI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main():
    parser = argparse.ArgumentParser(
        prog="creview",
        description="C言語設計レビュー (指摘専用。コード生成・修正案 一切なし)",
    )
    parser.add_argument("targets", nargs="*",
                        help="対象 .c/.h ファイルまたはディレクトリ")
    parser.add_argument("--spec", action="store_true",
                        help="仕様レビューモード (対象は仕様テキストファイル)")
    parser.add_argument("--format", choices=["text", "json", "markdown", "sarif"], default="text",
                        help="出力形式 (default: text)")
    parser.add_argument("--local-only", action="store_true",
                        help="ローカル静的解析のみ (API呼び出しなし)")
    parser.add_argument("--diff", action="store_true",
                        help="git diffの変更行のみレビュー")
    parser.add_argument("--severity",
                        choices=["critical", "design", "maint"],
                        help="指定重大度のみ表示")
    parser.add_argument("--count", action="store_true",
                        help="重大度別集計のみ出力")
    parser.add_argument("--fix-hint", action="store_true",
                        help="各指摘に修正ヒントを付与")
    parser.add_argument("--fix", action="store_true",
                        help="各指摘に最小修正・推奨修正のコード例を表示")
    parser.add_argument("--similar", action="store_true",
                        help="各指摘の類似パターンを全対象ファイルから水平展開")
    parser.add_argument("--stack", action="store_true",
                        help="関数別スタック使用量レポートを表示")
    parser.add_argument("--stack-threshold", type=int,
                        default=DEFAULT_STACK_THRESHOLD,
                        help=f"スタック超過閾値バイト(デフォルト{DEFAULT_STACK_THRESHOLD})")
    parser.add_argument("--buf-usage", action="store_true",
                        help="バッファ宣言サイズに対する書き込み使用率レポート")
    parser.add_argument("--baseline",
                        help="ベースラインJSONファイル。新規指摘のみ表示")
    parser.add_argument("--exit-code",
                        choices=["critical", "design", "maint"],
                        default="critical",
                        help="終了コード1を返す閾値 (default: critical)")
    parser.add_argument("--preset",
                        help="プリセットグループ (memory/security/concurrency/style/pr/strict)")
    parser.add_argument("--ask",
                        help="自然言語でチェック内容を指示 (API使用)")
    parser.add_argument("--list-rules", action="store_true",
                        help="利用可能な全ルール名を表示して終了")
    parser.add_argument("--list-presets", action="store_true",
                        help="利用可能なプリセット一覧を表示して終了")
    parser.add_argument("--version", action="version",
                        version=f"creview {VERSION}")
    args = parser.parse_args()

    # --list-rules: ルール一覧表示して終了
    if args.list_rules:
        for name, desc in RULE_DESCRIPTIONS.items():
            print(f"  {name:24s} {desc}")
        sys.exit(0)

    # --list-presets: プリセット一覧表示して終了
    if args.list_presets:
        for name, preset in PRESETS.items():
            rules_info = "全ルール" if preset.get("rules") is None else f"{len(preset['rules'])}ルール"
            print(f"  {name:16s} {preset['description']} ({rules_info})")
        sys.exit(0)

    if not args.targets:
        parser.error("対象ファイルまたはディレクトリを指定してください")

    config = load_config()

    # ── --preset / --ask 解決 ──
    only_rules = None  # None = 全ルール実行
    if args.preset:
        only_rules = resolve_preset(args.preset, args)
    elif args.ask:
        if not config.api_key:
            print("エラー: --ask にはAPI_KEYが必要", file=sys.stderr)
            sys.exit(1)
        print(f"指示を解釈中: 「{args.ask}」...", file=sys.stderr)
        ask_result = interpret_ask_with_api(args.ask, config)
        only_rules = apply_ask_result(ask_result, args)

    # ── 仕様レビューモード ──
    if args.spec:
        if not config.api_key:
            print("エラー: 仕様レビューにはAPI_KEYが必要", file=sys.stderr)
            if find_config():
                print("config.txt の API_KEY= にClaude APIキーを入力してください", file=sys.stderr)
            else:
                print("config.txt.sample を config.txt にリネームしてAPIキーを入力してください", file=sys.stderr)
            sys.exit(1)
        for spec_path in args.targets:
            try:
                result = run_api_review_spec(spec_path, config)
                print(result)
            except APIError as e:
                print(f"レビュー失敗", file=sys.stderr)
                sys.exit(1)
        sys.exit(0)

    # ── コードレビューモード ──
    files = collect_c_files(args.targets)
    if not files:
        print("対象ファイルなし", file=sys.stderr)
        sys.exit(1)

    has_critical = False
    has_design = False
    has_maint = False
    sarif_issues: Dict[str, List[Issue]] = {}  # SARIF用に全ファイル蓄積
    similar_cache: Dict[str, List[str]] = {}   # --similar用: ファイルパス→cleaned lines
    # --severity フィルタ用マッピング
    severity_filter = None
    if args.severity:
        sev_map = {"critical": Severity.CRITICAL, "design": Severity.DESIGN, "maint": Severity.MAINT}
        severity_filter = sev_map[args.severity]
    # --count 集計用
    count_totals: Dict[str, Dict[str, int]] = {}  # file -> {severity -> count}
    # --baseline: ベースライン読み込み
    baseline_issues = None
    if args.baseline:
        try:
            with open(args.baseline, "r", encoding="utf-8") as f:
                bl_data = json.load(f)
            if isinstance(bl_data, dict) and "issues" in bl_data:
                baseline_issues = bl_data["issues"]
            elif isinstance(bl_data, list):
                baseline_issues = bl_data
            else:
                baseline_issues = []
        except (OSError, json.JSONDecodeError) as e:
            print(f"ベースライン読み込み失敗: {e}", file=sys.stderr)
            baseline_issues = []

    for fpath in files:
        ignore = find_ignore(fpath)

        # EXCLUDE判定
        if is_excluded(fpath, ignore):
            continue

        # Phase 1: ローカル静的解析
        local_issues = run_local_analysis(fpath, ignore, only_rules=only_rules,
                                          stack_threshold=args.stack_threshold)

        # --diff: 変更行のみにフィルタ
        if args.diff:
            diff_lines = get_diff_lines(fpath)
            if diff_lines is not None:
                local_issues = filter_by_diff(local_issues, diff_lines)

        # --severity: 重大度フィルタ
        if severity_filter:
            local_issues = [i for i in local_issues if i.severity == severity_filter]

        # --baseline: ベースラインとの差分
        if baseline_issues is not None:
            baseline_set = set()
            for bi in baseline_issues:
                baseline_set.add((bi.get("file", ""), bi.get("line", 0), bi.get("message", "")))
            local_issues = [i for i in local_issues
                            if (i.filepath, i.line, i.message) not in baseline_set]

        # SARIF用に蓄積
        if args.format == "sarif":
            sarif_issues[fpath] = local_issues

        # --count: 集計モード
        if args.count:
            count_totals[fpath] = {
                "重大": sum(1 for i in local_issues if i.severity == Severity.CRITICAL),
                "設計不明": sum(1 for i in local_issues if i.severity == Severity.DESIGN),
                "保守危険": sum(1 for i in local_issues if i.severity == Severity.MAINT),
            }
        elif args.format == "sarif":
            pass  # 全ファイル処理後にまとめて出力
        else:
            use_hint = args.fix_hint
            use_fix = args.fix
            sim_map = None
            if args.similar and local_issues:
                sim_map = {}
                for iss in local_issues:
                    similar = find_similar_issues(iss, files, similar_cache)
                    if similar:
                        sim_map[(iss.filepath, iss.line)] = similar
            if args.format == "json":
                print(format_json_v2(local_issues, fpath))
            elif args.format == "markdown":
                print(format_markdown(local_issues, fpath, fix_hint=use_hint,
                                       fix=use_fix, similar_map=sim_map))
            else:
                if local_issues:
                    print(f"── ローカル解析: {fpath} ──")
                    print(format_text(local_issues, fix_hint=use_hint,
                                       fix=use_fix, similar_map=sim_map))

        # --stack / --buf-usage: 解析レポート
        if args.stack or args.buf_usage:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as sf:
                    report_src = sf.read()
                report_raw = report_src.split("\n")
                report_cl = strip_comments_and_strings(report_src)
                if args.stack:
                    stack_infos = analyze_file_stack(fpath, report_raw, report_cl)
                    if stack_infos:
                        print(format_stack_report(fpath, stack_infos))
                if args.buf_usage:
                    buf_infos = analyze_buf_usage(fpath, report_raw, report_cl)
                    if buf_infos:
                        print(format_buf_report(fpath, buf_infos))
            except OSError:
                pass

        if any(i.severity == Severity.CRITICAL for i in local_issues):
            has_critical = True
        if any(i.severity == Severity.DESIGN for i in local_issues):
            has_design = True
        if any(i.severity == Severity.MAINT for i in local_issues):
            has_maint = True

        # Phase 2: API深層レビュー (--local-onlyでスキップ)
        if args.local_only:
            pass  # 明示的スキップ
        elif not config.api_key:
            # ★ 事故防止: APIキー未設定を明示。ユーザーに誤認させない
            if args.format != "json":
                print(f"\n── AI深層レビュー: {fpath} ──")
                print("スキップ（API_KEY未設定。ローカル解析のみ実行済み）")
            else:
                print(json.dumps({
                    "type": "api_review",
                    "file": fpath,
                    "result": "スキップ（API_KEY未設定）"
                }, ensure_ascii=False, indent=2))
        else:
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    source = f.read()
                api_result = run_api_review_code(fpath, source, config)
                if args.format != "json":
                    print(f"\n── AI深層レビュー: {fpath} ──")
                    print(api_result)
                else:
                    # JSON時はAPI結果をraw_textとして追加出力
                    print(json.dumps({
                        "type": "api_review",
                        "file": fpath,
                        "result": api_result
                    }, ensure_ascii=False, indent=2))
            except APIError:
                if args.format != "json":
                    print(f"\n── AI深層レビュー: {fpath} ──")
                    print("レビュー失敗")
                else:
                    print(json.dumps({
                        "type": "api_review",
                        "file": fpath,
                        "result": "レビュー失敗"
                    }, ensure_ascii=False))

    # --format sarif: まとめて出力
    if args.format == "sarif":
        print(format_sarif(sarif_issues))

    # --count: 集計出力
    if args.count:
        total_c, total_d, total_m = 0, 0, 0
        for fpath, counts in count_totals.items():
            c, d, m = counts["重大"], counts["設計不明"], counts["保守危険"]
            total_c += c
            total_d += d
            total_m += m
            if c + d + m > 0:
                print(f"{fpath}: 重大={c} 設計不明={d} 保守危険={m}")
        print(f"合計: 重大={total_c} 設計不明={total_d} 保守危険={total_m}")

    # --exit-code 閾値判定
    exit_threshold = args.exit_code
    if exit_threshold == "critical":
        should_fail = has_critical
    elif exit_threshold == "design":
        should_fail = has_critical or has_design
    else:  # maint
        should_fail = has_critical or has_design or has_maint
    sys.exit(1 if should_fail else 0)


if __name__ == "__main__":
    main()
