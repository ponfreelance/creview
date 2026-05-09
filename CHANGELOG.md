# CHANGELOG

## 0.17.0 (2026-05-09) — Zenn コメント反映

公開 Zenn 記事「C言語の危険コードを静的解析ツールcreviewで検出する」へのコメント 5 件を反映した本体改修。

### 修正

- **`check_sizeof_pointer`** (CWE-467) を関数スコープ単位で判定するよう書き直し:
  - ローカル/ファイルスコープの**配列宣言** (`char buf[100]`) は flag しない
  - 関数パラメータの配列形 (`void f(char buf[100])`) は decay として flag
  - 関数パラメータのポインタ形 (`void f(char *buf)`) も decay として flag
  - 構造体メンバ配列 / typedef 配列 / sizeof(struct メンバ) は flag しない
  - 判定不能なら flag しない (high-precision over recall)
  - 同名変数が異なる関数で配列/ポインタとして混在しても誤検出しない (file-wide ptr_vars 共有を廃止)
- `check_magic_numbers` を**統一ルール**に書き換え:
  - 整数リテラル N が allowlist `{-1, 0, 1, 2}` 以外なら**全箇所で flag**
  - context (`buffer_size` / `loop_bound` / `bitmask` / `general`) で severity 切替
    - `malloc/calloc/realloc/memcpy/memset/snprintf` 等のサイズ引数 / 配列宣言サイズ → 重大
    - `for (i < N)` のループ上限 → 保守危険
    - bitmask / shift → 保守危険
    - 一般リテラル → 保守危険
  - sizeof 内の数値は除外
  - 配列宣言サイズ・関数引数リテラルも対象に追加 (従来の "malloc 引数だけ拾う癖" を是正)

### 追加

- **ATTR-NONNULL-001** ルール: ポインタ引数を NULL 検査せず unconditional dereference (`*p` / `p->` / `printf("%s", p)` / `strlen(p)` / `strcpy`) しているのに `__attribute__((nonnull))` 宣言がない関数を flag。
- **ATTR-WUR-001** ルール: int / status_t / `_t` 系を返し関数名 prefix が `validate_/check_/init_/set_/get_` のいずれかなのに `__attribute__((warn_unused_result))` がない関数を flag (CWE-252 / MISRA C 2012 Dir 4.7)。
- **NULL-CALLSITE-001** ルール: nonnull 宣言済み関数に `NULL` リテラルを直接渡す callsite を flag (CWE-476)。
- `--check-prerequisites <project_dir>` サブコマンド: Makefile / CMakeLists.txt / *.cmake / build.gradle をスキャンし `-Wall` `-Wextra` `-Werror` `-fanalyzer` `-Wpedantic` の有無を確認して終了。
- デフォルト実行時にも軽量チェックを走らせ、必須フラグ不足のときに L0 ヒントを stderr に表示。`--skip-prerequisites` で抑制可能。
- `.creviewrc.json` 設定ファイルサポート (`magic_number.allowlist`)。同経路で `.creviewignore` の隣に置く。
- `--magic-allowlist 8,16,32,64,0xFF` CLI フラグ (config より優先)。

### テスト

- `tests/fixtures/sizeof_cases.c` (7 ケース): CASE 1/2/6/7 が flag されない / CASE 3/4/5 が flag されることを pin。
- `tests/fixtures/attribute_contracts.c`: 3 ルールの違反ケースと nonnull/warn_unused_result 付きの安全ケースを pin。
- `tests/fixtures/magic_number_cases.c` (6 ケース): context ごとの severity を pin。
- `tests/fixtures/static_init_no_cwe457.c`: 静的記憶域変数を CWE-457 系 message に紐づけないことを pin (Zenn コメントの根本指摘 — `static int g_initialized;` は ISO C で 0 自動初期化される)。

### ドキュメント

- README に 4 層モデル (L0: コンパイラ警告 / L1: clang-tidy/cppcheck / L2: creview / L3: CSAF) での位置づけを明記。
- 「creview は L0/L1 の代替ではなく、その後段で取れない高次パターン (プロジェクト固有 / 日本語ナレッジ / severity / CWE/MISRA / `--preset pr` / AI 補強) を担う」を冒頭に置いた。

### 移行ガイド (0.14.x → 0.17.0)

- 既存ユーザは特に変更不要。マジックナンバー検出が**従来より厳しくなる** (allowlist `{-1,0,1,2}` 以外を全 flag)。ノイズが多い場合は `.creviewrc.json` に `magic_number.allowlist` を書くか、`MAGIC_OK` を `.creviewignore` に追加。
- 新ルール `attr_nonnull` / `attr_wur` / `null_callsite` が追加された。組み込み以外で attribute を使わないコードベースでは noisy になり得るので、`.creviewignore` の `RULE_OFF attr_nonnull` 等で個別無効化可能。
