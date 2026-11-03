# -*- coding: utf-8 -*-
"""F2 端到端靶窗 v2：专名按钮行，每钮点击自改文本（触发自身区域变化矩形，
膨胀核 9+外扩 16 裹住相邻按钮 → 增量裁块走快速实例重扫专名行）。
用法：python _tk_f2_proper.py（由驱动脚本 Popen 拉起）
"""
import tkinter as tk


def main():
    r = tk.Tk()
    r.title("F2专名靶")
    r.geometry("520x120+60+60")
    r.configure(bg="white")
    条 = tk.Frame(r, bg="white")
    条.pack(pady=8)
    词s = ["max", "min", "Settings", "Search", "Volume", "Home", "Tools", "User"]

    def 改(w, btn):
        btn.config(text=w + "*")

    for w in 词s:
        b = tk.Button(条, text=w, font=("Segoe UI", 8), bg="white",
                      padx=1, pady=0)
        b.config(command=lambda ww=w, bb=b: 改(ww, bb))
        b.pack(side="left", padx=1)
    r.attributes("-topmost", True)
    r.mainloop()


if __name__ == "__main__":
    main()
