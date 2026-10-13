import asyncio
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave
import tempfile

from bootstrap import install
install()
from astrbot_plugin_voice_music.core.sender import MusicSender

temporary = tempfile.TemporaryDirectory(prefix='voice_music_full_test_')
ROOT = Path(temporary.name).resolve()
audio = ROOT / 'test_330s.wav'
with wave.open(str(audio), 'wb') as stream:
    stream.setnchannels(1)
    stream.setsampwidth(2)
    stream.setframerate(8000)
    stream.writeframes(b'\0\0' * 8000 * 330)
original = hashlib.sha256(audio.read_bytes()).hexdigest()

class Bot:
    def __init__(self): self.calls = []; self.error = False
    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error: raise RuntimeError('simulated API failure')
        return {'message_id': 123}

class Event:
    def __init__(self, group='123'): self.bot = Bot(); self.group = group; self._has_send_oper = False
    def get_platform_name(self): return 'aiocqhttp'
    def get_group_id(self): return self.group
    def get_sender_id(self): return '456'
    async def send(self, message): raise AssertionError('Must not call WAV/base64 event.send')

class Downloader:
    async def download_audio_multi(self, *args, **kwargs): return audio

async def main():
    cfg = SimpleNamespace(get=lambda key, default=None: str(ROOT) if key == 'onebot_shared_audio_root' else default, max_payload_bytes=5242880)
    sender = MusicSender(cfg, Downloader())
    cleaned = []
    sender._defer_cleanup = lambda *paths: cleaned.extend(paths)
    player = SimpleNamespace(audio_url_candidates=lambda song: ['https://unused.invalid/audio'], audio_headers=lambda song: {})
    song = SimpleNamespace(display_name=lambda: 'test 330 seconds')
    for group in ['123', '']:
        ev = Event(group)
        assert await sender._record_local(ev, player, song) is None
        assert len(ev.bot.calls) == 1 and ev._has_send_oper
        action, params = ev.bot.calls[0]
        assert action == ('send_group_msg' if group else 'send_private_msg')
        assert params['group_id' if group else 'user_id'] == int(group or '456')
        assert params['message'] == [{'type': 'record', 'data': {'file': audio.as_uri()}}]
        assert len(json.dumps(params).encode()) < 1024
    assert hashlib.sha256(audio.read_bytes()).hexdigest() == original
    with wave.open(str(audio)) as stream:
        assert stream.getnframes() / stream.getframerate() == 330
    print('PASS: complete 330s audio preserved, one native record request < 1 KiB, group/private targets correct')
    ev = Event()
    assert await sender._record_link(ev, player, song) is None and len(ev.bot.calls) == 1
    print('PASS: legacy voice alias also uses complete shared audio')
    ev = Event()
    assert '不在' in await sender._record_onebot_shared(ev, player, song, str(ROOT / 'other'))
    assert not ev.bot.calls
    ev = Event('not-a-number')
    assert '目标' in await sender._record_local(ev, player, song) and not ev.bot.calls
    print('PASS: invalid shared path and destination rejected without sending')
    ev = Event()
    ev.bot.error = True
    assert 'simulated API failure' in await sender._record_local(ev, player, song)
    assert len(ev.bot.calls) == 1 and not ev._has_send_oper and audio in cleaned
    print('PASS: native send error reported, cleanup scheduled, no duplicate/fallback')
    print('FULL_VOICE_TESTS_PASSED; no QQ messages transmitted')

try:
    asyncio.run(main())
finally:
    temporary.cleanup()
