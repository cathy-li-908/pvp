# -*- encoding: utf-8 -*-
'''
pvp —— 【尽伏诛】PVP 青果（OlivOS）插件包入口。

命名约定：插件名 / namespace / 包名统一为 `pvp`（OlivOS 用目录名做模块名），
「尽伏诛」只是当前游戏名，可被骰主替换，所以不出现在标识符里。
内部模块保留 `jfz`（尽伏诛首字母）前缀 —— 写成 `pvp.pvpCore` 太拗口。

================================================================
【为什么要在这里集中 import 一遍】
================================================================
OlivOS 加载插件的顺序是「按 app.json 的 priority 从大到小 import 包」，
而包的 __init__.py 就是入口。本插件内部分成十来个模块，彼此会互相调用
（引擎要用存储、存储要用公共层、骰子桥接要用引擎……），如果不在这里
按依赖顺序 import 一遍，子模块里写 `import pvp` 时可能拿到一个
「还没装好零件」的半成品包，从而出现 AttributeError。

所以这里按「被依赖方在前、依赖方在后」的顺序全部 import 一遍：

    jfzCore     公共层（谁都不依赖）
    jfzStore    存储层（只依赖公共层）
    jfzSend     发送层（只依赖公共层）
    jfzDice     骰子桥接层（依赖公共层 / 存储层 / 引擎）
    jfzShop     商城与经济（依赖公共层 / 存储层）
    jfzEngine   对局引擎（依赖上面全部）
    jfzAdmin    管理指令（依赖公共层 / 存储层 / 引擎）
    jfzReply    指令路由（依赖上面全部）
    msgCustom*  回复词与帮助文档注入

最后才 import main：main 里会 `import pvp` 去调 jfzStore.initStorage()，
必须等上面这些子模块都挂到包上之后再执行。
'''

# 先确保外部依赖在包之前完成初始化。OlivaDiceCore 只依赖 OlivOS，
# 提前 import 可以避免「子模块里 import OlivaDiceCore 时它还没装好」的循环问题。
import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

from . import jfzCore  # NOQA: F401
from . import jfzStore  # NOQA: F401
from . import jfzSend  # NOQA: F401
from . import jfzShop  # NOQA: F401
from . import jfzDice  # NOQA: F401
from . import jfzEngine  # NOQA: F401
from . import jfzAdmin  # NOQA: F401
from . import jfzReply  # NOQA: F401
from . import msgCustom  # NOQA: F401
from . import msgCustomManager  # NOQA: F401

from . import main  # NOQA: F401
