# -*- encoding: utf-8 -*-
'''
OlivOS 的最小可运行桩 —— 只实现 pvp 插件真正用到的那几个接口。

【为什么不是「随便糊一个」】
这些桩的签名和语义都是照着 /private/tmp/olivos_dev/OlivOS 的真实源码写的：
  - API.Event 的构造签名 (sdk_event, log_func, Proc)
  - API.Event.send(send_type, target_id, message, host_id, flag_log, remote)
  - contentAPI.fake_sdk_event(bot_info, data, platform, fakename, plugin_namespace, message_mode_tx)
    → 返回的对象带 .bot_info / .platform / .data.extend / .base_info
  - 假的 event 在真实实现里 `data` 是一个 fake_event 实例（只有 default 属性），
    插件自己往上面挂 extend；这里保持一致，才能验证「挂 extend 能不能成功」。

出站消息统一记到 OUTBOX 里，测试脚本可以断言「发给了谁、发了什么」。
'''

import sys
import types

# 所有出站消息（含主动推送）。元素：{'type': 'group', 'target': '123', 'text': '...'}
OUTBOX = []

# 收到的「当前会话回复」。元素：(botHash, 群ID, 用户ID, 文本)
REPLIES = []


# ---------------------------------------------------------------------------
# OlivOS.API
# ---------------------------------------------------------------------------

_API = types.ModuleType('OlivOS.API')


class Event(object):
    def __init__(self, sdk_event=None, log_func=None, Proc=None):
        self.sdk_event = sdk_event
        self.log_func = log_func
        self.Proc = Proc
        self.bot_info = sdk_event.bot_info if sdk_event is not None else None
        self.platform = dict(sdk_event.platform) if sdk_event is not None else {}
        self.base_info = dict(sdk_event.base_info) if sdk_event is not None else {}
        self.data = sdk_event.data if sdk_event is not None else None
        self.plugin_info = {
            'namespace': getattr(sdk_event, 'plugin_namespace', None),
            'message_mode_tx': getattr(sdk_event, 'message_mode_tx', None) or 'old_string',
            'func_type': 'fake_event' if sdk_event is not None else None,
        }
        self.active = True
        self.blocked = False

    def send(self, send_type, target_id, message, host_id=None, flag_log=True, remote=False):
        if remote:
            return None
        OUTBOX.append({'type': send_type, 'target': str(target_id), 'text': str(message)})
        return True

    def reply(self, message, flag_log=True, remote=False):
        if remote:
            return None
        groupId = str(getattr(self.data, 'group_id', '') or '')
        userId = str(getattr(self.data, 'user_id', '') or '')
        botHash = str(self.bot_info.hash) if self.bot_info is not None else ''
        REPLIES.append((botHash, groupId, userId, str(message)))
        return True

    def set_block(self):
        self.blocked = True


_API.Event = Event
sys.modules['OlivOS.API'] = _API


# ---------------------------------------------------------------------------
# OlivOS.pluginAPI
# ---------------------------------------------------------------------------

_PLUGIN_API = types.ModuleType('OlivOS.pluginAPI')
_PLUGIN_API.gProc = None


class shallow(object):
    def __init__(self, bot_info_dict=None):
        self.Proc_data = {'bot_info_dict': bot_info_dict or {}}
        self.log = lambda *args, **kwargs: None


_PLUGIN_API.shallow = shallow
sys.modules['OlivOS.pluginAPI'] = _PLUGIN_API


# ---------------------------------------------------------------------------
# OlivOS.contentAPI
# ---------------------------------------------------------------------------

_CONTENT_API = types.ModuleType('OlivOS.contentAPI')


class _FakeData(object):
    """对应真实实现里的 API.fake_event —— 初始只有一个 default 属性。"""

    def __init__(self):
        self.default = None


class fake_sdk_event(object):
    def __init__(self, bot_info, data=None, platform=None, fakename='unity',
                 plugin_namespace=None, message_mode_tx=None):
        self.fakename = fakename
        self.plugin_namespace = plugin_namespace
        self.message_mode_tx = message_mode_tx
        self.bot_info = bot_info
        self.platform = {'sdk': 'fake', 'platform': 'fake', 'model': 'default'}
        if isinstance(platform, dict):
            self.platform.update(platform)
        if bot_info is not None:
            self.platform.update(getattr(bot_info, 'platform', {}) or {})
        self.data = _FakeData()
        self.base_info = {'time': 0, 'self_id': getattr(bot_info, 'id', None), 'post_type': None}


_CONTENT_API.fake_sdk_event = fake_sdk_event
sys.modules['OlivOS.contentAPI'] = _CONTENT_API


# ---------------------------------------------------------------------------
# OlivOS.messageAPI / infoAPI
# ---------------------------------------------------------------------------

_MESSAGE_API = types.ModuleType('OlivOS.messageAPI')
_MESSAGE_API.dictMessageType = {}
sys.modules['OlivOS.messageAPI'] = _MESSAGE_API

_INFO_API = types.ModuleType('OlivOS.infoAPI')
_INFO_API.OlivOS_message_mode_rx_default = 'old_string'
_INFO_API.OlivOS_message_mode_tx_default = 'old_string'
sys.modules['OlivOS.infoAPI'] = _INFO_API


# ---------------------------------------------------------------------------
# 顶层
# ---------------------------------------------------------------------------

API = _API
pluginAPI = _PLUGIN_API
contentAPI = _CONTENT_API
messageAPI = _MESSAGE_API
infoAPI = _INFO_API


# ---------------------------------------------------------------------------
# 测试辅助（真实 OlivOS 里没有，仅供断言用）
# ---------------------------------------------------------------------------

def resetOutbox():
    del OUTBOX[:]
    del REPLIES[:]


def outboxTo(target):
    """取出所有发往某个群的消息文本。"""
    return [item['text'] for item in OUTBOX if str(item['target']) == str(target)]


def repliesTo(groupId):
    """取出所有「在某个群里回复当前会话」的文本。"""
    return [item[3] for item in REPLIES if str(item[1]) == str(groupId)]


# 测试用的账号信息
class FakeBotInfo(object):
    def __init__(self, botId='10001', botHash='HASH_A', sdk='onebot', platform='qq'):
        self.id = botId
        self.hash = botHash
        self.platform = {'sdk': sdk, 'platform': platform, 'model': 'default'}
        self.post_info = types.SimpleNamespace(port=6700, access_token='tok')


def makeGroupEvent(message, groupId, userId, botInfo=None, senderName='测试用户'):
    """
    造一个「群消息」plugin_event，用于直接调 main.Event.group_message。
    """
    botInfo = botInfo or FakeBotInfo()
    ev = Event.__new__(Event)
    ev.sdk_event = None
    ev.log_func = None
    ev.Proc = None
    ev.bot_info = botInfo
    ev.platform = {'sdk': botInfo.platform['sdk'], 'platform': botInfo.platform['platform'],
                   'model': botInfo.platform['model']}
    ev.base_info = {'self_id': botInfo.id}
    ev.data = types.SimpleNamespace(
        message=message,
        group_id=str(groupId),
        user_id=str(userId),
        host_id=None,
        sender={'name': senderName, 'id': str(userId)},
        extend={},
    )
    ev.plugin_info = {'namespace': 'pvp', 'message_mode_tx': 'old_string',
                      'func_type': 'group_message'}
    ev.active = True
    ev.blocked = False
    return ev
