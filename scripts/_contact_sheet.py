# -*- coding: utf-8 -*-
"""从预览 mp4 抽关键帧拼成对比图，直观展示 讲解↔手势 的交叉淡化过渡。"""
import os
import sys

import cv2
import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "preview_gesture_transition.mp4")
OUT = os.path.join(ROOT, "preview_contact_sheet.png")

# (帧号, 说明)：覆盖 纯讲解 / 淡入中 / 纯手势 / 淡出中
picks = [
    (20, "talk 讲解"), (38, "淡入 talk→nod"), (60, "nod 点头"), (92, "淡出 nod→talk"),
    (140, "shake 摇头"), (200, "shrug 摊手"), (300, "point 指点"), (350, "talk 讲解"),
]

cap = cv2.VideoCapture(SRC)
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
grabbed = {}
want = {p[0] for p in picks}
i = 0
while True:
    ok, frame = cap.read()
    if not ok:
        break
    if i in want:
        grabbed[i] = frame
    i += 1
cap.release()

th, tw = 360, 640  # 每格缩放尺寸
cells = []
for idx, label in picks:
    f = grabbed.get(idx)
    if f is None:
        f = np.zeros((th, tw, 3), np.uint8)
    f = cv2.resize(f, (tw, th))
    cv2.putText(f, f"#{idx} {label}", (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 5, cv2.LINE_AA)
    cv2.putText(f, f"#{idx} {label}", (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2, cv2.LINE_AA)
    cells.append(f)

row1 = np.hstack(cells[:4])
row2 = np.hstack(cells[4:])
sheet = np.vstack([row1, row2])
cv2.imwrite(OUT, sheet)
print(f"total frames={total}  已保存对比图: {OUT}  size={sheet.shape[1]}x{sheet.shape[0]}")
