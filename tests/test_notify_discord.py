import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('notify_discord', Path(__file__).parents[1] / 'scripts/notify_discord.py')
notify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notify)


class DiscordNotificationTests(unittest.TestCase):
    def test_normal_message_and_github_suffix(self):
        with patch.object(notify.urllib.request, 'urlopen') as urlopen:
            notify.send('https://discord.com/api/webhooks/123/secret/github', 'https://example.github.io/news/')
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'https://discord.com/api/webhooks/123/secret')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('User-agent'),
                         'DiscordBot (https://github.com/nobusukeyanagi/news, 1.0)')
        self.assertEqual(json.loads(request.data), {
            'content': '📰 ニュースを更新しました\nhttps://example.github.io/news/',
            'allowed_mentions': {'parse': []},
        })

    def test_http_error_exposes_status_without_webhook_token(self):
        url = 'https://discord.com/api/webhooks/123/private-token'
        error = urllib.error.HTTPError(url, 404, 'Not Found', {},
                                       io.BytesIO(b'{"code":10015,"message":"Unknown Webhook"}'))
        with patch.object(notify.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 404 / Discord code 10015') as context:
                notify.send(url, 'https://example.github.io/news/')
        self.assertNotIn('private-token', str(context.exception))

    def test_non_discord_url_is_rejected(self):
        with self.assertRaises(ValueError):
            notify.webhook_url('https://other.example/api/webhooks/123/token')

    def test_snapshot_includes_only_new_titles_in_page_order(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'snapshot.json'
            snapshot.write_text(json.dumps({'items': [
                {'title': '最初のニュース', 'is_new': True},
                {'title': '前回からあるニュース', 'is_new': False},
                {'title': '次のニュース', 'is_new': True},
            ]}), encoding='utf-8')
            titles = notify.new_titles(snapshot)
        self.assertEqual(titles, ['最初のニュース', '次のニュース'])
        with patch.object(notify.urllib.request, 'urlopen') as urlopen:
            notify.send('https://discord.com/api/webhooks/123/secret',
                        'https://example.github.io/news/', titles)
        self.assertEqual(json.loads(urlopen.call_args.args[0].data)['content'],
                         '📰 ニュースを更新しました\nhttps://example.github.io/news/\n'
                         '最初のニュース\n次のニュース')

    def test_many_new_titles_are_sent_without_omissions(self):
        titles = [f'{number:02d} ' + '長いニュース' * 8 for number in range(64)]
        with patch.object(notify.urllib.request, 'urlopen') as urlopen:
            notify.send('https://discord.com/api/webhooks/123/secret',
                        'https://example.github.io/news/', titles)
        contents = [json.loads(call.args[0].data)['content'] for call in urlopen.call_args_list]
        self.assertGreater(len(contents), 1)
        self.assertTrue(all(len(content) <= 2000 for content in contents))
        self.assertEqual('\n'.join(contents).splitlines()[2:], titles)


if __name__ == '__main__':
    unittest.main()
