# -*- coding: utf-8 -*-
"""
调试工具（不是测试的一部分，平时不用跑）。

用途：当 run_tests.py 在「掷骰记账」这一段出问题时，本脚本把测试执行到
「双方声明完毕 → 进入掷骰阶段」为止，然后手动发一次 .ra99，
逐关打印 jfzDice.onMsgHook 的判定过程，直接指出是第几关把这次掷骰拦下的。

为什么会需要它：钩子对**全服所有掷骰**都会触发，所以内部有一串「不满足就静默
return」的关卡（不是个人群 / 不是本人 / 不在掷骰阶段 / 记录读不到 / 技能值不符…）。
静默 return 是正确设计（不能对无关掷骰乱回复），但出问题时现象只有
「掷了骰但没计分」，从外面看不出是哪一关。这个脚本把那串判据拆开逐条打印。

运行：python3 debug_gate.py
"""
import os
import sys
import io

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(TEST_DIR, 'run_tests.py')

sys.path.insert(0, os.path.join(TEST_DIR, 'stub'))
with io.open(SRC, encoding='utf-8') as f:
    lines = f.readlines()

# 截到「# --- 防作弊：掷骰值与要求不符 ---」之前
cut = None
for i, ln in enumerate(lines):
    if '防作弊：掷骰值与要求不符' in ln:
        cut = i
        break
assert cut is not None, '没找到截断点'

head = ''.join(lines[:cut])

ns = {'__name__': '__main__'}
exec(compile(head, SRC, 'exec'), ns)

C = ns['C']
pvp = ns['pvp']
OlivaDiceCore = ns['OlivaDiceCore']
gm = ns['gm']
BOT_HASH = ns['BOT_HASH']
P1 = ns['P1']

print('\n' + '=' * 70)
print('截断点状态：')
m = pvp.jfzStore.getMatch()
print('  stage       =', m.get('stage'))
print('  rounds      =', m.get('rounds'))
print('  round_plan  =', m.get('round_plan'))
print('  rolls       =', m.get('rolls'))
print('  张三 user_qq =', pvp.jfzStore.getPlayer('张三').get('user_qq'))
print('  张三 group  =', pvp.jfzStore.getPlayer('张三').get('group_id'))
print('=' * 70)


# --- 插桩：把 onMsgHook 的每一关拆开打印 ---
D = pvp.jfzDice


def probe(event, funcType, sender, dectData, message):
    print('\n--- 钩子被调用 ---')
    print('  funcType =', funcType)
    print('  dectData =', dectData)

    if funcType != 'reply':
        print('  ✗ 第1关：不是 reply'); return
    if not isinstance(dectData, (list, tuple)) or len(dectData) < 3:
        print('  ✗ 第2关：dectData 不合法'); return
    groupId = dectData[1]
    userId = dectData[2]
    botHash = str(event.bot_info.hash)
    print('  groupId=%s userId=%s botHash=%s' % (groupId, userId, botHash))

    key = (botHash, str(groupId), str(userId))
    print('  dictLastCommand 里有没有这个键 =', key in D.dictLastCommand)
    if key in D.dictLastCommand:
        print('     记下的指令 = %r' % D.dictLastCommand[key])
    lastCommand = D.dictLastCommand.pop(key, None)
    print('  第3关 isRaCommand(%r) = %s' % (lastCommand, C.isRaCommand(lastCommand)))
    if not C.isRaCommand(lastCommand):
        return

    pvp.jfzStore.reloadLight()
    playerName = pvp.jfzStore.getPlayerByGroup(groupId)
    print('  第4关 getPlayerByGroup(%s) = %r' % (groupId, playerName))
    if not playerName:
        return
    pd = pvp.jfzStore.getPlayer(playerName)
    print('     user_qq=%r, 传入 userId=%r' % (pd.get('user_qq'), userId))

    match = pvp.jfzStore.getMatch()
    print('  第5关 state=%r stage=%r' % (match.get('state'), match.get('stage')))
    print('     challenger=%r target=%r' % (match.get('challenger_name'), match.get('target_name')))

    rollData = D.readRollRecord(botHash, userId, event)
    print('  第6关 readRollRecord =', rollData)
    if rollData is None:
        return
    rolledValue, skillValue = rollData

    declaredSkill = C.parseRaSkillValue(lastCommand)
    print('  第6.5关 declaredSkill=%r skillValue=%r' % (declaredSkill, skillValue))

    rollIndex = len(D.getRolls(match, playerName))
    exp = pvp.jfzEngine.getExpectedRoundAttack(match, playerName, rollIndex)
    print('  第7关 rollIndex=%d expectedAttack=%r skillValue=%r' % (rollIndex, exp, skillValue))
    print('  → 全部通过，应记账')


D.onMsgHook = probe

_origRemember = D.rememberCommand
_origForget = D.forgetCommand


def spyRemember(botHash, groupId, userId, commandText):
    print('  [remember] botHash=%r groupId=%r userId=%r text=%r' % (botHash, groupId, userId, commandText))
    return _origRemember(botHash, groupId, userId, commandText)


def spyForget(botHash, groupId, userId):
    print('  [forget]   botHash=%r groupId=%r userId=%r' % (botHash, groupId, userId))
    return _origForget(botHash, groupId, userId)


D.rememberCommand = spyRemember
D.forgetCommand = spyForget

_pn = pvp.jfzStore.getPlayerByGroup('1001')
_pd = pvp.jfzStore.getPlayer(_pn) or {}
print('  [state] getPlayerByGroup("1001") = %r' % _pn)
print('  [state] player.user_qq = %r (type %s)' % (_pd.get('user_qq'), type(_pd.get('user_qq')).__name__))
print('  [state] 比较 user_qq %s "10001" → %s' % (
    _pd.get('user_qq'),
    str(_pd.get('user_qq', '')) != str('10001'),
))

print('\n>>> 先单独发一次 gm(".ra99")，看 rememberCommand 有没有生效')
ev = ns['gm']('.ra99', P1['group'], P1['qq'], P1['name'])
print('gm 之后 dictLastCommand =', D.dictLastCommand)

print('\n>>> 再手动触发钩子')
ns['clearLogs']()
OlivaDiceCore.onediceOverride.setRDRecord(BOT_HASH, P1['qq'], 'qq', 1, 99)
hook = OlivaDiceCore.crossHook.dictHookFunc['msgHook']
hook(ev, 'reply', {'name': '骰子', 'id': '10001'}, [None, P1['group'], P1['qq']], '掷骰结果（桩）')
print('\n掷骰后 rolls =', pvp.jfzStore.getMatch().get('rolls'))
