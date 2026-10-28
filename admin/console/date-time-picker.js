/* Theme-aware local date/time inputs. Values remain on the original input. */
window.WBDateTimePicker = (() => {
  const instances = new Map();
  let opened = null;
  const pad = value => String(value).padStart(2, '0');
  const dayValue = date => date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate());
  const sameDay = (a, b) => dayValue(a) === dayValue(b);

  function attach(id, endOfDay = false) {
    const input = document.getElementById(id);
    if (!input) return;
    if (instances.get(id)?.input === input) return;
    if (instances.has(id)) destroy(id);
    input.hidden = true;
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'journal-date-button';
    button.id = id + 'Button';
    button.setAttribute('aria-haspopup', 'dialog');
    button.setAttribute('aria-expanded', 'false');
    button.setAttribute('aria-controls', id + 'Picker');
    input.after(button);
    const panel = document.createElement('div');
    panel.id = id + 'Picker'; panel.className = 'date-time-picker'; panel.hidden = true;
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', '选择' + input.getAttribute('aria-label'));
    panel.innerHTML = '<div class="date-picker-title"></div>' +
      '<div class="date-picker-month"><button type="button" data-month="-1" aria-label="上个月">‹</button><strong aria-live="polite"></strong><button type="button" data-month="1" aria-label="下个月">›</button></div>' +
      '<div class="date-picker-week" aria-hidden="true"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>' +
      '<div class="date-picker-days"></div>' +
      '<div class="date-picker-time"><span>时间</span><input type="number" min="0" max="23" step="1" inputmode="numeric" aria-label="小时" /><span>:</span><input type="number" min="0" max="59" step="1" inputmode="numeric" aria-label="分钟" /><span class="date-picker-local">本地时间</span></div>' +
      '<p class="date-picker-error" role="alert" hidden>请输入有效时间：小时 0–23，分钟 0–59。</p>' +
      '<div class="date-picker-footer"><button type="button" class="console-btn quiet" data-action="clear">清除</button><button type="button" class="console-btn quiet" data-action="today">今天</button><button type="button" class="console-btn primary" data-action="apply">确定</button></div>';
    document.body.append(panel);
    const state = {input, button, panel, endOfDay, dateOnly:input.type === 'date', timeOnly:input.type === 'time', selected:null, month:null};
    if (state.timeOnly) {
      button.classList.add('time-only-button');
      panel.classList.add('time-only-picker');
      panel.querySelectorAll('.date-picker-month, .date-picker-week, .date-picker-days').forEach(el => el.hidden = true);
      panel.querySelector('[data-action="clear"]').hidden = true;
      const today = panel.querySelector('[data-action="today"]');
      today.textContent = '09:00'; today.setAttribute('aria-label', '设为 09:00');
      const time = panel.querySelector('.date-picker-time');
      time.innerHTML = ['小时', '分钟'].map((label, index) =>
        '<div class="time-picker-column"><span>' + label + '</span>' +
        '<button type="button" data-step="1" data-field="' + index + '" aria-label="增加' + label + '">＋</button>' +
        '<input type="number" min="0" max="' + (index ? 59 : 23) + '" step="1" inputmode="numeric" aria-label="' + label + '" />' +
        '<button type="button" data-step="-1" data-field="' + index + '" aria-label="减少' + label + '">−</button></div>'
      ).join('<span class="time-picker-colon" aria-hidden="true">:</span>');
      const note = document.createElement('p');
      note.className = 'time-picker-zone'; note.textContent = input.dataset.timezone || '本地时间';
      time.after(note);
    }
    panel.querySelector('.date-picker-time').hidden = state.dateOnly;
    instances.set(id, state);
    sync(id);
    input.addEventListener('change', () => sync(id));
    button.addEventListener('click', () => opened === state ? close() : open(state));
    panel.addEventListener('click', event => {
      const step = event.target.closest('[data-step]');
      if (step) {
        const field = panel.querySelectorAll('input')[Number(step.dataset.field)];
        const limit = Number(field.max) + 1;
        field.value = pad(((Number(field.value) || 0) + Number(step.dataset.step) + limit) % limit);
        panel.querySelector('.date-picker-error').hidden = true;
        return;
      }
      const month = event.target.closest('[data-month]');
      if (month) {
        state.month.setMonth(state.month.getMonth() + Number(month.dataset.month));
        render(state); return;
      }
      const day = event.target.closest('[data-day]');
      if (day) {
        state.selected = new Date(day.dataset.day + 'T00:00:00');
        state.month = new Date(state.selected.getFullYear(), state.selected.getMonth(), 1);
        render(state);
        panel.querySelector('[data-day="' + dayValue(state.selected) + '"]').focus();
        return;
      }
      const action = event.target.closest('[data-action]')?.dataset.action;
      if (action === 'clear') return commit(state, '');
      if (action === 'today') {
        if (state.timeOnly) {
          const fields = panel.querySelectorAll('input');
          fields[0].value = '09'; fields[1].value = '00';
          panel.querySelector('.date-picker-error').hidden = true;
          return;
        }
        state.selected = new Date();
        state.month = new Date(state.selected.getFullYear(), state.selected.getMonth(), 1);
        render(state);
      }
      if (action === 'apply') {
        if (state.dateOnly) return commit(state, dayValue(state.selected));
        const fields = [...panel.querySelectorAll('input')];
        if (fields.some(field => field.value === '' || !field.checkValidity())) {
          panel.querySelector('.date-picker-error').hidden = false;
          fields.find(field => field.value === '' || !field.checkValidity()).focus();
          return;
        }
        const time = pad(Number(fields[0].value)) + ':' + pad(Number(fields[1].value));
        commit(state, state.timeOnly ? time : dayValue(state.selected) + 'T' + time);
      }
    });
    panel.addEventListener('keydown', event => {
      const day = event.target.closest('[data-day]');
      if (!day) return;
      const focusedDate = new Date(day.dataset.day + 'T00:00:00');
      const shifts = {ArrowLeft:-1, ArrowRight:1, ArrowUp:-7, ArrowDown:7};
      let shift = shifts[event.key];
      if (event.key === 'Home') shift = -((focusedDate.getDay() + 6) % 7);
      if (event.key === 'End') shift = 6 - ((focusedDate.getDay() + 6) % 7);
      if (shift === undefined) return;
      event.preventDefault();
      state.selected = focusedDate;
      state.selected.setDate(state.selected.getDate() + shift);
      state.month = new Date(state.selected.getFullYear(), state.selected.getMonth(), 1);
      render(state);
      panel.querySelector('[data-day="' + dayValue(state.selected) + '"]').focus();
    });
    panel.addEventListener('input', () => { panel.querySelector('.date-picker-error').hidden = true; });
  }
  function sync(id) {
    const state = instances.get(id);
    if (!state) return;
    const value = state.input.value;
    state.button.classList.toggle('has-value', !!value);
    state.button.textContent = value ? value.replace('T', ' ').replace(/-/g, '/') : state.timeOnly ? '选择时间' : state.dateOnly ? '选择日期' : '选择日期与时间';
    state.button.setAttribute('aria-label', state.input.getAttribute('aria-label') + '：' + (value || '未选择'));
  }
  function render(state) {
    const {panel, month, selected} = state;
    panel.querySelector('.date-picker-month strong').textContent = month.getFullYear() + ' 年 ' + (month.getMonth() + 1) + ' 月';
    const grid = panel.querySelector('.date-picker-days');
    grid.replaceChildren();
    const date = new Date(month);
    date.setDate(1 - (date.getDay() + 6) % 7);
    const today = new Date();
    for (let index = 0; index < 42; index++, date.setDate(date.getDate() + 1)) {
      const button = document.createElement('button');
      button.type = 'button'; button.dataset.day = dayValue(date);
      button.textContent = date.getDate();
      button.classList.toggle('outside', date.getMonth() !== month.getMonth());
      button.classList.toggle('selected', sameDay(date, selected));
      button.setAttribute('aria-pressed', String(sameDay(date, selected)));
      button.setAttribute('aria-label', date.getFullYear() + '年' + (date.getMonth() + 1) + '月' + date.getDate() + '日');
      const selectedInView = selected.getFullYear() === month.getFullYear() && selected.getMonth() === month.getMonth();
      button.tabIndex = (sameDay(date, selected) || (!selectedInView && sameDay(date, month))) ? 0 : -1;
      if (sameDay(date, today)) button.setAttribute('aria-current', 'date');
      grid.append(button);
    }
  }
  function open(state) {
    close();
    WBFilterCombobox.close();
    document.querySelectorAll('.ui-select.open').forEach(el => el.classList.remove('open'));
    opened = state;
    const date = state.input.value && !state.timeOnly ? new Date(state.input.value + (state.dateOnly ? 'T00:00:00' : '')) : new Date();
    state.selected = date;
    state.month = new Date(date.getFullYear(), date.getMonth(), 1);
    const fields = state.panel.querySelectorAll('input');
    const time = state.timeOnly ? (state.input.value || '09:00').split(':') : null;
    fields[0].value = time ? time[0] : state.input.value ? pad(date.getHours()) : state.endOfDay ? '23' : '00';
    fields[1].value = time ? time[1] : state.input.value ? pad(date.getMinutes()) : state.endOfDay ? '59' : '00';
    state.panel.querySelector('.date-picker-error').hidden = true;
    state.panel.querySelector('.date-picker-title').textContent = '选择' + state.input.getAttribute('aria-label');
    state.panel.hidden = false;
    state.button.setAttribute('aria-expanded', 'true');
    if (!state.timeOnly) render(state);
    const rect = state.button.getBoundingClientRect(), panel = state.panel;
    const width = Math.min(320, innerWidth - 16);
    panel.style.width = width + 'px';
    panel.style.maxHeight = (innerHeight - 16) + 'px';
    const height = panel.getBoundingClientRect().height;
    const below = innerHeight - rect.bottom - 8;
    Object.assign(panel.style, {
      left: Math.max(8, Math.min(rect.left, innerWidth - width - 8)) + 'px',
      top: Math.max(8, Math.min(below >= height ? rect.bottom + 6 : rect.top - height - 6, innerHeight - height - 8)) + 'px'
    });
    (state.timeOnly ? fields[0] : panel.querySelector('.selected'))?.focus({preventScroll:true});
  }
  function commit(state, value) {
    state.input.value = value;
    state.input.dispatchEvent(new Event('change', {bubbles:true}));
    close(true);
  }
  function close(focus = false) {
    if (!opened) return;
    const state = opened;
    opened = null; state.panel.hidden = true;
    state.button.setAttribute('aria-expanded', 'false');
    if (focus) state.button.focus({preventScroll:true});
  }
  function destroy(id) {
    const state = instances.get(id);
    if (!state) return;
    if (opened === state) close();
    state.panel.remove(); state.button.remove(); state.input.hidden = false;
    instances.delete(id);
  }
  function destroyWithin(root) {
    for (const [id, state] of instances) if (root.contains(state.input)) destroy(id);
  }
  document.addEventListener('pointerdown', event => {
    if (opened && !opened.panel.contains(event.target) && !opened.button.contains(event.target)) close();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && opened) { event.preventDefault(); event.stopPropagation(); close(true); }
  }, true);
  document.addEventListener('focusin', event => {
    if (opened && !opened.panel.contains(event.target) && !opened.button.contains(event.target)) close();
  });
  document.addEventListener('scroll', event => {
    if (opened && !opened.panel.contains(event.target)) close();
  }, true);
  window.addEventListener('resize', () => close());
  return {attach, sync, close, destroyWithin};
})();

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('input[data-date-picker]').forEach(input => WBDateTimePicker.attach(input.id));
});
