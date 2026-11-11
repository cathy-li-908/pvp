# -*- encoding: utf-8 -*-
'''
pvp —— 【尽伏诛】PVP 的事件入口。

================================================================
【这个文件为什么这么短】
================================================================
青果把一个框架事件广播给所有插件，每个插件按 app.json 的 priority
从大到小依次收到。本插件 priority=19990，比 OlivaDiceCore 的 20000 小，
所以：**同一条群消息，本插件先拿到，OlivaDiceCore 后拿到**。

这一点是整套设计的地基 —— 骰子钩子需要在「青果准备回复掷骰结果」时
知道「玩家上一条发的是什么」，而钩子触发时是拿不到原消息的。
靠的就是本插件先一步把待核对文本记下来（见 jfzDice.rememberCommand）。

================================================================
【为什么只实现了 4 个事件】
================================================================
OlivOS 的插件事件有一二十个，本插件只用到：

  init           包被 import 后立刻调用 → 建目录、读配置
  init_after     所有插件都 import 完之后调用（此时 OlivaDiceCore 就绪）
  group_message  群消息入口
  save           插件被重载/卸载前调用

不实现 private_message：本插件所有玩法都绑定在「个人群」上
（每个玩家有一个自己的 QQ 群），私聊里既没有群上下文、也没有对方的群，
做不了任何事，直接不处理。
'''

import OlivOS  # NOQA: F401
import OlivaDiceCore  # NOQA: F401

import pvp

# 插件托盘进程对象（OlivaDiceCore 里叫 Proc）。
# 青果的「主动发消息」接口需要一个事件上下文 + Proc.log，
# 而跨群推送是在后台线程里做的，拿不到当前事件，只能在这里存一份全局。
# 在 init_after 里赋值 —— 那时所有插件都初始化完，Proc 才完全可用。
gProc = None

# 主动发消息时要填一个「来源插件名」，青果用它做日志归类。
# 填插件名（namespace），这样在青果日志里能按 pvp 检索到本插件发出的消息。
gPluginName = 'pvp'


class Event(object):
    def init(plugin_event, Proc):
        """
        包被 import 后立刻调用。

        只做「不依赖别的插件」的事：建数据目录、把配置和玩家表读进内存。
        这里**不能**碰 OlivaDiceCore 的骰子相关对象 —— 它自己的 init 还没跑。
        """
        pvp.jfzStore.initStorage()
        pvp.jfzStore.reload()

    def init_after(plugin_event, Proc):
        """
        所有插件都 import 完之后调用。

        到这一步 OlivaDiceCore 已经完全就绪，可以做这三件依赖它的事情：

          1. 保存 Proc —— 后台发送线程要用它构造主动消息事件
          2. 挂骰子钩子 —— 读 `.ra` 的掷骰记录（RDRecordInt / RDRecordSkillInt）
          3. 注入自定义回复词与帮助条目

        另外还要启动两个后台线程：发送队列、活动时段看守。
        """
        global gProc
        gProc = Proc

        # 把回复词与帮助文档注入 OlivaDiceCore（让玩家能用骰子的 .help 查到本插件）
        pvp.msgCustomManager.initMsgCustom(Proc.Proc_data.get('bot_info_dict', {}))

        # 挂骰子钩子。内部会保存并链式调用原钩子，不会顶掉别的插件。
        pvp.jfzDice.installHook()

        # 启动跨群发送队列（按群限速，防止青果超频直接丢消息）
        pvp.jfzSend.startSender()

        # 启动活动时段看守：轮询检查「当日开放时段是否已结束」，
        # 到点要把所有等待中/进行中的对局直接取消并按规则返还道具。
        pvp.jfzEngine.startActivityWatcher()

        # 机器人重启后，把上次未结束的对局恢复出来（或按配置安全结束）
        pvp.jfzEngine.recoverAfterRestart()

    def group_message(plugin_event, Proc):
        """群消息入口。所有指令、PVP 发起格式、`.ra` 记录都在这里处理。"""
        try:
            pvp.jfzReply.unity_reply(plugin_event, Proc)
        except Exception:
            import traceback

            print('[pvp] 群消息处理异常：')
            traceback.print_exc()

    def save(plugin_event, Proc):
        """
        插件即将被重载/卸载时调用。

        本插件的每个写操作都会立刻落盘（见 jfzStore.saveXXX），
        这里没有需要补做的存储动作。保留这个事件是为了
        「重载前尽量把发送队列里还没发出去的消息发完」。
        """
        pvp.jfzSend.flush()

    def menu(plugin_event, Proc):
        """插件菜单按钮。本插件没有菜单项。"""
        pass
