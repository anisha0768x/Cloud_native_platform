import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
let chromium;
try { ({chromium}=require('playwright')); }
catch { ({chromium}=require('../../.presentation-build/node_modules/playwright')); }
const envFile=process.env.HELIO_VERIFY_ENV || 'distributed/.env.verify';
const env=Object.fromEntries((await fs.readFile(envFile,'utf8')).trim().split(/\r?\n/).map(line=>{const i=line.indexOf('=');return [line.slice(0,i),line.slice(i+1)];}));
assert(env.COMPOSE_PROJECT_NAME.startsWith('helio-verify-'),'Use an isolated verification project');
const base='http://127.0.0.1:'+env.HELIO_PORT;
const output=path.resolve('test-results/browser-v3');
await fs.mkdir(output,{recursive:true});
const report={checks:[],errors:[],screenshots:[],base};
const browser=await chromium.launch({headless:true,...(process.env.PLAYWRIGHT_CHANNEL?{channel:process.env.PLAYWRIGHT_CHANNEL}:{})});
try {
  const page=await browser.newPage({viewport:{width:1440,height:1000},acceptDownloads:true});
  page.on('pageerror',error=>report.errors.push(error.message));
  page.on('response',response=>{if(response.url().startsWith(base)&&response.status()>=500)report.errors.push(response.status()+' '+response.url());});
  await page.goto(base);
  await page.locator('#login-form').waitFor({state:'visible'});
  await page.screenshot({path:path.join(output,'sign-in.png'),fullPage:true});
  await page.locator('#login-form [name=email]').fill('verify@example.test');
  await page.locator('#login-form [name=password]').fill('Verification-only-'+env.SETUP_TOKEN.slice(0,18));
  await page.locator('#login-form button[type=submit]').click();
  await page.locator('.metric-grid').waitFor();
  report.checks.push('Cookie sign-in and measured command center');
  async function view(name) {
    await page.locator(`nav [data-page="${name}"]`).click();
    await page.waitForFunction(()=>document.querySelector('#content').getAttribute('aria-busy')==='false');
    const text=await page.locator('#content').innerText();
    assert(!/\b(?:undefined|NaN)\b|Cannot read|Request failed|could not complete/i.test(text),name+': '+text.slice(0,250));
    assert.equal(await page.locator('#content > .notice.error').count(),0,name+' rendered an error');
  }
  for(const name of ['overview','services','incidents','capacity','logs','predict','notifications','storage','costs','security','network','recovery','audit','settings']) {
    await view(name);
    assert((await page.locator('#content h1').innerText()).length>3);
    report.checks.push('Rendered '+name);
    if(['overview','capacity','network','predict'].includes(name)) {
      await page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
      report.screenshots.push(name+'.png');
    }
  }
  await view('services');
  await page.getByRole('button',{name:'Register service',exact:true}).click();
  const name='browser-'+Date.now();
  await page.locator('#dialog [name=name]').fill(name);
  await page.locator('#dialog button[type=submit]').click();
  await page.getByRole('link',{name,exact:true}).click();
  await page.locator('[data-form=metric]').waitFor();
  await page.locator('[data-form=metric] [name=metric_name]').selectOption('latency_p95_ms');
  await page.locator('[data-form=metric] [name=value]').fill('120');
  await page.locator('[data-form=metric] button[type=submit]').click();
  await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('persisted'));
  report.checks.push('Service registration and persisted metric through browser forms');
  await view('capacity');
  await page.locator('[data-form=scale] [name=replicas]').fill('2');
  await page.locator('[data-form=scale] button[type=submit]').click();
  await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('replicas verified'));
  await page.locator('[data-form=load] [name=total]').fill('30');
  await page.locator('[data-form=load] [name=work_ms]').fill('20');
  const loadResponse=page.waitForResponse(r=>r.url()===base+'/api/workload/load'&&r.request().method()==='POST');
  await page.locator('[data-form=load] button[type=submit]').click();
  const load=await (await loadResponse).json();
  assert(load.id,'Workload request was not accepted');
  await page.waitForFunction(async id=>{const d=await (await fetch('/api/workload/runs')).json();return d.runs.some(r=>r.id===id&&r.status==='completed');},load.id,{timeout:45000});
  report.checks.push('Browser scaling and real workload execution');
  await view('storage');
  const bytes=Buffer.from('Browser S3 round-trip '+Date.now());
  const filename='browser-'+Date.now()+'.txt';
  await page.locator('[name=file]').setInputFiles({name:filename,mimeType:'text/plain',buffer:bytes});
  await page.locator('[data-form=upload] button[type=submit]').click();
  const row=page.getByRole('row').filter({hasText:filename});
  await row.waitFor();
  const downloadPromise=page.waitForEvent('download');
  await row.getByRole('link',{name:'Download',exact:true}).click();
  const download=await downloadPromise;
  assert.deepEqual(await fs.readFile(await download.path()),bytes);
  report.checks.push('Byte-exact S3 upload and download in browser');
  await view('recovery');
  const backupResponse=page.waitForResponse(r=>r.url()===base+'/api/backups'&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Create backup',exact:true}).click();
  const backupResult=await backupResponse;
  assert.equal(backupResult.status(),201,'Backup creation failed: '+await backupResult.text());
  await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('snapshot created'),null,{timeout:180000});
  await page.getByRole('button',{name:'Verify restore',exact:true}).first().click();
  await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('Restore verified'),null,{timeout:180000});
  report.checks.push('Browser backup and PostgreSQL restore validation');
  await page.setViewportSize({width:390,height:844});
  await page.locator('#mobile-menu').click();
  await page.locator('nav [data-page=overview]').click();
  await page.waitForFunction(()=>document.querySelector('#content').getAttribute('aria-busy')==='false');
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1),'Mobile horizontal overflow');
  await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
  report.checks.push('390-pixel mobile layout and navigation');
  assert(!/team\s*11|group\s*11|grp\s*11/i.test(await page.locator('body').innerText()));
  assert.deepEqual(report.errors,[]);
  report.checks.push('No stale team branding, JavaScript errors or server failures');
  report.status='passed';
} catch(error) {
  report.status='failed';report.failure=error.stack;
  throw error;
} finally {
  await browser.close();
  await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({status:report.status,checks:report.checks.length,output},null,2));
}
