# -*- encoding: utf-8 -*-
'''
对局引擎 —— 排队、匹配、道具声明、掷骰、结算、血量与掉落。

================================================================
【一场 PVP 的完整生命周期】
================================================================
  玩家 A 在自己的个人群发「【尽伏诛】PVP / 发起人：A / 被发起人：B」
        │
        ├─ 校验（状态 / 次数 / 时段 / 对方是否已在战斗中）
        ├─ 入队，回复「已收到 PVP 请求，排队中。当前队列位置：N」
        │
        ▼ 轮到这条请求（没有别的对局在跑）
  排到通知 → 双群同时收到；**B 那边看不到 A 的身份，只显示「神秘对手」**
        │
        ▼ 道具声明子阶段（无超时，双方各自声明、互不可见）
  生成每回合 `.raN` 列表（含道具修正）→ 双方各收到自己那份
        │
        ▼ 掷骰子阶段（无超时）
  玩家逐条发 `.raN` → jfzDice 钩子捕获 → 校验本回合要求值 → 记账
        │
        ▼ 双方掷满
  结算子阶段：逐回合累计伤害 → 双方血量同时扣减 → 判定死亡与掉落
        │
        ▼
  双群播报战报 → 自动开启队列中的下一场

================================================================
【为什么全局只有一场对局在跑】
================================================================
PRD 4.3.2 只说了「队列先到先得、同一被发起人串行处理」，
4.6.2 说结算完要「自动开启队列中的下一场」。
把它读成「同时只有一场」有三个好处：
  1. 血量结算不会出现跨场次的读写竞争（A 的两场同时扣血会互相覆盖）；
  2. 「被其他 PVP 挑战」的关闭判定（PRD 4.9）有一个明确的时间边界；
  3. 实现和排查都简单 —— 状态就一个对象。
如果以后确实要并发多场，只需要把 gMatch 换成字典 + 给每场加锁，
其余逻辑不用动。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

import threading
import time
import traceback

import pvp


# 活动时段看守线程的轮询间隔（秒）。
# 30 秒足够精确到「每日结束时间」这一点 —— 活动时段是按分钟配的
# （14:00-18:00），误差 30 秒对玩法没有任何影响。
WATCH_INTERVAL_SECONDS = 30

_watcherThread = None
_watcherRunning = False


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _sendToPlayerGroup(botHash, playerName, text, plugin_event=None):
    """
    把一个玩家所在个人群推一条消息。

    【投递通道的优先级】
      1. plugin_event 不为 None 时，先尝试**活事件直发**（jfzSend.sendViaEvent）：
         活事件的 bot_info / control_queue 是框架刚填好的，和会话回复同一条
         被真机验证过的通道 —— 这是 AmourGame 的既有做法，也是最可靠的。
      2. 直发失败或没有活事件（后台线程、超时看守），回落到自建发送队列
         （fake 事件 + 限流 + 重试）。

    【为什么玩家不存在 / 群号为空要打日志而不是静默跳过】
    以前这里直接 return，一行日志都没有 —— 跨群推送又没有别的回执，
    结果就是「玩家收不到任何下文，后台却一片安宁」，完全无从排查。
    这两种情况都意味着注册数据有问题（玩家被删了 / 群号没登记），
    属于必须让骰主在控制台看到的事。
    """
    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        print('[pvp] ★ 跨群推送被跳过：玩家「%s」不在玩家表里（数据可能被清过）。' % playerName)
        return
    groupId = str(player.get('group_id', '') or '')
    if not groupId:
        print('[pvp] ★ 跨群推送被跳过：玩家「%s」没有登记个人群号。' % playerName)
        return
    if plugin_event is not None and pvp.jfzSend.sendViaEvent(plugin_event, groupId, text):
        return
    pvp.jfzSend.sendToGroup(botHash, groupId, text)


def _deliverToPlayer(plugin_event, botHash, playerName, text):
    """
    把一条消息送达某个玩家 —— **能用会话回复就用会话回复**。

    【为什么要有这个函数】这是对「真机上没有下文」问题的正面修复：
      - 玩家刚发过消息的那条会话，是**真机验证过最可靠**的通道
        （replyText 走 OlivaDiceCore.msgReply.replyMsg，被动回复优先）；
      - 跨群推送走自建队列 + fake 事件，链路长（control_queue → link 进程），
        任何一环出问题都是静默失败，玩家那头的表现就是「机器人没下文了」。
    所以原则是：**谁正在跟我们说话，谁的下文就直接回在他的会话里**，
    跨群推送只留给「不在场」的另一方（那是它唯一的通道）。

    plugin_event 为 None（超时看守、重启恢复等后台场景）时全部走跨群推送。
    """
    if plugin_event is not None:
        player = pvp.jfzStore.getPlayer(playerName)
        if player:
            currentGroupId = str(pvp.jfzCore.getGroupId(plugin_event) or '')
            playerGroupId = str(player.get('group_id', '') or '')
            if playerGroupId and playerGroupId == currentGroupId:
                pvp.jfzCore.replyText(plugin_event, text)
                return
    _sendToPlayerGroup(botHash, playerName, text, plugin_event=plugin_event)


def _playerGroupId(playerName):
    """取玩家登记的个人群号（字符串）。查不到就返回空串。"""
    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        return ''
    return str(player.get('group_id', '') or '')


def _sendToBothSides(botHash, match, challengerText, targetText, plugin_event=None):
    """
    同时给双方的个人群推消息。

    ★ 两条文本必须分开传，不能共用 ★
    因为被发起人那一侧的一切内容都要隐去发起人身份（PRD 4.3.3「被发起人全程不知道发起人身份」）。
    如果图省事让两边共用一条文本，就会出现「B 收到了 A 的名字」这种致命泄露，
    而且是那种上线后很难第一时间发现的泄露。

    ★ 两人在同一个群时，同一段文本不发两遍 ★
    正常玩法里一人一个个人群，但测试/演示时可能出现两人同群。
    超时取消通知这类「两侧文本完全相同」的消息如果照发两遍，
    在那个群里就是两条一模一样的消息 —— 用户明确讨厌重复消息。
    （文本不同的情况不做去重：那说明两侧内容本就不一样，各发各的。）
    """
    if challengerText == targetText:
        groupChallenger = _playerGroupId(match.get('challenger_name', ''))
        groupTarget = _playerGroupId(match.get('target_name', ''))
        if groupChallenger and groupChallenger == groupTarget:
            _sendToPlayerGroup(
                botHash, match.get('challenger_name', ''), challengerText,
                plugin_event=plugin_event,
            )
            return
    _sendToPlayerGroup(botHash, match.get('challenger_name', ''), challengerText, plugin_event=plugin_event)
    _sendToPlayerGroup(botHash, match.get('target_name', ''), targetText, plugin_event=plugin_event)


def _opponentOf(match, playerName):
    """取对手的玩家名。"""
    if playerName == match.get('challenger_name'):
        return match.get('target_name', '')
    return match.get('challenger_name', '')


def _isTargetSide(match, playerName):
    """这个玩家是不是「被发起人」。"""
    return playerName == match.get('target_name')


def _displayOpponent(match, playerName):
    """
    对手在**这个人眼里**应该显示成什么。

    ★ 这是匿名规则唯一的落点 ★
    PRD 4.3.3：被发起人全程不知道发起人身份 → 被发起人那一侧一律显示「神秘对手」。
    """
    return '神秘对手' if _isTargetSide(match, playerName) else _opponentOf(match, playerName)


# ---------------------------------------------------------------------------
# 排队（PRD 4.3）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleChallenge(plugin_event, challengerName, targetName):
    """
    处理「【游戏名】PVP / 发起人：X / 被发起人：Y」。

    校验顺序照抄 PRD 4.3.1 列举的顺序，只是想给玩家的提示更有针对性：
      发起人已注册 → 发起人状态正常 → 当日次数 → 活动时段
      → 自己不在对局/队列里 → 被发起人已注册 → 被发起人状态 → 被发起人不在对局里
    """
    botHash = pvp.jfzCore.getBotHash(plugin_event)
    groupId = pvp.jfzCore.getGroupId(plugin_event)
    userId = pvp.jfzCore.getUserId(plugin_event)
    gameName = pvp.jfzStore.getGameName()

    if not pvp.jfzStore.isRegistered():
        return pvp.jfzCore.replyText(
            plugin_event, '当前还没有注册游戏，请联系骰主执行「【PVP】注册后台群 游戏名」。'
        )

    # --- 发起人 ---
    challenger = pvp.jfzStore.getPlayer(challengerName)
    if not challenger:
        return pvp.jfzCore.replyText(
            plugin_event, '找不到玩家「%s」，请确认名称或联系管理注册。' % challengerName
        )

    # 发起指令必须发在发起人自己的个人群里。
    # 否则任何人都能在任何群里冒名发起 —— 那会让「当日主动次数」形同虚设。
    if str(challenger.get('group_id', '')) != str(groupId):
        return pvp.jfzCore.replyText(
            plugin_event,
            '发起 PVP 必须在本群（你的个人群）内进行。\n'
            '「%s」的个人群是 %s。' % (challengerName, challenger.get('group_id', ''))
        )
    if str(challenger.get('user_qq', '')) != str(userId):
        return pvp.jfzCore.replyText(
            plugin_event, '只有玩家本人才能以自己的名义发起 PVP。'
        )

    # 发起人必须「正常」（PRD 4.3.1 + 3.1：濒死不能主动发起）
    challengerStatus = pvp.jfzCore.deriveStatus(challenger.get('hp', 0))
    if challengerStatus != pvp.jfzCore.STATUS_NORMAL:
        return pvp.jfzCore.replyText(
            plugin_event,
            '你当前状态为「%s」（血量 %d），无法主动发起 PVP。\n'
            '%s' % (
                pvp.jfzCore.getStatusName(challengerStatus),
                int(challenger.get('hp', 0)),
                '血量恢复到 20 以上才能发起。' if challengerStatus == pvp.jfzCore.STATUS_DYING
                else '死皮状态下无法进行任何玩法。',
            )
        )

    # 当日主动次数（PRD 4.2 设置主动次数）
    limit = pvp.jfzStore.getConfigInt('daily_attack_limit', 3)
    used = pvp.jfzStore.getDailyAttackUsed(challengerName)
    if used >= limit:
        return pvp.jfzCore.replyText(
            plugin_event, '你今天的主动发起次数已经用完了（%d / %d）。' % (used, limit)
        )

    # 活动时段（PRD 4.2）
    inWindow, reason = pvp.jfzCore.isInActivityWindow(pvp.jfzStore.getActivityConfig())
    if not inWindow:
        return pvp.jfzCore.replyText(plugin_event, '当前无法发起 PVP。\n%s' % reason)

    # 自己不能在战斗中 / 不能已有排队中的请求（PRD 4.9）
    if pvp.jfzStore.isPlayerInMatch(challengerName):
        return pvp.jfzCore.replyText(
            plugin_event, '你正在 PVP 对局中，无法发起新的 PVP。'
        )
    if pvp.jfzStore.isPlayerInQueue(challengerName):
        return pvp.jfzCore.replyText(
            plugin_event, '你已经有一条 PVP 请求在排队中，请等它处理完再发起。'
        )

    # --- 被发起人 ---
    target = pvp.jfzStore.getPlayer(targetName)
    if not target:
        return pvp.jfzCore.replyText(
            plugin_event, '找不到玩家「%s」，请确认名称或联系管理注册。' % targetName
        )
    if targetName == challengerName:
        return pvp.jfzCore.replyText(plugin_event, '不能挑战自己。')

    targetStatus = pvp.jfzCore.deriveStatus(target.get('hp', 0))
    if targetStatus == pvp.jfzCore.STATUS_DEAD:
        return pvp.jfzCore.replyText(
            plugin_event, '「%s」当前是死皮状态，无法被挑战。' % targetName
        )

    if pvp.jfzStore.isPlayerInMatch(targetName):
        return pvp.jfzCore.replyText(
            plugin_event, '「%s」正在对局中，请稍后再试。' % targetName
        )

    # --- 通过校验，入队 ---
    matchId = pvp.jfzStore.generateMatchId()
    request = {
        'match_id': matchId,
        'challenger_name': challengerName,
        'challenger_qq': str(challenger.get('user_qq', '')),
        'challenger_group': str(challenger.get('group_id', '')),
        'target_name': targetName,
        'target_qq': str(target.get('user_qq', '')),
        'target_group': str(target.get('group_id', '')),
        'challenger_hp_before': int(challenger.get('hp', 0)),
        'target_hp_before': int(target.get('hp', 0)),
        'created_at': pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S'),
        'bot_hash': botHash,
        'game_name': gameName,
    }
    position = pvp.jfzStore.enqueueRequest(request)

    # 次数在「发起时」扣掉（PRD 4.3.1 的校验点就在发起这一步）。
    # 这条请求如果一直没被匹配上就被取消（比如到了收工时间），
    # 由 cancelAll 退还 —— 见那里的说明。
    pvp.jfzStore.addDailyAttackUsed(challengerName, 1)

    # 【回执要交代「接下来会发生什么」】
    # 以前只回「排队中。当前队列位置：N」，玩家不知道 N 意味着什么、
    # 要等多久；更糟的是「前面还有一场对局在打」的时候，回执发完就真的
    # 再也没有下文了（对局打完才会轮到这条请求）。所以这里把状态说清楚：
    #   - 有对局在打：明确告诉发起人「要等当前这场打完才会轮到你」
    #   - 前面还有别的排队请求：告诉他排在几个人后面
    replyText = '已收到 PVP 请求，排队中。'
    if pvp.jfzStore.hasActiveMatch():
        replyText += '\n当前有一场对局正在进行，结束后会自动开始你的对局。'
    elif position > 1:
        replyText += '\n你前面还有 %d 条排队中的请求，轮到后会自动开始。' % (position - 1)
    pvp.jfzCore.replyText(plugin_event, replyText)

    # 如果当前没有对局在跑，立刻尝试开打。
    # 把 plugin_event 带下去：发起人正站在这条会话里，
    # 他的「对局已匹配 + 声明引导」必须直接回在这里，不能指望跨群推送。
    startNextMatch(botHash, plugin_event=plugin_event)


# ---------------------------------------------------------------------------
# 匹配与开局（PRD 4.3.2 / 4.3.3 / 4.5.1）
# ---------------------------------------------------------------------------

def startNextMatch(botHash, plugin_event=None):
    """
    尝试把队首请求变成一场对局。

    没有对局在跑时才动；队首请求里的双方在**这个时候**要重新校验一遍 ——
    因为从入队到出队之间可能过了很久（前面还在打），
    期间可能有人被打死了、或者到了收工时间。

    plugin_event：触发本次开局的消息事件（如果有的话）。
    发起人那边能借此走会话直发；后台链式开局（上一场结算后）没有事件，走跨群推送。
    """
    if pvp.jfzStore.hasActiveMatch():
        return False

    while True:
        request = pvp.jfzStore.popQueue()
        if request is None:
            return False

        ok, reason = _validateRequestBeforeStart(request)
        if not ok:
            # 请求作废：把主动次数还给发起人（对局从未发生，不该算数）
            challengerName = request.get('challenger_name', '')
            if challengerName:
                pvp.jfzStore.addDailyAttackUsed(challengerName, -1)
                _sendToPlayerGroup(
                    botHash, challengerName,
                    '你对「%s」发起的 PVP 无法开始：%s\n本次不计入主动次数。' % (
                        request.get('target_name', ''), reason
                    ),
                    plugin_event=plugin_event,
                )
            continue

        _beginMatch(request, botHash, plugin_event=plugin_event)
        return True


def _validateRequestBeforeStart(request):
    """出队前的重新校验。返回 (可以开始?, 不能开始的原因)。"""
    inWindow, reason = pvp.jfzCore.isInActivityWindow(pvp.jfzStore.getActivityConfig())
    if not inWindow:
        return False, reason

    challenger = pvp.jfzStore.getPlayer(request.get('challenger_name', ''))
    target = pvp.jfzStore.getPlayer(request.get('target_name', ''))
    if not challenger or not target:
        return False, '对局双方中有人已被删除。'

    challengerStatus = pvp.jfzCore.deriveStatus(challenger.get('hp', 0))
    if challengerStatus != pvp.jfzCore.STATUS_NORMAL:
        return False, '发起人「%s」当前状态为「%s」，无法战斗。' % (
            request.get('challenger_name', ''), pvp.jfzCore.getStatusName(challengerStatus)
        )

    targetStatus = pvp.jfzCore.deriveStatus(target.get('hp', 0))
    if targetStatus == pvp.jfzCore.STATUS_DEAD:
        return False, '被发起人「%s」当前已是死皮状态。' % request.get('target_name', '')

    if pvp.jfzStore.isPlayerInMatch(request.get('challenger_name', '')):
        return False, '发起人已在其他对局中。'
    if pvp.jfzStore.isPlayerInMatch(request.get('target_name', '')):
        return False, '被发起人已被匹配到其他对局。'

    return True, ''


def _beginMatch(request, botHash, plugin_event=None):
    """
    建对局、双群通知、进入道具声明子阶段。

    plugin_event：发起人刚发完挑战消息的事件 ——
    发起人本人的两条通知（排到 + 声明引导）走**会话直发**，
    被发起人不在场，只能走跨群推送（这是他唯一的通道）。

    【为什么「排到通知」和「道具声明引导」要分两条发】
    PRD 4.3.3 与 4.4 是两段内容。合成一条在手机上会非常长
    （被发起人那边尤其），拆开发玩家一眼就能看到「轮到你了，该干什么」。
    """
    gameTag = pvp.jfzCore.gameTag(pvp.jfzStore.getGameName())

    match = {
        'match_id': request.get('match_id', pvp.jfzStore.generateMatchId()),
        'state': pvp.jfzCore.MATCH_ACTIVE,
        'stage': pvp.jfzCore.STAGE_DECLARE,
        'challenger_name': request.get('challenger_name', ''),
        'challenger_qq': request.get('challenger_qq', ''),
        'challenger_group': request.get('challenger_group', ''),
        'target_name': request.get('target_name', ''),
        'target_qq': request.get('target_qq', ''),
        'target_group': request.get('target_group', ''),
        'rounds': pvp.jfzStore.getConfigInt('rounds', 10),
        'challenger_hp_before': int(request.get('challenger_hp_before', 0)),
        'target_hp_before': int(request.get('target_hp_before', 0)),
        'created_at': request.get('created_at', ''),
        # 进入声明阶段的时刻。声明超时（declare_timeout_minutes）从这里起算。
        # 用独立字段而不是复用 created_at：created_at 是「发起请求」的时刻，
        # 请求可能在队列里等了半小时才轮到，拿它算超时会冤枉刚拿到对局的人。
        'declare_started_at': pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S'),
        'bot_hash': botHash,
        'game_name': pvp.jfzStore.getGameName(),
        # 声明表：{玩家名: {'done': bool, 'items': [{'name':.., 'round':int|None}]}}
        'declared': {},
        # 掷骰记录：{玩家名: [{'roll':.., 'skill':.., 'level':.., 'damage':..}]}
        'rolls': {},
        # 每回合应有的 .raN：{玩家名: [int, int, ...]}
        'round_plan': {},
        # 已实际扣减的道具账本，用于「取消对局时返还」
        'consumed': [],
        'round_results': [],
        'cancel_reason': '',
    }
    pvp.jfzStore.setMatch(match)

    # --- 排到通知 ---
    challengerText = (
        '%sPVP\n'
        '发起人：%s\n'
        '被发起人：%s\n\n'
        '对局已匹配，即将进入道具声明阶段。' % (
            gameTag, match['challenger_name'], match['target_name']
        )
    )
    # ★ 被发起人这一侧不出现发起人 QQ 与名称（PRD 验收标准 6）★
    # 保留 PRD 6 模板的两行形状，只把名字换成占位符 —— 这样被发起人一眼就知道
    # 「这是一场 PVP，我是被发起人」，但拿不到发起人是谁。
    targetText = (
        '%sPVP\n'
        '发起人：神秘对手\n'
        '被发起人：%s\n\n'
        '对局已匹配，即将进入道具声明阶段。' % (gameTag, match['target_name'])
    )
    # 发起人正在会话里，直接回；被发起人不在场，
    # 用发起人的活事件直发到他的群（比 fake 事件队列可靠得多）。
    _deliverToPlayer(
        plugin_event, botHash, match.get('challenger_name', ''), challengerText
    )
    _sendToPlayerGroup(
        botHash, match.get('target_name', ''), targetText, plugin_event=plugin_event
    )

    # --- 道具声明引导 ---
    _deliverToPlayer(
        plugin_event, botHash, match.get('challenger_name', ''), getDeclareGuide()
    )
    _sendToPlayerGroup(
        botHash, match.get('target_name', ''), getDeclareGuide(), plugin_event=plugin_event
    )

    print('[pvp] 对局开始：%s vs %s（%s）' % (
        match['challenger_name'], match['target_name'], match['match_id']
    ))


def getDeclareGuide():
    """
    道具声明引导文本（PRD 6「道具声明」模板）。

    ★ 相对 PRD 模板加了两行说明 ★
    1. 「可连续发送」：PRD 的模板把「不使用」解释成「不使用任何道具」，
       但 PRD 4.2 又允许管理把「每种道具限用 N」配成大于 1，
       那就必须能连续声明多件 —— 否则 N=2 这个配置永远不可能生效。
       只发一条【PVP】不使用 的用法完全不变，是**严格超集**。
    2. 「声明限时」：声明超时（declare_timeout_minutes）大于 0 时明确告知，
       免得玩家以为可以无限期拖下去。
    """
    text = (
        '【是否使用道具】\n'
        '请回复以下指令之一：\n\n'
        '【PVP】使用道具 道具名 回合数    （回合型，回合数 = 第几回合使用）\n'
        '【PVP】使用道具 道具名           （全场型，不写回合号）\n'
        '【PVP】不使用                      （声明完毕）\n\n'
        '声明后不可撤回，双方声明完毕即开战。\n'
        '如需使用多件道具，可连续发送多条「【PVP】使用道具」，\n'
        '最后发送「【PVP】不使用」表示声明完毕。'
    )
    timeoutMinutes = pvp.jfzStore.getConfigInt('declare_timeout_minutes', 10)
    if timeoutMinutes > 0:
        text += '\n\n★ 声明阶段限时 %d 分钟，超时对局会自动取消并返还。' % timeoutMinutes
    return text


# ---------------------------------------------------------------------------
# 道具声明子阶段（PRD 4.4 / 4.8.2 / 4.8.3）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleUseItemInMatch(plugin_event, playerName, itemName, roundText=''):
    """
    `【PVP】使用道具 道具名 [回合数]` —— 对局中的道具声明。

    返回 True 表示已处理并回复过。

    校验顺序（每条失败都给出「为什么不行」+「怎么办」）：
      对局中 → 声明阶段 → 道具存在 → 类型匹配 → 背包有货 → 回合号合法
      → 该回合没被别人占 → 本场这种道具没超限
    """
    match = pvp.jfzStore.getMatch()
    botHash = pvp.jfzCore.getBotHash(plugin_event)

    if match.get('stage') != pvp.jfzCore.STAGE_DECLARE:
        return pvp.jfzCore.replyText(
            plugin_event, '当前不在道具声明阶段。'
        )

    declared = match.get('declared', {})
    if not isinstance(declared, dict):
        declared = {}
    myDeclare = declared.get(playerName, {'done': False, 'items': []})
    if myDeclare.get('done'):
        return pvp.jfzCore.replyText(
            plugin_event, '你已经完成声明，不能再修改（声明后不可撤回）。'
        )

    itemDef = pvp.jfzCore.getItemDef(itemName)
    if itemDef is None:
        return pvp.jfzCore.replyText(
            plugin_event,
            '没有「%s」这件道具。\n当前可声明的道具：遁隐符 / 胜临符 / 错影符。' % itemName
        )

    itemType = str(itemDef.get('type', ''))

    # 场外型不能在战斗里声明（PRD 4.8.2：使用时机是「不在 PVP 中时」）
    if itemType == pvp.jfzCore.TYPE_OFF_FIELD:
        return pvp.jfzCore.replyText(
            plugin_event,
            '「%s」是场外型道具，不能在 PVP 中使用。\n'
            '请在不在对局中时发送「【PVP】使用道具 %s」。' % (itemName, itemName)
        )

    # 回合型必须带回合号；全场型不能带
    roundNo = None
    if itemType == pvp.jfzCore.TYPE_ROUND:
        roundNo = pvp.jfzCore.toInt(roundText, None)
        totalRounds = int(match.get('rounds', 10))
        if roundNo is None:
            return pvp.jfzCore.replyText(
                plugin_event,
                '「%s」是回合型道具，必须写明第几回合使用。\n'
                '例：【PVP】使用道具 %s 2' % (itemName, itemName)
            )
        if roundNo < 1 or roundNo > totalRounds:
            return pvp.jfzCore.replyText(
                plugin_event, '回合数必须在 1 ~ %d 之间。' % totalRounds
            )
    else:
        if roundText and str(roundText).strip():
            return pvp.jfzCore.replyText(
                plugin_event,
                '「%s」是全场型道具，不需要写回合数。\n'
                '例：【PVP】使用道具 %s' % (itemName, itemName)
            )

    # 背包里必须有货（PRD 4.8.3：声明时立即扣减）
    inventory = pvp.jfzStore.getInventory(playerName)
    if int(inventory.get(itemName, 0)) <= 0:
        return pvp.jfzCore.replyText(
            plugin_event,
            '你的背包里没有「%s」。\n可发送「【PVP】商城」购买，「【PVP】背包」查看。' % itemName
        )

    myItems = myDeclare.get('items', [])
    if not isinstance(myItems, list):
        myItems = []

    # --- 同一个回合只能放一个道具（双方合计，PRD 4.8.2）---
    if roundNo is not None:
        occupiedBy = _getRoundItemOwner(match, roundNo)
        if occupiedBy is not None:
            ownerName, ownerItem = occupiedBy
            if ownerName == playerName:
                return pvp.jfzCore.replyText(
                    plugin_event,
                    '第 %d 回合你已声明了「%s」，同一回合只能放置一个道具。' % (roundNo, ownerItem)
                )
            return pvp.jfzCore.replyText(
                plugin_event,
                '第 %d 回合已经有道具了，同一回合只能放置一个道具（双方合计）。' % roundNo
            )
    else:
        # 全场型（遁隐符）：整场只能有一张，两边都不例外
        for sideName, sideDeclare in declared.items():
            for entry in sideDeclare.get('items', []) or []:
                if entry.get('name') == itemName:
                    return pvp.jfzCore.replyText(
                        plugin_event,
                        '本场已经有一张「%s」了（由%s声明），同一种道具一场只能有一张。' % (
                            itemName, '你' if sideName == playerName else '对方'
                        )
                    )

    # --- 每种道具本场限用次数（PRD 4.2 设置每种道具限用）---
    limit = pvp.jfzStore.getConfigInt('item_limit_per_match', 1)
    usedThisItem = len([e for e in myItems if e.get('name') == itemName])
    if usedThisItem >= limit:
        return pvp.jfzCore.replyText(
            plugin_event,
            '本场「%s」你已经声明过 %d 次，达到上限（每种道具每场限用 %d 次）。' % (
                itemName, usedThisItem, limit
            )
        )

    # --- 通过校验：先扣道具，再记账 ---
    ok, leftCount = pvp.jfzStore.removeItem(playerName, itemName, 1)
    if not ok:
        return pvp.jfzCore.replyText(
            plugin_event, '你的背包里没有「%s」。' % itemName
        )

    consumed = match.get('consumed', [])
    if not isinstance(consumed, list):
        consumed = []
    consumed.append({'player': playerName, 'item': itemName, 'round': roundNo})

    myItems.append({'name': itemName, 'round': roundNo})
    myDeclare['items'] = myItems
    myDeclare.setdefault('done', False)
    declared[playerName] = myDeclare

    pvp.jfzStore.updateMatch(declared=declared, consumed=consumed)

    if roundNo is not None:
        effectText = '第 %d 回合生效' % roundNo
    else:
        effectText = '本场生效'
    pvp.jfzCore.replyText(
        plugin_event,
        '已声明「%s」（%s）。剩余 %d 件。\n'
        '如需继续声明请再发「【PVP】使用道具 …」，'
        '声明完毕后发送「【PVP】不使用」。' % (itemName, effectText, leftCount)
    )

    # 声明完就检查一次双方是否都完成
    _checkDeclareFinished(botHash, plugin_event=plugin_event)
    return True


def _getRoundItemOwner(match, roundNo):
    """
    查第 roundNo 回合被谁放了道具。返回 (玩家名, 道具名) 或 None。

    ★ 必须查**双方** ★
    PRD 4.8.2 写的是「同一个回合只能放置一个道具（双方合计）」
    —— 只查自己的会漏掉「A 放了第 2 回合、B 也放第 2 回合」这种冲突，
    结果是同一回合叠了两个修正，` .raN ` 算出来和谁都对不上。
    """
    declared = match.get('declared', {})
    if not isinstance(declared, dict):
        return None
    for playerName, sideDeclare in declared.items():
        for entry in (sideDeclare.get('items') or []):
            if entry.get('round') == roundNo:
                return playerName, entry.get('name', '')
    return None


@pvp.jfzCore.handled
def handleDeclareDone(plugin_event, playerName):
    """`【PVP】不使用` —— 声明完毕（PRD 4.4）。"""
    match = pvp.jfzStore.getMatch()
    if match.get('stage') != pvp.jfzCore.STAGE_DECLARE:
        return pvp.jfzCore.replyText(plugin_event, '当前不在道具声明阶段。')

    declared = match.get('declared', {})
    if not isinstance(declared, dict):
        declared = {}
    myDeclare = declared.get(playerName, {'done': False, 'items': []})
    if myDeclare.get('done'):
        return pvp.jfzCore.replyText(plugin_event, '你已经完成声明了。')

    myDeclare['done'] = True
    myDeclare.setdefault('items', [])
    declared[playerName] = myDeclare
    pvp.jfzStore.updateMatch(declared=declared)

    if myDeclare['items']:
        itemText = '、'.join('%s' % entry.get('name') for entry in myDeclare['items'])
        pvp.jfzCore.replyText(
            plugin_event, '已记录你的声明（%s），声明完毕。' % itemText
        )
    else:
        pvp.jfzCore.replyText(plugin_event, '已记录：本场不使用道具，声明完毕。')

    _checkDeclareFinished(pvp.jfzCore.getBotHash(plugin_event), plugin_event=plugin_event)
    return True


def _checkDeclareFinished(botHash, plugin_event=None):
    """双方都声明完毕 → 生成掷骰列表，进入掷骰子阶段。"""
    match = pvp.jfzStore.getMatch()
    if not match or match.get('stage') != pvp.jfzCore.STAGE_DECLARE:
        return

    declared = match.get('declared', {})
    if not isinstance(declared, dict):
        return

    for name in [match.get('challenger_name'), match.get('target_name')]:
        side = declared.get(name, {})
        if not isinstance(side, dict) or not side.get('done'):
            return

    # 双方都声明完毕 → 开战
    _startFight(botHash, match, plugin_event=plugin_event)


def _startFight(botHash, match, plugin_event=None):
    """
    道具声明结束 → 生成每回合 `.raN` 列表 → 进入掷骰子阶段。

    plugin_event：最后一个声明完的玩家的事件 —— 他的掷骰列表直接回在
    他的会话里（他刚发完「不使用」，人就在场）；对方走跨群推送。

    ★ 遁隐符要在这里拦一道 ★
    它声明之后整场就取消了，不需要再掷骰。PRD 4.4 说「双方声明完毕即开战」，
    但 4.8.2 又说遁隐符是「整场取消」—— 显然应该是「声明完立刻取消」，
    而不是「开打了再取消」。所以这里先检查有没有遁隐符。
    """
    declared = match.get('declared', {})
    for name in [match.get('challenger_name'), match.get('target_name')]:
        for entry in (declared.get(name, {}) or {}).get('items', []) or []:
            if entry.get('name') == '遁隐符':
                _finishByEscape(botHash, match, name, plugin_event=plugin_event)
                return

    rounds = int(match.get('rounds', 10))

    challengerName = match.get('challenger_name', '')
    targetName = match.get('target_name', '')

    plan = {}
    for name in [challengerName, targetName]:
        # 基础值按**人**取，不是全场一个数：骰主可以单独给某人设过攻击值。
        # 「个人值 → 全局值」的回落逻辑统一收在 store 层（getPlayerAttack），
        # 这里不重复判断，免得两边规则跑偏。
        plan[name] = _buildRoundPlan(match, name, pvp.jfzStore.getPlayerAttack(name), rounds)

    match = pvp.jfzStore.updateMatch(
        stage=pvp.jfzCore.STAGE_FIGHT,
        round_plan=plan,
        rolls={challengerName: [], targetName: []},
        # 掷骰阶段的起始时刻，供掷骰超时兜底计时用（见 _checkFightTimeout）
        fight_started_at=pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S'),
    )

    for name in [challengerName, targetName]:
        _deliverToPlayer(plugin_event, botHash, name, _getRollListText(match, name))


def _buildRoundPlan(match, playerName, baseAttack, rounds):
    """
    算某个人每个回合的 `.raN` 值（PRD 4.5.1）：

        基础值 = 该玩家的攻击值（个人值优先，否则全局初始攻击，见 getPlayerAttack）
        + 自身胜临符修正（该回合 +10）
        + 对方错影符修正（该回合 -10）

    返回长度 = rounds 的列表。列表下标是**回合号 - 1**，
    和玩家看到的「第 N 回合」差 1，所以取用时统一用 roundNo - 1，
    免得两处各写一套、来回换算错。
    """
    opponent = _opponentOf(match, playerName)
    declared = match.get('declared', {})
    plan = [int(baseAttack)] * int(rounds)

    def apply(selfName, entry):
        roundNo = entry.get('round')
        if not isinstance(roundNo, int) or roundNo < 1 or roundNo > len(plan):
            return
        itemName = entry.get('name')
        itemDef = pvp.jfzCore.getItemDef(itemName)
        if not itemDef:
            return
        if selfName == playerName and 'self_mod' in itemDef:
            plan[roundNo - 1] += int(itemDef.get('self_mod', 0))
        elif selfName == opponent and 'enemy_mod' in itemDef:
            plan[roundNo - 1] += int(itemDef.get('enemy_mod', 0))

    for entry in ((declared.get(playerName, {}) or {}).get('items') or []):
        apply(playerName, entry)
    for entry in ((declared.get(opponent, {}) or {}).get('items') or []):
        apply(opponent, entry)

    # 夹到青果认识的骰值区间 [1, 100]。
    # 下界：`.ra0` / `.ra-10` 是非法指令（错影符叠加就可能压到 0 以下）；
    # 上界：`.ra110` 同样是非法指令 —— 攻击值本身最大 100，
    #       再叠加胜临符 +10 就会超出，所以这里必须夹，
    #       否则玩家会收到一张「照着发但毫无反应」的掷骰列表。
    for index in range(len(plan)):
        if plan[index] < pvp.jfzCore.ATTACK_MIN:
            plan[index] = pvp.jfzCore.ATTACK_MIN
        elif plan[index] > pvp.jfzCore.ATTACK_MAX:
            plan[index] = pvp.jfzCore.ATTACK_MAX
    return plan


def _getRollListText(match, playerName):
    """
    渲染掷骰列表（PRD 6「掷骰列表」模板）。

    每行带上「这个数值为什么和基础值不同」的注解 ——
    PRD 4.5.1 的模板里就写了 `← 你用了胜临符` 这种说明。
    没有注解的话玩家只会觉得「怎么有的回合要掷 60、有的要掷 40」，
    然后就会来问客服。
    """
    plan = (match.get('round_plan', {}) or {}).get(playerName, [])
    rounds = int(match.get('rounds', 10))
    opponent = _opponentOf(match, playerName)
    declared = match.get('declared', {})

    # 建立「第 N 回合 → 注解文本」的映射
    notes = {}
    for entry in ((declared.get(playerName, {}) or {}).get('items') or []):
        if entry.get('round'):
            notes[entry['round']] = '← 你用了%s' % entry.get('name')
    for entry in ((declared.get(opponent, {}) or {}).get('items') or []):
        if entry.get('round'):
            notes[entry['round']] = '← 对方用了%s' % entry.get('name')

    lines = ['【本场共 %d 回合，你的每回合掷骰值如下】' % rounds, '']
    for index in range(rounds):
        value = plan[index] if index < len(plan) else 0
        roundNo = index + 1
        line = '第 %d 回合：.ra%d' % (roundNo, value)
        if roundNo in notes:
            line += '      %s' % notes[roundNo]
        lines.append(line)
    lines.append('')
    lines.append('请逐条发送，一个回合发一条。')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 遁隐符（PRD 4.7.2 / 4.8.3）
# ---------------------------------------------------------------------------

def _finishByEscape(botHash, match, escapePlayerName, plugin_event=None):
    """
    遁隐符：整场取消，血量维持，背包不动，对局立即 [已结束]。

    【道具账怎么算】（PRD 4.8.3 明确写了）
      - 用符的一方：**消耗**遁隐符（不返还）
      - 对方：已声明的道具**全部返还**
    所以返还时要把账本里「对方」的条目退回去，用符自己那条跳过。
    """
    opponent = _opponentOf(match, escapePlayerName)

    # 道具账（PRD 4.8.3）：
    #   - 用符的一方：**消耗**那张遁隐符（不返还）
    #   - 其余一切已声明扣减的道具：**全部返还**（双方都一样）
    #
    # 【为什么用符那一方「别的道具」也要退】
    # PRD 写的是「使用遁隐符的一方消耗**该符**；对方已声明的道具返还」。
    # 它只说了「符」本身被消耗，没说用符的人其他道具要一起没收。
    # 而对当事人来说，他声明完遁隐符之后整场就结束了 ——
    # 如果连他一起声明的胜临符也扣掉，等于「用了逃跑道具还要额外被罚」，
    # 这既没有规则依据，也很难跟玩家解释。所以只扣符本身。
    refunded = _refundConsumedItems(match, skipEntries=[(escapePlayerName, '遁隐符')])

    gameTag = pvp.jfzCore.gameTag(match.get('game_name', ''))

    # 两边看到的文本不同：被发起人那一侧不能出现别人名字
    challengerName = match.get('challenger_name', '')
    if escapePlayerName == challengerName:
        challengerText = (
            '%sPVP 已结束\n你使用了「遁隐符」，本场对局立即结束。\n'
            '血量维持，背包不动。' % gameTag
        )
        targetText = (
            '%sPVP 已结束\n对方使用了「遁隐符」，本场对局立即结束。\n'
            '血量维持，背包不动；你已声明的道具已返还。' % gameTag
        )
    else:
        challengerText = (
            '%sPVP 已结束\n对方使用了「遁隐符」，本场对局立即结束。\n'
            '血量维持，背包不动；你已声明的道具已返还。' % gameTag
        )
        targetText = (
            '%sPVP 已结束\n你使用了「遁隐符」，本场对局立即结束。\n'
            '血量维持，背包不动。' % gameTag
        )

    if refunded:
        text = '本次返还的道具：%s' % refunded
        challengerText += '\n' + text
        targetText += '\n' + text

    _finishMatch(botHash, match, reason='escape')
    # 用符的人刚发完声明、人就在场 → 会话直发；对方走活事件直发到他的群。
    _deliverToPlayer(plugin_event, botHash, challengerName, challengerText)
    _deliverToPlayer(plugin_event, botHash, match.get('target_name', ''), targetText)

    print('[pvp] 对局因遁隐符结束：%s 使用，对手 %s' % (escapePlayerName, opponent))

    startNextMatch(botHash, plugin_event=plugin_event)


# ---------------------------------------------------------------------------
# 掷骰子阶段（PRD 4.5）
# ---------------------------------------------------------------------------

def getExpectedRoundAttack(match, playerName, rollIndex):
    """
    取某个人「第 rollIndex 次掷骰」（0 起算）应当使用的 `.raN` 值。

    掷满了（rollIndex >= 回合数）返回 None —— 表示这之后的掷骰都不该计分。
    """
    plan = (match.get('round_plan', {}) or {}).get(playerName, [])
    if not isinstance(plan, list):
        return None
    if rollIndex < 0 or rollIndex >= len(plan):
        return None
    try:
        return int(plan[rollIndex])
    except Exception:
        return None


def onAttackValueMismatch(plugin_event, playerName, gotValue, expectedValue, roundNo):
    """
    掷骰值与本回合要求不符 → 不计分，提示重掷（PRD 4.5.3 / 6）。

    ★ 这是防作弊的关键一环 ★
    玩家想发 `.ra99` 提高成功率时，`RDRecordSkillInt = 99 ≠ 要求值`，
    在这里被拦下。不拦的话「每个回合要求多少」这套设计就完全失效了。
    """
    return pvp.jfzCore.replyText(
        plugin_event,
        '掷骰值与要求不符。\n'
        '本回合要求：.ra%d\n'
        '你发送的是：.ra%d\n'
        '请重新发送 .ra%d。' % (expectedValue, gotValue, expectedValue)
    )


def onRollUnreadable(plugin_event, playerName):
    """掷骰记录读不到时的提示（连掷、奖励骰等）。"""
    return pvp.jfzCore.replyText(
        plugin_event,
        '这一次掷骰没有被记录。\n'
        '请一个回合发一条，不要使用连掷（.ra50#3）或奖励骰/惩罚骰。\n'
        '可以重新发送本回合要求的 `.raN`。'
    )


def recordRoll(plugin_event, playerName, rollValue, skillValue, levelName):
    """
    记录一次有效掷骰，并在双方都掷满时触发结算。

    在骰子钩子里被调用（见 jfzDice.onMsgHook），此时已经过了全部校验。
    """
    botHash = pvp.jfzCore.getBotHash(plugin_event)
    match = pvp.jfzStore.getMatch()

    if match.get('stage') != pvp.jfzCore.STAGE_FIGHT:
        return

    challengerName = match.get('challenger_name', '')
    targetName = match.get('target_name', '')
    opponentName = targetName if playerName == challengerName else challengerName

    rolls = match.get('rolls', {})
    if not isinstance(rolls, dict):
        rolls = {}
    rolls.setdefault(challengerName, [])
    rolls.setdefault(targetName, [])

    if playerName not in [challengerName, targetName]:
        return

    damage = pvp.jfzDice.getDamageByLevel(levelName)
    rolls[playerName] = list(rolls[playerName]) + [{
        'roll': int(rollValue),
        'skill': int(skillValue),
        'level': levelName,
        'damage': int(damage),
    }]

    rounds = int(match.get('rounds', 10))
    doneSelf = len(rolls[playerName]) >= rounds
    doneChallenger = len(rolls[challengerName]) >= rounds
    doneTarget = len(rolls[targetName]) >= rounds

    match = pvp.jfzStore.updateMatch(rolls=rolls)

    roundNo = len(rolls[playerName])
    # 【掷骰回执只发一条、不带进度】
    # 曾经加过「进度：你 X/N，对方 Y/N」和掷骰前的「现在是第 N 回合」引导，
    # 结果一次掷骰冒出两条插件消息。玩家明确要求：不显示进度、合并成一条。
    # 少掷了怎么办：掷骰超时（fight_timeout_minutes）会按已有记录结算，
    # 战报里会注明谁缺几回合，不会无声卡死。
    pvp.jfzCore.replyText(
        plugin_event,
        '第 %d 回合已记录：%d（%s）' % (roundNo, int(rollValue), levelName)
    )

    if doneChallenger and doneTarget:
        # 双方都掷满 → 给掷骰者一个明确的「进入结算」信号，然后直接结算。
        # 战报里掷骰者本人的那份走会话直发（他刚掷完最后一骰，人就在场）。
        pvp.jfzCore.replyText(plugin_event, '%d 回合已全部掷完，等待结算。' % rounds)
        settleMatch(botHash, match, plugin_event=plugin_event)
        return

    if doneSelf:
        pvp.jfzCore.replyText(
            plugin_event,
            '你的 %d 回合已全部掷完，等待对方掷骰。' % rounds
        )


# ---------------------------------------------------------------------------
# 结算（PRD 4.6 / 4.7）
# ---------------------------------------------------------------------------

def settleMatch(botHash, match, plugin_event=None):
    """
    逐回合累计伤害、双方血量同时扣减、判定死亡与掉落、双群播报。

    plugin_event：触发本次结算的掷骰事件（如果有）——
    掷骰者本人的战报走会话直发，对方走跨群推送。
    超时结算等后台场景没有事件，两边都走跨群推送。
    """
    challengerName = match.get('challenger_name', '')
    targetName = match.get('target_name', '')
    rounds = int(match.get('rounds', 10))

    rolls = match.get('rolls', {}) or {}
    challengerRolls = rolls.get(challengerName, []) or []
    targetRolls = rolls.get(targetName, []) or []

    # --- 逐回合对比 ---
    # 【为什么按「回合数」而不是「列表长度」循环】
    # 正常人两边长度都是 rounds，但万一有某一边短了（数据被手改过），
    # 按 rounds 循环 + 越界当「无判定（0 伤害）」处理，
    # 比直接 IndexError 崩掉要体面得多 —— 崩掉的话这场就永远结算不了。
    roundResults = []
    challengerTotalDamage = 0
    targetTotalDamage = 0
    for index in range(rounds):
        challengerResult = challengerRolls[index] if index < len(challengerRolls) else None
        targetResult = targetRolls[index] if index < len(targetRolls) else None

        challengerDamage = int(challengerResult['damage']) if challengerResult else 0
        targetDamage = int(targetResult['damage']) if targetResult else 0

        challengerTotalDamage += challengerDamage
        targetTotalDamage += targetDamage

        roundResults.append({
            'round': index + 1,
            'challenger_level': challengerResult['level'] if challengerResult else '未掷',
            'challenger_roll': challengerResult['roll'] if challengerResult else 0,
            'challenger_damage': challengerDamage,
            'target_level': targetResult['level'] if targetResult else '未掷',
            'target_roll': targetResult['roll'] if targetResult else 0,
            'target_damage': targetDamage,
        })

    # --- 双方血量同时扣减 ---
    # 「同时」的含义：两边各自的伤害都按**战前血量**算，互不影响，
    # 不会出现「A 先把 B 打死、B 的伤害就不算了」这种情况。
    challengerPlayer = pvp.jfzStore.getPlayer(challengerName) or {}
    targetPlayer = pvp.jfzStore.getPlayer(targetName) or {}

    challengerHpBefore = int(challengerPlayer.get('hp', 0))
    targetHpBefore = int(targetPlayer.get('hp', 0))

    challengerHpAfter = pvp.jfzCore.clampHp(challengerHpBefore - targetTotalDamage)
    targetHpAfter = pvp.jfzCore.clampHp(targetHpBefore - challengerTotalDamage)

    pvp.jfzStore.setPlayerHp(challengerName, challengerHpAfter)
    pvp.jfzStore.setPlayerHp(targetName, targetHpAfter)

    # --- 死亡掉落（PRD 4.7.2）---
    # ★ 这里只记「谁死了、掉了什么」，不拼文本 ★
    # 原因见 _buildDropText：掉落播报里会出现双方的名字，而两边看到的名字不同
    # （被发起人那一侧发起人叫「神秘对手」）。如果在这里拼成一条文本给两边共用，
    # 被发起人就会从掉落播报里看到发起人的真名 —— PRD 4.3.3 的匿名规则当场破功。
    challengerDead = challengerHpAfter <= 0
    targetDead = targetHpAfter <= 0
    dropInfo = None
    if challengerDead and targetDead:
        dropInfo = {'both': True}
    elif challengerDead:
        items, copper = pvp.jfzStore.transferAll(challengerName, targetName)
        dropInfo = {'both': False, 'deadIsChallenger': True, 'items': items, 'copper': copper}
    elif targetDead:
        items, copper = pvp.jfzStore.transferAll(targetName, challengerName)
        dropInfo = {'both': False, 'deadIsChallenger': False, 'items': items, 'copper': copper}

    # --- 战报 ---
    report = {
        'match_id': match.get('match_id', ''),
        'challenger_name': challengerName,
        'target_name': targetName,
        'rounds': rounds,
        'challenger_total_damage': challengerTotalDamage,
        'target_total_damage': targetTotalDamage,
        'challenger_hp_before': challengerHpBefore,
        'challenger_hp_after': challengerHpAfter,
        'target_hp_before': targetHpBefore,
        'target_hp_after': targetHpAfter,
        'round_results': roundResults,
        'settled_at': pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S'),
    }
    pvp.jfzStore.addReport(report)

    # 两边看到的名字不同：被发起人那一侧发起人叫「神秘对手」
    challengerText = _buildReportText(
        match, challengerName,
        challengerName, targetName, challengerTotalDamage, targetTotalDamage,
        challengerHpAfter, targetHpAfter, dropInfo,
    )
    targetText = _buildReportText(
        match, targetName,
        '神秘对手', targetName, challengerTotalDamage, targetTotalDamage,
        challengerHpAfter, targetHpAfter, dropInfo,
    )

    _finishMatch(botHash, match, reason='settled')

    # 先结束对局再播报：这样播报发出去的同时，玩家已经可以发起下一场了。
    # 反过来的话「战报都到了但还是显示在战斗中」，玩家会以为卡住。
    # _deliverToPlayer 会按群号判断：最后一骰是谁掷的，谁的那份战报走会话直发，
    # 另一份走跨群推送。两侧文本本就不同（被发起人侧是「神秘对手」），
    # 所以不存在把匿名侧文本回错会话的问题。
    _deliverToPlayer(plugin_event, botHash, challengerName, challengerText)
    _deliverToPlayer(plugin_event, botHash, targetName, targetText)

    print('[pvp] 对局结算：%s(%d→%d) vs %s(%d→%d)' % (
        challengerName, challengerHpBefore, challengerHpAfter,
        targetName, targetHpBefore, targetHpAfter,
    ))

    # 上一场刚打完，手里的活事件还在（最后掷骰那位的会话）——
    # 传给 startNextMatch，让下一场的双方通知也能走活事件直发。
    startNextMatch(botHash, plugin_event=plugin_event)


def _buildDropText(challengerDisplay, targetDisplay, dropInfo):
    """
    生成掉落说明文本。dropInfo 为 None / 空表示本场没有掉落。

    ★ 入参是「展示名」而不是「玩家名」★
    被发起人那一侧看到的发起人是「神秘对手」，所以这里必须接收
    _buildReportText 已经处理好的展示名，绝不能自己去 getPlayer 取真名。
    否则死亡播报会成为匿名规则的漏洞 —— 被发起人只要打赢一次
    （或被打死一次），就能从掉落那一行读到对手是谁。
    """
    if not dropInfo:
        return ''

    if dropInfo.get('both'):
        return '双方同时倒下，背包与铜币均不转移。'

    deadDisplay = challengerDisplay if dropInfo.get('deadIsChallenger') else targetDisplay
    aliveDisplay = targetDisplay if dropInfo.get('deadIsChallenger') else challengerDisplay
    items = dropInfo.get('items') or {}
    copper = int(dropInfo.get('copper') or 0)

    if not items and not copper:
        return '%s倒下了，%s身上没有可掉落的东西。' % (deadDisplay, aliveDisplay)

    parts = []
    if copper:
        parts.append('%d 铜币' % copper)
    if items:
        parts.append('、'.join('%s×%d' % (name, int(count)) for name, count in items.items()))
    return '%s倒下了，其全部背包与铜币归 %s：%s。' % (deadDisplay, aliveDisplay, '，'.join(parts))


def _buildReportText(match, viewerName, challengerDisplay, targetDisplay,
                     challengerDamage, targetDamage,
                     challengerHp, targetHp, dropInfo):
    """
    渲染战报（PRD 6「结算播报」模板）。

    viewerName 是「这份战报要发给谁」，用来决定最后那句胜负怎么写 ——
    必须站在收件人的视角写「你赢了 / 你倒下了」，
    而不是站在发起人视角写「张三胜利！」，否则被发起人会看不懂。

    challengerDisplay / targetDisplay 是**在这个收件人眼里**两人的名字
    （被发起人那一侧，发起人一律显示为「神秘对手」）。
    掉落说明也用这一组名字渲染，避免绕过匿名规则。
    """
    gameName = match.get('game_name', '') or pvp.jfzStore.getGameName()
    # 【战报不展示双方剩余血量】用户明确要求。
    # 战报是会被群成员围观、也会被玩家截图转发的文本，剩余血量是个人状态数据，
    # 不该在战报里长期留痕；要查血量有专门的查询指令。
    # （challengerHp / targetHp 仍然要传进来：胜负判定和掉落播报都要用它们。）
    lines = [
        '%s·战报' % pvp.jfzCore.gameTag(gameName).strip('【】'),
        '对局：%s vs %s' % (challengerDisplay, targetDisplay),
        '',
        '%s 累计造成伤害：%d' % (challengerDisplay, challengerDamage),
        '%s 累计造成伤害：%d' % (targetDisplay, targetDamage),
        '',
    ]

    # 【未掷回合要注明】掷骰超时结算时有人没掷满，缺的回合按 0 伤害计。
    # 不注明的话，玩家对比「累计伤害」和自己的掷骰记录会发现对不上，
    # 又是一轮「机器人算错了」的误会。
    try:
        rolls = match.get('rolls', {}) or {}
        challengerMiss = max(0, int(match.get('rounds', 0)) - len(rolls.get(match.get('challenger_name', '')) or []))
        targetMiss = max(0, int(match.get('rounds', 0)) - len(rolls.get(match.get('target_name', '')) or []))
        if challengerMiss or targetMiss:
            parts = []
            if challengerMiss:
                parts.append('%s 缺 %d 回合' % (challengerDisplay, challengerMiss))
            if targetMiss:
                parts.append('%s 缺 %d 回合' % (targetDisplay, targetMiss))
            lines.append('（%s 未掷，按 0 伤害计）' % '，'.join(parts))
            lines.append('')
    except Exception:
        pass

    if challengerHp <= 0 and targetHp <= 0:
        lines.append('双方同时倒下，本场平局。')
    else:
        winnerIsChallenger = challengerHp > 0
        winnerDisplay = challengerDisplay if winnerIsChallenger else targetDisplay
        if challengerDisplay == '神秘对手':
            # 站在被发起人视角：challenger 是神秘对手
            viewerWon = (winnerDisplay != '神秘对手')
        else:
            viewerWon = (winnerIsChallenger and viewerName == match.get('challenger_name')) or \
                        ((not winnerIsChallenger) and viewerName == match.get('target_name'))
        lines.append('%s 胜利！' % winnerDisplay)
        lines.append('你%s。' % ('取得了胜利' if viewerWon else '倒下了'))

    dropText = _buildDropText(challengerDisplay, targetDisplay, dropInfo)
    if dropText:
        lines.append('')
        lines.append(dropText)

    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 结束对局 / 返还道具
# ---------------------------------------------------------------------------

def _refundConsumedItems(match, skipEntries=None):
    """
    把账本上「已实际扣减」的道具退回玩家背包（PRD 4.8.3）。

    skipEntries 是 [(玩家名, 道具名), ...]，命中的条目**不返还**
    （目前唯一的用途是遁隐符里「用符那张符不退回」）。
    返回一段给人看的说明文本；没有需要返还的条目时返回空串。
    """
    skipSet = set((str(p), str(i)) for p, i in (skipEntries or []))
    consumed = match.get('consumed', [])
    if not isinstance(consumed, list) or not consumed:
        return ''

    byPlayer = {}
    for entry in consumed:
        playerName = entry.get('player', '')
        itemName = entry.get('item', '')
        if not playerName or not itemName:
            continue
        if (str(playerName), str(itemName)) in skipSet:
            continue
        byPlayer.setdefault(playerName, {})
        byPlayer[playerName][itemName] = byPlayer[playerName].get(itemName, 0) + 1

    parts = []
    for playerName, items in byPlayer.items():
        for itemName, count in items.items():
            pvp.jfzStore.addItem(playerName, itemName, count)
            parts.append('%s 的 %s×%d' % (playerName, itemName, count))
    return '、'.join(parts)


def _finishMatch(botHash, match, reason):
    """把对局置为 [已结束] 并落盘，然后清掉当前对局。"""
    match = dict(match)
    match['state'] = pvp.jfzCore.MATCH_FINISHED
    match['stage'] = pvp.jfzCore.STAGE_SETTLE
    match['cancel_reason'] = reason
    match['finished_at'] = pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S')
    pvp.jfzStore.setMatch(match)
    # 清空「当前对局」，让下一场能开始
    pvp.jfzStore.clearMatch()


# ---------------------------------------------------------------------------
# 取消对局（时段结束 / 强制终止）
# ---------------------------------------------------------------------------

def cancelAllForWindowClose(botHash=None):
    """
    每日开放时段结束（或整个活动结束）时：
    所有「等待中」和「进行中」的对局**直接取消**，不结算伤害，
    血量 / 背包 / 铜币维持，已声明消耗的道具全部返还（PRD 4.2 / 8）。

    返回 (被取消的排队数, 被取消的对局数)。
    """
    cancelledQueue = 0
    cancelledMatch = 0

    # --- 队列里的请求 ---
    # 这些请求从未匹配成功，说明对局根本没发生，
    # 所以主动次数一并退还（否则玩家白白损失一次「主动发起」）。
    queue = pvp.jfzStore.getQueue()
    if queue:
        pvp.jfzStore.clearQueue()
        cancelledQueue = len(queue)
        for request in queue:
            challengerName = request.get('challenger_name', '')
            if challengerName:
                pvp.jfzStore.addDailyAttackUsed(challengerName, -1)
                _sendToPlayerGroup(
                    botHash or request.get('bot_hash', ''), challengerName,
                    '活动时段已结束，你对「%s」的 PVP 请求已取消。\n'
                    '本次不计入主动次数。' % request.get('target_name', '')
                )

    # --- 进行中的对局 ---
    match = pvp.jfzStore.getMatch()
    if match and match.get('state') == pvp.jfzCore.MATCH_ACTIVE:
        cancelledMatch = 1
        refunded = _refundConsumedItems(match)
        matchBotHash = botHash or match.get('bot_hash', '')
        gameTag = pvp.jfzCore.gameTag(match.get('game_name', ''))

        text = '%sPVP 已结束\n活动时段已结束，本场对局直接取消。\n不结算伤害，血量与背包维持。' % gameTag
        if refunded:
            text += '\n已退回声明消耗的道具：%s' % refunded

        _finishMatch(matchBotHash, match, reason='window_closed')
        _sendToBothSides(matchBotHash, match, text, text)

    if cancelledQueue or cancelledMatch:
        print('[pvp] 活动时段结束：取消排队 %d 条、对局 %d 场。' % (cancelledQueue, cancelledMatch))

    return cancelledQueue, cancelledMatch


@pvp.jfzCore.handled
def handleForceEnd(plugin_event):
    """
    `【PVP】强制结束` —— 管理强制终止当前对局（PRD 3.2 里「管理强制终止」的落地）。

    ★ 这是本插件相对 PRD 指令表新增的指令 ★
    PRD 3.2 把「管理强制终止」列为对局结束的一种途径，
    但 5.2 的指令表里没有对应的指令 —— 没有指令就没办法终止，
    玩家不掷骰又**没有超时**（PRD 4.4 / 8），对局会永远卡住。
    所以补一条，行为对齐「时段结束取消」：不结算、血量维持、道具返还。
    """
    ok, reason = pvp.jfzAdmin.requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    match = pvp.jfzStore.getMatch()
    if not match or match.get('state') != pvp.jfzCore.MATCH_ACTIVE:
        return pvp.jfzCore.replyText(plugin_event, '当前没有进行中的对局。')

    botHash = pvp.jfzCore.getBotHash(plugin_event)
    refunded = _refundConsumedItems(match)
    gameTag = pvp.jfzCore.gameTag(match.get('game_name', ''))

    text = '%sPVP 已结束\n本场对局已被管理强制终止。\n不结算伤害，血量与背包维持。' % gameTag
    if refunded:
        text += '\n已退回声明消耗的道具：%s' % refunded

    _finishMatch(botHash, match, reason='force_ended')
    _sendToBothSides(botHash, match, text, text, plugin_event=plugin_event)

    pvp.jfzCore.replyText(plugin_event, '已强制结束当前对局（%s vs %s）。' % (
        match.get('challenger_name', ''), match.get('target_name', '')
    ))

    startNextMatch(botHash)
    return True


# ---------------------------------------------------------------------------
# 活动时段看守（PRD 4.2「活动时间结束处理」）
# ---------------------------------------------------------------------------

def startActivityWatcher():
    """
    启动活动时段看守线程。在 main.Event.init_after 里调用。

    【为什么必须有个后台线程】
    「每天 18:00 之后把所有对局直接取消」这件事**没有人发指令触发** ——
    它是一个到点自动发生的事件。青果没有给插件提供定时任务接口，
    所以只能自己起一个轻量线程轮询。

    【为什么用轮询而不是 sleep 到 18:00】
    sleep 到具体时刻的方案在「管理员中途改了每日结束时间」时就失效了
    （线程还在按老时间睡）。轮询每次都重新读配置，改完立刻生效。
    """
    global _watcherThread, _watcherRunning

    if _watcherRunning:
        return
    _watcherRunning = True
    _watcherThread = threading.Thread(target=_watcherLoop, name='pvp-Watcher')
    _watcherThread.daemon = True
    _watcherThread.start()
    print('[pvp] 活动时段看守已启动（每 %d 秒检查一次）。' % WATCH_INTERVAL_SECONDS)


def _watcherLoop():
    while True:
        try:
            time.sleep(WATCH_INTERVAL_SECONDS)
            checkAndCloseIfNeeded()
        except Exception:
            print('[pvp] 活动时段看守异常：')
            traceback.print_exc()


def _checkDeclareTimeout(botHash=None):
    """
    声明阶段超时检查：对局卡在「道具声明」太久就自动取消并返还。

    ★ 为什么必须有这个 ★
    PRD 4.4 / 8 对声明阶段**没有超时**，原实现里一对玩家只要都不声明、
    都不下线，对局就永远停在声明阶段 —— 队列里后面所有人的请求
    永远轮不上，表现就是「回复了排队中，然后再也没有下文」。
    管理虽然可以用【PVP】强制结束兜底，但前提是他得知道有局卡住了。
    这里按配置 declare_timeout_minutes（默认 10 分钟，配 0 关闭）自动处理，
    行为对齐「时段结束取消」：不结算、血量背包维持、道具与主动次数返还。

    返回 True 表示确实取消了一场对局。
    """
    match = pvp.jfzStore.getMatch()
    if not match or match.get('state') != pvp.jfzCore.MATCH_ACTIVE:
        return False
    if match.get('stage') != pvp.jfzCore.STAGE_DECLARE:
        return False

    timeoutMinutes = pvp.jfzStore.getConfigInt('declare_timeout_minutes', 10)
    if timeoutMinutes <= 0:
        return False

    # 老数据可能没有 declare_started_at（比如升级前就在打的对局），
    # 退回 created_at；再没有就算不出时间，按「不超时」处理 —— 宁可不动别误杀。
    started = pvp.jfzCore.parseStamp(
        match.get('declare_started_at') or match.get('created_at') or ''
    )
    if started is None:
        return False

    elapsedSeconds = (pvp.jfzCore.getNow() - started).total_seconds()
    if elapsedSeconds < timeoutMinutes * 60:
        return False

    botHash = botHash or match.get('bot_hash', '')
    challengerName = match.get('challenger_name', '')
    targetName = match.get('target_name', '')
    refunded = _refundConsumedItems(match)
    gameTag = pvp.jfzCore.gameTag(match.get('game_name', ''))

    text = (
        '%sPVP 已结束\n'
        '因为超时未完成道具声明（超过 %d 分钟），本场对局已取消。\n'
        '不结算伤害，血量与背包维持。' % (gameTag, timeoutMinutes)
    )
    if refunded:
        text += '\n已退回声明消耗的道具：%s' % refunded
    # 对局没打起来，主动次数一并退还 —— 与「时段结束取消」同一口径，
    # 否则发起人会白白损失一次当天资格。
    if challengerName:
        pvp.jfzStore.addDailyAttackUsed(challengerName, -1)
        text += '\n本次不计入发起人的主动次数。'

    _finishMatch(botHash, match, reason='declare_timeout')
    # ★ 双方都要收到 ★
    # 超时是「没有触发者」的事件，以前两条通知只能走 fake 事件队列 ——
    # 真机上那条链丢消息时，玩家那边就是「对局突然没了，谁也没告诉我是为什么」。
    # 现在 _doSend 会复用最近的活事件（jfzSend.rememberEvent）发送，可靠得多。
    _sendToBothSides(botHash, match, text, text)
    print('[pvp] 对局声明超时（%d 分钟），已取消：%s vs %s' % (
        timeoutMinutes, challengerName, targetName
    ))

    # 卡住的局清掉了，队列里等着的请求立刻顶上
    startNextMatch(botHash)
    return True


def _checkFightTimeout(botHash=None):
    """
    掷骰阶段超时检查：卡在掷骰太久就按**已有记录**直接结算。

    ★ 为什么取消不行、必须结算 ★
    声明阶段什么都没发生，取消即可；但掷骰阶段已经打了一部分，
    直接取消会白白丢掉已掷的回合，血量账也对不上。所以这里选择
    「按已有记录结算」：缺的回合按「未掷」计 0 伤害（settleMatch
    本来就支持越界当未掷处理），战报里会注明谁缺了几回合。
    发起人的主动次数照常消耗 —— 毕竟已经开打了。

    典型成因：某一方有一掷被防作弊/污染防护拦下而没补掷
    （玩家自己以为掷满了），或者一方中途下线不再掷了。
    配置 fight_timeout_minutes（默认 30 分钟，配 0 关闭）。
    """
    match = pvp.jfzStore.getMatch()
    if not match or match.get('state') != pvp.jfzCore.MATCH_ACTIVE:
        return False
    if match.get('stage') != pvp.jfzCore.STAGE_FIGHT:
        return False

    timeoutMinutes = pvp.jfzStore.getConfigInt('fight_timeout_minutes', 30)
    if timeoutMinutes <= 0:
        return False

    # 老数据可能没有 fight_started_at（升级前就在打的对局），
    # 退回 declare_started_at；再没有就按「不超时」处理 —— 宁可不动别误杀。
    started = pvp.jfzCore.parseStamp(
        match.get('fight_started_at') or match.get('declare_started_at') or ''
    )
    if started is None:
        return False

    elapsedSeconds = (pvp.jfzCore.getNow() - started).total_seconds()
    if elapsedSeconds < timeoutMinutes * 60:
        return False

    botHash = botHash or match.get('bot_hash', '')
    challengerName = match.get('challenger_name', '')
    targetName = match.get('target_name', '')
    rounds = int(match.get('rounds', 10))
    rolls = match.get('rolls', {}) or {}
    challengerDone = len(rolls.get(challengerName, []) or [])
    targetDone = len(rolls.get(targetName, []) or [])

    # 先把「超时结算」这件事告诉双方，战报紧随其后
    gameTag = pvp.jfzCore.gameTag(match.get('game_name', ''))
    notice = (
        '%sPVP 掷骰阶段已超过 %d 分钟未完成，现在按已记录的结果结算。\n'
        '当前进度：发起方 %d/%d，被发起方 %d/%d。未掷的回合按 0 伤害计。' % (
            gameTag, timeoutMinutes,
            challengerDone, rounds, targetDone, rounds,
        )
    )
    _sendToBothSides(botHash, match, notice, notice)

    print('[pvp] 对局掷骰超时（%d 分钟），按已有记录结算：%s(%d/%d) vs %s(%d/%d)' % (
        timeoutMinutes, challengerName, challengerDone, rounds,
        targetName, targetDone, rounds,
    ))

    settleMatch(botHash, match)
    return True


def checkAndCloseIfNeeded():
    """
    如果「现在不在活动开放时段」而还有对局/排队，就把它们全部取消。

    ★ 为什么用「不在时段内」而不是「刚好越过结束时间」判断 ★
    用时间点比较（比如 now >= daily_end_time）需要处理跨天、改配置、
    机器人重启等一堆边界。而「不在开放时段」这个条件天然覆盖了全部情况：
    今日收工后不在窗口内、整个活动结束后也不在窗口内、活动开始前同样不在。
    并且它是幂等的 —— 取消完之后队列和对局都空了，下次检查自然什么都不做。
    """
    if not pvp.jfzStore.isRegistered():
        return

    # 声明超时只在「开放时段内」有意义：不在时段内时，
    # 下面的 cancelAllForWindowClose 会把对局整个取消，轮不到这里管。
    try:
        _checkDeclareTimeout()
    except Exception:
        # 看守线程里任何异常都不能把它弄死 —— 弄死了就没有人再检查了
        print('[pvp] 声明超时检查异常：')
        traceback.print_exc()

    try:
        _checkFightTimeout()
    except Exception:
        print('[pvp] 掷骰超时检查异常：')
        traceback.print_exc()

    hasPending = bool(pvp.jfzStore.getQueue()) or pvp.jfzStore.hasActiveMatch()
    if not hasPending:
        return

    config = pvp.jfzStore.getActivityConfig()
    inWindow, _reason = pvp.jfzCore.isInActivityWindow(config)
    if inWindow:
        return

    cancelAllForWindowClose()


# ---------------------------------------------------------------------------
# 重启恢复（PRD 8「机器人重启」）
# ---------------------------------------------------------------------------

def recoverAfterRestart():
    """
    插件加载（含机器人重启）后把上次未结束的状态收拾干净。

    分两种情况：
      - 还在活动开放时段：把对局恢复起来，并给双方各发一条提醒
        （玩家多半已经忘了自己在打，不发提醒会一脸茫然地等下去）
      - 不在开放时段（比如隔夜重启）：按 PRD 8「按活动配置安全结束」，
        直接取消并返还道具
    """
    if not pvp.jfzStore.isRegistered():
        return

    # 清一次「每天主动次数」的跨天重置：重启往往发生在换日前后，
    # 而重置只在读次数时才发生，不主动触发一次的话
    # 管理员用「查看名单」看到的会是昨天的旧数字。
    for name in pvp.jfzStore.listPlayerNames():
        pvp.jfzStore.resetDailyAttackIfNeeded(name)

    config = pvp.jfzStore.getActivityConfig()
    inWindow, _reason = pvp.jfzCore.isInActivityWindow(config)

    match = pvp.jfzStore.getMatch()
    if match and match.get('state') == pvp.jfzCore.MATCH_ACTIVE:
        if not inWindow:
            cancelAllForWindowClose(match.get('bot_hash', ''))
        else:
            botHash = match.get('bot_hash', '')
            stage = match.get('stage', '')
            if stage == pvp.jfzCore.STAGE_DECLARE:
                # 重启后重新计时：玩家刚收到「对局仍有效」的提醒，
                # 应该拿到一个完整的声明窗口，而不是继承重启前已流逝的时间。
                match['declare_started_at'] = pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S')
                pvp.jfzStore.setMatch(match)
                notice = (
                    '机器人已重启，你的 PVP 对局仍然有效。\n'
                    '当前处于道具声明阶段，请按提示完成声明。'
                )
                _sendToBothSides(botHash, match, notice, notice)
            elif stage == pvp.jfzCore.STAGE_FIGHT:
                # 重启后重新计时（理由同声明阶段：玩家刚收到提醒，给完整窗口）
                match['fight_started_at'] = pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S')
                pvp.jfzStore.setMatch(match)
                challengerName = match.get('challenger_name', '')
                targetName = match.get('target_name', '')
                for name in [challengerName, targetName]:
                    rolls = (match.get('rolls', {}) or {}).get(name, []) or []
                    _sendToPlayerGroup(
                        botHash, name,
                        '机器人已重启，你的 PVP 对局仍然有效。\n'
                        '已记录 %d / %d 回合，请继续逐条发送剩余回合的 .raN。' % (
                            len(rolls), int(match.get('rounds', 10))
                        )
                    )
            print('[pvp] 已恢复重启前的对局：%s vs %s' % (
                match.get('challenger_name', ''), match.get('target_name', '')
            ))

    # 队列里的请求本来就在磁盘上，重新尝试开一场即可
    startNextMatch(match.get('bot_hash', '') if match else '')


# ---------------------------------------------------------------------------
# 玩家自查指令（PRD 2.2「查看本人背包/血量/战报」）
# ---------------------------------------------------------------------------
# PRD 5.3 的玩家指令表里没有【PVP】血量 / 【PVP】战报，但 2.2 的权限矩阵里
# 明确写了玩家可以「查看本人背包/血量/战报」。背包有【PVP】背包对应，
# 血量和战报没有入口 —— 所以补这两条（已在指令手册里标注为「扩展」）。

@pvp.jfzCore.handled
def handleStatus(plugin_event, context):
    """`【PVP】血量`"""
    if not context['ok']:
        return pvp.jfzCore.replyText(plugin_event, context['reason'])

    playerName = context['name']
    player = context['player']
    hp = int(player.get('hp', 0))
    status = pvp.jfzCore.deriveStatus(hp)
    limit = pvp.jfzStore.getConfigInt('daily_attack_limit', 3)
    used = pvp.jfzStore.getDailyAttackUsed(playerName)

    lines = [
        '【%s】' % playerName,
        '血量：%d / %d' % (hp, pvp.jfzCore.getHpLimit()),
        '状态：%s' % pvp.jfzCore.getStatusName(status),
        # 攻击值要让玩家看得到：掷骰列表里的 `.raN` 就是拿它当基础值算的，
        # 玩家看到「怎么我要掷 60 别人掷 50」时，这里能自己找到答案。
        '攻击值：%d（每回合 .raN 的基础值%s）' % (
            pvp.jfzStore.getPlayerAttack(playerName),
            '，由骰主单独设定' if pvp.jfzStore.getPersonalAttack(playerName) else '',
        ),
        '铜币：%d' % int(player.get('copper', 0)),
        '今日主动次数：%d / %d' % (used, limit),
    ]

    if status == pvp.jfzCore.STATUS_DYING:
        lines.append('（濒死：无法主动发起 PVP，可被挑战，可使用场外药）')
    elif status == pvp.jfzCore.STATUS_DEAD:
        lines.append('（死皮：无法进行任何玩法）')

    if pvp.jfzStore.isPlayerInMatch(playerName):
        lines.append('（当前正在 PVP 对局中）')
    else:
        inQueue = pvp.jfzStore.isPlayerInQueue(playerName)
        if inQueue:
            lines.append('（有 PVP 请求排队中）')
        inWindow, _reason = pvp.jfzCore.isInActivityWindow(pvp.jfzStore.getActivityConfig())
        lines.append('活动时段：%s' % ('开放中' if inWindow else '未开放'))

    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))


@pvp.jfzCore.handled
def handleMyReport(plugin_event, context):
    """`【PVP】战报` —— 查看最近一场战报（自己视角）。"""
    if not context['ok']:
        return pvp.jfzCore.replyText(plugin_event, context['reason'])

    playerName = context['name']
    report = pvp.jfzStore.getLastReport()
    if not report:
        return pvp.jfzCore.replyText(plugin_event, '还没有任何战报记录。')

    # 只显示与本人相关的战报，且发起人身份同样按匿名规则处理
    if playerName == report.get('challenger_name'):
        challengerDisplay = report.get('challenger_name', '')
        targetDisplay = report.get('target_name', '')
        viewerName = playerName
    elif playerName == report.get('target_name'):
        challengerDisplay = '神秘对手'
        targetDisplay = report.get('target_name', '')
        viewerName = playerName
    else:
        return pvp.jfzCore.replyText(plugin_event, '最近一场战报与你无关。')

    text = _buildReportText(
        {'game_name': pvp.jfzStore.getGameName()},
        viewerName,
        challengerDisplay, targetDisplay,
        int(report.get('challenger_total_damage', 0)),
        int(report.get('target_total_damage', 0)),
        int(report.get('challenger_hp_after', 0)),
        int(report.get('target_hp_after', 0)),
        # 战报存档里没有记掉落（掉落是「当时一次性转移」的效果，
        # 补看战报时再复述一遍反而会让人以为又掉了一次），所以传 None。
        None,
    )
    return pvp.jfzCore.replyText(plugin_event, text)


@pvp.jfzCore.handled
def handleQueueInfo(plugin_event):
    """`【PVP】排队`（扩展）—— 查看当前对局与排队情况。"""
    lines = ['【当前排队情况】']
    match = pvp.jfzStore.getMatch()
    if match and match.get('state') == pvp.jfzCore.MATCH_ACTIVE:
        stageName = {
            pvp.jfzCore.STAGE_DECLARE: '道具声明中',
            pvp.jfzCore.STAGE_FIGHT: '掷骰中',
            pvp.jfzCore.STAGE_SETTLE: '结算中',
        }.get(match.get('stage', ''), match.get('stage', ''))
        lines.append('进行中：%s vs %s（%s）' % (
            match.get('challenger_name', ''), match.get('target_name', ''), stageName
        ))
        if match.get('stage') == pvp.jfzCore.STAGE_FIGHT:
            # 掷骰进度一并展示 —— 「等待结算」卡死问题排查时，一眼就能看出谁少掷了
            rolls = match.get('rolls', {}) or {}
            rounds = int(match.get('rounds', 10))
            lines.append('掷骰进度：发起方 %d/%d，被发起方 %d/%d' % (
                len(rolls.get(match.get('challenger_name', '')) or []), rounds,
                len(rolls.get(match.get('target_name', '')) or []), rounds,
            ))
    else:
        lines.append('进行中：无')

    queue = pvp.jfzStore.getQueue()
    if not queue:
        lines.append('排队中：无')
    else:
        lines.append('排队中：%d 条' % len(queue))
        for index, request in enumerate(queue, 1):
            lines.append('  %d. %s → %s' % (
                index, request.get('challenger_name', ''), request.get('target_name', '')
            ))

    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))
