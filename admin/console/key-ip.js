window.WBKeyIP = (() => {
  let keyId = 0, days = 1, sortKey = 'requests', direction = 'desc', offset = 0;
  let generation = 0, busy = false, rows = [], active = [], devices = [], timer = null;
  const labels = {ip:'IP 地址',requests:'调用次数',failed:'失败',credits:'积分',tokens:'Token',last_seen:'最近调用'};
  const number = value => Number(value || 0).toLocaleString('zh-CN');
  const json = (method, body) => ({method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const visible = element => element && element === $('#keyIPContent') && !$('#modal').classList.contains('hidden');
  const modelChips = (models, liveOnly) => {
    const list = (models || []).filter(m => m.model);
    if (!list.length) return '<span class="text-xs" style="color:var(--fg-subtle)">—</span>';
    return `<div class="flex flex-wrap gap-1">${list.map(m => {
      const live = liveOnly ? (m.live_requests || 0) : (m.live_requests || m.live ? 1 : 0);
      const tone = live ? 'badge bg-emerald-100 text-emerald-700' : (m.recent ? 'badge bg-sky-100 text-sky-700' : 'badge bg-slate-100 text-slate-500');
      const count = live ? `×${number(m.live_requests)}` : (m.requests ? `·${number(m.requests)}` : '');
      return `<span class="${tone} font-mono" title="${live ? '正在使用' : (m.recent ? '15 分钟内用过' : '窗口内用过')} · ${number(m.tokens || 0)} Token${m.last_seen ? ' · 最近 ' + esc(fmtTime(m.last_seen)) : ''}">${live ? '<span class="inline-block w-1.5 h-1.5 rounded-full mr-1" style="background:currentColor"></span>' : ''}${esc(m.model)}${count ? `<span class="opacity-70"> ${count}</span>` : ''}</span>`;
    }).join('')}</div>`;
  };
  async function request(path, options) {
    const result = await safeJson(await api(path, options));
    if (!result.ok) throw new Error(typeof result.data.detail === 'string' ? result.data.detail : '请求失败（HTTP '+result.status+'）');
    return result.data;
  }
  async function open(id) {
    if (busy) return;
    keyId = Number(id); days = 1; sortKey = 'requests'; direction = 'desc'; offset = 0;
    clearTimeout(timer); rows = [];
    const key = _keyData.find(item => item.id === keyId);
    openModal(`<h3>来源 IP · ${esc(key?.name || ('Key #'+keyId))}</h3>
      <div class="key-ip-toolbar">
        <div class="key-ip-range"><span class="text-xs" style="color:var(--fg-muted)">时间范围</span>${uiSelectHTML('keyIPDays',{options:WBIP.ranges,value:'1',onChange:value=>WBKeyIP.range(value)})}</div>
        <button class="console-btn quiet" onclick="WBKeyIP.load()" title="刷新 IP 统计"><i class="fa-solid fa-rotate"></i>刷新</button>
      </div><div id="keyIPContent"><div id="keyIPSummary" class="stat-grid mb-3" role="status"></div>
      <p class="panel-note">并发为当前服务进程的在途请求，包含排队与流式请求；每 5 秒刷新。</p>
      <h4 class="mt-4 mb-2">当前在途</h4><div class="card overflow-x-auto"><table class="tbl min-w"><thead><tr><th>IP 地址</th><th>当前并发</th><th>使用模型</th><th>最早开始</th><th>状态</th><th>操作</th></tr></thead><tbody id="keyIPActive"></tbody></table></div>
      <h4 class="mt-4 mb-2">调用记录</h4><div class="card overflow-x-auto"><table class="tbl min-w"><thead><tr>${Object.entries(labels).map(([field,label])=>`<th data-key-ip-sort="${field}"><button onclick="WBKeyIP.sort('${field}')">${label}</button></th>`).join('')}<th>使用模型</th><th>当前并发</th><th>状态</th><th>操作</th></tr></thead><tbody id="keyIPRows"></tbody></table></div>
      <div id="keyIPPager" class="flex flex-wrap justify-between items-center gap-2 mt-3"></div>
      <h4 class="mt-4 mb-2">关联设备标识</h4><p class="panel-note">以下均由客户端上报，未经硬件验证；封禁相同标识可跨 IP 生效。未上报或重置标识时无法可靠关联。</p><div id="keyIPDevices"></div></div>`);
    initUISelects([['keyIPDays','时间范围']]);
    await load();
  }
  async function load() {
    clearTimeout(timer);
    if (busy) return;
    const content = $('#keyIPContent'), version = ++generation;
    if (!visible(content)) return;
    if (!rows.length) $('#keyIPSummary').textContent = '正在加载…';
    try {
      const query = new URLSearchParams({days,sort:sortKey,direction,offset,limit:100});
      const data = await request('/api/keys/'+keyId+'/ips?'+query);
      if (version !== generation || !visible(content)) return;
      if (offset > 0 && offset >= data.total_ips) {offset = 0;return await load();}
      rows = data.items || []; active = data.active || []; devices = data.devices || [];
      $('#keyIPSummary').innerHTML = statCard('来源 IP',number(data.total_ips))+statCard('调用次数',number(data.total_requests))+statCard('当前活跃 IP',number(data.active_ips))+statCard('当前并发',number(data.active_requests),{tone:'brand'});
      $('#keyIPDevices').innerHTML = WBIP.deviceHTML(devices,'WBKeyIP');
      const action = (row, index, live) => `<button class="console-btn quiet" onclick="WBKeyIP.toggle(${index},${live})">${row.blocked?'解除封禁':'封禁'}</button>`;
      $('#keyIPActive').innerHTML = active.map((row,i)=>`<tr><td><code>${esc(row.ip)}</code></td><td>${WBIP.concurrentTag(row.active_requests)}</td><td>${modelChips(row.models,true)}</td><td>${esc(fmtTime(row.first_started_at))}</td><td>${WBIP.statusTag(row.blocked)}</td><td>${action(row,i,true)}</td></tr>`).join('') || '<tr><td colspan="6">暂无在途请求</td></tr>';
      $('#keyIPRows').innerHTML = rows.map((row,i)=>`<tr><td><code>${esc(row.ip)}</code></td><td>${number(row.requests)}</td><td>${number(row.failed)}</td><td>${number(row.credits)}</td><td>${number(row.tokens)}</td><td>${esc(fmtTime(row.last_seen))}</td><td>${modelChips(row.models)}</td><td>${WBIP.concurrentTag(row.active_requests)}</td><td>${WBIP.statusTag(row.blocked)}</td><td>${action(row,i,false)}</td></tr>`).join('') || '<tr><td colspan="10">所选时间内暂无调用记录</td></tr>';
      document.querySelectorAll('[data-key-ip-sort]').forEach(th => {
        const field = th.dataset.keyIpSort;
        th.setAttribute('aria-sort',field === sortKey ? (direction === 'desc'?'descending':'ascending'):'none');
        th.querySelector('button').textContent = labels[field]+(field === sortKey?(direction === 'desc'?' ↓':' ↑'):'');
      });
      $('#keyIPPager').innerHTML = pagerHtml(data.total_ips,Math.floor(offset/100)+1,Math.ceil(data.total_ips/100),'WBKeyIP.goPage');
    } catch(error) {
      if (version === generation && visible(content)) $('#keyIPSummary').textContent = '加载失败：'+error.message;
    } finally {
      if (version === generation && visible(content)) timer = setTimeout(()=>{if($('#confirmMask')?.classList.contains('show')) {timer=setTimeout(()=>load(),5000);} else load();},5000);
    }
  }
  async function toggle(index, live) {
    if (busy) return;
    const row = (live ? active : rows)[index], content = $('#keyIPContent'), version = generation;
    if (!row || !visible(content)) return;
    if (!row.blocked && !await uiConfirm({title:'封禁 IP',message:`确认封禁 ${row.ip}？该 IP 使用任何 Key 的新网关请求都会返回 403。`})) return;
    if (version !== generation || !visible(content)) return;
    busy = true;
    $('#modalBox').querySelectorAll('button,select').forEach(control => {control.dataset.keyIpDisabled = control.disabled?'1':'0';control.disabled=true;});
    try {
      if (row.blocked) await request('/api/ip/blocked/'+row.blocked_id,json('PATCH',{enabled:false}));
      else await request('/api/ip/blocked',json('POST',{ip:row.ip,note:'Key #'+keyId}));
      toast(row.blocked?'已解除封禁':'已封禁 IP');
    } catch(error) {toast(error.message,'err');}
    finally {
      busy = false;
      if (visible(content)) {
        $('#modalBox').querySelectorAll('[data-key-ip-disabled]').forEach(control=>{control.disabled=control.dataset.keyIpDisabled==='1';delete control.dataset.keyIpDisabled;});
        await load();
      }
    }
  }
  return {open,load,toggle,
    async toggleDevice(index){const content=$('#keyIPContent');if(await WBIP.changeDevice(devices[index]) && visible(content))load();},
    goPage(page){if(!busy){offset=Math.max(0,(page-1)*100);load();}},
    range(value){if(!busy){days=Number(value);offset=0;load();}},
    sort(field){if(!busy && field in labels){direction=field===sortKey && direction==='desc'?'asc':'desc';sortKey=field;offset=0;load();}},
    page(delta){if(!busy){offset=Math.max(0,offset+delta*100);load();}}};
})();
