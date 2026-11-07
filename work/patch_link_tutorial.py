p = "README.md"
t = open(p, encoding="utf-8").read()
anchor = "> 只想看结论、不想读全文 → **[docs/verdict.md](docs/verdict.md)**（一页）。"
add = (
    anchor
    + "\n>\n> **第一次接触 GitHub / 完全新手** → "
      "**[docs/新手教程-GitHub编译内核.md](docs/新手教程-GitHub编译内核.md)**"
      "（手把手：注册、上传、编译、刷机、救砖）"
)
if anchor in t and "新手教程" not in t:
    t = t.replace(anchor, add, 1)
    open(p, "w", encoding="utf-8", newline="\n").write(t)
    print("README 已加新手教程入口")
else:
    print("未改动（锚点未命中或已存在）")
