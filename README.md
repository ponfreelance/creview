# CReview - C言語設計レビューAI

> Cのバグはスキル不足ではなく、レビューの見逃しで起きる。

C言語のコードや仕様書から**事故の原因になる箇所だけを検出する**レビュー専用CLIツール。

コード生成しません。修正提案しません。最適化しません。
やることは1つだけ。**危険箇所の指摘。**

## 検出する内容

| 分類 | 内容 |
|---|---|
| **【重大】** | クラッシュ・未定義動作・メモリ破壊の可能性 |
| **【設計不明】** | 仕様が曖昧で将来事故になる可能性 |
| **【保守危険】** | 将来バグを誘発しやすい設計依存 |

余計な解説は出ません。

## ダウンロード

[Releases](../../releases) から対応OSのアーカイブをダウンロード。

| OS | ファイル |
|---|---|
| Windows | `creview_win.zip` |
| Mac | `creview_mac.tar.gz` |
| Linux | `creview_linux.tar.gz` |

## セットアップ（3ステップ）

1. アーカイブを展開
2. `config.txt.sample` を `config.txt` にリネーム
3. `config.txt` に Claude APIキーを入力

## 使い方

```
creview main.c              # コードレビュー (.c / .h 対応)
creview src/                # フォルダ単位
creview --spec spec.txt     # 仕様レビュー
creview --local-only main.c # API不使用（ローカル解析のみ）
creview --format json src/  # JSON出力
```

Windowsは `start.bat` / `start_spec.bat` をダブルクリックでも起動できます。

### Mac初回起動時

```
chmod +x creview
xattr -d com.apple.quarantine creview
```

## 2つのモード

### ① コードレビュー

`.c` / `.h` ファイルを解析し、実装上の危険箇所を検出。
ローカル静的解析（36パターン）＋ Claude API深層レビューの二段構成。

`--local-only` ならAPIキー不要。ローカル解析のみで動作します。

### ② 仕様レビュー（APIキー必須）

仕様テキストを読み込み、そのまま実装した場合に事故になる曖昧点を検出。
状態遷移不整合・エラー処理不足・API境界曖昧・再試行条件未定義などをAIが指摘。

> これは「仕様どおりか確認するツール」ではありません。
> **仕様そのものの危険を見つける**ツールです。

## 主要機能

| コマンド | 機能 |
|---|---|
| `--fix` | 各指摘に修正コード例を2パターン（最小/推奨）表示 |
| `--fix-hint` | 各指摘に1行修正ヒント付与 |
| `--similar` | 同パターンの危険箇所を全ファイルから横断検索 |
| `--stack` | 関数別スタック使用量推定レポート |
| `--buf-usage` | バッファ宣言サイズ対書き込み使用率レポート |
| `--preset <名前>` | 目的別プリセット (memory/security/concurrency/style/pr/strict) |
| `--ask "指示"` | 自然言語でレビュー指示 |
| `--diff` | git差分の変更行のみレビュー |
| `--baseline <json>` | 前回結果との差分で新規指摘のみ表示 |
| `--format sarif` | GitHub Code Scanning連携用SARIF出力 |
| `--severity <level>` | 重大度フィルタ (critical/design/maint) |
| `--count` | ファイル別件数集計 |

## セキュリティ

- ローカル実行のみ
- 外部にコードを保存しません
- 自己更新しません
- テレメトリ・ログ収集なし
- 通信先は `api.anthropic.com` のみ

## 警告抑制

プロジェクトルートに `.creviewignore` を配置：

```
GLOBAL_OK       # グローバル変数警告を抑制
MACRO_ALLOW     # マクロ警告を抑制
MAGIC_OK        # マジックナンバー警告を抑制
VOLATILE_OK     # volatile関連を抑制
PACKED_OK       # packed struct関連を抑制
EXCLUDE test_*  # ファイル除外
RULE_OFF <rule> # 個別ルール無効化
```

## 推奨ワークフロー

```
1. 実装前    creview --spec spec.txt
2. 実装後    creview --fix-hint src/
3. メモリ    creview --preset memory src/
4. PR前      creview --preset pr src/
5. CI        creview --format sarif --local-only src/ > results.sarif
6. CI差分    creview --baseline prev.json --local-only src/
7. 自由指示  creview --ask "セキュリティを重点的に" src/
```

## 終了コード

| コード | 意味 |
|---|---|
| 0 | 重大指摘なし |
| 1 | 重大指摘あり |

`--exit-code design` で閾値変更可能。

## ビルド

詳細は [BUILD.txt](BUILD.txt) を参照。

```bash
pip install pyinstaller

# Windows
build.bat

# Mac / Linux
chmod +x build.sh
./build.sh
```

GitHub Actionsによる3OS同時ビルドにも対応（`.github/workflows/release.yml`）。

## カスタム対応

社内コーディング規約に合わせたレビュールールの追加・調整を承ります。

- 自社コーディング規約に基づく検出ルール追加
- MISRA-C / CERT-C 等の規格対応
- 既存CI/CDパイプラインへの組み込み支援
- レビュープロンプトの調整

お問い合わせ: [X (@pon_freelance)](https://x.com/pon_freelance) / nqg14616@nifty.com

## ライセンス

MIT
