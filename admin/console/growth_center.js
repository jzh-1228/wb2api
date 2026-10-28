/* 成长中心：按需加载，快照秒回；所有动态字段先转义。 */
window.WBGrowthCenter = (() => {
  const e = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const n = value => Number.isFinite(Number(value)) ? Math.max(0, Number(value)) : 0;
  let accounts = [], selected = null, data = null, generation = 0, timer = null, pollDeadline = 0;
  let records = [], recordsMore = false, recordsBefore = null, modalVersion = 0, busy = false;
  const colors = ['#edf0f2','#c4f0c2','#78d38c','#30a657','#196636'];
  const section = key => data?.snapshot?.sections?.[key]?.data || {};
  const endpoint = suffix => '/api/growth-center/accounts/' + selected + suffix;
  const fmt = value => {
    if (!value) return '—';
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN',{hour12:false});
  };
  const scoreIndex = value => value > 20 ? 4 : value > 8 ? 3 : value > 0 ? 2 : 0;
  const safeImage = value => { try { const u = new URL(value); return u.protocol === 'https:' ? e(u.href) : ''; } catch { return ''; } };
  const petSVG = '<svg viewBox="0 0 100 100" fill="none" aria-hidden="true"><path d="M23 39 21 15l24 17M77 39l2-24-24 17" fill="#9adbb8" stroke="#429b75" stroke-width="3"/><ellipse cx="50" cy="57" rx="34" ry="31" fill="#c8ecd6"/><path d="M31 56h9m20 0h9" stroke="#2b6850" stroke-width="4" stroke-linecap="round"/><path d="m47 64 3 3 3-3m-10 9c4 3 10 3 14 0" stroke="#2b6850" stroke-width="2.5" stroke-linecap="round"/><circle cx="29" cy="66" r="5" fill="#f1a8aa" opacity=".6"/><circle cx="71" cy="66" r="5" fill="#f1a8aa" opacity=".6"/><path d="m10 61 12 2m-11 6 11-1m68-7-12 2m11 6-11-1" stroke="#7fbaa0" stroke-width="2" stroke-linecap="round"/></svg>';
  const pet = buddy => { const src = safeImage(buddy?.thumbnail_url || buddy?.base_static_url); return '<div class="gc-pet">'+(src ? `<img src="${src}" alt="${e(buddy?.name || 'Buddy')}" loading="lazy" onerror="this.hidden=true;this.nextElementSibling.hidden=false"/><span hidden>${petSVG}</span>` : petSVG)+'</div>'; };
  const button = (label, handler, primary=false, disabled=false) => `<button type="button" class="gc-button${primary?' primary':''}" onclick="WBGrowthCenter.${handler}" ${disabled?'disabled':''}>${label}</button>`;
  const link = (label, handler) => `<button type="button" class="gc-link" onclick="WBGrowthCenter.${handler}">${label}</button>`;
  const root = () => document.getElementById('growthCenter');
  async function request(url, options) {
    const response = await api(url, options);
    const payload = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '请求失败，请稍后重试');
    return payload;
  }
  function options(value='') {
    const query=value.trim().toLowerCase();
    return accounts.filter(a=>String(a.name||'').toLowerCase().includes(query)||String(a.id).includes(query))
      .map(a=>`<button type="button" role="option" aria-selected="${a.id===selected}" class="gc-account-option${a.id===selected?' selected':''}" onclick="WBGrowthCenter.select(${Number(a.id)})"><span>${e(a.name)}${a.status!=='active'?' · 已停用':''}</span><small>${a.id===selected?'✓':''}</small></button>`).join('') || '<div class="gc-account-empty">无匹配账号</div>';
  }
  function header() {
    const account=accounts.find(a=>a.id===selected);
    return `<div class="gc-bar"><div><h1>成长计划</h1><p class="gc-muted">每一天的使用，都让成长看得见。</p></div><div class="gc-picker"><details id="gcAccount" class="gc-account-picker"><summary aria-label="成长计划账号"><span>${e(account?.name||'请选择账号')}</span><span aria-hidden="true">⌄</span></summary><div class="gc-account-panel"><input type="search" aria-label="搜索成长计划账号" placeholder="搜索账号名称或编号" oninput="WBGrowthCenter.filter(this.value)"/><div id="gcAccountOptions" role="listbox" aria-label="账号">${options()}</div></div></details>${button('↻ 刷新状态','refresh()',false,busy)}</div></div>`;
  }
  function stat(label,value,unit,foot) {
    return `<div class="gc-stat"><div class="gc-stat-label">${label}</div><div class="gc-stat-value">${value}<small>${unit}</small></div><div class="gc-stat-foot">${foot}</div></div>`;
  }
  function monthCalendar() {
    const today=data?.today || new Date().toISOString().slice(0,10), ym=today.slice(0,7), year=Number(ym.slice(0,4)), month=Number(ym.slice(5));
    const cells=new Map((section('heatmap').cells||[]).map(c=>[c.date,c]));
    const makeup=new Set(section('streak').streak?.makeup_dates||[]);
    const days=new Date(Date.UTC(year,month,0)).getUTCDate();
    return `<div class="gc-day-grid">${Array.from({length:days},(_,i)=>{
      const date=ym+'-'+String(i+1).padStart(2,'0'), cell=cells.get(date), on=n(cell?.score)>0 || makeup.has(date), future=date>today;
      return `<span class="gc-day${on?' on':''}${date===today?' today':''}${future?' future':''}${makeup.has(date)?' madeup':''}" title="${e(date)} · ${future?'尚未到来':on?'已活跃':'未活跃'}">${i+1}</span>`;
    }).join('')}</div>`;
  }
  function render() {
    if (!root()) return;
    if (!accounts.length) { root().innerHTML=header()+'<div class="gc-card gc-empty">暂无账号。先到账号池通过手机号或授权页面添加账号。</div>'; return; }
    if (!data) { root().innerHTML=header()+'<div class="gc-skeleton"></div>'; return; }
    const s=section('streak'), h=section('heatmap'), energy=section('energy'), quota=section('quota'), buddy=section('buddy').buddy;
    const travel=section('travel'), lottery=section('lottery'), profile=section('profile'), checkin=section('checkin');
    const streak=s.streak||{}, redemption=s.redemption_status||{}, tiers=redemption.tiers||[], active=h.today?.is_active===true;
    const loaded=Object.keys(data.snapshot?.sections||{}).length>0, refreshing=data.refresh?.running;
    const errors=Object.values(data.snapshot?.errors||{});
    const state={idle:'等待出发',traveling:'旅行中',arrived:'礼物已到站'}[travel.state]||'尚未同步';
    const schedule=data.schedule;
    root().innerHTML=header()+
      `<div class="gc-hero"><div><small>WORKBUDDY GROWTH PLAN</small><h2>成长，是每天多一点</h2><p>记录你的活跃足迹，收集 Buddy，让每一份奖励都有迹可循。</p></div><div class="gc-hero-level">${e(profile.level_name||'成长等级')}<b>${profile.level===undefined?'—':'Lv.'+n(profile.level)}</b><span>${n(profile.completed)} / ${n(profile.total)} 项任务</span></div></div>`+
      (refreshing?'<div class="gc-notice">正在同步官方状态，已获取的数据会陆续显示。你可以继续查看已有记录。</div>':'')+
      (errors.length?`<div class="gc-notice warn">${errors.map(e).join('；')}。已保留上次成功数据。</div>`:'')+
      (data.refresh?.error?`<div class="gc-notice warn">${e(data.refresh.error)}</div>`:'')+
      (!loaded?'<div class="gc-notice">此账号尚未同步成长数据。点击“刷新状态”即可获取连续登录、能量和全年记录。</div>':'')+
      `<div class="gc-summary">${stat('连续登录',streak.days===undefined?'—':n(streak.days),'天',active?'● 今日已活跃':'今天完成有效对话后点亮')}${stat('能量值',energy.balance===undefined?'—':n(energy.balance),'',`开启盲盒需要 ${n(quota.cost_per_open)||10} 能量`)}${stat('本月活跃',streak.month_total_days===undefined?'—':n(streak.month_total_days),'天',link('查看全年活跃记录 ↗','yearMap()'))}${stat('可用抽奖次数',lottery.chances===undefined?'—':n(lottery.chances),'次',lottery.module?.enabled?'限时连登抽奖已开启':'活动状态以上游同步结果为准')}</div>`+
      `<div class="gc-top-grid"><div class="gc-card"><div class="gc-buddy">${pet(buddy)}<div class="gc-buddy-info"><h3>${e(buddy?.name||'我的 Buddy')}${buddy?.rarity?`<span class="gc-tag">${e(buddy.rarity)}</span>`:''}</h3><p>${e(buddy?.personality||'完成首次对话后，巡检会尝试领取你的第一只 Buddy。')}</p><div class="gc-actions">${button('开启盲盒','confirmAction(\'open\')',true,n(quota.affordable)<1||busy)}${link('宠物图鉴','gallery()')}</div></div></div><div class="gc-travel"><span><b>${e(travel.location?.name||'Buddy 旅行')}</b> · ${state}${travel.state==='traveling'?`<div class="gc-muted">预计到站 ${e(fmt(travel.arrive_at))}</div>`:''}</span>${travel.state==='arrived'?button('领取礼物','confirmAction(\'gift\')',true,busy):link('旅行记录','official(\'travel\')')}</div></div><div class="gc-banner-stack"><div class="gc-mini-banner"><div><h3>连续登录 ${streak.days===undefined?'—':n(streak.days)} 天</h3><p>${streak.next_tier?`再登录 ${n(streak.next_tier_remaining)} 天可解锁下一档奖励`:'活跃地图 · 每月多档奖励累计领取'}</p></div>${button('选择奖励','rewards()')}</div><div class="gc-mini-banner purple"><div><h3>限时连登抽奖</h3><p>${n(lottery.chances)} 次机会，看看好运为你准备了什么</p></div>${button('去抽奖','lottery()',false,!lottery.module?.enabled)}</div></div></div>`+
      `<div class="gc-card gc-calendar"><div class="gc-section-head"><h2>${Number(data.today.slice(5,7))} 月活跃地图</h2><div class="gc-actions">${link('全年活跃记录','yearMap()')}${link('领取记录','official(\'travel\')')}</div></div><div class="gc-streak"><span>当前连续登录 <strong>${streak.days===undefined?'—':n(streak.days)}</strong> 天</span><span class="gc-muted">登录且完成有效对话，才会记录活跃</span></div>${monthCalendar()}<div class="gc-calendar-foot"><div class="gc-milestones">${tiers.map(t=>`<div class="gc-milestone"><b>★ ${n(t.days)} 天</b><span class="gc-status-dot${redemption['tier_'+t.tier+'_status']==='available'?' good':''}"></span>${({available:'可领取',claimed:'本月已领取',locked:'未达标'})[redemption['tier_'+t.tier+'_status']]||'未达标'}</div>`).join('')}</div>${button('兑换奖励','rewards()',true)}</div></div>`+
      `<div class="gc-card"><div class="gc-section-head"><div><h2>每日打卡</h2><div class="gc-muted">${checkin.active===false?'当前签到活动已结束':`每日积分奖励 ${n(checkin.daily_credit)}，签到与活跃分别记录`}</div></div>${button(checkin.today_checked_in?'今日已领取':'打卡领积分','confirmAction(\'checkin\')',true,busy||checkin.today_checked_in||checkin.active!==true)}</div><div class="gc-muted">${checkin.end_time?'活动截止 '+e(checkin.end_time):'状态以官方活动接口为准'}</div></div>`+
      `<div class="gc-card gc-history"><div class="gc-section-head"><div><h2>执行与领取记录</h2><div class="gc-muted">此账号的自动巡检与手动操作，失败原因和奖励明细都保留。</div></div>${link('官方领取记录','official(\'travel\')')}</div><div id="gcHistory">${historyHTML(records)}</div><div id="gcHistoryMore">${recordsMore?button('加载更早记录','moreRecords()'):''}</div></div>`+
      `<div class="gc-plan-line"><span>${schedule?`${schedule.enabled?'● 自动巡检已开启':'○ 自动巡检已暂停'} · ${e(schedule.cycle_label || ('每 '+schedule.interval_minutes+' 分钟'))} · 下次 ${e(fmt(schedule.next_run_at))}`:'自动巡检尚未配置'} ${link('管理定时任务','schedule()')}</span><span>状态更新：${e(fmt(data.snapshot?.updated_at))}</span></div>`;
  }
  function historyHTML(items) {
    if (!items.length) return '<div class="gc-empty">暂无本地记录。此模块启用后的巡检与领奖会自动记录；以前的旅行、抽奖记录可在“官方领取记录”中查看。</div>';
    return items.map(r=>`<div class="gc-history-row"><time class="gc-muted">${e(fmt(r.created_at))}</time><div><div class="gc-history-title">${e(r.action)} <span class="gc-tag">${r.source==='manual'?'手动操作':'自动巡检'}</span></div><div class="gc-muted">${r.ok?'已完成':'部分步骤需复查'}${!r.steps?.length?' · '+e(r.message):''}</div>${r.steps?.length?`<details><summary>查看 ${r.steps.length} 项明细</summary><div class="gc-history-steps">${r.steps.map(s=>`<div class="gc-step"><span class="${s.ok?'gc-positive':'gc-bad'}">${s.ok?'✓':'!'} ${e(s.step)}</span><span>${e(s.message)}${s.reward?` · +${n(s.reward)} 积分`:''}</span></div>`).join('')}</div></details>`:''}</div><span class="${r.credit?'gc-positive':'gc-muted'}">${r.credit?'+'+n(r.credit)+' 积分':'—'}</span></div>`).join('');
  }
  async function enter() {
    const g=++generation; clearTimeout(timer);
    try {
      accounts=(await request('/api/growth-center/accounts')).items||[];
      if(g!==generation) return;
      if(!accounts.some(a=>a.id===selected)) selected=accounts[0]?.id||null;
      render(); if(selected) await select(selected);
    } catch(error) { root().innerHTML=`<div class="gc-card gc-empty">${e(error.message)}<br>${button('重试','enter()')}</div>`; }
  }
  async function select(id) {
    const g=++generation; selected=id; data=null; records=[]; recordsMore=false; clearTimeout(timer); close(); render();
    const aid=id;
    try {
      const [view,log]=await Promise.all([request(endpoint('')),request(endpoint('/records'))]);
      if(g!==generation||aid!==selected)return;
      data=view; records=log.items; recordsMore=log.has_more; recordsBefore=log.next_before; render();
      const age=Date.now()-Date.parse(data.snapshot?.checked_at||'');
      if(!data.refresh.running && (!data.snapshot?.checked_at || age>300000)) await refresh();
      else {pollDeadline=Date.now()+240000;poll(g);}
    } catch(error){ if(g===generation)root().innerHTML=header()+`<div class="gc-card gc-empty">${e(error.message)}</div>`; }
  }
  function filter(value) {
    document.getElementById('gcAccountOptions').innerHTML=options(value);
  }
  async function refresh() {
    if(!selected||busy)return;
    const g=generation, aid=selected; busy=true; render();
    try { await request(endpoint('/refresh'),{method:'POST'}); if(g!==generation||aid!==selected)return;
      if(data)data.refresh={running:true}; pollDeadline=Date.now()+240000; poll(g);
    } catch(error){toast(error.message,'err');} finally{busy=false;if(g===generation)render();}
  }
  function poll(g) {
    clearTimeout(timer); timer=setTimeout(async()=>{
      if(g!==generation || document.documentElement.dataset.activeTab!=='growth')return;
      try{ const [view, log]=await Promise.all([request(endpoint('')),request(endpoint('/records'))]); if(g!==generation)return;
        data=view;
        if(!recordsMore || records[0]?.id!==log.items[0]?.id){records=log.items;recordsMore=log.has_more;recordsBefore=log.next_before;}
        const focus=document.activeElement,expanded=[...document.querySelectorAll('#gcHistory details')].map(d=>d.open);
        if(focus?.tagName!=='INPUT'&&!document.getElementById('gcAccount')?.open){render();document.querySelectorAll('#gcHistory details').forEach((d,i)=>d.open=expanded[i]||false);}
        poll(g);
      }catch(error){ if(g===generation)toast(error.message,'err'); }
    },data?.refresh?.running && Date.now()<pollDeadline ? 1600 : 15000);
  }
  async function moreRecords() {
    const g=generation;
    try{const log=await request(endpoint('/records?before='+recordsBefore));if(g!==generation)return;records.push(...log.items);recordsMore=log.has_more;recordsBefore=log.next_before;render();}catch(error){toast(error.message,'err');}
  }
  function close(){ const dialog=document.getElementById('gcDialog');if(dialog){dialog.close();dialog.remove();}modalVersion++; }
  function modal(title,body,width=860,sub='',footer='') {
    close(); const dialog=document.createElement('dialog');dialog.id='gcDialog';dialog.className='gc-dialog';dialog.style.setProperty('--gc-dialog-width',width+'px');
    dialog.setAttribute('aria-label',title);dialog.innerHTML=`<div class="gc-dialog-head"><h2>${e(title)}${sub?`<small>${e(sub)}</small>`:''}</h2><button class="gc-dialog-close" aria-label="关闭弹窗" onclick="WBGrowthCenter.close()">×</button></div><div class="gc-dialog-body">${body}</div>${footer?`<div class="gc-dialog-footer">${footer}</div>`:''}`;
    document.body.append(dialog);dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)close();}});dialog.addEventListener('cancel',event=>{event.preventDefault();close();});dialog.showModal();
  }
  function yearMap() {
    const heat=section('heatmap'), range=heat.range||{}, today=data.today;
    const known=(heat.cells||[]).filter(c=>c.date<=today&&(!range.start_date||c.date>=range.start_date));
    const months=Array.from({length:12},(_,index)=>{
      const month=index+1;
      const days=known.filter(c=>Number(c.date.slice(5,7))===month).map(c=>({date:c.date,score:n(c.score)}));
      return {month,years:[...new Set(days.map(c=>c.date.slice(0,4)))].join(' / '),days};
    });
    const active=known.filter(c=>n(c.score)>0).length,total=known.length,rate=total?Math.round(active/total*100):0;
    const weeks=new Set(known.filter(c=>n(c.score)>0).map(c=>Math.floor(Date.parse(c.date+'T00:00:00Z')/604800000))).size;
    modal('全年活跃记录',`<div class="gc-year-stats"><div class="gc-inline"><span><b>${active}</b> 天活跃</span><span class="gc-positive"><b>${rate}%</b> 活跃率</span><span><b>${weeks}</b> 周有活跃</span></div><div class="gc-legend">未活跃 ${colors.map(c=>`<i class="gc-swatch" style="background:${c}"></i>`).join('')} 活跃</div></div><div class="gc-heat-scroll"><div class="gc-months">${months.map(m=>`<div><div class="gc-month-title" title="${e(m.years)} 年 ${m.month} 月">${m.month} 月</div><div class="gc-month-grid">${m.days.map(d=>`<button type="button" class="gc-heat-cell" data-score="${d.score}" style="background:${colors[scoreIndex(d.score)]}" title="${d.date} · ${d.score>0?'已活跃，活跃值 '+d.score:'未活跃'}" onfocus="WBGrowthCenter.heatTip(this.title)" onmouseover="WBGrowthCenter.heatTip(this.title)" onclick="WBGrowthCenter.heatTip(this.title)"></button>`).join('')}</div></div>`).join('')}</div></div><div class="gc-heat-tip" id="gcHeatTip">点击方格查看日期；跨年的同月记录按实际日期排列，窄屏可左右滑动。</div>`,900,`${active} 天活跃 · 活跃率 ${rate}%`,`${e(range.start_date||'—')} ～ ${e(range.end_date||today)} · 官方数据更新 ${e(fmt(heat.updated_at))}`);
  }
  function heatTip(text){document.getElementById('gcHeatTip').textContent=text;}
  function rewards() {
    const s=section('streak'),red=s.redemption_status||{},tiers=red.tiers||[];
    modal('选择奖励',tiers.length?tiers.map(t=>{
      const status=red['tier_'+t.tier+'_status'],available=status==='available';
      const rewards=[['credit','积分'],['energy','能量'],['cards','张补登卡'],['chances','次抽奖']].filter(([k])=>n(t[k])>0).map(([k,l])=>'+'+n(t[k])+' '+l).join(' · ');
      const tier=/^[a-zA-Z0-9_-]+$/.test(t.tier)?t.tier:'';
      return `<div class="gc-reward-row"><div class="gc-reward-left"><div class="gc-reward-icon">♧</div><div><h3>连续登录 ${n(t.days)} 天</h3><p>${rewards}</p><small>${status==='claimed'?'本月已领取':available?'已达标，可兑换':'连续登录天数不足'}</small></div></div>${button(status==='claimed'?'已兑换':'兑换',`confirmAction('redeem','${tier}')`,true,!available||!tier||busy)}</div>`;
    }).join(''):'<div class="gc-empty">尚无奖励档位数据，请先刷新状态。</div>',500,'',`每档每月限领一次，可多档累计。补登卡 ${n(s.makeup_cards?.balance)}/${n(s.makeup_cards?.max)||4} 张；达到上限后超出部分不入账。`);
  }
  function gallery() {
    const list=section('buddies').buddies||[];
    modal('我的 Buddy',list.length?`<div class="gc-gallery">${list.map(b=>`<div class="gc-gallery-item">${pet(b)}<b>${e(b.name)}</b><span class="gc-tag">${e(b.rarity)}</span><p>${b.current_buddy?'正在陪伴你 · ':''}${e(b.source==='gacha'?'盲盒获取':'成长奖励')}</p><p>${e(fmt(b.acquired_at))}</p></div>`).join('')}</div>`:'<div class="gc-empty">暂无宠物数据，请先刷新状态或完成首次成长任务。</div>',700,`${list.length} 只 Buddy`);
  }
  function lottery() {
    const chances=n(section('lottery').chances);
    modal('限时连登抽奖',`<div class="gc-lottery"><div class="gc-lottery-symbol">✧</div><h3>给成长一点惊喜</h3><p>当前可用 <b class="gc-positive">${chances}</b> 次免费抽奖机会<br>每次消耗 1 次，奖品以官方活动结果为准。</p>${button('抽一次','confirmAction(\'draw\')',true,chances<1||busy)}<div style="margin-top:18px">${link('查看抽奖记录','official(\'draws\')')}</div></div>`,480,'','实物中奖后，请前往官方成长中心填写领取信息。');
  }
  function confirmAction(action,tier='') {
    if(busy)return;
    const quota=section('quota'), s=section('streak'), cards=n(s.makeup_cards?.balance), max=n(s.makeup_cards?.max)||4;
    const texts={open:[`开启 Buddy 盲盒`,`将消耗 ${n(quota.cost_per_open)||10} 能量，随机获得一只 Buddy。当前能量 ${n(section('energy').balance)}。`],gift:['领取旅行礼物','领取当前已到站旅程的礼物，积分以上游实际发放为准。'],draw:['抽一次','消耗 1 次现有免费抽奖机会。'],checkin:['每日打卡','领取今日签到积分。'],redeem:['兑换连登奖励','领取已达标的奖励档位；本月已领取的档位不会重复发放。'+(cards>=max?'补登卡已达到上限，超出部分无法入账，其余奖励正常领取。':'')]};
    const [title,message]=texts[action];
    modal(title,`<p class="gc-muted" style="font-size:13px;margin:0 0 24px">${e(message)}</p><div class="gc-actions" style="justify-content:flex-end">${button('取消','close()')}${button('确认',`perform('${action}','${e(tier)}')`,true)}</div>`,440);
  }
  async function perform(action,tier) {
    if(busy)return;const aid=selected,g=generation;busy=true;
    modal('正在处理','<div class="gc-empty gc-spinning">正在核对账号状态并处理，请稍候…</div>',440);
    try {
      const result=await request(endpoint('/action'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,tier})});
      if(aid!==selected||g!==generation)return;
      modal(result.ok?'领取成功':'需要核对',`<div class="gc-lottery"><div class="gc-lottery-symbol">${result.ok?'✓':'!'}</div><h3>${result.credit?'+'+n(result.credit)+' 积分':result.ok?'操作已完成':'结果未确认'}</h3><p>${e(result.reason)}</p>${button('我知道了','close()',true)}</div>`,460);
      const log=await request(endpoint('/records')); if(g!==generation)return;records=log.items;recordsMore=log.has_more;recordsBefore=log.next_before;
    } catch(error){if(g===generation)modal('操作提示',`<div class="gc-empty">${e(error.message)}</div><div class="gc-actions" style="justify-content:flex-end">${button('关闭','close()')}</div>`,440);}
    finally{busy=false;if(g===generation){render();refresh();}}
  }
  async function official(kind='travel',page=1) {
    const aid=selected;modal('领取记录',`<div class="gc-empty">正在获取官方记录…</div>`,720);const version=modalVersion;
    try{
      const record=await request(endpoint('/official-records?kind='+kind+'&page='+page));if(aid!==selected||version!==modalVersion)return;
      const tabs=[['travel','旅行礼物'],['draws','抽奖记录'],['rewards','奖品领取']];
      const body=`<div class="gc-dialog-tabs">${tabs.map(([key,label])=>`<button class="${key===kind?'active':''}" onclick="WBGrowthCenter.official('${key}')">${label}</button>`).join('')}</div>`+
        (record.items.length?record.items.map(r=>{
          const title=kind==='travel'?(r.location?.name||'Buddy 旅行'):(r.prize_name||r.prize?.prize_name||r.prize?.name||'抽奖奖励');
          const credit=n(r.reward_credit||r.credit_granted||r.credit_amount||r.prize?.credit_amount);
          return `<div class="gc-history-row"><time class="gc-muted">${e(fmt(r.claimed_at||r.created_at||r.drawn_at||r.depart_at))}</time><div><div class="gc-history-title">${e(title)}</div><div class="gc-muted">${kind==='travel'?(r.claimed_at?'已领取':'尚未领取'):e(r.status||'已记录')}</div></div><span class="gc-positive">${credit?'+'+credit+' 积分':'—'}</span></div>`;
        }).join(''):'<div class="gc-empty">暂无此类官方记录。</div>')+
        `<div class="gc-page-nav">${button('上一页',`official('${kind}',${page-1})`,false,page<=1)}<span>第 ${page} 页 · 共 ${n(record.total)} 条</span>${button('下一页',`official('${kind}',${page+1})`,false,page*record.page_size>=record.total)}</div>`;
      modal('领取记录',body,720,'官方成长接口记录','实物奖品的领取信息请在官方成长中心管理。');
    }catch(error){if(version===modalVersion)modal('领取记录',`<div class="gc-empty">${e(error.message)}<br>${button('重试',`official('${kind}',${page})`)}</div>`,720);}
  }
  document.addEventListener('click',event=>{const picker=document.getElementById('gcAccount');if(picker&&!picker.contains(event.target))picker.open=false;});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){const picker=document.getElementById('gcAccount');if(picker)picker.open=false;}});
  return {enter,select,filter,refresh,moreRecords,close,yearMap,heatTip,rewards,gallery,lottery,confirmAction,perform,official,schedule:()=>activateTab('schedules')};
})();
