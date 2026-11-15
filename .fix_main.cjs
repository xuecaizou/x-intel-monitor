// 修复 main.js 中被 shell 吃掉的 \n 转义（一次性脚本）
const fs = require('fs');
const p = 'editor/src/main.js';
let s = fs.readFileSync(p, 'utf8');
const NL = String.fromCharCode(10);
const BS = String.fromCharCode(92);
// 损坏形态：insertAround('<NL>```<NL>', '<NL>```<NL>') 与 table 同理
const pairs = [
  ["insertAround('" + NL + "```" + NL + "', '" + NL + "```" + NL + "')",
   "insertAround('" + BS + "n```" + BS + "n', '" + BS + "n```" + BS + "n')"],
  ["insertAround('" + NL + "| 列1 | 列2 |" + NL + "| --- | --- |" + NL + "|  |  |" + NL + "', '')",
   "insertAround('" + BS + "n| 列1 | 列2 |" + BS + "n| --- | --- |" + BS + "n|  |  |" + BS + "n', '')"],
];
let n = 0;
for (const [bad, good] of pairs) {
  while (s.includes(bad)) { s = s.replace(bad, good); n++; }
}
fs.writeFileSync(p, s);
console.log('fixed pairs:', n);
// 校验
try { new (require('module').Module)(); } catch (e) {}
const { execSync } = require('child_process');
try {
  execSync('node --check editor/src/main.js', { stdio: 'inherit' });
  console.log('JS OK');
} catch (e) { console.log('STILL BROKEN'); process.exit(1); }
