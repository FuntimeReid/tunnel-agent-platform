'use strict';
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const STORAGE = 'tunnel-ingest-demo-v1';
let saved = {}; try { saved = JSON.parse(localStorage.getItem(STORAGE) || '{}'); } catch (_) {}
const state = {step: 1, directions: [], directionId: saved.directionId || '', libraries: [], libraryId: saved.libraryId || '', records: saved.records || {}, files: [], busy: false, config: {ragflow_url: 'http://127.0.0.1:28080', poll_seconds: 5}};
let pollTimer = null, polling = false;
let documentsFor = '', documentPage = 1, documentsTotal = 0, documentsLoading = false;
let newLibraryActive = false;
let retrieving = false, retrievalFor = '';
const statusNames = {queued: '排队中', parsing: '解析中', done: '已完成', failed: '处理失败'};
const stageNames = {received: '等待处理', converting: '识别与转换', uploading: '上传到知识库', parsing: '解析与建立索引', done: '解析与索引完成', failed: '处理失败'};
const categoryNames = {paper: '论文', patent: '专利', standard: '标准', policy: '政策', manual: '手册', general: '通用资料'};
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function library() { return state.libraries.find(x => x.id === state.libraryId); }
function records() { return Object.values(state.records).filter(x => x.library_id === state.libraryId); }
function persist() { localStorage.setItem(STORAGE, JSON.stringify({directionId: state.directionId, libraryId: state.libraryId, records: state.records})); }
function notify(text, error = false) { $('#message').textContent = text; $('#message').className = 'message' + (error ? ' error' : ''); }
function clearMessage() { $('#message').className = 'message hidden'; }
async function request(path, options = {}) {
  const r = await fetch(path, options); let body;
  try { body = await r.json(); } catch (_) { throw new Error('服务器未返回可识别的结果，请稍后重试。'); }
  if (!r.ok || body.code !== 0) { const e = new Error(body.message || '请求失败'); e.body = body; e.status = r.status; throw e; }
  return body.data;
}
function renderSummary() {
  const lib = library(); const rows = records();
  $('#statLibrary').textContent = lib?.name || '尚未选择';
  $('#statTotal').textContent = lib?.document_count ?? '—';
  $('#statDone').textContent = lib?.done_count ?? '—';
  $('#statPending').textContent = rows.filter(x => !['done','failed'].includes(x.status)).length;
  $$('.library-chip').forEach(x => x.textContent = lib?.name || '尚未选择知识库');
  $('#toUpload').disabled = !lib || newLibraryActive; $('#uploadButton').disabled = !lib || !state.files.length || state.busy;
  $('#viewDocuments').disabled = !lib || documentsLoading;
  $('#createButton').disabled = !state.directionId;
  const done = rows.filter(x => x.status === 'done' && x.chunk_count > 0).length;
  const empty = rows.filter(x => x.status === 'done' && x.chunk_count === 0).length;
  $('#retrievalReady').textContent = lib ? `${lib.done_count ?? done} 份资料处理结束${empty ? '，其中 '+empty+' 份未提取到原文' : ''}` : '';
  $('#searchButton').disabled = !lib || retrieving;
  $('#retrievalMode').disabled = retrieving; $('#demoQuestion').disabled = retrieving;
  for (const el of $$('.stepper button')) { const n = Number(el.dataset.step); el.classList.toggle('current', n === state.step); el.classList.toggle('complete', (n === 1 && !!lib) || (n === 2 && rows.length > 0) || (n === 3 && done > 0 && rows.every(r => r.status === 'done'))); el.setAttribute('aria-current', n === state.step ? 'step' : 'false'); }
}
function go(step) {
  if (step > 1 && newLibraryActive) return notify('请先创建知识库。', true);
  if (step > 1 && !library()) return notify('请先选择或新建一个知识库。', true);
  state.step = step; clearMessage();
  $$('[data-panel]').forEach(x => x.classList.toggle('hidden', Number(x.dataset.panel) !== step));
  $$('.nav-item').forEach(x => x.classList.toggle('active', (Number(x.dataset.step) === 4) === (step === 4)));
  renderSummary(); if (step === 3) refreshRecords().catch(e => notify(e.message, true));
}
async function loadLibraries(show = false) {
  const [data, directions] = await Promise.all([request('/api/libraries'), request('/api/directions')]);
  state.libraries = data.libraries || []; state.directions = directions.directions || [];
  if (!library()) state.libraryId = '';
  if (!state.directionId && library()) state.directionId = library().direction_id;
  if (!state.directions.some(x => x.id === state.directionId)) state.directionId = '';
  $('#directionSelect').innerHTML = '<option value="">请选择方向</option>' + state.directions.map(x => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
  $('#directionSelect').value = state.directionId;
  renderLibraryOptions();
  renderLibrary(); persist(); if (show) notify('知识库列表已更新。');
}
function renderLibraryOptions() {
  const rows = state.libraries.filter(x => x.direction_id === state.directionId);
  if (!rows.some(x => x.id === state.libraryId)) state.libraryId = '';
  $('#librarySelect').innerHTML = '<option value="">'+(state.directionId ? '请选择知识库' : '请先选择方向')+'</option>' + rows.map(x => `<option value="${esc(x.id)}">${esc(x.name)} · ${x.document_count} 份文档</option>`).join('');
  $('#librarySelect').disabled = !state.directionId;
  $('#librarySelect').value = state.libraryId;
}
function renderLibrary() {
  if (documentsFor && documentsFor !== state.libraryId) {
    documentsFor = ''; $('#libraryDocuments').classList.add('hidden'); $('#viewDocuments').textContent = '查看文档';
  }
  if (retrievalFor && retrievalFor !== state.libraryId) { $('#retrievalResults').innerHTML = ''; $('#retrievalSummary').textContent = ''; retrievalFor = ''; }
  renderSummary(); renderRecords();
}
function libraryTab(isNew) {
  newLibraryActive = isNew;
  $('#existingLibraryPanel').classList.toggle('hidden', isNew);
  $('#newLibraryPanel').classList.toggle('hidden', !isNew);
  $('#existingLibraryTab').classList.toggle('active', !isNew);
  $('#newLibraryTab').classList.toggle('active', isNew);
  $('#existingLibraryTab').setAttribute('aria-selected', String(!isNew));
  $('#newLibraryTab').setAttribute('aria-selected', String(isNew));
  renderSummary();
}
function parameter(label, value) { return `<div><dt>${esc(label)}</dt><dd>${esc(value ?? '—')}</dd></div>`; }
function fileSize(size) { return size == null ? '—' : size >= 1048576 ? (size/1048576).toFixed(1)+' MiB' : (size/1024).toFixed(1)+' KiB'; }
async function loadDocuments(page = 1) {
  if (!library() || documentsLoading) return;
  const id = state.libraryId; documentsLoading = true; renderSummary();
  $('#libraryDocuments').classList.remove('hidden'); $('#viewDocuments').textContent = '收起文档';
  $('#documentList').textContent = '正在读取…';
  try {
    const [info, data] = await Promise.all([request(`/api/v1/datasets/${id}`), request(`/api/v1/datasets/${id}/documents?page=${page}&page_size=20`)]);
    if (state.libraryId !== id) return;
    documentsFor = id; documentPage = page; documentsTotal = data.total || 0;
    $('#libraryParameters').innerHTML = parameter('向量模型', info.embedding_model) + parameter('默认分块长度', info.parser_config?.chunk_token_num == null ? null : info.parser_config.chunk_token_num+' token') + parameter('原文块总数', info.chunk_count);
    const rows = (data.docs || []).map(doc => {
      const record = Object.values(state.records).find(x => x.doc_id === doc.id);
      const config = doc.parser_config || {};
      const status = doc.run === 'DONE' && !doc.chunk_count ? '未提取到原文' : ({DONE:'已完成', FAIL:'失败', CANCEL:'已取消', RUNNING:'解析中', UNSTART:'待处理'})[doc.run] || doc.run || '待处理';
      const parser = record?.route === 'ragflow_mineru' ? 'MinerU' : record?.route === 'mineru_md' ? 'MinerU → Markdown' : record?.route === 'direct' ? '文本' : config.layout_recognize;
      const values = (record?.filename && record.filename !== doc.name ? parameter('上传文件', record.filename) : '') + parameter('类别', categoryNames[record?.category] || record?.category) + parameter('解析方式', parser) + parameter('分块模板', doc.chunk_method) + parameter('分块长度设置', config.chunk_token_num == null ? null : config.chunk_token_num+' token');
      const detailId = 'doc-params-'+doc.id;
      return `<tr><td class="document-name">${esc(doc.name)}</td><td>${esc(status)}</td><td>${esc(doc.chunk_count ?? 0)}</td><td>${esc(fileSize(doc.size))}</td><td><button type="button" class="text-button" data-document-parameters="${esc(detailId)}" aria-controls="${esc(detailId)}" aria-expanded="false">参数</button></td></tr><tr id="${esc(detailId)}" class="document-parameter-row hidden"><td colspan="5"><dl class="parameter-grid">${values}</dl></td></tr>`;
    });
    $('#documentList').innerHTML = rows.length ? '<div class="document-table-wrap"><table class="document-table"><colgroup><col style="width:48%"><col style="width:15%"><col style="width:12%"><col style="width:15%"><col style="width:10%"></colgroup><thead><tr><th>文档名</th><th>状态</th><th>原文块</th><th>大小</th><th></th></tr></thead><tbody>'+rows.join('')+'</tbody></table></div>' : '<div class="empty-state">暂无文档</div>';
    $('#documentPageInfo').textContent = `第 ${page} 页 · 共 ${documentsTotal} 份`;
    $('#previousDocuments').disabled = page <= 1; $('#nextDocuments').disabled = page * 20 >= documentsTotal;
  } catch (e) { $('#documentList').textContent = '读取失败'; notify(e.message, true); }
  finally { documentsLoading = false; renderSummary(); }
}
function renderRetrieval(data) {
  const chunks = data.chunks || [];
  $('#retrievalSummary').textContent = `${chunks.length} 个原文块${typeof data.elapsed_seconds === 'number' ? ' · '+data.elapsed_seconds.toFixed(1)+' 秒' : ''}`;
  $('#retrievalResults').innerHTML = chunks.length ? chunks.map((chunk,i) => {
    const pages = [...new Set((chunk.positions || []).filter(x => Array.isArray(x)).map(x => x[0]))];
    return `<article class="retrieved-chunk"><div class="chunk-heading"><strong>${i+1}. ${esc(chunk.document_name || '未命名文档')}</strong>${pages.length ? '<span>第 '+pages.map(esc).join('、')+' 页</span>' : ''}</div><div class="chunk-text">${esc(chunk.text || '')}</div><div class="chunk-id">${esc(chunk.chunk_id)}</div></article>`;
  }).join('') : '<div class="empty-state">未找到相关原文</div>';
}
async function searchLibrary(event) {
  event.preventDefault();
  const text = $('#demoQuestion').value.trim();
  if (!library() || !text || retrieving) return;
  const id = state.libraryId, mode = $('#retrievalMode').value;
  retrieving = true; retrievalFor = id; clearMessage(); renderSummary();
  $('#searchButton').textContent = '检索中…'; $('#retrievalSummary').textContent = '检索中…'; $('#retrievalResults').innerHTML = '';
  try {
    const data = await request('/api/retrieve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({library_id:id,text,mode,top_n:12})});
    if (state.libraryId === id) renderRetrieval(data);
  } catch (error) { if(state.libraryId === id){$('#retrievalSummary').textContent='检索失败';notify(error.message,true);} }
  finally { retrieving = false; $('#searchButton').textContent = '检索'; renderSummary(); }
}
async function health() {
  $('#healthText').textContent = '正在检查连接';
  try { const data = await request('/api/health'); const ok = data.status === 'ok'; $('#healthText').textContent = ok ? '服务器连接正常' : '部分服务不可用'; $('#healthDot').className = ok ? 'ok' : 'bad'; }
  catch (e) { $('#healthText').textContent = '暂时无法连接服务'; $('#healthDot').className = 'bad'; notify(e.message, true); }
}
function selectFiles(incoming) {
  clearMessage(); const accepted = [], rejected = [];
  for (const f of incoming) {
    if (!/\.(pdf|docx|pptx|xlsx|md|markdown|txt)$/i.test(f.name)) rejected.push(`${f.name}：文件格式不支持`);
    else if (!f.size) rejected.push(`${f.name}：文件为空`);
    else if (f.size > 125829120) rejected.push(`${f.name}：超过 120 MiB`);
    else accepted.push(f);
  }
  const merged = [...state.files];
  for (const f of accepted) if (!merged.some(x => x.name === f.name && x.size === f.size && x.lastModified === f.lastModified)) merged.push(f);
  if (merged.length > (state.config.max_files || 10000)) { notify('所选文件过多。', true); return; }
  state.files = merged; renderFiles(); if (rejected.length) notify(rejected.join('；'), true);
}
function renderFiles() {
  $('#selectedFiles').innerHTML = state.files.map((f,i) => `<div class="file-row"><span class="file-icon">${esc(f.name.split('.').pop().toUpperCase().slice(0,4))}</span><span class="file-name">${esc(f.name)}</span><span class="file-size">${f.size > 1048576 ? (f.size/1048576).toFixed(1)+' MiB' : (f.size/1024).toFixed(1)+' KiB'}</span><button data-remove="${i}" aria-label="移除 ${esc(f.name)}">×</button></div>`).join('');
  renderSummary();
}
function makeUploadBatches(files, maxFiles = 10, maxBytes = 268435456) {
  const batches = []; let batch = [], bytes = 0;
  for (const file of files) {
    if (batch.length && (batch.length >= maxFiles || bytes + file.size > maxBytes)) { batches.push(batch); batch = []; bytes = 0; }
    batch.push(file); bytes += file.size;
  }
  if (batch.length) batches.push(batch);
  return batches;
}
function upload(form, completedBytes, batchBytes, totalBytes) {
  return new Promise((resolve,reject) => {
    const xhr = new XMLHttpRequest(); xhr.open('POST','/api/documents'); xhr.timeout = 920000;
    xhr.upload.onprogress = e => { if (e.lengthComputable) { const p = Math.min(100,Math.round((completedBytes+e.loaded/e.total*batchBytes)/totalBytes*100)); $('#uploadPercent').textContent = p + '%'; $('#uploadBar').style.width = p + '%'; } };
    xhr.onerror = () => reject(new Error('上传连接中断，请检查网络后重新上传。相同文件不会重复入库。'));
    xhr.ontimeout = () => reject(new Error('上传等待超时，请查询进度或重新上传，相同文件不会重复入库。'));
    xhr.onload = () => { try { const body = JSON.parse(xhr.responseText); if (xhr.status < 300 && body.code === 0) resolve(body.data); else { const e = new Error(body.message || '上传失败'); e.body = body; reject(e); } } catch(e) { reject(e); } };
    xhr.send(form);
  });
}
async function doUpload() {
  if (!library() || !state.files.length || state.busy) return;
  clearMessage(); state.busy = true; renderSummary(); $('#uploadProgress').classList.remove('hidden');
  const selected = [...state.files], targetLibrary = state.libraryId, category = $('#category').value;
  const totalBytes = selected.reduce((sum,f) => sum+f.size,0), batches = makeUploadBatches(selected, state.config.upload_batch_size || 10);
  let completedBytes = 0;
  try {
    const errors = [];
    for (const batch of batches) {
      const form = new FormData(); form.append('library_id',targetLibrary); form.append('category',category); batch.forEach(f => form.append('file',f));
      const batchBytes = batch.reduce((sum,f) => sum+f.size,0);
      const data = await upload(form,completedBytes,batchBytes,totalBytes);
      completedBytes += batchBytes;
      for (const item of data.items) {
        if (item.record_id) {
          const prev = state.records[item.record_id] || {};
          state.records[item.record_id] = {...prev,...item,library_id:targetLibrary,category:prev.category || category,progress:prev.progress || 0};
        } else errors.push(`${item.filename}：${item.error?.message || '上传失败'}`);
      }
      persist();
    }
    state.files = []; $('#files').value = ''; persist(); renderFiles(); await loadLibraries(); go(3);
    notify(errors.length ? errors.join('；') : '已上传。', !!errors.length);
    await refreshRecords(); schedulePoll();
  } catch(e) { notify(e.message, true); }
  finally { state.busy = false; $('#uploadProgress').classList.add('hidden'); renderSummary(); }
}
function renderRecords() {
  const rows = records(); const done = rows.filter(x => x.status === 'done' && x.chunk_count > 0).length, failed = rows.filter(x => x.status === 'failed').length, empty = rows.filter(x => x.status === 'done' && x.chunk_count === 0).length;
  $('#processingSummary').textContent = rows.length ? `共 ${rows.length} 份 · 已完成 ${done} 份${empty ? ' · 无原文 '+empty+' 份' : ''}${failed ? ' · 失败 '+failed+' 份' : ''}${rows.length-done-failed-empty ? ' · 处理中 '+(rows.length-done-failed-empty)+' 份' : ''}` : '当前知识库尚无上传记录';
  $('#recordList').innerHTML = rows.length ? rows.map(r => {
    const p = r.status === 'done' ? 100 : Math.max(0,Math.min(99,Math.round((r.progress || 0)*100)));
    const emptyResult = r.status === 'done' && r.chunk_count === 0;
    return `<article class="record ${esc(emptyResult ? 'empty-result' : r.status)}"><div class="record-head"><span class="file-icon">${esc((r.format || r.filename?.split('.').pop() || 'FILE').toUpperCase().slice(0,4))}</span><strong class="record-name">${esc(r.filename)}</strong><span class="status ${esc(emptyResult ? 'empty-result' : r.status)}">${esc(emptyResult ? '未提取到原文' : statusNames[r.status] || r.status)}</span></div><div class="progress-title"><span>${esc(stageNames[r.stage] || statusNames[r.status] || '等待处理')}</span><span>${p}%</span></div><div class="progress-track"><div style="width:${p}%"></div></div><div class="record-meta"><span>类别 <b>${esc(categoryNames[r.category] || r.category || '—')}</b></span><span>分块 <b>${esc(r.chunk_count ?? '—')} 块</b></span></div>${emptyResult ? '<div class="record-error">未提取到文字，请检查文件。</div>' : ''}${r.duplicate ? '<div class="duplicate">文件已存在，未重复入库。</div>' : ''}${r.error ? `<div class="record-error">${esc(r.error.message)} <button class="text-button" data-retry="${esc(r.record_id)}">重试</button></div>` : ''}${r.retry_note ? `<div class="record-error">正在重试：${esc(r.retry_note)}</div>` : ''}</article>`;
  }).join('') : '<div class="empty-state">暂无记录</div>';
  renderSummary();
}
async function refreshRecords() {
  if (state.config.public_demo && !polling) {
    const shared = await request('/api/shared-records');
    for (const r of shared.records || []) state.records[r.record_id] = r;
  }
  const ids = Object.keys(state.records); if (!ids.length || polling) { renderRecords(); return; }
  polling = true;
  try {
    for (let start=0; start<ids.length; start+=100) {
      const data = await request('/api/documents?ids=' + encodeURIComponent(ids.slice(start,start+100).join(',')));
      for (const r of data.records) {
        const prev = state.records[r.record_id];
        state.records[r.record_id] = {...prev,...r};
      }
      for (const id of data.missing || []) if (state.records[id]) state.records[id] = {...state.records[id],status:'failed',stage:'failed',error:{message:'服务器中已找不到这条记录，请确认知识库或记录是否被移除。'}};
    }
    persist(); renderRecords(); $('#lastUpdated').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN');
    await loadLibraries();
  } finally { polling = false; schedulePoll(); }
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (Object.values(state.records).some(x => !['done','failed'].includes(x.status))) pollTimer = setTimeout(() => refreshRecords().catch(e => { notify(e.message,true); }), state.config.poll_seconds*1000);
}
$$('[data-step]').forEach(el => el.addEventListener('click',() => go(Number(el.dataset.step))));
$('#librarySelect').addEventListener('change',e => { state.libraryId = e.target.value; persist(); clearMessage(); renderLibrary(); });
$('#directionSelect').addEventListener('change',e => { state.directionId = e.target.value; state.libraryId = ''; renderLibraryOptions(); renderLibrary(); persist(); clearMessage(); });
$('#addDirection').addEventListener('click',() => { $('#directionForm').classList.toggle('hidden'); if(!$('#directionForm').classList.contains('hidden'))$('#directionName').focus(); });
$('#cancelDirection').addEventListener('click',() => $('#directionForm').classList.add('hidden'));
$('#directionForm').addEventListener('submit',async e => {
  e.preventDefault(); const name=$('#directionName').value.trim(); if(!name)return;
  const button=$('#createDirection'); button.disabled=true;
  try { const direction=await request('/api/directions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})}); state.directionId=direction.id; state.libraryId=''; await loadLibraries(); $('#directionForm').classList.add('hidden'); $('#directionName').value=''; notify(direction.existed?'已选择已有方向。':'已创建方向。'); }
  catch(error){notify(error.message,true);}finally{button.disabled=false;}
});
$('#existingLibraryTab').addEventListener('click',() => libraryTab(false));
$('#newLibraryTab').addEventListener('click',() => libraryTab(true));
$('#viewDocuments').addEventListener('click',() => {if(!$('#libraryDocuments').classList.contains('hidden')){$('#libraryDocuments').classList.add('hidden');$('#viewDocuments').textContent='查看文档';}else{loadDocuments(1);}});
$('#refreshDocuments').addEventListener('click',() => loadDocuments(documentPage));
$('#previousDocuments').addEventListener('click',() => loadDocuments(documentPage-1));
$('#nextDocuments').addEventListener('click',() => loadDocuments(documentPage+1));
$('#documentList').addEventListener('click',event => {const button=event.target.closest('[data-document-parameters]');if(!button)return;const row=document.getElementById(button.dataset.documentParameters);const expand=button.getAttribute('aria-expanded')!=='true';row.classList.toggle('hidden',!expand);button.setAttribute('aria-expanded',String(expand));});
$('#reloadLibraries').addEventListener('click',() => loadLibraries(true).catch(e => notify(e.message,true)));
$('#refreshHealth').addEventListener('click',health);
$('#createForm').addEventListener('submit',async e => {
  e.preventDefault(); const name = $('#libraryName').value.trim(); if (!state.directionId) return notify('请先选择方向。',true); if (!name) return notify('请填写知识库名称。',true);
  const btn = $('#createButton'); btn.disabled = true; btn.textContent = '正在创建…';
  try { const lib = await request('/api/libraries',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,description:$('#libraryDescription').value.trim(),direction_id:state.directionId})}); state.libraryId = lib.id; await loadLibraries(); libraryTab(false); notify(lib.existed ? '已选择已有知识库。' : '已创建。'); }
  catch(err) { notify(err.message,true); } finally { btn.disabled = false; btn.textContent = '＋ 新建知识库'; }
});
$('#toUpload').addEventListener('click',() => go(2)); $('#toRetrieval').addEventListener('click',() => go(4));
$('#files').addEventListener('change',e => selectFiles(e.target.files));
$('#dropzone').addEventListener('dragover',e => {e.preventDefault();e.currentTarget.classList.add('dragging');});
$('#dropzone').addEventListener('dragleave',e => e.currentTarget.classList.remove('dragging'));
$('#dropzone').addEventListener('drop',e => {e.preventDefault();e.currentTarget.classList.remove('dragging');selectFiles(e.dataTransfer.files);});
$('#selectedFiles').addEventListener('click',e => {const el=e.target.closest('[data-remove]'); if(el){state.files.splice(Number(el.dataset.remove),1);renderFiles();}});
$('#uploadButton').addEventListener('click',doUpload);
$('#refreshRecords').addEventListener('click',() => refreshRecords().catch(e => notify(e.message,true)));
$('#recordList').addEventListener('click',async e => {const btn=e.target.closest('[data-retry]');if(!btn)return;btn.disabled=true;try{const r=await request(`/api/documents/${btn.dataset.retry}/retry`,{method:'POST'});state.records[r.record_id]={...state.records[r.record_id],...r};persist();await refreshRecords();}catch(err){notify(err.message,true);}finally{btn.disabled=false;}});
$('#retrievalForm').addEventListener('submit',searchLibrary);
$('#exportRecords').addEventListener('click',() => {const data={exported_at:new Date().toISOString(),library:library()||null,records:records(),note:'处理记录，不包含问答或检索质量评分。'};const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='入库处理记录.json';a.click();URL.revokeObjectURL(url);});
(async function init(){renderSummary();await health();try{state.config={...state.config,...await request('/api/config')};const demo=await request('/api/demo-state');if(demo){if(!Object.keys(state.records).length){state.libraryId=demo.library_id;for(const r of demo.records||[])state.records[r.record_id]=r;}const extra=(demo.extra_record_ids||[]).filter(id=>!state.records[id]);if(extra.length){const rows=await request('/api/documents?ids='+encodeURIComponent(extra.join(',')));for(const r of rows.records||[])state.records[r.record_id]=r;}persist();}await loadLibraries();if(library()&&records().length)go(3);else go(1);await refreshRecords();}catch(e){notify(e.message,true);}})();
