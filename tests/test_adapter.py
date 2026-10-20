"""Adapter contract tests without installing AstrBot or sending any platform message."""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


class Plain:
    def __init__(self, text): self.text = text


class Image:
    @staticmethod
    def fromFileSystem(path): return ('image', path)


class Star:
    def __init__(self, context): pass


class StarTools:
    root = None
    @classmethod
    def get_data_dir(cls, name): return cls.root


def decorator(*args, **kwargs):
    return lambda function: function


def load_adapter():
    modules = {name: types.ModuleType(name) for name in ['astrbot', 'astrbot.api', 'astrbot.api.event',
               'astrbot.api.message_components', 'astrbot.api.star']}
    modules['astrbot.api'].AstrBotConfig = dict
    modules['astrbot.api'].logger = types.SimpleNamespace(info=lambda *args: None, warning=lambda *args: None)
    modules['astrbot.api.event'].AstrMessageEvent = object
    modules['astrbot.api.event'].filter = types.SimpleNamespace(command=decorator, on_astrbot_loaded=decorator)
    modules['astrbot.api.message_components'].Plain = Plain
    modules['astrbot.api.message_components'].Image = Image
    modules['astrbot.api.star'].Star = Star
    modules['astrbot.api.star'].StarTools = StarTools
    modules['astrbot.api.star'].Context = object
    modules['astrbot.api.star'].register = decorator
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location('plugin_under_test', ROOT/'__init__.py', submodule_search_locations=[str(ROOT)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        loaded = __import__('plugin_under_test.main', fromlist=['BilibiliRecommend'])
        return loaded, sys.modules['plugin_under_test.service'].Video


adapter, Video = load_adapter()


class Event:
    def __init__(self, scope='adapter:FriendMessage:alice', sender='alice', group=''):
        self.unified_msg_origin, self.sender, self.group = scope, sender, group
        self.stopped = False
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.sender
    def stop_event(self): self.stopped = True
    def plain_result(self, text): return text
    def chain_result(self, chain): return chain


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        StarTools.root = Path(self.temp.name)
        self.config = {k: v['default'] for k, v in json.loads((ROOT/'_conf_schema.json').read_text(encoding='utf-8')).items()}
        self.calls = []
        self.context = types.SimpleNamespace(get_config=self.core_config)
        self.plugin = adapter.BilibiliRecommend(self.context, self.config)

    def core_config(self, **kwargs):
        self.calls.append(kwargs)
        return {'provider_settings': {'websearch_tavily_key': ['fake-inherited-key']}}

    async def asyncTearDown(self): self.temp.cleanup()

    async def test_independent_default_does_not_import_companion(self):
        with patch.object(adapter.importlib, 'import_module', side_effect=AssertionError('must not import')):
            await self.plugin.initialize()
        self.assertIsNone(self.plugin.companion_api)

    async def test_permissions_are_general_and_group_opt_in(self):
        self.assertTrue(self.plugin.allowed(Event()))
        self.assertTrue(self.plugin.allowed(Event('other:FriendMessage:bob', 'bob')))
        self.assertFalse(self.plugin.allowed(Event('other:GroupMessage:example', 'bob', 'example')))
        self.config['allow_groups'] = True
        self.assertTrue(self.plugin.allowed(Event('other:GroupMessage:example', 'bob', 'example')))

    async def test_user_and_session_lists_are_intersected(self):
        self.config.update(allowed_user_ids=['alice'], allowed_sessions=['adapter:FriendMessage:alice'])
        self.assertTrue(self.plugin.allowed(Event()))
        self.assertFalse(self.plugin.allowed(Event('adapter:FriendMessage:alice', 'bob')))
        self.assertFalse(self.plugin.allowed(Event('other:FriendMessage:alice', 'alice')))

    async def test_denied_command_does_not_reach_search(self):
        self.config['enabled'] = False
        self.plugin.service.recommend = AsyncMock()
        event = Event()
        result = [x async for x in self.plugin.recommend_command(event)]
        self.assertTrue(event.stopped)
        self.assertIn('没有启用', result[0])
        self.plugin.service.recommend.assert_not_awaited()

    async def test_own_key_takes_precedence(self):
        self.config['tavily_api_keys'] = ['fake-own-key']
        self.assertEqual(self.plugin.keys('example'), ('fake-own-key',))
        self.assertEqual(self.calls, [])
        self.config['tavily_api_keys'] = []
        self.assertEqual(self.plugin.keys('example'), ('fake-inherited-key',))
        self.assertEqual(self.calls, [{'umo': 'example'}])
        self.config['reuse_astrbot_tavily'] = False
        self.assertEqual(self.plugin.keys('example'), ())

    async def test_manual_output_has_real_link_and_optional_image(self):
        image = Path(self.temp.name)/'synthetic.png'
        image.write_bytes(b'synthetic-placeholder')
        video = Video('BV1EW4y1W7Z9', 'Public title', 'Uploader', 0, str(image))
        self.plugin.service.recommend = AsyncMock(return_value=video)
        result = [x async for x in self.plugin.recommend_command(Event(), 'YOASOBI')]
        self.assertIn(video.url, result[0][0].text)
        self.assertEqual(result[0][1], ('image', str(image)))
        self.plugin.service.recommend.assert_awaited_once_with('adapter:FriendMessage:alice', 'YOASOBI', ('fake-inherited-key',))

    async def test_user_facing_error_does_not_fabricate_video(self):
        self.plugin.service.recommend = AsyncMock(side_effect=adapter.RecommendationError('search unavailable'))
        result = [x async for x in self.plugin.recommend_command(Event())]
        self.assertEqual(result, ['search unavailable'])

    async def test_optional_bridge_requires_targets_and_keeps_manual_working(self):
        self.config['enable_companion_bridge'] = True
        with patch.object(adapter.importlib, 'import_module', side_effect=AssertionError('targets missing')):
            await self.plugin.initialize()
        self.config['proactive_targets'] = ['adapter:FriendMessage:alice']
        with patch.object(adapter.importlib, 'import_module', side_effect=ImportError('not installed')):
            await self.plugin.initialize()
        self.assertTrue(self.plugin.allowed(Event()))
        self.assertIsNone(self.plugin.companion_api)

    async def test_bridge_registration_and_data_contract(self):
        self.config.update(enable_companion_bridge=True, proactive_targets=['adapter:FriendMessage:alice'])
        captured = []
        api = types.SimpleNamespace(register_proactive_ability=lambda spec: captured.append(spec) or True,
                                    unregister_proactive_ability=lambda name: captured.append(name))
        module = types.SimpleNamespace(get_private_companion_api=lambda: api)
        with patch.object(adapter.importlib, 'import_module', return_value=module):
            await self.plugin.initialize()
            await self.plugin.loaded()
        self.assertEqual(len(captured), 1)
        good = {'user': {'umo': 'adapter:FriendMessage:alice', 'history': 'private history must not be used'}}
        self.assertTrue(captured[0]['availability'](good))
        self.assertFalse(captured[0]['availability']({'user': {'umo': 'other:FriendMessage:bob'}}))
        self.plugin.service.recommend = AsyncMock(return_value=Video('BV1EW4y1W7Z9', 'Title', 'Uploader', 0))
        result = await captured[0]['executor'](good)
        self.assertTrue(result['ok'])
        self.assertIn('https://www.bilibili.com/video/', result['extra_components'][0].text)
        self.assertNotIn('private history', result['context'])
        self.plugin.service.recommend.assert_awaited_once_with('adapter:FriendMessage:alice', '', ('fake-inherited-key',), proactive=True)
        self.config['allowed_user_ids'] = ['alice']
        self.assertFalse(captured[0]['availability'](good))
        await self.plugin.terminate()
        self.assertEqual(captured[-1], 'bilibili_recommend')


if __name__ == '__main__':
    unittest.main()
