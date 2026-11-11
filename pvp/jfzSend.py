# -*- encoding: utf-8 -*-
'''
发送层 —— 负责把消息推到「不是当前对话」的那个群。

================================================================
【为什么需要这一层，而且必须有节流】
================================================================
本插件最核心的动作就是**跨群推送**：
发起人在 A 群、被发起人在 B 群，机器人要同时给两边发
「已匹配」通知、道具声明引导、掷骰列表、战报。

青果和海豹在这一点上有关键区别：
  - 海豹对官方 QQ 的主动消息会**自动排队**，超频了按节奏慢慢发；
  - 青果**完全不排队**，超频了直接返回失败，消息就这么没了。

而 QQ 平台本身的限制依然在：
  - 单群 20 条 / 分钟
  - 单机器人 60 条 / 分钟
  - 单群 1000 条 / 天

一场 PVP 结算要在两个群各发一条战报，一晚上几十场就是上百条，
不排队必炸。所以跨群推送一律走这里的队列，由后台线程按
「每群每分钟不超过 18 条」匀速发出。

【回复当前群不走这里】
回复当前群用 jfzCore.replyText()（内部走 OlivaDiceCore.msgReply.replyMsg），
由青果自己处理主动/被动消息和分块，不需要也不应该经过队列 ——
经过队列反而会让玩家觉得机器人反应慢半拍。
'''

import OlivOS
import OlivaDiceCore

import time
import threading
import traceback

try:
    import queue as queueModule
except ImportError:  # Python 2 兜底，青果本体是 Python 3，正常走不到这里
    import Queue as queueModule

import pvp


# 待发送队列。元素是 (botHash, 群ID, 文本, 入队时间戳, 已重试次数)。
sendQueue = queueModule.Queue()

# 队列长度上限。机器人掉线导致发不出去时，也不能让内存无限涨。
QUEUE_MAX = 300

# 单条消息发送失败后的最大重试次数。
# 【为什么必须重试】跨群推送是整场对局的「唯一通知渠道」——匹配通知发丢了，
# 玩家就永远不知道该声明道具，对局表面上「卡住」了（实际双方都在干等）。
# 而发送失败往往是**瞬时**的：插件刚重载完 Proc 还没就绪、机器人正在重连。
# 所以失败不能像以前那样只打一行日志就丢弃，要放回队列再试几次。
SEND_RETRY_MAX = 3

# 每次重试之间隔多久（秒）。太短了碰不上「Proc 还没就绪」这种以秒计的窗口。
SEND_RETRY_INTERVAL = 2.0

# 每个群「最近一分钟发了几条」的记录，用于滑动窗口限流。
# 结构：{ 群ID: [时间戳, 时间戳, ...] }
dictGroupSendTime = {}
rateLock = threading.Lock()

# 单群每分钟最多发多少条。平台限制是 20，留一点余量避免刚好卡边界。
GROUP_LIMIT_PER_MINUTE = 18

workerThread = None
flagWorkerRunning = False


def startSender():
    """
    启动后台发送线程。在 main.Event.init_after 里调用。
    重复调用是安全的（已经在跑就直接返回）。
    """
    global workerThread, flagWorkerRunning

    if flagWorkerRunning:
        return
    flagWorkerRunning = True
    workerThread = threading.Thread(target=_workerLoop, name='pvp-Sender')
    # daemon=True：主程序退出时这个线程跟着结束，不会把进程拖住
    workerThread.daemon = True
    workerThread.start()
    print('[pvp] 发送队列已启动。')


def enqueueSend(botHash, groupId, text):
    """
    把一条跨群消息放进发送队列。这是本插件对外的唯一跨群发送入口。

    入队是立即返回的 —— 真正的发送由后台线程按节奏做，
    调用方（对局引擎）不会被阻塞，也就不会因为发战报而卡住掷骰响应。
    """
    if not botHash or groupId is None or str(groupId).strip() == '' or not text:
        return

    # 队列满了丢掉最旧的一条，给新消息腾位置。
    # 宁可丢一条旧播报，也不要让机器人卡死或者内存爆掉。
    if sendQueue.qsize() >= QUEUE_MAX:
        try:
            sendQueue.get_nowait()
            print('[pvp] 发送队列已满，丢弃了一条最旧的待发消息。')
        except Exception:
            pass

    sendQueue.put((str(botHash), str(groupId), str(text), time.time(), 0))


def sendViaEvent(plugin_event, groupId, text):
    """
    用「当前收到消息的活事件」直接向任意群发一条消息（不经过自建队列）。

    【为什么手上有活事件时要绕开自建队列】
    活事件是青果运行时刚递到手上的：bot_info、control_queue 都已经由框架
    填好 —— 它和会话回复走的是**同一条被真机验证过能用的通道**。
    而自建队列要靠「fake 事件 + gProc + 后台线程」拼出这条链，
    任何一环没就绪都是静默失败，玩家那头就是「没有下文」。
    这也正是 AmourGame 的既有做法：它从不造 fake 事件，
    跨群通知一律 plugin_event.send('group', 对方群, 文本)。

    返回 True / False 表示发没发出去（失败时调用方回落到自建队列）。
    """
    if plugin_event is None:
        return False
    if groupId is None or str(groupId).strip() == '' or not text:
        return False
    try:
        plugin_event.send('group', str(groupId), str(text))
        print('[pvp] 活事件直发已发出 -> 群 %s：%s' % (
            groupId, str(text)[:30].replace('\n', ' ')
        ))
        return True
    except Exception:
        print('[pvp] 活事件直发失败，这条消息将回落到发送队列：')
        traceback.print_exc()
        return False


def sendToGroup(botHash, groupId, text):
    """往指定群推一条消息（走队列）。语义化别名，读代码时更清楚。"""
    enqueueSend(botHash, groupId, text)


# ---------------------------------------------------------------------------
# 「最近的活事件」缓存 —— 让后台线程也能走活事件通道
# ---------------------------------------------------------------------------
#
# 【为什么后台线程也要蹭活事件】
# 超时取消、时段结束、重启恢复这些通知没有触发事件，以前只能造 fake 事件。
# 而 fake 事件那条链（gProc 是否就绪、Proc_data['bot_info_dict'] 里有没有这个
# hash、消息模板解析是否 active）在真机上是出过「发不出去还不报错」的。
# 活事件对象的 send() 只用到 bot_info + plugin_info['control_queue']
# （见 API.Event.send → __send，它带 force_active=True，不校验事件本身是否还有效），
# 这两样在框架运行期间一直有效 —— 所以把最近收到的那条活事件存下来复用，
# 后台通知就也能走那条被验证过的通道。
#
# 键是 botHash：多账号时不能拿 A 账号的事件去发 B 账号的群消息。
dictLastEvent = {}
_dictLastEventLock = threading.Lock()


def rememberEvent(plugin_event, botHash):
    """
    记下刚收到的一条活事件，供后台线程（看守 / 超时 / 重启恢复）复用作发送上下文。

    在群消息入口处调用即可 —— 本插件所有主动消息都发往「玩家个人群」，
    而个人群的消息必然经过群消息入口，缓存因此总是新鲜的。
    """
    if plugin_event is None or not botHash:
        return
    with _dictLastEventLock:
        dictLastEvent[str(botHash)] = plugin_event


def getRememberedEvent(botHash):
    """取缓存里的活事件：先按 botHash 精确匹配，找不到再退而求其次取任意一条。"""
    with _dictLastEventLock:
        if not dictLastEvent:
            return None
        exact = dictLastEvent.get(str(botHash))
        if exact is not None:
            return exact
        for candidate in dictLastEvent.values():
            return candidate
    return None


def sendToGroups(botHash, targets, text):
    """
    把同一条消息发给多个群。targets 是 [(群ID, 文本)] 或 [群ID]。

    战报要「两个群同时播报」且两边内容不同（被发起人那边是匿名的），
    所以支持按目标给不同文本。
    """
    for target in targets:
        if isinstance(target, (list, tuple)):
            groupId, textThis = target[0], target[1]
        else:
            groupId, textThis = target, text
        enqueueSend(botHash, groupId, textThis)


def flush(timeoutSeconds=5):
    """
    尽力把队列里剩下的消息发完（最多等 timeoutSeconds 秒）。
    在插件被重载 / 卸载时调用，少丢一点是一点。
    """
    deadline = time.time() + timeoutSeconds
    while time.time() < deadline:
        if sendQueue.empty():
            return
        time.sleep(0.2)


def _workerLoop():
    """后台线程主循环：不停从队列取消息发出，取不到就歇一会儿。"""
    while True:
        try:
            item = sendQueue.get(timeout=1.0)
        except Exception:
            # 队列空（或超时），继续下一轮
            continue

        try:
            botHash, groupId, text, _enqueueTs, tryCount = item
            _waitForRateLimit(groupId)
            if _doSend(botHash, 'group', groupId, text):
                continue

            # 发送失败：放回队列重试，而不是像以前那样直接丢掉。
            # 队列满时 put 不会等（Queue 默认无界），所以这里不会阻塞。
            if tryCount < SEND_RETRY_MAX:
                time.sleep(SEND_RETRY_INTERVAL)
                sendQueue.put((botHash, groupId, text, _enqueueTs, tryCount + 1))
                print('[pvp] 发送失败，已加入重试（第 %d / %d 次）：%s -> %s' % (
                    tryCount + 1, SEND_RETRY_MAX, groupId, text[:20].replace('\n', ' ')
                ))
            else:
                print('[pvp] ★★ 消息重试 %d 次仍失败，已丢弃（发给群 %s）：\n%s' % (
                    SEND_RETRY_MAX, groupId, text[:100]
                ))
        except Exception:
            print('[pvp] 发送线程异常：')
            traceback.print_exc()


def _waitForRateLimit(groupId):
    """
    滑动窗口限流：保证同一个群「每 60 秒最多发 GROUP_LIMIT_PER_MINUTE 条」。

    做法是把该群最近 60 秒内的发送时间戳都留着，发之前先看名额够不够，
    不够就睡到最早那条过期为止。这样既不会超频丢消息，也不会白白拖慢。

    【为什么用滑动窗口而不是固定间隔 sleep】
    固定间隔（比如每条睡 3 秒）在「一场对局要连发 5 条」时会白白多等，
    而结算播报是玩家最期待的内容，越早到越好。
    """
    while True:
        with rateLock:
            now = time.time()
            stamps = dictGroupSendTime.get(str(groupId), [])
            # 丢掉 60 秒之前的记录
            stamps = [t for t in stamps if now - t < 60]
            dictGroupSendTime[str(groupId)] = stamps

            if len(stamps) < GROUP_LIMIT_PER_MINUTE:
                stamps.append(now)
                return

            # 名额用完了，算出最早那条还要多久过期，睡过去再试
            waitSeconds = 60 - (now - stamps[0]) + 0.1

        if waitSeconds > 0:
            time.sleep(min(waitSeconds, 5.0))


def _doSend(botHash, sendType, targetId, text):
    """
    真正发出消息。返回 True / False 表示发没发出去（失败时调用方会重试）。

    【发送上下文的三种来源，按可靠性从高到低】
      1. 缓存里的活事件（rememberEvent 存下的最近一条群消息事件）——
         它的 bot_info / control_queue 由框架在 run_plugin 里填好，
         与玩家会话回复同一条通道，真机验证最可靠；
      2. 自己造的 fake 事件（青果官方插件模板做法，也是本插件早先唯一的做法）——
         需要 gProc 就绪、bot_info_dict 里有对应 hash，任何一环缺失都是静默失败；
      3. 彻底没有可用账号 → 发送失败，交给上层重试。

    【QQ 官方机器人必须额外做一步】
    要在事件上打 `flag_from_qq` 标记，青果才知道把这条消息路由到
    官方 QQ 的发送通道。不打标记消息会发不出去（或者走错通道）。
    NapCat（onebot）通道不需要这些标记。
    """
    # --- 来源 1：复用最近的活事件 ---
    liveEvent = getRememberedEvent(botHash)
    if liveEvent is not None:
        try:
            liveEvent.send(sendType, targetId, text)
            print('[pvp] 跨群推送已发出（复用活事件）-> 群 %s：%s' % (
                targetId, str(text)[:30].replace('\n', ' ')
            ))
            return True
        except Exception:
            print('[pvp] 复用活事件发送失败，回落到 fake 事件：')
            traceback.print_exc()

    Proc = pvp.main.gProc
    if Proc is None:
        # 【兜底】理论上 init_after 已经把 gProc 存好了，但万一某些启动顺序下
        # 没存上（比如插件被手动热载、init_after 没跑全），OlivOS 本体有个全局
        # 的 pluginAPI.gProc（插件托盘进程启动时就会赋值），拿它顶上 ——
        # 否则这里直接 return False，玩家那头就是「没有任何下文」。
        Proc = getattr(OlivOS.pluginAPI, 'gProc', None)
    if Proc is None:
        print('[pvp] 发送失败：插件托盘进程还没准备好。')
        return False

    botInfoDict = Proc.Proc_data.get('bot_info_dict', {})
    if not isinstance(botInfoDict, dict):
        botInfoDict = {}

    botInfo = botInfoDict.get(botHash)
    if botInfo is None:
        # 【账号兜底】找不到登记的账号时，用任何一个可用账号发，而不是把消息丢掉。
        # 为什么这么做：跨群推送的目标是玩家群，内容是游戏播报，
        # 「换一个机器人账号发」远好过「整个对局流程静默断掉」。
        # 会出现这种情况的场景：机器人重登后 hash 变了、配置里写了旧账号、
        # 或者重启恢复流程拿到的 bot_hash 对不上当前账号表。
        for candidateKey, candidateInfo in botInfoDict.items():
            if getattr(candidateInfo, 'enable', True) is False:
                continue
            botInfo = candidateInfo
            print('[pvp] 警告：找不到账号 %s，改用账号 %s 发送这条消息。' % (
                botHash, candidateKey
            ))
            break
    if botInfo is None:
        print('[pvp] 发送失败：账号表里一个可用账号都没有（bot_info_dict 为空）。')
        return False

    try:
        pluginEvent = OlivOS.API.Event(
            OlivOS.contentAPI.fake_sdk_event(
                bot_info=botInfo,
                fakename=pvp.main.gPluginName,
                # 显式带上本插件的命名空间：青果会据此找到本插件的上下文，
                # 从而自动带上正确的 message_mode_tx（就是 app.json 里的 old_string）。
                # 不传也能跑（会按 fakename 反查），传了更稳。
                plugin_namespace='pvp',
            ),
            Proc.log,
        )

        # 官方 QQ 通道标记
        if isOfficialQQBot(botInfo):
            extend = getattr(pluginEvent.data, 'extend', None)
            if not isinstance(extend, dict):
                extend = {}
            extend.update({
                'flag_from_qq': True,
                # False 表示这是群消息；单聊要传 True
                'flag_from_direct': (sendType == 'private'),
                # 主动消息不能带被动回复凭据，必须置空
                'reply_msg_id': None,
            })
            pluginEvent.data.extend = extend

        pluginEvent.send(sendType, targetId, text)
        # 【诊断日志】跨群推送一旦出问题就是「玩家没下文」，
        # 控制台里必须能查到「这条到底发没发」。发送量有队列限流兜着，不怕刷屏。
        print('[pvp] 跨群推送已发出 -> 群 %s：%s' % (
            targetId, str(text)[:30].replace('\n', ' ')
        ))
        return True

    except Exception:
        print('[pvp] 发送异常：')
        traceback.print_exc()
        return False


def isOfficialQQBot(botInfo):
    """
    判断这个账号是不是「QQ 官方机器人」通道。

    官方 QQ 的 sdk 名是 qqGuildv2_link（青果里频道和官方群共用这个适配器），
    NapCat / 个人号走的是 onebot。两者发送时需要的标记不一样，所以要区分。
    """
    try:
        platform = getattr(botInfo, 'platform', None)
        if isinstance(platform, dict):
            return platform.get('sdk') == 'qqGuildv2_link'
    except Exception:
        pass
    return False
