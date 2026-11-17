from xml.sax.saxutils import escape as E
W, H = 1240, 1350
out = []
ST = {"done": ("#e7f5ec", "#2f8f4e", ""), "part": ("#fff3d1", "#c98a00", ""), "todo": ("#f4f4f4", "#8a8a8a", ' stroke-dasharray="6 4"')}
TAG = {"必做": "#c0392b", "驗證": "#1f6feb", "待建": "#7d4cc2"}
def tw(s, size): return sum(size if ord(c) > 0x2e7f else size * 0.56 for c in s)
def text(x, y, s, size=13, weight="400", fill="#1f2328", anchor="start"):
    out.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{E(s)}</text>')
def box(x, y, w, h, st, rx=8):
    f, s, d = ST[st]
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{f}" stroke="{s}" stroke-width="1.5"{d}/>')
def chip(x, y, kind, num):
    s = f"{kind} #{num}" if num else kind
    w = tw(s, 11) + 14
    out.append(f'<rect x="{x}" y="{y - 13}" width="{w}" height="18" rx="9" fill="{TAG[kind]}"/>')
    text(x + 7, y, s, 11, "600", "#ffffff")
    return x + w + 6
def note(x, y, kind, num, s):
    nx = chip(x, y, kind, num)
    text(nx, y, s, 12.5, fill="#3b4148")
def line(pts, color="#57606a", dash=False, arrow=True, width=1.6):
    d = "M" + " L".join(f"{x},{y}" for x, y in pts)
    out.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}"' + (' stroke-dasharray="5 4"' if dash else "") +
               (f' marker-end="url(#a{color[1:]})"' if arrow else "") + "/>")
def band(y, h, title, sub, fill):
    out.append(f'<rect x="12" y="{y}" width="{W - 24}" height="{h}" rx="12" fill="{fill}" stroke="#d0d7de"/>')
    text(28, y + 24, title, 15, "700")
    text(28 + tw(title, 15) + 12, y + 24, sub, 12.5, fill="#57606a")

CX, CW, SH = 310, 390, 62
def stage(y, st, name, cmd, purpose):
    box(CX, y, CW, SH, st)
    text(CX + 14, y + 25, name, 15, "700")
    if cmd: text(CX + 14 + tw(name, 15) + 10, y + 25, cmd, 11.5, fill="#57606a")
    text(CX + 14, y + 47, purpose, 12.5, fill="#3b4148")

# ---------- 標題與圖例 ----------
text(24, 36, "研報知識庫 pipeline：各階段的目的與關係", 21, "700")
text(24, 58, "2026-11-17　由上往下是一篇研報走的路；左邊是被擋下或退回時去的地方；右邊是每個階段還掛著的待辦", 12.5, fill="#57606a")
x = 24
for st, lab in [("done", "已完成並測過"), ("part", "程式寫好，還沒對真實資料送出"), ("todo", "還沒做")]:
    box(x, 72, 22, 16, st, 4); text(x + 28, 85, lab, 12.5); x += 28 + tw(lab, 12.5) + 22
x += 10
for kind, lab in [("必做", "第一段：讓新批次能跑"), ("驗證", "第二段：用這 100 篇量出還不知道的數字"), ("待建", "第三段：全庫量產前要補齊")]:
    nx = chip(x, 85, kind, ""); text(nx, 85, lab, 12.5); x = nx + tw(lab, 12.5) + 20

# ---------- A 本機階段 ----------
band(104, 536, "本機階段", "不花錢，不把任何資料送出去（batch.py）", "#fafbfc")
A = [150 + 82 * i for i in range(6)]
stage(A[0], "done", "入庫", "ingest", "給每篇一個 id；檔案完全相同的只收一次")
stage(A[1], "done", "轉檔", "convert", "PDF → 文字（MinerU，用本機 GPU）")
stage(A[2], "done", "重複性檢查", "dedup", "內容相同但檔案不同的擋下（不看檔名，門檻 60%）")
stage(A[3], "done", "品質閘", "quality", "亂碼、掃描版沒字、超長的擋下，不浪費後面的錢")
stage(A[4], "done", "去固定頁", "clean", "刪目錄與免責聲明，每篇省約一成輸入費用")
stage(A[5], "done", "預算確認", "status", "送出前先估這批要花多少；超過預算就先停")
for a, b in zip(A, A[1:]): line([(CX + CW / 2, a + SH), (CX + CW / 2, b - 2)])
# hold
box(24, 300, 250, 140, "done")
text(38, 324, "hold：先擋下，通知你", 14, "700")
text(38, 347, "列出是哪一篇、為什麼被擋", 12.5, fill="#3b4148")
text(38, 369, "你確認沒問題 → release 放行，", 12.5, fill="#3b4148")
text(38, 387, "回到原階段繼續往下走", 12.5, fill="#3b4148")
text(38, 409, "確定有問題 → 留在 hold，", 12.5, fill="#3b4148")
text(38, 427, "不會進到花錢的階段", 12.5, fill="#3b4148")
line([(CX, A[0] + 31), (292, A[0] + 31), (292, 318), (276, 318)], "#b35900")
line([(CX, A[2] + 31), (276, A[2] + 31)], "#b35900")
line([(CX, A[3] + 31), (276, A[3] + 31)], "#b35900")
text(222, A[0] + 25, "快篩擋下", 11.5, fill="#b35900")
# 右側待辦
RX = 742
note(RX, A[0] + 36, "待建", 21, "新研報進來後自動跑本機階段")
note(RX, A[1] + 26, "必做", 5, "新批次實跑（這 100 篇正在轉）")
note(RX, A[1] + 48, "必做", 6, "改用 standard 全文後，寫卡的引文比對有沒有掉")
note(RX, A[2] + 36, "驗證", 11, "60% 門檻在真實資料上準不準")
note(RX, A[4] + 36, "驗證", 12, "新券商的研報有沒有被誤刪正文")
note(RX, A[5] + 36, "必做", 4, "預算上限、餘額不足整批停下")

# ---------- 交接 ----------
line([(CX + CW / 2, A[5] + SH), (CX + CW / 2, 698)])
text(CX + CW / 2 + 12, 654, "乾淨的全文 ＋ 花費估算　→　你決定送不送", 12.5, "600", "#3b4148")

# ---------- B 模型階段 ----------
band(660, 490, "模型階段", "送 API、會花錢（run.py）；寫卡用 Kimi，檢查用 DeepSeek，兩家不同才不會有相同的盲點", "#fafbfc")
B = [700, 782, 864, 946, 1066]
stage(B[0], "todo", "分流", "", "週報、點評只寫簡卡，不走後面的檢查（省錢）")
stage(B[1], "part", "寫卡", "Kimi", "全文 → 卡片；每個方法步驟都要附原文引文")
stage(B[2], "done", "程式硬檢查", "免費", "引文真的在全文裡？數字真的在同一列的引文裡？")
stage(B[3], "part", "模型檢查", "DeepSeek", "方法對象有沒有套錯、數字有沒有張冠李戴")
stage(B[4], "todo", "發布", "", "卡片進知識庫，開發策略時讓 agent 檢索")
for a, b in zip(B, B[1:]): line([(CX + CW / 2, a + SH), (CX + CW / 2, b - 2)])
text(CX + CW / 2 + 12, B[3] + SH + 34, "通過", 12.5, "600", "#2f8f4e")
# 重寫迴路
box(24, 800, 250, 200, "part")
text(38, 824, "重寫迴路（每道最多 1 次）", 14, "700")
text(38, 848, "程式檢查不過：", 12.5, "600", "#3b4148")
text(38, 866, "帶著錯誤清單重寫", 12.5, fill="#3b4148")
text(38, 892, "模型檢查退回：", 12.5, "600", "#3b4148")
text(38, 910, "帶著問題清單重寫，再複檢", 12.5, fill="#3b4148")
note(38, 942, "必做", 3, "迴路本身")
note(38, 968, "驗證", 7, "有多少重寫後還不過")
line([(CX, B[2] + 31), (276, B[2] + 31)], "#b35900"); text(282, B[2] + 24, "不過", 11.5, fill="#b35900")
line([(CX, B[3] + 31), (276, B[3] + 31)], "#b35900"); text(282, B[3] + 24, "退回", 11.5, fill="#b35900")
line([(274, B[1] + 31), (CX - 2, B[1] + 31)], "#2f8f4e"); text(280, B[1] + 24, "重寫", 11.5, fill="#2f8f4e")
box(24, 1044, 250, 78, "todo")
text(38, 1068, "升級裁決", 14, "700")
text(38, 1090, "交給 Opus 或你看原文決定", 12.5, fill="#3b4148")
note(38, 1112, "待建", 14, "")
line([(149, 1000), (149, 1042)], "#b35900"); text(157, 1026, "仍不過", 11.5, fill="#b35900")
# 右側待辦
note(RX, B[0] + 26, "驗證", 10, "這輪全部寫卡，用卡片的類型回頭訂分流規則")
note(RX, B[0] + 48, "待建", 13, "依規則實作分流")
note(RX, B[1] + 26, "必做", 1, "接上批次與 manifest")
note(RX, B[1] + 48, "待建", 18, "Kimi batch（六折，非同步）")
note(RX, B[3] + 14, "必做", 2, "接上批次與 manifest")
note(RX, B[3] + 36, "驗證", 9, "它判的 major 是真錯還是誤報")
note(RX, B[3] + 58, "待建", 19, "DeepSeek 排在離峰時段（半價）")
note(RX, B[4] + 14, "待建", 16, "渲染與目錄改成新卡片格式")
note(RX, B[4] + 36, "待建", 17, "檢索 Skill；卡片多了再測要不要語意搜尋")
note(RX, B[4] + 58, "待建", 20, "彙整新標籤建議，維護詞彙表")

# ---------- C 監控 ----------
band(1164, 150, "監控", "檢查員自己準不準？結果決定要不要換檢查模型", "#fafbfc")
box(110, 1206, 290, 92, "part")
text(124, 1230, "持續埋雷", 14, "700")
text(124, 1252, "在通過的卡上故意改錯一處再送檢，", 12.5, fill="#3b4148")
text(124, 1270, "算它抓到幾成", 12.5, fill="#3b4148")
note(124, 1290, "驗證", 8, "改成不用 Claude 產生")
box(420, 1206, 280, 92, "todo")
text(434, 1230, "抽樣審計", 14, "700")
text(434, 1252, "通過的卡抽幾張給另一個模型重審，", 12.5, fill="#3b4148")
text(434, 1270, "算檢查員漏掉幾成", 12.5, fill="#3b4148")
note(434, 1290, "待建", 15, "")
line([(255, 1206), (255, 1199), (720, 1199)], "#1f6feb", arrow=False)
line([(560, 1206), (560, 1199)], "#1f6feb", arrow=False)
line([(720, 1199), (720, B[3] + 31), (CX + CW + 2, B[3] + 31)], "#1f6feb")
text(1228, 1330, "狀態會變動，最新進度以 TODO.md 為準", 11.5, fill="#8a8a8a", anchor="end")

colors = ["57606a", "b35900", "2f8f4e", "1f6feb"]
defs = "".join(f'<marker id="a{c}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#{c}"/></marker>' for c in colors)
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'font-family="\'Noto Sans CJK TC\',\'Noto Sans TC\',\'PingFang TC\',\'Microsoft JhengHei\',\'Heiti TC\',sans-serif">'
       f'<defs>{defs}</defs><rect width="{W}" height="{H}" fill="#ffffff"/>' + "".join(out) + "</svg>")
open("/home/newt/quant-kb/docs/pipeline_overview.svg", "w").write(svg)
print(len(svg))
