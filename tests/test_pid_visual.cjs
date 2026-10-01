const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {execFileSync}=require('node:child_process');
const repo=path.resolve(__dirname,'..');
const fragment=fs.readFileSync(path.join(repo,'experiments/pid-tick-envelope.html'),'utf8');
const modelSource=fragment.match(/<script id="pid-envelope-model">([\s\S]*?)<\/script>/)[1];
const context=vm.createContext({});vm.runInContext(modelSource,context);
const model=context.PIDEnvelopeDemo;

test('visual scripts parse and the fragment contains no network data calls or order access',()=>{
  for(const match of fragment.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new vm.Script(match[1]);
  assert.doesNotMatch(fragment,/\bfetch\s*\(|XMLHttpRequest|WebSocket|OrderSend|MetaTrader5/);
  assert.match(fragment,/合成行情 · 离线机制演示/);
});

test('browser numerical model agrees with Python on both sides, five paths and changed PID settings',()=>{
  const python=process.platform==='win32'?path.join(repo,'.venv/Scripts/python.exe'):path.join(repo,'.venv/bin/python');
  const source=`import json\nfrom fusion.pid_envelope import EnvelopeConfig,simulate\ncases=[]\nfor side in (-1,1):\n for scenario in ('normal','rise','fall','gap','offline'):\n  cases.append(({'side':side},scenario))\ncases += [({'kp':.05,'ki':.5,'kd':1,'upper_distance':.3},'normal'),({'kp':3,'ki':0,'kd':0,'ack_delay':3,'activation':1,'lock_profit':4},'fall')]\nresults=[]\nfor cfg,scene in cases:\n r=simulate(EnvelopeConfig(**cfg),scene)\n results.append({'patch':cfg,'scenario':scene,'events':r['events'],'fill':r['fill'],'rows':[r['rows'][i] for i in [0,100,200,300,400,500,600,694,695,696,697,700,750,850,1000,1099]]})\nprint(json.dumps(results,allow_nan=False))`;
  const expected=JSON.parse(execFileSync(python,['-c',source],{cwd:repo,encoding:'utf8'}));
  function close(a,b,where='root'){
    if(typeof b==='number'){assert.equal(typeof a,'number',where);assert.ok(Math.abs(a-b)<=1e-8,where+': '+a+' != '+b);return;}
    if(b===null||typeof b!=='object'){assert.equal(a,b,where);return;}
    assert.deepEqual(Object.keys(a).sort(),Object.keys(b).sort(),where);
    for(const k of Object.keys(b))close(a[k],b[k],where+'.'+k);
  }
  for(const value of expected){
    const got=model.simulate(value.patch,value.scenario);
    close(got.events,value.events,'events');close(got.fill,value.fill,'fill');
    close([0,100,200,300,400,500,600,694,695,696,697,700,750,850,1000,1099].map(i=>got.rows[i]),value.rows,'rows');
  }
});

test('browser controller resets after a disconnected period and still checks old lines first',()=>{
  const e=new model.Experiment();e.step(0,2505);e.pid.integral=5;e.pid.derivative=3;
  for(let i=1;i<=100;i++)e.step(i/10,2505,false);
  e.step(10.1,2505,true);assert.equal(e.pid.integral,0);assert.equal(e.pid.derivative,0);
  assert.equal(e.step(10.1,2507).state,'exit_pending');
});
