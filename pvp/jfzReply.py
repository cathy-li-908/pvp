# -*- encoding: utf-8 -*-
'''
指令路由 —— group_message 的统一入口（插件的「大门口」）。

================================================================
【这里只做三件事，别把玩法逻辑写进来】
================================================================
  1. 记录「这条消息是什么」给骰子钩子用（**必须在所有 return 之前**，见下）
  2. 判断这条消息是不是本插件要管的三种形态之一
  3. 把参数原样转交给对应的处理函数

玩法判断、权限判断、数据读写全部在 jfzAdmin / jfzEngine / jfzShop 里。
把逻辑写在这一层是「9000 行 if/elif 链」的成因，本插件刻意避免。

================================================================
【本插件要管的三种消息形态】
================================================================
  ① `【PVP】xxx`               —— 所有自定义指令（PRD 第 1 节）
  ② `【游戏名】PVP` + 两行字段  —— 发起 PVP（PRD 4.3.1，游戏名由骰主声明）
  ③ `.raN`                     —— 掷骰，**本插件不接管**，交给青果，
                                  但要把「玩家发了什么」记下来给钩子比对

================================================================
【★ 为什么第 1 步必须在最前面 ★】
================================================================
骰子钩子的触发时机是「青果即将发出回复」，那一刻它拿不到「玩家刚才发了什么」。
所以本插件必须**先于** OlivaDiceCore 把原消息记下来。而本插件
priority=19990 < OlivaDiceCore 的 20000，本来就是先收到的
（OlivOS 的 plugin_models_call_list 按 priority 升序调用，见 pluginAPI.py）。

如果在中间某个 `return` 之后才记录，那么「玩家发 `.raN`」这条路径
（它一定会走到最后才 return）虽然能记上，但任何提前 return 的分支
都会漏记 —— 漏记的后果是那一次掷骰永远不计分，而且玩家看不出来。
所以记录放在函数第一行，之后随便 return 都安全。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

import re

import pvp


# 骰主指令（PRD 5.1）
MASTER_COMMANDS = {
    '注册后台群': 'handleRegisterBackstageGroup',
    '添加管理': 'handleAddAdmin',
    '移除管理': 'handleRemoveAdmin',
    '结束活动': 'handleEndActivity',
}

# 后台管理指令（PRD 5.2）
ADMIN_COMMANDS = {
    # 放在表头：这条本身就是「管理指令的目录」，管理忘了有哪些指令时第一眼就该看到它。
    # 权限走 requireMasterOrAdmin —— 这张表里所有指令的权限检查都已经统一成它了
    # （骰主 ⊇ 管理员，见 jfzAdmin 头部说明）。
    '管理': 'handleAdminHelp',
    '注册玩家': 'handleRegisterPlayer',
    '设置初始血量': 'handleSetInitialHp',
    # 血量的两个配置项是分开的两条指令：初始血量管新号起点，上限管血条长度。
    '设置血量上限': 'handleSetHpLimit',
    '设置初始铜币': 'handleSetInitialCopper',
    '设置初始攻击': 'handleSetInitialAttack',
    '设置个人攻击': 'handleSetPlayerAttack',
    '设置活动日期': 'handleSetActivityDate',
    '设置主动次数': 'handleSetDailyAttackLimit',
    '设置回合数': 'handleSetRounds',
    '设置每种道具限用': 'handleSetItemLimit',
    '设置声明超时': 'handleSetDeclareTimeout',
    '设置掷骰超时': 'handleSetFightTimeout',
    '设置伤害表': 'handleSetDamageTable',
    '发放铜币': 'handleGiveCopper',
    '扣除铜币': 'handleDeductCopper',
    '修改血量': 'handleModifyHp',
    # —— 本插件的扩展指令（PRD 里没有，见指令手册「扩展」一节）——
    '设置道具价格': 'handleSetItemPrice',
    '发放道具': 'handleGiveItem',
    '回收道具': 'handleTakeItem',
    '删除玩家': 'handleDeletePlayer',
    '强制结束': 'handleForceEnd',
    '配置': 'handleShowConfig',
    '名单': 'handleShowPlayers',
}

# 玩家指令（PRD 5.3）
PLAYER_COMMANDS = {
    '商城': 'handleShop',
    '购买': 'handleBuy',
    '背包': 'handleBackpack',
    '铜币': 'handleCopper',
    '血量': 'handleStatus',
    '战报': 'handleMyReport',
    '排队': 'handleQueueInfo',
}


# ---------------------------------------------------------------------------
# 管理指令说明表（`【PVP】管理` 的正文来源）
# ---------------------------------------------------------------------------
# 【为什么这里只写「怎么用」，不写「有哪些指令」】
# 指令清单是项目里最容易过期的东西：加了一条指令、改了三条 handler，
# 清单却没跟着改。管理照着清单发指令，收到「不认识的指令」，
# 既不知道是清单旧了还是自己打错了 —— 这种问题几乎无法自查。
#
# 所以清单的「有哪些指令」**直接取上面两张路由表**（MASTER_COMMANDS /
# ADMIN_COMMANDS），路由表里有的指令一定出现在清单里；这里只补「怎么写、干什么」。
# 漏补的会渲染成「说明待补」并在 run_tests.py 里被断言拦下，不会悄悄消失。
#
# 权限标签只允许两种，与 jfzAdmin 的两个权限检查函数一一对应：
#   骰主      → requireMaster（只有骰主能做）
#   骰主+管理 → requireMasterOrAdmin（骰主与管理员都能做）
# 【为什么没有「只有管理员能做」这一档】
# 本插件里骰主 ⊇ 管理员（理由见 jfzAdmin 头部）——「只有管理员能做」这种档位不存在，
# 标签白名单里刻意不给它位置：万一有人写出这个标签，那条指令会掉出所有分组、
# 在清单里凭空消失，run_tests.py 里的标签断言会当场拦下。
ADMIN_COMMAND_DOC = {
    # ---- 骰主专用 ----
    '注册后台群': ('骰主', '游戏名　在目标群注册后台群并声明游戏名'),
    '添加管理': ('骰主', 'QQ号　把该 QQ 加进后台管理员名单'),
    '移除管理': ('骰主', 'QQ号　从后台管理员名单移除'),
    '结束活动': ('骰主', '删除全部玩家 / 对局 / 配置数据，需回复「结束活动 确认」二次确认'),
    # ---- 骰主 + 后台管理员 ----
    '管理': ('骰主+管理', '本清单，随时回来查'),
    '注册玩家': ('骰主+管理', '群号 QQ号 玩家名　也可在玩家自己的个人群用「注册玩家 QQ号 玩家名」'),
    '修改血量': ('骰主+管理', '玩家名 150　设为 150；写「+50」增加、「-30」减少'),
    '配置': ('骰主+管理', '查看当前全部活动配置'),
    '名单': ('骰主+管理', '查看全部玩家数据（血量 / 状态 / 铜币 / 次数）'),
    '发放道具': ('骰主+管理', '玩家名 道具名 [数量]　数量默认 1'),
    '回收道具': ('骰主+管理', '玩家名 道具名 [数量]　数量默认 1'),
    '删除玩家': ('骰主+管理', '玩家名　从玩家表、排队队列、进行中的对局里一并摘掉'),
    '强制结束': ('骰主+管理', '立即终止当前对局：不结算、血量维持、道具返还，队列自动顶上'),
    '设置初始血量': ('骰主+管理', '200　新玩家注册时的出生血量（1 ~ 100000），与血量上限互不影响'),
    '设置血量上限': ('骰主+管理', '500　血条能有多长：回血封顶、血量夹取、显示里的分母（1 ~ 100000）'),
    '设置初始铜币': ('骰主+管理', '1000　新注册玩家的初始铜币'),
    # 清单是一行一条，说明里**不要放换行**（渲染时是直接拼接的，换行会顶格破版）。
    # 「设置初始攻击」的两种写法就压在一条里说清楚，个人值另有独立一条。
    '设置初始攻击': ('骰主+管理', '50　全体统一的基础攻击值（1 ~ 100）。'
                              '也支持「设置初始攻击 玩家名 60」直接给某人单独设'),
    '设置个人攻击': ('骰主+管理', '玩家名 60　只给某人单独设基础攻击值；写 0 = 清除个人设定，回到统一值'),
    # 参数示例直接取 jfzCore 里的那唯一一份（渲染时前面会自动带上【PVP】指令名）。
    '设置活动日期': ('骰主+管理', pvp.jfzCore.ACTIVITY_DATE_ARGS_EXAMPLE + '　波浪线前后空格可省'),
    '设置主动次数': ('骰主+管理', '3　每人每天可主动发起几次，0 = 不许主动发起'),
    '设置回合数': ('骰主+管理', '10　每场每人要掷几回合'),
    '设置每种道具限用': ('骰主+管理', '1　每种道具每场能用几次，0 = 禁用'),
    '设置声明超时': ('骰主+管理', '10　声明阶段超时分钟数，超时本场取消，0 = 不限'),
    '设置掷骰超时': ('骰主+管理', '30　掷骰阶段超时分钟数，超时按已记录回合结算，0 = 不限'),
    '设置伤害表': ('骰主+管理', '等级=扣血 …　例：设置伤害表 极难成功=15　可只改其中几档'),
    '设置道具价格': ('骰主+管理', '道具名 价格　例：设置道具价格 大还丹 500'),
    '发放铜币': ('骰主+管理', '玩家名 N'),
    '扣除铜币': ('骰主+管理', '玩家名 N　余额不足时扣到 0 为止'),
}

# 渲染顺序也要固定：从「只有骰主能做」到「骰主与管理员都能做」。
_ADMIN_DOC_GROUPS = [
    ('骰主专用', '骰主'),
    ('骰主与后台管理员都能用', '骰主+管理'),
]


def getAdminHelpText():
    """
    `【PVP】管理` 的正文：按权限分组列出全部管理与骰主指令。

    指令名来自两张路由表（写死会过期，见 ADMIN_COMMAND_DOC 的注释），
    分组顺序 = _ADMIN_DOC_GROUPS 的顺序，组内顺序 = 路由表里的声明顺序。
    """
    gameName = pvp.jfzStore.getGameName() or '（未注册）'

    # 路由表里有的都列出来；MASTER 表在前，ADMIN 表里与 MASTER 重名的跳过（顺序以先出现的为准）
    allCommands = list(MASTER_COMMANDS.keys())
    for command in ADMIN_COMMANDS:
        if command not in allCommands:
            allCommands.append(command)

    def _labelOf(command):
        entry = ADMIN_COMMAND_DOC.get(command)
        return entry[0] if entry else '骰主'

    lines = [
        '【PVP】管理指令一览（当前游戏：%s）' % gameName,
        '',
    ]
    for title, label in _ADMIN_DOC_GROUPS:
        commands = [c for c in allCommands if _labelOf(c) == label]
        if not commands:
            continue
        lines.append('== %s ==' % title)
        for command in commands:
            entry = ADMIN_COMMAND_DOC.get(command)
            body = entry[1] if entry else '说明待补，请提醒骰主更新本清单'
            lines.append('【PVP】%s　%s' % (command, body))
        lines.append('')

    # 结尾只留一个指路：玩家指令不在这一张清单里。
    # 以前这里还有一句「仅限后台管理员那几条骰主也做不了」——骰主已拥有管理员全部权限，
    # 那句话既不对也没必要，按骰主要求删掉了。
    lines.append('★ 玩家指令请看：【PVP】帮助')
    return '\n'.join(lines)


def unity_reply(plugin_event, Proc):
    """
    所有群消息的统一入口（由 main.Event.group_message 调用）。

    ★ 本函数**绝对不能**调用 plugin_event.set_block() ★
    我们只是「顺便看一眼」，`.ra` 等指令必须继续传给 OlivaDiceCore 处理。
    一旦把事件 block 掉，青果就不再往下分发，骰子会直接失效。
    """
    message = getattr(plugin_event.data, 'message', None)
    if message is None:
        return

    groupId = pvp.jfzCore.getGroupId(plugin_event)
    userId = pvp.jfzCore.getUserId(plugin_event)
    botHash = pvp.jfzCore.getBotHash(plugin_event)

    # 私聊里没有群上下文，本插件所有玩法都做不了。
    # （main.Event 没有实现 private_message，这里是双保险。）
    if not groupId:
        return

    text = pvp.jfzCore.stripLeadingSegments(message)
    if not text:
        return

    # ---- 第 1 步：先记下来，给骰子钩子用。放最前面，之后随便 return。----
    pvp.jfzDice.rememberCommand(botHash, groupId, userId, text)

    # 顺手把这条活事件存一份：后台线程（超时取消 / 时段结束 / 重启恢复）要发通知时
    # 复用它当发送上下文，比现造 fake 事件可靠得多。见 jfzSend.rememberEvent。
    pvp.jfzSend.rememberEvent(plugin_event, botHash)

    # ---- 第 2 步：`【PVP】xxx` ----
    command, args = pvp.jfzCore.parseCommand(text)
    if command is not None:
        _dispatchCommand(plugin_event, command, args)
        return

    # ---- 第 3 步：`【游戏名】PVP` ----
    gameName = pvp.jfzStore.getGameName()
    header = pvp.jfzCore.parseChallengeHeader(text, gameName)
    if header:
        pvp.jfzEngine.handleChallenge(plugin_event, header['发起人'], header['被发起人'])
        return

    # 游戏名对不上（比如骰主换了游戏名，玩家还用旧的）时给个明确提示。
    # 不提示的话玩家会以为机器人坏了 —— 这种「静默不响应」最难排查。
    if re.match(r'^【[^】]+】\s*[Pp][Vv][Pp]', text):
        if not gameName:
            pvp.jfzCore.replyText(
                plugin_event, '当前还没有注册游戏，请联系骰主注册后台群并声明游戏名。'
            )
        else:
            pvp.jfzCore.replyText(
                plugin_event,
                '当前游戏名是「%s」，发起 PVP 请用：\n\n'
                '【%s】PVP\n发起人：玩家名\n被发起人：玩家名' % (gameName, gameName)
            )
        return

    # ---- 第 4 步：`.raN` —— 交给青果，本插件只做「该不该现在掷」的提醒 ----
    if pvp.jfzCore.isRaCommand(text):
        _hintRaContext(plugin_event, botHash, groupId, userId)
        return


# ---------------------------------------------------------------------------
# 指令分发
# ---------------------------------------------------------------------------

def _dispatchCommand(plugin_event, command, args):
    """把 `【PVP】xxx yyy` 分发到对应处理函数。"""
    # ---- 帮助 ----
    if command in ['帮助', 'help', '说明', '规则']:
        pvp.jfzCore.replyText(plugin_event, getHelpText())
        return

    # ---- 骰主指令 ----
    if command in MASTER_COMMANDS:
        getattr(pvp.jfzAdmin, MASTER_COMMANDS[command])(plugin_event, args)
        return

    # ---- 后台管理指令 ----
    if command in ADMIN_COMMANDS:
        getattr(pvp.jfzAdmin, ADMIN_COMMANDS[command])(plugin_event, args)
        return

    # ---- 玩家指令 ----
    if command in PLAYER_COMMANDS:
        context = pvp.jfzCore.resolvePlayer(plugin_event)
        _dispatchPlayerCommand(plugin_event, command, args, context)
        return

    # ---- 使用道具 / 不使用（跨「对局中」和「对局外」两种语义）----
    if command == '使用道具':
        _handleUseItem(plugin_event, args)
        return
    if command == '不使用':
        _handleDecline(plugin_event)
        return

    # ---- 不认识 ----
    pvp.jfzCore.replyText(
        plugin_event,
        '不认识的指令：%s\n发送「【PVP】帮助」查看全部可用的指令。' % command
    )


def _dispatchPlayerCommand(plugin_event, command, args, context):
    """
    玩家指令的实际分发。

    每个指令的签名都不一样（有的只要 context、有的还要 args、有的两个都不要），
    所以这里逐条写清楚，而不是用 getattr 拼一个「万能调用」——
    万能调用在参数对不上时只会抛 TypeError，排查起来要翻半天。
    """
    if command == '商城':
        return pvp.jfzShop.handleShop(plugin_event, context)
    if command == '背包':
        return pvp.jfzShop.handleBackpack(plugin_event, context)
    if command == '铜币':
        return pvp.jfzShop.handleCopper(plugin_event, context)
    if command == '购买':
        return pvp.jfzShop.handleBuy(plugin_event, context, args)
    if command == '血量':
        return pvp.jfzEngine.handleStatus(plugin_event, context)
    if command == '战报':
        return pvp.jfzEngine.handleMyReport(plugin_event, context)
    if command == '排队':
        return pvp.jfzEngine.handleQueueInfo(plugin_event)
    return True


def _handleUseItem(plugin_event, args):
    """
    `【PVP】使用道具 道具名 [回合数]`

    ★ 这一条指令有**两种完全不同的语义**，靠「玩家在不在对局里」区分 ★
      在对局中（且处于道具声明阶段）→ 声明一件对战道具（胜临符/错影符/遁隐符）
      不在对局中                  → 立即使用一件场外道具（大还丹/小还丹/…）

    PRD 4.8.2 就是这么分的（「使用时机」那一列），所以路由层必须做这个判断。
    """
    tokens = pvp.jfzCore.splitArgs(args)
    if not tokens:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：\n'
            '对局中：【PVP】使用道具 胜临符 2　（回合型，第 2 回合使用）\n'
            '对局中：【PVP】使用道具 遁隐符　　（全场型）\n'
            '对局外：【PVP】使用道具 大还丹　　（场外型，立即生效）'
        )

    itemName = pvp.jfzCore.cleanName(tokens[0])
    roundText = tokens[1] if len(tokens) > 1 else ''

    context = pvp.jfzCore.resolvePlayer(plugin_event)
    playerName = context.get('name', '') if context.get('ok') else ''

    match = pvp.jfzStore.getMatch()
    inMatch = bool(playerName) and match.get('state') == pvp.jfzCore.MATCH_ACTIVE and playerName in [
        match.get('challenger_name'), match.get('target_name')
    ]

    if inMatch:
        return pvp.jfzEngine.handleUseItemInMatch(plugin_event, playerName, itemName, roundText)

    if not context.get('ok'):
        return pvp.jfzCore.replyText(plugin_event, context.get('reason', ''))
    return pvp.jfzShop.useOffFieldItem(plugin_event, playerName, itemName)


def _handleDecline(plugin_event):
    """`【PVP】不使用` —— 只在道具声明阶段有意义。"""
    context = pvp.jfzCore.resolvePlayer(plugin_event)
    if not context.get('ok'):
        return pvp.jfzCore.replyText(plugin_event, context.get('reason', ''))

    match = pvp.jfzStore.getMatch()
    playerName = context['name']
    if match.get('state') != pvp.jfzCore.MATCH_ACTIVE or playerName not in [
        match.get('challenger_name'), match.get('target_name')
    ]:
        return pvp.jfzCore.replyText(
            plugin_event, '你当前没有进行中的对局，无需声明。'
        )
    return pvp.jfzEngine.handleDeclareDone(plugin_event, playerName)


# ---------------------------------------------------------------------------
# 掷骰指令的「该不该现在掷」提醒
# ---------------------------------------------------------------------------

def _hintRaContext(plugin_event, botHash, groupId, userId):
    """
    玩家发 `.raN` 时给一点上下文提醒。

    ★ 这里**只是提醒**，绝不拦截、绝不回复代替骰子结果 ★
    掷骰本身由 OlivaDiceCore 处理（事件不会被 block），
    本插件要做的只是「说清楚这一掷到底算不算数」。

    三种情况：
      1. 这个群是个人群、且玩家正处在掷骰阶段 → 提示这是第几回合、要求值是多少
      2. 这个群是个人群、但玩家在别的阶段（比如还在声明道具）→ 明确说「现在不是掷骰阶段」
      3. 不是个人群 → 什么都不说（可能是普通的跑团掷骰，与本插件无关）
    """
    playerName = pvp.jfzStore.getPlayerNameByGroup(groupId)
    if not playerName:
        return

    playerData = pvp.jfzStore.getPlayer(playerName) or {}
    if str(playerData.get('user_qq', '')) != str(userId):
        # 群聊里其他人掷骰：与本插件无关，交给骰子正常处理。
        # 同时把待核对的指令清掉，避免钩子拿这条去和别人的掷骰记录比对。
        pvp.jfzDice.forgetCommand(botHash, groupId, userId)
        return

    match = pvp.jfzStore.getMatch()
    if match.get('state') != pvp.jfzCore.MATCH_ACTIVE or playerName not in [
        match.get('challenger_name'), match.get('target_name')
    ]:
        return

    stage = match.get('stage', '')
    if stage == pvp.jfzCore.STAGE_DECLARE:
        pvp.jfzCore.replyText(
            plugin_event,
            '本场对局还没有开始掷骰 —— 你现在处于道具声明阶段。\n'
            '请先发送「【PVP】使用道具 …」或「【PVP】不使用」完成声明。\n'
            '（刚才这一次掷骰不会计入本场。）'
        )
        return

    if stage != pvp.jfzCore.STAGE_FIGHT:
        return

    rolls = (match.get('rolls', {}) or {}).get(playerName, []) or []
    expected = pvp.jfzEngine.getExpectedRoundAttack(match, playerName, len(rolls))
    if expected is None:
        pvp.jfzCore.replyText(
            plugin_event, '你本场的 %d 个回合已经全部掷完，等待结算。' % int(match.get('rounds', 10))
        )
        return

    # 【掷骰前不再提示「现在是第 N 回合，需要发送 .raX」】
    # 以前这里先回一条引导、掷完再回一条「已记录」，一次掷骰冒出两条插件消息，
    # 玩家要求合并成一条。每回合掷什么开局时就整表发过了（_getRollListText），
    # 掷对了只有一条「第 N 回合已记录」，掷错了防作弊那关会提示重掷 ——
    # 这条引导没有任何不可替代的信息，去掉。


# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------

def getHelpText():
    """`【PVP】帮助` —— 让玩家不用翻文档就知道怎么玩。"""
    gameName = pvp.jfzStore.getGameName() or '游戏名'
    return (
        '【%s】PVP 玩法速查\n'
        '\n'
        '== 发起挑战（在自己的个人群发）==\n'
        '【%s】PVP\n'
        '发起人：你的玩家名\n'
        '被发起人：对方玩家名\n'
        '\n'
        '== 开战后 ==\n'
        '1. 道具声明：\n'
        '   【PVP】使用道具 胜临符 2　（第 2 回合你 +10）\n'
        '   【PVP】使用道具 错影符 4　（第 4 回合对方 -10）\n'
        '   【PVP】使用道具 遁隐符　　（整场取消）\n'
        '   【PVP】不使用\n'
        '2. 掷骰：按机器人给你的列表逐条发送 `.raN`，一个回合一条\n'
        '\n'
        '== 常用查询 ==\n'
        '【PVP】血量　　查看自己的血量 / 状态 / 铜币 / 今日次数\n'
        '【PVP】铜币　　只看自己的铜币\n'
        '【PVP】商城　　查看在售道具与价格\n'
        '【PVP】购买 大还丹　购买道具\n'
        '【PVP】背包　　查看自己的道具\n'
        '【PVP】使用道具 大还丹　场外回血（不在对局中时）\n'
        '【PVP】排队　　查看当前对局与排队情况\n'
        '【PVP】战报　　查看最近一场战报\n'
        '\n'
        '== 状态说明 ==\n'
        '血量 > 20：正常，可发起、可被挑战\n'
        '血量 1~20：濒死，不能主动发起，可被挑战\n'
        '血量 = 0：死皮，不能进行任何玩法\n'
        '\n'
        'RIP 与掉落：一方倒下时，其全部背包与铜币归胜利方。' % (gameName, gameName)
    )
