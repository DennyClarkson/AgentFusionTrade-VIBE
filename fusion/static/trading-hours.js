/* Trading-hour display and explicit, paused configuration publication. No auto-start. */
(function (root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.FusionTradingHours = value;
})(typeof window === 'undefined' ? globalThis : window, function () {
  'use strict';
  const mod = h => (h % 24 + 24) % 24;
  const clock = h => `${String(mod(h)).padStart(2, '0')}:00`;
  function hour(value) {
    if (!Number.isInteger(value) || value < 0 || value > 23) throw new Error('请选择 00:00–23:00 的整点时间');
    return value;
  }
  function zone(value) {
    if (value !== 0 && value !== 8) throw new Error('请选择 UTC 或北京时间');
    return value;
  }
  function patch(start, end, allDay, weekends, offset) {
    hour(start); hour(end); zone(offset);
    if (typeof allDay !== 'boolean' || typeof weekends !== 'boolean') throw new Error('交易时段选项无效');
    if (!allDay && start === end) throw new Error('开始和结束相同，请勾选“全天”或选择不同时间');
    return {session_start_utc: allDay ? 0 : mod(start-offset), session_end_utc: allDay ? 0 : mod(end-offset), block_weekends: weekends};
  }
  function windowLabel(workflow, offset) {
    zone(offset);
    const a=hour(workflow.session_start_utc), b=hour(workflow.session_end_utc);
    if (a===b) return '全天 00:00–24:00';
    const start=mod(a+offset), end=mod(b+offset);
    return `${clock(start)}–${end<start?'次日 ':''}${clock(end)}`;
  }
  function gate(now, workflow, sessions) {
    const d=new Date(now), h=d.getUTCHours(), day=d.getUTCDay();
    if (!Number.isFinite(d.getTime())) throw new Error('时间不可用');
    const a=hour(workflow.session_start_utc), b=hour(workflow.session_end_utc);
    const weekend=Boolean(workflow.block_weekends && (day===0||day===6));
    const inWindow=a===b || (a<b ? h>=a&&h<b : h>=a||h<b);
    const matches=sessions.filter(s=>s.start_utc<=h&&h<s.end_utc);
    const valid=matches.length===1 && Number.isFinite(matches[0].risk_scale);
    const risk=valid?matches[0]:null;
    return {weekend,inWindow,workflowOpen:inWindow&&!weekend,risk,allowed:inWindow&&!weekend&&valid&&risk.risk_scale>0};
  }
  const workflow = boot => boot.configs.find(r=>r.active&&r.category==='workflow');
  function sameProfile(actual, expected) {
    if (!actual || actual.id!==expected.id || actual.version!==expected.version) throw new Error('执行方案已被其他窗口修改，请刷新后重新设置');
  }
  function exposure(status, workspace) {
    if ((status.positions||[]).length || (status.paper_positions||[]).length || status.engine.unresolved_orders || workspace.bridge?.pending || workspace.bridge?.positions) {
      throw new Error('当前有持仓或未确认交易；请先处理后再修改时段');
    }
  }
  async function save({original, changes, request, onStage=()=>{}, wait=ms=>new Promise(r=>setTimeout(r,ms)), now=()=>Date.now(), timeoutMs=40000}) {
    hour(changes.session_start_utc); hour(changes.session_end_utc);
    if (typeof changes.block_weekends!=='boolean') throw new Error('周末设置无效');
    const fields=['session_start_utc','session_end_utc','block_weekends'];
    if (Object.keys(changes).some(k=>!fields.includes(k))) throw new Error('只允许修改交易时段');
    const get=path=>request(path,undefined,'GET');
    let saved=null;
    try {
      sameProfile(workflow(await get('/api/bootstrap')), original);
      const [before, native]=await Promise.all([get('/api/status'),get('/api/ea/workspace')]);
      exposure(before,native);
      if (!native.bridge?.connected) throw new Error('请先连接 FusionExecutor，再发布交易时段');
      const instance=native.bridge.instance_id, previousCommand=native.bridge.command_revision;
      onStage('pausing','正在暂停管理，等待本轮任务结束与 EA 暂停确认…');
      await request('/api/engine/stop',{});
      const deadline=now()+timeoutMs;
      let idle=false;
      while(now()<deadline) {
        const [status, w]=await Promise.all([get('/api/status'),get('/api/ea/workspace')]);
        exposure(status,w);
        if(w.bridge?.connected && w.bridge.instance_id!==instance) throw new Error('EA 实例已变化，请刷新后重试');
        if (!status.engine.running && !status.engine.armed && !status.engine.busy && !w.review?.busy && w.bridge?.connected && w.bridge.entries_enabled===false && w.bridge.command_revision>previousCommand) {idle=true;break;}
        await wait(500);
      }
      if(!idle) throw new Error('等待暂停确认超时；尚未修改时段，请等 AI 任务结束后重试');
      sameProfile(workflow(await get('/api/bootstrap')), original);
      onStage('saving','正在保存新的时段版本…');
      saved=await request(`/api/configs/workflow/${encodeURIComponent(original.id)}`,{name:original.name,data:{...original.data,...changes},expected_version:original.version},'PUT');
      onStage('saved','时段已保存，正在发布到 EA…');
      if (!saved.active) throw new Error('活动执行方案已切换');
      sameProfile(workflow(await get('/api/bootstrap')),saved);
      const published=await request('/api/ea/parameters/export',{});
      onStage('published','已发布，等待 EA 确认…');
      const ackDeadline=now()+timeoutMs;
      while(now()<ackDeadline) {
        const [status,w]=await Promise.all([get('/api/status'),get('/api/ea/workspace')]);
        exposure(status,w);
        if (status.engine.running || status.engine.armed) throw new Error('其他窗口已重新启动或解锁，请核对执行状态');
        if(w.bridge?.connected && w.bridge.instance_id!==instance) throw new Error('EA 实例已变化');
        if(w.bridge?.connected && w.bridge.entries_enabled===false && w.bridge.strategy_revision===published.revision && w.parameter_export?.revision===published.revision) {
          sameProfile(workflow(await get('/api/bootstrap')),saved);
          onStage('applied','EA 已确认新时段；管理已暂停，请在执行控制中解锁并重新启动。');
          return {saved,revision:published.revision,status:'applied'};
        }
        await wait(500);
      }
      throw new Error('尚未收到 EA 匹配版本回执');
    } catch(error) {
      if(saved) throw new Error(`时段已保存，应用未确认：${error.message}。请核对参数回执后再启动。`);
      throw error;
    }
  }
  return {patch,windowLabel,gate,save,clock,mod};
});
