"""音源平台层。

新增平台只需三步：
    1. 新建文件（或直接写在 meting.py 里），继承 BaseMusicPlayer / MetingPlayer；
    2. 声明 platform: ClassVar[Platform]（含 keywords）；
    3. 在本文件 import 并加入 __all__。
子类会被自动注册（见 base.BaseMusicPlayer.__init_subclass__），main.py 无需改动。
"""

from .base import BaseMusicPlayer
from .meting import MetingPlayer, NeteaseMeting, NeteaseWeb, TencentMeting

__all__ = [
    "BaseMusicPlayer",
    "MetingPlayer",
    "NeteaseMeting",
    "NeteaseWeb",
    "TencentMeting",
]
