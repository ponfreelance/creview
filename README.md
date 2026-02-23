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
ローカル静的解析（12パターン）＋ Claude API深層レビューの二段構成。

`--local-only` ならAPIキー不要。ローカル解析のみで動作します。

### ② 仕様レビュー（APIキー必須）

仕様テキストを読み込み、そのまま実装した場合に事故になる曖昧点を検出。  
状態遷移不整合・エラー処理不足・API境界曖昧・再試行条件未定義などをAIが指摘。

> これは「仕様どおりか確認するツール」ではありません。  
> **仕様そのものの危険を見つける**ツールです。

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
VOLATILE_OK     # volatile関連を抑制（将来用）
PACKED_OK       # packed struct関連を抑制（将来用）
```

## 終了コード

| コード | 意味 |
|---|---|
| 0 | 重大指摘なし |
| 1 | 重大指摘あり |

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

## ライセンス

MIT
