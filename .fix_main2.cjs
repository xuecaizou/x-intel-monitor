// 修复 main.js 182-184 的损坏字符串（真实换行 → \n 转义），按行重构
const fs = require('fs');
const p = 'editor/src/main.js';
let lines = fs.readFileSync(p, 'utf8').split('\n');
// 找 "if (kind === 'code') { insertAround('" 所在行，把后续直到含 "```" 结尾的损坏块重写
const i = lines.findIndex((l) => l.includes("if (kind === 'code') { insertAround('"));
if (i < 0) { console.log('code line not found'); process.exit(1); }
// 当前行只包含前半段；完整重写 code 与 table 两个分支
// 定位 table 分支行
const j = lines.findIndex((l) => l.includes("insertAround('") && l.includes('| 列1 | 列2 |'));
const BS = String.fromCharCode(92);
const fixCode = "    if (kind === 'code') { insertAround('" + BS + "n```" + BS + "n', '" + BS + "n```" + BS + "n'); return; }";
let fixTable = null;
if (j > 0) {
  fixTable = "    if (kind === 'table') { insertAround('" + BS + "n| 列1 | 列2 |" + BS + "n| --- | --- |"
    + BS + "n|  |  |" + BS + "n', ''); return; }";
}
// 重写区间 [i, j>0? j : i]
const start = i;
const end = j > i ? j : i;
const replacement = fixTable ? [fixCode, fixTable] : [fixCode];
lines.splice(start, end - start + 1, ...replacement);
fs.writeFileSync(p, lines.join('\n'));
// 校验
try {
  execSyncCheck();
  console.log('fixed OK');
} catch (e) { console.log('STILL BROKEN'); process.exit(1); }
function execSyncCheck() {
  require('child_process').execSync('node --check editor/src/main.js', { stdio: 'inherit' });
}
