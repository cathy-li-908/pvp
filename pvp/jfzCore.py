# -*- encoding: utf-8 -*-
'''
公共层 —— 指令解析、权限判定、血量状态、时间窗、文本模板。

================================================================
【为什么单独抽一层】
================================================================
同一个判断在玩法里会被用到十几次：
  - 「这个人是不是骰主」—— 注册后台群、添加管理、结束活动都要查
  - 「现在是不是活动开放时段」—— 发起 PVP、排队出队、商城都要查
  - 「血量对应的状态」—— 发起校验、被发起校验、战斗锁都要查

散在各模块里会出现「同一条规则两处写得不一样」的经典事故
（比如发起时按 ≤20 判濒死、被发起时按 <20 判，导致 20 血的人状态自相矛盾）。
所以全部收敛到这里，只写一遍。

================================================================
【本插件的指令形态（PRD 第 1 节）】
================================================================
1. 自定义指令统一前缀 `【PVP】`，例如：
       【PVP】注册后台群 尽伏诛
       【PVP】购买 大还丹
       【PVP】使用道具 胜临符 2
2. 发起 PVP 用「游戏名头」的多行格式，游戏名由骰主注册时声明：
       【尽伏诛】PVP
       发起人：张三
       被发起人：李四
3. 掷骰指令保持青果原生 `.raN`，**不加** `【PVP】`：
       .ra50

================================================================
【权限模型（PRD 第 2 节）】
================================================================
本插件在主程序之外**不引入任何新的身份**，只认三种角色：
  骰主        → OlivaDiceCore 的骰主名单（.master 那套）
  后台管理员  → 骰主用 `【PVP】添加管理 QQ号` 加进本插件名单的人
  玩家        → `【PVP】注册玩家` 建档的人

特别注意 PRD 明确写的两条：
  - **没有「个人群管理员」这个角色**，所以本插件不看 QQ 群主/群管理员身份；
  - **没有「复活玩家」指令**，统一叫「修改血量」。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore

import re
import time
import datetime


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 所有自定义指令的统一前缀。PRD 第 1 节明确要求，不要改成别的。
CMD_PREFIX = '【PVP】'

# 骰主注册时声明的「当前游戏名」的兜底值。
# 只在「还没注册任何游戏」时用于拼提示文本，不参与任何业务判断。
DEFAULT_GAME_NAME = '尽伏诛'

# 血量规则（PRD 4.7.1）
# ★ 注意：这里**没有** `HP_MAX` 常量 ★
# 血量有**两个各自独立**的活动配置项，语义完全不同，不要互相代替：
#   initial_hp —— 新玩家注册那一刻的出生血量（`设置初始血量`）
#   hp_limit   —— 血量天花板：夹取上界、道具回血上界、显示里的分母（`设置血量上限`）
# 二者可以不同。典型用法：上限 500、初始 200 —— 新号从 200 起，
# 靠回血道具 / 后续奖励把血线往 500 上养。
# 所以任何需要「上限」的地方必须调 getHpLimit()，需要「出生血量」的地方
# 必须调 getInitialHp()，两边都不许写死数字，更不许拿一个当另一个用。
HP_INITIAL = 200         # 初始血量兜底值（`initial_hp` 未配置时用）
HP_LIMIT = 200           # 血量上限兜底值（`hp_limit` 未配置时用）
HP_DYING_THRESHOLD = 20  # 血量 ≤ 这个数 → 濒死；= 0 → 死皮
HP_LIMIT_MAX = 100000    # `设置初始血量` / `设置血量上限` 允许的最大值，防手滑多打几个 0

# 玩家状态（PRD 3.1）
STATUS_NORMAL = 'normal'  # 正常
STATUS_DYING = 'dying'    # 濒死
STATUS_DEAD = 'dead'      # 死皮

# 对局对外状态（PRD 3.2）—— 只有这三个，内部子阶段不算状态
MATCH_WAITING = 'waiting'    # 等待中
MATCH_ACTIVE = 'active'      # 进行中
MATCH_FINISHED = 'finished'  # 已结束

# 进行中对局的内部子阶段（不外露，只给插件逻辑用）
STAGE_DECLARE = 'declare'  # 道具声明子阶段
STAGE_FIGHT = 'fight'      # 掷骰子阶段
STAGE_SETTLE = 'settle'    # 结算子阶段

# 一局最多允许的回合数上限，防止 `【PVP】设置回合数 99999` 把机器人拖死
ROUNDS_MAX = 100

# 攻击值（每回合 `.raN` 的基础值）的允许范围（PRD 4.2）。
# ★ 上界 100 不是随手定的 ★ 青果 `.ra` 的判定骰值范围就是 1~100，
# `.ra110` 是非法指令、发出去什么都不会发生。所以这个上界不仅要在
# 「设置」时卡住，生成每回合计划时也必须夹一道 —— 基础值 100 叠加
# 胜临符 +10 就得到 110，玩家照着掷骰列表发指令会直接没反应。
ATTACK_MIN = 1
ATTACK_MAX = 100
ATTACK_DEFAULT = 50     # 全局默认攻击值（配置项 initial_attack 的兜底）

# 单次「修改血量」/「发放铜币」的数值上限，防止手滑多打几个 0
VALUE_MAX = 100000000


# ---------------------------------------------------------------------------
# 血量 → 状态
# ---------------------------------------------------------------------------

def deriveStatus(hp):
    """
    由血量推导玩家状态（PRD 3.1 / 4.7.1）。

    ★ 状态**不落盘**，永远是「读血量现算」★
    这条是刻意设计的：管理员用 `【PVP】修改血量 张三 150` 把人救回来时，
    如果状态是独立存的字段，就还得记得手动把 status 从 dead 改回 normal，
    漏改一次就会出现「血量 150 但依然是死皮、什么都干不了」的鬼故事。
    现算则永远不会不一致。

        hp > 20   → normal
        1 ~ 20    → dying（濒死）
        hp = 0    → dead（死皮）
    """
    try:
        hp = int(hp)
    except Exception:
        hp = 0
    if hp <= 0:
        return STATUS_DEAD
    if hp <= HP_DYING_THRESHOLD:
        return STATUS_DYING
    return STATUS_NORMAL


def getStatusName(status):
    """状态英文键 → 中文显示名。"""
    return {
        STATUS_NORMAL: '正常',
        STATUS_DYING: '濒死',
        STATUS_DEAD: '死皮',
    }.get(status, '未知')


def _readHpConfig(key, fallback):
    """
    读一个血量相关的配置整数，顺带做整数化与下限保护。

    读配置走**函数内** `import pvp`（不是模块级 import）—— pvp/__init__.py 会
    反过来 import jfzCore，模块级会成环。同文件 isMaster 里已有同样写法。
    导入失败（纯逻辑单测的早期阶段 jfzStore 还没就绪）就退回 fallback。
    """
    value = fallback
    try:
        import pvp

        value = pvp.jfzStore.getConfigInt(key, fallback)
    except Exception:
        return fallback
    try:
        value = int(value)
    except Exception:
        return fallback
    if value < 1:
        return fallback
    return value


def getHpLimit():
    """
    当前血量上限（活动配置 `hp_limit`）：夹取上界、道具回血上界、显示里的分母。

    【上限和初始血量是两件事】
    初始血量（`initial_hp`）只说「新玩家注册时拿到多少血」，
    上限（`hp_limit`）说的是「这个人的血条最长能有多长」。拆成两个配置项之后，
    活动可以给玩家留成长空间：上限 500、初始 200，新号从 200 起，
    靠回血道具 / 奖励把血线往 500 上养。
    两边都别写死：写死 200 的地方在骰主改了配置之后就会和真实上界对不上，
    典型症状是「道具明明回了血，血条却不动了」（其实是被 clampHp 夹住）。

    ★ 这里返回的是**当前时刻**的上限，会随 `设置血量上限` 即时变化 ★
    所以不要在模块加载时把它缓存进某个全局变量，每次都要现调。
    """
    return _readHpConfig('hp_limit', HP_LIMIT)


def getInitialHp():
    """
    新玩家注册时的出生血量（活动配置 `initial_hp`）。

    ★ 出生血量不会超过血量上限 ★ —— 配得比上限大时按上限算。
    这是有意的：血量是玩家的持久属性，出生值本身就该是一个合法血量。
    如果不夹，「初始血量 500 / 上限 200」就会造出一个出生即超上限的存档，
    然后在这条档案第一次落盘时被 `_packPlayer` 悄悄削回 200 ——
    骰主设了 500、看到的却是 200，而且找不到是哪一步吃掉的。

    夹取放在这里（而不是放在 createPlayer）是因为「新玩家有多少血」只有这一个
    出口：注册、`名单`、`配置` 都从这里读，夹在一处就不会出现
    「注册回执照 500 显示、实际存档 200」这种两处数字打架的情况。
    """
    value = _readHpConfig('initial_hp', HP_INITIAL)
    limit = getHpLimit()
    return value if value < limit else limit


def clampHp(hp):
    """把血量夹到 [0, 当前血量上限] 区间。"""
    try:
        hp = int(hp)
    except Exception:
        hp = 0
    if hp < 0:
        return 0
    limit = getHpLimit()
    if hp > limit:
        return limit
    return hp


# ---------------------------------------------------------------------------
# 消息清理与解析
# ---------------------------------------------------------------------------

def getGroupId(plugin_event):
    """取当前群 ID（字符串）。私聊事件没有 group_id，返回空串。"""
    value = getattr(plugin_event.data, 'group_id', None)
    return '' if value is None else str(value)


def getUserId(plugin_event):
    """取发送者 ID（字符串）。"""
    value = getattr(plugin_event.data, 'user_id', None)
    return '' if value is None else str(value)


def getBotHash(plugin_event):
    """取机器人账号哈希（青果用它区分不同 Bot 实例的数据目录）。"""
    return str(plugin_event.bot_info.hash)


def getPlatform(plugin_event):
    """取平台名（如 'qq'）。"""
    try:
        return str(plugin_event.platform['platform'])
    except Exception:
        return ''


def collectBotIdList(plugin_event):
    """
    收集「机器人自己」的所有 ID，返回集合。

    青果/OneBot 下机器人可能有多个身份：
      base_info['self_id']        账号 ID
      extend['sub_self_id']       副账号 ID
      extend['sub_self_open_id']  ★ 机器人在「本群内」的 openid
    群里 @机器人 时用的可能是第三个，所以三个都要认，否则 @ 判定会漏。
    """
    botIds = set()

    baseInfo = getattr(plugin_event, 'base_info', None)
    if isinstance(baseInfo, dict) and baseInfo.get('self_id') is not None:
        botIds.add(str(baseInfo['self_id']))

    botInfo = getattr(plugin_event, 'bot_info', None)
    if botInfo is not None and getattr(botInfo, 'id', None) is not None:
        botIds.add(str(botInfo.id))

    extend = getattr(plugin_event.data, 'extend', None)
    if isinstance(extend, dict):
        for key in ['sub_self_id', 'sub_self_open_id']:
            value = extend.get(key, None)
            if value is not None:
                botIds.add(str(value))

    return botIds


def extractAtUserIds(message):
    """
    把消息里所有 @ 的 QQ 号 / openid 抽出来。

    本插件配的是 old_string 模式，实际是 CQ 码：`[CQ:at,qq=123]`。
    同时兼容 `[OP:at,id=123]`，万一以后改 message_mode 不用回来动这里。
    """
    if not message:
        return []
    pattern = re.compile(r'\[(?:CQ|OP):at,(?:qq|id)=([^,\]]+)(?:,[^\]]*)?\]')
    return [m.group(1).strip() for m in pattern.finditer(str(message))]


def stripLeadingSegments(message):
    """
    剥掉消息开头的「引用段」和「@机器人 段」，露出真正的指令正文。

    只剥**开头连续**的引用/at/空白；遇到真正的正文就停，否则会把内容吃掉。
    典型场景：玩家先 @ 一下骰子再写指令，`@骰子 【PVP】背包`。
    """
    text = str(message or '')

    while True:
        match = re.match(r'^\s*\[(?:CQ|OP):reply,[^\]]*\]', text)
        if not match:
            break
        text = text[match.end():]

    while True:
        match = re.match(r'^\s*\[(?:CQ|OP):at,[^\]]*\]', text)
        if not match:
            break
        text = text[match.end():]

    return text.strip()


def cleanName(text):
    """
    清理用户填的名称：去首尾空白、引号、书名号，以及开头的 @。

    管理员经常把「「张三」」「@张三」连着标点一起敲进来。
    """
    result = str(text or '').strip()
    result = re.sub(r'^[\s"\'“”「」【】]+', '', result)
    result = re.sub(r'[\s"\'“”「」【】]+$', '', result)
    result = result.lstrip('@')
    return result.strip()


def isIntText(text):
    """判断一段文本是不是整数（允许正负号）。"""
    return bool(re.match(r'^[+-]?\d+$', str(text or '').strip()))


def toInt(text, default=None):
    """宽松转 int，失败返回 default。"""
    try:
        return int(str(text).strip())
    except Exception:
        return default


def parseYmd(text):
    """
    把 '2026-09-21' / '20260921' 解析成 (是否合法, 标准化 'YYYY-MM-DD')。
    """
    value = str(text or '').strip().replace('/', '-').replace('.', '-')
    value = re.sub(r'^(\d{4})(\d{2})(\d{2})$', r'\1-\2-\3', value)
    match = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', value)
    if not match:
        return False, ''
    try:
        parsed = datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return False, ''
    return True, parsed.strftime('%Y-%m-%d')


def parseTimeText(text):
    """
    把 '14:00' 解析成 (是否合法, 'HH:MM')。支持全角冒号。不合法返回 (False, '')。
    """
    value = str(text or '').strip().replace('：', ':')
    match = re.match(r'^(\d{1,2}):(\d{1,2})$', value)
    if not match:
        return False, ''
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return False, ''
    return True, '%02d:%02d' % (hour, minute)


def splitArgs(text):
    """按空白拆参数。全角空格也认（手机输入法常打出来）。"""
    return re.split(r'[\s\u3000]+', str(text or '').strip())


# ---------------------------------------------------------------------------
# 活动日期的宽松解析
# ---------------------------------------------------------------------------

# 「设置活动日期」的格式示例。★ 全插件唯一一份 ★
# 用户可见的地方（用法报错、设置成功的回执、注册后台群的引导、【PVP】管理清单、
# 帮助文档）全部引用这里。不要在别处再手写一遍：格式改过一次就会发现，
# 手抄的示例散落在五六个文件里，改漏一个就是「文档和实现说的不一样」。
ACTIVITY_DATE_ARGS_EXAMPLE = '活动周期：2026-09-01 ~ 2026-09-02 每日开放：14:00 ~ 18:00'

ACTIVITY_DATE_EXAMPLE = '【PVP】设置活动日期 ' + ACTIVITY_DATE_ARGS_EXAMPLE

ACTIVITY_DATE_USAGE = (
    '用法：%s\n'
    '说明：「活动周期」是活动从哪天到哪天，「每日开放」是每天几点到几点。\n'
    '波浪线前后的空格写不写都行；标签省略、全角冒号、中文「至 / 到」也都认。'
    % ACTIVITY_DATE_EXAMPLE
)

# 【为什么这里不按位置切 4 个词元】
# 用户可见的格式是带标签的：活动周期：… ~ … 每日开放：… ~ …
# 但用户实际打出来的样子千奇百怪：波浪线两边没空格（2026-09-01~2026-09-02）、
# 把「每日开放」写在前面、把标签整个省掉只写 4 个数、用中文「至」当波浪线……
# 按位置硬切的话，上面每一种都会被判「格式不对」，而用户只是照我们给的示例换了个写法。
# 所以改成**直接认日期和时间**：全文扫出「日期样」「时间样」的片段，各按出现顺序取前两个。
# 「活动周期」与「每日开放」谁前谁后就自然无所谓了，分隔符也可以随便写。

# 日期样：2026-09-01 / 2026/9/1 / 2026.09.01 / 20260901
_ACTIVITY_DATE_RE = re.compile(r'\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{8}')
# 时间样：14:00 / 14：00（全角冒号）
_ACTIVITY_TIME_RE = re.compile(r'\d{1,2}[:：]\d{1,2}')


def parseActivityDateText(text):
    """
    从「设置活动日期」的参数里认出 4 个值。

    返回 (开始日期, 结束日期, 每日开始, 每日结束)——**都是原始文本**，
    格式校验由调用方用 parseYmd / parseTimeText 做；
    认不出来的位置是空串，这样调用方可以说清「缺的是哪一段」。

    只返回原始文本、不在这里校验，是为了让报错文案能指到具体那一项
    （「结束日期格式不对：xxx」比「格式不对，请重看用法」有用得多）。
    """
    raw = str(text or '')
    dates = _ACTIVITY_DATE_RE.findall(raw)
    times = _ACTIVITY_TIME_RE.findall(raw)
    return (
        dates[0] if len(dates) > 0 else '',
        dates[1] if len(dates) > 1 else '',
        times[0] if len(times) > 0 else '',
        times[1] if len(times) > 1 else '',
    )


# ---------------------------------------------------------------------------
# 指令解析
# ---------------------------------------------------------------------------

def parseCommand(message):
    """
    解析 `【PVP】xxx yyy` 形式的指令。

    返回 (指令正文, 剩余参数文本)；不是本插件指令时返回 (None, 原文)。

    ★ 只认 `【PVP】` 这一个前缀 ★
    发起 PVP 的 `【游戏名】PVP` 格式由 parseChallangeHeader 单独处理，
    不走这里 —— 因为游戏名是玩家数据（骰主可换），不能写死成常量。
    """
    text = stripLeadingSegments(message)
    match = re.match(r'^【PVP】\s*([\s\S]*)$', text)
    if not match:
        return None, text
    body = match.group(1).strip()
    if not body:
        # 光发一个【PVP】，当成「求帮助」
        return '帮助', ''
    parts = splitArgs(body)
    return parts[0], ' '.join(parts[1:])


def parseChallengeHeader(message, gameName):
    """
    解析发起 PVP 的多行格式（PRD 4.3.1）：

        【尽伏诛】PVP
        发起人：张三
        被发起人：李四

    返回 {'发起人': '张三', '被发起人': '李四'}；不匹配时返回 None。

    【为什么要传 gameName 进来而不是写死】
    PRD 第 1 节：`尽伏诛` 只是当前游戏名，后续可更换，
    消息模板里的 `【尽伏诛】` 要自动替换成当前游戏名。
    所以这里用「当前游戏名」去匹配，而不是匹配字面量。
    大小写不敏感（PPV/pvp 都认），冒号中英文都认。
    """
    if not message:
        return None
    text = stripLeadingSegments(message)

    escaped = re.escape(str(gameName or '').strip())
    if not escaped:
        return None

    match = re.match(r'^【%s】\s*[Pp][Vv][Pp]\s*([\s\S]*)$' % escaped, text)
    if not match:
        return None

    tail = match.group(1)
    fields = parseMultiLineFields(tail, ['发起人', '被发起人'])

    challenger = cleanName(fields.get('发起人', ''))
    target = cleanName(fields.get('被发起人', ''))
    if not challenger or not target:
        return None
    return {'发起人': challenger, '被发起人': target}


def parseMultiLineFields(text, knownKeys):
    """
    解析「一行一个字段」的文本，返回 {字段名: 值}。

        text      = '发起人：张三\\n被发起人：李四'
        knownKeys = ['发起人', '被发起人']
        → {'发起人': '张三', '被发起人': '李四'}

    【容错】
      - 冒号中英文都认（：和 :）
      - 「发起人：张三 被发起人：李四」写在**同一行**也能切开
        （做法是「值一直取到下一个已知字段名或文本结尾为止」）

    ★ knownKeys 必须列全 ★
    漏一个字段名，那个字段的值就会被并进上一个字段里。
    用法是每个调用点传自己的字段清单，避免全局清单越长越容易漏。
    """
    result = {}
    if not text:
        return result

    keys = [key for key in knownKeys if key]
    if not keys:
        return result

    keyPattern = '|'.join(sorted(keys, key=len, reverse=True))
    pattern = re.compile(
        r'(%s)\s*[：:]\s*([\s\S]*?)(?=\n?\s*(?:%s)\s*[：:]|$)' % (keyPattern, keyPattern)
    )
    for match in pattern.finditer(str(text)):
        key = match.group(1).strip()
        value = match.group(2).strip()
        # 只取第一次出现的值：重复填写时以第一个为准，避免后面把前面的覆盖掉
        if key not in result:
            result[key] = value

    # 同一行连写时，值里可能混进换行，统一收一下
    for key in list(result.keys()):
        result[key] = result[key].strip()
    return result


def isRaCommand(commandText):
    """
    判断一段文本是不是青果的 `.ra` 检定指令。

    【为什么要先剥前缀】
    青果识别指令时会先用 `msgIsCommand` 剥掉一个前缀字符（`.` `。` `/`），
    剥完之后的文本才拿去和 'ra' / 'rc' 比对。
    如果我们直接拿带前缀的 `.ra50` 去比对 'ra'，是**永远比不中**的
    —— 这个坑很隐蔽，表现为「钩子永远不触发、掷骰永远不计分」。

    所以这里完全复刻青果的两步：
      1. msgIsCommand 剥前缀（顺带告诉我们「这确实是一条指令」）
      2. isMatchWordStart 比对 ra / rc

    第 1 步返回的 flag 必须为真才继续 —— 否则玩家发一句
    「ra 是什么」也会被当成掷骰指令。
    """
    if not commandText:
        return False
    try:
        stripped, flagIsCommand = OlivaDiceCore.msgReply.msgIsCommand(
            str(commandText), OlivaDiceCore.crossHook.dictHookList['prefix']
        )
        if not flagIsCommand:
            return False
        return OlivaDiceCore.msgReply.isMatchWordStart(stripped, ['ra', 'rc'], isCommand=True)
    except Exception:
        return False


def parseRaSkillValue(commandText):
    """
    从掷骰指令文本里抠出「玩家自己填的技能值」。

        .ra50      → 50
        .ra 50     → 50
        .ra攻击50   → 50
        .ra体质     → None（没写数字，无法判断）

    返回 int 或 None。

    【这是干什么用的】
    骰子钩子读到的是「用户配置里最近一次掷骰」的记录，它**可能已经过期**——
    比如玩家发了 `.ra50`，但青果因为某种原因没有真正掷骰（配了禁用、被别的插件拦了），
    那这次钩子读到的是上一次的旧记录。
    拿玩家**自己敲的数字**和记录里的技能值对一下，不一致就说明记录是旧的，
    直接忽略，避免把旧结果当成本次结果计分（那是直接改错血的事故）。
    """
    if not commandText:
        return None
    try:
        stripped, flagIsCommand = OlivaDiceCore.msgReply.msgIsCommand(
            str(commandText), OlivaDiceCore.crossHook.dictHookList['prefix']
        )
    except Exception:
        return None
    if not flagIsCommand:
        return None
    numbers = re.findall(r'\d+', stripped)
    if not numbers:
        return None
    return int(numbers[-1])


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------

def isMaster(plugin_event, userId=None):
    """
    是不是骰主（PRD 2.1）。

    PRD 原文：「骰主 = OlivaDiceCore 最高权限持有者」。
    所以这里**不去读本插件自己的任何配置**，直接问 OlivaDiceCore 的骰主名单。
    好处是骰主名单是全局唯一的，用户用 `.master +@某人` 加的人立刻生效，
    不需要来本插件再登记一遍。

    另外保留一个「本插件配置里的骰主 ID」作为兜底（配置键 master_id）：
    有些环境（QQ 官方机器人）下 openid 和骰主名单里存的 userHash 对不上，
    这时骰主会被误判成普通玩家，连注册后台群都做不了。

    ★ 这个兜底只能靠改配置文件设置 ★
      看起来该配一条「设置骰主ID」的指令，但那条指令本身就要先过骰主校验 ——
      在骰主校验已经失效的环境里，谁来执行它？死循环。
      所以正确的做法是留一个不依赖任何权限的入口：直接编辑
      plugin/data/pvp/配置.json，把 master_id 填成自己的 QQ 号 / userid。
      详见交付文档 README 的「骰主判定失效怎么办」。
    """
    if userId is None:
        userId = getUserId(plugin_event)
    userId = str(userId)

    # 兜底 1：配置里手填的骰主 ID
    try:
        import pvp

        masterId = str(pvp.jfzStore.getConfig('master_id', '') or '').strip()
        if masterId and userId == masterId:
            return True
    except Exception:
        pass

    # 兜底 2：OlivaDiceCore 的骰主名单
    try:
        userHash = OlivaDiceCore.userConfig.getUserHash(
            userId=userId, userType='user', platform=getPlatform(plugin_event)
        )
        if OlivaDiceCore.ordinaryInviteManager.isInMasterList(getBotHash(plugin_event), userHash):
            return True
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------

def getNow():
    """
    取当前时间（固定 UTC+8）。

    【为什么写死 +8 而不是用本机时区】
    活动时段（每天 14:00-18:00）是给中国用户看的，必须按东八区判。
    如果机器人跑在海外 VPS（时区 UTC 或别的），用 datetime.now() 会
    整整错 8 小时 —— 表现为「活动时间明明到了却说未开放」这种很难查的问题。
    写死偏移就与宿主环境无关了。
    """
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    return now


def getTodayKey(now=None):
    """取今天的日期键 'YYYY-MM-DD'，用于「每日次数」的跨天重置。"""
    if now is None:
        now = getNow()
    return now.strftime('%Y-%m-%d')


def getNowTimeText(now=None):
    """取当前的 'HH:MM' 文本。"""
    if now is None:
        now = getNow()
    return now.strftime('%H:%M')


def parseStamp(text):
    """
    把 '%Y-%m-%d %H:%M:%S' 时间戳解析回带时区（UTC+8）的 datetime。

    【为什么时区要手动补】strftime 出来的是**不带时区**的字符串，
    直接 strptime 得到的是 naive datetime；拿它和 getNow()（aware）相减
    会抛 TypeError。存的时候用的就是东八区墙上时间（见 getNow 的说明），
    所以这里原样补回 +8 就能对上。

    解析不了（空串 / 旧版本数据没这个字段 / 格式变了）返回 None，
    调用方把「算不出时间」当成「不超时」处理，宁可不动也不要误杀对局。
    """
    try:
        parsed = datetime.datetime.strptime(str(text or '').strip(), '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return None
    return parsed.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))


def isInActivityWindow(config, now=None):
    """
    判断「现在是否处于活动开放时段」（PRD 4.2）。

    返回 (是否开放, 说明文本)。说明文本用于给玩家解释为什么不能打。

    判定顺序（先粗后细，每一步给出更具体的原因）：
      1. 还没配置活动日期        → 不开放
      2. 今天在活动总周期之外    → 不开放（还没开始 / 已经结束）
      3. 今天的当前时刻不在时段内 → 不开放（今天还没到点 / 今天已经收工）
    """
    if not isinstance(config, dict):
        return False, '尚未配置活动，请等待管理配置。'

    startDate = str(config.get('start_date', '') or '').strip()
    endDate = str(config.get('end_date', '') or '').strip()
    dailyStart = str(config.get('daily_start_time', '') or '').strip()
    dailyEnd = str(config.get('daily_end_time', '') or '').strip()

    if not startDate or not endDate or not dailyStart or not dailyEnd:
        return False, '尚未配置活动时间，请等待管理配置。'

    if now is None:
        now = getNow()
    today = now.strftime('%Y-%m-%d')

    if today < startDate:
        return False, '活动尚未开始（活动周期：%s ~ %s）。' % (startDate, endDate)
    if today > endDate:
        return False, '活动已经结束（活动周期：%s ~ %s）。' % (startDate, endDate)

    nowText = now.strftime('%H:%M')
    if nowText < dailyStart:
        return False, '今天尚未开放（每日开放时段：%s ~ %s）。' % (dailyStart, dailyEnd)
    if nowText >= dailyEnd:
        return False, '今天已收工（每日开放时段：%s ~ %s）。' % (dailyStart, dailyEnd)

    return True, ''


def isActivityDateOver(config, now=None):
    """
    判断「整个活动的日期周期是否已经走完」（PRD 4.2 末尾）。

    走完之后活动整体关闭：不能再发起任何对局，只能由骰主执行
    `【PVP】结束活动 确认` 来删除数据。
    """
    if not isinstance(config, dict):
        return True
    endDate = str(config.get('end_date', '') or '').strip()
    if not endDate:
        return True
    if now is None:
        now = getNow()
    return now.strftime('%Y-%m-%d') > endDate


def dateRangeText(startDate, endDate):
    """把活动周期渲染成 '2026-09-21 ~ 2026-09-23（共 3 天）'。"""
    text = '%s ~ %s' % (startDate, endDate)
    try:
        start = datetime.datetime.strptime(startDate, '%Y-%m-%d')
        end = datetime.datetime.strptime(endDate, '%Y-%m-%d')
        days = (end - start).days + 1
        if days > 0:
            text += '（共 %d 天）' % days
    except Exception:
        pass
    return text


# ---------------------------------------------------------------------------
# 回复
# ---------------------------------------------------------------------------

def replyText(plugin_event, text):
    """
    回一条消息给**当前对话**。总是返回 True。

    走 OlivaDiceCore.msgReply.replyMsg，这样：
      - 长文本按青果自己的配置分页
      - 复用青果的主动/被动消息逻辑（被动回复优先，不会白掉一条）
      - 能挂上 OlivaDiceCore 的敏感词过滤等钩子

    ★★ 关于返回值的约定（很重要，踩过坑）★★
    本函数**永远返回 True**，是刻意设计的。
    因为插件里到处是这种写法：

        if _handleBackpack(...):     # 处理函数内部写 return replyText(...)
            return

    如果 replyText 返回 None，那 `return replyText(...)` 就等于 `return None`，
    外层判为假，代码会**继续往下走再执行一遍发送流程** ——
    表现是「一条指令回了两条消息」，或者「查背包被当成买东西处理」。

    统一返回 True 之后，处理函数只要写 `return replyText(...)`
    就自然表示「我回复过了、这条指令到此为止」。
    """
    if text is not None and str(text) != '':
        try:
            OlivaDiceCore.msgReply.replyMsg(plugin_event, str(text))
        except Exception:
            import traceback

            print('[pvp] 回复失败，退回原生 reply()：')
            traceback.print_exc()
            try:
                plugin_event.reply(str(text))
            except Exception:
                pass
    return True


def handled(func):
    """
    标记「这是一个指令处理函数」：返回 None 时自动当作 True。

    【为什么需要它】
    插件里到处是 `if _handleXxx(...): return` 的写法。处理函数如果
    正常跑完却忘了写 return，就返回 None（假值），外层以为「没人处理」，
    接着又执行一遍别的分支 —— 表现为「一条指令回两条消息」，
    或者「管理指令被当成玩家指令处理」。

    人肉在每个分支末尾补 `return True` 极其容易漏，所以用装饰器统一兜底。

    注意：它**不会**把显式的 `return False` 变成 True ——
    显式 False 是「我没处理，请继续往下走」的意思，必须保留。
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        return True if result is None else result

    return wrapper


# ---------------------------------------------------------------------------
# 伤害表（PRD 4.6）
# ---------------------------------------------------------------------------
# 青果判定出来的成功等级有 6 种，这里按 PRD 4.6.1 的默认值建成一张映射表。
# 键用**中文等级名**而不是 resultType 枚举，是为了：
#   1. 用户能用 `【PVP】设置伤害表 大成功=20 ...` 直接改（见 jfzAdmin）；
#   2. 落盘进 JSON 时人眼可读，出问题时能直接看配置文件核对。
# jfzDice 里会把枚举转成中文名再来查这张表。
LEVEL_ORDER = ['大成功', '极难成功', '困难成功', '成功', '失败', '大失败']

DAMAGE_TABLE_DEFAULT = {
    '大成功': 20,
    '极难成功': 15,
    '困难成功': 10,
    '成功': 5,
    '失败': 0,
    '大失败': 0,
}

# 「大成功 / 极难成功 / 困难成功」算成功，结算时战报里要单独标出来。
LEVEL_SUCCESS_SET = set(['大成功', '极难成功', '困难成功', '成功'])


# ---------------------------------------------------------------------------
# 道具表（PRD 4.8.2）
# ---------------------------------------------------------------------------
# 这一张表是全插件对道具的**唯一口径**：商城按它列出商品、
# 声明阶段按它校验用法、引擎按它算 `.raN` 修正、管理指令按它校验道具名。
#
# type 三种取值对应 PRD 4.8.2 的三类：
#   场外型 —— 不在 PVP 中时使用，立即生效
#   全场型 —— 开战后、掷骰前声明，整场取消
#   回合型 —— 开战后、掷骰前声明，只改某个回合的 `.raN`
#
# price 是**默认售价**，可以在游戏里用 `【PVP】设置道具价格 道具名 N` 改
# （价格落盘在配置里；改价不会动已售出的道具）。
ITEM_TABLE = {
    '大还丹': {
        'type': '场外型',
        'price': 300,
        'effect': '回复 100 血量',
        'heal': 100,
    },
    '小还丹': {
        'type': '场外型',
        'price': 150,
        'effect': '回复 50 血量',
        'heal': 50,
    },
    '解毒丸': {
        'type': '场外型',
        'price': 120,
        'effect': '解除中毒状态（预留，当前版本无中毒玩法）',
        'heal': 0,
    },
    '幻息丹': {
        'type': '场外型',
        'price': 200,
        'effect': '效果待定（当前版本无实际效果）',
        'heal': 0,
    },
    '遁隐符': {
        'type': '全场型',
        'price': 800,
        'effect': '整场取消，血量维持，背包不动，对局立即结束',
    },
    '胜临符': {
        'type': '回合型',
        'price': 400,
        'effect': '指定回合自身 .raN +10',
        'self_mod': 10,
    },
    '错影符': {
        'type': '回合型',
        'price': 400,
        'effect': '指定回合对方 .raN -10',
        'enemy_mod': -10,
    },
}

# 道具在商城/背包里的展示顺序：先场外、再全场、再回合，同类按表内顺序。
ITEM_ORDER = ['大还丹', '小还丹', '解毒丸', '幻息丹', '遁隐符', '胜临符', '错影符']

TYPE_OFF_FIELD = '场外型'
TYPE_WHOLE_MATCH = '全场型'
TYPE_ROUND = '回合型'


def getItemDef(itemName):
    """取道具定义。不认识的返回 None。"""
    return ITEM_TABLE.get(str(itemName or '').strip())


def isItemName(itemName):
    """判断是不是本插件已知的道具名。"""
    return getItemDef(itemName) is not None


def getItemType(itemName):
    """取道具类型（'场外型' / '全场型' / '回合型'）。不认识返回空串。"""
    itemDef = getItemDef(itemName)
    if itemDef is None:
        return ''
    return str(itemDef.get('type', ''))


# ---------------------------------------------------------------------------
# 玩家上下文解析
# ---------------------------------------------------------------------------

def resolvePlayer(plugin_event):
    """
    找出「当前说话的人」对应的玩家档案。返回一个 dict：

        {
          'ok':            True/False
          'reason':        ok=False 时的人话原因
          'name':          玩家名（可能为空串）
          'player':        玩家档案 dict（可能为 None）
          'inPersonalGroup': 这条消息是不是发在他自己的个人群里
        }

    【为什么要分两步查】
    本插件的玩法都在「个人群」里，所以正常路径是「用群号反查玩家」。
    但有一类指令允许在**别的群**里发 —— PRD 4.1.3 与 5.2 说明管理员的
    「注册玩家 / 发放铜币 / 修改血量」既能在后台群发、也能在个人群发。
    所以第二步退化成「按 QQ 查玩家」。
    """
    groupId = getGroupId(plugin_event)
    userId = getUserId(plugin_event)

    if not groupId:
        return {'ok': False, 'reason': '该指令只能在群里使用。', 'name': '', 'player': None,
                'inPersonalGroup': False}
    if not userId:
        return {'ok': False, 'reason': '取不到发送者身份。', 'name': '', 'player': None,
                'inPersonalGroup': False}

    # 延迟 import：jfzStore 在模块顶层依赖 jfzCore，这里再 import 会形成循环
    import pvp

    player = pvp.jfzStore.getPlayerByGroup(groupId)
    if player:
        ownerQQ = str(player.get('user_qq', '') or '')
        if ownerQQ == userId:
            return {'ok': True, 'reason': '', 'name': str(player.get('display_name', '')),
                    'player': player, 'inPersonalGroup': True}
        return {
            'ok': False,
            'reason': '这个群登记的是玩家「%s」的个人群，只有本人能在这里用指令。' % player.get('display_name', ''),
            'name': str(player.get('display_name', '')),
            'player': player,
            'inPersonalGroup': True,
        }

    player = pvp.jfzStore.getPlayerByQQ(userId)
    if player:
        return {'ok': True, 'reason': '', 'name': str(player.get('display_name', '')),
                'player': player, 'inPersonalGroup': False}

    return {'ok': False, 'reason': '你还没有注册为玩家，请联系管理执行「【PVP】注册玩家」。',
            'name': '', 'player': None, 'inPersonalGroup': False}


# ---------------------------------------------------------------------------
# 文本模板
# ---------------------------------------------------------------------------

def gameTag(gameName):
    """
    生成消息里那个方括号标签：`【尽伏诛】`。

    PRD 第 1 节要求所有模板里的 `【尽伏诛】` 自动替换为当前游戏名，
    所以全插件不用字面量拼这个标签，一律走这里。
    """
    return '【%s】' % (str(gameName or '').strip() or DEFAULT_GAME_NAME)


def formatDuration(seconds):
    """把秒数渲染成 '1 分 30 秒' 这种给人看的文本。"""
    try:
        seconds = int(seconds)
    except Exception:
        return '未知'
    if seconds < 60:
        return '%d 秒' % seconds
    if seconds < 3600:
        return '%d 分 %d 秒' % (seconds // 60, seconds % 60)
    return '%d 小时 %d 分' % (seconds // 3600, (seconds % 3600) // 60)


def timestampNow():
    """取当前 Unix 时间戳（秒，浮点）。"""
    return time.time()
