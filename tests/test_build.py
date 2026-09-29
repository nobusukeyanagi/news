import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('build', Path(__file__).parents[1] / 'scripts/build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class ReaderTests(unittest.TestCase):
    def test_topics_excludes_sidebar_and_badges(self):
        doc = '''<main><div id="yjnMain"><div><a href="/categories/domestic">国内</a>
        <ul><li><a href="/pickup/1">サンプルの見出し<span>NEW</span></a></li></ul></div></div>
        <aside id="yjnSub"><a href="/pickup/2">ランキングの記事</a></aside></main>'''
        items = build.parse_topics(doc)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], 'サンプルの見出し')
        self.assertEqual(items[0]['category'], '国内')

    def test_body_pagination_and_caption_exclusion(self):
        doc = '''<article><h1>サンプル</h1><div class="article_body">
        <a href="/articles/abc/images/1"><img src="https://newsatcl-pctr.c.yimg.jp/photo.jpg">画像：提供元</a>
        <figure>写真の説明</figure><h2>小見出し</h2><p class="yjSlinkDirectlink">本文の例です。</p>
        <div class="related">関連ニュース</div></div>
        <a href="/articles/abc?page=2">次へ</a><a href="/articles/def?page=3">別の記事</a></article>'''
        item = build.parse_article(doc, 'https://news.yahoo.co.jp/articles/abc')
        self.assertEqual(item['body'], ['小見出し', '本文の例です。'])
        self.assertEqual(item['next_url'], 'https://news.yahoo.co.jp/articles/abc?page=2')
        self.assertEqual(item['images'], [{'url': 'https://newsatcl-pctr.c.yimg.jp/photo.jpg', 'caption': '画像：提供元'}])
        self.assertEqual([block['type'] for block in item['blocks']], ['image', 'text', 'text'])

    def test_images_follow_original_article_order_without_duplicate_captions(self):
        photo1 = 'https://newsatcl-pctr.c.yimg.jp/one.jpg'
        photo2 = 'https://newsatcl-pctr.c.yimg.jp/two.jpg'
        article = f'''<div class="article_body">
            <p>最初の段落</p>
            <a><img src="{photo1}"><p>画像：一枚目</p></a>
            <p>次の段落</p>
            <a><img src="{photo2}"><p>画像：二枚目</p></a>
            <p>最後の段落</p></div>'''

        class Client:
            def get(self, url):
                if '/pickup/' in url:
                    return '<a href="/articles/abc">記事全文を読む</a>'
                return article

            def save_image(self, url, directory):
                return 'images/' + url.rsplit('/', 1)[-1]

        item = build.fetch_article(Client(), {'title': 'テスト', 'category': '国内',
            'topic_url': 'https://news.yahoo.co.jp/pickup/1'}, Path('/unused'))
        page = build.render([item], datetime.now(timezone.utc))
        markers = ['最初の段落', 'images/one.jpg', '画像：一枚目', '次の段落',
                   'images/two.jpg', '画像：二枚目', '最後の段落']
        self.assertEqual(sorted(page.index(marker) for marker in markers),
                         [page.index(marker) for marker in markers])
        self.assertEqual(page.count('画像：一枚目'), 1)
        self.assertEqual(page.count('画像：二枚目'), 1)

    def test_unshown_link_text_is_removed_from_article_body(self):
        doc = '''<div class="article_body">
          <p>前段 <a href="/articles/elsewhere">【写真】別の記事</a> 後段</p>
          <p><a href="https://other.example.com/story">【一目でわかる世界情勢】米中の「敵」と「味方」</a></p>
          <a href="/articles/abc/images/1"><img src="https://newsatcl-pctr.c.yimg.jp/photo.jpg"><p>画像：提供元</p></a>
          <p>通常の本文</p>
        </div>'''
        item = build.parse_article(doc, 'https://news.yahoo.co.jp/articles/abc')
        self.assertEqual(item['body'], ['前段 後段', '通常の本文'])
        self.assertEqual([block['type'] for block in item['blocks']], ['text', 'image', 'text'])
        self.assertEqual(item['images'][0]['caption'], '画像：提供元')

    def test_summary_is_not_used_as_body(self):
        item = build.parse_article('''<script type="application/ld+json">{
        "@type":"NewsArticle", "description":"本文ではない概要"}</script>
        <main>周辺のメニュー</main>''', 'https://news.yahoo.co.jp/articles/abc')
        self.assertEqual(item['body'], [])

    def test_prefer_read_full_article_link(self):
        url = build.linked_article('''<a href="/articles/aaa">関連ニュース</a>
        <a href="/articles/bbb">記事全文を読む</a>''', build.SOURCE)
        self.assertTrue(url.endswith('/bbb'))

    def test_expert_pickup_uses_canonical_instead_of_related_story(self):
        url = build.linked_article('''<link rel="canonical" href="https://news.yahoo.co.jp/expert/articles/abc">
        <a href="/expert/articles/def">関連記事</a>''', build.SOURCE)
        self.assertEqual(url, 'https://news.yahoo.co.jp/expert/articles/abc')
        item = build.parse_article('''<article><section><h2>見出し</h2><p>エキスパート本文</p></section></article>''', url)
        self.assertEqual(item['body'], ['見出し', 'エキスパート本文'])

    def test_partial_failure_preserves_first_page(self):
        class Client:
            def get(self, url):
                if '/pickup/' in url:
                    return '<a href="/articles/abc">記事全文を読む</a>'
                if 'page=2' in url:
                    raise RuntimeError('テスト用の取得失敗')
                return '<div class="article_body"><p>1ページ目</p></div><a href="?page=2">次へ</a>'
        item = build.fetch_article(Client(), {'title': 'テスト', 'topic_url': 'https://news.yahoo.co.jp/pickup/1', 'category': '国内'})
        self.assertEqual(item['body'], ['1ページ目'])
        self.assertIn('一部', item['note'])

    def test_escaping_and_noindex(self):
        page = build.render([{'title': '<script>alert(1)</script>', 'category': '国内',
                              'topic_url': build.SOURCE, 'body': ['<img src=x onerror=alert(1)>']}], datetime.now(timezone.utc))
        self.assertNotIn('<script>alert(1)</script>', page)
        self.assertNotIn('<img ', page)
        self.assertIn('noindex, nofollow', page)
        self.assertIn('&lt;script&gt;', page)

    def test_photo_caption_and_columns(self):
        page = build.render([{'title': '見出し', 'category': '国内', 'topic_url': build.SOURCE,
                              'body': ['記事本文'], 'images': [{'src': 'images/p.jpg', 'caption': '<出典>'}]}],
                            datetime.now(timezone.utc))
        self.assertIn('grid-template-columns:minmax(230px,320px) minmax(0,1fr)', page)
        self.assertIn('<figcaption>&lt;出典&gt;</figcaption>', page)
        self.assertIn('font-size:.8125rem', page)
        self.assertIn('max-width:300px;height:auto;max-height:300px', page)

    def test_compact_header_and_category_list(self):
        items = [
            {'title': '記事A', 'category': '国内', 'topic_url': build.SOURCE, 'body': ['本文A']},
            {'title': '記事B', 'category': '国際', 'topic_url': build.SOURCE, 'body': ['本文B']},
        ]
        page = build.render(items, datetime(2026, 9, 29, 1, 23, tzinfo=timezone.utc))
        self.assertIn('<h1><a href="#top">最新ニュース2</a></h1><time', page)
        self.assertIn('更新：2026/09/29 10:23', page)
        self.assertIn('<h2>国内</h2><ul>', page)
        self.assertIn('<h2>国際</h2><ul>', page)
        self.assertNotIn('<ol>', page)
        self.assertNotIn('<h2>タイトル一覧</h2>', page)
        self.assertNotIn('本文取得 2件', page)
        self.assertNotIn('毎日6:00・18:00に更新予定', page)
        self.assertIn('header{position:sticky;top:0', page)
        self.assertIn('border-bottom:1px solid #bbb', page)
        self.assertIn('aria-controls="news-nav" aria-expanded="false"', page)
        self.assertIn('body.menu-open .layout nav{display:block}', page)
        self.assertIn('scroll-margin-top:calc(var(--header-height, 42px) + 12px)', page)
        self.assertIn('.feed article h2{color:#14532d}', page)
        self.assertNotIn('nav h2{color:#14532d}', page)

    def test_article_time_is_jst_and_meta_links_to_source(self):
        item = {'title': '記事', 'category': '国内', 'publisher': 'tenki.jp',
                'topic_url': 'https://news.yahoo.co.jp/pickup/1',
                'url': 'https://news.yahoo.co.jp/articles/abc',
                'published': '2026-09-28T20:54:36Z', 'body': ['本文']}
        page = build.render([item], datetime.now(timezone.utc))
        self.assertIn('>国内 / tenki.jp / 2026/09/29 05:54</a></p>', page)
        self.assertIn('<p class="meta"><a href="https://news.yahoo.co.jp/articles/abc"', page)
        self.assertNotIn('元記事を読む', page)
        self.assertNotIn('目次へ', page)

    def test_rebuild_from_snapshot_does_not_fetch_news(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            (site / 'images').mkdir()
            (site / 'images' / 'one.jpg').write_bytes(b'photo')
            snapshot = {'format_version': build.SNAPSHOT_VERSION,
                        'updated': '2026-09-29T06:00:00+09:00', 'items': [{
                'title': '保存した記事', 'category': '国内', 'topic_url': build.SOURCE,
                'body': ['本文'], 'blocks': [{'type': 'image', 'src': 'images/one.jpg',
                                             'caption': '撮影者'}, {'type': 'text', 'text': '本文'}]}]}
            (site / 'snapshot.json').write_text(json.dumps(snapshot), encoding='utf-8')
            with patch.object(sys, 'argv', ['build.py', '--from-snapshot', str(site / 'snapshot.json'),
                                            '--output', str(site)]), patch.object(build, 'Client',
                                                                                 side_effect=AssertionError('network')):
                build.main()
            page = (site / 'index.html').read_text(encoding='utf-8')
            self.assertIn('更新：2026/09/29 06:00', page)
            self.assertLess(page.index('images/one.jpg'), page.index('本文'))

    def test_old_snapshot_must_refetch_after_extraction_change(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory)
            (site / 'snapshot.json').write_text(json.dumps({'updated': '2026-09-29T06:00:00+09:00',
                                                               'items': [{'body': ['旧形式']}]}), encoding='utf-8')
            with patch.object(sys, 'argv', ['build.py', '--from-snapshot', str(site / 'snapshot.json'),
                                            '--output', str(site)]):
                with self.assertRaisesRegex(RuntimeError, '取り直します'):
                    build.main()


if __name__ == '__main__':
    unittest.main()
