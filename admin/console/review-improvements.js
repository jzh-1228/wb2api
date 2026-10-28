/* Authenticated maintenance and provenance-aware credit history. */
window.WBReview = (() => {
  let page = 1, generation = 0, updateGeneration = 0;
  let installMethod = 'docker';
  async function json(path, options) {
    const result = await safeJson(await api(path, options));
    if (!result.ok) throw new Error(typeof result.data.detail === 'string' ? result.data.detail : 'HTTP ' + result.status);
    return result.data;
  }
  function options(id, rows, label) {
    const select = document.getElementById(id), selected = select.value;
    select.innerHTML = '<option value="">' + label + '</option>' + rows.map(([value, text]) => '<option value="' + esc(String(value)) + '">' + esc(String(text)) + '</option>').join('');
    select.value = rows.some(([value]) => String(value) === selected) ? selected : '';
    WBFilterCombobox.attachSelect(id);
  }
  function sourceLabel(source) {
    return String(source).replace(/^(schedule|manual|task) · /, (_, prefix) => ({schedule:'定时',manual:'手动',task:'任务'}[prefix]) + ' · ');
  }
  function initJournal() {
    for (const id of ['journalAccount','journalSource','journalKind']) WBFilterCombobox.attachSelect(id);
    WBDateTimePicker.attach('journalStart');
    WBDateTimePicker.attach('journalEnd', true);
  }
  function resetJournal() {
    for (const id of ['journalAccount','journalSource','journalKind','journalStart','journalEnd']) {
      const input = document.getElementById(id);
      input.value = ''; input.dispatchEvent(new Event('change', {bubbles:true}));
    }
    loadJournal(1);
  }
  function journalRow(row) {
    const delta = Number(row.delta);
    const sign = delta > 0 ? 'positive' : delta < 0 ? 'negative' : 'neutral';
    const confirmed = row.kind === 'confirmed';
    return '<tr><td>' + esc(new Date(row.created_at).toLocaleString()) + '</td><td>' +
      esc(row.account_name || '#' + row.account_id) + '<small class="journal-account-id">#' + Number(row.account_id) +
      '</small></td><td><span class="journal-kind ' + (confirmed ? 'confirmed' : '') + '">' +
      (confirmed ? '确认奖励' : '余额观察') + '</span></td><td>' + esc(sourceLabel(row.source)) +
      '</td><td class="journal-number journal-delta ' + sign + '">' + (delta > 0 ? '+' : '') +
      delta.toLocaleString() + '</td><td class="journal-number' + (row.balance_after == null ? ' journal-unrecorded' : '') +
      '">' + (row.balance_after == null ? '未记录' : Number(row.balance_after).toLocaleString()) + '</td></tr>';
  }
  async function loadJournal(next = 1) {
    initJournal();
    WBFilterCombobox.close();
    WBDateTimePicker.close();
    const mine = ++generation;
    const query = new URLSearchParams({page: next, page_size: 50});
    for (const [id, name] of [['journalAccount','account_id'],['journalSource','source'],['journalKind','kind']]) {
      const value = document.getElementById(id).value; if (value) query.set(name, value);
    }
    const start = document.getElementById('journalStart').value, end = document.getElementById('journalEnd').value;
    if (start && end && start > end) {
      document.getElementById('journalHint').textContent = '开始时间不能晚于结束时间，请调整后重新筛选。';
      return;
    }
    if (start) query.set('start', new Date(start).toISOString());
    if (end) query.set('end', new Date(end).toISOString());
    document.getElementById('journalHint').textContent = '正在读取积分流水…';
    try {
      const data = await json('/api/maintenance/credit-events?' + query);
      if (mine !== generation) return;
      page = data.page;
      options('journalAccount', data.accounts.map(a => [a.id, (a.name || '账号') + ' · #' + a.id]), '全部账号');
      options('journalSource', data.sources.map(s => [s,sourceLabel(s)]), '全部来源');
      document.getElementById('journalBody').innerHTML = data.items.map(journalRow).join('') || '<tr><td colspan="6" class="journal-empty">没有符合条件的记录，试试调整筛选条件。</td></tr>';
      document.getElementById('journalHint').textContent = '共 ' + data.total + ' 条记录' + (data.importing ? ' · 历史记录导入中，请刷新继续' : '') + '。余额观察可能包含消费、奖励、到期或对账修正。';
      document.getElementById('journalPage').textContent = '第 ' + page + ' / ' + Math.max(1,Math.ceil(data.total / data.page_size)) + ' 页';
      document.getElementById('journalPrev').disabled = page <= 1;
      document.getElementById('journalNext').disabled = page * data.page_size >= data.total;
    } catch (error) { if (mine === generation) document.getElementById('journalHint').textContent = error.message; }
  }
  function highlightCommand(command) {
    // Tokenize raw text, then escape each token. Highlighting never inserts raw HTML.
    const pattern = /("(?:\\.|[^"\\])*"|'[^']*'|#[^\n]*|<[^>\n]+>|--?[\w][\w=-]*|\b(?:git|docker|python|mysqldump)\b|[>|])/g;
    let output = '', cursor = 0;
    for (const match of String(command).matchAll(pattern)) {
      const token = match[0];
      const kind = token[0] === '#' ? 'comment' : /^--?/.test(token) ? 'option' :
        /^["'<]/.test(token) ? 'string' : /^[>|]$/.test(token) ? 'operator' : 'command';
      output += esc(command.slice(cursor, match.index)) + '<span class="cmd-token-' + kind + '">' + esc(token) + '</span>';
      cursor = match.index + token.length;
    }
    return output + esc(command.slice(cursor));
  }
  function commandBlock(id, command) {
    return '<div class="maintenance-code"><div class="maintenance-code-head"><span>终端 · Bash</span>' +
      '<button type="button" class="console-btn quiet" onclick="copyText(\'#' + id + '\')" aria-label="复制命令">' +
      '<i class="fa-regular fa-copy" aria-hidden="true"></i>复制命令</button></div><pre><code id="' + id + '">' +
      highlightCommand(command) + '</code></pre></div>';
  }
  function selectInstall(method) {
    if (!['docker','linux'].includes(method)) return;
    installMethod = method;
    document.querySelectorAll('[data-install-method]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.installMethod === method)));
    document.querySelectorAll('[data-install-panel]').forEach(panel => { panel.hidden = panel.dataset.installPanel !== method; });
  }
  async function loadUpdate() {
    const mine = ++updateGeneration;
    const panel = document.getElementById('maintenanceInfo');
    panel.textContent = '正在检查正式版本…';
    try {
      const data = await json('/api/maintenance/update');
      if (mine !== updateGeneration) return;
      const status = data.error || !data.latest ? '版本检查未完成' : data.available ? '有新版本可升级' : '暂无新版本';
      const warning = !!data.error || !data.latest;
      panel.innerHTML = '<section class="card maintenance-version"><div class="maintenance-version-head"><div><span class="maintenance-eyebrow">软件版本</span><h2>保持更新，安心使用</h2></div><span class="maintenance-status ' +
        (warning ? 'warning' : data.available ? 'available' : '') + '"><i class="fa-solid ' +
        (warning ? 'fa-circle-exclamation' : data.available ? 'fa-arrow-up' : 'fa-check') + '" aria-hidden="true"></i>' + status +
        '</span></div><div class="maintenance-version-values"><div><span>当前版本</span><strong>' + esc(data.version) +
        '</strong></div><div><span>最新正式版</span><strong>' + esc(data.latest?.tag || '暂未获取') +
        '</strong></div><a class="console-btn" href="' + esc(data.releases_url) +
        '" target="_blank" rel="noopener noreferrer">版本与更新公告<i class="fa-solid fa-arrow-up-right-from-square" aria-hidden="true"></i></a></div>' +
        (data.error ? '<p class="maintenance-error">' + esc(data.error) + '</p>' : '') + '</section>' +
        '<section class="card maintenance-step"><div class="maintenance-step-head"><span class="maintenance-step-number">01</span><div><h2>备份数据</h2><p>升级前先保存一份数据库备份，便于恢复账号与配置。</p></div><span class="maintenance-db">' +
        esc(String(data.database || (data.backup_supported ? 'sqlite' : '数据库')).toUpperCase()) + '</span></div>' +
        (data.backup_supported ? '<div class="maintenance-backup-actions"><button class="console-btn primary" onclick="WBReview.backup()"><i class="fa-solid fa-download" aria-hidden="true"></i>下载 SQLite 备份</button>' +
          '<button class="console-btn" onclick="WBReview.pickRestore()"><i class="fa-solid fa-clock-rotate-left" aria-hidden="true"></i>恢复备份</button>' +
          '<input type="file" id="restoreFileInput" accept=".db,application/octet-stream" hidden onchange="WBReview.confirmRestore(this)" /></div>' :
          '<p class="maintenance-backup-help">当前数据库请使用管理工具完成备份' + (data.mysql_backup_command ? '，也可以在终端使用以下命令。' : '。') + '</p>') +
        (data.mysql_backup_command ? commandBlock('maintenance-command-backup', data.mysql_backup_command) : '') +
        '<p class="maintenance-private-note"><i class="fa-solid fa-lock" aria-hidden="true"></i>备份包含账号凭据和 API Key，请私密保存。</p></section>' +
        '<section class="card maintenance-step"><div class="maintenance-step-head"><span class="maintenance-step-number">02</span><div><h2>升级与重启</h2><p>选择当前的部署方式，在项目目录执行对应命令。</p></div></div>' +
        (data.commands ? ((data.commands_cn ? '<label class="maintenance-cn-toggle"><input type="checkbox" id="cnMirrorToggle" onchange="WBReview.toggleCnSource()" /><span>国内网络环境（pip 走清华镜像，附基础镜像加速提示）</span></label>' : '') +
          '<div class="maintenance-tabs" role="group" aria-label="部署方式">' +
          ['docker','linux'].filter(name => data.commands[name]).map(name => '<button type="button" data-install-method="' + name +
            '" aria-pressed="false" onclick="WBReview.selectInstall(\'' + name + '\')">' + (name === 'docker' ? 'Docker Compose' : 'Linux 源码') +
            '</button>').join('') + '</div>' +
          ['docker','linux'].filter(name => data.commands[name]).map(name => '<div data-install-panel="' + name + '">' +
            '<div data-cn-panel="default">' + commandBlock('maintenance-command-' + name, data.commands[name]) + '</div>' +
            (data.commands_cn && data.commands_cn[name] ? '<div data-cn-panel="cn" hidden>' + commandBlock('maintenance-command-cn-' + name, data.commands_cn[name]) + '</div>' : '') +
            '</div>').join('')) :
          '<p class="maintenance-backup-help">获取正式版本后显示对应的升级命令，请稍后重新检查。</p>') +
        '<p class="maintenance-deploy-note">先确认 <code>git status</code> 没有未处理的本地改动。NAS 可在容器管理器重新构建并重启，保留 <code>/data</code> 数据卷。</p></section>';
      selectInstall(data.commands?.[installMethod] ? installMethod : data.commands?.docker ? 'docker' : 'linux');
    } catch (error) { if (mine === updateGeneration) panel.textContent = error.message; }
  }
  async function backup() {
    try {
      const response = await api('/api/maintenance/backup', {method:'POST'});
      if (!response.ok) { const result = await safeJson(response); throw new Error(result.data.detail || 'HTTP ' + response.status); }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = 'wb2api-backup-' + new Date().toISOString().slice(0,10) + '.db';
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url),60000);
    } catch (error) { toast(error.message,'err'); }
  }
  function toggleCnSource() {
    const on = !!(document.getElementById('cnMirrorToggle')?.checked);
    document.querySelectorAll('[data-cn-panel]').forEach(panel => { panel.hidden = panel.dataset.cnPanel !== (on ? 'cn' : 'default'); });
  }
  function pickRestore() {
    const input = document.getElementById('restoreFileInput');
    if (!input) return toast('请先检查更新以加载备份面板', 'err');
    input.value = '';
    input.click();
  }
  function confirmRestore(input) {
    const file = input.files && input.files[0];
    if (!file) return;
    const size = file.size > 1048576 ? (file.size / 1048576).toFixed(1) + ' MB' : (file.size / 1024).toFixed(1) + ' KB';
    openModal('<h3><i class="fa-solid fa-clock-rotate-left mr-1"></i>确认恢复备份？</h3>' +
      '<p class="my-3">将用上传的备份<b>整体替换</b>当前数据库（账号、Key、日志、配置全部回到备份时刻）。</p>' +
      '<p class="text-sm" style="color:var(--fg-muted)">文件：<code>' + esc(file.name) + '</code> · ' + esc(size) + '<br/>' +
      '恢复前会自动在数据目录保存一份安全副本；完成后建议重启服务。</p>' +
      '<div class="flex gap-2 justify-end mt-4"><button class="console-btn" onclick="closeModal()">取消</button>' +
      '<button class="console-btn primary" id="restoreConfirmBtn" onclick="WBReview.doRestore()"><i class="fa-solid fa-clock-rotate-left"></i>开始恢复</button></div>', 'max-w-lg');
    window.__restoreFile = file;
  }
  async function doRestore() {
    const file = window.__restoreFile;
    const btn = document.getElementById('restoreConfirmBtn');
    if (!file) { closeModal(); return; }
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>恢复中…'; }
    try {
      const response = await api('/api/maintenance/restore', {method: 'POST', body: file, headers: {'Content-Type': 'application/octet-stream'}});
      const result = await safeJson(response);
      if (!result.ok) throw new Error(result.data.detail || 'HTTP ' + result.status);
      closeModal();
      const t = result.data.tables || {};
      toast('恢复完成：账号 ' + (t.accounts ?? '?') + ' · Key ' + (t.api_keys ?? '?') + ' · 安全副本 ' + (result.data.safety_copy || '已保存') + '。建议重启服务。', 'ok');
    } catch (error) {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-clock-rotate-left"></i>开始恢复'; }
      toast(error.message, 'err');
    } finally { window.__restoreFile = null; }
  }
  async function doRestartService() {
    const btn = document.getElementById('restartSvcBtn');
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>重启中…'; }
    try {
      const response = await api('/api/maintenance/restart', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({confirm: true})});
      const result = await safeJson(response);
      if (!result.ok) throw new Error(result.data.detail || 'HTTP ' + result.status);
      closeModal();
      toast('已触发重启：新实例将在几秒内就绪，页面将自动重连', 'ok');
      setTimeout(() => location.reload(), 8000);
    } catch (error) {
      if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fa-solid fa-power-off"></i>立即重启'; }
      toast(error.message, 'err');
    }
  }
  function accountLimit(id) {
    const account = accById(id);
    openModal('<h3>账号并发上限 · ' + esc(account?.name || String(id)) + '</h3><p class="my-3">留空跟随全局；0 表示不限；正整数限制同时请求数。只影响后续请求，维护中的账号仍会暂停接入。</p><input id="accountInFlight" type="number" min="0" step="1" max="2147483647" value="' + (account?.max_in_flight ?? '') + '" placeholder="留空跟随全局" aria-label="账号并发上限" /><button class="console-btn" onclick="WBReview.saveLimit(' + id + ')">保存</button>', 'max-w-lg');
  }
  async function saveLimit(id) {
    const raw = document.getElementById('accountInFlight').value;
    const value = raw === '' ? null : Number(raw);
    if (value !== null && (!Number.isInteger(value) || value < 0 || value > 2147483647)) return toast('请输入非负整数或留空','err');
    try { await json('/api/accounts/' + id, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_in_flight:value})}); closeModal(); loadAccounts(); } catch (error) { toast(error.message,'err'); }
  }
  return {loadJournal, resetJournal, journalPage:delta => loadJournal(Math.max(1,page+delta)), loadUpdate, selectInstall, toggleCnSource, backup, pickRestore, confirmRestore, doRestore, doRestartService, accountLimit, saveLimit};
})();
document.addEventListener('DOMContentLoaded', () => {
  if (document.documentElement.dataset.activeTab === 'journal') WBReview.loadJournal(1);
  if (document.documentElement.dataset.activeTab === 'maintenance') WBReview.loadUpdate();
});
