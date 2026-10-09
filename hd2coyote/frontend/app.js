'use strict';
const $ = id => document.getElementById(id);
const text = (id, value) => { $(id).textContent = value == null ? '—' : String(value); };
const value = id => $(id).value;
const number = id => { const n = Number(value(id)); if (!value(id).trim() || !Number.isFinite(n) || !$(id).checkValidity()) throw Error('请检查数值范围'); return n; };
const RULE_NAMES = {damage:'受到伤害', limb_injury:'肢体损伤', death:'阵亡', low_health:'低血量'};
const VIEWS = {home:['主页概览','设备、游戏与输出状态，在这里一目了然。'], device:['设备连接','在本机准备连接，用 DG-LAB App 扫码配对。'], rules:['强度与规则','先设定输出边界，再调整每个事件的反馈。'], waves:['波形库','管理预设与自定义波形，所有测试仍受安全边界约束。'], bridge:['游戏内桥','循序验证桥接档位，游戏更新后重新核对偏移。'], sources:['事件源','将游戏与外部事件接入同一套规则和安全逻辑。'], diagnostics:['诊断与日志','本机运行记录，帮助定位连接与桥接问题。'], settings:['设置与关于','外观、版本与应用信息。']};
let closed = false, closing = false, online = false, timer, lastStatus, cfg, sourceData, waveData, qrKey = '', toastTimer;
const initialized = new Set();
function node(tag, cls, content) { const el = document.createElement(tag); if (cls) el.className = cls; if (content != null) el.textContent = content; return el; }
function toast(message, error = false) { text('toast',message); $('toast').className = 'toast' + (error ? ' error' : ''); $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 8000 : 4000); }
async function api(path, body) {
  const controller = new AbortController(); const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const options = {signal:controller.signal, cache:'no-store'};
    if (body !== undefined) Object.assign(options, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const response = await fetch(path,options); const data = await response.json();
    if (!response.ok || data.ok === false && data.error) throw Error(data.error || '请求失败 ' + response.status);
    return data;
  } catch (error) { if (error.name === 'AbortError') throw Error('请求超时，执行结果未知；请核对状态，不要重复触发输出。'); throw error; }
  finally { clearTimeout(timeout); }
}
function confirmAction(title, message) {
  if ($('confirmDialog').open) return Promise.resolve(false);
  text('dialogTitle',title); text('dialogText',message);
  return new Promise(resolve => {
    const dialog = $('confirmDialog');
    const finish = result => { dialog.close(); $('dialogConfirm').onclick = null; $('dialogCancel').onclick = null; dialog.oncancel = null; resolve(result); };
    $('dialogConfirm').onclick = () => finish(true); $('dialogCancel').onclick = () => finish(false);
    dialog.oncancel = e => { e.preventDefault(); finish(false); }; dialog.showModal(); $('dialogCancel').focus();
  });
}
function controls() {
  document.querySelectorAll('[data-mutate]').forEach(el => { el.disabled = !online || closing || closed || el.dataset.busy === 'yes'; });
  $('shutdownBtn').disabled = closing || closed;
  document.querySelector('[data-action=trip]').disabled = closed;
}
function markUnavailable(message, permanent = false) {
  online = false; document.body.classList.add('unavailable');
  text('connectionBanner',message); $('connectionBanner').hidden = false;
  text('serviceStatus',permanent ? '已关闭' : '连接中断'); text('serviceDetail',permanent ? '重新打开程序以使用' : '正在尝试重新连接');
  text('pollState',permanent ? '停止同步' : '状态未知'); text('sessionBadge',permanent ? '已关闭' : '连接中断');
  ['outA','outB','outPct','hp','sess'].forEach(id => text(id,'—'));
  ['meterA','meterB','hpbar'].forEach(id => { $(id).style.width = '0%'; });
  text('armedBadge','状态未知'); text('deviceStatus','状态未知'); text('bridgeStatus','状态未知'); text('dev','状态未知');
  text('state','无法确认硬件输出，请在手机 App 核对并停止输出。');
  ['serviceDot','bridgeDot','deviceDot'].forEach(id => { $(id).className = 'status-dot neutral'; });
  $('qrwrap').replaceChildren(node('span','', '连接信息不可用')); qrKey = ''; controls();
}
function markClosed(message) {
  closed = true; closing = false; clearTimeout(timer); markUnavailable(message,true);
  text('shutdownBtn','已关闭');
}
function setBadge(id, label, cls = '') { text(id,label); $(id).className = 'badge ' + cls; }
function width(id, current, limit = 100) { $(id).style.width = Math.max(0,Math.min(100,Number(current || 0) / Math.max(1,limit) * 100)) + '%'; }
function renderEvents(events) {
  text('events',events.join('\n') || '暂无事件');
  const list = $('recentEvents'); list.replaceChildren();
  if (!events.length) { list.append(node('div','empty-state','暂无事件。连接设备并开始检测后，事件会显示在这里。')); return; }
  events.slice(-3).reverse().forEach(line => { const row = node('div','event'); row.append(node('time','',line.slice(0,8)),node('span','',line.slice(8).trim())); list.append(row); });
}
function renderDiagnostics(b) {
  const log = b.log_tail || [], shared = b.shared_log_tail || [];
  const boot = log.filter(x => /mode=|profile=/.test(x)).slice(-1)[0];
  text('dlast',boot || '尚无运行记录'); text('dstatus',b.status || '尚无 STATUS'); text('dloader',b.loader_line || '未发现桥加载记录');
  text('dpaths',b.config_path ? '桥配置：' + b.config_path : '尚无桥配置');
  text('dlog',log.concat(shared).join('\n') || '暂无日志'); text('drecon',(b.recon_report || []).join('\n') || '暂无报告');
  text('dwarn','配置文件不等于实际运行档位。写入后重启游戏，再检查 STATUS 与运行日志；分享前遮挡私人路径。');
}
function renderUpdate(u) {
  text('upCur','v' + (u.current || '—')); text('upRepo',u.repo); text('upLatest',u.latest?.tag || '尚未检查');
  text('upInfo',u.ok === true ? (u.update_available ? '发现新版本，请自行查看发布页。' : '当前已是最新版本。') : u.error || '仅在点击时联网检查，不自动下载安装。');
  const link = $('upLink'); link.hidden = true; link.removeAttribute('href');
  try { const url = new URL(u.latest?.url || u.latest?.html_url); if (url.protocol === 'https:' && url.hostname === 'github.com') { link.href = url.href; link.hidden = false; } } catch (_) {}
}
function render(s) {
  lastStatus = s; const c = s.controller, d = c.device, h = c.hook;
  online = true; document.body.classList.remove('unavailable'); $('connectionBanner').hidden = true;
  text('ver','v' + c.version + ' · 本机服务'); text('footerVersion','v' + c.version);
  text('serviceStatus',c.running ? '检测中' : '待机'); text('serviceDetail','控制服务在线 · 仅本机界面'); $('serviceDot').className = 'status-dot';
  text('pollState','刚刚同步'); setBadge('sessionBadge',c.running ? '检测运行中' : '控制器待机',c.running ? 'good' : 'quiet');
  const receiving = c.running && (c.source === 'vision' ? c.detecting : (c.sources || []).some(source => source.alive));
  text('bridgeStatus',c.running ? (receiving ? '正在接收' : '等待数据') : '尚未检测');
  text('bridgeDetail',h.detail || '启动检测后等待游戏数据'); $('bridgeDot').className = 'status-dot' + (receiving ? '' : ' neutral');
  const mock = /mock/i.test(d.kind); text('deviceStatus',mock ? 'Mock 模拟' : d.connected ? '已连接' : '未连接');
  text('deviceDetail',mock ? '不连接真实硬件' : d.connected ? 'DG-LAB App 在线' : '在设备页连接手机');
  $('deviceDot').className = 'status-dot' + (d.connected ? '' : ' neutral'); setBadge('dev',mock ? 'Mock 模拟设备' : d.connected ? '已连接' : '未连接',d.connected ? 'good' : 'quiet');
  text('outA',c.output.a); text('outB',c.output.b); text('limitA',d.limit_a); text('limitB',d.limit_b);
  width('meterA',c.output.a,d.limit_a); width('meterB',c.output.b,d.limit_b); text('outPct',c.output.pct + '%'); text('sess',c.session_seconds + ' s');
  setBadge('armedBadge',c.armed ? '已武装' : '已静音',c.armed ? 'warning' : 'quiet');
  text('state',c.last_error || c.mute_reason || (c.running ? '检测运行中，输出受安全上限约束。' : '尚未开始检测；请先确认设备与安全边界。'));
  const hasData = receiving && Number.isFinite(c.hp);
  text('hp',hasData ? Math.round(c.hp) + '%' : '—'); width('hpbar',hasData ? c.hp : 0);
  text('limbs',hasData ? (c.limbs.map((x,i) => x ? ['左肢','躯干','右肢'][i] || '肢体' + i : '').filter(Boolean).join('、') || '未检测到损伤') : '等待有效数据');
  text('rampValue',c.ramp.enabled ? c.ramp.pct + '% / ' + c.ramp.ceiling_pct + '%' : '未启用');
  text('src',(c.sources || []).map(x => x.label + ' · ' + (x.alive ? '在线' : '等待')).join('；') || '尚未启动');
  text('sourceBadge',c.source === 'vision' ? '屏幕识别' : '事件源');
  const key = d.qr_url || '';
  if (key !== qrKey) { qrKey = key; $('qrwrap').replaceChildren(); if (key) { const img = node('img'); img.alt = 'DG-LAB App 连接二维码，请勿分享'; img.src = '/qr.svg?t=' + Date.now(); img.onerror = () => { img.remove(); $('qrwrap').prepend(node('span','','二维码不可用，请检查设备服务。')); }; $('qrwrap').append(img,node('code','',key)); } }
  if (!key) $('qrwrap').replaceChildren(node('span','',mock ? 'Mock 模式无需扫码，也不会连接真实设备。' : '启动设备服务后，在这里显示连接二维码。'));
  renderEvents(s.events || []); renderDiagnostics(s.bridge || {}); renderUpdate(s.update || {}); controls();
}
async function refresh() {
  if (closed || closing) return;
  try { const s = await api('/api/status'); if (!closed && !closing) render(s); }
  catch (_) { if (!closed && !closing) markUnavailable('与本机控制器失去连接，输出状态未知。请在手机 App 停止输出；界面会自动重连。'); }
}
async function poll() { await refresh(); if (!closed) timer = setTimeout(poll,1000); }
async function loadConfig() {
  cfg = await api('/api/config'); const rows = $('rules'); rows.replaceChildren();
  Object.entries(cfg.rules).forEach(([key,rule]) => {
    const row = node('div','rule-row'), label = node('label'), check = node('input'), range = node('input'), out = node('output');
    check.type = 'checkbox'; check.id = 'r_' + key + '_on'; check.checked = rule.enabled; check.setAttribute('role','switch'); label.append(check,node('span','',RULE_NAMES[key] || key));
    range.type = 'range'; range.id = 'r_' + key; range.min = 0; range.max = Math.max(100,rule.base_pct); range.step = 1; range.value = rule.base_pct; range.setAttribute('aria-label',(RULE_NAMES[key] || key) + '强度'); out.textContent = rule.base_pct + '%'; range.oninput = () => { out.textContent = range.value + '%'; }; row.append(label,range,out); rows.append(row);
  });
  const fields = {master:cfg.safety.master_multiplier,maxpct:cfg.safety.max_pct,maxabs:cfg.safety.max_absolute,deviceKind:cfg.device.kind,deviceHost:cfg.device.host,devicePort:cfg.device.port,deviceAdvertise:cfg.device.advertise_ip,rpPerEvent:cfg.ramp.per_event,rpHp:cfg.ramp.hp_missing_pct,rpCeil:cfg.ramp.ceiling_pct,rpDecayAfter:cfg.ramp.decay_after_s,rpDecayPer:cfg.ramp.decay_per_s};
  Object.entries(fields).forEach(([id,v]) => { $(id).value = v; }); $('rpOn').checked = cfg.ramp.enabled; $('rpResetOnDeath').checked = cfg.ramp.reset_on_death;
}
async function loadBridge() {
  const b = (await api('/api/status')).bridge || {}, p = b.parsed || {}, o = p.offsets || {};
  const fields = {bmode:p.mode || 'safe',bport:p.port ?? 47777,bint:p.interval ?? .1,bprof:p.profile || 'steam_25480438',bhp:o.hp ?? 32,bhpma:o.hp_max ?? 36,blimb:o.limb_mask ?? 40,bshift:o.limb_shift ?? 0,bdead:o.dead ?? 44};
  Object.entries(fields).forEach(([id,v]) => { $(id).value = v; }); text('binfo',b.exists ? '已读取：' + b.config_path : '尚无配置文件；内置默认档位为 safe。写入后重启游戏。');
}
async function loadSources() {
  const s = sourceData = await api('/api/sources'); $('srcList').replaceChildren();
  s.catalog.forEach(item => { const row = node('div','source-row'), label = node('label'), input = node('input'); input.type = 'checkbox'; input.id = 'src_' + item.name; input.checked = s.enabled.includes(item.name); label.append(input,node('b','',item.label),node('span','muted',item.name)); row.append(label,node('small','',item.hint)); $('srcList').append(row); });
  text('srcInfo','HTTP 上报：' + s.http.url + (s.http.token_required ? '（需要令牌）' : '（无令牌）') + ' · 限速 ' + s.http.max_per_s + '/s。更换事件源会停止检测，需手动重启。');
  text('srcUsage',Object.entries(s.http.examples).map(([k,v]) => k + '  ' + JSON.stringify(v)).join('\n'));
}
async function loadWaves() {
  const w = waveData = await api('/api/waves'), selected = value('wPreset'); $('wPreset').replaceChildren();
  w.presets.forEach(p => { const option = node('option','',p); option.value = p; $('wPreset').append(option); }); $('wPreset').value = w.presets.includes(selected) ? selected : w.presets.includes('pinch') ? 'pinch' : w.presets[0];
  $('waveList').replaceChildren();
  if (!w.waves.length) $('waveList').append(node('div','empty-state','波形库为空，规则仍可使用内置预设。'));
  w.waves.forEach(row => { const button = node('button','wave-item'); button.type = 'button'; button.append(node('b','',row.name),node('small','',row.kind === 'units' ? row.unit_count + ' 个单元' : '预设 · ' + row.preset)); button.onclick = () => { $('wName').value = row.name; $('wUnits').value = (row.units || []).join(' '); $('wFreq').value = row.freq ?? ''; $('wPeak').value = row.peak ?? 100; if (row.preset) $('wPreset').value = row.preset; document.querySelectorAll('.wave-item').forEach(el => el.classList.remove('selected')); button.classList.add('selected'); text('wInfo','已选中「' + row.name + '」，修改后请保存。测试只使用已保存的版本。'); }; $('waveList').append(button); });
  text('wInfo','规则当前使用：' + Object.entries(w.rules).map(([k,v]) => (RULE_NAMES[k] || k) + ' → ' + v).join('、'));
}
async function navigate(name) {
  if (!VIEWS[name]) return;
  document.querySelectorAll('.view').forEach(el => { el.hidden = el.id !== 'view-' + name; });
  document.querySelectorAll('.nav-item').forEach(el => { const active = el.dataset.nav === name; el.classList.toggle('selected',active); if (active) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); });
  text('breadcrumb',VIEWS[name][0]); text('pageTitle',VIEWS[name][0]); text('pageSubtitle',VIEWS[name][1]); window.scrollTo(0,0);
  if (!initialized.has(name)) { const loader = {device:loadConfig,rules:loadConfig,waves:loadWaves,bridge:loadBridge,sources:loadSources}[name]; if (loader) await loader(); initialized.add(name); }
}
async function saveConfig(body) { const res = await api('/api/config',body); toast('设置已保存'); await refresh(); return res; }
async function pulse(wave) {
  if (!lastStatus?.controller.running || !lastStatus.controller.armed) throw Error('请先开始检测并重新武装，然后再测试。');
  const pct = number('tpct'), ms = number('tms');
  if (!await confirmAction('确认测试输出','即将发送 ' + pct + '% / ' + ms + ' ms 测试脉冲。真实设备可能产生输出，请确认手机 App 上限与佩戴状态。')) return;
  const res = await api(wave ? '/api/waves' : '/api/actions',wave ? {action:'test',name:wave,pct,ms} : {action:'test_pulse',pct,ms}); toast(res.detail || res.applied);
}
async function shutdownApp() {
  if (closed || closing) return;
  if (!await confirmAction('关闭程序？','将停止检测、请求清除输出并断开设备，然后关闭控制服务。请在手机 App 核对输出已停止。')) return;
  closing = true; controls(); text('shutdownBtn','正在关闭…');
  try { const res = await api('/api/actions',{action:'shutdown'}); markClosed(res.detail); }
  catch (e) { closing = false; text('shutdownBtn','关闭程序'); markUnavailable('关闭结果未知：' + e.message + ' 请在手机 App 停止输出。'); }
}
const actions = {
  theme:() => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'), shutdown:shutdownApp,
  loadBridge,loadSources,loadWaves,
  testPulse:() => pulse(), testWave:() => { if (!value('wName').trim()) throw Error('请先选择已保存的波形'); return pulse(value('wName').trim()); },
  saveSources:async () => { if (!sourceData) throw Error('请先读取事件源'); const enabled = sourceData.catalog.filter(x => $('src_' + x.name).checked).map(x => x.name); if (!enabled.length) throw Error('至少启用一个事件源'); if (!await confirmAction('切换事件源？','这将停止检测并重建设备连接。保存后需手动重新武装和开始检测。')) return; await saveConfig({sources:{enabled}}); await loadSources(); },
  injectEvent:async () => { const kind = value('evKind'), n = number('evVal'); if (!await confirmAction('注入调试事件？','此操作会进入规则层，已武装时可能产生真实输出。')) return; const body = kind === 'state' ? {ev:kind,hp:n,hp_max:100,limbs:[0,0,0]} : kind === 'damage' ? {ev:kind,severity:n} : kind === 'low_health' ? {ev:kind,ratio:n/100} : kind === 'limb_injury' ? {ev:kind,slot:1,name:'左肢'} : {ev:kind}; toast((await api('/api/event',body)).detail); },
  removeWave:async () => { const name = value('wName').trim(); if (!name) throw Error('请先选择要删除的波形'); if (!await confirmAction('删除波形？','删除「' + name + '」后，引用它的规则将回退到内置预设。')) return; toast((await api('/api/waves',{action:'remove',name})).applied); await loadWaves(); },
  exportWaves:async () => { $('wJson').value = (await api('/api/waves')).text; toast('已导出到文本框，可复制保存'); },
  importWaves:async () => { const body = value('wJson').trim(); if (!body) throw Error('请先粘贴波形 JSON'); if (!await confirmAction('导入波形？','同名波形可能被替换，并立即影响引用它的规则。')) return; toast((await api('/api/waves',{action:'import',text:body})).applied); await loadWaves(); },
  checkUpdate:async () => { text('upInfo','正在检查 GitHub Releases…'); renderUpdate(await api('/api/update',{})); }
};
const forms = {
  rulesForm:async () => { if (!cfg) throw Error('配置尚未加载'); const rules = {}; Object.keys(cfg.rules).forEach(k => { rules[k] = {enabled:$('r_' + k + '_on').checked,base_pct:number('r_' + k)}; }); await saveConfig({rules,safety:{master_multiplier:number('master'),max_pct:number('maxpct'),max_absolute:number('maxabs')}}); },
  deviceForm:async () => { if (!await confirmAction('保存设备设置？','当前检测和连接将停止。保存后需手动重新武装、连接和开始检测。')) return; await saveConfig({device:{kind:value('deviceKind'),host:value('deviceHost').trim(),port:number('devicePort'),advertise_ip:value('deviceAdvertise').trim()}}); },
  rampForm:() => saveConfig({ramp:{enabled:$('rpOn').checked,per_event:number('rpPerEvent'),hp_missing_pct:number('rpHp'),ceiling_pct:number('rpCeil'),decay_after_s:number('rpDecayAfter'),decay_per_s:number('rpDecayPer'),reset_on_death:$('rpResetOnDeath').checked}}),
  waveForm:async () => { const body = {action:'set',name:value('wName').trim(),peak:number('wPeak'),freq:value('wFreq') ? number('wFreq') : null}, units = value('wUnits').split(/[\s,;]+/).filter(Boolean); if (units.length) body.units = units; else body.preset = value('wPreset'); toast((await api('/api/waves',body)).applied); await loadWaves(); },
  bridgeForm:async () => { if (!await confirmAction('写入游戏内桥配置？','写入后需要重启游戏。recon / live 档位依赖正确的游戏版本和字段偏移。')) return; const res = await api('/api/bridge',{mode:value('bmode'),port:number('bport'),interval:number('bint'),profile:value('bprof').trim(),offsets:{hp:number('bhp'),hp_max:number('bhpma'),limb_mask:number('blimb'),limb_shift:number('bshift'),dead:number('bdead')}}); toast(res.note); await loadBridge(); }
};
async function perform(button, fn) {
  if (button.dataset.busy === 'yes') return; button.dataset.busy = 'yes'; button.disabled = true;
  try { await fn(); } catch (e) { toast(e.message || String(e),true); }
  finally { delete button.dataset.busy; button.disabled = false; controls(); }
}
document.querySelectorAll('[data-nav]').forEach(button => button.addEventListener('click',() => { navigate(button.dataset.nav).catch(e => toast(e.message,true)); }));
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click',() => perform(button,async () => {
  const name = button.dataset.action;
  if (actions[name]) return actions[name]();
  if (name === 'arm' && !await confirmAction('重新武装？','武装后，检测到事件可能触发真实输出。请先确认设备上限和佩戴状态。')) return;
  const res = await api('/api/actions',{action:name}); toast(res.detail); await refresh();
})));
Object.entries(forms).forEach(([id,fn]) => $(id).addEventListener('submit',e => { e.preventDefault(); perform($(id).querySelector('[type=submit]'),fn); }));
const media = matchMedia('(prefers-color-scheme: dark)'); let theme = 'system';
function setTheme(mode) { theme = mode; document.documentElement.dataset.theme = mode === 'system' ? (media.matches ? 'dark' : 'light') : mode; $('themeSelect').value = mode; try { localStorage.setItem('hd2-theme',mode); } catch (_) {} }
try { const saved = localStorage.getItem('hd2-theme'); if (['light','dark','system'].includes(saved)) theme = saved; } catch (_) {}
setTheme(theme); media.addEventListener('change',() => { if (theme === 'system') setTheme(theme); }); $('themeSelect').addEventListener('change',() => setTheme(value('themeSelect')));
controls(); poll();
