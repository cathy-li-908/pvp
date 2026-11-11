# -*- encoding: utf-8 -*-
'''
骰子桥接层 —— 把「玩家掷的 `.ra`」变成插件能用的数据。

================================================================
【这一步在解决什么问题】
================================================================
青果（OlivaDiceCore）的 `.ra` 检定，掷骰结果**不通过任何返回值暴露**，
全都留在函数内部的局部变量里。所以正常情况下别的插件读不到
「玩家刚掷了多少点、算成功还是失败」。

但是！`.ra` 会把这次掷骰写进「用户配置」留档
（OlivaDiceCore/onediceOverride.py 里对单次掷骰无条件写这两个键）：

    RDRecordInt      → 这次掷出的骰值（例如 78）
    RDRecordSkillInt → 这次用的技能值（也就是 .ra50 里的 50）

所以本插件的做法是：
  1) 在 init_after 里接管 OlivaDiceCore 的消息钩子 `crossHook.dictHookFunc['msgHook']`
  2) 钩子在「一条回复即将发出」时触发；如果上一条收到的指令是 `.ra` 开头，
     说明这条回复就是掷骰结果 → 去读上面那两个记录
  3) 成功等级**自己算**（调 OlivaDiceCore.skillCheck.getSkillCheckByTemplate），
     不去解析回复文本

================================================================
【为什么不解析回复文本】
================================================================
青果的所有回复文案都是**用户可改的**（GUI 里能改，或用
`.strPcSkillCheckFailed 哈哈失败哈哈` 直接改）。
模板里还能写 `|` 做随机分支。任何靠正则抠回复文本的方案都会因此失效，
而且失效得很隐蔽（用户改完文案，插件就永远判定「失败」）。
读掷骰记录 + 自己算等级则完全不依赖文案，用户怎么改都不受影响。

================================================================
【关于 msgHook 的两个坑】
================================================================
坑 1：`crossHook.dictHookFunc['msgHook']` 是**单个函数槽，不是列表** ——
     直接赋值会把别的插件挂上去的钩子顶掉。
     所以本模块先把原来的钩子存下来，在自己的实现里链式调用它。

坑 2：重载插件时槽里可能还留着**上一次加载的我们自己的钩子**。
     如果把它当成「原钩子」链下去，反复重载会让钩子链越来越长，
     同一个掷骰被处理 N 次。所以本模块给自己打的标记会识别出来并跳过。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore

import traceback

import pvp


# ---------------------------------------------------------------------------
# 成功等级 ↔ 中文名
# ---------------------------------------------------------------------------
# 【为什么要有这张表】
# 伤害表（PRD 4.6）的键用的是中文等级名，因为用户要能用
# `【PVP】设置伤害表 大成功=20 ...` 直接改，且落盘后 JSON 人眼可读。
# 但 OlivaDiceCore 判定出来的是 resultType 枚举，中间需要一次翻译。
LEVEL_TO_NAME = {
    OlivaDiceCore.skillCheck.resultType.SKILLCHECK_GREAT_SUCCESS: '大成功',
    OlivaDiceCore.skillCheck.resultType.SKILLCHECK_EXTREME_HARD_SUCCESS: '极难成功',
    OlivaDiceCore.skillCheck.resultType.SKILLCHECK_HARD_SUCCESS: '困难成功',
    OlivaDiceCore.skillCheck.resultType.SKILLCHECK_SUCCESS: '成功',
    OlivaDiceCore.skillCheck.resultType.SKILLCHECK_FAIL: '失败',
    OlivaDiceCore.skillCheck.resultType.SKILLCHECK_GREAT_FAIL: '大失败',
}


# 最近一条「收到的消息」缓存，用于判断下一次回复是不是掷骰结果。
# 键：(botHash, 群ID, 用户ID)  值：去掉引用/@ 之后的指令文本
#
# 【为什么要缓存这个】
# 钩子在「回复即将发出」时触发，而那时**拿不到**「玩家刚才发了什么」。
# 本插件 priority=19990 比 OlivaDiceCore 的 20000 小，
# 所以同一条群消息本插件会**先**收到 —— 在那里把指令文本记下来，
# 钩子触发时取出来比对。
#
# 用 pop 取出（消费一次）而不是 get，避免同一条指令被重复计分。
dictLastCommand = {}

# 钩子是否已安装，防止 init_after 被重复调用时反复安装。
flagHookInstalled = False


# ---------------------------------------------------------------------------
# 安装钩子
# ---------------------------------------------------------------------------

def installHook():
    """
    接管 OlivaDiceCore 的 msgHook，用于读取 `.ra` 的掷骰结果。
    在 main.Event.init_after 里调用（此时 OlivaDiceCore 已初始化完毕）。
    """
    global flagHookInstalled

    hookDict = OlivaDiceCore.crossHook.dictHookFunc
    currentHook = hookDict.get('msgHook', None)

    # 重载插件时槽里可能还留着上一次加载的我们自己的钩子。
    # 认出是自己之后就跳过它，直接接到它下面那一层，避免钩子链越来越长。
    if currentHook is not None and getattr(currentHook, '_isJfzHook', False):
        currentHook = getattr(currentHook, '_previousHook', None)

    def jfzHook(event, funcType, sender, dectData, message):
        # 自己的逻辑出错绝不能影响骰子本身的回复，所以整段包在 try 里
        try:
            onMsgHook(event, funcType, sender, dectData, message)
        except Exception:
            print('[pvp] 骰子钩子处理异常：')
            traceback.print_exc()
        # 链式调用原来的钩子，保证别的插件照常工作
        if currentHook is not None:
            return currentHook(event, funcType, sender, dectData, message)
        return None

    # 打上标记，供下次重载时识别
    jfzHook._isJfzHook = True
    jfzHook._previousHook = currentHook

    hookDict['msgHook'] = jfzHook
    flagHookInstalled = True
    print('[pvp] 骰子钩子已安装。')


# ---------------------------------------------------------------------------
# 记录待核对的指令
# ---------------------------------------------------------------------------

def rememberCommand(botHash, groupId, userId, commandText):
    """
    记下「刚收到的指令文本」。由 jfzReply 在处理每条群消息时调用。

    一律记下、不做判断 —— 反正钩子那边会比对是不是 `.ra`。
    这样即使玩家发的是别的指令，也只是多存一次字符串，代价可以忽略。
    """
    if groupId is None or userId is None:
        return
    dictLastCommand[(str(botHash), str(groupId), str(userId))] = str(commandText)


def forgetCommand(botHash, groupId, userId):
    """
    主动清掉某个人待核对的指令。

    用于「玩家发了 `.ra` 但这不是他会话里的合法掷骰」的场景 ——
    比如他正处于道具声明阶段却先掷了一次，那这次掷骰不该被计分，
    但也不能让这条记录留着，否则他后面正常掷骰时会被错配。
    """
    dictLastCommand.pop((str(botHash), str(groupId), str(userId)), None)


# ---------------------------------------------------------------------------
# 钩子主体
# ---------------------------------------------------------------------------

def onMsgHook(event, funcType, sender, dectData, message):
    """
    钩子主体。只在「一条回复即将发出」时做判断。

    这里要连着过好几道关，任何一道不过就直接返回 ——
    因为这个钩子对**全服所有掷骰**都会触发（包括别人插件的 `.r`、`.rd6` 等），
    必须尽快退出，不能做重活。
    """
    # 第 1 关：只看回复。recv / send_group / reply_private 一律不管。
    if funcType != 'reply':
        return

    # 第 2 关：取出这次消息的群和用户。dectData 就是 [host_id, group_id, user_id]。
    if not isinstance(dectData, (list, tuple)) or len(dectData) < 3:
        return
    groupId = dectData[1]
    userId = dectData[2]
    if groupId is None or userId is None:
        return

    botHash = str(event.bot_info.hash)

    # 第 3 关：上一条收到的指令是不是 `.ra`？
    # 取出来就删掉（pop），保证同一条指令只可能被计分一次。
    lastCommand = dictLastCommand.pop((botHash, str(groupId), str(userId)), None)
    if not pvp.jfzCore.isRaCommand(lastCommand):
        return

    # 第 4 关：这个群必须是本插件登记过的「玩家个人群」，
    # 而且掷骰的人必须就是这个群登记的那个玩家（防止群里其他人乱掷也计分）。
    # 只重读玩家表和对局表 —— 这个钩子调用很频繁，别读别的文件。
    pvp.jfzStore.reloadLight()
    playerName = pvp.jfzStore.getPlayerNameByGroup(groupId)
    if not playerName:
        return
    playerData = pvp.jfzStore.getPlayer(playerName)
    if not playerData:
        return
    if str(playerData.get('user_qq', '')) != str(userId):
        return

    # 第 5 关：必须正处在「掷骰子阶段」，而且这个人就是本场对局的双方之一
    match = pvp.jfzStore.getMatch()
    if match.get('state') != pvp.jfzCore.MATCH_ACTIVE:
        return
    if match.get('stage') != pvp.jfzCore.STAGE_FIGHT:
        return
    if playerName not in [match.get('challenger_name'), match.get('target_name')]:
        return

    # 第 6 关：读掷骰记录
    rollData = readRollRecord(botHash, userId, event)
    if rollData is None:
        # 没读到有效记录（比如连掷 .ra50#3、或者带奖励骰）。
        # 提示一下，但不计分 —— 否则玩家会以为「掷了但没反应」。
        pvp.jfzEngine.onRollUnreadable(event, playerName)
        return
    rolledValue, skillValue = rollData

    # 第 6.5 关：拿玩家**自己敲的数字**和记录里的技能值对一下。
    # 不一致说明这条记录是上一次掷骰的残留（青果这次没真的掷），
    # 必须忽略 —— 否则会把旧结果当成本次结果计分（等于直接改错血）。
    #
    # 【这一关绝不能静默 return】
    # 之前这里是无声返回：玩家看到骰子正常回复了结果，插件却一声不吭，
    # 这一掷就凭空消失了 —— 他以为自己掷完了 10 回合，实际只记了 9 回合，
    # 双方「都掷满」的结算条件永远不成立，对局就此无声卡死。
    # 现在明确告诉他「这一掷没被记录，请重掷」。
    declaredSkill = pvp.jfzCore.parseRaSkillValue(lastCommand)
    if declaredSkill is not None and declaredSkill != skillValue:
        pvp.jfzCore.replyText(
            event,
            '这一次掷骰的结果与你的指令不符（记录里的技能值是 %d），未被记录。\n'
            '请重新发送本回合要求的 .raN。' % int(skillValue)
        )
        return

    # 第 7 关：技能值必须等于**本回合**规定的值。
    # 这一关是防作弊的关键 —— 玩家想掷 `.ra99` 提高成功率，这里会拦下来。
    #
    # 【为什么是「本回合」而不是「本场」】回合型道具（胜临符 / 错影符）会让
    # 不同回合的攻击值不一样（比如第 2 回合该掷 .ra60、其余回合 .ra50）。
    # 所以核对时要看这个人「已经掷了几次」—— 下一次掷骰对应第几回合，
    # 就该用那个回合的值。掷错了不计数，重掷时仍然补记到同一个回合。
    rollIndex = len(getRolls(match, playerName))
    expectedAttack = pvp.jfzEngine.getExpectedRoundAttack(match, playerName, rollIndex)

    if expectedAttack is None:
        # 本场回合已掷满，多掷的不算
        return

    if skillValue != expectedAttack:
        pvp.jfzEngine.onAttackValueMismatch(event, playerName, skillValue, expectedAttack, rollIndex + 1)
        return

    # 成功等级自己算 —— 用 OlivaDiceCore 的判定规则，和 `.ra` 本身的结果完全一致
    levelName = judgeLevel(rolledValue, skillValue)

    # 交给引擎记账（在掷骰者自己的群里回复进度）
    pvp.jfzEngine.recordRoll(event, playerName, rolledValue, skillValue, levelName)


def getRolls(match, playerName):
    """取某个人本场已经记下的掷骰列表（只读，不落盘）。"""
    rolls = match.get('rolls', {})
    if not isinstance(rolls, dict):
        return []
    data = rolls.get(playerName, [])
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# 读掷骰记录
# ---------------------------------------------------------------------------

def readRollRecord(botHash, userId, event):
    """
    读取玩家最近一次掷骰的记录。返回 (骰值, 技能值)；读不到返回 None。

    【为什么可能读不到】
      - 玩家用的是连掷写法（`.ra50#3`）—— 那种走另一条分支，不写这条记录
      - 玩家用了奖励骰 / 惩罚骰 —— 骰值会是一个元组而不是单个数字，这里不认
    这两种情况都返回 None，由上层提示「这一掷没被记录」。
    """
    platform = event.platform['platform']
    try:
        rolledValue = OlivaDiceCore.onediceOverride.getRDDataIntUser(
            botHash=botHash, userId=str(userId), platform=platform
        )
    except Exception:
        return None

    # 只接受整数骰值。元组/列表说明是多次骰（奖励骰、4d6 之类），本插件不支持。
    if isinstance(rolledValue, bool) or not isinstance(rolledValue, int):
        return None

    # 骰值应该在 1-100 之间。超出范围说明记录是脏的（别的骰型的残留），不认。
    if rolledValue < 1 or rolledValue > 100:
        return None

    # 技能值没有现成的 getter，按 OlivaDiceCore 的 userConfig 接口直读
    try:
        userHash = OlivaDiceCore.userConfig.getUserHash(
            userId=str(userId), userType='user', platform=platform
        )
        skillValue = OlivaDiceCore.userConfig.getUserConfigByKeyWithHash(
            userHash=userHash, userConfigKey='RDRecordSkillInt', botHash=botHash
        )
    except Exception:
        return None

    if skillValue is None:
        return None
    try:
        skillValue = int(skillValue)
    except Exception:
        return None

    return rolledValue, skillValue


# ---------------------------------------------------------------------------
# 判定成功等级
# ---------------------------------------------------------------------------

def judgeLevel(rolledValue, skillValue):
    """
    用 OlivaDiceCore 的判定规则算成功等级，返回**中文等级名**。

    这就是 `.ra` 内部用的同一个函数（getSkillCheckByTemplate），
    传 template=None 会走内置的 COC7 默认规则：
      - 骰值 ≤ 技能值 / 5      → 极难成功
      - 骰值 ≤ 技能值 / 2      → 困难成功
      - 骰值 ≤ 技能值          → 成功
      - 骰值 == 1              → 大成功
      - 技能值 < 50 且骰值 ≥ 96 → 大失败
      - 技能值 ≥ 50 且骰值 == 100 → 大失败
      - 其余                   → 失败

    算不出来时按「失败」处理 —— 保守方向，不会凭空给人加分。
    """
    try:
        levelEnum, _threshold = OlivaDiceCore.skillCheck.getSkillCheckByTemplate(
            {'roll': int(rolledValue), 'skill': int(skillValue)}
        )
        return LEVEL_TO_NAME.get(levelEnum, '失败')
    except Exception:
        print('[pvp] 判定成功等级失败，按失败处理：')
        traceback.print_exc()
        return '失败'


def getDamageByLevel(levelName):
    """
    成功等级（中文名）→ 本场扣血值。

    伤害表从活动配置里读，所以管理员用 `【PVP】设置伤害表` 改完立刻生效。
    不认识的等级按 0 处理。
    """
    table = pvp.jfzStore.getDamageTable()
    return int(table.get(str(levelName), 0))
