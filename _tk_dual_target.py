# -*- coding: utf-8 -*-
"""书库 v2 同名消歧自测靶 —— ScreenPilot

两个同名「确定」按钮（书库 v1 的 (name,type) 键会把它们压成一条，v2 空间桶
消歧后并存为两候选）。点击后各自文本变「左已点N/右已点N」（OCR 读画面新文本
可验业务生效，避开 tk UIA 桥 Name 缓存旧名坑）。

用法：python _tk_dual_target.py  （阻塞运行；测试脚本用 Popen 拉起并 terminate）
"""
import tkinter as tk

root = tk.Tk()
root.title("书库v2消歧靶")
root.geometry("520x200+900+400")
tk.Label(root, text="双同名按钮消歧靶", font=("微软雅黑", 12)).pack(pady=14)

行 = tk.Frame(root)
行.pack()


def 造按钮(名字):
    计数 = {"n": 0}
    b = tk.Button(行, text="确定", width=10, font=("微软雅黑", 12))

    def 点击():
        计数["n"] += 1
        b.config(text="%s已点%d" % (名字, 计数["n"]))

    b.config(command=点击)
    b.pack(side="left", padx=36)


造按钮("左")
造按钮("右")
root.mainloop()
