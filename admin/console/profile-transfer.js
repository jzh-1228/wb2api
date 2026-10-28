/* The same portable profile document is used by browsers and both app shells. */
window.WBProfileTransfer = (() => {
  let busy = false;
  async function status() {
    const el = document.getElementById('mobileProfileState');
    if (!el) return;
    try {
      const r = await api('/api/client-profile');
      if (!r.ok) throw new Error(`读取失败（HTTP ${r.status}）`);
      const d = await r.json();
      const p = d.effective || {};
      el.textContent = `${d.source === 'saved' ? '已启用保存的参数' : '优先自动探测'} · WorkBuddy ${p.desktop_version || '—'} · CLI ${p.cli_version || '—'}`;
    } catch (e) { el.textContent = e.message; }
  }
  async function exportFile() {
    try {
      const r = await api('/api/client-profile/export');
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || '导出失败');
      await downloadText(`workbuddy-client-profile-${exportStamp()}.json`, JSON.stringify(d, null, 2));
      toast('客户端参数已导出', 'ok');
    } catch (e) { toast(e.message || '文件未保存', 'err'); }
  }
  function chooseImport() {
    if (busy) return toast('正在导入，请稍候', 'err');
    const input = document.createElement('input');
    input.type = 'file'; input.accept = '.json,application/json'; input.hidden = true;
    document.body.append(input);
    input.addEventListener('cancel', () => input.remove(), {once:true});
    input.onchange = async () => {
      const file = input.files?.[0]; input.remove();
      if (!file) return;
      busy = true;
      try {
        if (file.size > 256 * 1024) throw new Error('参数文件不能超过 256 KB');
        const data = JSON.parse((await file.text()).replace(/^\uFEFF/, ''));
        if (data.format !== 'workbuddy-client-profile' || data.version !== 1 || !data.profile || Array.isArray(data.profile))
          throw new Error('请选择“导出参数”生成的 JSON 文件');
        const r = await api('/api/client-profile/import', {method:'POST',
          headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || '导入失败');
        await status();
        if (document.getElementById('panel-client') && typeof loadClientProfile === 'function') await loadClientProfile();
        toast('参数已导入并启用，新请求立即生效', 'ok');
      } catch (e) { toast(e instanceof SyntaxError ? '参数文件不是有效 JSON' : e.message || '导入失败', 'err'); }
      finally { busy = false; }
    };
    input.click();
  }
  return {exportFile, chooseImport, status};
})();
