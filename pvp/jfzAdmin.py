# -*- encoding: utf-8 -*-
'''
管理指令 —— 骰主与后台管理员能做的事（PRD 4.1 / 4.2 / 4.7.3 / 4.10）。

================================================================
【权限：骰主 ⊇ 后台管理员（本插件的约定，PRD 2.2 里不是这样）】
================================================================
PRD 2.2 的矩阵里骰主的权限**不是**管理员权限的超集 ——
「配置活动参数」「发放/扣除铜币」骰主那一栏标的是 ✖。
本插件按骰主要求改成「**骰主拥有后台管理员的全部权限**」：
骰主是这台机器人上权限最高的人，不该出现「骰主调不了活动参数，
得先把自己加成管理员」这种绕一圈的操作（而且那个入口本身也只有骰主能开）。

所以本模块只有两个权限档：

    requireMaster         → 只有骰主：注册后台群 / 增删管理员 / 结束活动
    requireMasterOrAdmin  → 骰主与管理员都能做：其余全部管理指令

玩家永远不行（PRD 2.2 第三条，不受本次调整影响）。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

import pvp


# ---------------------------------------------------------------------------
# 权限检查
# ---------------------------------------------------------------------------

def _adminHint():
    """权限不足时给出的**可执行**建议，而不是干巴巴一句「没有权限」。"""
    return (
        '该指令仅限骰主与后台管理员执行。\n'
        '需要加人的话由骰主执行：【PVP】添加管理 <QQ号>'
    )


def requireMaster(plugin_event):
    """
    要求「骰主」。返回 (是否有权限, 失败原因)。

    骰主的判定直接问 OlivaDiceCore 的骰主名单（见 jfzCore.isMaster）。
    """
    if pvp.jfzCore.isMaster(plugin_event):
        return True, ''
    return False, '该指令仅限骰主执行。'


def requireMasterOrAdmin(plugin_event):
    """
    要求「骰主或后台管理员」—— 全部管理指令的统一权限检查。

    ★ 这里是**唯一**的管理权限入口 ★
    以前还分了一个只认管理员的 requireAdmin（照抄 PRD 2.2 的矩阵），
    结果是「骰主改不了活动参数」这种反直觉行为。现在骰主 ⊇ 管理员，
    两档合一，调用方也就不可能再选错那个更窄的检查函数。
    """
    if pvp.jfzCore.isMaster(plugin_event):
        return True, ''

    userId = pvp.jfzCore.getUserId(plugin_event)
    if pvp.jfzStore.isBackstageAdmin(userId):
        return True, ''

    about = '当前已注册后台群：%s' % (pvp.jfzStore.getBackstageGroup() or '（无）')
    return False, _adminHint() + '\n' + about


def requireBackstageGroup(plugin_event):
    """要求「这条消息发在后台群里」。返回 (是否满足, 失败原因)。"""
    groupId = pvp.jfzCore.getGroupId(plugin_event)
    if not pvp.jfzStore.getBackstageGroup():
        return False, '当前还没有注册后台群，请骰主先执行「【PVP】注册后台群 游戏名」。'
    if not pvp.jfzStore.isBackstageGroup(groupId):
        return False, '该指令只能在后台群（%s）里执行。' % pvp.jfzStore.getBackstageGroup()
    return True, ''


def requireRegistered(plugin_event):
    """要求「插件已注册（骰主已声明游戏名）」。（是否满足, 失败原因）"""
    if pvp.jfzStore.isRegistered():
        return True, ''
    return False, '当前还没有注册游戏。请骰主先在目标群执行「【PVP】注册后台群 游戏名」。'


# ---------------------------------------------------------------------------
# 4.1.1 注册后台群并声明游戏名（骰主）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleRegisterBackstageGroup(plugin_event, args):
    """`【PVP】注册后台群 游戏名`"""
    ok, reason = requireMaster(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    gameName = pvp.jfzCore.cleanName(args)
    if not gameName:
        return pvp.jfzCore.replyText(
            plugin_event, '用法：【PVP】注册后台群 游戏名\n例：【PVP】注册后台群 尽伏诛'
        )
    if '【' in gameName or '】' in gameName:
        # 游戏名会被拼进 `【游戏名】PVP` 这个模板里，
        # 名字里再带方括号会让解析变得无法确定，直接拒掉。
        return pvp.jfzCore.replyText(plugin_event, '游戏名里不能包含【或】。')

    groupId = pvp.jfzCore.getGroupId(plugin_event)
    if not groupId:
        return pvp.jfzCore.replyText(plugin_event, '该指令只能在群里执行。')

    oldName = pvp.jfzStore.getGameName()
    pvp.jfzStore.setBackstageGroup(gameName, groupId)

    lines = ['后台群注册成功。', '群号：%s' % groupId, '当前游戏名：%s' % gameName]
    if oldName and oldName != gameName:
        lines.append('（原游戏名「%s」已被替换，数据仍然沿用。）' % oldName)
    lines.append('')
    lines.append('接下来可以：')
    lines.append('1. 【PVP】添加管理 QQ号　添加后台管理员')
    lines.append('2. 【PVP】注册玩家 群号 QQ号 玩家名称　注册玩家')
    lines.append('3. %s　配置活动' % pvp.jfzCore.ACTIVITY_DATE_EXAMPLE)
    lines.append('4. 玩家在各自个人群发送：')
    lines.append('　　【%s】PVP' % gameName)
    lines.append('　　发起人：玩家名')
    lines.append('　　被发起人：玩家名')
    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))


# ---------------------------------------------------------------------------
# 4.1.2 添加 / 移除后台管理员（骰主）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleAddAdmin(plugin_event, args):
    """
    `【PVP】添加管理 QQ号`

    ★ PRD 4.1.2 特别说明了为什么用 QQ 号 ★
    因为管理员可能**还没注册为玩家**（本插件的管理员和玩家是两套名单），
    用玩家名会加不了人。
    """
    ok, reason = requireMaster(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    ok, reason = requireRegistered(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    qq = pvp.jfzCore.splitArgs(args)
    if not qq:
        return pvp.jfzCore.replyText(
            plugin_event, '用法：【PVP】添加管理 QQ号\n例：【PVP】添加管理 987654321'
        )
    qq = pvp.jfzCore.cleanName(qq[0])
    if not qq:
        return pvp.jfzCore.replyText(plugin_event, 'QQ 号不能为空。')

    if not pvp.jfzStore.addAdmin(qq):
        return pvp.jfzCore.replyText(plugin_event, '「%s」已经在管理员名单里了。' % qq)

    admins = pvp.jfzStore.getAdminList()
    return pvp.jfzCore.replyText(
        plugin_event,
        '已添加后台管理员：%s\n当前管理员共 %d 人：\n%s' % (qq, len(admins), '\n'.join(admins))
    )


@pvp.jfzCore.handled
def handleRemoveAdmin(plugin_event, args):
    """`【PVP】移除管理 QQ号`"""
    ok, reason = requireMaster(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    ok, reason = requireRegistered(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    qq = pvp.jfzCore.splitArgs(args)
    if not qq:
        return pvp.jfzCore.replyText(
            plugin_event, '用法：【PVP】移除管理 QQ号\n例：【PVP】移除管理 987654321'
        )
    qq = pvp.jfzCore.cleanName(qq[0])

    if not pvp.jfzStore.removeAdmin(qq):
        return pvp.jfzCore.replyText(plugin_event, '「%s」不在管理员名单里。' % qq)

    admins = pvp.jfzStore.getAdminList()
    if admins:
        tail = '当前管理员共 %d 人：\n%s' % (len(admins), '\n'.join(admins))
    else:
        tail = '当前管理员名单为空。'
    return pvp.jfzCore.replyText(plugin_event, '已移除后台管理员：%s\n%s' % (qq, tail))


# ---------------------------------------------------------------------------
# 4.1.3 注册玩家（骰主 / 后台管理员）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleRegisterPlayer(plugin_event, args):
    """
    `【PVP】注册玩家 群号 QQ号 玩家名称`（在后台群发）
    `【PVP】注册玩家 QQ号 玩家名称`（在用户个人群发）

    【为什么要「先看参数个数、再看内容」判断是哪种格式】
    两种格式的第一个参数含义不同（群号 vs QQ号），都是纯数字，无法靠内容区分。
    唯一可靠的判据就是参数个数 3 还是 2。
    所以参数个数既不是 2 也不是 3 时，必须明确报错而不是猜。
    """
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    ok, reason = requireRegistered(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    tokens = pvp.jfzCore.splitArgs(args)
    currentGroup = pvp.jfzCore.getGroupId(plugin_event)

    if len(tokens) == 3:
        groupId = pvp.jfzCore.cleanName(tokens[0])
        userQQ = pvp.jfzCore.cleanName(tokens[1])
        displayName = pvp.jfzCore.cleanName(tokens[2])
    elif len(tokens) == 2:
        groupId = currentGroup
        userQQ = pvp.jfzCore.cleanName(tokens[0])
        displayName = pvp.jfzCore.cleanName(tokens[1])
    else:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法（二选一）：\n'
            '在后台群：【PVP】注册玩家 群号 QQ号 玩家名称\n'
            '在个人群：【PVP】注册玩家 QQ号 玩家名称\n'
            '例：【PVP】注册玩家 123456789 987654321 张三'
        )

    if not groupId or not userQQ or not displayName:
        return pvp.jfzCore.replyText(plugin_event, '群号、QQ号、玩家名称都不能为空。')

    # 同一个个人群不能被两个玩家占用：否则「用群号反查玩家」会有歧义，
    # 掷骰钩子、发起 PVP、背包查询全部会走错人。
    occupied = pvp.jfzStore.getPlayerByGroup(groupId)
    if occupied:
        occupiedQQ = str(occupied.get('user_qq', ''))
        if occupiedQQ != userQQ:
            return pvp.jfzCore.replyText(
                plugin_event,
                '群 %s 已经登记为玩家「%s」的个人群，一个群只能绑定一个玩家。' % (
                    groupId, occupied.get('display_name', '')
                )
            )

    ok, message = pvp.jfzStore.createPlayer(userQQ, displayName, groupId)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, message)

    # 出生血量与上限是两个配置项，两者相等时不必把同一个数写两遍；
    # 只有被打过折扣（初始 > 上限）时才把上限亮出来，否则骰主会以为哪里算错了。
    startHp = pvp.jfzCore.getInitialHp()
    hpLimit = pvp.jfzCore.getHpLimit()
    hpText = ('初始血量：%d（血量上限 %d）' % (startHp, hpLimit)
              if startHp != hpLimit else '初始血量：%d' % startHp)

    lines = [
        message,
        'QQ：%s' % userQQ,
        '个人群：%s' % groupId,
        '%s　初始铜币：%d' % (
            hpText,
            pvp.jfzStore.getConfigInt('initial_copper', 1000),
        ),
        '攻击值：%d（每回合 .raN 的基础值）' % pvp.jfzStore.getPlayerAttack(displayName),
    ]
    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))


# ---------------------------------------------------------------------------
# 4.2 活动配置（骰主 / 后台管理员）
# ---------------------------------------------------------------------------

def _requireOperatorAndRegistered(plugin_event):
    """
    「骰主或管理员」+「已注册」的合并检查（活动配置类指令统一走这里）。

    以前叫 `_requireAdminAndRegistered`，只认管理员 —— 骰主改活动参数会被拒，
    与「骰主权限最高」的直觉相反。现在骰主 ⊇ 管理员，名字也一并改掉，
    免得后来人照旧名去找那个已经不存在的行为。
    """
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return reason
    ok, reason = requireRegistered(plugin_event)
    if not ok:
        return reason
    return ''


@pvp.jfzCore.handled
def handleSetInitialHp(plugin_event, args):
    """
    `【PVP】设置初始血量 N`

    ★ 只管「之后注册的新玩家出生血量」★
    「初始血量」与「血量上限」是两个各自独立的配置项（见 jfzCore 常量区的说明），
    这一条既不改变已有玩家的血量，也不改变血条的天花板。

    【为什么不顺手把在场玩家的血一起改成 N】
    那是「全体重置血量」，跟「初始铜币」是同一个坑：活动打到一半有人执行一次，
    所有人的血线全部被回满或被削，前面几场的伤害就白打了。
    要动在场玩家请用 `【PVP】修改血量`（也支持 `【PVP】修改血量 张三 +50`）。

    【出生血量不允许超过上限】
    超出时配置照写（骰主可能正打算接着去调上限），但实际生效值被上限压住，
    所以这里必须把「实际给多少」和「为什么给少了」一起说清楚 ——
    否则骰主设完回头发现新玩家血条短一截，只能去翻存档找原因。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 1 or value > pvp.jfzCore.HP_LIMIT_MAX:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：【PVP】设置初始血量 200（1 ~ %d 的整数）\n'
            '★ 只影响之后注册的新玩家 ★ 想让血条本身更长，请用「【PVP】设置血量上限」。'
            % pvp.jfzCore.HP_LIMIT_MAX
        )

    before = pvp.jfzCore.getInitialHp()
    pvp.jfzStore.setConfigValue('initial_hp', value)

    # 写完再读一次：getInitialHp() 会夹到上限，这里的值就是「新玩家实际能拿到多少血」。
    effective = pvp.jfzCore.getInitialHp()
    limit = pvp.jfzCore.getHpLimit()

    lines = [
        '初始血量已设为 %d（原为 %d）。' % (value, before),
        '· 之后注册的新玩家出生血量 = %d' % effective,
        '· 已有玩家的血量不会自动变动（要调请用「【PVP】修改血量」）',
        '· 血量上限仍为 %d（要改血条天花板的长度，请用「【PVP】设置血量上限」）' % limit,
    ]
    if value > limit:
        lines.append(
            '· ⚠ %d 超过了当前血量上限 %d，新玩家出生时会被压到 %d；'
            '想让它真正生效请接着执行「【PVP】设置血量上限 %d」'
            % (value, limit, effective, value)
        )
    if effective <= pvp.jfzCore.HP_DYING_THRESHOLD:
        lines.append(
            '· ⚠ %d ≤ 濒死线 %d：新玩家一注册就是濒死状态，通常不是你想要的。'
            % (effective, pvp.jfzCore.HP_DYING_THRESHOLD)
        )
    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))


@pvp.jfzCore.handled
def handleSetHpLimit(plugin_event, args):
    """
    `【PVP】设置血量上限 N`

    血量天花板：道具回血的封顶值、血量夹取上界、`血量`/`背包` 里「当前 / 上限」的分母。

    【为什么不顺手改初始血量】
    两者是独立的：上限说的是「血条能有多长」，初始血量说的是「新号从多少血起步」。
    常见的设法是上限高、初始低（上限 500 / 初始 200），给玩家留出成长空间 ——
    如果这里顺手把初始血量也改成 500，新号一进来就是满血天花板，成长空间就没了。

    ★ 调低会削到现存玩家的血 ★
    存档落盘时 hp 会被夹到当前上限（见 jfzStore._packPlayer），
    这是最容易让人「咦我的血怎么少了」的地方，所以回执里把受影响的人名列出来。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 1 or value > pvp.jfzCore.HP_LIMIT_MAX:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：【PVP】设置血量上限 500（1 ~ %d 的整数）\n'
            '★ 调小之后，血量超过它的玩家会被削到新上限 ★\n'
            '想保住某人的血量，请随后用「【PVP】修改血量 玩家名 N」单独补（但补不过新上限）。'
            % pvp.jfzCore.HP_LIMIT_MAX
        )

    before = pvp.jfzCore.getHpLimit()
    pvp.jfzStore.setConfigValue('hp_limit', value)

    lines = [
        '血量上限已设为 %d（原为 %d）。' % (value, before),
        '· 道具回血最多回满到 %d，血量显示为「当前 / %d」' % (value, value),
        '· 初始血量（新玩家出生血量）不受影响，当前为 %d'
        % pvp.jfzCore.getInitialHp(),
    ]
    if value < before:
        over = []
        for name in pvp.jfzStore.listPlayerNames():
            player = pvp.jfzStore.getPlayer(name) or {}
            if int(player.get('hp', 0)) > value:
                over.append(name)
        if over:
            shown = '、'.join(over[:8]) + ('…' if len(over) > 8 else '')
            lines.append(
                '· ★ 上限降低后，血量超过 %d 的 %d 名玩家（%s）会在下次存档时被削到 %d'
                % (value, len(over), shown, value)
            )
    if value <= pvp.jfzCore.HP_DYING_THRESHOLD:
        lines.append(
            '· ⚠ %d ≤ 濒死线 %d：所有血量高于它的玩家都会被打成濒死，通常不是你想要的。'
            % (value, pvp.jfzCore.HP_DYING_THRESHOLD)
        )
    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))


@pvp.jfzCore.handled
def handleSetInitialCopper(plugin_event, args):
    """
    `【PVP】设置初始铜币 N`

    ★ 只影响**之后注册**的玩家 ★
    PRD 4.2 的表格写的是「全体玩家统一初始铜币」，说的是「所有新玩家的起点一致」。
    如果把它实现成「一改就把在场所有人的铜币重置成这个数」，
    那就等价于一条不需要任何确认的「全体重置经济」——
    活动进行到一半有人执行一次，所有人的买卖记录就全废了。
    所以要调整在场玩家的钱，请用 `【PVP】发放铜币` / `【PVP】扣除铜币`。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 0:
        return pvp.jfzCore.replyText(plugin_event, '用法：【PVP】设置初始铜币 1000（非负整数）')

    pvp.jfzStore.setConfigValue('initial_copper', value)
    return pvp.jfzCore.replyText(
        plugin_event,
        '初始铜币已设为 %d。\n（仅对之后注册的玩家生效；调整在场玩家请用发放/扣除铜币。）' % value
    )


@pvp.jfzCore.handled
def handleSetInitialAttack(plugin_event, args):
    """
    `【PVP】设置初始攻击 N` —— 全体统一的基础攻击值（每回合 `.raN` 的基础）。

    ★ 也接受「玩家名 N」两参数写法 ★
    骰主脑子里这两件事都叫「设置初始攻击」（统一值 / 某个人的值），
    所以按内容识别：第一个词是数字 → 全局；否则当成玩家名 → 个人。
    这样他不用记两条指令。显式写法另有 `【PVP】设置个人攻击`（同一个 handler）。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    tokens = pvp.jfzCore.splitArgs(args)

    # 两个及以上参数 → 第一个是玩家名，走个人设定
    if len(tokens) >= 2:
        return _setPlayerAttack(plugin_event, tokens[0], tokens[1])

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < pvp.jfzCore.ATTACK_MIN or value > pvp.jfzCore.ATTACK_MAX:
        return pvp.jfzCore.replyText(plugin_event, _attackUsageText())

    pvp.jfzStore.setConfigValue('initial_attack', value)
    return pvp.jfzCore.replyText(
        plugin_event,
        '全体初始攻击已设为 %d（每回合 .ra%d）。\n'
        '· 之后再开打的对局都按新值算（进行中的对局不受影响，掷骰列表已发过）\n'
        '· 要给某个人单独设，发：【PVP】设置个人攻击 玩家名 数值' % (value, value)
    )


@pvp.jfzCore.handled
def handleSetPlayerAttack(plugin_event, args):
    """
    `【PVP】设置个人攻击 玩家名 N` —— 只覆盖某一个人的基础攻击值。

    `N` 写 0 表示**清除**个人设定，回落到全体统一值。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    tokens = pvp.jfzCore.splitArgs(args)
    if len(tokens) < 2:
        return pvp.jfzCore.replyText(plugin_event, _attackUsageText())
    return _setPlayerAttack(plugin_event, tokens[0], tokens[1])


def _attackUsageText():
    """攻击值相关指令的统一用法说明（三个入口共用一份，避免文案各写一套）。"""
    return (
        '用法：\n'
        '【PVP】设置初始攻击 %d　　　全体统一值（%d ~ %d）\n'
        '【PVP】设置个人攻击 玩家名 %d　只给某人单独设\n'
        '【PVP】设置个人攻击 玩家名 0　清除个人设定，回落到统一值\n'
        '★ 这是每回合 `.raN` 的基础值。青果 `.ra` 的判定骰值范围是 1~100，\n'
        '　每回合再叠加道具修正（胜临符 +10 / 错影符 -10），最终值会被夹在 1~100 内。'
        % (pvp.jfzCore.ATTACK_DEFAULT, pvp.jfzCore.ATTACK_MIN,
           pvp.jfzCore.ATTACK_MAX, pvp.jfzCore.ATTACK_DEFAULT)
    )


def _setPlayerAttack(plugin_event, nameText, valueText):
    """处理「给某人设攻击值」：写 0 视为清除个人设定。"""
    playerName = pvp.jfzCore.cleanName(nameText)
    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        return pvp.jfzCore.replyText(
            plugin_event,
            '找不到玩家「%s」。玩家名要写全（可先用【PVP】名单查看）。' % playerName
        )

    value = pvp.jfzCore.toInt(valueText, None)
    if value is None:
        return pvp.jfzCore.replyText(plugin_event, _attackUsageText())

    globalValue = pvp.jfzStore.getGlobalAttack()

    # 0 = 清除个人设定。刻意用 0 表示清除：攻击值的合法范围是 1~100，
    # 「攻击 0」本身没有意义，用它当哨兵值不会和任何真实设定撞车。
    if value == 0:
        ok, current = pvp.jfzStore.setPlayerAttack(playerName, None)
        if not ok:
            return pvp.jfzCore.replyText(plugin_event, str(current))
        return pvp.jfzCore.replyText(
            plugin_event,
            '「%s」的个人攻击值已清除，之后按全体统一值 %d 计算。' % (playerName, current)
        )

    if value < pvp.jfzCore.ATTACK_MIN or value > pvp.jfzCore.ATTACK_MAX:
        return pvp.jfzCore.replyText(plugin_event, _attackUsageText())

    ok, current = pvp.jfzStore.setPlayerAttack(playerName, value)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, str(current))

    lines = [
        '「%s」的个人攻击值已设为 %d（每回合 .ra%d）。' % (playerName, value, value),
        '· 只影响这个人，其他人的基础值仍是 %d' % globalValue,
        '· 下一场对局生效（进行中的对局按已发出的掷骰列表算）',
        '· 要取消：发【PVP】设置个人攻击 %s 0' % playerName,
    ]
    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))


@pvp.jfzCore.handled
def handleSetActivityDate(plugin_event, args):
    """
    `【PVP】设置活动日期 活动周期：2026-09-01 ~ 2026-09-02 每日开放：14:00 ~ 18:00`

    写法很松：波浪线两侧的空格可省、两段标签可省、「每日开放」写在前面也行
    （解析见 jfzCore.parseActivityDateText 的注释）。
    格式示例统一取 jfzCore.ACTIVITY_DATE_EXAMPLE，别在这里另写一份。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    startText, endText, beginText, endTimeText = pvp.jfzCore.parseActivityDateText(args)

    if not startText or not endText:
        return pvp.jfzCore.replyText(
            plugin_event,
            '没能从参数里认出「活动周期」的两个日期。\n' + pvp.jfzCore.ACTIVITY_DATE_USAGE
        )
    okStart, startDate = pvp.jfzCore.parseYmd(startText)
    if not okStart:
        return pvp.jfzCore.replyText(
            plugin_event, '活动周期的开始日期格式不对：%s\n请写成 2026-09-01。' % startText
        )
    okEnd, endDate = pvp.jfzCore.parseYmd(endText)
    if not okEnd:
        return pvp.jfzCore.replyText(
            plugin_event, '活动周期的结束日期格式不对：%s\n请写成 2026-09-02。' % endText
        )
    if endDate < startDate:
        return pvp.jfzCore.replyText(plugin_event, '活动周期的结束日期不能早于开始日期。')

    if not beginText or not endTimeText:
        return pvp.jfzCore.replyText(
            plugin_event,
            '没能从参数里认出「每日开放」的两个时间。\n' + pvp.jfzCore.ACTIVITY_DATE_USAGE
        )
    okBegin, dailyStart = pvp.jfzCore.parseTimeText(beginText)
    if not okBegin:
        return pvp.jfzCore.replyText(plugin_event, '每日开放开始时间格式不对：%s（应为 14:00）' % beginText)
    okEndTime, dailyEnd = pvp.jfzCore.parseTimeText(endTimeText)
    if not okEndTime:
        return pvp.jfzCore.replyText(plugin_event, '每日开放结束时间格式不对：%s（应为 18:00）' % endTimeText)
    if dailyEnd <= dailyStart:
        return pvp.jfzCore.replyText(plugin_event, '每日开放的结束时间必须晚于开始时间。')

    pvp.jfzStore.setConfigValue('start_date', startDate)
    pvp.jfzStore.setConfigValue('end_date', endDate)
    pvp.jfzStore.setConfigValue('daily_start_time', dailyStart)
    pvp.jfzStore.setConfigValue('daily_end_time', dailyEnd)

    # 回执里再带一次格式示例：下一次要改的时候不用去翻文档/清单。
    return pvp.jfzCore.replyText(
        plugin_event,
        '活动时间已设置。\n'
        '活动周期：%s\n'
        '每日开放：%s ~ %s\n'
        '下次修改照这个格式写：\n%s' % (
            pvp.jfzCore.dateRangeText(startDate, endDate), dailyStart, dailyEnd,
            pvp.jfzCore.ACTIVITY_DATE_EXAMPLE,
        )
    )


@pvp.jfzCore.handled
def handleSetDailyAttackLimit(plugin_event, args):
    """`【PVP】设置主动次数 N` —— 每人每天可主动发起的 PVP 次数。"""
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 0:
        return pvp.jfzCore.replyText(plugin_event, '用法：【PVP】设置主动次数 3（非负整数，0 表示不许主动发起）')

    pvp.jfzStore.setConfigValue('daily_attack_limit', value)
    return pvp.jfzCore.replyText(plugin_event, '每人每日主动次数已设为 %d。' % value)


@pvp.jfzCore.handled
def handleSetRounds(plugin_event, args):
    """`【PVP】设置回合数 N` —— 每场 PVP 的回合数。"""
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 1 or value > pvp.jfzCore.ROUNDS_MAX:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：【PVP】设置回合数 10（1 ~ %d 的整数）\n'
            '回合数就是每个玩家要逐条发送的 `.raN` 条数。' % pvp.jfzCore.ROUNDS_MAX
        )

    pvp.jfzStore.setConfigValue('rounds', value)
    note = ''
    if value > 20:
        note = '\n（提醒：回合数偏大时，一场对局玩家要发 %d 条 .ra 指令，请确认这是期望的。）' % value
    return pvp.jfzCore.replyText(plugin_event, '每场回合数已设为 %d。%s' % (value, note))


@pvp.jfzCore.handled
def handleSetItemLimit(plugin_event, args):
    """`【PVP】设置每种道具限用 N` —— 每种道具每场限用次数。"""
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 0:
        return pvp.jfzCore.replyText(plugin_event, '用法：【PVP】设置每种道具限用 1（非负整数）')

    pvp.jfzStore.setConfigValue('item_limit_per_match', value)
    return pvp.jfzCore.replyText(plugin_event, '每种道具每场限用次数已设为 %d。' % value)


@pvp.jfzCore.handled
def handleSetDeclareTimeout(plugin_event, args):
    """
    `【PVP】设置声明超时 N` —— 道具声明阶段超时（分钟），0 表示不限制。

    ★ 本插件相对 PRD 的扩展 ★ PRD 对声明阶段没有超时概念。
    没有超时的话，一对玩家都放着不声明，对局就永远卡在声明阶段，
    队列里后面所有人的请求永远轮不上 —— 这正是「排队中之后再无下文」的根源。
    默认 10 分钟；只想恢复 PRD 原行为就配 0（靠【PVP】强制结束兜底）。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 0 or value > 24 * 60:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：【PVP】设置声明超时 10（0 ~ 1440 的整数，单位分钟）\n'
            '0 表示不限制：对局会一直等双方声明完毕，卡住时用【PVP】强制结束兜底。'
        )

    pvp.jfzStore.setConfigValue('declare_timeout_minutes', value)
    if value == 0:
        return pvp.jfzCore.replyText(
            plugin_event, '声明超时已关闭：对局会一直等待双方声明完毕。'
        )
    return pvp.jfzCore.replyText(
        plugin_event,
        '声明超时已设为 %d 分钟：声明阶段超过 %d 分钟未完成，'
        '对局自动取消并返还道具与主动次数。' % (value, value)
    )


@pvp.jfzCore.handled
def handleSetFightTimeout(plugin_event, args):
    """
    `【PVP】设置掷骰超时 N` —— 掷骰阶段超时（分钟），0 表示不限制。

    ★ 本插件相对 PRD 的扩展 ★ 与声明超时同理：任何一掷被防作弊 /
    污染防护拦下而没补掷，那一方就永远少一格，双方「都掷满」的结算
    条件永远不成立。掷骰阶段超时后**按已有记录结算**（缺的回合按
    0 伤害计，战报注明），而不是取消 —— 已经打了一部分，取消会丢账。
    默认 30 分钟。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    value = pvp.jfzCore.toInt(args, None)
    if value is None or value < 0 or value > 24 * 60:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：【PVP】设置掷骰超时 30（0 ~ 1440 的整数，单位分钟）\n'
            '0 表示不限制：对局会一直等双方掷满，卡住时用【PVP】强制结束兜底。'
        )

    pvp.jfzStore.setConfigValue('fight_timeout_minutes', value)
    if value == 0:
        return pvp.jfzCore.replyText(
            plugin_event, '掷骰超时已关闭：对局会一直等待双方掷满全部回合。'
        )
    return pvp.jfzCore.replyText(
        plugin_event,
        '掷骰超时已设为 %d 分钟：掷骰阶段超过 %d 分钟未完成，'
        '将按已记录的结果结算（未掷回合按 0 伤害计）。' % (value, value)
    )


@pvp.jfzCore.handled
def handleSetDamageTable(plugin_event, args):
    """
    `【PVP】设置伤害表 等级=扣血 ...`

    例：`【PVP】设置伤害表 大成功=20 极难成功=15 困难成功=10 成功=5 失败=0 大失败=0`

    ★ 允许「只改其中几档」★
    PRD 的示例把六档全写上了，但逐档设置（一次只给一档）在实战里更常用 ——
    调平衡时往往只想动「困难成功」那一档。
    所以这里做的是**增量更新**：只覆盖本次出现的档位，没提到的保持原值。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    tokens = pvp.jfzCore.splitArgs(args)
    if not tokens:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：【PVP】设置伤害表 等级=扣血 ...\n'
            '例：【PVP】设置伤害表 大成功=20 极难成功=15 困难成功=10 成功=5 失败=0 大失败=0\n'
            '可用等级：%s' % ' / '.join(pvp.jfzCore.LEVEL_ORDER)
        )

    changed = []
    unknown = []
    for token in tokens:
        if '=' not in token:
            unknown.append(token)
            continue
        level, _, valueText = token.partition('=')
        level = level.strip()
        value = pvp.jfzCore.toInt(valueText, None)
        if level not in pvp.jfzCore.LEVEL_ORDER or value is None or value < 0:
            unknown.append(token)
            continue
        pvp.jfzStore.setDamageTableLevel(level, value)
        changed.append('%s=%d' % (level, value))

    if not changed:
        return pvp.jfzCore.replyText(
            plugin_event,
            '没有解析出任何有效档位。\n可用等级：%s\n格式：等级=扣血' % ' / '.join(pvp.jfzCore.LEVEL_ORDER)
        )

    lines = ['伤害表已更新：%s' % '  '.join(changed)]
    if unknown:
        lines.append('（忽略无法识别的项：%s）' % ' '.join(unknown))
    lines.append('')
    lines.append(pvp.jfzStore.getConfigTableText())
    return pvp.jfzCore.replyText(plugin_event, '\n'.join(lines))


# ---------------------------------------------------------------------------
# 4.7.3 修改玩家血量（骰主 / 后台管理员）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleModifyHp(plugin_event, args):
    """
    `【PVP】修改血量 玩家名称 血量值` —— 设为指定值
    `【PVP】修改血量 玩家名称 +N`     —— 增加
    `【PVP】修改血量 玩家名称 -N`     —— 减少

    ★ PRD 2.1 明确：没有「复活玩家」指令，统一为「修改玩家血量」★
    所以把死皮的人改回 100 血，他就自然恢复正常了
    （状态是现算的，见 jfzCore.deriveStatus）。
    """
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    ok, reason = requireRegistered(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    tokens = pvp.jfzCore.splitArgs(args)
    if len(tokens) < 2:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：\n'
            '【PVP】修改血量 张三 150　（设为 150）\n'
            '【PVP】修改血量 张三 +50　（增加 50，上限 %d）\n'
            '【PVP】修改血量 张三 -30　（减少 30，最低 0）' % pvp.jfzCore.getHpLimit()
        )

    playerName = pvp.jfzCore.cleanName(tokens[0])
    valueText = tokens[1].strip()
    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        return pvp.jfzCore.replyText(plugin_event, '找不到玩家「%s」。' % playerName)

    hpBefore = int(player.get('hp', 0))

    if valueText.startswith('+') or valueText.startswith('-'):
        delta = pvp.jfzCore.toInt(valueText, None)
        if delta is None:
            return pvp.jfzCore.replyText(plugin_event, '增减量格式不对：%s' % valueText)
        if abs(delta) > pvp.jfzCore.VALUE_MAX:
            return pvp.jfzCore.replyText(plugin_event, '增减量过大，请检查是否多打了几个 0。')
        hpAfter, realDelta = pvp.jfzStore.changePlayerHp(playerName, delta)
        action = '增加' if delta >= 0 else '减少'
        note = ''
        if realDelta != delta:
            note = '\n（因血量上下限 %d ~ 0 的限制，实际%s %d）' % (
                pvp.jfzCore.getHpLimit(), action, abs(realDelta)
            )
    else:
        value = pvp.jfzCore.toInt(valueText, None)
        if value is None:
            return pvp.jfzCore.replyText(
                plugin_event, '血量值格式不对：%s（应为整数，或用 +N / -N 表示增减）' % valueText
            )
        if value < 0 or value > pvp.jfzCore.VALUE_MAX:
            return pvp.jfzCore.replyText(
                plugin_event, '血量值应在 0 ~ %d 之间（上限 %d）。' % (
                    pvp.jfzCore.VALUE_MAX, pvp.jfzCore.getHpLimit()
                )
            )
        hpAfter = pvp.jfzStore.setPlayerHp(playerName, value)
        note = ''
        if value > pvp.jfzCore.getHpLimit():
            note = '\n（血量上限为 %d，已自动截断。）' % pvp.jfzCore.getHpLimit()

    statusBefore = pvp.jfzCore.getStatusName(pvp.jfzCore.deriveStatus(hpBefore))
    statusAfter = pvp.jfzCore.getStatusName(pvp.jfzCore.deriveStatus(hpAfter))

    return pvp.jfzCore.replyText(
        plugin_event,
        '「%s」血量：%d（%s）→ %d（%s）%s' % (
            playerName, hpBefore, statusBefore, hpAfter, statusAfter, note
        )
    )


# ---------------------------------------------------------------------------
# 【PVP】管理 —— 管理指令一览（扩展指令）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleAdminHelp(plugin_event, args=None):
    """
    `【PVP】管理` —— 列出全部管理 / 骰主指令（骰主 + 后台管理员可用）。

    【为什么正文不写在这个文件里】
    清单的内容必须跟着路由表（jfzReply.MASTER_COMMANDS / ADMIN_COMMANDS）走，
    自己手写一份就一定会漏（加了指令忘了补清单，而且不会有任何报错）。
    所以正文由 jfzReply.getAdminHelpText() 从路由表现场拼，
    本函数只负责「查权限 → 回一段文本」。

    ★ 权限用 requireMasterOrAdmin ★
    骰主与后台管理员都能查看自己的权限范围；玩家会被拒并收到 _adminHint
    （「该指令仅限骰主与后台管理员执行」），而不是一句干巴巴的「没权限」。

    args 用不到也必须保留形参：分发表统一按 f(plugin_event, args) 调用，
    少一个参数会 TypeError，而异常被 main.Event.group_message 吞掉，
    现象只有「发了指令毫无反应」——详见 handleShowConfig 的注释。
    """
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)
    return pvp.jfzCore.replyText(plugin_event, pvp.jfzReply.getAdminHelpText())


# ---------------------------------------------------------------------------
# 4.2 发放 / 扣除铜币（骰主 / 后台管理员）
# ---------------------------------------------------------------------------

def _handleCopperChange(plugin_event, args, isGive):
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    commandName = '发放铜币' if isGive else '扣除铜币'
    tokens = pvp.jfzCore.splitArgs(args)
    if len(tokens) < 2:
        return pvp.jfzCore.replyText(
            plugin_event, '用法：【PVP】%s 玩家名称 N\n例：【PVP】%s 张三 500' % (commandName, commandName)
        )

    playerName = pvp.jfzCore.cleanName(tokens[0])
    amount = pvp.jfzCore.toInt(tokens[1], None)
    if amount is None or amount < 0:
        return pvp.jfzCore.replyText(plugin_event, '数量必须是非负整数。')
    if amount > pvp.jfzCore.VALUE_MAX:
        return pvp.jfzCore.replyText(plugin_event, '数量过大，请检查是否多打了几个 0。')

    player = pvp.jfzStore.getPlayer(playerName)
    if not player:
        return pvp.jfzCore.replyText(plugin_event, '找不到玩家「%s」。' % playerName)

    before = int(player.get('copper', 0))
    after, realDelta = pvp.jfzStore.changePlayerCopper(playerName, amount if isGive else -amount)

    note = ''
    if not isGive and realDelta != -amount:
        note = '\n（余额不足，已扣到 0 为止。）'

    return pvp.jfzCore.replyText(
        plugin_event,
        '%s完成。「%s」铜币：%d → %d%s' % (
            '发放' if isGive else '扣除', playerName, before, after, note
        )
    )


@pvp.jfzCore.handled
def handleGiveCopper(plugin_event, args):
    """`【PVP】发放铜币 玩家名称 N`"""
    return _handleCopperChange(plugin_event, args, isGive=True)


@pvp.jfzCore.handled
def handleDeductCopper(plugin_event, args):
    """`【PVP】扣除铜币 玩家名称 N`"""
    return _handleCopperChange(plugin_event, args, isGive=False)


# ---------------------------------------------------------------------------
# 4.8 道具的发放 / 回收（本插件扩展，PRD 只写了玩家自己买）
# ---------------------------------------------------------------------------
# PRD 4.8 只有「商城 → 购买」一条获得道具的路径，但一场活动里
# 「开局给每人发一套道具」「把违规获得的道具收回」都是刚需。
# 所以补两条管理指令，并在指令手册里标注为「扩展」。

@pvp.jfzCore.handled
def handleGiveItem(plugin_event, args):
    """`【PVP】发放道具 玩家名称 道具名 [数量]`（扩展）"""
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    tokens = pvp.jfzCore.splitArgs(args)
    if len(tokens) < 2:
        return pvp.jfzCore.replyText(
            plugin_event, '用法：【PVP】发放道具 玩家名称 道具名 [数量]\n例：【PVP】发放道具 张三 大还丹 2'
        )

    playerName = pvp.jfzCore.cleanName(tokens[0])
    itemName = pvp.jfzCore.cleanName(tokens[1])
    count = 1
    if len(tokens) > 2:
        count = pvp.jfzCore.toInt(tokens[2], None)
        if count is None or count <= 0:
            return pvp.jfzCore.replyText(plugin_event, '数量必须是大于 0 的整数。')

    ok, message = pvp.jfzShop.giveItem(playerName, itemName, count)
    return pvp.jfzCore.replyText(plugin_event, message)


@pvp.jfzCore.handled
def handleTakeItem(plugin_event, args):
    """`【PVP】回收道具 玩家名称 道具名 [数量]`（扩展）"""
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    tokens = pvp.jfzCore.splitArgs(args)
    if len(tokens) < 2:
        return pvp.jfzCore.replyText(
            plugin_event, '用法：【PVP】回收道具 玩家名称 道具名 [数量]\n例：【PVP】回收道具 张三 大还丹 1'
        )

    playerName = pvp.jfzCore.cleanName(tokens[0])
    itemName = pvp.jfzCore.cleanName(tokens[1])
    count = 1
    if len(tokens) > 2:
        count = pvp.jfzCore.toInt(tokens[2], None)
        if count is None or count <= 0:
            return pvp.jfzCore.replyText(plugin_event, '数量必须是大于 0 的整数。')

    ok, message = pvp.jfzShop.takeItem(playerName, itemName, count)
    return pvp.jfzCore.replyText(plugin_event, message)


# ---------------------------------------------------------------------------
# 扩展：改商城价格 / 查看配置 / 查看名单 / 删除玩家
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleSetItemPrice(plugin_event, args):
    """
    `【PVP】设置道具价格 道具名 N`（扩展）

    PRD 没给商城价格，也没给改价指令 —— 但价格必须有出处。
    默认价写在 jfzCore.ITEM_TABLE 里，这条指令用来覆盖它。
    """
    errorText = _requireOperatorAndRegistered(plugin_event)
    if errorText:
        return pvp.jfzCore.replyText(plugin_event, errorText)

    tokens = pvp.jfzCore.splitArgs(args)
    if len(tokens) < 2:
        return pvp.jfzCore.replyText(
            plugin_event,
            '用法：【PVP】设置道具价格 道具名 价格\n例：【PVP】设置道具价格 大还丹 500\n'
            '可设置的道具：%s' % ' / '.join(pvp.jfzCore.ITEM_ORDER)
        )

    itemName = pvp.jfzCore.cleanName(tokens[0])
    price = pvp.jfzCore.toInt(tokens[1], None)
    if price is None or price < 0:
        return pvp.jfzCore.replyText(plugin_event, '价格必须是非负整数。')

    if not pvp.jfzStore.setItemPrice(itemName, price):
        return pvp.jfzCore.replyText(
            plugin_event, '没有「%s」这件道具。可设置的道具：%s' % (
                itemName, ' / '.join(pvp.jfzCore.ITEM_ORDER)
            )
        )
    return pvp.jfzCore.replyText(plugin_event, '「%s」售价已设为 %d 铜币。' % (itemName, price))


@pvp.jfzCore.handled
def handleShowConfig(plugin_event, args=None):
    """
    `【PVP】配置` —— 查看当前活动配置（骰主 / 后台管理员）。

    ★ 参数里的 args 必须留着，哪怕用不到 ★
    jfzReply 分发表是统一按 `f(plugin_event, args)` 调用的，
    少一个参数就会 TypeError；而那个异常会被 main.Event.group_message 的 try
    吞掉，玩家看到的现象只有「发了指令毫无反应」，根本猜不到是签名问题。
    所以「用不到参数」也不等于「可以省掉参数」。
    """
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)
    return pvp.jfzCore.replyText(plugin_event, pvp.jfzStore.getConfigTableText())


@pvp.jfzCore.handled
def handleShowPlayers(plugin_event, args=None):
    """`【PVP】名单` —— 查看全量玩家数据（骰主 / 后台管理员，PRD 2.2「查看全量数据」）。"""
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)
    return pvp.jfzCore.replyText(plugin_event, pvp.jfzStore.getPlayerTableText())


@pvp.jfzCore.handled
def handleDeletePlayer(plugin_event, args):
    """`【PVP】删除玩家 玩家名称`（扩展）—— 从玩家表、队列、对局里一并摘掉。"""
    ok, reason = requireMasterOrAdmin(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    playerName = pvp.jfzCore.cleanName(args)
    if not playerName:
        return pvp.jfzCore.replyText(plugin_event, '用法：【PVP】删除玩家 玩家名称')

    if not pvp.jfzStore.getPlayer(playerName):
        return pvp.jfzCore.replyText(plugin_event, '找不到玩家「%s」。' % playerName)

    # 先把队列里的相关请求摘掉，否则会留着指向一个已经不存在的人的请求
    pvp.jfzStore.removeQueueByPlayer(playerName)

    # 正在对局里的话，先把这场按「强制终止」处理，再删人 ——
    # 否则对局对象里会残留一个查不到的玩家，结算时直接报错。
    match = pvp.jfzStore.getMatch()
    if match and match.get('state') == pvp.jfzCore.MATCH_ACTIVE:
        if playerName in [match.get('challenger_name'), match.get('target_name')]:
            pvp.jfzEngine.cancelAllForWindowClose(pvp.jfzCore.getBotHash(plugin_event))

    pvp.jfzStore.deletePlayer(playerName)
    return pvp.jfzCore.replyText(plugin_event, '已删除玩家「%s」及其全部数据。' % playerName)


# ---------------------------------------------------------------------------
# 强制终止对局（PRD 3.2「管理强制终止」的落地，扩展指令）
# ---------------------------------------------------------------------------

def handleForceEnd(plugin_event, args=None):
    """
    `【PVP】强制结束` —— 转发给 jfzEngine 执行。

    【为什么实现不放在这里】
    这条指令要动的是「对局」本身：要把 match 清掉、把已声明的道具退回、
    还要给双方各推一条通知 —— 这些内部结构只有 jfzEngine 知道。
    放在本文件里就得把 match 的内部字段规则复制一份出来，迟早会不同步。

    【那为什么还要有这个转发函数】
    jfzReply.ADMIN_COMMANDS 的分发是 `getattr(jfzAdmin, 函数名)`，
    整张表假定「后台管理指令全部由 jfzAdmin 承接」。少了这个函数，
    路由表里就会有一条指向不存在函数的指令 —— 玩家一用就抛 AttributeError，
    而且异常被 main.group_message 的 try 吞掉，表现成「发了指令毫无反应」。
    """
    return pvp.jfzEngine.handleForceEnd(plugin_event)


# ---------------------------------------------------------------------------
# 4.10 结束活动并删除所有数据（骰主，二次确认）
# ---------------------------------------------------------------------------

@pvp.jfzCore.handled
def handleEndActivity(plugin_event, args):
    """
    `【PVP】结束活动`          → 第一步：只弹确认
    `【PVP】结束活动 确认`     → 第二步：真的删

    ★ 为什么第一步要「什么都不做」★
    这条指令会删掉所有玩家、背包、铜币、配置，不可恢复。
    如果 `【PVP】结束活动` 直接执行，那「想看看还剩几天」的人手一抖
    整场活动就没了。所以把破坏性动作全部压在第二个词上。
    """
    ok, reason = requireMaster(plugin_event)
    if not ok:
        return pvp.jfzCore.replyText(plugin_event, reason)

    confirmWord = pvp.jfzCore.cleanName(args)

    if confirmWord != '确认':
        gameName = pvp.jfzStore.getGameName() or '（未注册）'
        return pvp.jfzCore.replyText(
            plugin_event,
            '确认结束当前游戏【%s】吗？\n'
            '此操作将删除所有玩家、对局、背包、铜币、活动配置等全部数据，且不可恢复。\n'
            '如确认，请回复：\n'
            '【PVP】结束活动 确认' % gameName
        )

    if not pvp.jfzStore.isRegistered():
        return pvp.jfzCore.replyText(plugin_event, '当前没有已注册的游戏，无需结束。')

    botHash = pvp.jfzCore.getBotHash(plugin_event)
    gameName = pvp.jfzStore.getGameName()
    gameTag = pvp.jfzCore.gameTag(gameName)

    # 1. 取消所有等待中、进行中的对局（PRD 4.10 第 1 条）
    #    走「时段结束」那条路径：不结算、血量维持、道具返还，
    #    对玩家来说是最不伤的一种收场。
    cancelQueue, cancelMatch = pvp.jfzEngine.cancelAllForWindowClose(botHash)

    # 2. 删数据
    pvp.jfzStore.wipeAllData()

    return pvp.jfzCore.replyText(
        plugin_event,
        '%s已结束，所有数据已删除。\n'
        '（取消排队 %d 条、对局 %d 场；后台群注册关系与管理员名单已清空，需重新注册。）' % (
            gameTag, cancelQueue, cancelMatch
        )
    )
