/**
 * 把 docs/答辩要点_源文本.txt 转成 A4 可打印 Word 文档。
 *
 * 源文本格式约定（每行一个元素）：
 *   # 一级标题（第一个当作文档大标题，其余每个另起一页）
 *   ## 二级标题
 *   ### 题目行（加粗 + 浅底纹）
 *   - 正文条目（缩进）
 *   其它行 = 普通正文
 *   行内 **xxx** = 加粗
 *   空行 = 仅用于源文件分隔，不输出（间距由段距控制）
 *
 * 分页策略：一个题目（### + 其下全部条目）整体不跨页；
 *          段内不断行（keepLines）；条目块与下一题之间允许分页。
 *
 * 用法：node build.js
 */
const fs = require('fs');
const path = require('path');
const {
  Document, Packer, Paragraph, TextRun, AlignmentType,
  BorderStyle, ShadingType, Footer, PageNumber,
} = require('docx');

const SRC = path.join(__dirname, '..', '答辩要点_源文本.txt');
const OUT = path.join(__dirname, '..', '答辩要点_A4打印版.docx');

const FONT = '微软雅黑';
const NAVY = '1F3864';
const GREY = '595959';
const SZ_BODY = 20;   // 10pt
const SZ_TITLE = 36;  // 18pt
const SZ_H1 = 30;     // 15pt
const SZ_H2 = 24;     // 12pt
const SZ_H3 = 21;     // 10.5pt
const LINE = 288;     // 1.2 倍行距

function mkRuns(text, opts = {}) {
  const size = opts.size || SZ_BODY;
  const segs = String(text).split('**');
  const out = [];
  segs.forEach((s, i) => {
    if (s === '') return;
    out.push(new TextRun({
      text: s,
      font: FONT,
      size,
      color: opts.color,
      bold: opts.bold || (i % 2 === 1),
    }));
  });
  if (out.length === 0) out.push(new TextRun({ text: ' ', font: FONT, size }));
  return out;
}

const rawLines = fs.readFileSync(SRC, 'utf8').split(/\r?\n/);

function nextNonEmpty(fromIdx) {
  for (let j = fromIdx + 1; j < rawLines.length; j++) {
    const t = rawLines[j].trim();
    if (t !== '') return t;
  }
  return '';
}

const children = [];
let titleDone = false;
let firstSection = true;
let pendingSub = false;

rawLines.forEach((raw, idx) => {
  const line = raw.replace(/[ \t]+$/, '');
  if (line.trim() === '') return; // 空行不输出

  const next = nextNonEmpty(idx);
  const nextIsHeading = next.startsWith('#');

  // ---- 题目行：必须与下面的内容同页 ----
  if (line.startsWith('### ')) {
    children.push(new Paragraph({
      keepNext: true,
      keepLines: true,
      spacing: { before: 240, after: 70, line: LINE },
      shading: { fill: 'EEF3FA', type: ShadingType.CLEAR },
      indent: { left: 60, right: 60 },
      children: mkRuns(line.slice(4).trim(), { size: SZ_H3, bold: true }),
    }));
    return;
  }

  // ---- 二级标题：避免孤行标题 ----
  if (line.startsWith('## ')) {
    children.push(new Paragraph({
      keepNext: true,
      keepLines: true,
      spacing: { before: 240, after: 110, line: LINE },
      children: mkRuns(line.slice(3).trim(), { size: SZ_H2, bold: true, color: NAVY }),
    }));
    return;
  }

  // ---- 一级标题 ----
  if (line.startsWith('# ')) {
    const t = line.slice(2).trim();
    if (!titleDone) {
      titleDone = true;
      pendingSub = true;
      children.push(new Paragraph({
        keepNext: true,
        keepLines: true,
        alignment: AlignmentType.CENTER,
        spacing: { before: 0, after: 80 },
        children: mkRuns(t, { size: SZ_TITLE, bold: true, color: NAVY }),
      }));
      return;
    }
    const breakBefore = !firstSection;
    firstSection = false;
    children.push(new Paragraph({
      pageBreakBefore: breakBefore,
      keepLines: true,
      spacing: { before: breakBefore ? 0 : 300, after: 170, line: LINE },
      border: { bottom: { style: BorderStyle.SINGLE, size: 10, color: NAVY, space: 6 } },
      children: mkRuns(t, { size: SZ_H1, bold: true, color: NAVY }),
    }));
    return;
  }

  // ---- 正文条目：与下一条目同页；若下一行是新标题则允许分页 ----
  const keepNext = !nextIsHeading;

  if (line.startsWith('- ')) {
    children.push(new Paragraph({
      keepNext,
      keepLines: true,
      indent: { left: 320 },
      spacing: { after: 40, line: LINE },
      children: mkRuns(line.slice(2).trim()),
    }));
    return;
  }

  if (pendingSub) {
    pendingSub = false;
    children.push(new Paragraph({
      keepNext: true,
      keepLines: true,
      alignment: AlignmentType.CENTER,
      spacing: { after: 180, line: LINE },
      children: mkRuns(line.trim(), { size: 18, color: GREY }),
    }));
    return;
  }

  children.push(new Paragraph({
    keepNext,
    keepLines: true,
    spacing: { after: 40, line: LINE },
    children: mkRuns(line.trim()),
  }));
});

const doc = new Document({
  styles: { default: { document: { run: { font: FONT, size: SZ_BODY } } } },
  sections: [{
    properties: {
      page: {
        size: { width: 11906, height: 16838 },                          // A4
        margin: { top: 1134, right: 1134, bottom: 1134, left: 1134 },   // 2cm
      },
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({ text: '第 ', font: FONT, size: 16, color: GREY }),
            new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 16, color: GREY }),
            new TextRun({ text: ' 页', font: FONT, size: 16, color: GREY }),
          ],
        })],
      }),
    },
    children,
  }],
});

Packer.toBuffer(doc).then((buf) => {
  try {
    fs.writeFileSync(OUT, buf);
    console.log('生成成功：' + OUT);
  } catch (e) {
    if (e.code === 'EBUSY' || e.code === 'EPERM') {
      const alt = path.join(path.dirname(OUT), path.basename(OUT, '.docx') + '_v2.docx');
      fs.writeFileSync(alt, buf);
      console.log('原文件被占用（可能正在 Word 中打开），已改为生成：' + alt);
    } else {
      throw e;
    }
  }
  console.log('段落数：' + children.length);
}).catch((e) => {
  console.error(e);
  process.exit(1);
});
