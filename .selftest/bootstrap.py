"""Install minimal AstrBot stubs; tests never connect to a real QQ bot."""
import sys
import types
from pathlib import Path
from run_selftest import install_stubs

def install():
    install_stubs()
    package = types.ModuleType("astrbot_plugin_voice_music")
    package.__path__ = [str(Path(__file__).resolve().parents[1])]
    sys.modules[package.__name__] = package
