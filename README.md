# omu-kosen-route2

大阪モノレール沿線と寝屋川市方面を対象にした経路検索アプリです。
Python/Flask の画面から、pybind11 でビルドした C++ 検索コアを呼び出します。

## セットアップ

初回、またはキャッシュを作り直したいときは次を実行します。

```bash
python3 scripts/setup_project.py
```

このコマンドで以下をまとめて行います。

- `.venv` がなければ作成し、その Python で再実行
- `requirements.txt` の依存関係をインストール
- `src/route_core.cpp` をビルド
- `data/cache` に平日・土日用の時刻表JSONを生成
- 代表駅で簡易経路検索チェックを実行

## 起動

```bash
.venv/bin/python src/app.py
```

既定では `0.0.0.0:10071` で待ち受けます。同じネットワーク内の端末からは、このPCに割り当てられたIPアドレスとポート番号でアクセスします。

```bash
FLASK_RUN_HOST=127.0.0.1 FLASK_RUN_PORT=5001 .venv/bin/python src/app.py
```

デバッグ表示を有効にする場合だけ `ROUTE_DEBUG=1` または `FLASK_DEBUG=1` を指定します。

## 個別キャッシュ生成

```bash
.venv/bin/python src/generate_timetable_cache.py --service-day both
```

モノレールの門真市方面キャッシュだけを作る場合:

```bash
.venv/bin/python src/generate_timetable_cache.py --only mono-to-kadoma --service-day both
```

時刻表キャッシュは外部サイトから生成します。取得結果が空、必須項目が不足、乗換列車が見つからない場合は、壊れたJSONを保存せずエラーで停止します。

## テスト

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest
```
