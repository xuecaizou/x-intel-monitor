# -*- coding: utf-8 -*-
"""生成中文 Word 样式参考（app/assets/reference.docx）。

用法：py -3 scripts/make_reference_docx.py [pandoc路径]
流程：pandoc 默认 reference.docx → 重写 styles.xml 的东亚字体（正文宋体/标题黑体）。
业务写作模板与 Word 排版样式分离：本文件只控制字体/纸张等排版，不含业务内容。
样式版本变更时同步 app/wordexport.py 的 STYLE_VERSION。
"""
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "app" / "assets"


def main():
    pandoc = sys.argv[1] if len(sys.argv) > 1 else "pandoc"
    ASSETS.mkdir(parents=True, exist_ok=True)
    base = ASSETS / "_reference_base.docx"
    with open(base, "wb") as f:
        r = subprocess.run([pandoc, "--print-default-data-file", "reference.docx"],
                           stdout=f, timeout=60)
    if r.returncode != 0 or not base.exists() or base.stat().st_size < 1000:
        print("pandoc 默认 reference.docx 获取失败")
        sys.exit(1)

    src = zipfile.ZipFile(base)
    styles = src.read("word/styles.xml").decode("utf-8")

    heading_ids = re.compile(r'w:styleId="(Heading\d|Title|Subtitle|TOCHeading)"')
    # 逐个 style 块处理：标题类加 eastAsia=黑体，其余加 eastAsia=宋体
    blocks = re.split(r"(<w:style )", styles)
    out_parts = [blocks[0]]
    for i in range(1, len(blocks), 2):
        chunk = blocks[i] + blocks[i + 1]
        m = re.search(r'w:styleId="([^"]+)"', chunk)
        ea = "黑体" if (m and heading_ids.search(chunk)) else "宋体"
        if "<w:rFonts" in chunk:
            if "w:eastAsia=" not in chunk:
                chunk = chunk.replace("<w:rFonts ", f'<w:rFonts w:eastAsia="{ea}" ', 1)
        else:
            # 无 rFonts 的样式：给 rPr 补一个（多数样式有 rPr；没有就跳过，不影响正文）
            chunk = re.sub(r"(<w:rPr>)",
                           rf'\1<w:rFonts w:eastAsia="{ea}"/>',
                           chunk, count=1)
        out_parts.append(chunk)
    styles_new = "".join(out_parts)

    dst = ASSETS / "reference.docx"
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for item in src.namelist():
            if item == "word/styles.xml":
                z.writestr(item, styles_new)
            else:
                z.writestr(item, src.read(item))
    src.close()
    base.unlink()
    # 校验
    z = zipfile.ZipFile(dst)
    assert "word/document.xml" in z.namelist()
    s = z.read("word/styles.xml").decode("utf-8")
    n_body = s.count('w:eastAsia="宋体"')
    n_head = s.count('w:eastAsia="黑体"')
    print(f"OK {dst}  宋体×{n_body} 黑体×{n_head}")


if __name__ == "__main__":
    main()
