# -*- encoding: utf-8 -*-
'''
商城与经济 —— 铜币、道具买卖、背包、场外型道具的使用。

================================================================
【本模块只负责「不在对局里的那些事」】
================================================================
道具按使用时机分成三类（PRD 4.8.2）：

  场外型（大还丹 / 小还丹 / 解毒丸 / 幻息丹）
      → 不在 PVP 中时用，立即生效。**本模块处理**。

  全场型（遁隐符）/ 回合型（胜临符 / 错影符）
      → 只能在「开战后、掷骰前」的声明阶段用。
        它们要和本场对局绑在一起（改某个回合的 .raN、整场取消），
        所以归 jfzEngine 的声明阶段处理，**不在本模块**。

路由层（jfzReply）负责判断「玩家现在在不在对局里」，
再决定把 `【PVP】使用道具 xxx` 交给引擎还是交给本模块。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

import pvp


# ---------------------------------------------------------------------------
# 文本
# ---------------------------------------------------------------------------

def getShopText():
    """
    渲染商城（PRD 4.8.1）。列出全部道具的**当前售价**（可能被场控改过）。
    """
    lines = ['【商城】']
    currentType = None
    for itemName in pvp.jfzCore.ITEM_ORDER:
        itemDef = pvp.jfzCore.getItemDef(itemName)
        if itemDef is None:
            continue
        itemType = itemDef.get('type', '')
        if itemType != currentType:
            currentType = itemType
            lines.append('')
            lines.append('—— %s ——' % itemType)
        lines.append(
            '%s　%d 铜币　%s' % (
                itemName,
                pvp.jfzStore.getItemPrice(itemName),
                itemDef.get('effect', ''),
            )
        )
    lines.append('')
    lines.append('购买：发送「【PVP】购买 道具名」')
    lines.append('查看背包：「【PVP】背包」')
    return '\n'.join(lines)


def getBackpackText(playerName):
    """渲染某个玩家的背包与铜币（PRD 4.8.1）。"""
    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        return '找不到玩家「%s」。' % playerName

    inventory = pvp.jfzStore.getInventory(playerName)
    hp = int(player.get('hp', 0))
    status = pvp.jfzCore.getStatusName(pvp.jfzCore.deriveStatus(hp))

    lines = [
        '【背包】%s' % playerName,
        '血量：%d / %d（%s）' % (hp, pvp.jfzCore.getHpLimit(), status),
        '铜币：%d' % int(player.get('copper', 0)),
        '',
    ]

    empty = True
    for itemName in pvp.jfzCore.ITEM_ORDER:
        count = inventory.get(itemName, 0)
        if count > 0:
            empty = False
            lines.append('%s ×%d　%s' % (
                itemName, count, pvp.jfzCore.getItemDef(itemName).get('effect', '')
            ))
    if empty:
        lines.append('（背包是空的）')

    return '\n'.join(lines)


def getCopperText(playerName):
    """渲染铜币余额。"""
    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        return '找不到玩家「%s」。' % playerName
    return '「%s」当前铜币：%d' % (playerName, int(player.get('copper', 0)))


# ---------------------------------------------------------------------------
# 商城
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleShop(plugin_event, context):
    """`【PVP】商城`"""
    return pvp.jfzCore.replyText(plugin_event, getShopText())


@pvp.jfzCore.handled
def handleBackpack(plugin_event, context):
    """`【PVP】背包`"""
    if not context['ok']:
        return pvp.jfzCore.replyText(plugin_event, context['reason'])
    return pvp.jfzCore.replyText(plugin_event, getBackpackText(context['name']))


@pvp.jfzCore.handled
def handleCopper(plugin_event, context):
    """`【PVP】铜币`（扩展指令，方便玩家自查余额）"""
    if not context['ok']:
        return pvp.jfzCore.replyText(plugin_event, context['reason'])
    return pvp.jfzCore.replyText(plugin_event, getCopperText(context['name']))


@pvp.jfzCore.handled
def handleBuy(plugin_event, context, args):
    """
    `【PVP】购买 道具名 [数量]`（PRD 4.8.1）

    数量是本插件加的扩展（PRD 只写了道具名），不填默认 1。

    校验顺序刻意从「最外层的状态」往「最内层的钱」排：
      玩家身份 → 死皮禁购 → 战斗中禁购 → 道具名 → 数量 → 铜币
    这样玩家收到的提示永远是最该先解决的那一个问题。
    """
    if not context['ok']:
        return pvp.jfzCore.replyText(plugin_event, context['reason'])

    playerName = context['name']
    player = context['player']
    tokens = pvp.jfzCore.splitArgs(args)
    if not tokens:
        return pvp.jfzCore.replyText(
            plugin_event, '用法：【PVP】购买 道具名\n例：【PVP】购买 大还丹'
        )

    itemName = pvp.jfzCore.cleanName(tokens[0])
    count = 1
    if len(tokens) > 1:
        count = pvp.jfzCore.toInt(tokens[1], None)
        if count is None or count <= 0:
            return pvp.jfzCore.replyText(plugin_event, '数量必须是大于 0 的整数。')

    # 死皮不能购买（PRD 3.1：死皮不可购买/使用道具）
    status = pvp.jfzCore.deriveStatus(player.get('hp', 0))
    if status == pvp.jfzCore.STATUS_DEAD:
        return pvp.jfzCore.replyText(
            plugin_event, '你当前是「死皮」状态，无法购买道具。'
        )

    # 战斗中关闭商城（PRD 4.9）
    if pvp.jfzStore.isPlayerInMatch(playerName):
        return pvp.jfzCore.replyText(
            plugin_event, '你正在 PVP 对局中，战斗中无法购买道具。'
        )

    itemDef = pvp.jfzCore.getItemDef(itemName)
    if itemDef is None:
        return pvp.jfzCore.replyText(
            plugin_event, '商城没有「%s」这件道具。发送「【PVP】商城」查看在售道具。' % itemName
        )

    price = pvp.jfzStore.getItemPrice(itemName)
    total = price * count
    copper = int(player.get('copper', 0))
    if copper < total:
        return pvp.jfzCore.replyText(
            plugin_event,
            '铜币不足。\n「%s」单价 %d，买 %d 个需要 %d，你只有 %d。' % (
                itemName, price, count, total, copper
            )
        )

    # 先扣钱再加道具。扣钱和加道具各自都会立刻落盘，
    # 中间即使出异常也是「钱扣了但道具没加」这种**方向安全**的错误
    # （可以由管理补发），反过来白送道具就没法追了。
    leftCopper, _ = pvp.jfzStore.changePlayerCopper(playerName, -total)
    pvp.jfzStore.addItem(playerName, itemName, count)

    return pvp.jfzCore.replyText(
        plugin_event,
        '购买成功。\n%s ×%d（花费 %d 铜币）\n剩余铜币：%d\n发送「【PVP】背包」查看。' % (
            itemName, count, total, leftCopper
        )
    )


# ---------------------------------------------------------------------------
# 场外型道具的使用
# ---------------------------------------------------------------------------

# 当前版本「还没有实际效果」的道具。使用它们会被拒绝且**不消耗**。
#
# 【为什么不做成「用了就扣掉但什么都不发生」】
# PRD 4.8.2 里解毒丸写「解除中毒状态（预留）」、幻息丹写「效果待定」。
# 这两件在本次活动的规则里没有实际作用，如果玩家误发一次就白扣一件，
# 那是纯粹的负体验，而且很容易被当成 bug 报上来。
# 所以明确拒绝并说明原因，等后续版本补上玩法再放开。
NO_EFFECT_ITEMS = {'解毒丸', '幻息丹'}


def useOffFieldItem(plugin_event, playerName, itemName):
    """
    使用一件场外型道具（PRD 4.8.2 上半表）。立即生效。

    返回 True 表示已处理并回复过。
    """
    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        return pvp.jfzCore.replyText(plugin_event, '找不到玩家「%s」。' % playerName)

    itemDef = pvp.jfzCore.getItemDef(itemName)
    if itemDef is None:
        return pvp.jfzCore.replyText(
            plugin_event, '没有「%s」这件道具。' % itemName
        )

    itemType = itemDef.get('type', '')
    if itemType != pvp.jfzCore.TYPE_OFF_FIELD:
        # 全场型 / 回合型走到这里，说明玩家不在对局里却想用它们
        return pvp.jfzCore.replyText(
            plugin_event,
            '「%s」是%s道具，只能在 PVP 开战后、掷骰前的道具声明阶段使用。\n'
            '当前你不在对局中，无法使用。' % (itemName, itemType)
        )

    # 死皮不能用道具（PRD 3.1）
    status = pvp.jfzCore.deriveStatus(player.get('hp', 0))
    if status == pvp.jfzCore.STATUS_DEAD:
        return pvp.jfzCore.replyText(
            plugin_event, '你当前是「死皮」状态，无法使用道具。'
        )

    # 战斗中不能用场外道具（PRD 4.8.2「不在 PVP 中时」+ 4.9）
    if pvp.jfzStore.isPlayerInMatch(playerName):
        return pvp.jfzCore.replyText(
            plugin_event, '你正在 PVP 对局中，战斗期间无法使用场外道具。'
        )

    if itemName in NO_EFFECT_ITEMS:
        return pvp.jfzCore.replyText(
            plugin_event,
            '「%s」在当前版本还没有实际效果，为避免浪费，本次不消耗。\n'
            '如需使用可先留存在背包中。' % itemName
        )

    inventory = pvp.jfzStore.getInventory(playerName)
    if int(inventory.get(itemName, 0)) <= 0:
        return pvp.jfzCore.replyText(
            plugin_event, '你的背包里没有「%s」。发送「【PVP】背包」查看。' % itemName
        )

    heal = int(itemDef.get('heal', 0) or 0)

    if heal > 0:
        hpBefore = int(player.get('hp', 0))
        if hpBefore >= pvp.jfzCore.getHpLimit():
            # 满血时拒绝，避免白扣一件道具 —— 这是最容易被玩家骂的设计点
            return pvp.jfzCore.replyText(
                plugin_event,
                '你的血量已经满了（%d / %d），不需要使用「%s」，本次不消耗。' % (
                    hpBefore, pvp.jfzCore.getHpLimit(), itemName
                )
            )

        # 先扣道具再加血：宁可「扣了道具没加上血」（可补），
        # 也不要「加了血没扣道具」（白送）
        ok, leftCount = pvp.jfzStore.removeItem(playerName, itemName, 1)
        if not ok:
            return pvp.jfzCore.replyText(
                plugin_event, '你的背包里没有「%s」。发送「【PVP】背包」查看。' % itemName
            )

        hpAfter, delta = pvp.jfzStore.changePlayerHp(playerName, heal)
        statusAfter = pvp.jfzCore.deriveStatus(hpAfter)

        return pvp.jfzCore.replyText(
            plugin_event,
            '你使用了「%s」。\n血量：%d → %d（+%d）\n状态：%s\n剩余数量：%d' % (
                itemName, hpBefore, hpAfter, delta,
                pvp.jfzCore.getStatusName(statusAfter), leftCount
            )
        )

    # 没写 heal 又是场外型（理论上只有新增道具时才会出现），保守处理：不消耗。
    return pvp.jfzCore.replyText(
        plugin_event, '「%s」当前没有配置可用效果，本次不消耗。' % itemName
    )


# ---------------------------------------------------------------------------
# 管理侧：发放 / 回收道具
# ---------------------------------------------------------------------------

def giveItem(playerName, itemName, count=1):
    """发放道具。返回 (成功?, 提示文本)。"""
    if pvp.jfzStore.getPlayer(playerName) is None:
        return False, '找不到玩家「%s」。' % playerName
    if not pvp.jfzCore.isItemName(itemName):
        return False, '没有「%s」这件道具。' % itemName
    left = pvp.jfzStore.addItem(playerName, itemName, count)
    if left < 0:
        return False, '发放失败。'
    return True, '已给「%s」发放 %s ×%d，现有 %d 件。' % (playerName, itemName, count, left)


def takeItem(playerName, itemName, count=1):
    """回收道具。返回 (成功?, 提示文本)。"""
    if pvp.jfzStore.getPlayer(playerName) is None:
        return False, '找不到玩家「%s」。' % playerName
    if not pvp.jfzCore.isItemName(itemName):
        return False, '没有「%s」这件道具。' % itemName
    ok, left = pvp.jfzStore.removeItem(playerName, itemName, count)
    if not ok:
        return False, '「%s」的 %s 不足（现有 %d 件）。' % (playerName, itemName, left)
    return True, '已从「%s」回收 %s ×%d，剩余 %d 件。' % (playerName, itemName, count, left)
