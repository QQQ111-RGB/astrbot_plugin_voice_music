import asyncio
import sys
import time
from types import SimpleNamespace

from bootstrap import install
install()
from astrbot_plugin_voice_music.main import VoiceMusicPlugin, _session_key
from astrbot_plugin_voice_music.core.utils import parse_index_and_modes
from astrbot.core.message.components import At, Plain


class Event:
    is_at_or_wake_command = True  # A wake flag alone must never authorize a group request.

    def __init__(self, text, user='A', room='room1', at='BOT', private=False):
        self.message_str = text
        self.text = text
        self.user = user
        self.unified_msg_origin = room
        self.at = at
        self.private = private
        self.llm = True
        self.stopped = False

    def get_self_id(self): return 'BOT'
    def get_sender_id(self): return self.user
    def is_private_chat(self): return self.private
    def get_messages(self):
        return ([At(qq=self.at)] if self.at else []) + [Plain(self.text)]
    def should_call_llm(self, value): self.llm = value
    def stop_event(self): self.stopped = True
    def plain_result(self, text): return text


class Player:
    async def fetch_songs(self, keyword, limit, extra):
        return [SimpleNamespace(duration=180000, display_name=lambda: keyword + '1'),
                SimpleNamespace(duration=240000, display_name=lambda: keyword + '2')]


class Sender:
    def __init__(self): self.calls = []
    async def send_song(self, event, player, song, modes=None):
        self.calls.append((event.user, event.unified_msg_origin, song.display_name(), modes))
        await asyncio.sleep(0)
        return True


def plugin():
    p = VoiceMusicPlugin.__new__(VoiceMusicPlugin)
    p.cfg = SimpleNamespace(selection_timeout=60, song_limit=5)
    p.context = SimpleNamespace(get_config=lambda **kwargs: {"wake_prefix": ["*"]})
    p._pending = {}
    p._send_lock = asyncio.Lock()
    p.sender = Sender()
    p.player = Player()
    p.get_player = lambda **kwargs: p.player
    return p


async def handle(p, ev):
    return [result async for result in p.on_song(ev)]


async def run():
    p = plugin()
    for at in [None, 'OTHER', 'all']:
        ev = Event('点歌 晴天', at=at)
        assert await handle(p, ev) == [] and ev.llm and not p._pending
    print('PASS 01: bare command / other mention / @all ignored')
    start = Event('点歌 晴天')
    assert await handle(p, start) and not start.llm and start.stopped
    key = _session_key(start)
    old = p._pending[key]
    print('PASS 02: explicit @bot search creates owned list')
    for text, at in [('你好', None), ('1', None), ('2 语音', None), ('你好', 'BOT'), ('1 今天怎么样', 'BOT')]:
        ev = Event(text, at=at)
        assert await handle(p, ev) == [] and ev.llm and not ev.stopped
        assert p._pending[key] is old and not p.sender.calls
    print('PASS 03: ordinary chat and bare selections untouched')
    for user, room in [('B', 'room1'), ('A', 'room2')]:
        ev = Event('1 语音', user=user, room=room)
        assert await handle(p, ev) == [] and ev.llm and not p.sender.calls
    print('PASS 04: other users and rooms cannot select')
    assert await handle(p, Event('99')) and p._pending[key] is old
    print('PASS 05: invalid index preserves own list')
    ev = Event('1 语音')
    await asyncio.gather(handle(p, ev), handle(p, Event('1 语音')))
    assert len(p.sender.calls) == 1 and p.sender.calls[0][3] == ['record_local']
    assert key not in p._pending and not ev.llm
    print('PASS 06: valid voice claimed once, no duplicate send')
    await handle(p, Event('点歌 新歌'))
    exp = p._pending[key]
    exp.expires = time.monotonic() - 1
    ev = Event('1')
    assert await handle(p, ev) == [] and ev.llm and key not in p._pending
    print('PASS 07: expired list cannot be reused')
    await handle(p, Event('点歌 新歌'))
    await handle(p, Event('取消点歌'))
    assert key not in p._pending
    print('PASS 08: explicit cancellation clears only own list')
    await handle(p, Event('点歌 A歌', user='A'))
    await handle(p, Event('点歌 B歌', user='B'))
    await handle(p, Event('2', user='B'))
    assert p.sender.calls[-1][2] == 'B歌2' and key in p._pending
    print('PASS 09: simultaneous users have independent candidates')
    await handle(p, Event('点歌 C歌', user='A'))
    await handle(p, Event('1', user='A'))
    assert p.sender.calls[-1][2] == 'C歌1'
    print('PASS 10: new explicit request replaces previous candidates')
    await handle(p, Event('点歌 私聊', at=None, private=True, room='private'))
    await handle(p, Event('2 语音', at=None, private=True, room='private'))
    assert p.sender.calls[-1][2] == '私聊2'
    print('PASS 11: explicit private chat remains usable')
    for alias in ['语音', '本地语音', '语音链接', 'record_link']:
        assert parse_index_and_modes('1 ' + alias)[1] == ['record_local']
    assert not hasattr(VoiceMusicPlugin, 'play_song_by_name')
    print('PASS 12: every voice alias local; history/LLM playback tool removed')
    gate = asyncio.Event()
    original = p.player.fetch_songs
    async def slow(keyword, limit, extra):
        if keyword == '慢歌': await gate.wait()
        return await original(keyword, limit, extra)
    p.player.fetch_songs = slow
    task = asyncio.create_task(handle(p, Event('点歌 慢歌')))
    await asyncio.sleep(0)
    await handle(p, Event('点歌 快歌'))
    gate.set()
    assert await task == []
    await handle(p, Event('1'))
    assert p.sender.calls[-1][2] == '快歌1'
    print('PASS 13: stale slow search never overwrites newer request')
    ev = Event('我们来聊聊QQ音乐')
    assert await handle(p, ev) == [] and ev.llm
    print('PASS 14: command words inside ordinary sentences ignored')
    p = plugin()
    ev = Event('*点歌 唤醒词歌曲', at=None)
    ev.message_str = '点歌 唤醒词歌曲'  # AstrBot strips the prefix in message_str only.
    assert await handle(p, ev) and not ev.llm
    old = p._pending[_session_key(ev)]
    await handle(p, Event('普通聊天', at=None))
    await handle(p, Event('1 语音', at=None))
    await handle(p, Event('*1 语音', user='B', at=None))
    assert p._pending[_session_key(ev)] is old and not p.sender.calls
    await handle(p, Event('*1 语音', at=None))
    assert len(p.sender.calls) == 1 and p.sender.calls[-1][3] == ['record_local']
    print('PASS 15: configured wake prefix searches/selects; chat and other users cannot select')
    await handle(p, Event('点歌 混合方式'))
    await handle(p, Event('*2 语音', at=None))
    await handle(p, Event('*/点歌 反向混合', at=None))
    await handle(p, Event('1 语音'))
    assert p.sender.calls[-1][2] == '反向混合1'
    print('PASS 16: mention and wake prefix interchangeable, optional slash accepted')
    await handle(p, Event('*点歌 取消测试', at=None))
    await handle(p, Event('*取消点歌', at=None))
    assert not p._pending
    for value in ['今天*点歌 不应触发', '**点歌 不应触发']:
        ev = Event(value, at=None)
        assert await handle(p, ev) == [] and ev.llm
    assert await handle(p, Event('*点歌 错误目标', at='OTHER')) == []
    print('PASS 17: wake cancellation; embedded prefix / wrong target ignored')
    p.context.get_config = lambda **kwargs: {'wake_prefix': ['机器人', '机器人你好', '', '  '] if kwargs['umo'] == 'room1' else ['!']}
    await handle(p, Event('机器人你好点歌 长前缀', at=None))
    await handle(p, Event('机器人1 语音', at=None))
    assert p.sender.calls[-1][2] == '长前缀1'
    assert await handle(p, Event('*点歌 旧前缀', at=None)) == []
    assert await handle(p, Event('点歌 空前缀', at=None)) == []
    assert await handle(p, Event('!点歌 群二', at=None, room='room2'))
    print('PASS 18: conversation-specific live config, longest prefix, empty prefix rejected')
    p = plugin()
    from astrbot_plugin_voice_music.main import _addressed, _request_text
    ev = Event('*音乐状态', at=None)
    prefixes = p._wake_prefixes(ev)
    assert _addressed(ev, prefixes) and _request_text(ev, prefixes) == '音乐状态'
    print('PASS 19: status commands use the same wake authorization')
    print('ALL 19 GROUPS PASSED; no QQ messages transmitted')


asyncio.run(run())
