import importlib.util
import io
import json
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


if __name__ == '__main__':
    unittest.main()
