# -*- coding: utf-8 -*-
"""识屏系统自测靶窗口 —— ScreenPilot

tkinter 自造靶：替代 notepad 靶子（Win11 记事本有会话恢复坑，会污染用户已开
标签）。本靶可控、无系统副作用，测试完 taskkill 即清理。

按钮带按下计数（点击后标题变「自测按钮×N」），供漂移点击等用例做业务闭环验证。

用法：python tk_target.py  （阻塞运行；自测脚本用 Popen 拉起）
"""
import tkinter as tk

root = tk.Tk()
root.title("识屏自测靶")
root.geometry("420x200+1000+300")
tk.Label(root, text="ScreenPilot 自测靶", font=("微软雅黑", 12)).pack(pady=12)
输入框 = tk.Entry(root, width=38, font=("微软雅黑", 12))
输入框.pack(pady=8)

_count = {"n": 0}

def _点过():
    _count["n"] += 1
    按钮.config(text="自测按钮×%d" % _count["n"])

按钮 = tk.Button(root, text="自测按钮", width=12, command=_点过)
按钮.pack(pady=8)
root.mainloop()
