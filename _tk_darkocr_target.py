"""F4 验收 MCP 实弹靶：深底全自绘 Canvas 窗（UIA 贫瘠，无文字节点）。
中央孤立大数字 7（det 单字符盲→OCR 兜底读出），点「切换」变 0，
供 expect 判文本 appear('0') 实弹——修复前 det 盲必假失败。"""
import tkinter as tk

root = tk.Tk()
root.title("深色兜底靶")
root.configure(bg="#161616")
root.geometry("420x360+80+140")
root.attributes("-topmost", True)

cv = tk.Canvas(root, width=420, height=360, bg="#161616", highlightthickness=0)
cv.pack()

state = {"n": "7"}
num = cv.create_text(210, 150, text="7", font=("Segoe UI", 96, "bold"), fill="#ffffff", tags="num")
btn = cv.create_text(210, 305, text="切换", font=("Segoe UI", 18), fill="#cccccc", tags="btn")


def 切换(_e):
    state["n"] = "0" if state["n"] == "7" else "7"
    cv.itemconfig(num, text=state["n"])


cv.tag_bind(btn, "<Button-1>", 切换)
cv.tag_bind(num, "<Button-1>", 切换)
root.mainloop()
