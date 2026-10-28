/* IP access controls. Dynamic values are escaped; mutations disable controls. */
window.WBIP = (() => {
  let sortKey = 'requests', direction = 'desc', offset = 0, generation = 0;
  let activity = [], bans = [], currentIP = '', busy = false;
  const labels = {ip:'IP 地址',requests:'请求次数',failed:'失败次数',blocked_requests:'封禁拦截',credits:'积分',tokens:'Token',last_seen:'最近请求'};
  const e = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = value => Number(value || 0).toLocaleString('zh-CN');
  const ranges = [1,7,30,90].map(value=>({value:String(value),label:value===1?'近 24 小时':`近 ${value} 天`}));
  const statusTag = blocked => `<span class="badge ${blocked?'bg-red-100 text-red-700':'bg-emerald-100 text-emerald-700'}">${blocked?'已封禁':'允许'}</span>`;
  const banStatusTag = enabled => `<span class="badge ${enabled?'bg-red-100 text-red-700':'bg-slate-100 text-slate-500'}">${enabled?'已封禁':'已解除'}</span>`;
  const concurrentTag = value => `<span class="badge ${Number(value)>0?'bg-indigo-100 text-indigo-700':'bg-slate-100 text-slate-500'}" title="当前服务进程的在途请求，包含排队与流式请求">并发 ${num(value)}</span>`;
  function ipTooltip(row) {
    const info = row.client_details || {};
    const list = values => values?.length ? values.join(', ') : '未上报';
    const ua = info.user_agent || '';
    const client = /claude/i.test(ua) ? 'Claude 客户端' : /codex/i.test(ua) ? 'Codex 客户端'
      : /openai/i.test(ua) ? 'OpenAI SDK' : /anthropic/i.test(ua) ? 'Anthropic SDK'
      : /curl/i.test(ua) ? 'curl' : /python-httpx/i.test(ua) ? 'Python HTTPX'
      : /python-requests/i.test(ua) ? 'Python Requests' : /mozilla/i.test(ua) ? '浏览器' : '未知';
    return [`客户端（按 UA 识别）：${client}`, `UA：${ua || '未记录（更新后新请求开始采集）'}`,
      `设备 ID：${list(info.device_ids)}`, `指纹：${list(info.fingerprints)}`, `MAC：${list(info.macs)}`,
      'UA 和设备标识由客户端上报，可被修改；服务端无法跨公网获取网卡 MAC。',
      '最近已认证请求 · 每类设备标识最多显示 3 个 · 点击 IP 查看用量'].join('\n');
  }
  let devices = [], deviceBusy = false;
  function deviceHTML(items, scope) {
    const names = {device_id:'设备 ID',fingerprint:'客户端指纹',mac:'MAC',browser:'浏览器指纹'};
    return items.length ? `<div class="ip-device-list">${items.map((row,i)=>`<article class="card ip-device-item"><div class="flex flex-wrap justify-between items-start gap-2"><div class="min-w-0"><div class="text-xs text-slate-400">${e(names[row.kind]||row.kind)}</div><code>${e(row.value)}</code></div>${statusTag(row.blocked)}</div><div class="ip-device-meta"><span class="badge bg-amber-100 text-amber-700">客户端上报 · 未验证</span>${row.ips.map(ip=>`<span class="badge bg-slate-100 text-slate-500">${e(ip)}</span>`).join('')}</div><div class="mt-3"><button class="console-btn quiet" onclick="${scope}.toggleDevice(${i})">${row.blocked?'解除设备封禁':'封禁设备标识'}</button></div></article>`).join('')}</div>` : '<div class="card p-4 text-center text-slate-400">暂无客户端上报的设备标识</div>';
  }
  async function changeDevice(row) {
    if (!row || deviceBusy) return false;
    if (!row.blocked && !await uiConfirm({title:'封禁设备标识',message:`确认封禁 ${row.value}？携带这个上报标识的请求换 IP 后仍返回 403。该标识未经硬件验证。`})) return false;
    deviceBusy = true;
    try {await request('/api/ip/devices',json('POST',{kind:row.kind,value:row.value,blocked:!row.blocked,note:row.note||''}));toast(row.blocked?'已解除设备封禁':'已封禁设备标识');return true;}
    catch(error){toast(error.message,'err');return false;}
    finally {deviceBusy=false;}
  }
  async function request(path, options) {
    const r = await safeJson(await api(path, options));
    if (!r.ok) throw new Error(typeof r.data.detail === 'string' ? r.data.detail : '请求失败（HTTP '+r.status+'）');
    return r.data;
  }
  const json = (method, body) => ({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  function buttonsDisabled(value) {
    document.querySelectorAll('#panel-ip button').forEach(button => {
      if (value) {
        if (!button.hasAttribute('data-ip-disabled')) button.dataset.ipDisabled = button.disabled ? '1' : '0';
        button.disabled = true;
      } else if (button.hasAttribute('data-ip-disabled')) {
        button.disabled = button.dataset.ipDisabled === '1';
        delete button.dataset.ipDisabled;
      }
    });
  }
  async function load() {
    if (!$('#ipActivityDays')) {
      $('#ipActivityDaysSlot').innerHTML = '<span>时间范围</span>'+uiSelectHTML('ipActivityDays',{options:ranges,value:'1',onChange:()=>WBIP.range()});
      initUISelects([['ipActivityDays','时间范围']]);
    }
    const version = ++generation;
    $('#ipActivitySummary').textContent = '正在加载…';
    try {
      const query = new URLSearchParams({days:uiSelectValue('ipActivityDays'),sort:sortKey,direction,offset,limit:100});
      query.set('min_requests',$('#ipUsageMin').value||'0');
      const usageFilter=$('#ipUsageFilter').value;
      $('#ipUsageMaxLabel').classList.toggle('hidden',!usageFilter);
      $('#ipUsageMaxText').textContent=usageFilter==='credits'?'平均积分 ≤':'平均 Token ≤';
      if(usageFilter)query.set(usageFilter==='tokens'?'max_avg_tokens':'max_avg_credits',$('#ipUsageMax').value||'0');
      if($('#ipActivityIP').value.trim())query.set('ip',$('#ipActivityIP').value.trim());
      const [stats, list, deviceList] = await Promise.all([request('/api/ip/activity?'+query),request('/api/ip/blocked'),request('/api/ip/devices')]);
      if (version !== generation) return;
      if (offset >= stats.total_ips && offset > 0) {offset = 0; return load();}
      activity = stats.items; bans = list.items; currentIP = stats.current_ip;
      $('#ipActivitySummary').innerHTML = statCard('活跃 IP',num(stats.total_ips))+statCard('入口请求',num(stats.total_requests))+statCard('封禁 IP',num(stats.blocked_count),{tone:'danger'})+statCard('设备标识',num(deviceList.items?.length));
      devices = deviceList.items || [];
      $('#ipDevices').innerHTML = deviceHTML(devices,'WBIP');
      document.querySelectorAll('[data-ip-sort]').forEach(th => {
        const key = th.dataset.ipSort;
        th.setAttribute('aria-sort',key === sortKey ? (direction === 'desc' ? 'descending' : 'ascending') : 'none');
        th.querySelector('button').textContent = labels[key]+(key === sortKey ? (direction === 'desc'?' ↓':' ↑'):'');
      });
      $('#ipActivityBody').innerHTML = activity.length ? activity.map((row,i) => `<tr>
        <td><button type="button" class="console-btn quiet font-mono" data-usage-ip="${e(row.ip)}" data-tooltip="${e(row.ip)}" data-tooltip-description="${e(ipTooltip(row))}">${e(row.ip)}</button>${row.ip === currentIP ? '<small class="block text-slate-400">当前访问 IP</small>':''}</td>
        <td>${num(row.requests)}</td><td>${num(row.failed)}</td><td>${num(row.blocked_requests)}</td>
        <td>${num(row.credits)}<small class="block text-slate-400">均 ${row.avg_credits==null?'—':Number(row.avg_credits).toFixed(4)}</small></td>
        <td>${num(row.tokens)}<small class="block text-slate-400">均 ${row.avg_tokens==null?'—':num(row.avg_tokens)} · ${num(row.known_usage)} 次有用量</small></td>
        <td>${e(fmtTime(row.last_seen))}</td><td>${statusTag(row.blocked)}</td>
        <td><button type="button" class="console-btn quiet" onclick="WBIP.toggleActivity(${i})">${row.blocked?'解除封禁':'封禁'}</button></td></tr>`).join('') : '<tr><td colspan="9" class="text-center text-slate-400">所选时间内暂无请求</td></tr>';
      $('#ipActivityPager').innerHTML = pagerHtml(stats.total_ips,Math.floor(offset/100)+1,Math.ceil(stats.total_ips/100),'WBIP.goPage');
      $('#ipBlockedBody').innerHTML = bans.length ? bans.map((row,i) => `<tr><td><code>${e(row.ip)}</code></td><td>${e(row.note || '—')}</td><td>${banStatusTag(row.enabled)}</td><td>${e(fmtTime(row.updated_at))}</td><td><div class="flex gap-2"><button type="button" class="console-btn quiet" onclick="WBIP.toggleBan(${i})">${row.enabled?'解除封禁':'重新封禁'}</button><button type="button" class="console-btn quiet" onclick="WBIP.remove(${i})">删除记录</button></div></td></tr>`).join('') : '<tr><td colspan="5" class="text-center text-slate-400">暂无封禁记录</td></tr>';
      if (busy) buttonsDisabled(true);
    } catch (error) {
      if (version !== generation) return;
      $('#ipActivitySummary').textContent = '加载失败：'+error.message;
      toast(error.message,'err');
    }
  }
  async function mutate(action, message) {
    if (busy) return;
    busy = true; buttonsDisabled(true);
    try {await action();toast(message);await load();}
    catch(error) {toast(error.message,'err');}
    finally {busy = false;buttonsDisabled(false);}
  }
  async function confirmBlock(ip) {
    return uiConfirm({title:'封禁 IP',message:`确认封禁 ${ip}？该 IP 的所有网关请求将返回 403。${ip === currentIP?'这是当前访问 IP，封禁后也会影响你从此 IP 发起的 API 请求。':''}`});
  }
  async function add() {
    const ip = $('#ipBlockAddress').value.trim(), note = $('#ipBlockNote').value.trim();
    if (!ip || busy || !await confirmBlock(ip)) return;
    await mutate(async () => {await request('/api/ip/blocked',json('POST',{ip,note}));$('#ipBlockForm').reset();},'已封禁 IP');
  }
  async function toggleActivity(index) {
    const row = activity[index];
    if (!row || busy) return;
    if (row.blocked) await mutate(()=>request('/api/ip/blocked/'+row.blocked_id,json('PATCH',{enabled:false})),'已解除封禁');
    else if (await confirmBlock(row.ip)) await mutate(()=>request('/api/ip/blocked',json('POST',{ip:row.ip,note:''})),'已封禁 IP');
  }
  async function toggleBan(index) {
    const row = bans[index];
    if (!row || busy || (!row.enabled && !await confirmBlock(row.ip))) return;
    await mutate(()=>request('/api/ip/blocked/'+row.id,json('PATCH',{enabled:!row.enabled})),row.enabled?'已解除封禁':'已重新封禁');
  }
  async function remove(index) {
    const row = bans[index];
    if (!row || busy || !await uiConfirm({title:'删除封禁记录',message:`确认删除 ${row.ip} 的封禁记录？删除后该 IP 将允许访问网关。`})) return;
    await mutate(()=>request('/api/ip/blocked/'+row.id,{method:'DELETE'}),'已删除封禁记录');
  }
  return {load,add,toggleActivity,toggleBan,remove,ranges,statusTag,banStatusTag,concurrentTag,deviceHTML,changeDevice,
    usageFilter(){
      const mode=$('#ipUsageFilter').value;
      $('#ipUsageMaxLabel').classList.toggle('hidden',!mode);
      $('#ipUsageMaxText').textContent=mode==='credits'?'平均积分 ≤':'平均 Token ≤';
      $('#ipUsageMin').value=mode?'10':'0';$('#ipUsageMax').value=mode==='credits'?'0.01':'100';
      offset=0;load();
    },
    async toggleDevice(index){if(await changeDevice(devices[index]))load();},
    goPage(page){offset=Math.max(0,(page-1)*100);load();},
    range(){offset=0;load();},
    sort(key){direction=key===sortKey && direction==='desc'?'asc':'desc';sortKey=key;offset=0;load();},
    page(delta){offset=Math.max(0,offset+delta*100);load();}};
})();
