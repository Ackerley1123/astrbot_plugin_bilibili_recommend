import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from service import (DEFAULT_TOPICS, PublicClient, RecommendationError, RecommendationService,
                     Settings, StateStore, clean, cover_url, strings, video_id)

A, B = 'BV1EW4y1W7Z9', 'BV1Q14y127Sm'


class FakeClient:
    def __init__(self):
        self.urls = [f'https://www.bilibili.com/video/{x}/' for x in [A, B]]
        self.queries, self.metadata_calls = [], []
        self.metadata = {}
        self.cover_error = False

    async def search(self, keyword, keys):
        self.queries.append(keyword)
        return self.urls

    async def video(self, bvid):
        self.metadata_calls.append(bvid)
        return self.metadata.get(bvid, {'bvid': bvid, 'title': 'Test <em>video</em>', 'owner': {'name': 'Uploader'}, 'pubdate': 1, 'pic': 'https://i0.hdslb.com/example.jpg'})

    async def cover(self, url, bvid):
        if self.cover_error:
            raise OSError('cache unavailable')
        return ''


class ParsingTests(unittest.TestCase):
    def test_video_links_are_strict(self):
        self.assertEqual(video_id(f'https://www.bilibili.com/video/{A}/?p=1'), A)
        for url in [f'https://bilibili.com.evil.example/video/{A}', f'http://www.bilibili.com/video/{A}',
                    f'https://user:pass@www.bilibili.com/video/{A}', f'https://www.bilibili.com:444/video/{A}',
                    f'https://www.bilibili.com/video/{A}/extra', 'file:///video/'+A, 'not a URL']:
            self.assertIsNone(video_id(url), url)

    def test_cover_host_and_protocol(self):
        self.assertEqual(cover_url('http://i2.hdslb.com/example.jpg'), 'https://i2.hdslb.com/example.jpg')
        for url in ['https://hdslb.com.evil.example/x', 'https://127.0.0.1/x', 'file:///x',
                    'https://user:pass@i2.hdslb.com/x', 'https://i2.hdslb.com:444/x']:
            self.assertIsNone(cover_url(url))

    def test_configuration_bounds_and_empty_topics(self):
        s = Settings.from_config({'topics': ['<b></b>'], 'cooldown_seconds': -1, 'max_results': 999,
                                  'timeout_seconds': 'bad', 'cover_max_mb': 999})
        self.assertEqual(s.topics, DEFAULT_TOPICS)
        self.assertEqual((s.cooldown_seconds, s.max_results, s.timeout_seconds), (5, 10, 60))
        self.assertEqual(s.cover_max_bytes, 10*1024*1024)
        self.assertEqual(strings([' a ', 'a', None]), ('a',))
        self.assertEqual(clean('<b>Title</b>\n\x00 &amp; X'), 'Title & X')

    def test_corrupt_state_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)/'state.json'
            p.write_text('{broken', encoding='utf-8')
            with self.assertRaises(RecommendationError):
                StateStore(Path(folder))
            self.assertEqual(p.read_text(), '{broken')


class RecommendationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeClient()
        self.now = 1_700_000_000
        self.service = RecommendationService(self.root, Settings(), self.client, lambda: self.now)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def get(self, scope='adapter:FriendMessage:alice', keyword='JPOP', proactive=False):
        return await self.service.recommend(scope, keyword, ('fake-test-key',), proactive=proactive)

    async def test_metadata_and_link(self):
        video = await self.get()
        self.assertEqual(video.title, 'Test video')
        self.assertEqual(video.url, f'https://www.bilibili.com/video/{A}/')
        self.assertEqual(self.client.queries, ['JPOP'])

    async def test_dedup_persists_per_conversation(self):
        self.assertEqual((await self.get()).bvid, A)
        self.now += 16
        self.service = RecommendationService(self.root, Settings(), self.client, lambda: self.now)
        self.assertEqual((await self.get()).bvid, B)
        self.assertEqual((await self.get('adapter:FriendMessage:bob')).bvid, A)
        self.assertNotIn('alice', (self.root/'state.json').read_text())

    async def test_cooldown_does_not_call_search_again(self):
        await self.get()
        with self.assertRaisesRegex(RecommendationError, '再等'):
            await self.get()
        self.assertEqual(len(self.client.queries), 1)

    async def test_manual_does_not_consume_proactive_cooldown(self):
        await self.get()
        self.assertEqual((await self.get(proactive=True)).bvid, B)
        with self.assertRaisesRegex(RecommendationError, '再等'):
            await self.get(proactive=True)

    async def test_invalid_search_urls_never_reach_metadata(self):
        self.client.urls = ['https://evil.example/video/'+A, 'https://search.bilibili.com/all']
        with self.assertRaises(RecommendationError):
            await self.get()
        self.assertEqual(self.client.metadata_calls, [])

    async def test_unverified_candidate_is_skipped(self):
        self.client.metadata[A] = {'bvid': B, 'title': 'wrong ID'}
        self.assertEqual((await self.get()).bvid, B)

    async def test_cover_failure_keeps_verified_text(self):
        self.client.cover_error = True
        video = await self.get()
        self.assertEqual(video.bvid, A)
        self.assertEqual(video.image_path, '')

    async def test_repeated_results_fail_without_fabrication(self):
        await self.get()
        self.now += 16
        await self.get()
        self.now += 16
        with self.assertRaisesRegex(RecommendationError, '未重复'):
            await self.get()
        self.assertEqual(self.client.metadata_calls, [A, B])

    async def test_default_topics_rotate(self):
        await self.get(keyword='')
        self.now += 16
        await self.get(keyword='')
        self.assertEqual(self.client.queries, list(DEFAULT_TOPICS[:2]))

    async def test_missing_key_or_long_keyword_does_not_search(self):
        with self.assertRaisesRegex(RecommendationError, '密钥'):
            await self.service.recommend('test', '', ())
        with self.assertRaisesRegex(RecommendationError, '80'):
            await self.get(keyword='a'*81)
        self.assertEqual(self.client.queries, [])

    async def test_same_session_concurrency_is_rejected(self):
        entered, release = asyncio.Event(), asyncio.Event()
        search = self.client.search
        async def delayed(keyword, keys):
            entered.set()
            await release.wait()
            return await search(keyword, keys)
        self.client.search = delayed
        first = asyncio.create_task(self.get())
        await entered.wait()
        try:
            with self.assertRaisesRegex(RecommendationError, '正在'):
                await self.get()
        finally:
            release.set()
            await first

    async def test_bounded_state_and_locks(self):
        self.service = RecommendationService(self.root, Settings(max_sessions=2), self.client, lambda: self.now)
        for scope in ['a', 'b', 'c', 'd']:
            await self.get(scope)
        self.assertLessEqual(len(self.service.store.data['sessions']), 2)
        self.assertLessEqual(len(self.service.locks), 2)


class Response:
    def __init__(self, status=200, data=None, chunks=()):
        self.status, self.data, self.chunks = status, data, chunks
        self.content = self

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self, **kwargs): return self.data
    async def iter_chunked(self, size):
        for chunk in self.chunks: yield chunk


class Session:
    def __init__(self, responses): self.responses, self.calls = responses, []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = PublicClient(Path(self.temp.name), Settings())

    async def asyncTearDown(self): self.temp.cleanup()

    async def test_key_failover_uses_only_official_endpoint(self):
        session = Session([Response(401), Response(data={'results': [{'url': 'https://example.org'}]})])
        with patch('service.aiohttp.ClientSession', return_value=session):
            self.assertEqual(await self.client.search('JPOP', ('key-one', 'key-two')), ['https://example.org'])
        self.assertEqual(session.calls[1][1]['headers']['Authorization'], 'Bearer key-two')
        self.assertEqual(session.calls[1][0], 'https://api.tavily.com/search')
        self.assertFalse(session.calls[1][1]['allow_redirects'])
        self.assertEqual(session.calls[1][1]['json']['search_depth'], 'basic')

    async def test_api_response_text_is_not_leaked_on_failure(self):
        session = Session([Response(500, {'secret': 'DO-NOT-EXPOSE'})])
        with patch('service.aiohttp.ClientSession', return_value=session):
            with self.assertRaises(RecommendationError) as error:
                await self.client.search('JPOP', ('DO-NOT-EXPOSE',))
        self.assertNotIn('DO-NOT-EXPOSE', str(error.exception))

    async def test_metadata_requires_matching_public_bvid(self):
        session = Session([Response(data={'code': 0, 'data': {'bvid': B, 'title': 'mismatch'}})])
        with patch('service.aiohttp.ClientSession', return_value=session):
            self.assertIsNone(await self.client.video(A))

    async def test_cover_can_arrive_in_multiple_chunks(self):
        buffer = io.BytesIO()
        Image.new('RGB', (8, 8)).save(buffer, format='PNG')
        data = buffer.getvalue()
        session = Session([Response(chunks=[data[:12], data[12:]])])
        with patch('service.aiohttp.ClientSession', return_value=session):
            path = await self.client.cover('https://i0.hdslb.com/test.png', A)
        self.assertEqual(Path(path).read_bytes(), data)

    async def test_oversized_or_invalid_cover_is_rejected(self):
        self.client.settings = Settings(cover_max_bytes=10)
        session = Session([Response(chunks=[b'x'*11]), Response(chunks=[b'bad'])])
        with patch('service.aiohttp.ClientSession', return_value=session):
            self.assertEqual(await self.client.cover('https://i0.hdslb.com/a.png', A), '')
            self.assertEqual(await self.client.cover('https://i0.hdslb.com/a.png', A), '')


if __name__ == '__main__':
    unittest.main()
