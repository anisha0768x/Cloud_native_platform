'use strict';
const escapeText=value=>String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
fetch('/api/openapi.json').then(r=>r.json()).then(schema=>{
  const routes=Object.entries(schema.paths).flatMap(([path,methods])=>Object.entries(methods).map(([method,spec])=>({path,method,spec})));
  function render(){const filter=document.querySelector('#api-filter').value.toLowerCase();document.querySelector('#api-routes').innerHTML=routes.filter(r=>(r.path+' '+r.method+' '+r.spec.summary).toLowerCase().includes(filter)).map(r=>`<section class="card card-pad"><p><span class="tag">${escapeText(r.method.toUpperCase())}</span> <code>${escapeText(r.path)}</code></p><h3>${escapeText(r.spec.summary||'')}</h3><details><summary>Parameters, request body and responses</summary><pre class="code">${escapeText(JSON.stringify(r.spec,null,2))}</pre></details></section>`).join('')||'<p>No endpoints match.</p>';}
  document.querySelector('#api-filter').addEventListener('input',render);render();
}).catch(()=>document.querySelector('#api-routes').textContent='The API reference is unavailable. Check the server connection.');
