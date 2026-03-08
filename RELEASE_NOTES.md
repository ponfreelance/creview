# CReview v0.13.0

C言語設計レビュー専用CLI。指摘のみ。コード修正機能なし。

## ダウンロード

| OS | ファイル | 展開方法 |
|---|---|---|
| Windows | `creview_win.zip` | ZIP展開 |
| Mac | `creview_mac.tar.gz` | `tar xzf creview_mac.tar.gz` |
| Linux | `creview_linux.tar.gz` | `tar xzf creview_linux.tar.gz` |

### Mac初回起動時
```
chmod +x creview
xattr -d com.apple.quarantine creview
```

## セキュリティ情報

- **ローカル実行のみ**
- **外部にコードを保存しません**
- **自己更新しません**
- **通信先は Claude API (`api.anthropic.com`) のみです**
- **テレメトリ・ログ収集はありません**

SHA256ハッシュ値は `sha256.txt` に記載。
検証方法: `certutil -hashfile creview.exe SHA256`

## セットアップ（3ステップ）

1. ZIPを展開
2. `config.txt.sample` を `config.txt` にリネーム
3. `config.txt` にClaude APIキーを入力

## 使い方

- コードレビュー: `start.bat` をダブルクリック
- 仕様レビュー: `start_spec.bat` をダブルクリック
- コマンドライン: `creview main.c` / `creview --spec spec.txt`

## 出力

- 【重大】クラッシュ可能性
- 【設計不明】仕様曖昧
- 【保守危険】将来バグリスク

## 同梱ファイル

| ファイル | 説明 |
|---|---|
| creview.exe | 本体 |
| config.txt.sample | 設定テンプレート |
| manual.txt | マニュアル |
| start.bat | コードレビュー起動 |
| start_spec.bat | 仕様レビュー起動 |
| sha256.txt | ハッシュ値 |

---

## v0.4.0 → v0.13.0 更新内容

### v0.13.0 — バッファ使用率解析
- `--buf-usage`: バッファ宣言サイズに対する書き込み使用率レポート
  - snprintf/strncpy/memcpy/read/recv/fgets/fread等のサイズ制限を追跡
  - sprintf/strcpy/strcat/gets等のサイズ制限なし操作を「不定(危険)」と報告
  - リテラルコピーのサイズ自動判定 (`strcpy(buf, "hello")` → 6B)
  - sizeof式・演算式のサイズ解決

### v0.12.0 — スタックサイズ解析
- `--stack`: 関数別スタック使用量推定レポート
- `--stack-threshold`: 閾値変更（デフォルト8KB）
- alloca/VLA/再帰の検出

### v0.11.0 — 類似パターン水平展開
- `--similar`: 検出したバグと同じパターンを全ファイルから横断検索
- 1つの指摘から修正漏れを一括洗い出し

### v0.10.0 — 修正案表示
- `--fix`: 各指摘に「最小修正」と「推奨修正」の2パターンのコード例を表示

### v0.9.0 — プリセット＆自然言語指示
- `--preset`: memory/security/concurrency/style/pr/strict の6種プリセット
- `--ask`: Claude APIに自然言語で指示 (例: `--ask "メモリリークを重点的に"`)

### v0.8.0 — 修正ヒント＆ベースライン差分
- `--fix-hint`: 各指摘に1行修正ヒント付与
- `--baseline`: 前回結果との差分で新規指摘のみ表示
- `--exit-code`: 終了コード閾値変更
- `--list-rules`: 全ルール名一覧
- 新検出パターン: signed/unsigned混合比較、NULLチェック分岐後ポインタ使用、無限ループ、enum switch defaultなし、暗黙切り詰めキャスト、ビットフィールド符号未指定

### v0.7.0 — SARIF出力＆フィルタ
- `--format sarif`: SARIF 2.1.0出力（GitHub Code Scanning連携）
- `--severity`: 重大度フィルタ
- `--count`: 集計モード
- `RULE_OFF`: 個別ルール無効化
- 新検出パターン: use-after-free、未初期化変数、整数オーバーフロー、open/socket/pipeリソースリーク、固定バッファオーバーラン、pthread mutex不整合

### v0.6.0 — 除外・差分モード
- `--diff`: git差分の変更行のみレビュー
- `--format markdown`: Markdown出力
- `EXCLUDE`: ファイル除外パターン
- `NOCHECK`: 行単位の警告抑制
- 新検出パターン: fopen/fcloseリーク、フォーマット文字列脆弱性、sizeof(ポインタ)、switch fall-through、マクロ引数括弧未保護、return値混在、snprintf戻り値無視

### v0.5.0 — volatile＆packed検出
- volatile変数の非アトミック操作検出
- packed構造体のアラインメント問題検出
- `VOLATILE_OK` / `PACKED_OK` 抑制フラグ

### 数値の変化（v0.4.0 → v0.13.0）

| 項目 | v0.4.0 | v0.13.0 |
|---|---|---|
| ローカル検出パターン | 4 | 33 |
| 出力形式 | text/json | text/json/markdown/sarif |
| テスト件数 | - | 113 |
| CLIオプション | 3 | 20+ |
