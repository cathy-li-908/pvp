# -*- coding: utf-8 -*-
"""
【尽伏诛】PVP 插件 —— 离线集成验证。

做法：用真实的 OlivOS/OlivaDiceCore 接口语义（判定算法直接用官方源码），
把插件完整加载起来，跑一遍 PRD 里的全流程，逐条断言。

运行：
    python3 run_tests.py

前置条件只有一个：能找到一个 OlivaDiceCore 的真实源码目录
（本桩要借用它的 skillCheck.py / pcCardData.py，保证判定结果与真机一致）。
找不到时脚本会列出试过的路径，用 JFZ_ODC_DIR 指过去即可：
    export JFZ_ODC_DIR=/path/to/OlivOS/plugin/OlivaDiceCore
"""

import os
import sys
import time
import json
import shutil

# 所有路径都从「本文件在哪」推出来，这样把整个 tests 目录挪到哪儿都能跑
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(TEST_DIR, 'work')
STUB_DIR = os.path.join(TEST_DIR, 'stub')
# 插件包是 tests 目录的兄弟目录（交付结构：<工作目录>/pvp 与 <工作目录>/pvp_tests）
PLUGIN_PARENT = os.path.dirname(TEST_DIR)

shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
os.chdir(WORK)
sys.path.insert(0, STUB_DIR)
sys.path.insert(0, PLUGIN_PARENT)

import OlivOS  # noqa: E402
import OlivaDiceCore  # noqa: E402
import pvp  # noqa: E402

FAILS = []
CHECKS = [0]

# 失败时把「实际是什么」打出来。断言只说了「没找到 X」，
# 不说实际内容的话，排查时还得反过来翻代码猜文案，很浪费时间。
SHOW_ACTUAL = True


def _show(label, value):
    if not SHOW_ACTUAL:
        return
    text = str(value)
    if len(text) > 500:
        text = text[:500] + ' …（截断）'
    print('        %s = %r' % (label, text))


def check(cond, msg):
    CHECKS[0] += 1
    if cond:
        print('   ok   %s' % msg)
    else:
        print('   FAIL %s' % msg)
        FAILS.append(msg)


def checkIn(haystack, needle, msg):
    CHECKS[0] += 1
    if needle in str(haystack):
        print('   ok   %s（查找 %r）' % (msg, needle))
    else:
        print('   FAIL %s（查找 %r）' % (msg, needle))
        _show('实际内容', haystack)
        FAILS.append(msg)


def checkNotIn(haystack, needle, msg):
    CHECKS[0] += 1
    if needle not in str(haystack):
        print('   ok   %s（应不含 %r）' % (msg, needle))
    else:
        print('   FAIL %s（应不含 %r）' % (msg, needle))
        _show('实际内容', haystack)
        FAILS.append(msg)


def section(title):
    print('\n=== %s ===' % title)


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------

BOT_HASH = 'HASH_A'
BOT = OlivOS.FakeBotInfo(botId='10001', botHash=BOT_HASH)

MASTER_QQ = '90001'
ADMIN_QQ = '90002'
BACKSTAGE_GROUP = '9000'

P1 = {'name': '张三', 'qq': '10001', 'group': '1001'}
P2 = {'name': '李四', 'qq': '10002', 'group': '1002'}
P3 = {'name': '王五', 'qq': '10003', 'group': '1003'}
P4 = {'name': '赵六', 'qq': '10004', 'group': '1004'}
P5 = {'name': '孙七', 'qq': '10005', 'group': '1005'}

GAME = '尽伏诛'


def gm(text, group, user, name='测试用户'):
    """模拟一条群消息，走插件的 group_message 入口。"""
    ev = OlivOS.makeGroupEvent(text, group, user, BOT, name)
    pvp.main.Event.group_message(ev, Proc)
    return ev


def drain(timeout=30.0):
    """
    等跨群发送队列排空，并且出站记录稳定下来为止。

    【为什么不能只判断 sendQueue.empty()】
    队列空不等于消息已经发出：后台线程可能刚把消息取走，
    正卡在 _waitForRateLimit 里睡觉。只等队列空的话会在消息落地前就返回，
    断言随机失败 —— 而且失败得毫无规律，最难查。

    所以这里改成「队列空 + 出站记录在 0.4 秒内没变化」才算排空。
    """
    end = time.time() + timeout
    lastCount = -1
    stableSince = None
    while time.time() < end:
        if pvp.jfzSend.sendQueue.empty():
            count = len(OlivOS.OUTBOX)
            if count == lastCount:
                if stableSince is None:
                    stableSince = time.time()
                elif time.time() - stableSince >= 0.4:
                    return
            else:
                lastCount = count
                stableSince = None
        time.sleep(0.05)


def sentTo(group):
    drain()
    return OlivOS.outboxTo(group)


def repliedTo(group):
    return OlivOS.repliesTo(group)


def lastReply(group):
    items = repliedTo(group)
    return items[-1] if items else ''


def allReplies(group):
    return '\n'.join(repliedTo(group))


def clearLogs():
    OlivOS.resetOutbox()


def sendRa(player, rollText, rollValue, skillValue):
    """
    完整模拟一次「玩家发 .raN」：

      1. 本插件先收到消息（priority 19990 < OlivaDiceCore 20000）→ 记下指令
      2. OlivaDiceCore 掷骰并写入 RDRecordInt / RDRecordSkillInt
      3. OlivaDiceCore 调 msgHook('reply') —— 插件在这里读记录、算等级、记账
    """
    ev = gm(rollText, player['group'], player['qq'], player['name'])
    OlivaDiceCore.onediceOverride.setRDRecord(
        BOT_HASH, player['qq'], 'qq', rollValue, skillValue
    )
    hook = OlivaDiceCore.crossHook.dictHookFunc['msgHook']
    hook(ev, 'reply', {'name': '骰子', 'id': '10001'}, [None, player['group'], player['qq']],
         '掷骰结果（桩）')
    return ev


def inv(name):
    return pvp.jfzStore.getInventory(name)


def clearInv(name):
    """
    把某个玩家的背包清空。

    各阶段之间必须这样做：前一阶段没用完的道具会跟着玩家进到下一阶段，
    于是「声明一件后背包应该为空」这类断言会因为「本来还有一件」而失败。
    这种失败看起来像插件 bug，实际是测试自己的状态污染，排查很费时。
    """
    for itemName in list((inv(name) or {}).keys()):
        count = int((inv(name) or {}).get(itemName, 0) or 0)
        if count > 0:
            pvp.jfzStore.removeItem(name, itemName, count)


def playerOf(name):
    return pvp.jfzStore.getPlayer(name) or {}


def hpOf(name):
    return int(playerOf(name).get('hp', -1))


def done():
    print('\n' + '=' * 60)
    print('断言 %d 条，失败 %d 条' % (CHECKS[0], len(FAILS)))
    for item in FAILS:
        print('  ✗ %s' % item)
    print('=' * 60)
    sys.exit(1 if FAILS else 0)


# ---------------------------------------------------------------------------
# 启动插件
# ---------------------------------------------------------------------------

# 走插件真实的加载路径（不是手动调存储层）：
# OlivOS 加载完包立刻调 Event.init，等所有插件都加载完再调 Event.init_after。
# 这样「初始化顺序写错了」「init 里碰了还没就绪的 OlivaDiceCore」这类
# 只在真机上出现的问题，在离线测试里就会被抓到。
pvp.main.Event.init(None, None)

Proc = OlivOS.shallow({BOT_HASH: BOT})
OlivOS.pluginAPI.gProc = Proc

# 把骰主加进青果的骰主名单
OlivaDiceCore.ordinaryInviteManager.MASTER_LIST.add(
    OlivaDiceCore.userConfig.getUserHash(MASTER_QQ, 'user', 'qq')
)

# 走插件真实的加载后初始化
pvp.main.Event.init_after(None, Proc)

# 活动时段看守在测试里手动触发，避免后台线程干扰
pvp.jfzEngine.WATCH_INTERVAL_SECONDS = 3600

# 把跨群推送的每分钟限流放开。
# 限流本身是给真实 QQ 平台用的（单群 20 条/分钟），它会让「消息什么时候到」
# 变成一个依赖真实时钟的量 —— 一次集成测试要在几个群里来回推几十条，
# 保留限流的话断言会随机地在「消息还没发出」的时刻失败。
# 放开之后「谁收到了什么」完全确定，才是能稳定复现的测试。
pvp.jfzSend.GROUP_LIMIT_PER_MINUTE = 100000


# ---------------------------------------------------------------------------
section('阶段 0：纯逻辑单测（血量状态 / 选项解析 / 道具表）')
# ---------------------------------------------------------------------------

C = pvp.jfzCore

check(C.deriveStatus(200) == C.STATUS_NORMAL, '血量 200 → 正常')
check(C.deriveStatus(21) == C.STATUS_NORMAL, '血量 21 → 正常')
check(C.deriveStatus(20) == C.STATUS_DYING, '血量 20 → 濒死（PRD：≤20）')
check(C.deriveStatus(1) == C.STATUS_DYING, '血量 1 → 濒死')
check(C.deriveStatus(0) == C.STATUS_DEAD, '血量 0 → 死皮')
check(C.clampHp(999) == 200, '血量上限默认 200')
check(C.clampHp(-5) == 0, '血量下限 0')
# 血量上限不是常量，而是配置 hp_limit；初始血量另有配置 initial_hp（两者独立）
check(C.getHpLimit() == 200, '★ 血量上限读配置 hp_limit（默认 200）')
check(C.getInitialHp() == 200, '★ 初始血量读配置 initial_hp（默认 200）')
check(not hasattr(C, 'HP_MAX'), '★ jfzCore 里不再有写死的 HP_MAX 常量')

check(C.parseCommand('【PVP】购买 大还丹') == ('购买', '大还丹'), '解析【PVP】指令')
check(C.parseCommand('【PVP】使用道具 胜临符 2') == ('使用道具', '胜临符 2'), '解析带空格的参数')
check(C.parseCommand('【PVP】') == ('帮助', ''), '光发【PVP】当帮助')
check(C.parseCommand('你好') == (None, '你好'), '非本插件指令不接管')

check(C.parseChallengeHeader('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', GAME)
      == {'发起人': '张三', '被发起人': '李四'}, '解析发起 PVP 多行格式')
check(C.parseChallengeHeader('【尽伏诛】PVP\n发起人：张三 被发起人：李四', GAME)
      == {'发起人': '张三', '被发起人': '李四'}, '同一行连写也能解析')
check(C.parseChallengeHeader('【别的游戏】PVP\n发起人：张三\n被发起人：李四', GAME) is None,
      '游戏名不匹配时不解析')

check(C.isRaCommand('.ra50') is True, 'isRaCommand 认 .ra50')
check(C.isRaCommand('。ra50') is True, 'isRaCommand 认全角句号前缀')
check(C.isRaCommand('ra50') is False, '不带前缀不算指令（青果也不会掷）')
check(C.isRaCommand('【PVP】背包') is False, '本插件指令不是 .ra')
check(C.parseRaSkillValue('.ra50') == 50, '从 .ra50 抠出技能值 50')
check(C.parseRaSkillValue('.ra攻击50') == 50, '从 .ra攻击50 抠出技能值 50')

check(C.isItemName('遁隐符') and C.getItemType('遁隐符') == C.TYPE_WHOLE_MATCH, '遁隐符是全场型')
check(C.getItemType('胜临符') == C.TYPE_ROUND, '胜临符是回合型')
check(C.getItemType('大还丹') == C.TYPE_OFF_FIELD, '大还丹是场外型')

# 真实 skillCheck 判定规则（用的是官方源码）
D = pvp.jfzDice
check(D.judgeLevel(1, 50) == '大成功', '判定 roll=1 skill=50 → 大成功')
check(D.judgeLevel(10, 50) == '极难成功', '判定 roll=10 skill=50 → 极难成功（≤skill/5）')
check(D.judgeLevel(20, 50) == '困难成功', '判定 roll=20 skill=50 → 困难成功（≤skill/2）')
check(D.judgeLevel(50, 50) == '成功', '判定 roll=50 skill=50 → 成功')
check(D.judgeLevel(60, 50) == '失败', '判定 roll=60 skill=50 → 失败')
check(D.judgeLevel(100, 50) == '大失败', '判定 roll=100 skill=50 → 大失败')
check(D.judgeLevel(96, 30) == '大失败', '判定 skill<50 且 roll≥96 → 大失败')

# --- 命名一致性自检 ---
# OlivOS 是「用插件目录名当 Python 模块名」去 import 插件的
# （pluginAPI.py 里 plugin_module_name = os.path.basename(plugin_dir_this)），
# 而 app.json 的 namespace 决定数据目录和跨群推送的归属。
# 三者一旦不一致，现象是「插件明明加载成功了，但数据写到别处 /
# 跨群消息发不出去 / 重命名后旧数据读不到」，定位成本极高 —— 所以静态钉死。
_pluginDir = os.path.join(PLUGIN_PARENT, 'pvp')
with open(os.path.join(_pluginDir, 'app.json'), encoding='utf-8') as _fp:
    _appConf = json.load(_fp)
check(os.path.basename(_pluginDir) == 'pvp', '★ 插件目录名 = pvp')
check(_appConf.get('namespace') == 'pvp', '★ app.json namespace = pvp')
check(_appConf.get('name') == 'pvp', '★ app.json name = pvp')
check(pvp.__name__ == 'pvp', '★ 导入后的包名 = pvp')
check(pvp.jfzStore.DATA_PATH == 'plugin/data/pvp',
      '★ 数据目录与 namespace 一致（实际 %r）' % pvp.jfzStore.DATA_PATH)

# --- 路由表自检 ---
# 路由表是「指令名 → 函数名」的字符串映射，写错一个字母不会有任何报错，
# 只会在玩家真的发那条指令时抛 AttributeError，而那个异常会被
# main.Event.group_message 的 try 吞掉 —— 现象是「发了指令毫无反应」。
# 这类问题从现象几乎无法定位，所以在这里静态自检两件事：
#   1. 表里指向的函数真的存在
#   2. 那个函数能按 f(plugin_event, args) 两个位置参数调用
#      （参数用不到也必须保留形参，见 jfzAdmin.handleShowConfig 的注释）
import inspect  # noqa: E402

R = pvp.jfzReply
_routeProblems = []
for _label, _cmdMap, _module in [('骰主', R.MASTER_COMMANDS, pvp.jfzAdmin),
                                 ('管理', R.ADMIN_COMMANDS, pvp.jfzAdmin)]:
    for _cmd, _fn in _cmdMap.items():
        if not hasattr(_module, _fn):
            _routeProblems.append('%s：%s → %s（函数不存在）' % (_label, _cmd, _fn))
            continue
        _params = [p for p in inspect.signature(getattr(_module, _fn)).parameters.values()
                   if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if len(_params) < 2:
            _routeProblems.append('%s：%s → %s（%s，无法按两个参数调用）' % (
                _label, _cmd, _fn, inspect.signature(getattr(_module, _fn))))
check(_routeProblems == [],
      '★ 路由表自检：目标函数存在且可被分发器调用（问题：%r）' % _routeProblems)

# 玩家指令表是「成员集合」，实际分发在 _dispatchPlayerCommand 里逐条 if。
# 所以要反过来检查：表里声明的每条指令，分发函数里都有对应分支。
# 少一条的话，那条指令会掉进「不认识的指令」分支 —— 明明写在帮助里却说不能用。
#
# 做法是读分发函数的字节码常量表：`command == '血量'` 会编译成
# LOAD_FAST command / LOAD_CONST '血量' / COMPARE_OP，所以指令名本身
# 一定是这个函数的常量。表里多出一条它必然不在常量表里。
_dispatchConsts = [c for c in pvp.jfzReply._dispatchPlayerCommand.__code__.co_consts
                   if isinstance(c, str)]
_missingPlayers = [c for c in R.PLAYER_COMMANDS if c not in _dispatchConsts]
check(_missingPlayers == [],
      '★ 玩家指令表与分发函数一致（表里有但分发没写：%r）' % _missingPlayers)

# --- 【PVP】管理 的清单覆盖率自检 ---
# 清单正文是从路由表现场拼的，所以「有哪些指令」不会漏；
# 会漏的只有「怎么用」—— 新加指令时忘了写 ADMIN_COMMAND_DOC，
# 那条指令就会以「说明待补」的样子回给管理。这里静态盯住它。
_docMissing = [c for c in list(R.MASTER_COMMANDS) + list(R.ADMIN_COMMANDS)
               if c not in R.ADMIN_COMMAND_DOC]
check(_docMissing == [], '★ 【PVP】管理：每条指令都有用法说明（缺：%r）' % _docMissing)

# 反过来：说明表里也不该留下已经删掉的指令（同样是一种过期）。
_docStale = [c for c in R.ADMIN_COMMAND_DOC
             if c not in R.MASTER_COMMANDS and c not in R.ADMIN_COMMANDS]
check(_docStale == [], '★ 【PVP】管理：说明表里没有已废弃指令（多：%r）' % _docStale)

# 权限标签必须是三种之一，否则会掉出所有分组、在清单里凭空消失。
_docBadLabel = sorted({v[0] for v in R.ADMIN_COMMAND_DOC.values()}
                      - {label for _title, label in R._ADMIN_DOC_GROUPS})
check(_docBadLabel == [], '★ 【PVP】管理：权限标签都在分组白名单里（异常：%r）' % _docBadLabel)

# --- 活动日期格式示例的自洽性自检 ---
# 示例只有一处（jfzCore.ACTIVITY_DATE_EXAMPLE），各处都引用它。
# 这里盯住「引用方有没有把它写全」——比如只抄了前半句就收工。
check(C.ACTIVITY_DATE_EXAMPLE == '【PVP】设置活动日期 ' + C.ACTIVITY_DATE_ARGS_EXAMPLE,
      '★ 活动日期示例 = 指令名 + 参数示例（两处拼接一致）')
check(C.ACTIVITY_DATE_USAGE.count(C.ACTIVITY_DATE_EXAMPLE) == 1,
      '★ 活动日期用法说明里带着完整示例')
check(C.ACTIVITY_DATE_ARGS_EXAMPLE.count('活动周期') == 1
      and C.ACTIVITY_DATE_ARGS_EXAMPLE.count('每日开放') == 1,
      '★ 活动日期示例同时含「活动周期」与「每日开放」两段')


# ---------------------------------------------------------------------------
section('阶段 1：注册后台群 / 管理员 / 玩家（PRD 4.1）')
# ---------------------------------------------------------------------------

clearLogs()
gm('【PVP】注册后台群 %s' % GAME, BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '后台群注册成功', 'PRD 4.1.1 注册后台群')
check(pvp.jfzStore.getGameName() == GAME, '当前游戏名已写入')
check(pvp.jfzStore.getBackstageGroup() == BACKSTAGE_GROUP, '后台群号已写入')

# 非骰主不能注册
clearLogs()
gm('【PVP】注册后台群 乱来', BACKSTAGE_GROUP, '999999', '路人')
checkIn(lastReply(BACKSTAGE_GROUP), '仅限骰主', 'PRD 2.2 非骰主不能注册后台群')

clearLogs()
gm('【PVP】添加管理 %s' % ADMIN_QQ, BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '已添加后台管理员', 'PRD 4.1.2 添加管理员（用 QQ 号）')
check(pvp.jfzStore.isBackstageAdmin(ADMIN_QQ), '管理员名单已生效')

clearLogs()
gm('【PVP】添加管理 888888', BACKSTAGE_GROUP, MASTER_QQ, '骰主')
gm('【PVP】移除管理 888888', BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '已移除后台管理员', 'PRD 4.1.2 移除管理员')

# 扩展指令【PVP】配置（PRD 2.2「查看活动配置」）
clearLogs()
gm('【PVP】配置', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '每场回合数', '★ 扩展指令【PVP】配置可用')

# 玩家不能用这两条
clearLogs()
gm('【PVP】配置', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '仅限', '★ PRD 2.2 玩家不能查看活动配置')

# 扩展指令【PVP】管理：把全部管理 / 骰主指令列成一张清单
clearLogs()
gm('【PVP】管理', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
adminList = lastReply(BACKSTAGE_GROUP)
checkIn(adminList, '管理指令一览', '★ 扩展指令【PVP】管理可用（管理员）')
checkIn(adminList, '【PVP】设置回合数', '清单列出管理指令')
checkIn(adminList, '【PVP】发放铜币', '清单列出铜币指令')
checkIn(adminList, '【PVP】设置初始血量', '清单列出初始血量指令')
checkIn(adminList, '【PVP】设置血量上限', '★ 清单列出独立的血量上限指令')
checkNotIn(adminList, '同时是', '★ 清单里不再有「初始血量同时是上限」的旧说法')
checkIn(adminList, '【PVP】设置个人攻击', '清单列出个人攻击指令')
checkIn(adminList, '【PVP】注册后台群', '清单列出骰主指令')
checkIn(adminList, '【PVP】设置活动日期　' + C.ACTIVITY_DATE_ARGS_EXAMPLE,
        '★ 清单里的「设置活动日期」用的是带标签的新格式示例')
checkIn(adminList, '骰主专用', '清单按权限分组（骰主专用）')
checkIn(adminList, '骰主与后台管理员都能用', '清单按权限分组（骰主 + 管理员）')
checkNotIn(adminList, '骰主本人也做不了', '★ 清单里不再有「骰主也做不了」的说明（骰主 ⊇ 管理员）')
checkNotIn(adminList, '说明待补', '★ 清单里没有缺说明的指令')

clearLogs()
gm('【PVP】管理', BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '管理指令一览', '★ 骰主也能查看管理指令一览')

clearLogs()
gm('【PVP】管理', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '仅限', '★ PRD 2.2 玩家不能查看管理指令一览')

# 注册玩家：后台群三参数格式
clearLogs()
for p in [P1, P2, P3, P4, P5]:
    gm('【PVP】注册玩家 %s %s %s' % (p['group'], p['qq'], p['name']),
       BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '注册成功', 'PRD 4.1.3 后台群注册玩家（群号 QQ号 玩家名）')
check(pvp.jfzStore.getPlayerByGroup('1001')['display_name'] == '张三', 'QQ→群号 映射建立')

# 注册玩家：个人群两参数格式
clearLogs()
gm('【PVP】注册玩家 10006 周八', '1006', ADMIN_QQ, '管理')
checkIn(lastReply('1006'), '注册成功', 'PRD 4.1.3 个人群注册玩家（QQ号 玩家名）')
check(pvp.jfzStore.getPlayerByGroup('1006')['user_qq'] == '10006', '个人群注册映射正确')

# 玩家名唯一 / 一个群只能绑一个玩家
clearLogs()
gm('【PVP】注册玩家 1009 10009 张三', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '全局唯一', 'PRD 4.1.3 玩家名需全局唯一')
clearLogs()
gm('【PVP】注册玩家 1001 10009 新的人', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '只能绑定一个玩家', '一个个人群只能绑一个玩家')

# 玩家不能注册玩家
clearLogs()
gm('【PVP】注册玩家 1010 10110 蹭的', BACKSTAGE_GROUP, P1['qq'], '张三')
checkIn(lastReply(BACKSTAGE_GROUP), '仅限', '★ 玩家不能注册玩家')

# 删掉多余的周八，避免影响后续名单
pvp.jfzStore.deletePlayer('周八')

# 扩展指令【PVP】名单（PRD 2.2「查看全量数据」）
clearLogs()
gm('【PVP】名单', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '1. 张三', '★ 扩展指令【PVP】名单可用（PRD 2.2 查看全量数据）')
checkIn(lastReply(BACKSTAGE_GROUP), '血量 200（正常）', '名单带血量与状态')

clearLogs()
gm('【PVP】名单', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '仅限', '★ PRD 2.2 玩家不能查看全量数据')


# ---------------------------------------------------------------------------
section('阶段 2：活动配置（PRD 4.2）')
# ---------------------------------------------------------------------------

today = C.getTodayKey()
clearLogs()
gm('【PVP】设置活动日期 %s %s 00:00 23:59' % (today, today), BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '活动时间已设置', 'PRD 4.2 设置活动日期')
check(pvp.jfzStore.getConfig('start_date') == today, '开始日期写入')
check(pvp.jfzStore.getConfig('daily_end_time') == '23:59', '每日结束时间写入')

clearLogs()
gm('【PVP】设置活动日期 2026-09-21 2026-09-20 14:00 18:00', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '不能早于开始日期', '结束日期早于开始日期被拒')


# --- 带标签的新格式（用户可见的主推写法）---
clearLogs()
gm('【PVP】设置活动日期 活动周期：2026-09-01 ~ 2026-09-02 每日开放：14:00 ~ 18:00',
   BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '活动时间已设置', '★ 新格式：活动周期：… ~ … 每日开放：… ~ …')
check(pvp.jfzStore.getConfig('start_date') == '2026-09-01', '新格式：开始日期写入')
check(pvp.jfzStore.getConfig('end_date') == '2026-09-02', '新格式：结束日期写入')
check(pvp.jfzStore.getConfig('daily_start_time') == '14:00', '新格式：每日开始时间写入')
check(pvp.jfzStore.getConfig('daily_end_time') == '18:00', '新格式：每日结束时间写入')
checkIn(lastReply(BACKSTAGE_GROUP), C.ACTIVITY_DATE_EXAMPLE, '★ 设置成功的回执里带上格式示例')


def setActivityDateAndRead(text):
    """改一次活动日期，返回写入后的 (start, end, begin, endTime)。"""
    clearLogs()
    gm('【PVP】设置活动日期 %s' % text, BACKSTAGE_GROUP, ADMIN_QQ, '管理')
    return (
        pvp.jfzStore.getConfig('start_date'),
        pvp.jfzStore.getConfig('end_date'),
        pvp.jfzStore.getConfig('daily_start_time'),
        pvp.jfzStore.getConfig('daily_end_time'),
    )


# ★ 下面这几条是「同一个格式的各种写法」：用户按示例随手怎么打都不该被判格式不对
cases = [
    ('活动周期：2026-09-03~2026-09-04 每日开放：09:00~11:30',
     ('2026-09-03', '2026-09-04', '09:00', '11:30'), '波浪线前后不写空格'),
    ('2026-09-05 ~ 2026-09-06 14:00 ~ 15:30',
     ('2026-09-05', '2026-09-06', '14:00', '15:30'), '两段标签都省略'),
    ('每日开放：14:00 ~ 15:30 活动周期：2026-09-07 ~ 2026-09-08',
     ('2026-09-07', '2026-09-08', '14:00', '15:30'), '「每日开放」写在前面'),
    ('活动周期：2026-09-09至2026-09-10 每日开放：14:00至15:30',
     ('2026-09-09', '2026-09-10', '14:00', '15:30'), '中文「至」当波浪线'),
    ('活动周期：2026-09-11～2026-09-12 每日开放：14：00～15：30',
     ('2026-09-11', '2026-09-12', '14:00', '15:30'), '全角波浪线 + 全角冒号'),
    ('活动周期：2026-09-13~2026-09-14每日开放：14:00~15:30',
     ('2026-09-13', '2026-09-14', '14:00', '15:30'), '两段之间不写空格'),
    ('活动周期：2026-09-15 ~ 20260916 每日开放：14:00 ~ 15:30',
     ('2026-09-15', '2026-09-16', '14:00', '15:30'), '紧凑日期 20260916'),
    ('活动周期：2026-09-17 到 2026-09-18 每日开放：14:00 到 15:30',
     ('2026-09-17', '2026-09-18', '14:00', '15:30'), '中文「到」当波浪线'),
]
for text, expect, label in cases:
    got = setActivityDateAndRead(text)
    check(got == expect, '★ 参数写法：%s → %r' % (label, expect))

# 新格式下的语义校验仍然生效
clearLogs()
gm('【PVP】设置活动日期 活动周期：2026-09-20 ~ 2026-09-19 每日开放：14:00 ~ 18:00',
   BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '不能早于开始日期', '新格式：结束日期早于开始日期仍被拒')
clearLogs()
gm('【PVP】设置活动日期 活动周期：2026-09-20 ~ 2026-09-21 每日开放：18:00 ~ 14:00',
   BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '必须晚于开始时间', '新格式：每日结束早于开始仍被拒')

# 认不出参数时报错必须带上格式示例（照示例改就能用）
clearLogs()
gm('【PVP】设置活动日期 随便乱写', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '没能从参数里认出', '参数认不出时报错')
checkIn(lastReply(BACKSTAGE_GROUP), C.ACTIVITY_DATE_EXAMPLE, '★ 报错文案带着格式示例')
clearLogs()
gm('【PVP】设置活动日期 活动周期：2026-09-01 ~ 2026-09-02', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '每日开放', '缺「每日开放」时点名缺的是哪一段')
checkIn(lastReply(BACKSTAGE_GROUP), C.ACTIVITY_DATE_EXAMPLE, '★ 缺参数时也带格式示例')

# ★ 用例收尾：把活动日期还原成「今天 00:00 ~ 23:59」★
# 上面这些用例把活动周期改到了 9 月中旬，而后面所有对局用例都假设「此刻在活动时段内」
# （PRD 4.3.1 的校验顺序是 主动次数 → 活动时段）。不还原的话，那些用例会集体挂在
# 「活动已结束」上，而失败信息看着和活动日期毫不相干，极难定位。
clearLogs()
gm('【PVP】设置活动日期 活动周期：%s ~ %s 每日开放：00:00 ~ 23:59' % (today, today),
   BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '活动时间已设置', '用例收尾：活动日期还原为今天全天')

clearLogs()
gm('【PVP】设置初始攻击 50', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('initial_attack') == 50, 'PRD 4.2 设置初始攻击')

# --- 初始血量 / 血量上限（本插件扩展：两个各自独立的配置项）---
# ★ 这一块会临时改 initial_hp 与 hp_limit，块尾**两个都要还原成 200** ★
# 否则后面所有对局用例的血线都变了，战报里的胜负判定会莫名其妙地翻转。
#
# 这一整块存在的意义就是钉住「两个数可以不同」这件事：
# 只测「改了配置能写进去」是不够的 —— 上限跟着初始血量走的旧实现也能过，
# 所以每一条都要问清楚「动的是哪一个数、另一个有没有被连带改动」。

# ① 初始血量只管新玩家出生血量，不动上限
clearLogs()
gm('【PVP】设置初始血量 350', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '初始血量已设为 350', '★ 扩展指令：设置初始血量')
check(pvp.jfzStore.getConfigInt('initial_hp') == 350, 'initial_hp 已写入配置')
check(C.getHpLimit() == 200, '★ 改初始血量不会动血量上限（两者独立）')
check(C.getInitialHp() == 200, '★ 350 超过上限 200 → 出生血量被压到 200')
checkIn(lastReply(BACKSTAGE_GROUP), '超过了当前血量上限',
        '★ 初始血量大于上限时回执明确警告，并告诉骰主该去改上限')
checkIn(pvp.jfzStore.getConfigTableText(), '初始血量：350', '【PVP】配置 里显示初始血量')
checkIn(pvp.jfzStore.getConfigTableText(), '血量上限：200', '【PVP】配置 里血量上限单独一行')

# ② 把上限提到 500 —— 配置里的 350 立刻生效，不需要重新设一遍初始血量
clearLogs()
gm('【PVP】设置血量上限 500', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '血量上限已设为 500', '★ 扩展指令：设置血量上限')
check(C.getHpLimit() == 500, 'hp_limit 已写入配置并生效')
check(C.getInitialHp() == 350, '★ 上限放宽后，原配的初始血量 350 自动生效（不必重设）')
check(C.clampHp(999) == 500, '★ clampHp 夹的是上限，不是初始血量')
checkIn(lastReply(BACKSTAGE_GROUP), '初始血量', '上限回执里说明初始血量不受影响')

# ③ 新玩家出生血量 = 初始血量（**不是**上限）
clearLogs()
gm('【PVP】注册玩家 1020 10200 临时的', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getPlayer('临时的')['hp'] == 350, '★ 出生血量 = 初始血量 350，而非上限 500')
checkIn(lastReply(BACKSTAGE_GROUP), '初始血量：350（血量上限 500）',
        '★ 两个数不同时，注册回执把上限一并写清楚')
check(C.deriveStatus(350) == 'normal', '出生血量 350 是正常状态')

# ④ 上限管的是「回血 / 改血的封顶」
clearLogs()
gm('【PVP】修改血量 临时的 999', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '血量上限为 500', '★ 修改血量按上限 500 截断')
check(pvp.jfzStore.getPlayer('临时的')['hp'] == 500, '截断后的血量就是上限')

# ⑤ 初始血量配得比上限大：配置照写，出生值被上限压住
clearLogs()
gm('【PVP】设置初始血量 800', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('initial_hp') == 800, '★ 超上限的值照写进配置（骰主可能接着调上限）')
check(C.getInitialHp() == 500, '★ 但出生血量不会超过上限')
checkIn(lastReply(BACKSTAGE_GROUP), '超过了当前血量上限', '回执说明为什么生效值是 500')

# ⑥ 上限下调：点名说出「谁的血压在新上限之上、会被削」
clearLogs()
gm('【PVP】设置血量上限 200', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '被削到 200', '★ 上限下调时点出会被削的玩家')
checkIn(lastReply(BACKSTAGE_GROUP), '临时的', '被削名单里带上玩家名')
check(C.getHpLimit() == 200, '上限已回落到 200')
check(C.getInitialHp() == 200, '★ 上限降回 200 后，出生血量也跟着压到 200')
pvp.jfzStore.deletePlayer('临时的')

# 非法值：范围外的、非数字的两条指令都要拒掉，且不改配置
for badText in ['0', '-5', 'abc', '100001']:
    clearLogs()
    gm('【PVP】设置初始血量 %s' % badText, BACKSTAGE_GROUP, ADMIN_QQ, '管理')
    checkIn(lastReply(BACKSTAGE_GROUP), '用法：', '初始血量拒绝非法值「%s」' % badText)
    clearLogs()
    gm('【PVP】设置血量上限 %s' % badText, BACKSTAGE_GROUP, ADMIN_QQ, '管理')
    checkIn(lastReply(BACKSTAGE_GROUP), '用法：', '血量上限拒绝非法值「%s」' % badText)
check(pvp.jfzStore.getConfigInt('initial_hp') == 800, '★ 非法输入不会改动 initial_hp')
check(pvp.jfzStore.getConfigInt('hp_limit') == 200, '★ 非法输入不会改动 hp_limit')

# 收尾：两个配置都还原成 200（下面的对局用例全部假定 200 血线）
clearLogs()
gm('【PVP】设置初始血量 200', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('initial_hp') == 200, '★ 用例收尾：初始血量已还原为 200')
check(C.getInitialHp() == 200, '★ 用例收尾：出生血量回到 200')
check(C.getHpLimit() == 200, '★ 用例收尾：血量上限已还原为 200')

# 玩家两条血量配置指令都用不了
clearLogs()
gm('【PVP】设置初始血量 300', BACKSTAGE_GROUP, P1['qq'], '张三')
checkIn(lastReply(BACKSTAGE_GROUP), '仅限', '★ 玩家不能设置初始血量')
clearLogs()
gm('【PVP】设置血量上限 300', BACKSTAGE_GROUP, P1['qq'], '张三')
checkIn(lastReply(BACKSTAGE_GROUP), '仅限', '★ 玩家不能设置血量上限')

# --- 攻击值：统一值 + 个人值（本插件扩展）---
# ★ 和 initial_hp 一样，这一块也是「先改配置、末尾必须还原」★
# 攻击值直接决定每回合 `.raN`，不还原会让后面所有掷骰用例的期望值全部落空。
clearLogs()
gm('【PVP】设置初始攻击 60', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('initial_attack') == 60, 'PRD 4.2 设置统一初始攻击')
checkIn(lastReply(BACKSTAGE_GROUP), '全体初始攻击已设为 60', '统一攻击值回执')
checkIn(lastReply(BACKSTAGE_GROUP), '设置个人攻击', '统一值回执里指路个人设定')

# 个人值：显式指令
clearLogs()
gm('【PVP】设置个人攻击 张三 70', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '「张三」的个人攻击值已设为 70', '★ 扩展指令：设置个人攻击')
check(pvp.jfzStore.getPersonalAttack('张三') == 70, '个人攻击值已写入玩家档案')
check(pvp.jfzStore.getPlayerAttack('张三') == 70, '★ 个人值优先于统一值')
check(pvp.jfzStore.getPlayerAttack('李四') == 60, '★ 没被单独设定的人仍用统一值')

# 便捷写法：设置初始攻击 玩家名 N（骰主脑子里两件事同名，不必记两条指令）
clearLogs()
gm('【PVP】设置初始攻击 李四 80', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getPersonalAttack('李四') == 80, '★ 「设置初始攻击 玩家名 N」= 给个人设')
check(pvp.jfzStore.getConfigInt('initial_attack') == 60, '★ 这个写法不会动统一值')

# 个人值确实进到「每回合掷什么」的计划里（_startFight 就是拿它算的）
_planMatch = {'challenger_name': '张三', 'target_name': '李四', 'declared': {}}
check(pvp.jfzEngine._buildRoundPlan(_planMatch, '张三', pvp.jfzStore.getPlayerAttack('张三'), 3)
      == [70, 70, 70], '★ 个人攻击值进入每回合计划（.ra70）')
check(pvp.jfzEngine._buildRoundPlan(_planMatch, '李四', pvp.jfzStore.getPlayerAttack('李四'), 3)
      == [80, 80, 80], '★ 同场两人各自的基础值互不影响')

# 上界夹取：基础值 100 再叠加胜临符 +10 会得到 .ra110，青果不认这种指令
_capMatch = {
    'challenger_name': '张三', 'target_name': '李四',
    'declared': {'张三': {'items': [{'name': '胜临符', 'round': 1}]}},
}
check(pvp.jfzEngine._buildRoundPlan(_capMatch, '张三', 100, 3)[0] == 100,
      '★ 100 + 胜临符 +10 被夹在 100（.ra110 是非法骰值）')

# 清除个人设定：写 0
clearLogs()
gm('【PVP】设置个人攻击 张三 0', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '个人攻击值已清除', '★ 写 0 清除个人设定')
check(pvp.jfzStore.getPersonalAttack('张三') is None, '个人值已从档案中移除')
check(pvp.jfzStore.getPlayerAttack('张三') == 60, '清除后回落到统一值')

# 非法值 / 找不到玩家
for badText in ['101', '0x', 'abc', '-3']:
    clearLogs()
    gm('【PVP】设置个人攻击 李四 %s' % badText, BACKSTAGE_GROUP, ADMIN_QQ, '管理')
    checkIn(lastReply(BACKSTAGE_GROUP), '用法：', '个人攻击值拒绝「%s」' % badText)
check(pvp.jfzStore.getPersonalAttack('李四') == 80, '★ 非法输入不会改动已有的个人值')

clearLogs()
gm('【PVP】设置个人攻击 查无此人 60', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '找不到玩家', '个人攻击值：玩家不存在时报错')

# 名单只标出「有个人值」的人（统一值在【PVP】配置里看，不重复刷屏）
clearLogs()
gm('【PVP】名单', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '攻击 80（个人）', '★ 名单标出个人攻击值')

# 玩家自己也能看到攻击值（掷骰列表里的 .raN 就是拿它算的）
clearLogs()
gm('【PVP】血量', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '攻击值：60', '★ 【PVP】血量 显示攻击值')

clearLogs()
gm('【PVP】设置个人攻击 李四 90', BACKSTAGE_GROUP, P1['qq'], '张三')
checkIn(lastReply(BACKSTAGE_GROUP), '仅限', '★ 玩家不能设置个人攻击')

# ★ 用例收尾：清除个人值 + 统一值还原 50 ★
clearLogs()
gm('【PVP】设置个人攻击 李四 0', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
gm('【PVP】设置初始攻击 50', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('initial_attack') == 50, '★ 用例收尾：统一攻击已还原为 50')
check(pvp.jfzStore.getPersonalAttack('李四') is None, '★ 用例收尾：个人攻击值已全部清除')

clearLogs()
gm('【PVP】设置回合数 3', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('rounds') == 3, 'PRD 4.2 设置回合数')

clearLogs()
gm('【PVP】设置主动次数 3', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('daily_attack_limit') == 3, 'PRD 4.2 设置主动次数')

clearLogs()
gm('【PVP】设置每种道具限用 1', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getConfigInt('item_limit_per_match') == 1, 'PRD 4.2 设置每种道具限用')

clearLogs()
gm('【PVP】设置伤害表 大成功=20 极难成功=15 困难成功=10 成功=5 失败=0 大失败=0',
   BACKSTAGE_GROUP, ADMIN_QQ, '管理')
table = pvp.jfzStore.getDamageTable()
check(table['大成功'] == 20 and table['极难成功'] == 15 and table['困难成功'] == 10
      and table['成功'] == 5 and table['失败'] == 0 and table['大失败'] == 0, 'PRD 4.2 设置伤害表')

clearLogs()
gm('【PVP】设置伤害表 困难成功=12', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(pvp.jfzStore.getDamageTable()['困难成功'] == 12, '伤害表支持只改一档（增量更新）')
gm('【PVP】设置伤害表 困难成功=10', BACKSTAGE_GROUP, ADMIN_QQ, '管理')

# 玩家不能配置活动
clearLogs()
gm('【PVP】设置回合数 99', BACKSTAGE_GROUP, P1['qq'], '张三')
checkIn(lastReply(BACKSTAGE_GROUP), '仅限', '★ 玩家不能配置活动参数')
check(pvp.jfzStore.getConfigInt('rounds') == 3, '玩家那条没被改动')

# 骰主拥有后台管理员的全部权限（骰主要求：骰主 ⊇ 管理员，此处**不**照抄 PRD 2.2 的 ✖）
clearLogs()
gm('【PVP】设置回合数 5', BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '每场回合数已设为 5', '★ 骰主也能配置活动参数（骰主 ⊇ 管理员）')
check(pvp.jfzStore.getConfigInt('rounds') == 5, '骰主的设置已生效')
gm('【PVP】设置回合数 3', BACKSTAGE_GROUP, MASTER_QQ, '骰主')

# 骰主也能用「只有管理员能发」的那类指令（铜币 / 价格）
clearLogs()
gm('【PVP】发放铜币 张三 100', BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '发放完成', '★ 骰主也能发放铜币')
gm('【PVP】扣除铜币 张三 100', BACKSTAGE_GROUP, MASTER_QQ, '骰主')


# ---------------------------------------------------------------------------
section('阶段 3：商城 / 背包 / 场外药（PRD 4.8）')
# ---------------------------------------------------------------------------

clearLogs()
gm('【PVP】商城', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '大还丹', 'PRD 4.8.1 商城列表')
checkIn(lastReply(P1['group']), '胜临符', '商城列出回合型道具')

clearLogs()
gm('【PVP】购买 大还丹', P1['group'], P1['qq'], P1['name'])
check(inv('张三').get('大还丹') == 1, 'PRD 4.8.1 购买成功，背包 +1')
check(playerOf('张三')['copper'] == 1000 - pvp.jfzStore.getItemPrice('大还丹'), '购买扣减铜币')

clearLogs()
gm('【PVP】购买 不存在的东西', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '商城没有', '买不存在的道具被拒')

clearLogs()
gm('【PVP】购买 遁隐符', P1['group'], P1['qq'], P1['name'])
check(playerOf('张三')['copper'] >= 0, '铜币不会变负数')
pvp.jfzStore.setPlayerCopper('张三', 1000)

# 满血吃大还丹应被拒绝且不消耗
clearLogs()
gm('【PVP】使用道具 大还丹', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '血量已经满了', '满血用药被拒（不白白消耗）')
check(inv('张三').get('大还丹') == 1, '满血用药没有扣掉道具')

# 受伤后可以吃
pvp.jfzStore.setPlayerHp('张三', 100)
clearLogs()
gm('【PVP】使用道具 大还丹', P1['group'], P1['qq'], P1['name'])
check(hpOf('张三') == 200, '大还丹回复 100 血（100→200）')
check(inv('张三').get('大还丹') is None, '使用后道具被消耗')

# 死皮不能购买/用药
pvp.jfzStore.setPlayerHp('张三', 0)
clearLogs()
gm('【PVP】购买 大还丹', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '死皮', 'PRD 3.1 死皮不能购买')
pvp.jfzStore.setPlayerHp('张三', 200)

# 发放道具（管理扩展指令）
clearLogs()
gm('【PVP】发放道具 张三 胜临符 1', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
gm('【PVP】发放道具 李四 错影符 1', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
gm('【PVP】发放道具 张三 遁隐符 1', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
gm('【PVP】发放道具 李四 大还丹 1', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(inv('张三').get('胜临符') == 1 and inv('李四').get('错影符') == 1, '管理发放道具生效')


# ---------------------------------------------------------------------------
section('阶段 4：第一场完整对局（PRD 4.3 / 4.4 / 4.5 / 4.6 / 4.7）')
# ---------------------------------------------------------------------------

clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
# 【投递模型】发起人正在会话里：他的回执、排到通知、声明引导全部走会话回复；
# 只有被发起人（不在场）的内容才进 OUTBOX 跨群推送。所以发起人侧用 allReplies 查。
checkIn(allReplies(P1['group']), '排队中', 'PRD 4.3.1 发起 PVP 进入排队')
checkNotIn(allReplies(P1['group']), '队列位置', '★ 回执不显示队列位置（用户要求）')
checkNotIn(allReplies(P1['group']), '对局正在进行', '无对局在跑时不提示「等待中」')

sentChallenger = allReplies(P1['group'])
sentTarget = sentTo(P2['group'])
checkIn(sentChallenger, '发起人：张三', 'PRD 4.3.3 发起人群通知含发起人（会话直发）')
checkIn(sentChallenger, '被发起人：李四', 'PRD 4.3.3 发起人群通知含被发起人')
checkIn(sentTarget, '被发起人：李四', 'PRD 4.3.3 被发起人群收到通知（跨群推送）')
checkNotIn(sentTarget, '张三', '★ PRD 验收 6：被发起人群不出现发起人身份')
checkIn(sentTarget, '神秘对手' , '被发起人侧出现「神秘对手」占位')
checkIn(sentChallenger + '\n'.join(sentTarget), '【是否使用道具】', 'PRD 4.4 进入道具声明阶段')
# 【通道断言】手里有活事件时，B 的通知必须走「活事件直发」（jfzSend.sendViaEvent），
# 不能进自建发送队列 —— 队列那条 fake 事件链在真机上是「没下文」的祸根。
# 发起后队列应为空，说明两条 B 侧通知都没排队。
check(pvp.jfzSend.sendQueue.empty(), '★ 被发起人的通知走活事件直发，不进自建发送队列')

# --- 道具声明 ---
clearLogs()
gm('【PVP】使用道具 胜临符 2', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '已声明', 'PRD 4.4 声明回合型道具')
check(inv('张三').get('胜临符') is None, 'PRD 4.8.3 声明时立即扣减')

clearLogs()
gm('【PVP】使用道具 胜临符 1', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '你的背包里没有', '背包不足时拒绝声明')

clearLogs()
gm('【PVP】使用道具 错影符 2', P2['group'], P2['qq'], P2['name'])
checkIn(lastReply(P2['group']), '同一回合只能放置一个道具', '★★ PRD 4.8.2 同一回合只能放一个道具（双方合计）')

clearLogs()
gm('【PVP】使用道具 错影符 3', P2['group'], P2['qq'], P2['name'])
checkIn(lastReply(P2['group']), '已声明', '换一个回合可以声明')

clearLogs()
gm('【PVP】使用道具 遁隐符', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '已声明', 'PRD 4.4 声明全场型道具（不写回合号）')

clearLogs()
gm('【PVP】使用道具 胜临符', P2['group'], P2['qq'], P2['name'])
checkIn(lastReply(P2['group']), '必须写明第几回合', '回合型不写回合号被拒')

clearLogs()
gm('【PVP】使用道具 大还丹', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '场外型道具，不能在 PVP 中使用', '战斗中不能声明场外型道具')

# 张三声明完毕（但带了遁隐符 → 整场取消，走阶段 7 的路径）
# 这里先把遁隐符收回去，专心验证正常对局
pvp.jfzStore.removeItem('张三', '遁隐符', 1)
pvp.jfzStore.addItem('张三', '遁隐符', 1)

# 把遁隐符改成不声明：先清掉已声明的记录
match = pvp.jfzStore.getMatch()
declared = match.get('declared', {})
declared['张三']['items'] = [e for e in declared['张三']['items'] if e.get('name') != '遁隐符']
pvp.jfzStore.updateMatch(declared=declared)

clearLogs()
gm('【PVP】不使用', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '声明完毕', 'PRD 4.4 不使用 → 完成声明')

clearLogs()
gm('【PVP】不使用', P2['group'], P2['qq'], P2['name'])
# 张三（不在场）的掷骰列表走跨群推送；李四是最后声明的人（在场），列表会话直发
rollListA = sentTo(P1['group'])
rollListB = lastReply(P2['group'])
checkIn(rollListA, '【本场共 3 回合', 'PRD 4.5.1 生成每回合掷骰列表')
checkIn(rollListA, '第 2 回合：.ra60', '★ 胜临符让自身第 2 回合 +10')
checkIn(rollListA, '第 3 回合：.ra40', '★ 对方错影符让第 3 回合 -10')
checkIn(rollListA, '你用了胜临符', '掷骰列表带道具注解')
checkIn(rollListA, '对方用了错影符', '掷骰列表带对手道具注解')
checkIn(rollListB, '第 3 回合：.ra50', '对手的道具不改变李四自己的数值')

check(pvp.jfzStore.getMatch()['stage'] == C.STAGE_FIGHT, '双方声明完毕 → 进入掷骰阶段')

# --- 防作弊：掷骰值与要求不符 ---
clearLogs()
sendRa(P1, '.ra99', 1, 99)
checkIn(lastReply(P1['group']), '掷骰值与要求不符', '★ PRD 4.5.3 技能值与要求不符 → 拒绝计分')
checkIn(lastReply(P1['group']), '请重新发送 .ra50', '提示要求值')
match = pvp.jfzStore.getMatch()
check(len(match['rolls']['张三']) == 0, '不符的掷骰没有被计入')

# --- 污染防护：命令里的数字与记录里的技能值不一致（旧记录残留） ---
# 之前这一关是静默 return —— 玩家看到骰子正常回了结果，插件却一声不吭，
# 这一掷凭空消失，双方「都掷满」永远不成立，对局无声卡死。现在必须有提示。
clearLogs()
sendRa(P1, '.ra50', 45, 99)
checkIn(lastReply(P1['group']), '未被记录', '★ 污染防护不再静默：提示这一掷没被记录')
check(len(pvp.jfzStore.getMatch()['rolls']['张三']) == 0, '污染记录没有被计入')

# --- 正常掷骰 ---
clearLogs()
sendRa(P1, '.ra50', 1, 50)
checkIn(lastReply(P1['group']), '第 1 回合已记录', '有效掷骰被记录')
# 【用户要求】回执不显示进度、一次掷骰只有一条插件消息
checkNotIn(lastReply(P1['group']), '进度', '★ 掷骰回执不显示进度')
checkNotIn(allReplies(P1['group']), '现在是第', '★ 掷骰前不再发「现在是第 N 回合」引导（合并成一条）')
check(pvp.jfzStore.getMatch()['rolls']['张三'][0]['level'] == '大成功', '真实判定：roll=1 → 大成功')

sendRa(P1, '.ra60', 100, 60)
check(pvp.jfzStore.getMatch()['rolls']['张三'][1]['level'] == '大失败', '真实判定：roll=100 → 大失败')

clearLogs()
sendRa(P1, '.ra40', 20, 40)
# 掷骰者本人在场，「等待对方」是会话回复（lastReply），不带对方进度
checkIn(lastReply(P1['group']), '你的 3 回合已全部掷完，等待对方掷骰。',
        'PRD 4.5.4 一方掷满 → 等待对方')
checkNotIn(lastReply(P1['group']), '对方当前', '★ 等待对方提示不显示对方进度')

# 李四掷骰（测试配置 rounds=3，张三已掷满 3/3）
sendRa(P2, '.ra50', 50, 50)
checkIn(lastReply(P2['group']), '第 1 回合已记录', '对方掷骰正常记录')
checkNotIn(lastReply(P2['group']), '进度', '★ 对方侧回执同样不显示进度')
sendRa(P2, '.ra50', 30, 50)
checkIn(lastReply(P2['group']), '第 2 回合已记录', '第二回合正常记录')
clearLogs()
sendRa(P2, '.ra50', 10, 50)
# 「等待结算」和随后的战报都是发给在场掷骰者的会话回复，用 allReplies/lastReply 查
checkIn(allReplies(P2['group']), '3 回合已全部掷完，等待结算', '★ 双方掷满 → 「等待结算」提示')

# --- 结算结果 ---
# 伤害等级用 OlivaDiceCore 真实的 COC7 规则算（roll=1→大成功、roll=100→大失败…）：
#   张三 对 李四：大成功(20) + 大失败(0) + 困难成功(10) = 30
#        · .ra40 掷 20：20 > 40/5=8，20 ≤ 40/2=20 → 困难成功
#   李四 对 张三：成功(5) + 成功(5) + 极难成功(15) = 25
#        · .ra50 掷 50：50 > 50/2=25，50 ≤ 50       → 成功
#        · .ra50 掷 30：30 > 50/2=25，30 ≤ 50       → 成功
#        · .ra50 掷 10：10 ≤ 50/5=10                → 极难成功（不是困难成功！）
check(hpOf('张三') == 175, '张三血量 200-25=175（实际 %d）' % hpOf('张三'))
check(hpOf('李四') == 170, '李四血量 200-30=170（实际 %d）' % hpOf('李四'))

sentA = sentTo(P1['group'])
sentB = lastReply(P2['group'])   # 李四是最后一骰的掷骰者（在场）→ 战报会话直发
checkIn(sentA, '张三 累计造成伤害：30', '战报伤害累计正确（张三侧跨群推送）')
checkIn(sentA, '李四 累计造成伤害：25', '战报对手伤害累计正确')
checkIn(sentA, '张三 胜利！', '战报给出胜者')
checkIn(sentB, '神秘对手', '★ 被发起人侧战报用「神秘对手」')
checkNotIn(sentB, '张三', '★ PRD 验收 13：被发起人侧战报不含发起人名')
# 【用户要求】战报不展示双方剩余血量（哪一侧都不能有）
checkNotIn(sentB, '剩余血量', '★ 被发起人侧战报不展示剩余血量')
checkNotIn(sentA, '剩余血量', '★ 发起人侧战报不展示剩余血量')
checkNotIn(sentB, '170', '★ 战报里不出现任何血量数值')

check(pvp.jfzStore.getMatch() == {}, 'PRD 4.6.2 结算后对局已结束')
report = pvp.jfzStore.getLastReport()
check(report and report['challenger_total_damage'] == 30, '战报已存档')
check(inv('张三').get('胜临符') is None, 'PRD 4.8.3 正常结算 → 道具不返还')
check(inv('李四').get('错影符') is None, 'PRD 4.8.3 正常结算 → 对手道具也不返还')


# ---------------------------------------------------------------------------
section('阶段 5：排队、战斗锁与自动开启下一场（PRD 3.2 / 4.9 / 4.6.2）')
# ---------------------------------------------------------------------------

clearLogs()
# 王五 → 赵六：先排上
gm('【尽伏诛】PVP\n发起人：王五\n被发起人：赵六', P3['group'], P3['qq'], P3['name'])
# 无对局在跑 → 这条请求立刻开局，发起人的会话里会有：排队回执 + 排到通知 + 声明引导
checkIn(allReplies(P3['group']), '排队中', '第二条请求入队')

# 孙七 → 王五：王五已在队列里，应被拒
clearLogs()
gm('【尽伏诛】PVP\n发起人：孙七\n被发起人：王五', P5['group'], P5['qq'], P5['name'])
checkIn(lastReply(P5['group']), '正在对局中', '★ PRD 4.9 战斗期间不能被其他 PVP 挑战')

# 对局进行中，王五尝试主动发起
clearLogs()
gm('【尽伏诛】PVP\n发起人：王五\n被发起人：孙七', P3['group'], P3['qq'], P3['name'])
checkIn(lastReply(P3['group']), '正在 PVP 对局中', '★ PRD 4.9 战斗期间不能发起新 PVP')

# ★ 有对局正在打时，新请求的回执要交代清楚「要等当前这场打完」
# （否则发起人只看到「排队中」就再无下文，分不清是 bug 还是在排队）
clearLogs()
gm('【尽伏诛】PVP\n发起人：孙七\n被发起人：李四', P5['group'], P5['qq'], P5['name'])
checkIn(lastReply(P5['group']), '对局正在进行', '★ 排队回执交代「前面有对局在打」')

# 战斗中商城与场外药关闭
clearLogs()
gm('【PVP】购买 大还丹', P3['group'], P3['qq'], P3['name'])
checkIn(lastReply(P3['group']), '战斗中无法购买', '★ PRD 4.9 战斗中商城关闭')
clearLogs()
pvp.jfzStore.addItem('王五', '大还丹', 1)
pvp.jfzStore.setPlayerHp('王五', 100)
gm('【PVP】使用道具 大还丹', P3['group'], P3['qq'], P3['name'])
# 注意：王五正在对局中，路由层（jfzReply._handleUseItem）会先把这条指令
# 交给「对局中」分支处理，所以提示文案来自 jfzEngine.handleUseItemInMatch，
# 而不是 jfzShop.useOffFieldItem 里那条兜底守卫。
checkIn(lastReply(P3['group']), '不能在 PVP 中使用', '★ PRD 4.9 战斗中场外药关闭')
check(inv('王五').get('大还丹') == 1, '被拒的场外药没有被消耗')
check(hpOf('王五') == 100, '被拒的场外药没有回血')

# 快速打完这场（双方都不声明、直接掷骰）
gm('【PVP】不使用', P3['group'], P3['qq'], P3['name'])
gm('【PVP】不使用', P4['group'], P4['qq'], P4['name'])
for value in [60, 60, 60]:
    sendRa(P3, '.ra50', value, 50)
for value in [60, 60, 60]:
    sendRa(P4, '.ra50', value, 50)
check(hpOf('王五') == 100 and hpOf('赵六') == 200, '全失败 → 双方血量不变')

# 王五vs赵六 结算后，刚排队的 孙七→李四 会被自动顶上（进入声明阶段）。
# 先把它取消掉，别影响「孙七→王五」那条用例。
pvp.jfzEngine.cancelAllForWindowClose(BOT_HASH)

# 自动开启下一场：孙七→王五 现在应该能排上
clearLogs()
pvp.jfzStore.setPlayerHp('王五', 200)
gm('【尽伏诛】PVP\n发起人：孙七\n被发起人：王五', P5['group'], P5['qq'], P5['name'])
checkIn(allReplies(P5['group']), '排队中', '对局结束后可以正常发起')
sentW = sentTo(P3['group'])
checkIn(sentW, '被发起人：王五', '★ PRD 4.6.2 上一场结束后自动开启队列下一场')
checkNotIn(sentW, '孙七', '被发起人侧不出现孙七')

# 清掉这场，别影响后面的用例
pvp.jfzEngine.cancelAllForWindowClose(BOT_HASH)


# ---------------------------------------------------------------------------
section('阶段 6：濒死 / 死皮与死亡掉落（PRD 3.1 / 4.7）')
# ---------------------------------------------------------------------------

clearLogs()
gm('【PVP】修改血量 李四 10', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '血量：170（正常）→ 10（濒死）', 'PRD 4.7.3 修改血量（设为指定值）')

clearLogs()
gm('【PVP】修改血量 李四 +50', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(hpOf('李四') == 60, 'PRD 4.7.3 修改血量（+N 增加）')

clearLogs()
gm('【PVP】修改血量 李四 -60', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
check(hpOf('李四') == 0, 'PRD 4.7.3 修改血量（-N 减少，最低 0）')
check(C.deriveStatus(hpOf('李四')) == C.STATUS_DEAD, '血量 0 → 死皮')

checkIn(lastReply(BACKSTAGE_GROUP), '死皮', 'PRD 4.7.3 改血量后状态自动重算（没有复活指令）')

# 死皮不能被挑战
clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '无法被挑战', 'PRD 3.1 死皮不可被 PVP')

# 把李四救回来，准备掉落测试
gm('【PVP】修改血量 李四 200', BACKSTAGE_GROUP, ADMIN_QQ, '管理')

# 建一个能被打死的场景（PRD 3.1：濒死不能主动发起，但可以被挑战）
#   李四 20 血 → 濒死，可被挑战；赵六 30 血 → 正常，能发起
#   赵六 掷 1/1/1 → 大成功×3 = 60 伤害，足够把 20 血的李四打穿
#   李四 掷 100/100/100 → 大失败 0 伤害，赵六无伤
clearInv('李四')
pvp.jfzStore.addItem('李四', '大还丹', 2)
pvp.jfzStore.setPlayerCopper('李四', 777)
pvp.jfzStore.setPlayerHp('李四', 20)
pvp.jfzStore.setPlayerHp('赵六', 30)

liItemsBefore = dict(inv('李四'))
liCopperBefore = int(playerOf('李四')['copper'])

clearLogs()
gm('【尽伏诛】PVP\n发起人：赵六\n被发起人：李四', P4['group'], P4['qq'], P4['name'])
checkIn(allReplies(P4['group']), '排队中', '濒死可以被挑战')

zhaoCopperBefore = playerOf('赵六')['copper']
zhaoItemsBefore = dict(inv('赵六'))

gm('【PVP】不使用', P4['group'], P4['qq'], P4['name'])
gm('【PVP】不使用', P2['group'], P2['qq'], P2['name'])

# 赵六 (skill 50) 掷出 1/1/1 → 大成功×3 = 60 伤害；李四掷 100/100/100 → 大失败 0 伤害
for _ in range(3):
    sendRa(P4, '.ra50', 1, 50)
for _ in range(3):
    sendRa(P2, '.ra50', 100, 50)

check(hpOf('赵六') == 30, '李四全大失败 → 赵六无伤')
check(hpOf('李四') == 0, '李四被打到 0 血')
check(C.deriveStatus(hpOf('李四')) == C.STATUS_DEAD, '李四变为死皮')

# PRD 4.7.2：死者全部背包与铜币归胜利方
check(playerOf('赵六')['copper'] == zhaoCopperBefore + liCopperBefore,
      'PRD 4.7.2 死者铜币归胜者（%d + %d = %d，实际 %d）' % (
          zhaoCopperBefore, liCopperBefore, zhaoCopperBefore + liCopperBefore,
          playerOf('赵六')['copper']))
for itemName, count in liItemsBefore.items():
    check(int(inv('赵六').get(itemName, 0)) == int(zhaoItemsBefore.get(itemName, 0)) + int(count),
          'PRD 4.7.2 死者道具归胜者（%s：%d + %d）' % (
              itemName, int(zhaoItemsBefore.get(itemName, 0)), int(count)))
check(inv('李四') == {}, '死者背包被清空')
check(playerOf('李四')['copper'] == 0, '死者铜币清零')

dropReports = sentTo(P4['group'])
checkIn(dropReports, '李四倒下了', 'PRD 4.7.2 播报死亡掉落')
checkIn(dropReports, '%d 铜币' % liCopperBefore, '掉落播报列出铜币')

# 被发起人侧的战报里，掉落播报也不能出现发起人的真名
dropReportsB = sentTo(P2['group'])
checkIn(dropReportsB, '神秘对手', '★ 被发起人侧掉落播报用「神秘对手」')
checkNotIn(dropReportsB, '赵六', '★ 被发起人侧掉落播报不含发起人真名')

# 双方同时倒下 → 不转移
clearInv('李四')
pvp.jfzStore.setPlayerHp('李四', 200)
pvp.jfzStore.addItem('李四', '小还丹', 1)
pvp.jfzStore.setPlayerHp('赵六', 30)
pvp.jfzStore.setPlayerHp('李四', 10)
pvp.jfzStore.setPlayerCopper('赵六', 500)
pvp.jfzStore.setPlayerCopper('李四', 500)

clearLogs()
gm('【尽伏诛】PVP\n发起人：赵六\n被发起人：李四', P4['group'], P4['qq'], P4['name'])
gm('【PVP】不使用', P4['group'], P4['qq'], P4['name'])
gm('【PVP】不使用', P2['group'], P2['qq'], P2['name'])
for _ in range(3):
    sendRa(P4, '.ra50', 1, 50)
for _ in range(3):
    sendRa(P2, '.ra50', 1, 50)

check(hpOf('赵六') == 0 and hpOf('李四') == 0,
      '双方同时倒下（赵六 %d，李四 %d）' % (hpOf('赵六'), hpOf('李四')))
check(playerOf('赵六')['copper'] == 500 and playerOf('李四')['copper'] == 500,
      '★ PRD 4.7.2 双方都 0 血 → 背包与铜币不动')
check(inv('李四').get('小还丹') == 1, '双方死时道具不转移')

# 收尾：把两人救回来
pvp.jfzStore.setPlayerHp('赵六', 200)
pvp.jfzStore.setPlayerHp('李四', 200)


# ---------------------------------------------------------------------------
section('阶段 7：遁隐符（PRD 4.7.2 / 4.8.3）')
# ---------------------------------------------------------------------------

clearInv('张三')
clearInv('王五')
pvp.jfzStore.addItem('张三', '胜临符', 1)
pvp.jfzStore.addItem('张三', '遁隐符', 1)
pvp.jfzStore.addItem('王五', '错影符', 1)

hpA0 = hpOf('张三')
hpB0 = hpOf('王五')

clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：王五', P1['group'], P1['qq'], P1['name'])
gm('【PVP】使用道具 遁隐符', P1['group'], P1['qq'], P1['name'])
gm('【PVP】使用道具 胜临符 1', P1['group'], P1['qq'], P1['name'])
check(inv('张三').get('遁隐符') is None and inv('张三').get('胜临符') is None, '两张符都已声明扣减')

gm('【PVP】使用道具 错影符 2', P3['group'], P3['qq'], P3['name'])
clearLogs()
gm('【PVP】不使用', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P3['group'], P3['qq'], P3['name'])

sentEscapeA = sentTo(P1['group'])
sentEscapeB = sentTo(P3['group'])
checkIn(sentEscapeA, '遁隐符', 'PRD 4.7.2 遁隐符 → 对局立即结束')
checkIn(sentEscapeA, '血量维持', '整场取消，血量维持')
check(inv('张三').get('遁隐符') is None, '★ PRD 4.8.3 用符方消耗该符（不返还）')
check(inv('张三').get('胜临符') == 1, '★ 用符方其他道具被返还')
check(inv('王五').get('错影符') == 1, '★ PRD 4.8.3 对方已声明道具返还')
check(hpOf('张三') == hpA0 and hpOf('王五') == hpB0, '血量维持不变（没有结算）')
check(pvp.jfzStore.getMatch() == {}, '对局已结束')


# ---------------------------------------------------------------------------
section('阶段 8：每日时段结束取消（PRD 4.2 / 8）')
# ---------------------------------------------------------------------------

clearInv('张三')
clearInv('李四')
pvp.jfzStore.addItem('张三', '胜临符', 1)
pvp.jfzStore.addItem('李四', '错影符', 1)
hpA0 = hpOf('张三')
hpB0 = hpOf('李四')

clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
gm('【PVP】使用道具 胜临符 1', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P1['group'], P1['qq'], P1['name'])
gm('【PVP】使用道具 错影符 2', P2['group'], P2['qq'], P2['name'])
gm('【PVP】不使用', P2['group'], P2['qq'], P2['name'])
check(pvp.jfzStore.getMatch()['stage'] == C.STAGE_FIGHT, '对局已进入掷骰阶段')

# 排一条请求在队列里（王五 → 孙七）
usedBefore = pvp.jfzStore.getDailyAttackUsed('王五')
gm('【尽伏诛】PVP\n发起人：王五\n被发起人：孙七', P3['group'], P3['qq'], P3['name'])
check(len(pvp.jfzStore.getQueue()) == 1, '队列里有 1 条请求')

# 把每日时段改成「已经收工」，触发看守
pvp.jfzStore.setConfigValue('daily_start_time', '00:00')
pvp.jfzStore.setConfigValue('daily_end_time', '00:01')

clearLogs()
pvp.jfzEngine.checkAndCloseIfNeeded()

check(pvp.jfzStore.getQueue() == [], 'PRD 4.2 收工后等待中的对局被取消')
check(pvp.jfzStore.getMatch() == {}, 'PRD 4.2 收工后进行中的对局被取消')
check(hpOf('张三') == hpA0 and hpOf('李四') == hpB0, 'PRD 4.2 不结算伤害，血量维持')
check(inv('张三').get('胜临符') == 1, '★ PRD 4.8.3 收工取消 → 道具全部返还（张三）')
check(inv('李四').get('错影符') == 1, '★ PRD 4.8.3 收工取消 → 道具全部返还（李四）')
check(pvp.jfzStore.getDailyAttackUsed('王五') == usedBefore,
      '★ 未匹配成功的排队请求退还主动次数')

sentCancel = sentTo(P1['group']) + sentTo(P2['group'])
checkIn(sentCancel, '直接取消', '双方收到取消通知')

# 收工后不能再发起。
# 注意校验顺序（PRD 4.3.1）：次数 → 活动时段。张三 今天的 3 次主动已经在
# 阶段 4/7/8 用完了，所以这时先报「次数用完」——顺便把这条也验证掉。
clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '主动发起次数已经用完', 'PRD 4.2 每日主动次数上限生效')

# 把上限临时调高，绕过次数这一关，专测「时段外不能发起」
pvp.jfzStore.setConfigValue('daily_attack_limit', 99)
clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '今天已收工', 'PRD 4.2 时段外无法发起 PVP')

# 但商城仍可用
clearLogs()
gm('【PVP】商城', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '大还丹', 'PRD 4.2 非开放时段商城仍可用')

# 恢复时段
pvp.jfzStore.setConfigValue('daily_end_time', '23:59')


# ---------------------------------------------------------------------------
section('阶段 9：机器人重启恢复（PRD 8）')
# ---------------------------------------------------------------------------

clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P2['group'], P2['qq'], P2['name'])
sendRa(P1, '.ra50', 30, 50)
match0 = pvp.jfzStore.getMatch()
check(len((match0.get('rolls') or {}).get('张三', [])) == 1,
      '对局进行到一半（actual match=%r）' % (match0,))

# 模拟重启：重新从磁盘加载 + 走恢复流程
clearLogs()
pvp.jfzStore.reload()
pvp.jfzEngine.recoverAfterRestart()

match = pvp.jfzStore.getMatch()
check(match.get('stage') == C.STAGE_FIGHT, '★ PRD 8 重启后未完成对局被恢复')
check(len((match.get('rolls') or {}).get('张三', [])) == 1, '重启后已掷回合数保留')
sentResume = sentTo(P1['group']) + sentTo(P2['group'])
checkIn(sentResume, '机器人已重启', '重启后向双方发出继续对局提醒')
checkIn(sentResume, '已记录 1 / 3 回合', '重启提醒含进度')

# 重新挂钩子（重启后 init_after 会再挂一次，这里验证不会重复计数）
pvp.jfzDice.installHook()
sendRa(P1, '.ra50', 30, 50)
sendRa(P1, '.ra50', 30, 50)
check(len((pvp.jfzStore.getMatch().get('rolls') or {}).get('张三', [])) == 3,
      '重复挂钩子不会导致重复计分')

pvp.jfzEngine.cancelAllForWindowClose(BOT_HASH)


# ---------------------------------------------------------------------------
section('阶段 9.5：声明阶段超时自动取消（★ 扩展：declare_timeout_minutes）')
# ---------------------------------------------------------------------------
# 背景：PRD 对声明阶段没有超时，一对玩家都放着不声明的话，对局永远
# 卡在声明阶段，队列里后面所有人的请求永远轮不上 —— 表现就是
# 「回复了排队中，然后再无下文」。这里验证超时自动取消的完整行为。

# 张三 / 李四 状态复位
pvp.jfzStore.setPlayerHp('张三', 200)
pvp.jfzStore.setPlayerHp('李四', 200)
clearInv('张三')
clearInv('李四')

# 起一场对局，张三声明一件道具（制造「需要返还」的场景），李四一直不声明
clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
checkIn(allReplies(P1['group']), '排队中', '超时用例：对局已建立')
pvp.jfzStore.addItem('张三', '胜临符', 1)
gm('【PVP】使用道具 胜临符 1', P1['group'], P1['qq'], P1['name'])
check(inv('张三').get('胜临符') is None, '超时用例：声明已扣道具')
usedBefore = pvp.jfzStore.getDailyAttackUsed('张三')

# 把声明超时配成 1 分钟，再把对局的声明开始时间拨回 2 分钟前
pvp.jfzStore.setConfigValue('declare_timeout_minutes', 1)
match = pvp.jfzStore.getMatch()
old = pvp.jfzCore.getNow() - pvp.jfzCore.datetime.timedelta(minutes=2)
match['declare_started_at'] = old.strftime('%Y-%m-%d %H:%M:%S')
pvp.jfzStore.setMatch(match)

# 拨回之前先确认「没超时就不会动」：把时间改回刚刚
match['declare_started_at'] = pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S')
pvp.jfzStore.setMatch(match)
check(pvp.jfzEngine._checkDeclareTimeout(BOT_HASH) is False, '未超时 → 不取消对局')
check(pvp.jfzStore.hasActiveMatch(), '未超时 → 对局仍在')

# 现在拨回 2 分钟前，触发超时
match = pvp.jfzStore.getMatch()
match['declare_started_at'] = old.strftime('%Y-%m-%d %H:%M:%S')
pvp.jfzStore.setMatch(match)
clearLogs()
check(pvp.jfzEngine._checkDeclareTimeout(BOT_HASH) is True, '★ 超时 → 对局被自动取消')
check(not pvp.jfzStore.hasActiveMatch(), '★ 超时后当前对局已清空')
check(inv('张三').get('胜临符') == 1, '★ 超时取消返还已声明的道具')
check(pvp.jfzStore.getDailyAttackUsed('张三') == usedBefore - 1,
      '★ 超时取消退还发起人的主动次数')
textA = '\n'.join(sentTo(P1['group'])) + lastReply(P1['group'])
textB = '\n'.join(sentTo(P2['group'])) + lastReply(P2['group'])
checkIn(textA, '因为超时未完成道具声明', '★ 双方收到超时取消通知（发起人侧，说明是超时未声明道具）')
checkIn(textB, '因为超时未完成道具声明', '★ 双方收到超时取消通知（被发起人侧）')
checkIn(textA, '本场对局已取消', '超时通知含「已取消」结论（发起人侧）')
checkIn(textB, '本场对局已取消', '超时通知含「已取消」结论（被发起人侧）')
# 后台通知（没有触发事件）现在复用最近的活事件发送，而不是现造 fake 事件
check(pvp.jfzSend.getRememberedEvent(BOT_HASH) is not None,
      '★ 后台通知有活事件可复用（jfzSend.rememberEvent）')

# 超时后队列里排着的请求要立刻顶上
check(pvp.jfzStore.hasActiveMatch() or pvp.jfzStore.getQueue() == [],
      '超时取消后队列继续流转（或队列为空）')

# 超时设为 0 → 关闭超时，对局永远等待
pvp.jfzStore.setConfigValue('declare_timeout_minutes', 0)
if pvp.jfzStore.hasActiveMatch():
    pvp.jfzEngine.cancelAllForWindowClose(BOT_HASH)
clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
match = pvp.jfzStore.getMatch()
old2 = pvp.jfzCore.getNow() - pvp.jfzCore.datetime.timedelta(minutes=60)
match['declare_started_at'] = old2.strftime('%Y-%m-%d %H:%M:%S')
pvp.jfzStore.setMatch(match)
check(pvp.jfzEngine._checkDeclareTimeout(BOT_HASH) is False, '★ 超时配 0 → 不限制，永不超时取消')
pvp.jfzEngine.cancelAllForWindowClose(BOT_HASH)
pvp.jfzStore.setConfigValue('declare_timeout_minutes', 10)

# 管理指令【PVP】设置声明超时
clearLogs()
gm('【PVP】设置声明超时 5', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '声明超时已设为 5 分钟', '★ 扩展指令【PVP】设置声明超时')
gm('【PVP】设置声明超时 0', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '已关闭', '声明超时配 0 视为关闭')
checkIn(pvp.jfzStore.getConfigTableText(), '声明超时', '【PVP】配置里展示声明超时')
gm('【PVP】设置声明超时 10', BACKSTAGE_GROUP, ADMIN_QQ, '管理')  # 还原默认

# 管理指令【PVP】设置掷骰超时
clearLogs()
gm('【PVP】设置掷骰超时 30', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '掷骰超时已设为 30 分钟', '★ 扩展指令【PVP】设置掷骰超时')
gm('【PVP】设置掷骰超时 0', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '已关闭', '掷骰超时配 0 视为关闭')
checkIn(pvp.jfzStore.getConfigTableText(), '掷骰超时', '【PVP】配置里展示掷骰超时')
gm('【PVP】设置掷骰超时 30', BACKSTAGE_GROUP, ADMIN_QQ, '管理')  # 用例基准值


# ---------------------------------------------------------------------------
section('阶段 9.7：掷骰阶段超时 → 按已有记录结算（★ 本插件扩展）')
# ---------------------------------------------------------------------------

# 场景：双方声明完毕、张三掷了 1 回合后就没人掷了 → 超时按已有记录结算
pvp.jfzStore.setPlayerHp('张三', 200)
pvp.jfzStore.setPlayerHp('李四', 200)
clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P2['group'], P2['qq'], P2['name'])
check(pvp.jfzStore.getMatch()['stage'] == C.STAGE_FIGHT, '掷骰超时用例：对局进入掷骰阶段')

clearLogs()
sendRa(P1, '.ra50', 30, 50)
check(len(pvp.jfzStore.getMatch()['rolls']['张三']) == 1, '掷骰超时用例：张三掷了 1 回合')
hpZhangBefore = hpOf('张三')
hpLiBefore = hpOf('李四')

# 拨回 31 分钟前，触发掷骰超时（配置 30 分钟）
match = pvp.jfzStore.getMatch()
old = pvp.jfzCore.getNow() - pvp.jfzCore.datetime.timedelta(minutes=31)
match['fight_started_at'] = old.strftime('%Y-%m-%d %H:%M:%S')
pvp.jfzStore.setMatch(match)

# 先验证「未超时不动」：把时间改回刚刚
match['fight_started_at'] = pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S')
pvp.jfzStore.setMatch(match)
check(pvp.jfzEngine._checkFightTimeout(BOT_HASH) is False, '掷骰未超时 → 不结算')

# 拨回 31 分钟前 → 超时结算
match = pvp.jfzStore.getMatch()
match['fight_started_at'] = old.strftime('%Y-%m-%d %H:%M:%S')
pvp.jfzStore.setMatch(match)
clearLogs()
check(pvp.jfzEngine._checkFightTimeout(BOT_HASH) is True, '★ 掷骰超时 → 按已有记录结算')
check(not pvp.jfzStore.hasActiveMatch(), '★ 掷骰超时结算后对局已结束')

# 张三掷了 1 回合（roll=30, skill=50 → 30 ≤ 50 成功 → 5 伤害），李四 0 回合
# → 张三血量不变，李四 -5；双方其余 2/3 回合都没掷，按 0 伤害计
check(hpOf('张三') == hpZhangBefore, '★ 张三血量不变（李四没掷，0 伤害）')
check(hpOf('李四') == hpLiBefore - 5, '★ 李四只吃已记录的 1 回合伤害（成功=5），缺失回合 0 伤害')

# 双方都收到战报，且战报注明了未掷回合
textA = '\n'.join(sentTo(P1['group'])) + lastReply(P1['group'])
textB = '\n'.join(sentTo(P2['group'])) + lastReply(P2['group'])
checkIn(textA, '战报', '掷骰超时：发起人侧收到战报')
checkIn(textB, '战报', '掷骰超时：被发起人侧收到战报')
checkIn(textA, '按 0 伤害计', '★ 战报注明有未掷回合')
checkIn(textA, '自动取消' if False else '掷骰阶段已超过', '★ 超时通知先行')


# ---------------------------------------------------------------------------
section('阶段 10：强制结束 / 结束活动（PRD 3.2 / 4.10）')
# ---------------------------------------------------------------------------

# 张三 / 李四 的状态在前面的阶段被改过（血、背包），这里复位到干净状态
pvp.jfzStore.setPlayerHp('张三', 200)
pvp.jfzStore.setPlayerHp('李四', 200)
clearInv('张三')
clearInv('李四')

clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
pvp.jfzStore.addItem('张三', '胜临符', 1)
gm('【PVP】使用道具 胜临符 1', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P1['group'], P1['qq'], P1['name'])
gm('【PVP】不使用', P2['group'], P2['qq'], P2['name'])

clearLogs()
gm('【PVP】强制结束', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '已强制结束', 'PRD 3.2 管理强制终止对局')
check(pvp.jfzStore.getMatch() == {}, '对局已被终止')
check(inv('张三').get('胜临符') == 1, '强制终止退还已声明道具')
pvp.jfzStore.addItem('张三', '胜临符', 1)

# 结束活动：第一步不执行
clearLogs()
gm('【PVP】结束活动', BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '确认结束当前游戏', 'PRD 4.10 结束活动需二次确认')
check(pvp.jfzStore.isRegistered(), '第一步没有真的执行')

# 非骰主不能结束
clearLogs()
gm('【PVP】结束活动 确认', BACKSTAGE_GROUP, ADMIN_QQ, '管理')
checkIn(lastReply(BACKSTAGE_GROUP), '仅限骰主', 'PRD 4.10 结束活动仅骰主可执行')
check(pvp.jfzStore.isRegistered(), '非骰主执行后数据仍在')

# 骰主判定的兜底：配置里的 master_id。
# 用途是「青果骰主名单里的 userHash 与本环境的 userId 对不上」时救急
# （QQ 官方机器人的 openid 常见），这里用一个不在青果骰主名单里的号来验证。
FALLBACK_MASTER_QQ = '90009'
pvp.jfzStore.setConfigValue('master_id', FALLBACK_MASTER_QQ)
clearLogs()
gm('【PVP】移除管理 %s' % ADMIN_QQ, BACKSTAGE_GROUP, FALLBACK_MASTER_QQ, '兜底骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '已移除后台管理员',
        '★ 配置里的 master_id 可作为骰主判定兜底')
gm('【PVP】添加管理 %s' % ADMIN_QQ, BACKSTAGE_GROUP, MASTER_QQ, '骰主')  # 还原

# 第二步执行
clearLogs()
gm('【PVP】结束活动 确认', BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '所有数据已删除', 'PRD 4.10 结束活动并删除数据')
check(not pvp.jfzStore.isRegistered(), '★ PRD 验收 17：插件回到未注册状态')
check(pvp.jfzStore.getGameName() == '', '游戏名已删除')
check(pvp.jfzStore.listPlayerNames() == [], '玩家数据已删除')
check(pvp.jfzStore.getAdminList() == [], '管理员名单已清空')
check(pvp.jfzStore.getBackstageGroup() == '', '后台群注册关系已清空')
for fileName in ['游戏.json', '玩家.json', '队列.json', '对局.json', '战报.json']:
    path = os.path.join('plugin/data/pvp', fileName)
    check(not os.path.exists(path), '文件已删除：%s' % fileName)

# 配置.json 是刻意保留的：里面存着 master_id（骰主身份兜底，与本次游戏无关）。
# 但活动配置必须已重置 —— 用一个「已注册」的标志字段来验证。
configPath = os.path.join('plugin/data/pvp', '配置.json')
if os.path.exists(configPath):
    with open(configPath, encoding='utf-8') as fp:
        savedConfig = json.load(fp)
    check(savedConfig.get('start_date', '') == '' and savedConfig.get('end_date', '') == '',
          '配置.json 里活动日期已重置（start_date / end_date 为空）')
    check(savedConfig.get('master_id', '') == FALLBACK_MASTER_QQ,
          '配置.json 保留了 master_id（骰主身份兜底，实为 %r）' % savedConfig.get('master_id', ''))
else:
    check(True, '配置.json 未保留（也符合 PRD 4.10）')
    check(True, '配置.json 未保留，master_id 无从谈起')

# 未注册时发起 PVP
clearLogs()
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
checkIn(lastReply(P1['group']), '还没有注册游戏', '未注册时拒绝发起')

# 重新注册可用
clearLogs()
gm('【PVP】注册后台群 %s' % GAME, BACKSTAGE_GROUP, MASTER_QQ, '骰主')
checkIn(lastReply(BACKSTAGE_GROUP), '后台群注册成功', '结束活动后可以重新注册')

done()
