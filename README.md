# ニュース一覧

Yahoo!ニュースの https://news.yahoo.co.jp/topics に表示されている各カテゴリのニュースを、タイトルと取得可能な本文をまとめた1ページで読むための個人用ツールです。

## GitHubへの設置

1. GitHubで新しいリポジトリを作成します。リポジトリ名の例：`news-reader`。
2. このZIPを解凍し、**中身をリポジトリ直下**にアップロードします。`.github/workflows/news.yml` を忘れないでください。標準ブランチ名は `main` を想定しています。
3. リポジトリの **Settings → Pages → Build and deployment → Source** を **GitHub Actions** に変更します。
4. **Actions → Update news → Run workflow → Run workflow** を実行します。
5. 完了後、**Settings → Pages** に表示されるURLを開きます。

公開URLの例：`https://ユーザー名.github.io/news-reader/`

初回アップロードの自動実行がPages設定前に失敗しても、設定後に手動実行すれば再実行できます。ニュース取得には外部APIキーは不要です。プライベートリポジトリでPagesを使えるかはGitHubの契約に依存します。リポジトリを非公開にしても、通常のPages公開URLには閲覧認証は付きません。

## Discordへの更新通知

1. 通知したいDiscordチャンネルの設定からWebhookを作成し、URLをコピーします。
2. GitHubリポジトリの **Settings → Secrets and variables → Actions → New repository secret** で、名前を `DISCORD_WEBHOOK_URL`、値をWebhook URLにして保存します。Webhook URLをコードや公開ページに書かないでください。
3. 次回のニュース取得とPages公開が成功すると、そのチャンネルに公開ページのリンクを通知します。Secret未設定なら通知を省略します。

定時更新・手動更新、および保存済み記事がなく再取得した更新が対象です。デザイン変更など、保存済み記事からページだけを再生成したときは通知しません。通知はPagesへの公開が完了した後に送ります。
Discord通知が失敗した場合もPagesへの公開は完了しています。Actionsの「Notify Discord after news update」にHTTP状態コードとDiscordのエラー番号を表示します。Webhook URL自体はログに出しません。URL末尾にGitHub連携用の `/github` が付いていても、通常のメッセージ投稿用に変換します。

## 更新

- 日本時間の毎日 **3:00・15:00** にGitHub Actionsを起動します。
- cronはUTCの `0 6,18 * * *` です。
- 取得とデプロイに数分程度かかります。Actionsの混雑により、起動自体も遅延・スキップされる場合があります。
- 手動更新は **Actions → Update news → Run workflow** から行えます。
- `main` へのアップロードによる更新では、前回保存した記事と写真からHTMLを作り直します。デザインのみを変更したときはニュースを再取得しません。
- 保存済みデータが64件に満たない場合は、デザイン変更時でもニュースを取得し直します。
- 保存済みデータがない場合、キャッシュが失われた場合、本文抽出方法が変わった場合は全件取得します。定時・手動実行でも毎回ニュースを取得し直します。
- 公開リポジトリでは、リポジトリに60日間活動がないと定期実行が自動停止されることがあります。停止時はActions画面で再度有効にしてください。
- ページは開いた時点の最新の公開内容を表示します。開きっぱなしのページは再読み込みしてください。

## 表示する内容

- `/topics` のメイン一覧にあるニュースを掲載順に取得します（作成時点では8カテゴリ・計64件）。
- 通常更新では8カテゴリに各8件あることを確認します。一時的に欠けた一覧は最大3回取得し直し、それでも64件が揃わなければ新しいページを公開せず前回のページを維持します。本文が取得できない記事もタイトルと元記事リンクは掲載します。
- PCでは左にタイトル一覧、右にニュースを表示します。スマホでは縦に並びます。
- トピックのタイトル、元記事のタイトル、カテゴリ、配信元、元記事の配信日時、写真、写真のキャプション、本文、元記事リンクを表示します。
- 写真は1記事につき最大6枚を公開時に保存し、写真の下にキャプションを小さく表示します。取得できない写真は省略します。
- 本文はすべて最初から展開した状態です。動画・広告・コメントは掲載しません。
- 同じ記事の次ページへのリンクがある場合は、最大10ページまで順に取得します。
- 本文取得に失敗した記事は、元記事リンクと取得失敗の案内を表示します。
- 有料部分やアクセス制限は回避しません。無料部分だけ取得できる記事や、本文を取得できない動画記事などがあります。
- 一覧取得に失敗、または本文が全件取得できなかった場合は公開処理を停止し、前回公開したページを維持します。初回であれば公開されません。
- 本文の一部だけ取得できた場合は、その旨を表示します。ただし、配信元の仕様による省略をすべて自動判定できるわけではありません。
- 更新日時は取得処理を始めた時刻です。最新の訂正・続きは元記事で確認してください。

## noindexとデータ保存

HTMLのheadに以下を設定しています。

```html
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">
```

検索エンジンがnoindexを読めるように、robots.txtではクロールを禁止していません。プロジェクト単位のGitHub Pagesに置いたrobots.txtはドメイン全体の設定にはなりませんが、各HTMLのnoindexは個別に有効です。

**noindexは閲覧制限ではありません。URLを知る人は閲覧でき、検索エンジン以外の収集も防ぎません。** 個人閲覧用途を想定していますが、noindexにより記事の転載許諾が得られるわけではありません。配信元の条件を確認して利用してください。

記事本文と写真はGitにコミットしません。Actionsのキャッシュに前回分を保持し、そこからPagesへ公開します。公開内容は最新回に置き換え、アーカイブ一覧は作りません。キャッシュはGitHubの保存期限や容量制限で消えることがあります。Actionsの配信用アーティファクトの保存期間は1日です。公開ページ自体は次回更新まで残ります。

## ファイル構成

```text
.github/workflows/news.yml  定期取得とPagesへの公開
scripts/build.py           一覧取得・本文抽出・HTML生成
tests/test_build.py         抽出と安全なHTML出力の確認
favicon.ico                ブラウザのアイコン
apple-touch-icon.png       ホーム画面のアイコン
icon-master.png            アイコンの元画像
requirements.txt           Python依存パッケージ
index.html                 未取得状態の表示見本
robots.txt                 noindexの読み取りを妨げない設定
.gitignore                 記事出力などをGit保存から除外
README.md                  この説明
```

## ローカルで確認する場合

Python 3.12以降で実行します。

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests
python scripts/build.py --limit 3
python scripts/build.py --from-snapshot site/snapshot.json --limit 3
```

生成された `site/index.html` をブラウザで開きます。全件取得する場合は `--limit 3` を外してください。`--from-snapshot` は保存した記事と写真からページだけを再生成します。通信せず初回案内だけを生成する場合は `--empty` を指定します。

## 取得できなくなった場合

Actionsの `Fetch news and build page` のログで確認できます。robots.txtで禁止された場合や取得元がアクセスを拒否した場合は、その制限を回避せず停止します。Yahoo!ニュースのHTML構造が変わった場合は `parse_topics` / `parse_article` の調整が必要です。エラーの案内には元記事URLとエラー種別だけを含め、本文そのものはログ出力しません。

ActionsにはNode.js 24に対応したバージョンを使用しています。
