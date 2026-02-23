# CReview v0.4.0

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
