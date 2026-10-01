const {test}=require('node:test');
const assert=require('node:assert/strict');
const Hours=require('../fusion/static/trading-hours.js');

const sessions=[{name:'day',start_utc:0,end_utc:21,risk_scale:1},{name:'rollover',start_utc:21,end_utc:24,risk_scale:0}];
const profile=()=>({category:'workflow',id:'default',name:'My workflow',active:true,version:6,data:{session_start_utc:6,session_end_utc:21,block_weekends:true,module:'ea',mode:'demo',poll_seconds:12}});

test('Beijing hours round trip through UTC, including midnight and all day',()=>{
  const data=Hours.patch(14,5,false,true,8);
  assert.deepEqual(data,{session_start_utc:6,session_end_utc:21,block_weekends:true});
  assert.equal(Hours.windowLabel(data,8),'14:00–次日 05:00');
  assert.equal(Hours.windowLabel(data,0),'06:00–21:00');
  assert.deepEqual(Hours.patch(6,10,false,false,8),{session_start_utc:22,session_end_utc:2,block_weekends:false});
  assert.equal(Hours.windowLabel(Hours.patch(0,0,true,true,8),8),'全天 00:00–24:00');
  assert.throws(()=>Hours.patch(6,6,false,true,8),/全天/);
  assert.throws(()=>Hours.patch(6.5,21,false,true,8),/整点/);
  assert.throws(()=>Hours.patch(6,21,false,true,9),/UTC/);
});

test('cross-midnight window is start-inclusive and end-exclusive; zero risk is separate',()=>{
  const w=Hours.patch(22,2,false,false,0);
  for(const [stamp,allowed] of [['2026-10-01T21:59:59Z',false],['2026-10-01T22:00:00Z',true],['2026-10-02T01:59:59Z',true],['2026-10-02T02:00:00Z',false]]){
    assert.equal(Hours.gate(stamp,w,[{start_utc:0,end_utc:24,risk_scale:1}]).allowed,allowed,stamp);
  }
  const gate=Hours.gate('2026-10-01T22:00:00Z',Hours.patch(0,0,true,false,0),sessions);
  assert.equal(gate.workflowOpen,true);
  assert.equal(gate.allowed,false);
  assert.equal(gate.risk.name,'rollover');
  assert.equal(Hours.gate('2026-10-01T12:00:00Z',w,[]).risk,null);
});

test('weekends remain UTC, including Beijing Saturday morning',()=>{
  const w=Hours.patch(0,0,true,true,8),all=[{start_utc:0,end_utc:24,risk_scale:1}];
  for(const [stamp,allowed] of [['2026-10-03T07:59:59+08:00',true],['2026-10-03T08:00:00+08:00',false],['2026-10-05T07:59:59+08:00',false],['2026-10-05T08:00:00+08:00',true]]){
    assert.equal(Hours.gate(stamp,w,all).allowed,allowed,stamp);
  }
});

function harness(options={}){
  const original=profile(),changes=Hours.patch(6,10,false,true,8);
  const calls=[],stages=[];
  let active=structuredClone(original),phase='running',clock=0;
  const request=async(path,body,method='POST')=>{
    calls.push({path,body,method});
    if(path==='/api/bootstrap')return {configs:[structuredClone(options.staleBeforeStop&&phase==='running'?{...active,version:99}:active)]};
    if(path==='/api/status')return {positions:options.position?[{ticket:1}]:[],paper_positions:[],engine:{running:phase==='running'||!!options.neverIdle,armed:phase==='running'||(phase==='published'&&!!options.rearmed),busy:!!options.neverIdle,unresolved_orders:0}};
    if(path==='/api/ea/workspace')return {
      review:{busy:!!options.reviewBusy},parameter_export:phase==='published'?{revision:options.otherExport?333:222}:null,
      bridge:{connected:!options.disconnected,instance_id:options.reboot&&phase!=='running'?'boot-B':'boot-A',entries_enabled:phase==='running',positions:0,pending:false,command_revision:phase==='running'||options.noPauseAck?10:11,strategy_revision:phase==='published'&&!options.noConfigAck?222:111}
    };
    if(path==='/api/engine/stop'){phase='paused';if(options.staleAfterPause)active={...active,version:99};return {ok:true};}
    if(path==='/api/configs/workflow/default'&&method==='PUT'){
      active={...active,data:structuredClone(body.data),version:active.version+1};phase='saved';
      const response={...structuredClone(active),active:!options.savedInactive};
      if(options.staleAfterSave)active={...active,id:'different'};
      return response;
    }
    if(path==='/api/ea/parameters/export'){
      if(options.exportFailure)throw new Error('export unavailable');
      phase='published';if(options.staleBeforeAck)active={...active,version:99};return {status:'published',revision:222};
    }
    throw new Error(`unexpected request: ${method} ${path}`);
  };
  return {original,changes,calls,stages,run:()=>Hours.save({original,changes,request,onStage:(stage)=>stages.push(stage),now:()=>clock,wait:async ms=>{clock+=ms;},timeoutMs:1500})};
}
const mutations=h=>h.calls.filter(c=>c.method!=='GET');

test('save pauses and waits, preserves unrelated settings, publishes then confirms; never starts',async()=>{
  const h=harness();
  const result=await h.run();
  assert.equal(result.status,'applied');
  assert.deepEqual(mutations(h).map(c=>c.path),['/api/engine/stop','/api/configs/workflow/default','/api/ea/parameters/export']);
  const put=mutations(h)[1];
  assert.equal(put.method,'PUT');
  assert.equal(put.body.expected_version,6);
  assert.deepEqual(put.body.data,{...h.original.data,...h.changes});
  assert.equal(h.original.data.session_start_utc,6,'original profile must remain unchanged');
  assert.deepEqual(h.stages,['pausing','saving','saved','published','applied']);
});

test('exposure, missing EA or stale initial profile cause no mutations',async()=>{
  for(const option of [{position:true},{disconnected:true},{staleBeforeStop:true}]){
    const h=harness(option);await assert.rejects(h.run());assert.equal(mutations(h).length,0);
  }
});

test('idle without fresh pause acknowledgment, busy review, profile change or EA reboot never saves',async()=>{
  for(const option of [{noPauseAck:true},{neverIdle:true},{reviewBusy:true},{staleAfterPause:true},{reboot:true}]){
    const h=harness(option);await assert.rejects(h.run());
    assert.deepEqual(mutations(h).map(c=>c.path),['/api/engine/stop']);
  }
});

test('saved workflow with changed active profile is not exported or called applied',async()=>{
  for(const option of [{staleAfterSave:true},{savedInactive:true}]){
    const h=harness(option);
    await assert.rejects(h.run(),/已保存，应用未确认/);
    assert.equal(mutations(h).length,2);
    assert.equal(h.stages.includes('applied'),false);
  }
});

test('export failure or missing matching EA acknowledgment retain explicit partial outcome',async()=>{
  for(const option of [{exportFailure:true},{noConfigAck:true},{otherExport:true},{rearmed:true},{staleBeforeAck:true}]){
    const h=harness(option);await assert.rejects(h.run(),/已保存，应用未确认/);
    assert.equal(h.stages.includes('applied'),false);
    assert.equal(mutations(h).length,3,'must not retry export, roll back or start');
  }
});
