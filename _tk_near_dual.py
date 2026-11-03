# -*- coding: utf-8 -*-
"""书库 v2.1 近距同名消歧自测靶 —— ScreenPilot

第二单「书库收尾」施工二真靶：两个同名「确定」按钮**垂直相距 ~60px**
（顶部距，<150px 量级）。窗口做高（客户区 ~1000px）让 1/8 空间桶高 ≈129px
——两按钮落**同一空间桶**，v2 同键更新会把它们压成一条；桶内多候选
（路线 B）后并存为两候选。

布局坑（首轮实测）：tk pack 在窗口高度不足时会把后排按钮**压扁到 8px 高**
（OCR 读不出、UIA bbox 畸形）——改 place 绝对定位钉死坐标；tk UIA 桥
Name 常暴露为空，按钮文本靠 OCR 读画面并入（fuse 步骤1）。

点击后各自文本变「上已点N/下已点N」（OCR 读画面新文本验业务生效，避开
tk UIA 桥 Name 缓存旧名坑）。
用法：python _tk_near_dual.py  （阻塞运行；测试脚本用 Popen 拉起并 terminate）
"""
import tkinter as tk

root = tk.Tk()
root.title("书库近距消歧靶")
root.geometry("800x1000+560+30")
tk.Label(root, text="近距同名按钮消歧靶（顶部距60px）", font=("微软雅黑", 12)
         ).place(x=280, y=500)


def 造按钮(名字, y):
    计数 = {"n": 0}
    b = tk.Button(root, text="确定", width=10, font=("微软雅黑", 12))

    def 点击():
        计数["n"] += 1
        b.config(text="%s已点%d" % (名字, 计数["n"]))

    b.config(command=点击)
    b.place(x=350, y=y)  # 绝对定位钉死：顶部 y=10/70（距60px，同桶不重叠）


造按钮("上", 10)
造按钮("下", 70)
root.mainloop()
