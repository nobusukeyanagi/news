"""Yahoo!ニュースのトピックス一覧と、リンク先の取得可能な本文をHTML化する。"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

SOURCE = 'https://news.yahoo.co.jp/topics'
ORIGIN = 'https://news.yahoo.co.jp'
AGENT = 'PersonalNewsReader/1.0'
IMAGE_HOSTS = {'newsatcl-pctr.c.yimg.jp'}
ARTICLE = re.compile(r'^/articles/[a-f0-9]+/?$')
PICKUP = re.compile(r'^/pickup/\d+/?$')
JST = ZoneInfo('Asia/Tokyo')


def clean(text):
    return re.sub(r'[ \t\r\f\v]+', ' ', text).strip()


def article_url(url, base=SOURCE):
    p = urllib.parse.urlsplit(urllib.parse.urljoin(base, url))
    if p.scheme != 'https' or p.netloc != 'news.yahoo.co.jp' or not ARTICLE.fullmatch(p.path):
        return None
    page = urllib.parse.parse_qs(p.query).get('page', ['1'])[0]
    return ORIGIN + p.path.rstrip('/') + ('?page=' + page if page.isdigit() and int(page) > 1 else '')


def image_url(url, base):
    p = urllib.parse.urlsplit(urllib.parse.urljoin(base, url))
    if p.scheme == 'https' and p.hostname in IMAGE_HOSTS and p.port is None:
        return urllib.parse.urlunsplit(p)
    return None


class Client:
    def __init__(self):
        self.last = 0.0
        self.cache = {}
        self.robots = urllib.robotparser.RobotFileParser()
        # 判定できない場合も取得を続けず、既存の公開ページを維持する。
        robots = self._get(ORIGIN + '/robots.txt')
        self.robots.parse(robots.splitlines())

    def _get(self, url):
        for attempt in range(3):
            time.sleep(max(0, 1.0 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            request = urllib.request.Request(url, headers={
                'User-Agent': AGENT, 'Accept-Language': 'ja',
                'Accept': 'text/html,application/xml;q=0.9,*/*;q=0.8',
            })
            try:
                with urllib.request.urlopen(request, timeout=25) as response:
                    if urllib.parse.urlsplit(response.url).netloc != 'news.yahoo.co.jp':
                        raise RuntimeError('別サイトへの転送のため取得を中止')
                    return response.read(8_000_000).decode('utf-8', 'replace')
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 2:
                    raise
            time.sleep(2 ** (attempt + 1))
        raise RuntimeError('取得できませんでした')

    def get(self, url):
        p = urllib.parse.urlsplit(url)
        if p.scheme != 'https' or p.netloc != 'news.yahoo.co.jp':
            raise RuntimeError('取得対象外のURL')
        if not self.robots.can_fetch(AGENT, url):
            raise RuntimeError('robots.txtで取得が許可されていません')
        if url not in self.cache:
            self.cache[url] = self._get(url)
        return self.cache[url]

    def save_image(self, url, directory):
        """期限付き画像URLを公開時に保存して、更新まで表示可能にする。"""
        if not image_url(url, url):
            raise RuntimeError('許可されていない画像URL')
        # CDN側にもrobots.txtがある場合はそれに従う。
        origin = 'https://' + urllib.parse.urlsplit(url).hostname
        if origin not in self.cache:
            try:
                with urllib.request.urlopen(origin + '/robots.txt', timeout=15) as response:
                    rules = response.read(500_000).decode('utf-8', 'replace')
            except urllib.error.HTTPError as error:
                # 画像配信CDNはrobots.txtに400を返す場合がある。
                if error.code not in (400, 404):
                    raise
                rules = 'User-agent: *\nAllow: /\n'
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(rules.splitlines())
            self.cache[origin] = parser
        if not self.cache[origin].can_fetch(AGENT, url):
            raise RuntimeError('画像CDNのrobots.txtにより取得不可')
        request = urllib.request.Request(url, headers={'User-Agent': AGENT})
        time.sleep(max(0, 1.0 - (time.monotonic() - self.last)))
        self.last = time.monotonic()
        with urllib.request.urlopen(request, timeout=25) as response:
            if not image_url(response.url, response.url):
                raise RuntimeError('画像が別サイトへ転送されました')
            content_type = response.headers.get_content_type()
            extension = {'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif'}.get(content_type)
            if not extension:
                raise RuntimeError('対応外の画像形式です')
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise RuntimeError('画像サイズの上限を超過しました')
        directory.mkdir(parents=True, exist_ok=True)
        filename = hashlib.sha256(url.encode()).hexdigest()[:24] + extension
        (directory / filename).write_bytes(raw)
        return 'images/' + filename


def parse_topics(document):
    soup = BeautifulSoup(document, 'html.parser')
    # yjnSubのランキング・おすすめ記事を含めない。
    root = soup.select_one('#yjnMain') or soup.find('main') or soup
    items, seen = [], set()
    for a in root.select('a[href]'):
        url = urllib.parse.urljoin(SOURCE, a['href'])
        p = urllib.parse.urlsplit(url)
        if p.netloc != 'news.yahoo.co.jp' or not PICKUP.fullmatch(p.path) or url in seen:
            continue
        seen.add(url)
        title = clean(''.join(str(t) for t in a.find_all(string=True, recursive=False)))
        title = title or clean(a.get_text(' ', strip=True))
        category = 'ニュース'
        for parent in a.parents:
            category_link = parent.select_one('a[href^="/categories/"]')
            if category_link:
                category = category_link.get_text(strip=True)
                break
            if parent is root:
                break
        items.append({'title': title, 'topic_url': url, 'category': category})
    if not items:
        raise RuntimeError('トピックス一覧が取得できませんでした。サイト構造の変更・アクセス制限を確認してください。')
    return items


def linked_article(document, base):
    soup = BeautifulSoup(document, 'html.parser')
    anchors = soup.select('a[href]')
    # 関連ニュースより「記事全文を読む」を優先する。
    anchors.sort(key=lambda a: 0 if '記事全文を読む' in a.get_text() else 1)
    for a in anchors:
        url = article_url(a['href'], base)
        if url:
            return url
    raise RuntimeError('本文へのリンクが見つかりません')


def json_articles(value):
    if isinstance(value, dict):
        kind = value.get('@type', [])
        kind = [kind] if isinstance(kind, str) else kind
        if isinstance(kind, list) and any(k in ('NewsArticle', 'Article', 'ReportageNewsArticle') for k in kind):
            yield value
        for child in value.values():
            yield from json_articles(child)
    elif isinstance(value, list):
        for child in value:
            yield from json_articles(child)


def parse_article(document, url):
    soup = BeautifulSoup(document, 'html.parser')
    metadata = {}
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            candidates = list(json_articles(json.loads(script.string or script.get_text())))
            if candidates:
                metadata = candidates[0]
                break
        except (ValueError, TypeError):
            pass
    heading = soup.select_one('#uamods h1, article h1') or soup.find('h1')
    title = clean(str(metadata.get('headline') or (heading.get_text(' ', strip=True) if heading else '')))
    author = metadata.get('author') or {}
    if isinstance(author, list):
        author = author[0] if author else {}
    publisher = clean(str(author.get('name', '') if isinstance(author, dict) else author))
    published = metadata.get('datePublished', '')
    body = []
    images = []
    # ページ全体やdescriptionを「本文」として扱わない。
    containers = soup.select('.article_body') or soup.select('[itemprop="articleBody"]')
    for container in containers:
        for img in container.select('img'):
            candidate = image_url(img.get('src') or img.get('data-src') or '', url)
            if candidate and not any(pic['url'] == candidate for pic in images) and len(images) < 6:
                figure = img.find_parent('figure')
                caption_node = figure.find('figcaption') if figure else None
                anchor = img.find_parent('a')
                caption = clean(caption_node.get_text(' ', strip=True) if caption_node else (anchor.get_text(' ', strip=True) if anchor else img.get('alt', '')))
                images.append({'url': candidate, 'caption': caption[:300]})
        for unwanted in container.select('script, style, figure, figcaption, aside, nav, button, iframe'):
            unwanted.decompose()
        # 写真説明・関連記事・広告を除外。
        for unwanted in container.select('[class*="caption"], [class*="Caption"], [class*="related"], [class*="Related"], [class*="advert"]'):
            unwanted.decompose()
        blocks = container.select('p.yjSlinkDirectlink, p.highLightSearchTarget, h2, h3, h4')
        if not blocks:
            blocks = container.select('p, h2, h3, h4')
        if blocks:
            for block in blocks:
                text = clean(block.get_text('\n', strip=True))
                if text:
                    body.append(text)
        else:
            text = clean(container.get_text('\n', strip=True))
            if text:
                body.append(text)
    if not body and isinstance(metadata.get('articleBody'), str):
        body = [clean(p) for p in metadata['articleBody'].split('\n') if clean(p)]
    current = int(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get('page', ['1'])[0])
    pages = {}
    base_path = urllib.parse.urlsplit(url).path
    for a in soup.select('a[href]'):
        target = article_url(a['href'], url)
        if not target or urllib.parse.urlsplit(target).path != base_path:
            continue
        number = int(urllib.parse.parse_qs(urllib.parse.urlsplit(target).query).get('page', ['1'])[0])
        if number > current:
            pages[number] = target
    # 有料記事の無料部分を全文と誤表示しない。
    paywall = metadata.get('isAccessibleForFree') in (False, 'False', 'false')
    next_url = pages[min(pages)] if pages else None
    return {'article_title': title, 'publisher': publisher, 'published': published,
            'body': body, 'next_url': next_url, 'paywall': paywall, 'images': images}


def fetch_article(client, item, image_dir=None):
    result = dict(item, body=[], images=[], article_title='', publisher='', published='', note='')
    try:
        url = linked_article(client.get(item['topic_url']), item['topic_url'])
        result['url'] = url
        seen = set()
        while url and url not in seen and len(seen) < 10:
            seen.add(url)
            parsed = parse_article(client.get(url), url)
            if not parsed['body']:
                raise RuntimeError('本文が見つかりません（動画・有料記事・掲載終了・ページ構造変更など）')
            if len(seen) == 1:
                result.update({k: parsed[k] for k in ('article_title', 'publisher', 'published')})
            result['body'].extend(parsed['body'])
            if image_dir is not None:
                for image in parsed['images']:
                    if len(result['images']) >= 6:
                        break
                    try:
                        saved = client.save_image(image['url'], image_dir)
                        if not any(photo['src'] == saved for photo in result['images']):
                            result['images'].append({'src': saved, 'caption': image['caption']})
                    except (OSError, RuntimeError, urllib.error.URLError) as error:
                        print(f'::warning::画像取得失敗 {type(error).__name__}: {error}', file=sys.stderr)
            url = parsed['next_url']
            if parsed['paywall']:
                result['note'] = '公開されている部分のみ表示しています。続きは元記事をご確認ください。'
                break
        if url and not result['note']:
            result['note'] = '続きがある可能性があります。元記事をご確認ください。'
    except Exception as error:
        result['note'] = ('本文の一部のみ取得できました。' if result['body'] else '本文を取得できませんでした。') + '元記事をご確認ください。'
        # 記事本文はログに出さない。
        print(f'::warning::{item["topic_url"]}: {type(error).__name__}: {error}', file=sys.stderr)
    return result


def render(items, updated):
    esc = html.escape
    contents, sections = [], []
    count = sum(bool(i['body']) for i in items)
    for n, item in enumerate(items, 1):
        title = item['title']
        contents.append(f'<li><a href="#news-{n}">{esc(title)}</a></li>')
        meta = ' / '.join(filter(None, [item['category'], item.get('publisher', ''), item.get('published', '')]))
        paragraphs = ''.join('<p>' + esc(p).replace('\n', '<br>') + '</p>' for p in item['body'])
        photos = ''.join(f'<figure><img src="{esc(photo["src"], quote=True)}" alt="記事に掲載された写真" loading="lazy" decoding="async">' + (f'<figcaption>{esc(photo["caption"])}</figcaption>' if photo['caption'] else '') + '</figure>' for photo in item.get('images', []))
        note = f'<p class="notice">{esc(item["note"])}</p>' if item.get('note') else ''
        full_title = item.get('article_title', '')
        subtitle = f'<p class="full-title">{esc(full_title)}</p>' if full_title and full_title != title else ''
        url = item.get('url') or item['topic_url']
        sections.append(f'<article id="news-{n}"><h2>{esc(title)}</h2><p class="meta">{esc(meta)}</p>{subtitle}{photos}{paragraphs}{note}<p class="links"><a href="{esc(url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">元記事を読む</a> · <a href="#top">目次へ</a></p></article>')
    status = f'更新：{updated.astimezone(JST).strftime("%Y/%m/%d %H:%M")}（日本時間） · {len(items)}件 / 本文取得 {count}件'
    if not items:
        status = 'まだニュースを取得していません。GitHub Actionsの「Update news」を実行してください。'
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">
<meta name="referrer" content="no-referrer">
<title>ニュース一覧</title>
<style>
*{{box-sizing:border-box}}html{{scroll-behavior:auto}}body{{margin:0;background:#fff;color:#202020;font-family:system-ui,-apple-system,"Noto Sans JP",sans-serif;font-size:16px;line-height:1.9;overflow-wrap:anywhere}}figure{{margin:0 0 18px}}figure img{{display:block;width:auto;max-width:100%;height:auto;max-height:540px;object-fit:contain}}figcaption{{font-size:.8125rem;color:#555;line-height:1.55;margin-top:5px}}
main{{max-width:1440px;margin:0 auto;padding:28px 24px 56px}}h1{{font-size:1.6rem;margin:0 0 8px}}h2{{font-size:1.3rem;line-height:1.55;margin:0 0 8px}}a{{color:#174c86;text-underline-offset:3px}}a:focus-visible{{outline:2px solid #174c86;outline-offset:4px}}.meta,.schedule,.links{{font-size:.875rem;color:#555}}.meta{{margin:4px 0 14px}}.schedule{{margin:0 0 20px}}.layout{{display:grid;grid-template-columns:minmax(230px,320px) minmax(0,1fr);gap:36px;align-items:start}}nav{{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;border-top:1px solid #ccc;padding-top:18px}}nav h2{{font-size:1rem}}ol{{padding-left:1.6em;margin:0 0 28px}}li{{padding:3px 0}}article{{border-top:1px solid #bbb;padding:28px 0;scroll-margin-top:16px}}article p{{margin:0 0 18px}}.full-title{{font-weight:600}}.notice{{padding:10px 14px;border-left:3px solid #999;background:#f5f5f5}}footer{{border-top:1px solid #ccc;padding-top:20px;font-size:.875rem;color:#555}}@media(max-width:700px){{main{{padding:20px 16px 36px}}.layout{{display:block}}nav{{position:static;max-height:none;overflow:visible}}h2{{font-size:1.2rem}}}}@media print{{nav,.links,.schedule{{display:none}}main{{max-width:none;padding:0}}.layout{{display:block}}article{{break-inside:auto}}}}
</style></head><body><main id="top"><header><h1>ニュース一覧</h1><p class="meta">{esc(status)}</p><p class="schedule">毎日6:00・18:00に更新予定（日本時間）</p></header>
<div class="layout"><nav aria-label="タイトル一覧"><h2>タイトル一覧</h2><ol>{''.join(contents)}</ol></nav><div class="feed">
{''.join(sections)}
<footer>取得元：<a href="{SOURCE}" target="_blank" rel="noopener noreferrer nofollow">Yahoo!ニュース トピックス一覧</a><br>本文は取得時点の内容です。訂正・更新・続きは元記事をご確認ください。</footer>
</div></div></main></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='site')
    parser.add_argument('--limit', type=int, default=0, help='取得テスト用。0は一覧全件')
    parser.add_argument('--empty', action='store_true', help='通信せず初回案内ページを生成')
    args = parser.parse_args()
    now = datetime.now(JST)
    output = Path(args.output)
    items = []
    if not args.empty:
        client = Client()
        topics = parse_topics(client.get(SOURCE))
        if args.limit > 0:
            topics = topics[:args.limit]
        print(f'一覧 {len(topics)}件', flush=True)
        for n, item in enumerate(topics, 1):
            items.append(fetch_article(client, item, output / 'images'))
            print(f'{n}/{len(topics)} 本文取得={bool(items[-1]["body"])}', flush=True)
        if not any(i['body'] for i in items):
            raise RuntimeError('本文を1件も取得できなかったため公開を中止します。既存ページは維持されます。')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'index.html').write_text(render(items, now), encoding='utf-8')
    # noindexを読めるように、robots.txtによるクロール拒否は行わない。
    (output / 'robots.txt').write_text('User-agent: *\nDisallow:\n', encoding='utf-8')
    (output / '.nojekyll').touch()
    print(f'生成完了: {output / "index.html"}', flush=True)


if __name__ == '__main__':
    main()
