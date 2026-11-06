# -*- coding: utf-8 -*-
"""时间维度四件套 自造靶 —— ScreenPilot（2026-11-01 B1）

四个能力各带一个真值病灶（POC 靶思路）：
  - toast：点「保存」→ 「✔ 已保存」显示 1.2s 后消失；saved_n 真值写状态文件
  - 一隐一显：Entry 输入 -> 回显 Label 同步内容（输入即显示，真值 entry 落盘）
  - ESC 卡死：「假死」按钮点击后主线程 sleep(5)（真冻结，写 freeze_start/end）
  - 抠底分层：Canvas 双动画——横向移动方块（实体，净位移大）+ 原位呼吸块
    （动画层，位置固定只变色）+ 多行静态文字（UI 层，OCR 保留率判据用）

布局纪律：全部 place 绝对定位，不赌 pack 布局。
控件屏幕 bbox 由靶自己上报到状态文件 layout（winfo_rootx 物理像素——
本进程先设 DPI 感知，避免逻辑/物理像素错位）。

状态文件 _tk_watch_state.json：原子写（tmp+replace），测试脚本轮询。
用法：python _tk_watch_target.py（阻塞；自测脚本 Popen 拉起后 taskkill）
"""
import json
import os
import sys
import time

这里 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, 这里)

from sp import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()

import tkinter as tk  # noqa: E402

状态文件 = os.path.join(这里, "_tk_watch_state.json")
状态 = {"saved_n": 0, "freeze_start": None, "freeze_end": None,
        "entry": "", "layout": {}}


def 写状态():
    tmp = 状态文件 + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(状态, f, ensure_ascii=False)
    os.replace(tmp, 状态文件)


根 = tk.Tk()
根.title("识屏watch靶")
根.geometry("780x600+420+120")
根.attributes("-topmost", True)
根.configure(bg="#f4f5f7")

标题 = tk.Label(根, text="ScreenPilot · 时间维度自测靶",
                font=("微软雅黑", 16), bg="#f4f5f7")
标题.place(x=24, y=12)

# ---- toast（连拍窗靶）----
toast = tk.Label(根, text="✔ 已保存", font=("微软雅黑", 15, "bold"),
                 bg="#2f6f4e", fg="white", padx=16, pady=8)
_toast定时 = [None]


def 显示toast():
    if _toast定时[0]:
        根.after_cancel(_toast定时[0])
    toast.place(x=300, y=62)
    _toast定时[0] = 根.after(1200, lambda: toast.place_forget())


计数标签 = tk.Label(根, text="点击: 0", font=("微软雅黑", 13), bg="#f4f5f7")
计数标签.place(x=330, y=74)


def 点保存():
    状态["saved_n"] += 1
    计数标签.config(text="点击: %d" % 状态["saved_n"])
    写状态()
    显示toast()


保存按钮 = tk.Button(根, text="保存", font=("微软雅黑", 13), width=10, command=点保存)
保存按钮.place(x=24, y=64, height=40)

# ---- 假死（卡死探测靶）----
假死按钮 = tk.Button(根, text="假死", font=("微软雅黑", 13), width=10)


def 点假死():
    状态["freeze_start"] = time.time()
    状态["freeze_end"] = None
    写状态()
    根.update_idletasks()   # 先把「按下」渲染出去，基线不含点击前后差
    time.sleep(5)           # 主线程冻结：窗口彻底不响应消息
    状态["freeze_end"] = time.time()
    写状态()


假死按钮.config(command=点假死)
假死按钮.place(x=180, y=64, height=40)

# ---- ESC 响应（健康对照组）----
esc标签 = tk.Label(根, text="ESC已收", font=("微软雅黑", 15, "bold"),
                  bg="#2f6f4e", fg="white", padx=14, pady=6)
_esc定时 = [None]


def 收esc(_事件=None):
    if _esc定时[0]:
        根.after_cancel(_esc定时[0])
    esc标签.place(x=520, y=62)
    _esc定时[0] = 根.after(1500, lambda: esc标签.place_forget())
    return "break"


根.bind("<Escape>", 收esc)

# ---- 一隐一显（打字验证靶）----
输入变量 = tk.StringVar()
输入框 = tk.Entry(根, font=("微软雅黑", 15), width=46, textvariable=输入变量)
输入框.place(x=24, y=130, height=36)
回显 = tk.Label(根, text="回显: ", font=("微软雅黑", 15), anchor="w",
               bg="#ffffff", relief="sunken")
回显.place(x=24, y=180, width=640, height=38)


def 回显更新(*_):
    值 = 输入变量.get()
    状态["entry"] = 值
    写状态()
    回显.config(text="回显: " + 值)


输入变量.trace_add("write", 回显更新)

# ---- 抠底分层靶：静止 UI 文字 + 移动实体 + 原位呼吸动画 ----
画布 = tk.Canvas(根, bg="#1d2430", highlightthickness=0)
画布.place(x=24, y=236, width=716, height=330)
画布.create_text(30, 26, text="ScreenPilot", fill="#e8eef7",
                 font=("微软雅黑", 18), anchor="w")
画布.create_text(30, 62, text="状态面板", fill="#9fb3cc",
                 font=("微软雅黑", 16), anchor="w")
画布.create_text(30, 98, text="攻击 123  防御 456", fill="#e8eef7",
                 font=("微软雅黑", 17), anchor="w")
画布.create_text(30, 134, text="生命 7890 / 9999", fill="#e8eef7",
                 font=("微软雅黑", 17), anchor="w")
画布.create_text(30, 170, text="静止层等于界面", fill="#a9bbd1",
                 font=("微软雅黑", 15), anchor="w")

移动块 = 画布.create_rectangle(60, 214, 104, 258, fill="#e0b25c", outline="")
_移动x = [60]
呼吸块 = 画布.create_rectangle(580, 200, 650, 270, fill="#5aa9e6", outline="")
_呼吸态 = [0]


def 移动():
    _移动x[0] += 4
    if _移动x[0] > 600:
        _移动x[0] = 60
    画布.coords(移动块, _移动x[0], 214, _移动x[0] + 44, 258)
    根.after(100, 移动)


def 呼吸():
    _呼吸态[0] = 1 - _呼吸态[0]
    画布.itemconfig(呼吸块, fill="#5aa9e6" if _呼吸态[0] else "#12395e")
    根.after(250, 呼吸)


根.after(100, 移动)
根.after(250, 呼吸)


def _框(w):
    return [int(w.winfo_rootx()), int(w.winfo_rooty()),
            int(w.winfo_rootx() + w.winfo_width()),
            int(w.winfo_rooty() + w.winfo_height())]


def 上报布局():
    状态["layout"] = {
        "save": _框(保存按钮),
        "freeze": _框(假死按钮),
        "entry": _框(输入框),
        "echo": _框(回显),
        "title": _框(标题),
        "canvas": _框(画布),
    }
    写状态()


根.after(500, 上报布局)
根.after(900, 上报布局)   # 二次上报防首帧尺寸未定

try:
    根.mainloop()
finally:
    try:
        os.remove(状态文件)
    except Exception:
        pass
