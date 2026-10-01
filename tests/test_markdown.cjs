const {test}=require('node:test');
const assert=require('node:assert/strict');
const {render}=require('../fusion/static/markdown.js');

test('candidate report renders real table, inline code, bold and numeric alignment',()=>{
  const html=render('| 候选 | 验证 ID | 校准 | 结论 |\n|---|:---:|---:|---|\n| `stop_atr 1.5→1.4` | abc123 | **-6.68%** | `approved=false` |');
  assert.match(html,/<table class="markdown-table">/);
  assert.equal((html.match(/<th /g)||[]).length,4);
  assert.match(html,/<td class="align-right"><strong>-6.68%<\/strong>/);
  assert.match(html,/<code>approved=false<\/code>/);
});
test('optional edge pipes, missing cells and escaped/code pipes',()=>{
  const html=render('Name | Result\n--- | ---\n`a|b` | x\\|y\nshort |\nnext | good | extra');
  assert.match(html,/<code>a\|b<\/code>/);assert.match(html,/>x\|y<\/td>/);
  assert.equal((html.match(/<td /g)||[]).length,6);
  assert.doesNotMatch(html,/extra/);
});
test('plain pipes or malformed separators do not become tables',()=>{
  assert.doesNotMatch(render('foo | bar\nnot a separator\ntext'),/<table/);
  assert.doesNotMatch(render('| A | B |\n|---|\n|1|2|'),/<table/);
});
test('table content cannot inject HTML, links, images or attributes',()=>{
  const html=render('| <img src=x onerror=alert(1)> | B |\n|---|---|\n| <script>alert(1)</script> | [x](javascript:alert(1)) |');
  assert.doesNotMatch(html,/<script|<img|<a\b|onerror="/);
  assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img/);
});
test('fenced samples stay code and headings/lists around tables remain intact',()=>{
  const html=render('## 报告\n- A\n\n| a | b |\n|---|---|\n|1|2|\n\n```text\n| a | b |\n|---|---|\n<script>\n```\n\n### 结论\n保持');
  assert.equal((html.match(/<table /g)||[]).length,1);
  assert.match(html,/<ul><li>A<\/li><\/ul>/);
  assert.match(html,/<pre><code>\| a \| b \|/);
  assert.match(html,/<h3>结论<\/h3><p>保持<\/p>/);
});
