# -*- coding: utf-8 -*-
"""
复现「发起 PVP 后只回『排队中』就没有下文」的问题。
完全模拟真实时序：默认限流、真实等待，不走测试里那些"放开限制"的开关。
"""
import os
import sys
import time
import shutil

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(TEST_DIR, 'work_repro')
shutil.rmtree(WORK, ignore_errors=True)
os.makedirs(WORK)
os.chdir(WORK)
sys.path.insert(0, os.path.join(TEST_DIR, 'stub'))
sys.path.insert(0, os.path.dirname(TEST_DIR))

import OlivOS
import OlivaDiceCore
import pvp

MASTER_QQ = '10000'
ADMIN_QQ = '20000'
BOT_HASH = 'bot_hash_1'

BOT = OlivOS.FakeBotInfo(BOT_HASH)
Proc = OlivOS.shallow({BOT_HASH: BOT})
OlivOS.pluginAPI.gProc = Proc
OlivOS.resetOutbox()

pvp.main.Event.init(None, None)

OlivaDiceCore.ordinaryInviteManager.MASTER_LIST.add(
    OlivaDiceCore.userConfig.getUserHash(MASTER_QQ, 'user', 'qq')
)
pvp.main.Event.init_after(None, Proc)

# 保持默认限流（18 条/分钟），贴近真机
print('GROUP_LIMIT_PER_MINUTE =', pvp.jfzSend.GROUP_LIMIT_PER_MINUTE)


def gm(text, group, qq, name):
    ev = OlivOS.makeGroupEvent(text, group, qq, BOT, senderName=name)
    pvp.main.Event.group_message(ev, Proc)
    return OlivOS.repliesTo(group)


def sentTo(group):
    return OlivOS.outboxTo(group)


P1 = {'group': '111', 'qq': '111', 'name': '张三'}
P2 = {'group': '222', 'qq': '222', 'name': '李四'}

# 注册
gm('【PVP】注册后台群 尽伏诛', '999', MASTER_QQ, '骰主')
gm('【PVP】添加管理 %s' % ADMIN_QQ, '999', MASTER_QQ, '骰主')
today = time.strftime('%Y-%m-%d')
gm('【PVP】设置活动日期 %s %s 00:00 23:59' % (today, today), '999', ADMIN_QQ, '管理')
gm('【PVP】注册玩家 %s %s 张三' % (P1['group'], P1['qq']), '999', ADMIN_QQ, '管理')
gm('【PVP】注册玩家 %s %s 李四' % (P2['group'], P2['qq']), '999', ADMIN_QQ, '管理')

print()
print('========== 发起 PVP ==========')
gm('【尽伏诛】PVP\n发起人：张三\n被发起人：李四', P1['group'], P1['qq'], P1['name'])
print()
print('--- 张三群收到的（会话回复） ---')
for r in OlivOS.repliesTo(P1['group']):
    print(repr(r))
print('--- 李四群收到的（会话回复） ---')
for r in OlivOS.repliesTo(P2['group']):
    print(repr(r))

# 等发送队列的跨群消息真正落地
deadline = time.time() + 8
while time.time() < deadline:
    if pvp.jfzSend.sendQueue.empty():
        break
    time.sleep(0.1)
time.sleep(0.5)

print()
print('--- 张三群收到的（跨群推送） ---')
for r in OlivOS.outboxTo(P1['group']):
    print(repr(r))
print('--- 李四群收到的（跨群推送） ---')
for r in OlivOS.outboxTo(P2['group']):
    print(repr(r))

m = pvp.jfzStore.getMatch()
print()
print('--- 对局状态 ---')
print('state =', m.get('state'), ' stage =', m.get('stage'))
print('challenger =', m.get('challenger_name'), ' target =', m.get('target_name'))
print('queue =', pvp.jfzStore.getQueue())
print()
print('--- 声明状态 ---')
print('declared =', m.get('declared'))
print('rolls =', m.get('rolls'))
