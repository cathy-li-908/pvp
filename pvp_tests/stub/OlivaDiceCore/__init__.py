# -*- encoding: utf-8 -*-
'''
OlivaDiceCore 的桩。

================================================================
【关键的取舍：哪些用真的、哪些自己写】
================================================================
真正决定玩法结果的是「成功等级判定」—— 它决定了扣多少血。
所以这部分**直接用官方源码**，不自己重写：

  /private/tmp/odc/OlivaDiceCore/pcCardData.py    （纯数据，默认检定规则表）
  /private/tmp/odc/OlivaDiceCore/skillCheck.py    （判定算法）

其余部分（用户配置存取、钩子槽、msgReply 等）都是「接口」性质的，
本文件按真实源码的签名与语义手写，行为一致即可。
'''

import sys
import os
import types
import copy
import importlib.util

# ---------------------------------------------------------------------------
# 去哪里找 OlivaDiceCore 的真实源码
# ---------------------------------------------------------------------------
# 本桩只「借用」两个文件：默认检定规则表 pcCardData.py 与判定算法 skillCheck.py。
# 这两个文件决定了扣多少血，必须用真的，不能自己重写一份近似实现。
# 但它们是 OlivaDiceCore 的源码，不适合复制进本插件的交付物里，
# 所以改成「按顺序去找」：
#   1. 环境变量 JFZ_ODC_DIR（指向 OlivaDiceCore 目录）
#   2. 常见的 OlivOS 插件安装位置
#   3. 开发时用的本地副本
def _findRealDir():
    candidates = []
    envDir = os.environ.get('JFZ_ODC_DIR', '').strip()
    if envDir:
        candidates.append(envDir)
    home = os.path.expanduser('~')
    candidates += [
        os.path.join(home, 'OlivOS', 'plugin', 'OlivaDiceCore'),
        os.path.join(home, 'olivos', 'plugin', 'OlivaDiceCore'),
        '/opt/OlivOS/plugin/OlivaDiceCore',
        '/private/tmp/odc/OlivaDiceCore',
    ]
    for path in candidates:
        if os.path.isfile(os.path.join(path, 'skillCheck.py')):
            return path
    raise RuntimeError(
        '找不到 OlivaDiceCore 的真实源码目录（需要里面的 skillCheck.py 与 pcCardData.py）。\n'
        '请设置环境变量 JFZ_ODC_DIR 指向 OlivaDiceCore 目录后重试，例如：\n'
        '  export JFZ_ODC_DIR=/path/to/OlivOS/plugin/OlivaDiceCore\n'
        '已尝试过：\n  ' + '\n  '.join(candidates)
    )


REAL_DIR = _findRealDir()


def _loadReal(name, fileName):
    spec = importlib.util.spec_from_file_location('OlivaDiceCore.' + name, REAL_DIR + '/' + fileName)
    module = importlib.util.module_from_spec(spec)
    sys.modules['OlivaDiceCore.' + name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 真实模块：默认检定规则表 + 判定算法
# ---------------------------------------------------------------------------
pcCardData = _loadReal('pcCardData', 'pcCardData.py')
# 真实运行时这一步由 pcCard.loadTemplate() 完成（pcCard.py:50-51），这里手动补上
pcCardData.dictPcCardTemplateDefault = copy.deepcopy(pcCardData.dictPcCardTemplateDefaultTemp)

skillCheck = _loadReal('skillCheck', 'skillCheck.py')


# ---------------------------------------------------------------------------
# 钩子槽
# ---------------------------------------------------------------------------
crossHook = types.ModuleType('OlivaDiceCore.crossHook')
crossHook.dictHookList = {
    'model': [],
    'prefix': ['.', '。', '/'],
    'replyContextFliter': [],
    'replyContextPrefixFliter': [],
    'replyContextReg': {},
}


def _defaultMsgHook(event, funcType, sender, dectData, message):
    return None


crossHook.dictHookFunc = {
    'msgHook': _defaultMsgHook,
    'pokeHook': lambda *a, **k: None,
    'msgFormatHook': lambda data, valDict: data,
    'drawFormatHook': lambda data, ev: data,
}
sys.modules['OlivaDiceCore.crossHook'] = crossHook


# ---------------------------------------------------------------------------
# 用户配置（RDRecordInt / RDRecordSkillInt 存这里）
# ---------------------------------------------------------------------------
userConfig = types.ModuleType('OlivaDiceCore.userConfig')

# {(botHash, userHash): {configKey: value}}
_USER_CONFIG = {}


def getUserHash(userId, userType, platform, subId=None):
    return '%s|%s|%s' % (userType, platform, userId)


def getUserConfigByKeyWithHash(userHash, userConfigKey, botHash, forceNoRedirect=False):
    return _USER_CONFIG.get((botHash, userHash), {}).get(userConfigKey)


def setUserConfigByKey(userId, userType, platform, userConfigKey, userConfigValue,
                       botHash, forceNoRedirect=False):
    userHash = getUserHash(userId, userType, platform)
    _USER_CONFIG.setdefault((botHash, userHash), {})[userConfigKey] = userConfigValue


def writeUserConfigByUserHash(userHash):  # 真实实现里是落盘，桩里不用管
    return None


userConfig.getUserHash = getUserHash
userConfig.getUserConfigByKeyWithHash = getUserConfigByKeyWithHash
userConfig.setUserConfigByKey = setUserConfigByKey
userConfig.writeUserConfigByUserHash = writeUserConfigByUserHash
sys.modules['OlivaDiceCore.userConfig'] = userConfig


# ---------------------------------------------------------------------------
# 掷骰记录的读取入口
# ---------------------------------------------------------------------------
onediceOverride = types.ModuleType('OlivaDiceCore.onediceOverride')


def getRDDataIntUser(botHash, userId, platform):
    userHash = getUserHash(userId=userId, userType='user', platform=platform)
    return getUserConfigByKeyWithHash(userHash=userHash, userConfigKey='RDRecordInt', botHash=botHash)


def setRDRecord(botHash, userId, platform, rollValue, skillValue):
    """测试辅助：模拟青果掷了一次骰，写入 RDRecordInt / RDRecordSkillInt。"""
    userHash = getUserHash(userId=userId, userType='user', platform=platform)
    bucket = _USER_CONFIG.setdefault((botHash, userHash), {})
    bucket['RDRecordInt'] = rollValue
    bucket['RDRecordSkillInt'] = skillValue


onediceOverride.getRDDataIntUser = getRDDataIntUser
onediceOverride.setRDRecord = setRDRecord
onediceOverride.getRDDataIntUserByHash = lambda botHash, userHash: getUserConfigByKeyWithHash(
    userHash=userHash, userConfigKey='RDRecordInt', botHash=botHash
)
sys.modules['OlivaDiceCore.onediceOverride'] = onediceOverride


# ---------------------------------------------------------------------------
# 骰主名单
# ---------------------------------------------------------------------------
ordinaryInviteManager = types.ModuleType('OlivaDiceCore.ordinaryInviteManager')

# 测试里可以直接往这里塞 userHash
MASTER_LIST = set()


def isInMasterList(bot_hash, user_hash):
    return user_hash in MASTER_LIST


ordinaryInviteManager.MASTER_LIST = MASTER_LIST
ordinaryInviteManager.isInMasterList = isInMasterList
sys.modules['OlivaDiceCore.ordinaryInviteManager'] = ordinaryInviteManager


# ---------------------------------------------------------------------------
# msgReply：指令前缀剥离 + 回复
# ---------------------------------------------------------------------------
msgReply = types.ModuleType('OlivaDiceCore.msgReply')


def skipSpaceStart(data):
    """照抄真实实现（msgReply.py:7839）。"""
    tmp_output_str = ''
    if len(data) > 0:
        flag_have_para = False
        tmp_offset = 0
        tmp_total_offset = 0
        while True:
            tmp_offset += 1
            tmp_total_offset = tmp_offset - 1
            if tmp_total_offset >= len(data):
                break
            if data[tmp_total_offset] != ' ':
                flag_have_para = True
                break
        if flag_have_para:
            tmp_output_str = data[tmp_total_offset:]
    return tmp_output_str


def msgIsCommand(data, prefix_list):
    """照抄真实实现（msgReply.py:7825）。"""
    res = False
    res_data = data
    if type(data) is str:
        command_data = skipSpaceStart(data)
        for prefix_list_this in prefix_list:
            if isMatchWordStart(command_data, prefix_list_this):
                res_data = getMatchWordStartRight(command_data, prefix_list_this)
                res = True
                break
    return [res_data, res]


def getMatchWordStartRight(data, key, ignoreCase=True):
    """照抄真实实现（msgReply.py:8216）。"""
    tmp_output_str = ''
    tmp_data = data
    tmp_keys = [key] if isinstance(key, str) else key
    if ignoreCase:
        tmp_data = tmp_data.lower()
        tmp_keys = [k.lower() for k in tmp_keys]
    tmp_keys_sorted = sorted(tmp_keys, key=lambda x: len(x), reverse=True)
    for tmp_key in tmp_keys_sorted:
        if len(tmp_data) > len(tmp_key):
            if tmp_data[: len(tmp_key)] == tmp_key:
                tmp_output_str = data[len(tmp_key):]
                break
    return tmp_output_str


def isMatchWordStart(data, key, ignoreCase=True, fullMatch=False, isCommand=False):
    """照抄真实实现（msgReply.py:8187）。"""
    tmp_output = False
    flag_skip = False
    tmp_data = data.strip()
    tmp_keys = [key] if isinstance(key, str) else key
    if isCommand:
        if 'replyContextFliter' in crossHook.dictHookList:
            for k in tmp_keys:
                if k in crossHook.dictHookList['replyContextFliter']:
                    tmp_output = False
                    flag_skip = True
                    break
    if not flag_skip:
        if ignoreCase:
            tmp_data = tmp_data.lower()
            tmp_keys = [k.lower() for k in tmp_keys]
        tmp_keys_sorted = sorted(tmp_keys, key=lambda x: len(x), reverse=True)
        for tmp_key in tmp_keys_sorted:
            if not fullMatch and len(tmp_data) >= len(tmp_key):
                if tmp_data[: len(tmp_key)] == tmp_key:
                    tmp_output = True
                    break
            elif fullMatch and tmp_data == tmp_key:
                tmp_output = True
                break
    return tmp_output


def replyMsg(plugin_event, message, at_user=False):
    """
    真实实现会先触发 msgHook('reply') 再把消息发出去（msgReply.py:7372-7397）。
    桩里把这两步拆开：
      - 发消息 → 记进 OlivOS.REPLIES（用真实 event 的 reply）
      - 触发钩子 → 由测试脚本显式模拟（见 fireDiceReply），
        这样「掷骰回复」和「普通回复」能分开控制。
    """
    return plugin_event.reply(str(message))


msgReply.skipSpaceStart = skipSpaceStart
msgReply.msgIsCommand = msgIsCommand
msgReply.getMatchWordStartRight = getMatchWordStartRight
msgReply.isMatchWordStart = isMatchWordStart
msgReply.replyMsg = replyMsg
sys.modules['OlivaDiceCore.msgReply'] = msgReply


# ---------------------------------------------------------------------------
# 其余空壳
# ---------------------------------------------------------------------------
msgCustom = types.ModuleType('OlivaDiceCore.msgCustom')
msgCustom.dictStrCustomDict = {}
msgCustom.dictStrConst = {}
msgCustom.dictGValue = {}
msgCustom.dictTValue = {}
sys.modules['OlivaDiceCore.msgCustom'] = msgCustom

helpDocData = types.ModuleType('OlivaDiceCore.helpDocData')
helpDocData.dictHelpDoc = {}
sys.modules['OlivaDiceCore.helpDocData'] = helpDocData

data = types.ModuleType('OlivaDiceCore.data')
data.OlivaDiceCore_ver_short = '3.4.84'
data.dataDirRoot = '/tmp/jfztest/work/data'
sys.modules['OlivaDiceCore.data'] = data
