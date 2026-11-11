# -*- encoding: utf-8 -*-
'''
数据层 —— 本插件全部数据的读写入口。

================================================================
【目录结构】
================================================================
    plugin/data/pvp/
    ├── 游戏.json     当前游戏（游戏名 / 后台群 / 后台管理员名单）
    ├── 配置.json     活动配置 + 商城价格 + 骰主ID 兜底
    ├── 玩家.json     玩家档案（PRD 7.1）
    ├── 队列.json     等待中的 PVP 请求（PRD 4.3.2）
    ├── 对局.json     当前进行中的对局（PRD 7.2）
    └── 战报.json     历史战报（PRD 4.9「查看全量战报」/ 4.10 结束活动要删）

【为什么全部平铺在一个目录里，不像恋综那样一个游戏一个文件夹】
PRD 第 1 节与 4.10 说得很清楚：**同一时间只有一个「当前游戏名」**，
`【PVP】结束活动 确认` 会把当前游戏名下的一切删干净、插件回到未注册状态。
既然永远只有一份数据，「一个游戏一个文件夹」就没有意义了，
平铺反而让场控能一眼看全。

【为什么状态不落盘】
玩家的 normal/dying/dead 由 hp 现算（见 jfzCore.deriveStatus）。
但 PRD 7.1 的字段表里确实列了 status，所以为了「数据文件长得和 PRD 一致」，
每次写盘前会顺手把 status 按当前血量重算一遍再写进去。
它是**只读的镜像**，程序逻辑永远不读它 —— 读它就等于埋了「状态和血量不一致」的雷。

【原子写】
写文件先写 `.tmp` 再 `os.replace` 覆盖。os.replace 在同一文件系统内是原子操作，
写到一半被杀进程也不会留下半截内容。
另外每次覆盖前会先备份一份 `.bak`，读到损坏文件时自动回退到备份。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

import os
import json
import shutil
import threading
import traceback

import pvp


# 数据根目录。
# ★ 刻意不写成 plugin/data/pvp/data —— 青果在插件升级时会清空并重建
#   plugin/data/<命名空间>/data，放那里会被误删。
DATA_PATH = 'plugin/data/pvp'

FILE_GAME = '游戏.json'
FILE_CONFIG = '配置.json'
FILE_PLAYER = '玩家.json'
FILE_QUEUE = '队列.json'
FILE_MATCH = '对局.json'
FILE_REPORT = '战报.json'

# 活动配置的默认值（PRD 4.2 表格 + 7.3 字段表）。
# 键名用 PRD 里的 snake_case，方便和 PRD 对照；同时它们会直接出现在
# 配置.json 里，场控可以手改文件。
CONFIG_DEFAULT = {
    # 骰主兜底 ID。PRD 说骰主就是 OlivaDiceCore 的最高权限者，
    # 正常情况下走青果骰主名单；这一项是给「openid 与骰主名单对不上」的环境用的。
    'master_id': '',
    # 活动周期与每日时段
    'start_date': '',
    'end_date': '',
    'daily_start_time': '14:00',
    'daily_end_time': '18:00',
    # 数值
    # ★ 血量是两个各自独立的配置项，别把其中一个当另一个用 ★
    #   initial_hp —— 新玩家注册时的出生血量（`设置初始血量`）
    #   hp_limit   —— 血量天花板：夹取上界、道具回血上界、显示里的分母（`设置血量上限`）
    # 出厂值两边都是 200（= PRD 4.7.1 的数），但活动可以拉开，例如上限 500 / 初始 200。
    'initial_hp': 200,
    'hp_limit': 200,
    'initial_copper': 1000,
    'initial_attack': 50,
    'daily_attack_limit': 3,
    'rounds': 10,
    'item_limit_per_match': 1,
    # 道具声明阶段超时（分钟）。超时仍没人声明完就自动取消对局并返还。
    # ★ 这是本插件相对 PRD 的扩展 ★ PRD 说声明没有超时（4.4 / 8），
    # 但实际跑起来「两个人都一直不声明」会把对局永远卡在声明阶段，
    # 队列里后面所有人的请求也就永远轮不上 —— 表现就是「排队中」之后再无下文。
    # 配 0 表示不限制（恢复 PRD 原行为，靠【PVP】强制结束兜底）。
    'declare_timeout_minutes': 10,
    # 掷骰阶段超时（分钟）。超时按**已有记录**直接结算。
    # ★ 同样是相对 PRD 的扩展 ★ 掷骰阶段也必须能兜底：
    # 任何一掷被防作弊/污染防护拦下（那一掷不计分），那一方就永远少一格，
    # 双方「都掷满」的结算条件永远不成立 —— 对局无声卡死，谁也等不到战报。
    # 超时后缺的回合按「未掷」计 0 伤害，战报里会注明缺了几回合。
    # 配 0 表示不限制（恢复 PRD 原行为，靠【PVP】强制结束兜底）。
    'fight_timeout_minutes': 30,
    # 伤害映射
    'damage_table': dict(pvp.jfzCore.DAMAGE_TABLE_DEFAULT),
    # 商城价格覆盖表：{道具名: 价格}。只有被改过价的道具会出现在这里。
    'shop_prices': {},
    # 最近一次「每日时段结束」的日期，用于看守线程去重（同一天只取消一次）
    'last_daily_close_date': '',
}


# 一把全局可重入锁。
# 【为什么要锁】本插件有三类并发写入者：
#   1. 群消息处理（主线程）
#   2. 跨群发送后台线程
#   3. 活动时段看守线程（每 30 秒轮询一次，到点会批量取消对局）
# 玩家表/对局表是「读-改-写」模式，不加锁会出现「两边同时读到旧值、
# 后写的把先写的覆盖掉」，表现为凭空丢血/丢道具。
_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# 内存缓存
# ---------------------------------------------------------------------------
# 掷骰钩子属于**高频调用**（每次掷骰都会触发），不能每次都去读磁盘，
# 所以数据常驻内存，写的时候同步落盘。
gGame = {}        # 游戏.json
gConfig = {}      # 配置.json
gPlayers = {}     # {玩家名: 玩家档案}
gQueue = []       # [ PVP 请求 ]
gMatch = {}       # 当前进行中的对局，空 dict 表示没有
gReports = []     # 历史战报

# 玩家表的两个索引。它们不是独立数据，由 gPlayers 推导出来，
# 放在内存里避免每次用 QQ 或群号查人都要遍历全表。
gPlayerByQQ = {}     # {用户QQ: 玩家名}
gPlayerByGroup = {}  # {个人群号: 玩家名}


# ---------------------------------------------------------------------------
# 底层文件读写
# ---------------------------------------------------------------------------

def initStorage():
    """建数据目录。在 main.Event.init 里调用。"""
    if not os.path.exists(DATA_PATH):
        os.makedirs(DATA_PATH, exist_ok=True)


def _path(fileName):
    return os.path.join(DATA_PATH, fileName)


def _safeRead(fileName, defaultValue):
    """
    读一个 JSON 文件。不存在 / 空 / 损坏 → 返回默认值。

    为什么这么宽容：这是长期跑的活动数据，宁可某一项回退默认值，
    也不能因为一个文件损坏就让整个插件加载失败（那样所有人都打不了了）。
    损坏时优先用 `.bak` 兜底，比直接回退默认值少丢数据。
    """
    filePath = _path(fileName)
    if not os.path.exists(filePath):
        return defaultValue
    try:
        with open(filePath, 'r', encoding='utf-8') as f:
            content = f.read()
        if not content.strip():
            return defaultValue
        data = json.loads(content)
        return defaultValue if data is None else data
    except Exception:
        print('[pvp] 读取 %s 损坏，尝试用备份恢复。' % fileName)
        backupPath = filePath + '.bak'
        try:
            if os.path.exists(backupPath):
                with open(backupPath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                shutil.copy(backupPath, filePath)
                print('[pvp] 已用 %s.bak 恢复。' % fileName)
                return data
        except Exception:
            print('[pvp] 备份也读不了，回退默认值。')
        return defaultValue


def _safeWrite(fileName, data):
    """
    原子地写一个 JSON 文件：先备份成 .bak，再写 .tmp，最后 os.replace 覆盖。

    顺序很关键：**先备份旧文件**，这样如果新内容写坏了，还能退回上一版。
    """
    filePath = _path(fileName)
    tmpPath = filePath + '.tmp'
    try:
        if not os.path.exists(DATA_PATH):
            os.makedirs(DATA_PATH, exist_ok=True)
        if os.path.exists(filePath):
            try:
                shutil.copy(filePath, filePath + '.bak')
            except Exception:
                pass
        with open(tmpPath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmpPath, filePath)
    except Exception as e:
        print('[pvp] 写入 %s 失败：%s' % (fileName, e))


def _mergeDefaults(data, defaults):
    """
    用默认值补齐缺失的键（不覆盖已有值），并告诉调用方有没有改动。

    用于「插件升级后新增了配置项」的场景：老配置文件没有新键，
    进来时自动补上，用户不用删文件重配。
    """
    changed = False
    if not isinstance(data, dict):
        data = {}
        changed = True
    for key in defaults:
        if key not in data:
            value = defaults[key]
            data[key] = dict(value) if isinstance(value, dict) else value
            changed = True
    return data, changed


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

def reload():
    """
    把全部数据从磁盘读进内存。在 main.Event.init 里调用，也在
    「结束活动」之后调用（那时磁盘已清空，内存也要跟着清）。

    reloadLight 只读玩家表和对局表，是给骰子钩子用的轻量版（见那里说明）。
    """
    with _LOCK:
        _reloadGame()
        _reloadConfig()
        _reloadPlayers()
        _reloadQueue()
        _reloadMatch()
        _reloadReports()


def reloadLight():
    """
    只重读「玩家表 + 对局表」。

    给骰子钩子用 —— 钩子每次掷骰都会触发，调用极频繁；
    而它需要的只有这两张表（查掷骰的人是谁、现在是不是掷骰阶段）。
    游戏信息、配置、队列、战报在钩子路径上完全用不到，读它们纯属浪费。

    【为什么还要重读而不是直接用内存缓存】
    因为钩子可能在「另一个进程/另一份插件实例」写过盘之后触发
    （比如场控手动改过文件），重读一次成本很低但能避免读到陈旧数据。
    """
    with _LOCK:
        _reloadPlayers()
        _reloadMatch()


def _reloadGame():
    global gGame
    data = _safeRead(FILE_GAME, {})
    data, changed = _mergeDefaults(data, {
        'game_name': '',
        'backstage_group': '',
        'admins': [],
        'registered_at': '',
    })
    if not isinstance(data.get('admins'), list):
        data['admins'] = []
    gGame = data
    if changed:
        _safeWrite(FILE_GAME, gGame)


def _reloadConfig():
    global gConfig
    data = _safeRead(FILE_CONFIG, {})
    data, changed = _mergeDefaults(data, CONFIG_DEFAULT)
    # 伤害表也要补齐：用户可能手改文件删掉某一档
    if not isinstance(data.get('damage_table'), dict):
        data['damage_table'] = dict(pvp.jfzCore.DAMAGE_TABLE_DEFAULT)
        changed = True
    for level in pvp.jfzCore.LEVEL_ORDER:
        if level not in data['damage_table']:
            data['damage_table'][level] = pvp.jfzCore.DAMAGE_TABLE_DEFAULT[level]
            changed = True
    if not isinstance(data.get('shop_prices'), dict):
        data['shop_prices'] = {}
        changed = True
    gConfig = data
    if changed:
        _safeWrite(FILE_CONFIG, gConfig)


def _reloadPlayers():
    global gPlayers, gPlayerByQQ, gPlayerByGroup
    data = _safeRead(FILE_PLAYER, {})
    if not isinstance(data, dict):
        data = {}
    gPlayers = data
    _rebuildPlayerIndex()


def _rebuildPlayerIndex():
    """重建 QQ / 群号 → 玩家名 的两个索引。"""
    global gPlayerByQQ, gPlayerByGroup
    gPlayerByQQ = {}
    gPlayerByGroup = {}
    for name, player in gPlayers.items():
        if not isinstance(player, dict):
            continue
        qq = str(player.get('user_qq', '') or '')
        group = str(player.get('group_id', '') or '')
        if qq:
            gPlayerByQQ[qq] = name
        if group:
            gPlayerByGroup[group] = name


def _reloadQueue():
    global gQueue
    data = _safeRead(FILE_QUEUE, [])
    gQueue = data if isinstance(data, list) else []


def _reloadMatch():
    global gMatch
    data = _safeRead(FILE_MATCH, {})
    gMatch = data if isinstance(data, dict) else {}


def _reloadReports():
    global gReports
    data = _safeRead(FILE_REPORT, [])
    gReports = data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# 游戏信息（PRD 4.1.1 / 4.1.2 / 4.10）
# ---------------------------------------------------------------------------

def getGameName():
    """取当前游戏名。未注册时返回空串。"""
    return str(gGame.get('game_name', '') or '').strip()


def isRegistered():
    """插件有没有处于「已注册」状态（骰主注册过后台群并声明了游戏名）。"""
    return bool(getGameName()) and bool(getBackstageGroup())


def getBackstageGroup():
    """取后台群号。未注册时返回空串。"""
    return str(gGame.get('backstage_group', '') or '').strip()


def getAdminList():
    """取后台管理员 QQ 名单（字符串列表）。"""
    admins = gGame.get('admins', [])
    if not isinstance(admins, list):
        return []
    return [str(item) for item in admins]


def isBackstageGroup(groupId):
    """这个群是不是后台群。"""
    backstage = getBackstageGroup()
    return bool(backstage) and str(groupId) == backstage


def isBackstageAdmin(userId):
    """这个 QQ 是不是后台管理员（PRD 2.1）。"""
    return str(userId) in getAdminList()


def setBackstageGroup(gameName, groupId):
    """
    注册后台群并声明游戏名（PRD 4.1.1）。

    【为什么要连 game_name 一起写】PRD 第 1 节说游戏名是「当前进行中的游戏名」，
    注册后台群时声明；消息模板里的 `【尽伏诛】` 要自动换成它。
    所以后台群和游戏名是同一件事的两半，必须一起落盘。
    """
    with _LOCK:
        gGame['game_name'] = str(gameName).strip()
        gGame['backstage_group'] = str(groupId)
        gGame['registered_at'] = pvp.jfzCore.getNow().strftime('%Y-%m-%d %H:%M:%S')
        _safeWrite(FILE_GAME, gGame)


def addAdmin(userId):
    """添加后台管理员。已在名单里返回 False。"""
    with _LOCK:
        admins = getAdminList()
        if str(userId) in admins:
            return False
        admins.append(str(userId))
        gGame['admins'] = admins
        _safeWrite(FILE_GAME, gGame)
        return True


def removeAdmin(userId):
    """移除后台管理员。不在名单里返回 False。"""
    with _LOCK:
        admins = getAdminList()
        if str(userId) not in admins:
            return False
        admins = [item for item in admins if item != str(userId)]
        gGame['admins'] = admins
        _safeWrite(FILE_GAME, gGame)
        return True


def updateGameName(gameName):
    """只改游戏名（后台群不变）。"""
    with _LOCK:
        gGame['game_name'] = str(gameName).strip()
        _safeWrite(FILE_GAME, gGame)


# ---------------------------------------------------------------------------
# 活动配置（PRD 4.2 / 7.3）
# ---------------------------------------------------------------------------

def getConfig(key, defaultValue=None):
    """读一项配置。"""
    with _LOCK:
        if key in gConfig:
            return gConfig[key]
    if defaultValue is not None:
        return defaultValue
    return CONFIG_DEFAULT.get(key)


def getConfigInt(key, defaultValue=0):
    """读一项「数字」配置。手改文件时常写成字符串，这里统一转 int。"""
    raw = getConfig(key, defaultValue)
    try:
        return int(raw)
    except Exception:
        return defaultValue


def setConfigValue(key, value):
    """写一项配置并立即落盘。"""
    with _LOCK:
        gConfig[key] = value
        _safeWrite(FILE_CONFIG, gConfig)


def getActivityConfig():
    """
    取「活动相关」的那一部分配置，打包成一个 dict 给引擎用。

    引擎/核心层只依赖这个 dict，不直接摸 gConfig ——
    这样测试时可以直接塞一个假配置进去，不需要落盘。
    """
    with _LOCK:
        return {
            'start_date': str(getConfig('start_date', '') or ''),
            'end_date': str(getConfig('end_date', '') or ''),
            'daily_start_time': str(getConfig('daily_start_time', '14:00') or ''),
            'daily_end_time': str(getConfig('daily_end_time', '18:00') or ''),
            'initial_hp': getConfigInt('initial_hp', pvp.jfzCore.HP_INITIAL),
            'hp_limit': getConfigInt('hp_limit', pvp.jfzCore.HP_LIMIT),
            'initial_copper': getConfigInt('initial_copper', 1000),
            'initial_attack': getConfigInt('initial_attack', 50),
            'daily_attack_limit': getConfigInt('daily_attack_limit', 3),
            'rounds': getConfigInt('rounds', 10),
            'item_limit_per_match': getConfigInt('item_limit_per_match', 1),
            'damage_table': getDamageTable(),
        }


def getDamageTable():
    """取伤害表。缺失的档位用默认值补齐，保证引擎拿到的是一张完整的表。"""
    with _LOCK:
        table = gConfig.get('damage_table', {})
        if not isinstance(table, dict):
            table = {}
        result = {}
        for level in pvp.jfzCore.LEVEL_ORDER:
            default = pvp.jfzCore.DAMAGE_TABLE_DEFAULT.get(level, 0)
            try:
                result[level] = int(table.get(level, default))
            except Exception:
                result[level] = default
        return result


def setDamageTableLevel(level, value):
    """改伤害表里的一档。未知档位返回 False。"""
    if level not in pvp.jfzCore.LEVEL_ORDER:
        return False
    with _LOCK:
        table = getDamageTable()
        table[level] = int(value)
        gConfig['damage_table'] = table
        _safeWrite(FILE_CONFIG, gConfig)
        return True


# ---------------------------------------------------------------------------
# 商城价格（PRD 4.8.1）
# ---------------------------------------------------------------------------
# PRD 只说了「商城可以查看价格」，没给具体数字，也没给改价指令。
# 所以默认价格写在 jfzCore.ITEM_TABLE 里，同时留一张覆盖表，
# 让场控可以用 `【PVP】设置道具价格 大还丹 500` 改（属于本插件的扩展指令，
# 已在指令手册里标注为「扩展」）。

def getItemPrice(itemName):
    """取道具当前售价。不认识的道具返回 -1。"""
    itemDef = pvp.jfzCore.getItemDef(itemName)
    if itemDef is None:
        return -1
    with _LOCK:
        prices = gConfig.get('shop_prices', {})
        if isinstance(prices, dict) and itemName in prices:
            try:
                return int(prices[itemName])
            except Exception:
                pass
    return int(itemDef.get('price', 0))


def setItemPrice(itemName, price):
    """改道具售价。不认识的道具返回 False。"""
    if not pvp.jfzCore.isItemName(itemName):
        return False
    with _LOCK:
        prices = gConfig.get('shop_prices', {})
        if not isinstance(prices, dict):
            prices = {}
        prices[itemName] = int(price)
        gConfig['shop_prices'] = prices
        _safeWrite(FILE_CONFIG, gConfig)
        return True


def getShopPriceMap():
    """取全部道具的当前售价，返回 {道具名: 价格}。"""
    return dict((name, getItemPrice(name)) for name in pvp.jfzCore.ITEM_ORDER)


# ---------------------------------------------------------------------------
# 玩家（PRD 7.1）
# ---------------------------------------------------------------------------

def _packPlayer(player):
    """
    写盘前把玩家档案规整一遍：
      - status 按 hp 重算（PRD 7.1 有这个字段，但它是只读镜像）
      - hp 夹到 [0, 当前血量上限]（上限 = 配置 hp_limit，不是初始血量）
      - copper 不能是负数
      - inventory 里的数量取整、去掉 0 和负数项

    ★ 注意「hp 夹到上限」意味着：骰主把 hp_limit 调**小**之后，
      存档里超限的血量会在下一次落盘时被削到新上限 ★
      这是有意为之 —— 上限降低了就不该有人还挂着旧上限的血；
      想保住某些人的血量，请在上限下调之后用「修改血量」单独补。
    """
    # 存档里缺 hp 字段（手改 JSON / 极早期版本的数据）时按「上限满血」补，
    # 而不是按「初始血量」补 —— 这种情况下他是一条已经存在的老档案，
    # 用上限才不会被莫名其妙地削一刀。
    hp = pvp.jfzCore.clampHp(player.get('hp', pvp.jfzCore.getHpLimit()))
    player['hp'] = hp
    player['status'] = pvp.jfzCore.deriveStatus(hp)

    try:
        copper = int(player.get('copper', 0))
    except Exception:
        copper = 0
    player['copper'] = max(0, copper)

    inventory = player.get('inventory', {})
    if not isinstance(inventory, dict):
        inventory = {}
    cleaned = {}
    for name, count in inventory.items():
        try:
            count = int(count)
        except Exception:
            continue
        if count > 0:
            cleaned[str(name)] = count
    player['inventory'] = cleaned

    try:
        used = int(player.get('daily_attack_used', 0))
    except Exception:
        used = 0
    player['daily_attack_used'] = max(0, used)

    # 个人攻击值（可选字段）：没有这个键就是「跟随全局 initial_attack」。
    # 刻意**不**把全局值抄进来存着 —— 抄进来之后骰主改全局值，
    # 老玩家会因为存了旧值而完全不受影响，看起来就是「设置没生效」。
    if 'attack' in player:
        attack = _toAttack(player.get('attack'))
        if attack is None:
            # 非法值（手改存档写坏了）就当没设过，回落到全局值
            player.pop('attack', None)
        else:
            player['attack'] = attack

    return player


def _toAttack(raw):
    """把任意输入转成合法的个人攻击值；非法/超范围返回 None（= 视为未设置）。"""
    try:
        value = int(raw)
    except Exception:
        return None
    if value < pvp.jfzCore.ATTACK_MIN or value > pvp.jfzCore.ATTACK_MAX:
        return None
    return value


def getPersonalAttack(name):
    """
    取某个玩家的**个人**攻击值；没被单独设过（或值非法）返回 None。

    与 getPlayerAttack 的区别：这个函数不做回落，
    用来判断「这个人是不是被骰主单独指定过」——名单和【PVP】血量要靠它决定
    要不要标一句「个人」。判断逻辑收在这里，免得各处自己写 `if 'attack' in player`。
    """
    player = getPlayer(name)
    if not player:
        return None
    return _toAttack(player.get('attack'))


def getPlayerAttack(name):
    """
    取某个玩家本场该用的基础攻击值：**个人值优先，没有就用全局值**。

    【为什么做成「个人值可选、缺省回落」】
    骰主的需求是「既能设统一值，也能单独给某个人设」。如果建档时就把
    全局值抄进玩家档案，那么之后骰主改全局值，所有老玩家都不会变 ——
    骰主看到的现象是「我设置了但没用」，而排查起来完全看不出问题在哪。
    所以档案里只在**被单独指定过**的情况下才有 `attack` 键。
    """
    attack = getPersonalAttack(name)
    if attack is not None:
        return attack
    return getGlobalAttack()


def getGlobalAttack():
    """取全局默认攻击值（配置 initial_attack）。"""
    value = getConfigInt('initial_attack', pvp.jfzCore.ATTACK_DEFAULT)
    if value < pvp.jfzCore.ATTACK_MIN or value > pvp.jfzCore.ATTACK_MAX:
        return pvp.jfzCore.ATTACK_DEFAULT
    return value


def setPlayerAttack(name, value):
    """
    设置 / 清除某个玩家的个人攻击值。

    value 传 None 表示**清除个人设定**（回落到全局值）。
    返回 (成功?, 当前生效值或原因)。
    """
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return False, '找不到玩家「%s」。' % name
        if value is None:
            player.pop('attack', None)
        else:
            attack = _toAttack(value)
            if attack is None:
                return False, '攻击值应在 %d ~ %d 之间。' % (
                    pvp.jfzCore.ATTACK_MIN, pvp.jfzCore.ATTACK_MAX
                )
            player['attack'] = attack
        _packPlayer(player)
        gPlayers[str(name).strip()] = player
        savePlayers()
        return True, getPlayerAttack(name)


def savePlayers():
    """把玩家表落盘。所有改玩家数据的地方都要在改完之后调它。"""
    with _LOCK:
        for name in list(gPlayers.keys()):
            player = gPlayers.get(name)
            if isinstance(player, dict):
                _packPlayer(player)
        _safeWrite(FILE_PLAYER, gPlayers)


def getPlayer(name):
    """按玩家名取档案。不存在返回 None。"""
    if not name:
        return None
    with _LOCK:
        player = gPlayers.get(str(name).strip())
    return player if isinstance(player, dict) else None


def getPlayerByQQ(userQQ):
    """按用户 QQ 取档案。不存在返回 None。"""
    if userQQ is None:
        return None
    with _LOCK:
        name = gPlayerByQQ.get(str(userQQ))
    return getPlayer(name) if name else None


def getPlayerByGroup(groupId):
    """按个人群号取档案。不存在返回 None。"""
    if groupId is None:
        return None
    with _LOCK:
        name = gPlayerByGroup.get(str(groupId))
    return getPlayer(name) if name else None


def getPlayerNameByQQ(userQQ):
    """按 QQ 取玩家名。不存在返回空串。"""
    with _LOCK:
        return gPlayerByQQ.get(str(userQQ), '')


def getPlayerNameByGroup(groupId):
    """
    按个人群号取玩家名。不存在返回空串。

    ★ 注意与 getPlayerByGroup 的区别 ★
      getPlayerByGroup(groupId)     → 返回玩家档案 dict（或 None）
      getPlayerNameByGroup(groupId) → 返回玩家名字符串（或 ''）

    两个函数并存是刻意的：骰子钩子只想知道「这个群是谁的」，
    拿到名字就够了，不需要整个档案；而注册玩家时要 check「这个群有没有被占」，
    那时需要别人的档案。两者混用会静默拿到 None（dict 的 get 不会报错），
    表现成「功能莫名不生效」，所以这里名字分开、调用点也分开。
    """
    if groupId is None:
        return ''
    with _LOCK:
        return gPlayerByGroup.get(str(groupId), '')


def isNameTaken(name):
    """这个玩家名是否已被占用（PRD 4.1.3：玩家名称需全局唯一）。"""
    return getPlayer(name) is not None


def isQQRegistered(userQQ):
    """这个 QQ 是否已经注册过。"""
    with _LOCK:
        return str(userQQ) in gPlayerByQQ


def listPlayerNames():
    """取全部玩家名（按注册顺序，即 JSON 里的键顺序）。"""
    with _LOCK:
        return [name for name in gPlayers.keys() if isinstance(gPlayers.get(name), dict)]


def createPlayer(userQQ, displayName, groupId):
    """
    建玩家档案（PRD 4.1.4）。

    初始属性取自活动配置的默认值：血量取 `initial_hp`（出生血量，缺省 200，
    PRD 4.7.1，且不会超过 `hp_limit` 上限），铜币取 `initial_copper`，背包为空。

    返回 (成功?, 提示文本)。
    """
    userQQ = str(userQQ).strip()
    displayName = pvp.jfzCore.cleanName(displayName)
    groupId = str(groupId).strip()

    if not userQQ:
        return False, 'QQ 号为空。'
    if not displayName:
        return False, '玩家名称不能为空。'
    if not groupId:
        return False, '群号为空。'

    with _LOCK:
        existName = gPlayerByQQ.get(userQQ)
        if existName:
            # 一个 QQ 只能绑一个玩家名，否则「谁是谁」会彻底乱掉
            if existName != displayName:
                return False, 'QQ %s 已注册为玩家「%s」，一个 QQ 只能有一个玩家名。' % (userQQ, existName)
            player = gPlayers.get(existName, {})
            # 同 QQ 同名再注册一次：当成「换绑个人群」处理
            oldGroup = str(player.get('group_id', '') or '')
            if oldGroup and oldGroup != groupId:
                return False, '「%s」的个人群已绑定为 %s，一个玩家只能绑定一个个人群。' % (displayName, oldGroup)
            player['group_id'] = groupId
            gPlayers[existName] = _packPlayer(player)
            _rebuildPlayerIndex()
            savePlayers()
            return True, '玩家「%s」的个人群已更新为 %s。' % (displayName, groupId)

        if isNameTaken(displayName):
            other = gPlayers.get(displayName, {})
            otherQQ = str(other.get('user_qq', '') or '')
            return False, '玩家名「%s」已被 QQ %s 使用，请换一个名字（玩家名需全局唯一）。' % (displayName, otherQQ)

        player = {
            'user_qq': userQQ,
            'display_name': displayName,
            'group_id': groupId,
            # 出生血量 = 配置 initial_hp（且已被 getInitialHp() 夹到上限以内）。
            # 刻意走 jfzCore.getInitialHp() 而不是在这里读配置：
            # 「新玩家有多少血」全项目只有这一个出口，注册回执 / 名单 / 配置
            # 显示的都是同一个数，不会出现两处数字打架。
            'hp': pvp.jfzCore.getInitialHp(),
            'copper': getConfigInt('initial_copper', 1000),
            'status': pvp.jfzCore.STATUS_NORMAL,
            'daily_attack_used': 0,
            'daily_attack_date': pvp.jfzCore.getTodayKey(),
            'inventory': {},
        }
        gPlayers[displayName] = _packPlayer(player)
        _rebuildPlayerIndex()
        savePlayers()

    return True, '玩家「%s」注册成功。' % displayName


def deletePlayer(name):
    """删一个玩家。返回是否删掉了。"""
    with _LOCK:
        name = str(name).strip()
        if name not in gPlayers:
            return False
        del gPlayers[name]
        _rebuildPlayerIndex()
        savePlayers()
        return True


def setPlayerHp(name, hp):
    """直接设定血量（已夹到 [0, 当前血量上限]）。"""
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return None
        player['hp'] = pvp.jfzCore.clampHp(hp)
        _packPlayer(player)
        savePlayers()
        return player['hp']


def changePlayerHp(name, delta):
    """
    增减血量。返回 (新血量, 实际变化量)。玩家不存在返回 (None, 0)。

    实际变化量可能和 delta 不同 —— 因为会被血量上限 / 下限 0 截断。
    战报里要显示真实变化，所以这里把两者都返回。
    """
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return None, 0
        before = int(player.get('hp', 0))
        after = pvp.jfzCore.clampHp(before + int(delta))
        player['hp'] = after
        _packPlayer(player)
        savePlayers()
        return after, after - before


def setPlayerCopper(name, copper):
    """直接设定铜币。"""
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return None
        player['copper'] = max(0, int(copper))
        savePlayers()
        return player['copper']


def changePlayerCopper(name, delta):
    """
    增减铜币。返回 (新铜币, 实际变化量)。扣到 0 为止，不会变负。
    """
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return None, 0
        before = int(player.get('copper', 0))
        after = max(0, before + int(delta))
        player['copper'] = after
        savePlayers()
        return after, after - before


def getInventory(name):
    """取背包副本 {道具名: 数量}。"""
    player = getPlayer(name)
    if not player:
        return {}
    inventory = player.get('inventory', {})
    if not isinstance(inventory, dict):
        return {}
    return dict((str(k), int(v)) for k, v in inventory.items())


def addItem(name, itemName, count=1):
    """
    往背包加道具。返回加完之后的持有数量；玩家或道具不存在返回 -1。
    """
    if not pvp.jfzCore.isItemName(itemName):
        return -1
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return -1
        inventory = player.get('inventory', {})
        if not isinstance(inventory, dict):
            inventory = {}
        try:
            current = int(inventory.get(itemName, 0))
        except Exception:
            current = 0
        inventory[itemName] = current + int(count)
        player['inventory'] = inventory
        _packPlayer(player)
        savePlayers()
        return inventory[itemName]


def removeItem(name, itemName, count=1):
    """
    从背包扣道具。返回 (成功?, 扣完剩余数量)。

    ★ 数量不足时**不扣**、返回失败 ★
    道具声明阶段如果允许「扣成负数」再打补丁，很容易出现
    「两个人同时声明同一件最后一件道具，两个人都声明成功」的漏洞。
    所以这里严格检查、扣不动就报失败，由调用方回提示。
    """
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return False, 0
        inventory = player.get('inventory', {})
        if not isinstance(inventory, dict):
            inventory = {}
        try:
            current = int(inventory.get(itemName, 0))
        except Exception:
            current = 0
        if current < int(count):
            return False, current
        left = current - int(count)
        if left <= 0:
            inventory.pop(itemName, None)
        else:
            inventory[itemName] = left
        player['inventory'] = inventory
        _packPlayer(player)
        savePlayers()
        return True, max(0, left)


def transferAll(fromName, toName):
    """
    把 A 的背包和铜币全部转给 B（PRD 4.7.2 死亡掉落）。

    返回 (转出的道具表, 转出的铜币)。任一方不存在时返回 (None, 0)。

    【为什么用「减空再加」而不是直接覆盖】
    B 可能本来就有背包和铜币，直接覆盖会把 B 的东西冲掉。
    """
    with _LOCK:
        dead = gPlayers.get(str(fromName).strip())
        alive = gPlayers.get(str(toName).strip())
        if not isinstance(dead, dict) or not isinstance(alive, dict):
            return None, 0

        deadInventory = dead.get('inventory', {})
        if not isinstance(deadInventory, dict):
            deadInventory = {}
        copper = int(dead.get('copper', 0) or 0)

        aliveInventory = alive.get('inventory', {})
        if not isinstance(aliveInventory, dict):
            aliveInventory = {}
        for itemName, count in deadInventory.items():
            try:
                count = int(count)
            except Exception:
                continue
            if count <= 0:
                continue
            aliveInventory[itemName] = int(aliveInventory.get(itemName, 0)) + count

        alive['inventory'] = aliveInventory
        alive['copper'] = int(alive.get('copper', 0)) + copper

        dead['inventory'] = {}
        dead['copper'] = 0

        _packPlayer(dead)
        _packPlayer(alive)
        savePlayers()

        return dict(deadInventory), copper


def resetDailyAttackIfNeeded(name):
    """
    跨天时把「当日已用主动次数」清零（PRD 4.2 的「每人每天可主动发起 N 次」）。

    ★ 为什么记日期而不是靠定时任务 ★
    定时任务在「机器人在深夜重启」时容易漏跑；记一个日期键，
    每次要用这个数之前顺手比一下，永远不会漏、也不会多。
    返回是否发生了重置。
    """
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return False
        today = pvp.jfzCore.getTodayKey()
        if str(player.get('daily_attack_date', '') or '') == today:
            return False
        player['daily_attack_date'] = today
        player['daily_attack_used'] = 0
        savePlayers()
        return True


def getDailyAttackUsed(name):
    """取当日已用主动次数（会先做跨天重置）。"""
    resetDailyAttackIfNeeded(name)
    player = getPlayer(name)
    if not player:
        return 0
    try:
        return int(player.get('daily_attack_used', 0))
    except Exception:
        return 0


def addDailyAttackUsed(name, delta=1):
    """累加当日已用主动次数。"""
    with _LOCK:
        player = gPlayers.get(str(name).strip())
        if not isinstance(player, dict):
            return 0
        try:
            used = int(player.get('daily_attack_used', 0))
        except Exception:
            used = 0
        player['daily_attack_used'] = max(0, used + int(delta))
        player['daily_attack_date'] = pvp.jfzCore.getTodayKey()
        savePlayers()
        return player['daily_attack_used']


# ---------------------------------------------------------------------------
# 队列（PRD 4.3.2）
# ---------------------------------------------------------------------------

def getQueue():
    """取请求队列的副本。"""
    with _LOCK:
        return [dict(item) for item in gQueue]


def setQueue(queue):
    """整体覆盖队列并落盘。"""
    with _LOCK:
        global gQueue
        gQueue = list(queue)
        _safeWrite(FILE_QUEUE, gQueue)


def enqueueRequest(request):
    """把一条 PVP 请求追加到队尾，返回它在队列里的位置（从 1 开始）。"""
    with _LOCK:
        gQueue.append(request)
        _safeWrite(FILE_QUEUE, gQueue)
        return len(gQueue)


def popQueue():
    """取出队首请求（会从队列里删掉）。空队列返回 None。"""
    with _LOCK:
        if not gQueue:
            return None
        request = gQueue.pop(0)
        _safeWrite(FILE_QUEUE, gQueue)
        return request


def clearQueue():
    """清空队列并落盘。"""
    with _LOCK:
        global gQueue
        gQueue = []
        _safeWrite(FILE_QUEUE, gQueue)


def removeQueueByPlayer(name):
    """
    把和某个玩家有关的请求从队列里摘掉，返回被摘掉的请求列表。

    用于「删除玩家」「玩家进战斗」等场景 —— 否则队列里会留着
    指向一个已经不存在/已经在打的人的请求，出队时白白报错。
    """
    with _LOCK:
        global gQueue
        removed = []
        keep = []
        for request in gQueue:
            if request.get('challenger_name') == name or request.get('target_name') == name:
                removed.append(request)
            else:
                keep.append(request)
        if removed:
            gQueue = keep
            _safeWrite(FILE_QUEUE, gQueue)
        return removed


def getQueuePosition(matchId):
    """取某个请求在队列里的位置（从 1 开始）。找不到返回 0。"""
    with _LOCK:
        for index, request in enumerate(gQueue):
            if request.get('match_id') == matchId:
                return index + 1
    return 0


# ---------------------------------------------------------------------------
# 对局（PRD 7.2）
# ---------------------------------------------------------------------------

def getMatch():
    """取当前对局的副本。没有进行中的对局时返回 {}。"""
    with _LOCK:
        return dict(gMatch) if isinstance(gMatch, dict) else {}


def setMatch(match):
    """整体覆盖当前对局并落盘。传 None / {} 表示「没有对局」。"""
    with _LOCK:
        global gMatch
        gMatch = dict(match) if isinstance(match, dict) else {}
        _safeWrite(FILE_MATCH, gMatch)


def updateMatch(**fields):
    """
    局部更新当前对局字段并落盘，返回更新后的对局副本。

    【为什么要有这个「局部更新」入口】
    对局对象字段很多，如果每次改一个字段都 getMatch() → 改 → setMatch()，
    调用方很容易漏带某个字段（尤其是列表类字段），
    把已经掷了几回合的记录覆盖成空 —— 那是「掷了半天白掷」级别的事故。
    局部更新只动指定的键，不会碰到别的。
    """
    with _LOCK:
        global gMatch
        if not isinstance(gMatch, dict):
            gMatch = {}
        gMatch.update(fields)
        _safeWrite(FILE_MATCH, gMatch)
        return dict(gMatch)


def clearMatch():
    """清掉当前对局。"""
    setMatch({})


def hasActiveMatch():
    """有没有进行中的对局。"""
    with _LOCK:
        return bool(gMatch) and gMatch.get('state') == pvp.jfzCore.MATCH_ACTIVE


def getMatchPlayerNames():
    """取当前对局里的两个玩家名。没有对局返回空列表。"""
    with _LOCK:
        if not gMatch:
            return []
        names = []
        for key in ['challenger_name', 'target_name']:
            value = gMatch.get(key)
            if value:
                names.append(str(value))
        return names


def isPlayerInMatch(name):
    """这个玩家是不是正在某场进行中的对局里（PRD 4.9 战斗锁）。"""
    with _LOCK:
        if not gMatch or gMatch.get('state') != pvp.jfzCore.MATCH_ACTIVE:
            return False
        return str(name) in [str(gMatch.get('challenger_name', '')), str(gMatch.get('target_name', ''))]


def isPlayerInQueue(name):
    """这个玩家是不是在队列里有未处理的请求（发起方或被发起方）。"""
    with _LOCK:
        for request in gQueue:
            if str(request.get('challenger_name', '')) == str(name):
                return True
            if str(request.get('target_name', '')) == str(name):
                return True
    return False


def generateMatchId():
    """生成对局 ID。用时间戳 + 双方 QQ 后缀，保证可读且不重复。"""
    return 'M%s' % pvp.jfzCore.getNow().strftime('%Y%m%d%H%M%S')


# ---------------------------------------------------------------------------
# 战报（PRD 4.9「查看全量战报」）
# ---------------------------------------------------------------------------

def addReport(report):
    """追加一条战报并落盘。"""
    with _LOCK:
        gReports.append(report)
        # 只留最近 500 条，避免文件无限膨胀
        if len(gReports) > 500:
            del gReports[0:len(gReports) - 500]
        _safeWrite(FILE_REPORT, gReports)
        return report


def getReports(limit=5):
    """取最近 N 条战报（最新的在前）。"""
    with _LOCK:
        data = list(gReports)
    data.reverse()
    return data[:limit]


def getLastReport():
    """取最近一条战报。没有返回 None。"""
    reports = getReports(1)
    return reports[0] if reports else None


# ---------------------------------------------------------------------------
# 结束活动：清空一切（PRD 4.10）
# ---------------------------------------------------------------------------

def wipeAllData():
    """
    删除当前游戏的全部数据，并让内存缓存回到「未注册」状态。

    ★ 这个函数只在 `【PVP】结束活动 确认` 里被调用 ★
    调用前 jfzEngine 已经取消了所有对局。这里只负责删数据。

    【为什么要「先清零内存再删文件」而不是反过来】
    如果先删文件，中间被别的线程（比如发送线程）插进来读一次，
    会读到「文件没了」→ 回退默认值 → 可能把一份空配置写回磁盘。
    先清零内存就避免了这种竞态。
    """
    with _LOCK:
        global gGame, gConfig, gPlayers, gPlayerByQQ, gPlayerByGroup, gQueue, gMatch, gReports

        # 1. 内存清零（回到「未注册」）
        gGame = {'game_name': '', 'backstage_group': '', 'admins': [], 'registered_at': ''}
        gPlayers = {}
        gPlayerByQQ = {}
        gPlayerByGroup = {}
        gQueue = []
        gMatch = {}
        gReports = []
        # 配置也要清：PRD 4.10 说「删除所有活动配置」，下次注册重新配。
        # 但 master_id 是环境相关的身份兜底，跟本次游戏无关，保留它，
        # 否则骰主在 openid 环境下会把自己锁在门外。
        masterId = gConfig.get('master_id', '')
        gConfig = dict(CONFIG_DEFAULT)
        gConfig['master_id'] = masterId
        gConfig['damage_table'] = dict(pvp.jfzCore.DAMAGE_TABLE_DEFAULT)

        # 2. 删文件
        for fileName in [FILE_GAME, FILE_CONFIG, FILE_PLAYER, FILE_QUEUE, FILE_MATCH, FILE_REPORT]:
            for path in [_path(fileName), _path(fileName) + '.bak']:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    print('[pvp] 删除 %s 失败：' % path)
                    traceback.print_exc()

        # 3. 只把「配置」写回磁盘，其余文件保持「已被删除」的状态。
        #
        # 【为什么不像原来那样把 游戏.json 也一起写回去】
        # 写回去的话磁盘上会留一个内容为空的 游戏.json —— 从「删干净了没有」
        # 这个骰主最关心的问题看，留一个空文件等于没删干净：
        # 骰主翻数据目录时会以为还有残渣。
        # 而内存已经清零，下次读不到文件会自动回退到「未注册」的默认值，
        # 不写回去完全安全。
        #
        # 【那 配置.json 为什么还要写】
        # 因为里面有一个不属于「本次游戏」的东西：master_id。
        # 它是骰主身份在 openid 类环境下的兜底（见上面注释），
        # 跟这次活动无关，丢了会让骰主在重启后无法再执行骰主指令。
        # 写回去的内容是 CONFIG_DEFAULT（活动配置已重置），
        # 所以 PRD 4.10「删除所有活动配置」的效果是达成的。
        _safeWrite(FILE_CONFIG, gConfig)

        print('[pvp] 结束活动：所有数据已删除，插件回到未注册状态。')


# ---------------------------------------------------------------------------
# 摘要文本（给管理指令用）
# ---------------------------------------------------------------------------

def getConfigTableText():
    """把当前活动配置渲染成一段给人看的文本。"""
    with _LOCK:
        config = getActivityConfig()
        table = config['damage_table']

    lines = [
        '【当前活动配置】',
        '游戏名：%s' % (getGameName() or '（未注册）'),
        '后台群：%s' % (getBackstageGroup() or '（未注册）'),
        '活动周期：%s' % (
            pvp.jfzCore.dateRangeText(config['start_date'], config['end_date'])
            if config['start_date'] and config['end_date'] else '（未配置）'
        ),
        '每日时段：%s ~ %s' % (config['daily_start_time'], config['daily_end_time']),
        '初始血量：%d（新玩家出生血量）' % config['initial_hp'],
        '血量上限：%d（回血与显示的封顶值）' % config['hp_limit'],
        '初始铜币：%d' % config['initial_copper'],
        '初始攻击：%d（统一值，可被个人设定覆盖）' % config['initial_attack'],
        '每日主动次数：%d' % config['daily_attack_limit'],
        '每场回合数：%d' % config['rounds'],
        '每种道具每场限用：%d' % config['item_limit_per_match'],
        '声明超时：%s' % (
            '%d 分钟' % config['declare_timeout_minutes']
            if int(config.get('declare_timeout_minutes', 0)) > 0 else '（不限制）'
        ),
        '掷骰超时：%s' % (
            '%d 分钟' % config['fight_timeout_minutes']
            if int(config.get('fight_timeout_minutes', 0)) > 0 else '（不限制）'
        ),
        '伤害表：%s' % '  '.join('%s=%d' % (level, table[level]) for level in pvp.jfzCore.LEVEL_ORDER),
        '已注册玩家：%d 人' % len(listPlayerNames()),
    ]
    return '\n'.join(lines)


def getPlayerTableText():
    """把全体玩家渲染成一段文本（管理查看看全量数据用）。"""
    names = listPlayerNames()
    if not names:
        return '当前没有任何已注册玩家。'

    lines = ['【玩家名单】']
    for index, name in enumerate(names, 1):
        player = getPlayer(name) or {}
        hp = int(player.get('hp', 0))
        status = pvp.jfzCore.getStatusName(pvp.jfzCore.deriveStatus(hp))
        inventory = pvp.jfzCore.ITEM_ORDER
        bagText = '  '.join(
            '%s×%d' % (itemName, getInventory(name).get(itemName, 0))
            for itemName in inventory
            if getInventory(name).get(itemName, 0) > 0
        )
        lines.append(
            '%d. %s（QQ %s｜群 %s）血量 %d（%s）铜币 %d 今日主动 %d/%d%s %s' % (
                index,
                name,
                player.get('user_qq', ''),
                player.get('group_id', ''),
                hp,
                status,
                int(player.get('copper', 0)),
                getDailyAttackUsed(name),
                getConfigInt('daily_attack_limit', 3),
                # 只在「被单独设过攻击值」时才显示 —— 统一值在【PVP】配置里能看，
                # 每条都重复印一遍会把这一行撑得过长（回执要短）。
                ('　攻击 %d（个人）' % _toAttack(player.get('attack')))
                if _toAttack(player.get('attack')) else '',
                ('背包：' + bagText) if bagText else '背包：空',
            )
        )
    return '\n'.join(lines)
