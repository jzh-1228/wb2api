/* Official model promotion rules. No upstream HTML or SVG is executed. */
const WBModelMetadata = (() => {
  const credit = value => {
    const match=String(value??'').match(/(?:[x×]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*[x×])/);
    return match?Number(match[1]||match[2]):null;
  };
  function active(rule, now=new Date()) {
    const schedule=rule.schedule||{};
    if(schedule.validFrom && now.getTime()<Date.parse(schedule.validFrom)) return false;
    if(schedule.validUntil && now.getTime()>=Date.parse(schedule.validUntil)) return false;
    if(!schedule.daily?.length) return true;
    let parts;
    try { parts=new Intl.DateTimeFormat('en-GB',{timeZone:schedule.timezone||'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(now); }
    catch(_){return false;}
    const minute=Number(parts.find(p=>p.type==='hour').value)*60+Number(parts.find(p=>p.type==='minute').value);
    const minutes=v=>{const m=String(v).match(/^(\d{1,2}):(\d{2})$/);return m&&+m[1]<24&&+m[2]<60?+m[1]*60+(+m[2]):null;};
    return schedule.daily.some(window=>{
      const start=minutes(window.start),end=minutes(window.end);
      return start!=null&&end!=null&&(start===end||(start<end?minute>=start&&minute<end:minute>=start||minute<end));
    });
  }
  function display(model, now=new Date()) {
    const meta=model.metadata||model, rules=(meta.promotions||[]).filter(p=>p&&p.enabled!==false);
    const kind=model.model_type||(/^hunyuan-image-/i.test(model.model_id||model.id||'')||
      (Array.isArray(meta.tags)&&meta.tags.some(tag=>['text-to-image','image-to-image'].includes(tag)))?'image':'text');
    const enabled=rules.filter(p=>active(p,now));
    const rule=(enabled.length?enabled:rules).slice().sort((a,b)=>(Number(b.priority)||0)-(Number(a.priority)||0))[0];
    const isActive=!!rule&&enabled.includes(rule), badge=rule?.badge||{}, discount=rule?.discount||{};
    const base=kind==='image'?null:credit(meta.credits??model.credits_raw)??model.credit_multiplier??null;
    let price=base;
    if(isActive&&rule.kind==='discount'){
      const explicit=credit(discount.discountedCredits);
      if(explicit!=null) price=explicit;
      else if(base!=null&&discount.factor>0&&discount.factor<1) price=Math.round(base*discount.factor*10000)/10000;
    }
    let badges=[];
    if(rule&&(isActive||badge.display==='always')&&badge.label) badges.push(badge);
    // A promotion schedule takes precedence over legacy static tags, including
    // expired promotions. Otherwise stale tags could advertise an ended offer.
    if(!(meta.promotions||[]).length) for(const tag of meta.tags||[]){
      if(typeof tag!=='string') continue;
      const match=tag.match(/^badge:([^:]+)(?::(#[0-9a-f]{3,8}))?$/i);
      if(match&&!badges.some(b=>b.label===match[1])) badges.push({label:match[1],color:match[2]});
    }
    return {kind,name:meta.name||model.model_id||model.id,description:meta.descriptionZh||meta.description||meta.descriptionEn||model.description||'',
      badges,footer:isActive?(rule.hover?.textZh||rule.hover?.textEn||''):'',
      base,price,discountMode:isActive?discount.displayMode:'replace'};
  }
  return {active,display,credit};
})();
