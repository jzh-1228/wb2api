/* Bounded, on-demand request previews. Full payloads never enter the modal DOM. */
window.WBUsage = (() => {
  const previews = new Map();
  const ipCell = ip => ip && ip !== 'unknown'
    ? `<button type="button" class="console-btn quiet font-mono" data-usage-ip="${esc(ip)}" title="按此 IP 查询">${esc(ip)}</button><button type="button" class="console-btn quiet" data-copy-usage-ip="${esc(ip)}" aria-label="复制 IP">⧉</button>`
    : '<span style="color:var(--fg-subtle)">未记录</span>';

  document.addEventListener('click', event => {
    const filter = event.target.closest('[data-usage-ip]');
    if (filter) {
      $('#logIp').value = filter.dataset.usageIp;
      if ($('#panel-logs').classList.contains('hidden')) activateTab('logs');
      else loadLogs(1);
    }
    const copy = event.target.closest('[data-copy-usage-ip]');
    if (copy) {
      const input = document.createElement('textarea');
      input.id = 'usageIpCopy'; input.value = copy.dataset.copyUsageIp;
      input.style.cssText = 'position:fixed;left:-9999px'; document.body.appendChild(input);
      Promise.resolve(copyText('#usageIpCopy')).finally(() => input.remove());
    }
    const page = event.target.closest('[data-body-page]');
    if (page) {
      const detail = page.closest('[data-capture-part]'), state = previews.get(detail);
      const index = state && state.pageIndex + Number(page.dataset.bodyPage);
      if (state && index >= 0 && state.offsets[index] != null) readPage(detail, state.offsets[index], index);
    }
  });

  document.addEventListener('toggle', event => {
    const detail = event.target;
    if (detail.matches?.('[data-capture-root]') && !detail.open) {
      detail.querySelectorAll('[data-capture-part]').forEach(part => {part.open=false;releasePreview(part);});
    }
    if (!detail.matches?.('[data-capture-part]')) return;
    if (detail.open) readPage(detail, 0, 0);
    else releasePreview(detail);
  }, true);

  function releasePreview(detail) {
    previews.get(detail)?.controller?.abort();
    previews.delete(detail);
    const body=detail.querySelector('[data-body-text]');if(body)body.textContent='';
  }
  function closeDetail() { for(const detail of previews.keys())releasePreview(detail); }

  async function readPage(detail, offset, pageIndex) {
    let state=previews.get(detail);
    if(!state){state={offsets:[0],pageIndex:0};previews.set(detail,state);}
    state.controller?.abort();
    const controller=new AbortController();state.controller=controller;
    const body=detail.querySelector('[data-body-text]'), hint=detail.querySelector('[data-body-hint]');
    const buttons=detail.querySelectorAll('[data-body-page]');buttons.forEach(b=>b.disabled=true);
    hint.textContent='正在读取正文…';
    try {
      const params=new URLSearchParams({part:detail.dataset.capturePart,offset,limit:65536});
      const {ok,data}=await safeJson(await api(`/api/logs/${Number(detail.dataset.captureId)}/capture/body?${params}`,{signal:controller.signal}));
      if(controller.signal.aborted || !detail.isConnected || !detail.open)return;
      if(!ok)throw new Error(data.detail||'读取失败');
      state.pageIndex=pageIndex;
      if(data.has_more)state.offsets[pageIndex+1]=data.next_offset;
      body.textContent=data.text;
      hint.textContent=`第 ${pageIndex+1} 页 · ${data.cached_bytes ? offset+1 : 0}–${data.next_offset} / ${fmtNum(data.cached_bytes)} 字节${data.body_truncated?' · 采集时已截断':''}`;
      buttons[0].disabled=pageIndex===0;buttons[1].disabled=!data.has_more;
    } catch(error) {
      if(!controller.signal.aborted && detail.isConnected){body.textContent='';hint.textContent=String(error.message||error);}
    }
  }

  function bodyHtml(id, part, item, title, info='') {
    return `<details class="mt-2" data-capture-id="${Number(id)}" data-capture-part="${esc(part)}">
      <summary>${title} · ${fmtNum(item.body_bytes)} 字节${item.body_truncated?' · 已截断':''}</summary>
      ${info}<pre class="whitespace-pre-wrap break-all text-xs">${esc(JSON.stringify(item.headers, null, 2))}</pre>
      <pre data-body-text class="whitespace-pre-wrap break-all text-xs" style="max-height:360px;overflow:auto;overflow-wrap:anywhere;contain:content"></pre>
      <div class="flex flex-wrap items-center gap-2 mt-2"><span data-body-hint class="text-xs">展开后读取正文</span><button type="button" class="console-btn quiet" data-body-page="-1" disabled>上一页</button><button type="button" class="console-btn quiet" data-body-page="1" disabled>下一页</button></div>
    </details>`;
  }
  function payloadHtml(data) {
    const capture=data.request_capture;
    if(!capture)return '<p class="text-xs mt-3">这条记录没有可用的内存请求缓存。请打开“记录请求详情”后再发起请求；缓存重启、过期或容量淘汰后无法恢复。</p>';
    return `<details class="mt-3" data-capture-root><summary>请求头（凭据脱敏）和请求正文 · 按需读取</summary>
      <p class="text-xs mt-2">每页最多 64 KiB，展开对应请求后读取。下载完整 JSONL 可查看全部缓存内容。</p>
      ${bodyHtml(data.id,'ingress',capture.ingress,'入站请求')}
      <h4 class="text-xs font-semibold mt-3">实际发往上游（${capture.upstream.length} 次尝试）</h4>
      ${capture.upstream.map((item,index)=>bodyHtml(data.id,'upstream:'+index,item,`第 ${index+1} 次 · ${esc(item.method)} ${esc(item.url)} · HTTP ${esc(item.http_status||'未返回')}`,
        `<p class="text-xs">账号 #${esc(item.account_id??'—')} · ${item.dispatch_at?'开始发送：'+esc(item.dispatch_at):'未观察到发送请求头'}${item.error?' · '+esc(item.error):''} · 本地写入 ${fmtNum(item.upload?.written_bytes??0)} 字节</p>`)).join('')}
      ${capture.upstream_omitted?`<p class="text-xs">另有 ${capture.upstream_omitted} 次尝试因上限省略。</p>`:''}</details>`;
  }

  async function captureStatus() {
    try {
      const {ok,data}=await safeJson(await api('/api/logs/capture/settings'));
      if(!ok)throw new Error(data.detail||'读取失败');
      const toggle=$('#logCaptureEnabled');if(toggle){toggle.disabled=false;toggle.checked=!!data.enabled;}
      const hint=$('#logCaptureHint');if(hint)hint.textContent=`${data.enabled?'已开启':'已关闭'} · 缓存 ${data.cached_requests}/${data.max_entries} 条 · ${(data.cache_bytes/1024/1024).toFixed(1)} MiB · 单请求上限 ${Math.round(data.record_max_bytes/1024/1024)} MiB · ${data.ttl_seconds/60} 分钟过期 · 重启清空`;
    } catch(error){const hint=$('#logCaptureHint');if(hint)hint.textContent='请求详情缓存状态读取失败：'+String(error.message||error);}
  }
  async function setCapture(enabled) {
    const toggle=$('#logCaptureEnabled');if(toggle)toggle.disabled=true;
    try {
      const {ok,data}=await safeJson(await api('/api/logs/capture/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})}));
      if(!ok)throw new Error(data.detail||'设置失败');
    } catch(error){toast(error.message||'设置失败','err');}
    finally {await captureStatus();if(toggle)toggle.disabled=false;}
  }
  async function clearCache() {
    try {
      const {ok,data}=await safeJson(await api('/api/logs/capture/cache',{method:'DELETE'}));
      if(!ok)throw new Error(data.detail||'清空失败');
      closeDetail();await captureStatus();toast('详情内存缓存已清空');
    } catch(error){toast(error.message||'清空失败','err');}
  }
  async function download() {
    const input=$('#usageDiagnosticText');if(!input || $('#modal').classList.contains('hidden'))return;
    const id=Number(JSON.parse(input.value).id);
    try {
      const response=await api(`/api/logs/${id}/capture/download`);
      if(!response.ok)throw new Error('下载失败');
      await downloadText(`wb2api-usage-${id}.jsonl`,await response.text(),'application/x-ndjson');
    } catch(error){toast(error.message||'下载失败','err');}
  }
  return {ipCell,payloadHtml,download,captureStatus,setCapture,clearCache,closeDetail};
})();
