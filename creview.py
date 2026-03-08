#!/usr/bin/env python3
"""
creview v0.6.0 - C言語設計レビュー専用CLI
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
VERSION = "0.6.0"
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

    def __post_init__(self):
        if self.exclude_patterns is None:
            self.exclude_patterns = []


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
    return cfg


def _ignore_has_any(ig: IgnoreConfig) -> bool:
    """ignoreフラグが1つでもTrueか"""
    return any([ig.global_ok, ig.macro_allow, ig.volatile_ok,
                ig.packed_ok, ig.magic_ok, len(ig.exclude_patterns) > 0])


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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 1: ローカル静的解析エンジン
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_local_analysis(filepath: str, ignore: IgnoreConfig) -> List[Issue]:
    issues: List[Issue] = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except OSError as e:
        issues.append(Issue(Severity.CRITICAL, filepath, 0, f"ファイル読み込み失敗: {e}"))
        return issues

    raw = source.split("\n")
    cl = strip_comments_and_strings(source)

    check_null_deref(filepath, raw, cl, issues)
    check_unsafe_funcs(filepath, raw, cl, issues)
    check_memcpy_no_null(filepath, raw, cl, issues)
    check_array_index(filepath, raw, cl, issues)
    check_double_free(filepath, raw, cl, issues)
    check_return_inconsistency(filepath, raw, cl, issues)
    check_globals(filepath, raw, cl, issues, ignore)
    check_macros(filepath, raw, cl, issues, ignore)
    check_switch_fallthrough(filepath, raw, cl, issues)
    check_sizeof_pointer(filepath, raw, cl, issues)
    check_fd_leak(filepath, raw, cl, issues)
    check_magic_numbers(filepath, raw, cl, issues, ignore)
    check_volatile(filepath, raw, cl, issues, ignore)
    check_packed(filepath, raw, cl, issues, ignore)
    check_format_string(filepath, raw, cl, issues)
    check_use_after_free(filepath, raw, cl, issues)
    check_uninitialized(filepath, raw, cl, issues)
    check_sign_compare(filepath, raw, cl, issues)
    check_integer_overflow(filepath, raw, cl, issues)
    check_resource_leak(filepath, raw, cl, issues)
    check_snprintf_retval(filepath, raw, cl, issues)

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


def format_text(issues: List[Issue]) -> str:
    if not issues:
        return "重大なし\n設計不明なし\n保守危険なし"
    lines = []
    for iss in issues:
        lines.append(f"[{iss.severity.value}]")
        lines.append(f"{iss.filepath}:{iss.line}")
        lines.append(iss.message)
        lines.append("")
    return "\n".join(lines)


def format_markdown(issues: List[Issue], filepath: str) -> str:
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
            lines.append(f"- **L{iss.line}**: {iss.message}")
        lines.append("")
    return "\n".join(lines)


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
    parser.add_argument("targets", nargs="+",
                        help="対象 .c/.h ファイルまたはディレクトリ")
    parser.add_argument("--spec", action="store_true",
                        help="仕様レビューモード (対象は仕様テキストファイル)")
    parser.add_argument("--format", choices=["text", "json", "markdown"], default="text",
                        help="出力形式 (default: text)")
    parser.add_argument("--local-only", action="store_true",
                        help="ローカル静的解析のみ (API呼び出しなし)")
    parser.add_argument("--diff", action="store_true",
                        help="git diffの変更行のみレビュー")
    parser.add_argument("--version", action="version",
                        version=f"creview {VERSION}")
    args = parser.parse_args()

    config = load_config()

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

    for fpath in files:
        ignore = find_ignore(fpath)

        # EXCLUDE判定
        if is_excluded(fpath, ignore):
            continue

        # Phase 1: ローカル静的解析
        local_issues = run_local_analysis(fpath, ignore)

        # --diff: 変更行のみにフィルタ
        if args.diff:
            diff_lines = get_diff_lines(fpath)
            if diff_lines is not None:
                local_issues = filter_by_diff(local_issues, diff_lines)
            # diff_lines=None（git外）の場合はフィルタなしで全件出力

        if args.format == "json":
            print(format_json_v2(local_issues, fpath))
        elif args.format == "markdown":
            print(format_markdown(local_issues, fpath))
        else:
            if local_issues:
                print(f"── ローカル解析: {fpath} ──")
                print(format_text(local_issues))

        if any(i.severity == Severity.CRITICAL for i in local_issues):
            has_critical = True

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

    sys.exit(1 if has_critical else 0)


if __name__ == "__main__":
    main()
