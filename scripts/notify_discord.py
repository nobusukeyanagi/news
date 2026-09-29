"""Pages公開後にDiscordへ通知する。Webhook URLはログに出さない。"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def webhook_url(value):
    url = urllib.parse.urlsplit(value.strip())
    path = url.path.rstrip('/')
    # DiscordのGitHub連携用末尾は通常のメッセージ投稿には使えない。
    if path.endswith('/github'):
        path = path[:-7]
    if (url.scheme != 'https' or url.hostname not in ('discord.com', 'discordapp.com') or
            url.port is not None or not re.fullmatch(r'/api(?:/v\d+)?/webhooks/\d+/[^/]+', path)):
        raise ValueError('Discord Webhook URLの形式が正しくありません')
    return urllib.parse.urlunsplit(('https', url.netloc, path, url.query, ''))


def send(url, page_url):
    if not page_url:
        raise ValueError('公開ページのURLが取得できません')
    payload = {'content': '📰 ニュースを更新しました\n' + page_url,
               'allowed_mentions': {'parse': []}}
    request = urllib.request.Request(webhook_url(url), data=json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json'}, method='POST')
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20):
                return
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            try:
                discord_code = json.loads(error.read(4096)).get('code')
            except (ValueError, AttributeError):
                discord_code = None
            detail = f' / Discord code {discord_code}' if isinstance(discord_code, int) else ''
            raise RuntimeError(f'Discord通知失敗: HTTP {error.code}{detail}') from None
        except urllib.error.URLError:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError('Discord通知失敗: 通信できませんでした') from None


def main():
    url = os.environ.get('DISCORD_WEBHOOK_URL', '')
    if not url:
        print('::warning::DISCORD_WEBHOOK_URL が未設定のため通知を省略しました')
        return
    try:
        send(url, os.environ.get('NEWS_URL', ''))
    except (ValueError, RuntimeError) as error:
        sys.exit(str(error))
    print('Discordへ更新通知を送信しました')


if __name__ == '__main__':
    main()
