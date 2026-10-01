"""Yahoo!ニュースのトピックス一覧と、リンク先の取得可能な本文をHTML化する。"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

SOURCE = 'https://news.yahoo.co.jp/topics'
ORIGIN = 'https://news.yahoo.co.jp'
AGENT = 'PersonalNewsReader/1.0'
TOPIC_CATEGORIES = ('国内', '国際', '経済', 'エンタメ', 'スポーツ', 'IT', '科学', '地域')
TOPICS_PER_CATEGORY = 8
# 本文抽出方法が変わったとき、旧データをそのまま再描画しない。
SNAPSHOT_VERSION = 5
IMAGE_HOSTS = {'newsatcl-pctr.c.yimg.jp'}
ARTICLE = re.compile(r'^/(?:expert/)?articles/[a-f0-9]+/?$')
PICKUP = re.compile(r'^/pickup/\d+/?$')
JST = ZoneInfo('Asia/Tokyo')
FULLWIDTH_ALNUM = {code: code - 0xFEE0
                   for start, end in ((0xFF10, 0xFF19), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A))
                   for code in range(start, end + 1)}


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


def image_caption(img):
    """写真に隣接するキャプションを探し、記事本文からは独立させる。"""
    figure = img.find_parent('figure')
    if figure:
        node = figure.find('figcaption')
        if node:
            return clean(node.get_text(' ', strip=True))[:300]
    anchor = img.find_parent('a')
    if anchor:
        caption = clean(anchor.get_text(' ', strip=True))
        if caption:
            return caption[:300]
    # 画像とキャプションが同じラッパー内の兄弟要素になっている記事。
    ancestor = img.parent
    for _ in range(4):
        if not ancestor or ancestor.name in ('article', 'section', 'body'):
            break
        caption_node = ancestor.find(
            lambda node: node.name in ('p', 'div', 'span', 'figcaption') and
            any('caption' in value.lower() for value in node.get('class', [])) and
            not node.find('img'))
        if caption_node:
            caption = clean(caption_node.get_text(' ', strip=True))
            if caption:
                return caption[:300]
        ancestor = ancestor.parent
    return clean(img.get('alt', ''))[:300]


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

    def get(self, url, refresh=False):
        p = urllib.parse.urlsplit(url)
        if p.scheme != 'https' or p.netloc != 'news.yahoo.co.jp':
            raise RuntimeError('取得対象外のURL')
        if not self.robots.can_fetch(AGENT, url):
            raise RuntimeError('robots.txtで取得が許可されていません')
        if refresh or url not in self.cache:
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
    # トピックス本体に限定し、ランキングやおすすめ記事を混ぜない。
    root = soup.select_one('#uamods-topics') or soup.select_one('#yjnMain') or soup.find('main') or soup
    items, seen = [], set()
    for a in root.select('a[href]'):
        url = urllib.parse.urljoin(SOURCE, a['href'])
        p = urllib.parse.urlsplit(url)
        if p.netloc != 'news.yahoo.co.jp' or not PICKUP.fullmatch(p.path) or url in seen:
            continue
        title = clean(''.join(str(t) for t in a.find_all(string=True, recursive=False)))
        title = title or clean(a.get_text(' ', strip=True))
        if not title:
            continue
        category = 'ニュース'
        for parent in a.parents:
            category_link = parent.select_one('a[href^="/categories/"]')
            if category_link:
                category = category_link.get_text(strip=True)
                break
            if parent is root:
                break
        seen.add(url)
        items.append({'title': title, 'topic_url': url, 'category': category})
    if not items:
        raise RuntimeError('トピックス一覧が取得できませんでした。サイト構造の変更・アクセス制限を確認してください。')
    return items


def topic_counts(items):
    return Counter(item['category'] for item in items)


def full_topic_list(items):
    counts = topic_counts(items)
    return len(items) == len(TOPIC_CATEGORIES) * TOPICS_PER_CATEGORY and all(
        counts[category] == TOPICS_PER_CATEGORY for category in TOPIC_CATEGORIES)


def fetch_topics(client, require_full=True, attempts=3):
    """一時的な欠落は再取得し、64件が揃わなければ公開を止める。"""
    for attempt in range(attempts):
        document = client.get(SOURCE, refresh=attempt > 0)
        try:
            items = parse_topics(document)
        except RuntimeError:
            items = []
        if items and (not require_full or full_topic_list(items)):
            return items
        counts = topic_counts(items)
        summary = ', '.join(f'{category}:{counts[category]}' for category in TOPIC_CATEGORIES)
        print(f'::warning::一覧が不完全です ({len(items)}件、{summary})。再取得します', file=sys.stderr)
        if attempt + 1 < attempts:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f'8カテゴリ×8件が揃わないため公開を中止します（取得 {len(items)}件）。')


def linked_article(document, base):
    soup = BeautifulSoup(document, 'html.parser')
    # /pickup/ がエキスパート記事へ転送される場合、関連ニュースでなく転送先の本文を読む。
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical:
        url = article_url(canonical['href'], base)
        if url and '/expert/articles/' in urllib.parse.urlsplit(url).path:
            return url
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
    blocks = []
    images = []
    # ページ全体やdescriptionを「本文」として扱わない。
    containers = soup.select('.article_body') or soup.select('[itemprop="articleBody"]')
    if not containers and '/expert/articles/' in urllib.parse.urlsplit(url).path:
        containers = soup.select('article section')
    for container in containers:
        # 関連記事・広告を除外し、本文のタグを元記事の順番で走査する。
        for unwanted in container.select('script, style, aside, nav, button, iframe, [class*="related"], [class*="Related"], [class*="advert"]'):
            unwanted.decompose()
        # 別ページの本文を掲載していないため、そこへ誘導する文言も本文に残さない。
        # 写真リンクには画像とキャプションが含まれるため、ここでは保持する。
        paragraphs_with_removed_links = set()
        for link in container.select('a[href]'):
            if not link.find('img'):
                parent = link.find_parent(['p', 'h2', 'h3', 'h4'])
                if parent:
                    paragraphs_with_removed_links.add(id(parent))
                link.decompose()
        elements = container.select('img, p, h2, h3, h4')
        if elements:
            for element in elements:
                if element.name == 'img':
                    candidate = image_url(element.get('src') or element.get('data-src') or '', url)
                    if candidate and not any(pic['url'] == candidate for pic in images) and len(images) < 6:
                        caption = image_caption(element)
                        image = {'url': candidate, 'caption': caption[:300]}
                        images.append(image)
                        blocks.append({'type': 'image', **image})
                    continue
                # 写真リンク・figure内のキャプションを本文として繰り返さない。
                image_link = element.find_parent('a')
                figure = element.find_parent('figure')
                in_caption = any('caption' in css_class.lower()
                                 for parent in (element, *element.parents)
                                 for css_class in parent.get('class', []))
                if ((image_link and image_link.find('img')) or
                        (figure and figure.find('img')) or
                        element.find_parent('figcaption') or
                        in_caption):
                    continue
                separator = ' ' if id(element) in paragraphs_with_removed_links else '\n'
                # 記事内の <br> は段落区切りとして保持し、表示時に別の <p> にする。
                for br in element.find_all('br'):
                    br.replace_with('\ue000')
                text = clean(element.get_text(separator, strip=True)).replace('\ue000', '\n\n')
                if text:
                    body.append(text)
                    bold = element.find(['strong', 'b']) or element.select_one('[style*="font-weight"]')
                    is_heading = (element.name in ('h2', 'h3', 'h4') or
                                  bool(re.search(r'font-weight\s*:\s*(?:bold|[6-9]00)', element.get('style', ''), re.I)) or
                                  (bold is not None and clean(bold.get_text(' ', strip=True)) ==
                                   clean(element.get_text(' ', strip=True))))
                    blocks.append({'type': 'heading' if is_heading else 'text', 'text': text})
        else:
            text = clean(container.get_text('\n', strip=True))
            if text:
                body.append(text)
                blocks.append({'type': 'text', 'text': text})
    if not body and isinstance(metadata.get('articleBody'), str):
        body = [clean(p) for p in metadata['articleBody'].split('\n') if clean(p)]
        blocks.extend({'type': 'text', 'text': p} for p in body)
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
            'body': body, 'blocks': blocks, 'next_url': next_url, 'paywall': paywall, 'images': images}


def fetch_article(client, item, image_dir=None):
    result = dict(item, body=[], blocks=[], images=[], article_title='', publisher='', published='', note='')
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
            for block in parsed['blocks']:
                if block['type'] in ('text', 'heading'):
                    result['body'].append(block['text'])
                    result['blocks'].append(block)
                elif image_dir is not None and len(result['images']) < 6:
                    try:
                        saved = client.save_image(block['url'], image_dir)
                        if not any(photo['src'] == saved for photo in result['images']):
                            photo = {'src': saved, 'caption': block['caption']}
                            result['images'].append(photo)
                            result['blocks'].append({'type': 'image', **photo})
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


def format_published(value):
    if not value:
        return ''
    try:
        published = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if published.tzinfo is None:
            published = published.replace(tzinfo=JST)
        return published.astimezone(JST).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return re.sub(r'(\d{4})/(\d{2})/(\d{2})', r'\1-\2-\3', str(value))


# 複数記者の署名は、かなを含む名前にも対応する。単独の【速報】等は対象外。
MULTI_REPORTER = r'[一-龥][一-龥ぁ-んァ-ヶー]{2,11}(?:[、,，・][ \t]*[一-龥][一-龥ぁ-んァ-ヶー]{2,11})+'
MULTI_BYLINE = re.compile(r'^【' + MULTI_REPORTER + r'】$')
MULTI_BYLINE_SUFFIX = re.compile(r'([。.!！?？」』）])\s*【' + MULTI_REPORTER + r'】\s*$')
MAGAZINE_CREDIT = re.compile(
    r'^[「『]週刊文春[」』]編集部\s*[/／]\s*週刊文春\s*'
    r'[0-9０-９]{4}年[0-9０-９]{1,2}月[0-9０-９]{1,2}日号$')
BYLINE_BRACKET = re.compile(r'^【(?:[ァ-ヶー]{2,16}(?:AFP時事|共同|時事|ロイター|[一-龥]{3,6})|[一-龥]{3,6})】\s*')
WIRE_DATELINE = re.compile(r'^\[[^\]\n]{1,35}(?:ロイター|Reuters|AFP)[^\]\n]*\]\s*[-－―]\s*', re.I)
BYLINE_CREDIT = re.compile(
    r'^(?:\(c\)|©)\s*\d{4}\s+.{2,60}$|'
    r'^（(?:取材[・･/]文[・･/]?)?[一-龥ぁ-んァ-ヶー]{3,16}）$|'
    r'^（\d{1,2}月\d{1,2}日放送\s+.+より）$|'
    r'^(?:日本気象協会(?:\s+本社)?\s+[一-龥]{1,5}[\s\u3000]*[一-龥]{1,5}|'
    r'フジテレビ[、,，]\s*(?:政治|社会|外信|経済|報道|スポーツ)部)$', re.I)
BYLINE_AGENCY = re.compile(r'^(?:朝日新聞社|読売新聞社|毎日新聞社|日本経済新聞社|産経新聞社|共同通信社|時事通信社|AFP時事|ロイター|Full-Count編集部|All Nippon NewsNetwork\(ANN\)|TBSテレビ|テレビ朝日|富山テレビ放送)$')
BYLINE_PERSON = re.compile(r'^(?:[一-龥]{3,6}|[A-Z][a-z]+(?:-[a-z]+)?(?:\s+[A-Z][a-z]+(?:-[a-z]+)?){1,3})$')
BYLINE_SUFFIX = re.compile(r'([。.!！?？」』）])\s*(?:（(?:取材[・･/]文[・･/]?)?[一-龥]{3,6}）|【(?:[ァ-ヶー]{2,16})?[一-龥]{3,6}】)\s*$')
ENGLISH_BYLINE_SUFFIX = re.compile(r'([。.!！?])\s+[A-Z][a-z]+(?:-[a-z]+)?(?:\s+[A-Z][a-z]+(?:-[a-z]+)?){1,3}\s*$')
BYLINE_PREFIX = re.compile(r'^（(?:ブルームバーグ|CNN)）[：:\s]*')
HEADING_CREDITS = (
    'tenki.jp', '読売新聞オンライン', '共同通信', '毎日新聞', 'AERA with Kids＋',
    '高校生新聞', '時事通信', '朝日新聞', 'Bloomberg', 'ロイター', 'CNN.co.jp',
    'オリコン', 'スポニチアネックス', 'スポーツ報知', '産経新聞', '日テレNEWS NNN',
    'デイリースポーツ', 'ゴルフダイジェスト・オンライン（GDO）', 'Full-Count',
    '日刊スポーツ', 'HBCニュース北海道', 'ITmedia NEWS', '東海テレビ',
    '読売新聞（ヨミドクター）', 'テレビ朝日系（ANN）',
    'TBS NEWS DIG Powered by JNN', 'STVニュース北海道',
)
EXPERT_BRAND = re.compile(r'\s*[-－]\s*エキスパート\s*[-－]\s*Yahoo!ニュース\s*$')
EXPERT_POINTS = re.compile(r'(?m)^[ \t\u3000]*ココがポイント[ \t\u3000]*(?:\n|$)')
EXPERT_VIEW = re.compile(r'(?m)^[ \t\u3000]*エキスパートの補足・見解[ \t\u3000]*(?:\n|$)')


def body_paragraphs(text, publisher='', edge=False):
    """既存のスナップショットも含め、本文の段落と署名を表示時に整える。"""
    paragraphs, current = [], []
    for raw in text.replace('\r\n', '\n').split('\n'):
        indented = raw.startswith(('　', '\u00a0'))
        line = raw.lstrip(' \t\u3000\u00a0').strip()
        line = WIRE_DATELINE.sub('', line)
        line = BYLINE_BRACKET.sub('', line)
        line = BYLINE_PREFIX.sub('', line)
        # 元サイトでは配信元が通常の段落や同じ段落の改行に入ることもある。
        line = strip_heading_credit(line, publisher)
        if (BYLINE_CREDIT.fullmatch(line) or BYLINE_AGENCY.fullmatch(line) or
                MULTI_BYLINE.fullmatch(line) or MAGAZINE_CREDIT.fullmatch(line) or
                (line and line == publisher) or
                (edge and line and BYLINE_PERSON.fullmatch(line.replace('　', ' ')))):
            if current:
                paragraphs.append(' '.join(current))
                current = []
            continue
        if not line:
            if current:
                paragraphs.append(' '.join(current))
                current = []
        else:
            if indented and current:
                paragraphs.append(' '.join(current))
                current = []
            current.append(line)
    if current:
        paragraphs.append(' '.join(current))
    paragraphs = [strip_heading_credit(BYLINE_SUFFIX.sub(r'\1', MULTI_BYLINE_SUFFIX.sub(r'\1', paragraph)), publisher)
                  for paragraph in paragraphs]
    if edge:
        paragraphs = [ENGLISH_BYLINE_SUFFIX.sub(r'\1', MULTI_BYLINE_SUFFIX.sub(r'\1', paragraph)) for paragraph in paragraphs]
    return paragraphs


def strip_heading_credit(paragraph, publisher=''):
    paragraph = EXPERT_BRAND.sub('', paragraph)
    for name in (*HEADING_CREDITS, publisher):
        if name:
            paragraph = re.sub(r'[ \t\u3000\u00a0]*' + re.escape('（' + name + '）') +
                               r'[ \t\u3000\u00a0]*', ' ', paragraph)
    return re.sub(r'\s*（(?:取材[・･/]文[・･/]?)?[一-龥]{3,6}）$', '', paragraph).strip()


def without_expert_points(blocks):
    """ポイント欄だけを飛ばし、その後の専門家の見解は残す。"""
    result, skipping = [], False
    for block in blocks:
        if block['type'] not in ('text', 'heading'):
            if not skipping:
                result.append(block)
            continue
        text = block['text']
        while text:
            marker = EXPERT_VIEW.search(text) if skipping else EXPERT_POINTS.search(text)
            if not marker:
                if not skipping:
                    result.append({**block, 'text': text})
                break
            if not skipping and text[:marker.start()].strip():
                result.append({**block, 'text': text[:marker.start()]})
            text = text[marker.end():] if not skipping else text[marker.start():]
            skipping = not skipping
    return result


def news_identity(item):
    url = article_url(item.get('url', ''))
    return url.split('?')[0] if url else item.get('topic_url', '')


def mark_new_items(items, previous):
    known = {news_identity(item) for item in previous}
    for item in items:
        item['is_new'] = news_identity(item) not in known


def render(items, updated):
    esc = html.escape
    def display(value):
        return esc(str(value).translate(FULLWIDTH_ALNUM))

    categories, sections = {}, []
    for n, item in enumerate(items, 1):
        title = item['title']
        categories.setdefault(item['category'], []).append(f'<li data-new="{str(bool(item.get("is_new"))).lower()}"><a href="#news-{n}">{display(title)}</a></li>')
        meta = ' / '.join(filter(None, [item['category'], item.get('publisher', ''), format_published(item.get('published', ''))]))
        ordered = item.get('blocks') or ([{'type': 'image', **photo} for photo in item.get('images', [])] +
                                         [{'type': 'text', 'text': p} for p in item['body']])
        url = item.get('url') or item['topic_url']
        if '/expert/articles/' in urllib.parse.urlsplit(url).path:
            ordered = without_expert_points(ordered)
        text_positions = [i for i, block in enumerate(ordered) if block['type'] in ('text', 'heading')]
        content = []
        for position, block in enumerate(ordered):
            if block['type'] == 'image':
                caption = f'<figcaption>{display(block["caption"])}</figcaption>' if block['caption'] else ''
                content.append(f'<figure><img src="{esc(block["src"], quote=True)}" alt="記事に掲載された写真" loading="lazy" decoding="async">{caption}</figure>')
            else:
                edge = position in text_positions[:2] or position in text_positions[-3:]
                paragraphs = body_paragraphs(block['text'], item.get('publisher', ''), edge)
                paragraphs = [paragraph for paragraph in paragraphs if paragraph]
                paragraph_class = ' class="subheading"' if block['type'] == 'heading' else ''
                content.extend(f'<p{paragraph_class}>{display(paragraph)}</p>' for paragraph in paragraphs)
        note = f'<p class="notice">{display(item["note"])}</p>' if item.get('note') else ''
        full_title = strip_heading_credit(item.get('article_title', ''), item.get('publisher', ''))
        subtitle = f'<p class="full-title">{display(full_title)}</p>' if full_title and full_title != title else ''
        new_label = ' [NEW]' if item.get('is_new') else ''
        sections.append(f'<article id="news-{n}" data-new="{str(bool(item.get("is_new"))).lower()}"><h2>{display(title)}</h2><p class="meta"><a href="{esc(url, quote=True)}" target="_blank" rel="noopener noreferrer nofollow">{display(meta)}</a>{new_label}</p>{subtitle}{"".join(content)}{note}</article>')
    updated_label = f'<div class="update-controls"><time class="updated" datetime="{updated.isoformat()}">更新 {updated.astimezone(JST).strftime("%Y-%m-%d %H:%M")}</time><button class="new-filter" type="button" aria-pressed="false">すべて表示中</button></div>' if items else ''
    page_title = f'最新ニュース{len(items)}' if items else '最新ニュース'
    contents = ''.join(f'<section class="category"><h2>{display(category)}</h2><ul>{"".join(links)}</ul></section>' for category, links in categories.items())
    news = ''.join(sections) or '<p>まだニュースを取得していません。GitHub Actionsの「Update news」を実行してください。</p>'
    return f'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive, nosnippet, noimageindex">
<meta name="referrer" content="no-referrer">
<link rel="icon" href="favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="apple-touch-icon.png" sizes="180x180">
<title>最新ニュース64</title>
<style>
*{{box-sizing:border-box}}html{{scroll-behavior:auto}}body{{margin:0;background:#fff;color:#202020;font-family:"Noto Sans JP", "Hiragino Sans", "Yu Gothic", sans-serif;font-size:17px;line-height:1.9;overflow-wrap:anywhere}}figure{{margin:0 0 18px}}figure img{{display:block;width:auto;max-width:300px;height:auto;max-height:300px;object-fit:contain}}figcaption{{font-size:14px;color:#555;line-height:1.55;margin-top:5px}}
main{{max-width:1440px;margin:0 auto;padding:0 24px 56px}}header{{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid #bbb;padding:4px 0}}.header-row{{display:flex;align-items:center;gap:10px 18px;min-height:34px}}h1{{font-size:23px;line-height:1.3;margin:0;flex:none}}h1 a{{color:inherit;text-decoration:none}}h2{{font-size:20px;line-height:1.55;margin:0 0 8px}}.feed article h2{{color:#14532d}}a{{color:#174c86;text-underline-offset:3px}}a:focus-visible{{outline:2px solid #174c86;outline-offset:4px}}header a:focus-visible,header button:focus-visible{{outline:2px solid #14532d;outline-offset:3px}}.update-controls{{margin-left:auto;display:flex;flex-direction:column;align-items:flex-end;line-height:1.25}}.new-filter{{border:0;background:transparent;color:#555;font:inherit;font-size:14px;padding:2px 0;cursor:pointer}}[hidden]{{display:none!important}}.meta,.updated{{font-size:14px;color:#555}}.meta{{margin:4px 0 14px}}.meta a{{color:inherit}}.menu-toggle,.pull-refresh{{display:none}}.layout{{display:grid;grid-template-columns:minmax(230px,320px) minmax(0,1fr);gap:36px;align-items:start}}nav{{font-size:14px;position:sticky;top:calc(var(--header-height, 42px) + 8px);max-height:calc(100dvh - var(--header-height, 42px) - 16px);overflow:auto;padding-top:12px}}nav h2{{font-size:14px;line-height:1.4;margin:0 0 4px}}.category{{margin:0 0 18px}}.category ul{{list-style:none;margin:0;padding:0}}.category li{{padding:2px 0}}article{{border-top:1px solid #bbb;padding:28px 0;scroll-margin-top:calc(var(--header-height, 42px) + 12px)}}.feed article:first-child{{border-top:0;padding-top:12px}}article p{{margin:0 0 18px}}.subheading{{font-weight:700}}.full-title{{font-weight:600}}.notice{{padding:10px 14px;border-left:3px solid #999;background:#f5f5f5}}@media(max-width:700px){{body{{line-height:1.8}}header{{padding:1px 0;min-height:42px}}.header-row{{gap:6px 10px}}.new-filter{{font-size:14px;padding:0}}main{{padding:0 16px 36px}}.pull-refresh{{display:block;position:fixed;top:0;left:50%;z-index:25;padding:3px 14px;background:#14532d;color:#fff;border-radius:0 0 12px 12px;font-size:14px;pointer-events:none;transform:translate(-50%,-110%);transition:transform .15s}}.pull-refresh.active{{transform:translate(-50%,0)}}.layout{{display:block}}.menu-toggle{{display:inline-flex;align-items:center;justify-content:center;flex:none;order:3;margin-left:auto;width:32px;height:34px;border:0;background:transparent;color:inherit;padding:4px}}.hamburger{{display:flex;flex-direction:column;gap:4px}}.hamburger span{{display:block;width:20px;height:2px;background:currentColor}}h1{{font-size:20px}}.updated{{margin-left:0}}.layout nav{{display:none;position:fixed;top:var(--header-height, 42px);left:0;right:0;z-index:19;max-height:calc(100dvh - var(--header-height, 42px));overflow:auto;padding:12px 16px;background:#fff;border-bottom:1px solid #bbb;box-shadow:0 5px 10px #0002}}body.menu-open .layout nav{{display:block}}}}@media print{{header{{position:static}}.menu-toggle,nav,.pull-refresh{{display:none!important}}main{{max-width:none;padding:0}}.layout{{display:block}}article{{break-inside:auto}}}}
</style></head><body><div class="pull-refresh" role="status" aria-live="polite">下に引いて更新</div><main id="top"><header><div class="header-row"><button class="menu-toggle" type="button" aria-label="タイトル一覧を開く" aria-controls="news-nav" aria-expanded="false"><span class="hamburger" aria-hidden="true"><span></span><span></span><span></span></span></button><h1><a href="#top">{display(page_title)}</a></h1>{updated_label}</div></header>
<div class="layout"><nav id="news-nav" aria-label="タイトル一覧">{contents}</nav><div class="feed">
{news}
</div></div></main><script>
const filterButton = document.querySelector('.new-filter');
filterButton?.addEventListener('click', () => {{
  const onlyNew = filterButton.getAttribute('aria-pressed') !== 'true';
  filterButton.setAttribute('aria-pressed', String(onlyNew));
  filterButton.textContent = onlyNew ? 'NEWのみ表示中' : 'すべて表示中';
  document.querySelectorAll('[data-new]').forEach(node => node.hidden = onlyNew && node.dataset.new !== 'true');
  document.querySelectorAll('nav .category').forEach(node => node.hidden = !node.querySelector('li:not([hidden])'));
}});
const header = document.querySelector('header');
const menuButton = document.querySelector('.menu-toggle');
const menu = document.getElementById('news-nav');
const updateHeaderHeight = () => document.documentElement.style.setProperty('--header-height', `${{header.getBoundingClientRect().height}}px`);
updateHeaderHeight();
new ResizeObserver(updateHeaderHeight).observe(header);
function setMenu(open) {{
  document.body.classList.toggle('menu-open', open);
  menuButton.setAttribute('aria-expanded', String(open));
  menuButton.setAttribute('aria-label', open ? 'タイトル一覧を閉じる' : 'タイトル一覧を開く');
}}
menuButton.addEventListener('click', () => setMenu(menuButton.getAttribute('aria-expanded') !== 'true'));
menu.addEventListener('click', event => {{ if (event.target.closest('a[href^="#"]')) setMenu(false); }});
document.addEventListener('keydown', event => {{ if (event.key === 'Escape') setMenu(false); }});
document.querySelector('header h1 a').addEventListener('click', () => setMenu(false));
const pullIndicator = document.querySelector('.pull-refresh');
let startY = null, startX = 0, pullDistance = 0;
document.addEventListener('touchstart', event => {{
  startY = event.touches.length === 1 && matchMedia('(max-width:700px)').matches &&
    window.scrollY <= 0 && !document.body.classList.contains('menu-open') ? event.touches[0].clientY : null;
  startX = event.touches[0]?.clientX || 0;
  pullDistance = 0;
}}, {{passive:true}});
document.addEventListener('touchmove', event => {{
  if (startY === null || event.touches.length !== 1) return;
  const dy = event.touches[0].clientY - startY;
  if (dy <= 10 || Math.abs(event.touches[0].clientX - startX) > dy || window.scrollY > 0) return;
  event.preventDefault();
  pullDistance = Math.min(dy, 120);
  pullIndicator.classList.add('active');
  pullIndicator.textContent = pullDistance >= 90 ? '離して更新' : '下に引いて更新';
}}, {{passive:false}});
function finishPull() {{
  if (pullDistance >= 90) {{
    pullIndicator.textContent = '更新中…';
    window.location.reload();
  }} else {{
    pullIndicator.classList.remove('active');
  }}
  startY = null;
  pullDistance = 0;
}}
document.addEventListener('touchend', finishPull, {{passive:true}});
document.addEventListener('touchcancel', () => {{
  pullIndicator.classList.remove('active');
  startY = null;
  pullDistance = 0;
}}, {{passive:true}});
</script></body></html>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='site')
    parser.add_argument('--limit', type=int, default=0, help='取得テスト用。0は一覧全件')
    parser.add_argument('--previous-snapshot', type=Path, help='NEW判定用の前回取得結果')
    parser.add_argument('--empty', action='store_true', help='通信せず初回案内ページを生成')
    parser.add_argument('--from-snapshot', type=Path, help='保存した記事と画像を使ってHTMLだけ再生成')
    args = parser.parse_args()
    now = datetime.now(JST)
    output = Path(args.output)
    items = []
    if args.from_snapshot:
        snapshot = json.loads(args.from_snapshot.read_text(encoding='utf-8'))
        if snapshot.get('format_version') != SNAPSHOT_VERSION:
            raise RuntimeError('本文抽出方法が更新されたため、ニュースを取り直します')
        items = snapshot['items']
        now = datetime.fromisoformat(snapshot['updated'])
        if not items or not any(item.get('body') for item in items):
            raise RuntimeError('保存済みの記事がありません')
        if args.limit == 0 and not full_topic_list(items):
            raise RuntimeError('保存済みのニュースが64件揃わないため、ニュースを取り直します')
        for item in items:
            for block in item.get('blocks', []):
                if block['type'] == 'image' and not (output / block['src']).is_file():
                    raise RuntimeError(f'保存済み画像が見つかりません: {block["src"]}')
        print(f'保存済みの{len(items)}件からHTMLを再生成します（ニュースの再取得なし）', flush=True)
    elif not args.empty:
        client = Client()
        topics = fetch_topics(client, require_full=args.limit == 0)
        if args.limit > 0:
            topics = topics[:args.limit]
        print(f'一覧 {len(topics)}件', flush=True)
        for n, item in enumerate(topics, 1):
            items.append(fetch_article(client, item, output / 'images'))
            print(f'{n}/{len(topics)} 本文取得={bool(items[-1]["body"])}', flush=True)
        if not any(i['body'] for i in items):
            raise RuntimeError('本文を1件も取得できなかったため公開を中止します。既存ページは維持されます。')
    if not args.from_snapshot and not args.empty:
        previous = []
        if args.previous_snapshot and args.previous_snapshot.is_file():
            previous = json.loads(args.previous_snapshot.read_text(encoding='utf-8')).get('items', [])
        mark_new_items(items, previous)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'index.html').write_text(render(items, now), encoding='utf-8')
    for icon in ('favicon.ico', 'apple-touch-icon.png'):
        shutil.copyfile(Path(__file__).resolve().parents[1] / icon, output / icon)
    if not args.from_snapshot and not args.empty:
        (output / 'snapshot.json').write_text(
            json.dumps({'format_version': SNAPSHOT_VERSION, 'updated': now.isoformat(), 'items': items}, ensure_ascii=False), encoding='utf-8')
    # noindexを読めるように、robots.txtによるクロール拒否は行わない。
    (output / 'robots.txt').write_text('User-agent: *\nDisallow:\n', encoding='utf-8')
    (output / '.nojekyll').touch()
    print(f'生成完了: {output / "index.html"}', flush=True)


if __name__ == '__main__':
    main()
