# -*- encoding: utf-8 -*-
'''
实验：用真实 OlivOS 源码复刻 pvp 插件 _doSend 的发送路径，
截获最终进入 control_queue 的报文，看消息到底变成了什么。

不连任何服务器：sendControlEventSend 被打补丁截获。
'''
import sys
import traceback

OLIVOS_ROOT = '/private/tmp/olivos_dev'
sys.path.insert(0, OLIVOS_ROOT)
sys.path.insert(0, OLIVOS_ROOT + '/OlivOS/thirdPartyModule')

import OlivOS  # noqa

print('=== OlivOS 导入成功 ===')

# 1. 造一个 NapCat（onebot v11）账号
bot_info = OlivOS.API.bot_info_T(
    id=10000,
    platform_sdk='onebot',
    platform_platform='qq',
    platform_model='napcat',
    server_type='websocket',
)

# 2. 造 fake 事件（与 pvp/jfzSend._doSend 完全一致的参数）
fake = OlivOS.contentAPI.fake_sdk_event(
    bot_info=bot_info,
    fakename='pvp',
    plugin_namespace='pvp',
)

# 3. 构造插件事件（与 _doSend 一致：第二个参数传 Proc.log，这里传 None 等价）
plugin_event = OlivOS.API.Event(fake, None)

print('platform        =', plugin_event.platform)
print('message_mode_tx =', plugin_event.plugin_info.get('message_mode_tx'))
print('message_mode_rx =', plugin_event.plugin_info.get('message_mode_rx'))
print('control_queue   =', plugin_event.plugin_info.get('control_queue'))
print('func_type       =', plugin_event.plugin_info.get('func_type'))
print('data.type       =', type(plugin_event.data))

# 4. 打补丁截获最终报文
captured = []

def fake_sendControlEventSend(action, data, control_queue):
    captured.append((action, data, control_queue))

import OlivOS.adapter.onebotV11.onebotSDK as ob11
ob11.sendControlEventSend = fake_sendControlEventSend

# 5. 发送（两种 tx mode 各跑一次：
#    A. 不传 message_mode_tx —— 本机没有 gProc 时解析出的默认值
#    B. 传 'old_string' —— 真机上按 app.json 的 message_mode 解析出的值）
print('\n=== 调用 plugin_event.send("group", 12345, 文本) ===')
try:
    plugin_event.send('group', 12345, '【尽伏诛】测试消息：你好')
    print('send() 未抛异常')
except Exception:
    print('send() 抛异常：')
    traceback.print_exc()

print('\n=== 截获到 %d 个报文（A 场景） ===' % len(captured))
for action, data, cq in captured:
    print('action =', action)
    print('data   =', repr(data)[:800])
    print('control_queue =', cq)

# B 场景：真机上 app.json message_mode='old_string' 会注入 message_mode_tx
print('\n=== B 场景：message_mode_tx = old_string ===')
fake_b = OlivOS.contentAPI.fake_sdk_event(
    bot_info=bot_info,
    fakename='pvp',
    plugin_namespace='pvp',
    message_mode_tx='old_string',
)
plugin_event_b = OlivOS.API.Event(fake_b, None)
print('message_mode_tx =', plugin_event_b.plugin_info.get('message_mode_tx'))
try:
    plugin_event_b.send('group', 12345, '【尽伏诛】测试消息：你好')
    print('send() 未抛异常')
except Exception:
    print('send() 抛异常：')
    traceback.print_exc()

print('\n=== 截获到 %d 个报文 ===' % len(captured))
for action, data, cq in captured:
    print('action =', action)
    print('data   =', repr(data)[:800])
    print('control_queue =', cq)
