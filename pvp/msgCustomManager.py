# -*- encoding: utf-8 -*-
'''
自定义回复 / 帮助文档的注入器 —— 把 msgCustom.py 里的内容塞进 OlivaDiceCore。

================================================================
【这里的关键设计只有一条：只写「缺的东西」】
================================================================
所有注入都走 `if key not in 目标表:` 判断。

因为用户的修改是**存在青果自己的数据目录里**的：
  - 自定义回复 → data/<botHash>/customReply.json
  - 帮助文档   → data/<botHash>/helpDoc.json

青果启动时会先把这些文件读进来，然后才轮到插件的 init_after。
如果本插件无脑覆盖，用户改过的帮助文档会在每次重载插件时被悄悄还原 ——
那种「改完好像生效了，重启又变回去」的问题非常难查。
所以只在「表里根本没有这个 key」时才写入，等于「首次安装时铺一份默认值」。

写法与 OlivaDiceCore 生态里其它子插件（今日老婆、恋综等）完全一致。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

import pvp

# 有没有装青果的 GUI 插件。装了的话还要往它的「自定义回复备注」表里注入，
# 否则 GUI 里会显示成「无备注」。
has_NativeGUI = False
try:
    import OlivaDiceNativeGUI

    has_NativeGUI = True
except ImportError:
    has_NativeGUI = False


def initMsgCustom(bot_info_dict):
    """
    把本插件的自定义回复与帮助文档注入 OlivaDiceCore。
    在 main.Event.init_after 里调用（此时 OlivaDiceCore 已就绪）。
    """
    if not isinstance(bot_info_dict, dict):
        bot_info_dict = {}

    for botHash in bot_info_dict:
        # 自定义回复表：按 bot 分开，缺 key 才写
        if botHash not in OlivaDiceCore.msgCustom.dictStrCustomDict:
            OlivaDiceCore.msgCustom.dictStrCustomDict[botHash] = {}
        for key in pvp.msgCustom.dictStrCustom:
            if key not in OlivaDiceCore.msgCustom.dictStrCustomDict[botHash]:
                OlivaDiceCore.msgCustom.dictStrCustomDict[botHash][key] = \
                    pvp.msgCustom.dictStrCustom[key]

        # 帮助文档：同样按 bot 分开，缺 key 才写
        if botHash not in OlivaDiceCore.helpDocData.dictHelpDoc:
            OlivaDiceCore.helpDocData.dictHelpDoc[botHash] = {}
        for key in pvp.msgCustom.dictHelpDocTemp:
            if key not in OlivaDiceCore.helpDocData.dictHelpDoc[botHash]:
                OlivaDiceCore.helpDocData.dictHelpDoc[botHash][key] = \
                    pvp.msgCustom.dictHelpDocTemp[key]

        if has_NativeGUI:
            for key in pvp.msgCustom.dictStrCustomNote:
                if key not in OlivaDiceNativeGUI.msgCustom.dictStrCustomNote:
                    OlivaDiceNativeGUI.msgCustom.dictStrCustomNote[key] = \
                        pvp.msgCustom.dictStrCustomNote[key]

    # 全局常量表：这几张表是全插件共用的，直接 update
    OlivaDiceCore.msgCustom.dictStrConst.update(pvp.msgCustom.dictStrConst)
    OlivaDiceCore.msgCustom.dictGValue.update(pvp.msgCustom.dictGValue)
    OlivaDiceCore.msgCustom.dictTValue.update(pvp.msgCustom.dictTValue)

    if has_NativeGUI:
        OlivaDiceNativeGUI.msgCustom.dictStrCustomNote.update(pvp.msgCustom.dictStrCustomNote)

    print('[pvp] 自定义回复与帮助文档已注入（%d 个账号）。' % len(bot_info_dict))
